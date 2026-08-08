"""夸克网盘 HTTP 客户端，仅包含 CloudSubscribe 使用的接口。"""

from __future__ import annotations

import random
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlencode

import requests
from app.log import logger

from ..common import DriveRateLimiter, format_size, safe_int


def _format_expire_date(value: Any) -> str:
    timestamp = safe_int(value)
    if timestamp <= 0:
        return ""
    if timestamp > 10_000_000_000:
        timestamp //= 1000
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")


def _member_profile(member: Dict[str, Any]) -> Dict[str, Any]:
    member_type = str(member.get("member_type") or "").upper()
    identity = next((
        item for item in (member.get("identity") or [])
        if isinstance(item, dict)
           and item.get("status") in (1, "1", True)
           and (
                   item.get("user_identity_type") in (4, "4")
                   or "88_vip" in str(
               (item.get("extra") or {}).get("distribute_id") or ""
           ).lower()
           )
    ), None)
    if identity:
        label = "88VIP"
        expire_at = identity.get("expire_time")
    else:
        label = {
            "NORMAL": "非VIP",
            "VIP": "VIP",
            "SUPER_VIP": "SVIP",
            "EXP_SVIP": "体验SVIP",
            "Z_VIP": "88VIP",
            "MINI_VIP": "MINI VIP",
        }.get(member_type, member_type or "非VIP")
        expire_at = member.get("exp_at") or member.get("exp_svip_exp_at")
    return {
        "is_vip": bool(identity or (member_type and member_type != "NORMAL")),
        "label": label,
        "expire_date": _format_expire_date(expire_at),
    }


