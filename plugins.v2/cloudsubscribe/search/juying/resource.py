"""聚影资源搜索、缓存和短时票据兑换。"""

import re
import threading
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urljoin, urlparse

from app.log import logger
from cachetools import TTLCache

from .client import JuyingClient, JuyingError
from ..matching import extract_season, extract_year, normalize_title, title_matches


class JuyingResourceService:
    """使用认证客户端查询资源，不管理登录会话。"""

    _MAX_RESOURCE_PAGES = 10
    SUPPORTED_RESOURCE_TYPE_ORDER = (
        "115", "123", "quark", "guangya", "alipan", "ed2k", "magnet"
    )
    SUPPORTED_RESOURCE_TYPES = frozenset(SUPPORTED_RESOURCE_TYPE_ORDER)
    _TYPE_ALIASES = {
        "115": "115",
        "115pan": "115",
        "123": "123",
        "123pan": "123",
        "quark": "quark",
        "guangya": "guangya",
        "alipan": "alipan",
        "aliyun": "alipan",
        "magnet": "magnet",
        "magnetlink": "magnet",
        "ed2k": "ed2k",
    }

    def __init__(self, client: JuyingClient):
        self._client = client
        self._lock = threading.RLock()
        self._search_cache = TTLCache(maxsize=128, ttl=15 * 60)
        self._resource_cache = TTLCache(maxsize=1024, ttl=5 * 60)
        self._resource_context = TTLCache(maxsize=1024, ttl=15 * 60)

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(str(value or "0").strip())
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _type_matches(value: object, media_type: str) -> bool:
        actual = str(value or "").strip().casefold()
        return actual == "tv" if media_type == "tv" else actual in {"movie", "anime", "doc"}

    def _select_movie(
            self, rows: Iterable[Dict[str, Any]], expected_titles: List[str],
            expected_year: str, media_type: str, tmdb_id: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        ranked = []
        for index, row in enumerate(rows):
            exact_tmdb = bool(tmdb_id and self._safe_int(row.get("tmdb_id")) == int(tmdb_id))
            if not exact_tmdb and not self._type_matches(row.get("movie_type"), media_type):
                continue
            if not exact_tmdb and not title_matches(row.get("title"), expected_titles):
                continue
            row_year = extract_year(row.get("release_year") or row.get("year"))
            if media_type == "movie" and expected_year and row_year != expected_year:
                continue
            if media_type == "tv" and expected_year and row_year and row_year != expected_year:
                continue
            score = (1000 if exact_tmdb else 200) + (60 if row_year == expected_year else 0)
            ranked.append((score, -index, row))
        ranked.sort(reverse=True, key=lambda item: (item[0], item[1]))
        return ranked[0][2] if ranked else None

    def _find_movie(self, title: str, alternative_titles: List[str], year: str,
                    media_type: str, tmdb_id: Optional[int]):
        titles = list(dict.fromkeys(str(value).strip() for value in [title, *alternative_titles]
                                    if str(value or "").strip()))
        attempted = set()
        for query in titles:
            for query_year in ([year, ""] if year else [""]):
                key = (normalize_title(query), query_year)
                if key in attempted:
                    continue
                attempted.add(key)
                params: Dict[str, Any] = {"q": query, "page": 1, "page_size": 30}
                if query_year:
                    params["year"] = query_year
                payload = self._client.request_json("GET", "/api/app/movies/", params=params)
                selected = self._select_movie(
                    (row for row in (payload.get("results") or []) if isinstance(row, dict)),
                    titles, year, media_type, tmdb_id,
                )
                if selected:
                    return selected
        return None

    def _load_resources(self, movie_id: int) -> List[Dict[str, Any]]:
        resources, seen = [], set()
        for page in range(1, self._MAX_RESOURCE_PAGES + 1):
            payload = self._client.request_json(
                "GET", f"/api/app/movie/{movie_id}/resources/",
                params={"page": page, "page_size": 120 if page == 1 else 200},
            )
            rows = payload.get("resources") or []
            if not isinstance(rows, list) or not rows:
                break
            for row in rows:
                if not isinstance(row, dict):
                    continue
                resource_id = str(row.get("id") or "").strip()
                if resource_id and resource_id not in seen:
                    seen.add(resource_id)
                    resources.append(dict(row))
            if not payload.get("has_more"):
                break
        return resources

    @classmethod
    def _resource_type(cls, row: Dict[str, Any]) -> str:
        actual = str(row.get("resource_type") or "").strip().casefold()
        mapped = cls._TYPE_ALIASES.get(actual)
        if mapped:
            return mapped
        marker = " ".join(
            str(row.get(key) or "").strip().casefold()
            for key in (
                "resource_type_display", "title", "description",
                "resource_description", "share_link", "raw_share_link",
                "share_link_with_code",
            )
            if str(row.get(key) or "").strip()
        )
        if "magnet:?" in marker or "磁力" in marker:
            return "magnet"
        if "ed2k://" in marker or "电驴" in marker:
            return "ed2k"
        if "pan.quark.cn" in marker or "夸克" in marker or "quark" in marker:
            return "quark"
        if "guangyapan.com" in marker or "光鸭" in marker or "guangya" in marker:
            return "guangya"
        if "123云盘" in marker or "123网盘" in marker or "123pan" in marker:
            return "123"
        if "115.com" in marker or "115网盘" in marker:
            return "115"
        return ""

    @staticmethod
    def _resource_size(value: Any) -> Any:
        text = str(value or "").strip()
        if not text:
            return 0
        return f"{text}GB" if re.fullmatch(r"\d+(?:\.\d+)?", text) else text

    def _source_url(self, *values: Any) -> str:
        """只接受聚影站内返回的真实页面链接。"""
        expected_host = str(
            urlparse(self._client.base_url).hostname or ""
        ).casefold()
        for value in values:
            candidate = str(value or "").strip()
            if not candidate:
                continue
            resolved = urljoin(f"{self._client.base_url}/", candidate)
            parsed = urlparse(resolved)
            if (
                    parsed.scheme == "https"
                    and str(parsed.hostname or "").casefold() == expected_host
            ):
                return resolved
        return self._client.base_url

    def _load_search_context(self, title: str, alternative_titles: List[str], year: str,
                             media_type: str, tmdb_id: Optional[int], season: Optional[int],
                             force: bool = False,
                             filter_season: bool = True) -> List[Dict[str, Any]]:
        cache_key = (media_type, tmdb_id or 0, normalize_title(title),
                     tuple(normalize_title(item) for item in alternative_titles), year,
                     season or 0, bool(filter_season))
        if not force:
            cached = self._search_cache.get(cache_key)
            if cached is not None:
                logger.debug(f"[JUYING] 资源搜索缓存命中，候选 {len(cached)} 个")
                return [dict(item) for item in cached]
        movie = self._find_movie(title, alternative_titles, year, media_type, tmdb_id)
        if not movie:
            self._search_cache[cache_key] = []
            return []
        media_source_url = self._source_url(
            movie.get("source_url"), movie.get("detail_url"), movie.get("url")
        )
        context = {"title": title, "alternative_titles": alternative_titles, "year": year,
                   "media_type": media_type, "tmdb_id": tmdb_id, "season": season,
                   "filter_season": filter_season}
        public_rows = []
        for row in self._load_resources(int(movie.get("id"))):
            resource_type = self._resource_type(row)
            resource_id = str(row.get("id") or "").strip()
            if not resource_type or not resource_id:
                continue
            resource_title = str(row.get("title") or row.get("description") or
                                 row.get("resource_description") or f"聚影资源 #{resource_id}").strip()
            resource_season = extract_season(resource_title)
            if (filter_season and media_type == "tv" and season
                    and resource_season not in (None, season)):
                continue
            self._resource_cache[resource_id] = dict(row)
            self._resource_context[resource_id] = dict(context)
            public_rows.append({"resource_id": resource_id, "title": resource_title,
                                "description": str(
                                    row.get("resource_description") or row.get("description") or "").strip(),
                                "size": self._resource_size(row.get("file_size")),
                                "resource_type": resource_type,
                                "link_exposed": bool(row.get("link_exposed")),
                                "link_hidden_reason": str(row.get("link_hidden_reason") or ""),
                                "update_time": str(row.get("created_at") or ""),
                                "uploader": str(row.get("uploader") or ""),
                                "source_url": self._source_url(
                                    row.get("source_url"), row.get("detail_url"),
                                    row.get("url"), media_source_url,
                                )})
        self._search_cache[cache_key] = [dict(item) for item in public_rows]
        return public_rows

    def _reload_resource(self, resource_id: str) -> Optional[Dict[str, Any]]:
        context = self._resource_context.get(resource_id)
        if not context:
            return None
        self._load_search_context(**context, force=True)
        cached = self._resource_cache.get(resource_id)
        return dict(cached) if isinstance(cached, dict) else None

    @staticmethod
    def _host_matches(host: str, domains: Iterable[str]) -> bool:
        return any(host == domain or host.endswith(f".{domain}") for domain in domains)

    @classmethod
    def _target_type(cls, target: str) -> str:
        lowered = target.casefold()
        if lowered.startswith("magnet:?"):
            return "magnet"
        if lowered.startswith("ed2k://"):
            return "ed2k"
        parsed = urlparse(target)
        host = str(parsed.hostname or "").casefold()
        if parsed.scheme != "https" or not host:
            return ""
        if cls._host_matches(host, {"115.com", "115cdn.com"}):
            return "115"
        if cls._host_matches(host, {"123pan.com", "123684.com", "123865.com"}):
            return "123"
        if cls._host_matches(host, {"quark.cn"}):
            return "quark"
        if cls._host_matches(host, {"guangyapan.com"}):
            return "guangya"
        if cls._host_matches(host, {"alipan.com", "aliyundrive.com"}):
            return "alipan"
        return ""

    @classmethod
    def _validate_target(cls, resource_type: str, target: str) -> None:
        if resource_type not in cls.SUPPORTED_RESOURCE_TYPES or cls._target_type(target) != resource_type:
            raise JuyingError("聚影返回的资源链接格式异常", "juying_invalid_link")

    @staticmethod
    def _access_path(raw: Dict[str, Any], resource_id: str) -> str:
        endpoint = str(raw.get("access_endpoint") or "").strip()
        expected = f"/api/app/resource/{resource_id}/access/"
        return endpoint if endpoint == expected else expected

    @staticmethod
    def _append_access_code(resource_type: str, target: str, access_code: str) -> str:
        access_code = str(access_code or "").strip()
        if not access_code:
            return target
        if (
                resource_type == "115"
                and re.fullmatch(r"[A-Za-z0-9]{4}", access_code)
                and not re.search(r"[?&]password=", target, re.I)
        ):
            return f"{target}{'&' if '?' in target else '?'}password={access_code}"
        if resource_type == "123" and not re.search(r"[?&](?:pwd|code)=", target, re.I):
            return f"{target}{'&' if '?' in target else '?'}pwd={access_code}"
        if resource_type == "guangya" and not re.search(r"[?&](?:pwd|code)=", target, re.I):
            return f"{target}{'&' if '?' in target else '?'}code={access_code}"
        if resource_type == "quark" and not re.search(r"(?:提取码|密码|code)", target, re.I):
            return f"{target} 提取码: {access_code}"
        return target

    def _resolve_resource(self, resource_id: str) -> Dict[str, Any]:
        raw = self._resource_cache.get(resource_id)
        if not isinstance(raw, dict):
            raw = self._reload_resource(resource_id)
        if not isinstance(raw, dict):
            raise JuyingError("聚影资源票据已失效，请重新搜索", "juying_ticket_expired")
        if not raw.get("link_exposed"):
            raise JuyingError(str(raw.get("link_hidden_reason") or "当前账号不可访问该资源"),
                              "juying_link_hidden")
        ticket = str(raw.get("access_ticket") or "").strip()
        if not ticket:
            raise JuyingError("聚影资源缺少访问票据", "juying_ticket_expired")
        try:
            payload = self._client.request_json(
                "POST",
                self._access_path(raw, resource_id),
                protected_access=True,
                json={"access_ticket": ticket},
            )
        except JuyingError as error:
            if error.code != "juying_request_failed":
                raise
            refreshed = self._reload_resource(resource_id)
            refreshed_ticket = str((refreshed or {}).get("access_ticket") or "").strip()
            if not refreshed_ticket or refreshed_ticket == ticket:
                raise
            payload = self._client.request_json(
                "POST",
                self._access_path(refreshed or {}, resource_id),
                protected_access=True,
                json={"access_ticket": refreshed_ticket},
            )
        target = str(payload.get("target") or "").strip()
        if not target:
            raise JuyingError("聚影资源链接为空", "juying_empty_link")
        resource_type = self._target_type(target)
        self._validate_target(resource_type, target)
        access_code = str(payload.get("access_code") or "").strip()
        target = self._append_access_code(resource_type, target, access_code)
        return {"url": target, "resource_type": resource_type, "pan_type": resource_type,
                "access_mode": str(payload.get("access_mode") or ""),
                "expires_in": self._safe_int(payload.get("expires_in"))}

    def search(self, title: str, alternative_titles: Iterable[str], year: object,
               media_type: str, tmdb_id: Optional[int], season: Optional[int],
               resource_type_order: Iterable[str], limit: int = 5,
               test_mode: bool = False) -> List[Dict[str, Any]]:
        normalized_limit = max(1, min(int(limit or 5), 20))
        allowed_order = list(dict.fromkeys(
            str(value).strip().casefold()
            for value in resource_type_order
            if str(value).strip().casefold() in self.SUPPORTED_RESOURCE_TYPES
        ))
        if not allowed_order:
            return []
        alternatives = [str(value).strip() for value in alternative_titles if str(value or "").strip()]
        with self._lock:
            rows = self._load_search_context(str(title or "").strip(), alternatives,
                                             extract_year(year), "tv" if media_type == "tv" else "movie",
                                             tmdb_id, season,
                                             filter_season=not test_mode)
            rows.sort(key=lambda item: allowed_order.index(item["resource_type"])
            if item["resource_type"] in allowed_order else len(allowed_order))
            results = []
            for row in rows:
                if row["resource_type"] not in allowed_order:
                    continue
                if test_mode:
                    results.append({**row, "url": "", "pan_type": row["resource_type"],
                                    "source": "juying", "source_service": "juying",
                                    "source_url": row["source_url"],
                                    "juying_resource_id": row["resource_id"]})
                    if len(results) >= normalized_limit:
                        break
                    continue
                if not row["link_exposed"]:
                    continue
                try:
                    resolved = self._resolve_resource(row["resource_id"])
                except JuyingError as error:
                    if error.code == "juying_rate_limited":
                        raise
                    logger.debug(
                        f"[JUYING] 资源 #{row['resource_id']} 获取失败：{error.code}"
                    )
                    continue
                if resolved["resource_type"] not in allowed_order:
                    continue
                results.append({**resolved, "title": row["title"],
                                "description": row["description"], "size": row["size"],
                                "update_time": row["update_time"], "uploader": row["uploader"],
                                "source": "juying", "source_service": "juying",
                                "source_url": row["source_url"],
                                "juying_resource_id": row["resource_id"]})
                if len(results) >= normalized_limit:
                    break
            return results

    def clear_cache(self) -> Dict[str, int]:
        with self._lock:
            counts = {"search": len(self._search_cache), "resource": len(self._resource_cache),
                      "context": len(self._resource_context)}
            self._search_cache.clear()
            self._resource_cache.clear()
            self._resource_context.clear()
            return counts
