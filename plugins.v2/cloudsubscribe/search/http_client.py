"""搜索请求兼容入口与共享请求节流器。"""

import random
import threading
import time
from collections import deque
from functools import partial
from typing import Any, Callable, Dict, Optional

from app.log import logger

try:
    from curl_cffi import requests

    CURL_CFFI_AVAILABLE = True
except ImportError:
    import requests as _requests

    CURL_CFFI_AVAILABLE = False


    class _FallbackSession(_requests.Session):
        def __init__(self, *args, impersonate=None, **kwargs):
            super().__init__(*args, **kwargs)

        def request(self, method, url, *args, impersonate=None, **kwargs):
            return super().request(method, url, *args, **kwargs)


    class _FallbackRequests:
        """移除 curl_cffi 专属参数后转发给系统 requests。"""

        Session = _FallbackSession
        Response = _requests.Response
        exceptions = _requests.exceptions

        @staticmethod
        def request(method, url, **kwargs):
            kwargs.pop("impersonate", None)
            return _requests.request(method, url, **kwargs)

        @staticmethod
        def get(url, **kwargs):
            kwargs.pop("impersonate", None)
            return _requests.get(url, **kwargs)

        @staticmethod
        def post(url, **kwargs):
            kwargs.pop("impersonate", None)
            return _requests.post(url, **kwargs)


    requests = _FallbackRequests()
    logger.warning(
        "curl_cffi 未安装，普通搜索源暂时回退 requests；"
        "Dian115 不可用，请安装 curl_cffi 后重启 MoviePilot"
    )


def normalize_proxies(proxy: Any) -> Optional[Dict[str, str]]:
    """统一 MoviePilot 字符串、requests 字典和 server 代理配置。"""
    if not proxy:
        return None
    if isinstance(proxy, str):
        value = proxy.strip()
        return {"http": value, "https": value} if value else None
    if isinstance(proxy, dict):
        if proxy.get("server"):
            value = str(proxy.get("server") or "").strip()
            return {"http": value, "https": value} if value else None
        normalized = {
            str(key): str(value).strip()
            for key, value in proxy.items()
            if str(value or "").strip()
        }
        return normalized or None
    return None


def gated_request(
        gate: "RequestGate", requester: Callable, *args, **kwargs
):
    """通过共享门控执行任意 requests/Session 请求，隐藏闭包样板。"""
    return gate.run(partial(requester, *args, **kwargs))


def gated_idempotent_request(
        gate: "RequestGate", requester: Callable, method: str, *args,
        retry_delay: float = 0.75,
        retry_connection_errors: bool = True,
        **kwargs,
):
    """幂等请求遇到暂态连接异常时短暂重试一次。"""
    normalized_method = str(method or "").strip().upper()
    attempts = (
        2
        if retry_connection_errors and normalized_method in {"GET", "HEAD"}
        else 1
    )
    for attempt in range(attempts):
        try:
            return gated_request(
                gate, requester, normalized_method, *args, **kwargs
            )
        except requests.exceptions.RequestException:
            if attempt + 1 >= attempts:
                raise
            logger.warning(
                f"{gate.name} 连接异常，{retry_delay:.2f} 秒后重试一次"
            )
            time.sleep(max(0.0, float(retry_delay)))


