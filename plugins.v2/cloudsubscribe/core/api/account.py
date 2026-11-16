"""网盘与搜索源账户 API。"""

import asyncio
import copy
import inspect
import secrets
import time
from threading import RLock
from typing import Any, Dict, Tuple
from urllib.parse import parse_qs, urlparse

from app.log import logger
from fastapi import Request

from .page import clear_ui_options_cache
from .. import CloudDriveCapability, OwnerDelegator, SearchCapability
from ...utils.cache import create_platform_ttl_cache

_ACCOUNT_INFO_CACHE = create_platform_ttl_cache(
    "account:info", maxsize=16, ttl=5 * 60
)
_ACCOUNT_REFRESH_GUARD = create_platform_ttl_cache(
    "account:refresh_guard", maxsize=16, ttl=30
)
_ACCOUNT_INFO_LOCK = RLock()
_HDHIVE_OAUTH_PENDING = create_platform_ttl_cache(
    "hdhive:oauth_pending", maxsize=16, ttl=10 * 60
)
_HDHIVE_OAUTH_LOCK = RLock()


class AccountApi(OwnerDelegator):

    def _search_runtime_value(self, name: str, default: Any = None) -> Any:
        return getattr(self, name, default) or getattr(getattr(self, "_owner", None), name, default)

    def _search_account_card(
            self, source: str, info: Dict[str, Any]
    ) -> Dict[str, Any]:
        """将搜索渠道账户数据转换为通用信息卡片。"""
        badge = str(info.get("level") or info.get("role") or "").strip()
        if badge.lower() == "vip" or info.get("is_vip"):
            badge = "VIP"

        details = []
        seen_labels = set()

        def add_detail(label: str, value: Any) -> None:
            if value is None or value == "" or label in seen_labels:
                return
            val_str = str(value).strip()
            if "T" in val_str and ("Z" in val_str or "-" in val_str):
                val_str = val_str.replace("T", " ")
                if val_str.endswith("Z"):
                    val_str = val_str[:-1].strip()
                if "." in val_str:
                    val_str = val_str.split(".")[0].strip()
            if val_str:
                details.append({"label": label, "value": val_str})
                seen_labels.add(label)

        channel_name = source.upper()
        search_handler = getattr(self, "_search_handler", None)
        if search_handler and getattr(search_handler, "_search_registry", None):
            prov = search_handler._search_registry.get(source)
            if prov and prov.name:
                channel_name = prov.name

        name = str(info.get("name") or "").strip()
        email = str(info.get("email") or "").strip().lower()
        if not name or "@" in name or (email and name.lower() == email):
            name = f"{channel_name}用户"

        # 核心基础状态字段
        if "is_vip" in info:
            add_detail("会员状态", "VIP会员" if info.get("is_vip") else "普通用户")
        if info.get("level_name") or info.get("level"):
            add_detail("用户等级", info.get("level_name") or info.get("level"))
        if info.get("retention_deadline"):
            add_detail("保号截止", info.get("retention_deadline"))
        if info.get("registered_at"):
            add_detail("注册日期", info.get("registered_at"))
        if info.get("expires_at"):
            add_detail("会员到期", info.get("expires_at"))

        days = info.get("signin_days") if info.get("signin_days") is not None else info.get("checkin_days")
        if days is None:
            days = info.get("consecutive_signin")
        if days is not None:
            days_str = f"{days} 天" if not str(days).endswith("天") else str(days)
            add_detail("签到天数", days_str)

        # 字典自定义属性（自动映射常见英文键名，杜绝重复与英文泄露）
        label_map = {
            "level": "用户等级",
            "points": "可用积分",
            "signin_days": "签到天数",
            "checkin_days": "签到天数",
            "registered_at": "注册日期",
            "created_at": "注册日期",
            "expires_at": "会员到期",
            "vip_expires_at": "会员到期",
            "quota": "今日配额",
            "remaining_quota": "剩余配额",
            "quota_usage": "配额使用",
            "account_count": "账号数量",
            "vip_level": "VIP等级",
            "share_count": "分享数量",
            "upload_count": "上传资源",
            "favorite_count": "收藏资源",
            "unlock_count": "已解锁",
            "invite_count": "邀请用户",
        }
        extra_details = info.get("details")
        if isinstance(extra_details, dict):
            for k, v in extra_details.items():
                label = label_map.get(str(k).strip(), str(k).strip())
                add_detail(label, v)
        elif isinstance(extra_details, list):
            for item in extra_details:
                if isinstance(item, dict) and item.get("label") and item.get("value"):
                    label = label_map.get(str(item["label"]).strip(), str(item["label"]).strip())
                    add_detail(label, item["value"])

        points_avail = info.get("quota_text") or info.get("points") or 0
        points_label = str(
            info.get("points_label")
            or ("今日解锁配额" if info.get("quota_text") else "可用积分")
        )

        return {
            "connected": True,
            "user": {
                "name": name,
                "avatar": str(info.get("avatar") or ""),
                "membership_supported": False,
                "badge": badge,
            },
            "points": {
                "label": points_label,
                "available": points_avail,
            },
            "details": details,
        }

    def _load_search_account(self, source: str, force: bool = False) -> Dict[str, Any]:
        """读取单个搜索渠道的账户信息。"""
        source = str(source or "").strip().lower()
        registry = getattr(getattr(self, "_search_handler", None), "_search_registry", None)
        if not registry or source not in registry:
            return {
                "connected": False,
                "error": "请先填写对应渠道账号和密码并保存配置",
            }
        provider = registry.get(source)
        if not provider.supports(SearchCapability.ACCOUNT):
            return {
                "connected": False,
                "error": f"搜索渠道 [{source}] 不支持账户信息",
            }
        try:
            client = provider.require(SearchCapability.ACCOUNT)
            if not client:
                return {
                    "connected": False,
                    "error": "渠道客户端未初始化，请检查配置",
                }
            if hasattr(client, "is_ready") and not client.is_ready:
                return {
                    "connected": False,
                    "error": "渠道授权尚未完成，请先保存配置并授权",
                }
            getter = client.get_account_info
            parameters = inspect.signature(getter).parameters
            if "force" in parameters:
                info = getter(force=force)
            elif "refresh" in parameters:
                info = getter(refresh=force)
            else:
                info = getter()
            return self._search_account_card(source, info)
        except Exception as error:
            logger.debug(f"读取{source}搜索账户信息失败：{error}")
            return {
                "connected": False,
                "error": f"账户信息读取失败：{error}",
            }
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
        """读取单卡片信息，使用独立数据库快照并实施刷新冷却。"""
        normalized_key = str(account_key or "").strip().lower()
        if ":" not in normalized_key:
            raise ValueError("账户卡片标识无效")
        category, source = normalized_key.split(":", 1)
        if category not in {"drive", "search"} or not source:
            raise ValueError("账户卡片标识无效")

        with _ACCOUNT_INFO_LOCK:
            stored_account = self._get_data_store().load_account(normalized_key)
            cached_account = (
                    _ACCOUNT_INFO_CACHE.get(normalized_key) or stored_account
            )
            if cached_account:
                _ACCOUNT_INFO_CACHE.set(normalized_key, cached_account)
            # 快照只是上次失败（如热重载打断会话）时，不占用刷新冷却，允许立即重试
            failed_snapshot = bool(
                isinstance(cached_account, dict)
                and not cached_account.get("connected")
            )
            if (
                    refresh
                    and not failed_snapshot
                    and _ACCOUNT_REFRESH_GUARD.get(normalized_key)
            ):
                return cached_account or {
                    "connected": False,
                    "error": "账户信息正在冷却，请稍后再刷新",
                }, True
            if not refresh and cached_account:
                return cached_account, False
            _ACCOUNT_REFRESH_GUARD.set(normalized_key, True)

        account = (
            self._load_drive_account(source, force=refresh)
            if category == "drive"
            else self._load_search_account(source, force=refresh)
        )
        account["refreshed_at"] = int(time.time())
        with _ACCOUNT_INFO_LOCK:
            _ACCOUNT_INFO_CACHE.set(normalized_key, account)
            self._get_data_store().save_account(normalized_key, account)
        return account, False

    def _cached_account_info(
            self, account_key: str, fallback: Dict[str, Any]
    ) -> Dict[str, Any]:
        """配置页只读取内存或独立数据库快照，不访问第三方接口。"""
        with _ACCOUNT_INFO_LOCK:
            cached = _ACCOUNT_INFO_CACHE.get(account_key)
            if cached:
                return cached
            account = self._get_data_store().load_account(account_key)
            if account:
                _ACCOUNT_INFO_CACHE.set(account_key, account)
                return account
            return fallback

    def update_search_account_points(
            self,
            source: str,
            points: Any,
            signin_days: Any = None,
    ) -> bool:
        """用签到结果更新搜索渠道账户快照，避免重复请求第三方接口。"""
        account_key = f"search:{str(source or '').strip().lower()}"
        if points is None:
            return False

        with _ACCOUNT_INFO_LOCK:
            account = (
                    _ACCOUNT_INFO_CACHE.get(account_key)
                    or self._get_data_store().load_account(account_key)
            )
            if not isinstance(account, dict) or not account.get("connected"):
                return False
            account = copy.deepcopy(account)
            point_info = dict(account.get("points") or {})
            point_info["available"] = points
            account["points"] = point_info
            if signin_days is not None:
                days_text = f"{signin_days} 天" if not str(signin_days).endswith("天") else str(signin_days)
                details = list(account.get("details") or [])
                updated = False
                for item in details:
                    if isinstance(item, dict) and item.get("label") in {"签到天数", "累计签到", "连续签到"}:
                        item["value"] = days_text
                        updated = True
                if not updated:
                    details.append({"label": "签到天数", "value": days_text})
                account["details"] = details
            account["refreshed_at"] = int(time.time())
            _ACCOUNT_INFO_CACHE.set(account_key, account)
            _ACCOUNT_REFRESH_GUARD.set(account_key, True)
            self._get_data_store().save_account(account_key, account)
        return True

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
                clear_ui_options_cache()
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

    @staticmethod
    def _hdhive_oauth_values(payload: Dict[str, Any]) -> Tuple[str, str, str, str]:
        """校验授权发起和换 Token 共用的 OpenAPI 参数。"""
        client_id = str(payload.get("client_id") or "").strip()
        redirect_uri = str(payload.get("redirect_uri") or "").strip()
        response_mode = str(payload.get("response_mode") or "redirect").strip().lower()
        requested_scopes = [
            value for value in str(
                payload.get("scope") or "query unlock write"
            ).split()
            if value in {"query", "unlock", "write"}
        ]
        scopes = list(dict.fromkeys(requested_scopes))
        if "query" not in scopes:
            scopes.insert(0, "query")
        if "write" not in scopes:
            scopes.append("write")
        if not client_id:
            raise ValueError("请填写 HDHive OpenAPI Client ID")
        parsed = urlparse(redirect_uri)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.fragment:
            raise ValueError("OAuth Redirect URI 必须是无 fragment 的完整 HTTP/HTTPS 地址")
        if response_mode not in {"redirect", "postmessage"}:
            raise ValueError("当前插件仅支持 redirect 或 postmessage 授权回调")
        return client_id, redirect_uri, " ".join(scopes), response_mode

    def api_vue_hdhive_oauth_start(self, payload: Dict[str, Any]) -> dict:
        """生成带服务端 state 记录的 HDHive OpenAPI 授权链接。"""
        from ...search.hdhive import HDHiveOpenAPIClient

        try:
            payload = dict(payload or {})
            client_id, redirect_uri, scope, response_mode = self._hdhive_oauth_values(payload)
            state = secrets.token_urlsafe(32)
            client = HDHiveOpenAPIClient(
                app_secret="",
                client_id=client_id,
                proxy=self._search_proxy,
                request_interval=self._search_runtime_value(
                    "_hdhive_request_interval", 5.0
                ),
            )
            try:
                authorize_url = client.build_authorize_url(
                    redirect_uri=redirect_uri,
                    scope=scope,
                    state=state,
                    response_mode=response_mode,
                )
            finally:
                client.close()
            with _HDHIVE_OAUTH_LOCK:
                _HDHIVE_OAUTH_PENDING.set(state, {
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "scope": scope,
                    "response_mode": response_mode,
                })
            return {
                "success": True,
                "message": "HDHive 授权页已准备，请在 10 分钟内完成授权",
                "data": {
                    "authorize_url": authorize_url,
                    "state": state,
                    "response_mode": response_mode,
                    "expires_in": 600,
                },
            }
        except Exception as error:
            return {"success": False, "message": str(error)}

    def api_vue_hdhive_oauth_exchange(self, payload: Dict[str, Any]) -> dict:
        """校验 OAuth state，使用授权码换 Token 并返回当前用户摘要。"""
        from ...search.hdhive import HDHiveOpenAPIClient, HDHiveOpenAPIError

        client = None
        try:
            payload = dict(payload or {})
            client_id, redirect_uri, _scope, response_mode = self._hdhive_oauth_values(payload)
            app_secret = str(payload.get("app_secret") or "").strip()
            if not app_secret:
                raise ValueError("请填写 HDHive OpenAPI 应用 Secret")

            code = str(payload.get("code") or "").strip()
            state = str(payload.get("state") or "").strip()
            callback = str(payload.get("callback") or "").strip()
            if callback:
                parsed_callback = urlparse(callback)
                query = parsed_callback.query if parsed_callback.scheme else callback.lstrip("?")
                values = parse_qs(query, keep_blank_values=True)
                code = str((values.get("code") or [code])[0] or "").strip()
                state = str((values.get("state") or [state])[0] or "").strip()
            if not code or not state:
                raise ValueError("请粘贴包含 code 和 state 的完整回调 URL")

            with _HDHIVE_OAUTH_LOCK:
                pending = _HDHIVE_OAUTH_PENDING.get(state)
            if not pending:
                raise ValueError("授权 state 已失效，请重新打开授权页")
            if (
                    pending.get("client_id") != client_id
                    or pending.get("redirect_uri") != redirect_uri
                    or pending.get("response_mode") != response_mode
            ):
                raise ValueError("授权回调与发起授权时的应用配置不一致")

            client = HDHiveOpenAPIClient(
                app_secret=app_secret,
                client_id=client_id,
                proxy=self._search_proxy,
                request_interval=self._search_runtime_value(
                    "_hdhive_request_interval", 5.0
                ),
            )
            token_data = client.exchange_code(code, redirect_uri)
            warning = ""
            user = {}
            try:
                user = client.get_me().get("data") or {}
            except HDHiveOpenAPIError as error:
                warning = f"Token 已获取，但读取授权用户失败：[{error.code}] {error.message}"
            with _HDHIVE_OAUTH_LOCK:
                _HDHIVE_OAUTH_PENDING.delete(state)
            return {
                "success": True,
                "message": "HDHive OpenAPI 用户授权成功",
                "data": {
                    "access_token": client.access_token,
                    "refresh_token": client.refresh_token,
                    "token_expires_at": client.token_expires_at,
                    "expires_in": token_data.get("expires_in") or 0,
                    "refresh_expires_in": token_data.get("refresh_expires_in") or 0,
                    "scope": token_data.get("scope") or pending.get("scope") or "",
                    "scopes": token_data.get("scopes") or [],
                    "user": user,
                    "warning": warning,
                },
            }
        except HDHiveOpenAPIError as error:
            return {
                "success": False,
                "message": f"[{error.code}] {error.message} {error.description}".strip(),
            }
        except Exception as error:
            return {"success": False, "message": str(error)}
        finally:
            if client:
                client.close()


def clear_account_cache(account_key: str = "") -> None:
    normalized_key = str(account_key or "").strip().lower()
    with _ACCOUNT_INFO_LOCK:
        if normalized_key:
            _ACCOUNT_INFO_CACHE.delete(normalized_key)
            _ACCOUNT_REFRESH_GUARD.delete(normalized_key)
        else:
            _ACCOUNT_INFO_CACHE.clear()
            _ACCOUNT_REFRESH_GUARD.clear()
