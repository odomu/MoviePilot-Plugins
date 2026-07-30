"""Vue 配置页与详情页的接口适配。"""

import ast
import asyncio
import inspect
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from threading import RLock, Thread
from typing import Any, Dict, List, Optional, Tuple

from app.chain.mediaserver import MediaServerChain
from app.core.config import settings
from app.core.metainfo import MetaInfo
from app.db import SessionFactory
from app.db.models.mediaserver import MediaServerItem
from app.db.subscribe_oper import SubscribeOper
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType, NotificationType
from cachetools import TTLCache, cached
from fastapi import Request

from .. import CloudDriveCapability, OwnerDelegator
from ..config import UIConfig
from ..services.runtime import sync_lock
from ...utils import clear_magnet_metadata_cache

_UI_OPTIONS_CACHE = TTLCache(maxsize=1, ttl=2 * 60)
_SEARCH_TEST_EXECUTOR = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="CloudSubscribe-SearchTest",
)
_ACCOUNT_INFO_CACHE = TTLCache(maxsize=16, ttl=5 * 60)
_ACCOUNT_REFRESH_GUARD = TTLCache(maxsize=16, ttl=30)
_ACCOUNT_INFO_LOCK = RLock()
_ACCOUNT_INFO_DATA_KEY = "account_info_cache"
_SEARCH_TEST_TIMEOUT_SECONDS = 30


