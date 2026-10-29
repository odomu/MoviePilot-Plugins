"""搜索渠道共享的资源类型定义。"""

import re
from typing import Any, Dict, Mapping
from urllib.parse import urlparse

#: 资源类型唯一元数据源。
_RESOURCE_TYPE_META: Dict[str, Dict[str, Any]] = {
    "115": {
        "name": "115网盘", "icon": "mdi-cloud", "color": "primary",
        "hosts": ("115.com", "115cdn.com", "anxia.com"),
        "previewable": True, "preview_provider": "115",
        "password_param": "password", "aliases": ("115pan",),
    },
    "123": {
        "name": "123网盘", "icon": "mdi-cloud-refresh-outline", "color": "purple",
        "hosts": (
            "123pan.com", "123pan.cn", "123.cn", "123684.com", "123685.com",
            "123865.com", "123912.com", "123592.com",
        ),
        "previewable": True, "preview_provider": "123",
        "password_param": "pwd", "aliases": ("123pan",),
        "markers": ("123云盘", "123pan"),
    },
    "quark": {
        "name": "夸克网盘", "icon": "mdi-cloud-outline", "color": "amber-darken-3",
        "hosts": ("quark.cn",),
        "previewable": True, "preview_provider": "quark",
        "text_password": True, "markers": ("夸克", "quark"),
    },
    "alipan": {
        "name": "阿里云盘", "icon": "mdi-cloud-sync-outline", "color": "blue",
        "hosts": ("alipan.com", "aliyundrive.com", "aliyundrive.net"),
        "previewable": True, "preview_provider": "alipan",
        "text_password": True, "aliases": ("ali", "aliyun"),
        "markers": ("阿里网盘", "aliyundrive"),
    },
    "baidu": {
        "name": "百度网盘", "icon": "mdi-cloud-circle-outline", "color": "indigo",
        "hosts": ("pan.baidu.com", "yun.baidu.com"),
        "previewable": True, "preview_provider": "baidu",
        "password_param": "pwd", "markers": ("百度云", "baidu"),
    },
    "uc": {
        "name": "UC网盘", "icon": "mdi-cloud-download-outline",
        "color": "deep-orange",
        "hosts": ("drive.uc.cn", "fast.uc.cn"),
        "previewable": True, "preview_provider": "uc",
        "password_param": "pwd", "aliases": ("ucpan",),
        "markers": ("UC网盘", "drive.uc.cn"),
    },
    "tianyi": {
        "name": "天翼云盘", "icon": "mdi-cloud-check-outline", "color": "teal",
        "hosts": ("cloud.189.cn",),
        "previewable": True, "preview_provider": "tianyi",
        "text_password": True, "aliases": ("189",), "markers": ("天翼",),
    },
    "guangya": {
        "name": "光鸭网盘", "icon": "mdi-cloud-outline", "color": "teal-darken-1",
        "hosts": ("guangyapan.com",),
        "previewable": True, "preview_provider": "guangya",
        "password_param": "code", "markers": ("光鸭",),
    },
    "yun139": {
        "name": "移动云盘", "icon": "mdi-cloud-arrow-up-outline",
        "color": "teal-darken-2",
        "hosts": ("yun.139.com", "caiyun.feixin.10086.cn"),
        "previewable": True, "preview_provider": "yun139",
        "text_password": True, "aliases": ("139",),
        "markers": ("中国移动云盘", "139云盘"),
    },
    "xunlei": {
        "name": "迅雷网盘", "icon": "mdi-flash",
        "color": "light-blue-darken-1", "markers": ("迅雷",),
    },
    "pikpak": {
        "name": "PikPak", "icon": "mdi-cloud-upload-outline",
        "color": "deep-purple",
    },
    "magnet": {
        "name": "磁力链接", "icon": "mdi-magnet", "color": "red-darken-1",
        "previewable": True, "aliases": ("magnetlink",),
        "markers": ("magnet:?", "磁力"),
    },
    "ed2k": {
        "name": "电驴链接", "icon": "mdi-link-variant",
        "color": "blue-grey-darken-1", "offline": True,
        "markers": ("ed2k://", "电驴"),
    },
    "torrent": {
        "name": "BT种子", "icon": "mdi-seed",
        "color": "deep-purple", "previewable": True,
    },
    "cloud": {
        "name": "网盘路径", "icon": "mdi-folder-network-outline",
        "color": "blue-grey", "offline": True,
    },
    "share": {
        "name": "网盘分享", "icon": "mdi-link-variant", "color": "blue-grey",
    },
    "other": {
        "name": "其他来源", "icon": "mdi-folder-outline", "color": "blue-grey",
    },
    "unknown": {
        "name": "未知", "icon": "mdi-help-circle-outline", "color": "blue-grey",
    },
}

