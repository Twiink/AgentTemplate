"""版本化 Prompt 文件读取；不包含默认业务 Prompt，参数必须与模板占位符完全一致。"""

import hashlib
from pathlib import Path
from string import Formatter

from app.core.errors import AppError
from app.runtime.contracts import current_run


class PromptLoader:
    """只读 Prompt 目录；版本以内容摘要标识，调用自动记录到当前运行。"""

    def __init__(self, directory: Path):
        self.directory = directory.resolve()

    def render(self, name: str, parameters: dict[str, str]) -> tuple[str, str]:
        """加载并校验占位符；路径越界、文件缺失和参数不匹配都显式报错。"""
        path = (self.directory / name).resolve()
        if not path.is_relative_to(self.directory) or not path.is_file():
            raise AppError("prompt_not_found", "Prompt 文件不存在或路径越界", 404)
        content = path.read_text()
        fields = {field for _, field, _, _ in Formatter().parse(content) if field is not None}
        if any(not field.isidentifier() for field in fields) or fields != set(parameters):
            raise AppError("prompt_parameters", "Prompt 参数必须与简单占位符完全一致")
        version = hashlib.sha256(content.encode()).hexdigest()
        if context := current_run.get():
            context.prompt_versions[name] = version
        return content.format(**parameters), version
