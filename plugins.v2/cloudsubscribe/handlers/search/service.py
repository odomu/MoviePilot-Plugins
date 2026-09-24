"""
搜索处理模块
"""
import copy
import hashlib
import json
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple, Callable

from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType

from .platform_rules import PlatformRuleService
from ...core import (
    SEARCH_CIRCUIT_BREAKER,
    SearchCapability,
    SearchQuery,
    format_search_label,
    format_search_log_prefix,
    resolve_component,
)
from ...core.media import tmdb_id_of
from ...search.dian115 import Dian115SearchService
from ...search.hdhaven import HDHavenSearchService
from ...search.hdhive import HDHiveSearchService
from ...search.http_client import RequestGateCooldown
from ...search.matching import is_anime_media, positive_ints, unique_texts
from ...search.mikan.service import filter_fansubs
from ...search.pansou import PanSouSearchService
from ...search.registry import create_search_registry
from ...search.scanner import SearchSourceRegistry
from ...search.types import SUPPORTED_RESOURCE_TYPES, normalize_resource_type
from ...utils.cache import create_platform_ttl_cache

_COMPONENT_TYPES = (
    HDHiveSearchService,
    Dian115SearchService,
    HDHavenSearchService,
    PanSouSearchService,
    PlatformRuleService,
)


