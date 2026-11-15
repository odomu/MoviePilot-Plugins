"""离线任务完成检测与文件后处理。"""

import copy
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from app.db import SessionFactory
from app.db.models.subscribe import Subscribe
from app.log import logger
from app.schemas.types import MediaType

from .utils import extract_ed2k_filename, normalize_season
from ...core import CloudDriveCapability, OwnerDelegator
from ...search.subs_filter import anime_file_candidates
from ...utils import MediaFileParser


@dataclass
class ScrapeRequest:
    """批量刮削请求：同媒体同季合并后一次性补齐元数据。"""

    media: Any
    season: Optional[int]
    payload: Dict[str, Any]


@dataclass
class CommitRequest:
    """终态提交请求：批量刮削完成后统一登记完成状态。"""

    item: Dict[str, Any]
    pending_key: str
    strm_path: Optional[Path]
    media: Any
    media_data: Dict[str, Any]


class DirectoryFileIndex(dict):
    """支持文件名直接访问与动态网盘哈希（如 sha1, md5, sha256）及剧集季集 O(1) 快速反查的目录文件索引对象。"""

    def __init__(
            self,
            files: Optional[List[Any]] = None,
            algorithms: Optional[Set[str]] = None,
    ):
        super().__init__()
        self.by_hash: Dict[str, Any] = {}
        self.by_season_episode: Dict[Tuple[int, int], Any] = {}
        self.algorithms = frozenset(str(a).lower() for a in (algorithms or {"sha1", "md5"}))
        if files:
            for file_obj in files:
                self.add_file(file_obj)

    @property
    def by_sha1(self) -> Dict[str, Any]:
        return self.by_hash

    def add_file(self, file_obj: Any) -> None:
        name = getattr(file_obj, "name", None) or (file_obj.get("name") if isinstance(file_obj, dict) else None)
        if name:
            self[name] = file_obj
            se = MediaFileParser.extract_season_episode(name)
            if se and se not in self.by_season_episode:
                self.by_season_episode[se] = file_obj
        for algo in self.algorithms:
            val = getattr(file_obj, algo, None) or (file_obj.get(algo) if isinstance(file_obj, dict) else None)
            if val:
                self.by_hash[str(val).strip().upper()] = file_obj

    def remove_file(self, file_obj: Any) -> None:
        name = getattr(file_obj, "name", None) or (file_obj.get("name") if isinstance(file_obj, dict) else None)
        if name and name in self:
            del self[name]
            se = MediaFileParser.extract_season_episode(name)
            if se and self.by_season_episode.get(se) == file_obj:
                self.by_season_episode.pop(se, None)
        for algo in self.algorithms:
            val = getattr(file_obj, algo, None) or (file_obj.get(algo) if isinstance(file_obj, dict) else None)
            if val:
                self.by_hash.pop(str(val).strip().upper(), None)

    def get_by_hash(self, file_hash: str) -> Optional[Any]:
        if not file_hash:
            return None
        return self.by_hash.get(str(file_hash).strip().upper())

    def get_by_sha1(self, sha1: str) -> Optional[Any]:
        return self.get_by_hash(sha1)

    def get_by_season_episode(self, season: int, episode: int) -> Optional[Any]:
        return self.by_season_episode.get((season, episode))


class PostprocessBatchContext:
    """后处理单次批量执行会话上下文。"""

    def __init__(
            self,
            pending: Dict[str, Dict[str, Any]],
            pending_snapshot: Dict[str, Dict[str, Any]],
            due_keys: List[str],
            now: float,
            monitor_token: str,
            task_map: Dict[str, Any],
            tasks_valid: bool,
            subscribe_cache: Dict[int, Any],
            organize_enabled: bool = True,
    ):
        self.pending = pending
        self.pending_snapshot = pending_snapshot
        self.due_keys = due_keys
        self.now = now
        self.monitor_token = monitor_token
        self.task_map = task_map
        self.tasks_valid = tasks_valid
        self.subscribe_cache = subscribe_cache

        self.completed: int = 0
        self.failed: int = 0
        self.committed_keys: Set[str] = set()

        self.directory_snapshots: Dict[str, Tuple[bool, DirectoryFileIndex]] = {}
        self.media_context_cache: Dict[Tuple[Any, ...], Tuple[Any, Dict[str, Any]]] = {}
        self.prepared_files: Dict[str, Any] = {}
        self.moved_files: Dict[str, Any] = {}
        self.upgrade_delete_batch: Dict[str, Dict[str, Any]] = {}
        self.subscription_batches: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        self.finalized_details: List[Dict[str, Any]] = []
        self.notification_contexts: List[Tuple[Dict[str, Any], str]] = []
        # 批次两阶段：先汇总刮削请求，再统一登记终态，避免逐文件重复请求 TMDB
        self.scrape_requests: List[ScrapeRequest] = []
        self.staging_dirs_to_clean: Set[str] = set()
        self.commit_requests: List[CommitRequest] = []
        # 历史状态批次回写：逐文件整表重写会退化为 O(项数 × 历史条数)
        self.settled_success: Set[str] = set()
        self.settled_failure: Dict[str, str] = {}
        # 批次级缓存：整理开关与网盘哈希算法在一次批次内不变
        self.organize_enabled: bool = bool(organize_enabled)
        self.hash_algorithms: Optional[frozenset] = None

    def sync_rename(self, dir_path: str, old_file: Any, new_file: Any) -> None:
        """重命名成功后直接在内存索引中同步更新，避免向网盘重新发请求。"""
        normalized_dir = str(dir_path or "").rstrip("/")
        if normalized_dir in self.directory_snapshots:
            valid, index = self.directory_snapshots[normalized_dir]
            if valid and isinstance(index, DirectoryFileIndex):
                index.remove_file(old_file)
                index.add_file(new_file)

    def sync_move(self, src_dir: str, dst_dir: str, file_obj: Any) -> None:
        """移动成功后跨目录同步内存索引，免除双方目录的网络二次扫描。"""
        src_norm = str(src_dir or "").rstrip("/")
        dst_norm = str(dst_dir or "").rstrip("/")
        if src_norm in self.directory_snapshots:
            valid, index = self.directory_snapshots[src_norm]
            if valid and isinstance(index, DirectoryFileIndex):
                index.remove_file(file_obj)
        if dst_norm in self.directory_snapshots:
            valid, index = self.directory_snapshots[dst_norm]
            if valid and isinstance(index, DirectoryFileIndex):
                index.add_file(file_obj)


