"""检查自有文本的 Emoji 与 Python 中文说明；不会扫描依赖或私有文件。"""

import ast
import re
import sys
import tokenize
from pathlib import Path

# 扫描项目维护的文本；Unicode 图形区和表情修饰符均禁止出现在交付文本。
emoji = re.compile("[\U0001f000-\U0001faff\u2600-\u27bf\ufe0f\u200d]")
chinese = re.compile("[\u4e00-\u9fff]")
paths = [Path("AGENTS.md"), Path("README.md"), Path("Dockerfile"), Path("Makefile")]
for directory in ["src", "scripts", "tests", "config", "deploy", "docs", ".github"]:
    paths.extend(
        p
        for p in Path(directory).rglob("*")
        if p.is_file()
        and "__pycache__" not in str(p)
        and p.suffix in {".py", ".md", ".yaml", ".yml", ".sh", ".json", ".toml"}
        and p.name != "private.yaml"
    )
errors = []
for path in paths:
    if not path.exists():
        continue
    content = path.read_text()
    if emoji.search(content):
        errors.append(f"{path}：包含 Emoji 或表情格式字符")
    if path.suffix == ".py":
        tree = ast.parse(content)
        # 公共定义必须说明用途；中文详细程度仍需结合实现人工审查。
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if getattr(node, "name", "").startswith("_") and not isinstance(node, ast.Module):
                    continue
                doc = ast.get_docstring(node)
                if not doc or not chinese.search(doc):
                    errors.append(f"{path}:{getattr(node, 'lineno', 1)}：公共定义缺少中文说明")
        with path.open("rb") as handle:
            for token in tokenize.tokenize(handle.readline):
                if (
                    token.type == tokenize.COMMENT
                    and token.string != "#"
                    and not chinese.search(token.string)
                ):
                    if not token.string.startswith(("# type:", "# noqa", "#!/")):
                        errors.append(f"{path}:{token.start[0]}：说明性注释必须使用中文")
if errors:
    print("\n".join(errors))
    sys.exit(1)
print("中文说明与 Emoji 静态检查通过。")
