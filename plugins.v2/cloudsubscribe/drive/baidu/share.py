"""百度网盘分享目录与转存。"""

from __future__ import annotations

import json
import re
import time
from pathlib import PurePosixPath
from threading import RLock
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from app.log import logger

from .files import wait_task
from ..common import safe_int, should_skip_season_dir
from ...core.cloud import ShareLinkStatus
from ...utils.cache import create_platform_ttl_cache
from ...utils.share_expiry import (
    is_expired_share_error,
    is_share_expired,
    mark_share_expired,
)


class BaiduShareService:
    """百度网盘分享服务。"""

    def __init__(self, client: Any, files: Any):
        self.client = client
        self._files = files
        self.page_size = 100
        self._shares = create_platform_ttl_cache(
            "baidu:shares", client, maxsize=128, ttl=600
        )
        self._lock = RLock()
        self._blocked_until = 0.0
        self.last_error = ""

    @property
    def transfer_risk_blocked(self) -> bool:
        return time.monotonic() < self._blocked_until

    @staticmethod
    def extract_share_info(share_url: str) -> Dict[str, Any]:
        match = re.search(
            r"https?://(?:pan|yun)\.baidu\.com/"
            r"(?:s/[A-Za-z0-9_-]+|share/init\?[^\s<>]+)",
            str(share_url or ""),
            re.I,
        )
        if not match:
            return {}
        parsed = urlsplit(match.group(0))
        if parsed.path.startswith("/s/"):
            full_id = parsed.path.split("/")[2]
            if not full_id.startswith("1") or len(full_id) < 2:
                return {}
            short_id = full_id[1:]
        else:
            short_id = (parse_qs(parsed.query).get("surl") or [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", short_id):
            return {}
        password_match = re.search(
            r"[?&#]pwd=([A-Za-z0-9]{4})(?![A-Za-z0-9])"
            r"|pwd\s*[:：]\s*([A-Za-z0-9]{4})(?![A-Za-z0-9])"
            r"|(?:提取码|密码)\s*[:：]?\s*([A-Za-z0-9]{4})(?![A-Za-z0-9])",
            str(share_url),
            re.I,
        )
        password = next(
            (value for value in password_match.groups() if value), ""
        ) if password_match else ""
        url = f"https://pan.baidu.com/s/1{short_id}"
        return {
            "share_code": short_id,
            "share_id": short_id,
            "receive_code": password,
            "password": password,
            "url": url,
            "share_url": url,
        }

    @staticmethod
    def _share_key(info: Dict[str, Any]) -> str:
        return f"{info.get('share_id', '')}:{info.get('password', '')}"

    def _handle_risk(self, response: Dict[str, Any]) -> bool:
        if safe_int(response.get("errno")) not in (8001, 31326, 31034, 132):
            return False
        cooldown = max(60, int(getattr(self.client, "risk_cooldown", 1800) or 1800))
        self._blocked_until = time.monotonic() + cooldown
        self.last_error = str(
            response.get("message") or self.client.error_message(response)
        )
        return True

    def _verify_password(self, info: Dict[str, Any]) -> str:
        password = str(info.get("password") or "").strip()
        if not password:
            return self.client.cookie_value("BDCLND")
        response = self.client.request(
            "POST",
            "/share/verify",
            params={
                "surl": info.get("share_id") or "",
                "t": int(time.time() * 1000),
                "channel": "chunlei",
                "web": 1,
                "app_id": 250528,
                "clienttype": 0,
                "bdstoken": "null",
            },
            data={"pwd": password, "vcode": "", "vcode_str": ""},
            headers={
                "Referer": (
                    f"https://pan.baidu.com/share/init?surl={info.get('share_id') or ''}"
                ),
                "X-Requested-With": "XMLHttpRequest",
            },
        )
        if not self.client.is_success(response):
            raise RuntimeError(
                response.get("message") or "百度网盘提取码校验失败"
            )
        randsk = str(response.get("randsk") or "").strip()
        if not randsk:
            randsk = self.client.cookie_value("BDCLND")
        if not randsk:
            raise RuntimeError("百度网盘提取码校验成功但未获得访问令牌")
        # Cookie 保留原编码，sekey 参数再解码。
        self.client.set_cookie("BDCLND", randsk)
        return randsk

    def _listing_request(
            self, info: Dict[str, Any], directory: str = "", page: int = 1
    ) -> Dict[str, Any]:
        """读取分享目录；空目录表示根目录。"""
        params = {
            "shorturl": info["share_id"],
            "dir": directory or "/",
            "root": 0 if directory else 1,
            "web": 5,
            "page": max(1, int(page or 1)),
            "num": self.page_size,
            "order": "name",
            "desc": 0,
            "showempty": 1,
            "view_mode": 1,
            "channel": "chunlei",
        }
        response = self.client.request("GET", "/share/list", params=params)
        if (
                not self.client.is_success(response)
                and safe_int(response.get("errno")) == -9
                and info.get("password")
        ):
            self._verify_password(info)
            response = self.client.request("GET", "/share/list", params=params)
        if not self.client.is_success(response):
            self._handle_risk(response)
            raise RuntimeError(response.get("message") or "读取百度分享失败")
        data = dict(response)
        data.setdefault("has_more", False)
        if not isinstance(data.get("list"), list):
            raise RuntimeError("百度分享未返回文件列表，请检查分享状态")
        return data

    def _access(self, share_url: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
        info = self.extract_share_info(share_url)
        if not info:
            raise ValueError("无效的百度网盘分享链接")
        key = self._share_key(info)
        context = self._shares.get(key)
        if context is None:
            root = self._listing_request(info)
            context = {
                "shareid": str(root.get("share_id") or ""),
                "share_uk": str(root.get("uk") or ""),
                "seckey": self.client.cookie_value("BDCLND"),
                "root": root,
                "items": {},
            }
            self._shares.set(key, context)
        return info, context

    @staticmethod
    def _shared_file(
            raw: Dict[str, Any], parent_path: str, parent_id: str
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        fs_id = str(raw.get("fs_id") or raw.get("fid") or "").strip()
        name = str(raw.get("server_filename") or raw.get("name") or "").strip()
        if not fs_id or not name:
            return None
        is_dir = safe_int(raw.get("isdir")) == 1
        path = str(raw.get("path") or "").strip()
        if not path:
            path = str(PurePosixPath(parent_path) / name)
        relative = str(PurePosixPath(parent_path) / name)
        return {
            "id": fs_id,
            "name": name,
            "size": 0 if is_dir else safe_int(raw.get("size")),
            "is_dir": is_dir,
            "md5": "" if is_dir else str(raw.get("md5") or ""),
            "parent_path": parent_path,
            "relative_path": relative,
            "path": path,
            "parent_id": parent_id,
            "raw": raw,
        }

    def _directory(self, share_url: str, parent_id: str = "") -> List[Dict[str, Any]]:
        info, context = self._access(share_url)
        parent = None
        if parent_id and parent_id != "0":
            parent = context["items"].get(str(parent_id))
            if not parent or not parent.get("is_dir"):
                raise ValueError("百度分享目录已失效，请重新打开分享预览")
        remote_path = str(parent.get("path") or "") if parent else ""
        parent_path = str(parent.get("relative_path") or "") if parent else ""
        result: List[Dict[str, Any]] = []
        page = 1
        while True:
            data = (
                context["root"]
                if not remote_path and page == 1
                else self._listing_request(info, remote_path, page)
            )
            rows = data.get("list") or []
            for raw in rows:
                item = self._shared_file(raw, parent_path, parent_id or "")
                if not item:
                    continue
                context["items"][item["id"]] = item
                result.append({
                    key: value for key, value in item.items() if key != "raw"
                })
            more = data.get("has_more")
            if (
                    not rows
                    or (more is not None and str(more).lower() in {"0", "false"})
                    or (more is None and len(rows) < self.page_size)
            ):
                return result
            page += 1

    def list_share_directory(self, share_url: str, parent_id: str = "") -> list:
        return self._directory(share_url, parent_id)

    def list_share_files(self, share_url: str, **kwargs: Any) -> list:
        result: List[Dict[str, Any]] = []
        stack: List[str] = [""]
        visited: set[str] = set()
        target_season = kwargs.get("target_season")
        stop_requested = kwargs.get("stop_requested")
        while stack:
            if stop_requested and stop_requested():
                raise InterruptedError("百度分享读取已停止")
            parent_id = stack.pop()
            if parent_id in visited:
                continue
            visited.add(parent_id)
            for item in self._directory(share_url, parent_id):
                if item["is_dir"]:
                    if should_skip_season_dir(item["name"], target_season):
                        continue
                    stack.append(item["id"])
                    continue
                result.append({
                    "id": item["id"],
                    "name": item["name"],
                    "size": item["size"],
                    "is_dir": False,
                    "md5": item["md5"],
                    "parent_path": item["parent_path"],
                    "relative_path": item["relative_path"],
                    "path": item["path"],
                    "parent_id": item["parent_id"],
                })
        return result

    def check_share_status(self, share_url: str) -> ShareLinkStatus:
        status = ShareLinkStatus()
        try:
            rows = self._directory(share_url)
            status.is_valid = True
            status.file_count = len(rows)
        except Exception as error:
            message = str(error)
            status.error_message = message
            status.is_expired = any(
                marker in message for marker in ("过期", "失效")
            )
            status.is_cancelled = "取消" in message
            status.is_deleted = any(
                marker in message for marker in ("删除", "不存在")
            )
        return status

    def _save(
            self, share_url: str, file_ids: List[str], save_path: str
    ) -> List[str]:
        if self.transfer_risk_blocked:
            raise RuntimeError("百度分享转存处于风控冷却期，请稍后重试")
        if is_share_expired(share_url):
            raise RuntimeError("百度分享已确认失效，24小时内跳过转存")
        info, context = self._access(share_url)
        if not file_ids:
            file_ids = [item["id"] for item in self._directory(share_url)]
        if not file_ids:
            raise RuntimeError("百度分享为空，无法转存")
        if any(str(file_id) not in context["items"] for file_id in file_ids):
            raise ValueError("所选文件已不在百度分享中，请刷新后重试")
        shareid = str(context.get("shareid") or "")
        share_uk = str(context.get("share_uk") or "")
        sekey_raw = str(context.get("seckey") or "")
        if not sekey_raw and info.get("password"):
            sekey_raw = self._verify_password(info)
            context["seckey"] = sekey_raw
        if not shareid or not share_uk:
            raise RuntimeError("百度分享缺少转存凭据，请刷新分享或更新 Cookie")
        lookup = self._files.resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            raise RuntimeError("百度网盘转存目录不可用")
        # Cookie 保留原编码，sekey 参数再解码。
        params = {
            "shareid": shareid,
            "from": share_uk,
            "ondup": "fail",
            "async": 1,
        }
        data = {
            "fsidlist": json.dumps(
                [int(file_id) for file_id in file_ids],
                separators=(",", ":"),
            ),
            "path": lookup.directory_id,
        }
        if sekey_raw:
            self.client.set_cookie("BDCLND", sekey_raw)
            sekey = unquote(sekey_raw)
            params["sekey"] = sekey
            data["extra"] = json.dumps({"sekey": sekey}, separators=(",", ":"))
        response = self.client.request(
            "POST",
            "/share/transfer",
            params=params,
            data=data,
            headers={
                "User-Agent": "netdisk",
                "Referer": info.get("url") or "https://pan.baidu.com/",
            },
        )
        try:
            if not self.client.is_success(response):
                self._handle_risk(response)
                message = str(response.get("message") or "百度分享转存失败")
                if is_expired_share_error(message=message):
                    mark_share_expired(share_url, message)
                raise RuntimeError(message)
            task_id = str(
                response.get("taskid") or response.get("task_id") or ""
            ).strip()
            if task_id and task_id != "0" and not wait_task(self.client, task_id):
                raise RuntimeError("百度分享转存任务未成功完成")
            failed = {
                str(row.get("fs_id") or row.get("from_fs_id") or "")
                for row in (response.get("info") or [])
                if isinstance(row, dict) and safe_int(row.get("errno")) != 0
            }
            if failed:
                raise RuntimeError("百度分享部分文件转存失败，请检查目标目录")
            return [str(file_id) for file_id in file_ids]
        finally:
            self._files.refresh_directories()

    def transfer_share(self, share_url: str, save_path: str) -> bool:
        ids = [item["id"] for item in self._directory(share_url)]
        if not ids:
            logger.warning("百度分享为空，无法转存")
            return False
        succeeded, failed = self.transfer_files_batch(share_url, ids, save_path)
        return bool(succeeded) and not failed

    def transfer_file(
            self,
            share_url: str,
            file_id: str,
            save_path: str,
            target_name: str = "",
            **kwargs: Any,
    ) -> bool:
        file_id = str(file_id or "").strip()
        if not file_id:
            return False
        try:
            info, context = self._access(share_url)
            self._save(share_url, [file_id], save_path)
            original_name = str(
                (context["items"].get(file_id) or {}).get("name") or ""
            )
            if target_name and original_name and target_name != original_name:
                item = self._files.find_file(save_path, original_name)
                if item and not self._files.rename_file(save_path, item, target_name):
                    logger.warning(
                        f"百度网盘转存后重命名失败，保留原名："
                        f"{original_name} -> {target_name}"
                    )
            return True
        except (ValueError, RuntimeError) as error:
            self.last_error = str(error)
            if is_expired_share_error(message=self.last_error):
                mark_share_expired(share_url, self.last_error)
            logger.warning(f"百度网盘单文件转存失败：{error}")
            if target_name and self._files.find_file(save_path, target_name):
                return True
            return False

    def transfer_files_batch(
            self, share_url: str, file_ids: list, save_path: str, **kwargs: Any
    ) -> tuple:
        values = list(dict.fromkeys(
            str(file_id) for file_id in (file_ids or [])
            if str(file_id or "").strip()
        ))
        if not values:
            return [], []
        size = max(1, min(safe_int(kwargs.get("batch_size", 20)), 100))
        interval = max(0.0, min(float(kwargs.get("batch_interval", 3) or 0), 60.0))
        stop_requested = kwargs.get("stop_requested")
        succeeded: List[str] = []
        failed: List[str] = []
        self.last_error = ""
        for offset in range(0, len(values), size):
            if stop_requested and stop_requested():
                failed.extend(values[offset:])
                break
            batch = values[offset:offset + size]
            try:
                succeeded.extend(self._save(share_url, batch, save_path))
            except (ValueError, RuntimeError) as error:
                self.last_error = str(error)
                if is_expired_share_error(message=self.last_error):
                    mark_share_expired(share_url, self.last_error)
                failed.extend(batch)
                if self.transfer_risk_blocked:
                    failed.extend(values[offset + size:])
                    break
            if offset + size < len(values):
                time.sleep(interval)
        return succeeded, failed
