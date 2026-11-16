"""蜗牛搜索渠道自描述规范与表单声明。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import WoniuClient, WoniuError
from .provider import create_woniu_provider
from .resource import WoniuResourceService
from .service import WoniuSearchService
from ...core.definitions import (
    CheckinDefinition,
    FieldSpec,
    GroupSpec,
    SearchSourceDefinition,
    build_checkin_definition,
)


class WoniuSourceDefinition(SearchSourceDefinition):
    """蜗牛搜索渠道规范。"""

    id = "woniu"
    name = "蜗牛"
    icon = "mdi-movie-search-outline"
    color = "light-green-darken-2"
    order = 45

    @classmethod
    def create_client(cls, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        ctx = context or {}
        username = str(cls.config_value(config, "woniu_username", "") or "").strip()
        password = str(cls.config_value(config, "woniu_password", "") or "")
        if not username or not password:
            return None
        return WoniuClient(
            base_url=str(cls.config_value(config, "woniu_base_url", WoniuClient.BASE_URL) or WoniuClient.BASE_URL),
            username=username,
            password=password,
            proxy=ctx.get("proxy"),
            request_timeout=int(cls.config_value(config, "woniu_timeout", 60) or 60),
            request_interval=float(cls.config_value(config, "woniu_request_interval", 1.0) or 1.0),
            **cls.storage_hooks(ctx),
        )

    @classmethod
    def get_checkin_definition(cls) -> Optional[CheckinDefinition]:
        """自动注册蜗牛签到契约。"""
        return build_checkin_definition(
            cls,
            icon="mdi-calendar-check-outline",
            credential_attrs=("_woniu_username", "_woniu_password"),
            credential_keys=("woniu_username", "woniu_password"),
            error_types=(WoniuError,),
            group_title="蜗牛签到",
            points_label="积分",
            track_days=True,
            enable_cols=12,
        )

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="woniu",
                title="蜗牛账号",
                icon="mdi-account-key-outline",
                hint="使用蜗牛 (www.wn4k.com) 账号登录以解锁高清与原盘分享直链",
                fields=[
                    FieldSpec(
                        key="woniu_account_info",
                        type="account",
                        account_key="search:woniu",
                        compact=True,
                        cols=12,
                    ),
                    FieldSpec(
                        key="woniu_base_url",
                        label="服务地址",
                        placeholder="https://www.wn4k.com",
                        default="https://www.wn4k.com",
                        cols=12,
                    ),
                    FieldSpec(
                        key="woniu_username",
                        label="网页登录账号",
                        cols=6,
                    ),
                    FieldSpec(
                        key="woniu_password",
                        label="网页登录密码",
                        type="password",
                        cols=6,
                    ),
                    FieldSpec(
                        key="test_woniu",
                        label="测试搜索",
                        type="test-source",
                        source="woniu",
                        cols=12,
                    ),
                ],
            ),
            GroupSpec(
                tab="woniu",
                title="搜索与风控",
                icon="mdi-shield-search",
                hint="影片搜索和详情直链抓取共用限速保护，缓存搜索结果避免频繁调用。",
                fields=[
                    FieldSpec(
                        key="woniu_result_limit",
                        label="候选上限",
                        hint="单次搜索最多保留的候选资源数量",
                        type="number",
                        default=10,
                        min=1,
                        max=20,
                        cols=4,
                    ),
                    FieldSpec(
                        key="woniu_request_interval",
                        label="请求访问间隔",
                        hint="详情页等请求的基础间隔秒数",
                        type="number",
                        default=1.0,
                        min=0.5,
                        max=10,
                        step=0.5,
                        suffix="秒",
                        cols=4,
                    ),
                    FieldSpec(
                        key="woniu_timeout",
                        label="搜索超时",
                        hint="单次搜索超时秒数，默认 60 秒",
                        type="number",
                        default=60,
                        min=5,
                        max=120,
                        suffix="秒",
                        cols=4,
                    ),
                ],
            ),
        ]

    @classmethod
    def create_provider(
            cls, service: Any, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        ctx = context or {}
        resource_types = ctx.get("resource_types", ())
        if not client or not getattr(client, "is_configured", False) or not resource_types:
            return None
        resources = WoniuResourceService(client)
        limit = int(cls.config_value(config, "woniu_result_limit", 10) or 10)
        svc = WoniuSearchService(client, resources, resource_types, limit)
        return create_woniu_provider(
            svc,
            client,
            resource_types,
            {"result_limit": limit, "resource_types": list(resource_types)},
        )
