"""组合式职责对象的状态委托。"""

from typing import Any, Dict


def resolve_storage_owner(owner):
    """沿 ``_plugin``/``_owner`` 链找到真正实现 get_data/save_data 的宿主。

    搜索渠道的 ``service``/``handler`` 都只是委托层，把 ``getattr(self, "get_data")``
    直接传给客户端会拿到 ``None``，导致登录态、积分历史等无法落库。
    """
    current = owner
    seen = set()
    while current is not None and id(current) not in seen:
        if callable(getattr(current, "get_data", None)) and callable(
                getattr(current, "save_data", None)
        ):
            return current
        seen.add(id(current))
        current = getattr(current, "_plugin", None) or getattr(current, "_owner", None)
    return owner


def storage_hooks(owner) -> Dict[str, Any]:
    """返回客户端可用的存储读写钩子，自动穿透委托层。"""
    owner = resolve_storage_owner(owner)
    return {
        "get_data_func": getattr(owner, "get_data", None),
        "save_data_func": getattr(owner, "save_data", None),
    }


class OwnerDelegator:
    """将职责对象未定义的属性读写委托给所属对象。"""

    def __init__(self, owner):
        object.__setattr__(self, "_owner", owner)

    def storage_hooks(self) -> Dict[str, Any]:
        """返回客户端可用的存储读写钩子，自动穿透委托层。"""
        return storage_hooks(self._owner)

    def __getattr__(self, name):
        return getattr(self._owner, name)

    def __setattr__(self, name, value):
        if name == "_owner":
            object.__setattr__(self, name, value)
            return
        setattr(self._owner, name, value)


def get_component(owner, component_type, cache_name: str):
    """按类型创建并缓存职责组件。"""
    components = owner.__dict__.setdefault(cache_name, {})
    component = components.get(component_type)
    if component is None:
        component = component_type(owner)
        components[component_type] = component
    return component


def resolve_component(owner, component_types, name: str, cache_name: str):
    """从职责组件中解析宿主未直接定义的属性。"""
    for component_type in component_types:
        if hasattr(component_type, name):
            component = get_component(owner, component_type, cache_name)
            return getattr(component, name)
    raise AttributeError(
        f"{owner.__class__.__name__!s} object has no attribute {name!r}"
    )
