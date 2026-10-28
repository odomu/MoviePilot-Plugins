"""光鸭网盘目录、查询与文件变更能力。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.core.cache import TTLCache
from app.log import logger

from ..common import CloudDriveFileServiceBase, create_directory_cache, safe_int
from ...core.cloud import CloudFile
from ...core.transfer import HttpFileDownloadService


def cloud_file(item: Any) -> Optional[CloudFile]:
    """将光鸭 API 返回的文件字典转为标准的 CloudFile 对象。"""
    if not isinstance(item, dict):
        return None
    file_id = str(item.get("fileId") or item.get("id") or item.get("fid") or item.get("resId") or "").strip()
    name = str(item.get("fileName") or item.get("name") or item.get("filename") or "").strip()
    if not file_id or not name:
        return None

    res_type = safe_int(item.get("resType"))
    if res_type in (1, 2):
        is_directory = (res_type == 2)
    else:
        is_directory = bool(
            item.get("isDir")
            or item.get("is_dir")
            or str(item.get("type") or "").lower() in ("2", "dir", "folder")
        )

    size = 0 if is_directory else safe_int(
        item.get("totalSize") or item.get("fileSize") or item.get("size")
    )
    checksum = str(item.get("md5") or item.get("gcid") or item.get("gcId") or "").strip()

    playback_values = {}
    if not is_directory:
        playback_values = {
            "file_id": file_id,
            "gcid": checksum,
        }

    return CloudFile(
        id=file_id,
        name=name,
        is_directory=is_directory,
        size=size,
        sha1=str(item.get("sha1") or ""),
        md5=checksum,
        playback_values=playback_values,
        native=item,
    )


@dataclass
class GuangyaFileService(CloudDriveFileServiceBase):
    """封装光鸭目录解析、文件遍历与操作（参考 123 网盘扁平直观设计）。"""

    client: Any
    page_size: int = 50
    root_directory_id = ""
    provider_name = "光鸭"
    provider_key = "guangya"
    _directory_cache: TTLCache = field(init=False, repr=False)

    def __post_init__(self):
        self._directory_cache = create_directory_cache("guangya", self.client)

    def download_file(
            self,
            file_item: CloudFile,
            local_path: str,
            progress_callback=None,
            stop_requested=None,
            preserve_partial: bool = False,
            download_threads: int = 5,
    ) -> str:
        url, headers = self.resolve_download_link(file_item)
        return HttpFileDownloadService(
            lambda _: (url, headers), concurrency=download_threads,
        ).download_file(
            file_item, local_path, progress_callback, stop_requested,
            preserve_partial=preserve_partial,
        )

    def resolve_download_link(self, file_item: CloudFile) -> tuple[str, dict]:
        """获取光鸭文件直链下载地址。"""
        access_token = str(
            file_item.playback_values.get("share_access_token") or ""
        ).strip()
        if access_token:
            response = self.client.request(
                "POST",
                f"{self.client.API_BASE_URL}/userres/v1/get_share_download_url",
                json_data={"fileId": file_item.id, "accessToken": access_token},
                authenticated=False,
            )
        else:
            response = self.client.request(
                "POST",
                f"{self.client.API_BASE_URL}/userres/v1/get_res_download_url",
                json_data={"fileId": file_item.id},
            )
        data = self.client.data(response) or {}
        url = str(
            data.get("signedURL") or data.get("signedUrl") or data.get("downloadUrl")
            or data.get("url") or data.get("fileDownloadUrl") or ""
        ).strip()
        if not url:
            message = str(response.get("msg") or response.get("message") or "")
            raise RuntimeError(message or "光鸭未返回有效下载地址")
        return url, {}

    def _get_file_list(
            self, parent_id: str = "", page: int = 0, page_size: int = 50
    ) -> Dict[str, Any]:
        """根据抓包对齐的标准参数读取目录文件列表。"""
        return self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/userres/v1/file/get_file_list",
            json_data={
                "pageSize": page_size or self.page_size,
                "orderBy": 3,
                "sortType": 1,
                "parentId": parent_id or "",
                "page": page,
            },
        )

    def _list(self, directory_id: str) -> List[CloudFile]:
        directory_id = str(directory_id or "")
        cached = self._directory_cache.get(directory_id)
        if cached is not None:
            return list(cached)
        files: List[CloudFile] = []
        page = 0
        while True:
            response = self._get_file_list(directory_id, page, self.page_size)
            if not self.client.is_success(response):
                raise RuntimeError(response.get("msg") or response.get("error") or "读取光鸭目录失败")
            data = response.get("data") or {}
            raw_items = data if isinstance(data, list) else data.get("list") or []
            files.extend(item for raw in raw_items if (item := cloud_file(raw)))
            total = safe_int(data.get("total") if isinstance(data, dict) else 0)
            if len(raw_items) < self.page_size or (total and len(files) >= total):
                self._directory_cache.set(directory_id, tuple(files))
                return files
            page += 1

    def _create_folder(self, name: str, parent_id: str) -> Optional[CloudFile]:
        """直接创建目录，避免多层封装转发。"""
        response = self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/userres/v1/file/create_dir",
            json_data={"dirName": name, "parentId": parent_id or "", "failIfNameExist": False},
        )
        if not self.client.is_success(response):
            raise RuntimeError(response.get("msg") or response.get("error") or "创建光鸭目录失败")
        self._invalidate_directory_cache()
        data = self.client.data(response)
        return cloud_file(data)

    @staticmethod
    def _ensure_cloud_file(item: Any) -> CloudFile:
        if isinstance(item, CloudFile):
            return item
        if isinstance(item, dict):
            return CloudFile(
                id=str(item.get("id") or item.get("file_id") or ""),
                name=str(item.get("name") or item.get("file_name") or ""),
                size=int(item.get("size") or 0),
                is_directory=bool(item.get("is_directory") or item.get("is_dir")),
                playback_values=dict(item.get("playback_values") or {}),
                native=item.get("native") or item,
            )
        return CloudFile(
            id=str(getattr(item, "id", "")),
            name=str(getattr(item, "name", "")),
            size=int(getattr(item, "size", 0) or 0),
            is_directory=bool(getattr(item, "is_directory", False)),
            playback_values=dict(getattr(item, "playback_values", None) or {}),
            native=getattr(item, "native", None),
        )

    def rename_file(self, path: str, item: Any, target_name: str) -> bool:
        item = self._ensure_cloud_file(item)
        response = self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/userres/v1/file/rename",
            json_data={"fileId": item.id, "newName": target_name},
        )
        success = self.client.is_success(response)
        if success:
            self._invalidate_directory_cache()
            if item.is_directory:
                self._invalidate_path_cache()
        return success

    def move_file(
            self, item: Any, save_path: str, target_name: str
    ) -> Optional[CloudFile]:
        item = self._ensure_cloud_file(item)
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return None
        moved = self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/userres/v1/file/move_file",
            json_data={"fileIds": [item.id], "parentId": lookup.directory_id or ""},
        )
        if not self.client.is_success(moved):
            return None
        self._invalidate_directory_cache()
        if item.is_directory:
            self._invalidate_path_cache()
        final_name = target_name or item.name
        if target_name and target_name != item.name:
            if not self.rename_file(save_path, item, target_name):
                logger.warning(f"光鸭文件移入目录后重命名失败，保留原名：{item.name} -> {target_name}")
                final_name = item.name
            else:
                final_name = target_name
        found = self.find_file(save_path, final_name)
        if found:
            return found
        return CloudFile(
            id=item.id,
            name=final_name,
            size=item.size,
            is_directory=item.is_directory,
            playback_values=dict(item.playback_values or {}),
            native=item.native,
        )

    def delete_file(self, file_id: str) -> bool:
        response = self.client.request(
            "POST",
            f"{self.client.API_BASE_URL}/userres/v1/file/delete_file",
            json_data={"fileIds": [file_id]},
        )
        success = self.client.is_success(response)
        if success:
            self._invalidate_directory_cache()
            self._invalidate_path_cache()
        return success

    def _batch_action_completed(self, response: Dict[str, Any]) -> bool:
        if not self.client.is_success(response):
            return False
        data = self.client.data(response)
        task_id = str(
            data.get("taskId") or data.get("task_id") or ""
        ) if isinstance(data, dict) else ""
        if not task_id:
            return True
        for retry_index in range(120):
            status_response = self.client.request(
                "POST",
                f"{self.client.API_BASE_URL}/userres/v1/get_task_status",
                json_data={"taskId": task_id},
            )
            status_data = self.client.data(status_response)
            status = (
                status_data.get("status", status_data.get("taskStatus"))
                if isinstance(status_data, dict) else None
            )
            if status in (2, "2", "success", "done", "finished"):
                return True
            if status in (3, "3", "failed", "error") or status_response.get("code") in (145, "145"):
                return False
            if retry_index < 119:
                time.sleep(0.5)
        return False

    def move_files(
            self, items: dict[str, CloudFile], save_path: str
    ) -> dict[str, CloudFile]:
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return {}
        entries = list(dict(items or {}).items())
        moved = {}
        for offset in range(0, len(entries), 50):
            batch = entries[offset:offset + 50]
            response = self.client.request(
                "POST",
                f"{self.client.API_BASE_URL}/userres/v1/file/move_file",
                json_data={
                    "fileIds": [item.id for _, item in batch],
                    "parentId": lookup.directory_id or "",
                },
            )
            if self._batch_action_completed(response):
                moved.update({str(key): item for key, item in batch})
        if moved:
            self._invalidate_directory_cache()
            if any(item.is_directory for item in moved.values()):
                self._invalidate_path_cache()
        return moved

    def delete_files(self, file_ids: list[str]) -> set[str]:
        file_ids = list(dict.fromkeys(
            str(value) for value in (file_ids or []) if str(value or "")
        ))
        deleted = set()
        for offset in range(0, len(file_ids), 50):
            batch = file_ids[offset:offset + 50]
            response = self.client.request(
                "POST",
                f"{self.client.API_BASE_URL}/userres/v1/file/delete_file",
                json_data={"fileIds": batch},
            )
            if self._batch_action_completed(response):
                deleted.update(batch)
        if deleted:
            self._invalidate_directory_cache()
            self._invalidate_path_cache()
        return deleted

    def list_offline_task_files(self, task: Any, path: str) -> list[CloudFile]:
        """精确定位离线任务生成的文件或目录，避免全盘递归扫描。"""
        if not isinstance(task, dict):
            return []
        file_id = str(task.get("file_id") or task.get("fileId") or "").strip()
        name = str(task.get("name") or task.get("fileName") or "").strip()
        if not file_id:
            return []
        is_dir = bool(task.get("is_dir") or task.get("isDir"))
        if is_dir:
            try:
                response = self._get_file_list(parent_id=file_id, page=0, page_size=self.page_size)
                data = response.get("data") or {}
                raw_items = data if isinstance(data, list) else data.get("list") or []
                files = [f for f in (cloud_file(item) for item in raw_items) if f]
                if files:
                    return files
            except Exception as error:
                logger.debug(f"读取光鸭离线目录文件异常：{error}")
        return [
            CloudFile(
                id=file_id,
                name=name,
                is_directory=is_dir,
                size=safe_int(task.get("size") or task.get("totalSize")),
                playback_values={"file_id": file_id} if not is_dir else {},
                native=task,
            )
        ]
