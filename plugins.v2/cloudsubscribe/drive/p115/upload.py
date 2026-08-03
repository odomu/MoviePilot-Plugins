"""115 本地文件上传能力。"""

import hashlib
from pathlib import Path
from typing import Callable, Optional

from app.log import logger

from ...core import OwnerDelegator

try:
    from p115client import check_response

    P115_AVAILABLE = True
except ImportError:
    P115_AVAILABLE = False


class P115UploadService(OwnerDelegator):
    """使用 p115client 原生上传接口将本地文件写入 115。"""

    @staticmethod
    def _file_sha1(path: Path) -> str:
        digest = hashlib.sha1()
        with path.open("rb") as file:
            while chunk := file.read(8 * 1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest().upper()

    def upload_file(
            self,
            local_path: str,
            save_path: str,
            target_name: str = "",
            file_sha1: str = "",
            progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> bool:
        if not P115_AVAILABLE or not self.client:
            logger.error("115 本地上传不可用：客户端未初始化")
            return False
        source = Path(str(local_path or ""))
        if not source.is_file():
            logger.error(f"115 本地上传文件不存在：{source}")
            return False
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            logger.error(f"115 本地上传目录不可用：{save_path}")
            return False
        upload_name = str(target_name or source.name).strip()
        checksum = str(file_sha1 or "").strip().upper() or self._file_sha1(source)
        try:
            self.rate_limiter.wait()
            self._api_call_count += 1
            file_size = source.stat().st_size
            if progress_callback:
                with source.open("rb") as file:
                    response = self.client.upload_file(
                        _ProgressReader(file, file_size, progress_callback),
                        pid=int(lookup.directory_id or 0),
                        filename=upload_name,
                        filesha1=checksum,
                        filesize=file_size,
                        partsize=-1,
                    )
            else:
                response = self.client.upload_file(
                    source,
                    pid=int(lookup.directory_id or 0),
                    filename=upload_name,
                    filesha1=checksum,
                    filesize=file_size,
                    partsize=-1,
                )
            check_response(response)
            self._target_file_cache.clear()
            if progress_callback:
                progress_callback(file_size, file_size)
            logger.info(f"115 本地文件上传完成：{source.name} -> {save_path}/{upload_name}")
            return True
        except Exception as error:
            logger.error(f"115 本地文件上传失败：{source.name}，{error}")
            return False


class _ProgressReader:
    """为 p115client 的文件读取过程补充字节进度回调。"""

    def __init__(self, file, total: int, callback: Callable[[int, int], None]):
        self._file = file
        self._total = total
        self._callback = callback

    def read(self, size: int = -1):
        chunk = self._file.read(size)
        self._callback(self._file.tell(), self._total)
        return chunk

    def __getattr__(self, name):
        return getattr(self._file, name)
