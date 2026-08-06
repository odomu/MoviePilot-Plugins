"""HDHive WebAPI 登录、授权与受控请求客户端。"""

import base64
import hashlib
import json
import os
import random
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urljoin

from app.log import logger

from .security import HDHiveSecurityProtocol
from ...http_client import (
    CURL_CFFI_AVAILABLE,
    RequestGate,
    gated_idempotent_request,
    normalize_proxies,
    requests,
)


class HDHiveWebError(RuntimeError):
    """HDHive WebAPI 请求、认证或协议错误。"""

    def __init__(self, message: str, code: str = "", status_code: int = 0):
        super().__init__(message)
        self.code = str(code or "")
        self.status_code = int(status_code or 0)


class HDHiveClient:
    """维护网页登录 Cookie、安全会话和统一请求限速。"""

    BASE_URL = "https://hdhive.com"
    _SESSION_FILE = Path("/config/cache/cloudsubscribe/hdhive-curl-session.json")
    _SESSION_FILE_LOCK = threading.RLock()
    _LOGIN_ACTION_TTL = 60 * 60
    _RISK_COOLDOWN_SECONDS = 60
    _SOFT_RISK_COOLDOWN_SECONDS = 10 * 60
    _SERVER_ERROR_COOLDOWN_SECONDS = 5
    _MAX_REQUESTS_PER_MINUTE = 10
    _UNLOCK_WINDOW_SECONDS = 60.0
    _UNLOCK_STATE_LOCK = threading.RLock()
    _UNLOCK_HISTORIES: Dict[str, deque] = {}
    _UNLOCK_LOCKS: Dict[str, threading.RLock] = {}
    _RISK_COOLDOWNS: Dict[str, tuple] = {}
    _LOGIN_CHUNK_RE = re.compile(
        r"static/chunks/app/\(auth\)/login/page-[^\\\"']+\.js"
    )
    _LOGIN_ACTION_RE = re.compile(
        r"createServerReference\)\(\"([0-9a-f]{40,64})\".{0,200}?\"login\"",
        re.S,
    )
    _BIND_SECRET_RE = re.compile(
        r'[\\"]bindSecret[\\"]\s*:\s*[\\"]([^\\"]+)', re.I
    )
    _SIGNED_RESPONSE_PATHS = {
        "/api/customer/user/current",
        "/api/customer/points-logs",
    }
    _SECURITY_RETRY_CODES = {
        "invalid_session", "missing_signature", "signature_invalid",
        "session_user_mismatch",
    }

    def __init__(
            self,
            username: str,
            password: str,
            proxy: Any = None,
            request_interval: float = 5.0,
            unlocks_per_minute: int = 2,
            timeout: int = 30,
    ):
        if not CURL_CFFI_AVAILABLE:
            raise HDHiveWebError(
                "HDHive WebAPI 模式依赖未安装，请安装插件依赖后重启",
                code="curl_cffi_missing",
            )
        self._username = str(username or "").strip()
        self._password = str(password or "")
        self._proxies = normalize_proxies(proxy)
        self._timeout = max(5, min(int(timeout or 30), 120))
        self._unlocks_per_minute = max(
            1, min(int(unlocks_per_minute or 2), 3)
        )
        self._first_unlock_ready_at = (
                time.monotonic() + random.uniform(3.0, 8.0)
        )
        self._session_key = hashlib.sha256(
            f"{self.BASE_URL}\0{self._username}".encode("utf-8")
        ).hexdigest()
        self._session = requests.Session(impersonate="chrome")
        self._user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        )
        self._languages = "zh-CN,zh,en"
        self._session.headers.update({
            "user-agent": self._user_agent,
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self._lock = threading.RLock()
        self._authenticated = False
        self._login_action = ""
        self._login_action_expires_at = 0.0
        self._security = HDHiveSecurityProtocol()
        self._security_expires_at = 0.0
        self._bind_secret = ""
        self._clock_offset_ms = 0
        self._request_gate = RequestGate(
            "HDHive WebAPI",
            request_interval=request_interval,
            minimum_interval=2.0,
            risk_cooldown_seconds=self._RISK_COOLDOWN_SECONDS,
            server_error_cooldown_seconds=self._SERVER_ERROR_COOLDOWN_SECONDS,
            challenge_detector=self._is_challenge_response,
            max_requests_per_window=self._MAX_REQUESTS_PER_MINUTE,
            request_window_seconds=60.0,
        )
        self._load_cookies()

    @property
    def is_configured(self) -> bool:
        return bool(self._username and self._password)

    @property
    def cache_namespace(self) -> str:
        return self._session_key[:12]

    def matches_config(
            self, username: str, password: str, proxy: Any,
            request_interval: float, unlocks_per_minute: int,
    ) -> bool:
        """判断现有认证会话能否复用于当前配置。"""
        return (
                self._username == str(username or "").strip()
                and self._password == str(password or "")
                and self._proxies == normalize_proxies(proxy)
                and self._request_gate.request_interval
                == max(2.0, min(float(request_interval or 5.0), 10.0))
                and self._unlocks_per_minute
                == max(1, min(int(unlocks_per_minute or 2), 3))
        )

    def close(self) -> None:
        with self._lock:
            self._save_cookies()
            self._session.close()

    @staticmethod
    def _response_text(response) -> str:
        try:
            return response.content.decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            return str(response.text or "")

    @staticmethod
    def response_text(response) -> str:
        """安全读取响应正文，供资源协议层使用。"""
        return HDHiveClient._response_text(response)

    def activate_risk_cooldown(
            self, reason: str, seconds: Optional[int] = None
    ) -> None:
        """将协议层识别到的异常页面纳入所有 Web 请求的共享冷却。"""
        cooldown_seconds = max(
            1,
            min(
                int(seconds or self._SOFT_RISK_COOLDOWN_SECONDS),
                10 * 60,
            ),
        )
        self._request_gate.activate_cooldown(
            cooldown_seconds,
            reason=reason,
        )
        self._remember_risk_cooldown(cooldown_seconds, status=0)

    def _remember_risk_cooldown(self, seconds: float, status: int) -> None:
        cooldown_until = time.monotonic() + max(0.0, float(seconds or 0.0))
        with self._UNLOCK_STATE_LOCK:
            current_until, _ = self._RISK_COOLDOWNS.get(
                self._session_key, (0.0, 0)
            )
            if cooldown_until >= current_until:
                self._RISK_COOLDOWNS[self._session_key] = (
                    cooldown_until,
                    int(status or 0),
                )

    def _shared_risk_cooldown(self) -> tuple:
        with self._UNLOCK_STATE_LOCK:
            cooldown_until, status = self._RISK_COOLDOWNS.get(
                self._session_key, (0.0, 0)
            )
            remaining = cooldown_until - time.monotonic()
            if remaining <= 0:
                self._RISK_COOLDOWNS.pop(self._session_key, None)
                return 0.0, 0
            return remaining, int(status or 0)

    @staticmethod
    def _is_challenge_response(response) -> bool:
        content_type = str(response.headers.get("content-type") or "").lower()
        return (
                "text/html" in content_type
                or str(response.headers.get("cf-mitigated") or "").lower()
                == "challenge"
        )

    def _raw_request(self, method: str, path: str, **kwargs):
        shared_remaining, shared_status = self._shared_risk_cooldown()
        cooldown_remaining = max(
            shared_remaining,
            self._request_gate.cooldown_remaining,
        )
        if cooldown_remaining > 0:
            status = shared_status or self._request_gate.cooldown_status
            status_label = f"HTTP {status}" if status else "风险保护"
            raise HDHiveWebError(
                f"HDHive WebAPI 处于{status_label}冷却期，"
                f"跳过请求（剩余 {int(cooldown_remaining + 0.999)} 秒）",
                code=(
                    "rate_limited" if status in {0, 403, 429}
                    else "server_cooldown"
                ),
                status_code=status,
            )
        request_headers = dict(kwargs.pop("headers", {}) or {})
        try:
            csrf_token = str(
                self._session.cookies.get_dict().get("csrf_access_token") or ""
            ).strip()
        except Exception:
            csrf_token = ""
        if csrf_token and "x-csrf-token" not in {
            str(key).lower() for key in request_headers
        }:
            # 与站点前端及 pure-api-client.mjs 保持一致。
            request_headers["x-csrf-token"] = csrf_token
        try:
            response = gated_idempotent_request(
                self._request_gate,
                self._session.request,
                method,
                urljoin(f"{self.BASE_URL}/", str(path or "").lstrip("/")),
                retry_connection_errors=False,
                proxies=self._proxies,
                timeout=self._timeout,
                headers=request_headers,
                **kwargs,
            )
            cooldown_status = self._request_gate.cooldown_status
            if cooldown_status in {403, 429}:
                self._remember_risk_cooldown(
                    self._request_gate.cooldown_remaining,
                    status=cooldown_status,
                )
            return response
        except requests.exceptions.RequestException as error:
            raise HDHiveWebError(
                f"HDHive WebAPI 请求失败：{error}", code="request_failed"
            ) from error

    def _has_login_cookie(self) -> bool:
        try:
            cookies = self._session.cookies.get_dict()
        except Exception:
            return False
        return bool(cookies.get("token") and cookies.get("refresh_token"))

    def _login_action_id(self, force: bool = False) -> str:
        now = time.monotonic()
        if not force and self._login_action and self._login_action_expires_at > now:
            return self._login_action
        login_response = self._raw_request("GET", "/login")
        if login_response.status_code != 200:
            raise HDHiveWebError(
                f"HDHive 登录页请求失败（HTTP {login_response.status_code}）",
                code="login_page_failed",
                status_code=login_response.status_code,
            )
        chunk_match = self._LOGIN_CHUNK_RE.search(
            self._response_text(login_response)
        )
        if not chunk_match:
            raise HDHiveWebError(
                "HDHive 登录页未返回客户端登录模块", code="schema_changed"
            )
        chunk_response = self._raw_request(
            "GET", f"/_next/{chunk_match.group(0)}"
        )
        action_match = self._LOGIN_ACTION_RE.search(
            self._response_text(chunk_response)
        )
        if not action_match:
            raise HDHiveWebError(
                "HDHive 登录 Server Action 未找到", code="schema_changed"
            )
        self._login_action = action_match.group(1)
        self._login_action_expires_at = now + self._LOGIN_ACTION_TTL
        return self._login_action

    def _login(self, refresh_action: bool = False) -> None:
        if not self.is_configured:
            raise HDHiveWebError("HDHive 未配置用户名或密码", code="not_configured")
        action_id = self._login_action_id(force=refresh_action)
        encoded_password = base64.b64encode(
            self._password.encode("utf-8")
        ).decode("ascii")
        response = self._raw_request(
            "POST",
            "/login",
            headers={
                "accept": "text/x-component",
                "content-type": "text/plain;charset=UTF-8",
                "next-action": action_id,
                "next-url": "/",
                "origin": self.BASE_URL,
                "referer": f"{self.BASE_URL}/login",
            },
            data=json.dumps([{
                "username": self._username,
                "password": encoded_password,
                "password_transport": "base64",
            }, "/"], ensure_ascii=False, separators=(",", ":")),
        )
        response_text = self._response_text(response)
        if response.status_code == 404 and not refresh_action:
            self._login_action = ""
            self._login_action_expires_at = 0.0
            self._login(refresh_action=True)
            return
        if response.status_code != 200 or not self._has_login_cookie():
            message_match = re.search(
                r'\"error\"\s*:\s*\{.*?\"message\"\s*:\s*\"([^\"]+)',
                response_text,
                re.S,
            )
            message = message_match.group(1) if message_match else "登录失败"
            raise HDHiveWebError(
                f"HDHive {message}（HTTP {response.status_code}）",
                code="login_failed",
                status_code=response.status_code,
            )
        self._authenticated = True
        bind_match = self._BIND_SECRET_RE.search(response_text)
        if bind_match:
            self._bind_secret = bind_match.group(1).replace(r"\u003d", "=")
            self._security_expires_at = 0.0
        self._save_cookies()

    def _ensure_authenticated(self) -> None:
        if self._authenticated and self._has_login_cookie():
            return
        if self._has_login_cookie():
            self._authenticated = True
            return
        self._login()

    def _authenticated_request(
            self, method: str, path: str, retry_login: bool = True, **kwargs
    ):
        self._ensure_authenticated()
        response = self._raw_request(method, path, **kwargs)
        redirected_to_login = "/login" in str(getattr(response, "url", ""))
        if retry_login and (
                response.status_code in {401, 403} or redirected_to_login
        ):
            self._authenticated = False
            self._session.cookies.clear()
            self._login()
            return self._authenticated_request(
                method, path, retry_login=False, **kwargs
            )
        if response.status_code >= 400:
            raise HDHiveWebError(
                f"HDHive 网页请求失败（HTTP {response.status_code}）",
                code="request_failed",
                status_code=response.status_code,
            )
        return response

    def request(self, method: str, path: str, **kwargs):
        """执行已登录请求；认证失效时自动重新登录一次。"""
        response_handler: Optional[Callable] = kwargs.pop("response_handler", None)
        with self._lock:
            response = self._authenticated_request(method, path, **kwargs)
            return response_handler(response) if response_handler else response

    def _user_id(self) -> str:
        try:
            return str(self._session.cookies.get_dict().get("hdh_uid") or "0")
        except Exception:
            return "0"

    def _ensure_security_session(self, force: bool = False) -> None:
        self._ensure_authenticated()
        if (
                not force
                and self._security.cid
                and self._security_expires_at - 60 > time.time()
        ):
            return
        client_public_key = self._security.begin_handshake()
        fingerprint = hashlib.sha256(
            f"{self._user_agent}|{self._languages}".encode("utf-8")
        ).hexdigest()
        response = self._raw_request(
            "POST",
            "/api/public/security/session/handshake",
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": self.BASE_URL,
                "referer": f"{self.BASE_URL}/",
            },
            data=json.dumps({
                "client_pub": base64.b64encode(client_public_key).decode("ascii"),
                "ua_fingerprint": fingerprint,
                "ts": int(time.time() * 1000) + self._clock_offset_ms,
                "bind_token": self._bind_secret,
            }, ensure_ascii=False, separators=(",", ":")),
        )
        try:
            payload = response.json()
        except ValueError as error:
            raise HDHiveWebError(
                "HDHive 安全握手响应格式异常", code="handshake_invalid"
            ) from error
        data = payload.get("data") if isinstance(payload, dict) else None
        if response.status_code >= 400 or not isinstance(data, dict):
            message = str(
                (payload.get("error") or {}).get("message")
                if isinstance(payload, dict) and isinstance(payload.get("error"), dict)
                else payload.get("message") if isinstance(payload, dict) else ""
            )
            raise HDHiveWebError(
                f"HDHive 安全握手失败：{message or f'HTTP {response.status_code}'}",
                code="handshake_failed",
                status_code=response.status_code,
            )
        try:
            server_public_key = base64.b64decode(str(data.get("server_pub") or ""))
            self._security.finalize_handshake(
                str(data.get("cid") or ""), server_public_key
            )
            self._security_expires_at = float(data.get("expires_at") or 0)
        except (TypeError, ValueError) as error:
            raise HDHiveWebError(
                f"HDHive 安全握手参数无效：{error}", code="handshake_invalid"
            ) from error

    def _sync_security_time(self) -> None:
        response = self._raw_request("GET", "/api/public/security/time")
        try:
            server_time = int((response.json().get("data") or {}).get("server_time_ms"))
        except (AttributeError, TypeError, ValueError) as error:
            raise HDHiveWebError(
                "HDHive 服务端时间响应无效", code="clock_sync_failed"
            ) from error
        self._clock_offset_ms = server_time - int(time.time() * 1000)

    @classmethod
    def _requires_signed_response(cls, path: str) -> bool:
        if path in cls._SIGNED_RESPONSE_PATHS:
            return True
        return cls._is_unlock_path(path)

    @staticmethod
    def _is_unlock_path(path: str) -> bool:
        return bool(re.fullmatch(
            r"/api/customer/(?:resources|music_resources)/[^/]+/unlock",
            path,
        ) or re.fullmatch(
            r"/api/customer/tv-follow/packs/[^/]+/unlock", path
        ))

    def _unlock_lock(self) -> threading.RLock:
        with self._UNLOCK_STATE_LOCK:
            return self._UNLOCK_LOCKS.setdefault(
                self._session_key, threading.RLock()
            )

    def _wait_for_unlock_slot(self) -> None:
        """按账户限制解锁频率，并为受保护接口保留安全余量。"""
        human_interval = (
                self._UNLOCK_WINDOW_SECONDS / self._unlocks_per_minute
                + random.uniform(1.0, 4.0)
        )
        while True:
            with self._UNLOCK_STATE_LOCK:
                history = self._UNLOCK_HISTORIES.setdefault(
                    self._session_key, deque()
                )
                now = time.monotonic()
                while (
                        history
                        and now - history[0] >= self._UNLOCK_WINDOW_SECONDS
                ):
                    history.popleft()
                wait_seconds = max(self._first_unlock_ready_at - now, 0.0)
                if history:
                    wait_seconds = max(
                        wait_seconds,
                        human_interval - (now - history[-1]),
                    )
                if len(history) >= self._unlocks_per_minute:
                    wait_seconds = max(
                        wait_seconds,
                        self._UNLOCK_WINDOW_SECONDS - (now - history[0]),
                    )
            if wait_seconds <= 0:
                return
            logger.debug(
                f"HDHive 解锁接口按节奏等待 {wait_seconds:.1f} 秒"
            )
            time.sleep(wait_seconds)

    def _record_unlock_attempt(self) -> None:
        with self._UNLOCK_STATE_LOCK:
            history = self._UNLOCK_HISTORIES.setdefault(
                self._session_key, deque()
            )
            now = time.monotonic()
            while history and now - history[0] >= self._UNLOCK_WINDOW_SECONDS:
                history.popleft()
            history.append(now)

    def _signed_request(
            self,
            method: str,
            path: str,
            body: bytes = b"",
            headers: Optional[Dict[str, str]] = None,
            retry: bool = True,
    ):
        if self._is_unlock_path(path):
            with self._unlock_lock():
                self._wait_for_unlock_slot()
                return self._signed_request_once(
                    method, path, body=body, headers=headers, retry=retry
                )
        return self._signed_request_once(
            method, path, body=body, headers=headers, retry=retry
        )

    def _signed_request_once(
            self,
            method: str,
            path: str,
            body: bytes = b"",
            headers: Optional[Dict[str, str]] = None,
            retry: bool = True,
    ):
        self._ensure_security_session()
        timestamp = str(int(time.time() * 1000) + self._clock_offset_ms)
        nonce = self._security.nonce()
        signature = self._security.sign_request(
            method, path, timestamp, nonce, body, self._user_id()
        )
        request_headers = dict(headers or {})
        request_headers.update({
            "X-HDH-Cid": self._security.cid,
            "X-HDH-TS": timestamp,
            "X-HDH-Nonce": nonce,
            "X-HDH-Sig": signature,
            "X-HDH-Kid": self._security.KID,
        })
        is_unlock = self._is_unlock_path(path)
        try:
            response = self._raw_request(
                method, path, headers=request_headers, data=body or None
            )
        except HDHiveWebError as error:
            if is_unlock and error.code not in {
                "rate_limited", "server_cooldown"
            }:
                self._record_unlock_attempt()
            raise
        if is_unlock:
            self._record_unlock_attempt()
        response_body = bytes(response.content or b"")
        response_signature = str(response.headers.get("X-HDH-RSig") or "")
        if response_signature:
            if not self._security.verify_response(
                    path,
                    response.status_code,
                    str(response.headers.get("X-HDH-RTS") or ""),
                    response_body,
                    response_signature,
            ):
                raise HDHiveWebError(
                    "HDHive 响应签名校验失败", code="response_signature_invalid"
                )
        elif response.status_code != 401 and self._requires_signed_response(path):
            if response.status_code >= 400:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                error_value = payload.get("error") if isinstance(payload, dict) else None
                message = str(
                    error_value.get("message")
                    if isinstance(error_value, dict)
                    else payload.get("message") if isinstance(payload, dict) else ""
                ).strip()
                raise HDHiveWebError(
                    f"HDHive 受保护接口请求失败："
                    f"{message or f'HTTP {response.status_code}'}",
                    code=(
                        "rate_limited"
                        if response.status_code == 429 else "request_failed"
                    ),
                    status_code=response.status_code,
                )
            raise HDHiveWebError(
                "HDHive 受保护接口未返回响应签名",
                code="response_signature_required",
                status_code=response.status_code,
            )
        if response.status_code == 401 and retry:
            try:
                error_payload = response.json()
            except ValueError:
                error_payload = {}
            error_code = str(
                error_payload.get("code") or error_payload.get("error_code") or ""
            )
            if error_code in self._SECURITY_RETRY_CODES:
                self._ensure_security_session(force=True)
                return self._signed_request(
                    method, path, body, headers, retry=False
                )
            if error_code == "stale_ts":
                self._sync_security_time()
                return self._signed_request(
                    method, path, body, headers, retry=False
                )
            if error_code == "replay":
                return self._signed_request(
                    method, path, body, headers, retry=False
                )
        return response

    def signed_request(
            self,
            method: str,
            path: str,
            body: bytes = b"",
            headers: Optional[Dict[str, str]] = None,
            response_handler: Optional[Callable] = None,
    ):
        """执行带 HDHive 安全会话签名的授权请求。"""
        with self._lock:
            response = self._signed_request(
                method, path, body=body, headers=headers
            )
            return response_handler(response) if response_handler else response

    def get_account_info(self) -> Dict[str, Any]:
        """通过已签名的用户接口读取 HDHive 可用积分。"""
        response = self.signed_request("GET", "/api/customer/user/current")
        try:
            payload = response.json()
        except ValueError as error:
            raise HDHiveWebError(
                "HDHive 账户接口返回格式异常", code="schema_changed"
            ) from error
        data = payload.get("data") if isinstance(payload, dict) else None
        user_meta = data.get("user_meta") if isinstance(data, dict) else None
        if (
                response.status_code != 200
                or not isinstance(data, dict)
                or not isinstance(user_meta, dict)
                or "points" not in user_meta
        ):
            raise HDHiveWebError(
                "HDHive 账户接口缺少积分字段",
                code="schema_changed",
                status_code=response.status_code,
            )
        try:
            points = int(user_meta.get("points") or 0)
        except (TypeError, ValueError) as error:
            raise HDHiveWebError(
                "HDHive 账户积分格式异常", code="schema_changed"
            ) from error
        return {
            "name": str(
                data.get("nickname") or data.get("username") or "HDHive 用户"
            ),
            "email": str(data.get("email") or ""),
            "username": str(data.get("username") or ""),
            "avatar": str(data.get("avatar_url") or ""),
            "points": max(0, points),
            "is_vip": bool(data.get("is_active_vip") or data.get("is_vip")),
            "share_count": max(0, int(user_meta.get("share_num") or 0)),
            "signin_days": max(
                0, int(user_meta.get("signin_days_total") or 0)
            ),
            "created_at": str(data.get("created_at") or ""),
            "last_login_at": str(data.get("last_web_login_at") or ""),
            "status": str(data.get("lifecycle_status") or ""),
        }

    def _load_cookies(self) -> None:
        with self._SESSION_FILE_LOCK:
            try:
                payload = json.loads(self._SESSION_FILE.read_text(encoding="utf-8"))
                account = payload.get("accounts", {}).get(self._session_key) or {}
                if isinstance(account, list):
                    cookies = account
                else:
                    cookies = account.get("cookies") or []
                    self._bind_secret = str(account.get("bind_secret") or "")
            except (FileNotFoundError, json.JSONDecodeError, OSError, AttributeError):
                return
        now = time.time()
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            expires = float(cookie.get("expires") or 0)
            if expires > 0 and expires <= now:
                continue
            try:
                self._session.cookies.set(
                    str(cookie.get("name") or ""),
                    str(cookie.get("value") or ""),
                    domain=str(cookie.get("domain") or "hdhive.com"),
                    path=str(cookie.get("path") or "/"),
                    secure=bool(cookie.get("secure", True)),
                    expires=int(expires) if expires > 0 else None,
                )
            except Exception:
                continue
        self._authenticated = self._has_login_cookie()

    def _save_cookies(self) -> None:
        cookies = []
        try:
            for cookie in self._session.cookies.jar:
                cookies.append({
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "secure": bool(cookie.secure),
                    "expires": int(cookie.expires or 0),
                })
        except Exception:
            return
        with self._SESSION_FILE_LOCK:
            payload: Dict[str, Any] = {"version": 1, "accounts": {}}
            try:
                current = json.loads(self._SESSION_FILE.read_text(encoding="utf-8"))
                if isinstance(current, dict) and isinstance(current.get("accounts"), dict):
                    payload = current
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                pass
            payload["version"] = 1
            payload.setdefault("accounts", {})[self._session_key] = {
                "cookies": cookies,
                "bind_secret": self._bind_secret,
            }
            try:
                self._SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
                temp_file = self._SESSION_FILE.with_suffix(".tmp")
                temp_file.write_text(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                os.chmod(temp_file, 0o600)
                os.replace(temp_file, self._SESSION_FILE)
            except OSError as error:
                logger.debug(f"保存 HDHive WebAPI Cookie 失败：{error}")
