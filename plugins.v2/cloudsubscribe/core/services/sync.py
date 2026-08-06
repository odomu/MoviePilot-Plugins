"""订阅同步执行与并发编排。"""

import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Event as ThreadEvent, Thread
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.types import MediaType

from .runtime import sync_lock
from ...core import CloudDriveCapability, OwnerDelegator


class SyncExecutionService(OwnerDelegator):
    """执行订阅同步并维护全局执行边界。"""

    _SUBSCRIBE_SEARCH_BATCH_SECONDS = 1.0
    _SUBSCRIBE_SEARCH_DEBOUNCE_SECONDS = 60.0

    @staticmethod
    def _history_group_key(
            media_type: str,
            title: str,
            season: Any = None,
            tmdb_id: Any = None,
    ) -> Tuple[str, str, int]:
        normalized_type = "电影" if str(media_type or "") == MediaType.MOVIE.value else "电视剧"
        media_identity = (
            f"tmdb:{tmdb_id}"
            if str(tmdb_id or "").strip()
            else f"title:{str(title or '').strip()}"
        )
        return (
            normalized_type,
            media_identity,
            int(season or 1) if normalized_type == "电视剧" else 0,
        )

    @staticmethod
    def _subscribe_search_media_key(subscribe_id: Optional[int]) -> tuple:
        """使用媒体身份合并重复订阅，避免同一媒体因不同订阅 ID 重复搜索。"""
        if subscribe_id is None:
            return ("ALL",)
        try:
            subscribe = SubscribeOper().get(int(subscribe_id))
        except Exception as error:
            logger.warning(f"读取订阅媒体身份失败，回退按 ID 合并：{subscribe_id} - {error}")
            subscribe = None
        if not subscribe:
            return ("ID", int(subscribe_id))
        media_type = str(getattr(subscribe, "type", "") or "")
        media_id = str(
            getattr(subscribe, "tmdbid", None)
            or getattr(subscribe, "tmdb_id", None)
            or getattr(subscribe, "doubanid", None)
            or getattr(subscribe, "name", "")
        ).strip()
        season = (
            int(getattr(subscribe, "season", 1) or 1)
            if media_type == MediaType.TV.value else 0
        )
        return media_type, media_id, season

    def _prepare_searchable_subscribes(
            self, subscribes: List[Any]
    ) -> Tuple[List[Any], int]:
        """统一准备媒体身份、目标集和播出日历，供任务线程直接复用。"""
        prepared = []
        unresolved_count = 0
        for subscribe in subscribes:
            try:
                has_tmdb_id = int(
                    getattr(subscribe, "tmdbid", 0) or 0
                ) > 0
            except (TypeError, ValueError):
                has_tmdb_id = False
            repaired = has_tmdb_id or bool(
                self._sync_handler
                and self._sync_handler.repair_subscribe_tmdb_id(subscribe)
            )
            if not repaired:
                unresolved_count += 1
                logger.warning(
                    "订阅缺少 TMDB ID 且自动修复失败，任务创建前跳过："
                    f"#{getattr(subscribe, 'id', '')} "
                    f"{getattr(subscribe, 'name', '')} "
                    f"({getattr(subscribe, 'year', '')})"
                )
                continue

            is_tv = getattr(subscribe, "type", "") == MediaType.TV.value
            start_episode = (
                int(getattr(subscribe, "start_episode", 1) or 1)
                if is_tv else 0
            )
            total_episode = (
                int(getattr(subscribe, "total_episode", 0) or 0)
                if is_tv else 0
            )
            expected_episodes = (
                set(range(start_episode, total_episode + 1))
                if is_tv and total_episode >= start_episode else set()
            )
            calendar_entry = (
                self._sync_handler.get_tv_subscribe_calendar(subscribe)
                if self._sync_handler and expected_episodes else None
            )
            unreleased_episodes = {
                int(episode)
                for episode in (
                        (calendar_entry or {}).get("unreleased_episodes") or []
                )
            }
            preparation = {
                "tmdb_id": int(getattr(subscribe, "tmdbid", 0) or 0),
                "calendar": calendar_entry,
                "expected_episodes": sorted(expected_episodes),
                "aired_target_episodes": sorted(
                    expected_episodes - unreleased_episodes
                ),
                "unreleased_episodes": sorted(unreleased_episodes),
                "all_targets_future": bool(
                    calendar_entry
                    and calendar_entry.get("all_targets_future")
                ),
                "defer_until": str(
                    (calendar_entry or {}).get("defer_until") or ""
                ),
            }
            setattr(subscribe, "_cloudsubscribe_preparation", preparation)
            prepared.append(subscribe)
        return prepared, unresolved_count

    def _deduplicate_subscribes(
            self, subscribes: List[Any]
    ) -> Tuple[List[Any], int]:
        """按媒体身份保留最早订阅，并输出可定位的重复卡片明细。"""
        grouped: Dict[Tuple[str, str, int], List[Any]] = {}
        for subscribe in subscribes:
            grouped.setdefault(self._sync_media_key(subscribe), []).append(subscribe)

        canonical = []
        duplicate_count = 0
        duplicate_details = []
        for group in grouped.values():
            ordered = sorted(
                group,
                key=lambda item: int(getattr(item, "id", 0) or 0),
            )
            canonical.append(ordered[0])
            duplicates = ordered[1:]
            duplicate_count += len(duplicates)
            if duplicates:
                duplicate_details.append(
                    f"{getattr(ordered[0], 'name', '')}：保留 "
                    f"#{getattr(ordered[0], 'id', '')}，跳过 "
                    + ", ".join(
                        f"#{getattr(item, 'id', '')}" for item in duplicates
                    )
                )
        if duplicate_count:
            details = "；".join(duplicate_details[:10])
            if len(duplicate_details) > 10:
                details += f"；另有 {len(duplicate_details) - 10} 组"
            logger.warning(
                f"发现 {duplicate_count} 个同媒体重复订阅，本轮仅处理最早创建的订阅卡片："
                f"{details}"
            )
        return canonical, duplicate_count

    def queue_subscribe_search(
            self,
            subscribe_id: Optional[int],
            subscribe_state: Optional[str] = None,
            progress_callback: Optional[Callable[..., None]] = None,
    ) -> bool:
        """接收平台订阅搜索，并保留平台传入的状态范围。"""
        normalized_state = self._normalize_subscribe_state(subscribe_state)
        media_key = self._subscribe_search_media_key(subscribe_id)
        debounce_key = (media_key, normalized_state)
        now = time.monotonic()
        queue_lock = self._subscribe_search_queue_lock
        with queue_lock:
            if self._subscribe_search_queue_shutdown.is_set():
                return False
            recent = self._subscribe_search_recent
            expired_before = now - self._SUBSCRIBE_SEARCH_DEBOUNCE_SECONDS
            self._subscribe_search_recent = {
                key: completed_at
                for key, completed_at in recent.items()
                if completed_at > expired_before
            }
            active_media_keys = {
                self._subscribe_search_media_key(queued_id)
                for queued_id in self._subscribe_search_active
            }
            pending_media_id = next((
                queued_id
                for queued_id in self._subscribe_search_pending
                if self._subscribe_search_media_key(queued_id) == media_key
            ), None)
            if debounce_key in self._subscribe_search_recent:
                queue_state = "防抖合并"
                queued = False
            elif None in self._subscribe_search_active or media_key in active_media_keys:
                queue_state = "同媒体运行中合并"
                queued = False
            elif None in self._subscribe_search_pending and subscribe_id is not None:
                queue_state = "全量队列合并"
                queued = False
            elif pending_media_id is not None:
                previous_state = self._subscribe_search_pending.get(pending_media_id)
                self._subscribe_search_pending[pending_media_id] = (
                    self._merge_subscribe_states(previous_state, normalized_state)
                )
                queue_state = "同媒体窗口合并"
                queued = False
            else:
                previous_state = self._subscribe_search_pending.get(subscribe_id)
                if subscribe_id is None and self._subscribe_search_pending:
                    self._subscribe_search_pending.clear()
                self._subscribe_search_pending[subscribe_id] = (
                    self._merge_subscribe_states(previous_state, normalized_state)
                )
                queue_state = "已排队"
                queued = True
            start_coordinator = queued and not self._subscribe_search_coordinator_running
            if start_coordinator:
                self._subscribe_search_coordinator_running = True
            pending_count = len(self._subscribe_search_pending)
        logger.debug(
            f"订阅卡片搜索{queue_state}：subscribe_id={subscribe_id or 'ALL'}，"
            f"媒体键={media_key}，待处理队列 {pending_count}"
        )

        if progress_callback:
            progress_callback(
                value=0,
                text=(
                    "订阅搜索已加入网盘订阅助手队列"
                    if queued else "订阅搜索已合并到网盘订阅助手任务"
                ),
            )
        if start_coordinator:
            Thread(
                target=self._drain_subscribe_search_queue,
                daemon=True,
                name="cloudsubscribe-search-queue",
            ).start()
        return True

    @staticmethod
    def _normalize_subscribe_state(state: Optional[str]) -> Optional[str]:
        """规范平台订阅状态，保留 N/R/P/S 的顺序并去重。"""
        if state is None:
            return None
        values = []
        for value in str(state).split(","):
            value = value.strip().upper()
            if value in {"N", "R", "P", "S"} and value not in values:
                values.append(value)
        return ",".join(values) or None

    @classmethod
    def _merge_subscribe_states(
            cls,
            first: Optional[str],
            second: Optional[str],
    ) -> Optional[str]:
        if first is None or second is None:
            return None
        return cls._normalize_subscribe_state(f"{first},{second}")

    def _drain_subscribe_search_queue(self) -> None:
        """按到达批次消费卡片搜索队列；同一批订阅交给现有线程池并发。"""
        try:
            while True:
                time.sleep(self._SUBSCRIBE_SEARCH_BATCH_SECONDS)
                with self._subscribe_search_queue_lock:
                    if self._subscribe_search_queue_shutdown.is_set():
                        return
                    if not self._subscribe_search_pending:
                        return
                    batch = self._subscribe_search_pending
                    self._subscribe_search_pending = {}
                    self._subscribe_search_active = batch
                    queue_revision = self._subscribe_search_queue_revision

                subscribe_ids = None if None in batch else list(batch)
                subscribe_states = batch.get(None) if subscribe_ids is None else None
                logger.debug(
                    f"开始消费订阅卡片搜索队列："
                    f"{'全部订阅' if subscribe_ids is None else len(subscribe_ids)}，"
                    f"订阅并发上限 {self._subscription_concurrency}"
                )
                self.sync_subscribes(
                    subscribe_ids=subscribe_ids,
                    subscribe_states=subscribe_states,
                    wait_for_slot=True,
                    queue_revision=queue_revision,
                )
                with self._subscribe_search_queue_lock:
                    completed_at = time.monotonic()
                    for queued_id, queued_state in batch.items():
                        recent_key = self._subscribe_search_media_key(queued_id)
                        self._subscribe_search_recent[(recent_key, queued_state)] = completed_at
                    self._subscribe_search_active = {}
        finally:
            with self._subscribe_search_queue_lock:
                self._subscribe_search_active = {}
                self._subscribe_search_coordinator_running = False
                restart = bool(
                    self._subscribe_search_pending
                    and not self._subscribe_search_queue_shutdown.is_set()
                )
                if restart:
                    self._subscribe_search_coordinator_running = True
            if restart:
                Thread(
                    target=self._drain_subscribe_search_queue,
                    daemon=True,
                    name="cloudsubscribe-search-queue",
                ).start()

    def cancel_pending_subscribe_searches(self, shutdown: bool = False) -> None:
        with self._subscribe_search_queue_lock:
            self._subscribe_search_queue_revision += 1
            if shutdown:
                self._subscribe_search_queue_shutdown.set()
            self._subscribe_search_pending.clear()

    def _do_sync(
            self,
            subscribe_id: Optional[int] = None,
            subscribe_ids: Optional[List[int]] = None,
            subscribe_states: Optional[str] = None,
            manual_resources: Optional[List[Dict[str, Any]]] = None,
            manual_target: Optional[Dict[str, Any]] = None,
            upgrade_request: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if self._stop_requested():
            logger.info("同步任务已收到停止请求，取消执行")
            return False

        # 至少启用一个搜索源
        if (
                not manual_resources
                and (
                    not self._search_handler
                    or not self._search_handler.get_enabled_sources()
                )
        ):
            logger.error("没有已启用且配置完整的搜索源，无法执行")
            if self._notify:
                self.post_message(
                    mtype=self._notification_type,
                    title="【网盘订阅助手】配置错误",
                    text="请至少启用并正确配置一个搜索源。"
                )
            return False

        if not self._cloud_drive:
            logger.error("网盘提供方未初始化，请检查网盘配置")
            return False
        required = {
            CloudDriveCapability.AUTHENTICATION,
            CloudDriveCapability.SHARE_TRANSFER,
            CloudDriveCapability.DIRECTORY_READ,
            CloudDriveCapability.FILE_QUERY,
            CloudDriveCapability.FILE_MUTATION,
        }
        missing = [
            capability.value for capability in required
            if not self._cloud_drive.supports(capability)
        ]
        if missing:
            logger.error(
                f"{self._cloud_drive.name}缺少订阅同步所需能力：{', '.join(missing)}"
            )
            return False
        cloud_auth = self._cloud_drive.require(
            CloudDriveCapability.AUTHENTICATION
        )

        task_label = (
            "手动洗版"
            if upgrade_request
            else
            "手动添加"
            if manual_resources
            else f"{self._cloud_drive.name}订阅同步"
        )
        self._set_sync_status("running", "正在读取订阅列表", 5)

        try:
            if self._search_handler:
                if not manual_resources:
                    self._search_handler.reset_task_spent_points()
                self._search_handler.reset_search_metrics()
        except Exception:
            pass
        if self._sync_handler:
            self._sync_handler.reset_sync_metrics()

        # 获取订阅或构造无需订阅卡片的临时媒体目标。
        if upgrade_request:
            source = str(upgrade_request.get("source") or "history").strip().lower()
            if source == "resolved":
                subscribes = list(upgrade_request.get("targets") or [])
            elif source == "media_server":
                subscribes = self._sync_handler.resolve_media_server_upgrade_targets(
                    upgrade_request.get("items") or []
                )
            else:
                subscribes = self._sync_handler.resolve_history_upgrade_targets(
                    upgrade_request.get("records") or []
                )
        elif manual_target:
            subscribes = [self._sync_handler.build_transient_media_target(
                manual_target,
                target_id=-1,
                manual_upgrade=False,
            )]
        else:
            with SessionFactory() as db:
                subscribe_oper = SubscribeOper(db=db)
                if subscribe_ids is not None:
                    subscribes = []
                    seen_ids = set()
                    for queued_id in subscribe_ids:
                        normalized_id = int(queued_id or 0)
                        if normalized_id <= 0 or normalized_id in seen_ids:
                            continue
                        seen_ids.add(normalized_id)
                        subscribe = subscribe_oper.get(normalized_id)
                        if subscribe:
                            subscribes.append(subscribe)
                elif subscribe_id:
                    subscribe = subscribe_oper.get(subscribe_id)
                    subscribes = [subscribe] if subscribe else []
                else:
                    subscribes = subscribe_oper.list(subscribe_states or "N,R")

        if not subscribes:
            logger.debug("当前没有可处理的订阅")
            if self._notify:
                self.post_message(
                    mtype=self._notification_type,
                    title="【网盘订阅助手】执行完成",
                    text="当前无订阅数据。"
                )
            return True

        tv_subscribes = [s for s in subscribes if s.type == MediaType.TV.value]
        movie_subscribes = [s for s in subscribes if s.type == MediaType.MOVIE.value]

        if not tv_subscribes and not movie_subscribes:
            logger.debug("当前没有电影或电视剧订阅")
            return True

        exclude_ids = set(self._exclude_subscribes or [])
        all_subscribes = movie_subscribes + tv_subscribes
        excluded_count = 0
        deferred_count = 0
        postprocessing_count = 0
        unresolved_tmdb_count = 0
        active_subscribes = []
        transient_request = bool(manual_target or upgrade_request)
        if manual_resources or transient_request:
            active_subscribes, _ = self._deduplicate_subscribes(all_subscribes)
            if transient_request:
                active_subscribes, unresolved_tmdb_count = (
                    self._prepare_searchable_subscribes(active_subscribes)
                )
        else:
            pending_subscribe_ids = {
                int(item.get("subscribe_id") or 0)
                for item in (
                    self._sync_handler.get_pending_finalize_tasks()
                    if self._sync_handler else []
                )
                if int(item.get("subscribe_id") or 0) > 0
            }
            candidates = []
            for subscribe in all_subscribes:
                subscribe_id_value = int(getattr(subscribe, "id", 0) or 0)
                if self._is_subscribe_excluded(subscribe_id_value):
                    excluded_count += 1
                    continue
                if subscribe_id_value in pending_subscribe_ids:
                    postprocessing_count += 1
                    continue
                defer_entry = (
                    self._sync_handler.get_subscribe_defer(subscribe)
                    if self._sync_handler else None
                )
                if defer_entry:
                    deferred_count += 1
                    logger.debug(
                        f"订阅延期缓存命中，跳过本轮收集："
                        f"{getattr(subscribe, 'name', '')}，"
                        f"下次检查日期 {defer_entry.get('defer_until')}"
                    )
                    continue
                candidates.append(subscribe)

            candidates, _ = self._deduplicate_subscribes(candidates)
            prepared, unresolved_tmdb_count = self._prepare_searchable_subscribes(
                candidates
            )
            if prepared:
                logger.debug(
                    f"订阅批量预处理完成：{len(prepared)} 个唯一订阅"
                )
            for subscribe in prepared:
                preparation = getattr(
                    subscribe, "_cloudsubscribe_preparation", {}
                ) or {}
                if preparation.get("all_targets_future"):
                    deferred_count += 1
                    logger.debug(
                        f"订阅日历过滤，跳过本轮收集："
                        f"{getattr(subscribe, 'name', '')}，"
                        f"最早播出日期 {preparation.get('defer_until')}"
                    )
                    continue
                active_subscribes.append(subscribe)
        skipped_count = len(all_subscribes) - len(active_subscribes)
        total_subscribes = len(active_subscribes)
        if not active_subscribes:
            self._register_sync_tasks([])
            logger.debug(
                f"订阅收集完成，无需搜索：排除 {excluded_count} 个，"
                f"延期 {deferred_count} 个，后处理 {postprocessing_count} 个，"
                f"缺少 TMDB ID {unresolved_tmdb_count} 个"
            )
            return True

        if not cloud_auth.check_login():
            logger.error(f"{self._cloud_drive.name}登录状态校验失败")
            if self._notify:
                self.post_message(
                    mtype=self._notification_type,
                    title="【网盘订阅助手】登录失败",
                    text=f"{self._cloud_drive.name}登录凭证可能已过期，请更新后重试。"
                )
            return False

        logger.info(f"🚀 开始执行 {task_label}")

        history: List[dict] = self.get_data('history') or []
        history_by_media: Dict[Tuple[str, str, int], List[dict]] = {}
        for record in history:
            history_by_media.setdefault(
                self._history_group_key(
                    record.get("type"),
                    record.get("title"),
                    record.get("season"),
                    record.get("tmdb_id"),
                ),
                [],
            ).append(record)
        transfer_details: List[Dict[str, Any]] = []
        transferred_count = 0
        active_tv_count = sum(
            subscribe.type == MediaType.TV.value for subscribe in active_subscribes
        )
        active_movie_count = sum(
            subscribe.type == MediaType.MOVIE.value for subscribe in active_subscribes
        )
        scope_label = (
            f"手动洗版 {total_subscribes} 项"
            if upgrade_request
            else
            f"手动添加 {len(manual_resources)} 条"
            if manual_resources
            else "指定订阅"
            if subscribe_id or subscribe_ids is not None
            else "全部订阅"
        )
        self._set_sync_status(
            "running",
            f"已加载 {total_subscribes} 个订阅，准备搜索资源",
            8,
            {
                "current": 0,
                "total": total_subscribes,
                "transferred": 0,
                "phase": "准备搜索资源",
            },
        )
        if self._notify:
            skipped_text = f"，本轮跳过 {skipped_count} 个" if skipped_count else ""
            self.post_message(
                mtype=self._notification_type,
                title="【网盘订阅助手】开始同步",
                text=(
                    f"{self._cloud_drive.name} · {scope_label}\n"
                    f"待处理 {total_subscribes} 个订阅（电视剧 {active_tv_count}，"
                    f"电影 {active_movie_count}）{skipped_text}。\n"
                    "正在按搜索源优先级检查缺失内容并转存匹配资源。"
                ),
            )

        if self._sync_handler:
            self._sync_handler.reset_transfer_budget()
        self._register_sync_tasks(active_subscribes)
        grouped_subscribes = {
            self._sync_media_key(subscribe): [subscribe]
            for subscribe in active_subscribes
        }

        completed_subscribes = 0
        if grouped_subscribes:
            worker_count = min(
                self._subscription_concurrency,
                len(grouped_subscribes),
                self._cloud_drive.policy.max_concurrency,
            )
            logger.debug(
                f"订阅并发调度：{total_subscribes} 个订阅，"
                f"{len(grouped_subscribes)} 个媒体队列，并发数 {worker_count}"
            )
            executor = ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="cloudsubscribe-subscribe",
            )
            stop_waiting = False
            try:
                future_groups = {
                    executor.submit(
                        self._run_subscription_group,
                        group,
                        history_by_media.get(
                            self._history_group_key(
                                getattr(group[0], "type", ""),
                                getattr(group[0], "name", ""),
                                getattr(group[0], "season", None),
                                getattr(group[0], "tmdbid", None),
                            ),
                            [],
                        ),
                        exclude_ids,
                        manual_resources,
                    ): group
                    for group in grouped_subscribes.values()
                }

                def collect_future_result(future) -> None:
                    nonlocal completed_subscribes, transferred_count
                    group = future_groups[future]
                    try:
                        result = future.result()
                    except Exception as error:
                        logger.error(f"订阅并发任务异常：{error}")
                        result = {
                            "history": [],
                            "transfer_details": [],
                            "transferred": 0,
                        }
                        for subscribe in group:
                            self._update_sync_task(
                                self._sync_task_id(subscribe),
                                status="failed",
                                phase="处理失败",
                                progress=100,
                                message=str(error),
                            )
                    new_history_records = result["history"]
                    history.extend(new_history_records)
                    if new_history_records:
                        self._sync_handler._timed_sync_call(
                            "history_persist",
                            self._sync_handler.append_history_records,
                            new_history_records,
                        )
                    transfer_details.extend(result["transfer_details"])
                    transferred_count += int(result["transferred"] or 0)
                    completed_subscribes += len(group)
                    self._set_sync_status(
                        "running",
                        f"正在并行处理订阅（{completed_subscribes}/{total_subscribes}）",
                        10 + int(completed_subscribes / max(total_subscribes, 1) * 85),
                        {
                            "current": completed_subscribes,
                            "total": total_subscribes,
                            "transferred": transferred_count,
                            "phase": "并行搜索与转存",
                            "concurrency": worker_count,
                        },
                    )

                pending_futures = set(future_groups)
                while pending_futures:
                    completed_futures, pending_futures = wait(
                        pending_futures,
                        timeout=0.5,
                        return_when=FIRST_COMPLETED,
                    )
                    if not completed_futures:
                        if self._stop_requested():
                            stop_waiting = True
                            break
                        continue

                    for future in completed_futures:
                        collect_future_result(future)

                    if self._stop_requested():
                        stop_waiting = True
                        break

                if stop_waiting:
                    if pending_futures:
                        completed_futures, pending_futures = wait(
                            pending_futures, timeout=0
                        )
                    else:
                        completed_futures = set()
                    for future in completed_futures:
                        collect_future_result(future)
                    for future in pending_futures:
                        group = future_groups[future]
                        cancelled = future.cancel()
                        for subscribe in group:
                            self._update_sync_task(
                                self._sync_task_id(subscribe),
                                status="stopped",
                                phase="已取消" if cancelled else "已停止等待当前调用",
                                progress=100,
                            )
                    logger.debug(
                        f"停止等待订阅工作线程：待返回 {len(pending_futures)} 个，"
                        "已取消尚未开始的任务"
                    )
            finally:
                executor.shutdown(
                    wait=not stop_waiting,
                    cancel_futures=stop_waiting,
                )

        if skipped_count:
            mode_label = "指定模式" if self._subscribe_filter_mode == "include" else "排除模式"
            logger.debug(f"订阅过滤（{mode_label}）：跳过 {skipped_count} 个订阅")

        if self._stop_requested():
            logger.info(f"网盘订阅同步已停止，停止前共转存 {transferred_count} 个文件")
            if self._notify:
                self.post_message(
                    mtype=self._notification_type,
                    title="【网盘订阅助手】任务已停止",
                    text=f"已按请求停止处理，停止前共转存 {transferred_count} 个文件。"
                )
            return False

        self._set_sync_status(
            "running",
            "正在完成本次订阅任务",
            98,
            {
                "current": total_subscribes,
                "total": total_subscribes,
                "transferred": transferred_count,
                "phase": "保存结果与发送通知",
            },
        )
        logger.info(f"网盘订阅同步完成，共转存 {transferred_count} 个文件")
        pending_finalize_count = 0
        if self._sync_handler:
            pending_finalize_count = len(
                self._sync_handler.get_pending_finalize_tasks()
            )
            self._sync_context["pending_finalize"] = pending_finalize_count
            if pending_finalize_count:
                logger.info(
                    f"本次仍有 {pending_finalize_count} 个离线文件等待真实下载完成，"
                    "暂不发送完成确认"
                )
        if self._sync_handler:
            sync_metrics = self._sync_handler.get_sync_metrics()
            if sync_metrics:
                summary = [
                    f"{name} {metric.get('calls', 0)} 次/{metric.get('elapsed_ms', 0)}ms"
                    for name, metric in sorted(sync_metrics.items())
                ]
                logger.debug(f"同步阶段耗时汇总：{'；'.join(summary)}")
        if self._search_handler:
            metrics = self._search_handler.get_search_metrics()
            if metrics:
                summary = [
                    (
                        f"{source.upper()} 外部 {counters.get('external_calls', 0)} 次/"
                        f"{counters.get('external_elapsed_ms', 0)}ms，"
                        f"正缓存 {counters.get('positive_cache_hits', 0)} 次，"
                        f"负缓存 {counters.get('negative_cache_hits', 0)} 次"
                    )
                    for source, counters in sorted(metrics.items())
                ]
                logger.debug(f"搜索性能汇总：{'；'.join(summary)}")

        completed_count = sum(
            len(item.get("episodes") or [])
            if item.get("type") == "电视剧"
            else 1
            for item in transfer_details
        )
        if completed_count > 0 and self._webhook_handler:
            self._webhook_handler.send_transfer_complete(
                transfer_details=transfer_details,
                total_count=completed_count,
            )

        if self._notify:
            if completed_count > 0:
                self._sync_handler.send_transfer_notification(
                    transfer_details, completed_count
                )
            elif transferred_count == 0:
                self.post_message(
                    mtype=self._notification_type,
                    title="【网盘订阅助手】执行完成",
                    text="本次同步未发现需要转存的新资源。"
                )

        return True

    def _apply_global_config_once(self):
        """安装确认后首次执行时，应用一次系统级洗版配置。
        放在 sync_subscribes() 开头调用，确保插件加载成功后再修改订阅状态。
        """
        if self._global_config_applied:
            return
        try:
            # 批量评分（已选独立洗版订阅且ids有变化时自动触发）
            if self._upgrade_subscribe_ids:
                current_hash = str(sorted(str(i) for i in self._upgrade_subscribe_ids))
                if current_hash != self._last_scored_ids_hash:
                    logger.info("检测到独立洗版订阅列表有变化，自动触发整理记录评分")
                    self._batch_re_score()
                    self._last_scored_ids_hash = current_hash
            self._global_config_applied = True
            logger.info("插件全局配置已应用：洗版")
        except Exception as e:
            logger.error(f"插件全局配置应用失败（下次首次执行重试）: {e}")

    def _release_sync_resources(self, notification_batch_started: bool) -> None:
        if self._search_handler:
            try:
                self._search_handler.close()
            except Exception as error:
                logger.warning(f"同步结束关闭 HDHive 浏览器失败：{error}")
        try:
            # 配置重载会关闭旧 SyncHandler；必须避开正在使用它的后处理线程。
            with self._offline_monitor_lock:
                if notification_batch_started and self._sync_handler:
                    try:
                        self._sync_handler.finish_notification_batch()
                    except Exception as error:
                        logger.warning(f"同步结束提交媒体库刷新失败：{error}")
                self._apply_pending_config()
        finally:
            sync_lock.release()

    def sync_subscribes(
            self,
            subscribe_id: Optional[int] = None,
            subscribe_ids: Optional[List[int]] = None,
            subscribe_states: Optional[str] = None,
            progress_callback: Optional[Callable[..., None]] = None,
            manual_resources: Optional[List[Dict[str, Any]]] = None,
            manual_target: Optional[Dict[str, Any]] = None,
            upgrade_request: Optional[Dict[str, Any]] = None,
            wait_for_slot: bool = False,
            queue_revision: Optional[int] = None,
            result: Optional[Dict[str, Any]] = None,
            lock_acquired: bool = False,
    ) -> bool:
        is_full_sync = (
                subscribe_id is None
                and subscribe_ids is None
                and subscribe_states is None
                and not manual_resources
                and not manual_target
                and not upgrade_request
        )
        if lock_acquired:
            pass
        elif wait_for_slot:
            while not sync_lock.acquire(timeout=0.5):
                if (
                        self._subscribe_search_queue_shutdown.is_set()
                        or queue_revision != self._subscribe_search_queue_revision
                ):
                    if result is not None:
                        result.update(self._sync_execution_result(
                            False, "订阅搜索排队任务已取消"
                        ))
                    return False
            if (
                    self._subscribe_search_queue_shutdown.is_set()
                    or queue_revision != self._subscribe_search_queue_revision
            ):
                sync_lock.release()
                if result is not None:
                    result.update(self._sync_execution_result(
                        False, "订阅搜索排队任务已取消"
                    ))
                return False
        elif not sync_lock.acquire(blocking=False):
            logger.debug("已有订阅追更任务正在运行，跳过重复请求")
            if result is not None:
                result.update(self._sync_execution_result(
                    False, "已有订阅任务正在运行"
                ))
            return False
        notification_batch_started = False
        run_context: Dict[str, Any] = {}
        task_counts: Dict[str, int] = {}
        stop_requested = False
        try:
            with self._offline_monitor_lock:
                self._apply_pending_config()
            # 首次成功运行时才应用系统级配置（避免安装失败却污染MP配置）
            if is_full_sync:
                self._apply_global_config_once()
            if self._stop_event is None:
                self._stop_event = ThreadEvent()
            self._stop_event.clear()
            self._sync_running = True
            self._sync_run_started_at = time.time()
            self._set_sync_status("running", "正在准备订阅任务", 0, {})
            if self._sync_handler:
                notification_batch_started = self._sync_handler.begin_notification_batch()
            success = False
            try:
                if progress_callback:
                    progress_callback(value=0, text="网盘订阅助手开始处理订阅搜索")
                success = self._do_sync(
                    subscribe_id=subscribe_id,
                    subscribe_ids=subscribe_ids,
                    subscribe_states=subscribe_states,
                    manual_resources=manual_resources,
                    manual_target=manual_target,
                    upgrade_request=upgrade_request,
                )
            except Exception as e:
                logger.error(f"同步任务异常：{e}")
                success = False
            finally:
                stop_requested = self._stop_requested()
                run_context = dict(self._sync_context or {})
                with self._sync_tasks_lock:
                    current_tasks = [
                        task for task in self._sync_tasks.values()
                        if float(task.get("queued_at") or 0)
                           >= self._sync_run_started_at
                    ]
                for task in current_tasks:
                    status = str(task.get("status") or "unknown")
                    task_counts[status] = task_counts.get(status, 0) + 1
                self._sync_last_elapsed_ms = int(
                    max(0.0, time.time() - self._sync_run_started_at) * 1000
                )
                self._sync_last_finished_at = time.time()
                self._sync_running = False
                self._set_sync_status(
                    "idle",
                    "订阅任务已停止" if stop_requested else "当前没有订阅处理任务",
                    self._sync_progress if stop_requested else 100,
                    {},
                )
                if progress_callback:
                    progress_callback(
                        value=100,
                        text="订阅搜索已停止" if stop_requested else "订阅搜索完成"
                    )
                if self._sync_handler and is_full_sync and not stop_requested:
                    if self._enable_cloud_upgrade:
                        now = time.time()
                        if now - getattr(self, '_last_cloud_cleanup', 0) > 86400:
                            self._last_cloud_cleanup = now
                            self._sync_handler.auto_upgrade_scan()
                        self_heal_interval = max(0, self._self_heal_interval) * 60
                        if (
                                self_heal_interval
                                and now - getattr(self, '_last_self_heal_cleanup', 0)
                                >= self_heal_interval
                        ):
                            self._last_self_heal_cleanup = now
                            self._sync_handler._self_heal_cleanup()
            if result is not None:
                transferred = int(run_context.get("transferred") or 0)
                if stop_requested:
                    message = "订阅搜索已停止"
                elif task_counts.get("failed"):
                    success = False
                    message = (
                        f"订阅搜索完成，但有 {task_counts['failed']} 个订阅处理失败"
                    )
                elif not success:
                    message = "订阅搜索执行失败"
                elif transferred and run_context.get("pending_finalize"):
                    message = (
                        f"订阅搜索已提交 {transferred} 个文件，"
                        f"其中 {run_context['pending_finalize']} 个仍在下载，"
                        "完成后将再通知"
                    )
                elif transferred:
                    message = f"订阅搜索完成，共转存 {transferred} 个文件"
                else:
                    message = "订阅搜索完成，未发现需要转存的新资源"
                result.update(self._sync_execution_result(
                    success,
                    message,
                    context=run_context,
                    elapsed_ms=self._sync_last_elapsed_ms,
                    stopped=stop_requested,
                    task_counts=task_counts,
                ))
            return success
        finally:
            self._release_sync_resources(notification_batch_started)

    @staticmethod
    def _sync_execution_result(
            success: bool,
            message: str,
            context: Optional[Dict[str, Any]] = None,
            elapsed_ms: int = 0,
            stopped: bool = False,
            task_counts: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        context = context or {}
        return {
            "success": bool(success),
            "message": message,
            "data": {
                "processed": int(context.get("current") or 0),
                "total": int(context.get("total") or 0),
                "transferred": int(context.get("transferred") or 0),
                "elapsed_ms": max(0, int(elapsed_ms or 0)),
                "stopped": bool(stopped),
                "task_counts": dict(task_counts or {}),
            },
        }