class SearchHandler:
    """搜索处理器"""

    _TEST_RESULT_LIMIT = 10

    @staticmethod
    def _normalize_pansou_values(value: Any) -> List[str]:
        if isinstance(value, str):
            value = re.split(r"[,，\n]+", value)
        return unique_texts(value)

    def __getattr__(self, name):
        return resolve_component(
            self, _COMPONENT_TYPES, name, "_search_components"
        )

    def __init__(
            self,
            plugin: Any = None,
            **kwargs,
    ):
        """
        初始化搜索处理器。

        :param plugin: 宿主插件实例，传入时自动解析全部搜索客户端、配置项与存储。
        :param kwargs: 独立或覆盖配置参数。
        """
        params: Dict[str, Any] = dict(kwargs)

        def get_val(name: str, default: Any = None) -> Any:
            if name in params and params[name] is not None:
                return params[name]
            if plugin is not None:
                val = getattr(plugin, f"_{name}", None)
                if val is None:
                    val = getattr(plugin, name, None)
                if val is not None:
                    return val
            return params.get(name, default)

        self._plugin = plugin
        definitions = SearchSourceRegistry.get_definitions()
        for definition in definitions:
            for key in definition.get_config_keys():
                attr = f"_{key}"
                if attr not in self.__dict__:
                    setattr(self, attr, get_val(key))

        anime_pack_preferred = get_val("anime_pack_preferred", True)
        self._anime_pack_preferred = bool(anime_pack_preferred)
        self._pansou_channels = self._normalize_pansou_values(get_val("pansou_channels"))
        self._pansou_plugins = self._normalize_pansou_values(get_val("pansou_plugins"))
        self._pansou_cloud_types = [
            value.lower() for value in self._normalize_pansou_values(
                get_val("pansou_cloud_types")
            )
        ]
        self._pansou_filter = {
            "include": self._normalize_pansou_values(get_val("pansou_filter_include")),
            "exclude": self._normalize_pansou_values(get_val("pansou_filter_exclude")),
        }
        resource_type_order = get_val("resource_type_order")
        self._resource_type_order_config = list(
            ["115", "ed2k"]
            if resource_type_order is None else resource_type_order
        )
        self._resource_type_order_map = {}
        for index, value in enumerate(self._resource_type_order_config):
            self._resource_type_order_map.setdefault(value, index)

        pansou_concurrency = get_val("pansou_concurrency")
        try:
            self._pansou_concurrency = (
                max(1, min(int(pansou_concurrency), 100))
                if pansou_concurrency else None
            )
        except (TypeError, ValueError):
            self._pansou_concurrency = None

        self._pansou_result_limit = max(1, min(int(get_val("pansou_result_limit", 10) or 10), 100))
        self._pansou_refresh = bool(get_val("pansou_refresh", True))
        self._pansou_timeout = max(5, min(int(get_val("pansou_timeout", 60) or 60), 120))
        self._juying_resource_types = [
            value for value in unique_texts(
                self._resource_type_order_config, str.lower
            )
            if value in SUPPORTED_RESOURCE_TYPES
        ]
        self._search_source_order = get_val("search_source_order", []) or []
        self._search_proxy = get_val("search_proxy")
        self._search_cache_enabled = bool(get_val("search_cache_enabled", True))
        search_cache_ttl_minutes = get_val("search_cache_ttl_minutes", 30)
        self._search_cache_ttl = max(60, int(search_cache_ttl_minutes or 30) * 60)
        self._search_concurrency = max(1, min(int(get_val("search_concurrency", 2) or 1), 5))
        self._search_source_timeout = max(5, min(int(get_val("search_source_timeout", 60) or 60), 120))
        self._search_circuit_breaker_enabled = bool(get_val("search_circuit_breaker_enabled", True))
        self._search_circuit_breaker_threshold = max(
            1, min(int(get_val("search_circuit_breaker_threshold", 3) or 3), 10)
        )
        self._search_circuit_breaker_cooldown = max(
            10, min(int(get_val("search_circuit_breaker_cooldown", 60) or 60), 600)
        )
        SEARCH_CIRCUIT_BREAKER.configure(
            enabled=self._search_circuit_breaker_enabled,
            failure_threshold=self._search_circuit_breaker_threshold,
            cooldown_seconds=self._search_circuit_breaker_cooldown,
        )
        self._enable_cloud_upgrade = bool(get_val("enable_cloud_upgrade", False))
        self._upgrade_subscribe_ids = list(get_val("upgrade_subscribe_ids", []) or [])
        self._upgrade_subscribe_id_set = {
            str(value) for value in self._upgrade_subscribe_ids
        }
        self._search_cache_limit = 200
        self._search_negative_ttl = min(self._search_cache_ttl, 10 * 60)
        self._search_cache = create_platform_ttl_cache(
            "search:results",
            self,
            maxsize=self._search_cache_limit,
            ttl=self._search_cache_ttl,
        )
        self._search_metrics_lock = threading.RLock()
        self._search_metrics: Dict[str, Dict[str, int]] = {}
        self._platform_filter_lock = threading.RLock()
        self._platform_filter_module = None
        self._platform_filter_signature = ""
        self._platform_filter_signature_cache = create_platform_ttl_cache(
            "platform:filter_rules", maxsize=1, ttl=5
        )
        should_stop = get_val("should_stop")
        if should_stop is None and plugin is not None:
            should_stop = getattr(plugin, "_stop_requested", None)
        self._should_stop = should_stop
        for definition in definitions:
            definition.configure_owner(self, params)
        self._source_timeouts = {
            definition.id: definition.get_search_timeout(
                self.__dict__, self._search_source_timeout
            )
            for definition in definitions
        }
        self._search_registry = create_search_registry(self)


        if plugin is not None and hasattr(plugin, "get_data") and hasattr(plugin, "save_data"):
            self.configure_point_storage(plugin.get_data, plugin.save_data)

    def _is_cloud_upgrade_subscribe(self, subscribe: Any) -> bool:
        """判断订阅是否属于插件网盘洗版范围。"""
        if self._enable_cloud_upgrade and bool(
                getattr(subscribe, "_manual_upgrade", False)
        ):
            return True
        if (
                not self._enable_cloud_upgrade
                or not subscribe
                or not bool(getattr(subscribe, "best_version", False))
        ):
            return False
        selected_ids = self._upgrade_subscribe_id_set
        return not selected_ids or str(getattr(subscribe, "id", "")) in selected_ids

    def _stop_requested(self) -> bool:
        try:
            return bool(self._should_stop and self._should_stop())
        except Exception as error:
            logger.warning(f"读取搜索停止状态失败：{error}")
            return False

    def _get_source_search_timeout(self, source: str) -> float:
        """获取指定搜索渠道的超时时间（秒）。"""
        source = str(source or "").strip().lower()
        return self._source_timeouts.get(source, self._search_source_timeout)

    def get_enabled_sources(self, media_type: Optional[MediaType] = None) -> List[str]:
        """返回用户选择且当前可用的搜索渠道（完全由 search_source_order 优先级列表控制）。"""
        available_set = {
            provider.key for provider in self._search_registry.available()
        }
        anime_only_sources = {"mikan", "animegarden"}
        return [
            source for source in self._search_source_order
            if source in available_set
               and not (media_type == MediaType.MOVIE and source in anime_only_sources)
        ]

    def get_all_search_sources(self) -> List[str]:
        """返回所有当前已注册的搜索渠道（供网盘资源列表实时嗅探使用，无需手动启用即可搜索）。"""
        return [provider.key for provider in self._search_registry.available()]

    def get_available_sources_meta(
            self,
            mediainfo: Optional[Any] = None,
            media_type: Optional[MediaType] = None,
            is_anime: Optional[bool] = None,
    ) -> List[Dict[str, str]]:
        """返回规范有序的渠道列表（供前端详情页/弹窗直接渲染Tab），统一由后端控制显示与排序。"""
        registered = {provider.key: provider.name for provider in self._search_registry.available()}
        if not registered:
            return []
        display = {
            def_cls.id: def_cls
            for def_cls in SearchSourceRegistry.get_definitions()
        }

        # 优先读取用户配置的优先级顺序，其余按自描述规范标准偏好排列
        configured_order = getattr(self, "_search_source_order", []) or []
        default_pref = [d.id for d in SearchSourceRegistry.get_definitions()]

        merged_order: List[str] = []
        for s in list(configured_order) + default_pref:
            s_clean = str(s).strip().lower()
            if s_clean in registered and s_clean not in merged_order:
                merged_order.append(s_clean)
        for s in registered:
            if s not in merged_order:
                merged_order.append(s)

        anime_sources = {"mikan", "animegarden"}
        if is_anime is None and mediainfo:
            is_anime = is_anime_media(mediainfo)

        if is_anime:
            # 动漫番剧优先展示动漫源
            anime_part = [s for s in merged_order if s in anime_sources]
            other_part = [s for s in merged_order if s not in anime_sources]
            merged_order = anime_part + other_part
        elif media_type == MediaType.MOVIE:
            # 非动漫电影剔除纯番剧更新源
            merged_order = [s for s in merged_order if s not in anime_sources]

        def entry(src_key: str) -> Dict[str, str]:
            def_cls = display.get(src_key)
            return {
                "key": src_key,
                "name": registered.get(src_key, src_key),
                "icon": getattr(def_cls, "icon", "") or "mdi-magnify",
                "color": getattr(def_cls, "color", "") or "grey",
            }

        return [entry(src_key) for src_key in merged_order]



    @property
    def source_concurrency_enabled(self) -> bool:
        return self._search_concurrency > 1

    def _search_cache_key(
            self,
            source: str,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int],
            target_episodes: Optional[List[int]],
            subscribe: Any,
    ) -> str:
        media_id = (
                getattr(mediainfo, "tmdb_id", None)
                or tmdb_id_of(subscribe)
        )
        context = {
            "source": source,
            "tmdb_id": media_id,
            "title": str(getattr(mediainfo, "title", "") or "").strip(),
            "year": getattr(mediainfo, "year", None),
            "type": getattr(media_type, "value", str(media_type)),
            "season": int(season or 0),
            "episodes": sorted(positive_ints(target_episodes)),
            "best_version": self._is_cloud_upgrade_subscribe(subscribe),
            "filter_groups": list(getattr(subscribe, "filter_groups", None) or []),
            "provider": dict(
                self._search_registry.get(source).policy.cache_context
            ),
        }
        encoded = json.dumps(context, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha1(encoded.encode("utf-8")).hexdigest()

    def reset_search_metrics(self) -> None:
        with self._search_metrics_lock:
            self._search_metrics = {}

    def _record_search_metric(self, source: str, metric: str, value: int = 1) -> None:
        with self._search_metrics_lock:
            counters = self._search_metrics.setdefault(
                str(source or "unknown"),
                {
                    "external_calls": 0,
                    "positive_cache_hits": 0,
                    "negative_cache_hits": 0,
                    "external_elapsed_ms": 0,
                },
            )
            counters[metric] = int(counters.get(metric) or 0) + int(value or 0)

    def get_search_metrics(self) -> Dict[str, Dict[str, int]]:
        with self._search_metrics_lock:
            return copy.deepcopy(self._search_metrics)

    def _get_cached_results(
            self, key: str, source: str, search_label: str
    ) -> Optional[List[Dict]]:
        if not self._search_cache_enabled:
            return None
        item = self._search_cache.get(key)
        if not isinstance(item, dict):
            return None
        cached_results = item.get("results")
        results = copy.deepcopy(cached_results) if isinstance(cached_results, list) else None
        if results is None:
            return None
        policy = self._search_registry.get(source).policy
        if not results and not policy.cache_empty_results:
            self._search_cache.pop(key, None)
            return None
        self._record_search_metric(
            source,
            "negative_cache_hits" if not results else "positive_cache_hits",
        )
        logger.debug(
            f"[{search_label}][{source.upper()}] 搜索缓存命中：候选={len(results)}"
            f"{'（空结果缓存）' if not results else ''}"
        )
        return results

    def _set_cached_results(
            self, key: str, label: str, results: List[Dict], source: str = ""
    ) -> None:
        if not self._search_cache_enabled:
            return
        policy = self._search_registry.get(source).policy
        if not results and not policy.cache_empty_results:
            return
        self._search_cache.set(
            key,
            {
                "label": label,
                "results": copy.deepcopy(list(results or [])),
                "negative": not bool(results),
            },
            ttl=self._search_negative_ttl if not results else self._search_cache_ttl,
        )

    def get_cache_stats(self) -> Dict[str, Any]:
        """返回搜索缓存占用，并顺带清理过期项。"""
        positive = 0
        negative = 0
        for _, item in self._search_cache.items():
            if not isinstance(item, dict):
                continue
            if item.get("negative"):
                negative += 1
            else:
                positive += 1
        return {
            "enabled": self._search_cache_enabled,
            "entries": positive + negative,
            "positive": positive,
            "negative": negative,
            "limit": self._search_cache_limit,
            "ttl_seconds": self._search_cache_ttl,
            "negative_ttl_seconds": self._search_negative_ttl,
        }

    def clear_search_cache(self) -> Dict[str, int]:
        """清空搜索结果及各搜索源的详情、预览和响应缓存。"""
        search_count = len(list(self._search_cache.items()))
        self._search_cache.clear()
        self._platform_filter_signature_cache.clear()
        source_counts: Dict[str, int] = {}
        for provider in self._search_registry.available():
            if provider.supports(SearchCapability.POINT_BUDGET):
                source_counts[f"{provider.key}_unlocked_urls"] = int(
                    provider.require(
                        SearchCapability.POINT_BUDGET
                    ).clear_cached_urls() or 0
                )
            if provider.supports(SearchCapability.CACHE_MAINTENANCE):
                source_counts[provider.key] = int(provider.clear_cache() or 0)
        return {
            "search_results": search_count,
            **source_counts,
        }

    def _providers_with(self, capability: SearchCapability):
        return tuple(
            provider for provider in self._search_registry.available()
            if provider.supports(capability)
        )

    def clear_point_history(self) -> Dict[str, int]:
        """清空所有积分搜索渠道的持久化消费历史。"""
        return {
            provider.key: int(provider.require(
                SearchCapability.POINT_BUDGET
            ).clear_history() or 0)
            for provider in self._providers_with(SearchCapability.POINT_BUDGET)
        }

    def has_unlock_budget(self, source: str, points: Any) -> bool:
        provider = self._search_registry.get(source)
        return bool(provider.require(
            SearchCapability.POINT_BUDGET
        ).has_budget(points))

    def source_name(self, source: str) -> str:
        return self.get_source_display_name(source)

    def get_source_display_name(self, source: str) -> str:
        source_key = str(source or "").strip().lower()
        try:
            return self._search_registry.get(source_key).name
        except Exception:
            fallback_names = {
                "quark": "夸克网盘",
                "alipan": "阿里云盘",
                "115": "115网盘",
                "xunlei": "迅雷云盘",
                "uc": "UC网盘",
                "pansou": "盘搜",
                "tg": "Telegram",
                "mikan": "蜜柑计划",
                "animegarden": "动漫花园",
                "manual": "手动资源",
            }
            return fallback_names.get(source_key, source_key.upper())

    def _dispatch_default_search_progress(self, subscribe: Any, progress_data: Dict[str, Any]) -> None:
        if not self._plugin:
            return
        update_func = getattr(self._plugin, "_update_sync_task", None)
        task_id_func = getattr(self._plugin, "_sync_task_id", None)
        if not update_func or not task_id_func:
            return
        try:
            task_id = task_id_func(subscribe)
            total = max(1, progress_data.get("total", 1))
            completed = progress_data.get("completed", 0)
            ratio = completed / total
            progress_val = 40 + int(ratio * 25) if progress_data.get("active") else 65
            update_func(
                task_id,
                phase=progress_data.get("summary") or "搜索候选资源",
                progress=progress_val,
                search_active=progress_data.get("active", False),
                search_channels=progress_data.get("channels", []),
                search_total_results=progress_data.get("total_results", 0),
            )
        except Exception as e:
            logger.debug(f"自动推送搜索运行态失败: {e}")

    def supports(self, source: str, capability: SearchCapability) -> bool:
        try:
            return self._search_registry.get(source).supports(capability)
        except KeyError:
            return False

    def get_source_client(self, source: str) -> Any:
        return self._search_registry.get(source).require(
            SearchCapability.ACCOUNT
        )

    def unlock_resource(
            self,
            source: str,
            candidate: Dict[str, Any],
            search_label: str = "",
    ) -> Any:
        return self._search_registry.get(source).unlock(
            candidate, search_label=search_label
        )

    def preview_resource(
            self, source: str, candidate: Dict[str, Any]
    ) -> Dict[str, Any]:
        return self._search_registry.get(source).preview(candidate)

    def close(self, release_cache: bool = False) -> None:
        """释放搜索客户端。"""
        for provider in self._providers_with(SearchCapability.LIFECYCLE):
            provider.close()
        if (
                release_cache
                and self._hdhive_client
                and hasattr(self._hdhive_client, "close")
        ):
            self._hdhive_client.close()

    def configure_point_storage(self, get_data, save_data) -> None:
        """为所有积分搜索渠道配置持久化读写。"""
        for provider in self._providers_with(SearchCapability.POINT_BUDGET):
            provider.require(SearchCapability.POINT_BUDGET).configure_storage(
                get_data, save_data
            )

    def reset_point_budgets(self) -> None:
        """重置本轮同步的全部积分渠道任务预算。"""
        for provider in self._providers_with(SearchCapability.POINT_BUDGET):
            provider.require(SearchCapability.POINT_BUDGET).reset_task()

    def reset_subscription_budgets(self, subscription_key: str = "") -> None:
        """加载当前订阅在全部积分渠道中的历史消费。"""
        for provider in self._providers_with(SearchCapability.POINT_BUDGET):
            provider.require(SearchCapability.POINT_BUDGET).reset_subscription(
                subscription_key
            )

    def clear_subscription_budgets(self, subscription_key: str) -> None:
        """订阅完成后清理全部积分渠道的历史账本。"""
        for provider in self._providers_with(SearchCapability.POINT_BUDGET):
            provider.require(SearchCapability.POINT_BUDGET).clear_subscription(
                subscription_key
            )

    def _run_source_search(
            self,
            source: str,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            target_episode_air_dates: Optional[Dict[int, str]] = None,
            subscribe: Any = None,
            resource_list_mode: bool = False,
            result_limit: Optional[int] = None,
    ) -> List[Dict]:
        try:
            provider = self._search_registry.get(source)
        except KeyError as error:
            raise ValueError("搜索渠道未配置或不可用") from error
        query = SearchQuery(
            mediainfo=mediainfo,
            media_type=media_type,
            season=season,
            target_episodes=tuple(target_episodes or ()),
            target_episode_air_dates=dict(target_episode_air_dates or {}),
            subscribe=subscribe,
            resource_list_mode=resource_list_mode,
            result_limit=result_limit,
        )
        prefix = format_search_log_prefix(query, provider.key)
        started = time.monotonic()
        try:
            results = provider.search(query)
        except Exception as error:
            logger.warning(
                f"{prefix} 搜索失败：{error}，"
                f"耗时={time.monotonic() - started:.2f}s"
            )
            raise
        logger.debug(
            f"{prefix} 搜索完成："
            f"候选={len(results)}，耗时={time.monotonic() - started:.2f}s"
        )
        return results

    def _prepare_source_results(
            self,
            results: List[Dict],
            source: str,
            mediainfo: MediaInfo,
            media_type: MediaType,
            subscribe: Any,
            season: Optional[int],
            target_episodes: Optional[List[int]],
            apply_platform_rules: bool,
    ) -> List[Dict]:
        if is_anime_media(mediainfo) and source in ("mikan", "animegarden"):
            before = len(results)
            prefix = source

            strict_filter = bool(apply_platform_rules)
            results = filter_fansubs(
                results,
                prefix=prefix,
                strict=strict_filter,
                fansub_order=getattr(self, f"_{prefix}_fansub_order", None),
                exclude_re=getattr(self, f"_{prefix}_exclude_re", None),
                no_subs_re=getattr(self, f"_{prefix}_no_subs_re", None),
                chinese_re=getattr(self, f"_{prefix}_chinese_re", None),
            )
            if before != len(results):
                logger.debug(f"[{source.upper()}] 字幕与排除过滤（strict={strict_filter}）：{before} -> {len(results)}")
        for result in results:
            result.setdefault("source", source)
        ordered = self._prefilter_resource_order(
            results,
            season=season,
            target_episodes=target_episodes,
            log_prefix=f"[{self._search_label(mediainfo, media_type, season)}]"
                       f"[{source.upper()}]",
            filter_unsupported_types=apply_platform_rules,
        )
        if not apply_platform_rules:
            return ordered
        return self._filter_by_platform_rules(
            ordered,
            mediainfo,
            subscribe,
            season=season,
            target_episodes=target_episodes,
            prefiltered=True,
        )

    def test_source_result_limit(self) -> int:
        return self._TEST_RESULT_LIMIT

    def resolve_source_resource(self, source: str, **kwargs) -> Dict[str, Any]:
        try:
            provider = self._search_registry.get(source)
            provider.require(SearchCapability.RESOURCE_RESOLVE)
        except (KeyError, RuntimeError) as error:
            raise ValueError("搜索渠道未配置资源解析能力") from error
        return provider.resolve(**kwargs)

    def test_source(
            self,
            source: str,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int] = None,
    ) -> List[Dict]:
        source = str(source or "").strip().lower()
        try:
            provider = self._search_registry.get(source)
        except KeyError as error:
            raise ValueError("搜索渠道未配置或不可用") from error
        cache_key = self._search_cache_key(
            source, mediainfo, media_type, season, None, None
        )
        self._search_cache.pop(cache_key, None)
        if provider.supports(SearchCapability.CACHE_MAINTENANCE):
            provider.clear_cache()
        results = self._run_source_search(
            source,
            mediainfo,
            media_type,
            season,
            resource_list_mode=True,
            result_limit=self._TEST_RESULT_LIMIT,
        )
        return list(results)[:self._TEST_RESULT_LIMIT]

    def search_single_source(
            self,
            source: str,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            target_episode_air_dates: Optional[Dict[int, str]] = None,
            subscribe: Any = None,
            apply_platform_rules: bool = True,
            force_refresh: bool = False,
            result_limit: Optional[int] = None,
            raise_errors: bool = False,
    ) -> List[Dict]:
        source = str(source or "").strip().lower()
        if self._stop_requested():
            return []
        if source in {"mikan", "animegarden"} and not is_anime_media(mediainfo):
            logger.debug(
                f"[{self._search_label(mediainfo, media_type, season)}][{source.upper()}] 媒体非动漫类型，跳过检索")
            return []
        try:
            provider = self._search_registry.get(source)
        except KeyError:
            search_label = self._search_label(mediainfo, media_type, season)
            logger.warning(f"[{search_label}][{source.upper()}] 未知的搜索源")
            return []
        cache_key = self._search_cache_key(
            source, mediainfo, media_type, season, target_episodes, subscribe
        )
        search_label = self._search_label(mediainfo, media_type, season)
        results = (
            self._get_cached_results(cache_key, source, search_label)
            if (provider.policy.cacheable and not force_refresh) else None
        )
        is_anime_source = source in {"mikan", "animegarden"}
        is_list_mode = not apply_platform_rules
        actual_result_limit = None if (is_anime_source and is_list_mode) else result_limit
        if results is not None:
            if actual_result_limit is not None and len(results) > actual_result_limit:
                results = results[:actual_result_limit]
            return self._prepare_source_results(
                results,
                source,
                mediainfo,
                media_type,
                subscribe,
                season,
                target_episodes,
                apply_platform_rules,
            )

        # 熔断前置保护检查
        if self._search_circuit_breaker_enabled and not SEARCH_CIRCUIT_BREAKER.can_execute(source):
            state = SEARCH_CIRCUIT_BREAKER.get_state(source)
            remaining = SEARCH_CIRCUIT_BREAKER.get_cooldown_remaining(source)
            logger.debug(
                f"⚡ [{search_label}][{source.upper()}] 渠道已触发熔断保护({state.value})，跳过检索 (冷却剩余 {remaining:.1f}s)"
            )
            if raise_errors:
                raise RuntimeError(f"熔断保护({state.value})")
            return []

        external_started = time.monotonic()
        try:
            results = self._run_source_search(
                source,
                mediainfo,
                media_type,
                season,
                target_episodes,
                target_episode_air_dates,
                subscribe,
                resource_list_mode=is_list_mode,
                result_limit=actual_result_limit,
            )
        except RequestGateCooldown as error:
            logger.warning(f"[{search_label}][{source.upper()}] 渠道风控冷却中，快速跳过：{error}")
            if self._search_circuit_breaker_enabled:
                SEARCH_CIRCUIT_BREAKER.record_failure(source, str(error))
            if raise_errors:
                raise error
            return []
        except Exception as error:
            logger.warning(
                f"[{search_label}][{source.upper()}] 外部查询抛出异常：{error}",
                exc_info=True,
            )
            if self._search_circuit_breaker_enabled:
                SEARCH_CIRCUIT_BREAKER.record_failure(source, str(error))
            if raise_errors:
                raise error
            return []
        finally:
            self._record_search_metric(source, "external_calls")
            self._record_search_metric(
                source,
                "external_elapsed_ms",
                int((time.monotonic() - external_started) * 1000),
            )
        if self._stop_requested():
            return []
        if results is None:
            if self._search_circuit_breaker_enabled:
                SEARCH_CIRCUIT_BREAKER.record_failure(source, "查询返回空异常")
            if raise_errors:
                raise RuntimeError("查询返回空异常")
            return []

        if self._search_circuit_breaker_enabled:
            SEARCH_CIRCUIT_BREAKER.record_success(source)

        label = f"[{search_label}][{source.upper()}]"
        if provider.policy.cacheable:
            self._set_cached_results(cache_key, label, results, source=source)
        return self._prepare_source_results(
            results,
            source,
            mediainfo,
            media_type,
            subscribe,
            season,
            target_episodes,
            apply_platform_rules,
        )

    def search_sources(
            self,
            sources: List[str],
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            target_episode_air_dates: Optional[Dict[int, str]] = None,
            subscribe: Any = None,
            apply_platform_rules: bool = True,
            force_refresh: bool = False,
            result_limit: Optional[int] = None,
            progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, List[Dict]]:
        """并发查询相互独立的来源；各来源内部仍遵守限流与熔断，实时反馈进度与渠道详情。"""
        ordered_sources = list(dict.fromkeys(sources or []))
        search_label = self._search_label(mediainfo, media_type, season)
        results: Dict[str, List[Dict]] = {source: [] for source in ordered_sources}
        if not ordered_sources:
            return results

        channels_lock = threading.Lock()
        channels_state = {
            source: {
                "key": source,
                "name": self.get_source_display_name(source),
                "status": "pending",  # pending, searching, success, failed, timeout, circuit_break, skipped
                "count": 0,
                "elapsed_ms": 0,
                "error": "",
                "started_at": 0,
            }
            for source in ordered_sources
        }

        # 检查是否有熔断保护渠道
        for source in ordered_sources:
            if self._search_circuit_breaker_enabled and not SEARCH_CIRCUIT_BREAKER.can_execute(source):
                rem = SEARCH_CIRCUIT_BREAKER.get_cooldown_remaining(source)
                channels_state[source]["status"] = "circuit_break"
                channels_state[source]["error"] = f"熔断冷却中({rem:.0f}s)"

        def generate_search_summary(active: bool = True) -> Tuple[str, str]:
            completed_channels = [
                c for c in channels_state.values()
                if c["status"] in {"success", "failed", "timeout", "circuit_break", "skipped"}
            ]
            searching_channels = [
                c for c in channels_state.values()
                if c["status"] == "searching"
            ]
            total = len(ordered_sources)
            completed = len(completed_channels)
            total_results = sum(c["count"] for c in channels_state.values())

            parts = []
            for c in channels_state.values():
                name = c["name"]
                status = c["status"]
                if status == "success":
                    parts.append(f"{name}:{c['count']}")
                elif status == "failed":
                    parts.append(f"{name}:失败")
                elif status == "timeout":
                    parts.append(f"{name}:超时")
                elif status == "circuit_break":
                    parts.append(f"{name}:熔断")
                elif status == "skipped":
                    parts.append(f"{name}:跳过")

            channel_brief = " ".join(parts)
            if active:
                phase_text = f"搜索候选资源 ({completed}/{total})"
            else:
                if total_results > 0:
                    phase_text = f"搜索完成 · 找到 {total_results} 条候选"
                else:
                    phase_text = "未找到候选资源"

            return phase_text, channel_brief

        def emit_progress(active: bool = True):
            with channels_lock:
                now_mono = time.monotonic()
                for s_name, c_data in channels_state.items():
                    if c_data["status"] == "searching":
                        st = source_started_at.get(s_name)
                        if st:
                            c_data["elapsed_ms"] = int((now_mono - st) * 1000)
                phase_text, brief = generate_search_summary(active=active)
                completed_count = sum(
                    1 for c in channels_state.values()
                    if c["status"] in {"success", "failed", "timeout", "circuit_break", "skipped"}
                )
                total_results = sum(c["count"] for c in channels_state.values())
                channels_copy = copy.deepcopy(list(channels_state.values()))

            progress_data = {
                "active": active,
                "total": len(ordered_sources),
                "completed": completed_count,
                "total_results": total_results,
                "summary": phase_text,
                "brief": brief,
                "channels": channels_copy,
            }
            if progress_callback:
                try:
                    progress_callback(progress_data)
                except Exception as cb_err:
                    logger.debug(f"搜索进度回调执行异常: {cb_err}")
            elif subscribe is not None:
                self._dispatch_default_search_progress(subscribe, progress_data)

        # 触发初始进度
        emit_progress(active=True)

        workers = min(max(1, self._search_concurrency), len(ordered_sources))
        executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="cloudsubscribe-search",
        )
        stopped = False
        futures = {}
        source_started_at: Dict[str, float] = {}
        source_started_lock = threading.Lock()
        abandoned_sources = set()

        def run_source(source: str) -> List[Dict]:
            started_at = time.monotonic()
            with source_started_lock:
                source_started_at[source] = started_at
            with channels_lock:
                if channels_state[source]["status"] != "circuit_break":
                    channels_state[source]["status"] = "searching"
                    channels_state[source]["started_at"] = time.time()
            emit_progress(active=True)
            return self.search_single_source(
                source,
                mediainfo,
                media_type,
                season,
                target_episodes,
                target_episode_air_dates,
                subscribe,
                apply_platform_rules,
                force_refresh,
                result_limit,
                raise_errors=True,
            )

        try:
            for source in ordered_sources:
                future = executor.submit(run_source, source)
                futures[future] = source

            pending = set(futures)
            while pending:
                if self._stop_requested():
                    stopped = True
                    break

                now = time.monotonic()
                with source_started_lock:
                    started_snapshot = dict(source_started_at)
                timed_out_futures = [
                    future for future in pending
                    if (
                            started_snapshot.get(futures[future]) is not None
                            and now - started_snapshot[futures[future]]
                            > self._get_source_search_timeout(futures[future])
                    )
                ]
                for f in timed_out_futures:
                    source = futures[f]
                    abandoned_sources.add(source)
                    elapsed = int((now - started_snapshot.get(source, now)) * 1000)
                    with channels_lock:
                        channels_state[source]["status"] = "timeout"
                        channels_state[source]["elapsed_ms"] = elapsed
                        channels_state[source]["error"] = "响应超时"
                    logger.debug(
                        f"⏰ [{search_label}] 搜索渠道 {source.upper()} 响应超时，已主动停止等待"
                    )
                    if self._search_circuit_breaker_enabled:
                        limit = self._get_source_search_timeout(source)
                        SEARCH_CIRCUIT_BREAKER.record_failure(
                            source, f"单次搜索超时(>{limit:.1f}s)"
                        )
                    f.cancel()
                    pending.discard(f)

                if timed_out_futures:
                    emit_progress(active=bool(pending))

                if not pending:
                    break

                active_remaining = [
                    max(
                        0.05,
                        self._get_source_search_timeout(futures[future])
                        - (now - started_snapshot[futures[future]]),
                    )
                    for future in pending
                    if started_snapshot.get(futures[future]) is not None
                ]
                wait_step = min(0.3, min(active_remaining, default=0.3))

                done, pending = wait(pending, timeout=wait_step, return_when=FIRST_COMPLETED)
                for f in done:
                    source = futures[f]
                    elapsed = int((time.monotonic() - started_snapshot.get(source, time.monotonic())) * 1000)
                    try:
                        res = f.result() or []
                        results[source] = res
                        with channels_lock:
                            channels_state[source]["status"] = "success"
                            channels_state[source]["count"] = len(res)
                            channels_state[source]["elapsed_ms"] = elapsed
                    except RequestGateCooldown as error:
                        with channels_lock:
                            channels_state[source]["status"] = "failed"
                            channels_state[source]["elapsed_ms"] = elapsed
                            channels_state[source]["error"] = f"风控冷却: {error}"
                    except Exception as error:
                        err_msg = str(error)
                        status = "circuit_break" if "熔断" in err_msg else "failed"
                        with channels_lock:
                            channels_state[source]["status"] = status
                            channels_state[source]["elapsed_ms"] = elapsed
                            channels_state[source]["error"] = err_msg
                if done:
                    emit_progress(active=bool(pending))
        finally:
            if pending or stopped:
                for f in pending:
                    source = futures.get(f)
                    if source:
                        abandoned_sources.add(source)
                        with channels_lock:
                            if channels_state[source]["status"] in {"pending", "searching"}:
                                channels_state[source]["status"] = "skipped" if stopped else "failed"
                    f.cancel()
                if abandoned_sources:
                    logger.info(
                        f"⏹️ [{search_label}] 搜索流程结束，已停止等待未完成的渠道：{', '.join(sorted(s.upper() for s in abandoned_sources))}"
                    )
            executor.shutdown(wait=False, cancel_futures=True)

        with channels_lock:
            for s in ordered_sources:
                if channels_state[s]["status"] in {"pending", "searching"}:
                    channels_state[s]["status"] = "skipped" if stopped else "failed"
        emit_progress(active=False)

        if not stopped:
            logger.debug(
                f"[{search_label}] 搜索源查询完成："
                + " / ".join(
                    f"{source.upper()}={len(results.get(source) or [])}"
                    for source in ordered_sources
                )
            )
        return results

    @staticmethod
    def _search_label(
            mediainfo: MediaInfo, media_type: MediaType, season: Optional[int] = None
    ) -> str:
        return format_search_label(mediainfo, media_type, season)

    @staticmethod
    def _resource_timestamp(value: Any) -> float:
        text = str(value or "").strip()
        if not text:
            return 0
        if text.isdigit():
            timestamp = float(text)
            return timestamp / 1000 if timestamp > 10_000_000_000 else timestamp
        try:
            parsed = datetime.fromisoformat(text.replace("/", "-").replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return 0

    @staticmethod
    def _resource_unlock_points(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _resource_type(resource: Dict[str, Any]) -> str:
        """读取内部规范化类型；pan_type 仅用于尚未规范化的外部来源。"""
        return normalize_resource_type(
            resource.get("resource_type") or resource.get("pan_type")
        )

    @classmethod
    def _resource_availability_order(cls, resource: Dict[str, Any]) -> int:
        """直链或已解锁优先，其次免费访问，最后才是积分解锁。"""
        if str(resource.get("url") or "").strip() or resource.get("is_unlocked") is True:
            return 0
        if (
                resource.get("is_free") is True
                or not resource.get("need_unlock")
        ):
            return 1
        return 2

    @staticmethod
    def _resource_preview_episode_set(
            resource: Dict[str, Any], season: Optional[int]
    ) -> Optional[set]:
        preview = resource.get("preview_episodes")
        if not preview:
            return None
        if not isinstance(preview, dict):
            return None
        if season is None:
            values = [
                episode
                for episodes in preview.values()
                for episode in (episodes or [])
            ]
        else:
            season_key = str(int(season))
            if season_key not in preview:
                return set()
            values = preview.get(season_key) or []
        return positive_ints(values)

    @classmethod
    def _resource_target_coverage(
            cls,
            resource: Dict[str, Any],
            season: Optional[int],
            targets: set,
    ) -> tuple:
        if not targets:
            return 0, 0
        preview = cls._resource_preview_episode_set(resource, season)
        if preview is None:
            return 2, 0
        covered = targets & preview
        if not covered:
            return 3, 0
        if covered == targets:
            return 0, -len(covered)
        return 1, -len(covered)

    def _resource_type_order(self, resource: Dict[str, Any]) -> int:
        """按配置的资源类型优先级排序。"""
        return self._resource_type_order_map.get(
            self._resource_type(resource), len(self._resource_type_order_config)
        )

    def _resource_pack_priority(
            self, resource: Dict[str, Any], season: Optional[int]
    ) -> int:
        """动漫完结合集优先：合集返回 -1，非合集返回 0。"""
        if not getattr(self, "_anime_pack_preferred", True):
            return 0
        if resource.get("is_pack"):
            return -1
        title = str(resource.get("title") or resource.get("name") or "")
        if re.search(r"合集|全集|全\s*\d+\s*[话話集]|\bComplete\b|\bPack\b|\[0*\d+\s*[-~～–—至到]\s*0*\d+[^\]]*\]",
                     title, re.I):
            return -1
        preview = self._resource_preview_episode_set(resource, season)
        if preview and len(preview) > 1:
            return -1
        return 0

    def _resource_sort_key(
            self, resource: Dict[str, Any], season: Optional[int], targets: set
    ) -> tuple:
        return (
            self._resource_type_order(resource),
            self._resource_pack_priority(resource, season),
            self._resource_availability_order(resource),
            resource.get("is_official") is not True,
            -int(resource.get("fansub_priority") or 0),
            *self._resource_target_coverage(resource, season, targets),
            self._resource_unlock_points(resource.get("unlock_points")),
            -int(resource.get("platform_priority") or 0),
            -self._resource_timestamp(resource.get("update_time")),
        )

    def _prefilter_resource_order(
            self,
            resources: List[Dict],
            season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            log_prefix: str = "",
            filter_unsupported_types: bool = True,
    ) -> List[Dict]:
        """按类型、可用性、集数覆盖和积分筛选排序。"""
        targets = positive_ints(target_episodes)
        prepared = []
        unsupported_type_count = 0
        uncovered_count = 0
        for item in resources:
            resource_type = self._resource_type(item)
            type_order = self._resource_type_order_map.get(resource_type)
            if type_order is None:
                if filter_unsupported_types:
                    unsupported_type_count += 1
                    continue
                type_order = 999
            coverage = self._resource_target_coverage(item, season, targets)
            if filter_unsupported_types and coverage[0] >= 3:
                uncovered_count += 1
                continue
            sort_key = (
                type_order,
                self._resource_pack_priority(item, season),
                self._resource_availability_order(item),
                item.get("is_official") is not True,
                -int(item.get("fansub_priority") or 0),
                *coverage,
                self._resource_unlock_points(item.get("unlock_points")),
                -int(item.get("platform_priority") or 0),
                -self._resource_timestamp(item.get("update_time")),
            )
            prepared.append((sort_key, item))
        prepared.sort(key=lambda pair: pair[0])
        if log_prefix and (unsupported_type_count or uncovered_count):
            details = []
            if unsupported_type_count:
                details.append(f"类型不支持={unsupported_type_count}")
            if uncovered_count:
                details.append(f"明确未覆盖目标集数={uncovered_count}")
            logger.debug(
                f"{log_prefix} 搜索候选预过滤：{len(resources)} -> {len(prepared)}，"
                + "，".join(details)
            )
        return [item for _, item in prepared]

    def _hdhive_update_sort_key(self, resource: Dict[str, Any]) -> tuple:
        """HDHive 最新更新时间优先，其余规则作为稳定的次级顺序。"""
        return (-self._resource_timestamp(resource.get("update_time")),)

    def search_resources(
            self,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int] = None,
            subscribe: Any = None,
    ) -> List[Dict]:
        """
        统一的资源搜索方法，支持电影和电视剧
        按优先级尝试所有启用的搜索源，第一个有结果的就返回
        搜索优先级按已启用来源和用户配置确定

        注意：此方法主要供电影订阅使用。电视剧订阅使用 search_single_source 进行逐源搜索。

        :param mediainfo: 媒体信息
        :param media_type: 媒体类型（MOVIE 或 TV）
        :param season: 季号（电视剧必需）
        :return: 当前同步链可处理的网盘资源列表
        """
        sources = self.get_enabled_sources()
        search_label = self._search_label(mediainfo, media_type, season)
        if not self.source_concurrency_enabled:
            for source_index, source in enumerate(sources):
                results = self.search_single_source(
                    source=source,
                    mediainfo=mediainfo,
                    media_type=media_type,
                    season=season,
                    subscribe=subscribe,
                )
                if results:
                    return results
                remaining = sources[source_index + 1:]
                if remaining:
                    logger.debug(
                        f"[{search_label}][{source.upper()}] 未找到资源，"
                        f"将回退到 "
                        f"{'/'.join(item.capitalize() for item in remaining)} 搜索"
                    )
            return []

        source_results = self.search_sources(
            sources=sources,
            mediainfo=mediainfo,
            media_type=media_type,
            season=season,
            subscribe=subscribe,
        )
        for source in sources:
            results = source_results.get(source) or []
            if results:
                logger.debug(
                    f"[{search_label}][{source.upper()}] 并发搜索完成，"
                    f"按优先级采用 "
                    f"{len(results)} 个候选资源"
                )
                return results

        return []
