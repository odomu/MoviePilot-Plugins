"""HDHive WebAPI 登录、授权与受控请求客户端。"""

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
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

from app.core.config import settings
from app.log import logger

from .action import (
    ServerActionProtocol,
    ServerActionResponse,
)
from .captcha import HDHiveCaptchaError, HDHiveCaptchaSolver
from .parser import response_body, response_text
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
    _SESSION_FILE = (
            settings.PLUGIN_DATA_PATH
            / "CloudSubscribe"
            / "hdhive-curl-session.json"
    )
    _SESSION_FILE_LOCK = threading.RLock()
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
        self._server_actions = ServerActionProtocol(
            self._session.cookies,
            error_factory=HDHiveWebError,
            warning=logger.warning,
        )
        self._lock = threading.RLock()
        self._authenticated = False
        self._security = HDHiveSecurityProtocol()
        self._bind_secret = ""
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
        self._captcha = HDHiveCaptchaSolver(
            self._raw_request,
            server_actions=self._server_actions,
        )
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

    @classmethod
    def _body_cooldown_seconds(cls, response) -> int:
        """从 429 文本中提取站点给出的中文冷却秒数。"""
        if int(getattr(response, "status_code", 0) or 0) != 429:
            return 0
        text = response_text(response)[:4096]
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

    def _login(self, refresh_action: bool = False) -> None:
        if not self.is_configured:
            raise HDHiveWebError("HDHive 未配置用户名或密码", code="not_configured")
        response = self._server_actions.login(
            self._raw_request,
            self._username,
            self._password,
            base_url=self.BASE_URL,
            refresh_action=refresh_action,
        )
        if response.status_code != 200 or not self._has_login_cookie():
            message = response.message or "登录失败"
            raise HDHiveWebError(
                f"HDHive {message}（HTTP {response.status_code}）",
                code="login_failed",
                status_code=response.status_code,
            )
        self._authenticated = True
        bind_secret = self._server_actions.bind_secret(response)
        if bind_secret:
            self._bind_secret = bind_secret
            self._security.invalidate()
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
        with self._lock:
            return self._authenticated_request(method, path, **kwargs)

    def _user_id(self) -> str:
        try:
            return str(self._session.cookies.get_dict().get("hdh_uid") or "0")
        except Exception:
            return "0"

    def _ensure_security_session(self, force: bool = False) -> None:
        self._ensure_authenticated()
        if not force and self._security.ready():
            return
        started = time.monotonic()
        body = self._security.handshake_body(
            self._user_agent,
            self._languages,
            self._bind_secret,
        )
        response = self._raw_request(
            "POST",
            "/api/public/security/session/handshake",
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": self.BASE_URL,
                "referer": f"{self.BASE_URL}/",
            },
            data=body,
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
            self._security.accept_handshake(data)
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
        self._security.sync_time(server_time)

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
        if self._security.is_unlock_path(path):
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
        request_headers = dict(headers or {})
        request_headers.update(self._security.request_headers(
            method, signed_path, body, self._user_id()
        ))
        is_unlock = self._security.is_unlock_path(path)
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
        body_bytes = response_body(response)
        response_signature = str(response.headers.get("X-HDH-RSig") or "")
        if response_signature:
            if not self._security.verify_response(
                    signed_path,
                    response.status_code,
                    str(response.headers.get("X-HDH-RTS") or ""),
                    body_bytes,
                    response_signature,
            ):
                raise HDHiveWebError(
                    "HDHive 响应签名校验失败", code="response_signature_invalid"
                )
        elif (
                response.status_code != 401
                and self._security.requires_signed_response(signed_path)
        ):
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
            error_code = self._security.response_error_code(response)
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

    def _prepare_security_retry(self, error_code: str) -> bool:
        action = self._security.retry_action(error_code)
        if action == "handshake":
            self._ensure_security_session(force=True)
            return True
        if action == "clock":
            self._sync_security_time()
            return True
        return action == "retry"

    def signed_request(
            self,
            method: str,
            path: str,
            body: bytes = b"",
            headers: Optional[Dict[str, str]] = None,
            canonical_path: str = "",
    ):
        """执行带 HDHive 安全会话签名的授权请求。"""
        with self._lock:
            return self._signed_request(
                method,
                path,
                body=body,
                headers=headers,
                canonical_path=canonical_path,
            )

    def web_unlock_request(
            self,
            resource_page_path: str,
            slug: str,
            page_headers: Optional[Dict[str, str]] = None,
    ) -> ServerActionResponse:
        """严格按网页的详情页 + unlockResource Server Action 顺序解锁。"""
        try:
            with self._unlock_lock():
                with self._lock:
                    self._wait_for_unlock_slot()
                    started = time.monotonic()
                    # 页面发布新 chunk 时会额外读取一次模块，始终按上限预留。
                    with self._request_gate.immediate_sequence(
                            request_count=3,
                            cancel_check=self._stop_requested,
                            fail_on_cooldown=True,
                    ):
                        (
                            page_response,
                            response,
                            honeypot_token,
                            chunk,
                        ) = self._server_actions.unlock(
                            self._authenticated_request,
                            resource_page_path,
                            slug,
                            page_headers=page_headers,
                            base_url=self.BASE_URL,
                            on_submit=self._record_unlock_attempt,
                        )
                        logger.debug(
                            "HDHive 资源页 Action 上下文就绪："
                            f"honeypot长度={len(honeypot_token)}，"
                            f"chunk={chunk.rsplit('/', 1)[-1]}"
                        )
                    logger.debug(
                        "HDHive 网页解锁序列完成：详情页 HTTP "
                        f"{getattr(page_response, 'status_code', 0)}，"
                        f"Action HTTP {response.status_code}，"
                        f"耗时 {(time.monotonic() - started):.2f}s"
                    )
                    return response
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
        """通过网页 Server Action 签到，并返回前后积分与累计天数。"""
        with self.related_requests(5):
            before = self.get_account_info()
            response = None
            try:
                response = self._server_actions.checkin(
                    self._authenticated_request,
                    bool(is_gambler),
                    base_url=self.BASE_URL,
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
                status_code = response.status_code
                message = response.message
                error_code = response.code
                payload = response.payload or {}
                data = response.data
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
            "signin_points": 0 if already_checked_in else points_change,
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
        session_file = self._SESSION_FILE
        with self._SESSION_FILE_LOCK:
            try:
                payload = json.loads(session_file.read_text(encoding="utf-8"))
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
            if not self._server_actions.is_persistent_cookie(
                    str(cookie.get("name") or "")
            ):
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
        session_file = self._SESSION_FILE
        cookies = []
        try:
            for cookie in self._session.cookies.jar:
                if not self._server_actions.is_persistent_cookie(cookie.name):
                    continue
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
                current = json.loads(session_file.read_text(encoding="utf-8"))
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
                session_file.parent.mkdir(parents=True, exist_ok=True)
                temp_file = session_file.with_suffix(".tmp")
                temp_file.write_text(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                os.chmod(temp_file, 0o600)
                os.replace(temp_file, session_file)
            except OSError as error:
                logger.debug(f"保存 HDHive WebAPI Cookie 失败：{error}")
