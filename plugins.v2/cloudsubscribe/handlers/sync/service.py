"""
同步处理模块
负责核心的同步逻辑：处理电影订阅、处理电视剧订阅
"""
import copy
import hashlib
import re
import threading
import time
import traceback
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from typing import List, Dict, Any, Set, Optional, Callable, Tuple, Mapping, Iterable

from app.core.config import global_vars
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper

try:
    from app.helper.directory import DirectoryHelper
except Exception:
    try:
        from app.application.directory import DirectoryHelper
    except Exception:
        DirectoryHelper = None

from app.log import logger
from app.modules.filemanager import FileManagerModule
from app.modules.filemanager.transhandler import TransHandler
from app.schemas.types import MediaType, NotificationType

try:
    from app.utils.http import RequestUtils
except Exception:
    try:
        from app.adapters.network.http import RequestUtils
    except Exception:
        RequestUtils = None

from .baseline import UpgradeBaselineService
from .cleanup import HistoryCleanupService
from .history import HistoryService
from .matching import FileMatchingService
from .metadata import SyncMetadataService
from .movie import MovieSyncProcessor
from .naming import SyncNamingService
from .notify import SyncNotificationService
from .platform_history import PlatformHistoryService
from .postprocess import PostprocessService
from .pt_upgrade import PtUpgradeService
from .resources import ResourceTransferService
from .retry import HistoryRetryService
from .rule_scoring import UpgradeRuleScoringService
from .subtitles import SubtitleService
from .television import TelevisionSyncProcessor
from .upgrade import UpgradeService
from .utils import extract_ed2k_filename, format_episode_ranges
from ..notification import MediaServerNotifier, MediaServerResolver
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
from ...core.media import (
    legacy_media_ids,
    list_subscribes_by_tmdb_id,
    tmdb_id_of,
)
from ...drive.scanner import get_driver_definitions
from ...utils import StrmGenerator, StrmTemplateError
from ...utils.cache import create_platform_ttl_cache

_COMPONENT_TYPES = (
    MovieSyncProcessor,
    TelevisionSyncProcessor,
    HistoryService,
    HistoryCleanupService,
    HistoryRetryService,
    FileMatchingService,
    PostprocessService,
    UpgradeBaselineService,
    UpgradeRuleScoringService,
    ResourceTransferService,
    SubtitleService,
    UpgradeService,
    PtUpgradeService,
    SyncMetadataService,
    SyncNamingService,
    SyncNotificationService,
    PlatformHistoryService,
)


