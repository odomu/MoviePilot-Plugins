"""订阅与平台搜索钩子。"""

import inspect
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, List, Optional, Tuple

from app.chain.subscribe import SubscribeChain
from app.db.subscribe_oper import SubscribeOper
from app.log import logger

from .. import OwnerDelegator


class SubscriptionSearchHook(OwnerDelegator):
    """按接管时段分流订阅搜索，并精确恢复原函数。"""

    _JOB_IDS = ("subscribe_search", "new_subscribe_search", "subscribe_refresh")
    _PLATFORM_SEARCH_METHODS = {
        "search_by_id": "sync",
        "search_by_title": "sync",
        "async_search_by_id": "async",
        "async_search_by_title": "async",
        "async_search_by_title_stream": "stream",
        "async_search_by_id_stream": "stream",
    }

    def _install_subscribe_search_takeover(self) -> None:
        if not self._enabled:
            return
        self._install_platform_search_block()
        self._install_subscribe_chain_takeover()
        try:
            from app.scheduler import Scheduler

            scheduler = (
                Scheduler.get_existing_instance()
                if hasattr(Scheduler, "get_existing_instance")
                else Scheduler()
            )
            if scheduler is None:
                logger.debug("调度器尚未就绪，等待平台注册后安装订阅接管")
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
                logger.info(f"订阅搜索路由已接管：{', '.join(newly_installed)}")
            else:
                logger.debug("订阅搜索路由已保持接管")
        except Exception as error:
            logger.warning(f"安装订阅搜索路由失败：{error}")

    def _restore_subscribe_search_takeover(self) -> None:
        self._restore_platform_search_block()
        self._restore_subscribe_chain_takeover()
        originals = dict(self._subscribe_search_originals or {})
        if not originals:
            return
        try:
            from app.scheduler import Scheduler

            scheduler = (
                Scheduler.get_existing_instance()
                if hasattr(Scheduler, "get_existing_instance")
                else Scheduler()
            )
            jobs = getattr(scheduler, "_jobs", None) if scheduler else {}
            jobs = jobs or {}
            for job_id, original in originals.items():
                job = jobs.get(job_id)
                current = job.get("func") if job else None
                if job and getattr(current, "__self__", None) is self:
                    job["func"] = original
        except Exception as error:
            logger.warning(f"恢复订阅搜索路由失败：{error}")
        finally:
            self._subscribe_search_originals = {}

    def _install_platform_search_block(self) -> None:
        """挂接平台公开资源搜索入口，在 block 策略生效时提前终止。"""
        try:
            from app.chain.search import SearchChain

            installed = []
            for method_name, method_type in self._PLATFORM_SEARCH_METHODS.items():
                current = getattr(SearchChain, method_name, None)
                if not callable(current):
                    logger.warning(f"搜索入口不存在：SearchChain.{method_name}")
                    continue
                if getattr(current, "__cloudsubscribe_owner__", None) is self:
                    continue

                original = getattr(
                    current, "__cloudsubscribe_original__", current
                )
                self._platform_search_originals.setdefault(method_name, original)
                wrapper = self._create_platform_search_wrapper(
                    method_name=method_name,
                    method_type=method_type,
                    original=original,
                )
                setattr(SearchChain, method_name, wrapper)
                installed.append(method_name)
            if installed:
                logger.info(
                    "平台搜索阻止器已安装：" + ", ".join(installed)
                )
        except Exception as error:
            logger.warning(f"安装平台搜索阻止器失败：{error}")

    def _restore_platform_search_block(self) -> None:
        """仅恢复当前插件实例安装的平台搜索方法。"""
        originals = dict(self._platform_search_originals or {})
        if not originals:
            return
        try:
            from app.chain.search import SearchChain

            for method_name, original in originals.items():
                current = getattr(SearchChain, method_name, None)
                if getattr(current, "__cloudsubscribe_owner__", None) is self:
                    setattr(SearchChain, method_name, original)
        except Exception as error:
            logger.warning(f"恢复平台搜索入口失败：{error}")
        finally:
            self._platform_search_originals = {}

    def _create_platform_search_wrapper(
            self,
            method_name: str,
            method_type: str,
            original: Callable,
    ) -> Callable:
        owner = self

        if method_type == "stream":
            @wraps(original)
            async def stream_wrapper(chain, *args, **kwargs):
                if owner._is_platform_search_blocked():
                    yield {
                        "type": "done",
                        "stage": "done",
                        "value": 100,
                        "text": "网盘订阅接管中，已阻止平台搜索",
                        "items": [],
                        "total_items": 0,
                    }
                    return
                async for event in original(chain, *args, **kwargs):
                    yield event

            wrapper = stream_wrapper
        elif method_type == "async":
            @wraps(original)
            async def async_wrapper(chain, *args, **kwargs):
                if owner._is_platform_search_blocked():
                    return []
                return await original(chain, *args, **kwargs)

            wrapper = async_wrapper
        else:
            @wraps(original)
            def sync_wrapper(chain, *args, **kwargs):
                if owner._is_platform_search_blocked():
                    return []
                return original(chain, *args, **kwargs)

            wrapper = sync_wrapper

        wrapper.__cloudsubscribe_owner__ = self
        wrapper.__cloudsubscribe_original__ = original
        return wrapper

    def _is_platform_search_blocked(self) -> bool:
        return (
                self._platform_download_policy == "block"
                and self._is_takeover_active()
        )

    def _install_subscribe_chain_takeover(self) -> None:
        """挂接 SubscribeChain 内部的单订阅搜索执行器（适配 MoviePilot v3 持久化队列及单项搜索）。"""
        try:
            from app.chain.subscribe import SubscribeChain

            method_name = "_process_search_subscription"
            current = getattr(SubscribeChain, method_name, None)
            if not callable(current):
                return
            if getattr(current, "__cloudsubscribe_owner__", None) is self:
                return

            original = getattr(
                current, "__cloudsubscribe_original__", current
            )
            originals = getattr(self, "_subscribe_chain_originals", None)
            if originals is not None:
                originals.setdefault(method_name, original)
            wrapper = self._create_subscribe_chain_wrapper(
                method_name=method_name,
                original=original,
            )
            setattr(SubscribeChain, method_name, wrapper)
            logger.debug("已安装订阅执行接管器")
        except Exception as error:
            logger.warning(f"安装订阅执行接管器失败：{error}")

    def _restore_subscribe_chain_takeover(self) -> None:
        """恢复 SubscribeChain 的原始搜索执行方法。"""
        originals = dict(getattr(self, "_subscribe_chain_originals", None) or {})
        if not originals:
            return
        try:
            from app.chain.subscribe import SubscribeChain

            for method_name, original in originals.items():
                current = getattr(SubscribeChain, method_name, None)
                if getattr(current, "__cloudsubscribe_owner__", None) is self:
                    setattr(SubscribeChain, method_name, original)
        except Exception as error:
            logger.warning(f"恢复订阅执行接管器失败：{error}")
        finally:
            if hasattr(self, "_subscribe_chain_originals"):
                self._subscribe_chain_originals = {}

    def _create_subscribe_chain_wrapper(
            self,
            method_name: str,
            original: Callable,
    ) -> Callable:
        owner = self

        @wraps(original)
        def process_search_subscription_wrapper(
                chain_self,
                subscribe,
                searchchain=None,
                execution_context=None,
                *args,
                **kwargs,
        ):
            return owner._dispatch_process_search_subscription(
                original=original,
                chain_self=chain_self,
                subscribe=subscribe,
                searchchain=searchchain,
                execution_context=execution_context,
                *args,
                **kwargs,
            )

        process_search_subscription_wrapper.__cloudsubscribe_owner__ = self
        process_search_subscription_wrapper.__cloudsubscribe_original__ = original
        return process_search_subscription_wrapper

    def _dispatch_process_search_subscription(
            self,
            original: Callable,
            chain_self: Any,
            subscribe: Any,
            searchchain: Any = None,
            execution_context: Any = None,
            *args: Any,
            **kwargs: Any,
    ) -> Any:
        if searchchain is None and "searchchain" in kwargs:
            searchchain = kwargs.pop("searchchain")
        if execution_context is None and "execution_context" in kwargs:
            execution_context = kwargs.pop("execution_context")

        subscribe_id = getattr(subscribe, "id", None)
        subscribe_state = getattr(subscribe, "state", "R")
        use_plugin = self._is_takeover_active()
        if subscribe_state == "N" and self._enabled and self._takeover_new_subscribes:
            use_plugin = True
        if subscribe_id and self._is_subscribe_excluded(subscribe_id):
            use_plugin = False

        if not use_plugin:
            return original(
                chain_self,
                subscribe,
                searchchain=searchchain,
                execution_context=execution_context,
                *args,
                **kwargs,
            )

        # 平台 v3 订阅搜索接管逻辑：
        # 1. 尝试更新订阅 last_search 时间戳，保持平台最近搜索事实同步
        if execution_context is None or not getattr(execution_context, "resuming_sites", False):
            apply_update = getattr(chain_self, "_SubscribeChain__apply_subscribe_update", None)
            if callable(apply_update):
                try:
                    subscribe = apply_update(
                        subscribe,
                        {"last_search": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                        scene="search",
                    )
                except Exception as update_err:
                    logger.debug(f"更新订阅 last_search 失败（可忽略）：{update_err}")

        # 2. 如果存在 execution_context，通知当前阶段进入 searching
        if execution_context and hasattr(execution_context, "report_phase"):
            try:
                execution_context.report_phase("searching")
            except Exception:
                pass

        # 3. 投递到网盘订阅助手搜索队列
        subscribe_name = getattr(subscribe, "name", "") or getattr(subscribe, "title", "")
        logger.debug(
            f"订阅搜索转入网盘任务：id={subscribe_id or 'ALL'}，标题={subscribe_name}"
        )
        self.queue_subscribe_search(
            subscribe_id=subscribe_id,
            subscribe_state=subscribe_state,
            progress_callback=None,
        )

        # 4. 返回 subscribeSnapshot，供 v3 的 SubscriptionSearchTaskRunner
        # 正常调用 finish_returned_search_task 将原生队列任务标记为 completed 终态。
        return subscribe

    @staticmethod
    def _call_subscribe_chain_search(**kwargs) -> Any:
        """安全调用 SubscribeChain.search，根据支持的参数自动适配，防止 TypeError。同时兼容 v2 和 v3。"""
        chain = SubscribeChain()
        search_func = getattr(chain, "search", None)
        if not callable(search_func):
            return None

        sids = kwargs.get("sids")
        try:
            sig = inspect.signature(search_func)
            has_var_kwargs = any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            )
            # 如果原生 search 支持 sids 或支持 **kwargs（如 v3）
            if has_var_kwargs or "sids" in sig.parameters:
                filtered = kwargs if has_var_kwargs else {k: v for k, v in kwargs.items() if k in sig.parameters}
                return search_func(**filtered)

            # 原生 search 不支持 sids（如 v2 环境）
            if sids:
                single_kwargs = dict(kwargs)
                single_kwargs.pop("sids", None)
                results = []
                for sub_id in sids:
                    single_kwargs["sid"] = sub_id
                    filtered = {k: v for k, v in single_kwargs.items() if k in sig.parameters}
                    results.append(search_func(**filtered))
                return results[-1] if results else None

            filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
            return search_func(**filtered)
        except Exception as err:
            logger.debug(f"检查 SubscribeChain.search 签名失败，尝试直接调用：{err}")
            try:
                return search_func(**kwargs)
            except TypeError:
                if sids:
                    base_keys = ("state", "manual", "progress_callback")
                    base_kwargs = {k: v for k, v in kwargs.items() if k in base_keys}
                    results = []
                    for sub_id in sids:
                        results.append(search_func(sid=sub_id, **base_kwargs))
                    return results[-1] if results else None
                base_keys = ("sid", "state", "manual", "progress_callback")
                base_kwargs = {k: v for k, v in kwargs.items() if k in base_keys}
                return search_func(**base_kwargs)

    @staticmethod
    def _call_subscribe_chain_refresh(**kwargs) -> Any:
        """安全调用 SubscribeChain.refresh，根据支持的参数自动适配。"""
        chain = SubscribeChain()
        refresh_func = getattr(chain, "refresh", None)
        if not callable(refresh_func):
            return None
        try:
            sig = inspect.signature(refresh_func)
            has_var_kwargs = any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            )
            if has_var_kwargs:
                return refresh_func(**kwargs)
            filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
            return refresh_func(**filtered)
        except Exception as err:
            logger.debug(f"检查 SubscribeChain.refresh 签名失败，尝试直接调用：{err}")
            try:
                return refresh_func(**kwargs)
            except TypeError:
                return refresh_func(progress_callback=kwargs.get("progress_callback"))

    def _dispatch_subscribe_search(
            self,
            sid: Optional[int] = None,
            state: Optional[str] = "R",
            manual: Optional[bool] = False,
            progress_callback: Optional[Callable[..., None]] = None,
            sids: Optional[Tuple[int, ...]] = None,
            scheduled_interval: Optional[int] = None,
            **kwargs: Any,
    ):
        use_plugin = self._is_takeover_active()
        if state == "N" and self._enabled and self._takeover_new_subscribes:
            use_plugin = True
        if sid and self._is_subscribe_excluded(sid):
            use_plugin = False
        if not use_plugin:
            return self._call_subscribe_chain_search(
                sid=sid,
                state=state,
                manual=manual,
                progress_callback=progress_callback,
                sids=sids,
                scheduled_interval=scheduled_interval,
                **kwargs,
            )

        # 自身会每 5 分钟触发新增订阅搜索；接管状态下仅消费
        # 平台自动任务，统一由插件配置的 Cron 执行自动同步。
        if not bool(manual):
            return True

        if sids:
            logger.info(
                f"订阅搜索转入网盘任务：subscribe_ids={sids}，"
                f"manual={bool(manual)}"
            )
            for subscribe_id in sids:
                if self._is_subscribe_excluded(subscribe_id):
                    self._call_subscribe_chain_search(
                        sid=subscribe_id,
                        state=state,
                        manual=manual,
                        progress_callback=progress_callback,
                        scheduled_interval=scheduled_interval,
                        **kwargs,
                    )
                else:
                    self.queue_subscribe_search(
                        subscribe_id=subscribe_id,
                        subscribe_state=state,
                        progress_callback=progress_callback,
                    )
            return True

        if sid is None:
            return self._dispatch_all_subscribe_search(
                state=state,
                manual=manual,
                progress_callback=progress_callback,
                scheduled_interval=scheduled_interval,
                **kwargs,
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
            mtype: Optional[str] = None,
            **kwargs: Any,
    ):
        """按平台下载策略决定接管态是否继续RSS/PT 刷新。"""
        if not self._is_takeover_active():
            return self._call_subscribe_chain_refresh(
                progress_callback=progress_callback,
                mtype=mtype,
                **kwargs,
            )

        if progress_callback:
            progress_callback(
                value=100,
                text="订阅已由网盘订阅助手接管，跳过原生资源刷新",
            )
        logger.debug("接管态已跳过原生订阅资源刷新")
        return True

    def _dispatch_all_subscribe_search(
            self,
            state: Optional[str],
            manual: Optional[bool],
            progress_callback: Optional[Callable[..., None]],
            scheduled_interval: Optional[int] = None,
            **kwargs: Any,
    ) -> bool:
        """将全量平台任务拆成插件接管与原生保留两部分。"""
        try:
            subscribes = SubscribeOper().list(state or "N,R") or []
        except Exception as error:
            logger.warning(f"读取订阅接管范围失败，已回退原生搜索：{error}")
            return self._call_subscribe_chain_search(
                state=state,
                manual=manual,
                progress_callback=progress_callback,
                scheduled_interval=scheduled_interval,
                **kwargs,
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
            self._call_subscribe_chain_search(
                sid=subscribe_id,
                state=None,
                manual=manual,
                progress_callback=(
                    progress_callback
                    if not managed_ids and index == 0
                    else None
                ),
                scheduled_interval=scheduled_interval,
                **kwargs,
            )
        return True

    def _partition_subscribe_ids(self, subscribes) -> Tuple[List[int], List[int]]:
        """按过滤规则划分插件与原生搜索的订阅。"""
        managed_ids = []
        native_ids = []
        for subscribe in subscribes:
            subscribe_id = getattr(subscribe, "id", None)
            if not subscribe_id:
                continue
            target = native_ids if self._is_subscribe_excluded(subscribe_id) else managed_ids
            target.append(subscribe_id)
        return managed_ids, native_ids
