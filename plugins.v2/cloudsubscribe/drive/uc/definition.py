"""UC 网盘驱动配置。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .client import UCClient
from .provider import UCDrive, create_uc_provider
from ...core.definitions import DriverDefinition, FieldSpec, GroupSpec


class UCDriverDefinition(DriverDefinition):
    """UC 网盘驱动规范。"""

    id = "uc"
    name = "UC网盘"
    icon = "mdi-cloud-download-outline"
    order = 33
    qrcode_service_type = UCClient
    qrcode_credentials = {"cookie": "uc_cookie"}
    qrcode_required_credentials = ("cookie",)
    qrcode_hint = "请使用手机 UC 浏览器扫码登录"

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        return [
            GroupSpec(
                tab="uc",
                title="UC账号",
                hide_heading=True,
                fields=[
                    FieldSpec(
                        key="uc_account_info",
                        type="account",
                        account_key="drive:uc",
                        cols=12,
                    ),
                    FieldSpec(
                        key="uc_cookie",
                        label="UC Cookie",
                        type="password",
                        default="",
                        hint="推荐点击右侧二维码用手机 UC 浏览器扫码登录；也可手动填写包含 __puus 的完整 Cookie。",
                        scan_provider="uc",
                        cols=12,
                    ),
                    FieldSpec(
                        key="uc_transfer_path",
                        label="网盘转存路径",
                        type="cloud-directory",
                        placeholder="/",
                        default="/",
                        drive_provider="uc",
                        hint="UC 分享先保存到此路径，跨盘临时目录也创建在此路径下。",
                        cols=6,
                    ),
                    FieldSpec(
                        key="uc_media_path",
                        label="媒体库目录",
                        type="cloud-directory",
                        placeholder="/",
                        default="/",
                        drive_provider="uc",
                        hint="最终媒体从此目录开始按平台规则分类；默认 / 表示网盘根目录。",
                        cols=6,
                    ),
                ],
            ),
            GroupSpec(
                tab="uc",
                title="请求与风控",
                icon="mdi-timer-cog-outline",
                hint="UC 接口超时设置；分享转存风控使用“转存设置”中的公共配置。",
                fields=[
                    FieldSpec(
                        key="uc_request_timeout",
                        label="请求超时（秒）",
                        type="number",
                        default=30,
                        min=5,
                        max=300,
                        cols=6,
                    ),
                ],
            ),
        ]

    @classmethod
    def create_client(cls, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        ctx = context or {}
        cookie = str(config.get("_uc_cookie") or config.get("uc_cookie") or "").strip()
        if not cookie:
            return None
        persist = ctx.get("persist_config")
        client = UCClient(
            cookie=cookie,
            on_cookie_refresh=(
                lambda value: persist(uc_cookie=value)
                if callable(persist) else None
            ),
            timeout=int(
                config.get("_uc_request_timeout")
                or config.get("uc_request_timeout")
                or 30
            ),
        )
        client.risk_cooldown = int(
            config.get("_transfer_risk_cooldown")
            or config.get("transfer_risk_cooldown")
            or 1800
        )
        return client

    @classmethod
    def create_provider(
            cls, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        if not client:
            return None
        return create_uc_provider(UCDrive(client=client))
