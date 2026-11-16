"""Dian115 渠道自描述规范与表单声明。"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from .client import Dian115Client, Dian115Error
from .provider import create_dian115_provider
from ...core.definitions import (
    CheckinDefinition,
    FieldSpec,
    GroupSpec,
    SearchSourceDefinition,
    build_checkin_definition,
)


class Dian115SourceDefinition(SearchSourceDefinition):
    """Dian115 搜索渠道规范。"""

    id = "dian115"
    name = "Dian115"
    icon = "mdi-cloud-search"
    color = "amber-darken-2"
    order = 30

    @classmethod
    def configure_owner(cls, owner: Any, config: Dict[str, Any]) -> None:
        value = lambda key, default=None: cls.config_value(owner.__dict__, key, default)
        owner._dian115_email = str(value("dian115_email", "") or "").strip()
        owner._dian115_password = str(value("dian115_password", "") or "").strip()
        owner._dian115_auto_unlock = bool(value("dian115_auto_unlock", False))
        owner._dian115_max_unlock_points = max(0, int(value("dian115_max_unlock_points", 50) or 0))
        owner._dian115_max_points_per_sub = max(0, int(value("dian115_max_points_per_sub", 20) or 0))
        owner._dian115_candidate_limit = max(1, min(int(value("dian115_candidate_limit", 4) or 4), 20))
        owner._dian115_request_interval = max(0.2, min(float(value("dian115_request_interval", 1) or 1), 10.0))
        owner._dian115_unlocks_per_minute = max(1, min(int(value("dian115_unlocks_per_minute", 6) or 6), 10))
        owner._dian115_timeout = max(5, min(int(value("dian115_timeout", 60) or 60), 120))
        owner._dian115_client = None
        owner._dian115_resources = None
        owner._dian115_client_lock = threading.RLock()

    @classmethod
    def build_test_context(
            cls, config: Dict[str, Any], context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        points = max(0, int((context or {}).get("confirmed_unlock_points") or 0))
        return {
            "dian115_max_unlock_points": points,
            "dian115_max_points_per_sub": points,
        }

    @classmethod
    def get_checkin_definition(cls) -> Optional[CheckinDefinition]:
        """自动注册 Dian115 签到契约：模式下拉框由 modes 自动生成。"""
        return build_checkin_definition(
            cls,
            credential_attrs=("_dian115_email", "_dian115_password"),
            credential_keys=("dian115_email", "dian115_password"),
            error_types=(Dian115Error,),
            modes=("normal", "lucky"),
            mode_labels={"normal": "普通签到", "lucky": "运气签到"},
            group_title="Dian115 签到与娱乐",
            enable_cols=4,
            group_hint="运气签到可得 3～10 倍奖励，也有 21% 概率扣除 1 倍普通签到积分。",
            fields=[
                FieldSpec(
                    key="dian115_lottery_enabled",
                    label="启用幸运转盘",
                    type="switch",
                    default=False,
                    hint="每次消耗 5 积分",
                    cols=4,
                    show_condition="config.dian115_checkin_enabled",
                ),
                FieldSpec(
                    key="dian115_lottery_count",
                    label="每日转盘目标次数",
                    type="number",
                    default=1,
                    min=1,
                    max=20,
                    suffix="次",
                    hint="最多 20 次；会先读取站点当天已用次数，只执行尚缺的次数。",
                    cols=8,
                    show_condition="config.dian115_checkin_enabled && config.dian115_lottery_enabled",
                ),
            ],
        )

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="dian115",
                title="Dian115 账号",
                icon="mdi-account-key-outline",
                hint="使用浏览器仿真完成 Turnstile 登录并维护访问状态。",
                fields=[
                    FieldSpec(
                        key="dian115_account_info",
                        type="account",
                        account_key="search:dian115",
                        compact=True,
                        cols=12,
                    ),
                    FieldSpec(
                        key="dian115_base_url",
                        label="服务地址",
                        placeholder="https://115.dian.me",
                        default="https://m.dian115.com",
                        cols=12,
                    ),
                    FieldSpec(
                        key="dian115_email",
                        label="登录邮箱",
                        default="",
                        cols=6,
                    ),
                    FieldSpec(
                        key="dian115_password",
                        label="登录密码",
                        type="password",
                        default="",
                        cols=6,
                    ),
                    FieldSpec(
                        key="test_dian115",
                        label="测试搜索",
                        type="test-source",
                        source="dian115",
                        cols=12,
                    ),
                ],
            ),
            GroupSpec(
                tab="dian115",
                title="Dian115 积分解锁",
                icon="mdi-ticket-confirmation-outline",
                fields=[
                    FieldSpec(
                        key="dian115_auto_unlock",
                        label="允许积分解锁",
                        type="switch",
                        default=False,
                        cols=4,
                    ),
                    FieldSpec(
                        key="dian115_max_unlock_points",
                        label="单次积分总预算",
                        hint="单次同步最大解锁积分",
                        type="number",
                        default=50,
                        min=0,
                        cols=4,
                        show_condition="config.dian115_auto_unlock",
                    ),
                    FieldSpec(
                        key="dian115_max_points_per_sub",
                        label="单订阅解锁预算",
                        hint="单订阅累计解锁预算",
                        type="number",
                        default=20,
                        min=0,
                        cols=4,
                        show_condition="config.dian115_auto_unlock",
                    ),
                ],
            ),
            GroupSpec(
                tab="dian115",
                title="搜索与风控",
                icon="mdi-shield-search",
                fields=[
                    FieldSpec(
                        key="dian115_candidate_limit",
                        label="候选上限",
                        hint="最大保留候选数量",
                        type="number",
                        default=4,
                        min=1,
                        max=20,
                        cols=6,
                    ),
                    FieldSpec(
                        key="dian115_unlocks_per_minute",
                        label="每分钟解锁次数",
                        hint="解锁频次限制，默认 6 次",
                        type="number",
                        default=6,
                        min=1,
                        max=10,
                        suffix="次",
                        cols=6,
                    ),
                    FieldSpec(
                        key="dian115_request_interval",
                        label="请求间隔",
                        hint="接口请求基础间隔秒数",
                        type="number",
                        default=1.0,
                        min=0.2,
                        max=10,
                        step=0.2,
                        suffix="秒",
                        cols=6,
                    ),
                    FieldSpec(
                        key="dian115_timeout",
                        label="搜索超时",
                        hint="单次搜索超时秒数，默认 60 秒",
                        type="number",
                        default=60,
                        min=5,
                        max=120,
                        suffix="秒",
                        cols=6,
                    ),
                ],
            ),
        ]

    @classmethod
    def create_client(cls, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        ctx = context or {}
        email = str(cls.config_value(config, "dian115_email", "") or "").strip()
        password = str(cls.config_value(config, "dian115_password", "") or "").strip()
        if not email or not password:
            return None
        return Dian115Client(
            email=email,
            password=password,
            proxy=ctx.get("proxy"),
            request_interval=float(cls.config_value(config, "dian115_request_interval", 1.0) or 1.0),
            unlocks_per_minute=int(cls.config_value(config, "dian115_unlocks_per_minute", 6) or 6),
            timeout=int(cls.config_value(config, "dian115_timeout", 30) or 30),
            **cls.storage_hooks(ctx),
            lottery_enabled=bool(cls.config_value(config, "dian115_lottery_enabled", False)),
            lottery_count=int(cls.config_value(config, "dian115_lottery_count", 0) or 0),
        )

    @classmethod
    def create_provider(
            cls, service: Any, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        ctx = context or {}
        dian115_service = ctx.get("dian115_service")
        if not dian115_service and ctx.get("owner"):
            try:
                from .service import Dian115SearchService
                dian115_service = Dian115SearchService(ctx["owner"])
            except Exception:
                dian115_service = None
        if dian115_service and getattr(dian115_service, "available", False):
            return create_dian115_provider(dian115_service)
        return None