class SyncHandler:
    """同步处理器"""

    _OFFLINE_PENDING_KEY = "pending_offline_strm"
    _OFFLINE_CHECK_DELAYS = (10, 20, 40, 60, 120, 300)
    _OFFLINE_TIMEOUT = 30 * 60
    _FILE_FINALIZE_TIMEOUT = 30 * 60
    _OFFLINE_MONITOR_LEASE_SECONDS = 15 * 60
    _MEDIA_RECOGNITION_CACHE_LIMIT = 256
    _PLATFORM_ROOT_CACHE_LIMIT = 256
    _RESOURCE_SEASON_DIR_CACHE_LIMIT = 256
    _SUBSCRIBE_DEFER_CACHE_LIMIT = 512
    _SUBSCRIBE_CALENDAR_CACHE_LIMIT = 512
    _RUNTIME_CACHE_TTL = 6 * 60 * 60
    _SUBSCRIBE_DEFER_CACHE_TTL = 32 * 24 * 60 * 60
    _SUBSCRIBE_CALENDAR_CACHE_TTL = 26 * 60 * 60
    _NOTIFICATION_BATCH_WINDOW_SECONDS = 2
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
            plugin: Optional[Any] = None,
            cloud_drive: Optional[CloudDriveProvider] = None,
            search_handler: Optional[SearchHandler] = None,
            subscribe_handler: Optional[SubscribeHandler] = None,
            chain: Any = None,
            cloud_transfer_path: Optional[str] = None,
            cloud_media_root: Optional[str] = None,
            cloud_transfer_paths: Optional[Mapping[str, str]] = None,
            cloud_media_paths: Optional[Mapping[str, str]] = None,
            **kwargs,
    ):
        """
        初始化同步处理器。

        :param plugin: 宿主插件实例（如 CloudSubscribe），提供全局配置与平台能力回调
        :param cloud_drive: 当前网盘提供方（可选，默认从 plugin 获取）
        :param search_handler: 搜索处理器（可选，默认从 plugin 获取）
        :param subscribe_handler: 订阅处理器（可选，默认从 plugin 获取）
        :param chain: MediaChain 实例（可选，默认从 plugin 获取）
        :param cloud_transfer_path: 当前网盘转存暂存路径（可选）
        :param cloud_media_root: 当前网盘媒体库分类根目录（可选）
        :param cloud_transfer_paths: 各网盘提供方的转存暂存路径映射（可选）
        :param cloud_media_paths: 各网盘提供方的媒体库路径映射（可选）
        """
        self._plugin = plugin

        def _get_val(name: str, default: Any = None) -> Any:
            if name in kwargs and kwargs[name] is not None:
                return kwargs[name]
            if plugin is not None:
                if hasattr(plugin, f"_{name}"):
                    val = getattr(plugin, f"_{name}")
                    if val is not None:
                        return val
                if hasattr(plugin, name):
                    val = getattr(plugin, name)
                    if val is not None:
                        return val
            return default

        self._cloud_drive = (
            cloud_drive
            if cloud_drive is not None
            else getattr(plugin, "_cloud_drive", None) if plugin else None
        )
        self._search_handler = (
            search_handler
            if search_handler is not None
            else getattr(plugin, "_search_handler", None) if plugin else None
        )
        self._subscribe_handler = (
            subscribe_handler
            if subscribe_handler is not None
            else getattr(plugin, "_subscribe_handler", None) if plugin else None
        )
        self._chain = (
            chain
            if chain is not None
            else getattr(plugin, "chain", None) if plugin else None
        )

        self._organize_after_transfer = bool(_get_val("organize_after_transfer", True))
        self._organize_subtitles = bool(_get_val("organize_subtitles", True))
        self._subtitle_traditional_to_simplified = bool(
            _get_val("subtitle_traditional_to_simplified", False)
        )
        self._anime_pack_preferred = bool(_get_val("anime_pack_preferred", True))
        offline_timeout = _get_val("offline_timeout", 30)
        self._offline_timeout = max(10, min(int(offline_timeout or 30), 1440)) * 60
        self._OFFLINE_TIMEOUT = self._offline_timeout
        self._cross_transfer_enabled = bool(_get_val("cross_transfer_enabled", False))
        cross_media_types = _get_val("cross_transfer_media_types", ("movie", "tv"))
        self._cross_transfer_media_types = {
            self._normalize_cross_transfer_media_type(value)
            for value in (cross_media_types or ("movie", "tv"))
        }
        self._cloud_drive_registry = _get_val(
            "cloud_drive_registry",
            getattr(plugin, "_cloud_drive_registry", None) if plugin else None
        )
        self._cross_transfer_manager = _get_val(
            "cross_transfer_manager",
            getattr(plugin, "_cross_transfer_manager", None) if plugin else None
        )

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

        transfer_task_batch_size = _get_val("transfer_task_batch_size", 50)
        self._transfer_task_batch_size = max(
            1, min(int(transfer_task_batch_size or 50), 1000)
        )
        policy = self._cloud_drive.policy if self._cloud_drive else None
        configured_batch_size = max(1, int(_get_val("batch_size", 20) or 1))
        self._batch_size = min(
            configured_batch_size,
            policy.max_batch_size if policy and policy.supports_batch else configured_batch_size,
        )
        self._batch_interval = max(0.0, min(float(_get_val("batch_interval", 3) or 0), 60.0))
        transfer_risk_cooldown = _get_val("transfer_risk_cooldown", 1800)
        self._transfer_risk_cooldown = max(
            60, min(int(transfer_risk_cooldown or 1800), 86400)
        )
        self._share_transfer_risk_lock = threading.Lock()
        self._share_transfer_blocked_until: Dict[str, float] = {}
        self._offline_blacklist = create_platform_ttl_cache(
            "offline_blacklist",
            ttl=86400,
            maxsize=10000,
        )
        self._skip_other_season_dirs = bool(_get_val("skip_other_season_dirs", True))
        self._notify = bool(_get_val("notify", False))
        self._notification_type = _get_val("notification_type", NotificationType.Plugin)

        self._post_message = _get_val(
            "post_message_func",
            getattr(plugin, "post_message", None) if plugin else None
        )
        self._get_data = _get_val(
            "get_data_func",
            getattr(plugin, "get_data", None) if plugin else None
        )
        self._save_data = _get_val(
            "save_data_func",
            getattr(plugin, "save_data", None) if plugin else None
        )
        self._should_stop = _get_val(
            "should_stop",
            getattr(plugin, "_stop_requested", None) if plugin else None
        )
        self._offline_pending_changed = _get_val(
            "offline_pending_changed",
            getattr(plugin, "_update_offline_monitor", None) if plugin else None
        )
        self._history_changed = _get_val(
            "history_changed",
            getattr(plugin, "_mark_history_changed", None) if plugin else None
        )
        self._file_finalized = _get_val(
            "file_finalized",
            getattr(plugin, "_on_file_finalized", None) if plugin else None
        )
        self._task_update = _get_val(
            "task_update",
            getattr(plugin, "_update_sync_task", None) if plugin else None
        )
        self._postprocess_task_update = _get_val(
            "postprocess_task_update",
            getattr(plugin, "_update_postprocess_task", None) if plugin else None
        )
        self._task_context = _get_val(
            "task_context",
            getattr(plugin, "_current_task_context", None) if plugin else None
        )

        self._self_heal_interval = _get_val("self_heal_interval", 10)
        self._enable_cloud_upgrade = bool(_get_val("enable_cloud_upgrade", False))
        self._enable_pt_upgrade = bool(_get_val("enable_pt_upgrade", False))
        if self._enable_pt_upgrade and not self._cloud_upload:
            logger.warning("PT洗版已启用，但当前网盘不支持本地文件上传")
        upgrade_sub_ids = _get_val("upgrade_subscribe_ids", None)
        self._upgrade_subscribe_ids = list(upgrade_sub_ids or [])
        self._upgrade_subscribe_id_set = {
            str(value) for value in self._upgrade_subscribe_ids
        }
        raw_mode = str(_get_val("upgrade_mode", "largest") or "largest").strip().lower()
        self._upgrade_mode = (
            raw_mode if raw_mode in {"coexist", "replace", "largest", "smallest"} else "largest"
        )
        self._local_resource_path = str(_get_val("local_resource_path", "") or "").strip()

        paths = cloud_transfer_paths if cloud_transfer_paths is not None else self._configured_transfer_paths(plugin)
        self._cloud_transfer_paths = {
            str(key).strip().lower(): self._normalize_cloud_path(value)
            for key, value in dict(paths or {}).items()
            if str(key).strip()
        }

        media_paths = cloud_media_paths if cloud_media_paths is not None else self._configured_media_paths(plugin)
        self._cloud_media_paths = {
            str(key).strip().lower(): self._normalize_cloud_path(value)
            for key, value in dict(media_paths or {}).items()
            if str(key).strip()
        }

        # 优先从各网盘专属路径映射中取当前网盘路径，兼容显式参数传入
        drive_key = getattr(self._cloud_drive, "key", "") if self._cloud_drive else ""
        raw_transfer_path = (
                cloud_transfer_path
                or (self._cloud_transfer_paths.get(drive_key) if drive_key else None)
                or "/"
        )
        self._cloud_transfer_path = (
                str(raw_transfer_path or "/").strip().rstrip("/") or "/"
        )
        raw_media_root = (
                cloud_media_root
                or (self._cloud_media_paths.get(drive_key) if drive_key else None)
                or "/"
        )
        self._CLOUD_MEDIA_ROOT = self._normalize_cloud_path(raw_media_root)

        if self._cloud_drive:
            self._cloud_transfer_paths.setdefault(
                self._cloud_drive.key, self._cloud_transfer_path
            )
            self._cloud_media_paths.setdefault(
                self._cloud_drive.key, self._CLOUD_MEDIA_ROOT
            )

        self._strm_generate_enabled = bool(_get_val("strm_generate_enabled", True))
        self._nfo_scrape_enabled = bool(_get_val("nfo_scrape_enabled", False))
        self._image_scrape_enabled = bool(_get_val("image_scrape_enabled", False))
        self._platform_transfer_history_enabled = bool(
            _get_val("platform_transfer_history_enabled", False)
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
                    s_base_url = _get_val("strm_base_url", StrmGenerator.DEFAULT_BASE_URL)
                    s_template = _get_val("strm_url_template", StrmGenerator.DEFAULT_TEMPLATE)
                    self._strm_generator = StrmGenerator(
                        s_base_url,
                        s_template,
                        provider_variables=self._playback_reference.template_variables,
                    )
                except StrmTemplateError as error:
                    logger.error(f"STRM 生成配置无效，已停止直接生成：{error}")

        media_server_refresh_enabled = bool(_get_val("media_server_refresh_enabled", False))
        media_servers = _get_val("media_servers", None)
        media_server_path_mappings = _get_val("media_server_path_mappings", "")
        media_server_refresh_delay = _get_val("media_server_refresh_delay", 0)
        emby_mediainfo_enabled = bool(_get_val("emby_mediainfo_enabled", False))

        self._media_server_notifier = MediaServerNotifier(
            enabled=media_server_refresh_enabled,
            mediaservers=media_servers,
            path_mappings=media_server_path_mappings,
            delay_seconds=media_server_refresh_delay,
            emby_mediainfo_enabled=emby_mediainfo_enabled,
        )
        self._notification_delay_seconds = max(
            0, int(media_server_refresh_delay or 0)
        )
        self._notification_batch_lock = threading.RLock()
        self._notification_batch: List[Dict[str, Any]] = []
        self._notification_batch_timer: Optional[threading.Timer] = None
        self._media_server_resolver = MediaServerResolver()
        MediaServerResolver.configure(media_servers)
        self._offline_pending_lock = threading.RLock()
        self._pt_upgrade_lock = threading.RLock()
        self._pt_upgrade_active = set()
        self._platform_history_lock = threading.RLock()
        self._sync_metrics_lock = threading.RLock()
        self._sync_metrics: Dict[str, Dict[str, int]] = {}
        self._media_recognition_lock = threading.RLock()
        self._platform_media_recognition_lock = threading.Lock()
        self._media_recognition_cache = create_platform_ttl_cache(
            "sync:media_recognition", self,
            maxsize=self._MEDIA_RECOGNITION_CACHE_LIMIT,
            ttl=self._RUNTIME_CACHE_TTL,
        )
        self._media_recognition_inflight: Dict[Tuple[Any, ...], Future] = {}
        self._resource_season_dir_lock = threading.RLock()
        self._resource_season_dir_cache = create_platform_ttl_cache(
            "sync:resource_season_dirs", self,
            maxsize=self._RESOURCE_SEASON_DIR_CACHE_LIMIT,
            ttl=self._RUNTIME_CACHE_TTL,
        )
        self._platform_root_lock = threading.RLock()
        self._platform_root_cache = create_platform_ttl_cache(
            "sync:platform_roots", self,
            maxsize=self._PLATFORM_ROOT_CACHE_LIMIT,
            ttl=self._RUNTIME_CACHE_TTL,
        )
        self._subscribe_defer_lock = threading.RLock()
        self._subscribe_defer_cache = create_platform_ttl_cache(
            "sync:subscribe_defer", self,
            maxsize=self._SUBSCRIBE_DEFER_CACHE_LIMIT,
            ttl=self._SUBSCRIBE_DEFER_CACHE_TTL,
        )
        self._subscribe_calendar_cache = create_platform_ttl_cache(
            "sync:subscribe_calendar", self,
            maxsize=self._SUBSCRIBE_CALENDAR_CACHE_LIMIT,
            ttl=self._SUBSCRIBE_CALENDAR_CACHE_TTL,
        )
        self._baseline_cache_lock = threading.RLock()
        self._baseline_transfer_cache = create_platform_ttl_cache(
            "sync:baseline_transfer", self, maxsize=256,
            ttl=self._RUNTIME_CACHE_TTL,
        )
        self._baseline_plugin_cache = create_platform_ttl_cache(
            "sync:baseline_plugin", self, maxsize=256,
            ttl=self._RUNTIME_CACHE_TTL,
        )
        self._baseline_media_server_cache = create_platform_ttl_cache(
            "sync:baseline_media_server", self, maxsize=256,
            ttl=self._RUNTIME_CACHE_TTL,
        )

    def append_history_records(
            self,
            records: List[Dict[str, Any]],
            reopen_terminal: bool = False,
    ) -> int:
        """写入历史后通知运行态订阅者，避免前端等待整批任务结束。"""
        count = self._get_component(HistoryService).append_history_records(
            records, reopen_terminal=reopen_terminal
        )
        if count and self._history_changed:
            self._history_changed()
        return count

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

    @staticmethod
    def subscription_budget_key(
            subscribe: Any, media_type: Optional[MediaType] = None
    ) -> str:
        """生成普通转存和洗版共用的订阅积分键。"""
        resolved_type = media_type or {
            MediaType.MOVIE.value: MediaType.MOVIE,
            MediaType.TV.value: MediaType.TV,
        }.get(str(getattr(subscribe, "type", "") or ""))
        tmdb_id = str(tmdb_id_of(subscribe) or "")
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
            baseline_media_server = len(self._baseline_media_server_cache)
            self._baseline_transfer_cache.clear()
            self._baseline_plugin_cache.clear()
            self._baseline_media_server_cache.clear()
        return {
            "media_recognition": media_recognition,
            "resource_season_dirs": resource_season_dirs,
            "platform_roots": platform_roots,
            "subscribe_defer": subscribe_defer,
            "subscribe_calendar": subscribe_calendar,
            "baseline_transfer": baseline_transfer,
            "baseline_plugin": baseline_plugin,
            "baseline_media_server": baseline_media_server,
        }

    def reset_sync_metrics(self) -> None:
        with self._sync_metrics_lock:
            self._sync_metrics = {}
        self.clear_runtime_cache()

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
        """提交尚未发送的完成通知并释放通知定时器。"""
        self._flush_transfer_notifications()
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
        self._notification_delay_seconds = max(
            0, int(media_server_refresh_delay or 0)
        )
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
            logger.debug(f"读取停止状态失败：{err}")
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
            f"{key} 分享转存检测到风控，冷却 {self._transfer_risk_cooldown} 秒"
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
            transient_target: bool = False,
    ) -> List[Dict[str, Any]]:
        """在同一任务内拆批完成全部剧集转存。"""
        items = list(matched_items or [])
        if not items:
            return []
        batch_size = self._transfer_task_batch_size
        batch_count = (len(items) + batch_size - 1) // batch_size
        if batch_count > 1:
            logger.info(
                f"匹配 {len(items)} 个文件，将按每批最多 {batch_size} 个"
                f"分 {batch_count} 批在当前任务内处理"
            )
        results = []
        for batch_index, offset in enumerate(range(0, len(items), batch_size), 1):
            if self._stop_requested():
                break
            batch_items = items[offset:offset + batch_size]
            if batch_count > 1:
                logger.debug(
                    f"开始处理转存批次 {batch_index}/{batch_count}："
                    f"文件={len(batch_items)}"
                )
                self._set_task_phase(
                    subscribe,
                    f"正在转存第 {batch_index}/{batch_count} 批剧集",
                    90 + int((batch_index / batch_count) * 4),
                )
            batch_results = self._transfer_episode_batch(
                batch_items,
                share_url,
                mediainfo,
                subscribe,
                season,
                sub_key,
                track_subscription=track_subscription,
                transient_target=transient_target,
            )
            results.extend(batch_results)
            if self._stop_requested():
                break
            if batch_index < batch_count and not self._wait_transfer_batch_interval():
                break
        return results

    def _wait_transfer_batch_interval(self) -> bool:
        """在任务内批次之间等待，并允许停止请求及时中断。"""
        deadline = time.monotonic() + self._batch_interval
        while time.monotonic() < deadline:
            if self._stop_requested():
                return False
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        return True

    def _transfer_episode_batch(
            self,
            matched_items: List[Dict[str, Any]],
            share_url: str,
            mediainfo: MediaInfo,
            subscribe,
            season: int,
            sub_key: str,
            track_subscription: bool = True,
            transient_target: bool = False,
    ) -> List[Dict[str, Any]]:
        """执行一个剧集转存批次及对应后处理。"""
        selected_items = list(matched_items or [])
        if not selected_items:
            return []
        if self._stop_requested():
            return []

        file_ids = [str(item["file"]["id"]) for item in selected_items]
        cloud_resource = self._is_cloud_resource_url(share_url)
        direct_cloud_resource = (
                cloud_resource and self._is_direct_cloud_resource_url(share_url)
        )
        organize_enabled = getattr(self, "_organize_after_transfer", True)
        rename_items = {}
        for item in selected_items:
            file_item = item["file"]
            item_url = str(file_item.get("url") or share_url).strip()
            rename_items[str(file_item["id"])] = {
                "sha1": file_item.get("sha1"),
                "target_name": (
                    None if (not organize_enabled or self._is_offline_url(item_url)) else item["target_name"]
                ),
                "url": item_url,
            }
        try:
            source_provider = self._resource_provider_for_url(share_url)
            provider_key = getattr(source_provider, "key", "") or getattr(
                self._cloud_drive, "key", "default"
            )
            if not cloud_resource:
                self._ensure_share_transfer_available(provider_key)
            # 手动资源标记保存在匹配项的 resource 元数据中；这里不能引用不存在的 resources。
            is_manual_cross = any(
                bool(
                    (item.get("resource") or {}).get("source") == "manual"
                    or (item.get("resource") or {}).get("is_cross")
                    or (item.get("resource") or {}).get("_manual")
                    or item.get("source") == "manual"
                    or item.get("is_cross")
                    or item.get("_manual")
                )
                for item in selected_items
            )
            cross_batch = bool(
                (self._cross_transfer_enabled or is_manual_cross) and source_provider
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
                        item_staging_dir = self._resource_staging_dir(
                            str(item["file"].get("url") or share_url), item["file"]
                        )
                        save_target_name = (
                            item["target_name"] if organize_enabled else item["file"].get("name")
                        )
                        success = self._transfer_file(
                            str(item["file"].get("url") or share_url),
                            item["file"],
                            item_staging_dir if not organize_enabled else self._cloud_transfer_path,
                            save_target_name,
                            str(item["file"].get("sha1") or ""),
                            parent_task_id=parent_task_id,
                            stop_requested=batch_stop_requested,
                            media_type=getattr(getattr(mediainfo, "type", None), "name", ""),
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
            elif direct_cloud_resource:
                processed_items = selected_items
                success_ids = file_ids
            else:
                processed_items = selected_items
                pre_existing_ids = set()
                share_transferred = False
                if not organize_enabled and hasattr(self._share_transfer, "transfer_share"):
                    try:
                        logger.info(
                            f"转存后整理已关闭，执行整包分享转存以保留母文件夹与目录结构：{share_url} -> {self._cloud_transfer_path}"
                        )
                        share_success = bool(self._timed_sync_call(
                            "share_transfer",
                            self._share_transfer.transfer_share,
                            share_url=share_url,
                            save_path=self._cloud_transfer_path,
                        ))
                        if share_success:
                            success_ids = list(file_ids)
                            failed_ids = []
                            share_transferred = True
                    except Exception as share_err:
                        logger.warning(f"整包分享转存未成功，将尝试按文件列表转存：{share_err}")

                if not share_transferred:
                    # 转存前预检转存目录：若转存路径下已存在待转存文件，直接复用并跳过向网盘发起重复转存，防止网盘报错或重复转存卡死
                    staging_valid, staging_index = self._cloud_directory_snapshot(self._cloud_transfer_path)
                    if staging_valid and staging_index:
                        for item in selected_items:
                            file_id = str(item["file"]["id"])
                            target_name = str(item.get("target_name") or "").strip()
                            raw_name = str(item["file"].get("name") or "").strip()
                            file_size = int(item["file"].get("size") or 0)
                            file_sha1 = str(item["file"].get("sha1") or "").upper()
                            matched_staging_file = None
                            if target_name and target_name in staging_index:
                                matched_staging_file = staging_index[target_name]
                            elif raw_name and raw_name in staging_index:
                                matched_staging_file = staging_index[raw_name]
                            elif file_sha1:
                                matched_staging_file = next(
                                    (f for f in staging_index.values() if
                                     str(getattr(f, "sha1", "") or "").upper() == file_sha1),
                                    None
                                )
                            elif file_size > 0:
                                matched_staging_file = next(
                                    (f for f in staging_index.values() if
                                     int(getattr(f, "size", 0) or 0) == file_size and (
                                             target_name and getattr(f, "name", "").startswith(Path(target_name).stem)
                                             or raw_name and getattr(f, "name", "").startswith(Path(raw_name).stem)
                                     )),
                                    None
                                )
                            if matched_staging_file:
                                pre_existing_ids.add(file_id)
                                item["file"]["staging_name"] = matched_staging_file.name
                                logger.debug(
                                    f"转存目录已存在目标资源，复用并跳过重复转存：{self._cloud_transfer_path}/{matched_staging_file.name}"
                                )

                    remaining_file_ids = [fid for fid in file_ids if fid not in pre_existing_ids]
                    if remaining_file_ids:
                        success_ids, failed_ids = self._timed_sync_call(
                            "share_transfer",
                            self._share_transfer.transfer_files_batch,
                            share_url=share_url,
                            file_ids=remaining_file_ids,
                            save_path=self._cloud_transfer_path,
                            batch_size=self._batch_size,
                            batch_interval=self._batch_interval,
                            risk_cooldown=self._transfer_risk_cooldown,
                            rename_items=rename_items,
                        )
                    else:
                        success_ids, failed_ids = [], []

                    success_ids = list(success_ids or []) + list(pre_existing_ids)

                    # 对网盘转存返回失败的项进行转存目录复核自愈（防止网盘因已存在报错等返回假失败）
                    if failed_ids:
                        _, post_staging_index = self._cloud_directory_snapshot(self._cloud_transfer_path)
                        recheck_success = []
                        for fid in failed_ids:
                            target_item = next((it for it in selected_items if str(it["file"]["id"]) == fid), None)
                            if not target_item:
                                continue
                            t_name = str(target_item.get("target_name") or "")
                            r_name = str(target_item["file"].get("name") or "")
                            if (t_name and t_name in post_staging_index) or (r_name and r_name in post_staging_index):
                                recheck_success.append(fid)
                                logger.debug(f"转存虽返回失败但转存目录已核验到文件，自愈恢复：{t_name or r_name}")
                        if recheck_success:
                            success_ids.extend(recheck_success)
                            recheck_set = set(recheck_success)
                            failed_ids = [fid for fid in failed_ids if fid not in recheck_set]

                if (
                        failed_ids and not success_ids
                        and bool(getattr(
                    self._share_transfer, "transfer_risk_blocked", False
                ))
                ):
                    self._activate_share_transfer_cooldown(provider_key)
        except Exception as error:
            message = str(error)
            if any(marker in message.lower() for marker in (
                    "风控", "封禁", "受限", "频繁", "rate limit", "too many", "429",
            )):
                self._activate_share_transfer_cooldown(locals().get("provider_key", "default"))
            raise

        success_id_set = {str(file_id) for file_id in (success_ids or [])}
        transferred_subtitle_ids = set()
        for item in processed_items:
            if str(item["file"]["id"]) not in success_id_set:
                continue
            subtitle_files = item.get("subtitle_files") or []
            item["subtitles"] = self._transfer_companion_subtitles(
                share_url=str(item["file"].get("url") or share_url),
                files=[item["file"], *subtitle_files],
                video_file=item["file"],
                target_video_name=item["target_name"],
                media_type="tv",
                season=season,
                episode=item.get("episode"),
                transferred_ids=transferred_subtitle_ids,
            ) if subtitle_files else []
        batch_strm_results = self._generate_or_queue_strm_batch(
            [
                {
                    "result_key": str(item["file"]["id"]),
                    "share_url": str(item["file"].get("url") or share_url),
                    "cloud_dir": item["target_dir"],
                    "file_name": item["target_name"],
                    "staging_dir": self._resource_staging_dir(
                        str(item["file"].get("url") or share_url), item["file"]
                    ),
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
                    "subtitles": item.get("subtitles") or [],
                    "skip_history": bool(
                        (item.get("resource") or {}).get("skip_history")
                    ),
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
            transient_target=transient_target,
            target_subscribe=(
                self._serialize_pending_target_subscribe(subscribe)
                if transient_target else None
            ),
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
        return results

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
            scrape_items = self._metadata_scraper.filter_missing_items([{
                "media_path": media_path,
                "season": season,
                "episode": episode,
            }], mediainfo)
            if not scrape_items:
                logger.debug(f"跳过元数据刮削：{media_path.parent}，NFO 和图片均已存在")
                return media_path
            logger.info(
                f"开始元数据刮削：{media_path}，"
                f"NFO={'是' if self._nfo_scrape_enabled else '否'}，"
                f"图片={'是' if self._image_scrape_enabled else '否'}"
            )
            created = self._metadata_scraper.scrape_batch(scrape_items, mediainfo)
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
            scrape_items = self._metadata_scraper.filter_missing_items(
                scrape_items, mediainfo
            )
            if not scrape_items:
                logger.debug(
                    f"跳过批量元数据刮削：{mediainfo.title_year}，"
                    "NFO 和图片均已存在"
                )
                return
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
        text = str(share_url or "").strip()
        if not (self._is_magnet_url(text) or self._is_ed2k_url(text)):
            return ""
        match = re.search(r"\|([0-9A-Fa-f]{32})(?:\|[^|]*)*\|/$", text)
        if match:
            return match.group(1).upper()
        if self._offline_download:
            try:
                magnet = self._offline_download.parse_magnet_link(text)
                if (magnet or {}).get("hash"):
                    return str(magnet.get("hash") or "").upper()
            except Exception:
                pass
        return hashlib.sha1(text.encode("utf-8")).hexdigest().upper() if text else ""

    def _resource_log_reference(self, share_url: str) -> str:
        """Magnet 日志仅展示 infoHash，避免输出完整 Tracker 参数。"""
        if self._is_magnet_url(share_url):
            return f"infoHash={self._offline_hash(share_url) or '未知'}"
        return str(share_url or "")

    def _add_offline_blacklist(self, key_or_url: str, reason: str = "") -> None:
        """将失败或超时的离线任务/资源加入黑名单（1天TTL）。"""
        if not key_or_url or not hasattr(self, "_offline_blacklist"):
            return
        keys = set()
        text = str(key_or_url).strip()
        if text:
            keys.add(text)
        info_hash = self._offline_hash(text) if hasattr(self, "_offline_hash") else ""
        if info_hash:
            keys.add(info_hash.upper())
        now = time.time()
        for k in keys:
            if k:
                self._offline_blacklist[k] = {
                    "reason": reason,
                    "time": now,
                }
        logger.info(f"🚫 离线资源已加入黑名单（1天过期）：{info_hash or text}，原因：{reason}")

    def _remove_offline_blacklist(self, key_or_url: str) -> None:
        """从黑名单中移除指定的离线任务。"""
        if not key_or_url or not hasattr(self, "_offline_blacklist"):
            return
        keys = [str(key_or_url).strip()]
        info_hash = self._offline_hash(key_or_url) if hasattr(self, "_offline_hash") else ""
        if info_hash:
            keys.append(info_hash.upper())
        for k in keys:
            if k and k in self._offline_blacklist:
                try:
                    del self._offline_blacklist[k]
                except Exception:
                    pass

    def _is_offline_blacklisted(self, resource: Optional[Dict[str, Any]] = None, share_url: str = "") -> bool:
        """检查离线资源是否在黑名单中。"""
        if not hasattr(self, "_offline_blacklist"):
            return False
        url = share_url or (str((resource or {}).get("url") or (resource or {}).get("link") or "") if resource else "")
        info_hash = self._offline_hash(url) if (url and hasattr(self, "_offline_hash")) else ""
        if info_hash and info_hash in self._offline_blacklist:
            return True
        if url and url in self._offline_blacklist:
            return True
        if resource:
            res_hash = str(
                resource.get("info_hash") or resource.get("hash") or (resource.get("magnet_metadata") or {}).get(
                    "hash") or "").upper()
            if res_hash and res_hash in self._offline_blacklist:
                return True
        return False

    @classmethod
    def _is_same_media_target(
            cls,
            item: Dict[str, Any],
            mediainfo: Optional[Any] = None,
            subscribe: Optional[Any] = None,
            subscribe_id: Optional[Any] = None,
    ) -> bool:
        """多维度判断 pending 中的任务与当前候选是否属于同一媒体。"""
        # 1. 优先比对权威 TMDB ID（若双方都有，不一致则绝对不是同一媒体，一致则判定为同一媒体）
        item_tmdb_id = (
                (item.get("mediainfo") or {}).get("tmdb_id")
                or (item.get("target_subscribe") or {}).get("tmdbid")
                or tmdb_id_of(item.get("target_subscribe"))
        )
        target_tmdb_id = (
                getattr(mediainfo, "tmdb_id", None)
                or (tmdb_id_of(subscribe) if subscribe else None)
                or (mediainfo.get("tmdb_id") if isinstance(mediainfo, dict) else None)
        )
        try:
            if item_tmdb_id and target_tmdb_id and int(item_tmdb_id) > 0 and int(target_tmdb_id) > 0:
                if int(item_tmdb_id) != int(target_tmdb_id):
                    return False
                return True
        except (ValueError, TypeError):
            pass

        # 2. 比对豆瓣 ID
        item_douban_id = (
                (item.get("mediainfo") or {}).get("douban_id")
                or (item.get("target_subscribe") or {}).get("doubanid")
        )
        target_douban_id = (
                getattr(mediainfo, "douban_id", None)
                or (legacy_media_ids(subscribe).get("doubanid") if subscribe else None)
                or (mediainfo.get("douban_id") if isinstance(mediainfo, dict) else None)
        )
        if item_douban_id and target_douban_id:
            s_item_douban = str(item_douban_id).strip()
            s_target_douban = str(target_douban_id).strip()
            if s_item_douban and s_target_douban:
                if s_item_douban != s_target_douban:
                    return False
                return True

        # 3. 比对 subscribe_id
        target_sid = str(subscribe_id or (getattr(subscribe, "id", None) if subscribe else "") or "").strip()
        item_sid = str(item.get("subscribe_id") or "").strip()
        if target_sid and target_sid != "0" and item_sid and item_sid != "0":
            if target_sid == item_sid:
                return True

        # 4. 提取并比对规范化标题 + 年份
        item_media = item.get("mediainfo") or {}
        item_title = str(
            item_media.get("title") or (item.get("target_subscribe") or {}).get("name") or "").strip().lower()
        target_title = str(getattr(mediainfo, "title", None) or getattr(subscribe, "name", None) or (
            mediainfo.get("title") if isinstance(mediainfo, dict) else "") or "").strip().lower()
        if item_title and target_title and item_title == target_title:
            item_year = str(item_media.get("year") or (item.get("target_subscribe") or {}).get("year") or "").strip()
            target_year = str(getattr(mediainfo, "year", None) or getattr(subscribe, "year", None) or (
                mediainfo.get("year") if isinstance(mediainfo, dict) else "") or "").strip()
            if not item_year or not target_year or item_year == target_year:
                return True

        return False

    @classmethod
    def _unreserved_episodes(
            cls,
            pending: Dict[str, Any],
            subscribe_id: Any = None,
            season: Any = None,
            targets: Iterable[int] = (),
            mediainfo: Optional[Any] = None,
            subscribe: Optional[Any] = None,
    ) -> List[int]:
        """过滤掉当前已有未完成离线任务的目标集数，防止并发重复提交下载。"""
        reserved: Set[int] = set()
        target_season = str(season or "").strip()
        for item in (pending or {}).values():
            item_season = str(item.get("season") or "").strip()
            if item_season == target_season and not item.get("upgrade"):
                if cls._is_same_media_target(item, mediainfo=mediainfo, subscribe=subscribe, subscribe_id=subscribe_id):
                    for value in item.get("target_episodes") or []:
                        try:
                            num = int(value)
                            if num > 0:
                                reserved.add(num)
                        except (ValueError, TypeError):
                            continue
        return sorted(set(targets or []) - reserved)

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
            submit_queue: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """提交离线下载（Magnet/ED2K/直链离线等）到隔离目录；下载完成后再按真实文件树匹配。"""
        info_hash = self._offline_hash(share_url)
        magnet_title = self._prepare_magnet_resource(resource, share_url)
        metadata = resource.get("magnet_metadata") or {}
        title_seasons = self._magnet_title_seasons(resource)
        if season is not None and title_seasons and int(season) not in title_seasons:
            logger.debug(
                "离线资源标题预过滤排除，未请求远端内容元数据："
                f"标题季数={','.join(f'S{value:02d}' for value in sorted(title_seasons))}，"
                f"目标季数=S{int(season):02d}，标题={magnet_title or info_hash}"
            )
            return ""
        title_episodes = self._magnet_title_episodes(
            resource, int(season or 1)
        )
        preview_episodes = (
                title_episodes
                or self._resource_preview_episodes(resource, int(season or 1))
        )
        target_episode_set = {
            int(value) for value in (target_episodes or []) if int(value) > 0
        }
        if season is not None and target_episode_set and title_episodes:
            confirmed_targets = target_episode_set & title_episodes
            if not confirmed_targets:
                logger.debug(
                    "离线资源标题预过滤排除，未请求远端内容元数据："
                    f"标题集数={self._format_episode_ranges(title_episodes)}，"
                    f"目标集数={self._format_episode_ranges(target_episode_set)}，"
                    f"标题={magnet_title or info_hash}"
                )
                return ""
            target_episodes[:] = sorted(confirmed_targets)
            logger.debug(
                "离线资源标题预过滤命中，跳过远端内容元数据获取："
                f"标题集数={self._format_episode_ranges(title_episodes)}，"
                f"目标集数={self._format_episode_ranges(target_episode_set)}，"
                f"标题={magnet_title or info_hash}"
            )
        if (
                self._is_magnet_url(share_url)
                and not title_episodes
                and not preview_episodes
                and not metadata.get("torrent_files")
        ):
            logger.debug(
                "Magnet 标题未识别明确集数，开始获取远端内容元数据："
                f"{magnet_title or info_hash}"
            )
            magnet_info = self._offline_download.parse_magnet_link(
                share_url, fetch_metadata=True
            )
            fetched_metadata = (magnet_info or {}).get("metadata") or {}
            if fetched_metadata:
                metadata = {
                    **metadata,
                    **{
                        key: value for key, value in fetched_metadata.items()
                        if value not in (None, "", [], {})
                    },
                }
                resource["magnet_metadata"] = metadata
                magnet_title = self._prepare_magnet_resource(resource, share_url)
                title_seasons = self._magnet_title_seasons(resource)
                if (
                        season is not None
                        and title_seasons
                        and int(season) not in title_seasons
                ):
                    logger.debug(
                        "Magnet 远端内容元数据季数不匹配，已跳过："
                        f"内容季数={','.join(f'S{value:02d}' for value in sorted(title_seasons))}，"
                        f"目标季数=S{int(season):02d}，标题={magnet_title or info_hash}"
                    )
                    return ""
                title_episodes = self._magnet_title_episodes(
                    resource, int(season or 1)
                )
                metadata_preview = metadata.get("preview_episodes") or {}
                if metadata_preview:
                    resource["preview_episodes"] = metadata_preview
                preview_episodes = (
                        title_episodes
                        or self._resource_preview_episodes(
                    resource, int(season or 1)
                )
                )
        if (
                not info_hash
                or not self._get_data
                or (
                self._is_magnet_url(share_url)
                and not bool(metadata.get("metadata_available"))
                and not bool(title_episodes)
                and not bool(preview_episodes)
        )
        ):
            logger.debug(
                "离线资源标题和元数据均未提供可确认内容，已跳过："
                f"{magnet_title or info_hash}"
            )
            return ""
        if season is not None and target_episodes and preview_episodes:
            target_episode_set = {
                int(value) for value in target_episodes if int(value) > 0
            }
            confirmed_targets = target_episode_set & preview_episodes
            if not confirmed_targets:
                logger.debug(
                    "离线资源内容确认未覆盖目标集数，已跳过网盘离线下载候选："
                    f"内容集数={self._format_episode_ranges(preview_episodes)}，"
                    f"目标集数={self._format_episode_ranges(target_episode_set)}，"
                    f"标题={magnet_title or info_hash}"
                )
                return ""
            target_episodes[:] = sorted(confirmed_targets)
        subscribe_id = int(getattr(subscribe, "id", 0) or 0)
        prefix = "magnet" if self._is_magnet_url(share_url) else "ed2k"
        pending_key = f"{prefix}:{info_hash}:{subscribe_id}"
        staging_dir = f"{self._cloud_transfer_path.rstrip('/')}"

        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            existing = pending.get(pending_key)
            if existing and existing.get("status") != "submitting":
                return pending_key
            if season and target_episodes and not upgrade:
                target_episodes[:] = self._unreserved_episodes(
                    pending,
                    subscribe_id=subscribe_id,
                    season=season,
                    targets=target_episodes,
                    mediainfo=mediainfo,
                    subscribe=subscribe,
                )
                if not target_episodes:
                    logger.debug(f"跳过同集重复离线：S{int(season):02d}")
                    return ""

            # 写入占位（In-Flight 状态，防止并发穿透同集提交）
            pending[pending_key] = {
                "pending_key": pending_key,
                "task_type": prefix,
                "status": "submitting",
                "subscribe_id": subscribe_id,
                "season": season,
                "target_episodes": sorted({int(value) for value in target_episodes if int(value) > 0}),
                "mediainfo": self._serialize_mediainfo(mediainfo),
                "target_subscribe": {
                    "tmdbid": tmdb_id_of(subscribe) if subscribe else None,
                    "name": str(getattr(subscribe, "name", "") or ""),
                } if subscribe else {},
                "created_at": time.time(),
            }
            self._save_offline_pending(pending)

        submit_context = {
            "pending_key": pending_key,
            "prefix": prefix,
            "info_hash": info_hash,
            "share_url": share_url,
            "staging_dir": staging_dir,
            "resource": resource,
            "subscribe": subscribe,
            "mediainfo": mediainfo,
            "subscribe_id": subscribe_id,
            "season": season,
            "target_episodes": list(target_episodes or []),
            "sub_key": sub_key,
            "upgrade": upgrade,
            "upgrade_mode": upgrade_mode,
            "upgrade_baseline": upgrade_baseline,
            "transient_target": transient_target,
        }
        if submit_queue is not None:
            submit_queue.append(submit_context)
            return pending_key
        successful = self._submit_offline_packages([submit_context])
        return pending_key if pending_key in successful else ""

    def _complete_offline_submission(self, context: Dict[str, Any]) -> None:
        """将已由网盘接受的离线任务从提交占位转为正式待下载记录。"""
        pending_key = str(context["pending_key"])
        prefix = str(context["prefix"])
        info_hash = str(context["info_hash"])
        share_url = str(context["share_url"])
        staging_dir = str(context["staging_dir"])
        resource = context["resource"]
        subscribe = context["subscribe"]
        mediainfo = context["mediainfo"]
        subscribe_id = int(context["subscribe_id"] or 0)
        season = context.get("season")
        target_episodes = list(context.get("target_episodes") or [])
        sub_key = str(context.get("sub_key") or "")
        upgrade = bool(context.get("upgrade"))
        upgrade_mode = str(context.get("upgrade_mode") or "")
        upgrade_baseline = context.get("upgrade_baseline") or {}
        transient_target = bool(context.get("transient_target"))
        now = time.time()
        ed2k_file_name = extract_ed2k_filename(share_url)
        if not ed2k_file_name and isinstance(resource.get("file_list"), list) and resource["file_list"]:
            ed2k_file_name = str(resource["file_list"][0]).strip()
        display_name = str(
            ed2k_file_name
            or (resource.get("magnet_metadata") or {}).get("display_name")
            or resource.get("title") or info_hash
        )
        target_dir = ""
        target_name = ""
        if getattr(self, "_organize_after_transfer", True) and prefix != "magnet" and mediainfo:
            try:
                if mediainfo.type == MediaType.TV:
                    target_ep = target_episodes[0] if target_episodes and len(target_episodes) == 1 else None
                    if target_ep is not None and season is not None:
                        target_dir, target_name = self._platform_target(
                            self._CLOUD_MEDIA_ROOT, subscribe, mediainfo,
                            display_name, int(season), int(target_ep)
                        )
                else:
                    target_dir, target_name = self._platform_target(
                        self._CLOUD_MEDIA_ROOT, subscribe, mediainfo,
                        display_name
                    )
            except Exception as target_err:
                logger.debug(f"计算离线任务目标路径失败，将保留原始目录：{target_err}")
        if target_name:
            source_suffix = Path(display_name).suffix
            if source_suffix and not target_name.endswith(source_suffix):
                target_name = f"{Path(target_name).stem}{source_suffix}"

        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            pending[pending_key] = {
                "pending_key": pending_key,
                "task_type": prefix,
                "task_id": info_hash,
                "share_url": share_url,
                "staging_dir": staging_dir,
                "cloud_dir": target_dir or staging_dir,
                "file_name": target_name or display_name,
                "staging_name": display_name,
                "episode": target_episodes[0] if target_episodes and len(target_episodes) == 1 else None,
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
                "notification_episodes": sorted({
                    int(value) for value in (target_episodes or []) if int(value) > 0
                }),
                "resource": dict(resource),
                "sub_key": str(sub_key or ""),
                "upgrade": bool(upgrade),
                "upgrade_mode": str(upgrade_mode or self._upgrade_mode),
                "upgrade_baseline": dict(upgrade_baseline or {}),
                "transient_target": bool(transient_target),
                "target_subscribe": (
                    self._serialize_pending_target_subscribe(subscribe)
                    if transient_target else {}
                ),
            }
            self._save_offline_pending(pending)
            pending_count = len(pending)
        self._notify_offline_pending_changed(pending_count)
        logger.info(
            f"离线任务已提交：{pending[pending_key].get('staging_name') or pending[pending_key]['file_name']}"
        )

    def _rollback_offline_submission(self, context: Dict[str, Any]) -> None:
        pending_key = str(context.get("pending_key") or "")
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            if pending_key in pending and pending[pending_key].get("status") == "submitting":
                pending.pop(pending_key, None)
                self._save_offline_pending(pending)
        self._add_offline_blacklist(
            str(context.get("share_url") or ""), "提交离线下载失败"
        )

    def _submit_offline_packages(
            self, contexts: List[Dict[str, Any]]
    ) -> set[str]:
        """同目录任务优先一次批量提交，并逐项完成或回滚占位记录。"""
        contexts = [item for item in contexts if item.get("pending_key")]
        if not contexts:
            return set()
        successful_hashes: set[str] = set()
        batch_submit = getattr(
            self._offline_download, "add_offline_downloads_batch", None
        )
        same_directory = len({str(item["staging_dir"]) for item in contexts}) == 1
        if callable(batch_submit) and same_directory and len(contexts) > 1:
            try:
                success_values, _ = batch_submit(
                    [
                        {"url": item["share_url"]}
                        for item in contexts
                    ],
                    save_path=str(contexts[0]["staging_dir"]),
                    batch_size=min(20, len(contexts)),
                )
                successful_hashes = {
                    re.sub(r"[^0-9A-Fa-f]", "", str(value or "")).upper()
                    for value in success_values
                }
            except Exception as error:
                logger.error(f"批量提交网盘离线下载接口异常：{error}")
        else:
            for item in contexts:
                try:
                    if self._offline_download.add_offline_download(
                            item["share_url"], item["staging_dir"]
                    ):
                        successful_hashes.add(str(item["info_hash"]).upper())
                except Exception as error:
                    logger.error(f"提交网盘离线下载接口异常：{error}")

        successful_keys: set[str] = set()
        for item in contexts:
            normalized_hash = re.sub(
                r"[^0-9A-Fa-f]", "", str(item.get("info_hash") or "")
            ).upper()
            if normalized_hash in successful_hashes:
                self._complete_offline_submission(item)
                successful_keys.add(str(item["pending_key"]))
            else:
                self._rollback_offline_submission(item)
        return successful_keys

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
            logger.debug(f"更新网盘文件后处理监控状态失败：{error}")

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

    def _pending_identity(
            self,
            share_url: str,
            cloud_dir: str,
            file_name: str,
            source_identity: str,
    ) -> Tuple[str, str, str]:
        """统一计算后处理键，避免单项和批量流程产生不同规则。"""
        info_hash = self._offline_hash(share_url)
        if info_hash:
            return (
                info_hash,
                "magnet" if self._is_magnet_url(share_url) else "ed2k",
                info_hash,
            )
        path_digest = hashlib.sha1(
            f"{str(cloud_dir or '').rstrip('/')}/{file_name}".encode("utf-8")
        ).hexdigest()[:12]
        provider_key = str(
            getattr(self._cloud_drive, "key", "cloud") or "cloud"
        )
        cloud_resource = self._is_cloud_resource_url(share_url)
        task_type = (
            "cloud"
            if cloud_resource and self._is_direct_cloud_resource_url(share_url)
            else "cross_cloud"
            if cloud_resource
            else "share"
        )
        return (
            f"{provider_key}:{source_identity}:{path_digest}",
            task_type,
            "",
        )

    @staticmethod
    def _serialize_pending_target_subscribe(subscribe: Any) -> Dict[str, Any]:
        return {
            "name": str(getattr(subscribe, "name", "") or ""),
            "year": getattr(subscribe, "year", None),
            "type": str(getattr(subscribe, "type", "") or ""),
            "tmdbid": tmdb_id_of(subscribe),
            "doubanid": legacy_media_ids(subscribe).get("doubanid"),
            "season": getattr(subscribe, "season", None),
            "start_episode": getattr(subscribe, "start_episode", None),
            "total_episode": getattr(subscribe, "total_episode", None),
            "media_category": getattr(subscribe, "media_category", None),
            "episode_group": getattr(subscribe, "episode_group", None),
            "filter_groups": getattr(subscribe, "filter_groups", None),
            "best_version": bool(getattr(subscribe, "best_version", False)),
            "_manual_upgrade": bool(getattr(subscribe, "_manual_upgrade", False)),
        }

    def _build_pending_record(
            self,
            *,
            current: Dict[str, Any],
            pending_key: str,
            task_type: str,
            info_hash: str,
            source_hash: str,
            share_url: str,
            cloud_dir: str,
            file_name: str,
            staging_dir: str,
            staging_name: str,
            file_size: int,
            now: float,
            mediainfo: MediaInfo,
            media_data: Optional[Dict[str, Any]] = None,
            subscribe_id: Optional[int] = None,
            success_episodes: Optional[List[int]] = None,
            notification_episodes: Optional[List[int]] = None,
            season: Optional[int] = None,
            sub_key: str = "",
            transient_target: bool = False,
            target_subscribe: Optional[Dict[str, Any]] = None,
            upgrade: bool = False,
            upgrade_mode: str = "",
            upgrade_old_cloud_dir: str = "",
            upgrade_old_file_name: str = "",
            upgrade_old_file_id: str = "",
            upgrade_old_size: int = 0,
            subtitles: Optional[List[Dict[str, Any]]] = None,
            skip_history: bool = False,
    ) -> Dict[str, Any]:
        """构造单个后处理记录；单项和批量入口共用同一字段规则。"""
        current = current or {}
        is_transient_target = bool(
            transient_target or current.get("transient_target")
        )
        success_values = success_episodes or current.get("success_episodes") or []
        notification_values = (
                notification_episodes
                or current.get("notification_episodes")
                or success_values
                or current.get("success_episodes")
                or []
        )
        pending_task_id = str(
            info_hash
            or current.get("task_id")
            or (
                f"subscribe:{int(subscribe_id)}"
                if subscribe_id
                else f"media:{sub_key}" if sub_key else ""
            )
        )
        return {
            **current,
            "pending_key": pending_key,
            "task_type": task_type,
            "task_id": pending_task_id,
            "source_sha1": source_hash,
            "share_url": share_url,
            "cloud_dir": cloud_dir,
            "file_name": file_name,
            "staging_dir": staging_dir,
            "staging_name": staging_name,
            "file_size": int(file_size or current.get("file_size") or 0),
            "upgrade": bool(upgrade or current.get("upgrade")),
            "upgrade_mode": str(
                upgrade_mode or current.get("upgrade_mode") or self._upgrade_mode
            ),
            "upgrade_old_cloud_dir": str(
                upgrade_old_cloud_dir
                or current.get("upgrade_old_cloud_dir")
                or ""
            ),
            "upgrade_old_file_name": str(
                upgrade_old_file_name
                or current.get("upgrade_old_file_name")
                or ""
            ),
            "upgrade_old_file_id": str(
                upgrade_old_file_id or current.get("upgrade_old_file_id") or ""
            ),
            "upgrade_old_size": int(
                upgrade_old_size or current.get("upgrade_old_size") or 0
            ),
            "created_at": float(current.get("created_at") or now),
            "next_check_at": now + self._OFFLINE_CHECK_DELAYS[0],
            "check_index": int(current.get("check_index") or 0),
            "history_ready": bool(skip_history or current.get("skip_history")),
            "skip_history": bool(skip_history or current.get("skip_history")),
            "mediainfo": (
                media_data
                if media_data is not None
                else self._serialize_mediainfo(mediainfo)
            ),
            "subscribe_id": subscribe_id or current.get("subscribe_id"),
            "success_episodes": sorted(
                {
                    int(episode)
                    for episode in success_values
                    if int(episode) > 0
                }
            ),
            "season": (
                max(1, int(season or current.get("season") or 1))
                if getattr(mediainfo, "type", None) == MediaType.TV
                else None
            ),
            "episode": next(iter(notification_episodes or success_episodes or []), None),
            "notification_episodes": sorted(
                {
                    int(episode)
                    for episode in notification_values
                    if int(episode) > 0
                }
            ),
            "sub_key": str(sub_key or current.get("sub_key") or ""),
            "transient_target": is_transient_target,
            "target_subscribe": (
                copy.deepcopy(target_subscribe or current.get("target_subscribe") or {})
                if is_transient_target else {}
            ),
            "subtitles": copy.deepcopy(
                subtitles if subtitles is not None else current.get("subtitles") or []
            ),
        }

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
            subtitles: Optional[List[Dict[str, Any]]] = None,
            transient_target: bool = False,
            target_subscribe: Optional[Dict[str, Any]] = None,
            skip_history: bool = False,
    ) -> str:
        item = {
            "result_key": "single",
            "share_url": share_url,
            "cloud_dir": cloud_dir,
            "file_name": file_name,
            "source_sha1": source_sha1,
            "file_size": file_size,
            "staging_dir": staging_dir,
            "staging_name": staging_name,
            "success_episodes": success_episodes,
            "notification_episodes": notification_episodes,
            "upgrade": upgrade,
            "upgrade_mode": upgrade_mode,
            "upgrade_old_cloud_dir": upgrade_old_cloud_dir,
            "upgrade_old_file_name": upgrade_old_file_name,
            "upgrade_old_file_id": upgrade_old_file_id,
            "upgrade_old_size": upgrade_old_size,
            "subtitles": subtitles or [],
            "skip_history": skip_history,
        }
        result = self._queue_file_finalize_batch(
            items=[item],
            mediainfo=mediainfo,
            subscribe_id=subscribe_id,
            season=season,
            sub_key=sub_key,
            transient_target=transient_target,
            target_subscribe=target_subscribe,
        )
        pending_key = result.get("single", "")
        if pending_key:
            info_hash = self._offline_hash(share_url)
            if info_hash:
                logger.debug(f"⏳ 已登记离线完成监控：{file_name}")
            else:
                logger.debug(f"⏳ 文件仍在115系统处理中，已登记重命名与STRM后处理：{file_name}")
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
            subtitles: Optional[List[Dict[str, Any]]] = None,
            transient_target: bool = False,
            target_subscribe: Optional[Dict[str, Any]] = None,
            skip_history: bool = False,
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
            subtitles=subtitles,
            transient_target=transient_target,
            target_subscribe=target_subscribe,
            skip_history=skip_history,
        )
        return None, pending_key

    def _queue_file_finalize_batch(
            self,
            items: List[Dict[str, Any]],
            mediainfo: MediaInfo,
            subscribe_id: Optional[int] = None,
            season: Optional[int] = None,
            sub_key: str = "",
            transient_target: bool = False,
            target_subscribe: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        """一次持久化整批未就绪文件，避免逐项读写插件数据。"""
        if not items or not self._get_data:
            return {}
        now = time.time()
        media_data = self._serialize_mediainfo(mediainfo)
        result: Dict[str, str] = {}
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            organize_enabled = getattr(self, "_organize_after_transfer", True)
            for item in items:
                share_url = item["share_url"]
                staging_dir = str(
                    item.get("staging_dir") or item.get("cloud_dir") or "/"
                ).rstrip("/") or "/"
                staging_name = str(item.get("staging_name") or item.get("file_name") or "")
                if not organize_enabled:
                    cloud_dir = staging_dir
                    file_name = staging_name
                else:
                    cloud_dir = item["cloud_dir"]
                    file_name = item["file_name"]
                source_hash, source_identity = self._finalize_source_identity(
                    item.get("source_sha1") or "",
                    staging_dir,
                    staging_name,
                    item.get("file_size") or 0,
                )
                pending_key, task_type, info_hash = self._pending_identity(
                    share_url, cloud_dir, file_name, source_identity
                )
                current = pending.get(pending_key) or {}
                pending[pending_key] = self._build_pending_record(
                    current=current,
                    pending_key=pending_key,
                    task_type=task_type,
                    info_hash=info_hash,
                    source_hash=source_hash,
                    share_url=share_url,
                    cloud_dir=cloud_dir,
                    file_name=file_name,
                    staging_dir=staging_dir,
                    staging_name=staging_name,
                    file_size=item.get("file_size") or 0,
                    now=now,
                    mediainfo=mediainfo,
                    media_data=media_data,
                    subscribe_id=subscribe_id,
                    success_episodes=item.get("success_episodes"),
                    notification_episodes=item.get("notification_episodes"),
                    season=season,
                    sub_key=sub_key,
                    upgrade=bool(item.get("upgrade")),
                    upgrade_mode=item.get("upgrade_mode") or "",
                    upgrade_old_cloud_dir=item.get("upgrade_old_cloud_dir") or "",
                    upgrade_old_file_name=item.get("upgrade_old_file_name") or "",
                    upgrade_old_file_id=item.get("upgrade_old_file_id") or "",
                    upgrade_old_size=item.get("upgrade_old_size") or 0,
                    subtitles=item.get("subtitles") or [],
                    transient_target=transient_target,
                    target_subscribe=target_subscribe,
                    skip_history=bool(item.get("skip_history")),
                )
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
            transient_target: bool = False,
            target_subscribe: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Tuple[Optional[Path], str]]:
        """复用批量重命名缓存生成 STRM，避免逐文件查询115。"""
        results: Dict[str, Tuple[Optional[Path], str]] = {}
        generated = 0
        queued_items: List[Dict[str, Any]] = []
        ready_items: List[Dict[str, Any]] = []
        organize_enabled = getattr(self, "_organize_after_transfer", True)
        for item in items:
            result_key = str(item["result_key"])
            if not organize_enabled or item.get("staging_dir"):
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
        if organize_enabled:
            self._scrape_metadata_batch(ready_items, mediainfo, season=season)
        pending_keys = self._queue_file_finalize_batch(
            queued_items,
            mediainfo,
            subscribe_id=subscribe_id,
            season=season,
            sub_key=sub_key,
            transient_target=transient_target,
            target_subscribe=target_subscribe,
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
        if item.get("transient_target"):
            return
        task_type = str(item.get("task_type") or "share").strip().lower()
        provider_name = str(
            getattr(self._cloud_drive, "name", "网盘") or "网盘"
        )
        completion_source = {
            "share": f"{provider_name}分享转存",
            "cloud": f"{provider_name}路径整理",
            "cross_cloud": f"跨盘转存到{provider_name}后整理",
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
                logger.debug(f"后处理订阅进度媒体信息恢复失败：{error}")
        if not mediainfo or not success_episodes:
            logger.debug(
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
                candidates = list_subscribes_by_tmdb_id(
                    SubscribeOper(), mediainfo.tmdb_id, season
                )
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
                    and hasattr(self._search_handler, "clear_subscription_budgets")
            ):
                self._search_handler.clear_subscription_budgets(sub_key)
        except Exception as error:
            logger.error(f"文件后处理完成后更新订阅失败：{subscribe_id}，{error}")

    @staticmethod
    def _format_episode_ranges(episodes: Set[int]) -> str:
        return format_episode_ranges(episodes)


    @staticmethod
    def _normalize_cloud_path(path: str) -> str:
        return str(PurePosixPath("/" + str(path or "/").strip().lstrip("/")))

    @staticmethod
    def _configured_transfer_paths(plugin: Any) -> Dict[str, str]:
        """按驱动自描述读取各网盘的转存路径，新增网盘无需改动此处。"""
        paths = {}
        for definition_cls in get_driver_definitions():
            key = definition_cls.get_transfer_path_key()
            if not key:
                continue
            val = getattr(plugin, f"_{key}", None) or "/"
            paths[definition_cls.id] = val
        return paths

    @staticmethod
    def _configured_media_paths(plugin: Any) -> Dict[str, str]:
        """按驱动自描述读取各网盘的媒体库目录，新增网盘无需改动此处。"""
        paths = {}
        for definition_cls in get_driver_definitions():
            key = definition_cls.get_media_path_key()
            if not key:
                continue
            val = getattr(plugin, f"_{key}", None) or "/"
            paths[definition_cls.id] = val
        return paths

    def _cross_transfer_staging_path(self, provider_key: str) -> str:
        base_path = self._cloud_transfer_paths.get(
            str(provider_key or "").strip().lower(), "/"
        )
        # 直接复用已配置的转存目录，不为跨盘任务创建额外目录。
        return str(PurePosixPath(base_path))

    @classmethod
    def _cleanup_cross_transfer_staging(
            cls,
            source: CloudDriveProvider,
            staged_path: str = "",
            item: Optional[CloudFile] = None,
    ) -> None:
        """清理跨盘转存生成的临时源盘文件。
        
        注意：绝对不能删除 staged_path 目录本身！因为该目录直接复用用户配置的转存路径（如 /整理/待整理）。
        """
        if not source or not source.supports(CloudDriveCapability.FILE_MUTATION):
            return
        if item and getattr(item, "id", None):
            try:
                mutation = source.require(CloudDriveCapability.FILE_MUTATION)
                mutation.delete_file(item.id)
                logger.debug(f"已清理源盘跨盘临时文件：{getattr(item, 'name', '')} ({item.id})")
            except Exception as error:
                logger.debug(f"清理源盘跨盘临时文件失败：{error}")

    def _transfer_file(
            self, share_url: str, file_item: Dict[str, Any], save_path: str,
            target_name: str, source_sha1: str = "",
            parent_task_id: str = "",
            stop_requested: Optional[Callable[[], bool]] = None,
            media_type: str = "",
    ) -> bool:
        should_stop = stop_requested or self._stop_requested
        cloud_resource = self._is_cloud_resource_url(share_url)
        source = self._resource_provider_for_url(share_url)
        cross_provider = bool(
            source and self._cloud_drive and source.key != self._cloud_drive.key
        )
        if cross_provider:
            item_media_type = self._normalize_cross_transfer_media_type(
                file_item.get("media_type") or media_type
            )
            is_manual_override = bool(
                file_item.get("source") == "manual"
                or file_item.get("is_cross")
                or file_item.get("_manual")
            )
            if not is_manual_override:
                if item_media_type and item_media_type not in self._cross_transfer_media_types:
                    logger.debug(
                        f"跨盘转存跳过：{source.name} -> {self._cloud_drive.name}，"
                        f"媒体类型 {item_media_type} 未启用"
                    )
                    return False
            required = (
                    (self._cross_transfer_enabled or is_manual_override)
                    and self._cross_transfer_manager
                    and (
                            cloud_resource
                            or source.supports(CloudDriveCapability.SHARE_TRANSFER)
                    )
                    and source.supports(CloudDriveCapability.FILE_QUERY)
                    and source.supports(CloudDriveCapability.FILE_DOWNLOAD)
                    and self._cloud_drive.supports(CloudDriveCapability.LOCAL_UPLOAD)
                    and self._cloud_drive.supports(CloudDriveCapability.FILE_QUERY)
            )
            if not required:
                logger.warning(
                    f"无法跨盘转存单个文件：{source.name} -> "
                    f"{self._cloud_drive.name}，请检查跨盘开关和网盘能力"
                )
                return False
            if cloud_resource:
                item = self._cloud_file_from_dict(file_item)
                if not item.id:
                    logger.warning(f"无法跨盘整理文件：{source.name} 文件 ID 为空")
                    return False
            else:
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
                item = CloudFile(
                    item.id,
                    item.name,
                    False,
                    item.size,
                    source_sha1,
                    item.md5,
                    playback_values=item.playback_values,
                    native=item.native,
                )
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
                if not cloud_resource:
                    # 只清理分享转存产生的源盘暂存文件，绝不删除用户选择的网盘文件。
                    self._cleanup_cross_transfer_staging(source, "", item)
        if source and not source.supports(CloudDriveCapability.SHARE_TRANSFER):
            logger.warning(
                f"{source.name}暂不支持分享转存，已跳过该资源：{share_url}"
            )
            return False
        service = source.require(CloudDriveCapability.SHARE_TRANSFER) if source else self._share_transfer
        target_check_name = target_name or file_item.get("name")
        success = bool(service.transfer_file(
            share_url=share_url, file_id=file_item.get("id"),
            save_path=save_path, target_name=target_name,
            source_sha1=source_sha1,
        ))
        if not success and target_check_name:
            t_valid, t_index = self._cloud_directory_snapshot(save_path)
            if t_valid and (
                    target_check_name in t_index
                    or (target_name and target_name in t_index)
                    or (file_item.get("name") and file_item.get("name") in t_index)
            ):
                logger.debug(f"单文件转存返回失败但转存目录复核已存在，自愈复用：{save_path}/{target_check_name}")
                return True
        return success

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

                mediainfo = self._subscribe_mediainfo(
                    subscribe, MediaType.TV
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
                logger.debug(f"订阅完结检查异常 {getattr(subscribe, 'name', '?')}：{e}")
                logger.debug(traceback.format_exc())

        return completed_count
