"""MoviePilot 订阅搜索调度钩子。"""

from typing import Callable, List, Optional, Tuple

from app.chain.subscribe import SubscribeChain
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from .. import OwnerDelegator


class SubscriptionSearchHook(OwnerDelegator):
    """按接管时段分流 MoviePilot 订阅搜索，并精确恢复原函数。"""

    _JOB_IDS = ("subscribe_search", "new_subscribe_search", "subscribe_refresh")

    def _install_subscribe_search_takeover(self) -> None:
        if not self._enabled:
            return
        try:
            from app.scheduler import Scheduler

            scheduler = Scheduler.get_existing_instance()
            if scheduler is None:
                logger.debug("MoviePilot 调度器尚未就绪，等待平台注册后安装订阅接管")
                return
            jobs = getattr(scheduler, "_jobs", None) or {}
            newly_installed = []
            for job_id in self._JOB_IDS:
                job = jobs.get(job_id)
                if not job:
                    continue
                current = job.get("func")
                if getattr(current, "__self__", None) is self:
                    continue
                self._subscribe_search_originals.setdefault(job_id, current)
                job["func"] = (
                    self._dispatch_subscribe_refresh
                    if job_id == "subscribe_refresh"
                    else self._dispatch_subscribe_search
                )
                newly_installed.append(job_id)
            if newly_installed:
                logger.info(f"MoviePilot 订阅搜索路由已接管：{', '.join(newly_installed)}")
            else:
                logger.debug("MoviePilot 订阅搜索路由已保持接管")
        except Exception as error:
            logger.warning(f"安装 MoviePilot 订阅搜索路由失败：{error}")

    def _restore_subscribe_search_takeover(self) -> None:
        originals = dict(self._subscribe_search_originals or {})
        if not originals:
            return
        try:
            from app.scheduler import Scheduler

            scheduler = Scheduler.get_existing_instance()
            jobs = getattr(scheduler, "_jobs", None) if scheduler else {}
            jobs = jobs or {}
            for job_id, original in originals.items():
                job = jobs.get(job_id)
                current = job.get("func") if job else None
                if job and getattr(current, "__self__", None) is self:
                    job["func"] = original
        except Exception as error:
            logger.warning(f"恢复 MoviePilot 订阅搜索路由失败：{error}")
        finally:
            self._subscribe_search_originals = {}

    def _dispatch_subscribe_search(
            self,
            sid: Optional[int] = None,
            state: Optional[str] = "R",
            manual: Optional[bool] = False,
            progress_callback: Optional[Callable[..., None]] = None,
    ):
        use_plugin = self._is_takeover_active()
        if state == "N" and self._enabled and self._takeover_new_subscribes:
            use_plugin = True
        if sid and self._is_subscribe_excluded(sid):
            use_plugin = False
        if not use_plugin:
            return SubscribeChain().search(
                sid=sid,
                state=state,
                manual=manual,
                progress_callback=progress_callback,
            )

        # MoviePilot 自身会每 5 分钟触发新增订阅搜索；接管状态下仅消费
        # 平台自动任务，统一由插件配置的 Cron 执行自动同步。
        if not bool(manual):
            return True

        if sid is None:
            return self._dispatch_all_subscribe_search(
                state=state,
                manual=manual,
                progress_callback=progress_callback,
            )

        logger.info(
            f"订阅搜索转入网盘任务：subscribe_id={sid or 'ALL'}，"
            f"manual={bool(manual)}"
        )
        return self.queue_subscribe_search(
            subscribe_id=sid,
            subscribe_state=state,
            progress_callback=progress_callback,
        )

    def _dispatch_subscribe_refresh(
            self,
            progress_callback: Optional[Callable[..., None]] = None,
    ):
        """接管态阻止 RSS/PT 刷新再次进入 MoviePilot 全量订阅匹配。"""
        if not self._is_takeover_active():
            return SubscribeChain().refresh(progress_callback=progress_callback)

        if progress_callback:
            progress_callback(
                value=100,
                text="订阅已由网盘订阅助手接管，跳过 MoviePilot 原生资源刷新",
            )
        logger.debug("接管态已跳过 MoviePilot 原生订阅资源刷新")
        return True

    def _dispatch_all_subscribe_search(
            self,
            state: Optional[str],
            manual: Optional[bool],
            progress_callback: Optional[Callable[..., None]],
    ) -> bool:
        """将全量平台任务拆成插件接管与原生保留两部分。"""
        try:
            subscribes = SubscribeOper().list(state or "N,R") or []
        except Exception as error:
            logger.warning(f"读取订阅接管范围失败，已回退 MoviePilot 原生搜索：{error}")
            return SubscribeChain().search(
                state=state,
                manual=manual,
                progress_callback=progress_callback,
            )

        managed_ids, native_ids = self._partition_subscribe_ids(subscribes)
        logger.info(
            f"订阅搜索分流：状态={state or 'N,R'}，"
            f"插件处理 {len(managed_ids)} 个，原生处理 {len(native_ids)} 个"
        )
        for index, subscribe_id in enumerate(managed_ids):
            self.queue_subscribe_search(
                subscribe_id=subscribe_id,
                subscribe_state=state,
                progress_callback=progress_callback if index == 0 else None,
            )
        for index, subscribe_id in enumerate(native_ids):
            SubscribeChain().search(
                sid=subscribe_id,
                state=None,
                manual=manual,
                progress_callback=(
                    progress_callback
                    if not managed_ids and index == 0
                    else None
                ),
            )
        return True

    def _partition_subscribe_ids(self, subscribes) -> Tuple[List[int], List[int]]:
        """按过滤规则划分插件与 MoviePilot 原生搜索的订阅。"""
        managed_ids = []
        native_ids = []
        for subscribe in subscribes:
            subscribe_id = getattr(subscribe, "id", None)
            if not subscribe_id:
                continue
            target = native_ids if self._is_subscribe_excluded(subscribe_id) else managed_ids
            target.append(subscribe_id)
        return managed_ids, native_ids
