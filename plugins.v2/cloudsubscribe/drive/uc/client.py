"""UC 网盘 HTTP 客户端，仅包含 CloudSubscribe 使用的接口。

协议形态与夸克 clouddrive 一致，但域名、渠道标识与 UA 不同：
文件接口走 ``pc-api.uc.cn``，网页会话域名 ``drive.uc.cn`` 的 ``/1/clouddrive``
直连会返回 403 csrf，因此分享相关接口同样使用 ``pc-api.uc.cn``。
"""

from __future__ import annotations

import random
import time
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
    label = {
        "NORMAL": "非会员",
        "VIP": "会员",
        "SVIP": "超级会员",
    }.get(member_type, member_type or "非会员")
    return {
        "is_vip": bool(member_type and member_type != "NORMAL"),
        "label": label,
        "expire_date": _format_expire_date(
            member.get("exp_at") or member.get("exp_svip_exp_at")
        ),
    }


class UCClient:
    BASE_URL = "https://pc-api.uc.cn/1/clouddrive"
    SHARE_PAGE_BASE_URL = "https://pc-api.uc.cn/1/clouddrive"
    SHARE_BASE_URL = "https://pc-api.uc.cn/1/clouddrive"
    DEFAULT_PARAMS = {"pr": "UCBrowser", "fr": "pc", "uc_param_str": ""}
    DEFAULT_HEADERS = {
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) uc-cloud-drive/2.5.20 Chrome/100.0.4896.160 "
            "Electron/18.3.5.4-b478491100 Safari/537.36 Channel/pckk_other_ch"
        ),
        "referer": "https://drive.uc.cn/",
        "origin": "https://drive.uc.cn",
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
    }
    #: 扫码登录走 UC 统一账号；519 是网盘站点自身 client_id（抓包里 _UP_28A_52_=519）。
    QR_API_URL = "https://api.open.uc.cn"
    QR_CLIENT_ID = "519"
    QR_CONFIRM_URL = "https://su.uc.cn/1_n0ZCv"
    QR_UC_PARAM_STR = "dsdnfrpfbivesscpgimibtbmnijblauputogpintnwktprchmt"
    QR_BIZ_STR = "S:custom|C:titlebar_fix"
    #: 扫码组件实测抓包：以 broccoli.uc.cn 为来源、credentials=omit 的表单 POST。
    QR_HEADERS = {
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
        ),
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "cache-control": "no-cache",
        "pragma": "no-cache",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://broccoli.uc.cn",
        "referer": "https://broccoli.uc.cn/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "sec-ch-ua": '"Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
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
            "uc", self._cookie, min_interval=0.5
        )
        self.risk_cooldown = 1800
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
            logger.error(f"UC网盘请求失败：{endpoint} - {error}")
            return {"status": -1, "code": -1, "message": str(error), "data": {}}

    @classmethod
    def _post_qr(cls, endpoint: str, data: Dict[str, str]) -> Dict[str, Any]:
        """按扫码组件实测抓包调用统一账号接口：表单 POST、__t/__dt 防缓存、不带 Cookie。"""
        body = {
            "client_id": cls.QR_CLIENT_ID,
            "v": "1.2",
            "request_id": str(int(time.time() * 1000)),
            **data,
        }
        response = cls._login_rate_limiter.call(
            requests.post,
            f"{cls.QR_API_URL}{endpoint}",
            params={"__t": int(time.time() * 1000), "__dt": random.randint(100, 99999)},
            data=body,
            headers=cls.QR_HEADERS,
            timeout=30,
            retry_exceptions=(requests.Timeout, requests.ConnectionError),
        )
        response.raise_for_status()
        return response.json()

    @classmethod
    def create_qrcode_login(cls, client_type: str = "") -> Dict[str, Any]:
        """申请 UC 扫码登录二维码（手机 UC 浏览器扫描）。"""
        payload = cls._post_qr("/cas/ajax/getTokenForQrcodeLogin", {})
        token = str(((payload.get("data") or {}).get("members") or {}).get("token") or "")
        if payload.get("status") != 2000000 or not token:
            raise RuntimeError(payload.get("message") or "UC二维码令牌获取失败")
        query = urlencode({
            "uc_param_str": cls.QR_UC_PARAM_STR,
            "token": token,
            "client_id": cls.QR_CLIENT_ID,
            "uc_biz_str": cls.QR_BIZ_STR,
        })
        return {
            "qr_token": token,
            "qr_url": f"{cls.QR_CONFIRM_URL}?{query}",
            "expires_in": 300,
        }

    @classmethod
    def check_qrcode_login(cls, **kwargs: Any) -> Dict[str, Any]:
        token = str(kwargs.get("qr_token") or "").strip()
        if not token:
            raise ValueError("缺少 UC 二维码令牌")
        payload = cls._post_qr(
            "/cas/ajax/getServiceTicketByQrcodeToken", {"token": token}
        )
        status = payload.get("status")
        members = (payload.get("data") or {}).get("members") or {}
        ticket = str(members.get("service_ticket") or "") if isinstance(members, dict) else ""
        if status == 2000000 and ticket:
            return cls._exchange_ticket(ticket)
        if status in (50004002, 50004003, 50004004):
            return {"status": "expired", "message": "二维码已失效，请刷新"}
        return {"status": "waiting", "message": "等待手机 UC 浏览器扫码确认"}

    @classmethod
    def _exchange_ticket(cls, ticket: str) -> Dict[str, Any]:
        """用 service_ticket 换登录 Cookie：先取站点 ctoken，再带票据请求 account/info。"""
        session = requests.Session()
        session.headers.update(cls.QR_HEADERS)
        session.headers.pop("content-type", None)
        session.headers.pop("origin", None)
        session.headers["referer"] = "https://drive.uc.cn/"
        session.headers["accept"] = "application/json, text/plain, */*"
        try:
            cls._login_rate_limiter.call(
                session.get, "https://drive.uc.cn/",
                timeout=30, retry_exceptions=(requests.Timeout, requests.ConnectionError),
            )
            response = cls._login_rate_limiter.call(
                session.get,
                "https://drive.uc.cn/account/info",
                params={"st": ticket},
                timeout=30,
                retry_exceptions=(requests.Timeout, requests.ConnectionError),
            )
            try:
                exchange = response.json()
            except ValueError:
                exchange = {}
            if response.status_code >= 400:
                return {"status": "expired", "message": f"UC扫码换票失败：HTTP {response.status_code}"}
            if not exchange.get("success"):
                logger.warning(f"UC扫码换票失败：{exchange}")
                return {
                    "status": "expired",
                    "message": (
                            "扫码已确认，但换票失败："
                            + str(exchange.get("code") or "UNKNOWN") + " "
                            + str(exchange.get("msg") or exchange.get("message") or "")
                    ).strip(),
                }
            cookies = "; ".join(f"{i.name}={i.value}" for i in session.cookies)
            probe = cls(cookie=cookies, timeout=15)
            try:
                valid = probe.check_login()
            finally:
                probe.close()
            if valid:
                return {"status": "success", "message": "登录成功", "cookie": cookies}
            names = [i.name for i in session.cookies]
            return {
                "status": "expired",
                "message": "扫码已确认，但登录态校验失败；已获得 Cookie：" + (", ".join(names) or "空"),
            }
        finally:
            session.close()

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

    def check_login(self) -> bool:
        return self.is_success(self.get_member_info())

    def get_account_info(self) -> Dict[str, Any]:
        if not self.cookie:
            return {"connected": False, "error": "请填写 UC Cookie"}
        member_response = self.get_member_info()
        if not self.is_success(member_response):
            message = str(member_response.get("message") or "Cookie 已失效")
            if "require login" in message.lower():
                names = [part.split("=", 1)[0] for part in self.cookie.split("; ") if "=" in part]
                message = (
                    f"{message}；当前 Cookie 缺少登录凭据（__puus），"
                    f"现有：{', '.join(names) or '空'}"
                )
            return {"connected": False, "error": message}
        member = self.data(member_response)
        total = safe_int(member.get("total_capacity") or member.get("total"))
        used = safe_int(member.get("use_capacity") or member.get("used"))
        profile = _member_profile(member)
        return {
            "connected": True,
            "user": {
                "name": str(
                    member.get("nickname") or member.get("nick_name") or "UC用户"
                ),
                "avatar": str(member.get("avatar_url") or member.get("avatar") or ""),
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
