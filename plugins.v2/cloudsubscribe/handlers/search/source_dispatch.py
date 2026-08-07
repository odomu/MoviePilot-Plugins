"""搜索源分发、单源缓存后处理与测试入口。"""

import time
from typing import Any, Dict, List, Optional

from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType

from ...core import OwnerDelegator


class SourceDispatchService(OwnerDelegator):
    """把来源选择和结果准备从搜索主协调器中隔离出来。"""

    def _run_source_search(
            self,
            source: str,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            target_episode_air_dates: Optional[Dict[int, str]] = None,
            subscribe: Any = None,
            test_mode: bool = False,
    ) -> Optional[List[Dict]]:
        if source == "hdhive":
            return self._search_hdhive(
                mediainfo,
                media_type,
                season,
                target_episodes=target_episodes,
                target_episode_air_dates=target_episode_air_dates,
                subscribe=subscribe,
                test_mode=test_mode,
            )
        if source == "dian115":
            return self._search_dian115(
                mediainfo,
                media_type,
                season,
                target_episodes=target_episodes,
                subscribe=subscribe,
                test_mode=test_mode,
            )
        if source == "pansou":
            if media_type == MediaType.MOVIE:
                return self._search_pansou_movie(mediainfo, test_mode=test_mode)
            return self._search_pansou_tv(mediainfo, season, test_mode=test_mode)
        if source == "seedhub":
            return self._search_seedhub(
                mediainfo,
                media_type,
                season,
                raise_errors=test_mode,
                test_mode=test_mode,
            )
        if source == "butailing":
            return self._search_butailing(
                mediainfo,
                media_type,
                season,
                subscribe=subscribe,
                raise_errors=test_mode,
                test_mode=test_mode,
            )
        if source == "juying":
            return self._search_juying(
                mediainfo,
                media_type,
                season,
                raise_errors=test_mode,
                test_mode=test_mode,
            )
        if source == "pinglian":
            return self._search_pinglian(
                mediainfo,
                media_type,
                season,
                raise_errors=test_mode,
                test_mode=test_mode,
            )
        if source == "online_docs":
            titles = []
            for value in (
                    getattr(mediainfo, "title", ""),
                    getattr(mediainfo, "original_title", ""),
                    getattr(mediainfo, "original_name", ""),
            ):
                text = str(value or "").strip()
                if text and text not in titles:
                    titles.append(text)
            return self._online_docs_client.search(
                keyword=titles[0] if titles else "",
                alternative_titles=titles[1:],
                limit=100 if test_mode else 20,
                test_mode=test_mode,
            )
        raise ValueError("不支持的搜索渠道")

    def _prepare_source_results(
            self,
            results: List[Dict],
            source: str,
            mediainfo: MediaInfo,
            subscribe: Any,
            season: Optional[int],
            target_episodes: Optional[List[int]],
            apply_platform_rules: bool,
    ) -> List[Dict]:
        for result in results:
            result.setdefault("source", source)
        ordered = self._prefilter_resource_order(
            results, season=season, target_episodes=target_episodes
        )
        if source == "hdhive":
            return sorted(ordered, key=self._hdhive_update_sort_key)
        if not apply_platform_rules:
            return ordered
        return self._filter_by_platform_rules(
            ordered,
            mediainfo,
            subscribe,
            season=season,
            target_episodes=target_episodes,
        )

    def test_source(
            self,
            source: str,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int] = None,
    ) -> List[Dict]:
        """强制刷新单个来源，仅返回候选，不进入转存或离线任务。"""
        source = str(source or "").strip().lower()
        if source not in self._SUPPORTED_SOURCES:
            raise ValueError("不支持的搜索渠道")
        cache_key = self._search_cache_key(
            source, mediainfo, media_type, season, None, None
        )
        try:
            self._search_cache.pop(cache_key)
        except KeyError:
            pass
        client = {
            "seedhub": self._seedhub_client,
            "butailing": self._butailing_client,
            "juying": self._juying_resources,
            "pinglian": self._pinglian_client,
        }.get(source)
        if client:
            client.clear_cache()
        results = self._run_source_search(
            source, mediainfo, media_type, season, test_mode=True
        ) or []
        for result in results:
            result.setdefault("source", source)
        return list(results)

    def search_single_source(
            self,
            source: str,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            target_episode_air_dates: Optional[Dict[int, str]] = None,
            subscribe: Any = None,
            apply_platform_rules: bool = True,
    ) -> List[Dict]:
        """使用指定来源查询并准备候选资源。"""
        source = str(source or "").strip().lower()
        if self._stop_requested():
            return []
        if source not in self._SUPPORTED_SOURCES:
            search_label = self._search_label(mediainfo, media_type, season)
            logger.warning(f"[{search_label}][{source.upper()}] 未知的搜索源")
            return []
        cache_key = self._search_cache_key(
            source, mediainfo, media_type, season, target_episodes, subscribe
        )
        search_label = self._search_label(mediainfo, media_type, season)
        use_cache = source != "juying"
        results = (
            self._get_cached_results(cache_key, source, search_label)
            if use_cache else None
        )
        if results is not None:
            return self._prepare_source_results(
                results,
                source,
                mediainfo,
                subscribe,
                season,
                target_episodes,
                apply_platform_rules,
            )

        external_started = time.monotonic()
        try:
            results = self._run_source_search(
                source,
                mediainfo,
                media_type,
                season,
                target_episodes,
                target_episode_air_dates,
                subscribe,
            )
        finally:
            self._record_search_metric(source, "external_calls")
            self._record_search_metric(
                source,
                "external_elapsed_ms",
                int((time.monotonic() - external_started) * 1000),
            )
        if self._stop_requested() or results is None:
            return []
        label = f"[{search_label}][{source.upper()}]"
        if use_cache:
            self._set_cached_results(cache_key, label, results, source=source)
        return self._prepare_source_results(
            results,
            source,
            mediainfo,
            subscribe,
            season,
            target_episodes,
            apply_platform_rules,
        )