class QuarkClient:
    BASE_URL = "https://drive-pc.quark.cn/1/clouddrive"
    SHARE_PAGE_BASE_URL = "https://drive-h.quark.cn/1/clouddrive"
    SHARE_BASE_URL = "https://drive.quark.cn/1/clouddrive"
    PAN_CLOUDDRIVE_URL = "https://pan.quark.cn/1/clouddrive"
    ACCOUNT_URL = "https://pan.quark.cn/account"
    QR_LOGIN_URL = "https://uop.quark.cn/cas/ajax"
    DEFAULT_PARAMS = {"pr": "ucpro", "fr": "pc", "uc_param_str": ""}
    DEFAULT_HEADERS = {
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/94.0.4606.71 Safari/537.36 "
            "Core/1.94.225.400 QQBrowser/12.2.5544.400"
        ),
        "referer": "https://pan.quark.cn/",
        "origin": "https://pan.quark.cn",
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
    }
    _login_rate_limiter = DriveRateLimiter(min_interval=0.8)

    def __init__(
            self,
            cookie: str = "",
            on_cookie_refresh: Optional[Callable[[str], None]] = None,
            timeout: int = 30,
    ):
        self._cookie = str(cookie or "").strip()
        self._on_cookie_refresh = on_cookie_refresh
        self._timeout = max(5, int(timeout or 30))
        self.rate_limiter = DriveRateLimiter.shared(
            "quark", self._cookie, min_interval=0.5
        )
        self._session = requests.Session()
        self._session.headers.update(self.DEFAULT_HEADERS)

    @property
    def cookie(self) -> str:
        return self._cookie

    @property
    def request_timeout(self) -> int:
        return self._timeout

    def close(self) -> None:
        self._session.close()

    @staticmethod
    def is_success(response: Any) -> bool:
        if not isinstance(response, dict):
            return False
        status = response.get("status")
        code = response.get("code")
        return status in (200, "200", 2000000, "2000000", 0, "0", None) and code in (
            0, "0", 200, "200", None
        )

    @staticmethod
    def data(response: Any) -> Any:
        return response.get("data") or {} if isinstance(response, dict) else {}

    def _params(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = {
            **self.DEFAULT_PARAMS,
            "__t": int(time.time() * 1000),
            "__dt": random.randint(100, 9999),
        }
        if extra:
            params.update(extra)
        return params

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = dict(self.DEFAULT_HEADERS)
        if self._cookie:
            headers["cookie"] = self._cookie
        if extra:
            headers.update(extra)
        return headers

    def download_headers(
            self, extra: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        """返回文件下载所需的认证请求头，供文件服务使用。"""
        return self._headers(extra)

    def _refresh_cookie(self, response: requests.Response) -> None:
        current = {}
        for part in self._cookie.split(";"):
            if "=" in part:
                key, value = part.strip().split("=", 1)
                current[key] = value
        changed = False
        for key in ("__puus", "__pus"):
            value = response.cookies.get(key)
            if value:
                current[key] = value
                changed = True
        if not changed:
            return
        cookie = "; ".join(f"{key}={value}" for key, value in current.items())
        if cookie == self._cookie:
            return
        self._cookie = cookie
        if self._on_cookie_refresh:
            self._on_cookie_refresh(cookie)

    def request(
            self,
            method: str,
            endpoint: str,
            *,
            params: Optional[Dict[str, Any]] = None,
            json_data: Optional[Dict[str, Any]] = None,
            base_url: Optional[str] = None,
            request_headers: Optional[Dict[str, str]] = None,
            request_timeout: Any = None,
    ) -> Dict[str, Any]:
        url = f"{(base_url or self.BASE_URL).rstrip('/')}/{endpoint.lstrip('/')}"
        try:
            headers = self._headers(request_headers)
            if method.upper() == "GET":
                headers.pop("content-type", None)
            response = self.rate_limiter.call(
                self._session.request,
                method.upper(),
                url,
                params=self._params(params),
                json=json_data,
                headers=headers,
                timeout=(
                    request_timeout
                    if request_timeout is not None else self._timeout
                ),
                retry_exceptions=(requests.Timeout, requests.ConnectionError),
            )
            self._refresh_cookie(response)
            if response.status_code >= 400:
                try:
                    error = response.json()
                except ValueError:
                    error = {}
                return {
                    "status": error.get("status", response.status_code),
                    "code": error.get("code", response.status_code),
                    "http_status": response.status_code,
                    "message": (
                            error.get("message") or error.get("msg")
                            or response.text[:300]
                            or (
                                "Cookie 已失效" if response.status_code == 401
                                else f"HTTP {response.status_code}"
                            )
                    ),
                    "data": error,
                }
            return response.json() if response.text else {"status": 200, "code": 0, "data": {}}
        except (requests.RequestException, ValueError) as error:
            logger.error(f"夸克网盘请求失败：{endpoint} - {error}")
            return {"status": -1, "code": -1, "message": str(error), "data": {}}

    @classmethod
    def create_qrcode_login(cls, client_type: str = "") -> Dict[str, Any]:
        session = requests.Session()
        response = cls._login_rate_limiter.call(
            session.get,
            f"{cls.QR_LOGIN_URL}/getTokenForQrcodeLogin",
            params={"client_id": "532", "v": "1.2", "request_id": str(uuid.uuid4())},
            headers=cls.DEFAULT_HEADERS,
            timeout=30,
            retry_exceptions=(requests.Timeout, requests.ConnectionError),
        )
        response.raise_for_status()
        payload = response.json()
        token = (payload.get("data") or {}).get("members", {}).get("token")
        if payload.get("status") != 2000000 or not token:
            raise RuntimeError(payload.get("message") or "夸克二维码令牌获取失败")
        qr_url = "https://su.quark.cn/4_eMHBJ?" + urlencode({
            "token": token,
            "client_id": "532",
            "ssb": "weblogin",
            "uc_param_str": "",
            "uc_biz_str": "S:custom|OPT:SAREA@0|OPT:IMMERSIVE@1|OPT:BACK_BTN_STYLE@0",
        })
        return {"qr_token": token, "qr_url": qr_url, "expires_in": 300}

    @classmethod
    def check_qrcode_login(cls, **kwargs: Any) -> Dict[str, Any]:
        token = str(kwargs.get("qr_token") or "").strip()
        if not token:
            raise ValueError("缺少夸克二维码令牌")
        session = requests.Session()
        response = cls._login_rate_limiter.call(
            session.get,
            f"{cls.QR_LOGIN_URL}/getServiceTicketByQrcodeToken",
            params={
                "client_id": "532",
                "v": "1.2",
                "token": token,
                "request_id": str(uuid.uuid4()),
            },
            headers=cls.DEFAULT_HEADERS,
            timeout=30,
            retry_exceptions=(requests.Timeout, requests.ConnectionError),
        )
        if response.status_code != 200:
            return {"status": "waiting", "message": "等待扫码"}
        payload = response.json()
        status = payload.get("status")
        message = str(payload.get("message") or "")
        ticket = (payload.get("data") or {}).get("members", {}).get("service_ticket")
        if status == 2000000 and ticket:
            account_response = cls._login_rate_limiter.call(
                session.get,
                f"{cls.ACCOUNT_URL}/info",
                params={"st": ticket, "lw": "scan"},
                headers=cls.DEFAULT_HEADERS,
                timeout=30,
                retry_exceptions=(requests.Timeout, requests.ConnectionError),
            )
            account_response.raise_for_status()
            cookies = "; ".join(
                f"{item.name}={item.value}"
                for item in session.cookies
                if not item.domain or "quark.cn" in item.domain
            )
            if not cookies:
                raise RuntimeError("扫码成功但未获得夸克 Cookie")
            return {"status": "success", "message": "登录成功", "cookie": cookies}
        if status in (50004002, 50004003, 50004004):
            return {"status": "expired", "message": message or "二维码已失效"}
        return {"status": "waiting", "message": "等待扫码"}

    def get_user_info(self) -> Dict[str, Any]:
        if not self._cookie:
            return {"status": 401, "message": "未配置 Cookie", "data": {}}
        try:
            headers = self._headers()
            headers.pop("content-type", None)
            response = self.rate_limiter.call(
                self._session.get,
                f"{self.ACCOUNT_URL}/info",
                params={"fr": "pc", "platform": "pc"},
                headers=headers,
                timeout=self._timeout,
                retry_exceptions=(requests.Timeout, requests.ConnectionError),
            )
            self._refresh_cookie(response)
            if response.status_code >= 400 or "text/html" in response.headers.get("content-type", ""):
                return {"status": response.status_code, "message": "Cookie 已失效", "data": {}}
            result = response.json()
            if result.get("status") == 2000000:
                result["status"] = 200
            return result
        except (requests.RequestException, ValueError) as error:
            return {"status": -1, "message": str(error), "data": {}}

    def get_member_info(self) -> Dict[str, Any]:
        return self.request(
            "GET",
            "member",
            params={
                "fetch_subscribe": "true",
                "_ch": "home",
                "fetch_identity": "true",
            },
            base_url=self.BASE_URL,
        )

    def get_capacity(self) -> Dict[str, Any]:
        member = self.get_member_info()
        data = self.data(member)
        if self.is_success(member) and isinstance(data, dict) and (
                data.get("total_capacity") is not None or data.get("use_capacity") is not None
        ):
            return member
        return self.request("GET", "capacity")

    def check_login(self) -> bool:
        return self.is_success(self.get_member_info())

    def get_account_info(self) -> Dict[str, Any]:
        if not self.cookie:
            return {"connected": False, "error": "请填写 Cookie 或扫码登录"}
        member_response = self.get_member_info()
        if not self.is_success(member_response):
            return {
                "connected": False,
                "error": member_response.get("message") or "Cookie 已失效",
            }
        member = self.data(member_response)
        user_response = self.get_user_info()
        user = self.data(user_response) if self.is_success(user_response) else {}
        members = user.get("members") if isinstance(user, dict) else None
        if isinstance(members, dict):
            user = {**user, **members}
        total = safe_int(member.get("total_capacity") or member.get("total"))
        used = safe_int(member.get("use_capacity") or member.get("used"))
        profile = _member_profile(member)
        return {
            "connected": True,
            "user": {
                "name": str(
                    user.get("nickname") or user.get("nick_name")
                    or user.get("name") or "夸克用户"
                ),
                "avatar": str(user.get("avatar_url") or user.get("avatar") or ""),
                "is_vip": profile["is_vip"],
                "vip_label": profile["label"],
                "is_forever_vip": False,
                "vip_expire_date": profile["expire_date"],
            },
            "storage": {
                "total": format_size(total),
                "used": format_size(used),
                "remaining": format_size(max(0, total - used)),
            },
        }

    def get_task_status(self, task_id: str, retry_index: int = 0) -> Dict[str, Any]:
        return self.request("GET", "task", params={"task_id": task_id, "retry_index": retry_index})

    def wait_for_task(self, task_id: str, timeout: int = 60) -> bool:
        deadline = time.monotonic() + max(1, timeout)
        retry_index = 0
        while time.monotonic() < deadline:
            result = self.get_task_status(task_id, retry_index)
            status = (self.data(result) or {}).get("status")
            if status == 2:
                return True
            if status == 3:
                return False
            retry_index += 1
            time.sleep(1)
        return False
