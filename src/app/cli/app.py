"""命令行应用对象；各命令模块导入时向它注册命令。"""

import typer

cli = typer.Typer(
    help="AgentTemplate 初始化、诊断、开发与交付命令",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