class PluginApi(OwnerDelegator):
    """处理 Vue 页面发起的插件操作。"""

    AGENT_CONFIG_FIELDS = frozenset({
        "show_sidebar_nav",
        "agent_enabled",
        "notify",
        "search_cache_enabled",
        "search_cache_ttl_minutes",
        "search_concurrency",
        "subscription_concurrency",
        "pansou_result_limit",
        "hdhive_candidate_limit",
    })
    _AGENT_BOOL_FIELDS = frozenset({
        "show_sidebar_nav",
        "agent_enabled",
        "notify",
        "search_cache_enabled",
    })
    _AGENT_INT_RANGES = {
        "search_cache_ttl_minutes": (1, 1440),
        "search_concurrency": (1, 5),
        "subscription_concurrency": (1, 5),
        "pansou_result_limit": (1, 100),
        "hdhive_candidate_limit": (1, 20),
    }
    _SEARCH_TEST_CONFIG_FIELDS = {
        "pansou": frozenset({
            "pansou_url", "pansou_username", "pansou_password",
            "pansou_auth_enabled", "pansou_channels", "pansou_plugins",
            "pansou_cloud_types", "pansou_filter_include",
            "pansou_filter_exclude", "pansou_concurrency",
            "pansou_result_limit", "pansou_timeout",
        }),
        "hdhive": frozenset({
            "hdhive_query_mode", "hdhive_api_key", "hdhive_client_id",
            "hdhive_access_token", "hdhive_refresh_token",
            "hdhive_token_expires_at", "hdhive_username", "hdhive_password",
            "hdhive_candidate_limit", "hdhive_request_interval",
            "hdhive_torrentclaw_enabled",
            "hdhive_torrentclaw_subtitle_languages",
        }),
        "dian115": frozenset({
            "dian115_email", "dian115_password", "dian115_candidate_limit",
            "dian115_request_interval",
        }),
        "juying": frozenset({
            "juying_username", "juying_password", "juying_result_limit",
            "juying_request_interval",
        }),
        "seedhub": frozenset({"seedhub_result_limit"}),
        "butailing": frozenset({"butailing_result_limit"}),
    }

    def _test_search_config(
            self, source: str, overrides: Any
    ) -> Dict[str, Any]:
        """仅合并当前渠道测试真正需要的配置字段。"""
        base = dict(UIConfig.get_default_config())
        if isinstance(self._applied_config, dict):
            base.update(self._applied_config)
        allowed = self._SEARCH_TEST_CONFIG_FIELDS[source] | {"resource_type_order"}
        if isinstance(overrides, dict):
            base.update({
                key: value for key, value in overrides.items() if key in allowed
            })
        return {key: base.get(key) for key in allowed}

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

    @staticmethod
    def _history_group_key(media_type: str, tmdb_id: Any) -> str:
        return f"tmdb:{media_type or '未知类型'}:{tmdb_id}"

    def _history_emby_play_items(self, history: list) -> Dict[str, str]:
        """一次数据库查询返回可在已启用 Emby 中播放的历史汇总项。"""
        services = MediaServerHelper().get_services(type_filter="emby")
        active_services = {
            name: service
            for name, service in services.items()
            if not service.instance.is_inactive()
        }
        if not active_services:
            return {}
        eligible = {
            (int(item.get("tmdb_id")), str(item.get("type") or ""))
            for item in history
            if item.get("tmdb_id")
               and str(item.get("status") or "") == "成功"
               and not item.get("finalize_key")
        }
        if not eligible:
            return {}
        tmdb_ids = {tmdb_id for tmdb_id, _ in eligible}
        with SessionFactory() as db:
            rows = db.query(MediaServerItem).filter(
                MediaServerItem.server == "emby",
                MediaServerItem.tmdbid.in_(tmdb_ids),
            ).all()
        result = {}
        for row in rows:
            identity = (int(row.tmdbid or 0), str(row.item_type or ""))
            if identity not in eligible or not row.item_id:
                continue
            result[self._history_group_key(identity[1], identity[0])] = str(
                row.item_id
            )
        return result

    def api_vue_emby_play(self, item_id: str) -> dict:
        """仅从已启用 Emby 实例解析播放链接。"""
        item_id = str(item_id or "").strip()
        if not item_id:
            return {"success": False, "message": "缺少 Emby 媒体条目ID"}
        services = MediaServerHelper().get_services(type_filter="emby")
        chain = MediaServerChain()
        for name, service in services.items():
            if service.instance.is_inactive():
                continue
            item = chain.iteminfo(server=name, item_id=item_id)
            if not item:
                continue
            play_url = chain.get_play_url(server=name, item_id=item_id)
            if play_url:
                return {"success": True, "data": {"url": play_url}}
        return {"success": False, "message": "未在已启用的 Emby 中找到播放地址"}

    @staticmethod
    def _search_account_card(
            source: str, info: Dict[str, Any]
    ) -> Dict[str, Any]:
        """将搜索渠道账户数据转换为通用信息卡片。"""
        badge = str(info.get("level") or info.get("role") or "").strip()
        if badge.lower() == "vip":
            badge = "VIP"
        if source == "hdhive" and info.get("is_vip"):
            badge = "VIP"

        details = []

        def add_detail(label: str, value: Any) -> None:
            text = str(value or "").strip()
            if text:
                details.append({"label": label, "value": text})

        name = str(info.get("name") or "").strip()
        email = str(info.get("email") or "").strip().lower()
        if not name or "@" in name or (email and name.lower() == email):
            name = {
                "hdhive": "HDHive 用户",
                "dian115": "Dian115 用户",
                "juying": "聚影用户",
            }.get(source, "渠道用户")
        if source == "hdhive":
            add_detail("会员状态", "VIP" if info.get("is_vip") else "普通用户")
            add_detail("累计签到", f"{int(info.get('signin_days') or 0)} 天")
            add_detail("分享数量", f"{int(info.get('share_count') or 0)} 个")
            status = {
                "active": "正常",
                "inactive": "未激活",
                "suspended": "已停用",
            }.get(str(info.get("status") or "").lower())
            add_detail("账户状态", status)
        elif source == "dian115":
            add_detail("会员状态", "VIP" if info.get("is_vip") else "普通用户")
            add_detail(
                "连续签到", f"{int(info.get('consecutive_signin') or 0)} 天"
            )
            add_detail("已解锁", f"{int(info.get('unlock_count') or 0)} 次")
        else:
            add_detail("累计签到", f"{int(info.get('checkin_days') or 0)} 天")
            add_detail("上传资源", f"{int(info.get('upload_count') or 0)} 个")
            add_detail("收藏资源", f"{int(info.get('favorite_count') or 0)} 个")
        return {
            "connected": True,
            "user": {
                "name": name,
                "avatar": str(info.get("avatar") or ""),
                "membership_supported": False,
                "badge": badge,
            },
            "points": {
                "label": "可用积分",
                "available": max(0, int(info.get("points") or 0)),
            },
            "details": details,
        }

    def _load_search_account(self, source: str) -> Dict[str, Any]:
        """读取单个搜索渠道的账户信息。"""
        from ...search.dian115 import Dian115Client
        from ...search.hdhive import HDHiveClient
        from ...search.juying import JuyingClient

        client = None
        try:
            if source == "hdhive":
                if not self._hdhive_enabled:
                    return {
                        "connected": False,
                        "error": "启用并保存 HDHive 配置后读取账户信息",
                    }
                if not self._hdhive_username or not self._hdhive_password:
                    if self._hdhive_query_mode == "web":
                        return {
                            "connected": False,
                            "error": "请填写 HDHive 用户名和密码并保存配置",
                        }
                    return {
                        "connected": False,
                        "error": "HDHive OpenAPI 未提供个人信息接口，可配置网页账号读取",
                    }
                client = HDHiveClient(
                    username=self._hdhive_username,
                    password=self._hdhive_password,
                    proxy=settings.PROXY,
                    request_interval=self._hdhive_request_interval,
                    timeout=10,
                )
            elif source == "dian115":
                if not self._dian115_enabled:
                    return {
                        "connected": False,
                        "error": "启用并保存 Dian115 配置后读取账户信息",
                    }
                if not self._dian115_email or not self._dian115_password:
                    return {
                        "connected": False,
                        "error": "请填写 Dian115 邮箱和密码并保存配置",
                    }
                client = Dian115Client(
                    email=self._dian115_email,
                    password=self._dian115_password,
                    proxy=settings.PROXY,
                    request_interval=self._dian115_request_interval,
                    timeout=10,
                    get_data_func=self.get_data,
                    save_data_func=self.save_data,
                )
            elif source == "juying":
                if not self._juying_enabled:
                    return {
                        "connected": False,
                        "error": "启用并保存聚影配置后读取账户信息",
                    }
                if not self._juying_username or not self._juying_password:
                    return {
                        "connected": False,
                        "error": "请填写聚影账号和密码并保存配置",
                    }
                client = JuyingClient(
                    username=self._juying_username,
                    password=self._juying_password,
                    proxy=settings.PROXY,
                    request_timeout=10,
                    request_interval=self._juying_request_interval,
                    get_data_func=self.get_data,
                    save_data_func=self.save_data,
                )
            else:
                raise ValueError("不支持的搜索账户")
            return self._search_account_card(source, client.get_account_info())
        except Exception as error:
            logger.debug(f"读取{source}搜索账户信息失败：{error}")
            return {
                "connected": False,
                "error": "账户信息读取失败，请检查登录凭据或稍后重试",
            }
        finally:
            if client:
                client.close()

    def _load_drive_account(
            self, provider_key: str, force: bool = False
    ) -> Dict[str, Any]:
        """读取单个网盘账户；手动刷新时绕过支持的内部缓存。"""
        if not self._cloud_drive_registry:
            return {"connected": False, "error": "网盘服务尚未初始化"}
        try:
            provider = self._cloud_drive_registry.get(provider_key)
            if not provider.supports(CloudDriveCapability.ACCOUNT):
                return {"connected": False, "error": "当前网盘不支持账户信息"}
            service = provider.require(CloudDriveCapability.ACCOUNT)
            getter = service.get_account_info
            parameters = inspect.signature(getter).parameters
            if force and "cache_ttl" in parameters:
                return getter(cache_ttl=0)
            return getter()
        except Exception as error:
            logger.debug(f"读取{provider_key}网盘账户信息失败：{error}")
            return {
                "connected": False,
                "error": "网盘账户信息读取失败，请检查凭据或稍后重试",
            }

    def _account_info(
            self, account_key: str, refresh: bool = False
    ) -> Tuple[Dict[str, Any], bool]:
        """读取单卡片信息，持久化快照并实施刷新冷却。"""
        normalized_key = str(account_key or "").strip().lower()
        if ":" not in normalized_key:
            raise ValueError("账户卡片标识无效")
        category, source = normalized_key.split(":", 1)
        if category not in {"drive", "search"} or not source:
            raise ValueError("账户卡片标识无效")

        with _ACCOUNT_INFO_LOCK:
            stored = self.get_data(_ACCOUNT_INFO_DATA_KEY) or {}
            stored_account = (
                stored.get(normalized_key) if isinstance(stored, dict) else None
            )
            cached_account = (
                    _ACCOUNT_INFO_CACHE.get(normalized_key) or stored_account
            )
            if cached_account:
                _ACCOUNT_INFO_CACHE[normalized_key] = cached_account
            if refresh and normalized_key in _ACCOUNT_REFRESH_GUARD:
                return cached_account or {
                    "connected": False,
                    "error": "账户信息正在冷却，请稍后再刷新",
                }, True
            if not refresh and cached_account:
                return cached_account, False
            _ACCOUNT_REFRESH_GUARD[normalized_key] = True

        account = (
            self._load_drive_account(source, force=refresh)
            if category == "drive"
            else self._load_search_account(source)
        )
        account["refreshed_at"] = int(time.time())
        with _ACCOUNT_INFO_LOCK:
            _ACCOUNT_INFO_CACHE[normalized_key] = account
            stored = self.get_data(_ACCOUNT_INFO_DATA_KEY) or {}
            stored = dict(stored) if isinstance(stored, dict) else {}
            stored[normalized_key] = account
            self.save_data(_ACCOUNT_INFO_DATA_KEY, stored)
        return account, False

    def _cached_account_info(
            self, account_key: str, fallback: Dict[str, Any]
    ) -> Dict[str, Any]:
        """配置页只读取内存或持久化快照，不访问第三方接口。"""
        with _ACCOUNT_INFO_LOCK:
            cached = _ACCOUNT_INFO_CACHE.get(account_key)
            if cached:
                return cached
            stored = self.get_data(_ACCOUNT_INFO_DATA_KEY) or {}
            account = stored.get(account_key) if isinstance(stored, dict) else None
            if account:
                _ACCOUNT_INFO_CACHE[account_key] = account
                return account
            return fallback

    async def api_vue_refresh_account(self, request: Request) -> dict:
        """手动刷新单个账户信息卡片，不联动其他卡片或 Tab。"""
        try:
            payload = await request.json()
            account_key = str(
                payload.get("key") if isinstance(payload, dict) else ""
            ).strip()
            account, limited = await asyncio.to_thread(
                self._account_info, account_key, True
            )
            category, source = account_key.lower().split(":", 1)
            with _ACCOUNT_INFO_LOCK:
                _UI_OPTIONS_CACHE.clear()
            return {
                "success": True,
                "message": (
                    "刷新过于频繁，已显示最近一次账户信息"
                    if limited else "账户信息已刷新"
                ),
                "data": {
                    "key": f"{category}:{source}",
                    "account": account,
                    "limited": limited,
                },
            }
        except Exception as error:
            logger.debug(f"手动刷新账户信息失败：{error}")
            return {"success": False, "message": f"刷新账户信息失败：{error}"}

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

    def _build_test_search_handler(
            self,
            source: str,
            config: Dict[str, Any],
            deadline: Optional[float] = None,
    ):
        """使用当前表单配置创建隔离搜索器，不修改已保存配置或运行中服务。"""
        from ...handlers.search import SearchHandler
        from ...search.butailing import ButailingClient
        from ...search.hdhive import HDHiveOpenAPIClient
        from ...search.juying import JuyingClient, JuyingResourceService
        from ...search.pansou import PanSouClient
        from ...search.seedhub import SeedHubClient

        def as_list(value: Any) -> list:
            if isinstance(value, list):
                return list(value)
            if value is None:
                return []
            return [value]

        proxy = settings.PROXY
        hdhive_query_mode = str(config.get("hdhive_query_mode") or "api")
        if hdhive_query_mode not in {"api", "web"}:
            hdhive_query_mode = "web" if (
                    config.get("hdhive_username") and config.get("hdhive_password")
            ) else "api"
        hdhive_client = None
        if source == "hdhive" and hdhive_query_mode == "api":
            hdhive_client = HDHiveOpenAPIClient(
                app_secret=str(config.get("hdhive_api_key") or ""),
                client_id=str(config.get("hdhive_client_id") or ""),
                access_token=str(config.get("hdhive_access_token") or ""),
                refresh_token=str(config.get("hdhive_refresh_token") or ""),
                token_expires_at=float(
                    config.get("hdhive_token_expires_at") or 0
                ),
                proxy=proxy,
                request_interval=float(
                    config.get("hdhive_request_interval", 2) or 2
                ),
            )
        resource_type_order = list(dict.fromkeys(
            str(value).strip().lower()
            for value in as_list(config.get("resource_type_order"))
            if str(value).strip().lower()
            in {"115", "123", "quark", "guangya", "ed2k", "magnet"}
        ))
        if not resource_type_order:
            resource_type_order = ["115", "ed2k"]
        if source in {"seedhub", "butailing"} and "magnet" not in resource_type_order:
            resource_type_order.append("magnet")
        if source == "juying":
            resource_type_order.extend(
                resource_type
                for resource_type in JuyingResourceService.SUPPORTED_RESOURCE_TYPE_ORDER
                if resource_type not in resource_type_order
            )

        pansou_client = None
        pansou_timeout = 20
        if source == "pansou":
            pansou_url = str(config.get("pansou_url") or "").strip()
            if not pansou_url:
                raise ValueError("PanSou 服务地址为空")
            pansou_timeout = min(
                20, max(5, int(config.get("pansou_timeout", 30) or 30))
            )
            pansou_client = PanSouClient(
                base_url=pansou_url,
                username=str(config.get("pansou_username") or ""),
                password=str(config.get("pansou_password") or ""),
                auth_enabled=bool(config.get("pansou_auth_enabled", False)),
                proxy=proxy,
                search_timeout=pansou_timeout,
                get_data_func=self.get_data,
                save_data_func=self.save_data,
            )

        seedhub_client = SeedHubClient(proxy=proxy) if source == "seedhub" else None
        butailing_client = (
            ButailingClient(proxy=proxy) if source == "butailing" else None
        )
        juying_client = None
        if source == "juying":
            juying_client = JuyingClient(
                username=str(config.get("juying_username") or ""),
                password=str(config.get("juying_password") or ""),
                proxy=proxy,
                request_interval=float(
                    config.get("juying_request_interval", 1) or 1
                ),
                get_data_func=self.get_data,
                save_data_func=self.save_data,
            )
        handler = SearchHandler(
            pansou_client=pansou_client,
            hdhive_client=hdhive_client,
            seedhub_client=seedhub_client,
            butailing_client=butailing_client,
            juying_client=juying_client,
            pansou_enabled=source == "pansou",
            hdhive_enabled=source == "hdhive",
            dian115_enabled=source == "dian115",
            seedhub_enabled=source == "seedhub",
            butailing_enabled=source == "butailing",
            juying_enabled=source == "juying",
            hdhive_username=str(config.get("hdhive_username") or ""),
            hdhive_password=str(config.get("hdhive_password") or ""),
            hdhive_query_mode=hdhive_query_mode,
            # HDHive 测试使用独立只读路径；显式关闭自动解锁能力。
            hdhive_auto_unlock=False,
            hdhive_max_unlock_points=0,
            hdhive_max_points_per_sub=0,
            dian115_email=str(config.get("dian115_email") or ""),
            dian115_password=str(config.get("dian115_password") or ""),
            # 测试搜索不进入同步链；收费候选仅展示，不会消耗积分。
            dian115_auto_unlock=bool(
                config.get("dian115_auto_unlock", False)
            ),
            dian115_max_unlock_points=0,
            dian115_max_points_per_sub=0,
            pansou_channels=config.get("pansou_channels") or [],
            pansou_plugins=config.get("pansou_plugins") or [],
            pansou_cloud_types=config.get("pansou_cloud_types") or [],
            pansou_filter_include=config.get("pansou_filter_include") or [],
            pansou_filter_exclude=config.get("pansou_filter_exclude") or [],
            resource_type_order=resource_type_order,
            pansou_concurrency=config.get("pansou_concurrency") or None,
            pansou_result_limit=min(
                10, int(config.get("pansou_result_limit", 10) or 10)
            ),
            pansou_refresh=False,
            pansou_timeout=pansou_timeout,
            seedhub_result_limit=min(
                5, int(config.get("seedhub_result_limit", 20) or 20)
            ),
            butailing_result_limit=min(
                10, int(config.get("butailing_result_limit", 20) or 20)
            ),
            juying_result_limit=min(
                5, int(config.get("juying_result_limit", 5) or 5)
            ),
            search_source_order=[source],
            search_cache_enabled=False,
            search_concurrency=1,
            hdhive_candidate_limit=min(
                4, int(config.get("hdhive_candidate_limit", 4) or 4)
            ),
            hdhive_request_interval=float(
                config.get("hdhive_request_interval", 2) or 2
            ),
            dian115_candidate_limit=min(
                4, int(config.get("dian115_candidate_limit", 4) or 4)
            ),
            dian115_request_interval=float(
                config.get("dian115_request_interval", 1) or 1
            ),
            hdhive_torrentclaw_enabled=bool(
                config.get("hdhive_torrentclaw_enabled", False)
            ),
            hdhive_torrentclaw_subtitle_languages=as_list(
                config.get("hdhive_torrentclaw_subtitle_languages") or ["zh"]
            ),
            should_stop=(
                (lambda: time.monotonic() >= deadline) if deadline else None
            ),
        )
        handler.set_data_funcs(self.get_data, self.save_data)
        return handler

    def api_vue_search_tmdb_candidates(self, payload: Dict[str, Any]) -> dict:
        """按标题返回可供用户选择的 TMDB 电影和电视剧候选。"""
        title = str((payload or {}).get("title") or "").strip()
        if not title or len(title) > 100:
            return {"success": False, "message": "请输入 1 到 100 个字符的媒体名称"}
        try:
            meta = MetaInfo(title)
            candidates = self.chain.search_medias(
                meta=meta,
                source="themoviedb",
            ) or []
        except Exception as error:
            logger.warning(f"[{title}][TMDB] 媒体候选查询失败：{error}")
            return {"success": False, "message": f"TMDB 查询失败：{error}"}

        items = []
        seen = set()
        for candidate in candidates:
            candidate_type = getattr(candidate, "type", None)
            media_type = (
                "movie" if candidate_type == MediaType.MOVIE
                else "tv" if candidate_type == MediaType.TV
                else ""
            )
            try:
                tmdb_id = int(getattr(candidate, "tmdb_id", 0) or 0)
            except (TypeError, ValueError):
                tmdb_id = 0
            identity = (media_type, tmdb_id)
            if not media_type or tmdb_id <= 0 or identity in seen:
                continue
            seen.add(identity)
            items.append({
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "media_type_name": "电影" if media_type == "movie" else "电视剧",
                "title": str(getattr(candidate, "title", None) or title),
                "original_title": str(
                    getattr(candidate, "original_title", None) or ""
                ),
                "year": getattr(candidate, "year", None),
                "poster": str(getattr(candidate, "poster_path", None) or ""),
                "vote_average": getattr(candidate, "vote_average", None),
            })
            if len(items) >= 20:
                break
        return {
            "success": True,
            "message": f"TMDB 找到 {len(items)} 个候选",
            "data": {"items": items},
        }

    def api_vue_test_search_source(self, payload: Dict[str, Any]) -> dict:
        """使用页面输入执行隔离的单来源搜索，不触发下载、转存或历史写入。"""
        payload = dict(payload or {})
        source = str(payload.get("source") or "").strip().lower()
        source_names = {
            "hdhive": "HDHive",
            "dian115": "Dian115",
            "pansou": "PanSou",
            "juying": "聚影",
            "seedhub": "SeedHub",
            "butailing": "不太灵",
        }
        if source not in source_names:
            return {"success": False, "message": "不支持的搜索渠道"}
        title = str(payload.get("title") or "").strip()
        if not title or len(title) > 100:
            return {"success": False, "message": "请输入 1 到 100 个字符的媒体名称"}
        tmdb_id_value = str(payload.get("tmdb_id") or "").strip()
        try:
            tmdb_id = int(tmdb_id_value)
        except (TypeError, ValueError):
            return {"success": False, "message": "请先选择 TMDB 媒体条目"}
        if not 1 <= tmdb_id <= 999999999:
            return {"success": False, "message": "请先选择 TMDB 媒体条目"}
        media_type_value = str(payload.get("media_type") or "tv").strip().lower()
        if media_type_value not in {"movie", "tv"}:
            return {"success": False, "message": "媒体类型仅支持电影或电视剧"}
        media_type = MediaType.MOVIE if media_type_value == "movie" else MediaType.TV
        try:
            year = int(payload.get("year")) if str(payload.get("year") or "").strip() else None
        except (TypeError, ValueError):
            return {"success": False, "message": "年份必须是整数"}
        if year is not None and not 1900 <= year <= 2100:
            return {"success": False, "message": "年份必须在 1900 到 2100 之间"}
        try:
            season = int(payload.get("season") or 1) if media_type == MediaType.TV else None
        except (TypeError, ValueError):
            return {"success": False, "message": "季号必须是整数"}
        if season is not None and not 1 <= season <= 999:
            return {"success": False, "message": "季号必须在 1 到 999 之间"}
        config = self._test_search_config(source, payload.get("config"))
        original_title = str(payload.get("original_title") or "").strip()[:200]
        mediainfo = MediaInfo(
            type=media_type,
            title=title,
            year=str(year) if year is not None else None,
        )
        mediainfo.tmdb_id = tmdb_id
        mediainfo.original_title = original_title

        test_started = time.monotonic()
        test_timeout = (
            120 if source == "dian115" else _SEARCH_TEST_TIMEOUT_SECONDS
        )
        test_deadline = test_started + test_timeout

        def run_test() -> list:
            handler = None
            try:
                handler = self._build_test_search_handler(
                    source, config, deadline=test_deadline
                )
                return handler.test_source(
                    source=source,
                    mediainfo=mediainfo,
                    media_type=media_type,
                    season=season,
                )
            finally:
                if handler:
                    try:
                        handler.close(release_cache=False)
                    except Exception as close_error:
                        logger.debug(
                            f"[{source.upper()}] 测试搜索器关闭失败：{close_error}"
                        )

        try:
            logger.debug(
                f"[{title}{f' S{season:02d}' if season else ''}]"
                f"[{source.upper()}] 开始只读渠道测试："
                f"TMDB ID={tmdb_id}，类型={media_type_value}"
            )
            future = _SEARCH_TEST_EXECUTOR.submit(run_test)
            results = future.result(timeout=test_timeout)
        except FutureTimeoutError:
            future.cancel()
            logger.warning(
                f"[{title}{f' S{season:02d}' if season else ''}]"
                f"[{source.upper()}] 渠道测试超过 "
                f"{test_timeout} 秒，已提前返回"
            )
            return {
                "success": False,
                "message": (
                    f"{source_names[source]} 测试超过 "
                    f"{test_timeout} 秒，请检查渠道服务或代理状态"
                ),
            }
        except Exception as error:
            logger.warning(
                f"[{title}{f' S{season:02d}' if season else ''}]"
                f"[{source.upper()}] 渠道测试失败：{error}"
            )
            return {
                "success": False,
                "message": f"{source_names[source]} 测试失败：{error}",
            }
        logger.debug(
            f"[{title}{f' S{season:02d}' if season else ''}]"
            f"[{source.upper()}] 只读渠道测试完成："
            f"候选={len(results or [])}，耗时={time.monotonic() - test_started:.2f}s"
        )

        resource_type_names = {
            "115": "115",
            "123": "123云盘",
            "ed2k": "ED2K",
            "magnet": "Magnet",
            "quark": "夸克",
            "guangya": "光鸭",
            "aliyun": "阿里云盘",
            "alipan": "阿里云盘",
            "ali": "阿里云盘",
            "baidu": "百度网盘",
            "baidupan": "百度网盘",
            "xunlei": "迅雷云盘",
            "189": "天翼云盘",
            "123pan": "123云盘",
        }
        items = []
        resource_type_counts: Dict[str, int] = {}

        def display_size(item: Dict[str, Any]) -> Any:
            human = str(item.get("size_human") or "").strip()
            if human:
                return human
            value = item.get("size")
            if not isinstance(value, (int, float)) or value <= 0:
                return value or 0
            units = ("B", "KB", "MB", "GB", "TB")
            number = float(value)
            unit = 0
            while number >= 1024 and unit < len(units) - 1:
                number /= 1024
                unit += 1
            return f"{number:.2f}{units[unit]}"

        def display_tags(item: Dict[str, Any]) -> List[str]:
            values = [item.get("tags") or []]
            values.extend(
                item.get(key)
                for key in (
                    "resolution", "quality", "source_type", "codec",
                    "audio_codec", "hdr_type", "subtitle",
                )
                if item.get(key)
            )

            tags: List[str] = []

            def append_tag(value: Any) -> None:
                if isinstance(value, dict):
                    for nested in value.values():
                        append_tag(nested)
                    return
                if isinstance(value, (list, tuple, set)):
                    for nested in value:
                        append_tag(nested)
                    return

                text = str(value or "").strip()
                if not text:
                    return
                if text[:1] in ("[", "{") and text[-1:] in ("]", "}"):
                    try:
                        parsed = ast.literal_eval(text)
                    except (SyntaxError, ValueError):
                        parsed = None
                    if isinstance(parsed, (dict, list, tuple, set)):
                        append_tag(parsed)
                        return
                if text not in tags:
                    tags.append(text)

            for value in values:
                append_tag(value)
            return tags

        for item in (results or [])[:100]:
            resource_type = str(
                item.get("resource_type") or item.get("pan_type") or "unknown"
            ).strip().lower()
            try:
                unlock_points = max(0, int(item.get("unlock_points") or 0))
            except (TypeError, ValueError):
                unlock_points = 0
            items.append({
                "title": str(item.get("title") or "未命名资源"),
                "source": str(item.get("source") or source),
                "source_name": source_names.get(
                    str(item.get("source") or source), source_names[source]
                ),
                "resource_type": resource_type,
                "resource_type_name": resource_type_names.get(
                    resource_type, resource_type.upper() or "未知"
                ),
                "size": display_size(item),
                "size_bytes": item.get("size") or 0,
                "tags": display_tags(item),
                "description": str(item.get("description") or "").strip(),
                "source_url": str(
                    item.get("source_url") or item.get("media_page_url") or ""
                ).strip(),
                "unlock_points": unlock_points,
                "need_unlock": bool(item.get("need_unlock")),
                "need_access": bool(item.get("need_access")),
                "is_unlocked": bool(item.get("is_unlocked")),
                "is_free": bool(item.get("is_free")),
            })
            resource_type_counts[resource_type] = (
                    resource_type_counts.get(resource_type, 0) + 1
            )
        return {
            "success": True,
            "message": f"{source_names[source]} 测试完成，找到 {len(results or [])} 个候选",
            "data": {
                "source": source,
                "source_name": source_names[source],
                "media": (
                    f"{getattr(mediainfo, 'title', None) or title}"
                    f"{f' ({getattr(mediainfo, 'year', None)})' if getattr(mediainfo, 'year', None) else ''}"
                    f"{f' S{season:02d}' if season else ''}"
                ),
                "count": len(results or []),
                "items": items,
                "resource_types": [
                    {
                        "value": resource_type,
                        "title": resource_type_names.get(
                            resource_type,
                            resource_type.upper() or "未知",
                        ),
                        "count": count,
                    }
                    for resource_type, count in sorted(
                        resource_type_counts.items(),
                        key=lambda pair: (
                            {
                                "115": 0,
                                "123": 1,
                                "quark": 2,
                                "guangya": 3,
                                "ed2k": 4,
                                "magnet": 5,
                            }.get(pair[0], 99),
                            pair[0],
                        ),
                    )
                ],
            },
        }

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
            _UI_OPTIONS_CACHE.clear()
            with _ACCOUNT_INFO_LOCK:
                _ACCOUNT_INFO_CACHE.clear()
                _ACCOUNT_REFRESH_GUARD.clear()
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

    def api_vue_start_sync(
            self,
            payload: Optional[Dict[str, Any]] = None,
            wait: bool = False,
    ) -> dict:
        if self._sync_running or sync_lock.locked():
            return {"success": False, "message": "已有订阅任务正在运行"}
        payload = payload or {}
        raw_subscribe_ids = payload.get("subscribe_ids") or []
        raw_targets = payload.get("history_targets") or []
        try:
            selected_count = max(0, int(payload.get("selected_count") or 0))
        except (TypeError, ValueError):
            selected_count = 0
        selection_requested = bool(
            selected_count or raw_subscribe_ids or raw_targets
        )
        if not isinstance(raw_subscribe_ids, list) or not isinstance(raw_targets, list):
            return {"success": False, "message": "立即搜索范围参数无效"}
        if len(raw_subscribe_ids) > 200 or len(raw_targets) > 200:
            return {"success": False, "message": "单次最多选择 200 个历史媒体"}

        subscribe_ids = set()
        if selection_requested:
            with SessionFactory() as db:
                subscribes = SubscribeOper(db=db).list() or []
            supported_types = {MediaType.TV.value, MediaType.MOVIE.value}
            subscriptions_by_id = {
                int(subscribe.id): subscribe
                for subscribe in subscribes
                if int(getattr(subscribe, "id", 0) or 0) > 0
                   and getattr(subscribe, "type", None) in supported_types
            }
            for value in raw_subscribe_ids:
                try:
                    subscribe_id = int(value or 0)
                except (TypeError, ValueError):
                    continue
                if subscribe_id in subscriptions_by_id:
                    subscribe_ids.add(subscribe_id)

            media_type_values = {
                "tv": MediaType.TV.value,
                "电视剧": MediaType.TV.value,
                "movie": MediaType.MOVIE.value,
                "电影": MediaType.MOVIE.value,
            }
            for target in raw_targets:
                if not isinstance(target, dict):
                    continue
                try:
                    tmdb_id = int(target.get("tmdb_id") or 0)
                except (TypeError, ValueError):
                    tmdb_id = 0
                media_type = media_type_values.get(
                    str(target.get("media_type") or "").strip().lower(), ""
                )
                title = " ".join(
                    str(target.get("title") or "").strip().casefold().split()
                )
                year = str(target.get("year") or "").strip()
                seasons = set()
                for value in target.get("seasons") or []:
                    try:
                        season = int(value or 0)
                    except (TypeError, ValueError):
                        continue
                    if season > 0:
                        seasons.add(season)

                for subscribe_id, subscribe in subscriptions_by_id.items():
                    if media_type and getattr(subscribe, "type", None) != media_type:
                        continue
                    try:
                        subscribe_tmdb_id = int(
                            getattr(subscribe, "tmdbid", 0) or 0
                        )
                    except (TypeError, ValueError):
                        subscribe_tmdb_id = 0
                    if tmdb_id > 0:
                        if subscribe_tmdb_id != tmdb_id:
                            continue
                    else:
                        subscribe_title = " ".join(
                            str(getattr(subscribe, "name", "") or "")
                            .strip().casefold().split()
                        )
                        subscribe_year = str(
                            getattr(subscribe, "year", "") or ""
                        ).strip()
                        if not title or subscribe_title != title:
                            continue
                        if year and subscribe_year and subscribe_year != year:
                            continue
                    if media_type == MediaType.TV.value and seasons:
                        try:
                            subscribe_season = int(
                                getattr(subscribe, "season", 1) or 1
                            )
                        except (TypeError, ValueError):
                            subscribe_season = 1
                        if subscribe_season not in seasons:
                            continue
                    subscribe_ids.add(subscribe_id)

            if not subscribe_ids:
                return {
                    "success": False,
                    "message": "所选历史记录未匹配到有效订阅，未启动全部搜索",
                }

        selected_ids = sorted(subscribe_ids) if selection_requested else None
        sync_kwargs = {"subscribe_ids": selected_ids}
        if wait:
            result: Dict[str, Any] = {}
            self.sync_subscribes(**sync_kwargs, result=result)
            data = dict(result.get("data") or {})
            data.update({
                "scope": "selected" if selection_requested else "all",
                "subscribe_count": len(selected_ids or []),
            })
            result["data"] = data
            return result
        Thread(
            target=self.sync_subscribes,
            kwargs=sync_kwargs,
            daemon=True,
            name="p115-subscribe-sync",
        ).start()
        if selection_requested:
            message = f"已按所选历史记录启动 {len(selected_ids)} 个订阅的搜索"
            scope = "selected"
        else:
            message = "全部订阅搜索任务已启动"
            scope = "all"
        return {
            "success": True,
            "message": message,
            "data": {"scope": scope, "subscribe_count": len(selected_ids or [])},
        }

    def api_vue_start_manual_sync(
            self, payload: Dict[str, Any], wait: bool = False
    ) -> dict:
        """校验指定订阅和资源链接后进入现有转存流程。"""
        if self._sync_running or sync_lock.locked():
            return {"success": False, "message": "已有订阅任务正在运行"}
        try:
            subscribe_id = int((payload or {}).get("subscribe_id") or 0)
        except (TypeError, ValueError):
            subscribe_id = 0
        media_target = None
        if subscribe_id <= 0:
            raw_media = (payload or {}).get("media") or {}
            try:
                tmdb_id = int(raw_media.get("tmdb_id") or 0)
                media_type = str(raw_media.get("media_type") or "").strip().lower()
                title = str(raw_media.get("title") or "").strip()
                season = max(1, int(raw_media.get("season") or 1))
                episode_start = max(1, int(raw_media.get("episode_start") or 1))
                episode_end = max(
                    episode_start, int(raw_media.get("episode_end") or episode_start)
                )
            except (TypeError, ValueError):
                return {"success": False, "message": "TMDB 媒体范围格式错误"}
            if tmdb_id <= 0 or media_type not in {"movie", "tv"} or not title:
                return {"success": False, "message": "请选择订阅或有效的 TMDB 媒体"}
            media_target = {
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "title": title,
                "year": raw_media.get("year"),
                "season": season if media_type == "tv" else None,
                "episode_start": episode_start if media_type == "tv" else None,
                "episode_end": episode_end if media_type == "tv" else None,
            }

        raw_links = (payload or {}).get("resource_links") or []
        if isinstance(raw_links, str):
            raw_links = raw_links.splitlines()
        if not isinstance(raw_links, list):
            return {"success": False, "message": "资源链接格式错误"}

        links = []
        for value in raw_links:
            link = str(value or "").strip()
            if link and link not in links:
                links.append(link)
        if not links:
            return {"success": False, "message": "请至少填写一个资源链接"}
        if len(links) > 50:
            return {"success": False, "message": "单次最多处理 50 个资源链接"}

        if subscribe_id > 0:
            with SessionFactory() as db:
                subscribe = SubscribeOper(db=db).get(subscribe_id)
            if not subscribe:
                return {"success": False, "message": "指定订阅不存在"}
            if subscribe.type not in {MediaType.TV.value, MediaType.MOVIE.value}:
                return {"success": False, "message": "仅支持电影或电视剧订阅"}

        share_transfer = None
        offline_download = None
        if self._cloud_drive:
            if self._cloud_drive.supports(CloudDriveCapability.SHARE_TRANSFER):
                share_transfer = self._cloud_drive.require(
                    CloudDriveCapability.SHARE_TRANSFER
                )
            if self._cloud_drive.supports(CloudDriveCapability.OFFLINE_DOWNLOAD):
                offline_download = self._cloud_drive.require(
                    CloudDriveCapability.OFFLINE_DOWNLOAD
                )
        if not share_transfer:
            return {"success": False, "message": "当前网盘不支持分享转存"}

        magnet_links = [
            link for link in links
            if offline_download and offline_download.is_magnet_url(link)
        ]
        magnet_info_by_url = {}
        if magnet_links:
            with ThreadPoolExecutor(
                    max_workers=min(3, len(magnet_links)),
                    thread_name_prefix="p115-magnet-metadata",
            ) as executor:
                results = executor.map(
                    lambda value: offline_download.parse_magnet_link(
                        value, fetch_metadata=True
                    ),
                    magnet_links,
                )
                magnet_info_by_url = dict(zip(magnet_links, results))

        resources = []
        invalid_links = []
        for index, link in enumerate(links, start=1):
            if offline_download and offline_download.is_ed2k_url(link):
                resource_type = "ed2k"
                valid = bool(offline_download.parse_ed2k_link(link))
            elif offline_download and offline_download.is_magnet_url(link):
                resource_type = "magnet"
                magnet_info = magnet_info_by_url.get(link)
                valid = bool(
                    magnet_info
                    and (magnet_info.get("metadata") or {}).get("metadata_available")
                )
            else:
                resource_type = self._cloud_drive.key
                share_info = share_transfer.extract_share_info(link)
                valid = bool(
                    share_info.get("share_code") and share_info.get("receive_code")
                )
            if not valid:
                invalid_links.append(index)
                continue
            resources.append({
                "url": link,
                "title": f"手动添加 {index}",
                "resource_type": resource_type,
                "source": "manual",
                "source_url": link,
                "unlock_points": 0,
                **(
                    {"magnet_metadata": magnet_info["metadata"]}
                    if resource_type == "magnet" else {}
                ),
            })
        if invalid_links:
            indexes = "、".join(str(index) for index in invalid_links)
            return {
                "success": False,
                "message": (
                    f"第 {indexes} 行资源无效；Magnet 必须能解析出名称或完整文件元数据"
                ),
            }

        order = {value: index for index, value in enumerate(self._resource_type_order)}
        resources.sort(key=lambda item: order.get(item["resource_type"], len(order)))
        sync_kwargs = {
            "subscribe_id": subscribe_id or None,
            "manual_resources": resources,
            "manual_target": media_target,
        }
        if wait:
            result: Dict[str, Any] = {}
            self.sync_subscribes(**sync_kwargs, result=result)
            data = dict(result.get("data") or {})
            data["resource_count"] = len(resources)
            result["data"] = data
            return result
        Thread(
            target=self.sync_subscribes,
            kwargs=sync_kwargs,
            daemon=True,
            name="p115-manual-sync",
        ).start()
        return {
            "success": True,
            "message": (
                f"手动添加任务已提交，共 {len(resources)} 条资源，开始自动处理"
                if subscribe_id
                else f"无订阅媒体任务已提交，共 {len(resources)} 条资源，开始自动处理"
            ),
        }

    def start_selected_resources(
            self,
            subscribe_id: int,
            resources: list[Dict[str, Any]],
    ) -> dict:
        """将智能体会话缓存中的原始候选直接送入现有同步链。"""
        if self._sync_running or sync_lock.locked():
            return {"success": False, "message": "已有订阅任务正在运行"}
        try:
            subscribe_id = int(subscribe_id or 0)
        except (TypeError, ValueError):
            subscribe_id = 0
        if subscribe_id <= 0:
            return {"success": False, "message": "请选择订阅"}
        with SessionFactory() as db:
            subscribe = SubscribeOper(db=db).get(subscribe_id)
        if not subscribe:
            return {"success": False, "message": "指定订阅不存在"}
        if subscribe.type not in {MediaType.TV.value, MediaType.MOVIE.value}:
            return {"success": False, "message": "仅支持电影或电视剧订阅"}

        selected = []
        for resource in list(resources or [])[:20]:
            item = dict(resource or {})
            resource_type = str(
                item.get("resource_type") or item.get("pan_type") or ""
            ).strip().lower()
            has_direct_url = bool(str(item.get("url") or "").strip())
            can_unlock = bool(
                item.get("need_unlock")
                and item.get("slug")
            )
            if not has_direct_url and not can_unlock:
                continue
            item["resource_type"] = resource_type
            selected.append(item)
        if not selected:
            return {"success": False, "message": "没有可处理的候选资源"}

        Thread(
            target=self.sync_subscribes,
            kwargs={
                "subscribe_id": subscribe_id,
                "manual_resources": selected,
            },
            daemon=True,
            name="cloudsubscribe-agent-resource",
        ).start()
        return {
            "success": True,
            "message": f"已提交 {len(selected)} 个候选资源，开始按现有规则处理",
            "data": {"submitted": len(selected)},
        }

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

    def api_vue_media_server_content(
            self,
            server: str = "",
            keyword: str = "",
            tmdb_id: Optional[int] = None,
            media_type: str = "",
    ) -> dict:
        if not self._sync_handler:
            return {"success": False, "message": "同步处理器未初始化"}
        try:
            data = self._sync_handler.list_media_server_content(
                server=server,
                keyword=keyword,
                tmdb_id=tmdb_id,
                media_type=media_type,
            )
            return {
                "success": True,
                "message": f"已读取 {len(data.get('items') or [])} 个媒体库内容",
                "data": data,
            }
        except Exception as error:
            logger.warning(f"读取媒体服务器内容失败：{error}")
            return {"success": False, "message": str(error)}

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
