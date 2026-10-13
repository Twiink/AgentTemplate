"""版本化 Prompt 文件读取；支持 YAML front matter，参数必须与模板占位符完全一致。

front matter 可选字段：
- purpose: 用途说明
- variables: 变量列表及说明
- recommended_models: 推荐模型列表
- temperature: 建议温度
- max_tokens: 建议输出长度
"""

import hashlib
import re
from pathlib import Path
from string import Formatter
from typing import Any

import yaml

from app.core.errors import AppError, Code
from app.runtime.contracts import current_run


class PromptMetadata:
    """Prompt 元数据，从 front matter 解析。"""

    def __init__(self, data: dict[str, Any]):
        self.purpose = data.get("purpose", "")
        self.variables = data.get("variables", {})
        self.recommended_models = data.get("recommended_models", [])
        self.temperature = data.get("temperature")
        self.max_tokens = data.get("max_tokens")
        self.raw = data


class PromptLoader:
    """只读 Prompt 目录；版本以内容摘要标识，调用自动记录到当前运行。

    支持 YAML front matter（用 --- 包围）记录元数据。加载时验证参数与占位符匹配。
    """

    def __init__(self, directory: Path):
        self.directory = directory.resolve()

    def _parse(self, raw: str) -> tuple[str, PromptMetadata]:
        """分离 front matter 和正文；无 front matter 时元数据为空。"""
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", raw, re.DOTALL)
        if not match:
            return raw, PromptMetadata({})

        try:
            metadata = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as error:
            raise AppError(
                Code.PROMPT_PARAMETERS, f"Prompt front matter 解析失败: {error}"
            ) from error

        return match.group(2), PromptMetadata(metadata)

    def render(self, name: str, parameters: dict[str, str]) -> tuple[str, str, PromptMetadata]:
        """加载并校验占位符；路径越界、文件缺失和参数不匹配都显式报错。

        返回：(渲染后的内容, 版本哈希, 元数据)
        """
        path = (self.directory / name).resolve()
        if not path.is_relative_to(self.directory) or not path.is_file():
            raise AppError(Code.PROMPT_NOT_FOUND, "Prompt 文件不存在或路径越界", 404)

        raw = path.read_text()
        content, metadata = self._parse(raw)

        # 校验参数与占位符匹配
        fields = {field for _, field, _, _ in Formatter().parse(content) if field is not None}
        if any(not field.isidentifier() for field in fields):
            raise AppError(Code.PROMPT_PARAMETERS, "Prompt 占位符必须是简单标识符")

        # 如果元数据声明了变量，检查是否提供
        if metadata.variables:
            declared = set(metadata.variables.keys())
            if fields != declared:
                raise AppError(
                    Code.PROMPT_PARAMETERS,
                    f"参数不匹配：需要 {declared}，提供了 {set(parameters.keys())}",
                )
        elif fields != set(parameters.keys()):
            raise AppError(Code.PROMPT_PARAMETERS, "Prompt 参数必须与占位符完全一致")

        # 版本基于完整原始内容（包括 front matter）
        version = hashlib.sha256(raw.encode()).hexdigest()[:16]

        # 记录到当前运行上下文
        if context := current_run.get():
            context.prompt_versions[name] = version

        return content.format(**parameters), version, metadata
