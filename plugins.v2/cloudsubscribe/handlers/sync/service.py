"""
同步处理模块
负责核心的同步逻辑：处理电影订阅、处理电视剧订阅
"""
import copy
import datetime
import hashlib
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from typing import List, Dict, Any, Set, Optional, Callable, Tuple, Mapping

from app.core.config import global_vars
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper
from app.helper.directory import DirectoryHelper
from app.log import logger
from app.modules.filemanager import FileManagerModule
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.types import MediaType, NotificationType

from .baseline import UpgradeBaselineService
from .history import HistoryService
from .movie import MovieSyncProcessor
from .offline import OfflineTaskService
from .pt_upgrade import PtUpgradeService
from .rule_scoring import UpgradeRuleScoringService
from .television import TelevisionSyncProcessor
from .upgrade import UpgradeService
from ..notification import EmbyMediaResolver, MediaServerNotifier
from ..search import SearchHandler
from ..subscription import SubscribeHandler
from ...core import (
    CloudDriveCapability,
    CloudFile,
    CloudDriveProvider,
    get_component,
    resolve_component,
    MediaScraper,
)
from ...utils import FileMatcher, MediaFileParser, StrmGenerator, StrmTemplateError

_COMPONENT_TYPES = (
    MovieSyncProcessor,
    TelevisionSyncProcessor,
    HistoryService,
    OfflineTaskService,
    UpgradeBaselineService,
    UpgradeRuleScoringService,
    UpgradeService,
    PtUpgradeService,
)


