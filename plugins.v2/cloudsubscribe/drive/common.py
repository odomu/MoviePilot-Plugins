"""网盘适配器共享的无状态转换和目录操作。"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, ClassVar, Dict, Mapping, Optional, Sequence

from app.log import logger

from ..core.cloud import CloudFile, DirectoryListing, DirectoryLookup


def safe_int(value: Any) -> int:
    """将外部接口值转换为整数，空值和非法值按 0 处理。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def format_size(value: Any) -> str:
    """以 1024 为进制格式化字节数。"""
    size = float(max(0, safe_int(value)))
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            break
        size /= 1024
    return f"{size:.2f} {unit}"


def extract_list(data: Any, keys: Sequence[str]) -> list:
    """从网盘响应数据中提取列表，兼容各提供方的容器字段。"""
    if isinstance(data, list):
        return data
    if isinstance(data, Mapping):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def normalize_path(path: str) -> str:
    return str(PurePosixPath("/" + str(path or "/").lstrip("/")))


class CloudDriveFileServiceBase:
    """复用目录解析、遍历和基础文件操作，具体 API 调用由文件服务提供。"""

    root_directory_id: ClassVar[str]
    provider_name: ClassVar[str]
    client: Any
    _path_ids: Dict[str, str]

    def _list(self, directory_id: str) -> list[CloudFile]:
        raise NotImplementedError

    def _create_folder(self, name: str, parent_id: str) -> Optional[CloudFile]:
        raise NotImplementedError

    def _is_success(self, response: Any) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        self.client.close()

    def resolve_directory(self, path: str, create: bool = False) -> DirectoryLookup:
        current_id = self.root_directory_id
        current_path = ""
        try:
            for part in normalize_path(path).split("/"):
                if not part:
                    continue
                current_path = f"{current_path}/{part}"
                if current_path in self._path_ids:
                    current_id = self._path_ids[current_path]
                    continue
                match = next(
                    (
                        item
                        for item in self._list(current_id)
                        if item.is_directory and item.name == part
                    ),
                    None,
                )
                if not match and create:
                    match = self._create_folder(part, current_id)
                    if not match:
                        match = next(
                            (
                                item
                                for item in self._list(current_id)
                                if item.is_directory and item.name == part
                            ),
                            None,
                        )
                if not match:
                    return DirectoryLookup(True, None)
                current_id = match.id
                self._path_ids[current_path] = current_id
            return DirectoryLookup(True, current_id)
        except Exception as error:
            logger.warning(f"解析{self.provider_name}目录失败：{path} - {error}")
            return DirectoryLookup(False)

    def list_directory(self, directory_id: str) -> DirectoryListing:
        try:
            return DirectoryListing(
                True,
                tuple(self._list(directory_id or self.root_directory_id)),
            )
        except Exception as error:
            logger.warning(f"读取{self.provider_name}目录失败：{error}")
            return DirectoryListing(False)

    def list_directories(self, path: str) -> list[Dict[str, str]]:
        lookup = self.resolve_directory(path)
        if not lookup.checked or lookup.directory_id is None:
            return []
        base = PurePosixPath(normalize_path(path))
        return [
            {
                "id": item.id,
                "name": item.name,
                "path": str(base / item.name),
            }
            for item in self._list(lookup.directory_id)
            if item.is_directory
        ]

    def list_files_recursive(self, path: str, **kwargs: Any) -> list[CloudFile]:
        lookup = self.resolve_directory(path)
        if not lookup.checked or lookup.directory_id is None:
            return []
        result: list[CloudFile] = []
        stack = [lookup.directory_id]
        while stack:
            for item in self._list(stack.pop()):
                if item.is_directory:
                    stack.append(item.id)
                else:
                    result.append(item)
        return result

    def find_file(
            self, path: str, file_name: str, **kwargs: Any
    ) -> Optional[CloudFile]:
        lookup = self.resolve_directory(path)
        if not lookup.checked or lookup.directory_id is None:
            return None
        return next(
            (
                item
                for item in self._list(lookup.directory_id)
                if item.name == file_name
            ),
            None,
        )

    def find_file_strict(self, path: str, file_name: str) -> Optional[CloudFile]:
        return self.find_file(path, file_name)

    def get_cached_file(self, path: str, file_name: str) -> Optional[CloudFile]:
        return self.find_file(path, file_name)

    def rename_file(self, path: str, item: CloudFile, target_name: str) -> bool:
        return self._is_success(self.client.rename_file(item.id, target_name))

    def move_file(
            self, item: CloudFile, save_path: str, target_name: str
    ) -> Optional[CloudFile]:
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return None
        if not self._is_success(self.client.move_files([item.id], lookup.directory_id)):
            return None
        if target_name and target_name != item.name:
            if not self._is_success(self.client.rename_file(item.id, target_name)):
                return None
        return self.find_file(save_path, target_name or item.name)

    def delete_file(self, file_id: str) -> bool:
        return self._is_success(self.client.delete_files([file_id]))
