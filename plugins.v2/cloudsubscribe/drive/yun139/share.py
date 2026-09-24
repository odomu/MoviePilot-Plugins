"""移动云盘分享浏览、直链解析与转存。"""

from __future__ import annotations

import base64
import json
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote, unquote

from Crypto.Cipher import AES
from app.log import logger

from .client import Yun139ApiError
from ..common import safe_int
from ...core.cloud import CloudFile, ShareLinkStatus
from ...core.transfer import HttpFileDownloadService

SHARE_BASE_URL = "https://share-kd-njs.yun.139.com/yun-share"
SHARE_LIST_PATH = "/richlifeApp/devapp/IOutLink/getOutLinkInfoV6"
SHARE_CONTENT_PATH = "/richlifeApp/devapp/IOutLink/getContentInfoFromOutLink"
SHARE_DOWNLOAD_PATH = "/richlifeApp/devapp/IOutLink/dlFromOutLinkV3"
SHARE_AES_KEY = b"PVGDwmcvfs1uV3d1"
ROOT_NODE_ID = "root"
#: 分享链接中的分享标识：/w/i/<id>、linkID=<id> 与 <id>#提取码。
_SHARE_ID_PATTERNS = (
    re.compile(r"/(?:w|s)/i/([A-Za-z0-9]+)", re.I),
    re.compile(r"[?&](?:linkID|link_id|shareId|sID)=([A-Za-z0-9]+)", re.I),
)
_PASSWORD_PATTERNS = (
    re.compile(r"(?:提取码|密码|pwd|passwd|code)\s*[：:=]?\s*([A-Za-z0-9]{1,16})", re.I),
    re.compile(r"[?&](?:pwd|passwd|password|code)=([A-Za-z0-9]{1,16})", re.I),
)


