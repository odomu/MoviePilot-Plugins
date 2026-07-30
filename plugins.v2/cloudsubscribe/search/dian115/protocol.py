"""Dian115 前端资源路由协议。"""

import base64
from typing import Any

_KEY_VERSION = 1
_KEY_MASK = (55, 161, 92, 233)
_VALID_SOURCES = {"tmdb", "resource", "share"}
_VALID_MEDIA_TYPES = {"movie", "tv", "other"}


def encode_resource_key(
        source: str,
        media_type: str,
        resource_id: Any,
        season: Any = 0,
) -> str:
    """ XOR + Base64URL 资源键编码。"""
    normalized_source = str(source or "").strip().lower()
    normalized_type = str(media_type or "").strip().lower()
    normalized_id = int(resource_id)
    normalized_season = int(season or 0)
    raw = (
        f"{_KEY_VERSION}|{normalized_source}|{normalized_type}|"
        f"{normalized_id}|{normalized_season}"
    ).encode("utf-8")
    encoded = bytes(
        value ^ _KEY_MASK[index % len(_KEY_MASK)]
        for index, value in enumerate(raw)
    )
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def resource_path(media_type: str, tmdb_id: Any, season: Any = 0) -> str:
    """生成 TMDB 媒体详情路由。"""
    return f"/r/{encode_resource_key('tmdb', media_type, tmdb_id, season)}"


def share_path(share_id: Any) -> str:
    """生成单条分享的解锁路由。"""
    return f"/s/{encode_resource_key('share', 'other', share_id)}"
