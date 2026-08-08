"""配置保存与智能体配置 API。"""

from typing import Any, Dict

from app.log import logger
from fastapi import Request

from .account import clear_account_cache
from .page import clear_ui_options_cache
from .. import OwnerDelegator
from ..config import UIConfig
from ..services.runtime import sync_lock


class ConfigApi(OwnerDelegator):
    AGENT_CONFIG_FIELDS = frozenset(
        {"show_sidebar_nav", "agent_enabled", "notify", "search_cache_enabled", "search_cache_ttl_minutes",
         "search_concurrency", "subscription_concurrency", "pansou_result_limit", "hdhive_candidate_limit"})
    _AGENT_BOOL_FIELDS = frozenset({"show_sidebar_nav", "agent_enabled", "notify", "search_cache_enabled"})
    _AGENT_INT_RANGES = {"search_cache_ttl_minutes": (1, 1440), "search_concurrency": (1, 5),
                         "subscription_concurrency": (1, 5), "pansou_result_limit": (1, 100),
                         "hdhive_candidate_limit": (1, 20), "hdhive_unlocks_per_minute": (1, 3),
                         "dian115_unlocks_per_minute": (1, 10)}

    def _queue_pending_config(self, payload: Dict[str, Any]) -> None:
        """保存运行期间最后一次配置，等待同步任务结束后应用。"""
        with self._pending_config_lock:
            self._pending_config = dict(payload)

    def _apply_pending_config(self) -> bool:
        """应用运行期间暂存的最新配置；调用方必须持有全局同步锁。"""
        with self._pending_config_lock:
            payload = self._pending_config
            self._pending_config = None
        if not payload:
            return False

        try:
            self._apply_plugin_config(payload, reset_runtime=False)
            logger.info("待更新配置已自动应用")
            return True
        except Exception as error:
            with self._pending_config_lock:
                if self._pending_config is None:
                    self._pending_config = payload
            logger.error(f"应用待更新配置失败，将在下次同步前重试：{error}")
            return False

    async def api_vue_save_config(self, request: Request) -> dict:
        """在 Vue 配置页内保存配置，避免触发宿主关闭弹窗。"""
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                return {"success": False, "message": "配置数据格式错误"}
            self.update_config(payload)
            clear_ui_options_cache()
            clear_account_cache()
            if not sync_lock.acquire(blocking=False):
                self._queue_pending_config(payload)
                return {
                    "success": True,
                    "message": "配置已保存，将在当前订阅任务结束后自动生效",
                    "data": payload,
                }
            try:
                self._apply_plugin_config(payload, reset_runtime=False)
            finally:
                sync_lock.release()
            return {"success": True, "message": "配置已保存并生效", "data": payload}
        except Exception as error:
            logger.error(f"保存插件配置失败：{error}")
            return {"success": False, "message": str(error)}

    def update_agent_config(self, updates: Dict[str, Any]) -> dict:
        """校验并应用智能体允许修改的非敏感配置。"""
        updates = dict(updates or {})
        if not updates:
            return {"success": False, "message": "没有提供需要修改的配置"}
        unknown = sorted(set(updates) - self.AGENT_CONFIG_FIELDS)
        if unknown:
            return {
                "success": False,
                "message": f"不允许智能体修改这些配置：{', '.join(unknown)}",
            }

        normalized: Dict[str, Any] = {}
        for key, value in updates.items():
            if key in self._AGENT_BOOL_FIELDS:
                if not isinstance(value, bool):
                    return {"success": False, "message": f"配置 {key} 必须是布尔值"}
                normalized[key] = value
                continue
            minimum, maximum = self._AGENT_INT_RANGES[key]
            if isinstance(value, bool) or not isinstance(value, int):
                return {"success": False, "message": f"配置 {key} 必须是整数"}
            if not minimum <= value <= maximum:
                return {
                    "success": False,
                    "message": f"配置 {key} 必须在 {minimum} 到 {maximum} 之间",
                }
            normalized[key] = value

        payload = dict(self._applied_config or UIConfig.get_default_config())
        changed = {
            key: value
            for key, value in normalized.items()
            if payload.get(key) != value
        }
        if not changed:
            return {"success": True, "message": "配置未变化", "data": {"changed": {}}}
        payload.update(changed)
        self.update_config(payload)
        if not sync_lock.acquire(blocking=False):
            self._queue_pending_config(payload)
            return {
                "success": True,
                "message": "配置已保存，将在当前订阅任务结束后自动生效",
                "data": {"changed": changed, "pending": True},
            }
        try:
            self._apply_plugin_config(payload, reset_runtime=False)
        finally:
            sync_lock.release()
        return {
            "success": True,
            "message": "配置已保存并生效",
            "data": {"changed": changed, "pending": False},
        }