class PostprocessService(OwnerDelegator):
    """监控待处理文件并完成重命名、STRM和历史状态更新。"""

    # 任务类型：缺省按普通转存；离线类需要轮询任务就绪
    _DEFAULT_TASK_TYPE = "share"
    _OFFLINE_TASK_TYPES = frozenset({"magnet", "ed2k", "offline"})
    # 需要按平台规则重算目标目录/取离线任务名的类型（Magnet 走整包流程）
    _FILE_WAIT_TASK_TYPES = frozenset({"ed2k", "offline"})

    # 后处理步骤：键 → 展示文案；步骤清单与默认进度说明共用同一文案
    _POSTPROCESS_STEPS = {
        "locate": "检查并定位文件",
        "organize": "重命名与移动",
        "strm": "生成 STRM",
        "subtitle": "处理字幕",
        "metadata": "刮削元数据",
        "commit": "登记完成状态",
        "notify": "消息通知",
    }

    _FINALIZE_MAX_FAILURES = 5
    # 记录落库是后处理的前置条件；超过该时限仍未落库说明该轮同步已放弃，
    # 放行以避免遗留任务被永久阻塞（正常同步在秒级内即完成落库）。
    _HISTORY_READY_GRACE = 30 * 60
    _FINALIZE_DEAD_LOG = "文件后处理连续失败 {} 次，已终止重试：{}"
    _FINALIZE_DEAD_REASON = "文件后处理连续失败 {} 次，已停止自动重试"
    _FINALIZE_DEAD_TITLE = "网盘文件后处理失败"
    _FINALIZE_DEAD_TEXT = (
        "{}\n\n连续 {} 次后处理失败，已停止自动重试，"
        "请检查网盘文件与媒体目录状态"
    )

    def _supported_hash_algorithms(self) -> frozenset[str]:
        """动态获取当前目标网盘支持的文件哈希校验算法集合。"""
        cloud_drive = getattr(self, "_cloud_drive", None)
        if cloud_drive and cloud_drive.supports(CloudDriveCapability.RAPID_UPLOAD):
            try:
                rapid = cloud_drive.require(CloudDriveCapability.RAPID_UPLOAD)
                algorithms = getattr(rapid, "algorithms", None)
                if algorithms:
                    return frozenset(str(a).lower() for a in algorithms)
            except Exception:
                pass
        return frozenset({"sha1", "md5"})

    def _cloud_directory_snapshot(
            self,
            cloud_dir: str,
            cache: Optional[Dict[str, Tuple[bool, DirectoryFileIndex]]] = None,
            algorithms: Optional[frozenset] = None,
    ) -> Tuple[bool, DirectoryFileIndex]:
        """获取并缓存网盘目录下的文件双索引。"""
        normalized_dir = str(cloud_dir or "").rstrip("/")
        if cache is not None and normalized_dir in cache:
            return cache[normalized_dir]
        lookup = self._cloud_directories.resolve_directory(normalized_dir)
        algorithms = algorithms or self._supported_hash_algorithms()
        if not lookup.checked:
            result = (False, DirectoryFileIndex(algorithms=algorithms))
        elif lookup.directory_id is None:
            result = (True, DirectoryFileIndex(algorithms=algorithms))
        else:
            listing = self._cloud_directories.list_directory(lookup.directory_id)
            if not listing.checked:
                result = (False, DirectoryFileIndex(algorithms=algorithms))
            else:
                result = (True, DirectoryFileIndex(listing.files, algorithms=algorithms))
        if cache is not None:
            cache[normalized_dir] = result
        return result

    @classmethod
    def _task_type(cls, item: Dict[str, Any]) -> str:
        """归一化任务类型：缺省按普通转存处理。"""
        return str(item.get("task_type") or cls._DEFAULT_TASK_TYPE).strip().lower()

    def _batch_hash_algorithms(self, ctx: "PostprocessBatchContext") -> frozenset:
        """每批次只解析一次目标网盘支持的哈希算法。"""
        if ctx.hash_algorithms is None:
            ctx.hash_algorithms = self._supported_hash_algorithms()
        return ctx.hash_algorithms

    def _snapshot(
            self, ctx: "PostprocessBatchContext", cloud_dir: str
    ) -> Tuple[bool, "DirectoryFileIndex"]:
        """批次内目录快照：复用目录缓存与哈希算法缓存。"""
        return self._cloud_directory_snapshot(
            cloud_dir, ctx.directory_snapshots, self._batch_hash_algorithms(ctx)
        )

    def _snapshot_getter(self, ctx: "PostprocessBatchContext"):
        """批次内目录快照访问器，供需要回调的调用方使用。"""
        algorithms = self._batch_hash_algorithms(ctx)
        return lambda cloud_dir: self._cloud_directory_snapshot(
            cloud_dir, ctx.directory_snapshots, algorithms
        )

    @staticmethod
    def _postprocess_task_id(item: Dict[str, Any]) -> str:
        subscribe_id = int(item.get("subscribe_id") or 0)
        if subscribe_id > 0:
            return f"subscribe:{subscribe_id}"
        sub_key = str(item.get("sub_key") or "").strip()
        return f"media:{sub_key}" if sub_key else ""

    def _postprocess_steps(
            self, item: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, str]]:
        item = item or {}
        task_type = self._task_type(item)
        waiting_offline = task_type in self._OFFLINE_TASK_TYPES and not bool(
            item.get("offline_completed") or item.get("moved_at")
        )
        has_subtitles = bool((item or {}).get("subtitles"))
        strm_enabled = bool(
            self._strm_generate_enabled
            and self._strm_generator
            and self._local_resource_path
        )
        metadata_enabled = bool(
            self._metadata_scraper
            and self._local_resource_path
            and (self._nfo_scrape_enabled or self._image_scrape_enabled)
        )
        return [
            {
                "key": key,
                "label": "等待离线下载" if key == "locate" and waiting_offline else label,
            }
            for key, label in self._POSTPROCESS_STEPS.items()
            if not (key == "strm" and not strm_enabled)
               and not (key == "subtitle" and not has_subtitles)
               and not (key == "metadata" and not metadata_enabled)
               and not (key == "notify" and not self._notify)
        ]

    def _update_postprocess_progress(
            self,
            item: Dict[str, Any],
            pending_key: str,
            step: str,
            detail: str = "",
    ) -> None:
        """通过现有任务运行态推送当前文件和处理步骤；detail 缺省取步骤默认文案。"""
        if not self._postprocess_task_update:
            return
        detail = detail or self._POSTPROCESS_STEPS.get(step, "")
        task_id = self._postprocess_task_id(item)
        if not task_id:
            return
        steps = self._postprocess_steps(item)
        step_index = next(
            (
                index for index, value in enumerate(steps)
                if value["key"] == step
            ),
            None,
        )
        if step_index is None:
            return
        self._postprocess_task_update(
            task_id,
            _pending_key=pending_key,
            current_file=str(item.get("file_name") or "").strip(),
            postprocess_active=True,
            postprocess_detail=str(detail or "").strip(),
            postprocess_step=step,
            postprocess_step_index=step_index,
            postprocess_step_total=len(steps),
            postprocess_steps=steps,
        )

    def _cleanup_failed_offline_task(
            self, item: Dict[str, Any], reason: str
    ) -> None:
        """失败后清理残留离线任务：只删任务本身，已下载文件一律保留。"""
        task_id = str(item.get("task_id") or "").strip()
        if not task_id:
            return
        delete_task = getattr(getattr(self, "_offline_tasks", None), "delete_offline_task", None)
        if callable(delete_task) and not bool(item.get("download_completed_at")):
            try:
                delete_task(task_id, delete_source_file=False)
                logger.info(f"已清理未完成的离线任务：{task_id}")
            except Exception as error:
                logger.warning(f"清理未完成的离线任务失败：{task_id}，{error}")
        logger.warning(f"离线任务未完成，保留已下载文件：{task_id}，原因：{reason}")

    @staticmethod
    def _upgrade_backup_name(file_name: str, task_id: str) -> str:
        """仅在原文件名后追加短任务 ID，避免隐藏文件和冗长标记。"""
        source = Path(str(file_name or ""))
        short_id = "".join(
            value for value in str(task_id or "") if value.isalnum()
        )[:10]
        if not short_id:
            short_id = uuid.uuid4().hex[:10]
        return f"{source.stem}-{short_id}{source.suffix}"

    @staticmethod
    def _due_pending_keys(
            pending: Dict[str, Dict[str, Any]],
            now: float,
            force: bool = False,
            pending_keys: Optional[Set[str]] = None,
    ) -> List[str]:
        selected = set(pending_keys or [])
        return [
            key
            for key, item in pending.items()
            if (not selected or key in selected)
               and (
                       item.get("history_ready")
                       or now - float(item.get("created_at") or now) >= PostprocessService._HISTORY_READY_GRACE
               )
               and (bool(selected and key in selected) or now >= float(item.get("_monitor_until") or 0))
               and (force or now >= float(item.get("next_check_at") or 0))
        ]

    @staticmethod
    def _pending_media_context(
            item: Dict[str, Any], pending_key: str = ""
    ) -> Tuple[Any, ...]:
        """待处理条目的媒体上下文标识；按 媒体ID→标题→订阅→sub_key→待处理键 回退。"""
        media_data = item.get("mediainfo") or {}
        media_type = str(media_data.get("type") or item.get("type") or "")
        media_id = (
                media_data.get("tmdb_id")
                or media_data.get("douban_id")
                or media_data.get("media_id")
        )
        if media_id:
            return ("media", media_type, str(media_id))
        title = str(media_data.get("title") or item.get("title") or "").strip()
        if title:
            return (
                "title",
                media_type,
                title.casefold(),
                str(media_data.get("year") or item.get("year") or ""),
            )
        subscribe_id = int(item.get("subscribe_id") or 0)
        if subscribe_id > 0:
            return ("subscribe", subscribe_id)
        sub_key = str(item.get("sub_key") or "").strip()
        if sub_key:
            return ("sub_key", sub_key)
        return ("pending", pending_key) if pending_key else ()

    @staticmethod
    def _media_context_key(item: Dict[str, Any]) -> Optional[Tuple[Any, ...]]:
        """媒体识别缓存键；无法定位媒体时返回 None。"""
        base = PostprocessService._pending_media_context(item)
        if not base or base[0] == "pending":
            return None
        if base[0] in {"media", "title"}:
            return (*base, int(item.get("season") or 0))
        return base

    @staticmethod
    def _offline_media_group_key(
            item: Dict[str, Any], pending_key: str
    ) -> Tuple[Any, ...]:
        """离线后处理并发分组键；同媒体由单个工作线程顺序处理。"""
        return PostprocessService._pending_media_context(
            item, pending_key
        ) or ("pending", pending_key)

    def get_due_offline_task_groups(
            self,
            force: bool = False,
            pending_keys: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        """按媒体聚合当前到期任务；同一媒体由单个工作线程顺序处理。"""
        if not self._get_data:
            return []
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
        now = time.time()
        due_keys = self._due_pending_keys(
            pending, now, force=force, pending_keys=pending_keys
        )
        groups: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        for pending_key in due_keys:
            item = pending[pending_key]
            group_key = self._offline_media_group_key(item, pending_key)
            group = groups.setdefault(group_key, {
                "pending_keys": set(),
                "needs_offline": False,
                "task_ids": set(),
            })
            group["pending_keys"].add(pending_key)
            group["needs_offline"] = group["needs_offline"] or str(
                self._task_type(item)
            ) in self._OFFLINE_TASK_TYPES
            task_id = str(item.get("task_id") or "").strip().upper()
            if task_id:
                group["task_ids"].add(task_id)
        return list(groups.values())

    @staticmethod
    def _strm_file_ready(strm_path: Optional[Path]) -> bool:
        try:
            return bool(
                strm_path
                and strm_path.is_file()
                and strm_path.stat().st_size > 0
            )
        except OSError:
            return False

    def _delete_upgrade_old_strm(
            self,
            item: Dict[str, Any],
            replacement_path: Optional[Path] = None,
    ) -> None:
        """新 STRM 就绪后清理路径不同的旧版本 STRM。"""
        if not self._strm_generator or not self._local_resource_path:
            return
        old_dir = str(item.get("upgrade_old_cloud_dir") or "").strip()
        old_name = str(item.get("upgrade_old_file_name") or "").strip()
        if not old_dir or not old_name:
            return
        try:
            strm_path = self._strm_generator.local_path(
                local_root=self._local_resource_path,
                cloud_root=self._CLOUD_MEDIA_ROOT,
                cloud_dir=old_dir,
                file_name=old_name,
            )
            if replacement_path and (
                    strm_path.resolve(strict=False)
                    == replacement_path.resolve(strict=False)
            ):
                return
            if strm_path.is_file():
                strm_path.unlink()
                logger.debug(f"洗版清理旧 STRM：{strm_path}")
        except (OSError, ValueError) as error:
            logger.debug(f"洗版清理旧 STRM 失败：{old_dir}/{old_name}，{error}")

    def _prime_upgrade_old_target(self, item: Dict[str, Any]) -> None:
        """把升级基线里的旧文件信息落到替换字段，离线分支只写 upgrade_baseline。"""
        if item.get("upgrade_old_file_name") or item.get("upgrade_old_file_id"):
            return
        baseline = item.get("upgrade_baseline")
        if not isinstance(baseline, dict) or not baseline:
            return
        episode = item.get("episode")
        target: Any = None
        if episode:
            try:
                target = baseline.get(str(int(episode)))
            except (TypeError, ValueError):
                target = None
        if not isinstance(target, dict):
            target = next(
                (value for value in baseline.values() if isinstance(value, dict)),
                None,
            )
        if not isinstance(target, dict):
            return
        old_dir = str(target.get("cloud_dir") or "").strip()
        old_name = str(target.get("file_name") or "").strip()
        old_id = str(target.get("file_id") or "").strip()
        if not (old_dir or old_name or old_id):
            return
        item["upgrade_old_cloud_dir"] = old_dir
        item["upgrade_old_file_name"] = old_name
        item["upgrade_old_file_id"] = old_id
        if not int(item.get("upgrade_old_size") or 0):
            item["upgrade_old_size"] = int(target.get("size") or 0)

    def _replace_upgrade_file(
            self,
            item: Dict[str, Any],
            pending_key: str,
            target_file: Any,
            staging_dir: str,
            file_name: str,
            now: float,
            directory_snapshot,
    ) -> Optional[Any]:
        """将新文件移入目标位置；旧文件仅临时避让，等待最终提交删除。"""
        final_dir = str(item.get("cloud_dir") or "/").rstrip("/") or "/"
        item["_upgrade_source_file_id"] = str(
            getattr(target_file, "id", "") or ""
        )
        old_dir = str(item.get("upgrade_old_cloud_dir") or final_dir).rstrip("/") or "/"
        old_name = str(item.get("upgrade_old_file_name") or "").strip()
        old_id = str(item.get("upgrade_old_file_id") or "").strip()
        backup_name = str(item.get("upgrade_backup_name") or "").strip()
        old_file = None
        if old_id or old_name:
            old_valid, old_index = directory_snapshot(old_dir)
            if old_valid:
                old_file = next(
                    (value for value in old_index.values()
                     if old_id and str(getattr(value, "id", "")) == old_id),
                    None,
                )
                old_file = old_file or old_index.get(old_name)

        if not item.get("upgrade_old_backed_up") and old_file and (
                str(getattr(old_file, "id", "")) != str(getattr(target_file, "id", ""))
        ):
            backup_name = backup_name or (
                self._upgrade_backup_name(
                    old_name, item.get("task_id") or pending_key
                )
            )
            logger.debug(
                f"洗版临时备份旧文件：{old_dir}/{old_name} -> {backup_name}"
            )
            if not self._cloud_mutations.rename_file(old_dir, old_file, backup_name):
                logger.debug(f"洗版替换无法备份旧文件：{old_dir}/{old_name}")
                return None
            item["upgrade_old_backed_up"] = True
            item["upgrade_backup_name"] = backup_name
            item["upgrade_old_file_id"] = str(getattr(old_file, "id", "") or old_id)
        elif old_file and not item.get("upgrade_old_backed_up"):
            # 基线旧文件就是本次定位到的文件，无需避让与回收
            item["upgrade_old_deleted"] = True

        if not item.get("moved_at"):
            if target_file.name != file_name:
                if not self._cloud_mutations.rename_file(staging_dir, target_file, file_name):
                    if item.get("upgrade_old_backed_up") and old_file:
                        if self._cloud_mutations.rename_file(old_dir, old_file, old_name):
                            item.pop("upgrade_old_backed_up", None)
                        else:
                            logger.error(
                                f"洗版新文件重命名失败且旧文件恢复失败：{old_dir}/{backup_name}"
                            )
                    self._schedule_finalize_retry(item, now)
                    return None
                item["staging_name"] = file_name
                target_file = self._cloud_query.get_cached_file(staging_dir, file_name)
                if not target_file:
                    self._schedule_finalize_retry(item, now)
                    return None
            moved_file = (
                target_file if staging_dir == final_dir
                else self._cloud_mutations.move_file(target_file, final_dir, file_name)
            )
            if not moved_file:
                if item.get("upgrade_old_backed_up") and old_file:
                    if self._cloud_mutations.rename_file(old_dir, old_file, old_name):
                        item.pop("upgrade_old_backed_up", None)
                    else:
                        logger.error(
                            f"洗版新文件移动失败且旧文件恢复失败：{old_dir}/{backup_name}"
                        )
                self._schedule_finalize_retry(item, now)
                return None
            item["moved_at"] = now
            target_file = moved_file

        return target_file

    def _upgrade_old_file_id(
            self,
            item: Dict[str, Any],
            directory_snapshot,
    ) -> str:
        """解析已备份旧文件的真实 ID，供单个或批量回收复用。"""
        if item.get("upgrade_old_deleted") or not item.get("upgrade_old_backed_up"):
            return ""
        old_dir = str(
            item.get("upgrade_old_cloud_dir") or item.get("cloud_dir") or "/"
        ).rstrip("/") or "/"
        backup_name = str(item.get("upgrade_backup_name") or "").strip()
        backup_id = str(item.get("upgrade_old_file_id") or "").strip()
        if backup_id:
            return backup_id
        if not backup_name:
            return ""
        _, backup_index = directory_snapshot(old_dir)
        backup_file = backup_index.get(backup_name)
        return str(getattr(backup_file, "id", "") or "") if backup_file else ""

    def _delete_unbacked_upgrade_old_file(
            self,
            item: Dict[str, Any],
            old_dir: str,
            directory_snapshot,
    ) -> bool:
        """旧文件未做避让备份时的兜底回收：按 ID 或名称直接删除。"""
        old_name = str(item.get("upgrade_old_file_name") or "").strip()
        old_id = str(item.get("upgrade_old_file_id") or "").strip()
        if not old_name and not old_id:
            return True
        if old_id and old_id == str(item.get("_upgrade_source_file_id") or ""):
            # 旧文件即刚移入的新文件，无需回收
            item["upgrade_old_deleted"] = True
            return True
        if not old_id:
            valid, index = directory_snapshot(old_dir)
            if not valid:
                logger.debug(f"洗版旧文件目录读取失败，删除待重试：{old_dir}/{old_name}")
                return False
            found = index.get(old_name)
            old_id = str(getattr(found, "id", "") or "")
            if old_id and old_id == str(item.get("_upgrade_source_file_id") or ""):
                item["upgrade_old_deleted"] = True
                return True
        if old_id and self._cloud_mutations.delete_file(old_id):
            item["upgrade_old_deleted"] = True
            logger.debug(f"洗版完成，已删除未避让的旧文件：{old_dir}/{old_name or old_id}")
            return True
        logger.debug(f"洗版旧文件删除待重试：{old_dir}/{old_name or old_id}")
        return False

    def _delete_upgrade_old_file(
            self,
            item: Dict[str, Any],
            directory_snapshot,
    ) -> bool:
        """新文件及 STRM 就绪后，提交洗版并删除临时避让的旧文件。"""
        if item.get("upgrade_old_deleted"):
            return True
        old_dir = str(
            item.get("upgrade_old_cloud_dir") or item.get("cloud_dir") or "/"
        ).rstrip("/") or "/"
        if not item.get("upgrade_old_backed_up"):
            return self._delete_unbacked_upgrade_old_file(
                item, old_dir, directory_snapshot
            )
        backup_name = str(item.get("upgrade_backup_name") or "").strip()
        backup_id = self._upgrade_old_file_id(item, directory_snapshot)
        if backup_id and self._cloud_mutations.delete_file(backup_id):
            item["upgrade_old_deleted"] = True
        if item.get("upgrade_old_deleted"):
            logger.debug(f"洗版完成，已删除旧文件：{old_dir}/{backup_name}")
            return True
        logger.debug(f"洗版新文件已就绪，旧文件删除待重试：{old_dir}/{backup_name}")
        return False

    def _prepare_postprocess_context(
            self,
            force: bool = False,
            pending_keys: Optional[Set[str]] = None,
            offline_tasks: Optional[List[Dict[str, Any]]] = None,
            offline_tasks_valid: Optional[bool] = None,
    ) -> Optional[PostprocessBatchContext]:
        """初始化后处理批次会话上下文，完成到期任务筛选与租约锁定。"""
        if (
                not self._get_data
                or not self._cloud_directories
                or not self._cloud_query
                or not self._cloud_mutations
        ):
            return None
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            if not pending:
                return None

            now = time.time()
            due_keys = self._due_pending_keys(
                pending, now, force=force, pending_keys=pending_keys
            )
            if not due_keys:
                return PostprocessBatchContext(
                    pending=pending,
                    pending_snapshot=copy.deepcopy(pending),
                    due_keys=[],
                    now=now,
                    monitor_token="",
                    task_map={},
                    tasks_valid=False,
                    subscribe_cache={},
                )

            monitor_token = uuid.uuid4().hex
            for key in due_keys:
                item = pending[key]
                item["_monitor_token"] = monitor_token
                item["_monitor_until"] = now + self._OFFLINE_MONITOR_LEASE_SECONDS
            self._save_offline_pending(pending)
            pending_snapshot = copy.deepcopy(pending)

        subscribe_ids = {
            int((pending.get(key) or {}).get("subscribe_id") or 0)
            for key in due_keys
            if int((pending.get(key) or {}).get("subscribe_id") or 0) > 0
        }
        subscribe_cache: Dict[int, Any] = {}
        if subscribe_ids:
            try:
                with SessionFactory() as db:
                    subscribes = db.query(Subscribe).filter(
                        Subscribe.id.in_(sorted(subscribe_ids))
                    ).all()
                    subscribe_cache = {
                        int(subscribe.id): subscribe for subscribe in subscribes
                    }
                for subscribe_id in subscribe_ids - set(subscribe_cache):
                    subscribe_cache[subscribe_id] = None
            except Exception as error:
                logger.debug(f"批量读取后处理订阅失败，将按需查询：{error}")

        needs_offline = any(
            self._task_type(pending.get(key) or {}) in self._OFFLINE_TASK_TYPES
            for key in due_keys
        )
        tasks = offline_tasks
        if needs_offline and tasks is None and self._offline_tasks:
            snapshot = self._offline_tasks.get_offline_task_list_snapshot(
                force=True,
            )
            tasks = snapshot.get("tasks") or []
            offline_tasks_valid = bool(snapshot.get("refresh_ok"))
        tasks_valid = bool(offline_tasks_valid)
        task_map: Dict[str, Any] = {}
        for task in (tasks or []):
            task_id = str(task.get("id") or "").upper()
            if task_id:
                task_map[task_id] = task
            native_id = str(task.get("native_id") or "").upper()
            if native_id and native_id not in task_map:
                task_map[native_id] = task

        # Pending magnet IDs also contain the subscription ID.
        for pending_item in pending.values():
            pending_id = str(pending_item.get("task_id") or "").upper()
            parts = pending_id.split(":")
            if len(parts) == 3 and parts[0] == "MAGNET":
                native_task = task_map.get(parts[1])
                if native_task is not None:
                    task_map[pending_id] = native_task

        context = PostprocessBatchContext(
            pending=pending,
            pending_snapshot=pending_snapshot,
            due_keys=due_keys,
            now=now,
            monitor_token=monitor_token,
            task_map=task_map,
            tasks_valid=tasks_valid,
            subscribe_cache=subscribe_cache,
            organize_enabled=getattr(self, "_organize_after_transfer", True),
        )
        return context

    @classmethod
    def _queue_subscription_completion(
            cls,
            item: Dict[str, Any],
            media: Any,
            media_data: Dict[str, Any],
            ctx: PostprocessBatchContext,
    ) -> None:
        """记录已完成的订阅集数，供批次结束时聚合更新订阅进度。"""
        if item.get("transient_target"):
            return
        episode_values = (
                item.get("success_episodes")
                or item.get("notification_episodes")
                or item.get("target_episodes")
                or ([item.get("episode")] if item.get("episode") else [])
        )
        episodes = set()
        for value in episode_values:
            try:
                episode = int(value or 0)
            except (TypeError, ValueError):
                continue
            if episode > 0:
                episodes.add(episode)
        if not media or not episodes:
            return
        key = (
            int(item.get("subscribe_id") or 0),
            int(getattr(media, "tmdb_id", 0) or 0),
            int(item.get("season") or 0),
            cls._task_type(item).strip().lower(),
        )
        batch = ctx.subscription_batches.setdefault(key, {
            "item": copy.deepcopy(item),
            "mediainfo": media,
            "media_data": dict(media_data or {}),
            "episodes": set(),
        })
        batch["episodes"].update(episodes)

    def _extract_item_hash(self, item: Dict[str, Any]) -> str:
        """根据当前目标网盘动态支持的文件算法，从待办项中提取匹配的文件哈希。"""
        for algo in self._supported_hash_algorithms():
            val = item.get(f"source_{algo}") or item.get(algo)
            if val:
                return str(val).strip().upper()
        for fallback in ("source_sha1", "sha1", "source_md5", "md5"):
            val = item.get(fallback)
            if val:
                return str(val).strip().upper()
        return ""

    @staticmethod
    def _match_by_name(file_index: "DirectoryFileIndex", candidate_names: List[str]) -> Optional[Any]:
        """策略一：精确文件名命中（O(1)）。"""
        for name in candidate_names:
            if name and name in file_index:
                return file_index[name]
        return None

    @staticmethod
    def _match_by_hash(file_index: "DirectoryFileIndex", target_hash: str) -> Optional[Any]:
        """策略二：文件哈希命中（O(1)，兼容存储类型差异）。"""
        clean_hash = str(target_hash or "").strip().upper()
        if clean_hash and hasattr(file_index, "get_by_hash"):
            return file_index.get_by_hash(clean_hash)
        return None

    @staticmethod
    def _match_by_fuzzy_name(
            file_index: "DirectoryFileIndex", candidate_names: List[str]
    ) -> Optional[Any]:
        """策略三：前后缀与网盘重名副本容错（URL 截断名、特殊编码、"(1)" 副本）。"""
        for candidate_name in candidate_names:
            if not candidate_name:
                continue
            cand_stem = Path(candidate_name).stem.lower()
            cand_ext = Path(candidate_name).suffix.lower()
            for fname, fobj in file_index.items():
                if candidate_name.endswith(fname) or fname.endswith(candidate_name):
                    return fobj
                fname_stem = Path(fname).stem.lower()
                fname_ext = Path(fname).suffix.lower()
                if cand_stem and fname_stem and (
                        fname_stem.startswith(cand_stem)
                        or cand_stem.startswith(fname_stem)
                ):
                    if not cand_ext or not fname_ext or cand_ext == fname_ext:
                        return fobj
        return None

    @staticmethod
    def _match_by_season_episode(
            file_index: "DirectoryFileIndex",
            item: Optional[Dict[str, Any]],
            candidate_names: List[str],
    ) -> Optional[Any]:
        """策略四：季集索引定位（避免逐文件正则解析）。"""
        if not item or not hasattr(file_index, "get_by_season_episode"):
            return None
        target_season = item.get("season")
        target_eps = item.get("success_episodes") or item.get("notification_episodes")
        target_ep = (
            target_eps[0] if isinstance(target_eps, list) and target_eps
            else item.get("episode")
        )
        if target_season is None or target_ep is None:
            for cand in candidate_names:
                if not cand:
                    continue
                season_episode = MediaFileParser.extract_season_episode(cand)
                if season_episode:
                    if target_season is None:
                        target_season = season_episode[0]
                    if target_ep is None:
                        target_ep = season_episode[1]
                    break
        if target_season is None or target_ep is None:
            return None
        try:
            return file_index.get_by_season_episode(
                int(target_season), int(target_ep)
            )
        except (ValueError, TypeError):
            return None

    def _locate_cloud_file(
            self,
            dir_path: str,
            candidate_names: List[str],
            target_hash: str,
            ctx: PostprocessBatchContext,
            item: Optional[Dict[str, Any]] = None,
    ) -> Optional[Any]:
        """在指定网盘目录下定位物理文件：精确名 → 哈希 → 模糊名 → 季集索引。"""
        if not dir_path:
            return None
        valid, file_index = self._snapshot(ctx, dir_path)
        if not valid or not file_index:
            return None
        for find in (
                lambda: self._match_by_name(file_index, candidate_names),
                lambda: self._match_by_hash(file_index, target_hash),
                lambda: self._match_by_fuzzy_name(file_index, candidate_names),
                lambda: self._match_by_season_episode(file_index, item, candidate_names),
        ):
            found = find()
            if found:
                return found
        return None

    def _has_existing_cloud_file(
            self, item: Dict[str, Any], pending_key: str, ctx: PostprocessBatchContext
    ) -> bool:
        """检查任务对应的物理文件是否已在暂存目录或最终媒体目录就位，命中时复用对象避免二次定位。"""
        task = ctx.task_map.get(str(item.get("task_id") or pending_key).upper())
        if bool(item.get("moved_at") or (task and task.get("completed"))):
            return True
        cached_file = ctx.moved_files.get(pending_key) or ctx.prepared_files.get(pending_key)
        if cached_file:
            return True
        staging_dir = str(item.get("staging_dir") or item.get("cloud_dir") or "/").rstrip("/") or "/"
        final_dir = str(item.get("cloud_dir") or "/").rstrip("/") or "/"
        task_name = str((task or {}).get("name") or "").strip()
        candidate_names = [
            str(item.get("file_name") or ""),
            str(item.get("staging_name") or ""),
            task_name,
            extract_ed2k_filename(str(item.get("share_url") or "")),
        ]
        target_hash = self._extract_item_hash(item)
        found = self._locate_cloud_file(staging_dir, candidate_names, target_hash, ctx, item=item)
        if found:
            ctx.prepared_files[pending_key] = found
            return True
        if final_dir != staging_dir:
            found = self._locate_cloud_file(final_dir, candidate_names, target_hash, ctx, item=item)
            if found:
                ctx.moved_files[pending_key] = found
                item["moved_at"] = ctx.now
                item["staging_dir"] = final_dir
                return True
        return False

    def _commit_single_item(
            self,
            item: Dict[str, Any],
            pending_key: str,
            strm_path: Optional[Path],
            media: Any,
            media_data: Dict[str, Any],
            ctx: PostprocessBatchContext,
    ) -> None:
        """单项后处理完成后立即提交历史终态并将通知入队，避免中途停止丢失已完成项。"""
        self._update_postprocess_progress(item, pending_key, "commit")
        if item.get("upgrade") and str(
                item.get("upgrade_mode") or self._upgrade_mode
        ) != "coexist":
            self._delete_upgrade_old_strm(
                item, replacement_path=strm_path
            )
        try:
            self._queue_subscription_completion(item, media, media_data, ctx)
            detail = self._notify_pending_file_finalized(
                item,
                pending_key,
                strm_path,
                mediainfo=media,
                media_data=media_data,
                finish_subscription=media is None,
                subscribe_cache=ctx.subscribe_cache,
            )
        except Exception as error:
            # 记账与通知属于收尾副作用，失败不得阻断结算：否则待处理项会滞留并被无限重试
            logger.error(
                f"文件后处理收尾异常，仍继续结算："
                f"{item.get('file_name') or pending_key}，原因：{error}"
            )
            detail = None
        if detail:
            ctx.finalized_details.append(detail)
            ctx.notification_contexts.append((item, pending_key))
        if not strm_path:
            logger.debug(f"文件后处理完成：{item.get('file_name') or pending_key}")
        self._settle_pending_item(
            item, pending_key, ctx, detail="文件处理完成"
        )

    def _cleanup_staging_dirs(self, ctx: "PostprocessBatchContext") -> None:
        """批次结束后清理空暂存目录；关闭整理时保留，被待处理项占用时跳过。"""
        if not (getattr(self, "_organize_after_transfer", True) and self._cloud_batch_mutations):
            return
        pending: set = set()
        for item in ctx.pending.values():
            pending.update(self._staging_cleanup_candidates(item, ctx))
        # 逐批次只检查一次：不做跨批次否决缓存，否则同订阅分批结算时目录会被永久跳过
        for path in sorted(ctx.staging_dirs_to_clean - pending):
            self._delete_empty_dir(path)
        return

    def _delete_empty_dir(self, path: str) -> bool:
        """目录确认无任何条目时删除它；其余情况一律保留。"""
        directories = getattr(self, "_cloud_directories", None)
        if not directories or not self._cloud_batch_mutations:
            return False
        try:
            lookup = directories.resolve_directory(path)
            directory_id = getattr(lookup, "directory_id", None)
            if not directory_id:
                return False
            listing = directories.list_directory(str(directory_id))
            if not getattr(listing, "checked", False) or listing.files:
                return False
            deleted = self._cloud_batch_mutations.delete_files([str(directory_id)])
            if not deleted:
                logger.debug(f"空暂存目录删除失败（网盘未删除）：{path}")
                return False
            if getattr(directories.resolve_directory(path), "directory_id", None):
                logger.debug(f"空暂存目录删除未生效，下次继续回收：{path}")
                return False
            logger.debug(f"已清理空的暂存目录：{path}")
            return True
        except Exception as error:
            logger.debug(f"清理暂存目录失败：{path}，{error}")
            return False

    def _staging_cleanup_candidates(
            self, item: Dict[str, Any], ctx: "PostprocessBatchContext"
    ) -> List[str]:
        """本项用过、可参与空目录回收的暂存目录（转存根目录与最终目录除外）。"""
        candidates: List[str] = []
        root = str(getattr(self, "_cloud_transfer_path", "") or "").rstrip("/")
        excluded = {"/", root, str(item.get("cloud_dir") or "").rstrip("/")}
        located = item.get("_located_source_dir")
        values: List[Any] = [item.get("staging_dir")]
        if isinstance(located, (list, tuple, set)):
            values.extend(located)
        else:
            values.append(located)
        # 订阅级暂存目录由上下文推导，避免扫描猜测目录名
        values.append(self._context_staging_dir(item, ctx))
        for value in values:
            path = str(value or "").rstrip("/")
            if not path or path in excluded or path in candidates:
                continue
            candidates.append(path)
        return candidates

    def _context_staging_dir(self, item: Dict[str, Any], ctx: "PostprocessBatchContext") -> str:
        """按订阅上下文（订阅 + 媒体 + 季）推导任务使用的暂存子目录。"""
        try:
            subscribe_id = int(item.get("subscribe_id") or 0)
        except (TypeError, ValueError):
            subscribe_id = 0
        subscribe = ctx.subscribe_cache.get(subscribe_id) if subscribe_id else None
        media_data = item.get("mediainfo") or {}
        deserialize = getattr(self, "_deserialize_mediainfo", None)
        label_func = getattr(self, "_staging_label", None)
        if not subscribe or not media_data or not callable(deserialize) or not callable(label_func):
            return ""
        try:
            mediainfo = deserialize(dict(media_data))
        except Exception:
            return ""
        if not mediainfo:
            return ""
        transfer_root = str(getattr(self, "_cloud_transfer_path", "") or "").rstrip("/")
        label = str(label_func(subscribe, mediainfo, item.get("season")) or "").strip()
        if not transfer_root or not label:
            return ""
        return f"{transfer_root}/{label}"

    def _settle_pending_item(
            self,
            item: Dict[str, Any],
            pending_key: str,
            ctx: PostprocessBatchContext,
            *,
            detail: str,
            item_failed: bool = False,
            reason: str = "",
    ) -> None:
        """结算单项：登记历史状态、移出待处理并收尾前端任务进度。"""
        if item_failed:
            ctx.settled_failure[pending_key] = str(reason or "")
        else:
            ctx.settled_success.add(pending_key)
        ctx.pending.pop(pending_key, None)
        ctx.committed_keys.add(pending_key)
        if item_failed:
            ctx.failed += 1
        else:
            ctx.completed += 1
        # 无论成败都登记本项用过的暂存目录，批次结束只回收确认空的目录
        for candidate in self._staging_cleanup_candidates(item, ctx):
            ctx.staging_dirs_to_clean.add(candidate)
        task_id = self._postprocess_task_id(item)
        if not task_id or not self._postprocess_task_update:
            return
        payload: Dict[str, Any] = {
            "file_completed": True,
            "postprocess_active": False,
            "postprocess_detail": detail,
        }
        if item_failed:
            payload["item_failed"] = True
        self._postprocess_task_update(task_id, _pending_key=pending_key, **payload)

    def _abort_single_item(
            self,
            item: Dict[str, Any],
            pending_key: str,
            reason: str,
            ctx: PostprocessBatchContext,
    ) -> None:
        """单项后处理定位或执行失败后，彻底结算该项，更新历史并通知前端停止该项。"""
        logger.warning(
            f"文件后处理失败并停止：{item.get('file_name') or pending_key}，原因：{reason}"
        )
        self._settle_pending_item(
            item, pending_key, ctx,
            detail=f"定位失败：{reason}",
            item_failed=True,
            reason=reason,
        )

    def _finalize_ready_item(
            self,
            item: Dict[str, Any],
            pending_key: str,
            strm_path: Optional[Path],
            media: Any,
            media_data: Dict[str, Any],
            ctx: PostprocessBatchContext,
    ) -> None:
        """处理就绪项的洗版旧文件删除并最终提交。"""
        is_replacement = item.get("upgrade") and str(
            item.get("upgrade_mode") or self._upgrade_mode
        ) != "coexist"
        if is_replacement and self._cloud_batch_mutations:
            backup_id = self._upgrade_old_file_id(
                item, self._snapshot_getter(ctx)
            )
            if backup_id:
                ctx.upgrade_delete_batch[pending_key] = {
                    "item": item,
                    "file_id": backup_id,
                    "strm_path": strm_path,
                    "media": media,
                    "media_data": media_data,
                }
                return
        if is_replacement and not self._delete_upgrade_old_file(
                item, self._snapshot_getter(ctx)
        ):
            if self._finalize_failure(item, pending_key):
                return
            self._schedule_finalize_retry(item, ctx.now)
            return
        self._commit_single_item(
            item, pending_key, strm_path, media, media_data, ctx
        )

    def _finalize_after_metadata(
            self,
            item: Dict[str, Any],
            pending_key: str,
            file_name: str,
            strm_path: Optional[Path],
            media: Any,
            media_data: Dict[str, Any],
            ctx: PostprocessBatchContext,
    ) -> None:
        """登记刮削与终态提交请求，由批次统一执行（先批量刮削，再批量登记完成状态）。"""
        if (
                ctx.organize_enabled
                and media
                and self._metadata_scraper
                and self._local_resource_path
                and (self._nfo_scrape_enabled or self._image_scrape_enabled)
        ):
            self._update_postprocess_progress(item, pending_key, "metadata")
            ctx.scrape_requests.append(ScrapeRequest(
                media=media,
                season=item.get("season"),
                payload={
                    "cloud_dir": item["cloud_dir"],
                    "file_name": file_name,
                    "notification_episodes": (
                        [item.get("episode")] if item.get("episode") else []
                    ),
                },
            ))
        ctx.commit_requests.append(
            CommitRequest(item, pending_key, strm_path, media, media_data)
        )

    def _needs_upgrade_backup(
            self, ctx: PostprocessBatchContext, item: Dict[str, Any], pending_key: str
    ) -> bool:
        """是否需要为洗版旧文件做批量备份避让。"""
        task = ctx.task_map.get(str(item.get("task_id") or pending_key).upper())
        task_type = self._task_type(item)
        if task_type == "magnet":
            return False
        if task_type in self._FILE_WAIT_TASK_TYPES and not bool(
                task and task.get("completed")
        ):
            return False
        if not item.get("upgrade") or item.get("upgrade_old_backed_up"):
            return False
        return str(item.get("upgrade_mode") or self._upgrade_mode) != "coexist"

    def _run_grouped_mutation(
            self,
            ctx: PostprocessBatchContext,
            groups: Dict[str, Dict[str, Dict[str, Any]]],
            mutate: Callable[[str, Dict[str, Dict[str, Any]]], Dict[str, Any]],
            label: str,
            apply: Callable[..., None],
    ) -> None:
        """分组执行网盘批量操作；``mutate`` 固定以 (目录, 条目) 调用。"""
        for group_dir, group_items in groups.items():
            first_key = next(iter(group_items), "")
            first_item = ctx.pending.get(first_key)
            if first_item:
                self._update_postprocess_progress(
                    first_item, first_key, "organize",
                    f"{label} {len(group_items)} 个文件",
                )
            results = mutate(group_dir, group_items) or {}
            for pending_key, result in results.items():
                if pending_key in ctx.pending:
                    apply(ctx, group_dir, pending_key, result, group_items)

    @staticmethod
    def _apply_upgrade_backup(
            ctx: PostprocessBatchContext,
            group_dir: str,
            pending_key: str,
            backup_file: Any,
            group_items: Dict[str, Dict[str, Any]],
    ) -> None:
        """记录洗版旧文件备份结果并同步内存目录索引。"""
        item = ctx.pending[pending_key]
        item["upgrade_old_backed_up"] = True
        item["upgrade_old_file_id"] = str(
            getattr(backup_file, "id", "") or item.get("upgrade_old_file_id") or ""
        )
        origin = group_items.get(pending_key)
        if origin:
            ctx.sync_rename(group_dir, origin["item"], backup_file)

    @staticmethod
    def _apply_rename(
            ctx: PostprocessBatchContext,
            group_dir: str,
            pending_key: str,
            target_file: Any,
            group_items: Dict[str, Dict[str, Any]],
    ) -> None:
        """记录重命名结果，标记文件已就绪待移动。"""
        item = ctx.pending[pending_key]
        item["staging_name"] = item.get("file_name") or item.get("staging_name") or ""
        ctx.prepared_files[pending_key] = target_file
        origin = group_items.get(pending_key)
        if origin:
            ctx.sync_rename(group_dir, origin["item"], target_file)

    @staticmethod
    def _apply_move(
            ctx: PostprocessBatchContext,
            group_dir: str,
            pending_key: str,
            target_file: Any,
            group_items: Dict[str, Dict[str, Any]],
    ) -> None:
        """记录移动结果，标记文件已进入最终媒体目录。"""
        item = ctx.pending[pending_key]
        item["moved_at"] = ctx.now
        ctx.moved_files[pending_key] = target_file
        ctx.sync_move(str(item.get("staging_dir") or "/"), group_dir, target_file)

    def _collect_upgrade_backup_groups(
            self, ctx: PostprocessBatchContext
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """收集需要备份避让的洗版旧文件，按目录分组。"""
        groups: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for pending_key in ctx.due_keys:
            item = ctx.pending.get(pending_key) or {}
            if not self._needs_upgrade_backup(ctx, item, pending_key):
                continue
            old_dir = str(
                item.get("upgrade_old_cloud_dir") or item.get("cloud_dir") or "/"
            ).rstrip("/") or "/"
            old_name = str(item.get("upgrade_old_file_name") or "").strip()
            old_id = str(item.get("upgrade_old_file_id") or "").strip()
            directory_valid, old_index = self._snapshot(ctx, old_dir)
            if not directory_valid:
                continue
            old_file = next(
                (
                    value for value in old_index.values()
                    if old_id and str(getattr(value, "id", "")) == old_id
                ),
                None,
            ) or old_index.get(old_name)
            if not old_file:
                continue
            backup_name = self._upgrade_backup_name(
                old_name, item.get("task_id") or pending_key
            )
            groups.setdefault(old_dir, {})[pending_key] = {
                "item": old_file,
                "target_name": backup_name,
            }
            item["upgrade_backup_name"] = backup_name
        return groups

    def _collect_rename_groups(
            self, ctx: PostprocessBatchContext
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """收集需要重命名的暂存文件，按暂存目录分组。"""
        groups: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for pending_key in ctx.due_keys:
            item = ctx.pending.get(pending_key) or {}
            task_type = self._task_type(item)
            is_replacement = item.get("upgrade") and str(
                item.get("upgrade_mode") or self._upgrade_mode
            ) != "coexist"
            if (
                    task_type in self._OFFLINE_TASK_TYPES
                    or item.get("moved_at")
                    or (is_replacement and not item.get("upgrade_old_backed_up"))
            ):
                continue
            task = ctx.task_map.get(str(item.get("task_id") or pending_key).upper())
            staging_dir = str(
                item.get("staging_dir") or item.get("cloud_dir") or "/"
            ).rstrip("/") or "/"
            directory_valid, file_index = self._snapshot(ctx, staging_dir)
            if not directory_valid:
                continue
            file_name = str(item.get("file_name") or pending_key)
            staging_name = str(item.get("staging_name") or item.get("file_name") or "")
            # 暂存目录已是目标文件名，直接进入待移动队列
            if file_name and file_name in file_index:
                ctx.prepared_files[pending_key] = file_index[file_name]
                continue
            source_file = self._locate_cloud_file(
                staging_dir,
                [staging_name, str((task or {}).get("name") or "").strip()],
                self._extract_item_hash(item),
                ctx,
                item=item,
            )
            if not source_file:
                continue
            if not ctx.organize_enabled or getattr(source_file, "name", "") == file_name:
                ctx.prepared_files[pending_key] = source_file
                continue
            groups.setdefault(staging_dir, {})[pending_key] = {
                "item": source_file,
                "target_name": file_name,
            }
        return groups

    def _collect_move_groups(
            self, ctx: PostprocessBatchContext
    ) -> Dict[str, Dict[str, Any]]:
        """按最终目录分组待移动文件；无需移动的直接就绪。"""
        groups: Dict[str, Dict[str, Any]] = {}
        for pending_key, target_file in ctx.prepared_files.items():
            item = ctx.pending.get(pending_key) or {}
            staging_dir = str(item.get("staging_dir") or "/").rstrip("/") or "/"
            final_dir = str(item.get("cloud_dir") or "/").rstrip("/") or "/"
            if staging_dir == final_dir or not ctx.organize_enabled:
                # 整理关闭时：将 cloud_dir 修正为转存路径，文件保留原位
                if not ctx.organize_enabled and staging_dir != final_dir:
                    item["cloud_dir"] = staging_dir
                    item["file_name"] = getattr(
                        target_file, "name", item.get("file_name") or ""
                    )
                    logger.debug(
                        f"转存后整理已关闭，保留在转存目录（批量阶段）："
                        f"{staging_dir}/{getattr(target_file, 'name', '')}"
                    )
                item["moved_at"] = ctx.now
                ctx.moved_files[pending_key] = target_file
                continue
            groups.setdefault(final_dir, {})[pending_key] = target_file
        return groups

    def _execute_batch_cloud_mutations(self, ctx: PostprocessBatchContext) -> None:
        """统一执行洗版旧文件批量避让、新文件批量重命名与批量移动。"""
        if not self._cloud_batch_mutations:
            return
        self._run_grouped_mutation(
            ctx,
            self._collect_upgrade_backup_groups(ctx),
            self._cloud_batch_mutations.rename_files,
            "批量备份",
            self._apply_upgrade_backup,
        )
        self._run_grouped_mutation(
            ctx,
            self._collect_rename_groups(ctx),
            self._cloud_batch_mutations.rename_files,
            "批量重命名",
            self._apply_rename,
        )
        # 注意：rename_files(path, items) 与 move_files(items, save_path) 参数顺序不同
        self._run_grouped_mutation(
            ctx,
            self._collect_move_groups(ctx),
            lambda group_dir, group_items: self._cloud_batch_mutations.move_files(
                group_items, group_dir
            ),
            "批量移动",
            self._apply_move,
        )

    def _should_skip_for_stop(
            self, item: Dict[str, Any], pending_key: str, ctx: PostprocessBatchContext
    ) -> bool:
        """任务收到停止请求时，核验网盘中是否已有现成文件，保护已有物理文件不被遗弃。"""
        if not self._stop_requested():
            return False
        if not self._has_existing_cloud_file(item, pending_key, ctx):
            logger.info(f"任务停止：目标文件尚未就绪，安全跳过并终止：{item.get('file_name') or pending_key}")
            self._abort_single_item(item, pending_key, "任务已被用户主动停止，终止后处理", ctx)
            return True
        return False

    def _handle_finalize_dead_item(
            self, item: Dict[str, Any], pending_key: str, ctx: PostprocessBatchContext
    ) -> None:
        """处理达到连续失败上限的死任务。"""
        fail_count = int(item.get("fail_count") or 0)
        reason = self._FINALIZE_DEAD_REASON.format(fail_count)
        self._notify_finalize_dead(item, pending_key)
        self._abort_single_item(item, pending_key, reason, ctx)

    @property
    def _offline_timeout_seconds(self) -> float:
        """离线下载总超时（秒）；配置缺失时回退 30 分钟。"""
        try:
            value = float(getattr(self, "_OFFLINE_TIMEOUT", 1800) or 1800)
        except (TypeError, ValueError):
            value = 1800.0
        return max(60.0, value)

    def _poll_offline_ready(
            self, item: Dict[str, Any], pending_key: str, ctx: PostprocessBatchContext
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        """统一检查网盘离线任务就绪状态。"""
        task_id = str(item.get("task_id") or "").strip().upper()
        task = ctx.task_map.get(task_id) if task_id else None

        # 1. 任务明确失败
        if task and bool(task.get("failed")):
            reason = f"离线下载失败：{task.get('status_text') or '任务错误'}"
            self._add_offline_blacklist(item.get("share_url") or pending_key, reason)
            self._cleanup_failed_offline_task(item, reason)
            self._abort_single_item(item, pending_key, reason, ctx)
            return task, False

        # 2. 检查完成状态（任务标记完成 或 暂存目录中【本文件】已就位）
        staging_dir = str(item.get("staging_dir") or item.get("cloud_dir") or "/")
        task_done = bool(
            item.get("moved_at")
            or (task and task.get("completed"))
        )
        directory_valid, file_index = (
            self._snapshot(ctx, staging_dir)
            if not task_done and task is None else (False, {})
        )
        if not task_done and directory_valid and file_index:
            if self._task_type(item) == "magnet":
                # 磁力整包：目录出现内容即交由 Magnet 流程整理
                task_done = True
            else:
                # ed2k/离线：目录里有别的集不等于本集下完，按名称/哈希/模糊名/季集精确匹配
                candidate_names = [
                    str(item.get("staging_name") or ""),
                    str(item.get("file_name") or ""),
                    str(item.get("source_file_name") or ""),
                ]
                target_hash = self._extract_item_hash(item)
                task_done = bool(
                    self._match_by_name(file_index, candidate_names)
                    or self._match_by_hash(file_index, target_hash)
                    or self._match_by_fuzzy_name(file_index, candidate_names)
                    or self._match_by_season_episode(file_index, item, candidate_names)
                )

        if task_done:
            item.setdefault("download_completed_at", ctx.now)
            return task, True

        # 3. 未完成：判断超时或调度重试
        created_at = float(item.get("created_at") or ctx.now)
        timeout_mins = max(1, int(self._offline_timeout_seconds // 60))
        if ctx.now - created_at >= self._offline_timeout_seconds:
            reason = f"离线下载超过 {timeout_mins} 分钟未完成，已退出"
            self._add_offline_blacklist(item.get("share_url") or pending_key, reason)
            self._cleanup_failed_offline_task(item, reason)
            self._abort_single_item(item, pending_key, reason, ctx)
            return task, False

        self._schedule_finalize_retry(item, ctx.now)
        retry_at = float(item.get("next_check_at") or ctx.now)
        retry_minutes = max(1, int(max(0, retry_at - ctx.now) + 59) // 60)
        self._update_postprocess_progress(
            item, pending_key, "locate", f"等待离线下载完成（将在 {retry_minutes} 分钟后复查）"
        )
        return task, False

    def _finalize_magnet_due_item(
            self,
            item: Dict[str, Any],
            pending_key: str,
            ctx: PostprocessBatchContext,
            task: Optional[Dict[str, Any]],
    ) -> None:
        """整理 Magnet 下载文件包。"""
        self._update_postprocess_progress(
            item, pending_key, "organize", "整理 Magnet 下载文件"
        )
        finalized = self._finalize_magnet_package(
            item, pending_key, ctx, offline_task=task
        )
        if finalized is None:
            if not self._finalize_failure(item, pending_key):
                self._schedule_finalize_retry(item, ctx.now)
            return
        ctx.pending.pop(pending_key, None)
        ctx.committed_keys.add(pending_key)
        task_id = self._postprocess_task_id(item)
        if task_id and self._postprocess_task_update:
            self._postprocess_task_update(
                task_id,
                _pending_key=pending_key,
                file_completed=True,
                postprocess_active=False,
                postprocess_detail="Magnet 任务完成",
            )
        if finalized:
            self._update_postprocess_progress(
                item, pending_key, "commit", "登记文件和通知结果"
            )
            ctx.finalized_details.extend(finalized)
            ctx.notification_contexts.append((item, pending_key))
            ctx.completed += len(finalized)
        else:
            self._add_offline_blacklist(
                item.get("share_url") or pending_key,
                "Magnet 下载完成但未匹配到目标媒体文件",
            )
            ctx.failed += 1

    def _organize_item_subtitles(
            self,
            item: Dict[str, Any],
            pending_key: str,
            ctx: PostprocessBatchContext,
            file_name: str,
            strm_path: Optional[Path] = None,
    ) -> None:
        """整理伴随字幕；未完成仅记录日志，不影响主文件继续处理。"""
        if not (item.get("subtitles") and getattr(self, "_organize_subtitles", True)):
            return
        self._update_postprocess_progress(item, pending_key, "subtitle")
        if not self._finalize_subtitle_files(
                item, self._snapshot_getter(ctx), strm_path=strm_path
        ):
            logger.debug(f"伴随字幕整理未完成，跳过字幕继续处理视频主文件：{file_name}")

    def _refresh_dynamic_target_dir(
            self,
            item: Dict[str, Any],
            pending_key: str,
            ctx: PostprocessBatchContext,
            staging_dir: str,
            staging_name: str,
            file_name: str,
    ) -> str:
        """离线任务尚未移动时，按平台分类规则重算目标媒体目录与文件名。"""
        if not ctx.organize_enabled:
            return file_name
        current_final = str(item.get("cloud_dir") or "").rstrip("/") or "/"
        if current_final != staging_dir:
            return file_name
        try:
            calc_media, _ = self._restore_pending_media_context(item, pending_key)
            if not calc_media:
                return file_name
            calc_sub = ctx.subscribe_cache.get(int(item.get("subscribe_id") or 0))
            season_val = (
                max(1, int(item.get("season") or 1))
                if calc_media.type == MediaType.TV else None
            )
            target_episodes = [
                int(v) for v in (item.get("target_episodes") or []) if int(v) > 0
            ]
            dyn_dir, dyn_name = self._platform_target(
                self._CLOUD_MEDIA_ROOT, calc_sub, calc_media, staging_name,
                season=season_val,
                episode=target_episodes[0] if target_episodes else (
                    int(item["episode"]) if item.get("episode") else None
                ),
            )
            if not dyn_dir:
                return file_name
            item["cloud_dir"] = dyn_dir
            source_suffix = Path(staging_name).suffix
            if source_suffix and not dyn_name.endswith(source_suffix):
                dyn_name = f"{Path(dyn_name).stem}{source_suffix}"
            item["file_name"] = dyn_name
            return dyn_name
        except Exception as dyn_err:
            logger.debug(f"动态计算离线文件目标媒体路径异常：{dyn_err}")
            return file_name

    def _candidate_file_names(
            self,
            ctx: PostprocessBatchContext,
            item: Dict[str, Any],
            pending_key: str,
            file_name: str,
            staging_name: str,
    ) -> List[str]:
        """网盘定位候选名：目标名、暂存名、离线任务名、ed2k 名、资源清单名。"""
        task = ctx.task_map.get(str(item.get("task_id") or pending_key).upper())
        task_name = (
            str((task or {}).get("name") or "").strip()
            if self._task_type(item) in self._FILE_WAIT_TASK_TYPES else ""
        )
        ed2k_url_name = extract_ed2k_filename(str(item.get("share_url") or ""))
        res_file_list = (item.get("resource") or {}).get("file_list")
        res_file_name = (
            str(res_file_list[0]).strip()
            if isinstance(res_file_list, list) and res_file_list else ""
        )
        return [file_name, staging_name, task_name, ed2k_url_name, res_file_name]

    def _recover_target_in_final_dir(
            self,
            item: Dict[str, Any],
            pending_key: str,
            ctx: PostprocessBatchContext,
            file_name: str,
            staging_dir: str,
            candidates: List[str],
            target_hash: str,
    ) -> Tuple[Optional[Any], str]:
        """已在最终目录找回文件时直接就绪，必要时按目标名重命名。"""
        final_dir = str(item.get("cloud_dir") or "/").rstrip("/") or "/"
        if final_dir == staging_dir:
            return None, file_name
        final_target = self._locate_cloud_file(
            final_dir, candidates, target_hash, ctx, item=item
        )
        if not final_target:
            return None, file_name
        item["moved_at"] = ctx.now
        item["_located_source_dir"] = final_dir
        ctx.moved_files[pending_key] = final_target
        if getattr(final_target, "name", "") != file_name:
            source_suffix = Path(final_target.name).suffix
            if source_suffix and not file_name.endswith(source_suffix):
                file_name = f"{Path(file_name).stem}{source_suffix}"
                item["file_name"] = file_name
            if self._cloud_mutations.rename_file(final_dir, final_target, file_name):
                renamed_target = self._cloud_query.get_cached_file(final_dir, file_name)
                if renamed_target:
                    ctx.sync_rename(final_dir, final_target, renamed_target)
                    final_target = renamed_target
                    ctx.moved_files[pending_key] = renamed_target
        logger.info(
            f"后处理在最终目录找到文件并就绪，继续生成STRM："
            f"{final_dir}/{getattr(final_target, 'name', file_name)}"
        )
        return final_target, file_name

    def _organize_and_finalize_single_item(
            self, item: Dict[str, Any], pending_key: str, ctx: PostprocessBatchContext
    ) -> None:
        """执行单文件的物理定位、自愈匹配、重命名/移动、洗版替换、字幕与 STRM 生成。"""
        task_type = self._task_type(item)
        file_name = str(item.get("file_name") or pending_key)
        created_at = float(item.get("created_at") or ctx.now)

        already_moved = bool(item.get("moved_at"))
        staging_dir = str(
            item.get("cloud_dir") if already_moved
            else item.get("staging_dir") or item.get("cloud_dir") or "/"
        ).rstrip("/") or "/"
        staging_name = (
            file_name if already_moved
            else str(item.get("staging_name") or file_name)
        )

        if not already_moved and task_type in self._FILE_WAIT_TASK_TYPES:
            file_name = self._refresh_dynamic_target_dir(
                item, pending_key, ctx, staging_dir, staging_name, file_name
            )

        # 仅在尚未就绪物理文件时才执行网盘检索与状态广播，避免重复检查与多余推送
        target_file = ctx.moved_files.get(pending_key) or ctx.prepared_files.get(pending_key)
        if not target_file:
            self._update_postprocess_progress(
                item, pending_key, "locate", f"在 {staging_dir} 定位 {staging_name}"
            )
            candidates = self._candidate_file_names(
                ctx, item, pending_key, file_name, staging_name
            )
            target_hash = self._extract_item_hash(item)
            target_file = self._locate_cloud_file(
                staging_dir, candidates, target_hash, ctx, item=item
            )
            if target_file:
                ctx.prepared_files[pending_key] = target_file
                item["_located_source_dir"] = staging_dir
            elif not already_moved:
                target_file, file_name = self._recover_target_in_final_dir(
                    item, pending_key, ctx, file_name, staging_dir,
                    candidates, target_hash,
                )
                if target_file:
                    already_moved = True
                    staging_dir = str(item.get("cloud_dir") or "/").rstrip("/") or "/"

        if not target_file:
            fail_count = int(item.get("fail_count") or 0) + 1
            item["fail_count"] = fail_count
            is_offline_downloading = (
                    task_type in self._OFFLINE_TASK_TYPES
                    and not bool(item.get("download_completed_at"))
            )
            # 若是普通转存/整理或已完成下载的离线任务，最多允许重试 1 次（给网盘极短暂的元数据刷新时间）；
            # 只有正在下载中的离线任务才允许重试更多次
            max_failures = self._FINALIZE_MAX_FAILURES if is_offline_downloading else 2
            ready_at = float(item.get("download_completed_at") or created_at)
            timeout_seconds = self._offline_timeout_seconds if is_offline_downloading else 120.0

            if fail_count >= max_failures or (ctx.now - ready_at >= timeout_seconds):
                reason = (
                    f"网盘文件定位失败（在转存路径 {staging_dir} 及目标路径均未找到匹配文件，已尝试 {fail_count} 次）"
                )
                self._abort_single_item(item, pending_key, reason, ctx)
            else:
                self._schedule_finalize_retry(item, ctx.now)
            return

        if item.get("upgrade") and str(
                item.get("upgrade_mode") or self._upgrade_mode
        ) != "coexist":
            self._prime_upgrade_old_target(item)
            self._update_postprocess_progress(
                item, pending_key, "organize", "替换旧版本文件"
            )
            replaced_file = self._replace_upgrade_file(
                item=item,
                pending_key=pending_key,
                target_file=target_file,
                staging_dir=staging_dir,
                file_name=file_name,
                now=ctx.now,
                directory_snapshot=self._snapshot_getter(ctx),
            )
            if not replaced_file:
                return
            target_file = replaced_file
            already_moved = True

        if not already_moved and not ctx.organize_enabled:
            item["cloud_dir"] = staging_dir
            item["file_name"] = getattr(target_file, "name", staging_name)
            item["moved_at"] = ctx.now
            already_moved = True
            logger.debug(
                f"转存后整理已关闭，保留在转存目录：{staging_dir}/{item['file_name']}"
            )

        if not already_moved:
            self._update_postprocess_progress(item, pending_key, "organize")
            if target_file.name != file_name:
                source_suffix = Path(target_file.name).suffix
                if source_suffix and not file_name.endswith(source_suffix):
                    file_name = f"{Path(file_name).stem}{source_suffix}"
                    item["file_name"] = file_name
                staging_valid, staging_index = self._snapshot(ctx, staging_dir)
                if staging_valid and file_name in staging_index and str(staging_index[file_name].id) == str(
                        target_file.id):
                    target_file = staging_index[file_name]
                else:
                    orig_target = target_file
                    if not self._cloud_mutations.rename_file(
                            staging_dir, target_file, file_name
                    ):
                        if self._finalize_failure(item, pending_key):
                            self._abort_single_item(
                                item, pending_key, f"网盘文件重命名失败，已重试 {item.get('fail_count', 0)} 次", ctx
                            )
                        else:
                            self._schedule_finalize_retry(item, ctx.now)
                        return
                    item["staging_name"] = file_name
                    target_file = self._cloud_query.get_cached_file(
                        staging_dir, file_name
                    )
                    if not target_file:
                        if self._finalize_failure(item, pending_key):
                            self._abort_single_item(
                                item, pending_key, f"重命名后定位失败，已重试 {item.get('fail_count', 0)} 次", ctx
                            )
                        else:
                            self._schedule_finalize_retry(item, ctx.now)
                        return
                    ctx.sync_rename(staging_dir, orig_target, target_file)

            final_dir = str(item["cloud_dir"]).rstrip("/") or "/"
            if staging_dir != final_dir:
                final_valid, final_index = self._snapshot(ctx, final_dir)
                existing_in_final = final_index.get(file_name) if final_valid else None
                source_size = int(item.get("file_size") or getattr(target_file, "size", 0) or 0)
                existing_size = int(getattr(existing_in_final, "size", 0) or 0)
                matched_hash = False
                for algo in self._supported_hash_algorithms():
                    source_val = str(getattr(target_file, algo, "") or item.get(f"source_{algo}") or item.get(
                        algo) or "").strip().upper()
                    existing_val = str(getattr(existing_in_final, algo, "") or "").strip().upper()
                    if source_val and existing_val and source_val == existing_val:
                        matched_hash = True
                        break
                matched_existing = bool(
                    existing_in_final and (
                            matched_hash
                            or (source_size > 0 and existing_size > 0 and abs(source_size - existing_size) <= 1024)
                            or (existing_size > 0 and not source_size)
                            or str(existing_in_final.id) == str(target_file.id)
                    )
                )
                if matched_existing:
                    logger.debug(
                        f"媒体目录已存在目标文件，自愈复用并跳过移动：{final_dir}/{file_name}"
                    )
                    if str(existing_in_final.id) != str(target_file.id):
                        try:
                            self._cloud_mutations.delete_file(target_file.id)
                        except Exception as clean_err:
                            logger.debug(f"清理暂存区多余副本失败：{clean_err}")
                    moved_file = existing_in_final
                else:
                    moved_file = self._cloud_mutations.move_file(
                        target_file, item["cloud_dir"], file_name
                    )
            else:
                moved_file = target_file
            if not moved_file:
                final_valid, final_index = self._snapshot(ctx, final_dir)
                if final_valid and file_name in final_index:
                    moved_file = final_index[file_name]
                    logger.debug(
                        f"移动操作返回失败但在目标目录找到文件，自愈恢复：{final_dir}/{file_name}"
                    )
                else:
                    if self._finalize_failure(item, pending_key):
                        self._abort_single_item(
                            item, pending_key, f"移动到媒体目录失败，已重试 {item.get('fail_count', 0)} 次", ctx
                        )
                    else:
                        self._schedule_finalize_retry(item, ctx.now)
                    return
            ctx.sync_move(staging_dir, final_dir, moved_file)
            target_file = moved_file
            item["moved_at"] = ctx.now

        context_key = self._media_context_key(item)
        cached_context = (
            ctx.media_context_cache.get(context_key) if context_key else None
        )
        if cached_context:
            media, media_data = cached_context
            item["mediainfo"] = media_data
        else:
            media, media_data = self._restore_pending_media_context(
                item, pending_key
            )
            resolved_key = context_key or self._media_context_key(item)
            if resolved_key and media:
                ctx.media_context_cache[resolved_key] = (media, media_data)

        if not ctx.organize_enabled:
            # 整理已关闭：跳过元数据刮削、跳过 STRM 生成与字幕整理，直接就绪终态
            ctx.commit_requests.append(
                CommitRequest(item, pending_key, None, media, media_data)
            )
            return

        if (
                not self._strm_generate_enabled
                or not self._strm_generator
                or not self._local_resource_path
        ):
            self._organize_item_subtitles(item, pending_key, ctx, file_name)
            self._finalize_after_metadata(
                item, pending_key, file_name, None, media, media_data, ctx
            )
            return

        self._update_postprocess_progress(item, pending_key, "strm")
        strm_path = self._generate_strm(
            item["cloud_dir"], file_name, target_file=target_file, log_success=False
        )
        if strm_path and not self._strm_file_ready(strm_path):
            logger.error(f"STRM 生成后文件不存在或为空：{strm_path}")
            strm_path = None
        if strm_path:
            self._organize_item_subtitles(
                item, pending_key, ctx, file_name, strm_path=strm_path
            )
            if not self._strm_file_ready(strm_path):
                logger.error(f"洗版后 STRM 文件不存在或为空：{strm_path}")
                self._schedule_finalize_retry(item, ctx.now)
                return
            self._finalize_after_metadata(
                item, pending_key, file_name, strm_path, media, media_data, ctx
            )
            return

        ready_at = float(item.get("download_completed_at") or created_at)
        if ctx.now - ready_at >= self._FILE_FINALIZE_TIMEOUT:
            self._settle_pending_item(
                item, pending_key, ctx,
                detail="文件已下载但30分钟内仍无法生成 STRM",
                item_failed=True,
                reason="文件已下载但30分钟内仍无法生成 STRM",
            )
        else:
            self._schedule_finalize_retry(item, ctx.now)

    def _process_due_items(self, ctx: PostprocessBatchContext) -> None:
        """按序调度处理所有到期的后处理任务。"""
        for pending_key in ctx.due_keys:
            item = ctx.pending.get(pending_key)
            if not item or self._should_skip_for_stop(item, pending_key, ctx):
                continue
            if item.get("finalize_dead"):
                self._handle_finalize_dead_item(item, pending_key, ctx)
                continue
            if self._dispatch_offline_item(item, pending_key, ctx):
                continue
            self._organize_and_finalize_single_item(item, pending_key, ctx)

    def _dispatch_offline_item(
            self, item: Dict[str, Any], pending_key: str, ctx: PostprocessBatchContext
    ) -> bool:
        """离线类任务先行处理；未就绪或已交由 Magnet 流程时返回 True。"""
        task_type = self._task_type(item)
        if task_type not in self._OFFLINE_TASK_TYPES:
            return False
        task, ready = self._poll_offline_ready(item, pending_key, ctx)
        if not ready:
            return True
        if task_type == "magnet":
            self._finalize_magnet_due_item(item, pending_key, ctx, task)
            return True
        return False

    @staticmethod
    def _metadata_group_key(media: Any, season: Any) -> Tuple[Any, ...]:
        """同媒体同季合并为一次批量刮削。"""
        return (
            str(getattr(media, "tmdb_id", "") or ""),
            str(getattr(media, "type", "") or ""),
            str(getattr(media, "title", "") or ""),
            int(season or 0),
        )

    def _batch_stage_reporter(
            self,
            task_ids: List[str],
            stage: str,
    ) -> Callable[[int, int, str], None]:
        """把批量阶段（刮削/登记）的逐文件进度上报到任务卡片，供融合进度动态计算。"""
        completed_key = f"_postprocess_{stage}_completed"
        total_key = f"_postprocess_{stage}_total"

        def report(index: int, total: int, _name: str = "") -> None:
            if not self._postprocess_task_update or not task_ids:
                return
            payload = {completed_key: int(index), total_key: int(total)}
            for task_id in task_ids:
                self._postprocess_task_update(task_id, **payload)

        return report

    def _batch_task_ids(self, items: List[Dict[str, Any]]) -> List[str]:
        """收集批次内所有文件所属的任务卡片 ID（去重保序）。"""
        task_ids: List[str] = []
        for item in items:
            task_id = self._postprocess_task_id(item)
            if task_id and task_id not in task_ids:
                task_ids.append(task_id)
        return task_ids

    def _run_metadata_requests(self, ctx: PostprocessBatchContext) -> None:
        """按媒体与季合并本批次刮削请求，一次补齐全部缺失元数据。"""
        requests, ctx.scrape_requests = ctx.scrape_requests, []
        batch_task_ids = self._batch_task_ids(list(ctx.pending.values()))
        if self._postprocess_task_update:
            # 进入批量阶段：进度改由实际文件数驱动，避免按文件序号估算
            for task_id in batch_task_ids:
                self._postprocess_task_update(task_id, _postprocess_batch_active=True)
        groups: Dict[Tuple[Any, ...], List[ScrapeRequest]] = {}
        for request in requests:
            groups.setdefault(
                self._metadata_group_key(request.media, request.season), []
            ).append(request)
        for group in groups.values():
            payloads = [r.payload for r in group if r.payload.get("file_name")]
            if payloads:
                task_ids = self._batch_task_ids(payloads) or batch_task_ids
                self._scrape_metadata_batch(
                    payloads,
                    group[0].media,
                    season=group[0].season,
                    progress=self._batch_stage_reporter(task_ids, "scrape"),
                )

    def _clear_batch_stage_counters(self, task_ids: List[str], stage: str) -> None:
        """批量阶段结束后清空计数器，避免上一轮工作量污染下一轮融合进度。"""
        if not self._postprocess_task_update or not task_ids:
            return
        payload = {
            f"_postprocess_{stage}_completed": 0,
            f"_postprocess_{stage}_total": 0,
        }
        for task_id in task_ids:
            self._postprocess_task_update(task_id, **payload)

    def _run_commit_requests(self, ctx: PostprocessBatchContext) -> None:
        """批量刮削完成后，按原顺序统一登记各文件终态与完成状态。"""
        requests, ctx.commit_requests = ctx.commit_requests, []
        for request in requests:
            self._finalize_ready_item(
                request.item,
                request.pending_key,
                request.strm_path,
                request.media,
                request.media_data,
                ctx,
            )
        # 登记阶段的进度即文件结算数，批次结束后清空本轮各阶段计数与批量态
        task_ids = self._batch_task_ids([r.item for r in requests])
        self._clear_batch_stage_counters(task_ids, "scrape")
        self._clear_batch_stage_counters(task_ids, "strm")
        if self._postprocess_task_update:
            for task_id in task_ids:
                self._postprocess_task_update(task_id, _postprocess_batch_active=False)

    def _flush_settled_status(self, ctx: PostprocessBatchContext) -> None:
        """批次收尾统一回写历史状态，把逐文件 N 次整表重写收敛为每批次一次。"""
        successes, failures = ctx.settled_success, ctx.settled_failure
        ctx.settled_success, ctx.settled_failure = set(), {}
        if successes:
            self._mark_offline_history_status_batch(successes, "成功")
        by_reason: Dict[str, Set[str]] = {}
        for pending_key, reason in failures.items():
            by_reason.setdefault(reason, set()).add(pending_key)
        for reason, keys in by_reason.items():
            self._mark_offline_history_status_batch(keys, "失败", reason)

    def _flush_batch_postprocess(self, ctx: PostprocessBatchContext) -> None:
        """批次后处理收尾：批量回收洗版旧文件、批量提交订阅集数、发送批次聚合通知。"""
        self._cleanup_staging_dirs(ctx)
        if ctx.upgrade_delete_batch:
            delete_ids = list(dict.fromkeys(
                value["file_id"] for value in ctx.upgrade_delete_batch.values()
            ))
            deleted_ids = {
                str(file_id) for file_id in
                self._cloud_batch_mutations.delete_files(delete_ids)
            }
            for pending_key, value in ctx.upgrade_delete_batch.items():
                item = value["item"]
                if str(value["file_id"]) not in deleted_ids:
                    self._schedule_finalize_retry(item, ctx.now)
                    continue
                item["upgrade_old_deleted"] = True
                self._commit_single_item(
                    item,
                    pending_key,
                    value["strm_path"],
                    value["media"],
                    value["media_data"],
                    ctx,
                )
            success_count = len(deleted_ids & set(map(str, delete_ids)))
            total_count = len(delete_ids)
            logger.info(
                f"洗版旧文件批量回收完成：成功 {success_count}/{total_count} 个"
            )
            if success_count < total_count:
                logger.warning(
                    f"洗版旧文件有 {total_count - success_count} 个回收失败，"
                    "已保留后处理任务等待重试"
                )

        for batch in ctx.subscription_batches.values():
            completion_item = batch["item"]
            completion_item["success_episodes"] = sorted(batch["episodes"])
            completion_item["notification_episodes"] = sorted(
                batch["episodes"]
            )
            self._finish_pending_subscription(
                completion_item,
                batch["media_data"],
                mediainfo=batch["mediainfo"],
            )

        strm_names = [
            Path(detail["strm_path"]).name
            for detail in ctx.finalized_details
            if detail.get("strm_path")
        ]
        if strm_names:
            logger.debug(f"批量生成 STRM {len(strm_names)} 个：{strm_names}")
        if ctx.finalized_details:
            if self._notify and ctx.notification_contexts:
                progress_item, progress_key = ctx.notification_contexts[-1]
                self._update_postprocess_progress(
                    progress_item,
                    progress_key,
                    "notify",
                    f"汇总发送 {len(ctx.finalized_details)} 个文件的完成通知",
                )
            self._send_finalized_batch(ctx.finalized_details)

    def _reconcile_pending_state(self, ctx: PostprocessBatchContext) -> Dict[str, int]:
        """将批次处理结果协调写回持久化存储，并更新前端任务进度状态。"""
        with self._offline_pending_lock:
            current_pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            for pending_key in ctx.due_keys:
                original_item = ctx.pending_snapshot.get(pending_key)
                processed_item = ctx.pending.get(pending_key)
                current_item = current_pending.get(pending_key)
                if current_item is None or original_item is None:
                    continue
                if current_item.get("_monitor_token") != ctx.monitor_token:
                    continue
                generation = (
                    original_item.get("created_at"),
                    original_item.get("share_url"),
                    original_item.get("file_name"),
                    original_item.get("task_type"),
                )
                current_generation = (
                    current_item.get("created_at"),
                    current_item.get("share_url"),
                    current_item.get("file_name"),
                    current_item.get("task_type"),
                )
                if generation != current_generation:
                    continue
                if processed_item is None:
                    current_pending.pop(pending_key, None)
                    continue
                processed_item.pop("_monitor_token", None)
                processed_item.pop("_monitor_until", None)
                for field in set(original_item) | set(processed_item):
                    if original_item.get(field) == processed_item.get(field):
                        continue
                    if field in processed_item:
                        current_item[field] = copy.deepcopy(processed_item[field])
                    else:
                        current_item.pop(field, None)
            self._save_offline_pending(current_pending)
            pending_count = len(current_pending)

        result = {
            "checked": len(ctx.due_keys),
            "completed": ctx.completed,
            "failed": ctx.failed,
            "pending": pending_count,
        }
        task_items = {
            pending_key: item
            for pending_key in ctx.due_keys
            if (item := ctx.pending_snapshot.get(pending_key))
               and self._postprocess_task_id(item)
        }
        if self._postprocess_task_update:
            for pending_key, item in task_items.items():
                if pending_key in current_pending:
                    waiting_item = current_pending.get(pending_key) or item
                    is_offline = self._task_type(waiting_item) in self._OFFLINE_TASK_TYPES
                    retry_at = float(waiting_item.get("next_check_at") or ctx.now)
                    mins = max(1, int(max(0, retry_at - ctx.now) + 59) // 60)
                    wait_detail = f"等待离线下载完成（将在 {mins} 分钟后复查）" if is_offline else f"等待文件就绪（将在 {mins} 分钟后复查）"
                    self._postprocess_task_update(
                        self._postprocess_task_id(waiting_item),
                        _pending_key=pending_key,
                        postprocess_active=True,
                        postprocess_step="locate",
                        postprocess_detail=wait_detail,
                    )
                else:
                    self._postprocess_task_update(
                        self._postprocess_task_id(item),
                        _pending_key=pending_key,
                        postprocess_active=False,
                        postprocess_detail="",
                    )
        self._notify_offline_pending_changed(result["pending"])
        return result

    def monitor_offline_strm_tasks(
            self,
            force: bool = False,
            pending_keys: Optional[Set[str]] = None,
            offline_tasks: Optional[List[Dict[str, Any]]] = None,
            offline_tasks_valid: Optional[bool] = None,
    ) -> Dict[str, int]:
        """检查离线下载和网盘文件后处理；手动刷新可立即重试指定任务。"""
        ctx = self._prepare_postprocess_context(
            force=force,
            pending_keys=pending_keys,
            offline_tasks=offline_tasks,
            offline_tasks_valid=offline_tasks_valid,
        )
        if not ctx:
            return {"checked": 0, "completed": 0, "failed": 0, "pending": 0}
        if not ctx.due_keys:
            return {"checked": 0, "completed": 0, "failed": 0, "pending": len(ctx.pending)}

        try:
            self._execute_batch_cloud_mutations(ctx)
            self._process_due_items(ctx)
            self._run_metadata_requests(ctx)
            self._run_commit_requests(ctx)
        finally:
            # 收尾与暂存目录清理必须执行：中途异常也不能漏掉本轮已结算的目录
            try:
                self._flush_batch_postprocess(ctx)
                self._flush_settled_status(ctx)
            except Exception as error:
                logger.warning(f"后处理批次收尾失败：{error}")
            result = self._reconcile_pending_state(ctx)
        return result

    def _resolve_magnet_subscribe(
            self, item: Dict[str, Any], ctx: PostprocessBatchContext, mediainfo: Any
    ) -> Any:
        """解析 Magnet 任务对应的订阅；临时目标用内存对象兜底。"""
        subscribe_id = int(item.get("subscribe_id") or 0)
        subscribe = ctx.subscribe_cache.get(subscribe_id) if subscribe_id else None
        if subscribe or not (item.get("transient_target") or subscribe_id <= 0):
            return subscribe
        target_data = dict(item.get("target_subscribe") or {})
        for attr in (
                "quality", "resolution", "effect", "include", "exclude",
                "audio_quality", "audio_format", "min_bitrate", "min_bit_depth",
                "min_sample_rate", "filter", "filter_groups", "sites", "custom_words",
        ):
            target_data.setdefault(attr, None)
        target_data.setdefault("id", subscribe_id or -1)
        target_data.setdefault("name", getattr(mediainfo, "title", "未命名媒体"))
        target_data.setdefault("year", getattr(mediainfo, "year", None))
        target_data.setdefault("season", item.get("season"))
        subscribe = SimpleNamespace(**target_data)
        subscribe.to_dict = lambda: dict(subscribe.__dict__)
        return subscribe

    def _list_magnet_files(
            self, item: Dict[str, Any], offline_task: Optional[Dict[str, Any]]
    ) -> List[Any]:
        """读取 Magnet 完成后的真实文件树：任务级精准定位优先，其次递归扫描。"""
        task_files_func = getattr(self._cloud_query, "list_offline_task_files", None)
        staging_path = item.get("staging_dir") or "/"
        files: List[Any] = []
        if callable(task_files_func) and offline_task:
            try:
                files = task_files_func(offline_task, item.get("staging_dir") or item.get("cloud_dir") or "/")
            except Exception as e:
                logger.debug(f"精准定位离线任务文件异常，回退扫描：{e}")
        if not files:
            files = self._cloud_query.list_files_recursive(staging_path, max_depth=6)
        if not files and item.get("cloud_dir") and item.get("cloud_dir") != item.get("staging_dir"):
            files = self._cloud_query.list_files_recursive(item.get("cloud_dir"), max_depth=6)
        return files

    def _finalize_magnet_package(
            self,
            item: Dict[str, Any],
            pending_key: str,
            ctx: PostprocessBatchContext,
            offline_task: Optional[Dict[str, Any]] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """读取完成后的真实文件树，只移动实际匹配的媒体文件。"""
        mediainfo, media_data = self._restore_pending_media_context(item, pending_key)
        if not mediainfo:
            self._cleanup_failed_offline_task(item, "媒体元数据不存在")
            return []
        subscribe = self._resolve_magnet_subscribe(item, ctx, mediainfo)
        if not subscribe:
            self._cleanup_failed_offline_task(item, "订阅已不存在")
            ctx.settled_failure[pending_key] = "Magnet 下载完成时订阅已不存在"
            return []

        files = self._list_magnet_files(item, offline_task)
        video_files = [
            file_item for file_item in files
            if MediaFileParser.is_video(str(file_item.get("name") or ""))
        ]
        if not video_files:
            recovered = self._try_recover_magnet_from_destination(
                item, pending_key, mediainfo, subscribe
            )
            if recovered is not None:
                return recovered
            logger.debug(f"Magnet 已完成但真实文件树尚未就绪：{item.get('file_name')}")
            return None

        def directory_snapshot(cloud_dir: str) -> Tuple[bool, Dict[str, Any]]:
            return self._cloud_directory_snapshot(cloud_dir)

        matched: List[Tuple[Optional[int], Any, int]] = []
        season = item.get("season")
        if mediainfo.type == MediaType.TV:
            target_episodes = []
            for value in item.get("target_episodes") or []:
                try:
                    episode = int(str(value or "0"))
                except ValueError:
                    continue
                if episode > 0:
                    target_episodes.append(episode)
            resource = item.get("resource") or {}
            if resource.get("source") in {"mikan", "animegarden"}:
                candidates = anime_file_candidates(
                    video_files,
                    resource.get("title") or "",
                    normalize_season(season),
                    target_episodes,
                )
                episode_files = {
                    episode: self._search_handler.select_file_candidate(files, mediainfo, subscribe)
                    for episode, files in candidates.items()
                }
            else:
                episode_files = self._match_episode_files(
                    video_files,
                    mediainfo,
                    subscribe,
                    normalize_season(season),
                    target_episodes,
                )
            matched = [
                (episode, episode_files[episode][0], episode_files[episode][1])
                for episode in target_episodes
                if episode_files.get(episode, (None, 0))[0]
            ]
        else:
            movie_file, movie_score = self._match_movie_file(
                video_files, mediainfo, subscribe
            )
            if movie_file:
                matched = [(None, movie_file, movie_score)]

        if not matched:
            reason = "Magnet 下载完成，但真实文件名未匹配当前订阅"
            logger.debug(f"{reason}：{item.get('file_name')}")
            self._cleanup_failed_offline_task(item, reason)
            ctx.settled_failure[pending_key] = reason
            return []

        history_records = []
        details = []
        success_episodes = []
        resource = item.get("resource") or {}
        share_url = str(item.get("share_url") or "")
        upgrade_baseline = item.get("upgrade_baseline") or {}
        for episode, source_file, current_score in matched:
            source_name = str(source_file.get("name") or "")
            baseline_key = str(episode) if episode else "movie"
            old_baseline = upgrade_baseline.get(baseline_key) or {}
            is_upgrade = bool(old_baseline)
            source_size = self._resource_size_bytes(source_file.get("size"))
            if is_upgrade:
                should_upgrade, reason = self._should_upgrade_candidate(
                    int(old_baseline.get("score") or 0),
                    current_score,
                    int(old_baseline.get("size") or 0),
                    source_size,
                )
                if not should_upgrade:
                    label = f"E{int(episode):02d}" if episode else mediainfo.title
                    logger.debug(f"Magnet 下载后洗版候选跳过 {label}：{reason}")
                    continue
            cloud_dir, target_name = self._platform_target(
                self._CLOUD_MEDIA_ROOT,
                subscribe,
                mediainfo,
                source_name,
                season=max(1, int(season or 1)) if episode else None,
                episode=episode,
            )
            source_suffix = Path(source_name).suffix
            if source_suffix and not target_name.endswith(source_suffix):
                target_name = f"{Path(target_name).stem}{source_suffix}"
            mode = str(item.get("upgrade_mode") or self._upgrade_mode)
            organize_enabled = ctx.organize_enabled
            if is_upgrade and mode == "coexist":
                target_name = self._coexist_target_name(
                    target_name, source_name, source_size, source_file.get("sha1") or ""
                )
            if is_upgrade and mode != "coexist":
                old_dir = str(old_baseline.get("cloud_dir") or "").strip()
                old_name = str(old_baseline.get("file_name") or "").strip()
                old_file_id = str(old_baseline.get("file_id") or "").strip()
                if not old_dir or not old_name:
                    old_dir, old_name = self._platform_target(
                        self._CLOUD_MEDIA_ROOT,
                        subscribe,
                        mediainfo,
                        old_name or source_name,
                        season=max(1, int(season or 1)) if episode else None,
                        episode=episode,
                    )
                if not old_file_id:
                    old_file = self._cloud_query.get_cached_file(old_dir, old_name)
                    old_file_id = str(getattr(old_file, "id", "") or "")
                replace_item = {
                    **item,
                    "cloud_dir": cloud_dir,
                    "file_name": target_name,
                    "upgrade_old_cloud_dir": old_dir,
                    "upgrade_old_file_name": old_name,
                    "upgrade_old_file_id": old_file_id,
                }
                source_dir = str(
                    (getattr(source_file, "native", None) or {}).get("_cloud_dir")
                    or item.get("cloud_dir") or "/"
                ).rstrip("/") or "/"
                located_sources = item.get("_located_source_dir")
                if not isinstance(located_sources, list):
                    located_sources = []
                    item["_located_source_dir"] = located_sources
                if source_dir not in located_sources:
                    located_sources.append(source_dir)
                moved = self._replace_upgrade_file(
                    replace_item,
                    f"{pending_key}:{baseline_key}",
                    source_file,
                    source_dir,
                    target_name,
                    time.time(),
                    directory_snapshot,
                )
            else:
                moved = self._cloud_mutations.move_file(
                    source_file, cloud_dir, target_name
                )
            if not moved:
                continue
            if not self._finalize_magnet_single_output(
                    cloud_dir=cloud_dir,
                    target_name=target_name,
                    target_file=moved,
                    mediainfo=mediainfo,
                    subscribe=subscribe,
                    season=season,
                    episode=episode,
                    is_upgrade=is_upgrade,
                    mode=mode,
                    replace_item=replace_item if (is_upgrade and mode != "coexist") else None,
                    directory_snapshot=directory_snapshot,
            ):
                continue

            if episode:
                success_episodes.append(int(episode))
            else:
                success_episodes.append(1)
            episode_fields = (
                {"season": int(season or 1), "episode": int(episode)}
                if episode else {}
            )
            record = self._build_transfer_history_item(
                mediainfo=mediainfo,
                subscribe=subscribe,
                status="成功",
                share_url=share_url,
                file_name=target_name,
                source_file_name=source_name,
                cloud_dir=cloud_dir,
                resource=resource,
                file_size=source_size,
                source_sha1=str(source_file.get("sha1") or ""),
                rule_score=current_score,
                upgrade=is_upgrade,
                **episode_fields,
            )
            history_records.append(record)
            detail = {
                "type": record["type"],
                "title": mediainfo.title,
                "year": mediainfo.year,
                "image": mediainfo.get_poster_image(),
                "file_name": target_name,
            }
            if episode:
                detail.update({"season": int(season or 1), "episodes": [int(episode)]})
            details.append(detail)

        details = self._persist_magnet_finalized_outputs(
            item=item,
            pending_key=pending_key,
            mediainfo=mediainfo,
            media_data=media_data,
            history_records=history_records,
            details=details,
            success_episodes=success_episodes,
        )
        if details:
            logger.info(
                f"Magnet 下载后文件匹配完成：移动 {len(history_records)} 个文件，"
                f"未匹配内容保留在隔离目录"
            )
        return details

    def _finalize_magnet_single_output(
            self,
            cloud_dir: str,
            target_name: str,
            target_file: Any,
            mediainfo: Any,
            subscribe: Any,
            season: Optional[int],
            episode: Optional[int],
            is_upgrade: bool = False,
            mode: str = "",
            replace_item: Optional[Dict[str, Any]] = None,
            directory_snapshot: Optional[Any] = None,
    ) -> bool:
        """完成 Magnet 单个匹配文件的元数据刮削、STRM 生成与媒体服务器通知。"""
        self._scrape_metadata(
            cloud_dir,
            target_name,
            mediainfo,
            season=season,
            episode=episode,
        )
        strm_path = None
        if self._strm_generate_enabled and self._strm_generator and self._local_resource_path:
            strm_path = self._generate_strm(
                cloud_dir, target_name, target_file=target_file,
                lookup_target=False, log_success=False,
            )
            if strm_path:
                if is_upgrade and mode != "coexist" and replace_item and directory_snapshot:
                    if not self._delete_upgrade_old_file(replace_item, directory_snapshot):
                        logger.debug(f"Magnet 洗版旧文件删除失败：{target_name}")
                        return False
                    self._delete_upgrade_old_strm(replace_item, replacement_path=strm_path)
                self._media_server_notifier.notify(
                    path=strm_path, mediainfo=mediainfo, file_name=target_name
                )
        elif self._local_resource_path:
            if is_upgrade and mode != "coexist" and replace_item and directory_snapshot:
                if not self._delete_upgrade_old_file(replace_item, directory_snapshot):
                    logger.debug(f"Magnet 洗版旧文件删除失败：{target_name}")
                    return False
                self._delete_upgrade_old_strm(replace_item)
            notify_path = self._resolve_resource_season_dir(
                self._local_resource_path,
                subscribe,
                mediainfo,
                max(1, int(season or 1)),
            )
            if notify_path:
                self._media_server_notifier.notify(
                    path=notify_path, mediainfo=mediainfo, file_name=target_name
                )
        return True

    def _persist_magnet_finalized_outputs(
            self,
            item: Dict[str, Any],
            pending_key: str,
            mediainfo: Any,
            media_data: Optional[Dict[str, Any]],
            history_records: List[Dict[str, Any]],
            details: List[Dict[str, Any]],
            success_episodes: List[int],
    ) -> Optional[List[Dict[str, Any]]]:
        """统一持久化 Magnet 匹配或自愈产生的历史记录、订阅状态并返回详情。"""
        if not history_records:
            return None
        persisted_records = [
            record for record in history_records
            if not record.get("skip_history")
        ]
        with self._offline_pending_lock:
            history = [
                record for record in (self._get_data("history") or [])
                if str(record.get("finalize_key") or "") != pending_key
            ]
            history.extend(persisted_records)
            self._save_data("history", history)
        self._record_platform_transfer_histories(persisted_records)
        item["success_episodes"] = (
            [] if item.get("transient_target") else success_episodes
        )
        item["notification_episodes"] = (
            success_episodes if mediainfo.type == MediaType.TV else []
        )
        if media_data is not None and not item.get("transient_target"):
            self._finish_pending_subscription(
                item, media_data, mediainfo=mediainfo
            )
        return details

    def _try_recover_magnet_from_destination(
            self,
            item: Dict[str, Any],
            pending_key: str,
            mediainfo: Any,
            subscribe: Any,
    ) -> Optional[List[Dict[str, Any]]]:
        """当离线暂存目录文件为空时，检查目标媒体库是否已存在匹配文件（自愈机制）。"""
        season = item.get("season")
        episodes = []
        if mediainfo.type == MediaType.TV:
            for value in item.get("target_episodes") or []:
                try:
                    ep = int(str(value or "0"))
                    if ep > 0:
                        episodes.append(ep)
                except ValueError:
                    continue
            if not episodes:
                return None
        else:
            episodes = [None]

        recovered_files = []
        for ep in episodes:
            cloud_dir, target_name = self._platform_target(
                self._CLOUD_MEDIA_ROOT,
                subscribe,
                mediainfo,
                item.get("file_name") or mediainfo.title,
                season=max(1, int(season or 1)) if ep else None,
                episode=ep,
            )
            if not cloud_dir or not target_name:
                return None
            dest_file = self._cloud_query.get_cached_file(cloud_dir, target_name)
            if not dest_file:
                lookup = self._cloud_directories.resolve_directory(cloud_dir)
                if lookup.checked and lookup.directory_id is not None:
                    listing = self._cloud_directories.list_directory(lookup.directory_id)
                    if listing.checked:
                        dest_file = next(
                            (f for f in listing.files if f.name == target_name or (
                                    MediaFileParser.is_video(f.name) and (
                                f"E{ep:02d}" in f.name.upper() if ep else True
                            )
                            )),
                            None
                        )
            if not dest_file:
                return None
            recovered_files.append((ep, dest_file, cloud_dir, dest_file.name or target_name))

        if not recovered_files:
            return None

        logger.debug(f"离线任务自愈成功：{item.get('file_name')}")
        history_records = []
        details = []
        success_episodes = []
        share_url = str(item.get("share_url") or "")
        resource = item.get("resource") or {}

        for ep, target_file, cloud_dir, final_name in recovered_files:
            self._finalize_magnet_single_output(
                cloud_dir=cloud_dir,
                target_name=final_name,
                target_file=target_file,
                mediainfo=mediainfo,
                subscribe=subscribe,
                season=season,
                episode=ep,
            )
            if ep:
                success_episodes.append(int(ep))
            else:
                success_episodes.append(1)

            episode_fields = (
                {"season": int(season or 1), "episode": int(ep)}
                if ep else {}
            )
            record = self._build_transfer_history_item(
                mediainfo=mediainfo,
                subscribe=subscribe,
                status="成功",
                share_url=share_url,
                file_name=final_name,
                source_file_name=str(
                    item.get("staging_name") or item.get("source_name") or item.get("source_file_name") or final_name),
                cloud_dir=cloud_dir,
                resource=resource,
                file_size=getattr(target_file, "size", 0) or 0,
                source_sha1=str(getattr(target_file, "sha1", "") or ""),
                rule_score=0,
                upgrade=False,
                **episode_fields,
            )
            history_records.append(record)
            detail = {
                "type": record["type"],
                "title": mediainfo.title,
                "year": mediainfo.year,
                "image": mediainfo.get_poster_image(),
                "file_name": final_name,
            }
            if ep:
                detail.update({"season": int(season or 1), "episodes": [int(ep)]})
            details.append(detail)

        return self._persist_magnet_finalized_outputs(
            item=item,
            pending_key=pending_key,
            mediainfo=mediainfo,
            media_data=None,
            history_records=history_records,
            details=details,
            success_episodes=success_episodes,
        )

    def _schedule_finalize_retry(self, item: Dict[str, Any], now: float) -> None:
        check_index = min(
            int(item.get("check_index") or 0) + 1,
            len(self._OFFLINE_CHECK_DELAYS) - 1,
        )
        item["check_index"] = check_index
        retry_at = now + self._OFFLINE_CHECK_DELAYS[check_index]
        if self._task_type(item) in self._OFFLINE_TASK_TYPES:
            created_at = float(item.get("created_at") or now)
            retry_at = min(retry_at, created_at + self._offline_timeout_seconds)
        item["next_check_at"] = retry_at
        retry_minutes = max(1, int(max(0, retry_at - now) + 59) // 60)
        logger.debug(
            f"文件后处理尚未完成：{item.get('file_name')}，"
            f"{retry_minutes} 分钟后复查"
        )

    def _finalize_failure(
            self, item: Dict[str, Any], pending_key: str, max_failures: Optional[int] = None
    ) -> bool:
        """记录一次后处理失败；达到上限返回 True 并标记为死任务（finalize_dead）。"""
        threshold = (
            max_failures if max_failures is not None else self._FINALIZE_MAX_FAILURES
        )
        fail_count = int(item.get("fail_count") or 0) + 1
        item["fail_count"] = fail_count
        if fail_count < threshold:
            return False
        item["finalize_dead"] = True
        item["next_check_at"] = 0.0
        logger.error(
            self._FINALIZE_DEAD_LOG.format(
                fail_count, str(item.get("file_name") or pending_key)
            )
        )
        return True

    def _notify_finalize_dead(
            self, item: Dict[str, Any], pending_key: str
    ) -> None:
        post_msg = getattr(self, "_post_message", None) or getattr(self, "post_message", None)
        if not post_msg or not getattr(self, "_notify", False):
            return
        try:
            post_msg(
                mtype=self._notification_type,
                title=self._FINALIZE_DEAD_TITLE,
                text=self._FINALIZE_DEAD_TEXT.format(
                    str(item.get("file_name") or pending_key),
                    int(item.get("fail_count") or 0),
                ),
            )
        except Exception as error:
            logger.debug(f"后处理失败通知发送异常：{error}")
