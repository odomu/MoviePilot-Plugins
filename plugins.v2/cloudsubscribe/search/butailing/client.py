"""不太灵 Magnet 搜索客户端。"""

import hashlib
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from ..http_client import RequestGate, gated_request, normalize_proxies, requests
from ..matching import (
    extract_season,
    extract_year,
    title_matches,
    unique_texts,
)
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
        self._search_locks = tuple(threading.Lock() for _ in range(16))
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

    def _search_rows(self, keyword: str) -> List[Dict[str, Any]]:
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

    def _detail(self, douban_id: int) -> Dict[str, Any]:
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

    @staticmethod
    def _candidate_titles(row: Dict[str, Any]) -> List[str]:
        values = [row.get("title"), row.get("otitle")]
        aliases = str(row.get("alias") or "")
        values.extend(part.strip() for part in aliases.replace("/", ",").split(","))
        return [str(value).strip() for value in values if str(value or "").strip()]

    def _select_row(
            self,
            rows: Iterable[Dict[str, Any]],
            expected_titles: List[str],
            expected_year: str,
            media_type: str,
            season: Optional[int],
            douban_id: Optional[object],
            imdb_id: Optional[object] = None,
    ) -> Optional[Dict[str, Any]]:
        target_type = 2 if media_type == "tv" else 1
        expected_douban_id = str(douban_id or "").strip()
        expected_imdb_id = str(imdb_id or "").strip().casefold()
        best_title_match = None
        best_external_id_match = None
        for index, row in enumerate(rows):
            try:
                row_type = int(row.get("type") or 0)
            except (TypeError, ValueError):
                continue
            if row_type != target_type:
                continue
            candidate_titles = self._candidate_titles(row)
            exact_external_id = bool(
                expected_douban_id
                and expected_douban_id in {
                    str(row.get("doub_id") or "").strip(),
                    str(row.get("idcode") or "").strip(),
                }
            ) or bool(
                expected_imdb_id
                and str(row.get("IMDB_number") or "").strip().casefold()
                == expected_imdb_id
            )
            matched_title = any(
                title_matches(value, expected_titles) for value in candidate_titles
            )
            if not exact_external_id and not matched_title:
                continue
            candidate_season = next(
                (value for value in map(extract_season, candidate_titles) if value),
                None,
            )
            if season and candidate_season and candidate_season != season:
                continue
            if season and season > 1 and candidate_season is None:
                # 系列级 ID 和作品标题常指向第一季，不能据此把无季号条目当成续季。
                continue
            candidate_year = extract_year(row.get("years") or row.get("release"))
            if media_type == "movie" and expected_year:
                if not candidate_year or candidate_year != expected_year:
                    continue
            score = 1000 if exact_external_id else 100
            if candidate_year and candidate_year == expected_year:
                score += 40
            if season and candidate_season == season:
                score += 100
            elif season and candidate_season is None:
                score += 10
            ranked = (score, -index, row)
            if exact_external_id:
                if (
                        best_external_id_match is None
                        or (score, -index) > best_external_id_match[:2]
                ):
                    best_external_id_match = ranked
            elif matched_title:
                if (
                        best_title_match is None
                        or (score, -index) > best_title_match[:2]
                ):
                    best_title_match = ranked
        best = best_external_id_match or best_title_match
        return best[2] if best else None

    def search(
            self,
            keywords: Iterable[str],
            expected_titles: Iterable[str],
            expected_year: object,
            media_type: str,
            season: Optional[int] = None,
            douban_id: Optional[object] = None,
            imdb_id: Optional[object] = None,
            limit: int = 20,
    ) -> List[Dict[str, Any]]:
        titles = [str(value).strip() for value in expected_titles if str(value or "").strip()]
        normalized_keywords = unique_texts(keywords)
        expected_douban_id = str(douban_id or "").strip()
        if not expected_douban_id.isdigit() or int(expected_douban_id) <= 0:
            expected_douban_id = ""
        if not expected_douban_id and (not titles or not normalized_keywords):
            return []
        normalized_limit = max(1, min(int(limit or 20), 80))
        year = extract_year(expected_year)
        cache_key = (tuple(normalized_keywords), tuple(titles), year, media_type,
                     season, expected_douban_id, imdb_id)
        lock = self._search_locks[hash(cache_key) % len(self._search_locks)]
        with lock:
            if expected_douban_id:
                detail = self._detail(int(expected_douban_id))
                selected = self._select_row(
                    [detail], titles, year, media_type, season,
                    expected_douban_id, imdb_id,
                )
                if not selected:
                    return []
                selected_douban_id = selected.get("doub_id") or expected_douban_id
                selected_title = selected.get("title") or (titles[0] if titles else "")
            else:
                selected = None
                for keyword in normalized_keywords:
                    rows = self._search_rows(keyword)
                    selected = self._select_row(
                        rows, titles, year, media_type, season, None, imdb_id
                    )
                    if selected:
                        break
                if not selected or not selected.get("doub_id"):
                    return []
                selected_douban_id = selected["doub_id"]
                selected_title = selected.get("title") or titles[0]
                detail = self._detail(int(selected_douban_id))
            if not detail:
                return []
            seeds = detail.get("all_seeds") or []
            results = []
            seen = set()
            for index, seed in enumerate(seeds):
                if not isinstance(seed, dict):
                    continue
                magnet = str(seed.get("zlink") or "").strip()
                key = magnet.casefold()
                if not magnet.lower().startswith("magnet:?") or key in seen:
                    continue
                seen.add(key)
                title = str(seed.get("zname") or "").strip()
                if not title:
                    title = f"{selected_title} - 磁力资源 #{index + 1}"
                results.append({
                    "id": "btl-" + hashlib.sha1(magnet.encode("utf-8")).hexdigest()[:16],
                    "url": magnet,
                    "title": title,
                    "size": str(seed.get("zsize") or "").strip() or 0,
                    "quality": str(seed.get("zqxd") or "").strip(),
                    "resource_type": "magnet",
                    "pan_type": "magnet",
                    "source": "butailing",
                    "source_service": "butailing",
                    "source_url": f"https://web5.mukaku.com/mv/{selected_douban_id}",
                    "douban_id": int(selected_douban_id),
                })
                if len(results) >= normalized_limit:
                    break
            return results

    def clear_cache(self) -> Dict[str, int]:
        with self._cache_lock:
            counts = {
                "list": len(list(self._list_cache.items())),
                "detail": len(list(self._detail_cache.items())),
            }
            self._list_cache.clear()
            self._detail_cache.clear()
        return counts
