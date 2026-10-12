"""本地与 S3 附件后端；元数据在数据库，内容通过不透明对象键定位。S3 依赖 s3 extra 中的 boto3。"""

import asyncio
from pathlib import Path
from typing import Protocol

from app.core.errors import AppError


class ArtifactStore(Protocol):
    """独立于 HTTP 的二进制存储契约；写入、读取、删除真实对象。"""

    async def put(self, key: str, content: bytes, content_type: str) -> None:
        """写入内容；失败抛出异常，调用者不得登记为成功。"""
        ...

    async def get(self, key: str) -> bytes:
        """读取对象；缺失返回明确错误。"""
        ...

    async def delete(self, key: str) -> None:
        """幂等删除对象。"""
        ...


class LocalStore:
    """本地原子文件存储；对象键禁止分隔符及符号链接越界。"""

    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        """验证服务端对象键，不把用户文件名拼入文件系统路径。"""
        if not key.replace("-", "").isalnum():
            raise AppError("invalid_object_key", "对象键无效")
        path = (self.directory / key).resolve()
        if not path.is_relative_to(self.directory):
            raise AppError("invalid_object_key", "对象键越界")
        return path

    async def put(self, key: str, content: bytes, content_type: str) -> None:
        """先写临时文件再替换，读取者不会看见半写入内容。"""
        path = self.path(key)

        def write():
            """临时文件完成后原子替换对象，避免部分内容可见。"""
            temp = path.with_suffix(".tmp")
            temp.write_bytes(content)
            temp.replace(path)

        await asyncio.to_thread(write)

    async def get(self, key: str) -> bytes:
        """在线程读取文件，避免较大附件阻塞事件循环。"""
        try:
            return await asyncio.to_thread(self.path(key).read_bytes)
        except FileNotFoundError as error:
            raise AppError("artifact_missing", "附件内容不存在", 404) from error

    async def delete(self, key: str) -> None:
        """重复删除可安全执行。"""
        await asyncio.to_thread(self.path(key).unlink, missing_ok=True)


class S3Store:
    """S3 兼容存储；SDK 连接与重试有上限，客户端由资源上下文关闭。"""

    def __init__(self, config):
        # boto3 属于 s3 extra，只在使用 S3 后端时导入。
        import boto3
        from botocore.config import Config

        if not config.bucket:
            raise AppError("storage_not_configured", "S3 bucket 未配置", 503)
        self.bucket = config.bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=config.endpoint,
            region_name=config.region,
            aws_access_key_id=config.access_key.get_secret_value() if config.access_key else None,
            aws_secret_access_key=config.secret_key.get_secret_value()
            if config.secret_key
            else None,
            config=Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 2}),
        )

    async def put(self, key: str, content: bytes, content_type: str) -> None:
        """上传实际对象，服务端错误由调用者转为失败。"""
        await asyncio.to_thread(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=content,
            ContentType=content_type,
        )

    async def get(self, key: str) -> bytes:
        """读取后立即关闭响应体，释放连接池连接。"""

        def read():
            """读取实际对象并关闭响应体，不泄漏池连接。"""
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            with response["Body"] as body:
                return body.read()

        return await asyncio.to_thread(read)

    async def delete(self, key: str) -> None:
        """S3 DeleteObject 按后端语义幂等执行。"""
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)

    async def close(self) -> None:
        """关闭 SDK 池连接。"""
        await asyncio.to_thread(self.client.close)