class SyncHandler:
    """同步处理器"""

    _OFFLINE_PENDING_KEY = "pending_offline_strm_v1"
    _OFFLINE_CHECK_DELAYS = (10, 20, 40, 60, 120, 300)
    _OFFLINE_TIMEOUT = 30 * 60
    _FILE_FINALIZE_TIMEOUT = 24 * 60 * 60
    _OFFLINE_MONITOR_LEASE_SECONDS = 15 * 60
    _MEDIA_RECOGNITION_CACHE_LIMIT = 256
    _PLATFORM_ROOT_CACHE_LIMIT = 256
    _RESOURCE_SEASON_DIR_CACHE_LIMIT = 256
    _SUBSCRIBE_DEFER_CACHE_LIMIT = 512
    _SUBSCRIBE_CALENDAR_CACHE_LIMIT = 512
    _CLOUD_MEDIA_ROOT = "/"
    _OFFLINE_RESOURCE_URL_RE = re.compile(
        r"ed2k://\|file\|[^|\r\n]+\|\d+\|[0-9A-Fa-f]{32}"
        r"(?:\|(?:h|p)=[^|\r\n]+)*\|/|magnet:\?[^\s\r\n]+",
        re.IGNORECASE,
    )

    def _get_component(self, component_type):
        return get_component(self, component_type, "_handler_components")

    def __getattr__(self, name):
        return resolve_component(self, _COMPONENT_TYPES, name, "_handler_components")

    def __init__(
            self,
            cloud_drive: Optional[CloudDriveProvider],
            search_handler: SearchHandler,
            subscribe_handler: SubscribeHandler,
            chain,
            cloud_transfer_path: str,
            cloud_media_root: str = "/",
            cloud_transfer_paths: Optional[Mapping[str, str]] = None,
            max_transfer_per_sync: int = 50,
            cross_transfer_enabled: bool = False,
            cross_transfer_media_types: Optional[List[str]] = None,
            cloud_drive_registry=None,
            cross_transfer_manager=None,
            batch_size: int = 20,
            batch_interval: float = 3,
            transfer_risk_cooldown: int = 1800,
            skip_other_season_dirs: bool = True,
            notify: bool = False,
            notification_type: NotificationType = NotificationType.Plugin,
            post_message_func: Callable = None,
            get_data_func: Callable = None,
            save_data_func: Callable = None,
            self_heal_interval: int = 10,
            enable_cloud_upgrade: bool = False,
            enable_pt_upgrade: bool = False,
            upgrade_mode: str = "largest",
            upgrade_subscribe_ids: Optional[List[int]] = None,
            local_resource_path: str = "",
            strm_generate_enabled: bool = True,
            nfo_scrape_enabled: bool = False,
            image_scrape_enabled: bool = False,
            strm_base_url: str = StrmGenerator.DEFAULT_BASE_URL,
            strm_url_template: str = StrmGenerator.DEFAULT_TEMPLATE,
            media_server_refresh_enabled: bool = False,
            media_servers: Optional[List[str]] = None,
            media_server_path_mappings: str = "",
            media_server_refresh_delay: int = 0,
            emby_mediainfo_enabled: bool = False,
            platform_transfer_history_enabled: bool = False,
            should_stop: Callable[[], bool] = None,
            offline_pending_changed: Callable[[int], None] = None,
            file_finalized: Callable[[List[Dict[str, Any]], int], None] = None,
            task_update: Callable[..., None] = None,
            task_context: Callable[[], Tuple[str, Any]] = None,
    ):
        """
        初始化同步处理器

        :param cloud_drive: 当前网盘提供方；各操作按能力服务分别获取
        :param search_handler: 搜索处理器
        :param subscribe_handler: 订阅处理器
        :param chain: MediaChain 实例
        :param cloud_transfer_path: 当前网盘转存暂存路径
        :param cloud_media_root: 当前网盘媒体库分类根目录
        :param cloud_transfer_paths: 各网盘提供方的转存暂存路径
        :param max_transfer_per_sync: 单次同步最大转存数量
        :param batch_size: 批量转存每批文件数
        :param skip_other_season_dirs: 跳过其他季目录
        :param notify: 是否发送通知
        :param notification_type: 消息通知类型
        :param post_message_func: 发送消息的函数
        :param get_data_func: 获取数据的函数
        :param save_data_func: 保存数据的函数
        :param self_heal_interval: 自愈检查间隔（分钟）
        :param enable_cloud_upgrade: 启用网盘洗版
        :param enable_pt_upgrade: 启用PT 整理后上传洗版
        :param upgrade_mode: 洗版文件处理模式
        :param local_resource_path: 容器内可访问的本地或挂载媒体根路径
        :param strm_generate_enabled: 转存成功后是否直接生成 STRM
        :param strm_base_url: STRM 模板中的 base_url
        :param strm_url_template: STRM 内容模板
        :param platform_transfer_history_enabled: 是否写入整理历史
        :param should_stop: 当前同步任务是否已请求停止
        :param offline_pending_changed: 待后处理任务数量变化回调
        :param file_finalized: 文件真正完成后的通知回调
        :param task_update: 订阅任务阶段更新回调
        :param task_context: 当前订阅任务标识与停止事件回调
        """
        self._cloud_drive = cloud_drive
        self._cross_transfer_enabled = bool(cross_transfer_enabled)
        self._cross_transfer_media_types = set(cross_transfer_media_types or ("movie", "tv"))
        self._cloud_drive_registry = cloud_drive_registry
        self._cross_transfer_manager = cross_transfer_manager
        self._cloud_auth = self._optional_cloud_service(
            CloudDriveCapability.AUTHENTICATION
        )
        self._cloud_account = self._optional_cloud_service(
            CloudDriveCapability.ACCOUNT
        )
        self._share_transfer = self._optional_cloud_service(
            CloudDriveCapability.SHARE_TRANSFER
        )
        self._offline_download = self._optional_cloud_service(
            CloudDriveCapability.OFFLINE_DOWNLOAD
        )
        self._cloud_directories = self._optional_cloud_service(
            CloudDriveCapability.DIRECTORY_READ
        )
        self._cloud_query = self._optional_cloud_service(
            CloudDriveCapability.FILE_QUERY
        )
        self._cloud_mutations = self._optional_cloud_service(
            CloudDriveCapability.FILE_MUTATION
        )
        self._checksum_rename = self._optional_cloud_service(
            CloudDriveCapability.CHECKSUM_RENAME
        )
        self._cloud_batch_mutations = self._optional_cloud_service(
            CloudDriveCapability.BATCH_FILE_MUTATION
        )
        self._playback_reference = self._optional_cloud_service(
            CloudDriveCapability.PLAYBACK_REFERENCE
        )
        self._offline_tasks = self._optional_cloud_service(
            CloudDriveCapability.OFFLINE_TASKS
        )
        self._cloud_upload = self._optional_cloud_service(
            CloudDriveCapability.LOCAL_UPLOAD
        )
        self._search_handler = search_handler
        self._subscribe_handler = subscribe_handler
        self._chain = chain
        self._max_transfer_per_sync = max_transfer_per_sync
        policy = cloud_drive.policy if cloud_drive else None
        configured_batch_size = max(1, int(batch_size or 1))
        self._batch_size = min(
            configured_batch_size,
            policy.max_batch_size if policy and policy.supports_batch else configured_batch_size,
        )
        self._batch_interval = max(0.0, min(float(batch_interval or 0), 60.0))
        self._transfer_risk_cooldown = max(
            60, min(int(transfer_risk_cooldown or 1800), 86400)
        )
        self._share_transfer_risk_lock = threading.Lock()
        self._share_transfer_blocked_until: Dict[str, float] = {}
        self._skip_other_season_dirs = skip_other_season_dirs
        self._notify = notify
        self._notification_type = notification_type
        self._post_message = post_message_func
        self._get_data = get_data_func
        self._save_data = save_data_func
        self._self_heal_interval = self_heal_interval
        self._enable_cloud_upgrade = enable_cloud_upgrade
        self._enable_pt_upgrade = bool(enable_pt_upgrade)
        if self._enable_pt_upgrade and not self._cloud_upload:
            logger.warning("PT洗版已启用，但当前网盘不支持本地文件上传")
        self._upgrade_subscribe_ids = list(upgrade_subscribe_ids or [])
        self._upgrade_mode = (
            str(upgrade_mode or "largest").strip().lower()
            if str(upgrade_mode or "largest").strip().lower()
               in {"coexist", "replace", "largest", "smallest"}
            else "largest"
        )
        self._local_resource_path = str(local_resource_path or "").strip()
        self._cloud_transfer_path = (
                str(cloud_transfer_path or "/").strip().rstrip("/") or "/"
        )
        self._CLOUD_MEDIA_ROOT = self._normalize_cloud_path(cloud_media_root)
        self._cloud_transfer_paths = {
            str(key).strip().lower(): self._normalize_cloud_path(value)
            for key, value in dict(cloud_transfer_paths or {}).items()
            if str(key).strip()
        }
        if self._cloud_drive:
            self._cloud_transfer_paths.setdefault(
                self._cloud_drive.key, self._cloud_transfer_path
            )
        self._strm_generate_enabled = bool(strm_generate_enabled)
        self._nfo_scrape_enabled = bool(nfo_scrape_enabled)
        self._image_scrape_enabled = bool(image_scrape_enabled)
        self._platform_transfer_history_enabled = bool(
            platform_transfer_history_enabled
        )
        self._metadata_scraper = (
            MediaScraper(
                nfo_enabled=self._nfo_scrape_enabled,
                image_enabled=self._image_scrape_enabled,
            )
            if self._nfo_scrape_enabled or self._image_scrape_enabled
            else None
        )
        if self._metadata_scraper:
            enabled_types = "、".join(
                name for enabled, name in (
                    (self._nfo_scrape_enabled, "NFO"),
                    (self._image_scrape_enabled, "图片"),
                ) if enabled
            )
            if self._local_resource_path:
                logger.info(
                    f"元数据刮削已启用：{enabled_types}，"
                    f"本地资源目录={self._local_resource_path}"
                )
            else:
                logger.warning(
                    f"元数据刮削已启用：{enabled_types}，但未配置本地资源目录，"
                    "无法生成 NFO 或图片"
                )
        self._path_mapper = StrmGenerator(
            StrmGenerator.DEFAULT_BASE_URL, StrmGenerator.DEFAULT_TEMPLATE
        )
        self._strm_generator = None
        if self._strm_generate_enabled:
            if not self._playback_reference:
                logger.error(
                    "当前网盘不支持播放引用，已停止直接生成 STRM"
                )
                self._strm_generate_enabled = False
            else:
                try:
                    self._strm_generator = StrmGenerator(
                        strm_base_url,
                        strm_url_template,
                        provider_variables=self._playback_reference.template_variables,
                    )
                except StrmTemplateError as error:
                    logger.error(f"STRM 生成配置无效，已停止直接生成：{error}")
        self._media_server_notifier = MediaServerNotifier(
            enabled=media_server_refresh_enabled,
            mediaservers=media_servers,
            path_mappings=media_server_path_mappings,
            delay_seconds=media_server_refresh_delay,
            emby_mediainfo_enabled=emby_mediainfo_enabled,
        )
        self._emby_media_resolver = EmbyMediaResolver()
        self._should_stop = should_stop
        self._offline_pending_changed = offline_pending_changed
        self._file_finalized = file_finalized
        self._task_update = task_update
        self._task_context = task_context
        self._offline_pending_lock = threading.RLock()
        self._pt_upgrade_lock = threading.RLock()
        self._pt_upgrade_active = set()
        self._platform_history_lock = threading.RLock()
        self._transfer_budget_lock = threading.RLock()
        self._transfer_budget_used = 0
        self._sync_metrics_lock = threading.RLock()
        self._sync_metrics: Dict[str, Dict[str, int]] = {}
        self._media_recognition_lock = threading.RLock()
        self._platform_media_recognition_lock = threading.Lock()
        self._media_recognition_cache: OrderedDict[Tuple[Any, ...], Any] = OrderedDict()
        self._media_recognition_inflight: Dict[Tuple[Any, ...], Future] = {}
        self._resource_season_dir_lock = threading.RLock()
        self._resource_season_dir_cache: OrderedDict[
            Tuple[Any, ...], Optional[Path]
        ] = OrderedDict()
        self._platform_root_lock = threading.RLock()
        self._platform_root_cache: OrderedDict[
            Tuple[Any, ...], Optional[Path]
        ] = OrderedDict()
        self._subscribe_defer_lock = threading.RLock()
        self._subscribe_defer_cache: OrderedDict[
            Tuple[Any, ...], Dict[str, str]
        ] = OrderedDict()
        self._subscribe_calendar_cache: OrderedDict[
            Tuple[Any, ...], Dict[str, Any]
        ] = OrderedDict()
        self._baseline_cache_lock = threading.RLock()
        self._baseline_transfer_cache: Dict[
            Tuple[Any, ...], Dict[int, List[Dict[str, Any]]]
        ] = {}
        self._baseline_plugin_cache: Dict[
            Tuple[Any, ...], Dict[int, List[Dict[str, Any]]]
        ] = {}
        self._baseline_emby_cache: Dict[
            Tuple[Any, ...], Dict[int, Dict[str, Any]]
        ] = {}

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

    @staticmethod
    def subscription_budget_key(
            subscribe: Any, media_type: Optional[MediaType] = None
    ) -> str:
        """生成普通转存和洗版共用的 HDHive 订阅积分键。"""
        resolved_type = media_type or {
            MediaType.MOVIE.value: MediaType.MOVIE,
            MediaType.TV.value: MediaType.TV,
        }.get(str(getattr(subscribe, "type", "") or ""))
        tmdb_id = str(getattr(subscribe, "tmdbid", "") or "").strip()
        identity = (
            f"tmdb_{tmdb_id}"
            if tmdb_id
            else str(getattr(subscribe, "name", "") or "").strip()
        )
        if resolved_type == MediaType.MOVIE:
            return f"{identity}_movie"
        season = max(1, int(getattr(subscribe, "season", 1) or 1))
        return f"{identity}_S{season}"

    def _optional_cloud_service(self, capability: CloudDriveCapability):
        if not self._cloud_drive or not self._cloud_drive.supports(capability):
            return None
        return self._cloud_drive.require(capability)

    def clear_runtime_cache(self) -> Dict[str, int]:
        """清空同步过程中可重建的计算缓存。"""
        with self._media_recognition_lock:
            media_recognition = len(self._media_recognition_cache)
            self._media_recognition_cache.clear()
        with self._resource_season_dir_lock:
            resource_season_dirs = len(self._resource_season_dir_cache)
            self._resource_season_dir_cache.clear()
        with self._platform_root_lock:
            platform_roots = len(self._platform_root_cache)
            self._platform_root_cache.clear()
        with self._subscribe_defer_lock:
            subscribe_defer = len(self._subscribe_defer_cache)
            subscribe_calendar = len(self._subscribe_calendar_cache)
            self._subscribe_defer_cache.clear()
            self._subscribe_calendar_cache.clear()
        with self._baseline_cache_lock:
            baseline_transfer = len(self._baseline_transfer_cache)
            baseline_plugin = len(self._baseline_plugin_cache)
            baseline_emby = len(self._baseline_emby_cache)
            self._baseline_transfer_cache.clear()
            self._baseline_plugin_cache.clear()
            self._baseline_emby_cache.clear()
        return {
            "media_recognition": media_recognition,
            "resource_season_dirs": resource_season_dirs,
            "platform_roots": platform_roots,
            "subscribe_defer": subscribe_defer,
            "subscribe_calendar": subscribe_calendar,
            "baseline_transfer": baseline_transfer,
            "baseline_plugin": baseline_plugin,
            "baseline_emby": baseline_emby,
        }

    def reset_sync_metrics(self) -> None:
        with self._sync_metrics_lock:
            self._sync_metrics = {}
        self.clear_runtime_cache()

    @staticmethod
    def _calendar_date(value: Any) -> Optional[datetime.date]:
        normalized = str(value or "").strip()[:10]
        if not normalized:
            return None
        try:
            return datetime.date.fromisoformat(normalized)
        except ValueError:
            return None

    def _subscribe_defer_key(self, subscribe: Any) -> Tuple[Any, ...]:
        media_type = str(getattr(subscribe, "type", "") or "")
        is_tv = media_type == MediaType.TV.value
        return (
            int(getattr(subscribe, "id", 0) or 0),
            media_type,
            int(getattr(subscribe, "tmdbid", 0) or 0),
            str(getattr(subscribe, "doubanid", "") or ""),
            str(getattr(subscribe, "name", "") or ""),
            str(getattr(subscribe, "year", "") or ""),
            int(getattr(subscribe, "season", 1) or 1) if is_tv else 0,
            int(getattr(subscribe, "start_episode", 1) or 1) if is_tv else 0,
            int(getattr(subscribe, "total_episode", 0) or 0) if is_tv else 0,
            self._is_cloud_upgrade_subscribe(subscribe),
        )

    def defer_subscribe_until(
            self,
            subscribe: Any,
            defer_until: datetime.date,
            reason: str,
    ) -> bool:
        """缓存明确的未来上映/播出日期，日期到达后自动失效。"""
        if not defer_until or defer_until <= datetime.date.today():
            return False
        key = self._subscribe_defer_key(subscribe)
        subscribe_id = key[0]
        with self._subscribe_defer_lock:
            stale_keys = [
                cached_key
                for cached_key in self._subscribe_defer_cache
                if cached_key[0] == subscribe_id and cached_key != key
            ]
            for stale_key in stale_keys:
                self._subscribe_defer_cache.pop(stale_key, None)
            self._subscribe_defer_cache[key] = {
                "defer_until": defer_until.isoformat(),
                "reason": str(reason or "尚未上映或播出"),
            }
            self._subscribe_defer_cache.move_to_end(key)
            while len(self._subscribe_defer_cache) > self._SUBSCRIBE_DEFER_CACHE_LIMIT:
                self._subscribe_defer_cache.popitem(last=False)
        logger.debug(
            f"订阅已延期至 {defer_until.isoformat()}："
            f"{getattr(subscribe, 'name', '')}，{reason}"
        )
        return True

    def get_subscribe_defer(self, subscribe: Any) -> Optional[Dict[str, str]]:
        """返回仍有效的订阅延期信息；订阅范围变化或日期到达时立即失效。"""
        key = self._subscribe_defer_key(subscribe)
        subscribe_id = key[0]
        today = datetime.date.today()
        with self._subscribe_defer_lock:
            entry = self._subscribe_defer_cache.get(key)
            if entry:
                defer_until = self._calendar_date(entry.get("defer_until"))
                if defer_until and defer_until > today:
                    self._subscribe_defer_cache.move_to_end(key)
                    return dict(entry)
                self._subscribe_defer_cache.pop(key, None)
                return None
            stale_keys = [
                cached_key
                for cached_key in self._subscribe_defer_cache
                if cached_key[0] == subscribe_id
            ]
            for stale_key in stale_keys:
                self._subscribe_defer_cache.pop(stale_key, None)
        return None

    def get_tv_subscribe_calendar(
            self,
            subscribe: Any,
            tmdb_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """复用平台订阅日历缓存，返回当前订阅目标集的播出状态。"""
        if str(getattr(subscribe, "type", "") or "") != MediaType.TV.value:
            return None
        tmdb_id = int(tmdb_id or getattr(subscribe, "tmdbid", 0) or 0)
        season = int(getattr(subscribe, "season", 1) or 1)
        start_episode = int(getattr(subscribe, "start_episode", 1) or 1)
        total_episode = int(getattr(subscribe, "total_episode", 0) or 0)
        if tmdb_id <= 0 or total_episode < start_episode:
            return None

        key = (*self._subscribe_defer_key(subscribe), tmdb_id)
        today = datetime.date.today()
        checked_on = today.isoformat()
        subscribe_id = key[0]
        with self._subscribe_defer_lock:
            entry = self._subscribe_calendar_cache.get(key)
            if entry and entry.get("checked_on") == checked_on:
                self._subscribe_calendar_cache.move_to_end(key)
                return dict(entry)
            stale_keys = [
                cached_key
                for cached_key in self._subscribe_calendar_cache
                if cached_key[0] == subscribe_id and cached_key != key
            ]
            for stale_key in stale_keys:
                self._subscribe_calendar_cache.pop(stale_key, None)
            if entry:
                self._subscribe_calendar_cache.pop(key, None)

        try:
            from app.chain.tmdb import TmdbChain

            query_kwargs = {
                "tmdbid": tmdb_id,
                "season": season,
            }
            episode_group = str(
                getattr(subscribe, "episode_group", "") or ""
            ).strip()
            if episode_group:
                query_kwargs["episode_group"] = episode_group
            episodes = self._timed_sync_call(
                "tmdb_episodes",
                TmdbChain().tmdb_episodes,
                **query_kwargs,
            )
        except Exception as error:
            logger.warning(
                f"{getattr(subscribe, 'name', '')} S{season:02d} "
                f"读取平台订阅日历失败：{error}"
            )
            return None

        expected_episodes = set(range(start_episode, total_episode + 1))
        season_known_air_dates: Dict[int, str] = {}
        season_aired_episodes: Set[int] = set()
        known_air_dates: Dict[int, str] = {}
        aired_episodes: Set[int] = set()
        for episode in episodes or []:
            try:
                episode_number = int(getattr(episode, "episode_number", 0) or 0)
            except (TypeError, ValueError):
                continue
            air_date = self._calendar_date(getattr(episode, "air_date", None))
            if episode_number <= 0 or not air_date:
                continue
            season_known_air_dates[episode_number] = air_date.isoformat()
            if air_date <= today:
                season_aired_episodes.add(episode_number)
            if episode_number not in expected_episodes:
                continue
            known_air_dates[episode_number] = air_date.isoformat()
            if air_date <= today:
                aired_episodes.add(episode_number)

        future_air_dates = {
            episode: air_date
            for episode, value in known_air_dates.items()
            if (air_date := self._calendar_date(value)) and air_date > today
        }
        last_aired_episode = max(season_aired_episodes, default=0)
        future_boundary_episode = min(
            (
                episode
                for episode in future_air_dates
                if episode > last_aired_episode
            ),
            default=0,
        )
        # TMDB 只返回到当前已公布集数时，订阅总集数后面的未知尾部同样不能搜索。
        # 只在至少存在一条可靠播出日期时建立边界，避免 TMDB 整季无数据时误跳过。
        unreleased_boundary_episode = min(
            (
                episode
                for episode in expected_episodes
                if season_known_air_dates and episode > last_aired_episode
            ),
            default=0,
        )
        boundary_reason = ""
        if unreleased_boundary_episode:
            boundary_reason = (
                "future"
                if unreleased_boundary_episode in future_air_dates
                else "unknown_tail"
            )
        unreleased_episodes = {
            episode
            for episode in expected_episodes
            if episode in future_air_dates
               or (
                       unreleased_boundary_episode > 0
                       and episode >= unreleased_boundary_episode
               )
        }
        all_targets_future = bool(
            expected_episodes and unreleased_episodes == expected_episodes
        )
        next_air_date = min(future_air_dates.values(), default=None)
        defer_until = next_air_date if all_targets_future else None
        entry = {
            "checked_on": checked_on,
            "known_air_dates": known_air_dates,
            "aired_episodes": sorted(aired_episodes),
            "aired_episode_air_dates": {
                episode: known_air_dates[episode]
                for episode in sorted(aired_episodes)
            },
            "unknown_episodes": sorted(expected_episodes - set(known_air_dates)),
            "unreleased_episodes": sorted(unreleased_episodes),
            "future_boundary_episode": future_boundary_episode,
            "unreleased_boundary_episode": unreleased_boundary_episode,
            "unreleased_boundary_reason": boundary_reason,
            "next_air_date": next_air_date.isoformat() if next_air_date else "",
            "all_targets_future": all_targets_future,
            "defer_until": defer_until.isoformat() if defer_until else "",
        }
        with self._subscribe_defer_lock:
            self._subscribe_calendar_cache[key] = entry
            self._subscribe_calendar_cache.move_to_end(key)
            while (
                    len(self._subscribe_calendar_cache)
                    > self._SUBSCRIBE_CALENDAR_CACHE_LIMIT
            ):
                self._subscribe_calendar_cache.popitem(last=False)

        if defer_until:
            self.defer_subscribe_until(
                subscribe,
                defer_until,
                f"目标剧集最早于 {defer_until.isoformat()} 播出",
            )
        return dict(entry)

    def _record_sync_metric(self, name: str, elapsed_ms: int) -> None:
        with self._sync_metrics_lock:
            metric = self._sync_metrics.setdefault(
                name, {"calls": 0, "elapsed_ms": 0}
            )
            metric["calls"] += 1
            metric["elapsed_ms"] += max(0, int(elapsed_ms or 0))

    def _timed_sync_call(self, name: str, func: Callable, *args, **kwargs):
        started = time.monotonic()
        try:
            return func(*args, **kwargs)
        finally:
            self._record_sync_metric(
                name, int((time.monotonic() - started) * 1000)
            )

    @staticmethod
    def _tmdb_id_from_media(value: Any) -> int:
        raw_id = (
            value.get("id")
            if isinstance(value, dict)
            else getattr(value, "tmdb_id", None)
        )
        try:
            return max(0, int(raw_id or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _normalized_media_title(value: Any) -> str:
        return re.sub(r"[\W_]+", "", str(value or "").casefold())

    def _match_tmdb_search_candidate(
            self,
            subscribe: Any,
            media_type: MediaType,
            candidates: List[Any],
    ) -> int:
        """仅接受类型、标题、年份均一致的唯一 TMDB 搜索结果。"""
        expected_title = self._normalized_media_title(
            getattr(subscribe, "name", "")
        )
        expected_year = str(getattr(subscribe, "year", "") or "").strip()
        matched_ids = set()
        for candidate in candidates or []:
            candidate_type = getattr(candidate, "type", None)
            if candidate_type != media_type:
                continue
            candidate_year = str(getattr(candidate, "year", "") or "").strip()
            if expected_year and candidate_year != expected_year:
                continue
            candidate_titles = {
                self._normalized_media_title(getattr(candidate, field, ""))
                for field in ("title", "original_title")
            }
            if expected_title not in candidate_titles:
                continue
            if tmdb_id := self._tmdb_id_from_media(candidate):
                matched_ids.add(tmdb_id)
        return matched_ids.pop() if len(matched_ids) == 1 else 0

    def repair_subscribe_tmdb_id(self, subscribe: Any) -> bool:
        """在订阅收集阶段使用平台媒体链修复缺失的 TMDB ID。"""
        if self._tmdb_id_from_media({"id": getattr(subscribe, "tmdbid", None)}):
            return True

        media_type = {
            MediaType.MOVIE.value: MediaType.MOVIE,
            MediaType.TV.value: MediaType.TV,
        }.get(str(getattr(subscribe, "type", "") or ""))
        subscribe_id = int(getattr(subscribe, "id", 0) or 0)
        if not media_type or subscribe_id <= 0:
            return False

        tmdb_id = 0
        source_lookups = (
            (
                "doubanid",
                "get_tmdbinfo_by_doubanid",
                getattr(subscribe, "doubanid", None),
            ),
            (
                "bangumiid",
                "get_tmdbinfo_by_bangumiid",
                getattr(subscribe, "bangumiid", None),
            ),
        )
        for source_name, method_name, source_id in source_lookups:
            lookup = getattr(self._chain, method_name, None)
            if not source_id or not callable(lookup):
                continue
            try:
                kwargs = (
                    {"doubanid": str(source_id), "mtype": media_type}
                    if source_name == "doubanid"
                    else {"bangumiid": int(source_id)}
                )
                result = self._timed_sync_call(
                    "subscribe_tmdb_repair", lookup, **kwargs
                )
                tmdb_id = self._tmdb_id_from_media(result)
            except Exception as error:
                logger.warning(
                    f"订阅 TMDB ID 自动修复的 {source_name} 映射失败："
                    f"{getattr(subscribe, 'name', '')} - {error}"
                )
            if tmdb_id:
                break

        if not tmdb_id:
            meta = MetaInfo(str(getattr(subscribe, "name", "") or ""))
            meta.year = getattr(subscribe, "year", None)
            meta.type = media_type
            try:
                candidates = self._timed_sync_call(
                    "subscribe_tmdb_repair",
                    self._chain.search_medias,
                    meta=meta,
                    source="themoviedb",
                ) or []
                tmdb_id = self._match_tmdb_search_candidate(
                    subscribe, media_type, candidates
                )
            except Exception as error:
                logger.warning(
                    f"订阅 TMDB ID 自动修复的标题查询失败："
                    f"{getattr(subscribe, 'name', '')} - {error}"
                )

        # 同步准备阶段可能早于平台搜索缓存建立；识别链是同一套平台
        # 能力，但会按标题/年份直接返回唯一 MediaInfo，作为最后兜底。
        if not tmdb_id:
            try:
                recognized = self._recognize_media_once(
                    (
                        "subscribe_tmdb_repair",
                        media_type.value,
                        getattr(subscribe, "name", ""),
                        getattr(subscribe, "year", None),
                    ),
                    meta=meta,
                    mtype=media_type,
                    tmdbid=None,
                    doubanid=getattr(subscribe, "doubanid", None),
                    cache=True,
                )
                tmdb_id = self._tmdb_id_from_media(recognized)
            except Exception as error:
                logger.warning(
                    f"订阅 TMDB ID 自动修复的媒体识别失败："
                    f"{getattr(subscribe, 'name', '')} - {error}"
                )

        if not tmdb_id:
            logger.debug(
                f"订阅 TMDB ID 自动修复未找到唯一匹配："
                f"{getattr(subscribe, 'name', '')} ({getattr(subscribe, 'year', '')})"
            )
            return False

        try:
            updated = SubscribeOper().update(subscribe_id, {"tmdbid": tmdb_id})
        except Exception as error:
            logger.warning(
                f"订阅 TMDB ID 自动回填失败："
                f"{getattr(subscribe, 'name', '')} -> {tmdb_id} - {error}"
            )
            return False
        if not updated:
            logger.warning(f"订阅 TMDB ID 自动回填失败：订阅 {subscribe_id} 不存在")
            return False

        setattr(subscribe, "tmdbid", tmdb_id)
        logger.info(
            f"订阅 TMDB ID 已自动回填："
            f"{getattr(subscribe, 'name', '')} -> {tmdb_id}"
        )
        return True

    def _set_task_phase(self, subscribe: Any, phase: str, progress: int) -> None:
        """回写订阅任务的真实处理阶段。"""
        if self._task_update:
            self._task_update(
                f"subscribe:{getattr(subscribe, 'id', '')}",
                phase=phase,
                progress=max(0, min(100, int(progress))),
            )

    def _recognize_media_once(self, key: Tuple[Any, ...], **kwargs: Any):
        with self._media_recognition_lock:
            if key in self._media_recognition_cache:
                self._media_recognition_cache.move_to_end(key)
                return self._media_recognition_cache[key]
            future = self._media_recognition_inflight.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._media_recognition_inflight[key] = future

        if not owner:
            return future.result()

        try:
            # 的媒体识别链包含非线程安全的远端客户端游标，不并发调用。
            with self._platform_media_recognition_lock:
                mediainfo = self._timed_sync_call(
                    "media_recognition", self._chain.recognize_media, **kwargs
                )
        except BaseException as error:
            future.set_exception(error)
            with self._media_recognition_lock:
                if self._media_recognition_inflight.get(key) is future:
                    self._media_recognition_inflight.pop(key, None)
            raise

        if mediainfo:
            with self._media_recognition_lock:
                self._media_recognition_cache[key] = mediainfo
                self._media_recognition_cache.move_to_end(key)
                while len(self._media_recognition_cache) > self._MEDIA_RECOGNITION_CACHE_LIMIT:
                    self._media_recognition_cache.popitem(last=False)
        future.set_result(mediainfo)
        with self._media_recognition_lock:
            if self._media_recognition_inflight.get(key) is future:
                self._media_recognition_inflight.pop(key, None)
        return mediainfo

    def get_sync_metrics(self) -> Dict[str, Dict[str, int]]:
        with self._sync_metrics_lock:
            return copy.deepcopy(self._sync_metrics)

    def _is_offline_url(self, url: str) -> bool:
        return bool(
            self._offline_download
            and self._offline_download.is_offline_url(url)
        )

    def _is_ed2k_url(self, url: str) -> bool:
        return bool(
            self._offline_download and self._offline_download.is_ed2k_url(url)
        )

    def _is_magnet_url(self, url: str) -> bool:
        return bool(
            self._offline_download and self._offline_download.is_magnet_url(url)
        )

    def close(self) -> None:
        """提交尚未发送的媒体目录通知并释放通知定时器。"""
        self._media_server_notifier.close(flush=True)

    def update_notification_config(
            self,
            notify: bool,
            notification_type: NotificationType,
            media_server_refresh_enabled: bool,
            media_servers: List[str],
            media_server_path_mappings: str,
            media_server_refresh_delay: int,
            emby_mediainfo_enabled: bool,
    ) -> None:
        self._notify = bool(notify)
        self._notification_type = notification_type
        old_notifier = self._media_server_notifier
        self._media_server_notifier = MediaServerNotifier(
            enabled=media_server_refresh_enabled,
            mediaservers=media_servers,
            path_mappings=media_server_path_mappings,
            delay_seconds=media_server_refresh_delay,
            emby_mediainfo_enabled=emby_mediainfo_enabled,
        )
        old_notifier.close(flush=True)

    def begin_notification_batch(self) -> bool:
        """开始一次同步任务的媒体目录通知聚合。"""
        return self._media_server_notifier.begin_task_batch()

    def finish_notification_batch(self) -> bool:
        """同步任务收尾后统一提交媒体目录通知。"""
        return self._media_server_notifier.finish_task_batch()

    def _stop_requested(self) -> bool:
        try:
            return bool(self._should_stop and self._should_stop())
        except Exception as err:
            logger.warning(f"读取停止状态失败：{err}")
            return False

    def _current_task_context(self) -> Tuple[str, Any]:
        if not self._task_context:
            return "", None
        try:
            task_id, stop_event = self._task_context()
            return str(task_id or ""), stop_event
        except Exception as error:
            logger.debug(f"读取当前订阅任务上下文失败：{error}")
            return "", None

    def reset_transfer_budget(self) -> None:
        """重置本轮同步共享的转存文件数配额。"""
        with self._transfer_budget_lock:
            self._transfer_budget_used = 0

    def _remaining_transfer_quota(self) -> int:
        with self._transfer_budget_lock:
            return max(0, self._max_transfer_per_sync - self._transfer_budget_used)

    def _reserve_transfer_slots(self, requested: int) -> int:
        """原子预留转存名额，返回本次实际取得的名额数。"""
        with self._transfer_budget_lock:
            reserved = min(max(0, int(requested or 0)), self._remaining_transfer_quota())
            self._transfer_budget_used += reserved
            return reserved

    def _release_transfer_slots(self, count: int) -> None:
        """转存失败时归还已预留但未使用的名额。"""
        with self._transfer_budget_lock:
            self._transfer_budget_used = max(
                0, self._transfer_budget_used - max(0, int(count or 0))
            )

    def _ensure_share_transfer_available(self, provider_key: str) -> None:
        key = str(provider_key or "default").lower()
        with self._share_transfer_risk_lock:
            remaining = self._share_transfer_blocked_until.get(key, 0.0) - time.monotonic()
        if remaining > 0:
            raise RuntimeError(f"{key} 分享转存处于风控冷却期，剩余 {int(remaining)} 秒")

    def _activate_share_transfer_cooldown(self, provider_key: str) -> None:
        key = str(provider_key or "default").lower()
        with self._share_transfer_risk_lock:
            self._share_transfer_blocked_until[key] = max(
                self._share_transfer_blocked_until.get(key, 0.0),
                time.monotonic() + self._transfer_risk_cooldown,
            )
        logger.warning(
            f"{key} 分享转存连续失败，冷却 {self._transfer_risk_cooldown} 秒"
        )

    def _transfer_episode_items(
            self,
            matched_items: List[Dict[str, Any]],
            share_url: str,
            mediainfo: MediaInfo,
            subscribe,
            season: int,
            sub_key: str,
            track_subscription: bool = True,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """统一执行剧集批量转存、STRM后处理和媒体目录通知。"""
        requested = len(matched_items)
        reserved = self._reserve_transfer_slots(requested)
        if reserved <= 0:
            return [], 0
        selected_items = matched_items[:reserved]
        if reserved < requested:
            logger.info(
                f"匹配 {requested} 个文件，受单次同步配额限制仅转存 {reserved} 个"
            )
        if self._stop_requested():
            self._release_transfer_slots(reserved)
            return [], reserved

        file_ids = [str(item["file"]["id"]) for item in selected_items]
        rename_items = {}
        for item in selected_items:
            file_item = item["file"]
            item_url = str(file_item.get("url") or share_url).strip()
            rename_items[str(file_item["id"])] = {
                "sha1": file_item.get("sha1"),
                "target_name": (
                    None if self._is_offline_url(item_url) else item["target_name"]
                ),
                "url": item_url,
            }
        try:
            source_provider = self._resource_provider_for_url(share_url)
            provider_key = getattr(source_provider, "key", "") or getattr(
                self._cloud_drive, "key", "default"
            )
            self._ensure_share_transfer_available(provider_key)
            cross_batch = bool(
                self._cross_transfer_enabled and source_provider
                and self._cloud_drive and source_provider.key != self._cloud_drive.key
            )
            if cross_batch:
                parent_task_id, task_stop_event = self._current_task_context()
                source_abort_event = threading.Event()

                def batch_stop_requested() -> bool:
                    return bool(
                        global_vars.is_system_stopped
                        or self._stop_requested()
                        or source_abort_event.is_set()
                        or (task_stop_event and task_stop_event.is_set())
                    )

                def transfer_one(item: Dict[str, Any]) -> Tuple[str, Optional[bool]]:
                    file_id = str(item["file"]["id"])
                    if batch_stop_requested():
                        return file_id, None
                    try:
                        success = self._transfer_file(
                            str(item["file"].get("url") or share_url),
                            item["file"],
                            self._cloud_transfer_path,
                            item["target_name"],
                            str(item["file"].get("sha1") or ""),
                            parent_task_id=parent_task_id,
                            stop_requested=batch_stop_requested,
                            media_type=getattr(getattr(mediainfo, "type", None), "value", ""),
                        )
                    except Exception as error:
                        if batch_stop_requested():
                            return file_id, None
                        error_text = str(error)
                        if any(marker in error_text for marker in (
                                "封禁转存", "风控", "未返回下载地址",
                                "No space left on device", "磁盘可用空间不足",
                        )):
                            source_abort_event.set()
                            logger.error(
                                f"跨盘转存批次已熔断：{item['target_name']}，{error_text}"
                            )
                            return file_id, False
                        logger.error(
                            f"跨盘转存文件失败：{item['target_name']}，{error}"
                        )
                        return file_id, False
                    if not success and batch_stop_requested():
                        return file_id, None
                    return file_id, success

                provider_limits = [3]
                for provider in (source_provider, self._cloud_drive):
                    limit = int(
                        getattr(getattr(provider, "policy", None), "max_concurrency", 1)
                        or 1
                    )
                    provider_limits.append(limit)
                worker_count = min(len(selected_items), *provider_limits)
                outcomes: Dict[str, bool] = {}
                executor = ThreadPoolExecutor(
                    max_workers=max(1, worker_count),
                    thread_name_prefix="cloudsubscribe-file-download",
                )
                futures = {
                    executor.submit(transfer_one, item): str(item["file"]["id"])
                    for item in selected_items
                }
                try:
                    for future in as_completed(futures):
                        try:
                            file_id, success = future.result()
                        except CancelledError:
                            continue
                        if success is not None:
                            outcomes[file_id] = success
                        if batch_stop_requested():
                            for pending in futures:
                                pending.cancel()
                finally:
                    executor.shutdown(wait=True, cancel_futures=True)
                processed_items = [
                    item for item in selected_items
                    if str(item["file"]["id"]) in outcomes
                ]
                success_ids = [
                    file_id for file_id, success in outcomes.items() if success
                ]
                if outcomes and not success_ids and all(
                        success is False for success in outcomes.values()
                ):
                    self._activate_share_transfer_cooldown(provider_key)
            else:
                processed_items = selected_items
                success_ids, failed_ids = self._timed_sync_call(
                    "share_transfer",
                    self._share_transfer.transfer_files_batch,
                    share_url=share_url,
                    file_ids=file_ids,
                    save_path=self._cloud_transfer_path,
                    batch_size=self._batch_size,
                    batch_interval=self._batch_interval,
                    risk_cooldown=self._transfer_risk_cooldown,
                    rename_items=rename_items,
                )
                if failed_ids and not success_ids:
                    self._activate_share_transfer_cooldown(provider_key)
        except Exception as error:
            message = str(error)
            if any(marker in message.lower() for marker in (
                    "风控", "封禁", "受限", "频繁", "rate limit", "too many", "429",
            )):
                self._activate_share_transfer_cooldown(locals().get("provider_key", "default"))
            self._release_transfer_slots(reserved)
            raise

        success_id_set = {str(file_id) for file_id in (success_ids or [])}
        self._release_transfer_slots(reserved - len(success_id_set))
        batch_strm_results = self._generate_or_queue_strm_batch(
            [
                {
                    "result_key": str(item["file"]["id"]),
                    "share_url": str(item["file"].get("url") or share_url),
                    "cloud_dir": item["target_dir"],
                    "file_name": item["target_name"],
                    "staging_dir": self._cloud_transfer_path,
                    "staging_name": (
                            item["file"].get("staging_name")
                            or item["file"]["name"]
                    ),
                    "source_sha1": item["file"].get("sha1"),
                    "file_size": item["file"].get("size") or 0,
                    "success_episodes": item.get(
                        "success_episodes",
                        [] if item.get("is_upgrade") or not track_subscription
                        else [item["episode"]],
                    ),
                    "notification_episodes": item.get(
                        "notification_episodes", [item["episode"]]
                    ),
                    "upgrade": item.get("is_upgrade"),
                    "upgrade_mode": self._upgrade_mode,
                    "upgrade_old_cloud_dir": item.get("upgrade_old_cloud_dir"),
                    "upgrade_old_file_name": item.get("upgrade_old_file_name"),
                    "upgrade_old_file_id": item.get("upgrade_old_file_id"),
                    "upgrade_old_size": item.get("upgrade_old_size") or 0,
                }
                for item in processed_items
                if str(item["file"]["id"]) in success_id_set
            ],
            mediainfo,
            subscribe_id=(
                getattr(subscribe, "id", None) if track_subscription else None
            ),
            season=season,
            sub_key=sub_key,
        )
        results = []
        for item in processed_items:
            file_id = str(item["file"]["id"])
            success = file_id in success_id_set
            strm_path, pending_key = batch_strm_results.get(file_id, (None, ""))
            if success and not strm_path and not pending_key:
                logger.error(
                    f"文件已转存但后处理任务登记失败：{item['target_name']}"
                )
                self._release_transfer_slots(1)
                success = False
            if success and strm_path:
                self._media_server_notifier.notify(
                    path=strm_path,
                    mediainfo=mediainfo,
                    file_name=item["target_name"],
                )
            results.append({
                "item": item,
                "file_id": file_id,
                "success": success,
                "pending_key": pending_key,
            })
        return results, reserved

    def _match_episode_files(
            self,
            files: list,
            mediainfo: MediaInfo,
            subscribe,
            season: int,
            episodes: List[int],
    ) -> Dict[int, tuple]:
        """按结构收集剧集候选，再使用规则组选择文件。"""
        episode_list = list(dict.fromkeys(int(value) for value in episodes))
        candidates = FileMatcher.episode_candidates(
            files, season, episode_list, mediainfo=mediainfo
        )
        return {
            episode: self._search_handler.select_file_candidate(
                candidates.get(episode) or [], mediainfo, subscribe
            )
            for episode in episode_list
        }

    def _match_movie_file(
            self, files: list, mediainfo: MediaInfo, subscribe,
            resource_title: str = "",
    ) -> tuple:
        """按媒体文件结构收集电影候选，再使用规则组选择。"""
        matched = self._search_handler.select_file_candidate(
            FileMatcher.movie_candidates(files, mediainfo=mediainfo),
            mediainfo,
            subscribe,
        )
        if matched[0]:
            return matched

        # 文件名被网盘混淆时，仅允许“资源标题匹配 + 唯一大视频”进入兜底，
        # 避免短剧合集、音乐包或多文件资源被误当成目标电影。
        fallback = FileMatcher.movie_candidates(files)
        if (
                len(fallback) != 1
                or not FileMatcher.media_name_matches(resource_title, mediainfo)
        ):
            return None, 0
        actual = fallback[0]
        scoring_item = dict(actual)
        scoring_item["name"] = (
            f"{str(resource_title).strip()}{Path(str(actual.get('name') or '')).suffix}"
        )
        selected, score = self._search_handler.select_file_candidate(
            [scoring_item], mediainfo, subscribe
        )
        return (actual, score) if selected else (None, 0)

    def _generate_strm(
            self,
            cloud_dir: str,
            file_name: str,
            target_file: Optional[CloudFile] = None,
            lookup_target: bool = True,
            log_success: bool = True,
    ) -> Optional[Path]:
        """使用网盘提供方的播放引用值生成 STRM。"""
        if not self._strm_generate_enabled:
            return None
        if not self._strm_generator:
            return None
        if not self._local_resource_path:
            logger.warning("已启用 STRM 直接生成，但未配置本地/挂载媒体根路径")
            return None

        if not target_file and lookup_target:
            target_file = self._cloud_query.find_file(
                cloud_dir, file_name
            )
        if not target_file:
            if log_success:
                logger.debug(
                    f"网盘目标文件尚未就绪，暂不生成 STRM："
                    f"{cloud_dir.rstrip('/')}/{file_name}"
                )
            return None
        template_values = self._playback_reference.reference_values(target_file)
        try:
            strm_path, content = self._strm_generator.write(
                local_root=self._local_resource_path,
                cloud_root=self._CLOUD_MEDIA_ROOT,
                cloud_dir=cloud_dir,
                file_name=file_name,
                template_values=template_values,
            )
            if log_success:
                logger.debug(f"STRM 已生成：{strm_path} -> {content}")
            return strm_path
        except (OSError, StrmTemplateError) as error:
            logger.error(f"生成 STRM 失败：{file_name}，原因：{error}")
            return None

    def _scrape_metadata(
            self,
            cloud_dir: str,
            file_name: str,
            mediainfo: MediaInfo,
            season: Optional[int] = None,
            episode: Optional[int] = None,
    ) -> Optional[Path]:
        """在最终分类路径补齐元数据；失败不影响网盘文件处理。"""
        if not self._metadata_scraper or not self._local_resource_path:
            return None
        try:
            mapped_path = self._path_mapper.local_path(
                local_root=self._local_resource_path,
                cloud_root=self._CLOUD_MEDIA_ROOT,
                cloud_dir=cloud_dir,
                file_name=file_name,
            )
            media_path = mapped_path.with_suffix(Path(file_name).suffix)
            logger.info(
                f"开始元数据刮削：{media_path}，"
                f"NFO={'是' if self._nfo_scrape_enabled else '否'}，"
                f"图片={'是' if self._image_scrape_enabled else '否'}"
            )
            created = self._metadata_scraper.scrape_batch([{
                "media_path": media_path,
                "season": season,
                "episode": episode,
            }], mediainfo)
            if created:
                logger.info(f"元数据刮削完成：{media_path.parent}，新增 {created} 个文件")
            else:
                logger.info(
                    f"元数据刮削完成但无新增文件：{media_path.parent}，"
                    f"目标文件可能已存在或 TMDB 未返回内容"
                )
            return media_path
        except Exception as error:
            logger.warning(f"元数据刮削失败：{file_name}，{error}")
            return None

    def _scrape_metadata_batch(
            self,
            items: List[Dict[str, Any]],
            mediainfo: MediaInfo,
            season: Optional[int] = None,
    ) -> None:
        """按一次转存批次刮削，避免重复请求剧根与季元数据。"""
        if not self._metadata_scraper or not self._local_resource_path or not items:
            return
        scrape_items = []
        try:
            for item in items:
                mapped_path = self._path_mapper.local_path(
                    local_root=self._local_resource_path,
                    cloud_root=self._CLOUD_MEDIA_ROOT,
                    cloud_dir=item["cloud_dir"],
                    file_name=item["file_name"],
                )
                episode = next(iter(
                    item.get("notification_episodes")
                    or item.get("success_episodes")
                    or []
                ), None)
                scrape_items.append({
                    "media_path": mapped_path.with_suffix(Path(item["file_name"]).suffix),
                    "season": season,
                    "episode": episode,
                })
            logger.debug(
                f"开始批量元数据刮削：{mediainfo.title_year}，"
                f"{len(scrape_items)} 个媒体文件，"
                f"NFO={'是' if self._nfo_scrape_enabled else '否'}，"
                f"图片={'是' if self._image_scrape_enabled else '否'}"
            )
            created = self._metadata_scraper.scrape_batch(scrape_items, mediainfo)
            if created:
                logger.debug(
                    f"批量元数据刮削完成：{mediainfo.title_year}，"
                    f"{len(scrape_items)} 个媒体文件，新增 {created} 个文件"
                )
            else:
                logger.debug(
                    f"批量元数据刮削完成但无新增文件：{mediainfo.title_year}，"
                    f"{len(scrape_items)} 个媒体文件，"
                    "目标文件可能已存在或 TMDB 未返回内容"
                )
        except Exception as error:
            logger.warning(f"批量元数据刮削失败：{error}")

    def _offline_hash(self, share_url: str) -> str:
        match = re.search(r"\|([0-9A-Fa-f]{32})(?:\|[^|]*)*\|/$", str(share_url or ""))
        if match:
            return match.group(1).upper()
        magnet = self._offline_download.parse_magnet_link(share_url)
        return str((magnet or {}).get("hash") or "").upper()

    def _resource_log_reference(self, share_url: str) -> str:
        """Magnet 日志仅展示 infoHash，避免输出完整 Tracker 参数。"""
        if self._is_magnet_url(share_url):
            return f"infoHash={self._offline_hash(share_url) or '未知'}"
        return str(share_url or "")

    def _queue_magnet_package(
            self,
            resource: Dict[str, Any],
            share_url: str,
            subscribe: Any,
            mediainfo: MediaInfo,
            season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            sub_key: str = "",
            upgrade: bool = False,
            upgrade_mode: str = "",
            upgrade_baseline: Optional[Dict[str, Any]] = None,
            transient_target: bool = False,
    ) -> str:
        """提交 Magnet 到隔离目录；下载完成后再按真实文件树匹配。"""
        info_hash = self._offline_hash(share_url)
        metadata = resource.get("magnet_metadata") or {}
        if not metadata.get("metadata_available"):
            magnet_info = self._offline_download.parse_magnet_link(
                share_url, fetch_metadata=True
            )
            metadata = (magnet_info or {}).get("metadata") or {}
            if metadata:
                resource["magnet_metadata"] = metadata
        if (
                not info_hash
                or not self._get_data
                or not bool(metadata.get("metadata_available"))
        ):
            logger.warning("Magnet 未取得内容元数据，拒绝提交网盘离线下载")
            return ""
        subscribe_id = int(getattr(subscribe, "id", 0) or 0)
        pending_key = f"magnet:{info_hash}:{subscribe_id}"
        staging_dir = f"{self._cloud_transfer_path.rstrip('/')}"
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            if pending_key in pending:
                return pending_key
        if not self._offline_download.add_offline_download(share_url, staging_dir):
            return ""
        now = time.time()
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            pending[pending_key] = {
                "pending_key": pending_key,
                "task_type": "magnet",
                "task_id": info_hash,
                "share_url": share_url,
                "cloud_dir": staging_dir,
                "file_name": str(
                    (resource.get("magnet_metadata") or {}).get("display_name")
                    or resource.get("title") or info_hash
                ),
                "created_at": now,
                "next_check_at": now + self._OFFLINE_CHECK_DELAYS[0],
                "check_index": 0,
                "history_ready": True,
                "mediainfo": self._serialize_mediainfo(mediainfo),
                "subscribe_id": subscribe_id,
                "season": season,
                "target_episodes": sorted({
                    int(value) for value in (target_episodes or []) if int(value) > 0
                }),
                "resource": dict(resource),
                "sub_key": str(sub_key or ""),
                "upgrade": bool(upgrade),
                "upgrade_mode": str(upgrade_mode or self._upgrade_mode),
                "upgrade_baseline": dict(upgrade_baseline or {}),
                "transient_target": bool(transient_target),
                "target_subscribe": {
                    "name": str(getattr(subscribe, "name", "") or ""),
                    "year": getattr(subscribe, "year", None),
                    "type": str(getattr(subscribe, "type", "") or ""),
                    "tmdbid": getattr(subscribe, "tmdbid", None),
                    "doubanid": getattr(subscribe, "doubanid", None),
                    "season": getattr(subscribe, "season", None),
                    "start_episode": getattr(subscribe, "start_episode", None),
                    "total_episode": getattr(subscribe, "total_episode", None),
                    "media_category": getattr(subscribe, "media_category", None),
                    "episode_group": getattr(subscribe, "episode_group", None),
                    "filter_groups": getattr(subscribe, "filter_groups", None),
                    "best_version": bool(getattr(subscribe, "best_version", False)),
                    "_manual_upgrade": bool(getattr(subscribe, "_manual_upgrade", False)),
                } if transient_target else {},
            }
            self._save_offline_pending(pending)
            pending_count = len(pending)
        self._notify_offline_pending_changed(pending_count)
        logger.info(
            f"⏳ Magnet 已提交115隔离目录，完成后按真实文件匹配：{pending[pending_key]['file_name']}"
        )
        return pending_key

    @staticmethod
    def _serialize_mediainfo(mediainfo: MediaInfo) -> Dict[str, Any]:
        if not mediainfo:
            return {}
        try:
            if hasattr(mediainfo, "to_dict"):
                return mediainfo.to_dict()
            if hasattr(mediainfo, "model_dump"):
                return mediainfo.model_dump(mode="json")
            if hasattr(mediainfo, "dict"):
                return mediainfo.dict()
        except Exception as error:
            logger.debug(f"序列化媒体信息失败，将仅生成 STRM：{error}")
        return {}

    @staticmethod
    def _deserialize_mediainfo(media_data: Dict[str, Any]) -> Optional[MediaInfo]:
        if not media_data:
            return None
        mediainfo = MediaInfo()
        if hasattr(mediainfo, "from_dict"):
            mediainfo.from_dict(dict(media_data))
            return mediainfo
        return MediaInfo(**media_data)

    def _save_offline_pending(self, pending: Dict[str, Dict[str, Any]]) -> None:
        if self._save_data:
            self._save_data(self._OFFLINE_PENDING_KEY, pending)

    def _notify_offline_pending_changed(self, pending_count: int) -> None:
        try:
            if self._offline_pending_changed:
                self._offline_pending_changed(max(0, int(pending_count or 0)))
        except Exception as error:
            logger.warning(f"更新网盘文件后处理监控状态失败：{error}")

    @staticmethod
    def _finalize_source_identity(
            source_sha1: str, staging_dir: str, staging_name: str,
            file_size: int,
    ) -> Tuple[str, str]:
        source_hash = re.sub(
            r"[^0-9A-Fa-f]", "", str(source_sha1 or "")
        ).upper()
        if len(source_hash) != 40:
            source_hash = ""
        identity = source_hash or hashlib.sha1(
            "\0".join((
                str(staging_dir or "/").rstrip("/") or "/",
                str(staging_name or ""),
                str(max(0, int(file_size or 0))),
            )).encode("utf-8")
        ).hexdigest().upper()
        return source_hash, identity

    def _queue_file_finalize(
            self,
            share_url: str,
            cloud_dir: str,
            file_name: str,
            mediainfo: MediaInfo,
            source_sha1: str = "",
            file_size: int = 0,
            subscribe_id: Optional[int] = None,
            success_episodes: Optional[List[int]] = None,
            season: Optional[int] = None,
            notification_episodes: Optional[List[int]] = None,
            sub_key: str = "",
            staging_dir: str = "",
            staging_name: str = "",
            upgrade: bool = False,
            upgrade_mode: str = "",
            upgrade_old_cloud_dir: str = "",
            upgrade_old_file_name: str = "",
            upgrade_old_file_id: str = "",
            upgrade_old_size: int = 0,
    ) -> str:
        info_hash = self._offline_hash(share_url)
        effective_staging_dir = str(staging_dir or cloud_dir).rstrip("/") or "/"
        effective_staging_name = str(staging_name or file_name)
        source_hash, source_identity = self._finalize_source_identity(
            source_sha1, effective_staging_dir, effective_staging_name, file_size
        )
        if not self._get_data:
            logger.error(f"无法登记文件后处理任务：{file_name}")
            return ""
        now = time.time()
        if info_hash:
            pending_key = info_hash
            task_type = "magnet" if self._is_magnet_url(share_url) else "ed2k"
        else:
            path_digest = hashlib.sha1(
                f"{cloud_dir.rstrip('/')}/{file_name}".encode("utf-8")
            ).hexdigest()[:12]
            provider_key = str(getattr(self._cloud_drive, "key", "cloud") or "cloud")
            pending_key = f"{provider_key}:{source_identity}:{path_digest}"
            task_type = "share"
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            current = pending.get(pending_key) or {}
            pending[pending_key] = {
                **current,
                "pending_key": pending_key,
                "task_type": task_type,
                "task_id": info_hash,
                "source_sha1": source_hash,
                "share_url": share_url,
                "cloud_dir": cloud_dir,
                "file_name": file_name,
                "staging_dir": effective_staging_dir,
                "staging_name": effective_staging_name,
                "file_size": int(file_size or current.get("file_size") or 0),
                "upgrade": bool(upgrade or current.get("upgrade")),
                "upgrade_mode": str(upgrade_mode or current.get("upgrade_mode") or self._upgrade_mode),
                "upgrade_old_cloud_dir": str(upgrade_old_cloud_dir or current.get("upgrade_old_cloud_dir") or ""),
                "upgrade_old_file_name": str(upgrade_old_file_name or current.get("upgrade_old_file_name") or ""),
                "upgrade_old_file_id": str(upgrade_old_file_id or current.get("upgrade_old_file_id") or ""),
                "upgrade_old_size": int(upgrade_old_size or current.get("upgrade_old_size") or 0),
                "created_at": float(current.get("created_at") or now),
                "next_check_at": now + self._OFFLINE_CHECK_DELAYS[0],
                "check_index": int(current.get("check_index") or 0),
                "history_ready": False,
                "mediainfo": self._serialize_mediainfo(mediainfo),
                "subscribe_id": subscribe_id or current.get("subscribe_id"),
                "success_episodes": sorted(
                    {
                        int(episode)
                        for episode in (
                            success_episodes or current.get("success_episodes") or []
                    )
                        if int(episode) > 0
                    }
                ),
                "season": (
                    max(1, int(season or current.get("season") or 1))
                    if getattr(mediainfo, "type", None) == MediaType.TV
                    else None
                ),
                "episode": next(
                    iter(notification_episodes or success_episodes or []), None
                ),
                "notification_episodes": sorted(
                    {
                        int(episode)
                        for episode in (
                            notification_episodes
                            or current.get("notification_episodes")
                            or success_episodes
                            or current.get("success_episodes")
                            or []
                    )
                        if int(episode) > 0
                    }
                ),
                "sub_key": str(sub_key or current.get("sub_key") or ""),
            }
            self._save_offline_pending(pending)
            pending_count = len(pending)
        self._notify_offline_pending_changed(pending_count)
        if info_hash:
            logger.info(f"⏳ 已登记离线完成监控：{file_name}")
        else:
            logger.info(f"⏳ 文件仍在115系统处理中，已登记重命名与STRM后处理：{file_name}")
        return pending_key

    def _generate_or_queue_strm(
            self,
            share_url: str,
            cloud_dir: str,
            file_name: str,
            mediainfo: MediaInfo,
            source_sha1: str = "",
            file_size: int = 0,
            subscribe_id: Optional[int] = None,
            success_episodes: Optional[List[int]] = None,
            season: Optional[int] = None,
            notification_episodes: Optional[List[int]] = None,
            sub_key: str = "",
            target_file: Optional[CloudFile] = None,
            lookup_target: bool = True,
            log_success: bool = True,
            staging_dir: str = "",
            staging_name: str = "",
            upgrade: bool = False,
            upgrade_mode: str = "",
            upgrade_old_cloud_dir: str = "",
            upgrade_old_file_name: str = "",
            upgrade_old_file_id: str = "",
            upgrade_old_size: int = 0,
    ) -> Tuple[Optional[Path], str]:
        strm_path = None
        if not staging_dir:
            strm_path = self._generate_strm(
                cloud_dir,
                file_name,
                target_file=target_file,
                lookup_target=lookup_target,
                log_success=log_success,
            )
            self._scrape_metadata(
                cloud_dir,
                file_name,
                mediainfo,
                season=season,
                episode=next(iter(notification_episodes or success_episodes or []), None),
            )
        if strm_path:
            return strm_path, ""
        pending_key = self._queue_file_finalize(
            share_url=share_url,
            cloud_dir=cloud_dir,
            file_name=file_name,
            mediainfo=mediainfo,
            source_sha1=source_sha1,
            file_size=file_size,
            subscribe_id=subscribe_id,
            success_episodes=success_episodes,
            season=season,
            notification_episodes=notification_episodes,
            sub_key=sub_key,
            staging_dir=staging_dir,
            staging_name=staging_name,
            upgrade=upgrade,
            upgrade_mode=upgrade_mode,
            upgrade_old_cloud_dir=upgrade_old_cloud_dir,
            upgrade_old_file_name=upgrade_old_file_name,
            upgrade_old_file_id=upgrade_old_file_id,
            upgrade_old_size=upgrade_old_size,
        )
        return None, pending_key

    def _queue_file_finalize_batch(
            self,
            items: List[Dict[str, Any]],
            mediainfo: MediaInfo,
            subscribe_id: Optional[int] = None,
            season: Optional[int] = None,
            sub_key: str = "",
    ) -> Dict[str, str]:
        """一次持久化整批未就绪文件，避免逐项读写插件数据。"""
        if not items or not self._get_data:
            return {}
        now = time.time()
        media_data = self._serialize_mediainfo(mediainfo)
        result: Dict[str, str] = {}
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            for item in items:
                share_url = item["share_url"]
                cloud_dir = item["cloud_dir"]
                file_name = item["file_name"]
                info_hash = self._offline_hash(share_url)
                staging_dir = str(
                    item.get("staging_dir") or cloud_dir
                ).rstrip("/") or "/"
                staging_name = str(item.get("staging_name") or file_name)
                source_hash, source_identity = self._finalize_source_identity(
                    item.get("source_sha1") or "",
                    staging_dir,
                    staging_name,
                    item.get("file_size") or 0,
                )
                if info_hash:
                    pending_key = info_hash
                    task_type = "magnet" if self._is_magnet_url(share_url) else "ed2k"
                else:
                    path_digest = hashlib.sha1(
                        f"{cloud_dir.rstrip('/')}/{file_name}".encode("utf-8")
                    ).hexdigest()[:12]
                    provider_key = str(
                        getattr(self._cloud_drive, "key", "cloud") or "cloud"
                    )
                    pending_key = f"{provider_key}:{source_identity}:{path_digest}"
                    task_type = "share"
                current = pending.get(pending_key) or {}
                pending[pending_key] = {
                    **current,
                    "pending_key": pending_key,
                    "task_type": task_type,
                    "task_id": info_hash,
                    "source_sha1": source_hash,
                    "share_url": share_url,
                    "cloud_dir": cloud_dir,
                    "file_name": file_name,
                    "staging_dir": staging_dir,
                    "staging_name": staging_name,
                    "file_size": int(
                        item.get("file_size") or current.get("file_size") or 0
                    ),
                    "upgrade": bool(item.get("upgrade") or current.get("upgrade")),
                    "upgrade_mode": str(item.get("upgrade_mode") or current.get("upgrade_mode") or self._upgrade_mode),
                    "upgrade_old_cloud_dir": str(
                        item.get("upgrade_old_cloud_dir") or current.get("upgrade_old_cloud_dir") or ""),
                    "upgrade_old_file_name": str(
                        item.get("upgrade_old_file_name") or current.get("upgrade_old_file_name") or ""),
                    "upgrade_old_file_id": str(
                        item.get("upgrade_old_file_id") or current.get("upgrade_old_file_id") or ""),
                    "upgrade_old_size": int(item.get("upgrade_old_size") or current.get("upgrade_old_size") or 0),
                    "created_at": float(current.get("created_at") or now),
                    "next_check_at": now + self._OFFLINE_CHECK_DELAYS[0],
                    "check_index": int(current.get("check_index") or 0),
                    "history_ready": False,
                    "mediainfo": media_data,
                    "subscribe_id": subscribe_id or current.get("subscribe_id"),
                    "success_episodes": sorted(
                        {
                            int(episode)
                            for episode in (
                                item.get("success_episodes")
                                or current.get("success_episodes")
                                or []
                        )
                            if int(episode) > 0
                        }
                    ),
                    "season": (
                        max(1, int(season or current.get("season") or 1))
                        if getattr(mediainfo, "type", None) == MediaType.TV
                        else None
                    ),
                    "episode": next(
                        iter(
                            item.get("notification_episodes")
                            or item.get("success_episodes")
                            or []
                        ),
                        None,
                    ),
                    "notification_episodes": sorted(
                        {
                            int(episode)
                            for episode in (
                                item.get("notification_episodes")
                                or current.get("notification_episodes")
                                or item.get("success_episodes")
                                or current.get("success_episodes")
                                or []
                        )
                            if int(episode) > 0
                        }
                    ),
                    "sub_key": str(sub_key or current.get("sub_key") or ""),
                }
                result[str(item["result_key"])] = pending_key
            if result:
                self._save_offline_pending(pending)
            pending_count = len(pending)
        self._notify_offline_pending_changed(pending_count)
        return result

    def _generate_or_queue_strm_batch(
            self,
            items: List[Dict[str, Any]],
            mediainfo: MediaInfo,
            subscribe_id: Optional[int] = None,
            season: Optional[int] = None,
            sub_key: str = "",
    ) -> Dict[str, Tuple[Optional[Path], str]]:
        """复用批量重命名缓存生成 STRM，避免逐文件查询115。"""
        results: Dict[str, Tuple[Optional[Path], str]] = {}
        generated = 0
        queued_items: List[Dict[str, Any]] = []
        ready_items: List[Dict[str, Any]] = []
        for item in items:
            result_key = str(item["result_key"])
            if item.get("staging_dir"):
                queued_items.append(item)
                continue
            cloud_dir = item["cloud_dir"]
            file_name = item["file_name"]
            target_file = self._cloud_query.get_cached_file(
                cloud_dir, file_name
            )
            strm_path = self._generate_strm(
                cloud_dir,
                file_name,
                target_file=target_file,
                lookup_target=False,
                log_success=False,
            )
            ready_items.append(item)
            if strm_path:
                results[result_key] = (strm_path, "")
                generated += 1
            else:
                queued_items.append(item)
        self._scrape_metadata_batch(ready_items, mediainfo, season=season)
        pending_keys = self._queue_file_finalize_batch(
            queued_items,
            mediainfo,
            subscribe_id=subscribe_id,
            season=season,
            sub_key=sub_key,
        )
        for item in queued_items:
            result_key = str(item["result_key"])
            results[result_key] = (None, pending_keys.get(result_key, ""))
        if items:
            logger.debug(
                f"批量文件终态检查完成：即时生成 STRM {generated} 个，"
                f"待移动或文件就绪 {len(pending_keys)} 个"
            )
        return results

    def _finish_pending_subscription(
            self,
            item: Dict[str, Any],
            media_data: Dict[str, Any],
            mediainfo: Optional[MediaInfo] = None,
    ) -> None:
        """文件最终就绪后再更新订阅进度并执行完结。"""
        task_type = str(item.get("task_type") or "share").strip().lower()
        provider_name = str(
            getattr(self._cloud_drive, "name", "网盘") or "网盘"
        )
        completion_source = {
            "share": f"{provider_name}分享转存",
            "ed2k": "ED2K离线下载",
            "magnet": "Magnet离线下载",
        }.get(task_type, "文件后处理")
        subscribe_id = int(item.get("subscribe_id") or 0)
        episode_values = (
                item.get("success_episodes")
                or item.get("notification_episodes")
                or ([item.get("episode")] if item.get("episode") else [])
        )
        success_episodes = [
            int(episode)
            for episode in episode_values
            if int(episode) > 0
        ]
        if mediainfo is None and media_data:
            try:
                mediainfo = self._deserialize_mediainfo(media_data)
            except Exception as error:
                logger.warning(f"后处理订阅进度媒体信息恢复失败：{error}")
        if not mediainfo or not success_episodes:
            logger.warning(
                f"跳过后处理订阅进度更新：媒体信息={'有' if mediainfo else '无'}，"
                f"完成集数={success_episodes or '无'}"
            )
            return
        try:
            subscribe = None
            if subscribe_id:
                with SessionFactory() as db:
                    subscribe = SubscribeOper(db=db).get(subscribe_id)
            if not subscribe and mediainfo.tmdb_id:
                season = (
                    max(1, int(item.get("season") or 1))
                    if mediainfo.type == MediaType.TV else None
                )
                candidates = SubscribeOper().list_by_tmdbid(
                    mediainfo.tmdb_id, season
                ) or []
                subscribe = next(
                    (
                        candidate for candidate in candidates
                        if str(getattr(candidate, "type", "")) == mediainfo.type.value
                    ),
                    None,
                )
                if subscribe:
                    subscribe_id = int(subscribe.id)
                    item["subscribe_id"] = subscribe_id
                    logger.info(
                        f"后处理任务已重新关联订阅：{subscribe.name}，"
                        f"订阅ID={subscribe_id}"
                    )
            if not subscribe:
                logger.warning(
                    f"后处理完成时未找到对应订阅：订阅ID={subscribe_id or '无'}，"
                    f"TMDB={mediainfo.tmdb_id}，季={item.get('season') or '-'}"
                )
                return
            self._subscribe_handler.check_and_finish_subscribe(
                subscribe=subscribe,
                mediainfo=mediainfo,
                success_episodes=success_episodes,
            )
            with SessionFactory() as db:
                remaining_subscribe = SubscribeOper(db=db).get(subscribe_id)
            if remaining_subscribe:
                downloaded = {
                    int(episode)
                    for episode in (getattr(remaining_subscribe, "note", None) or [])
                    if str(episode).isdigit()
                }
                total_ep = int(
                    getattr(remaining_subscribe, "total_episode", 0) or 0
                )
                start_ep = int(
                    getattr(remaining_subscribe, "start_episode", 1) or 1
                )
                expected_count = max(0, total_ep - start_ep + 1)
                completed_count = len({
                    episode for episode in downloaded
                    if start_ep <= episode <= total_ep
                }) if expected_count else len(downloaded)
                progress = (
                    int(completed_count * 100 / expected_count)
                    if expected_count else 0
                )
                self._set_task_phase(
                    remaining_subscribe,
                    f"{completion_source}完成，订阅进度 "
                    f"{completed_count}/{expected_count or '-'}",
                    progress,
                )
                logger.debug(
                    f"{completion_source}完成后订阅进度已更新："
                    f"{remaining_subscribe.name}，"
                    f"已完成 {completed_count}/{expected_count or '-'}，"
                    f"缺失 {int(getattr(remaining_subscribe, 'lack_episode', 0) or 0)} 集"
                )
            else:
                self._set_task_phase(subscribe, "订阅已完成并移至历史", 100)
                logger.debug(
                    f"{completion_source}完成后订阅已完结并移至历史："
                    f"{subscribe.name}"
                )
            sub_key = str(item.get("sub_key") or "")
            should_clear_points = mediainfo.type == MediaType.MOVIE
            if mediainfo.type == MediaType.TV:
                total_ep = int(getattr(subscribe, "total_episode", 0) or 0)
                start_ep = int(getattr(subscribe, "start_episode", 1) or 1)
                if total_ep >= start_ep:
                    expected = set(range(start_ep, total_ep + 1))
                    downloaded = set(getattr(subscribe, "note", None) or [])
                    downloaded.update(success_episodes)
                    should_clear_points = not (expected - downloaded)
            if (
                    should_clear_points
                    and sub_key
                    and hasattr(self._search_handler, "clear_sub_points")
            ):
                self._search_handler.clear_sub_points(sub_key)
        except Exception as error:
            logger.error(f"文件后处理完成后更新订阅失败：{subscribe_id}，{error}")

    def _platform_classified_root(
            self,
            root_path: str,
            subscribe,
            mediainfo: MediaInfo,
    ) -> Optional[Path]:
        """缓存分类根目录，避免逐集重复执行相同目录规则。"""
        key = (
            str(root_path),
            getattr(mediainfo, "source", None),
            getattr(mediainfo, "media_id", None),
            getattr(mediainfo, "tmdb_id", None),
            getattr(mediainfo, "title", None),
            getattr(mediainfo, "year", None),
            getattr(mediainfo, "type", None),
            getattr(mediainfo, "category", None),
            getattr(subscribe, "id", None),
            getattr(subscribe, "media_category", None),
        )
        with self._platform_root_lock:
            if key in self._platform_root_cache:
                self._platform_root_cache.move_to_end(key)
                return self._platform_root_cache[key]

        directory = DirectoryHelper().get_dir(media=mediainfo, include_unsorted=True)
        resolved = None
        if directory:
            updates = {"library_path": root_path}
            if hasattr(directory, "model_copy"):
                target_directory = directory.model_copy(deep=True, update=updates)
            else:
                target_directory = directory.copy(deep=True, update=updates)
            classified_root = TransHandler().get_dest_dir(
                mediainfo=mediainfo,
                target_dir=target_directory,
            )
            if classified_root:
                resolved = Path(classified_root)

        with self._platform_root_lock:
            self._platform_root_cache[key] = resolved
            self._platform_root_cache.move_to_end(key)
            while len(self._platform_root_cache) > self._PLATFORM_ROOT_CACHE_LIMIT:
                self._platform_root_cache.popitem(last=False)
        return resolved

    def _platform_rename_path(
            self,
            root_path: str,
            subscribe,
            mediainfo: MediaInfo,
            source_name: str,
            season: int = None,
            episode: int = None,
    ) -> Optional[Path]:
        """使用当前分类目录和重命名模板生成完整目标路径。"""
        effective_media = self._effective_mediainfo(subscribe, mediainfo)
        classified_root = self._platform_classified_root(
            root_path, subscribe, effective_media
        )
        if not classified_root:
            return None
        meta = MetaInfo(source_name)
        meta.type = effective_media.type
        meta.year = getattr(subscribe, "year", None) or effective_media.year
        if season is not None:
            meta.begin_season = season
        if episode is not None:
            meta.begin_episode = episode
        relative_name = FileManagerModule.recommend_name(meta, effective_media)
        if not relative_name:
            return None
        return classified_root / Path(relative_name)

    def _platform_target(
            self,
            root_path: str,
            subscribe,
            mediainfo: MediaInfo,
            source_name: str,
            season: int = None,
            episode: int = None,
    ) -> Tuple[str, str]:
        """生成平台分类后的目标目录和规范文件名。"""
        target_path = self._platform_rename_path(
            root_path, subscribe, mediainfo, source_name, season, episode
        )
        if not target_path:
            raise ValueError(f"MoviePilot 未生成目标路径：{mediainfo.title_year}")
        return target_path.parent.as_posix(), target_path.name

    @staticmethod
    def _effective_mediainfo(subscribe, mediainfo: MediaInfo) -> MediaInfo:
        """使用订阅卡片的展示信息生成整理专用媒体副本。"""
        effective_media = copy.deepcopy(mediainfo)
        subscribe_title = str(getattr(subscribe, "name", "") or "").strip()
        if subscribe_title:
            effective_media.title = subscribe_title
        subscribe_year = getattr(subscribe, "year", None)
        if subscribe_year:
            effective_media.year = subscribe_year
        media_category = getattr(subscribe, "media_category", None)
        if media_category:
            effective_media.category = media_category
        return effective_media

    def _resolve_resource_season_dir(
            self,
            resource_root: str,
            subscribe,
            mediainfo: MediaInfo,
            season: int
    ) -> Optional[Path]:
        """使用的目录分类和命名规则生成媒体季目录。"""
        if not resource_root or not mediainfo:
            return None

        media_type = getattr(getattr(mediainfo, "type", None), "value", None)
        cache_key = (
            str(resource_root),
            media_type or str(getattr(mediainfo, "type", "") or ""),
            getattr(mediainfo, "source", None),
            getattr(mediainfo, "media_id", None),
            getattr(mediainfo, "tmdb_id", None),
            getattr(mediainfo, "title", None),
            getattr(mediainfo, "year", None),
            getattr(mediainfo, "category", None),
            getattr(subscribe, "id", None),
            getattr(subscribe, "name", None),
            getattr(subscribe, "year", None),
            getattr(subscribe, "media_category", None),
            int(season or 0),
        )
        with self._resource_season_dir_lock:
            if cache_key in self._resource_season_dir_cache:
                self._resource_season_dir_cache.move_to_end(cache_key)
                return self._resource_season_dir_cache[cache_key]
            resolved = None
            try:
                rename_path = self._platform_rename_path(
                    root_path=resource_root,
                    subscribe=subscribe,
                    mediainfo=mediainfo,
                    source_name=getattr(subscribe, "name", None) or mediainfo.title,
                    season=season,
                    episode=1,
                )
                if rename_path:
                    resolved = rename_path.parent
            except Exception as error:
                logger.warning(f"资源路径解析失败：{mediainfo.title_year}，{error}")
            self._resource_season_dir_cache[cache_key] = resolved
            self._resource_season_dir_cache.move_to_end(cache_key)
            while (
                    len(self._resource_season_dir_cache)
                    > self._RESOURCE_SEASON_DIR_CACHE_LIMIT
            ):
                self._resource_season_dir_cache.popitem(last=False)
            return resolved

    def _get_local_resource_files(
            self,
            subscribe,
            mediainfo: MediaInfo,
            season: int
    ) -> List[Path]:
        """获取平台规则生成的季目录中的本地或挂载媒体文件。"""
        season_dir = self._resolve_resource_season_dir(
            self._local_resource_path, subscribe, mediainfo, season
        )
        if not season_dir:
            return []
        if not season_dir.is_dir():
            logger.debug(f"资源季目录不存在，跳过扫描: {season_dir}")
            return []
        try:
            allowed_extensions = set(MediaFileParser.VIDEO_EXTENSIONS) | {".strm"}
            return [
                item for item in season_dir.iterdir()
                if item.is_file() and item.suffix.lower() in allowed_extensions
            ]
        except OSError as error:
            logger.warning(f"资源季目录读取失败 {season_dir}: {error}")
            return []

    def _scan_local_resource_episodes(
            self,
            subscribe,
            mediainfo: MediaInfo,
            season: int,
            start_episode: Optional[int] = None,
            total_episode: Optional[int] = None
    ) -> Set[int]:
        """按元数据解析器识别已落盘或已挂载的剧集。"""
        resource_files = self._get_local_resource_files(subscribe, mediainfo, season)
        found_episodes = self._parse_resource_episode_names(
            (resource_file.name for resource_file in resource_files),
            season=season,
            start_episode=start_episode,
            total_episode=total_episode,
        )

        if found_episodes:
            logger.info(
                f"媒体路径检查：{getattr(subscribe, 'name', '?')} S{season:02d} "
                f"识别到 {len(found_episodes)} 集"
            )
        return found_episodes

    @staticmethod
    def _parse_resource_episode_names(
            file_names,
            season: int,
            start_episode: Optional[int] = None,
            total_episode: Optional[int] = None,
    ) -> Set[int]:
        """使用元数据解析器从文件名提取目标季集数。"""
        found_episodes = set()
        for file_name in file_names:
            file_meta = MetaInfo(Path(str(file_name)).stem)
            file_season = file_meta.begin_season or season
            if file_season != season:
                continue
            episodes = list(getattr(file_meta, "episode_list", None) or [])
            if not episodes and file_meta.begin_episode:
                episodes = [file_meta.begin_episode]
            for episode in episodes:
                if start_episode is not None and episode < start_episode:
                    continue
                if total_episode and episode > total_episode:
                    continue
                found_episodes.add(int(episode))
        return found_episodes

    def _scan_cloud_resource_episode_files(
            self,
            subscribe,
            mediainfo: MediaInfo,
            season: int,
            start_episode: int,
            total_episode: int,
    ) -> Tuple[bool, Dict[int, CloudFile], str]:
        """一次读取目标季目录，返回真实存在的逐集网盘文件。"""
        cloud_dir = self._resolve_resource_season_dir(
            self._CLOUD_MEDIA_ROOT, subscribe, mediainfo, season
        )
        if not cloud_dir:
            return False, {}, ""
        cloud_path = cloud_dir.as_posix()
        lookup = self._cloud_directories.resolve_directory(cloud_path)
        if not lookup.checked:
            return False, {}, cloud_path
        if lookup.directory_id is None:
            return True, {}, cloud_path

        listing = self._cloud_directories.list_directory(lookup.directory_id)
        if not listing.checked:
            return False, {}, cloud_path
        episode_files: Dict[int, CloudFile] = {}
        for item in listing.files:
            if item.is_directory:
                continue
            name = item.name
            if not MediaFileParser.is_video(name):
                continue
            episodes = self._parse_resource_episode_names(
                [name], season, start_episode, total_episode
            )
            for episode in episodes:
                current = episode_files.get(episode)
                if not current:
                    episode_files[episode] = item
                    continue
                current_size = int(getattr(current, "size", 0) or 0)
                candidate_size = int(getattr(item, "size", 0) or 0)
                prefer_candidate = (
                    candidate_size < current_size
                    if self._upgrade_mode == "smallest"
                    else candidate_size > current_size
                )
                if prefer_candidate:
                    episode_files[episode] = item
        return True, episode_files, cloud_path

    def _scan_cloud_resource_episodes(
            self,
            subscribe,
            mediainfo: MediaInfo,
            season: int,
            start_episode: int,
            total_episode: int,
    ) -> Tuple[bool, Set[int], str]:
        """扫描平台规则生成的网盘季目录；目录不存在时不创建。"""
        valid, episode_files, cloud_path = self._scan_cloud_resource_episode_files(
            subscribe=subscribe,
            mediainfo=mediainfo,
            season=season,
            start_episode=start_episode,
            total_episode=total_episode,
        )
        label = f"115媒体路径 {cloud_path}" if cloud_path else ""
        return valid, set(episode_files), label

    def _find_cloud_movie_file(
            self,
            subscribe,
            mediainfo: MediaInfo,
    ) -> Optional[Tuple[str, str, CloudFile]]:
        """只检查平台规则生成的网盘电影目录，不递归扫描其他路径。"""
        try:
            cloud_dir, expected_name = self._platform_target(
                self._CLOUD_MEDIA_ROOT,
                subscribe,
                mediainfo,
                f"{getattr(subscribe, 'name', None) or mediainfo.title}.mkv",
            )
        except Exception as error:
            logger.warning(f"115电影目标路径计算失败：{mediainfo.title_year}，{error}")
            return None
        lookup = self._cloud_directories.resolve_directory(cloud_dir)
        if not lookup.checked or lookup.directory_id is None:
            return None
        expected_stem = Path(expected_name).stem
        listing = self._cloud_directories.list_directory(lookup.directory_id)
        if not listing.checked:
            return None
        for item in listing.files:
            if item.is_directory:
                continue
            name = item.name
            path = Path(name)
            if not MediaFileParser.is_video(name):
                continue
            if path.stem == expected_stem:
                return cloud_dir, name, item
        return None

    @staticmethod
    def _summarize_share_episodes(
            files: List[dict], season: int, mediainfo: Optional[MediaInfo] = None
    ) -> Tuple[int, Set[int]]:
        """递归统计分享中的实际视频数量和目标季集数。"""
        video_count = 0
        episodes = set()

        def walk(items: List[dict]):
            nonlocal video_count
            for item in items or []:
                if item.get("is_dir"):
                    walk(item.get("children") or [])
                    continue
                name = str(item.get("name") or "")
                if not MediaFileParser.is_video(name):
                    continue
                video_count += 1
                episode = FileMatcher.episode_from_file(item, season, mediainfo)
                if episode is not None:
                    episodes.add(episode)

        walk(files)
        return video_count, episodes

    @staticmethod
    def _format_episode_ranges(episodes: Set[int]) -> str:
        """把集数集合压缩为 E01-E03、E05 形式，避免日志刷屏。"""
        numbers = sorted({int(episode) for episode in episodes})
        if not numbers:
            return "无"
        ranges = []
        start = previous = numbers[0]
        for number in numbers[1:]:
            if number == previous + 1:
                previous = number
                continue
            ranges.append(f"E{start:02d}" if start == previous else f"E{start:02d}-E{previous:02d}")
            start = previous = number
        ranges.append(f"E{start:02d}" if start == previous else f"E{start:02d}-E{previous:02d}")
        return "、".join(ranges)

    @staticmethod
    def _resource_preview_episodes(resource: Dict[str, Any], season: int) -> Set[int]:
        """读取搜索阶段已取得的文件预览，不额外请求115接口。"""
        preview_episodes = resource.get("preview_episodes") or {}
        values = preview_episodes.get(str(season), preview_episodes.get(season, []))
        episodes = set()
        for value in values or []:
            try:
                episodes.add(int(value))
            except (TypeError, ValueError):
                continue
        return episodes

    @staticmethod
    def _resource_history_meta(resource: Dict[str, Any], share_url: str) -> Dict[str, Any]:
        source = str(resource.get("source") or "unknown").strip().lower()
        resource_type = str(
            resource.get("resource_type") or resource.get("pan_type") or ""
        ).strip().lower()
        if not resource_type:
            resource_type = "ed2k" if str(share_url).lower().startswith("ed2k://") else "115"
        points = resource.get("unlock_points")
        try:
            points = int(points) if points is not None else None
        except (TypeError, ValueError):
            points = None
        source_url = str(
            resource.get("source_url")
            or resource.get("page_url")
            or resource.get("detail_url")
            or share_url
            or ""
        ).strip()
        return {
            "resource_type": resource_type,
            "source": source,
            "source_url": source_url,
            "media_page_url": str(
                resource.get("media_page_url") or ""
            ).strip(),
            "points": points,
        }

    @staticmethod
    def _expand_resource_urls(
            resources: List[Dict[str, Any]],
            resource_index: int,
            resource: Dict[str, Any],
            value: Any,
    ) -> str:
        """展开列表或字符串中的多条离线链接，后续条目不重复计算积分。"""
        raw_values = value if isinstance(value, (list, tuple)) else [value]
        urls = []
        for raw_value in raw_values:
            text = str(raw_value or "").replace("｜", "|").strip()
            if not text:
                continue
            matches = list(SyncHandler._OFFLINE_RESOURCE_URL_RE.finditer(text))
            extracted = [match.group(0).strip() for match in matches]
            remainder = SyncHandler._OFFLINE_RESOURCE_URL_RE.sub("", text).strip()
            candidates = extracted if extracted and not remainder else [text]
            for url in candidates:
                if url not in urls:
                    urls.append(url)
        if not urls:
            return ""

        resource["url"] = urls[0]
        resource["need_unlock"] = False
        resource["need_access"] = False
        if len(urls) > 1:
            expanded = []
            for url in urls[1:]:
                item = copy.deepcopy(resource)
                item["url"] = url
                item["unlock_points"] = 0
                expanded.append(item)
            resources[resource_index + 1:resource_index + 1] = expanded
            source_name = str(resource.get("source") or "资源源").upper()
            logger.debug(
                f"{source_name} 同一资源包含 {len(urls)} 条链接，"
                "已展开处理且积分仅计一次"
            )
        return urls[0]

    def _resolve_candidate_resource_url(
            self,
            resources: List[Dict[str, Any]],
            resource_index: int,
            resource: Dict[str, Any],
            search_label: str,
            log_prefix: str = "",
    ) -> str:
        """统一处理积分搜索源的延迟解锁，并展开一次返回的多条链接。"""
        share_url = str(resource.get("url") or "").strip()
        if share_url:
            return self._expand_resource_urls(
                resources, resource_index, resource, share_url
            )
        if not (
                resource.get("need_unlock") or resource.get("need_access")
        ):
            return ""
        slug = str(resource.get("slug") or "").strip()
        if not slug:
            return ""
        try:
            unlock_points = int(resource.get("unlock_points") or 0)
        except (TypeError, ValueError):
            unlock_points = 0
        prefix = f"{log_prefix} " if log_prefix else ""
        resource_title = str(resource.get("title") or "").strip()
        source = str(resource.get("source") or "").strip().lower()
        is_dian115 = source == "dian115"
        has_budget = (
            self._search_handler.has_dian115_unlock_budget(unlock_points)
            if is_dian115
            else self._search_handler.has_hdhive_unlock_budget(unlock_points)
        )
        source_label = "Dian115" if is_dian115 else "HDHive"
        if not has_budget:
            logger.debug(
                f"{prefix}跳过 {source_label} 资源 {resource_title}："
                f"需要 {unlock_points} 积分，当前预算不足"
            )
            return ""
        action_label = "获取免费资源链接" if unlock_points <= 0 else "消耗积分解锁"
        media_page_url = str(resource.get("media_page_url") or "").strip()
        media_page_suffix = f"，媒体页：{media_page_url}" if media_page_url else ""
        logger.info(
            f"{prefix}遇到尚未取得链接的 {source_label} 资源 {resource_title} "
            f"(slug: {slug})，尝试{action_label}{media_page_suffix}"
        )
        if is_dian115:
            unlocked = self._search_handler.unlock_dian115_resource(
                int(resource.get("dian115_share_id") or slug),
                int(resource.get("dian115_resource_id") or 0),
                unlock_points,
                search_label=search_label,
                tmdb_id=int(resource.get("dian115_tmdb_id") or 0),
                media_type=str(resource.get("dian115_media_type") or ""),
                season=int(resource.get("dian115_season") or 0),
            )
        else:
            unlocked = self._search_handler.unlock_hdhive_resource(
                slug,
                unlock_points,
                resource.get("resource_type"),
                media_page_url=media_page_url,
                search_label=search_label,
            )
        if self._stop_requested() or not unlocked:
            if not self._stop_requested():
                logger.error(
                    f"{prefix}未能取得 {source_label} 资源链接：{resource_title}"
                )
            return ""
        return self._expand_resource_urls(
            resources, resource_index, resource, unlocked
        )

    def _validate_resource_url(
            self,
            share_url: str,
            resource_label: str = "分享链接",
            log_prefix: str = "",
    ) -> bool:
        """使用当前网盘 Provider 的统一能力校验资源链接。"""
        share_service = self._resource_provider_for_url(share_url).require(
            CloudDriveCapability.SHARE_TRANSFER
        ) if self._resource_provider_for_url(share_url) else self._share_transfer
        if not share_service:
            return False
        status = self._timed_sync_call(
            "share_validation",
            share_service.check_share_status,
            share_url,
        )
        if status.is_valid:
            return True
        prefix = f"{log_prefix} " if log_prefix else ""
        logger.debug(
            f"{prefix}{resource_label}无效："
            f"{self._resource_log_reference(share_url)}，原因：{status.status_text}"
        )
        return False

    def _validated_resource_files(
            self,
            share_url: str,
            resource_title: str = "",
            target_season: Optional[int] = None,
            log_prefix: str = "",
    ) -> List[Dict[str, Any]]:
        """校验分享并读取文件列表，供电影、剧集和洗版共同使用。"""
        if not self._validate_resource_url(
                share_url, resource_label="分享链接", log_prefix=log_prefix
        ):
            return []
        kwargs = {"target_season": target_season} if target_season is not None else {}
        provider = self._resource_provider_for_url(share_url)
        share_service = provider.require(CloudDriveCapability.SHARE_TRANSFER) if provider else self._share_transfer
        if not share_service:
            return []
        files = self._timed_sync_call(
            "share_listing",
            share_service.list_share_files,
            share_url,
            **kwargs,
        ) or []
        files = list(MediaFileParser.iter_files(files))
        if not files:
            label = resource_title or self._resource_log_reference(share_url)
            logger.debug(f"{log_prefix + ' ' if log_prefix else ''}分享链接无内容：{label}")
        return list(files)

    def _transfer_history_status(self, success: bool, share_url: str) -> str:
        if not success:
            return "失败"
        return "下载中" if self._is_offline_url(share_url) else "成功"

    @staticmethod
    def _supported_resource_type(resource: Dict[str, Any], share_url: str) -> str:
        resource_type = str(
            resource.get("resource_type") or resource.get("pan_type") or ""
        ).strip().lower()
        if resource_type:
            return resource_type
        normalized_url = str(share_url).lstrip().lower()
        if normalized_url.startswith("ed2k://"):
            return "ed2k"
        if normalized_url.startswith("magnet:?"):
            return "magnet"
        for marker, value in (
                ("quark", "quark"), ("189.cn", "tianyi"),
                ("cloud.189", "tianyi"), ("guangya", "guangya"),
                ("123pan", "123"), ("123.cn", "123"),
                ("123684.com", "123"), ("123865.com", "123"),
                ("alipan.com", "alipan"), ("aliyundrive.com", "alipan"),
        ):
            if marker in normalized_url:
                return value
        return "115"

    def _resource_provider_for_url(self, share_url: str) -> Optional[CloudDriveProvider]:
        if not self._cloud_drive_registry:
            return self._cloud_drive
        key = self._supported_resource_type({}, share_url)
        aliases = {
            "189": "tianyi", "aliyun": "alipan"
        }
        try:
            return self._cloud_drive_registry.get(aliases.get(key, key))
        except KeyError:
            return self._cloud_drive if key == "115" else None

    @staticmethod
    def _cloud_file_from_dict(item: Dict[str, Any]) -> CloudFile:
        return CloudFile(
            id=str(item.get("id") or ""), name=str(item.get("name") or ""),
            is_directory=False, size=int(item.get("size") or 0),
            sha1=str(item.get("sha1") or ""), md5=str(item.get("md5") or ""),
            native=item,
        )

    @staticmethod
    def _normalize_cloud_path(path: str) -> str:
        return str(PurePosixPath("/" + str(path or "/").strip().lstrip("/")))

    def _cross_transfer_staging_path(self, provider_key: str) -> str:
        base_path = self._cloud_transfer_paths.get(
            str(provider_key or "").strip().lower(), "/"
        )
        # 直接复用已配置的转存目录，不为跨盘任务创建额外目录。
        return str(PurePosixPath(base_path))

    @staticmethod
    def _cleanup_cross_transfer_staging(
            source: CloudDriveProvider, staged_path: str,
            item: Optional[CloudFile] = None,
    ) -> None:
        if not source.supports(CloudDriveCapability.FILE_MUTATION):
            return
        mutation = source.require(CloudDriveCapability.FILE_MUTATION)
        if staged_path and source.supports(CloudDriveCapability.DIRECTORY_READ):
            try:
                lookup = source.require(
                    CloudDriveCapability.DIRECTORY_READ
                ).resolve_directory(staged_path)
                if lookup.checked and lookup.directory_id is not None:
                    if mutation.delete_file(lookup.directory_id):
                        return
            except Exception as error:
                logger.warning(f"清理源盘跨盘临时目录失败：{error}")
        if item:
            try:
                mutation.delete_file(item.id)
            except Exception as error:
                logger.warning(f"清理源盘跨盘临时文件失败：{error}")

    def _transfer_file(
            self, share_url: str, file_item: Dict[str, Any], save_path: str,
            target_name: str, source_sha1: str = "",
            parent_task_id: str = "",
            stop_requested: Optional[Callable[[], bool]] = None,
            media_type: str = "",
    ) -> bool:
        should_stop = stop_requested or self._stop_requested
        source = self._resource_provider_for_url(share_url)
        cross_provider = bool(
            source and self._cloud_drive and source.key != self._cloud_drive.key
        )
        if cross_provider:
            item_media_type = str(file_item.get("media_type") or media_type or "").strip().lower()
            if item_media_type and item_media_type not in self._cross_transfer_media_types:
                return False
            required = (
                    self._cross_transfer_enabled
                    and self._cross_transfer_manager
                    and source.supports(CloudDriveCapability.SHARE_TRANSFER)
                    and source.supports(CloudDriveCapability.FILE_QUERY)
                    and source.supports(CloudDriveCapability.FILE_DOWNLOAD)
                    and self._cloud_drive.supports(CloudDriveCapability.LOCAL_UPLOAD)
            )
            if not required:
                logger.warning(
                    f"无法跨盘转存单个文件：{source.name} -> "
                    f"{self._cloud_drive.name}，请检查跨盘开关和网盘能力"
                )
                return False
            source_file_id = str(file_item.get("id") or "").strip()
            if not source_file_id:
                logger.warning(f"无法跨盘转存单个文件：{source.name} 文件 ID 为空")
                return False
            staged_path = self._cross_transfer_staging_path(source.key)
            source_share = source.require(CloudDriveCapability.SHARE_TRANSFER)
            try:
                staged = source_share.transfer_file(
                    share_url=share_url, file_id=source_file_id,
                    save_path=staged_path,
                    target_name=file_item.get("name") or target_name,
                )
            except Exception:
                self._cleanup_cross_transfer_staging(source, "")
                raise
            if not staged:
                self._cleanup_cross_transfer_staging(source, "")
                return False
            source_files = source.require(CloudDriveCapability.FILE_QUERY)
            staged_name = file_item.get("name") or target_name
            item = None
            for attempt in range(10):
                item = source_files.find_file(staged_path, staged_name)
                if item or should_stop():
                    break
                time.sleep(min(0.5 + attempt * 0.25, 2.0))
            if not item:
                logger.warning(
                    f"跨盘临时文件尚未可见：{source.name} "
                    f"{staged_path}/{staged_name}"
                )
                self._cleanup_cross_transfer_staging(source, "")
                return False
            if source_sha1 and not item.sha1:
                item = CloudFile(item.id, item.name, False, item.size, source_sha1, item.md5, native=item.native)
            try:
                if not parent_task_id:
                    parent_task_id, _ = self._current_task_context()
                task = self._cross_transfer_manager.create_from_cloud_file(
                    source.key, item, self._cloud_drive.key, save_path, target_name,
                    fallback=True,
                    parent_task_id=parent_task_id,
                )
                success = self._cross_transfer_manager.wait(
                    task["id"], cancel_check=should_stop
                )
                completed_task = next(
                    (
                        value for value in self._cross_transfer_manager.list()
                        if value.get("id") == task["id"]
                    ),
                    {},
                )
                if success:
                    result_name = str(
                        completed_task.get("result_file_name")
                        or target_name or item.name
                    ).strip()
                    if result_name:
                        file_item["staging_name"] = result_name
                    result_sha1 = str(
                        completed_task.get("result_sha1") or ""
                    ).strip()
                    result_md5 = str(
                        completed_task.get("result_md5") or ""
                    ).strip()
                    if result_sha1:
                        file_item["sha1"] = result_sha1
                    if result_md5:
                        file_item["md5"] = result_md5
                    result_size = int(
                        completed_task.get("result_file_size") or 0
                    )
                    if result_size > 0:
                        file_item["size"] = result_size
                if not success:
                    if (
                            should_stop()
                            or completed_task.get("status") in {"canceled", "stopping"}
                    ):
                        logger.info(
                            f"跨盘转存已由用户停止：{source.name} -> "
                            f"{self._cloud_drive.name}"
                        )
                    else:
                        logger.error(
                            f"跨盘转存失败：{source.name} -> {self._cloud_drive.name}，"
                            f"阶段={completed_task.get('phase') or 'unknown'}，"
                            f"原因={completed_task.get('error') or completed_task.get('message') or '未知错误'}"
                        )
                return success
            finally:
                # 只清理暂存文件，保留稳定目录供后续任务复用。
                self._cleanup_cross_transfer_staging(source, "", item)
        service = source.require(CloudDriveCapability.SHARE_TRANSFER) if source else self._share_transfer
        return bool(service.transfer_file(
            share_url=share_url, file_id=file_item.get("id"),
            save_path=save_path, target_name=target_name,
            source_sha1=source_sha1,
        ))

    def _is_supported_resource(self, resource: Dict[str, Any], share_url: str) -> bool:
        if not self._cloud_drive:
            return False
        resource_type = self._supported_resource_type(resource, share_url)
        if not self._cloud_drive.supports_resource_type(resource_type):
            if not self._cross_transfer_enabled:
                return False
            source = self._resource_provider_for_url(share_url)
            if not source or not source.supports(CloudDriveCapability.SHARE_TRANSFER):
                return False
            if not source.supports(CloudDriveCapability.FILE_DOWNLOAD):
                return False
            if not self._cloud_drive.supports(CloudDriveCapability.LOCAL_UPLOAD):
                return False
        if resource_type in {"ed2k", "magnet"}:
            return self._offline_download is not None
        source = self._resource_provider_for_url(share_url)
        return bool(source and source.supports(CloudDriveCapability.SHARE_TRANSFER))

    @classmethod
    def _format_resource_summary(cls, resources: List[Dict[str, Any]]) -> str:
        labels = {"share": "网盘分享", "ed2k": "ED2K", "magnet": "Magnet"}
        summary_counts: Dict[str, Dict[str, int]] = {}
        seen = set()
        for resource in resources or []:
            resource_type = cls._supported_resource_type(
                resource, str(resource.get("url") or "")
            )
            label = labels.get(resource_type, resource_type.upper() or "未知")
            identity = str(
                resource.get("unlock_group") or resource.get("source_url")
                or resource.get("url") or resource.get("title") or ""
            )
            key = (resource_type, identity)
            if key in seen:
                continue
            seen.add(key)
            counts = summary_counts.setdefault(
                label, {"total": 0, "available": 0, "paid": 0, "official": 0}
            )
            counts["total"] += 1
            if resource.get("is_official"):
                counts["official"] += 1
            if resource.get("need_unlock"):
                counts["paid"] += 1
            else:
                counts["available"] += 1
        summaries = []
        for label, counts in summary_counts.items():
            statuses = [f"可用 {counts['available']}"]
            if counts["paid"]:
                statuses.append(f"待解锁 {counts['paid']}")
            if counts["official"]:
                statuses.append(f"官组 {counts['official']}")
            summaries.append(
                f"{label} {counts['total']}（{'，'.join(statuses)}）"
            )
        return f"共 {len(seen)} 个资源页：" + "；".join(summaries)

    @staticmethod
    def _reconcile_subscribe_physical_episodes(
            subscribe,
            episodes: Set[int],
            start_episode: int,
            total_episode: int,
    ) -> Dict[str, Any]:
        """以 Emby 与115实际数据纠正订阅进度，包括移除误标集数。"""
        expected = set(range(start_episode, total_episode + 1))
        verified = {int(episode) for episode in episodes} & expected
        current = {
            int(episode) for episode in (subscribe.note or [])
            if str(episode).isdigit()
        }
        new_note = sorted(verified)
        new_lack = len(expected - verified)
        update_data = {}
        if current != verified:
            update_data["note"] = new_note
        if int(subscribe.lack_episode or 0) != new_lack:
            update_data["lack_episode"] = new_lack
        if update_data:
            SubscribeOper().update(subscribe.id, update_data)
            subscribe.note = new_note
            subscribe.lack_episode = new_lack
        return {
            "added": sorted(verified - current),
            "removed": sorted(current - verified),
            "missing": sorted(expected - verified),
            "updated": bool(update_data),
        }

    def send_transfer_notification(self, transfer_details: List[Dict[str, Any]], total_count: int):
        """按普通转存、跨盘转存和洗版分别发送完成通知。"""
        if not transfer_details or not self._post_message:
            return
        kind_config = {
            "transfer": ("【网盘订阅助手】转存完成", "转存"),
            "cross_transfer": ("【网盘订阅助手】跨盘转存完成", "跨盘转存"),
            "upgrade": ("【网盘洗版】洗版完成", "洗版"),
        }
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for detail in transfer_details:
            kind = str(detail.get("notification_kind") or "transfer")
            grouped.setdefault(kind if kind in kind_config else "transfer", []).append(detail)

        for kind, details in grouped.items():
            text_lines = []
            first_image = None
            file_count = 0
            for detail in details:
                if detail.get("type") == "电影":
                    title = detail.get("title", "未知")
                    year = detail.get("year", "")
                    text_lines.append(f"{title} ({year})")
                    file_count += 1
                else:
                    title = detail.get("title", "未知")
                    season = max(1, int(detail.get("season") or 1))
                    episodes = sorted(detail.get("episodes") or [])
                    file_count += len(episodes)
                    if len(episodes) <= 5:
                        ep_str = ", ".join(f"E{episode:02d}" for episode in episodes)
                    else:
                        ep_str = (
                            f"E{episodes[0]:02d}-E{episodes[-1]:02d} "
                            f"共{len(episodes)}集"
                        )
                    text_lines.append(f"{title} S{season:02d} {ep_str}")
                if not first_image and detail.get("image"):
                    first_image = detail.get("image")
            if len(text_lines) > 10:
                text_lines = text_lines[:10]
                text_lines.append(f"... 等共 {len(details)} 项")
            notification_title, action = kind_config[kind]
            self._post_message(
                mtype=self._notification_type,
                title=notification_title,
                text=f"本次共{action} {file_count} 个文件\n\n" + "\n".join(text_lines),
                image=first_image,
            )
            logger.info(
                f"{action}完成通知已发送：{file_count} 个文件，"
                f"{len(details)} 个媒体项"
            )

    def guardian_check(self, all_subs) -> int:
        """
        集数守护 & 日历修复：扫描媒体库 strm 文件，同步订阅 note/lack_episode。

        修复场景：
        - PT bypass、115直搜、洗版模式等非标准路径下载后 note 未更新
        - 日历显示"未入库"但文件实际已在媒体库中
        - 订阅进度与实际文件不一致

        :param all_subs: 所有订阅列表（SubscribeOper().list() 结果）
        :return: 本次完成的订阅数（新增的 lack_episode=0 的个数）
        """
        from app.db.subscribe_oper import SubscribeOper
        from app.schemas.types import MediaType

        completed_count = 0

        for subscribe in all_subs:
            try:
                # 只处理活跃的电视剧订阅
                if getattr(subscribe, 'state', None) == 'D':
                    continue
                sub_type = getattr(subscribe, 'type', None)
                if sub_type != MediaType.TV.value:
                    continue

                season = subscribe.season or 1
                total_ep = subscribe.total_episode or 0
                start_ep = subscribe.start_episode or 1

                if total_ep <= 0:
                    continue

                meta = MetaInfo(subscribe.name)
                meta.year = subscribe.year
                meta.begin_season = season
                meta.type = MediaType.TV
                mediainfo = self._recognize_media_once(
                    (
                        "guardian", MediaType.TV.value,
                        getattr(subscribe, 'tmdbid', None),
                        getattr(subscribe, 'doubanid', None), subscribe.name,
                        subscribe.year, season, True,
                    ),
                    meta=meta,
                    mtype=MediaType.TV,
                    tmdbid=getattr(subscribe, 'tmdbid', None),
                    doubanid=getattr(subscribe, 'doubanid', None),
                    cache=True,
                )
                if not mediainfo:
                    continue

                found_episodes = self._scan_local_resource_episodes(
                    subscribe=subscribe,
                    mediainfo=mediainfo,
                    season=season,
                    start_episode=start_ep,
                    total_episode=total_ep,
                )
                if not found_episodes:
                    continue

                remaining_lack = self._subscribe_handler.check_and_finish_subscribe(
                    subscribe=subscribe,
                    mediainfo=mediainfo,
                    success_episodes=sorted(found_episodes),
                )
                if remaining_lack == 0:
                    completed_count += 1

            except Exception as e:
                logger.warning(f"订阅完结检查异常 {getattr(subscribe, 'name', '?')}：{e}")
                import traceback
                logger.debug(traceback.format_exc())

        return completed_count
