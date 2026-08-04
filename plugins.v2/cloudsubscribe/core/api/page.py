"""配置页选项与详情页数据 API。"""

from app.core.config import settings
from app.log import logger
from app.schemas.types import NotificationType
from cachetools import TTLCache, cached

from .. import CloudDriveCapability, OwnerDelegator
from ..config import UIConfig

_UI_OPTIONS_CACHE = TTLCache(maxsize=1, ttl=2 * 60)


class PageApi(OwnerDelegator):
    def api_vue_page_data(self) -> dict:
        history = self.get_data("history") or []
        return {
            "success": True,
            "data": {
                "history": history,
                "emby_play_items": self._history_emby_play_items(history),
                "offline_supported": bool(
                    self._cloud_drive
                    and self._cloud_drive.supports(CloudDriveCapability.OFFLINE_TASKS)
                ),
                "runtime": {
                    "status": self._sync_status,
                    "task": self._sync_task_text,
                    "progress": self._sync_progress,
                    "context": dict(self._sync_context),
                    "tasks": self._serialize_sync_tasks(),
                },
            },
        }

    @cached(cache=_UI_OPTIONS_CACHE)
    def api_vue_ui_options(self) -> dict:
        from ...search.pansou import PanSouClient

        providers = (
            self._cloud_drive_registry.available()
            if self._cloud_drive_registry else []
        )
        accounts = {}
        for provider in providers:
            if not provider.supports(CloudDriveCapability.ACCOUNT):
                continue
            accounts[provider.key] = self._cached_account_info(
                f"drive:{provider.key}",
                {
                    "connected": False,
                    "error": "点击刷新按钮读取账户信息",
                },
            )
        account = accounts.get(self._cloud_drive_key, {
            "connected": False,
            "error": "请先配置当前网盘账号",
        })

        search_accounts = {
            "hdhive": {
                "connected": False,
                "error": "启用并保存 HDHive 配置后读取账户信息",
            },
            "dian115": {
                "connected": False,
                "error": "启用并保存 Dian115 配置后读取账户信息",
            },
            "juying": {
                "connected": False,
                "error": "启用并保存聚影配置后读取账户信息",
            },
        }

        for source in search_accounts:
            search_accounts[source] = self._cached_account_info(
                f"search:{source}", search_accounts[source]
            )
        cloud_drives = [
            {
                "title": provider.name,
                "value": provider.key,
                "capabilities": sorted(
                    capability.value for capability in provider.capabilities
                ),
                "resource_types": sorted(provider.resource_types),
                "policy": {
                    "pagination_mode": provider.policy.pagination_mode,
                    "max_page_size": provider.policy.max_page_size,
                    "supports_batch": provider.policy.supports_batch,
                    "max_batch_size": provider.policy.max_batch_size,
                    "supports_cancel": provider.policy.supports_cancel,
                    "max_concurrency": provider.policy.max_concurrency,
                    "cache_ttl_seconds": dict(provider.policy.cache_ttl_seconds),
                },
            }
            for provider in providers
        ]
        pansou_options = {
            "status": "unavailable",
            "plugins": [],
            "channels": [],
            "cloud_types": [
                {
                    "title": PanSouClient.TYPE_NAMES.get(value, value),
                    "value": value,
                }
                for value in PanSouClient.SUPPORTED_CLOUD_TYPES
            ],
        }
        pansou_url = str(getattr(self, "_pansou_url", "") or "").strip()
        if pansou_url:
            client = self._pansou_client or PanSouClient(
                base_url=pansou_url,
                auth_enabled=False,
                proxy=settings.PROXY,
                search_timeout=5,
            )
            health = client.health(timeout=3)
            pansou_options.update({
                "status": str(health.get("status") or "error"),
                "error": str(health.get("error") or ""),
                "plugins": [
                    {"title": value, "value": value}
                    for value in health.get("plugins", [])
                ],
                "channels": [
                    {"title": value, "value": value}
                    for value in health.get("channels", [])
                ],
            })
        return {
            "success": True,
            "data": {
                "defaults": UIConfig.get_default_config(),
                "subscribes": UIConfig.get_subscribe_options_grouped(),
                "sites": UIConfig.get_site_name_options(),
                "mediaservers": UIConfig.get_media_server_options(),
                "notification_types": [
                    {"title": item.value, "value": item.name}
                    for item in NotificationType
                ],
                "account": account,
                "accounts": accounts,
                "search_accounts": search_accounts,
                "cloud_drives": cloud_drives,
                "pansou": pansou_options,
            },
        }

    def api_vue_cloud_directories(
            self, path: str = "/", provider: str = ""
    ) -> dict:
        """列出指定或当前网盘目录，供配置页选择转存路径。"""
        normalized_path = str(path or "/").strip()
        if not normalized_path.startswith("/"):
            normalized_path = f"/{normalized_path}"
        normalized_path = normalized_path.rstrip("/") or "/"
        drive = self._cloud_drive
        provider_key = str(provider or "").strip().lower()
        if provider_key and self._cloud_drive_registry:
            try:
                drive = self._cloud_drive_registry.get(provider_key)
            except KeyError:
                return {"success": False, "message": "网盘提供方不存在"}
        if not drive or not drive.supports(
                CloudDriveCapability.DIRECTORY_READ
        ):
            return {"success": False, "message": "当前网盘不支持目录浏览"}
        try:
            service = drive.require(CloudDriveCapability.DIRECTORY_READ)
            directories = service.list_directories(normalized_path)
            breadcrumbs = [{"name": "根目录", "path": "/"}]
            current_path = ""
            for part in (item for item in normalized_path.split("/") if item):
                current_path = f"{current_path}/{part}"
                breadcrumbs.append({"name": part, "path": current_path})
            return {
                "success": True,
                "data": {
                    "path": normalized_path,
                    "breadcrumbs": breadcrumbs,
                    "directories": directories,
                },
            }
        except Exception as error:
            logger.error(f"读取网盘目录失败：{normalized_path}，{error}")
            return {"success": False, "message": f"读取网盘目录失败：{error}"}


def clear_ui_options_cache() -> None:
    _UI_OPTIONS_CACHE.clear()
