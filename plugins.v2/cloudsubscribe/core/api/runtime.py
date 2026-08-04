"""运行状态、离线任务与历史操作 API。"""

from typing import Any, Dict, Optional

from app.core.config import settings
from app.log import logger

from .. import CloudDriveCapability, OwnerDelegator
from ...utils import clear_magnet_metadata_cache


class RuntimeApi(OwnerDelegator):
    def api_vue_stop_sync(self) -> dict:
        return self.api_stop_sync(settings.API_TOKEN)

    def api_vue_stop_sync_task(self, payload: Dict[str, Any]) -> dict:
        return self.api_stop_sync_task(
            settings.API_TOKEN,
            str((payload or {}).get("task_id") or ""),
        )

    def api_vue_runtime_status(self) -> dict:
        return self.api_runtime_status(settings.API_TOKEN)

    def api_vue_offline_tasks(self, refresh: bool = False) -> dict:
        return self.api_offline_tasks(settings.API_TOKEN, refresh)

    def api_vue_delete_offline_task(self, payload: Dict[str, Any]) -> dict:
        return self.api_delete_offline_task(
            settings.API_TOKEN,
            str((payload or {}).get("task_id") or ""),
            str((payload or {}).get("pending_key") or ""),
        )

    def api_vue_delete_offline_tasks(self, payload: Dict[str, Any]) -> dict:
        task_ids = [
            str(value).strip()
            for value in ((payload or {}).get("task_ids") or [])
            if str(value).strip()
        ]
        pending_keys = [
            str(value).strip()
            for value in ((payload or {}).get("pending_keys") or [])
            if str(value).strip()
        ]
        return self.api_delete_offline_tasks(
            settings.API_TOKEN, task_ids, pending_keys
        )

    def api_vue_retry_offline_tasks(self, payload: Dict[str, Any]) -> dict:
        pending_keys = [
            str(value).strip()
            for value in ((payload or {}).get("pending_keys") or [])
            if str(value).strip()
        ]
        task_ids = [
            str(value).strip()
            for value in ((payload or {}).get("task_ids") or [])
            if str(value).strip()
        ]
        return self.api_retry_offline_tasks(
            settings.API_TOKEN,
            pending_keys,
            task_ids,
        )

    def api_vue_clear_history(self, payload: Optional[Dict[str, Any]] = None) -> dict:
        return self.api_clear_history(
            settings.API_TOKEN,
            force=(payload or {}).get("force") is True,
        )

    def api_vue_delete_history(self, payload: Dict[str, Any]) -> dict:
        return self.api_delete_history(settings.API_TOKEN, payload or {})

    def api_vue_delete_history_batch(self, payload: Dict[str, Any]) -> dict:
        data = payload or {}
        return self.api_delete_history_batch(
            settings.API_TOKEN,
            identities=data.get("records") or [],
            delete_linked_files=data.get("delete_linked_files") is True,
        )

    def api_vue_upgrade_history(self, payload: Dict[str, Any]) -> dict:
        return self.api_upgrade_history(settings.API_TOKEN, payload or {})

    def api_vue_notify_history(self, payload: Dict[str, Any]) -> dict:
        return self.api_notify_history(settings.API_TOKEN, payload or {})

    def api_vue_retry_history(self, payload: Dict[str, Any]) -> dict:
        if not self._sync_handler:
            return {"success": False, "message": "同步处理器未初始化"}
        try:
            result = self._sync_handler.retry_history_record(
                record_time=str((payload or {}).get("time") or ""),
                share_url=str((payload or {}).get("share_url") or ""),
                file_name=str((payload or {}).get("file_name") or ""),
            )
            return {"success": True, "message": "历史记录已重新处理", "data": result}
        except Exception as error:
            logger.error(f"重新处理历史记录异常：{error}")
            return {"success": False, "message": str(error)}

    def api_vue_clear_cache(self) -> dict:
        counts = {
            "search_results": 0,
            "hdhive_web": 0,
            "hdhive_openapi": 0,
            "dian115_details": 0,
            "share_info": 0,
            "share_status": 0,
            "share_files": 0,
            "magnet_metadata": 0,
        }
        try:
            if self._search_handler:
                counts.update(self._search_handler.clear_search_cache())
            counts["magnet_metadata"] = clear_magnet_metadata_cache()
            counts.update(self.clear_platform_cache())
            if self._cloud_drive and self._cloud_drive.supports(
                    CloudDriveCapability.CACHE_MAINTENANCE
            ):
                cache_service = self._cloud_drive.require(
                    CloudDriveCapability.CACHE_MAINTENANCE
                )
                counts.update(cache_service.clear_cache())
            total = sum(int(value or 0) for value in counts.values())
            logger.info(f"插件缓存已清理：{total} 项")
            return {
                "success": True,
                "message": f"缓存已清理，共移除 {total} 项",
                "data": counts,
            }
        except Exception as error:
            logger.error(f"清理插件缓存失败：{error}")
            return {"success": False, "message": str(error)}
