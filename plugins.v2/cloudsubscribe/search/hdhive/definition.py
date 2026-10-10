"""HDHive 搜索渠道自描述规范与表单声明。"""

from __future__ import annotations

import re
import threading
from typing import Any, Dict, List, Optional

from .open.client import HDHiveOpenAPIClient, HDHiveOpenAPIError
from .provider import create_hdhive_provider
from .web.client import HDHiveClient, HDHiveWebError
from ...core.definitions import (
    CheckinDefinition,
    FieldSpec,
    GroupSpec,
    SearchSourceDefinition,
    build_checkin_definition,
)


def _is_openapi_mode(owner: Any) -> bool:
    """当前是否使用 HDHive OpenAPI 查询模式。"""
    return str(
        getattr(owner, "_hdhive_query_mode", "web") or "web"
    ).strip().lower() == "api"


def _checkin_ready(owner: Any) -> bool:
    """OpenAPI 模式要求已授权的应用客户端，WebAPI 模式校验账号密码。"""
    if _is_openapi_mode(owner):
        client = getattr(owner, "_hdhive_client", None)
        return bool(client and client.is_ready)
    return bool(
        getattr(owner, "_hdhive_username", None)
        and getattr(owner, "_hdhive_password", None)
    )


def _checkin_hint(owner: Any) -> str:
    """按查询模式给出对应的未配置提示。"""
    if _is_openapi_mode(owner):
        return "请先配置并保存 HDHive OpenAPI 应用 Secret 和用户授权"
    return "请先配置并保存 HDHive 账号和密码"


