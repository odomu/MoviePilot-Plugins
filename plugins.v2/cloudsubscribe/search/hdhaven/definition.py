"""HDHaven 渠道自描述扩展规范与表单声明。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import HDHavenClient, HDHavenError
from .provider import create_hdhaven_provider
from ...core.definitions import (
    CheckinDefinition,
    FieldSpec,
    GroupSpec,
    SearchSourceDefinition,
    build_checkin_definition,
)


class HDHavenSourceDefinition(SearchSourceDefinition):
    """HDHaven 搜索渠道自描述规范。"""

    id = "hdhaven"
    name = "HDHaven"
    icon = "mdi-movie-open-star-outline"
    order = 20

    @classmethod
    def configure_owner(cls, owner: Any, config: Dict[str, Any]) -> None:
        value = lambda key, default=None: cls.config_value(owner.__dict__, key, default)
        owner._hdhaven_base_url = str(value("hdhaven_base_url", "https://hdhaven.com") or "https://hdhaven.com").strip()
        owner._hdhaven_username = str(value("hdhaven_username", "") or "")
        owner._hdhaven_password = str(value("hdhaven_password", "") or "")
        owner._hdhaven_auto_unlock = bool(value("hdhaven_auto_unlock", True))
        owner._hdhaven_max_unlock_points = max(0, int(value("hdhaven_max_unlock_points", 50) or 50))
        owner._hdhaven_max_points_per_sub = max(0, int(value("hdhaven_max_points_per_sub", 20) or 20))
        owner._hdhaven_request_interval = float(value("hdhaven_request_interval", 2) or 2)
        owner._hdhaven_unlocks_per_minute = max(1, min(int(value("hdhaven_unlocks_per_minute", 5) or 5), 10))
        owner._hdhaven_magnet_enabled = bool(value("hdhaven_magnet_enabled", False))
        owner._hdhaven_candidate_limit = max(1, min(int(value("hdhaven_candidate_limit", 4) or 4), 20))
        owner._hdhaven_timeout = max(5, min(int(value("hdhaven_timeout", 60) or 60), 120))

    @classmethod
    def build_test_context(
            cls, config: Dict[str, Any], context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        points = max(0, int((context or {}).get("confirmed_unlock_points") or 0))
        return {
            "hdhaven_auto_unlock": False,
            "hdhaven_max_unlock_points": points,
            "hdhaven_max_points_per_sub": points,
        }

    @classmethod
    def get_checkin_definition(cls) -> Optional[CheckinDefinition]:
        """自动注册 HDHaven 签到契约：模式下拉框由 modes 自动生成。"""
        return build_checkin_definition(
            cls,
            credential_attrs=("_hdhaven_username", "_hdhaven_password"),
            credential_keys=("hdhaven_username", "hdhaven_password"),
            error_types=(HDHavenError,),
            modes=("normal", "gambler"),
            group_title="HDHaven 签到",
            enable_cols=4,
            mode_hint="HDHaven 赌狗签到有几率暴击获得多倍丰厚积分，也有几率扣除积分。",
        )

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="hdhaven",
                title="HDHaven",
                icon="mdi-movie-open-star-outline",
                fields=[
                    FieldSpec(
                        key="hdhaven_account_info",
                        type="account",
                        account_key="search:hdhaven",
                        compact=True,
                        cols=12,
                    ),
                    FieldSpec(
                        key="hdhaven_base_url",
                        label="服务地址",
                        placeholder="https://hdhaven.org",
                        default="https://hdhaven.com",
                        cols=12,
                    ),
                    FieldSpec(
                        key="hdhaven_username",
                        label="用户名",
                        default="",
                        hint="填写 HDHaven 账号用户名，后台将通过 Turnstile 自动登录鉴权",
                        cols=6,
                    ),
                    FieldSpec(
                        key="hdhaven_password",
                        label="密码",
                        type="password",
                        default="",
                        cols=6,
                    ),
                    FieldSpec(
                        key="test_hdhaven",
                        label="测试搜索",
                        type="test-source",
                        source="hdhaven",
                        cols=12,
                    ),
                ],
            ),
            GroupSpec(
                tab="hdhaven",
                title="HDHaven 积分解锁与预算",
                icon="mdi-ticket-confirmation-outline",
                fields=[
                    FieldSpec(
                        key="hdhaven_auto_unlock",
                        label="允许积分解锁",
                        hint="关闭时仅转存免费资源，开启后将按预算安全解锁",
                        type="switch",
                        default=True,
                        cols=4,
                    ),
                    FieldSpec(
                        key="hdhaven_max_unlock_points",
                        label="单次积分总预算",
                        hint="单次同步最大解锁积分",
                        type="number",
                        default=50,
                        min=0,
                        cols=4,
                        show_condition="config.hdhaven_auto_unlock",
                    ),
                    FieldSpec(
                        key="hdhaven_max_points_per_sub",
                        label="单订阅解锁预算",
                        hint="单订阅累计解锁预算",
                        type="number",
                        default=20,
                        min=0,
                        cols=4,
                        show_condition="config.hdhaven_auto_unlock",
                    ),
                ],
            ),
            GroupSpec(
                tab="hdhaven",
                title="搜索与风控",
                icon="mdi-shield-search",
                fields=[
                    FieldSpec(
                        key="hdhaven_candidate_limit",
                        label="候选上限",
                        hint="最大保留候选数量",
                        type="number",
                        default=4,
                        min=1,
                        max=20,
                        cols=6,
                    ),
                    FieldSpec(
                        key="hdhaven_timeout",
                        label="搜索超时",
                        hint="单次搜索超时秒数，默认 60 秒",
                        type="number",
                        default=60,
                        min=5,
                        max=120,
                        suffix="秒",
                        cols=6,
                    ),
                    FieldSpec(
                        key="hdhaven_request_interval",
                        label="请求访问间隔",
                        hint="接口请求基础间隔秒数",
                        type="number",
                        default=2.0,
                        min=1.0,
                        max=10.0,
                        step=0.5,
                        suffix="秒",
                        cols=6,
                    ),
                    FieldSpec(
                        key="hdhaven_unlocks_per_minute",
                        label="每分钟解锁次数",
                        hint="解锁频次限制，默认 5 次",
                        type="number",
                        default=5,
                        min=1,
                        max=10,
                        step=1,
                        suffix="次/分钟",
                        cols=6,
                    ),
                    FieldSpec(
                        key="hdhaven_magnet_enabled",
                        label="获取原生磁力链接",
                        hint="按优先级获取 HDHaven 原生磁力资源（Torrentio/海外高种）",
                        type="switch",
                        default=False,
                        cols=12,
                    ),
                ],
            ),
        ]

    @classmethod
    def create_client(cls, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        username = str(cls.config_value(config, "hdhaven_username", "") or "").strip()
        password = str(cls.config_value(config, "hdhaven_password", "") or "").strip()
        if not username or not password:
            return None
        owner = (context or {}).get("storage_owner")
        return HDHavenClient(
            username=username,
            password=password,
            base_url=str(
                cls.config_value(config, "hdhaven_base_url", HDHavenClient.DEFAULT_BASE_URL)
                or HDHavenClient.DEFAULT_BASE_URL
            ),
            proxy=(context or {}).get("proxy"),
            request_interval=float(cls.config_value(config, "hdhaven_request_interval", 1.0) or 1.0),
            get_data_func=getattr(owner, "get_data", None),
            save_data_func=getattr(owner, "save_data", None),
        )

    @classmethod
    def create_provider(
            cls, service: Any, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        ctx = context or {}
        hdhaven_service = ctx.get("hdhaven_service")
        if not hdhaven_service and ctx.get("owner"):
            try:
                from .service import HDHavenSearchService
                hdhaven_service = HDHavenSearchService(ctx["owner"])
            except Exception:
                hdhaven_service = None
        if hdhaven_service and getattr(hdhaven_service, "available", False):
            return create_hdhaven_provider(hdhaven_service)
        return None

