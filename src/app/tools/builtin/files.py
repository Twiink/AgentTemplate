"""工作目录内的文件读取、写入与搜索；逐级打开路径，拒绝符号链接、绝对路径与越界访问。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from pydantic import BaseModel, Field

from app.core.config import ToolConfig
from app.core.errors import AppError


class ReadFile(BaseModel):
    """工作目录内 UTF-8 文件读取参数。"""

    path: str


class WriteFile(ReadFile):
    """写入必须给出稳定 operation_id，恢复时重复调用复用同一结果。"""

    content: str
    operation_id: str = Field(min_length=1, max_length=200)


class SearchFiles(BaseModel):
    """文件名与内容搜索参数；最多扫描固定数量文件。"""

    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=20, ge=1, le=100)


def file_descriptor(config: ToolConfig, path: str, *, write: bool = False):
    """逐级使用 dir_fd 和 O_NOFOLLOW 打开路径，防止符号链接及检查后替换攻击。"""
    parts = Path(path).parts
    if not parts or Path(path).is_absolute() or any(p in {"..", "."} for p in parts):
        raise AppError("path_denied", "文件路径越界", 403)
    root = config.directory.resolve()
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        flags = os.O_NOFOLLOW | (os.O_WRONLY | os.O_CREAT | os.O_TRUNC if write else os.O_RDONLY)
        return os.open(parts[-1], flags, 0o600, dir_fd=fd)
    except OSError as error:
        raise AppError("file_access_denied", "文件不存在或路径不允许访问", 403) from error
    finally:
        os.close(fd)


async def read_file(config: ToolConfig, path: str):
    """限制文件大小并按 UTF-8 读取，二进制文件给出明确错误。"""

    def read():
        """在工作线程内读取有限字节，拒绝越界及超大文件。"""
        with os.fdopen(file_descriptor(config, path), "rb") as handle:
            data = handle.read(config.max_bytes + 1)
            if len(data) > config.max_bytes:
                raise AppError("file_too_large", "文件超过工具读取限制")
            return data.decode("utf-8")

    return await asyncio.to_thread(read)


async def write_file(config: ToolConfig, path: str, content: str, operation_id: str):
    """执行经批准的文件写入；大小在打开文件之前校验。"""
    data = content.encode()
    if len(data) > config.max_bytes:
        raise AppError("file_too_large", "写入内容超过限制")

    def write():
        """写入已批准内容并同步文件，错误由调用账本记录。"""
        with os.fdopen(file_descriptor(config, path, write=True), "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    await asyncio.to_thread(write)
    return {"path": path, "bytes": len(data)}


async def search_files(config: ToolConfig, query: str, limit: int):
    """扫描最多一千个普通文件；跳过链接及无法读取文件，结果不回显完整内容。"""
    root = config.directory.resolve()
    results = []
    for count, path in enumerate(root.rglob("*")):
        if count >= 1000 or len(results) >= limit:
            break
        if path.is_symlink() or not path.is_file():
            continue
        relative = str(path.relative_to(root))
        try:
            content = await read_file(config, relative)
        except (AppError, UnicodeError):
            continue
        if query in relative or query in content:
            results.append(
                {
                    "path": relative,
                    "matching_lines": [
                        i + 1 for i, line in enumerate(content.splitlines()) if query in line
                    ][:20],
                }
            )
    return results
