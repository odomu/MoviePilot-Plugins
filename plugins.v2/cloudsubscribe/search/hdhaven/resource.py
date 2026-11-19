import copy
import re
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Set
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from app.log import logger
from app.utils.string import StringUtils

from ...utils import MediaFileParser
from ...utils.cache import create_platform_ttl_cache
from ..types import normalize_resource_type, resource_type_from_url
from .client import HDHavenClient, HDHavenError


def decode_embedded_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        return text.encode("raw_unicode_escape").decode("unicode_escape")
    except Exception:
        return text


def valid_share_url(value: str, resource_type_value: str) -> bool:
    candidate = str(value or "").strip()
    if not candidate or "\\" in candidate or any(
            ord(character) < 32 for character in candidate
    ):
        return False
    if resource_type_value == "magnet":
        return candidate.lower().startswith("magnet:?")
    if resource_type_value == "ed2k":
        return candidate.lower().startswith("ed2k://")
    try:
        parsed = urlparse(candidate)
        hostname = parsed.hostname
    except ValueError:
        return False
    valid = (
            parsed.scheme.lower() in {"http", "https"}
            and bool(hostname)
            and resource_type_from_url(candidate) == resource_type_value
    )
    if not valid or resource_type_value != "115":
        return valid
    share_code = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    receive_code = dict(parse_qsl(parsed.query)).get("password", "")
    return bool(
        re.fullmatch(r"[A-Za-z0-9]+", share_code)
        and re.fullmatch(r"[A-Za-z0-9]{4}", receive_code)
    )


def normalize_share_url(
        value: Any, resource_type_value: str, access_code: Any = ""
) -> str:
    candidate = decode_embedded_text(value).strip().rstrip("),.;]}")
    if resource_type_value == "115" and candidate:
        parsed = urlsplit(candidate)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        embedded_code = next((
            item for key, item in query
            if key.lower() in {"password", "pwd", "receive_code"}
        ), "")
        code = decode_embedded_text(access_code or embedded_code).strip()
        if re.fullmatch(r"[A-Za-z0-9]{4}", code):
            query = [
                (key, item) for key, item in query
                if key.lower() not in {"password", "pwd", "receive_code"}
            ]
            query.append(("password", code))
            candidate = urlunsplit(parsed._replace(query=urlencode(query)))
    return candidate if valid_share_url(candidate, resource_type_value) else ""


class _UnlockLimiter:
    """资源解锁节流控制。"""

    WINDOW_SECONDS = 60.0
    _STATE_LOCK = threading.RLock()
    _HISTORIES: Dict[str, deque] = {}
    _LOCKS: Dict[str, threading.RLock] = {}

    def __init__(self, session_key: str, per_window: int = 2):
        self._key = str(session_key or "default")
        self._per_window = max(1, min(int(per_window or 2), 5))

    def lock(self) -> threading.RLock:
        with self._STATE_LOCK:
            return self._LOCKS.setdefault(self._key, threading.RLock())

    @property
    def per_window(self) -> int:
        return self._per_window

    def _evict(self, history: deque, now: float) -> None:
        while history and (now - history[0]) >= self.WINDOW_SECONDS:
            history.popleft()

    def _next_wait_seconds(self) -> float:
        with self._STATE_LOCK:
            history = self._HISTORIES.setdefault(self._key, deque())
            now = time.monotonic()
            self._evict(history, now)
            if len(history) < self._per_window:
                return 0.0
            return max(0.0, (history[0] + self.WINDOW_SECONDS) - now)

    def record(self) -> None:
        with self._STATE_LOCK:
            history = self._HISTORIES.setdefault(self._key, deque())
            now = time.monotonic()
            self._evict(history, now)
            history.append(now)

    def wait_for_slot(self, cooldown_remaining: Optional[Callable[[], float]] = None) -> None:
        def ensure_runnable():
            if cooldown_remaining:
                remaining = cooldown_remaining()
                if remaining > 0:
                    raise HDHavenError(
                        f"HDHaven 处于风控冷却期，跳过解锁（剩余 {int(remaining + 0.999)} 秒）",
                        code="rate_limited",
                    )

        while True:
            ensure_runnable()
            wait_seconds = self._next_wait_seconds()
            if wait_seconds <= 0:
                return
            logger.debug(f"HDHaven 解锁按节奏等待 {wait_seconds:.1f} 秒")
            deadline = time.monotonic() + wait_seconds
            while deadline > time.monotonic():
                ensure_runnable()
                time.sleep(min(deadline - time.monotonic(), 0.25))


