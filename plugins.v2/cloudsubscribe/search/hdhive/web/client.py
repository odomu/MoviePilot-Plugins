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
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

from app.log import logger

from .captcha import HDHiveCaptchaError, HDHiveCaptchaSolver
from .security import HDHiveSecurityProtocol
from ...http_client import (
    CURL_CFFI_AVAILABLE,
    RequestGate,
    RequestGateCancelled,
    RequestGateCooldown,
    gated_idempotent_request,
    normalize_proxies,
    requests,
)
from ....utils.cache import create_platform_ttl_cache


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
    _UNLOCK_READY_ATS: Dict[str, float] = {}
    _RISK_COOLDOWNS: Dict[str, tuple] = {}
    _RISK_COOLDOWN_CACHE_TTL = 10 * 60
    _LOGIN_CHUNK_RE = re.compile(
        r"static/chunks/app/\(auth\)/login/page-[^\\\"']+\.js"
    )
    _LOGIN_ACTION_RE = re.compile(
        r"createServerReference\)\(\"([0-9a-f]{40,64})\".{0,200}?\"login\"",
        re.S,
    )
    _RESOURCE_PAGE_CHUNK_RE = re.compile(
        r"static/chunks/app/\(no-layout\)/resource/[^\"']+/page-[A-Za-z0-9]+\.js"
    )
    _UNLOCK_ACTION_RE = re.compile(
        r"createServerReference\)\(\"([0-9a-f]{40,64})\""
        r".{0,240}?\"unlockResource\"",
        re.S,
    )
    _HONEYPOT_TOKEN_RE = re.compile(
        r'"honeypotToken"\s*:\s*("(?:\\.|[^"\\])*")'
    )
    _BIND_SECRET_RE = re.compile(
        r'[\\"]bindSecret[\\"]\s*:\s*[\\"]([^\\"]+)', re.I
    )
    _SIGNED_RESPONSE_PATHS = {
        "/api/customer/user/current",
        "/api/customer/points-logs",
        "/api/customer/user/checkin",
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
            should_stop: Optional[Callable[[], bool]] = None,
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
        self._should_stop = should_stop
        self._unlocks_per_minute = max(
            1, min(int(unlocks_per_minute or 2), 3)
        )
        self._session_key = hashlib.sha256(
            f"{self.BASE_URL}\0{self._username}".encode("utf-8")
        ).hexdigest()
        self._risk_cooldown_cache = create_platform_ttl_cache(
            "hdhive:web:risk_cooldown",
            self._session_key,
            maxsize=1,
            ttl=self._RISK_COOLDOWN_CACHE_TTL,
        )
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
        self._unlock_actions: Dict[str, str] = {}
        self._security = HDHiveSecurityProtocol()
        self._security_expires_at = 0.0
        self._bind_secret = ""
        self._clock_offset_ms = 0
        self._request_gate = RequestGate.shared(
            "HDHive WebAPI",
            self._session_key,
            request_interval=request_interval,
            minimum_interval=2.0,
            risk_cooldown_seconds=self._RISK_COOLDOWN_SECONDS,
            server_error_cooldown_seconds=self._SERVER_ERROR_COOLDOWN_SECONDS,
            challenge_detector=self._is_challenge_response,
            # 普通页面请求只串行领取限速槽，不在网络 I/O 期间占用账号门锁。
            # 登录、验证码和解锁仍由客户端锁及 immediate_sequence 独占。
            serial_requests=False,
            max_requests_per_window=self._MAX_REQUESTS_PER_MINUTE,
            request_window_seconds=60.0,
        )
        self._captcha = HDHiveCaptchaSolver(self._raw_request)
        self._load_cookies()

    @property
    def is_configured(self) -> bool:
        return bool(self._username and self._password)

    @property
    def cache_namespace(self) -> str:
        return self._session_key[:12]

    @property
    def cooldown_remaining(self) -> float:
        """返回账户风险冷却与请求门控冷却中的较长剩余时间。"""
        shared_remaining, _ = self._shared_risk_cooldown()
        return max(shared_remaining, self._request_gate.cooldown_remaining)

    def _stop_requested(self) -> bool:
        try:
            return bool(self._should_stop and self._should_stop())
        except Exception as error:
            logger.warning(f"读取 HDHive 停止状态失败：{error}")
            return False

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

    @classmethod
    def _body_cooldown_seconds(cls, response) -> int:
        """从 429 文本中提取站点给出的中文冷却秒数。"""
        if int(getattr(response, "status_code", 0) or 0) != 429:
            return 0
        text = cls._response_text(response)[:4096]
        match = re.search(r"(?:冷却|重试|限制)[^0-9]{0,24}(\d+)\s*秒", text)
        if not match:
            return 0
        try:
            return max(1, min(int(match.group(1)), 10 * 60))
        except (TypeError, ValueError):
            return 0

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
        duration = max(0.0, float(seconds or 0.0))
        cooldown_until = time.monotonic() + duration
        with self._UNLOCK_STATE_LOCK:
            current_until, _ = self._RISK_COOLDOWNS.get(
                self._session_key, (0.0, 0)
            )
            if cooldown_until >= current_until:
                self._RISK_COOLDOWNS[self._session_key] = (
                    cooldown_until,
                    int(status or 0),
                )
        if duration > 0:
            try:
                current = self._risk_cooldown_cache.get("state") or {}
                current_until = float(current.get("until") or 0)
                wall_until = time.time() + duration
                if wall_until >= current_until:
                    ttl = max(
                        1,
                        min(
                            int(duration + 0.999),
                            self._RISK_COOLDOWN_CACHE_TTL,
                        ),
                    )
                    self._risk_cooldown_cache.set(
                        "state",
                        {"until": wall_until, "status": int(status or 0)},
                        ttl=ttl,
                    )
            except Exception as error:
                logger.debug(f"HDHive 风控冷却持久化失败：{error}")

    def _shared_risk_cooldown(self) -> tuple:
        wall_remaining = 0.0
        wall_status = 0
        try:
            persisted = self._risk_cooldown_cache.get("state") or {}
            wall_remaining = float(persisted.get("until") or 0) - time.time()
            wall_status = int(persisted.get("status") or 0)
        except Exception as error:
            logger.debug(f"HDHive 风控冷却读取失败：{error}")
        with self._UNLOCK_STATE_LOCK:
            cooldown_until, status = self._RISK_COOLDOWNS.get(
                self._session_key, (0.0, 0)
            )
            remaining = cooldown_until - time.monotonic()
            if remaining <= 0 and wall_remaining <= 0:
                self._RISK_COOLDOWNS.pop(self._session_key, None)
                return 0.0, 0
            if wall_remaining > remaining:
                return wall_remaining, wall_status
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
            body_cooldown = self._body_cooldown_seconds(response)
            if body_cooldown > self._request_gate.cooldown_remaining:
                self._request_gate.activate_cooldown(
                    body_cooldown,
                    status=429,
                    reason="HTTP 429 风控",
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

    @contextmanager
    def related_requests(self, request_count: int):
        """连续执行协议链，并固定客户端锁先于门控锁以避免反向等待。"""
        with self._lock:
            with self._request_gate.immediate_sequence(
                    request_count=request_count,
                    cancel_check=self._stop_requested,
            ):
                yield

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

    def _login_with_sequence(self) -> None:
        """连续完成登录页、JS 模块和登录 Action。"""
        with self.related_requests(3):
            self._login()

    def _ensure_authenticated(self) -> None:
        if self._authenticated and self._has_login_cookie():
            return
        if self._has_login_cookie():
            self._authenticated = True
            return
        self._login_with_sequence()

    def _authenticated_request(
            self,
            method: str,
            path: str,
            retry_login: bool = True,
            retry_captcha: bool = True,
            **kwargs,
    ):
        self._ensure_authenticated()
        response = self._raw_request(method, path, **kwargs)
        if self._captcha.is_challenge_response(response):
            logger.debug(
                f"HDHive 请求命中验证码挑战：{method} {str(path).split('?', 1)[0]}"
            )
            if not retry_captcha:
                raise HDHiveWebError(
                    "HDHive 验证通过后仍返回安全验证页",
                    code="captcha_retry_failed",
                )
            with self.related_requests(4):
                try:
                    clearance_seconds = self._captcha.solve(response, path)
                except HDHiveCaptchaError as error:
                    logger.debug(
                        f"HDHive 验证码处理失败：code={error.code}，原因={error}"
                    )
                    raise HDHiveWebError(str(error), code=error.code) from error
                self._save_cookies()
                logger.info(
                    "HDHive 动态验证码验证通过"
                    + (
                        f"，有效期 {clearance_seconds} 秒"
                        if clearance_seconds > 0 else ""
                    )
                )
                return self._authenticated_request(
                    method,
                    path,
                    retry_login=retry_login,
                    retry_captcha=False,
                    **kwargs,
                )
        redirected_to_login = "/login" in str(getattr(response, "url", ""))
        if retry_login and (
                response.status_code in {401, 403} or redirected_to_login
        ):
            self._authenticated = False
            self._session.cookies.clear()
            self._login_with_sequence()
            return self._authenticated_request(
                method, path, retry_login=False, **kwargs
            )
        if response.status_code >= 400:
            raise HDHiveWebError(
                f"HDHive 网页请求失败（HTTP {response.status_code}）",
                code=("rate_limited" if response.status_code == 429 else "request_failed"),
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
        started = time.monotonic()
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
            logger.debug(
                "HDHive 安全会话握手完成："
                f"耗时={time.monotonic() - started:.2f}s"
            )
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
        normalized_path = urlsplit(path).path
        if normalized_path in cls._SIGNED_RESPONSE_PATHS:
            return True
        return cls._is_unlock_path(normalized_path)

    @staticmethod
    def _is_unlock_path(path: str) -> bool:
        path = urlsplit(path).path
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
            if self._stop_requested():
                raise HDHiveWebError(
                    "HDHive 解锁等待已停止", code="stopped"
                )
            cooldown_remaining = self.cooldown_remaining
            if cooldown_remaining > 0:
                raise HDHiveWebError(
                    "HDHive WebAPI 处于风控冷却期，跳过解锁"
                    f"（剩余 {int(cooldown_remaining + 0.999)} 秒）",
                    code="rate_limited",
                )
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
                ready_at = self._UNLOCK_READY_ATS.get(self._session_key)
                if ready_at is None:
                    ready_at = now + random.uniform(3.0, 8.0)
                    self._UNLOCK_READY_ATS[self._session_key] = ready_at
                wait_seconds = max(ready_at - now, 0.0)
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
            deadline = time.monotonic() + wait_seconds
            while True:
                if self._stop_requested():
                    raise HDHiveWebError(
                        "HDHive 解锁等待已停止", code="stopped"
                    )
                cooldown_remaining = self.cooldown_remaining
                if cooldown_remaining > 0:
                    raise HDHiveWebError(
                        "HDHive WebAPI 处于风控冷却期，跳过解锁"
                        f"（剩余 {int(cooldown_remaining + 0.999)} 秒）",
                        code="rate_limited",
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 0.25))

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
            retry_captcha: bool = True,
            canonical_path: str = "",
    ):
        if self._is_unlock_path(path):
            with self._unlock_lock():
                self._wait_for_unlock_slot()
                return self._signed_request_once(
                    method,
                    path,
                    body=body,
                    headers=headers,
                    retry=retry,
                    retry_captcha=retry_captcha,
                    canonical_path=canonical_path,
                )
        return self._signed_request_once(
            method,
            path,
            body=body,
            headers=headers,
            retry=retry,
            retry_captcha=retry_captcha,
            canonical_path=canonical_path,
        )

    def _signed_request_once(
            self,
            method: str,
            path: str,
            body: bytes = b"",
            headers: Optional[Dict[str, str]] = None,
            retry: bool = True,
            retry_captcha: bool = True,
            canonical_path: str = "",
    ):
        self._ensure_security_session()
        signed_path = str(canonical_path or urlsplit(path).path or "/")
        timestamp = str(int(time.time() * 1000) + self._clock_offset_ms)
        nonce = self._security.nonce()
        signature = self._security.sign_request(
            method, signed_path, timestamp, nonce, body, self._user_id()
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
        if self._captcha.is_challenge_response(response):
            if not retry_captcha:
                raise HDHiveWebError(
                    "HDHive 验证通过后仍要求安全验证",
                    code="captcha_retry_failed",
                    status_code=response.status_code,
                )
            logger.debug(
                f"HDHive 签名请求命中验证码挑战：{method} {signed_path}"
            )
            with self.related_requests(4):
                try:
                    clearance_seconds = self._captcha.solve(response, path)
                except HDHiveCaptchaError as error:
                    raise HDHiveWebError(
                        str(error),
                        code=error.code,
                        status_code=response.status_code,
                    ) from error
                self._save_cookies()
                logger.info(
                    "HDHive 动态验证码验证通过"
                    + (
                        f"，有效期 {clearance_seconds} 秒"
                        if clearance_seconds > 0 else ""
                    )
                )
                retried = self._signed_request(
                    method,
                    path,
                    body,
                    headers,
                    retry=retry,
                    retry_captcha=False,
                    canonical_path=signed_path,
                )
                setattr(retried, "hdhive_captcha_verified", True)
                return retried
        response_body = bytes(response.content or b"")
        response_signature = str(response.headers.get("X-HDH-RSig") or "")
        if response_signature:
            if not self._security.verify_response(
                    signed_path,
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
                risk_message = any(marker in message for marker in (
                    "高频", "人机验证", "安全验证", "访问频繁", "操作频繁",
                ))
                if risk_message:
                    self.activate_risk_cooldown("受保护接口要求人机验证")
                raise HDHiveWebError(
                    f"HDHive 受保护接口请求失败："
                    f"{message or f'HTTP {response.status_code}'}",
                    code=(
                        "rate_limited"
                        if response.status_code == 429 or risk_message
                        else "request_failed"
                    ),
                    status_code=response.status_code,
                )
            raise HDHiveWebError(
                "HDHive 受保护接口未返回响应签名",
                code="response_signature_required",
                status_code=response.status_code,
            )
        if response.status_code == 401 and retry:
            error_code = self._security_error_code(response)
            if self._prepare_security_retry(error_code):
                return self._signed_request(
                    method,
                    path,
                    body,
                    headers,
                    retry=False,
                    retry_captcha=retry_captcha,
                    canonical_path=signed_path,
                )
        return response

    @staticmethod
    def _security_error_code(response) -> str:
        if int(getattr(response, "status_code", 0) or 0) != 401:
            return ""
        try:
            error_payload = response.json()
        except ValueError:
            error_payload = {}
        if not isinstance(error_payload, dict):
            return ""
        return str(
            error_payload.get("code") or error_payload.get("error_code") or ""
        )

    def _prepare_security_retry(self, error_code: str) -> bool:
        if error_code in self._SECURITY_RETRY_CODES:
            self._ensure_security_session(force=True)
            return True
        if error_code == "stale_ts":
            self._sync_security_time()
            return True
        return error_code == "replay"

    def signed_request(
            self,
            method: str,
            path: str,
            body: bytes = b"",
            headers: Optional[Dict[str, str]] = None,
            response_handler: Optional[Callable] = None,
            canonical_path: str = "",
    ):
        """执行带 HDHive 安全会话签名的授权请求。"""
        with self._lock:
            response = self._signed_request(
                method,
                path,
                body=body,
                headers=headers,
                canonical_path=canonical_path,
            )
            return response_handler(response) if response_handler else response

    @classmethod
    def _resource_action_context(cls, response) -> tuple[str, str]:
        """从详情页 RSC 数据中读取蜜罐字段值和页面 chunk。"""
        text = cls._response_text(response)
        normalized = text.replace(r'\"', '"')
        token_match = cls._HONEYPOT_TOKEN_RE.search(normalized)
        chunk_match = cls._RESOURCE_PAGE_CHUNK_RE.search(text)
        if not token_match:
            raise HDHiveWebError(
                "HDHive 资源页未返回 honeypotToken 字段",
                code="action_proof_missing",
            )
        if not chunk_match:
            raise HDHiveWebError(
                "HDHive 资源页未返回解锁客户端模块",
                code="schema_changed",
            )
        try:
            token = str(json.loads(token_match.group(1)) or "")
        except (TypeError, ValueError) as error:
            raise HDHiveWebError(
                "HDHive honeypotToken 字段格式异常",
                code="action_proof_invalid",
            ) from error
        logger.debug(
            "HDHive 资源页 Action 上下文就绪："
            f"honeypot长度={len(token)}，"
            f"chunk={chunk_match.group(0).rsplit('/', 1)[-1]}"
        )
        return token, chunk_match.group(0)

    def _unlock_action_id(self, chunk: str) -> str:
        action_id = self._unlock_actions.get(chunk)
        if action_id:
            return action_id
        response = self._authenticated_request("GET", f"/_next/{chunk}")
        match = self._UNLOCK_ACTION_RE.search(self._response_text(response))
        if not match:
            raise HDHiveWebError(
                "HDHive 解锁 Server Action 未找到",
                code="schema_changed",
            )
        action_id = match.group(1)
        self._unlock_actions = {chunk: action_id}
        logger.debug("HDHive 解锁 Server Action 已解析")
        return action_id

    def web_unlock_request(
            self,
            resource_page_path: str,
            slug: str,
            page_headers: Optional[Dict[str, str]] = None,
            response_handler: Optional[Callable] = None,
    ):
        """严格按网页的详情页 + unlockResource Server Action 顺序解锁。"""
        try:
            with self._unlock_lock():
                with self._lock:
                    self._wait_for_unlock_slot()
                    started = time.monotonic()
                    posted = False
                    try:
                        # 页面发布新 chunk 时会额外读取一次模块，始终按上限预留。
                        with self._request_gate.immediate_sequence(
                                request_count=3,
                                cancel_check=self._stop_requested,
                                fail_on_cooldown=True,
                        ):
                            page_response = self._authenticated_request(
                                "GET",
                                resource_page_path,
                                headers=page_headers or {},
                            )
                            honeypot_token, chunk = self._resource_action_context(
                                page_response
                            )
                            action_id = self._unlock_action_id(chunk)
                            posted = True
                            response = self._authenticated_request(
                                "POST",
                                resource_page_path,
                                headers={
                                    "accept": "text/x-component",
                                    "content-type": "text/plain;charset=UTF-8",
                                    "next-action": action_id,
                                    "next-url": resource_page_path,
                                    "origin": self.BASE_URL,
                                    "referer": (
                                        f"{self.BASE_URL}{resource_page_path}"
                                    ),
                                    "sec-fetch-dest": "empty",
                                    "sec-fetch-mode": "cors",
                                    "sec-fetch-site": "same-origin",
                                },
                                data=json.dumps(
                                    [slug, honeypot_token],
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                            )
                        logger.debug(
                            "HDHive 网页解锁序列完成：详情页 HTTP "
                            f"{getattr(page_response, 'status_code', 0)}，"
                            f"Action HTTP {getattr(response, 'status_code', 0)}，"
                            f"耗时 {(time.monotonic() - started):.2f}s"
                        )
                    finally:
                        if posted:
                            self._record_unlock_attempt()
                    return response_handler(response) if response_handler else response
        except RequestGateCooldown as error:
            raise HDHiveWebError(
                "HDHive WebAPI 处于风控冷却期，跳过解锁"
                f"（剩余 {int(error.remaining + 0.999)} 秒）",
                code="rate_limited",
                status_code=error.status,
            ) from error
        except RequestGateCancelled as error:
            raise HDHiveWebError(
                "HDHive 解锁等待已停止", code="stopped"
            ) from error

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
            "captcha_verified": bool(
                getattr(response, "hdhive_captcha_verified", False)
            ),
        }

    @staticmethod
    def _checkin_message(payload: Dict[str, Any]) -> str:
        data = payload.get("data") if isinstance(payload, dict) else None
        error = payload.get("error") if isinstance(payload, dict) else None
        candidates = (
            data.get("message") if isinstance(data, dict) else "",
            data.get("description") if isinstance(data, dict) else "",
            data.get("detail") if isinstance(data, dict) else "",
            payload.get("message") if isinstance(payload, dict) else "",
            payload.get("description") if isinstance(payload, dict) else "",
            payload.get("detail") if isinstance(payload, dict) else "",
            error.get("message") if isinstance(error, dict) else "",
            error.get("description") if isinstance(error, dict) else "",
            error.get("detail") if isinstance(error, dict) else "",
            error if isinstance(error, str) else "",
        )
        return next((str(value).strip() for value in candidates if value), "")

    @staticmethod
    def _checkin_error_code(payload: Dict[str, Any]) -> str:
        error = payload.get("error") if isinstance(payload, dict) else None
        return str(
            payload.get("code")
            or payload.get("error_code")
            or (error.get("code") if isinstance(error, dict) else "")
            or ""
        ).strip()

    @staticmethod
    def _points_log_items(payload: Any) -> List[Dict[str, Any]]:
        """兼容积分日志接口的列表和常见分页对象。"""
        containers = []
        if isinstance(payload, dict):
            containers.extend((payload.get("data"), payload))
        else:
            containers.append(payload)
        for container in containers:
            if isinstance(container, list):
                return [item for item in container if isinstance(item, dict)]
            if not isinstance(container, dict):
                continue
            for key in ("items", "logs", "records", "results", "rows", "list"):
                items = container.get(key)
                if isinstance(items, list):
                    return [item for item in items if isinstance(item, dict)]
        return []

    @staticmethod
    def _parse_points_log_time(value: Any) -> Optional[datetime]:
        if isinstance(value, (int, float)):
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            try:
                return datetime.fromtimestamp(timestamp).astimezone()
            except (OSError, OverflowError, ValueError):
                return None
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed
        return parsed.astimezone()

    @classmethod
    def _is_today_checkin_log(cls, item: Dict[str, Any]) -> bool:
        description = " ".join(
            str(item.get(key) or "")
            for key in (
                "change_type", "type", "action", "source", "title",
                "remark", "description", "message",
            )
        )
        normalized = re.sub(r"[\s_-]+", "", description.casefold())
        if not any(marker in normalized for marker in ("签到", "checkin", "signin")):
            return False
        occurred_at = None
        for key in (
                "created_at", "createdAt", "create_time", "add_time",
                "occurred_at", "updated_at", "date", "time",
        ):
            occurred_at = cls._parse_points_log_time(item.get(key))
            if occurred_at is not None:
                break
        return bool(
            occurred_at
            and occurred_at.date() == datetime.now().astimezone().date()
        )

    def get_points_logs(
            self,
            page: int = 1,
            page_size: int = 20,
    ) -> List[Dict[str, Any]]:
        """读取积分明细；签名只使用不含查询参数的 canonical path。"""
        normalized_page = max(1, int(page or 1))
        normalized_size = max(1, min(int(page_size or 20), 100))
        path = (
            "/api/customer/points-logs"
            f"?page={normalized_page}&page_size={normalized_size}"
        )
        response = self.signed_request(
            "GET",
            path,
            canonical_path="/api/customer/points-logs",
        )
        try:
            payload = response.json()
        except ValueError as error:
            raise HDHiveWebError(
                "HDHive 积分日志接口返回格式异常",
                code="schema_changed",
                status_code=response.status_code,
            ) from error
        if response.status_code >= 400:
            raise HDHiveWebError(
                "HDHive 积分日志读取失败",
                code="request_failed",
                status_code=response.status_code,
            )
        return self._points_log_items(payload)

    def has_checked_in_today(self) -> bool:
        return any(
            self._is_today_checkin_log(item)
            for item in self.get_points_logs(page=1, page_size=20)
        )

    def checkin(self, is_gambler: bool = False) -> Dict[str, Any]:
        """执行无浏览器签到，并返回签到前后的积分与累计天数。"""
        request_body = json.dumps(
            {"is_gambler": True} if is_gambler else {},
            separators=(",", ":"),
        ).encode("utf-8")
        with self.related_requests(3):
            before = self.get_account_info()
            response = None
            payload: Dict[str, Any] = {}
            try:
                response = self.signed_request(
                    "POST",
                    "/api/customer/user/checkin",
                    body=request_body,
                    headers={
                        "accept": "application/json",
                        "content-type": "application/json",
                        "origin": self.BASE_URL,
                        "referer": f"{self.BASE_URL}/user/{self._user_id()}",
                    },
                )
            except HDHiveWebError as error:
                if error.status_code != 400 or not self.has_checked_in_today():
                    raise
                status_code = error.status_code
                message = "今日已签到"
                error_code = error.code
                checked_in_value = False
                already_checked_in = True
                success = True
            else:
                try:
                    payload = response.json()
                except ValueError as error:
                    raise HDHiveWebError(
                        "HDHive 签到接口返回格式异常",
                        code="schema_changed",
                        status_code=response.status_code,
                    ) from error
                if not isinstance(payload, dict):
                    raise HDHiveWebError(
                        "HDHive 签到接口返回格式异常",
                        code="schema_changed",
                        status_code=response.status_code,
                    )
                status_code = int(response.status_code or 0)
                message = self._checkin_message(payload)
                error_code = self._checkin_error_code(payload)
                data = payload.get("data")
                data = data if isinstance(data, dict) else {}
                already_checked_in = bool(
                    data.get("already_checked_in")
                    or any(marker in message for marker in (
                        "已经签到", "今日已签到", "签到过", "明天再来",
                    ))
                    or error_code in {
                        "ALREADY_CHECKED_IN", "CHECKIN_ALREADY_COMPLETED",
                    }
                )
                checked_in_value = data.get("checked_in")
                if (
                        status_code < 400
                        and checked_in_value is False
                        and not already_checked_in
                ):
                    already_checked_in = True
                success = bool(
                    already_checked_in
                    or (
                            status_code < 400
                            and payload.get("success") is not False
                    )
                )
                if (
                        not success
                        and status_code == 400
                        and self.has_checked_in_today()
                ):
                    already_checked_in = True
                    success = True
                    message = "今日已签到"
            after = (
                before if already_checked_in
                else self.get_account_info() if success
                else before
            )
        points_before = int(before.get("points") or 0)
        points_after = int(after.get("points") or 0)
        points_change = points_after - points_before
        return {
            "success": success,
            "checked_in": bool(checked_in_value) and not already_checked_in,
            "already_checked_in": already_checked_in,
            "status": (
                "今日已签到" if already_checked_in
                else "签到成功" if success else "签到失败"
            ),
            "message": message or (
                "今日已签到" if already_checked_in
                else "签到成功" if success
                else f"签到失败（HTTP {status_code}）"
            ),
            "is_gambler": bool(is_gambler),
            "points_change": points_change,
            "points_before": points_before,
            "points_after": points_after,
            "signin_days": int(after.get("signin_days") or 0),
            "status_code": status_code,
            "error_code": error_code,
            "captcha_verified": bool(
                before.get("captcha_verified")
                or after.get("captcha_verified")
                or getattr(response, "hdhive_captcha_verified", False)
            ),
            "raw": payload,
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