class RequestGate:
    """串行协调登录、挑战和业务接口的请求间隔及风控冷却。"""

    def __init__(
            self,
            name: str,
            request_interval: float,
            minimum_interval: float = 0.2,
            risk_cooldown_seconds: int = 60,
            server_error_cooldown_seconds: int = 5,
            challenge_detector: Optional[Callable] = None,
            serial_requests: bool = True,
            max_requests_per_window: int = 0,
            request_window_seconds: float = 60.0,
    ):
        self._name = str(name or "搜索接口")
        self._request_interval = max(
            minimum_interval, min(float(request_interval or minimum_interval), 10.0)
        )
        self._risk_cooldown_seconds = max(1, int(risk_cooldown_seconds or 60))
        self._server_error_cooldown_seconds = max(
            1, int(server_error_cooldown_seconds or 5)
        )
        self._challenge_detector = challenge_detector
        self._serial_requests = bool(serial_requests)
        self._max_requests_per_window = max(0, int(max_requests_per_window or 0))
        self._request_window_seconds = max(1.0, float(request_window_seconds or 60.0))
        self._request_history = deque()
        self._last_request_at = 0.0
        self._cooldown_until = 0.0
        self._cooldown_status = 0
        self._lock = threading.RLock()

    @property
    def request_interval(self) -> float:
        return self._request_interval

    @property
    def name(self) -> str:
        return self._name

    @property
    def cooldown_remaining(self) -> float:
        """返回当前冷却剩余秒数，供调用方决定等待或快速失败。"""
        with self._lock:
            return max(0.0, self._cooldown_until - time.monotonic())

    @property
    def cooldown_status(self) -> int:
        with self._lock:
            return self._cooldown_status if self._cooldown_until > time.monotonic() else 0

    def run(self, request: Callable):
        """在同一把门锁内执行请求，确保所有接口共用限速。"""
        if not self._serial_requests:
            self._reserve_request_slot()
            response = request()
            with self._lock:
                self._apply_cooldown(response)
            return response
        with self._lock:
            self._wait_for_slot_locked()
            self._record_request_locked()
            try:
                response = request()
                self._apply_cooldown(response)
                return response
            finally:
                self._last_request_at = time.monotonic()

    def _reserve_request_slot(self) -> None:
        with self._lock:
            self._wait_for_slot_locked()
            self._record_request_locked()
            self._last_request_at = time.monotonic()

    def _wait_for_slot_locked(self) -> None:
        now = time.monotonic()
        self._prune_request_history_locked(now)
        window_wait = 0.0
        if (
                self._max_requests_per_window
                and len(self._request_history) >= self._max_requests_per_window
        ):
            window_wait = max(
                self._request_window_seconds - (now - self._request_history[0]),
                0.0,
            )
        wait_seconds = max(
            self._cooldown_until - now,
            window_wait,
            self._request_interval * random.uniform(0.85, 1.25)
            - (now - self._last_request_at),
            0.0,
        )
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    def _prune_request_history_locked(self, now: float) -> None:
        while (
                self._request_history
                and now - self._request_history[0] >= self._request_window_seconds
        ):
            self._request_history.popleft()

    def _record_request_locked(self) -> None:
        if not self._max_requests_per_window:
            return
        now = time.monotonic()
        self._prune_request_history_locked(now)
        self._request_history.append(now)

    def activate_cooldown(
            self, seconds: int, status: int = 0, reason: str = "风险保护"
    ) -> None:
        """由协议层识别到软风控信号时主动开启共享冷却。"""
        normalized_seconds = max(1, min(int(seconds or 1), 10 * 60))
        with self._lock:
            cooldown_until = time.monotonic() + normalized_seconds
            if cooldown_until >= self._cooldown_until:
                self._cooldown_until = cooldown_until
                self._cooldown_status = int(status or 0)
        logger.warning(
            f"{self._name} 触发{reason}，冷却 {normalized_seconds} 秒"
        )

    def _apply_cooldown(self, response) -> None:
        status = int(getattr(response, "status_code", 0) or 0)
        if status not in {403, 429, 500, 502, 503, 504}:
            return
        if status == 403 and self._challenge_detector:
            try:
                if not self._challenge_detector(response):
                    return
            except Exception:
                return
        retry_after = str(response.headers.get("retry-after") or "").strip()
        try:
            seconds = max(1, min(int(float(retry_after)), 10 * 60))
        except (TypeError, ValueError):
            seconds = (
                self._server_error_cooldown_seconds
                if status >= 500 else self._risk_cooldown_seconds
            )
        cooldown_until = time.monotonic() + seconds
        if cooldown_until >= self._cooldown_until:
            self._cooldown_until = cooldown_until
            self._cooldown_status = status
        logger.warning(
            f"{self._name} 触发 HTTP {status} 风控，冷却 {seconds} 秒"
        )


class AccountActionGate:
    """按渠道账号共享受保护操作的滚动窗口和执行锁。"""

    _STATE_LOCK = threading.RLock()
    _LOCKS: Dict[str, threading.RLock] = {}
    _HISTORIES: Dict[str, deque] = {}

    def __init__(
            self,
            name: str,
            account_key: str,
            max_actions: int,
            maximum_actions: int,
            window_seconds: float = 60.0,
    ):
        self._name = str(name or "受保护操作")
        self._account_key = str(account_key or "default")
        self._max_actions = max(
            1, min(int(max_actions or 1), int(maximum_actions or 1))
        )
        self._window_seconds = max(1.0, float(window_seconds or 60.0))

    @property
    def max_actions(self) -> int:
        return self._max_actions

    def run(self, action: Callable):
        """串行等待可用操作槽；实际发起后无论结果均计入窗口。"""
        with self._action_lock():
            self._wait_for_slot()
            try:
                return action()
            finally:
                self._record_attempt()

    def _action_lock(self) -> threading.RLock:
        with self._STATE_LOCK:
            return self._LOCKS.setdefault(
                self._account_key, threading.RLock()
            )

    def _wait_for_slot(self) -> None:
        while True:
            with self._STATE_LOCK:
                history = self._HISTORIES.setdefault(
                    self._account_key, deque()
                )
                now = time.monotonic()
                self._prune(history, now)
                wait_seconds = 0.0
                if history:
                    interval = self._window_seconds / self._max_actions
                    wait_seconds = max(interval - (now - history[-1]), 0.0)
                if len(history) >= self._max_actions:
                    wait_seconds = max(
                        wait_seconds,
                        self._window_seconds - (now - history[0]),
                    )
            if wait_seconds <= 0:
                return
            logger.debug(f"{self._name}按风控节奏等待 {wait_seconds:.1f} 秒")
            time.sleep(wait_seconds)

    def _record_attempt(self) -> None:
        with self._STATE_LOCK:
            history = self._HISTORIES.setdefault(self._account_key, deque())
            now = time.monotonic()
            self._prune(history, now)
            history.append(now)

    def _prune(self, history: deque, now: float) -> None:
        while history and now - history[0] >= self._window_seconds:
            history.popleft()
