"""PanSou 搜索结果匹配与候选构造。"""

import re
from typing import Any, Dict, List

import unicodedata
from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType

from ..magnet import clear_cache, normalize_magnets
from ..matching import normalize_season
from ..types import PANSOU_RESOURCE_TYPES, normalize_resource_type, resource_type_name
from ...core import SearchQuery


class PanSouSearchService:
    """将 PanSou 协议响应转换为统一搜索候选。"""

    def __init__(
            self,
            client: Any = None,
            resource_types: Any = (),
            channels: Any = None,
            plugins: Any = None,
            filter_config: Any = None,
            concurrency: Any = None,
            result_limit: int = 10,
            refresh: bool = True,
            owner: Any = None,
    ):
        if client is not None and not hasattr(client, "_pansou_client"):
            self._client = client
            self._owner = owner
        else:
            self._owner = client or owner
            self._client = getattr(self._owner, "_pansou_client", None)

        self._resource_types = (
            tuple(resource_types)
            if resource_types
            else tuple(getattr(self._owner, "_resource_type_order_config", ()) or ())
        )
        self._channels = (
            list(channels)
            if channels is not None
            else list(getattr(self._owner, "_pansou_channels", ()) or ())
        )
        self._plugins = (
            list(plugins)
            if plugins is not None
            else list(getattr(self._owner, "_pansou_plugins", ()) or ())
        )
        self._filter = (
            dict(filter_config)
            if filter_config is not None
            else dict(getattr(self._owner, "_pansou_filter", {}) or {})
        )
        self._concurrency = (
            concurrency
            if concurrency is not None
            else getattr(self._owner, "_pansou_concurrency", None)
        )
        self._result_limit = max(1, int(result_limit or 10))
        self._refresh = bool(
            refresh
            if refresh is not None
            else getattr(self._owner, "_pansou_refresh", True)
        )
    _PUNCT_GAP_RE = re.compile(
        r"[\s\u3000:：·•.,，。!！?？（）【】\[\]/／\\＼-]+"
    )

    @staticmethod
    def _normalize_for_match(text: str) -> str:
        value = unicodedata.normalize("NFKC", str(text or ""))
        for old, new in (
                ("：", ":"), ("，", ","), ("（", "("), ("）", ")"),
                ("【", "["), ("】", "]"), ("！", "!"), ("？", "?"),
                ("–", "-"), ("—", "-"), ("…", "..."),
        ):
            value = value.replace(old, new)
        return re.sub(r"[\s\u3000]+", " ", value).strip().casefold()

    @classmethod
    def _compact_for_match(cls, text: str) -> str:
        return cls._PUNCT_GAP_RE.sub("", cls._normalize_for_match(text))

    @classmethod
    def _title_matches_search_key(cls, key: str, title: str) -> bool:
        if not key:
            return True
        if key in (title or ""):
            return True
        normalized_key = cls._normalize_for_match(key)
        normalized_title = cls._normalize_for_match(title)
        if normalized_key and normalized_key in normalized_title:
            return True
        compact_key = cls._compact_for_match(key)
        return bool(
            len(compact_key) >= 2
            and compact_key in cls._compact_for_match(title)
        )

    @staticmethod
    def _is_word_char(value: str) -> bool:
        return bool(value) and (value.isalnum() or "\u3400" <= value <= "\u9fff")

    @classmethod
    def _title_matches_media(
            cls,
            media_titles: List[str],
            media_year: Any,
            resource_title: str,
            strict_year: bool = True,
    ) -> bool:
        normalized_resource = cls._normalize_for_match(resource_title)
        if not normalized_resource:
            return False
        comparable_resource = cls._PUNCT_GAP_RE.sub(
            " ", normalized_resource
        ).strip()
        expected_year = str(media_year or "").strip()
        # 仅在 strict_year=True 且资源标题中明确出现了年份时才做年份过滤。
        # 对剧集搜索，调用方可传 strict_year=False 来放宽约束，避免误杀。
        if strict_year and expected_year:
            resource_years = set(
                re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", normalized_resource)
            )
            if resource_years and expected_year not in resource_years:
                return False
        for media_title in media_titles:
            comparable_title = cls._PUNCT_GAP_RE.sub(
                " ", cls._normalize_for_match(media_title)
            ).strip()
            if not comparable_title:
                continue
            start = 0
            while True:
                index = comparable_resource.find(comparable_title, start)
                if index < 0:
                    break
                previous = comparable_resource[index - 1] if index else ""
                prefix = comparable_resource[:index].rstrip()
                if (
                        not cls._is_word_char(previous)
                        or bool(expected_year and prefix.endswith(expected_year))
                ):
                    return True
                start = index + 1
        return False

    @staticmethod
    def _media_titles(mediainfo: MediaInfo) -> List[str]:
        if not mediainfo:
            return []
        titles = [
            getattr(mediainfo, "title", None),
            getattr(mediainfo, "original_title", None),
            getattr(mediainfo, "cn_name", None),
            getattr(mediainfo, "en_name", None),
        ]
        return list(dict.fromkeys(
            str(t).strip() for t in titles if t and str(t).strip()
        ))
    @staticmethod
    def _resource_type(resource: Dict[str, Any]) -> str:
        value = str(resource.get("resource_type") or "").strip().lower()
        return "alipan" if value == "aliyun" else value

    @classmethod
    def _normalize_results(
            cls,
            rows: Any,
            keyword: str,
            media_titles: List[str],
            media_year: Any,
            allowed_types: List[str],
            limit: int,
            strict_year: bool = True,
    ) -> Dict[str, List[Dict[str, Any]]]:
        groups: Dict[str, List[Dict[str, Any]]] = {}
        allowed = set(allowed_types)
        # 超出上限倍数后提前退出，避免大结果集浪费 CPU
        hard_cap = max(limit * 5, 50)
        total_matched = 0
        for item in rows if isinstance(rows, list) else []:
            if not isinstance(item, dict):
                continue
            item_title = re.sub(r"<[^>]+>", "", str(item.get("title") or ""))
            raw_tags = item.get("tags") or []
            if not isinstance(raw_tags, list):
                raw_tags = [raw_tags]
            tags = [str(tag).strip() for tag in raw_tags if str(tag or "").strip()]
            for link in item.get("links") or []:
                if not isinstance(link, dict):
                    continue
                title = re.sub(
                    r"<[^>]+>", "", str(link.get("work_title") or item_title)
                ).strip()
                if (
                        not cls._title_matches_search_key(keyword, title)
                        and not any(
                            cls._title_matches_search_key(value, title)
                            for value in media_titles
                        )
                ):
                    continue
                if media_titles and not cls._title_matches_media(
                        media_titles, media_year, title, strict_year=strict_year
                ):
                    continue
                resource_type = str(link.get("type") or "unknown").strip().lower()
                if allowed and resource_type not in allowed:
                    continue
                group = groups.setdefault(
                    resource_type_name(resource_type, resource_type), []
                )
                candidate = {
                    "url": link.get("url") or "",
                    "title": title,
                    "update_time": item.get("datetime") or "",
                    "resource_type": resource_type,
                    "tags": tags,
                }
                description = re.sub(
                    r"<[^>]+>", "", str(
                        link.get("description")
                        or item.get("description")
                        or item.get("content")
                        or item.get("message")
                        or ""
                    )
                ).strip()
                if description and description != title:
                    candidate["description"] = description
                source_url = str(
                    item.get("source_url") or item.get("message_url") or ""
                ).strip()
                if source_url.startswith(("http://", "https://")):
                    candidate["source_url"] = source_url
                password = str(link.get("password") or "").strip()
                if password:
                    candidate["password"] = password
                group.append(candidate)
                total_matched += 1
                if total_matched >= hard_cap:
                    break
            if total_matched >= hard_cap:
                break
        for group in groups.values():
            group.sort(key=lambda row: row.get("update_time", ""), reverse=True)
        return groups

    @staticmethod
    def _round_robin(
            groups: List[List[Dict[str, Any]]], limit: int
    ) -> List[Dict[str, Any]]:
        results = []
        offsets = [0] * len(groups)
        while groups and len(results) < limit:
            for index in range(len(groups) - 1, -1, -1):
                group = groups[index]
                offset = offsets[index]
                if offset >= len(group):
                    groups.pop(index)
                    offsets.pop(index)
                    continue
                results.append(group[offset])
                offsets[index] += 1
                if len(results) >= limit:
                    break
        return results

    def search(self, query: SearchQuery) -> List[Dict[str, Any]]:
        mediainfo = query.mediainfo
        media_type = query.media_type
        season = normalize_season(query.season) if media_type == MediaType.TV else None
        keyword = (
            str(mediainfo.title or "").strip()
            if media_type == MediaType.TV else
            f"{mediainfo.title} {mediainfo.year or ''}".strip()
        )
        media_title = str(getattr(mediainfo, "title", None) or "").strip()
        year_str = f" ({mediainfo.year})" if getattr(mediainfo, "year", None) else ""
        season_str = f" S{season:02d}" if season is not None else ""
        prefix = f"[{media_title}{year_str}{season_str}][PANSOU]"
        client = self._client or getattr(self._owner, "_pansou_client", None)
        if not client:
            logger.warning(f"{prefix} 客户端未初始化，跳过查询")
            return []
        titles = self._media_titles(mediainfo)
        limit = (
            max(1, int(query.result_limit or self._result_limit))
            if query.resource_list_mode else self._result_limit
        )
        allowed_types = (
            [
                "aliyun" if value == "alipan" else value
                for value in PANSOU_RESOURCE_TYPES
            ] if query.resource_list_mode else
            [
                "aliyun" if value == "alipan" else value
                for value in (self._resource_types or getattr(self._owner, "_resource_type_order_config", ()))
            ]
        )
        if not query.resource_list_mode and query.subscribe is not None:
            target_drive = str(getattr(self._owner, "_cloud_drive_key", "") or "").strip().lower()
            if target_drive:
                supported = set(getattr(self._owner, "_cloud_drive_resource_types", ()) or ())
                if not supported:
                    supported = {target_drive} | ({"magnet", "ed2k"} if target_drive == "115" else set())
                allowed_types = [
                    value for value in allowed_types
                    if normalize_resource_type(value) in supported
                ]
                if not allowed_types:
                    return []
        response = client.request_search(
            keyword=keyword,
            cloud_types=allowed_types,
            channels=[] if query.resource_list_mode else self._channels,
            plugins=[] if query.resource_list_mode else self._plugins,
            filter_config={} if query.resource_list_mode else self._filter,
            refresh=self._refresh,
            concurrency=self._concurrency,
            response_mode="merge" if query.resource_list_mode else "results",
        )
        raw_items = (response or {}).get("results") or []
        pure_title = str(mediainfo.title or "").strip()
        if (not raw_items) and keyword != pure_title and pure_title:
            logger.debug(f"{prefix} 带年份关键词 '{keyword}' 无结果，降级尝试纯标题 '{pure_title}'")
            fallback_res = client.request_search(
                keyword=pure_title,
                cloud_types=allowed_types,
                channels=[] if query.resource_list_mode else self._channels,
                plugins=[] if query.resource_list_mode else self._plugins,
                filter_config={} if query.resource_list_mode else self._filter,
                refresh=self._refresh,
                concurrency=self._concurrency,
                response_mode="merge" if query.resource_list_mode else "results",
            )
            if fallback_res and fallback_res.get("results"):
                response = fallback_res
                keyword = pure_title

        if not response or response.get("error"):
            reason = response.get("error") if response else "接口未返回结果"
            logger.warning(f"{prefix} 搜索失败：关键词 '{keyword}'，原因：{reason}")
            return []

        strict_year = media_type != MediaType.TV or season == 1
        groups = self._normalize_results(
            response.get("results"), keyword, titles,
            None if query.resource_list_mode else getattr(mediainfo, "year", None),
            allowed_types, limit, strict_year=strict_year,
        )
        # 用 candidate 的 resource_type 字段标准化后与配置对比。
        resource_type_set = {
            normalize_resource_type(t)
            for t in (allowed_types or self._resource_types or ())
            if t
        }
        grouped = [
            group for group in groups.values()
            if group and (
                query.resource_list_mode
                or not resource_type_set
                or normalize_resource_type(group[0].get("resource_type", "")) in resource_type_set
            )
        ]
        candidates = self._round_robin(grouped, limit)
        candidates = normalize_magnets(candidates, "pansou")
        usable = [
            resource for resource in candidates
            if (
                       query.resource_list_mode
                       or resource.get("resource_type") != "magnet"
                       or resource.get("magnet_metadata")
               )
               and self._media_type_matches(resource, media_type)
        ]
        logger.info(
            f"{prefix} 渠道统计：原始条目={int(response.get('raw_count') or 0)}，"
            f"网盘筛选后={int(response.get('filtered_count') or 0)}，"
            f"截断后={int(response.get('processed_count') or 0)}，"
            f"匹配链接={sum(len(group) for group in groups.values())}，"
            f"有效返回={len(usable)}"
        )
        return usable

    @staticmethod
    def _media_type_matches(
            resource: Dict[str, Any], media_type: MediaType
    ) -> bool:
        tags = " ".join(str(tag) for tag in (resource.get("tags") or [])).lower()
        if not tags:
            return True
        has_movie = any(marker in tags for marker in ("电影", "影片", "movie"))
        has_tv = any(
            marker in tags for marker in ("电视剧", "剧集", "连续剧", "tv series", "tv")
        )
        if media_type == MediaType.MOVIE:
            return not (has_tv and not has_movie)
        return not (has_movie and not has_tv)

    def clear_cache(self) -> int:
        client = self._client or getattr(self._owner, "_pansou_client", None)
        return clear_cache(client)
