"""配置页选项与详情页数据 API。"""

import copy
import datetime
from urllib.parse import urlencode

import pytz
from app.core.config import settings
from app.log import logger
from app.schemas.types import NotificationType

from .. import CloudDriveCapability, OwnerDelegator
from ..config import UIConfig
from ...utils.cache import create_platform_ttl_cache

_UI_OPTIONS_CACHE = create_platform_ttl_cache(
    "ui:options", maxsize=16, ttl=2 * 60
)


class PageApi(OwnerDelegator):
    @staticmethod
    def _history_filter_values(value: str) -> set[str]:
        return {
            item.strip().lower()
            for item in str(value or "").split(",")
            if item.strip()
        }

    def api_vue_page_data(
            self,
            page: int = 1,
            page_size: int = 10,
            keyword: str = "",
            resource_types: str = "",
            sources: str = "",
            task_types: str = "",
            statuses: str = "",
    ) -> dict:
        page_result = self._get_data_store().query_history_page(
            page=page,
            page_size=page_size,
            keyword=keyword,
            resource_types=self._history_filter_values(resource_types),
            sources=self._history_filter_values(sources),
            task_types=self._history_filter_values(task_types),
            statuses=self._history_filter_values(statuses),
        )
        page_groups = page_result.pop("groups", [])
        page_history = [
            record for group in page_groups
            for record in group.get("records", [])
        ]
        if self._sync_handler:
            page_history = self._sync_handler.prepare_history_records(
                page_history
            )
            prepared_by_id = {
                str(record.get("record_id") or ""): record
                for record in page_history
            }
            history_groups = []
            for group in page_groups:
                group["records"] = [
                    prepared_by_id.get(
                        str(record.get("record_id") or ""), record
                    )
                    for record in group.get("records", [])
                ]
                history_groups.append(
                    self._sync_handler.prepare_history_group(group)
                )
        else:
            page_history = [copy.deepcopy(item) for item in page_history]
            history_groups = [copy.deepcopy(group) for group in page_groups]
        return {
            "success": True,
            "data": {
                "history_groups": history_groups,
                "history_page": {
                    "page": page_result["page"],
                    "page_size": page_result["page_size"],
                    "total": page_result["total"],
                    "total_pages": page_result["total_pages"],
                    "filter_options": page_result["filter_options"],
                },
                "emby_play_items": self._history_emby_play_items(page_history),
            },
        }

    def api_vue_history_summary(self) -> dict:
        today = datetime.datetime.now(
            tz=pytz.timezone(settings.TZ)
        ).strftime("%Y-%m-%d")
        return {
            "success": True,
            "data": self._get_data_store().history_summary(today),
        }

    def api_vue_ui_options(self, scope: str = "base") -> dict:
        normalized_scope = str(scope or "base").strip().lower()
        normalized_scope = {
            "transfer": "subscriptions",
            "upgrade": "subscriptions",
            "manual": "subscriptions",
        }.get(normalized_scope, normalized_scope)
        if normalized_scope not in {
            "base", "subscriptions", "drive", "search", "notify"
        }:
            return {"success": False, "message": "未知的配置选项范围"}
        cache_key = f"instance:{id(self)}:{normalized_scope}"
        cached = _UI_OPTIONS_CACHE.get(cache_key)
        if isinstance(cached, dict):
            return copy.deepcopy(cached)

        if normalized_scope == "subscriptions":
            result = {
                "success": True,
                "data": {
                    "subscribes": UIConfig.get_subscribe_options_grouped(),
                },
            }
            _UI_OPTIONS_CACHE.set(cache_key, copy.deepcopy(result))
            return result

        if normalized_scope in {"base", "drive"}:
            providers = (
                self._cloud_drive_registry.available()
                if self._cloud_drive_registry else []
            )
            if normalized_scope == "base":
                result = {
                    "success": True,
                    "data": {
                        "defaults": UIConfig.get_default_config(),
                        "cloud_drives": [
                            {
                                "title": provider.name,
                                "value": provider.key,
                                "capabilities": sorted(
                                    capability.value
                                    for capability in provider.capabilities
                                ),
                                "resource_types": sorted(provider.resource_types),
                                "policy": {
                                    "pagination_mode": provider.policy.pagination_mode,
                                    "max_page_size": provider.policy.max_page_size,
                                    "supports_batch": provider.policy.supports_batch,
                                    "max_batch_size": provider.policy.max_batch_size,
                                    "supports_cancel": provider.policy.supports_cancel,
                                    "max_concurrency": provider.policy.max_concurrency,
                                    "cache_ttl_seconds": dict(
                                        provider.policy.cache_ttl_seconds
                                    ),
                                },
                            }
                            for provider in providers
                        ],
                    },
                }
            else:
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
                result = {
                    "success": True,
                    "data": {
                        "account": accounts.get(self._cloud_drive_key, {
                            "connected": False,
                            "error": "请先配置当前网盘账号",
                        }),
                        "accounts": accounts,
                    },
                }
            _UI_OPTIONS_CACHE.set(cache_key, copy.deepcopy(result))
            return result

        if normalized_scope == "notify":
            mediaservers = UIConfig.get_media_server_options()
            result = {
                "success": True,
                "data": {
                    "mediaservers": mediaservers,
                    "media_library_webhook_urls": {
                        str(item.get("value") or ""): (
                                "/api/v1/webhook/?"
                                + urlencode({
                            "token": settings.API_TOKEN,
                            "source": str(item.get("value") or ""),
                        })
                        )
                        for item in mediaservers
                        if str(item.get("type") or "").strip().lower() == "emby"
                           and str(item.get("value") or "").strip()
                    },
                    "notification_types": [
                        {"title": item.value, "value": item.name}
                        for item in NotificationType
                    ],
                },
            }
            _UI_OPTIONS_CACHE.set(cache_key, copy.deepcopy(result))
            return result

        from ...search.pansou import PanSouClient
        from ...search.types import PANSOU_RESOURCE_TYPES, resource_type_name

        search_accounts = {
            "hdhive": {
                "connected": False,
                "error": "配置并保存 HDHive 账户后读取账户信息",
            },
            "dian115": {
                "connected": False,
                "error": "配置并保存 Dian115 账户后读取账户信息",
            },
            "juying": {
                "connected": False,
                "error": "配置并保存聚影账户后读取账户信息",
            },
            "pinglian": {
                "connected": False,
                "error": "配置并保存盘链账户后读取账户信息",
            },
        }

        for source in search_accounts:
            search_accounts[source] = self._cached_account_info(
                f"search:{source}", search_accounts[source]
            )
        pansou_options = {
            "status": "unavailable",
            "plugins": [],
            "channels": [],
            "cloud_types": [
                {
                    "title": resource_type_name(value, value),
                    "value": value,
                }
                for value in PANSOU_RESOURCE_TYPES
            ],
        }
        pansou_url = str(getattr(self, "_pansou_url", "") or "").strip()
        if pansou_url:
            client = self._pansou_client or PanSouClient(
                base_url=pansou_url,
                auth_enabled=False,
                proxy=self._search_proxy,
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
        result = {
            "success": True,
            "data": {
                "search_accounts": search_accounts,
                "pansou": pansou_options,
            },
        }
        _UI_OPTIONS_CACHE.set(cache_key, copy.deepcopy(result))
        return result

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

    def api_vue_create_cloud_directory(
            self, path: str = "/", name: str = "", provider: str = ""
    ) -> dict:
        """在目录选择器当前目录创建子文件夹。"""
        normalized_path = str(path or "/").strip() or "/"
        folder_name = str(name or "").strip()
        if not folder_name or folder_name in {".", ".."} or "/" in folder_name or "\\" in folder_name:
            return {"success": False, "message": "文件夹名称无效"}
        drive = self._cloud_drive
        provider_key = str(provider or "").strip().lower()
        if provider_key and self._cloud_drive_registry:
            try:
                drive = self._cloud_drive_registry.get(provider_key)
            except KeyError:
                return {"success": False, "message": "网盘提供方不存在"}
        if not drive or not drive.supports(CloudDriveCapability.DIRECTORY_READ):
            return {"success": False, "message": "当前网盘不支持目录操作"}
        try:
            service = drive.require(CloudDriveCapability.DIRECTORY_READ)
            target_path = f"{normalized_path.rstrip('/')}/{folder_name}" if normalized_path != "/" else f"/{folder_name}"
            lookup = service.resolve_directory(target_path, create=True)
            if not lookup.checked or lookup.directory_id is None:
                return {"success": False, "message": "创建文件夹失败"}
            return {"success": True, "data": {"path": target_path}}
        except Exception as error:
            logger.error(f"创建网盘目录失败：{target_path if 'target_path' in locals() else folder_name}，{error}")
            return {"success": False, "message": f"创建文件夹失败：{error}"}


def clear_ui_options_cache() -> int:
    count = len(list(_UI_OPTIONS_CACHE.items()))
    _UI_OPTIONS_CACHE.clear()
    return count
