"""LangGraph 运行适配器：执行启动时已构建的图，保存步骤边界并识别人工中断。

图在启动时由 AgentRegistry 构建一次，这里不再重复装配；运行期只负责身份注入、
事件转发与中断识别。LlamaIndex 适配在阶段 3.2 随框架收敛一并删除。
"""

from __future__ import annotations

from app.core.errors import AppError, Code


class WaitingInput(Exception):
    """已经保存快照的等待状态；执行方捕获后释放任务资源。"""

    def __init__(self, pending):
        self.pending = pending


class LangGraphAdapter:
    """执行原生 CompiledGraph 和 create_agent 返回对象，不构建默认业务图。"""

    async def execute(self, item, context, input, options, snapshot, resume):
        """保留原生输入及命名空间配置，服务端覆盖线程标识，输出保留最终原生状态。"""
        from langgraph.types import Command

        graph = item.graph
        checkpointer = await context.resources.checkpoint()
        if getattr(graph, "checkpointer", None) is not checkpointer:
            raise AppError(Code.CHECKPOINTER_REQUIRED, "项目构建必须使用模板提供的 Checkpointer")
        native = dict(options.get("langgraph", {}))
        configurable = dict(native.get("configurable", {}))
        for key in {"thread_id", "checkpoint_ns", "checkpoint_id"}:
            if key in configurable:
                raise AppError(Code.RESERVED_CONFIGURATION, "原生配置不能覆盖检查点身份")
        configurable["thread_id"] = context.checkpoint_thread
        config = {
            **native,
            "configurable": configurable,
            "recursion_limit": context.resources.settings.recursion_limit,
        }
        before = await graph.aget_state(config)
        checkpoint_id = (before.config or {}).get("configurable", {}).get("checkpoint_id")
        waiting = [i for task in before.tasks for i in task.interrupts]
        if resume is not None and snapshot and snapshot.get("static_interrupt"):
            payload = None
        elif resume is not None and waiting:
            value = resume.get("value", resume)
            payload = Command(resume=value)
        elif snapshot is not None and before.next:
            payload = None
        elif (
            snapshot is not None
            and before.values
            and checkpoint_id != snapshot.get("base_checkpoint_id")
        ):
            return before.values
        else:
            payload = input
        # 标识已有检查点执行；失效租约恢复时读取原生检查点而非再次注入原始输入。
        await context.resources.runs.snapshot(
            context,
            {
                "started": True,
                "base_checkpoint_id": snapshot.get("base_checkpoint_id")
                if snapshot
                else checkpoint_id,
            },
        )
        final = None
        async for kind, data in graph.astream(
            payload, config=config, stream_mode=["messages", "updates", "values", "custom"]
        ):
            if kind == "messages":
                message, metadata = data
                if (
                    getattr(message, "type", "") == "AIMessageChunk"
                    or message.__class__.__name__ == "AIMessageChunk"
                ):
                    content = message.content
                    if content:
                        await context.resources.runs.emit(
                            context, "text_delta", {"content": content}
                        )
                    if getattr(message, "tool_call_chunks", None):
                        await context.resources.runs.emit(
                            context, "tool_delta", {"calls": message.tool_call_chunks}
                        )
            elif kind == "values":
                final = data
            elif kind == "custom":
                await context.resources.runs.emit(context, "custom", {"data": data})
            elif kind == "updates":
                for node, update in data.items():
                    if node == "__interrupt__":
                        continue
                    # 状态更新只发节点名；避免把内部完整正文放入观测事件。
                    await context.resources.runs.emit(context, "node", {"node": node})
                    if isinstance(update, dict):
                        for message in update.get("messages", []):
                            if getattr(message, "type", None) == "tool":
                                await context.resources.runs.emit(
                                    context,
                                    "tool_result",
                                    {
                                        "name": getattr(message, "name", None),
                                        "call_id": message.tool_call_id,
                                        "status": getattr(message, "status", "success"),
                                    },
                                )
        state = await graph.aget_state(config)
        interrupts = [interrupt for task in state.tasks for interrupt in task.interrupts]
        if interrupts:
            raise WaitingInput({"interrupts": [{"id": i.id, "value": i.value} for i in interrupts]})
        if state.next:
            await context.resources.runs.snapshot(
                context,
                {
                    "started": True,
                    "static_interrupt": True,
                    "base_checkpoint_id": checkpoint_id,
                },
            )
            raise WaitingInput({"next": list(state.next)})
        return state.values if final is None else final
