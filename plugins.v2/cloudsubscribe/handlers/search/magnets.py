"""SeedHub、不太灵与聚影资源搜索。"""

from typing import Any, Dict, List, Optional

from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType

from ...core import OwnerDelegator
from ...search.butailing import ButailingError
from ...search.juying import JuyingError
from ...search.matching import unique_texts
from ...search.pinglian import PinglianError
from ...search.seedhub import SeedHubError
from ...utils import parse_magnet_metadata


class ExternalResourceSearchService(OwnerDelegator):
    """适配外部资源服务并统一输出 CloudSubscribe 候选。"""

    @staticmethod
    def _media_titles(mediainfo: MediaInfo) -> List[str]:
        return unique_texts((
            getattr(mediainfo, "title", ""),
            getattr(mediainfo, "original_title", ""),
            getattr(mediainfo, "original_name", ""),
        ))

    @staticmethod
    def _seedhub_keywords(
            titles: List[str], year: Any, media_type: MediaType, season: Optional[int]
    ) -> List[str]:
        keywords = []
        for title in titles:
            if media_type == MediaType.TV and season:
                keywords.extend((f"{title} S{season:02d}", f"{title} 第{season}季"))
            if year:
                keywords.append(f"{title} {year}")
            keywords.append(title)
        return unique_texts(keywords)

    @staticmethod
    def _butailing_keywords(titles: List[str]) -> List[str]:
        return unique_texts(titles)

    def _normalize_magnets(
            self,
            resources: List[Dict[str, Any]],
            source: str,
    ) -> List[Dict[str, Any]]:
        normalized = []
        seen = set()
        for resource in resources or []:
            url = str(resource.get("url") or "").strip()
            if not url.casefold().startswith("magnet:?"):
                normalized.append(resource)
                continue
            provider_text = " ".join(
                str(resource.get(key) or "").strip()
                for key in ("title", "quality")
                if str(resource.get(key) or "").strip()
            )
            metadata = parse_magnet_metadata(url, provider_text)
            if not metadata:
                continue
            info_hash = str(metadata.get("info_hash") or "").upper()
            if not info_hash or info_hash in seen:
                continue
            seen.add(info_hash)
            item = dict(resource)
            item.update({
                "source": source,
                "resource_type": "magnet",
                "pan_type": "magnet",
                "magnet_metadata": metadata,
                "info_hash": info_hash,
            })
            if metadata.get("display_name"):
                item["magnet_name"] = metadata["display_name"]
            if metadata.get("size") and not item.get("size"):
                item["size"] = metadata["size"]
            if metadata.get("preview_episodes"):
                item["preview_episodes"] = metadata["preview_episodes"]
            normalized.append(item)
        return normalized

    def _search_seedhub(
            self,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int],
            raise_errors: bool = False,
            test_mode: bool = False,
    ) -> List[Dict[str, Any]]:
        label = self._search_label(mediainfo, media_type, season)
        prefix = f"[{label}][SEEDHUB]"
        if not self._seedhub_client:
            return []
        titles = self._media_titles(mediainfo)
        try:
            resources = self._seedhub_client.search(
                keywords=self._seedhub_keywords(
                    titles, getattr(mediainfo, "year", None), media_type, season
                ),
                expected_titles=titles,
                expected_year=getattr(mediainfo, "year", None),
                media_type="tv" if media_type == MediaType.TV else "movie",
                season=season,
                limit=80 if test_mode else self._seedhub_result_limit,
                test_mode=test_mode,
            )
        except SeedHubError as error:
            logger.warning(f"{prefix} 搜索失败：{error}")
            if raise_errors:
                raise
            return []
        results = self._normalize_magnets(resources, "seedhub")
        logger.debug(f"{prefix} 查询完成：可用候选={len(results)}")
        return results

    def _search_butailing(
            self,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int],
            subscribe: Any = None,
            raise_errors: bool = False,
            test_mode: bool = False,
    ) -> List[Dict[str, Any]]:
        label = self._search_label(mediainfo, media_type, season)
        prefix = f"[{label}][BUTAILING]"
        if not self._butailing_client:
            return []
        titles = self._media_titles(mediainfo)
        douban_id = (
            getattr(mediainfo, "douban_id", None)
            or getattr(subscribe, "doubanid", None)
        )
        try:
            resources = self._butailing_client.search(
                keywords=self._butailing_keywords(titles),
                expected_titles=titles,
                expected_year=getattr(mediainfo, "year", None),
                media_type="tv" if media_type == MediaType.TV else "movie",
                season=season,
                douban_id=douban_id,
                limit=80 if test_mode else self._butailing_result_limit,
                test_mode=test_mode,
            )
        except ButailingError as error:
            logger.warning(f"{prefix} 搜索失败：{error}")
            if raise_errors:
                raise
            return []
        results = self._normalize_magnets(resources, "butailing")
        logger.debug(f"{prefix} 查询完成：可用候选={len(results)}")
        return results

    def _search_juying(
            self,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int],
            raise_errors: bool = False,
            test_mode: bool = False,
    ) -> List[Dict[str, Any]]:
        label = self._search_label(mediainfo, media_type, season)
        prefix = f"[{label}][JUYING]"
        if not self._juying_resources:
            return []
        titles = self._media_titles(mediainfo)
        try:
            resources = self._juying_resources.search(
                title=titles[0] if titles else "",
                alternative_titles=titles[1:],
                year=getattr(mediainfo, "year", None),
                media_type="tv" if media_type == MediaType.TV else "movie",
                tmdb_id=getattr(mediainfo, "tmdb_id", None),
                season=season,
                resource_type_order=self._juying_resource_types,
                limit=80 if test_mode else self._juying_result_limit,
                test_mode=test_mode,
            )
        except JuyingError as error:
            logger.warning(f"{prefix} 搜索失败：{error}")
            if raise_errors:
                raise
            return []
        results = self._normalize_magnets(resources, "juying")
        logger.debug(f"{prefix} 查询完成：可用候选={len(results)}")
        return results

    def _search_pinglian(
            self,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int],
            raise_errors: bool = False,
            test_mode: bool = False,
    ) -> List[Dict[str, Any]]:
        label = self._search_label(mediainfo, media_type, season)
        prefix = f"[{label}][PINGLIAN]"
        if not self._pinglian_client:
            return []
        titles = self._media_titles(mediainfo)
        logger.debug(
            f"{prefix} 开始查询：关键词={','.join(titles) or '无'}，"
            f"模式={'测试' if test_mode else '正式'}，"
            f"资源类型={'全部（测试）' if test_mode else '/'.join(self._resource_type_order_config) or '无'}"
        )
        try:
            resources = self._pinglian_client.search(
                title=titles[0] if titles else "",
                alternative_titles=titles[1:],
                year=getattr(mediainfo, "year", None),
                resource_type_order=self._resource_type_order_config,
                limit=80 if test_mode else self._pinglian_result_limit,
                test_mode=test_mode,
            )
        except PinglianError as error:
            logger.warning(f"{prefix} 搜索失败：{error}")
            if raise_errors:
                raise
            return []
        logger.debug(f"{prefix} 查询完成：可用候选={len(resources)}")
        return resources
