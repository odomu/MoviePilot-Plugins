"""夸克目录与文件操作能力。"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..common import CloudDriveFileServiceBase, extract_list, safe_int
from ...core.cloud import CloudFile


def list_data(client: Any, response: Any) -> list:
    return extract_list(client.data(response), ("list", "files", "items", "records"))


def cloud_file(item: Any) -> Optional[CloudFile]:
    if not isinstance(item, dict):
        return None
    file_id = item.get("fid") or item.get("file_id") or item.get("id")
    name = str(item.get("file_name") or item.get("name") or item.get("filename") or "").strip()
    if file_id in (None, "") or not name:
        return None
    file_type = item.get("file_type")
    is_directory = bool(item.get("dir") or item.get("is_dir") or file_type == 0)
    if file_type not in (None, 0, "0"):
        is_directory = False
    return CloudFile(
        id=str(file_id),
        name=name,
        is_directory=is_directory,
        size=0 if is_directory else safe_int(item.get("size") or item.get("file_size")),
        sha1=str(item.get("sha1") or ""),
        playback_values={"file_id": str(file_id)} if not is_directory else {},
        native=item,
    )


@dataclass
class QuarkFileService(CloudDriveFileServiceBase):
    client: Any
    page_size: int = 100
    root_directory_id = "0"
    provider_name = "夸克"
    _path_ids: Dict[str, str] = field(default_factory=lambda: {"/": "0"})

    def _get_file_list(
            self, parent_id: str = "0", page: int = 1, size: int = 100
    ) -> Dict[str, Any]:
        return self.client.request(
            "GET",
            "file/sort",
            params={
                "pdir_fid": parent_id,
                "_page": page,
                "_size": size,
                "_sort": "file_name:asc",
            },
        )

    def _create_folder_request(
            self, name: str, parent_id: str = "0"
    ) -> Dict[str, Any]:
        return self.client.request(
            "POST",
            "file",
            json_data={
                "pdir_fid": parent_id,
                "file_name": name,
                "dir_init_lock": False,
                "dir_path": "",
            },
        )

    def _list(self, directory_id: str) -> List[CloudFile]:
        files: List[CloudFile] = []
        page = 1
        while True:
            response = self._get_file_list(directory_id, page, self.page_size)
            if not self.client.is_success(response):
                raise RuntimeError(response.get("message") or "读取夸克目录失败")
            raw_items = list_data(self.client, response)
            files.extend(item for raw in raw_items if (item := cloud_file(raw)))
            data = self.client.data(response)
            total = safe_int(data.get("total") if isinstance(data, dict) else 0)
            if len(raw_items) < self.page_size or (total and len(files) >= total):
                return files
            page += 1

    def _create_folder(self, name: str, parent_id: str) -> Optional[CloudFile]:
        response = self._create_folder_request(name, parent_id)
        if not self._is_success(response):
            raise RuntimeError(response.get("message") or "创建夸克目录失败")
        return cloud_file(self.client.data(response))

    def _is_success(self, response: Any) -> bool:
        return self.client.is_success(response)

    def rename_file(self, path: str, item: CloudFile, target_name: str) -> bool:
        response = self.client.request(
            "POST", "file/rename",
            json_data={"fid": item.id, "file_name": target_name},
        )
        return self._is_success(response)

    def move_file(
            self, item: CloudFile, save_path: str, target_name: str
    ) -> Optional[CloudFile]:
        lookup = self.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return None
        moved = self.client.request(
            "POST", "file/move",
            json_data={
                "action_type": 1,
                "to_pdir_fid": lookup.directory_id,
                "filelist": [item.id],
                "exclude_fids": [],
            },
        )
        if not self._is_success(moved):
            return None
        if target_name and target_name != item.name:
            if not self.rename_file(save_path, item, target_name):
                return None
        return self.find_file(save_path, target_name or item.name)

    def delete_file(self, file_id: str) -> bool:
        response = self.client.request(
            "POST", "file/delete",
            json_data={"action_type": 2, "filelist": [file_id], "exclude_fids": []},
        )
        return self._is_success(response)
