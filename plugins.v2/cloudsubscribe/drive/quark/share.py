"""夸克分享访问与转存能力。"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable

from app.log import logger

from .files import cloud_file, list_data
from ..common import safe_int
from ...core.cloud import ShareLinkStatus


class QuarkShareService:
    """封装夸克分享接口、文件遍历和转存。"""

    def __init__(self, client: Any, files: Any):
        self._files = files
        self.client = client
        self.page_size = files.page_size

    def _get_share_token(self, share_id: str, password: str = "") -> Dict[str, Any]:
        return self.client.request(
            "POST",
            "share/sharepage/token",
            json_data={
                "pwd_id": share_id,
                "passcode": password,
                "support_visit_limit_private_share": True,
            },
            base_url=self.client.SHARE_BASE_URL,
        )

    def _get_share_files(
            self, share_id: str, token: str, parent_id: str = "0",
            page: int = 1, size: int = 100,
    ) -> Dict[str, Any]:
        return self.client.request(
            "GET",
            "share/sharepage/detail",
            params={
                "pwd_id": share_id,
                "stoken": token,
                "pdir_fid": parent_id,
                "force": "0",
                "_page": page,
                "_size": size,
                "_fetch_banner": "1",
                "_fetch_share": "1",
                "_fetch_total": "1",
                "_sort": "file_type:asc,file_name:asc",
            },
            base_url=self.client.SHARE_BASE_URL,
        )

    def _save_shared_files(
            self, share_id: str, token: str, file_ids: list, target_id: str
    ) -> Dict[str, Any]:
        result = self.client.request(
            "POST",
            "share/sharepage/save",
            json_data={
                "fid_list": file_ids,
                "fid_token_list": [],
                "to_pdir_fid": target_id,
                "pwd_id": share_id,
                "stoken": token,
                "pdir_fid": "0",
                "pdir_save_all": not file_ids,
                "exclude_fids": [],
                "scene": "link",
            },
            base_url=self.client.SHARE_BASE_URL,
        )
        task_id = (self.client.data(result) or {}).get("task_id")
        if self.client.is_success(result) and task_id:
            result["task_success"] = self.client.wait_for_task(str(task_id))
        return result

    @staticmethod
    def extract_share_info(share_url: str) -> Dict[str, Any]:
        match = re.search(
            r"(?:https?://pan\.quark\.cn/s/|quark://share/)([A-Za-z0-9]+)",
            share_url or "",
            re.I,
        )
        if not match:
            return {}
        code_match = re.search(
            r"(?:提取码|密码|code)\s*[：:]?\s*([A-Za-z0-9]+)",
            share_url,
            re.I,
        )
        receive_code = code_match.group(1) if code_match else ""
        return {
            "share_code": match.group(1),
            "receive_code": receive_code,
            "share_id": match.group(1),
            "password": receive_code,
        }

    def _share_access(self, share_url: str) -> tuple[Dict[str, Any], str]:
        info = self.extract_share_info(share_url)
        if not info:
            raise ValueError("无效的夸克分享链接")
        response = self._get_share_token(info["share_id"], info["password"])
        token = str((self.client.data(response) or {}).get("stoken") or "")
        if not self.client.is_success(response) or not token:
            raise RuntimeError(response.get("message") or "获取夸克分享令牌失败")
        return info, token

    def check_share_status(self, share_url: str) -> ShareLinkStatus:
        status = ShareLinkStatus()
        try:
            info, token = self._share_access(share_url)
            response = self._get_share_files(info["share_id"], token, size=1)
            if not self.client.is_success(response):
                status.error_message = response.get("message") or "分享不可用"
                return status
            status.is_valid = True
            data = self.client.data(response)
            status.file_count = safe_int(data.get("total") if isinstance(data, dict) else 0)
            status.share_info = {
                "share_title": str(data.get("title") or "")
                if isinstance(data, dict) else ""
            }
        except Exception as error:
            status.error_message = str(error)
        return status

    def list_share_files(self, share_url: str, **kwargs: Any) -> list:
        try:
            info, token = self._share_access(share_url)
            result = []
            stack = ["0"]
            while stack:
                parent_id = stack.pop()
                page = 1
                while True:
                    response = self._get_share_files(
                        info["share_id"], token, parent_id, page, self.page_size
                    )
                    if not self.client.is_success(response):
                        return result
                    items = list_data(self.client, response)
                    for raw in items:
                        item = cloud_file(raw)
                        if not item:
                            continue
                        if item.is_directory:
                            stack.append(item.id)
                        else:
                            result.append(dict(item))
                    if len(items) < self.page_size:
                        break
                    page += 1
            return result
        except Exception as error:
            logger.warning(f"读取夸克分享文件失败：{error}")
            return []

    def _save_share(
            self, share_url: str, file_ids: Iterable[str], save_path: str
    ) -> bool:
        info, token = self._share_access(share_url)
        lookup = self._files.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return False
        result = self._save_shared_files(
            info["share_id"], token, [str(value) for value in file_ids],
            lookup.directory_id,
        )
        return (
                self.client.is_success(result)
                and result.get("task_success", True) is not False
        )

    def transfer_share(self, share_url: str, save_path: str) -> bool:
        return self._save_share(share_url, [], save_path)

    def transfer_file(
            self, share_url: str, file_id: str, save_path: str,
            target_name: str, **kwargs: Any,
    ) -> bool:
        return self._save_share(share_url, [file_id], save_path)

    def transfer_files_batch(
            self, share_url: str, file_ids: list, save_path: str, **kwargs: Any
    ) -> tuple:
        normalized = [str(value) for value in file_ids]
        return (
            (normalized, [])
            if self._save_share(share_url, normalized, save_path)
            else ([], normalized)
        )
