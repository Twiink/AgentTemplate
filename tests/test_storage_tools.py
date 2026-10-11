"""验证本地真实文件、文档解析与限制策略，不使用外部服务替身。"""

from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.errors import AppError
from app.services.knowledge import KnowledgeService
from app.services.storage import LocalStore
from app.services.tools import ToolRegistry


@pytest.mark.asyncio
async def test_local_storage(tmp_path):
    """实际保存、读取、删除附件，并拒绝路径对象键。"""
    store = LocalStore(tmp_path)
    await store.put("object-1", "内容".encode(), "text/plain")
    assert await store.get("object-1") == "内容".encode()
    with pytest.raises(AppError):
        await store.get("../outside")
    await store.delete("object-1")
    with pytest.raises(AppError):
        await store.get("object-1")


@pytest.mark.asyncio
async def test_file_boundary(tmp_path):
    """真实符号链接及绝对路径不能越出工具工作目录。"""
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("外部内容")
    (root / "link.txt").symlink_to(outside)
    settings = Settings(tools={"directory": root})
    tools = ToolRegistry(SimpleNamespace(settings=settings))
    with pytest.raises(AppError):
        await tools.read_file("link.txt")
    with pytest.raises(AppError):
        await tools.read_file("../outside.txt")
    await tools.write_file("inside.txt", "内容", "operation-1")
    assert await tools.read_file("inside.txt") == "内容"


def test_actual_document_parsers(tmp_path):
    """解析真实 TXT、Markdown、HTML 与 DOCX 字节，保留页面位置和切分内容。"""
    import io

    from docx import Document

    resources = SimpleNamespace(db=None)
    service = KnowledgeService(resources)
    assert service.parse("text.md", "标题\n内容".encode())[0]["content"] == "标题\n内容"
    assert (
        service.parse("text.html", b"<p>content</p><script>secret</script>")[0]["content"]
        == "content"
    )
    document = Document()
    document.add_paragraph("文档内容")
    stream = io.BytesIO()
    document.save(stream)
    assert "文档内容" in service.parse("text.docx", stream.getvalue())[0]["content"]
    chunks = service.split([{"content": "abcdef", "page": 1}], 4, 1)
    assert chunks[0]["content"] == "abcd" and chunks[1]["content"] == "def"
    with pytest.raises(AppError):
        service.parse("binary.unknown", b"data")
