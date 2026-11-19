"""HDHaven 搜索、预览、风控与积分解锁服务编排器。"""

import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.log import logger
from app.schemas.types import MediaType

from .client import HDHavenClient, HDHavenError
from .resource import (
    HDHavenResourceService,
    normalize_share_url,
    parse_episode_range,
    preview_episodes_from_files,
    valid_share_url,
)
from ..budget import PointBudgetLedger
from ..types import (
    SUPPORTED_CLOUD_TYPES,
    normalize_resource_type,
    preview_item,
    resource_type_name,
)
from ...core import OwnerDelegator, SearchQuery, format_search_label
from ...core.media import tmdb_id_of
from ...utils.cache import create_platform_ttl_cache


class HDHavenSearchService(OwnerDelegator):
    """提供 HDHaven 搜索与转存服务。"""

    _HISTORY_KEY = "hdhaven_sub_points_history"

    def __init__(self, owner: Any):
        super().__init__(owner)
        unlocked_cache = create_platform_ttl_cache(
            "search:hdhaven_unlocked_urls",
            str(getattr(owner, "_hdhaven_username", "") or "default").casefold(),
            maxsize=256,
            ttl=30 * 60,
        )
        object.__setattr__(self, "_budget", PointBudgetLedger(
            self._HISTORY_KEY,
            int(getattr(owner, "_hdhaven_max_unlock_points", 50) or 50),
            int(getattr(owner, "_hdhaven_max_points_per_sub", 20) or 20),
            unlocked_cache=unlocked_cache,
        ))
        object.__setattr__(self, "_lock", threading.RLock())
        object.__setattr__(self, "_unlock_operation_lock", threading.Lock())
        object.__setattr__(self, "_client", None)
        object.__setattr__(self, "_resource_service", None)

    @property
    def available(self) -> bool:
        return bool(self._hdhaven_username and self._hdhaven_password)

    @property
    def budget(self) -> PointBudgetLedger:
        return self._budget

    @staticmethod
    def _valid_share_value(value: Any, resource_type: str) -> bool:
        values = value if isinstance(value, (list, tuple, set)) else [value]
        expected = str(resource_type or "").strip().casefold()
        return bool(values) and all(
            valid_share_url(str(item).strip(), expected)
            for item in values
        )

    @property
    def resource_types(self):
        types = set(self._resource_type_order_config)
        return frozenset(types)

    @property
    def cache_context(self) -> Dict[str, Any]:
        return {
            "version": 2,
            "base_url": str(getattr(self, "_hdhaven_base_url", "") or ""),
            "magnet_enabled": bool(getattr(self, "_hdhaven_magnet_enabled", False)),
            "resource_types": sorted(self.resource_types),
        }

    def get_client(self) -> HDHavenClient:
        base_url = str(getattr(self, "_hdhaven_base_url", "https://hdhaven.com") or "https://hdhaven.com").strip()
        with self._lock:
            if (
                    self._client is None
                    or self._client.base_url != base_url
                    or self._client.username != self._hdhaven_username
                    or self._client.password != self._hdhaven_password
                    or self._client.proxy != self._search_proxy
            ):
                if self._client:
                    self._client.close()
                client = HDHavenClient(
                    base_url=base_url,
                    username=self._hdhaven_username,
                    password=self._hdhaven_password,
                    proxy=self._search_proxy,
                    request_interval=float(
                        getattr(self, "_hdhaven_request_interval", 2.0) or 2.0
                    ),
                    **self.storage_hooks(),
                )
                object.__setattr__(self, "_client", client)
            return self._client

    def _get_resource_service(self) -> HDHavenResourceService:
        client = self.get_client()
        with self._lock:
            if self._resource_service is None or self._resource_service.client is not client:
                unlocks_limit = int(getattr(self, "_hdhaven_unlocks_per_minute", 5) or 5)
                object.__setattr__(self, "_resource_service", HDHavenResourceService(client, unlocks_limit))
            return self._resource_service

    def search(self, query: SearchQuery) -> Optional[List[Dict[str, Any]]]:
        """直达 TMDB 资源与磁力搜索。"""
        mediainfo = query.mediainfo
        media_type = query.media_type
        season = query.season
        subscribe = query.subscribe
        resource_list_mode = bool(getattr(query, "resource_list_mode", False))
        result_limit = getattr(query, "result_limit", None)

        tmdb_id = mediainfo.tmdb_id or tmdb_id_of(subscribe)
        search_prefix = f"[{format_search_label(mediainfo, media_type, season)}][HDHAVEN]"

        if not tmdb_id:
            logger.warning(f"{search_prefix} 缺少 TMDB ID，跳过直达查询")
            return []

        if not self.available:
            logger.warning(f"{search_prefix} 未配置 HDHaven 凭据（Token 或用户名密码）")
            return []

        client = self.get_client()
        if client.cooldown_remaining > 0:
            logger.warning(
                f"{search_prefix} HDHaven 处于风控保护中（剩余 {client.cooldown_remaining:.0f}s），快速跳过"
            )
            return []

        type_str = "movie" if media_type == MediaType.MOVIE else "tv"
        service = self._get_resource_service()
        results: List[Dict[str, Any]] = []

        # 资源列表模式：不按用户网盘偏好过滤，返回 HDHaven 上全部网盘资源
        # 订阅搜索模式：仅返回用户在网盘偏好中选择的类型
        pan_targets = None if resource_list_mode else [
            t for t in self.resource_types if t in SUPPORTED_CLOUD_TYPES
        ]
        query_magnets = bool(self._hdhaven_magnet_enabled and (
                resource_list_mode or "magnet" in self.resource_types
                or not self.resource_types
        ))
        if not resource_list_mode and not pan_targets and not query_magnets:
            logger.warning(
                f"{search_prefix} 网盘偏好中没有 HDHaven 可查询的网盘类型，"
                "且磁力搜索未开启，已跳过查询"
            )
            return []
        try:
            if resource_list_mode or pan_targets:
                results.extend(service.get_pan_resources(
                    tmdb_id=int(tmdb_id),
                    media_type=type_str,
                    enabled_pan_types=pan_targets,
                ))

            if query_magnets:
                results.extend(service.get_magnets(
                    tmdb_id=int(tmdb_id),
                    media_type=type_str,
                ))
        except HDHavenError as error:
            message = f"{search_prefix} HDHaven 查询失败：{error}"
            if getattr(error, "code", "") in {"rate_limited", "cancelled"}:
                logger.debug(message)
            else:
                logger.warning(message)
            return None

        target_episodes = list(getattr(query, "target_episodes", []) or [])
        target_episode_air_dates = dict(getattr(query, "target_episode_air_dates", {}) or {})
        subscribe_id = getattr(subscribe, "id", None) if subscribe else None

        filtered_results = []
        for item in results:
            if not resource_list_mode and item.get("need_unlock") and not self._hdhaven_auto_unlock:
                continue
            if not resource_list_mode and not item.get("url") and not item.get("need_unlock"):
                continue
            if (
                    not resource_list_mode
                    and type_str == "tv"
                    and target_episodes
                    and item.get("episode_range")
            ):
                available = parse_episode_range(
                    str(item.get("episode_range") or ""),
                    fallback_season=season,
                ).get(str(max(1, int(season or 1))), [])
                if available and not (set(target_episodes) & set(available)):
                    continue

            item["identity_verified"] = True
            item["search_label"] = search_prefix
            item["target_season"] = int(season) if season is not None else None
            item["target_episodes"] = target_episodes
            item["target_episode_air_dates"] = target_episode_air_dates
            item["subscribe_id"] = subscribe_id
            item["media_type"] = type_str
            item["preview_episodes_authoritative"] = bool(item.get("preview_episodes"))
            filtered_results.append(item)

        order_map = {
            str(value).strip().casefold(): index
            for index, value in enumerate(self._resource_type_order_config)
        }

        def update_timestamp(value: Any) -> float:
            text = str(value or "").strip().replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(text).timestamp()
            except (TypeError, ValueError, OverflowError):
                return 0.0

        filtered_results.sort(key=lambda item: (
            order_map.get(str(item.get("resource_type") or "").casefold(), 999),
            0 if item.get("is_unlocked") or item.get("is_free") else 1,
            -update_timestamp(item.get("update_time")),
        ))

        limit = (
            max(1, int(result_limit or self._hdhaven_candidate_limit))
            if resource_list_mode
            else max(1, int(self._hdhaven_candidate_limit))
        )
        final_results = filtered_results[:limit]
        logger.debug(f"{search_prefix} 查询完成：共命中 {len(final_results)} 条资源（网盘+磁力）")
        return final_results

    def preview(self, candidate: Any) -> Dict[str, Any]:
        resource = dict(candidate or {})
        slug = resource.get("resource_ref") or resource.get("slug") or resource.get("id")
        if not slug:
            return {"files": [], "file_count": 0}
        service = self._get_resource_service()
        try:
            res = service.get_file_preview(str(slug))
            files = res.get("files") or []
            target_season = resource.get("target_season")
            if files and "preview_episodes" not in res:
                res["preview_episodes"] = preview_episodes_from_files(files, target_season)
            return res
        except HDHavenError as err:
            # 403 冷却期/风控时静默降级，由前端根据 episode_range 展示剧集覆盖信息
            if getattr(err, "code", "") in ("rate_limited", "cooldown"):
                logger.debug(f"HDHaven 处于冷却期，预览降级为元数据展示：slug={slug}，{err}")
                return {
                    "files": [],
                    "file_count": 0,
                    "cooldown_message": str(err),
                }
            raise

    def preview_files(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """渠道自有预览：只读列出 HDHaven 文件列表，未解锁时降级为集数范围说明。"""
        payload = dict(payload or {})
        resource_ref = str(payload.get("resource_ref") or "").strip()
        resource_type = normalize_resource_type(payload.get("resource_type"))
        candidate = {
            "resource_ref": resource_ref,
            "slug": resource_ref,
            "id": resource_ref,
            "resource_type": resource_type,
            "unlock_points": int(payload.get("unlock_points") or 0),
            "is_unlocked": bool(payload.get("is_unlocked")),
            "episode_range": str(payload.get("episode_range") or ""),
            "raw_item": dict(payload.get("raw_item") or {}),
            "target_season": payload.get("target_season"),
            "target_episodes": payload.get("target_episodes"),
            "supports_file_preview": payload.get("supports_file_preview"),
            "provider_data": dict(payload.get("provider_data") or {}),
            "search_label": "测试只读预览",
        }
        preview = self.preview(candidate)
        files = [
            {**preview_item(item), "can_enter": False}
            for item in (preview.get("files") or [])
        ][:500]
        episode_range_str = str(payload.get("episode_range") or "").strip()
        if files:
            message = f"只读预览到 {len(files)} 个文件，未执行解锁"
        elif episode_range_str:
            message = f"资源需先解锁才可预览文件，集数范围：{episode_range_str}"
        else:
            message = "资源需先解锁才可预览文件"
        return {
            "success": True,
            "message": message,
            "data": {
                "items": files,
                "count": len(files),
                "provider_name": "HDHaven",
                "resource_type": resource_type,
                "resource_type_name": resource_type_name(resource_type, "HDHaven"),
                "share_url": "",
                "parent_id": "",
                "preview_episodes": preview.get("preview_episodes") or {},
                "covers_target": preview.get("covers_target"),
                "resource_validate_status": preview.get("resource_validate_status") or "",
                "resource_validate_message": preview.get("resource_validate_message")
                or (f"集数范围：{episode_range_str}" if episode_range_str else ""),
            },
        }

    def unlock(self, candidate: Any, search_label: str = "") -> str:
        resource = dict(candidate or {})
        slug = resource.get("resource_ref") or resource.get("slug") or resource.get("id")
        resource_type = resource.get("resource_type") or resource.get("pan_type", "")
        if not slug:
            return ""

        if resource_type == "magnet" or resource.get("source") == "magnet":
            return str(resource.get("url") or resource.get("download_url") or resource.get("link", "")).strip()

        target_season = resource.get("target_season")
        target_episodes = resource.get("target_episodes") or []
        media_type = resource.get("media_type") or "tv"
        point_cost = int(resource.get("unlock_points") or resource.get("point_cost") or 0)
        subscribe_id = resource.get("subscribe_id")
        episode_range = resource.get("episode_range") or ""
        raw_item = resource.get("raw_item") or {}

        cached_url = self._budget.cached_url(slug)
        if cached_url and self._valid_share_value(cached_url, resource_type):
            return cached_url
        if cached_url:
            self._budget.discard_cached_url(slug)

        status = self._budget.status(point_cost)
        if status is None or not status.allowed:
            logger.warning(
                f"[{search_label or 'HDHAVEN'}] 解锁积分预算不足："
                f"{self._budget.format_snapshot(point_cost)}"
            )
            return ""

        if self._stop_requested():
            logger.info(f"[{search_label or 'HDHAVEN'}] 已停止任务，跳过积分解锁")
            return ""

        service = self._get_resource_service()
        if not self._unlock_operation_lock.acquire(timeout=0.25):
            logger.debug(f"[{search_label or 'HDHAVEN'}] 已有解锁请求进行中，跳过重复请求：{slug}")
            return ""
        try:
            res = service.unlock_resource(
                slug=str(slug),
                resource_type=resource_type,
                target_season=target_season,
                target_episodes=target_episodes,
                media_type=media_type,
                point_cost=point_cost,
                budget=self._budget,
                subscribe_id=subscribe_id,
                episode_range=episode_range,
                raw_item=raw_item,
            )
        except HDHavenError as error:
            logger.warning(f"[{search_label or 'HDHAVEN'}] 解锁失败：{error}")
            return ""
        except Exception as error:
            logger.exception(f"[{search_label or 'HDHAVEN'}] 解锁异常：{error}")
            return ""
        finally:
            self._unlock_operation_lock.release()

        raw_url = str(res.get("download_url") or res.get("url") or "").strip()
        code = str(res.get("share_code") or res.get("access_code") or "").strip()
        url = normalize_share_url(raw_url, resource_type, code) if raw_url else ""

        # 记录规范化后的最终链接，后续重复命中时直接使用且不重复扣点
        if url:
            self._budget.record_result(slug, url, 0)

        if res.get("is_unlocked") and point_cost > 0:
            try:
                account_info = self.get_account_info()
                new_points = account_info.get("points")
                if isinstance(new_points, dict):
                    new_points = new_points.get("available")
                if new_points is not None:
                    owner_obj = getattr(self, "_owner", None)
                    if owner_obj and hasattr(owner_obj, "update_search_account_points"):
                        owner_obj.update_search_account_points("hdhaven", int(new_points))
            except Exception as err:
                logger.debug(f"HDHaven 解锁后更新账号积分失败：{err}")

        if url and resource_type and not self._valid_share_value(url, resource_type):
            logger.warning(
                f"[{search_label or 'HDHAVEN'}] 解锁返回链接类型不匹配，已拒绝："
                f"type={resource_type}，url={url}"
            )
            self._budget.discard_cached_url(slug)
            return ""
        return url

    def checkin(self, mode: str = "normal") -> Dict[str, Any]:
        client = self.get_client()
        return client.checkin(mode)

    def get_account_info(self) -> Dict[str, Any]:
        return self.get_client().get_account_info()

    def clear_cache(self) -> int:
        total = 0
        resource_service = self._resource_service
        if resource_service:
            result = resource_service.clear_cache()
            total += sum(int(value or 0) for value in result.values()) if isinstance(result, dict) else int(result or 0)
        return total

    def close(self) -> None:
        with self._lock:
            if self._client:
                self._client.close()
                self._client = None
                self._resource_service = None
