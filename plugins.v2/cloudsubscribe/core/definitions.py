"""自描述驱动与搜索渠道扩展规范（Service Provider Interface）。"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple, Type, Union

from .delegation import storage_hooks as _storage_hooks


@dataclass
class FieldSpec:
    """表单字段规范定义，用于动态下发前端渲染。"""

    key: str
    label: str = ""
    type: str = "text"  # text, password, number, switch, select, cloud-directory, account, test-source 等
    hint: str = ""
    cols: int = 12
    placeholder: str = ""
    default: Any = None
    options: Optional[List[Dict[str, Any]]] = None
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    suffix: str = ""
    show_condition: Optional[str] = None
    scan_provider: Optional[str] = None
    drive_provider: Optional[str] = None
    account_key: Optional[str] = None
    source: Optional[str] = None
    test_source: Optional[str] = None
    compact: bool = False
    items: Optional[List[Dict[str, Any]]] = None
    lines: Optional[List[str]] = None
    multiple: bool = False
    searchable: bool = False
    disabled: bool = False
    readonly: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "type": self.type,
            "cols": self.cols,
        }
        if self.hint:
            data["hint"] = self.hint
        if self.placeholder:
            data["placeholder"] = self.placeholder
        if self.default is not None:
            data["default"] = self.default
        raw_options = self.items if self.items is not None else self.options
        if raw_options is not None:
            # 复制列表，避免被缓存的渠道定义被下游（前端 schema 组装）就地修改。
            data["items"] = list(raw_options)
            data["options"] = list(raw_options)
        if self.lines is not None:
            data["lines"] = list(self.lines)
        if self.multiple:
            data["multiple"] = self.multiple
        if self.searchable:
            data["searchable"] = self.searchable
        if self.disabled:
            data["disabled"] = self.disabled
        if self.readonly:
            data["readonly"] = self.readonly
        if self.min is not None:
            data["min"] = self.min
        if self.max is not None:
            data["max"] = self.max
        if self.step is not None:
            data["step"] = self.step
        if self.suffix:
            data["suffix"] = self.suffix
        if self.show_condition:
            data["show"] = self.show_condition
        if self.scan_provider:
            data["scanProvider"] = self.scan_provider
        if self.drive_provider:
            data["driveProvider"] = self.drive_provider
        if self.account_key:
            data["accountKey"] = self.account_key
        if self.source:
            data["source"] = self.source
        if self.test_source:
            data["testSource"] = self.test_source
        if self.compact:
            data["compact"] = self.compact
        if self.extra:
            data.update(self.extra)
        return data


@dataclass
class GroupSpec:
    """配置分组规范定义。"""

    tab: str = ""
    title: str = ""
    icon: str = ""
    hint: str = ""
    hide_heading: bool = False
    before_tabs: bool = False
    show_condition: Optional[str] = None
    fields: List[FieldSpec] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "title": self.title,
            "fields": [f.to_dict() for f in self.fields],
        }
        if self.tab:
            data["tab"] = self.tab
        if self.icon:
            data["icon"] = self.icon
        if self.hint:
            data["hint"] = self.hint
        if self.hide_heading:
            data["hideHeading"] = self.hide_heading
        if self.before_tabs:
            data["beforeTabs"] = self.before_tabs
        if self.show_condition:
            data["show"] = self.show_condition
        return data


def checkin_mode_options(
        modes: Sequence[str],
        labels: Optional[Mapping[str, str]] = None,
) -> List[Dict[str, Any]]:
    """按渠道自描述的模式文案生成下拉选项；未声明文案的模式回落到模式标识。"""
    mapping = labels or {}
    return [
        {"title": str(mapping.get(mode) or mode), "value": mode}
        for mode in modes
    ]


@dataclass(frozen=True)
class CheckinDefinition:
    """签到提供方自描述契约：只描述元数据与表单，执行由 CheckinService 统一负责。"""

    key: str
    name: str
    icon: str = "mdi-calendar-check-outline"
    credential_attrs: Tuple[str, ...] = ()
    credential_keys: Tuple[str, ...] = ()
    error_types: Tuple[Type[Exception], ...] = (Exception,)
    modes: Tuple[str, ...] = ("normal",)
    #: 渠道自定义的模式文案（如 lucky -> 幸运签到）；未声明的模式直接展示标识。
    mode_labels: Mapping[str, str] = field(default_factory=dict)
    order: int = 100
    group: Optional[GroupSpec] = None
    points_label: str = "积分"
    track_days: bool = True
    #: 非空表示客户端来自网盘驱动管理器（网盘渠道），否则来自搜索渠道注册表。
    drive_key: str = ""
    #: 未配置凭据时的提示，可为字符串或 owner -> 文本 的函数（双模式渠道按模式给提示）。
    hint: Union[str, Callable[[Any], str]] = ""
    #: 自定义凭据判定，缺省按 credential_attrs 判断。
    ready: Optional[Callable[[Any], bool]] = None

    @property
    def enabled_key(self) -> str:
        return f"{self.key}_checkin_enabled"

    @property
    def mode_key(self) -> str:
        return f"{self.key}_checkin_mode"

    @property
    def enabled_attr(self) -> str:
        """宿主上保存“是否启用签到”的运行时属性名。"""
        return f"_{self.enabled_key}"

    @property
    def mode_attr(self) -> str:
        """宿主上保存签到模式的运行时属性名。"""
        return f"_{self.mode_key}"

    @property
    def history_key(self) -> str:
        return f"{self.key}_checkin_history"

    @property
    def default_mode(self) -> str:
        return self.modes[0] if self.modes else "normal"

    def mode_label(self, mode: str) -> str:
        """取渠道自定义的模式文案，未声明时回落到模式标识。"""
        key = str(mode or "").strip().lower()
        return str(self.mode_labels.get(key) or key)

    def to_provider_spec(self) -> Dict[str, Any]:
        """供前端 checkin-timeline 消费的提供者规格。"""
        return {
            "key": self.key,
            "name": self.name,
            "icon": self.icon,
            "enabledKey": self.enabled_key,
            "modeKey": self.mode_key,
            "credentialKeys": list(self.credential_keys),
            "pointsLabel": self.points_label,
            "trackDays": self.track_days,
        }


def build_checkin_definition(
        definition_cls: Any,
        *,
        group_title: str,
        drive_key: str = "",
        hint: Union[str, Callable[[Any], str]] = "",
        ready: Optional[Callable[[Any], bool]] = None,
        key: str = "",
        name: str = "",
        icon: str = "",
        order: Optional[int] = None,
        group_icon: str = "",
        credential_attrs: Sequence[str] = (),
        credential_keys: Sequence[str] = (),
        error_types: Sequence[Type[Exception]] = (Exception,),
        modes: Sequence[str] = ("normal",),
        mode_labels: Optional[Mapping[str, str]] = None,
        points_label: str = "积分",
        track_days: bool = True,
        enable_label: str = "启用每日签到",
        enable_hint: str = "",
        enable_cols: int = 4,
        mode_label: str = "签到模式",
        group_hint: str = "",
        fields: Sequence[FieldSpec] = (),
) -> CheckinDefinition:
    """按渠道自描述自动补齐签到契约与通用表单字段。

    key/name/icon/order 默认取自所属渠道定义类，只有差异项需要显式声明；启用开关与
    签到模式下拉框按模式列表自动生成。执行细节由 CheckinService 统一处理，渠道只需在
    客户端实现 checkin(mode)。
    """
    resolved_key = str(key or getattr(definition_cls, "id", "") or "").strip().lower()
    if not resolved_key:
        raise ValueError("签到提供方缺少 key")
    resolved_name = str(name or getattr(definition_cls, "name", "") or resolved_key)
    resolved_icon = str(
        icon or getattr(definition_cls, "icon", "") or "mdi-calendar-check-outline"
    )
    resolved_order = int(
        getattr(definition_cls, "order", 100) if order is None else order
    )
    resolved_modes = tuple(dict.fromkeys(
        str(mode or "").strip().lower()
        for mode in modes
        if str(mode or "").strip()
    )) or ("normal",)
    resolved_labels = {
        str(key or "").strip().lower(): str(value or "").strip()
        for key, value in dict(mode_labels or {}).items()
        if str(key or "").strip() and str(value or "").strip()
    }

    resolved_enable_cols = max(1, min(int(enable_cols or 4), 12))
    if len(resolved_modes) > 1:
        # 开关与模式选择固定同排：两者列宽互补，任一渠道改宽都不会把选择框挤到下一行
        # （outlined 标签换行后会被上一行压住），因此这里由开关列宽推导选择框列宽。
        resolved_enable_cols = min(resolved_enable_cols, 8)
    group_fields: List[FieldSpec] = [
        FieldSpec(
            key=f"{resolved_key}_checkin_enabled",
            label=enable_label,
            type="switch",
            hint=enable_hint,
            cols=resolved_enable_cols,
        )
    ]
    if len(resolved_modes) > 1:
        group_fields.append(FieldSpec(
            key=f"{resolved_key}_checkin_mode",
            label=mode_label,
            type="select",
            options=checkin_mode_options(resolved_modes, resolved_labels),
            cols=12 - resolved_enable_cols,
            show_condition=f"config.{resolved_key}_checkin_enabled",
        ))
    group_fields.extend(fields)

    return CheckinDefinition(
        mode_labels=resolved_labels,
        key=resolved_key,
        name=resolved_name,
        icon=resolved_icon,
        credential_attrs=tuple(credential_attrs),
        credential_keys=tuple(credential_keys),
        error_types=tuple(error_types) or (Exception,),
        modes=resolved_modes,
        order=resolved_order,
        points_label=str(points_label or "积分"),
        track_days=bool(track_days),
        group=GroupSpec(
            tab="checkin",
            title=str(group_title or f"{resolved_name} 签到"),
            icon=str(group_icon or resolved_icon),
            hint=str(group_hint or ""),
            fields=group_fields,
        ),
        drive_key=str(drive_key or ""),
        hint=hint,
        ready=ready,
    )


class DriverDefinition:
    """网盘驱动自描述规范基类。"""

    id: str = ""
    name: str = ""
    icon: str = "mdi-cloud-outline"
    order: int = 100
    qrcode_service_type: Any = None
    qrcode_credentials: Mapping[str, str] = {}
    qrcode_required_credentials: Tuple[str, ...] = ()
    qrcode_hint: str = "请扫描二维码完成登录"
    qrcode_channels: Tuple[Mapping[str, str], ...] = ()

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        """返回该驱动配置表单的分组与字段定义。"""
        return []

    @classmethod
    def get_transfer_path_key(cls) -> str:
        """返回该驱动「网盘转存路径」的表单键名；未声明时返回空串。"""
        for group in cls.get_config_groups():
            for field in group.fields:
                if field.key.endswith("_transfer_path"):
                    return field.key
        return ""

    @classmethod
    def get_media_path_key(cls) -> str:
        """返回该驱动「媒体库目录」的表单键名；未声明时返回空串。"""
        for group in cls.get_config_groups():
            for field in group.fields:
                if field.key.endswith("_media_path"):
                    return field.key
        return ""

    @classmethod
    def get_checkin_definition(cls) -> Optional[CheckinDefinition]:
        """如果该网盘支持签到，返回其自描述契约。"""
        return None

    @classmethod
    def create_qrcode_service(cls) -> Any:
        """按客户端方法形态自动选择类服务或无凭据实例。"""
        service_type = cls.qrcode_service_type
        if service_type is None:
            return None
        creator = getattr(service_type, "create_qrcode_login", None)
        if not callable(creator):
            return None
        parameters = tuple(inspect.signature(creator).parameters.values())
        if parameters and parameters[0].name == "self":
            return service_type()
        return service_type

    @classmethod
    def qrcode_meta(cls) -> Dict[str, Any]:
        return {
            "name": cls.name,
            "hint": cls.qrcode_hint,
            "channels": [dict(item) for item in cls.qrcode_channels],
        }

    @classmethod
    def qrcode_config_values(cls, result: Mapping[str, Any]) -> Dict[str, str]:
        missing = [
            key for key in cls.qrcode_required_credentials
            if not str(result.get(key) or "").strip()
        ]
        if missing:
            raise RuntimeError(f"扫码成功但未获得凭据：{', '.join(missing)}")
        return {
            config_key: str(result.get(result_key) or "").strip()
            for result_key, config_key in cls.qrcode_credentials.items()
            if str(result.get(result_key) or "").strip()
        }

    @classmethod
    def create_client(cls, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        """从全局配置字典中构建驱动客户端实例。"""
        raise NotImplementedError

    @classmethod
    def create_provider(
            cls, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        """将客户端包装为标准的 CloudDriveProvider 实例。"""
        raise NotImplementedError

    @classmethod
    def get_account_info(
            cls, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Dict[str, Any]:
        """读取该驱动的实时账户信息。"""
        return {"connected": bool(client), "error": ""}


class SearchSourceDefinition:
    """搜索渠道自描述规范基类。"""

    id: str = ""
    name: str = ""
    icon: str = "mdi-magnify"
    #: 渠道在界面上的配色，由后端下发给前端直接渲染。
    color: str = "grey"
    order: int = 100

    @staticmethod
    def config_value(config: Mapping[str, Any], key: str, default: Any = None) -> Any:
        value = config.get(f"_{key}")
        if value is None:
            value = config.get(key)
        return default if value is None else value

    @classmethod
    def storage_hooks(cls, context: Optional[Any] = None) -> Dict[str, Any]:
        """返回客户端所需的存储读写钩子，统一解析真正的存储宿主。"""
        ctx = context or {}
        owner = ctx.get("storage_owner") if isinstance(ctx, Mapping) else ctx
        return _storage_hooks(owner)

    @classmethod
    def get_config_groups(cls, context: Optional[Dict[str, Any]] = None) -> List[GroupSpec]:
        """返回该搜索渠道表单的分组与字段定义。"""
        return []

    @classmethod
    def get_checkin_definition(cls) -> Optional[CheckinDefinition]:
        """如果该搜索渠道支持签到，返回其自描述契约。"""
        return None

    @classmethod
    def get_config_keys(cls) -> FrozenSet[str]:
        cached = cls.__dict__.get("_config_keys_cache")
        if cached is not None:
            return cached
        keys = {
            field.key
            for group in cls.get_config_groups()
            for field in group.fields
            if field.key and not field.key.startswith("test_")
        }
        cls._config_keys_cache = frozenset(keys)
        return cls._config_keys_cache

    @classmethod
    def configure_owner(
            cls, owner: Any, config: Mapping[str, Any]
    ) -> None:
        """归一化渠道运行参数并初始化渠道私有状态。"""

    @classmethod
    def get_search_timeout(
            cls, config: Mapping[str, Any], default: float = 60.0
    ) -> float:
        """返回渠道搜索超时；未声明独立配置时使用全局值。"""
        key = f"{cls.id}_timeout"
        if key not in cls.get_config_keys():
            return float(default)
        try:
            value = float(cls.config_value(config, key, default) or default)
        except (TypeError, ValueError):
            value = float(default)
        return max(5.0, min(value, 120.0))

    @classmethod
    def build_test_context(
            cls, config: Dict[str, Any], context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """返回隔离测试处理器所需的渠道专属依赖。"""
        return {}

    @classmethod
    def close_test_resources(cls) -> None:
        """释放 Definition 持有的可复用测试资源。"""

    @classmethod
    def create_client(cls, config: Dict[str, Any], context: Optional[Any] = None) -> Any:
        """构建底层搜索 API 客户端。"""
        return None

    @classmethod
    def create_service(
            cls, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        """构建搜索业务服务。"""
        return None

    @classmethod
    def create_provider(
            cls, service: Any, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Any:
        """构建标准 SearchProvider 实例以接入 SearchRegistry。"""
        raise NotImplementedError

    @classmethod
    def get_account_info(
            cls, client: Any, config: Dict[str, Any], context: Optional[Any] = None
    ) -> Dict[str, Any]:
        """读取该搜索源的账户信息。"""
        return {"connected": bool(client), "error": ""}
