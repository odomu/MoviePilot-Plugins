"""MoviePilot 插件单测的公共 mock 与模块加载工具。

测试环境缺少 MoviePilot 运行时（``app.*``）依赖，各测试文件此前需要重复声明
``AutoMockModule``/``_ensure_mock_package``/``OwnerDelegator`` 并手写 importlib 加载。
本模块把这些公共逻辑集中一处：

* :class:`AutoMockModule` / :func:`ensure_package` / :func:`install_app_mocks`
  —— 用属性即返回 ``MagicMock`` 的占位模块补齐 ``app.*`` 依赖；
* :class:`OwnerDelegator` —— 与插件运行期一致的职责委托基类；
* :func:`set_media_type` —— 注入 ``app.schemas.types.MediaType`` 桩；
* :func:`load_module` —— 按仓库相对路径加载插件模块并保持包层级以支持相对导入。
"""

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

# tests/ 的上级目录即仓库根目录
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AutoMockModule(types.ModuleType):
    """未定义属性自动返回 ``MagicMock`` 的占位模块。"""

    def __getattr__(self, name):
        value = MagicMock()
        setattr(self, name, value)
        return value


def ensure_package(name: str) -> AutoMockModule:
    """确保 ``name`` 已注册为占位包（保留 ``__path__`` 与既有属性）。"""
    existing = sys.modules.get(name)
    if isinstance(existing, AutoMockModule):
        return existing
    module = AutoMockModule(name)
    module.__path__ = []
    if existing is not None:
        # 已被其它测试加载过的模块，保留其公开属性避免重复加载时丢失真实实现。
        for key, value in list(vars(existing).items()):
            if not key.startswith("_"):
                setattr(module, key, value)
    sys.modules[name] = module
    parent_name, _, child = name.rpartition(".")
    parent = sys.modules.get(parent_name) if parent_name else None
    if parent is not None:
        setattr(parent, child, module)
    return module


def install_app_mocks(*extra_packages: str) -> None:
    """注册 ``app.*`` 占位包；``extra_packages`` 可追加项目内的占位包。"""
    for name in ("app", "app.log", "app.schemas", "app.schemas.types"):
        ensure_package(name)
    for name in extra_packages:
        ensure_package(name)


class OwnerDelegator:
    """职责对象状态委托（等价于 ``core/delegation.py``，``owner`` 可缺省）。"""

    def __init__(self, owner=None):
        object.__setattr__(self, "_owner", owner)

    def __getattr__(self, name):
        owner = self.__dict__.get("_owner")
        if owner is None:
            return None
        return getattr(owner, name)

    def __setattr__(self, name, value):
        if name == "_owner":
            object.__setattr__(self, name, value)
            return
        owner = self.__dict__.get("_owner")
        if owner is not None:
            setattr(owner, name, value)
        else:
            object.__setattr__(self, name, value)


def _member(value):
    class _EnumMember:
        def __init__(self, raw):
            self.value = raw

        def __str__(self):
            return str(self.value)

        def __eq__(self, other):
            return other == self.value or other is self

        def __hash__(self):
            return hash(self.value)

    return _EnumMember(value)


def set_media_type(movie: str = "movie", tv: str = "tv"):
    """注入 ``app.schemas.types.MediaType`` 桩并返回该类。

    成员同时具备 ``.value`` 与字符串等价比较，兼容 ``MediaType.TV.value`` 和
    ``MediaType.TV == "电视剧"`` 两种写法。
    """
    media_type = type("MediaType", (), {
        "MOVIE": _member(movie),
        "TV": _member(tv),
    })
    ensure_package("app.schemas.types").MediaType = media_type
    return media_type


def _ensure_parents(name: str) -> None:
    parts = name.split(".")[:-1]
    for index in range(1, len(parts) + 1):
        parent = ".".join(parts[:index])
        if parent not in sys.modules:
            ensure_package(parent)


def _register(name: str, module) -> None:
    _ensure_parents(name)
    sys.modules[name] = module
    parent_name, _, child = name.rpartition(".")
    parent = sys.modules.get(parent_name) if parent_name else None
    if parent is not None:
        setattr(parent, child, module)


def register_module(name: str, *, package: bool = False, **attrs):
    """创建并注册真实 ``types.ModuleType`` 占位模块，``attrs`` 为预置属性。"""
    module = types.ModuleType(name)
    if package:
        module.__path__ = []
    for key, value in attrs.items():
        setattr(module, key, value)
    _register(name, module)
    return module


def register_mock(name: str, **attrs):
    """创建并注册 ``MagicMock`` 占位模块，未预置属性访问时自动返回 Mock。"""
    module = MagicMock()
    module.__path__ = []
    for key, value in attrs.items():
        setattr(module, key, value)
    _register(name, module)
    return module


def register_package(name: str, paths=(), **attrs):
    """注册占位包；``paths`` 非空时作为真实 ``__path__`` 供相对导入使用。"""
    module = ensure_package(name)
    if paths:
        module.__path__ = list(paths)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def load_module(module_name: str, relative_path: str):
    """加载仓库内插件模块；保持包层级以支持其相对导入。"""
    path = relative_path
    if not os.path.isabs(path):
        path = os.path.join(REPO_ROOT, relative_path)
    _ensure_parents(module_name)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name.rsplit(".", 1)[0]
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