class HDHiveSourceDefinition(SearchSourceDefinition):
    """HDHive 搜索渠道规范。"""

    id = "hdhive"
    name = "HDHive"
    icon = "mdi-hexagon-multiple-outline"
    color = "amber-darken-1"
    order = 10
    _TEST_CLIENT_LIMIT = 4
    _test_clients_lock = threading.RLock()
    _test_clients: List[HDHiveClient] = []

    @classmethod
    def configure_owner(cls, owner: Any, config: Dict[str, Any]) -> None:
        value = lambda key, default=None: cls.config_value(owner.__dict__, key, default)
        owner._hdhive_client = value("hdhive_client")
        owner._hdhive_username = str(value("hdhive_username", "") or "")
        owner._hdhive_password = str(value("hdhive_password", "") or "")
        mode = str(value("hdhive_query_mode", "web") or "web").lower()
        owner._hdhive_query_mode = mode if mode in {"api", "web"} else "web"
        owner._hdhive_auto_unlock = bool(value("hdhive_auto_unlock", False))
        owner._hdhive_max_unlock_points = max(0, int(value("hdhive_max_unlock_points", 50) or 0))
        owner._hdhive_max_points_per_sub = max(0, int(value("hdhive_max_points_per_sub", 20) or 0))
        owner._hdhive_candidate_limit = max(1, min(int(value("hdhive_candidate_limit", 4) or 4), 20))
        owner._hdhive_timeout = max(5, min(int(value("hdhive_timeout", 60) or 60), 120))
        owner._hdhive_request_interval = max(2.0, min(float(value("hdhive_request_interval", 5) or 5), 10.0))
        owner._hdhive_unlocks_per_minute = max(1, min(int(value("hdhive_unlocks_per_minute", 2) or 2), 3))
        owner._hdhive_web_client = value("hdhive_web_client")
        owner._hdhive_web_client_owned = bool(
            owner._hdhive_web_client is None or value("hdhive_web_client_owned", True)
        )
        owner._hdhive_web_resources = None
        owner._hdhive_web_lock = threading.RLock()
        owner._hdhive_unlock_operation_lock = threading.Lock()
        owner._hdhive_torrentclaw_enabled = bool(
            value("hdhive_torrentclaw_enabled", False)
            and "magnet" in owner._resource_type_order_config
        )
        languages = value("hdhive_torrentclaw_subtitle_languages", ["zh"]) or ["zh"]
        if isinstance(languages, str):
            languages = re.split(r"[,，\s]+", languages)
        owner._hdhive_torrentclaw_subtitle_languages = list(dict.fromkeys(
            str(item or "").strip().lower().replace("_", "-")
            for item in languages if str(item or "").strip()
        ))

    @classmethod
    def get_checkin_definition(cls) -> Optional[CheckinDefinition]:
        """自动注册 HDHive 签到契约，WebAPI 与 OpenAPI 共用同一执行器。"""
        return build_checkin_definition(
            cls,
            credential_attrs=("_hdhive_username", "_hdhive_password"),
            credential_keys=("hdhive_username", "hdhive_password"),
            error_types=(HDHiveWebError, HDHiveOpenAPIError),
            modes=("normal", "gambler"),
            ready=_checkin_ready,
            hint=_checkin_hint,
            group_title="HDHive 签到",
            enable_cols=4,
            mode_hint="HDHive 赌狗模式会将签到奖励乘以 -1～3 的随机倍数，最坏扣除 3 积分。",
        )

    @classmethod
    def build_test_context(
            cls, config: Dict[str, Any], context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        ctx = context or {}
        proxy = ctx.get("proxy")
        points = max(0, int(ctx.get("confirmed_unlock_points") or 0))
        common = {
            "hdhive_auto_unlock": False,
            "hdhive_max_unlock_points": points,
            "hdhive_max_points_per_sub": points,
        }
        mode = str(config.get("hdhive_query_mode") or "web").strip().lower()
        if mode == "api":
            return {
                **common,
                "hdhive_query_mode": "api",
                "hdhive_client": HDHiveOpenAPIClient(
                    app_secret=str(config.get("hdhive_api_key") or ""),
                    client_id=str(config.get("hdhive_client_id") or ""),
                    access_token=str(config.get("hdhive_access_token") or ""),
                    refresh_token=str(config.get("hdhive_refresh_token") or ""),
                    token_expires_at=float(config.get("hdhive_token_expires_at") or 0),
                    proxy=proxy,
                    request_interval=float(config.get("hdhive_request_interval", 5) or 5),
                ),
            }
        result: Dict[str, Any] = {**common, "hdhive_query_mode": "web"}
        if ctx.get("deadline") is not None:
            return result
        username = str(config.get("hdhive_username") or "")
        password = str(config.get("hdhive_password") or "")
        request_interval = float(config.get("hdhive_request_interval", 5) or 5)
        with cls._test_clients_lock:
            for client in cls._test_clients:
                if client.matches_config(username, password, proxy, request_interval):
                    result.update({
                        "hdhive_web_client": client,
                        "hdhive_web_client_owned": False,
                    })
                    return result
            client = HDHiveClient(
                username=username,
                password=password,
                proxy=proxy,
                request_interval=request_interval,
            )
            owned = len(cls._test_clients) >= cls._TEST_CLIENT_LIMIT
            if not owned:
                cls._test_clients.append(client)
        result.update({
            "hdhive_web_client": client,
            "hdhive_web_client_owned": owned,
        })
        return result

    @classmethod
    def close_test_resources(cls) -> None:
        with cls._test_clients_lock:
            clients = list(cls._test_clients)
            cls._test_clients.clear()
        for client in clients:
            try:
                client.close()
            except Exception:
                pass

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="hdhive",
                title="HDHive 接入",
                icon="mdi-hexagon-multiple-outline",
                hint="默认使用功能完整的 WebAPI；OpenAPI 适合已申请应用并完成 OAuth 用户授权的场景。",
                fields=[
                    FieldSpec(
                        key="hdhive_account_info",
                        type="account",
                        account_key="search:hdhive",
                        compact=True,
                        cols=12,
                    ),
                    FieldSpec(
                        key="hdhive_base_url",
                        label="服务地址",
                        default="https://re0.me",
                        cols=12,
                        show_condition="config.hdhive_query_mode === 'web'",
                    ),
                    FieldSpec(
                        key="hdhive_query_mode",
                        label="查询模式",
                        type="select",
                        default="web",
                        options=[
                            {"title": "WebAPI", "value": "web"},
                            {"title": "OpenAPI", "value": "api"},
                        ],
                        cols=4,
                    ),
                    FieldSpec(
                        key="hdhive_api_key",
                        label="API Key / 应用 Secret",
                        type="password",
                        cols=4,
                        show_condition="config.hdhive_query_mode === 'api'",
                    ),
                    FieldSpec(
                        key="hdhive_client_id",
                        label="Client ID",
                        cols=4,
                        show_condition="config.hdhive_query_mode === 'api'",
                    ),
                    FieldSpec(
                        key="hdhive_redirect_uri",
                        label="OAuth Redirect URI",
                        hint="必须与 HDHive OpenAPI 应用配置完全一致，且不能包含 fragment。",
                        cols=6,
                        show_condition="config.hdhive_query_mode === 'api'",
                    ),
                    FieldSpec(
                        key="hdhive_response_mode",
                        label="OAuth 回调模式",
                        type="select",
                        default="redirect",
                        options=[
                            {"title": "Redirect（复制回调 URL）", "value": "redirect"},
                            {"title": "PostMessage（弹窗自动回传）", "value": "postmessage"},
                        ],
                        cols=6,
                        show_condition="config.hdhive_query_mode === 'api'",
                    ),
                    FieldSpec(
                        key="hdhive_oauth",
                        type="hdhive-oauth",
                        cols=12,
                        show_condition="config.hdhive_query_mode === 'api'",
                    ),
                    FieldSpec(
                        key="hdhive_access_token",
                        label="Access Token",
                        type="password",
                        cols=6,
                        show_condition="config.hdhive_query_mode === 'api'",
                    ),
                    FieldSpec(
                        key="hdhive_refresh_token",
                        label="Refresh Token",
                        type="password",
                        cols=6,
                        show_condition="config.hdhive_query_mode === 'api'",
                    ),
                    FieldSpec(
                        key="hdhive_username",
                        label="HDHive 用户名",
                        cols=4,
                        show_condition="config.hdhive_query_mode === 'web'",
                    ),
                    FieldSpec(
                        key="hdhive_password",
                        label="HDHive 密码",
                        type="password",
                        cols=4,
                        show_condition="config.hdhive_query_mode === 'web'",
                    ),
                    FieldSpec(
                        key="test_hdhive",
                        label="测试搜索",
                        type="test-source",
                        source="hdhive",
                        cols=12,
                    ),
                ],
            ),
            GroupSpec(
                tab="hdhive",
                title="HDHive 积分解锁",
                icon="mdi-ticket-confirmation-outline",
                fields=[
                    FieldSpec(
                        key="hdhive_auto_unlock",
                        label="允许积分解锁",
                        type="switch",
                        default=False,
                        cols=4,
                    ),
                    FieldSpec(
                        key="hdhive_max_unlock_points",
                        label="单次积分总预算",
                        hint="单次同步最大解锁积分",
                        type="number",
                        default=50,
                        min=0,
                        cols=4,
                        show_condition="config.hdhive_auto_unlock",
                    ),
                    FieldSpec(
                        key="hdhive_max_points_per_sub",
                        label="单订阅解锁预算",
                        hint="单订阅累计解锁预算",
                        type="number",
                        default=20,
                        min=0,
                        cols=4,
                        show_condition="config.hdhive_auto_unlock",
                    ),
                ],
            ),
            GroupSpec(
                tab="hdhive",
                title="搜索与风控",
                icon="mdi-shield-search",
                fields=[
                    FieldSpec(
                        key="hdhive_candidate_limit",
                        label="候选上限",
                        hint="最大保留候选数量",
                        type="number",
                        default=4,
                        min=1,
                        max=20,
                        cols=3,
                    ),
                    FieldSpec(
                        key="hdhive_timeout",
                        label="搜索超时",
                        hint="单次搜索超时秒数，默认 60 秒",
                        type="number",
                        default=60,
                        min=5,
                        max=120,
                        suffix="秒",
                        cols=3,
                    ),
                    FieldSpec(
                        key="hdhive_request_interval",
                        label="请求访问间隔",
                        hint="接口请求基础间隔秒数",
                        type="number",
                        default=5.0,
                        min=2,
                        max=10,
                        step=0.5,
                        suffix="秒",
                        cols=3,
                    ),
                    FieldSpec(
                        key="hdhive_unlocks_per_minute",
                        label="每分钟解锁次数",
                        hint="WebAPI 解锁频次限制，默认 2 次",
                        type="number",
                        default=2,
                        min=1,
                        max=5,
                        step=1,
                        suffix="次/分钟",
                        cols=3,
                        show_condition="config.hdhive_query_mode === 'web'",
                    ),
                    FieldSpec(
                        key="hdhive_torrentclaw_enabled",
                        label="获取 TorrentClaw Magnet",
                        hint="按优先级获取磁力资源",
                        type="switch",
                        default=False,
                        cols=12,
                        show_condition="config.hdhive_query_mode === 'web'",
                    ),
                ],
            ),
        ]

    @classmethod
    def create_client(cls, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        ctx = context or {}
        proxy = ctx.get("proxy")
        mode = str(cls.config_value(config, "hdhive_query_mode", "web") or "web").strip().lower()
        if mode == "api":
            api_key = str(cls.config_value(config, "hdhive_api_key", "") or "").strip()
            client_id = str(cls.config_value(config, "hdhive_client_id", "") or "").strip()
            if not api_key or not client_id:
                return None
            return HDHiveOpenAPIClient(
                app_secret=api_key,
                client_id=client_id,
                access_token=str(cls.config_value(config, "hdhive_access_token", "") or ""),
                refresh_token=str(cls.config_value(config, "hdhive_refresh_token", "") or ""),
                token_expires_at=float(cls.config_value(config, "hdhive_token_expires_at", 0) or 0),
                proxy=proxy,
                request_interval=float(cls.config_value(config, "hdhive_request_interval", 5) or 5),
            )
        username = str(cls.config_value(config, "hdhive_username", "") or "").strip()
        password = str(cls.config_value(config, "hdhive_password", "") or "").strip()
        if not username or not password:
            return None
        owner = ctx.get("storage_owner")
        return HDHiveClient(
            username=username,
            password=password,
            proxy=proxy,
            request_interval=float(cls.config_value(config, "hdhive_request_interval", 5) or 5),
            get_data_func=getattr(owner, "get_data", None),
            save_data_func=getattr(owner, "save_data", None),
        )

    @classmethod
    def create_provider(
            cls, service: Any, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        ctx = context or {}
        hdhive_service = ctx.get("hdhive_service")
        if not hdhive_service and ctx.get("owner"):
            try:
                from .service import HDHiveSearchService
                hdhive_service = HDHiveSearchService(ctx["owner"])
            except Exception:
                hdhive_service = None
        if hdhive_service and getattr(hdhive_service, "available", False):
            return create_hdhive_provider(hdhive_service)
        return None

