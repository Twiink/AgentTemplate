"""三框架原生运行适配器；LangChain 复用 LangGraph，LlamaIndex 保存步骤边界 JSON Context。"""

from __future__ import annotations

import inspect

from app.core.errors import AppError


class WaitingInput(Exception):
    """已经保存快照的等待状态；Worker 捕获后释放任务资源。"""

    def __init__(self, pending):
        self.pending = pending


class LangGraphAdapter:
    """适配原生 CompiledGraph 和 create_agent 返回对象，不构建默认业务图。"""

    async def execute(self, registration, context, input, options, snapshot, resume):
        """保留原生输入及命名空间配置，服务端覆盖线程标识，输出保留最终原生状态。"""
        from langgraph.types import Command

        checkpointer = await context.resources.checkpoint()
        graph = registration.factory(context, checkpointer)
        if inspect.isawaitable(graph):
            graph = await graph
        if getattr(graph, "checkpointer", None) is not checkpointer:
            raise AppError("checkpointer_required", "项目工厂必须使用模板提供的 Checkpointer")
        native = dict(options.get(registration.framework, {}))
        configurable = dict(native.get("configurable", {}))
        for key in {"thread_id", "checkpoint_ns", "checkpoint_id"}:
            if key in configurable:
                raise AppError("reserved_configuration", "原生配置不能覆盖检查点身份")
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
                "framework": registration.framework,
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
            raise WaitingInput(
                {
                    "framework": registration.framework,
                    "interrupts": [{"id": i.id, "value": i.value} for i in interrupts],
                }
            )
        if state.next:
            await context.resources.runs.snapshot(
                context,
                {
                    "framework": registration.framework,
                    "started": True,
                    "static_interrupt": True,
                    "base_checkpoint_id": checkpoint_id,
                },
            )
            raise WaitingInput({"framework": registration.framework, "next": list(state.next)})
        return state.values if final is None else final


class LlamaIndexAdapter:
    """遵循官方步骤状态事件保存 Context；序列化 JSON，禁止 pickle 运行期资源。"""

    async def execute(self, registration, context, input, options, snapshot, resume):
        """恢复 Context 后发送原生 HumanResponseEvent，等待输入时保存并取消 handler。"""
        from workflows import Context
        from workflows.events import (
            HumanResponseEvent,
            InputRequiredEvent,
            StepState,
            StepStateChanged,
        )

        if snapshot and snapshot.get("finished"):
            return snapshot["result"]
        workflow = registration.factory(context, None)
        if inspect.isawaitable(workflow):
            workflow = await workflow
        native = dict(options.get("llamaindex", {}))
        if any(key in native for key in {"ctx", "start_event"}):
            raise AppError("reserved_configuration", "原生配置不能替换持久上下文")
        ctx = (
            Context.from_dict(workflow, snapshot["context"])
            if snapshot and "context" in snapshot
            else Context(workflow)
        )
        context.llama_context = ctx
        handler = workflow.run(ctx=ctx, **({} if snapshot else input), **native)
        try:
            if resume:
                handler.ctx.send_event(HumanResponseEvent(**resume.get("value", resume)))
            async for event in handler.stream_events(expose_internal=True):
                if isinstance(event, StepStateChanged):
                    if event.step_state == StepState.NOT_RUNNING:
                        await context.resources.runs.snapshot(
                            context, {"framework": "llamaindex", "context": ctx.to_dict()}
                        )
                    await context.resources.runs.emit(
                        context, "node", {"node": event.name, "state": event.step_state.value}
                    )
                elif isinstance(event, InputRequiredEvent):
                    await context.resources.runs.snapshot(
                        context, {"framework": "llamaindex", "context": ctx.to_dict()}
                    )
                    pending = {"framework": "llamaindex", "event": event.model_dump(mode="json")}
                    await handler.cancel_run()
                    raise WaitingInput(pending)
                else:
                    data = event.model_dump(mode="json")
                    if "delta" in data:
                        await context.resources.runs.emit(
                            context, "text_delta", {"content": data["delta"]}
                        )
                    elif event.__class__.__name__ in {"ToolCall", "ToolCallResult"}:
                        await context.resources.runs.emit(
                            context,
                            "tool",
                            {"type": event.__class__.__name__, "tool_name": data.get("tool_name")},
                        )
                    else:
                        await context.resources.runs.emit(
                            context, "custom", {"type": event.__class__.__name__, "data": data}
                        )
            result = await handler
            await context.resources.runs.snapshot(
                context,
                {
                    "framework": "llamaindex",
                    "context": ctx.to_dict(),
                    "finished": True,
                    "result": result.model_dump(mode="json")
                    if hasattr(result, "model_dump")
                    else result,
                },
            )
            return result
        finally:
            await handler.cancel_run()
