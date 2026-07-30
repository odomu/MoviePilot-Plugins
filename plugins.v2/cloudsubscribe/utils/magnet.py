"""Magnet URI 元数据解析。"""

import base64
import hashlib
import io
from pathlib import Path
from typing import Any, Dict

import httpx
from torf import Magnet, Torrent, TorfError

from app.core.cache import TTLCache
from app.core.config import settings
from app.core.metainfo import MetaInfo

_TORRENT_CACHE_TTL = 30 * 60
DEFAULT_METADATA_URL_TEMPLATE = "https://itorrents.org/torrent/{info_hash}.torrent"
_TORRENT_METADATA_CACHE = TTLCache(
    region="cloudsubscribe:magnet_metadata",
    maxsize=256,
    ttl=_TORRENT_CACHE_TTL,
)


def clear_magnet_metadata_cache() -> int:
    """清理 Magnet 元数据缓存并返回移除数量。"""
    count = len(_TORRENT_METADATA_CACHE)
    _TORRENT_METADATA_CACHE.clear()
    return count


def _extract_preview_episodes(
        display_name: str,
        provider_text: str,
        torrent_files: list,
) -> Dict[str, list]:
    """使用 MoviePilot 元数据识别汇总资源包含的季集。"""
    episodes: Dict[str, set] = {}
    resource_seasons = [1]
    resource_title = display_name or provider_text
    if resource_title:
        resource_meta = MetaInfo(
            title=resource_title,
            subtitle=provider_text if display_name and provider_text else None,
        )
        resource_seasons = resource_meta.season_list or [1]
        if resource_meta.episode_list:
            for season in resource_seasons:
                episodes.setdefault(str(season), set()).update(resource_meta.episode_list)

    for file in torrent_files:
        file_path = Path(str(file))
        if file_path.suffix.lower() not in settings.RMT_MEDIAEXT:
            continue
        file_meta = MetaInfo(file_path.name)
        if not file_meta.episode_list:
            continue
        file_seasons = (
            file_meta.season_list
            if file_meta.begin_season is not None
            else resource_seasons
        )
        for season in file_seasons:
            episodes.setdefault(str(season), set()).update(file_meta.episode_list)

    return {season: sorted(values) for season, values in episodes.items()}


def _fetch_torrent_metadata(
        info_hash: str, timeout: float, url_template: str
) -> Dict[str, Any]:
    """从固定 torrent 缓存读取并校验元数据，不请求任意提供者 URL。"""
    cache_key = hashlib.sha1(
        f"{url_template}|{info_hash}".encode("utf-8")
    ).hexdigest()
    cached = _TORRENT_METADATA_CACHE.get(cache_key)
    if isinstance(cached, dict):
        return dict(cached)
    metadata: Dict[str, Any] = {}
    try:
        with httpx.stream(
                "GET",
                url_template.format(info_hash=info_hash),
                headers={"User-Agent": "MoviePilot-CloudSubscribe/2.0"},
                timeout=max(1, float(timeout)),
                follow_redirects=True,
        ) as response:
            response.raise_for_status()
            chunks = []
            total_size = 0
            for chunk in response.iter_bytes():
                total_size += len(chunk)
                if total_size > 10 * 1024 * 1024:
                    raise ValueError("torrent 元数据响应过大")
                chunks.append(chunk)
            payload = b"".join(chunks)
        if not payload or len(payload) > 10 * 1024 * 1024:
            raise ValueError("torrent 元数据响应为空或过大")
        torrent = Torrent.read_stream(io.BytesIO(payload), validate=True)
        if str(torrent.infohash or "").upper() != info_hash:
            raise ValueError("torrent 元数据 Info Hash 不匹配")
        filepaths = [str(file) for file in torrent.files]
        if not filepaths and torrent.name:
            filepaths = [str(torrent.name)]
        metadata = {
            "display_name": str(torrent.name or "").strip(),
            "size": int(torrent.size or 0),
            "torrent_files": filepaths,
            "metadata_source": "itorrents",
        }
    except (OSError, httpx.HTTPError, TorfError, ValueError):
        metadata = {}
    _TORRENT_METADATA_CACHE[cache_key] = dict(metadata)
    return metadata


def parse_magnet_metadata(
        uri: str,
        provider_text: str = "",
        fetch_info: bool = False,
        timeout: float = 8,
        metadata_url_template: str = DEFAULT_METADATA_URL_TEMPLATE,
) -> Dict[str, Any]:
    """解析 URI 和提供者文本；可按需短时获取完整 torrent 元数据。"""
    try:
        magnet = Magnet.from_string(str(uri or "").strip())
    except (TorfError, TypeError, ValueError):
        return {}

    info_hash = str(magnet.infohash or "").upper()
    if len(info_hash) == 32:
        try:
            info_hash = base64.b16encode(base64.b32decode(info_hash)).decode("ascii")
        except (ValueError, TypeError):
            return {}

    torrent_files = []
    metadata_source = "uri" if magnet.dn else ""
    if fetch_info and not magnet.dn:
        fetched = _fetch_torrent_metadata(
            info_hash,
            timeout,
            str(metadata_url_template or DEFAULT_METADATA_URL_TEMPLATE),
        )
        if fetched:
            magnet.dn = fetched["display_name"]
            magnet.xl = fetched["size"] or None
            torrent_files = fetched["torrent_files"]
            metadata_source = fetched["metadata_source"]

    provider_text = str(provider_text or "").strip()
    display_name = str(magnet.dn or "").strip()
    preview_episodes = _extract_preview_episodes(
        display_name=display_name,
        provider_text=provider_text,
        torrent_files=torrent_files,
    )

    return {
        "info_hash": info_hash,
        "display_name": display_name,
        "size": int(magnet.xl or 0),
        "trackers": [str(value) for value in magnet.tr],
        "webseeds": [str(value) for value in magnet.ws],
        "keywords": list(magnet.kt or []),
        "torrent_files": torrent_files,
        "metadata_available": bool(magnet.dn or torrent_files),
        "provider_metadata_available": bool(provider_text),
        "metadata_source": metadata_source,
        "preview_episodes": preview_episodes,
    }
