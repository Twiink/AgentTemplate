"""生成 Agent、Prompt 和专用工具骨架；已存在的项目文件不覆盖。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated

import typer

from app.cli.app import cli
from app.core.errors import AppError, Code

scaffold_cli = typer.Typer(help="生成 Agent、Prompt 和工具骨架", no_args_is_help=True)
cli.add_typer(scaffold_cli, name="scaffold")
CURRENT_DIRECTORY = Path.cwd()


def validate_identifier(value: str, label: str) -> str:
    """校验 Python 模块及代码标识符可安全用于文件名和源码。"""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", value):
        raise typer.BadParameter(f"{label} 必须匹配 [a-z][a-z0-9_]{{0,39}}")
    return value


def create_files(files: dict[Path, str]) -> None:
    """以排他方式创建一组文件；失败时只移除本次已创建的文件。"""
    existing = [str(path) for path in files if path.exists()]
    if existing:
        raise AppError(Code.VALIDATION_ERROR, "文件已存在：" + ", ".join(existing), 409)

    created = []
    try:
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            created.append(path)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def agent_files(root: Path, name: str, model_alias: str) -> dict[Path, str]:
    """生成最小可编辑的 Agent 包和配套 Prompt。"""
    package = root / "src/app/agents" / name
    prompt = root / "prompts" / name / "system.md"
    source = f'''"""{name} Agent：业务入口。"""

from langchain.agents import create_agent

from app.agents.registry import AgentSpec, BuildContext


def build(ctx: BuildContext):
    """装配模型、工具、Prompt 和模板检查点。"""
    system_prompt, _, _ = ctx.prompts.render("{name}/system.md", {{}})
    return create_agent(
        model=ctx.models.chat({model_alias!r}),
        tools=ctx.tools.langchain([]),
        system_prompt=system_prompt,
        checkpointer=ctx.checkpointer,
        store=ctx.store,
    )


spec = AgentSpec(
    id={name!r},
    description="请填写该 Agent 的用途和边界。",
    build=build,
)
'''
    prompt_source = (
        f"---\npurpose: {name} Agent 的系统指令\nvariables: {{}}\n---\n\n"
        f"你是 {name} Agent。请根据实际业务补充职责、工具边界和输出要求。\n"
    )
    return {
        package / "__init__.py": f'"""{name} Agent 包。"""\n',
        package / "agent.py": source,
        prompt: prompt_source,
    }


def tool_file(root: Path, agent: str, name: str) -> Path:
    """返回 Agent 专用工具的源码路径。"""
    return root / "src/app/agents" / agent / "tools" / f"{name}.py"


def tool_source(agent: str, name: str) -> str:
    """生成通过 ToolRegistry 统一治理的只读自定义工具定义。"""
    return f'''"""{agent} Agent 的 {name} 工具。"""

from pydantic import BaseModel, Field

from app.tools.registry import ToolDefinition


class Input(BaseModel):
    """工具输入参数。"""
    text: str = Field(description="请填写参数用途")


def execute(text: str) -> str:
    """请实现只读工具行为；写操作需将 write 设为 True。"""
    return text


definition = ToolDefinition(
    name={name!r},
    description="请填写工具用途和边界。",
    schema=Input,
    function=execute,
    scope={f"tools:custom:{name}"!r},
)
'''


@scaffold_cli.command("agent")
def scaffold_agent(
    name: Annotated[str, typer.Argument(help="小写 Agent 标识符")],
    model_alias: Annotated[
        str, typer.Option("--model", help="Settings 中配置的模型别名")
    ] = "default",
    root: Annotated[Path, typer.Option("--root", help="项目根目录")] = CURRENT_DIRECTORY,
) -> None:
    """创建 Agent 包和配套系统 Prompt，不覆盖已有文件。"""
    validate_identifier(name, "Agent 名称")
    files = agent_files(root, name, model_alias)
    create_files(files)
    typer.echo(f"已生成 Agent {name}：src/app/agents/{name}/agent.py")


@scaffold_cli.command("prompt")
def scaffold_prompt(
    agent: Annotated[str, typer.Argument(help="Agent 标识符")],
    name: Annotated[str, typer.Argument(help="Prompt 文件名，不含扩展名")],
    root: Annotated[Path, typer.Option("--root", help="项目根目录")] = CURRENT_DIRECTORY,
) -> None:
    """创建 Agent 专用 Prompt，不覆盖已有文件。"""
    validate_identifier(agent, "Agent 名称")
    validate_identifier(name, "Prompt 名称")
    path = root / "prompts" / agent / f"{name}.md"
    content = (
        f"---\npurpose: {agent} 的 {name} Prompt\nvariables: {{}}\n---\n\n请填写 Prompt 正文。\n"
    )
    create_files({path: content})
    typer.echo(f"已生成 Prompt：{path}")


@scaffold_cli.command("tool")
def scaffold_tool(
    agent: Annotated[str, typer.Argument(help="Agent 标识符")],
    name: Annotated[str, typer.Argument(help="工具标识符")],
    root: Annotated[Path, typer.Option("--root", help="项目根目录")] = CURRENT_DIRECTORY,
) -> None:
    """创建 Agent 专用 ToolDefinition 骨架，不覆盖已有文件。"""
    validate_identifier(agent, "Agent 名称")
    validate_identifier(name, "工具名称")
    directory = root / "src/app/agents" / agent / "tools"
    files = {
        directory / "__init__.py": '"""Agent 专用工具。"""\n',
        tool_file(root, agent, name): tool_source(agent, name),
    }
    create_files(files)
    typer.echo(f"已生成工具：{tool_file(root, agent, name)}")