def preview_episodes_from_files(
        files: List[Dict[str, Any]], target_season: Optional[int] = None
) -> Dict[str, List[int]]:
    episodes: Dict[str, Set[int]] = {}
    fallback_season = max(1, int(target_season or 1))

    for item in files or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("path") or item.get("name") or "").strip()
        if not name:
            continue
        parsed = MediaFileParser.extract_episode_number(name, fallback_season)
        if parsed:
            episodes.setdefault(str(parsed[0]), set()).add(parsed[1])

    return {
        season: sorted(list(values))
        for season, values in episodes.items()
        if values
    }


def parse_episode_range(text: str, fallback_season: Optional[int] = None) -> Dict[str, List[int]]:
    """解析资源标题/remark 的季集范围，与搜索源共用同一套发行写法。"""
    return MediaFileParser.extract_episode_coverage(
        str(text or ""),
        fallback_season=max(1, int(fallback_season or 1)),
    )


class HDHavenResourceService:
    _PREVIEW_CACHE_TTL = 15 * 60
    _PREVIEW_LOCKS: Dict[str, threading.RLock] = {}
    _LOCK = threading.RLock()

    def __init__(self, client: HDHavenClient, unlocks_per_minute: int = 2):
        self.client = client
        session_key = f"{client.username or 'default'}"
        self._session_key = session_key
        self._unlock_limiter = _UnlockLimiter(session_key, unlocks_per_minute)
        self._preview_cache = create_platform_ttl_cache(
            "hdhaven:preview", session_key, maxsize=256, ttl=self._PREVIEW_CACHE_TTL
        )

    def _get_preview_lock(self, slug: str) -> threading.RLock:
        with self._LOCK:
            return self._PREVIEW_LOCKS.setdefault(slug, threading.RLock())

    def clear_cache(self) -> Dict[str, int]:
        with self._LOCK:
            count = len(list(self._preview_cache.items()))
            self._preview_cache.clear()
            return {"previews": count}

    def get_pan_resources(
            self,
            tmdb_id: int,
            media_type: str = "movie",
            enabled_pan_types: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        normalized_type = "movie" if media_type.lower() == "movie" else "tv"
        raw_list = []

        # 调用影视关联资源接口：GET /api/resources/{type}/{tmdb_id}
        try:
            resp = self.client.request("GET", f"/api/resources/{normalized_type}/{int(tmdb_id)}")
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    d = data.get("data")
                    if isinstance(d, list):
                        raw_list = d
                    elif isinstance(d, dict):
                        raw_list = d.get("items") or d.get("resources") or []
            elif resp.status_code >= 400:
                raise HDHavenError(
                    f"HDHaven 影视关联资源接口返回 HTTP {resp.status_code}",
                    code="resource_query_failed",
                    status_code=resp.status_code,
                )
        except HDHavenError:
            # 交给搜索编排层统一记录失败原因，避免 403/登录失效被误当成“0 条结果”。
            raise
        except Exception as err:
            logger.debug(f"HDHaven 请求影视关联资源失败：{err}")

        results = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            pan_type = str(item.get("pan_type") or item.get("storage") or item.get("type") or "").lower()
            std_pan = normalize_resource_type(pan_type)
            if enabled_pan_types and std_pan not in enabled_pan_types:
                continue

            slug = str(item.get("slug") or item.get("id") or "")
            orig_title = str(item.get("title") or item.get("name") or "").strip()
            remark = str(item.get("remark") or "").strip()
            # 若发布者 remark 含有更具体的分辨率/压制组/季集信息，优先作为显示标题
            title = remark if (remark and len(remark) >= 6) else orig_title

            point_cost = int(item.get("unlock_points") or item.get("points") or item.get("cost") or 0)
            is_free = bool(item.get("is_free")) or point_cost == 0
            raw_url = str(item.get("share_url") or item.get("link") or item.get("url") or "")
            access_code = str(
                item.get("access_code") or item.get("code") or item.get("pwd") or item.get("share_code") or "")
            download_url = normalize_share_url(raw_url, std_pan, access_code) if raw_url else ""
            is_unlocked = bool(item.get("is_unlocked")) or bool(download_url)
            size_str = str(item.get("share_size") or item.get("size") or "")
            size_bytes = int(item.get("size_bytes") or StringUtils.num_filesize(size_str) or 0)
            episode_range = str(item.get("episode_range") or "").strip()

            desc_parts = [f"网盘: {std_pan}", f"积分: {point_cost}{' (免费)' if is_free else ''}"]
            if episode_range:
                desc_parts.append(f"范围: {episode_range}")
            description = " | ".join(desc_parts)

            results.append({
                "url": download_url,
                "title": title,
                "description": description,
                "size": size_bytes,
                "size_str": size_str,
                "size_human": size_str or StringUtils.num_filesize(size_bytes),
                "resource_type": std_pan,
                "source": "hdhaven",
                "provider": "hdhaven",
                "uploader": str(item.get("uploader") or item.get("author") or ""),
                "resolution": str(item.get("resolution") or ""),
                "quality": str(item.get("quality") or ""),
                "is_official": bool(item.get("is_official")),
                "update_time": str(
                    item.get("updated_at")
                    or item.get("posted_at")
                    or item.get("created_at")
                    or ""
                ),
                "slug": slug,
                "resource_ref": slug,
                "id": slug,
                "need_unlock": not is_unlocked and point_cost > 0,
                "need_access": not is_unlocked and point_cost <= 0,
                "unlock_points": point_cost,
                "is_unlocked": is_unlocked,
                "is_free": is_free,
                "can_preview": True,
                "supports_file_preview": True,
                "download_url": download_url,
                "episode_range": episode_range,
                "provider_data": {
                    "slug": slug,
                    "id": slug,
                    "tmdb_id": tmdb_id,
                    "media_type": normalized_type,
                    "episode_range": episode_range,
                },
                "raw_item": item,
            })

        return results

    def get_magnets(
            self,
            tmdb_id: int,
            media_type: str = "movie",
            limit: int = 50,
    ) -> List[Dict[str, Any]]:
        normalized_type = "movie" if media_type.lower() == "movie" else "tv"
        path = f"/api/magnets/{normalized_type}/{int(tmdb_id)}"

        try:
            resp = self.client.request("GET", path)
            if resp.status_code != 200:
                logger.debug(f"HDHaven 磁力查询返回 HTTP {resp.status_code}")
                return []
            data = resp.json()
        except HDHavenError as err:
            if getattr(err, "code", "") in {"rate_limited", "cancelled"}:
                raise
            logger.warning(f"HDHaven 磁力查询失败：{err}")
            return []
        except Exception as err:
            logger.warning(f"HDHaven 磁力查询失败：{err}")
            return []

        data_obj = data.get("data") if isinstance(data, dict) else {}
        if isinstance(data_obj, dict):
            raw_list = data_obj.get("results") or data_obj.get("list") or []
        elif isinstance(data_obj, list):
            raw_list = data_obj
        else:
            raw_list = []

        results = []

        for item in raw_list[:limit]:
            if not isinstance(item, dict):
                continue
            magnet_url = str(item.get("magnet") or item.get("url") or "").strip()
            if not magnet_url.startswith("magnet:"):
                continue

            title = str(item.get("title") or "")
            size_str = str(item.get("size") or "").strip()
            size_bytes = int(item.get("size_bytes") or StringUtils.num_filesize(size_str) or 0)
            seeders = int(item.get("seeders") or 0)
            leechers = int(item.get("leechers") or 0)
            info_hash = str(item.get("info_hash") or "")

            results.append({
                "url": magnet_url,
                "title": title,
                "size": size_bytes,
                "size_str": size_str,
                "size_human": size_str,
                "seeders": seeders,
                "leechers": leechers,
                "info_hash": info_hash,
                "resource_type": "magnet",
                "source": "hdhaven",
                "provider": "hdhaven",
                "slug": info_hash or title,
                "resource_ref": info_hash or title,
                "id": info_hash or title,
                "need_unlock": False,
                "need_access": False,
                "unlock_points": 0,
                "is_unlocked": True,
                "is_free": True,
                "can_preview": False,
                "supports_file_preview": False,
                "download_url": magnet_url,
                "provider_data": {
                    "info_hash": info_hash,
                    "tmdb_id": tmdb_id,
                    "media_type": normalized_type,
                },
                "raw_item": item,
            })

        return results

    def get_file_preview(self, slug: str) -> Dict[str, Any]:
        cached = self._preview_cache.get(slug)
        if isinstance(cached, dict):
            return copy.deepcopy(cached)

        with self._get_preview_lock(slug):
            cached = self._preview_cache.get(slug)
            if isinstance(cached, dict):
                return copy.deepcopy(cached)

            clean = str(slug or "").strip().replace("-", "")
            uuid_str = (
                f"{clean[:8]}-{clean[8:12]}-{clean[12:16]}-{clean[16:20]}-{clean[20:]}"
                if len(clean) == 32 else str(slug or "").strip()
            )
            resp = self.client.request("GET", f"/api/resources/{uuid_str}")
            if resp.status_code != 200:
                raise HDHavenError(
                    f"资源预览接口异常（HTTP {resp.status_code}）：{resp.text[:200]}",
                    status_code=resp.status_code,
                )
            try:
                payload = resp.json() or {}
            except Exception as err:
                raise HDHavenError(f"资源预览接口响应非有效 JSON：{err}", status_code=resp.status_code) from err

            if not payload.get("success"):
                msg = payload.get("message") or "获取预览文件失败"
                raise HDHavenError(f"资源预览失败：{msg}", status_code=resp.status_code)

            data = payload.get("data") or {}
            raw_files = data.get("files") or []
            files = [
                {
                    "name": str(item.get("name") or item.get("path") or "").strip(),
                    "path": str(item.get("name") or item.get("path") or "").strip(),
                    "bytes": int(item.get("bytes") or item.get("size") or 0),
                    "size": int(item.get("bytes") or item.get("size") or 0),
                }
                for item in raw_files
                if isinstance(item, dict)
            ]
            result = {
                "title": data.get("title") or "",
                "slug": data.get("slug") or uuid_str,
                "is_unlocked": bool(data.get("is_unlocked")),
                "files": files,
                "file_count": int(data.get("file_count") or len(files)),
                "message": data.get("message") or "",
            }
            self._preview_cache.set(slug, copy.deepcopy(result))
            return result

    def check_resource_validity(
            self,
            slug: str,
            target_season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            media_type: str = "tv",
            episode_range: str = "",
            raw_item: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        preview = None
        preview_error = None
        try:
            preview = self.get_file_preview(slug)
        except Exception as err:
            preview_error = err
            logger.debug(f"HDHaven 资源文件预览不可用：slug={slug}，err={err}")

        # 若成功获取到文件列表，使用精确文件列表进行分析
        files = (preview or {}).get("files") or []
        if files:
            video_exts = (".mkv", ".mp4", ".ts", ".iso", ".avi", ".mov", ".flv", ".wmv", ".m2ts")
            video_files = [
                f for f in files
                if MediaFileParser.is_video(str(f.get("name") or f.get("path") or ""))
                   or str(f.get("name") or f.get("path") or "").lower().endswith(video_exts)
            ]
            if not video_files:
                logger.debug(f"HDHaven 资源无视频文件，跳过解锁：slug={slug}")
                return {"is_valid": False, "skip_reason": "no_video_files", "preview_episodes": {}}

            if media_type.lower() == "movie":
                return {"is_valid": True, "skip_reason": "", "preview_episodes": {}}

            preview_episodes = preview_episodes_from_files(files, target_season)
            targets = set(int(ep) for ep in (target_episodes or []) if int(ep) > 0)
            season_key = str(max(1, int(target_season or 1)))
            available = set(preview_episodes.get(season_key, []))

            if targets and (not available or not (targets & available)):
                logger.debug(
                    f"HDHaven 资源未覆盖缺集（目标={targets}，包含={available}），跳过：slug={slug}"
                )
                return {
                    "is_valid": False,
                    "skip_reason": "target_not_covered",
                    "preview_episodes": preview_episodes,
                }

            return {
                "is_valid": True,
                "skip_reason": "",
                "preview_episodes": preview_episodes,
            }

        # 若未获取到文件列表（如收费资源在未解锁前返回 403，要求先解锁后查看文件）
        # 降级使用卡片元数据中的 episode_range / remark 进行覆盖校验
        range_str = episode_range or str((raw_item or {}).get("episode_range") or "").strip()
        remark_str = str((raw_item or {}).get("remark") or "").strip()
        title_str = str((raw_item or {}).get("title") or "").strip()

        if media_type.lower() == "movie":
            return {"is_valid": True, "skip_reason": "", "preview_episodes": {}}

        fallback_episodes = parse_episode_range(range_str, fallback_season=target_season)
        if not fallback_episodes:
            fallback_episodes = parse_episode_range(remark_str, fallback_season=target_season)
        if not fallback_episodes:
            fallback_episodes = parse_episode_range(title_str, fallback_season=target_season)

        targets = set(int(ep) for ep in (target_episodes or []) if int(ep) > 0)
        season_key = str(max(1, int(target_season or 1)))
        available = set(fallback_episodes.get(season_key, []))

        if targets and available and not (targets & available):
            logger.debug(
                f"HDHaven 资源元数据未覆盖缺集（目标={targets}，范围={available}），跳过：slug={slug}"
            )
            return {
                "is_valid": False,
                "skip_reason": "target_not_covered",
                "preview_episodes": fallback_episodes,
            }

        if preview_error and "403" not in str(preview_error) and not fallback_episodes:
            return {
                "is_valid": False,
                "skip_reason": f"preview_failed: {preview_error}",
                "preview_episodes": {},
            }

        return {
            "is_valid": True,
            "skip_reason": "",
            "preview_episodes": fallback_episodes,
        }

    def unlock_resource(
            self,
            slug: str,
            resource_type: str = "",
            target_season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            media_type: str = "tv",
            point_cost: int = 0,
            budget: Optional[Any] = None,
            subscribe_id: Any = None,
            episode_range: str = "",
            raw_item: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._unlock_limiter.lock():
            validity = self.check_resource_validity(
                slug=slug,
                target_season=target_season,
                target_episodes=target_episodes,
                media_type=media_type,
                episode_range=episode_range,
                raw_item=raw_item,
            )
            if not validity["is_valid"]:
                return {
                    "url": "",
                    "is_unlocked": False,
                    "skip_reason": validity["skip_reason"],
                    "preview_episodes": validity["preview_episodes"],
                }

            if budget:
                cached_link = budget.cached_url(slug)
                if cached_link:
                    logger.debug(f"HDHaven 命中已解锁直链缓存：slug={slug}")
                    return {
                        "url": cached_link,
                        "share_code": "",
                        "resource_type": resource_type,
                        "is_unlocked": True,
                        "preview_episodes": validity["preview_episodes"],
                    }

            if point_cost > 0 and budget:
                if not budget.has_budget(point_cost):
                    logger.warning(
                        f"HDHaven 积分解锁被预算规则拦截：{budget.format_snapshot(point_cost)}，slug={slug}"
                    )
                    return {
                        "url": "",
                        "is_unlocked": False,
                        "skip_reason": "budget_exceeded",
                        "preview_episodes": validity["preview_episodes"],
                    }

            self._unlock_limiter.wait_for_slot(cooldown_remaining=lambda: self.client.cooldown_remaining)

            try:
                clean = str(slug or "").strip().replace("-", "")
                uuid_str = (
                    f"{clean[:8]}-{clean[8:12]}-{clean[12:16]}-{clean[16:20]}-{clean[20:]}"
                    if len(clean) == 32 else str(slug or "").strip()
                )
                resp = self.client.request("POST", "/api/resources/unlock", json_data={"slug": uuid_str})
                if resp.status_code not in (200, 201):
                    raise HDHavenError(
                        f"资源解锁接口异常（HTTP {resp.status_code}）：{resp.text[:200]}",
                        status_code=resp.status_code,
                    )
                payload = resp.json() or {}
                if not payload.get("success"):
                    msg = payload.get("message") or "解锁资源失败"
                    raise HDHavenError(f"资源解锁失败：{msg}", status_code=resp.status_code)
                data = payload.get("data") or {}
                raw_url = str(data.get("url") or data.get("share_url") or "").strip()
                code = str(data.get("access_code") or data.get("code") or data.get("pwd") or "").strip()
                share_url = normalize_share_url(raw_url, resource_type, code) if raw_url else ""
            except Exception as err:
                raise HDHavenError(f"HDHaven 解锁资源异常：{err}") from err

            self._unlock_limiter.record()
            if budget and share_url:
                budget.record_result(slug, share_url, point_cost if share_url else 0)

            return {
                "url": share_url,
                "share_code": code,
                "resource_type": resource_type,
                "is_unlocked": bool(share_url),
                "preview_episodes": validity["preview_episodes"],
            }
