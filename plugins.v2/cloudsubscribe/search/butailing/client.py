"""不太灵 Magnet 搜索客户端。"""

import threading
import time
from typing import Any, Dict, List

from ..http_client import RequestGate, gated_request, normalize_proxies, requests
from ...utils.cache import create_platform_ttl_cache


class ButailingError(RuntimeError):
    """不太灵 API 请求或响应失败。"""


class ButailingClient:
    """通过不太灵 API 精确定位作品并提取 Magnet。"""

    _DEFAULT_BASE_URL = "https://web5.mukaku.com/prod/api/v1/"
    _DEFAULT_APP_ID = "83768d9ad4"
    _DEFAULT_IDENTITY = "23734adac0301bccdcb107c4aa21f96c"

    def __init__(
            self,
            base_url: str = _DEFAULT_BASE_URL,
            app_id: str = _DEFAULT_APP_ID,
            identity: str = _DEFAULT_IDENTITY,
            proxy: Any = None,
            request_timeout: int = 30,
            request_interval: float = 0.3,
    ):
        self.base_url = str(base_url or self._DEFAULT_BASE_URL).rstrip("/") + "/"
        self._app_id = str(app_id or self._DEFAULT_APP_ID)
        self._identity = str(identity or self._DEFAULT_IDENTITY)
        self._proxies = normalize_proxies(proxy)
        self._request_timeout = max(5, min(int(request_timeout or 30), 60))
        cache_identity = f"{self.base_url}|{self._app_id}|{self._identity}"
        self._list_cache = create_platform_ttl_cache(
            "butailing:lists", cache_identity, maxsize=128, ttl=15 * 60
        )
        self._detail_cache = create_platform_ttl_cache(
            "butailing:details", cache_identity, maxsize=256, ttl=30 * 60
        )
        self._cache_lock = threading.RLock()
        self._request_gate = RequestGate.shared(
            "不太灵",
            f"{cache_identity}|{self._proxies}",
            request_interval=request_interval,
            minimum_interval=0.2,
            serial_requests=False,
        )

    def _request(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        request_params = {
            "app_id": self._app_id,
            "identity": self._identity,
            **params,
        }
        last_error = ""
        for attempt in range(2):
            try:
                response = gated_request(
                    self._request_gate,
                    requests.get,
                    f"{self.base_url}{action}",
                    impersonate="chrome",
                    params=request_params,
                    proxies=self._proxies,
                    timeout=(8, self._request_timeout),
                )
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                    if attempt == 0:
                        time.sleep(0.3)
                        continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ButailingError("不太灵 API 返回了非对象响应")
                return payload
            except ButailingError:
                raise
            except (requests.exceptions.RequestException, ValueError) as error:
                last_error = type(error).__name__
                if attempt == 0:
                    time.sleep(0.3)
                    continue
        raise ButailingError(f"不太灵 API 请求失败：{last_error or '未知错误'}")

    def search_rows(self, keyword: str) -> List[Dict[str, Any]]:
        with self._cache_lock:
            cached = self._list_cache.get(keyword)
        if cached is not None:
            return [dict(item) for item in cached]
        payload = self._request("getVideoList", {
            "sb": keyword,
            "page": 1,
            "limit": 24,
        })
        data = payload.get("data") or {}
        rows = data.get("data") if isinstance(data, dict) else []
        rows = [dict(item) for item in rows or [] if isinstance(item, dict)]
        with self._cache_lock:
            self._list_cache.set(keyword, [dict(item) for item in rows])
        return rows

    def detail(self, douban_id: int) -> Dict[str, Any]:
        cache_key = str(douban_id)
        with self._cache_lock:
            cached = self._detail_cache.get(cache_key)
        if cached is not None:
            return dict(cached)
        payload = self._request("getVideoDetail", {"id": douban_id})
        data = payload.get("data") or {}
        detail = dict(data) if isinstance(data, dict) else {}
        with self._cache_lock:
            self._detail_cache.set(cache_key, dict(detail))
        return detail

    def clear_cache(self) -> Dict[str, int]:
        with self._cache_lock:
            counts = {
                "list": len(list(self._list_cache.items())),
                "detail": len(list(self._detail_cache.items())),
            }
            self._list_cache.clear()
            self._detail_cache.clear()
        return counts
