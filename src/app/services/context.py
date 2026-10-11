"""会话上下文裁剪与可选摘要；输入原记录保持不变，注入方式由项目决定。"""

from typing import Any


class ContextPolicy:
    """使用保守 UTF-8 字节预算，避免不了解服务商分词时把估算称为真实 Token。"""

    @staticmethod
    def trim(messages: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
        """保留最新完整消息及所有系统约束；系统信息超预算时直接拒绝。"""
        import json

        from app.core.errors import AppError

        system = [m for m in messages if m.get("role") == "system"]

        def size(m):
            """以序列化字节数提供保守预算估计。"""
            return len(json.dumps(m, ensure_ascii=False).encode()) + 16

        remaining = budget - sum(size(m) for m in system)
        if remaining < 0:
            raise AppError("context_budget", "系统约束超过上下文预算")
        selected = []
        for message in reversed([m for m in messages if m.get("role") != "system"]):
            if size(message) > remaining:
                break
            selected.append(message)
            remaining -= size(message)
        return system + list(reversed(selected))

    @staticmethod
    async def summarize(messages, model, instruction: str) -> str:
        """按项目提供的摘要指令调用真实模型；保留原记录，由调用者保存摘要版本。"""
        import json

        from langchain_core.messages import HumanMessage, SystemMessage

        result = await model.ainvoke(
            [
                SystemMessage(content=instruction),
                HumanMessage(content=json.dumps(messages, ensure_ascii=False)),
            ]
        )
        return str(result.content)