class Yun139ShareService:
    """封装移动云盘分享接口、文件遍历与转存。"""

    def __init__(self, client: Any, files: Any, upload: Any):
        self.client = client
        self.files = files
        self.upload = upload
        #: 分享节点 → 名称/大小，转存时用于兜底命名与进度展示。
        self._node_info: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def encode_node_id(link_id: str, password: str, node_id: str) -> str:
        """把分享标识、提取码与节点 ID 编码为可自解释的文件 ID。"""
        return "|".join((
            quote(str(link_id or ""), safe=""),
            quote(str(password or ""), safe=""),
            quote(str(node_id or ROOT_NODE_ID), safe=""),
        ))

    @staticmethod
    def decode_node_id(value: str) -> Tuple[str, str, str]:
        parts = str(value or "").split("|", 2)
        if len(parts) != 3:
            raise ValueError("移动云盘分享文件标识无效")
        link_id, password, node_id = (unquote(part) for part in parts)
        if not link_id:
            raise ValueError("移动云盘分享文件标识缺少分享 ID")
        return link_id, password, node_id or ROOT_NODE_ID

    @staticmethod
    def extract_share_info(share_url: str) -> Dict[str, Any]:
        """从分享链接中解析分享 ID 与提取码。"""
        text = str(share_url or "").strip()
        if not text:
            return {}
        link_id = ""
        for pattern in _SHARE_ID_PATTERNS:
            matched = pattern.search(text)
            if matched:
                link_id = matched.group(1)
                break
        if not link_id:
            # 兼容配置里的「分享ID#提取码」写法。
            head = text.split("#", 1)[0].split("?", 1)[0].strip()
            if re.fullmatch(r"[A-Za-z0-9]{6,32}", head):
                link_id = head
        if not link_id:
            return {}
        password = ""
        for pattern in _PASSWORD_PATTERNS:
            matched = pattern.search(text)
            if matched:
                password = matched.group(1)
                break
        if not password and "#" in text:
            tail = text.rsplit("#", 1)[1].strip()
            if re.fullmatch(r"[A-Za-z0-9]{1,16}", tail):
                password = tail
        return {"link_id": link_id, "password": password}

    def _share_headers(self) -> Dict[str, str]:
        authorization = self.client.authorization
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 "
                "Firefox/140.0"
            ),
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "X-Deviceinfo": "||9|12.27.0|firefox|140.0|||linux unknown|1920X526|zh-CN|||",
            "hcy-cool-flag": "1",
            "CMS-DEVICE": "default",
            "x-m4c-caller": "PC",
            "X-Yun-Api-Version": "v1",
            "Origin": self.client.WEB_ORIGIN,
            "Referer": self.client.WEB_ORIGIN + "/",
        }
        if authorization:
            headers["Authorization"] = f"Basic {authorization}"
        return headers

    @staticmethod
    def _encrypt(payload: Dict[str, Any]) -> str:
        """请求体整体加密：排序 JSON → AES-128-CBC/PKCS7 → base64(IV + 密文)。"""
        plain = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        pad_len = AES.block_size - len(plain) % AES.block_size
        iv = secrets.token_bytes(AES.block_size)
        cipher = AES.new(SHARE_AES_KEY, AES.MODE_CBC, iv)
        return base64.b64encode(iv + cipher.encrypt(plain + bytes([pad_len]) * pad_len)).decode("ascii")

    @staticmethod
    def _decrypt(raw: bytes) -> Dict[str, Any]:
        """响应可能是明文 JSON，也可能是 base64(IV + 密文)。"""
        text = bytes(raw or b"").strip()
        if not text:
            raise Yun139ApiError("移动云盘分享接口返回空响应")
        if text[:1] == b"{":
            payload = json.loads(text.decode("utf-8"))
        else:
            decoded = base64.b64decode(text + b"=" * (-len(text) % 4))
            if len(decoded) <= AES.block_size:
                raise Yun139ApiError("移动云盘分享响应密文长度异常")
            iv, cipher_text = decoded[:AES.block_size], decoded[AES.block_size:]
            plain = AES.new(SHARE_AES_KEY, AES.MODE_CBC, iv).decrypt(cipher_text)
            pad_len = plain[-1] if plain else 0
            if 0 < pad_len <= AES.block_size:
                plain = plain[:-pad_len]
            start, end = plain.find(b"{"), plain.rfind(b"}")
            if start < 0 or end <= start:
                raise Yun139ApiError("移动云盘分享响应解密失败")
            payload = json.loads(plain[start:end + 1].decode("utf-8"))
        if not isinstance(payload, dict):
            raise Yun139ApiError("移动云盘分享响应结构异常")
        return payload

    def _share_post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """调用分享接口并返回响应信封。"""
        payload = self.client.post_encrypted(
            SHARE_BASE_URL + path,
            self._encrypt(body),
            self._share_headers(),
        )
        return self._decrypt(payload)

    @staticmethod
    def _check_envelope(payload: Dict[str, Any], action: str) -> Dict[str, Any]:
        success = payload.get("success")
        code = str(payload.get("code") or "").strip()
        if success is False and code not in {"0", "0000", ""}:
            message = str(
                payload.get("message") or payload.get("msg") or ""
            ).strip()
            raise Yun139ApiError(
                f"移动云盘{action}失败（{code}）：{message or '未知原因'}", code=code
            )
        data = payload.get("data")
        return data if isinstance(data, dict) else payload

    def _require_share(self, share_url: str) -> Tuple[str, str]:
        info = self.extract_share_info(share_url)
        if not info:
            raise ValueError("无效的移动云盘分享链接")
        return str(info["link_id"]), str(info["password"])

    def _list_node(
            self, link_id: str, password: str, node_id: str = ROOT_NODE_ID
    ) -> Tuple[list, list]:
        """列出分享中某个目录的子目录与文件。"""
        payload = self._share_post(SHARE_LIST_PATH, {
            "getOutLinkInfoReq": {
                "account": self.client.account,
                "linkID": link_id,
                "passwd": password,
                "pCaID": node_id or ROOT_NODE_ID,
            },
        })
        data = self._check_envelope(payload, "读取分享目录")
        folders = [
            item for item in (data.get("caLst") or []) if isinstance(item, dict)
        ]
        files = [
            item for item in (data.get("coLst") or []) if isinstance(item, dict)
        ]
        return folders, files

    @staticmethod
    def _modified_at(value: Any) -> str:
        """分享接口的时间格式为 20060102150405，转换为 ISO 便于展示。"""
        text = str(value or "").strip()
        if len(text) != 14 or not text.isdigit():
            return ""
        return (
            f"{text[0:4]}-{text[4:6]}-{text[6:8]}"
            f"T{text[8:10]}:{text[10:12]}:{text[12:14]}"
        )

    def _share_node(
            self, link_id: str, password: str, entry: Dict[str, Any], is_directory: bool
    ) -> Optional[CloudFile]:
        node_id = str(
            (entry.get("caId") if is_directory else entry.get("coId")) or ""
        ).strip()
        name = str(
            (entry.get("caName") if is_directory else entry.get("coName")) or ""
        ).strip()
        if not node_id or not name:
            return None
        item = CloudFile(
            self.encode_node_id(link_id, password, node_id),
            name,
            is_directory,
            size=safe_int(entry.get("coSize")),
            native={
                "node_id": node_id,
                "modified_at": self._modified_at(entry.get("udTime")),
            },
        )
        self._node_info[item.id] = {
            "name": name,
            "size": item.size,
            "node_id": node_id,
        }
        return item

    def check_share_status(self, share_url: str) -> ShareLinkStatus:
        """校验分享是否可访问，并带回分享名称与顶层文件数。"""
        status = ShareLinkStatus()
        try:
            link_id, password = self._require_share(share_url)
            folders, files = self._list_node(link_id, password)
            status.is_valid = True
            status.share_info = {
                "share_title": str(
                    (files[0].get("lkName") if files else "")
                    or (folders[0].get("lkName") if folders else "")
                    or ""
                ),
                "password": password,
            }
            status.file_count = len(folders) + len(files)
        except Exception as error:
            status.error_message = str(error)
        return status

    def list_share_directory(
            self, share_url: str, parent_id: str = ""
    ) -> list:
        """列出分享目录的当前层，预览调用方需要保留目录节点与错误。"""
        if parent_id:
            link_id, password, node_id = self.decode_node_id(parent_id)
        else:
            link_id, password = self._require_share(share_url)
            node_id = ROOT_NODE_ID
        folders, files = self._list_node(link_id, password, node_id)
        items = []
        for entry in folders:
            node = self._share_node(link_id, password, entry, True)
            if node:
                items.append(dict(node))
        for entry in files:
            node = self._share_node(link_id, password, entry, False)
            if node:
                items.append(dict(node))
        return items

    def list_share_files(self, share_url: str, **kwargs: Any) -> list:
        """递归遍历分享中的全部文件，供候选识别与转存使用。"""
        try:
            link_id, password = self._require_share(share_url)
            result: list = []
            stack = [(ROOT_NODE_ID, "")]
            while stack:
                node_id, parent_path = stack.pop()
                folders, files = self._list_node(link_id, password, node_id)
                for entry in folders:
                    node = self._share_node(link_id, password, entry, True)
                    if node:
                        dir_name = str(node.name or "").strip()
                        sub_path = f"{parent_path}/{dir_name}".strip("/") if parent_path else dir_name
                        stack.append((str(node.native.get("node_id") or ""), sub_path))
                for entry in files:
                    node = self._share_node(link_id, password, entry, False)
                    if node:
                        item_dict = dict(node)
                        if parent_path:
                            item_dict["parent_path"] = parent_path
                            item_dict["relative_path"] = f"{parent_path}/{node.name}"
                        result.append(item_dict)
            return result
        except Exception as error:
            logger.warning(f"读取移动云盘分享文件失败：{error}")
            return []

    def _resolve_download_url(self, link_id: str, password: str, node_id: str) -> str:
        """取分享文件直链：CDN 地址优先，其次重定向地址与源站地址。"""
        content = self._check_envelope(
            self._share_post(SHARE_CONTENT_PATH, {
                "getContentInfoFromOutLinkReq": {
                    "contentId": node_id,
                    "linkID": link_id,
                    "passwd": password,
                    "account": self.client.account,
                },
            }),
            "读取分享文件信息",
        )
        info = content.get("contentInfo")
        info = info if isinstance(info, dict) else {}
        payload = self._check_envelope(
            self._share_post(SHARE_DOWNLOAD_PATH, {
                "dlFromOutLinkReqV3": {
                    "account": self.client.account,
                    "linkID": link_id,
                    "passwd": password,
                    "coIDLst": {"item": [node_id]},
                },
            }),
            "获取分享下载地址",
        )
        ext_info = payload.get("extInfo")
        ext_info = ext_info if isinstance(ext_info, dict) else {}
        for candidate in (
                ext_info.get("cdnDownloadUrl"),
                payload.get("redrUrl"),
                payload.get("downloadURL"),
                info.get("cdnDownLoadUrl"),
                info.get("presentURL"),
        ):
            url = str(candidate or "").strip()
            if url:
                return url
        raise Yun139ApiError("移动云盘分享文件未返回下载地址")

    def _download_to_local(
            self, item: CloudFile, target: Path, stop_requested=None
    ) -> Path:
        link_id, password, node_id = self.decode_node_id(item.id)
        url = self._resolve_download_url(link_id, password, node_id)
        headers = {
            "User-Agent": self.client.USER_AGENT,
            "Referer": self.client.WEB_ORIGIN + "/",
            "Origin": self.client.WEB_ORIGIN,
        }
        return HttpFileDownloadService(
            lambda _: (url, headers),
        ).download_file(item, str(target), stop_requested=stop_requested)

    def _save_share_file(
            self,
            file_id: str,
            save_path: str,
            target_name: str = "",
            stop_requested=None,
    ) -> bool:
        """把一个分享文件转存到本盘：下载到本地临时文件后上传。"""
        known = self._node_info.get(str(file_id), {})
        name = str(target_name or known.get("name") or "").strip()
        item = CloudFile(
            str(file_id),
            name or "share-file",
            False,
            size=safe_int(known.get("size")),
        )
        workdir = Path(tempfile.mkdtemp(prefix="yun139-share-"))
        try:
            local_path = self._download_to_local(
                item, workdir / (name or "share-file"), stop_requested
            )
            uploaded = self.upload.upload_file(
                str(local_path), save_path, name or Path(local_path).name
            )
            if not uploaded:
                logger.warning(
                    f"移动云盘分享转存上传失败：{name or file_id} -> {save_path}"
                )
            return bool(uploaded)
        except Exception as error:
            logger.warning(f"移动云盘分享转存失败：{name or file_id}：{error}")
            return False
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def transfer_share(self, share_url: str, save_path: str) -> bool:
        """把分享中的全部文件转存到本盘对应目录结构下。"""
        files = self.list_share_files(share_url)
        if not files:
            logger.warning("移动云盘分享中没有可转存的文件")
            return False
        all_succeeded = True
        for item in files:
            file_id = str(item.get("id") or "")
            if not file_id:
                continue
            parent_path = str(item.get("parent_path") or "").strip("/").strip()
            target_dir = f"{save_path.rstrip('/')}/{parent_path}" if parent_path else save_path
            if not self.transfer_file(share_url, file_id, target_dir, target_name=item.get("name") or ""):
                all_succeeded = False
        return all_succeeded

    def transfer_file(
            self, share_url: str, file_id: str, save_path: str,
            target_name: str = "", **kwargs: Any,
    ) -> bool:
        """转存单个分享文件；已存在同名文件时视为成功。"""
        node_id = str(file_id or "").strip()
        if not node_id:
            return False
        success = self._save_share_file(
            node_id, save_path, target_name, kwargs.get("stop_requested")
        )
        if success:
            return True
        name = str(target_name or self._node_info.get(node_id, {}).get("name") or "")
        if name and self.files.find_file(save_path, name):
            return True
        return False

    def transfer_files_batch(
            self, share_url: str, file_ids: list, save_path: str, **kwargs: Any
    ) -> tuple:
        """顺序转存多个分享文件，返回成功与失败列表。"""
        interval = max(0.0, min(float(kwargs.get("batch_interval", 0) or 0), 60.0))
        stop_requested = kwargs.get("stop_requested")
        succeeded: list = []
        failed: list = []
        for index, file_id in enumerate(
                dict.fromkeys(str(value) for value in (file_ids or []))
        ):
            if stop_requested and stop_requested():
                failed.extend(
                    str(value)
                    for value in list(file_ids)[index:]
                    if str(value) not in succeeded
                )
                break
            if self.transfer_file(share_url, file_id, save_path):
                succeeded.append(file_id)
            else:
                failed.append(file_id)
                if interval and index + 1 < len(file_ids):
                    time.sleep(interval)
        return succeeded, failed
