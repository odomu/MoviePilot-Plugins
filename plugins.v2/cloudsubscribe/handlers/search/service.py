"""
搜索处理模块
负责所有搜索相关逻辑：HDHive、Dian115、PanSou 等搜索源
"""
import copy
import hashlib
import json
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from app.core.cache import TTLCache
from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType

from .dian115 import Dian115SearchService
from .hdhive import HDHiveSearchService
from .magnets import ExternalResourceSearchService
from .sources import PanSouSearchService
from ...core import get_component, resolve_component
from ...search.juying import JuyingResourceService
from ...search.matching import positive_ints, unique_texts

_COMPONENT_TYPES = (
    HDHiveSearchService,
    Dian115SearchService,
    ExternalResourceSearchService,
    PanSouSearchService,
)


class SearchHandler:
    """搜索处理器"""

    _HDHIVE_SEARCH_CACHE_VERSION = 1
    _DIAN115_SEARCH_CACHE_VERSION = 1
    _SUPPORTED_SOURCES = frozenset({
        "hdhive", "dian115", "pansou", "seedhub", "butailing", "juying",
    })

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
            pansou_client,
            hdhive_client,
            seedhub_client=None,
            butailing_client=None,
            juying_client=None,
            pansou_enabled: bool = False,
            hdhive_enabled: bool = False,
            dian115_enabled: bool = False,
            seedhub_enabled: bool = False,
            butailing_enabled: bool = False,
            juying_enabled: bool = False,
            hdhive_username: str = "",
            hdhive_password: str = "",
            hdhive_query_mode: str = "api",
            hdhive_auto_unlock: bool = False,
            hdhive_max_unlock_points: int = 50,
            hdhive_max_points_per_sub: int = 20,
            dian115_email: str = "",
            dian115_password: str = "",
            dian115_auto_unlock: bool = False,
            dian115_max_unlock_points: int = 50,
            dian115_max_points_per_sub: int = 20,
            pansou_channels: Any = None,
            pansou_plugins: Any = None,
            pansou_cloud_types: Any = None,
            pansou_filter_include: Any = None,
            pansou_filter_exclude: Any = None,
            resource_type_order: Optional[List[str]] = None,
            pansou_concurrency: Optional[int] = None,
            pansou_result_limit: int = 10,
            pansou_refresh: bool = True,
            pansou_timeout: int = 30,
            seedhub_result_limit: int = 20,
            butailing_result_limit: int = 20,
            juying_result_limit: int = 5,
            search_source_order: Optional[List[str]] = None,
            search_cache_enabled: bool = True,
            search_cache_ttl_minutes: int = 30,
            search_concurrency: int = 2,
            hdhive_candidate_limit: int = 4,
            hdhive_request_interval: float = 2.0,
            dian115_candidate_limit: int = 4,
            dian115_request_interval: float = 1.0,
            hdhive_torrentclaw_enabled: bool = False,
            hdhive_torrentclaw_subtitle_languages: Any = None,
            enable_cloud_upgrade: bool = False,
            upgrade_subscribe_ids: Optional[List[int]] = None,
            should_stop: Any = None,
    ):
        """
        初始化搜索处理器

        :param pansou_client: PanSou 客户端实例
        :param hdhive_client: HDHive OpenAPI 客户端实例（API 模式使用）
        :param pansou_enabled: 是否启用 PanSou
        :param hdhive_enabled: 是否启用 HDHive
        :param hdhive_username: HDHive 用户名
        :param hdhive_password: HDHive 密码
        :param hdhive_query_mode: HDHive 查询模式
        :param hdhive_auto_unlock: 是否自动解锁 HDHive 资源
        :param pansou_channels: PanSou 搜索频道
        :param search_source_order: 自定义搜索源优先级列表，如 ["pansou", "hdhive"]
        """
        self._pansou_client = pansou_client
        self._hdhive_client = hdhive_client
        self._seedhub_client = seedhub_client
        self._butailing_client = butailing_client
        self._juying_client = juying_client
        self._juying_resources = (
            JuyingResourceService(juying_client) if juying_client else None
        )
        self._pansou_enabled = pansou_enabled
        self._hdhive_enabled = hdhive_enabled
        self._dian115_enabled = bool(dian115_enabled)
        self._seedhub_enabled = bool(seedhub_enabled)
        self._butailing_enabled = bool(butailing_enabled)
        self._juying_enabled = bool(juying_enabled)
        self._hdhive_username = hdhive_username
        self._hdhive_password = hdhive_password
        self._hdhive_query_mode = str(hdhive_query_mode or "api")
        if self._hdhive_query_mode not in {"api", "web"}:
            self._hdhive_query_mode = (
                "web" if hdhive_username and hdhive_password else "api"
            )
        self._hdhive_auto_unlock = hdhive_auto_unlock
        self._hdhive_web_client = None
        self._hdhive_web_resources = None
        self._hdhive_web_lock = threading.RLock()
        self._dian115_email = str(dian115_email or "").strip()
        self._dian115_password = str(dian115_password or "").strip()
        self._dian115_auto_unlock = bool(dian115_auto_unlock)
        self._dian115_max_unlock_points = max(
            0, int(dian115_max_unlock_points or 0)
        )
        self._dian115_max_points_per_sub = max(
            0, int(dian115_max_points_per_sub or 0)
        )
        self._dian115_client = None
        self._dian115_resources = None
        self._dian115_client_lock = threading.RLock()
        self._dian115_budget_lock = threading.RLock()
        self._dian115_budget_context = threading.local()
        self._dian115_current_spent_points = 0
        self._dian115_sub_spent_points = 0
        self._dian115_current_sub_key = ""
        self._dian115_get_data_func = None
        self._dian115_save_data_func = None
        self._budget_lock = threading.RLock()
        self._budget_context = threading.local()
        self._hdhive_max_unlock_points = hdhive_max_unlock_points
        self._hdhive_max_points_per_sub = hdhive_max_points_per_sub
        self._current_spent_points = 0
        self._sub_spent_points = 0
        self._current_sub_key = ""
        self._pansou_channels = self._normalize_pansou_values(pansou_channels)
        self._pansou_plugins = self._normalize_pansou_values(pansou_plugins)
        self._pansou_cloud_types = [
            value.lower() for value in self._normalize_pansou_values(
                pansou_cloud_types
            )
        ]
        self._pansou_filter = {
            "include": self._normalize_pansou_values(pansou_filter_include),
            "exclude": self._normalize_pansou_values(pansou_filter_exclude),
        }
        self._resource_type_order_config = list(
            ["115", "ed2k"]
            if resource_type_order is None else resource_type_order
        )
        try:
            self._pansou_concurrency = (
                max(1, min(int(pansou_concurrency), 100))
                if pansou_concurrency else None
            )
        except (TypeError, ValueError):
            self._pansou_concurrency = None
        self._pansou_result_limit = max(1, min(int(pansou_result_limit or 10), 100))
        self._pansou_refresh = bool(pansou_refresh)
        self._pansou_timeout = max(5, min(int(pansou_timeout or 30), 120))
        self._seedhub_result_limit = max(
            1, min(int(seedhub_result_limit or 20), 80)
        )
        self._butailing_result_limit = max(
            1, min(int(butailing_result_limit or 20), 80)
        )
        self._juying_result_limit = max(
            1, min(int(juying_result_limit or 5), 20)
        )
        self._juying_resource_types = [
            value for value in unique_texts(
                self._resource_type_order_config, str.lower
            )
            if value in JuyingResourceService.SUPPORTED_RESOURCE_TYPES
        ]
        self._search_source_order = search_source_order or []
        self._search_cache_enabled = bool(search_cache_enabled)
        self._search_cache_ttl = max(60, int(search_cache_ttl_minutes or 30) * 60)
        self._search_concurrency = max(1, min(int(search_concurrency or 1), 5))
        self._hdhive_candidate_limit = max(1, min(int(hdhive_candidate_limit or 4), 20))
        self._hdhive_request_interval = max(
            0.5, min(float(hdhive_request_interval or 2.0), 10.0)
        )
        self._dian115_candidate_limit = max(
            1, min(int(dian115_candidate_limit or 4), 20)
        )
        self._dian115_request_interval = max(
            0.2, min(float(dian115_request_interval or 1.0), 10.0)
        )
        self._hdhive_torrentclaw_enabled = bool(
            hdhive_torrentclaw_enabled
            and "magnet" in self._resource_type_order_config
        )
        raw_subtitle_languages = hdhive_torrentclaw_subtitle_languages or ["zh"]
        if isinstance(raw_subtitle_languages, str):
            raw_subtitle_languages = re.split(r"[,，\s]+", raw_subtitle_languages)
        self._hdhive_torrentclaw_subtitle_languages = unique_texts(
            raw_subtitle_languages,
            lambda value: value.lower().replace("_", "-"),
        )
        self._enable_cloud_upgrade = bool(enable_cloud_upgrade)
        self._upgrade_subscribe_ids = list(upgrade_subscribe_ids or [])
        self._search_cache_limit = 200
        self._search_negative_ttl = min(self._search_cache_ttl, 10 * 60)
        self._search_cache = TTLCache(
            region="cloudsubscribe:search_results",
            maxsize=self._search_cache_limit,
            ttl=self._search_cache_ttl,
        )
        self._search_metrics_lock = threading.RLock()
        self._search_metrics: Dict[str, Dict[str, int]] = {}
        self._platform_filter_lock = threading.RLock()
        self._platform_filter_module = None
        self._platform_filter_signature = ""
        self._should_stop = should_stop

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
        selected_ids = {str(value) for value in (self._upgrade_subscribe_ids or [])}
        return not selected_ids or str(getattr(subscribe, "id", "")) in selected_ids

    @property
    def _sub_spent_points(self) -> int:
        return int(getattr(self._budget_context, "sub_spent_points", 0) or 0)

    @_sub_spent_points.setter
    def _sub_spent_points(self, value: int) -> None:
        self._budget_context.sub_spent_points = int(value or 0)

    @property
    def _current_sub_key(self) -> str:
        return str(getattr(self._budget_context, "current_sub_key", "") or "")

    @_current_sub_key.setter
    def _current_sub_key(self, value: str) -> None:
        self._budget_context.current_sub_key = str(value or "")

    @property
    def _dian115_sub_spent_points(self) -> int:
        return int(getattr(self._dian115_budget_context, "sub_spent_points", 0) or 0)

    @_dian115_sub_spent_points.setter
    def _dian115_sub_spent_points(self, value: int) -> None:
        self._dian115_budget_context.sub_spent_points = int(value or 0)

    @property
    def _dian115_current_sub_key(self) -> str:
        return str(getattr(self._dian115_budget_context, "current_sub_key", "") or "")

    @_dian115_current_sub_key.setter
    def _dian115_current_sub_key(self, value: str) -> None:
        self._dian115_budget_context.current_sub_key = str(value or "")

    def _stop_requested(self) -> bool:
        try:
            return bool(self._should_stop and self._should_stop())
        except Exception as error:
            logger.warning(f"读取搜索停止状态失败：{error}")
            return False

    def get_enabled_sources(self) -> List[str]:
        """
        获取已启用且可用的搜索源列表，按优先级排序

        优先级规则：
        1. 用户配置了自定义优先级（search_source_order）时按其顺序排列；
           未出现在自定义列表中的已启用源按默认顺序追加在末尾
        2. 未配置时使用代码声明的默认顺序

        :return: 搜索源名称列表
        """
        # 按默认优先级收集已启用且可用的源
        available = []

        # HDHive
        if self._hdhive_enabled:
            if self._hdhive_query_mode == "web" and self._hdhive_username and self._hdhive_password:
                available.append("hdhive")
            elif self._hdhive_query_mode == "api" and self._hdhive_client and self._hdhive_client.is_ready:
                available.append("hdhive")

        # Dian115
        if (
                self._dian115_enabled
                and self._dian115_email
                and self._dian115_password
        ):
            available.append("dian115")

        # PanSou
        if (
                self._pansou_enabled
                and self._pansou_client
        ):
            available.append("pansou")

        # 聚影
        if (
                self._juying_enabled
                and self._juying_client
                and self._juying_client.is_configured
                and self._juying_resource_types
        ):
            available.append("juying")

        # SeedHub
        if (
                self._seedhub_enabled
                and self._seedhub_client
                and "magnet" in self._resource_type_order_config
        ):
            available.append("seedhub")

        # 不太灵
        if (
                self._butailing_enabled
                and self._butailing_client
                and "magnet" in self._resource_type_order_config
        ):
            available.append("butailing")

        # 应用用户自定义优先级
        if self._search_source_order:
            sources = [s for s in self._search_source_order if s in available]
            sources += [s for s in available if s not in sources]
            return sources

        return available

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
                or getattr(subscribe, "tmdbid", None)
                or getattr(subscribe, "tmdb_id", None)
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
            "hdhive_mode": self._hdhive_query_mode if source == "hdhive" else "",
            "hdhive_cache_version": (
                self._HDHIVE_SEARCH_CACHE_VERSION if source == "hdhive" else 0
            ),
            "hdhive_torrentclaw_subtitle_languages": (
                self._hdhive_torrentclaw_subtitle_languages
                if source == "hdhive" else []
            ),
            "hdhive_torrentclaw_enabled": (
                self._hdhive_torrentclaw_enabled if source == "hdhive" else False
            ),
            "dian115_cache_version": (
                self._DIAN115_SEARCH_CACHE_VERSION if source == "dian115" else 0
            ),
            "dian115_auto_unlock": (
                self._dian115_auto_unlock if source == "dian115" else False
            ),
            "dian115_candidate_limit": (
                self._dian115_candidate_limit if source == "dian115" else 0
            ),
            "pansou_channels": self._pansou_channels if source == "pansou" else "",
            "pansou_plugins": self._pansou_plugins if source == "pansou" else [],
            "pansou_cloud_types": self._pansou_cloud_types if source == "pansou" else [],
            "pansou_filter": self._pansou_filter if source == "pansou" else {},
            "pansou_concurrency": self._pansou_concurrency if source == "pansou" else 0,
            "pansou_result_limit": self._pansou_result_limit if source == "pansou" else 0,
            "pansou_refresh": self._pansou_refresh if source == "pansou" else False,
            "pansou_timeout": self._pansou_timeout if source == "pansou" else 0,
            "seedhub_result_limit": (
                self._seedhub_result_limit if source == "seedhub" else 0
            ),
            "butailing_result_limit": (
                self._butailing_result_limit if source == "butailing" else 0
            ),
            "juying_result_limit": (
                self._juying_result_limit if source == "juying" else 0
            ),
            "juying_resource_types": (
                self._juying_resource_types if source == "juying" else []
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
        if source == "hdhive" and not results:
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
        if source == "hdhive" and not results:
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
        active = [item for _, item in self._search_cache.items() if isinstance(item, dict)]
        positive = sum(not item.get("negative") for item in active)
        negative = len(active) - positive
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

        with self._hdhive_web_lock:
            web_count = self._clear_client_cache(self._hdhive_web_resources)
        return {
            "search_results": search_count,
            "hdhive_web": web_count,
            "hdhive_openapi": self._clear_client_cache(self._hdhive_client),
            "seedhub": self._clear_client_cache(self._seedhub_client),
            "butailing": self._clear_client_cache(self._butailing_client),
            "juying": self._clear_client_cache(self._juying_resources),
            "dian115_details": self._clear_client_cache(
                get_component(self, Dian115SearchService, "_search_components")
            ),
        }

    @staticmethod
    def _clear_client_cache(client: Any) -> int:
        if not client or not hasattr(client, "clear_cache"):
            return 0
        result = client.clear_cache()
        return sum(result.values()) if isinstance(result, dict) else int(result or 0)

    def _points_services(self):
        return tuple(
            get_component(self, service_type, "_search_components")
            for service_type in (HDHiveSearchService, Dian115SearchService)
        )

    def close(self, release_cache: bool = False) -> None:
        """释放搜索客户端。"""
        get_component(
            self,
            HDHiveSearchService,
            "_search_components",
        ).close(release_cache=release_cache)
        if self._hdhive_client and hasattr(self._hdhive_client, "close"):
            self._hdhive_client.close()
        get_component(
            self,
            Dian115SearchService,
            "_search_components",
        ).close()
        if self._juying_client:
            self._juying_client.close()

    def set_data_funcs(self, get_func, save_func) -> None:
        """为各积分搜索源注入持久化函数。"""
        for service in self._points_services():
            service.set_data_funcs(get_func, save_func)

    def reset_task_spent_points(self) -> None:
        """重置本轮同步中各积分搜索源的任务账本。"""
        for service in self._points_services():
            service.reset_task_spent_points()

    def reset_sub_spent_points(self, sub_key: str = "") -> None:
        """加载当前订阅在各积分搜索源中的历史消费。"""
        for service in self._points_services():
            service.reset_sub_spent_points(sub_key)

    def clear_sub_points(self, sub_key: str) -> None:
        """订阅完成后清理各积分搜索源的历史账本。"""
        for service in self._points_services():
            service.clear_sub_points(sub_key)

    def search_sources(
            self,
            sources: List[str],
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            target_episode_air_dates: Optional[Dict[int, str]] = None,
            subscribe: Any = None,
    ) -> Dict[str, List[Dict]]:
        """并发查询相互独立的来源；各来源内部仍遵守自己的限流和串行约束。"""
        ordered_sources = list(dict.fromkeys(sources or []))
        search_label = self._search_label(mediainfo, media_type, season)
        if len(ordered_sources) <= 1 or self._search_concurrency <= 1:
            return {
                source: self.search_single_source(
                    source=source,
                    mediainfo=mediainfo,
                    media_type=media_type,
                    season=season,
                    target_episodes=target_episodes,
                    target_episode_air_dates=target_episode_air_dates,
                    subscribe=subscribe,
                )
                for source in ordered_sources
            }

        results: Dict[str, List[Dict]] = {source: [] for source in ordered_sources}
        workers = min(self._search_concurrency, len(ordered_sources))
        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="p115-search")
        stopped = False
        try:
            futures = {
                executor.submit(
                    self.search_single_source,
                    source,
                    mediainfo,
                    media_type,
                    season,
                    target_episodes,
                    target_episode_air_dates,
                    subscribe,
                ): source
                for source in ordered_sources
            }
            pending = set(futures)
            while pending:
                if self._stop_requested():
                    stopped = True
                    for future in pending:
                        future.cancel()
                    logger.info(
                        f"⏹️ [{search_label}] 已停止等待搜索源，未开始的查询已取消"
                    )
                    break
                done, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                for future in done:
                    source = futures[future]
                    try:
                        results[source] = future.result()
                    except Exception as error:
                        logger.error(
                            f"[{search_label}] 搜索源 {source} 并发查询失败：{error}"
                        )
        finally:
            executor.shutdown(wait=not stopped, cancel_futures=stopped)
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
        title = str(getattr(mediainfo, "title", "") or "未知标题")
        year = getattr(mediainfo, "year", None)
        label = f"{title} ({year})" if year else title
        if media_type == MediaType.TV and season is not None:
            label += f" S{int(season):02d}"
        return label

    @staticmethod
    def _resource_size(value: Any) -> int:
        if isinstance(value, (int, float)):
            return max(0, int(value))
        match = re.search(r"([\d.]+)\s*(B|KB|MB|GB|TB)", str(value or ""), re.IGNORECASE)
        if not match:
            return 0
        unit = match.group(2).upper()
        return int(float(match.group(1)) * 1024 ** ("B", "KB", "MB", "GB", "TB").index(unit))

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
        return str(
            resource.get("resource_type") or resource.get("pan_type") or ""
        ).strip().lower()

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
        try:
            return self._resource_type_order_config.index(
                self._resource_type(resource)
            )
        except ValueError:
            return len(self._resource_type_order_config)

    def _resource_sort_key(
            self, resource: Dict[str, Any], season: Optional[int], targets: set
    ) -> tuple:
        return (
            self._resource_type_order(resource),
            self._resource_availability_order(resource),
            resource.get("is_official") is not True,
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
    ) -> List[Dict]:
        """按类型、可用性、HDHive 官组、集数覆盖和积分筛选排序。"""
        targets = positive_ints(target_episodes)
        resources = [
            item for item in resources
            if self._resource_type(item) in self._resource_type_order_config
               and self._resource_target_coverage(item, season, targets)[0] < 3
        ]
        return sorted(
            resources,
            key=lambda item: self._resource_sort_key(item, season, targets),
        )

    @staticmethod
    def _resource_filter_title(resource: Dict[str, Any]) -> str:
        """将搜索源的结构化发布信息还原为 MoviePilot 可识别的规则标题。"""
        video_info = resource.get("video_info") or {}
        language_values = []
        for value in (
                resource.get("languages"), resource.get("subtitle_languages")
        ):
            if isinstance(value, (list, tuple, set)):
                language_values.extend(value)
            elif value:
                language_values.append(value)
        fields = (
            resource.get("title"),
            resource.get("resolution"),
            resource.get("quality"),
            resource.get("source_type"),
            resource.get("codec"),
            resource.get("audio_codec"),
            resource.get("audio_channels"),
            resource.get("hdr_type"),
            video_info.get("hdr") if isinstance(video_info, dict) else "",
            resource.get("release_group"),
            " ".join(str(value) for value in language_values if str(value).strip()),
            resource.get("subtitle"),
            resource.get("description"),
        )
        return " ".join(dict.fromkeys(
            str(value).strip() for value in fields if str(value or "").strip()
        ))

    def _filter_by_platform_rules(
            self,
            resources: List[Dict],
            mediainfo: MediaInfo,
            subscribe: Any = None,
            season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
    ) -> List[Dict]:
        """使用 MoviePilot 平台规则组筛选并按平台优先级排序。"""
        if not resources:
            return []
        resources = self._prefilter_resource_order(
            resources, season=season, target_episodes=target_episodes
        )
        try:
            from app.schemas import TorrentInfo

            rule_groups = self._platform_rule_groups(subscribe)
            if not rule_groups:
                return list(resources)

            torrents = []
            resource_by_url = {}
            for index, resource in enumerate(resources):
                title = self._resource_filter_title(resource)
                page_url = f"https://cloudsubscribe.invalid/resource/{index}"
                torrent = TorrentInfo(
                    title=title or f"resource-{index}",
                    description=str(resource.get("description") or ""),
                    page_url=page_url,
                    size=self._resource_size(resource.get("size")),
                    labels=[],
                )
                torrents.append(torrent)
                resource_by_url[page_url] = resource

            matched = self.filter_torrents_by_rules(
                rule_groups=rule_groups,
                torrent_list=torrents,
                mediainfo=mediainfo,
            ) or []
            matched.sort(
                key=lambda item: int(getattr(item, "pri_order", 0) or 0),
                reverse=True,
            )
            filtered = []
            for item in matched:
                resource = resource_by_url.get(getattr(item, "page_url", None))
                if resource is None:
                    continue
                resource = dict(resource)
                resource["platform_priority"] = int(
                    getattr(item, "pri_order", 0) or 0
                )
                filtered.append(resource)
            targets = positive_ints(target_episodes)
            filtered.sort(
                key=lambda resource: self._resource_sort_key(
                    resource, season, targets
                )
            )
            logger.debug(
                f"平台优先级规则组筛选资源：{len(resources)} -> {len(filtered)}，"
                f"规则组：{rule_groups}"
            )
            return filtered
        except Exception as error:
            logger.error(f"平台优先级规则组筛选失败，已拒绝本批资源：{error}")
            return []

    def _platform_rule_groups(self, subscribe: Any = None) -> List[str]:
        """读取订阅指定规则组，否则使用 MoviePilot 对应的全局规则组。"""
        from app.db.systemconfig_oper import SystemConfigOper
        from app.schemas.types import SystemConfigKey

        rule_groups = list(getattr(subscribe, "filter_groups", None) or [])
        if rule_groups:
            return rule_groups
        config_key = (
            SystemConfigKey.BestVersionFilterRuleGroups
            if self._is_cloud_upgrade_subscribe(subscribe)
            else SystemConfigKey.SubscribeFilterRuleGroups
        )
        return list(SystemConfigOper().get(config_key) or [])

    def rank_file_candidates(
            self,
            files: List[Any],
            mediainfo: MediaInfo,
            subscribe: Any = None,
    ) -> List[tuple]:
        """使用 MoviePilot 规则组筛选实际文件并返回 ``(文件, pri_order)``。"""
        candidates = [item for item in files or [] if item]
        if not candidates:
            return []
        rule_groups = self._platform_rule_groups(subscribe)
        if not rule_groups:
            return sorted(
                ((item, 0) for item in candidates),
                key=lambda pair: self._resource_size(pair[0].get("size")),
                reverse=True,
            )

        from app.schemas import TorrentInfo

        torrents = []
        by_url = {}
        for index, item in enumerate(candidates):
            page_url = f"https://cloudsubscribe.invalid/file/{index}"
            torrent = TorrentInfo(
                title=str(item.get("name") or f"file-{index}"),
                description="",
                page_url=page_url,
                size=self._resource_size(item.get("size")),
                labels=[],
            )
            torrents.append(torrent)
            by_url[page_url] = item
        try:
            matched = self.filter_torrents_by_rules(
                rule_groups=rule_groups,
                torrent_list=torrents,
                mediainfo=mediainfo,
            )
        except Exception as error:
            logger.error(f"平台优先级规则匹配文件失败：{error}")
            return []
        ranked = [
            (by_url[item.page_url], int(item.pri_order or 0))
            for item in matched or []
            if getattr(item, "page_url", None) in by_url
        ]
        ranked.sort(
            key=lambda pair: (pair[1], self._resource_size(pair[0].get("size"))),
            reverse=True,
        )
        return ranked

    def select_file_candidate(
            self,
            files: List[Any],
            mediainfo: MediaInfo,
            subscribe: Any = None,
    ) -> tuple:
        ranked = self.rank_file_candidates(files, mediainfo, subscribe)
        return ranked[0] if ranked else (None, 0)

    def filter_torrents_by_rules(
            self,
            rule_groups: List[str],
            torrent_list: List[Any],
            mediainfo: MediaInfo,
    ) -> List[Any]:
        """复用MoviePilot过滤模块，避免每次搜索或评分重复加载规则集。"""
        with self._platform_filter_lock:
            signature = self._platform_rules_signature()
            if (
                    self._platform_filter_module is None
                    or signature != self._platform_filter_signature
            ):
                from app.modules.filter import FilterModule
                self._platform_filter_module = FilterModule()
                self._platform_filter_module.init_module()
                self._platform_filter_signature = signature
                logger.debug("MoviePilot平台过滤规则已同步到 CloudSubscribe")
            return self._platform_filter_module.filter_torrents(
                rule_groups=rule_groups,
                torrent_list=torrent_list,
                mediainfo=mediainfo,
            ) or []

    @staticmethod
    def _platform_rules_signature() -> str:
        """检测平台规则配置变化，避免长期复用已经过期的 FilterModule。"""
        from app.db.systemconfig_oper import SystemConfigOper
        from app.schemas.types import SystemConfigKey

        oper = SystemConfigOper()
        payload = {
            "groups": oper.get(SystemConfigKey.UserFilterRuleGroups) or [],
            "rules": oper.get(SystemConfigKey.CustomFilterRules) or [],
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha1(serialized.encode("utf-8")).hexdigest()

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
        :return: 115网盘资源列表
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

    def _run_source_search(
            self,
            source: str,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            target_episode_air_dates: Optional[Dict[int, str]] = None,
            subscribe: Any = None,
            test_mode: bool = False,
    ) -> Optional[List[Dict]]:
        """统一搜索源分发。"""
        if source == "hdhive":
            return self._search_hdhive(
                mediainfo,
                media_type,
                season,
                target_episodes=target_episodes,
                target_episode_air_dates=target_episode_air_dates,
                subscribe=subscribe,
                test_mode=test_mode,
            )
        if source == "dian115":
            return self._search_dian115(
                mediainfo,
                media_type,
                season,
                target_episodes=target_episodes,
                subscribe=subscribe,
                test_mode=test_mode,
            )
        if source == "pansou":
            if media_type == MediaType.MOVIE:
                return self._search_pansou_movie(mediainfo, test_mode=test_mode)
            return self._search_pansou_tv(
                mediainfo, season, test_mode=test_mode
            )
        if source == "seedhub":
            return self._search_seedhub(
                mediainfo,
                media_type,
                season,
                raise_errors=test_mode,
                test_mode=test_mode,
            )
        if source == "butailing":
            return self._search_butailing(
                mediainfo,
                media_type,
                season,
                subscribe=subscribe,
                raise_errors=test_mode,
                test_mode=test_mode,
            )
        if source == "juying":
            return self._search_juying(
                mediainfo,
                media_type,
                season,
                raise_errors=test_mode,
                test_mode=test_mode,
            )
        raise ValueError("不支持的搜索渠道")

    def _prepare_source_results(
            self,
            results: List[Dict],
            source: str,
            mediainfo: MediaInfo,
            subscribe: Any,
            season: Optional[int],
            target_episodes: Optional[List[int]],
            apply_platform_rules: bool,
    ) -> List[Dict]:
        for result in results:
            result.setdefault("source", source)
        ordered = self._prefilter_resource_order(
            results, season=season, target_episodes=target_episodes
        )
        if source == "hdhive" or not apply_platform_rules:
            return ordered
        return self._filter_by_platform_rules(
            ordered,
            mediainfo,
            subscribe,
            season=season,
            target_episodes=target_episodes,
        )

    def test_source(
            self,
            source: str,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int] = None,
    ) -> List[Dict]:
        """强制刷新单个来源，仅返回候选，不进入转存或离线任务。"""
        source = str(source or "").strip().lower()
        if source not in self._SUPPORTED_SOURCES:
            raise ValueError("不支持的搜索渠道")
        cache_key = self._search_cache_key(
            source, mediainfo, media_type, season, None, None
        )
        try:
            self._search_cache.pop(cache_key)
        except KeyError:
            pass
        client = {
            "seedhub": self._seedhub_client,
            "butailing": self._butailing_client,
            "juying": self._juying_resources,
        }.get(source)
        if client:
            client.clear_cache()
        results = self._run_source_search(
            source, mediainfo, media_type, season, test_mode=True
        ) or []
        for result in results:
            result.setdefault("source", source)
        return list(results)

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
    ) -> List[Dict]:
        """
        使用指定的单一搜索源查询资源

        :param source: 搜索源名称（如 "hdhive"、"dian115"、"pansou"）
        :param mediainfo: 媒体信息
        :param media_type: 媒体类型
        :param season: 季号（电视剧时使用）
        :return: 115网盘资源列表
        """
        source = str(source or "").strip().lower()
        if self._stop_requested():
            return []
        if source not in self._SUPPORTED_SOURCES:
            search_label = self._search_label(mediainfo, media_type, season)
            logger.warning(f"[{search_label}][{source.upper()}] 未知的搜索源")
            return []
        cache_key = self._search_cache_key(
            source, mediainfo, media_type, season, target_episodes, subscribe
        )
        search_label = self._search_label(mediainfo, media_type, season)
        use_cache = source != "juying"
        results = (
            self._get_cached_results(cache_key, source, search_label)
            if use_cache else None
        )
        if results is not None:
            return self._prepare_source_results(
                results,
                source,
                mediainfo,
                subscribe,
                season,
                target_episodes,
                apply_platform_rules,
            )

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
            )
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
            return []
        label = f"[{search_label}][{source.upper()}]"
        if use_cache:
            self._set_cached_results(cache_key, label, results, source=source)
        return self._prepare_source_results(
            results,
            source,
            mediainfo,
            subscribe,
            season,
            target_episodes,
            apply_platform_rules,
        )
