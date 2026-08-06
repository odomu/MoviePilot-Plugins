"""平台入口共享的聚合与业务调用。"""

import copy
import datetime
import re
import time
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Tuple
from uuid import uuid4

import pytz
from app.core.cache import TTLCache
from app.core.config import settings
from app.core.metainfo import MetaInfo
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.types import MediaType

from ...core import CloudDriveCapability, OwnerDelegator


class PlatformIntegrationService(OwnerDelegator):
    """统一服务于页面、仪表盘、命令、工作流与智能体。"""

    _OVERVIEW_TTL_SECONDS = 3.0
    _RESOURCE_LINK_PATTERN = re.compile(
        r"ed2k://\|file\|.*?\|/|magnet:\?\S+|https?://\S+",
        re.IGNORECASE,
    )

    def __init__(self, owner):
        super().__init__(owner)
        self._overview_cache = TTLCache(
            region="cloudsubscribe:platform_overview",
            maxsize=4,
            ttl=int(self._OVERVIEW_TTL_SECONDS),
        )
        self._agent_resource_cache = TTLCache(
            region="cloudsubscribe:agent_resources",
            maxsize=256,
            ttl=30 * 60,
        )

    @staticmethod
    def extract_resource_links(value: Any) -> List[str]:
        if isinstance(value, (list, tuple, set)):
            candidates: Iterable[Any] = value
        else:
            candidates = PlatformIntegrationService._RESOURCE_LINK_PATTERN.findall(
                str(value or "")
            )
        links = []
        for candidate in candidates:
            link = str(candidate or "").strip().rstrip(",，。;；)")
            if link and link not in links:
                links.append(link)
        return links[:50]

    def _cache_stats(self) -> Dict[str, Any]:
        stats: Dict[str, Any] = {}
        if self._search_handler:
            stats["search"] = self._search_handler.get_cache_stats()
        if self._cloud_drive and self._cloud_drive.supports(
                CloudDriveCapability.CACHE_MAINTENANCE
        ):
            cache_service = self._cloud_drive.require(
                CloudDriveCapability.CACHE_MAINTENANCE
            )
            stats[self._cloud_drive.key] = cache_service.get_cache_stats()
        return stats

    def get_platform_overview(self, recent_limit: int = 5) -> Dict[str, Any]:
        limit = max(0, min(int(recent_limit or 0), 20))
        cached = self._overview_cache.get("overview")
        if isinstance(cached, dict):
            result = copy.deepcopy(cached)
            result["recent_history"] = list(cached.get("recent_history") or [])[:limit]
            return result

        history = [
            dict(record)
            for record in (self.get_data("history") or [])
            if isinstance(record, dict)
        ]
        history.sort(key=lambda item: str(item.get("time") or ""), reverse=True)
        today = datetime.datetime.now(pytz.timezone(settings.TZ)).strftime("%Y-%m-%d")
        total = len(history)
        success = sum(record.get("status") == "成功" for record in history)
        failed = sum(record.get("status") == "失败" for record in history)
        transferred_today = sum(
            str(record.get("time") or "").startswith(today) for record in history
        )
        tasks = self._serialize_runtime_tasks()
        provider = self._cloud_drive
        overview = {
            "runtime": {
                "status": self._sync_status,
                "task": self._sync_task_text,
                "progress": self._sync_progress,
                "tasks": tasks,
            },
            "stats": [
                {"title": "总转存", "value": total, "color": "primary", "icon": "mdi-cloud-upload-outline"},
                {"title": "今日转存", "value": transferred_today, "color": "info", "icon": "mdi-calendar-today"},
                {"title": "成功", "value": success, "color": "success", "icon": "mdi-check-circle-outline"},
                {"title": "失败", "value": failed, "color": "error", "icon": "mdi-alert-circle-outline"},
            ],
            "history_count": total,
            "recent_history": history[:20],
            "cache": self._cache_stats(),
            "provider": {
                "key": provider.key if provider else "",
                "name": provider.name if provider else "未配置",
                "capabilities": sorted(
                    capability.value for capability in provider.capabilities
                ) if provider else [],
            },
        }
        self._overview_cache["overview"] = copy.deepcopy(overview)
        result = copy.deepcopy(overview)
        result["recent_history"] = history[:limit]
        return result

    def clear_platform_cache(self) -> Dict[str, int]:
        """清理平台聚合与智能体候选缓存。"""
        counts = {
            "platform_overview": len(list(self._overview_cache.items())),
            "agent_resources": len(list(self._agent_resource_cache.items())),
        }
        self._overview_cache.clear()
        self._agent_resource_cache.clear()
        return counts

    def close(self) -> None:
        pass

    @staticmethod
    def _agent_cache_key(session_id: str, search_id: str) -> str:
        return f"{str(session_id or 'unknown')}:{str(search_id or '').strip()}"

    @staticmethod
    def _normalize_agent_title(value: Any) -> str:
        return re.sub(r"[\W_]+", "", str(value or "").casefold())

    @staticmethod
    def _agent_media_type(value: Any) -> Optional[MediaType]:
        normalized = str(getattr(value, "value", value) or "").strip().lower()
        if normalized in {"movie", MediaType.MOVIE.value.lower()}:
            return MediaType.MOVIE
        if normalized in {"tv", MediaType.TV.value.lower()}:
            return MediaType.TV
        return None

    @staticmethod
    def _subscribe_identity(subscribe: Any) -> Tuple[Any, ...]:
        source = str(getattr(subscribe, "media_source", "") or "").strip()
        media_id = str(getattr(subscribe, "media_id", "") or "").strip()
        if source and media_id:
            return "source", source, media_id
        for field in ("tmdbid", "doubanid", "bangumiid", "anilistid"):
            value = getattr(subscribe, field, None)
            if value not in (None, ""):
                return field, str(value)
        return (
            "title",
            PlatformIntegrationService._normalize_agent_title(
                getattr(subscribe, "name", "")
            ),
            str(getattr(subscribe, "year", "") or ""),
        )

    def _match_agent_subscribe(
            self,
            subscribe_id: Optional[int],
            title: str,
            media_type: Optional[MediaType],
            season: Optional[int],
            latest_season: bool,
    ) -> Tuple[Any, Optional[str]]:
        oper = SubscribeOper()
        subscribe = oper.get(int(subscribe_id)) if subscribe_id else None
        if subscribe_id and not subscribe:
            return None, f"未找到订阅 ID {subscribe_id}"

        normalized_title = self._normalize_agent_title(title)
        if subscribe and normalized_title and normalized_title != self._normalize_agent_title(
                getattr(subscribe, "name", "")
        ):
            return None, "媒体名称与指定订阅不一致"
        if subscribe and media_type and self._agent_media_type(
                getattr(subscribe, "type", None)
        ) != media_type:
            return None, "媒体类型与指定订阅不一致"
        if subscribe and season is not None:
            if int(getattr(subscribe, "season", 0) or 0) == int(season):
                return subscribe, None
            return None, f"指定订阅不对应第 {season} 季"
        if subscribe:
            return subscribe, None

        subscribes = []
        if normalized_title:
            subscribes = [
                item for item in (oper.list() or [])
                if self._normalize_agent_title(getattr(item, "name", ""))
                   == normalized_title
                   and (
                           not media_type
                           or self._agent_media_type(getattr(item, "type", None)) == media_type
                   )
            ]
            identities = {self._subscribe_identity(item) for item in subscribes}
            if len(identities) > 1:
                subscribes = []

        if not subscribes:
            return subscribe, None
        if season is not None:
            matched = [
                item for item in subscribes
                if int(getattr(item, "season", 0) or 0) == int(season)
            ]
            if matched:
                return matched[0], None
            return None, None
        return subscribes[0], None

    @staticmethod
    def _latest_media_season(mediainfo: Any) -> int:
        seasons = []
        for value in (getattr(mediainfo, "seasons", None) or {}).keys():
            try:
                season = int(value)
            except (TypeError, ValueError):
                continue
            if season > 0:
                seasons.append(season)
        if seasons:
            return max(seasons)
        return max(1, int(getattr(mediainfo, "number_of_seasons", 0) or 1))

    def _match_recognized_subscribe(
            self,
            mediainfo: Any,
            media_type: MediaType,
            season: Optional[int],
    ) -> Any:
        return SubscribeOper().get_by(
            type=media_type.value,
            season=season if media_type == MediaType.TV else None,
            tmdbid=getattr(mediainfo, "tmdb_id", None),
            doubanid=getattr(mediainfo, "douban_id", None),
            bangumiid=getattr(mediainfo, "bangumi_id", None),
            anilistid=getattr(mediainfo, "anilist_id", None),
            media_source=getattr(mediainfo, "source", None),
            media_id=getattr(mediainfo, "media_id", None),
        )

    def _recognize_subscribe_media(self, subscribe: Any):
        media_type = (
            MediaType.MOVIE
            if str(getattr(subscribe, "type", "")) == MediaType.MOVIE.value
            else MediaType.TV
        )
        season = int(getattr(subscribe, "season", 0) or 1) if media_type == MediaType.TV else None
        meta = MetaInfo(str(getattr(subscribe, "name", "") or ""))
        meta.year = getattr(subscribe, "year", None)
        meta.type = media_type
        mediainfo = self._sync_handler._recognize_media_once(
            (
                "agent",
                media_type.value,
                getattr(subscribe, "tmdbid", None),
                getattr(subscribe, "doubanid", None),
                getattr(subscribe, "name", None),
                getattr(subscribe, "year", None),
                season or 0,
            ),
            meta=meta,
            mtype=media_type,
            tmdbid=getattr(subscribe, "tmdbid", None),
            doubanid=getattr(subscribe, "doubanid", None),
            cache=True,
        )
        return mediainfo, media_type, season

    def _recognize_agent_media(
            self,
            subscribe: Any,
            title: str,
            media_type: Optional[MediaType],
            season: Optional[int],
            latest_season: bool,
    ) -> Tuple[Any, Optional[MediaType], Optional[int]]:
        if subscribe:
            mediainfo, resolved_type, resolved_season = (
                self._recognize_subscribe_media(subscribe)
            )
            if mediainfo and resolved_type == MediaType.TV:
                if latest_season:
                    resolved_season = self._latest_media_season(mediainfo)
                elif season is not None:
                    resolved_season = season
            return mediainfo, resolved_type, resolved_season

        meta = MetaInfo(title)
        if media_type:
            meta.type = media_type
        if season is not None:
            meta.begin_season = season
        mediainfo = self._sync_handler._recognize_media_once(
            ("agent", media_type.value if media_type else "", title, season or 0),
            meta=meta,
            mtype=media_type,
            cache=True,
        )
        if not mediainfo:
            return None, media_type, season
        resolved_type = self._agent_media_type(getattr(mediainfo, "type", None))
        if not resolved_type:
            return mediainfo, None, season
        if media_type and resolved_type != media_type:
            return None, resolved_type, season
        if resolved_type == MediaType.MOVIE:
            return mediainfo, resolved_type, None
        resolved_season = season
        if latest_season:
            resolved_season = self._latest_media_season(mediainfo)
        if resolved_season is None:
            resolved_season = int(
                getattr(meta, "begin_season", 0)
                or getattr(mediainfo, "season", 0)
                or 1
            )
        return mediainfo, resolved_type, resolved_season

    @staticmethod
    def _candidate_reason(resource: Dict[str, Any], index: int) -> List[str]:
        reasons = []
        if index == 0:
            reasons.append("当前平台规则排序第一")
        source_priority = int(resource.get("source_priority") or 0)
        if source_priority:
            reasons.append(f"搜索源优先级第 {source_priority} 位")
        if resource.get("is_official"):
            reasons.append("官方或官组资源")
        priority = int(resource.get("platform_priority") or 0)
        if priority:
            reasons.append(f"规则优先级 {priority}")
        if not resource.get("need_unlock"):
            reasons.append("无需积分解锁")
        elif int(resource.get("unlock_points") or 0) > 0:
            reasons.append(f"需要 {int(resource.get('unlock_points') or 0)} 积分解锁")
        definition = str(
            resource.get("resolution") or resource.get("quality") or ""
        ).strip()
        if definition:
            reasons.append(f"清晰度 {definition}")
        if resource.get("update_time"):
            reasons.append(f"更新时间 {resource.get('update_time')}")
        return reasons or ["已通过订阅规则筛选"]

    @staticmethod
    def _resource_summary_size(value: Any) -> int:
        if isinstance(value, (int, float)):
            return max(0, int(value))
        match = re.search(
            r"([\d.]+)\s*(B|KB|MB|GB|TB)",
            str(value or ""),
            re.IGNORECASE,
        )
        if not match:
            return 0
        unit = match.group(2).upper()
        return int(
            float(match.group(1))
            * 1024 ** ("B", "KB", "MB", "GB", "TB").index(unit)
        )

    def search_platform_resources(
            self,
            session_id: str,
            subscribe_id: Optional[int] = None,
            title: str = "",
            media_type: str = "",
            season: Optional[int] = None,
            latest_season: bool = False,
            limit: int = 20,
    ) -> Dict[str, Any]:
        """按订阅或媒体名称搜索候选，并保存完整结果供智能体分步选择。"""
        started = time.monotonic()
        title = str(title or "").strip()
        suffix = re.search(r"\s*最新(?:一)?季\s*$", title)
        if suffix:
            latest_season = True
            title = title[:suffix.start()].strip()
        if not subscribe_id and not title:
            return {"success": False, "message": "请提供媒体名称或订阅 ID"}
        if season is not None and latest_season:
            return {"success": False, "message": "指定季号与最新季不能同时使用"}
        requested_type = self._agent_media_type(media_type)
        if media_type and not requested_type:
            return {
                "success": False,
                "message": "媒体类型仅支持 movie（电影）或 tv（电视剧）",
            }
        if not requested_type and (season is not None or latest_season):
            requested_type = MediaType.TV
        try:
            if not self._search_handler:
                return {"success": False, "message": "搜索服务尚未就绪"}
            subscribe, match_error = self._match_agent_subscribe(
                subscribe_id=subscribe_id,
                title=title,
                media_type=requested_type,
                season=season,
                latest_season=latest_season,
            )
            if match_error:
                return {"success": False, "message": match_error}
            if subscribe and not title:
                title = str(getattr(subscribe, "name", "") or "").strip()
            mediainfo, resolved_type, resolved_season = self._recognize_agent_media(
                subscribe=subscribe,
                title=title,
                media_type=requested_type,
                season=season,
                latest_season=latest_season,
            )
            if not mediainfo:
                return {"success": False, "message": f"无法识别媒体：{title}"}
            if resolved_type not in {MediaType.MOVIE, MediaType.TV}:
                return {"success": False, "message": "仅支持电影或电视剧资源搜索"}
            if resolved_type == MediaType.MOVIE and (season or latest_season):
                return {"success": False, "message": "电影不支持季号或最新季参数"}
            subscribe_type = self._agent_media_type(
                getattr(subscribe, "type", None)
            ) if subscribe else None
            subscribe_season = int(
                getattr(subscribe, "season", 0) or 0
            ) if subscribe_type == MediaType.TV else None
            if (
                    subscribe_type != resolved_type
                    or subscribe_season != resolved_season
            ):
                subscribe = self._match_recognized_subscribe(
                    mediainfo,
                    resolved_type,
                    resolved_season,
                )
            sources = self._search_handler.get_enabled_sources()
            if not sources:
                return {"success": False, "message": "没有可用的搜索源"}
            source_results = self._search_handler.search_sources(
                sources=sources,
                mediainfo=mediainfo,
                media_type=resolved_type,
                season=resolved_season,
                subscribe=subscribe,
            )
            resources = []
            for source_priority, source in enumerate(sources, start=1):
                for source_position, resource in enumerate(
                        source_results.get(source) or [], start=1
                ):
                    item = dict(resource)
                    item.setdefault("source", source)
                    item["source_priority"] = source_priority
                    item["source_position"] = source_position
                    resources.append(item)
            resources = resources[: max(1, min(int(limit or 20), 50))]
        except Exception as error:
            logger.error(f"智能体搜索网盘资源失败：{error}")
            return {"success": False, "message": "搜索资源失败，请检查媒体信息和搜索源配置"}

        candidates = []
        cached_resources = {}
        bound_subscribe_id = int(getattr(subscribe, "id", 0) or 0) or None
        for index, resource in enumerate(resources):
            candidate_id = f"r{index + 1:03d}"
            item = dict(resource)
            item["candidate_id"] = candidate_id
            cached_resources[candidate_id] = item
            available = bool(
                str(item.get("url") or "").strip()
                or (
                        item.get("need_unlock")
                        and item.get("slug")
                )
            )
            candidates.append({
                "candidate_id": candidate_id,
                "title": str(item.get("title") or "未知资源"),
                "source": str(item.get("source") or "unknown"),
                "rank": index + 1,
                "source_priority": int(item.get("source_priority") or 0),
                "source_position": int(item.get("source_position") or 0),
                "resource_type": str(
                    item.get("resource_type") or item.get("pan_type") or "unknown"
                ).lower(),
                "size": item.get("size") or 0,
                "resolution": item.get("resolution") or "",
                "quality": item.get("quality") or "",
                "update_time": item.get("update_time") or "",
                "platform_priority": int(item.get("platform_priority") or 0),
                "is_official": bool(item.get("is_official")),
                "need_unlock": bool(item.get("need_unlock")),
                "unlock_points": int(item.get("unlock_points") or 0),
                "available": available,
                "transferable": bool(bound_subscribe_id and available),
                "recommendation_reasons": self._candidate_reason(item, index),
            })

        search_id = uuid4().hex[:12]
        media = {
            "subscribe_id": bound_subscribe_id,
            "title": str(getattr(mediainfo, "title", "") or title),
            "year": getattr(mediainfo, "year", None),
            "type": resolved_type.value,
            "season": resolved_season,
        }
        cache_key = self._agent_cache_key(session_id, search_id)
        self._agent_resource_cache[cache_key] = {
            "subscribe_id": bound_subscribe_id,
            "media": media,
            "resources": cached_resources,
        }
        available = [item for item in candidates if item["available"]]
        transferable = [item for item in candidates if item["transferable"]]
        total_size = sum(
            self._resource_summary_size(item.get("size"))
            for item in candidates
        )
        summary = {
            "total": len(candidates),
            "available": len(available),
            "transferable": len(transferable),
            "need_unlock": sum(item["need_unlock"] for item in candidates),
            "official": sum(item["is_official"] for item in candidates),
            "free": sum(not item["need_unlock"] for item in candidates),
            "total_size_bytes": total_size,
            "average_size_bytes": total_size // len(candidates) if candidates else 0,
            "latest_update_time": max(
                (str(item.get("update_time") or "") for item in candidates),
                default="",
            ),
            "by_source": dict(Counter(item["source"] for item in candidates)),
            "by_resource_type": dict(
                Counter(item["resource_type"] for item in candidates)
            ),
        }
        logger.info(
            f"智能体资源搜索完成：{media['title']}"
            f"{f' S{resolved_season:02d}' if resolved_season else ''}，"
            f"订阅 {bound_subscribe_id or '未绑定'}，候选 {len(candidates)} 个，"
            f"耗时 {int((time.monotonic() - started) * 1000)} ms"
        )
        return {
            "success": True,
            "message": "资源搜索完成",
            "search_id": search_id,
            "media": media,
            "summary": summary,
            "source_priority_order": sources,
            "sort_rule": (
                "先按配置的搜索源优先级，再按资源类型优先级、MoviePilot规则优先级、"
                "官组、可直接访问状态、更新时间和解锁积分保持稳定顺序"
            ),
            "recommended_candidate_ids": [
                item["candidate_id"] for item in available[:3]
            ],
            "candidates": candidates,
            "next_step": (
                    "请使用中文汇总候选，并结合规则优先级、官组、清晰度、"
                    "资源大小、更新时间和解锁成本说明推荐理由。"
                    + (
                        "用户需要手动选择时，优先调用 ask_user_choice 展示候选 ID；"
                        "收到选择后使用 search_id 调用 cloudsubscribe_select_resources。"
                        if bound_subscribe_id else
                        "本次搜索未绑定现有订阅，只能展示和推荐；需要转存时先创建或选择订阅，"
                        "再使用订阅 ID 重新搜索。"
                    )
                    + "不要自行构造或改写资源链接。"
            ),
        }

    def select_platform_resources(
            self,
            session_id: str,
            search_id: str,
            candidate_ids: List[str],
    ) -> Dict[str, Any]:
        """只从当前会话缓存取回已搜索链接并提交，禁止模型直接拼接链接。"""
        cache_key = self._agent_cache_key(session_id, search_id)
        cached = self._agent_resource_cache.get(cache_key)
        if not isinstance(cached, dict):
            return {"success": False, "message": "候选资源已过期，请重新搜索"}
        subscribe_id = int(cached.get("subscribe_id") or 0)
        if subscribe_id <= 0:
            return {
                "success": False,
                "message": "本次搜索未关联现有订阅，请先创建或选择订阅后重新搜索",
            }
        resources = cached.get("resources") or {}
        selected = []
        invalid = []
        for candidate_id in dict.fromkeys(str(value).strip() for value in candidate_ids):
            resource = resources.get(candidate_id)
            usable = bool(
                str((resource or {}).get("url") or "").strip()
                or (
                        (resource or {}).get("need_unlock")
                        and (resource or {}).get("slug")
                )
            )
            if not usable:
                invalid.append(candidate_id)
                continue
            selected.append(dict(resource))
        if invalid:
            return {
                "success": False,
                "message": f"候选不可用或不可直接提交：{', '.join(invalid)}",
            }
        if not selected:
            return {"success": False, "message": "没有选择可提交的候选资源"}
        result = dict(self.start_selected_resources(int(subscribe_id), selected))
        data = dict(result.get("data") or {})
        data["candidate_ids"] = list(dict.fromkeys(candidate_ids))
        result["data"] = data
        return result

    def get_runtime_performance(self, include_tasks: bool = True) -> Dict[str, Any]:
        """汇总当前任务、搜索缓存和同步阶段性能指标。"""
        now = time.time()
        tasks = self._serialize_runtime_tasks() if include_tasks else []
        search_metrics = (
            self._search_handler.get_search_metrics()
            if self._search_handler else {}
        )
        sync_metrics = (
            self._sync_handler.get_sync_metrics()
            if self._sync_handler else {}
        )
        queue = {"pending": 0, "active": 0}
        if self._subscribe_search_queue_lock is not None:
            with self._subscribe_search_queue_lock:
                queue = {
                    "pending": len(self._subscribe_search_pending),
                    "active": len(self._subscribe_search_active),
                }
        external_calls = sum(
            int(metric.get("external_calls") or 0)
            for metric in search_metrics.values()
        )
        external_elapsed_ms = sum(
            int(metric.get("external_elapsed_ms") or 0)
            for metric in search_metrics.values()
        )
        positive_hits = sum(
            int(metric.get("positive_cache_hits") or 0)
            for metric in search_metrics.values()
        )
        negative_hits = sum(
            int(metric.get("negative_cache_hits") or 0)
            for metric in search_metrics.values()
        )
        run_elapsed_ms = (
            int(max(0.0, now - self._sync_run_started_at) * 1000)
            if self._sync_running and self._sync_run_started_at else 0
        )
        return {
            "success": True,
            "message": "运行性能数据已汇总",
            "runtime": {
                "status": self._sync_status,
                "task": self._sync_task_text,
                "progress": self._sync_progress,
                "running": self._sync_running,
                "elapsed_ms": run_elapsed_ms,
                "last_elapsed_ms": int(self._sync_last_elapsed_ms or 0),
                "last_finished_at": float(self._sync_last_finished_at or 0),
                "transferred": int(self._sync_context.get("transferred") or 0),
                "configured_concurrency": int(self._subscription_concurrency or 1),
            },
            "queue": queue,
            "tasks": tasks,
            "search": {
                "summary": {
                    "external_calls": external_calls,
                    "external_elapsed_ms": external_elapsed_ms,
                    "positive_cache_hits": positive_hits,
                    "negative_cache_hits": negative_hits,
                },
                "sources": search_metrics,
                "cache": self._search_handler.get_cache_stats()
                if self._search_handler else {},
            },
            "sync_stages": sync_metrics,
            "next_step": (
                "请用中文说明当前任务是否正常推进、最耗时的搜索源或同步阶段、"
                "缓存命中效果，以及是否需要调整并发或缓存配置。"
            ),
        }

    def api_platform_overview(self) -> Dict[str, Any]:
        return {"success": True, "data": self.get_platform_overview(6)}

    def start_platform_sync(self) -> Dict[str, Any]:
        return self.api_vue_start_sync()

    def submit_platform_links(
            self,
            subscribe_id: int,
            resource_links: Any,
            wait: bool = False,
    ) -> Dict[str, Any]:
        links = self.extract_resource_links(resource_links)
        return self.api_vue_start_manual_sync({
            "subscribe_id": subscribe_id,
            "resource_links": links,
        }, wait=wait)

    @staticmethod
    def _set_workflow_output(context: Any, name: str, result: Dict[str, Any]) -> Any:
        outputs = dict(getattr(context, "node_outputs", None) or {})
        outputs[name] = dict(result)
        context.node_outputs = outputs
        return context

    def workflow_start_sync(
            self,
            context: Any,
            subscribe_id: int = 0,
            subscribe_ids: Optional[List[int]] = None,
            subscribe_states: Optional[str] = None,
            **kwargs,
    ) -> tuple[bool, Any]:
        requested_ids: List[int] = []
        raw_ids: Any = subscribe_ids
        if raw_ids is None and subscribe_id:
            raw_ids = [subscribe_id]
        if raw_ids is None:
            context_subscribes = list(getattr(context, "subscribes", None) or [])
            raw_ids = [getattr(item, "id", 0) for item in context_subscribes] or None
        if raw_ids is not None:
            if not isinstance(raw_ids, (list, tuple, set)):
                raw_ids = [raw_ids]
            try:
                normalized_ids = {int(value) for value in raw_ids}
            except (TypeError, ValueError):
                result = {"success": False, "message": "订阅 ID 参数格式错误"}
                return False, self._set_workflow_output(
                    context, "cloudsubscribe_sync", result
                )
            requested_ids = sorted(value for value in normalized_ids if value > 0)
            existing_ids = {
                int(item.id) for item in (SubscribeOper().list() or [])
                if int(getattr(item, "id", 0) or 0) in requested_ids
            }
            missing_ids = [value for value in requested_ids if value not in existing_ids]
            if not requested_ids or missing_ids:
                message = (
                    f"订阅不存在：{', '.join(map(str, missing_ids))}"
                    if missing_ids else "请提供有效的订阅 ID"
                )
                result = {"success": False, "message": message}
                return False, self._set_workflow_output(
                    context, "cloudsubscribe_sync", result
                )
        result: Dict[str, Any] = {}
        self.sync_subscribes(
            subscribe_ids=requested_ids or None,
            subscribe_states=subscribe_states,
            result=result,
        )
        data = dict(result.get("data") or {})
        data.update({
            "scope": (
                "selected" if requested_ids
                else "states" if subscribe_states
                else "all"
            ),
            "subscribe_ids": requested_ids,
            "subscribe_count": len(requested_ids),
            "subscribe_states": subscribe_states,
        })
        result["data"] = data
        return bool(result.get("success")), self._set_workflow_output(
            context, "cloudsubscribe_sync", result
        )

    def workflow_process_links(
            self,
            context: Any,
            subscribe_id: int = 0,
            resource_links: Any = None,
            **kwargs,
    ) -> tuple[bool, Any]:
        if not subscribe_id:
            subscribes = list(getattr(context, "subscribes", None) or [])
            if len(subscribes) == 1:
                subscribe_id = int(getattr(subscribes[0], "id", 0) or 0)
        links = self.extract_resource_links(
            resource_links if resource_links is not None else getattr(context, "content", "")
        )
        result = self.submit_platform_links(subscribe_id, links, wait=True)
        return bool(result.get("success")), self._set_workflow_output(
            context, "cloudsubscribe_links", result
        )
