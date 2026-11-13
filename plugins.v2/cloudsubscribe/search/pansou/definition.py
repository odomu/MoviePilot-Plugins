"""PanSou 搜索渠道自描述规范与表单声明。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import PanSouClient
from .provider import create_pansou_provider
from .service import PanSouSearchService
from ...core.definitions import FieldSpec, GroupSpec, SearchSourceDefinition


class PanSouSourceDefinition(SearchSourceDefinition):
    """PanSou 搜索渠道规范。"""

    id = "pansou"
    name = "PanSou"
    icon = "mdi-magnify-scan"
    color = "indigo"
    order = 5

    @classmethod
    def create_client(cls, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        ctx = context or {}
        base_url = str(cls.config_value(config, "pansou_url", "") or "").strip()
        if not base_url:
            return None
        owner = ctx.get("storage_owner")
        return PanSouClient(
            base_url=base_url,
            username=str(cls.config_value(config, "pansou_username", "") or ""),
            password=str(cls.config_value(config, "pansou_password", "") or ""),
            auth_enabled=bool(cls.config_value(config, "pansou_auth_enabled", False)),
            proxy=ctx.get("proxy"),
            search_timeout=int(cls.config_value(config, "pansou_timeout", 60) or 60),
            get_data_func=getattr(owner, "get_data", None),
            save_data_func=getattr(owner, "save_data", None),
        )

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="pansou",
                title="连接配置",
                icon="mdi-server-network",
                fields=[
                    FieldSpec(
                        key="pansou_url",
                        label="服务地址",
                        placeholder="https://pansou.cc",
                        default="https://so.252035.xyz/",
                        cols=8,
                    ),
                    FieldSpec(
                        key="pansou_auth_enabled",
                        label="启用身份认证",
                        type="switch",
                        default=False,
                        cols=4,
                    ),
                    FieldSpec(
                        key="pansou_username",
                        label="用户名",
                        default="",
                        cols=4,
                        show_condition="config.pansou_auth_enabled",
                    ),
                    FieldSpec(
                        key="pansou_password",
                        label="密码",
                        type="password",
                        default="",
                        cols=4,
                        show_condition="config.pansou_auth_enabled",
                    ),
                    FieldSpec(
                        key="test_pansou",
                        label="测试搜索",
                        type="test-source",
                        source="pansou",
                        cols=12,
                    ),
                ],
            ),
            GroupSpec(
                tab="pansou",
                title="搜索范围",
                icon="mdi-source-branch",
                fields=[
                    FieldSpec(
                        key="pansou_channels",
                        label="限定频道",
                        type="select",
                        default=[],
                        cols=4,
                        multiple=True,
                        searchable=True,
                        extra={
                            "dynamicOptions": {
                                "scope": "pansou",
                                "key": "channels",
                                "refreshable": True,
                            },
                        },
                    ),
                    FieldSpec(
                        key="pansou_plugins",
                        label="限定插件",
                        type="select",
                        default=[],
                        cols=4,
                        multiple=True,
                        searchable=True,
                        extra={
                            "dynamicOptions": {
                                "scope": "pansou",
                                "key": "plugins",
                                "refreshable": True,
                            },
                        },
                    ),
                ],
            ),
            GroupSpec(
                tab="pansou",
                title="过滤与性能",
                icon="mdi-filter-cog-outline",
                fields=[
                    FieldSpec(
                        key="pansou_refresh",
                        label="强制刷新",
                        hint="开启时绕过服务端缓存；关闭可显著加快搜索",
                        type="switch",
                        default=False,
                        cols=12,
                    ),
                    FieldSpec(
                        key="pansou_filter_include",
                        label="必须包含任一关键词",
                        hint="至少包含一个关键词 (OR)",
                        type="combobox",
                        default=[],
                        cols=6,
                    ),
                    FieldSpec(
                        key="pansou_filter_exclude",
                        label="排除任一关键词",
                        hint="包含任一关键词即排除 (OR)",
                        type="combobox",
                        default=[],
                        cols=6,
                    ),
                    FieldSpec(
                        key="pansou_concurrency",
                        label="并发数（可选）",
                        placeholder="自动",
                        type="number",
                        default=None,
                        min=1,
                        max=10,
                        cols=4,
                    ),
                    FieldSpec(
                        key="pansou_result_limit",
                        label="每个提供方最大结果数",
                        placeholder="自动",
                        type="number",
                        default=10,
                        min=1,
                        max=100,
                        cols=4,
                    ),
                    FieldSpec(
                        key="pansou_timeout",
                        label="请求超时（秒）",
                        type="number",
                        default=60,
                        min=5,
                        max=120,
                        cols=4,
                    ),
                ],
            ),
        ]

    @classmethod
    def get_dynamic_options(
            cls, config: Dict[str, Any], context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """按配置向 PanSou 服务探测健康度，返回当前可用的插件与频道列表。"""
        from app.log import logger
        from ..types import PANSOU_RESOURCE_TYPES, resource_type_name
        ctx = context or {}
        base_url = str(cls.config_value(config, "pansou_url", "") or "").strip()
        options = {
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
        if not base_url:
            return options

        client = PanSouClient(
            base_url=base_url,
            auth_enabled=bool(cls.config_value(config, "pansou_auth_enabled", False)),
            username=str(cls.config_value(config, "pansou_username", "") or ""),
            password=str(cls.config_value(config, "pansou_password", "") or ""),
            proxy=ctx.get("proxy"),
            search_timeout=5,
        )
        try:
            probe_timeout = max(5, min(int(cls.config_value(config, "pansou_timeout", 5) or 5), 10))
            health = client.health(timeout=probe_timeout)
            options.update({
                "status": "error" if health.get("status") == "error" else "ok",
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
        except Exception as error:
            logger.debug(f"读取 PanSou 配置选项失败：{error}")
            options["status"] = "error"
            options["error"] = str(error)
        return options

    @classmethod
    def create_provider(
            cls, service: Any, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        ctx = context or {}
        owner = ctx.get("owner")
        if not client:
            return None
        if owner:
            setattr(owner, "_pansou_client", client)

        resource_types = ctx.get("resource_types", ())
        channels = [v.strip() for v in cls.config_value(config, "pansou_channels", []) or [] if str(v).strip()]
        plugins = [v.strip() for v in cls.config_value(config, "pansou_plugins", []) or [] if str(v).strip()]
        inc = [v.strip() for v in cls.config_value(config, "pansou_filter_include", []) or [] if str(v).strip()]
        exc = [v.strip() for v in cls.config_value(config, "pansou_filter_exclude", []) or [] if str(v).strip()]
        filter_config = {"include": inc, "exclude": exc}
        concurrency = cls.config_value(config, "pansou_concurrency", None)
        try:
            concurrency = int(concurrency) if concurrency else None
        except (TypeError, ValueError):
            concurrency = None
        result_limit = int(cls.config_value(config, "pansou_result_limit", 10) or 10)
        refresh = bool(cls.config_value(config, "pansou_refresh", False))

        pansou_service = service or PanSouSearchService(
            client=client,
            resource_types=resource_types,
            channels=channels,
            plugins=plugins,
            filter_config=filter_config,
            concurrency=concurrency,
            result_limit=result_limit,
            refresh=refresh,
            owner=owner,
        )
        return create_pansou_provider(
            pansou_service,
            resource_types,
            {
                "channels": channels,
                "plugins": plugins,
                "concurrency": concurrency,
                "result_limit": result_limit,
                "refresh": refresh,
            },
        )
