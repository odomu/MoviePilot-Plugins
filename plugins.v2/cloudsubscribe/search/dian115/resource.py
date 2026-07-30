"""Dian115 资源详情缓存与分享解锁协议。"""

import copy
import threading
import time
from typing import Any, Dict, Optional

from app.log import logger

from .client import Dian115Client, Dian115Error
from .protocol import resource_path, share_path


class Dian115ResourceService:
    """负责资源接口及缓存，复用唯一的 Dian115 认证客户端。"""

    _DETAIL_CACHE_TTL = 10 * 60
    _DETAIL_CACHE_LIMIT = 512

    def __init__(self, client: Dian115Client):
        self._client = client
        self._detail_cache: Dict[
            tuple[int, str, int], tuple[float, Dict[str, Any]]
        ] = {}
        self._detail_locks = tuple(threading.Lock() for _ in range(32))
        self._lock = threading.RLock()

    def matches_client(self, client: Dian115Client) -> bool:
        return self._client is client

    def clear_cache(self) -> int:
        with self._lock:
            count = len(self._detail_cache)
            self._detail_cache.clear()
            return count

    def _prune_detail_cache_locked(self, now: Optional[float] = None) -> None:
        current = time.monotonic() if now is None else now
        expired = [
            key for key, (expires_at, _) in self._detail_cache.items()
            if expires_at <= current
        ]
        for key in expired:
            self._detail_cache.pop(key, None)
        overflow = len(self._detail_cache) - self._DETAIL_CACHE_LIMIT + 1
        if overflow > 0:
            oldest = sorted(
                self._detail_cache,
                key=lambda key: self._detail_cache[key][0],
            )[:overflow]
            for key in oldest:
                self._detail_cache.pop(key, None)

    def resource_detail(
            self,
            tmdb_id: int,
            media_type: str,
            season: int = 0,
            force_refresh: bool = False,
    ) -> Dict[str, Any]:
        """按 TMDB 媒体标识读取资源及分享列表。"""
        normalized_id = int(tmdb_id)
        normalized_type = str(media_type or "").strip().lower()
        normalized_season = int(season or 0)
        cache_key = (normalized_id, normalized_type, normalized_season)
        now = time.monotonic()
        with self._lock:
            cached = self._detail_cache.get(cache_key)
            if not force_refresh and cached and cached[0] > now:
                logger.debug(
                    f"Dian115 详情命中缓存：tmdb={normalized_id}，"
                    f"type={normalized_type}，season={normalized_season}"
                )
                return copy.deepcopy(cached[1])
        detail_lock = self._detail_locks[hash(cache_key) % len(self._detail_locks)]
        with detail_lock:
            now = time.monotonic()
            with self._lock:
                cached = self._detail_cache.get(cache_key)
                if not force_refresh and cached and cached[0] > now:
                    logger.debug(
                        f"Dian115 详情等待后命中缓存：tmdb={normalized_id}，"
                        f"type={normalized_type}，season={normalized_season}"
                    )
                    return copy.deepcopy(cached[1])
            path = resource_path(
                normalized_type, normalized_id, normalized_season
            )
            key = path.rsplit("/", 1)[-1]
            payload = self._client.request_json(
                "GET",
                "/api/portal/resource-detail",
                path,
                params={"key": key},
            )
            payload["resource_key"] = key
            payload["resource_path"] = path
            with self._lock:
                self._prune_detail_cache_locked()
                self._detail_cache[cache_key] = (
                    time.monotonic() + self._DETAIL_CACHE_TTL,
                    copy.deepcopy(payload),
                )
            return payload

    def unlock_share(
            self,
            share_id: int,
            resource_id: int = 0,
            max_unlock_points: Optional[int] = None,
            tmdb_id: int = 0,
            media_type: str = "",
            season: int = 0,
    ) -> Dict[str, Any]:
        """解锁分享；提交前刷新价格，避免价格变化突破授权上限。"""
        normalized_share_id = int(share_id or 0)
        if normalized_share_id <= 0:
            raise Dian115Error("Dian115 分享 ID 无效")
        current_path = share_path(normalized_share_id)
        started = time.monotonic()
        logger.debug(
            f"Dian115 准备获取分享：share_id={normalized_share_id}，"
            f"预算={max_unlock_points if max_unlock_points is not None else '未限制'}"
        )
        if (
                max_unlock_points is not None
                and int(tmdb_id or 0) > 0
                and str(media_type or "").strip().lower() in {"movie", "tv"}
        ):
            detail = self.resource_detail(
                int(tmdb_id),
                str(media_type).strip().lower(),
                int(season or 0),
                force_refresh=True,
            )
            current_share = next(
                (
                    item for item in (detail.get("shares") or [])
                    if int((item or {}).get("id") or 0) == normalized_share_id
                ),
                None,
            )
            if not current_share:
                raise Dian115Error("Dian115 分享已下架", code="share_not_found")
            current_cost = max(0, int(current_share.get("unlock_cost") or 0))
            already_accessible = bool(
                current_share.get("is_unlocked")
                or current_share.get("url")
                or current_share.get("url_115")
                or (
                        current_share.get("share_code")
                        and current_share.get("receive_code")
                )
            )
            logger.debug(
                f"Dian115 解锁前价格复核：share_id={normalized_share_id}，"
                f"cost={current_cost}，already_accessible={already_accessible}"
            )
            if current_cost > int(max_unlock_points) and not already_accessible:
                raise Dian115Error(
                    "Dian115 当前解锁价格超过预算："
                    f"需要 {current_cost}，预算 {int(max_unlock_points)}",
                    code="unlock_budget_exceeded",
                )
        body = {"share_id": normalized_share_id}
        if int(resource_id or 0) > 0:
            body["resource_id"] = int(resource_id)
        payload = self._client.request_json(
            "POST",
            "/api/portal/unlock",
            current_path,
            headers={"content-type": "application/json"},
            json=body,
        )
        unlock = payload.get("unlock") or {}
        try:
            actual_points = max(0, int(unlock.get("cost_points") or 0))
        except (TypeError, ValueError):
            actual_points = 0
        if max_unlock_points is not None and actual_points > int(max_unlock_points):
            logger.error(
                "Dian115 实际扣费超过调用方预算："
                f"share_id={normalized_share_id}，实际={actual_points}，"
                f"预算={int(max_unlock_points)}"
            )
        payload["actual_points"] = actual_points
        logger.debug(
            f"Dian115 分享获取完成：share_id={normalized_share_id}，"
            f"actual_points={actual_points}，"
            f"耗时={time.monotonic() - started:.2f}s"
        )
        with self._lock:
            normalized_type = str(media_type or "").strip().lower()
            if int(tmdb_id or 0) > 0 and normalized_type in {"movie", "tv"}:
                self._detail_cache.pop(
                    (int(tmdb_id), normalized_type, int(season or 0)),
                    None,
                )
            else:
                self._detail_cache.clear()
        return payload