#: 资源类型排序。
RESOURCE_TYPE_ORDER = (
    "115", "123", "quark", "uc", "guangya", "tianyi", "yun139", "alipan",
    "baidu", "ed2k", "magnet",
)
SUPPORTED_RESOURCE_TYPES = frozenset(RESOURCE_TYPE_ORDER)
RESOURCE_TYPE_PRIORITY = {
    resource_type: index
    for index, resource_type in enumerate(RESOURCE_TYPE_ORDER)
}

#: 外部类型别名。
TYPE_ALIASES = {
    alias: key
    for key, meta in _RESOURCE_TYPE_META.items()
    for alias in meta.get("aliases", ())
}
#: 链接域名。
TYPE_HOSTS = {
    key: set(meta["hosts"])
    for key, meta in _RESOURCE_TYPE_META.items()
    if meta.get("hosts")
}
SUPPORTED_CLOUD_TYPES = tuple(TYPE_HOSTS)

#: 前端展示字段。
_DISPLAY_FIELDS = ("name", "icon", "color", "previewable", "offline")
RESOURCE_TYPE_DISPLAY = {
    key: {
        field: meta[field] for field in _DISPLAY_FIELDS if field in meta
    }
    for key, meta in _RESOURCE_TYPE_META.items()
}
TYPE_NAMES = {
    key: meta["name"] for key, meta in _RESOURCE_TYPE_META.items()
}

#: PanSou 外部类型名。
PANSOU_RESOURCE_TYPES = (
    "aliyun", "quark", "guangya", "tianyi", "yun139",
    "115", "123", "baidu", "magnet", "ed2k",
)
SHARE_PASSWORD_QUERY_KEYS = {
    key: meta["password_param"]
    for key, meta in _RESOURCE_TYPE_META.items()
    if meta.get("password_param")
}
TEXT_PASSWORD_TYPES = frozenset(
    key for key, meta in _RESOURCE_TYPE_META.items()
    if meta.get("text_password")
)
PREVIEW_PROVIDER_KEYS = {
    key: meta["preview_provider"]
    for key, meta in _RESOURCE_TYPE_META.items()
    if meta.get("preview_provider")
}
PREVIEW_RESOURCE_TYPES = frozenset({*PREVIEW_PROVIDER_KEYS, "magnet"})
CANDIDATE_RESOURCE_TYPES = frozenset(TYPE_NAMES) - {"share"}

#: 文本识别标记。
_TYPE_TEXT_MARKERS = {
    key: tuple(dict.fromkeys(
        (_RESOURCE_TYPE_META[key]["name"],)
        + tuple(_RESOURCE_TYPE_META[key].get("hosts", ()))
        + tuple(_RESOURCE_TYPE_META[key].get("markers", ()))
    ))
    for key in RESOURCE_TYPE_ORDER
}

#: 不参与面板识别的占位类型。
_NON_RESOURCE_TYPE_KEYS = frozenset({"cloud", "share", "unknown", "other"})


def _panel_type_index() -> Dict[str, str]:
    """生成面板类型索引。"""
    index: Dict[str, str] = {}
    for key, meta in _RESOURCE_TYPE_META.items():
        if key in _NON_RESOURCE_TYPE_KEYS:
            continue
        index.setdefault(key.casefold(), key)
        name = str(meta.get("name") or "").strip()
        if name:
            index.setdefault(name.casefold(), key)
    return index


_PANEL_PLATFORM_TYPES = _panel_type_index()


