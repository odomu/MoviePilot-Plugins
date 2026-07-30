"""聚影普通账号网页资源搜索客户端。"""

import threading
import time
from typing import Any, Callable, Dict, Optional

from app.log import logger

from ..http_client import (
    RequestGate,
    gated_idempotent_request,
    gated_request,
    normalize_proxies,
    requests,
)


class JuyingError(RuntimeError):
    """聚影登录、搜索或资源解析失败。"""

    def __init__(self, message: str, code: str = "juying_error"):
        super().__init__(message)
        self.code = code


class JuyingClient:
    """维护聚影 CSRF、登录令牌和受控 JSON 请求。"""

    BASE_URL = "https://www.jying.top"
    _SESSION_DATA_KEY = "juying_auth_session"
    _LOGIN_LOCK = threading.RLock()

    _HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "MoviePilot-CloudSubscribe-Juying/1.0",
    }

    def __init__(
            self,
            username: str,
            password: str,
            proxy: Any = None,
            request_timeout: int = 30,
            request_interval: float = 1.0,
            get_data_func: Optional[Callable] = None,
            save_data_func: Optional[Callable] = None,
    ):
        self.base_url = self.BASE_URL
        self.username = str(username or "").strip()
        self.password = str(password or "")
        self._proxies = normalize_proxies(proxy)
        self._request_timeout = max(5, min(int(request_timeout or 30), 60))
        self._session = requests.Session(impersonate="chrome")
        self._session.headers.update(self._HEADERS)
        self._token = ""
        self._get_data_func = get_data_func
        self._save_data_func = save_data_func
        self._lock = threading.RLock()
        self._circuit_open_until = 0.0
        self._request_gate = RequestGate(
            "聚影",
            request_interval=request_interval,
            minimum_interval=0.5,
        )
        self._restore_token()

    def _restore_token(self) -> None:
        if not self._get_data_func:
            return
        try:
            data = self._get_data_func(self._SESSION_DATA_KEY) or {}
            if (
                    not isinstance(data, dict)
                    or str(data.get("username") or "").strip() != self.username
            ):
                return
            token = str(data.get("token") or "").strip()
            if token:
                self._token = token
                logger.debug("聚影已恢复持久化登录状态")
        except Exception as error:
            logger.debug(f"聚影恢复持久化登录状态失败：{error}")

    def _save_token(self) -> None:
        if not self._save_data_func:
            return
        try:
            self._save_data_func(
                self._SESSION_DATA_KEY,
                {
                    "username": self.username,
                    "token": self._token,
                    "updated_at": int(time.time()),
                } if self._token else {},
            )
        except Exception as error:
            logger.debug(f"聚影持久化登录状态失败：{error}")

    def _set_token(self, token: str = "") -> None:
        self._token = str(token or "").strip()
        self._save_token()

    def _csrf_headers(self) -> Dict[str, str]:
        headers = {
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/",
        }
        csrf_token = str(self._session.cookies.get("csrftoken") or "")
        if csrf_token:
            headers["X-CSRFToken"] = csrf_token
        return headers

    @staticmethod
    def _json_response(response: requests.Response) -> bool:
        return "application/json" in str(
            response.headers.get("content-type") or ""
        ).casefold()

    def _ensure_available(self) -> None:
        if not self.username or not self.password:
            raise JuyingError("聚影账号或密码未配置", "juying_not_configured")
        if self._circuit_open_until > time.monotonic():
            raise JuyingError("聚影请求暂时受限，请稍后重试", "juying_rate_limited")

    @property
    def is_configured(self) -> bool:
        return bool(self.username and self.password)

    def _login(self, force: bool = False) -> None:
        self._ensure_available()
        if self._token and not force:
            return
        with self._LOGIN_LOCK:
            if not force:
                self._restore_token()
                if self._token:
                    return
            csrf_response = gated_idempotent_request(
                self._request_gate,
                self._session.request,
                "GET",
                f"{self.base_url}/api/csrf/",
                proxies=self._proxies,
                timeout=(8, self._request_timeout),
            )
            if csrf_response.status_code != 200:
                raise JuyingError(
                    f"聚影 CSRF 初始化失败（HTTP {csrf_response.status_code}）",
                    "juying_login_failed",
                )
            response = gated_request(
                self._request_gate,
                self._session.post,
                f"{self.base_url}/api/app/login/",
                json={"username": self.username, "password": self.password},
                headers=self._csrf_headers(),
                proxies=self._proxies,
                timeout=(8, self._request_timeout),
            )
            if response.status_code != 200 or not self._json_response(response):
                raise JuyingError(
                    f"聚影登录失败（HTTP {response.status_code}）",
                    "juying_login_failed",
                )
            payload = response.json()
            token = (
                str(payload.get("token") or "").strip()
                if isinstance(payload, dict) else ""
            )
            if not token:
                message = payload.get("message") if isinstance(payload, dict) else ""
                raise JuyingError(
                    str(message or "聚影登录未返回会话令牌"),
                    "juying_login_failed",
                )
            self._set_token(token)

    def _request(
            self,
            method: str,
            path: str,
            retry_auth: bool = True,
            **kwargs: Any,
    ) -> Dict[str, Any]:
        self._login()
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.update(self._csrf_headers())
        headers["X-App-User-Token"] = self._token
        try:
            response = gated_idempotent_request(
                self._request_gate,
                self._session.request,
                method,
                f"{self.base_url}{path}",
                headers=headers,
                proxies=self._proxies,
                timeout=(8, self._request_timeout),
                **kwargs,
            )
        except requests.exceptions.RequestException as error:
            raise JuyingError(
                f"聚影请求失败：{type(error).__name__}",
                "juying_request_failed",
            ) from error

        refreshed = str(response.headers.get("x-refreshed-token") or "").strip()
        if refreshed:
            self._set_token(refreshed)
        if response.status_code == 401 and retry_auth:
            self._set_token()
            self._login(force=True)
            return self._request(method, path, retry_auth=False, **kwargs)
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after") or ""
            try:
                seconds = max(60, min(600, int(float(retry_after))))
            except (TypeError, ValueError):
                seconds = 300
            self._circuit_open_until = time.monotonic() + seconds
            raise JuyingError("聚影请求过于频繁，已临时暂停该渠道", "juying_rate_limited")
        if response.status_code >= 400:
            message = ""
            if self._json_response(response):
                try:
                    body = response.json()
                    message = str(body.get("message") or body.get("detail") or "")
                except ValueError:
                    pass
            raise JuyingError(
                message or f"聚影请求失败（HTTP {response.status_code}）",
                "juying_request_failed",
            )
        if not self._json_response(response):
            raise JuyingError(
                "聚影返回了非 JSON 页面，可能触发了站点验证或接口已改版",
                "juying_schema_changed",
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise JuyingError("聚影返回数据格式异常", "juying_schema_changed") from error
        if not isinstance(payload, dict):
            raise JuyingError("聚影返回数据格式异常", "juying_schema_changed")
        return payload

    def request_json(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        """执行带登录态的聚影 JSON 请求。"""
        with self._lock:
            return self._request(method, path, **kwargs)

    def get_account_info(self) -> Dict[str, Any]:
        """读取当前聚影账户及可用积分。"""
        payload = self.request_json("GET", "/api/app/profile/")
        user = payload.get("user") if isinstance(payload, dict) else None
        if (
                payload.get("status") != "success"
                or not isinstance(user, dict)
                or ("points" not in user and "reward_points" not in user)
        ):
            raise JuyingError(
                "聚影账户接口缺少积分字段", "juying_schema_changed"
            )
        raw_points = (
            user.get("points")
            if "points" in user else user.get("reward_points")
        )
        try:
            points = int(raw_points or 0)
        except (TypeError, ValueError) as error:
            raise JuyingError(
                "聚影账户积分格式异常", "juying_schema_changed"
            ) from error
        return {
            "name": str(user.get("username") or user.get("email") or "聚影用户"),
            "email": str(user.get("email") or ""),
            "username": str(user.get("username") or ""),
            "avatar": str(user.get("avatar") or ""),
            "points": max(0, points),
            "level": str(user.get("level_name") or ""),
            "upload_count": max(0, int(user.get("upload_count") or 0)),
            "favorite_count": max(0, int(user.get("favorite_count") or 0)),
            "checkin_days": max(0, int(user.get("checkin_days") or 0)),
            "registered_days": max(0, int(payload.get("registered_days") or 0)),
            "created_at": str(user.get("date_joined") or ""),
        }

    def close(self) -> None:
        with self._lock:
            self._session.close()
