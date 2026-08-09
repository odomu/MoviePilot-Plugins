"""HDHive WebAPI 资源查询、详情解析与解锁。"""

import copy
import html
import json
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlsplit, urlunsplit

from app.log import logger

from .client import HDHiveClient, HDHiveWebError
from ...matching import positive_ints, unique_texts
from ...types import (
    SUPPORTED_CLOUD_TYPES,
    SUPPORTED_RESOURCE_TYPES,
    normalize_resource_type,
    resource_type_from_url,
)
from ....utils import MediaFileParser
from ....utils.cache import create_platform_ttl_cache

HDHIVE_RESOURCE_TYPES = frozenset(SUPPORTED_RESOURCE_TYPES)
HDHIVE_DETAIL_RESOURCE_TYPES = HDHIVE_RESOURCE_TYPES - {"magnet"}


class HDHiveResourceService:
    """负责 HDHive 资源查询、解析、缓存和解锁。"""

    BASE_URL = "https://hdhive.com"
    _RESOURCE_CACHE_TTL = 5 * 60
    _RESOURCE_CACHE_LIMIT = 128
    _RESOURCE_LOCKS = tuple(threading.Lock() for _ in range(64))
    _PREVIEW_CACHE_TTL = 10 * 60
    _PREVIEW_CACHE_LIMIT = 256
    _ACCESSIBLE_URL_CACHE_TTL = 10 * 60
    _ACCESSIBLE_URL_CACHE_LIMIT = 256
    _ACCESSIBLE_URL_LOCKS = tuple(threading.Lock() for _ in range(64))
    _PREVIEW_INTERVAL_SECONDS = 12.0
    _PREVIEW_WINDOW_SECONDS = 60.0
    _PREVIEWS_PER_WINDOW = 4
    _PREVIEW_STATE_LOCK = threading.RLock()
    _PREVIEW_HISTORIES: Dict[str, deque] = {}
    _PREVIEW_LOCKS: Dict[str, threading.RLock] = {}
    _PREVIEW_ACCOUNT_LOCKS: Dict[str, threading.Lock] = {}
    _PREVIEW_LOCK_BUCKETS = 32
    _SCHEMA_FAILURE_TTL = 60
    _PREVIEW_UNAVAILABLE_TTL = 15
    _TORRENTCLAW_CACHE_TTL = 30 * 60
    _TORRENTCLAW_CACHE_LIMIT = 128
    _TORRENTCLAW_REQUEST_INTERVAL = 60.0
    _TORRENTCLAW_STATE_LOCK = threading.RLock()
    _TORRENTCLAW_LAST_REQUEST_AT: Dict[str, float] = {}
    _TORRENTCLAW_RETRY_UNTIL: Dict[str, float] = {}
    _NEXT_REDIRECT_RE = re.compile(
        r"NEXT_REDIRECT;(?:replace|push);"
        r"((?:\\/|/)(?:movie|tv)(?:\\/|/)[A-Za-z0-9_-]+);[0-9]{3};",
        re.I,
    )
    _NEXT_SCRIPT_RE = re.compile(
        r"self\.__next_f\.push\((\[.*?\])\)</script>", re.S
    )
    _ED2K_URL_RE = re.compile(
        r"ed2k://\|file\|[^|\r\n]+\|\d+\|[0-9A-Fa-f]{32}"
        r"(?:\|(?:h|p)=[^|\r\n]+)*\|/",
        re.I,
    )
    _EMBEDDED_ESCAPE_RE = re.compile(
        r"\\+(u003a|u002f|u0026|u003d|u003f|u002b|/|[\"'])", re.I
    )

    def __init__(
            self,
            client: HDHiveClient,
            torrentclaw_enabled: bool = False,
            torrentclaw_subtitle_languages: Optional[List[str]] = None,
    ):
        self._client = client
        self._torrentclaw_enabled = bool(torrentclaw_enabled)
        self._torrentclaw_subtitle_languages = self._normalize_languages(
            torrentclaw_subtitle_languages or ["zh"]
        )
        session_key = client.cache_namespace
        self._session_key = session_key
        self._resource_cache = create_platform_ttl_cache(
            "hdhive:web:rows",
            session_key,
            maxsize=self._RESOURCE_CACHE_LIMIT,
            ttl=self._RESOURCE_CACHE_TTL,
        )
        self._schema_failure_cache = create_platform_ttl_cache(
            "hdhive:web:schema_failures",
            session_key,
            maxsize=self._RESOURCE_CACHE_LIMIT,
            ttl=self._SCHEMA_FAILURE_TTL,
        )
        self._preview_cache = create_platform_ttl_cache(
            "hdhive:web:file_preview",
            session_key,
            maxsize=self._PREVIEW_CACHE_LIMIT,
            ttl=self._PREVIEW_CACHE_TTL,
        )
        self._preview_unavailable_cache = create_platform_ttl_cache(
            "hdhive:web:preview_unavailable",
            session_key,
            maxsize=self._PREVIEW_CACHE_LIMIT,
            ttl=self._PREVIEW_UNAVAILABLE_TTL,
        )
        self._preview_capability_cache = create_platform_ttl_cache(
            "hdhive:web:preview_capability",
            session_key,
            maxsize=self._PREVIEW_CACHE_LIMIT,
            ttl=self._PREVIEW_CACHE_TTL,
        )
        self._accessible_url_cache = create_platform_ttl_cache(
            "hdhive:web:accessible_url",
            session_key,
            maxsize=self._ACCESSIBLE_URL_CACHE_LIMIT,
            ttl=self._ACCESSIBLE_URL_CACHE_TTL,
        )
        self._torrentclaw_cache = create_platform_ttl_cache(
            "hdhive:web:torrentclaw",
            session_key,
            maxsize=self._TORRENTCLAW_CACHE_LIMIT,
            ttl=self._TORRENTCLAW_CACHE_TTL,
        )
        self._lock = threading.RLock()

    def matches_config(
            self,
            client: HDHiveClient,
            torrentclaw_enabled: bool,
            torrentclaw_subtitle_languages: Any,
    ) -> bool:
        return (
                self._client is client
                and self._torrentclaw_enabled == bool(torrentclaw_enabled)
                and self._torrentclaw_subtitle_languages
                == self._normalize_languages(torrentclaw_subtitle_languages or ["zh"])
        )

    def clear_cache(self) -> Dict[str, int]:
        with self._lock:
            counts = {
                "resources": len(list(self._resource_cache.items())),
                "previews": len(list(self._preview_cache.items())),
                "accessible_urls": len(list(self._accessible_url_cache.items())),
                "torrentclaw": len(list(self._torrentclaw_cache.items())),
            }
            self._resource_cache.clear()
            self._schema_failure_cache.clear()
            self._preview_cache.clear()
            self._preview_unavailable_cache.clear()
            self._preview_capability_cache.clear()
            self._accessible_url_cache.clear()
            self._torrentclaw_cache.clear()
            return counts

    def _preview_lock(self, preview_key: str) -> threading.RLock:
        """合并同一账号、同一资源的并发预览，不阻塞其他缓存命中。"""
        bucket = hash(preview_key) % self._PREVIEW_LOCK_BUCKETS
        lock_key = f"{self._client.cache_namespace}:{bucket}"
        with self._PREVIEW_STATE_LOCK:
            return self._PREVIEW_LOCKS.setdefault(lock_key, threading.RLock())

    def _preview_account_lock(self) -> threading.Lock:
        with self._PREVIEW_STATE_LOCK:
            return self._PREVIEW_ACCOUNT_LOCKS.setdefault(
                self._client.cache_namespace, threading.Lock()
            )

    def _wait_for_preview_slot(self, log_prefix: str) -> None:
        """限制详情页 file-list 的账号级调用频率。"""
        logged = False
        while True:
            if self._client.cooldown_remaining > 0:
                raise HDHiveWebError(
                    "HDHive WebAPI 处于风控冷却期，跳过文件预览",
                    code="rate_limited",
                )
            with self._PREVIEW_STATE_LOCK:
                history = self._PREVIEW_HISTORIES.setdefault(
                    self._client.cache_namespace, deque()
                )
                now = time.monotonic()
                while history and now - history[0] >= self._PREVIEW_WINDOW_SECONDS:
                    history.popleft()
                wait_seconds = 0.0
                if history:
                    wait_seconds = max(
                        wait_seconds,
                        self._PREVIEW_INTERVAL_SECONDS - (now - history[-1]),
                    )
                if len(history) >= self._PREVIEWS_PER_WINDOW:
                    wait_seconds = max(
                        wait_seconds,
                        self._PREVIEW_WINDOW_SECONDS - (now - history[0]),
                    )
                if wait_seconds <= 0:
                    history.append(now)
                    return
            if not logged:
                logger.debug(
                    f"{log_prefix} file-list 预览限频等待 {wait_seconds:.1f} 秒"
                )
                logged = True
            time.sleep(min(wait_seconds, 0.25))

    @staticmethod
    def _preview_episodes_from_files(
            files: List[Dict[str, Any]], target_season: Optional[int]
    ) -> Dict[str, List[int]]:
        episodes: Dict[str, set] = {}
        fallback_season = max(1, int(target_season or 1))
        for item in files or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("path") or item.get("name") or "").strip()
            parsed = MediaFileParser.extract_season_episode(name)
            if parsed:
                episodes.setdefault(str(parsed[0]), set()).add(parsed[1])
                continue
            match = re.search(r"(?i)(?:^|[^A-Za-z0-9])EP?0*(\d{1,4})(?!\d)", name)
            if not match:
                match = re.search(r"第\s*(\d{1,4})\s*集", name)
            if match:
                episodes.setdefault(str(fallback_season), set()).add(
                    int(match.group(1))
                )
        return {
            season: sorted(values)
            for season, values in episodes.items()
            if values
        }

    def _load_file_preview(
            self,
            slug: str,
            resource_type: str,
            log_prefix: str,
            detail_path: str = "",
    ) -> Dict[str, Any]:
        """限频读取并缓存 file-list，不访问详情页且绝不触发解锁。"""
        preview_key = f"{resource_type}:{slug}"
        preview = self._preview_cache.get(preview_key)
        if isinstance(preview, dict):
            logger.debug(f"{log_prefix} 命中 file-list 预览缓存：slug={slug}")
            return copy.deepcopy(preview)
        failure_key = f"preview:{preview_key}"
        unavailable = self._preview_unavailable_cache.get(preview_key)
        if unavailable:
            raise HDHiveWebError(str(unavailable), code="preview_unavailable")
        cached_failure = self._schema_failure_cache.get(failure_key)
        if cached_failure:
            raise HDHiveWebError(str(cached_failure), code="preview_invalid")
        with self._preview_lock(preview_key):
            preview = self._preview_cache.get(preview_key)
            if isinstance(preview, dict):
                return copy.deepcopy(preview)
            unavailable = self._preview_unavailable_cache.get(preview_key)
            if unavailable:
                raise HDHiveWebError(
                    str(unavailable), code="preview_unavailable"
                )
            cached_failure = self._schema_failure_cache.get(failure_key)
            if cached_failure:
                raise HDHiveWebError(str(cached_failure), code="preview_invalid")
            account_lock = self._preview_account_lock()
            if not account_lock.acquire(blocking=False):
                raise HDHiveWebError(
                    "HDHive 已有资源预览正在进行，请稍后再试",
                    code="preview_busy",
                )
            try:
                preview_started = time.monotonic()
                self._wait_for_preview_slot(log_prefix)
                requested_at = time.monotonic()
                resolved_detail_path = self._resolve_detail_path(
                    resource_type, slug, detail_path
                )
                response = self._client.signed_request(
                    "GET",
                    f"/api/customer/resources/{slug}/file-list",
                    headers={
                        "accept": "application/json",
                        "referer": f"{self.BASE_URL}{resolved_detail_path}",
                    },
                )
                logger.debug(
                    f"{log_prefix} file-list 请求完成：slug={slug}，"
                    f"限频等待={requested_at - preview_started:.2f}s，"
                    f"请求链={time.monotonic() - requested_at:.2f}s"
                )
                try:
                    payload = response.json()
                except ValueError as error:
                    self._schema_failure_cache.set(
                        failure_key, "HDHive file-list 响应格式异常"
                    )
                    raise HDHiveWebError(
                        "HDHive file-list 响应格式异常", code="preview_invalid"
                    ) from error
                preview = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(preview, dict):
                    error_value = payload.get("error") if isinstance(payload, dict) else None
                    message = str(
                        payload.get("message")
                        or (
                            error_value.get("message")
                            if isinstance(error_value, dict) else error_value
                        )
                        or ""
                        if isinstance(payload, dict) else ""
                    ).strip()
                    business_failure = isinstance(payload, dict) and (
                            payload.get("success") is False
                            or bool(message)
                            or isinstance(error_value, dict)
                    )
                    if business_failure:
                        failure_message = "HDHive 暂时无法提供该资源文件列表"
                        self._preview_unavailable_cache.set(
                            preview_key, failure_message
                        )
                        logger.debug(
                            f"{log_prefix} file-list 业务失败：slug={slug}，"
                            f"原因={message or '站点未返回文件列表'}"
                        )
                        raise HDHiveWebError(
                            failure_message, code="preview_unavailable"
                        )
                    failure_message = (
                        f"HDHive file-list 缺少 data：{message}"
                        if message else "HDHive file-list 缺少 data"
                    )
                    self._schema_failure_cache.set(failure_key, failure_message)
                    raise HDHiveWebError(
                        failure_message, code="preview_invalid"
                    )
                self._preview_cache.set(preview_key, copy.deepcopy(preview))
                return copy.deepcopy(preview)
            finally:
                account_lock.release()

    def preview_resource(
            self,
            slug: str,
            resource_type: str,
            target_season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            supports_file_preview: Optional[bool] = None,
            detail_path: str = "",
            log_prefix: str = "[HDHIVE]",
    ) -> Dict[str, Any]:
        """只读预览未解锁资源，返回文件和集数信息。"""
        normalized_slug = str(slug or "").strip()
        normalized_type = str(resource_type or "").strip().lower()
        if (
                not normalized_slug
                or normalized_type not in HDHIVE_DETAIL_RESOURCE_TYPES
        ):
            raise HDHiveWebError(
                "HDHive 资源标识或类型无效", code="invalid_resource"
            )
        can_preview = supports_file_preview
        if can_preview is None:
            can_preview = self._resolve_file_preview_capability(
                normalized_slug, normalized_type, log_prefix, detail_path
            )
        if not can_preview:
            raise HDHiveWebError(
                f"HDHive {normalized_type.upper()} 资源不支持文件列表预览",
                code="preview_unsupported",
            )
        preview = self._load_file_preview(
            normalized_slug, normalized_type, log_prefix, detail_path
        )
        files = preview.get("files") or []
        preview_episodes = self._preview_episodes_from_files(
            files, target_season
        )
        targets = positive_ints(target_episodes)
        available = positive_ints(preview_episodes.get(
            str(max(1, int(target_season or 1)))
        ))
        return {
            "files": copy.deepcopy(files),
            "preview_episodes": preview_episodes,
            "resource_validate_status": str(
                preview.get("resource_validate_status") or ""
            ),
            "resource_validate_message": str(
                preview.get("resource_validate_message") or ""
            ),
            "covers_target": bool(targets & available) if targets else None,
        }

    @classmethod
    def _decode_embedded_text(cls, value: Any) -> str:
        """还原 Next.js Flight/JSON 中可能重复编码的 URL 转义。"""
        replacements = {
            "u003a": ":",
            "u002f": "/",
            "u0026": "&",
            "u003d": "=",
            "u003f": "?",
            "u002b": "+",
            "/": "/",
            '"': '"',
            "'": "'",
        }
        text = str(value or "")
        for _ in range(4):
            decoded = html.unescape(unquote(text))
            decoded = cls._EMBEDDED_ESCAPE_RE.sub(
                lambda match: replacements[match.group(1).lower()], decoded
            )
            if decoded == text:
                break
            text = decoded
        return text

    @staticmethod
    def _valid_share_url(value: str, resource_type: str) -> bool:
        candidate = str(value or "").strip()
        if not candidate or "\\" in candidate or any(
                ord(character) < 32 for character in candidate
        ):
            return False
        if resource_type == "ed2k":
            return bool(HDHiveResourceService._ED2K_URL_RE.fullmatch(candidate))
        try:
            parsed = urlparse(candidate)
            hostname = parsed.hostname
        except ValueError:
            return False
        valid = (
                parsed.scheme.lower() in {"http", "https"}
                and bool(hostname)
                and resource_type_from_url(candidate) == resource_type
        )
        if not valid or resource_type != "115":
            return valid
        share_code = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        receive_code = dict(parse_qsl(parsed.query)).get("password", "")
        return bool(
            re.fullmatch(r"[A-Za-z0-9]+", share_code)
            and re.fullmatch(r"[A-Za-z0-9]{4}", receive_code)
        )

    @classmethod
    def _normalize_share_url(
            cls, value: Any, resource_type: str, access_code: Any = ""
    ) -> str:
        candidate = cls._decode_embedded_text(value).strip().rstrip("),.;]}")
        if resource_type == "115" and candidate:
            parsed = urlsplit(candidate)
            query = parse_qsl(parsed.query, keep_blank_values=True)
            embedded_code = next((
                item for key, item in query
                if key.lower() in {"password", "pwd", "receive_code"}
            ), "")
            code = cls._decode_embedded_text(
                access_code or embedded_code
            ).strip()
            if re.fullmatch(r"[A-Za-z0-9]{4}", code):
                query = [
                    (key, item) for key, item in query
                    if key.lower() not in {"password", "pwd", "receive_code"}
                ]
                query.append(("password", code))
                candidate = urlunsplit(parsed._replace(query=urlencode(query)))
        return candidate if cls._valid_share_url(candidate, resource_type) else ""

    @classmethod
    def _share_url_from_values(cls, values: Any, resource_type: str) -> str:
        pending = [values]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                access_code = next((
                    item for key, item in value.items()
                    if str(key).replace("_", "").lower()
                       in {"password", "pwd", "receivecode"}
                ), "")
                for item in value.values():
                    if isinstance(item, (dict, list, tuple, set)):
                        continue
                    normalized = cls._normalize_share_url(
                        item, resource_type, access_code
                    )
                    if normalized:
                        return normalized
                pending.extend(value.values())
                continue
            if isinstance(value, (list, tuple, set)):
                pending.extend(value)
                continue
            text = cls._decode_embedded_text(value)
            if resource_type == "ed2k":
                match = cls._ED2K_URL_RE.search(text)
                if match:
                    normalized = cls._normalize_share_url(
                        match.group(0), resource_type
                    )
                    if normalized:
                        return normalized
            for match in re.finditer(r"https?://[^\s\\\"'<>]+", text, re.I):
                if match.end() < len(text) and text[match.end()] == "\\":
                    continue
                candidate = match.group(0).rstrip("),.;]}")
                access_code = ""
                if resource_type == "115":
                    nearby = text[max(0, match.start() - 256):match.end() + 256]
                    code_match = re.search(
                        r'["\'](?:password|pwd|receive_?code)["\']\s*[:=]\s*'
                        r'["\']([A-Za-z0-9]{4})["\']',
                        nearby,
                        re.I,
                    )
                    access_code = code_match.group(1) if code_match else ""
                normalized = cls._normalize_share_url(
                    candidate, resource_type, access_code
                )
                if normalized:
                    return normalized
        return ""

    def _http_access_resource(
            self,
            slug: str,
            resource_type: str,
            listed_points: int,
            is_unlocked: bool,
            target_season: Optional[int],
            target_episodes: Optional[List[int]],
            supports_file_preview: Optional[bool],
            detail_path: str,
            log_prefix: str,
    ) -> Dict[str, Any]:
        """按 HDHive 页面真实流程预览并取得资源链接。"""
        detail_path = self._resolve_detail_path(
            resource_type, slug, detail_path
        )
        if is_unlocked:
            access_key = f"{resource_type}:{slug}"
            share_url = self._accessible_url_cache.get(access_key) or ""
            if share_url and not self._valid_share_url(share_url, resource_type):
                self._accessible_url_cache.delete(access_key)
                share_url = ""
            if share_url:
                logger.debug(
                    f"{log_prefix} 命中已解锁资源链接缓存：slug={slug}"
                )
                return {
                    "url": share_url,
                    "actual_points": 0,
                    "preview_episodes": {},
                    "is_unlocked": True,
                }
            lock_key = f"{self._session_key}:{access_key}"
            access_lock = self._ACCESSIBLE_URL_LOCKS[
                hash(lock_key) % len(self._ACCESSIBLE_URL_LOCKS)
                ]
            with access_lock:
                share_url = self._accessible_url_cache.get(access_key) or ""
                if share_url and not self._valid_share_url(share_url, resource_type):
                    self._accessible_url_cache.delete(access_key)
                    share_url = ""
                if not share_url:
                    page_started = time.monotonic()
                    page_response = self._client.request(
                        "GET",
                        detail_path,
                        headers={
                            "accept": "text/html,application/xhtml+xml",
                            "referer": f"{self.BASE_URL}/",
                        },
                    )
                    page_text = getattr(page_response, "text", "") or ""
                    parse_started = time.monotonic()
                    share_url = self._share_url_from_values(
                        page_text, resource_type
                    )
                    logger.debug(
                        f"{log_prefix} 已解锁资源详情读取完成：slug={slug}，"
                        f"请求={parse_started - page_started:.2f}s，"
                        f"解析={time.monotonic() - parse_started:.3f}s"
                    )
                    if share_url:
                        self._accessible_url_cache.set(access_key, share_url)
                if not share_url:
                    logger.warning(
                        f"{log_prefix} 已解锁资源详情页未解析到链接：slug={slug}"
                    )
                return {
                    "url": share_url,
                    "actual_points": 0,
                    "preview_episodes": {},
                    "is_unlocked": bool(share_url),
                }

        can_preview = supports_file_preview
        if can_preview is None:
            can_preview = self._resolve_file_preview_capability(
                slug, resource_type, log_prefix, detail_path
            )
        preview_episodes: Dict[str, List[int]] = {}
        if can_preview:
            preview = self._load_file_preview(
                slug, resource_type, log_prefix, detail_path
            )
            files = preview.get("files") or []
            preview_episodes = self._preview_episodes_from_files(files, target_season)
            targets = positive_ints(target_episodes)
            season_key = str(max(1, int(target_season or 1)))
            available = positive_ints(preview_episodes.get(season_key))
            if targets and (not available or not (targets & available)):
                logger.debug(f"{log_prefix} file-list 未覆盖当前缺集，跳过资源：slug={slug}")
                return {
                    "url": "",
                    "actual_points": 0,
                    "preview_episodes": preview_episodes,
                    "is_unlocked": False,
                    "skip_reason": "target_not_covered",
                }
            if str(preview.get("resource_validate_status") or "").lower() == "invalid":
                logger.debug(f"{log_prefix} file-list 标记资源失效，跳过资源：slug={slug}")
                return {
                    "url": "",
                    "actual_points": 0,
                    "preview_episodes": preview_episodes,
                    "is_unlocked": False,
                    "skip_reason": "resource_invalid",
                }
        else:
            logger.debug(
                f"{log_prefix} {resource_type.upper()} 不支持 file-list，"
                f"已按资源卡片 remark 预筛结果继续：slug={slug}"
            )
        response = self._client.web_unlock_request(
            detail_path,
            slug,
            page_headers={
                "accept": "text/html,application/xhtml+xml",
                "referer": f"{self.BASE_URL}/",
            },
            response_handler=lambda value: self._unlock_response(
                value, listed_points, resource_type
            ),
        )
        return {**response, "preview_episodes": preview_episodes, "is_unlocked": bool(response.get("url"))}

    @classmethod
    def _find_group_data(cls, value: Any) -> Optional[Dict[str, Any]]:
        pending = [value]
        while pending:
            current = pending.pop()
            if isinstance(current, dict):
                group_data = current.get("groupData")
                if isinstance(group_data, dict):
                    return group_data
                pending.extend(current.values())
            elif isinstance(current, list):
                pending.extend(current)
        return None

    @classmethod
    def _parse_group_data(cls, html: str) -> Dict[str, Any]:
        decoder = json.JSONDecoder()
        for script_match in cls._NEXT_SCRIPT_RE.finditer(html or ""):
            try:
                payload = json.loads(script_match.group(1))
            except (TypeError, ValueError):
                continue
            fragments = payload if isinstance(payload, list) else [payload]
            for fragment in fragments:
                if not isinstance(fragment, str) or "groupData" not in fragment:
                    continue
                for start, char in enumerate(fragment):
                    if char not in "[{":
                        continue
                    try:
                        parsed, _ = decoder.raw_decode(fragment, start)
                    except ValueError:
                        continue
                    group_data = cls._find_group_data(parsed)
                    if group_data is not None:
                        return group_data
        if any(marker in (html or "") for marker in ("暂无资源", "暂时没有资源", "尚无资源")):
            return {}
        raise HDHiveWebError(
            "HDHive 详情页未解析到 groupData", code="schema_changed"
        )

    @staticmethod
    def _flatten_group_data(group_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for values in (group_data or {}).values():
            if not isinstance(values, list):
                continue
            rows.extend(dict(value) for value in values if isinstance(value, dict))
        return HDHiveResourceService._deduplicate(rows)

    def _load_resource_rows(
            self,
            media_type: str,
            tmdb_id: int,
            resource_types: List[str],
            force_refresh: bool = False,
            log_prefix: str = "[HDHIVE]",
    ) -> List[Dict[str, Any]]:
        normalized_type = str(media_type or "").strip().lower()
        if normalized_type not in {"movie", "tv"}:
            raise HDHiveWebError("HDHive 媒体类型无效", code="invalid_media_type")
        enabled_types = tuple(dict.fromkeys(
            str(value or "").strip().lower()
            for value in (resource_types or [])
            if str(value or "").strip().lower() in HDHIVE_RESOURCE_TYPES
        ))
        if not enabled_types:
            return []
        cache_key = f"v2:{normalized_type}:{int(tmdb_id)}"
        requested = set(enabled_types)

        def select_rows(raw_rows: Any) -> List[Dict[str, Any]]:
            rows = [
                dict(row) for row in (raw_rows or [])
                if isinstance(row, dict)
                   and HDHiveResourceService._resource_type(row) in requested
            ]
            if self._torrentclaw_enabled and "magnet" in requested:
                rows.extend(self._load_torrentclaw_rows(
                    normalized_type, int(tmdb_id), log_prefix
                ))
            return HDHiveResourceService._deduplicate(rows)

        if not force_refresh:
            cached = self._resource_cache.get(cache_key)
            if isinstance(cached, list):
                logger.debug(
                    f"{log_prefix} WebAPI 命中资源页缓存：{len(cached)} 条原始资源"
                )
                return select_rows(cached)
            if self._schema_failure_cache.get(cache_key):
                logger.debug(f"{log_prefix} WebAPI 命中详情解析失败短缓存，跳过重复请求")
                return []
        lock_key = f"{self._session_key}:{cache_key}"
        cache_lock = self._RESOURCE_LOCKS[
            hash(lock_key) % len(self._RESOURCE_LOCKS)
            ]
        with cache_lock:
            if not force_refresh:
                cached = self._resource_cache.get(cache_key)
                if isinstance(cached, list):
                    logger.debug(
                        f"{log_prefix} WebAPI 等待并命中资源页缓存："
                        f"{len(cached)} 条原始资源"
                    )
                    return select_rows(cached)
                if self._schema_failure_cache.get(cache_key):
                    logger.debug(
                        f"{log_prefix} WebAPI 等待并命中详情解析失败短缓存，"
                        "跳过重复请求"
                    )
                    return []
            try:
                route_started = time.monotonic()
                with self._client.related_requests(2):
                    detail_path = self._client.request(
                        "GET",
                        f"/tmdb/{normalized_type}/{int(tmdb_id)}",
                        headers={
                            "accept": (
                                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                                "*/*;q=0.8"
                            ),
                            "cache-control": "no-cache",
                            "referer": f"{self._client.BASE_URL}/",
                        },
                        response_handler=self._detail_path_from_response,
                    )
                    detail_started = time.monotonic()
                    group_data = self._client.request(
                        "GET",
                        detail_path,
                        headers={
                            "accept": (
                                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                                "*/*;q=0.8"
                            ),
                            "cache-control": "no-cache",
                            "referer": f"{self._client.BASE_URL}/",
                        },
                        response_handler=self._group_data_from_response,
                    )
                parsed_at = time.monotonic()
                logger.debug(
                    f"{log_prefix} WebAPI 页面阶段耗时："
                    f"媒体页={detail_started - route_started:.2f}s，"
                    f"资源页={parsed_at - detail_started:.2f}s"
                )
            except HDHiveWebError as error:
                if error.code == "schema_changed":
                    self._schema_failure_cache.set(cache_key, True)
                    logger.debug(f"{log_prefix} WebAPI 详情结构未识别，写入 60 秒短缓存")
                    return []
                raise
            rows = self._flatten_group_data(group_data)
            logger.debug(
                f"{log_prefix} WebAPI 分组解析耗时："
                f"{time.monotonic() - parsed_at:.3f}s"
            )
            if not group_data:
                logger.debug(f"{log_prefix} WebAPI 详情页无资源分组，按正常空结果处理")
            self._resource_cache.set(cache_key, copy.deepcopy(rows))
            return select_rows(rows)

    def _detail_path_from_response(self, response) -> str:
        headers = getattr(response, "headers", {}) or {}
        current_path = str(
            headers.get("x-current-path")
            or headers.get("x-current-url")
            or ""
        ).strip()
        if current_path:
            current_path = current_path.split("?", 1)[0]
            if re.fullmatch(r"/(?:tv|movie)/[A-Za-z0-9_-]+", current_path, re.I):
                return current_path
        redirect_match = self._NEXT_REDIRECT_RE.search(
            self._client.response_text(response)
        )
        if redirect_match:
            return redirect_match.group(1).replace("\\/", "/")
        raise HDHiveWebError(
            "HDHive TMDB 入口未返回资源详情路径",
            code="schema_changed",
        )

    def _group_data_from_response(self, response) -> Dict[str, Any]:
        html = self._client.response_text(response)
        try:
            return self._parse_group_data(html)
        except HDHiveWebError as error:
            if error.code != "schema_changed":
                raise
            risk_markers = (
                "cf-chl-", "challenge-platform", "captcha", "访问频繁",
                "页面过期", "请刷新页面", "安全验证",
            )
            if not any(marker.lower() in html.lower() for marker in risk_markers):
                raise
            self._client.activate_risk_cooldown("详情页挑战保护")
            raise HDHiveWebError(
                "HDHive 详情页触发安全验证，已进入 600 秒风险保护冷却",
                code="rate_limited",
            ) from error

    def _load_torrentclaw_rows(
            self, media_type: str, tmdb_id: int, log_prefix: str
    ) -> List[Dict[str, Any]]:
        cache_key = f"{media_type}:{tmdb_id}"
        state_key = self._client.cache_namespace
        cached = self._torrentclaw_cache.get(cache_key)
        if isinstance(cached, dict):
            return HDHiveResourceService._torrentclaw_rows(cached)
        with self._lock:
            with self._TORRENTCLAW_STATE_LOCK:
                now = time.monotonic()
                last_request_at = self._TORRENTCLAW_LAST_REQUEST_AT.get(
                    state_key, 0.0
                )
                retry_until = self._TORRENTCLAW_RETRY_UNTIL.get(
                    state_key, 0.0
                )
                wait_seconds = max(
                    retry_until - now,
                    self._TORRENTCLAW_REQUEST_INTERVAL
                    - (now - last_request_at),
                    0.0,
                )
            if wait_seconds > 0:
                logger.debug(
                    f"{log_prefix} TorrentClaw 等待限速 {wait_seconds:.1f}s"
                )
                time.sleep(wait_seconds)
            started = time.monotonic()
            response = self._client.request(
                "GET",
                "/api/torrentclaw/torrents",
                params={"tmdbId": tmdb_id, "type": media_type},
                headers={
                    "accept": "application/json",
                    "referer": f"{self._client.BASE_URL}/",
                },
            )
            with self._TORRENTCLAW_STATE_LOCK:
                self._TORRENTCLAW_LAST_REQUEST_AT[state_key] = time.monotonic()
            try:
                payload = response.json()
            except ValueError as error:
                raise HDHiveWebError(
                    "TorrentClaw 返回数据格式异常", code="schema_changed"
                ) from error
            message = str(payload.get("message") or "") if isinstance(payload, dict) else ""
            if "获取过于频繁" in message:
                retry_match = re.search(r"(\d+)\s*秒后重试", message)
                retry_seconds = int(retry_match.group(1)) if retry_match else 60
                with self._TORRENTCLAW_STATE_LOCK:
                    self._TORRENTCLAW_RETRY_UNTIL[state_key] = (
                            time.monotonic() + retry_seconds + 1
                    )
                logger.debug(
                    f"{log_prefix} TorrentClaw 限流，{retry_seconds}s 后可重试"
                )
                return []
            if not isinstance(payload, dict):
                return []
            self._torrentclaw_cache.set(cache_key, payload)
            rows = HDHiveResourceService._torrentclaw_rows(payload)
            preferred = self._torrentclaw_subtitle_languages
            if preferred:
                matched = [
                    row for row in rows
                    if any(
                        str(actual or "").lower() == expected
                        or str(actual or "").lower().startswith(f"{expected}-")
                        for actual in (row.get("subtitle_languages") or [])
                        for expected in preferred
                    )
                ]
                if matched:
                    rows = matched
            logger.debug(
                f"{log_prefix} TorrentClaw 返回 {len(rows)} 条，"
                f"耗时={time.monotonic() - started:.2f}s"
            )
            return rows

    def search_test_resources(
            self,
            tmdb_id: int,
            media_type: str,
            candidate_limit: int = 20,
            log_prefix: str = "[HDHIVE]",
    ) -> List[Dict[str, Any]]:
        """只读取资源页原始候选，不访问详情、不套预算或订阅规则。"""
        return self.search_resources(
            tmdb_id=tmdb_id,
            media_type=media_type,
            include_paid=True,
            resource_types=list(HDHIVE_RESOURCE_TYPES),
            candidate_limit=max(1, int(candidate_limit or 20)),
            log_prefix=log_prefix,
        )

    def search_resources(
            self,
            tmdb_id: int,
            media_type: str,
            include_paid: bool,
            target_season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            target_episode_air_dates: Optional[Dict[int, str]] = None,
            resource_types: Optional[List[str]] = None,
            magnet_filter: Optional[
                Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]
            ] = None,
            candidate_limit: Optional[int] = 4,
            log_prefix: str = "[HDHIVE]",
    ) -> List[Dict[str, Any]]:
        enabled_types = list(resource_types or HDHIVE_RESOURCE_TYPES)
        rows = self._load_resource_rows(
            media_type, int(tmdb_id), enabled_types, log_prefix=log_prefix
        )
        detail_rows = [
            row for row in rows
            if HDHiveResourceService._resource_type(row) != "magnet"
        ]
        magnet_rows = [
            row for row in rows
            if HDHiveResourceService._resource_type(row) == "magnet"
        ]
        preview_count = 0
        for row in detail_rows:
            preview_episodes = self._preview_episodes_from_row(
                row, target_season=target_season
            )
            if preview_episodes:
                row["preview_episodes"] = preview_episodes
                preview_count += 1
        target_episode_set = positive_ints(target_episodes)
        target_season_key = str(max(1, int(target_season or 1)))
        coverage_order = {}
        matched_rows = []
        remark_filtered_count = 0
        for row in detail_rows:
            preview = row.get("preview_episodes") or {}
            if not target_episode_set or not preview:
                coverage = (2, 0) if target_episode_set else (0, 0)
            else:
                covered = target_episode_set & positive_ints(
                    preview.get(target_season_key)
                )
                if not covered:
                    remark_filtered_count += 1
                    continue
                coverage = (
                    (0, -len(covered))
                    if covered == target_episode_set
                    else (1, -len(covered))
                )
            coverage_order[id(row)] = coverage
            matched_rows.append(row)
        type_order = {
            resource_type: index
            for index, resource_type in enumerate(enabled_types)
        }
        detail_rows = sorted(
            matched_rows,
            key=lambda row: (
                -self._resource_timestamp(self._resource_update_time(row)),
                type_order.get(self._resource_type(row), len(type_order)),
                not self._is_free_resource(row),
                row.get("is_official") is not True,
                *coverage_order[id(row)],
                self._unlock_points(row),
            ),
        )
        if magnet_filter:
            filtered = magnet_filter(magnet_rows)
            if filtered:
                magnet_rows = filtered
        earliest_air_time = HDHiveResourceService._earliest_target_air_time(
            target_episode_set, target_episode_air_dates or {}
        )
        media_page_url = (
            f"{self._client.BASE_URL}/tmdb/{media_type}/{int(tmdb_id)}"
        )
        results: List[Dict[str, Any]] = []
        accepted_groups = set()
        stale_count = 0
        limit = (
            None if candidate_limit is None
            else max(1, min(int(candidate_limit or 4), 20))
        )
        for row in detail_rows:
            if limit is not None and len(accepted_groups) >= limit:
                break
            resource_type = HDHiveResourceService._resource_type(row)
            slug = HDHiveResourceService._resource_slug(row)
            if not resource_type or not slug:
                continue
            detail_path = HDHiveResourceService._resource_detail_path(
                resource_type, slug, row.get("website")
            )
            update_time = self._resource_update_time(row)
            resource_time = HDHiveResourceService._resource_timestamp(update_time)
            if (
                    earliest_air_time and resource_time
                    and resource_time < earliest_air_time - 24 * 60 * 60
            ):
                stale_count += 1
                continue
            points = HDHiveResourceService._unlock_points(row)
            is_unlocked = bool(row.get("is_unlocked"))
            is_free = points == 0
            share_url = (
                self._share_url_from_values(row, resource_type)
                if is_unlocked else ""
            )
            common = {
                "title": str(row.get("title") or f"HDHive {resource_type.upper()}资源"),
                "description": str(row.get("remark") or ""),
                "resolution": row.get("video_resolution") or "",
                "quality": "",
                "subtitle": row.get("subtitle_language") or "",
                "size": row.get("share_size") or 0,
                "update_time": update_time,
                "slug": slug,
                "unlock_group": f"{resource_type}:{slug}",
                "resource_type": resource_type,
                "listed_unlock_points": points,
                "is_free": is_free,
                "is_unlocked": is_unlocked,
                "url": share_url,
                "is_official": bool(row.get("is_official")),
                "source_url": f"{self.BASE_URL}{detail_path}",
                "detail_path": detail_path,
                "media_page_url": media_page_url,
                "target_season": int(target_season or 0),
                "target_episodes": sorted(target_episode_set),
                "preview_episodes": copy.deepcopy(
                    row.get("preview_episodes") or {}
                ),
                "supports_file_preview": self._file_preview_capability(row),
            }
            if is_unlocked:
                results.append({
                    **common,
                    "need_access": False,
                    "need_unlock": False,
                    "unlock_points": points,
                })
            elif is_free:
                results.append({
                    **common,
                    "url": "",
                    "need_access": False,
                    "need_unlock": True,
                    "unlock_points": 0,
                })
            elif include_paid:
                results.append({
                    **common,
                    "url": "",
                    "need_unlock": True,
                    "need_access": False,
                    "unlock_points": points,
                })
            else:
                continue
            accepted_groups.add(f"{resource_type}:{slug}")

        for row in magnet_rows:
            if limit is not None and len(accepted_groups) >= limit:
                break
            magnet_url = str(row.get("url") or "").strip()
            if not magnet_url.lower().startswith("magnet:?"):
                continue
            slug = HDHiveResourceService._resource_slug(row)
            group = f"magnet:{slug or magnet_url}"
            if group in accepted_groups:
                continue
            results.append({
                "title": str(row.get("title") or "HDHive Magnet资源"),
                "description": row.get("description", ""),
                "resource_type": "magnet",
                "url": magnet_url,
                "slug": slug,
                "unlock_group": group,
                "need_unlock": False,
                "need_access": False,
                "unlock_points": 0,
                "listed_unlock_points": 0,
                "is_free": True,
                "is_unlocked": True,
                "size": row.get("size") or 0,
                "update_time": row.get("created_at") or "",
                "source_url": str(row.get("source_url") or ""),
                "media_page_url": media_page_url,
            })
            accepted_groups.add(group)
        logger.debug(
            f"{log_prefix} WebAPI 候选整理完成：原始={len(rows)}，"
            f"集数已识别={preview_count}，remark过滤={remark_filtered_count}，"
            f"时间过滤={stale_count}，"
            f"资源页={len(accepted_groups)}，候选={len(results)}"
        )
        return results

    def unlock_resource(
            self,
            slug: str,
            unlock_points: int,
            resource_type: str,
            media_page_url: str = "",
            is_unlocked: bool = False,
            target_season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            supports_file_preview: Optional[bool] = None,
            detail_path: str = "",
            log_prefix: str = "[HDHIVE]",
    ) -> Dict[str, Any]:
        """按详情页、file-list 预览和 Server Action 顺序获取链接。"""
        normalized_slug = str(slug or "").strip()
        normalized_type = str(resource_type or "").strip().lower()
        if not normalized_slug or normalized_type not in HDHIVE_DETAIL_RESOURCE_TYPES:
            raise HDHiveWebError("HDHive 资源标识或类型无效", code="invalid_resource")
        listed_points = max(0, int(unlock_points or 0))
        return self._http_access_resource(
            normalized_slug,
            normalized_type,
            listed_points,
            bool(is_unlocked),
            target_season,
            target_episodes,
            supports_file_preview,
            detail_path,
            log_prefix,
        )

    def _unlock_response(
            self, response, listed_points: int, normalized_type: str
    ) -> Dict[str, Any]:
        """在客户端串行锁内解析解锁结果并隔离页面上下文失败。"""
        status_code = int(getattr(response, "status_code", 0) or 0)
        content_type = str(
            getattr(response, "headers", {}).get("content-type") or ""
        ).lower()
        if "text/x-component" in content_type:
            payload = self._server_action_payload(response)
        else:
            try:
                payload = response.json()
            except ValueError as error:
                raise HDHiveWebError(
                    "HDHive 解锁响应格式异常", code="unlock_invalid_response"
                ) from error
        data = payload.get("data") if isinstance(payload, dict) else None
        if not (
                status_code < 400
                and isinstance(payload, dict)
                and payload.get("success")
                and isinstance(data, dict)
        ):
            error_value = payload.get("error") if isinstance(payload, dict) else None
            error_message = (
                error_value.get("message") if isinstance(error_value, dict) else ""
            )
            message = str(
                payload.get("message") or error_message
                if isinstance(payload, dict) else ""
            ).strip()
            if any(marker in message for marker in ("页面过期", "请刷新页面")):
                self._resource_cache.clear()
                raise HDHiveWebError(
                    f"HDHive 资源页上下文刷新后仍已过期，停止本次解锁：{message}",
                    code="page_expired",
                    status_code=status_code,
                )
            if any(marker in message for marker in (
                    "高频", "人机验证", "安全验证", "访问频繁", "操作频繁",
            )):
                self._client.activate_risk_cooldown(
                    "网页解锁要求人机验证但未返回 challenge"
                )
                raise HDHiveWebError(
                    f"HDHive 网页解锁要求人机验证但未返回可处理的 challenge：{message}",
                    code="captcha_required",
                    status_code=status_code,
                )
            if status_code == 429:
                raise HDHiveWebError(
                    f"HDHive 获取资源触发 HTTP 429 风控：{message or '请求过于频繁'}",
                    code="rate_limited",
                    status_code=status_code,
                )
            raise HDHiveWebError(
                f"HDHive 获取资源失败：{message or f'HTTP {status_code}'}",
                code="unlock_failed",
                status_code=status_code,
            )
        value = str(data.get("full_url") or data.get("url") or "").strip()
        urls = (
            list(dict.fromkeys(
                candidate
                for candidate in (
                    match.group(0) for match in self._ED2K_URL_RE.finditer(value)
                )
                if self._valid_share_url(candidate, normalized_type)
            ))
            if normalized_type == "ed2k"
            else (
                [value]
                if self._valid_share_url(value, normalized_type)
                else []
            )
        )
        charged_points = 0 if data.get("already_owned") else listed_points
        if urls:
            self._resource_cache.clear()
        return {
            "url": urls if len(urls) > 1 else (urls[0] if urls else ""),
            "actual_points": charged_points,
            "success": True,
        }

    @classmethod
    def _server_action_payload(cls, response) -> Dict[str, Any]:
        """解析 Next.js Server Action 的 text/x-component 响应。"""
        text = HDHiveClient.response_text(response)
        for line in text.splitlines():
            if not re.match(r"^[0-9a-f]+:\{", line, re.I):
                continue
            try:
                value = json.loads(line.split(":", 1)[1])
            except (json.JSONDecodeError, IndexError):
                continue
            pending = [value]
            while pending:
                current = pending.pop()
                if isinstance(current, dict):
                    action_response = current.get("response")
                    if isinstance(action_response, dict):
                        return action_response
                    action_error = current.get("error")
                    if isinstance(action_error, dict):
                        return {
                            "success": False,
                            "error": action_error,
                            "message": str(action_error.get("message") or ""),
                        }
                    pending.extend(current.values())
                elif isinstance(current, list):
                    pending.extend(current)
        raise HDHiveWebError(
            "HDHive 解锁 Server Action 响应格式异常",
            code="unlock_invalid_response",
        )

    @staticmethod
    def _normalize_languages(values: Any) -> List[str]:
        if isinstance(values, str):
            values = re.split(r"[,，\s]+", values)
        return unique_texts(
            values, lambda value: value.lower().replace("_", "-")
        )

    @classmethod
    def _torrentclaw_rows(cls, value: Any) -> List[Dict[str, Any]]:
        result = value.get("result") if isinstance(value, dict) else None
        if not isinstance(result, dict):
            return []
        content_url = str(result.get("contentUrl") or "").strip()
        source_url = urljoin(f"{cls.BASE_URL}/", content_url) if content_url else ""
        rows = []
        for torrent in result.get("torrents") or []:
            if not isinstance(torrent, dict):
                continue
            magnet_url = str(torrent.get("magnetUrl") or "").strip()
            if not magnet_url.lower().startswith("magnet:?"):
                continue
            rows.append({
                "website": "magnet",
                "url": magnet_url,
                "slug": str(torrent.get("infoHash") or "").strip(),
                "title": str(
                    torrent.get("rawTitle") or result.get("title")
                    or "HDHive Magnet资源"
                ),
                "quality": torrent.get("quality") or "",
                "codec": torrent.get("codec") or "",
                "source_type": torrent.get("sourceType") or "",
                "audio_codec": torrent.get("audioCodec") or "",
                "audio_channels": torrent.get("audioChannels"),
                "hdr_type": torrent.get("hdrType") or "",
                "release_group": torrent.get("releaseGroup") or "",
                "video_info": torrent.get("videoInfo") or {},
                "size": torrent.get("sizeBytes") or 0,
                "size_bytes": torrent.get("sizeBytes") or 0,
                "seeders": torrent.get("seeders") or 0,
                "leechers": torrent.get("leechers") or 0,
                "source": torrent.get("source") or "torrentclaw",
                "quality_score": torrent.get("qualityScore"),
                "created_at": torrent.get("uploadedAt") or "",
                "languages": torrent.get("languages") or [],
                "subtitle_languages": torrent.get("subtitleLanguages") or [],
                "subtitle": ", ".join(
                    str(language).strip()
                    for language in (torrent.get("subtitleLanguages") or [])
                    if str(language).strip()
                ),
                "season": torrent.get("season"),
                "episode": torrent.get("episode"),
                "is_free": True,
                "is_unlocked": True,
                "unlock_points": 0,
                "source_url": source_url,
            })
        return rows

    @staticmethod
    def _resource_timestamp(value: Any) -> float:
        text = str(value or "").strip()
        if not text:
            return 0
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return 0

    @staticmethod
    def _resource_update_time(row: Dict[str, Any]) -> str:
        return str(
            row.get("updated_at")
            or row.get("posted_at")
            or row.get("created_at")
            or ""
        )

    @staticmethod
    def _episode_range(start: int, end: int) -> List[int]:
        if start <= 0 or end < start or end - start > 500:
            return []
        return list(range(start, end + 1))

    @classmethod
    def _preview_episodes_from_row(
            cls, row: Dict[str, Any], target_season: Optional[int]
    ) -> Dict[str, List[int]]:
        """从 HDHive 卡片 remark 提取季集范围。"""
        text = str(row.get("remark") or "").strip()
        if not text:
            return {}

        episodes: Dict[str, set] = {}
        for match in re.finditer(
                r"S(?P<season>\d{1,3})\s*E(?P<start>\d{1,4})"
                r"(?:\s*(?:-|~|—|至)\s*(?:S(?P<end_season>\d{1,3})\s*)?"
                r"E?(?P<end>\d{1,4}))?",
                text,
                re.I,
        ):
            season = int(match.group("season"))
            end_season = int(match.group("end_season") or season)
            if season <= 0 or season != end_season:
                continue
            start = int(match.group("start"))
            values = cls._episode_range(start, int(match.group("end") or start))
            if values:
                episodes.setdefault(str(season), set()).update(values)

        fallback_season = max(1, int(target_season or 1))
        for match in re.finditer(
                r"(?<![A-Za-z0-9])(?P<start>\d{1,4})\s*"
                r"(?:-|~|—|至)\s*(?P<end>\d{1,4})\s*集", text
        ):
            values = cls._episode_range(
                int(match.group("start")), int(match.group("end"))
            )
            if values:
                episodes.setdefault(str(fallback_season), set()).update(values)

        for match in re.finditer(r"(?:更新至|更至)\s*(\d{1,4})\s*集", text):
            values = cls._episode_range(1, int(match.group(1)))
            if values:
                episodes.setdefault(str(fallback_season), set()).update(values)

        for match in re.finditer(r"第\s*(\d{1,4})\s*集", text):
            episode = int(match.group(1))
            if episode > 0:
                episodes.setdefault(str(fallback_season), set()).add(episode)

        return {
            season: sorted(values)
            for season, values in episodes.items()
            if values
        }

    @classmethod
    def _earliest_target_air_time(
            cls, target_episodes: set, episode_air_dates: Dict[int, str]
    ) -> float:
        values = [
            cls._resource_timestamp(episode_air_dates.get(episode))
            for episode in target_episodes
            if episode_air_dates.get(episode)
        ]
        return min((value for value in values if value > 0), default=0)

    @staticmethod
    def _is_free_resource(row: Dict[str, Any]) -> bool:
        return bool(row.get("is_unlocked")) or (
                row.get("unlock_points") is not None
                and HDHiveResourceService._unlock_points(row) == 0
        )

    @staticmethod
    def _resource_type(row: Dict[str, Any]) -> str:
        website = normalize_resource_type(row.get("website") or "")
        return website if website in HDHIVE_RESOURCE_TYPES else ""

    @staticmethod
    def _file_preview_capability(
            row: Optional[Dict[str, Any]] = None
    ) -> Optional[bool]:
        """读取站点显式能力字段；缺失时返回未知，不按网盘类型猜测。"""
        if isinstance(row, dict):
            for key in (
                    "file_list_preview_enabled", "fileListPreviewEnabled",
                    "supports_file_list", "supportsFileList",
            ):
                if key not in row or row.get(key) is None:
                    continue
                value = row.get(key)
                if isinstance(value, str):
                    return value.strip().lower() in {"1", "true", "yes", "on"}
                return bool(value)
        return None

    def _resolve_file_preview_capability(
            self, slug: str, resource_type: str, log_prefix: str,
            detail_path: str = "",
    ) -> bool:
        """从资源详情页实际渲染参数确认 file-list 能力并短期缓存。"""
        capability_key = f"{resource_type}:{slug}"
        cached = self._preview_capability_cache.get(capability_key)
        if isinstance(cached, bool):
            return cached
        detail_path = self._resolve_detail_path(
            resource_type, slug, detail_path
        )
        response = self._client.request(
            "GET",
            detail_path,
            headers={
                "accept": "text/html,application/xhtml+xml",
                "referer": f"{self.BASE_URL}/",
            },
        )
        page_text = self._decode_embedded_text(
            self._client.response_text(response)
        )
        matched = re.search(
            r'["\']fileListPreviewEnabled["\']\s*:\s*(true|false)',
            page_text,
            re.I,
        )
        if not matched:
            raise HDHiveWebError(
                "HDHive 资源页未声明文件列表预览能力，已停止探测",
                code="preview_capability_unknown",
            )
        supported = matched.group(1).lower() == "true"
        self._preview_capability_cache.set(capability_key, supported)
        logger.debug(
            f"{log_prefix} 资源页 file-list 能力：slug={slug}，"
            f"支持={supported}"
        )
        return supported

    @staticmethod
    def _unlock_points(row: Dict[str, Any]) -> int:
        try:
            return max(0, int(row.get("unlock_points") or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _resource_slug(row: Dict[str, Any]) -> str:
        return str(row.get("slug") or "").strip()

    @staticmethod
    def _resource_detail_path(
            resource_type: str, slug: str, route_type: Any = ""
    ) -> str:
        normalized_type = normalize_resource_type(resource_type)
        normalized_slug = str(slug or "").strip()
        if (
                normalized_type not in HDHIVE_DETAIL_RESOURCE_TYPES
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", normalized_slug)
        ):
            raise HDHiveWebError(
                "HDHive 资源类型或详情标识无效", code="invalid_resource"
            )
        segments = ["resource"]
        if normalized_type in SUPPORTED_CLOUD_TYPES:
            raw_route_type = str(route_type or normalized_type).strip().lower()
            segments.append(
                raw_route_type
                if normalize_resource_type(raw_route_type) == normalized_type
                else normalized_type
            )
        segments.append(normalized_slug)
        return "/" + "/".join(segments)

    @classmethod
    def _resolve_detail_path(
            cls, resource_type: str, slug: str, detail_path: str = ""
    ) -> str:
        """校验并复用卡片详情路径，旧候选缺失时再按资源类别推导。"""
        normalized_type = normalize_resource_type(resource_type)
        normalized_slug = str(slug or "").strip()
        candidate = str(detail_path or "").strip()
        if not candidate:
            return cls._resource_detail_path(normalized_type, normalized_slug)
        parsed = urlparse(urljoin(f"{cls.BASE_URL}/", candidate))
        if parsed.scheme not in {"http", "https"} or parsed.hostname != "hdhive.com":
            raise HDHiveWebError(
                "HDHive 资源详情地址无效", code="invalid_resource"
            )
        parts = [part for part in parsed.path.split("/") if part]
        expected_size = 3 if normalized_type in SUPPORTED_CLOUD_TYPES else 2
        valid = (
                normalized_type in HDHIVE_DETAIL_RESOURCE_TYPES
                and len(parts) == expected_size
                and parts[0] == "resource"
                and parts[-1] == normalized_slug
        )
        if expected_size == 3:
            valid = valid and normalize_resource_type(parts[1]) == normalized_type
        if not valid:
            raise HDHiveWebError(
                "HDHive 资源详情地址与资源类型不匹配", code="invalid_resource"
            )
        return parsed.path

    @classmethod
    def _deduplicate(cls, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        result = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            slug = cls._resource_slug(row)
            if slug and slug not in seen:
                seen.add(slug)
                result.append(row)
        return result
