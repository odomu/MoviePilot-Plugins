"""PanSou 资源搜索。"""

from typing import Any, Dict, List, Optional

from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType

from ...core import OwnerDelegator
from ...utils import parse_magnet_metadata


class PanSouSearchService(OwnerDelegator):
    """提供 PanSou 搜索实现。"""

    def _pansou_search(
            self,
            keyword: str,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int] = None,
            test_mode: bool = False,
    ) -> List[Dict]:
        """
        PanSou 搜索的通用逻辑

        :param keyword: 搜索关键词
        :return: 115分享与可提交到115离线下载的ED2K、Magnet资源列表
        """
        titles = [str(getattr(mediainfo, "title", "") or "").strip()]
        title_en = str(
            getattr(mediainfo, "original_title", "")
            or getattr(mediainfo, "original_name", "")
            or ""
        ).strip()
        if title_en:
            titles.append(title_en)
        search_results = self._pansou_client.search(
            keyword=keyword,
            cloud_types=self._pansou_cloud_types,
            channels=self._pansou_channels,
            plugins=self._pansou_plugins,
            limit=self._pansou_result_limit,
            expected_titles=titles,
            expected_year=getattr(mediainfo, "year", None),
            filter_config={} if test_mode else self._pansou_filter,
            refresh=self._pansou_refresh,
            concurrency=self._pansou_concurrency,
            test_mode=test_mode,
        )

        search_prefix = (
            f"[{self._search_label(mediainfo, media_type, season)}][PANSOU]"
        )
        if not search_results:
            logger.warning(
                f"{search_prefix} 搜索失败：关键词 '{keyword}'，"
                "接口未返回结果"
            )
            return []
        if search_results.get("error"):
            logger.warning(
                f"{search_prefix} 搜索失败：关键词 '{keyword}'，"
                f"原因：{search_results['error']}"
            )
            return []

        results = search_results.get("results", {})
        share_results = results.get("115网盘", [])
        magnet_results = results.get("磁力链接", [])
        ed2k_results = results.get("电驴链接", [])
        supported_groups = {"115网盘", "磁力链接", "电驴链接"}
        other_count = sum(
            len(items)
            for group_name, items in results.items()
            if group_name not in supported_groups and isinstance(items, list)
        )
        for resource in magnet_results:
            provider_text = " ".join(
                str(resource.get(key) or "").strip()
                for key in ("title", "description")
                if str(resource.get(key) or "").strip()
            )
            metadata = parse_magnet_metadata(resource.get("url", ""), provider_text)
            if not metadata:
                continue
            resource["magnet_metadata"] = metadata
            resource["info_hash"] = metadata["info_hash"]
            if metadata["display_name"]:
                resource["magnet_name"] = metadata["display_name"]
            if metadata["size"] and not resource.get("size"):
                resource["size"] = metadata["size"]
            if metadata["preview_episodes"]:
                resource["preview_episodes"] = metadata["preview_episodes"]
        if other_count:
            other_action = "保留展示" if test_mode else "不支持，已跳过"
        else:
            other_action = "无"
        candidates = (
            [item for group in results.values() if isinstance(group, list) for item in group]
            if test_mode else [*share_results, *ed2k_results, *magnet_results]
        )
        usable = [
            resource
            for resource in candidates
            if resource.get("resource_type") != "magnet" or resource.get("magnet_metadata")
            if test_mode or self._pansou_media_type_matches(resource, media_type)
        ]
        logger.debug(
            f"{search_prefix} 查询完成：原始条目={int(search_results.get('raw_count') or 0)}，"
            f"匹配链接={int(search_results.get('count') or 0)}，"
            f"可用候选={len(usable)}，其他网盘={other_count}（{other_action}），"
            f"耗时={int(search_results.get('elapsed_ms') or 0)}ms"
        )
        return usable

    @staticmethod
    def _pansou_media_type_matches(
            resource: Dict[str, Any], media_type: MediaType
    ) -> bool:
        """仅按 PanSou 返回的明确分类标签排除冲突类型，未知分类不误杀。"""
        tags = " ".join(str(tag) for tag in (resource.get("tags") or [])).lower()
        if not tags:
            return True
        movie_markers = ("电影", "影片", "movie")
        tv_markers = ("电视剧", "剧集", "连续剧", "tv series", "tv")
        has_movie = any(marker in tags for marker in movie_markers)
        has_tv = any(marker in tags for marker in tv_markers)
        if media_type == MediaType.MOVIE:
            return not (has_tv and not has_movie)
        return not (has_movie and not has_tv)

    def _search_pansou_movie(
            self,
            mediainfo: MediaInfo,
            test_mode: bool = False,
    ) -> List[Dict]:
        """
        仅使用 PanSou 搜索电影资源

        :param mediainfo: 媒体信息
        :return: 115网盘资源列表
        """
        if not self._pansou_client:
            logger.warning(f"PanSou 客户端未初始化，跳过 PanSou 查询")
            return []

        keyword = f"{mediainfo.title} {mediainfo.year or ''}".strip()
        results = self._pansou_search(
            keyword, mediainfo, MediaType.MOVIE, test_mode=test_mode
        )
        return results

    def _search_pansou_tv(
            self,
            mediainfo: MediaInfo,
            season: int,
            test_mode: bool = False,
    ) -> List[Dict]:
        """
        仅使用 PanSou 搜索电视剧资源

        :param mediainfo: 媒体信息
        :param season: 季号
        :return: 115网盘资源列表
        """
        if not self._pansou_client:
            logger.warning(f"PanSou 客户端未初始化，跳过 PanSou 查询")
            return []

        season_number = max(1, int(season or 1))
        keyword = str(mediainfo.title or "").strip()
        results = self._pansou_search(
            keyword, mediainfo, MediaType.TV, season_number, test_mode=test_mode
        )
        return results
