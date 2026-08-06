"""天翼目录读取与文件查询。"""

import json
import time
from pathlib import PurePosixPath

from ...core.cloud import CloudFile, DirectoryListing, DirectoryLookup
from ...core.transfer import HttpFileDownloadService


class TianyiFileService:
    def __init__(self, client):
        self.client = client
        self._items_by_id: dict[str, CloudFile] = {}

    def _remember(self, item: CloudFile) -> CloudFile:
        if item.id:
            self._items_by_id[item.id] = item
        return item

    def download_file(self, file_item: CloudFile, local_path: str,
                      progress_callback=None, stop_requested=None,
                      preserve_partial: bool = False,
                      download_threads: int = 5) -> str:
        url, headers = self.resolve_download_link(file_item)
        return HttpFileDownloadService(
            lambda _: (url, headers), concurrency=download_threads,
        ).download_file(
            file_item, local_path, progress_callback, stop_requested,
            preserve_partial=preserve_partial,
        )

    def resolve_download_link(self, file_item: CloudFile) -> tuple[str, dict]:
        data = self.client.request(
            "GET", "https://cloud.189.cn/api/portal/getFileInfo.action",
            params={"fileId": file_item.id},
        )
        url = str(data.get("downloadUrl") or data.get("fileDownloadUrl") or "")
        if url.startswith("//"):
            url = "https:" + url
        url = url.replace("http://", "https://", 1)
        headers = {
            "Cookie": self.client.session.headers.get("Cookie", ""),
            "Referer": "https://cloud.189.cn/",
        }
        return url, headers

    def resolve_directory(self, path: str, create: bool = False) -> DirectoryLookup:
        if str(path or "/") in ("", "/"):
            return DirectoryLookup(True, "-11")
        current = "-11"
        for part in str(path).strip("/").split("/"):
            found = next((f for f in self.list_directory(current).files
                          if f.is_directory and f.name == part), None)
            if not found and create:
                data = self.client.request(
                    "POST", "https://cloud.189.cn/api/open/file/createFolder.action",
                    data={"parentFolderId": current, "folderName": part},
                )
                result = data.get("data") if isinstance(data.get("data"), dict) else data
                folder_id = str(
                    result.get("id") or result.get("folderId")
                    or result.get("fileId") or ""
                )
                if folder_id:
                    found = self._remember(
                        CloudFile(folder_id, part, True, native=result)
                    )
                else:
                    found = next(
                        (f for f in self.list_directory(current).files
                         if f.is_directory and f.name == part),
                        None,
                    )
            if not found:
                return DirectoryLookup(True, None)
            current = found.id
        return DirectoryLookup(True, current)

    def list_directory(self, directory_id: str) -> DirectoryListing:
        result, page = [], 1
        while True:
            data = self.client.request("GET", "https://cloud.189.cn/api/open/file/listFiles.action",
                                       params={"pageSize": 60, "pageNum": page, "mediaType": 0,
                                               "folderId": directory_id, "iconOption": 5,
                                               "orderBy": "lastOpTime", "descending": "true"})
            info = data.get("fileListAO") or {}
            for item in info.get("folderList") or []:
                result.append(self._remember(
                    CloudFile(str(item.get("id") or ""), str(item.get("name") or ""), True, native=item)))
            for item in info.get("fileList") or []:
                result.append(self._remember(CloudFile(str(item.get("id") or ""), str(item.get("name") or ""), False,
                                                       size=int(item.get("size") or 0), md5=str(item.get("md5") or ""),
                                                       native=item)))
            if len(result) >= int(info.get("count") or 0):
                break
            page += 1
        return DirectoryListing(True, tuple(result))

    def list_directories(self, path: str):
        lookup = self.resolve_directory(path)
        if not lookup.directory_id:
            return []
        base = PurePosixPath("/" + str(path or "/").strip("/"))
        return [
            {"id": f.id, "name": f.name, "path": str(base / f.name)}
            for f in self.list_directory(lookup.directory_id).files
            if f.is_directory
        ]

    def find_file(self, path: str, file_name: str, **kwargs):
        lookup = self.resolve_directory(path)
        if not lookup.directory_id:
            return None
        return next((f for f in self.list_directory(lookup.directory_id).files if f.name == file_name), None)

    find_file_strict = find_file
    get_cached_file = find_file

    def _find_with_retry(self, path: str, file_name: str) -> CloudFile | None:
        for index in range(10):
            if item := self.find_file(path, file_name):
                return item
            if index < 9:
                time.sleep(0.5)
        return None

    def list_files_recursive(self, path: str, **kwargs):
        lookup = self.resolve_directory(path)
        if not lookup.directory_id:
            return []
        return self._list_files_recursive_by_id(lookup.directory_id)

    def _list_files_recursive_by_id(self, directory_id: str) -> list[CloudFile]:
        result = []
        for item in self.list_directory(directory_id).files:
            if item.is_directory:
                result.extend(self._list_files_recursive_by_id(item.id))
            else:
                result.append(item)
        return result

    def rename_file(self, path: str, item: CloudFile, target_name: str) -> bool:
        url = "https://cloud.189.cn/api/open/file/renameFolder.action" \
            if item.is_directory else "https://cloud.189.cn/api/open/file/renameFile.action"
        data = (
            {"folderId": item.id, "destFolderName": target_name}
            if item.is_directory
            else {"fileId": item.id, "destFileName": target_name}
        )
        self.client.request("POST", url, data=data)
        return self._find_with_retry(path, target_name) is not None

    def _batch_task(self, task_type: str, item: CloudFile, target_id: str = "") -> None:
        created = self.client.request(
            "POST", "https://cloud.189.cn/api/open/batch/createBatchTask.action",
            data={
                "type": task_type,
                "targetFolderId": target_id,
                "taskInfos": json.dumps([{
                    "fileId": item.id,
                    "fileName": item.name,
                    "isFolder": 1 if item.is_directory else 0,
                }], ensure_ascii=False),
            },
        )
        task_id = str(created.get("taskId") or created.get("task_id") or "")
        if not task_id:
            raise RuntimeError("天翼批量任务未返回任务 ID")
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            status = self.client.request(
                "POST", "https://cloud.189.cn/api/open/batch/checkBatchTask.action",
                data={"type": task_type, "taskId": task_id},
            )
            task_status = int(status.get("taskStatus") or 0)
            if task_status == 4:
                if int(status.get("failedCount") or 0) > 0:
                    raise RuntimeError("天翼批量任务存在失败文件")
                return
            if task_status == 2:
                raise RuntimeError("天翼批量任务存在同名冲突")
            time.sleep(0.4)
        raise TimeoutError("等待天翼批量任务完成超时")

    def move_file(
            self, item: CloudFile, save_path: str, target_name: str
    ) -> CloudFile | None:
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.directory_id:
            return None
        self._batch_task("MOVE", item, lookup.directory_id)
        if target_name and target_name != item.name:
            moved = CloudFile(
                item.id, item.name, item.is_directory, item.size,
                item.sha1, item.md5, item.playback_values, item.native,
            )
            if not self.rename_file(save_path, moved, target_name):
                return None
        return self._find_with_retry(save_path, target_name or item.name)

    def delete_file(self, file_id: str) -> bool:
        item = self._items_by_id.get(str(file_id or ""))
        if not item:
            return False
        self._batch_task("DELETE", item)
        self._items_by_id.pop(item.id, None)
        return True