def normalize_resource_type(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return TYPE_ALIASES.get(normalized, normalized)


def resource_type_from_url(value: str) -> str:
    """按链接识别资源类型。"""
    target = str(value or "").strip()
    lowered = target.casefold()
    if lowered.startswith("magnet:?"):
        return "magnet"
    if lowered.startswith("ed2k://"):
        return "ed2k"
    try:
        host = str(urlparse(target).hostname or "").casefold()
    except ValueError:
        return ""
    for resource_type, domains in TYPE_HOSTS.items():
        if any(host == domain or host.endswith(f".{domain}") for domain in domains):
            return resource_type
    return ""


def resource_type_from_text(value: str) -> str:
    """按类型值、链接或描述识别。"""
    text = str(value or "").strip()
    normalized = normalize_resource_type(text)
    if normalized in SUPPORTED_RESOURCE_TYPES:
        return normalized
    from_url = resource_type_from_url(text)
    if from_url:
        return from_url
    lowered = text.casefold()
    for resource_type in RESOURCE_TYPE_ORDER:
        markers = _TYPE_TEXT_MARKERS.get(resource_type, ())
        if any(marker in lowered for marker in markers):
            return resource_type
    return ""


def resource_type_from_panel(value: str) -> str:
    """识别面板类型。"""
    text = str(value or "").strip()
    if not text:
        return ""
    return resource_type_from_text(text) or _PANEL_PLATFORM_TYPES.get(
        text.casefold(), ""
    )


def resource_type_name(value: str, fallback: str = "") -> str:
    normalized = normalize_resource_type(value)
    return TYPE_NAMES.get(normalized) or str(fallback or normalized).strip()


def resource_type_catalog() -> list[dict]:
    """返回前端展示目录。"""
    ordered = list(RESOURCE_TYPE_ORDER) + [
        key for key in _RESOURCE_TYPE_META
        if key not in RESOURCE_TYPE_PRIORITY
    ]
    catalog = []
    for key in ordered:
        meta = RESOURCE_TYPE_DISPLAY.get(key)
        if not meta:
            continue
        catalog.append({
            "value": key,
            "name": meta["name"],
            "icon": meta.get("icon") or "mdi-folder-outline",
            "color": meta.get("color") or "blue-grey",
            "previewable": bool(meta.get("previewable")),
            "offline": bool(meta.get("offline")),
        })
    return catalog


def resource_type_aliases() -> dict:
    """返回别名表。"""
    return dict(TYPE_ALIASES)


def append_share_password(resource_type: str, target: str, password: str) -> str:
    """追加分享提取码。"""
    password = str(password or "").strip()
    normalized = normalize_resource_type(resource_type)
    if not password or normalized in {"magnet", "ed2k"}:
        return target
    key = SHARE_PASSWORD_QUERY_KEYS.get(normalized)
    if not key:
        if normalized in TEXT_PASSWORD_TYPES and "提取码" not in str(target):
            return f"{target} 提取码: {password}"
        return target
    if any(
            re.search(rf"[?&]{name}=", str(target), re.IGNORECASE)
            for name in SHARE_PASSWORD_QUERY_KEYS.values()
    ):
        return target
    return f"{target}{'&' if '?' in str(target) else '?'}{key}={password}"


def common_parent_id(rows: Any) -> str:
    """返回共同父目录。"""
    parents = {
        str((row or {}).get("parent_id") or (row or {}).get("pdir_fid") or "").strip()
        for row in (rows or [])
        if isinstance(row, Mapping)
    }
    parents.discard("")
    return parents.pop() if len(parents) == 1 else ""


def preview_item(item: Any) -> Dict[str, Any]:
    """归一化预览条目。"""
    if not isinstance(item, dict):
        item = getattr(item, "__dict__", {}) or {}
    name = next((str(item.get(key) or "").strip() for key in
                 ("name", "path", "file_name", "filename", "fileName")
                 if item.get(key)), "")
    size = next((item.get(key) for key in
                 ("size", "file_size", "fileSize")
                 if item.get(key) is not None), 0)
    is_dir = bool(item.get("is_dir") or item.get("is_folder")
                  or item.get("isDirectory") or item.get("type") == "folder")
    file_id = next((str(item.get(key) or "").strip() for key in
                    ("id", "file_id", "fileId", "fid", "cid")
                    if item.get(key) is not None), "")
    return {
        "id": file_id,
        "name": name or "未命名文件",
        "size": size or 0,
        "is_dir": is_dir,
        "can_enter": bool(is_dir and file_id),
    }
