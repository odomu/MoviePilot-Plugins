"""
TMDB 剧集解析、日历与订阅元数据识别修复服务。
"""
import datetime
import re
from concurrent.futures import Future
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Set, Tuple

from app.core.config import settings
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.types import MediaType
from app.utils.http import RequestUtils

from ...core import OwnerDelegator
from ...core.media import (
    apply_media_identity,
    legacy_media_ids,
    media_identity,
    recognize_media,
    search_medias,
    tmdb_id_of,
    tmdb_identity_update,
)
from ...utils.cache import normalize_platform_cache_key


class _TmdbSeasonPageParser(HTMLParser):
    """解析 TMDB 季页面中服务端渲染的剧集卡片。"""

    def __init__(self, season: int):
        super().__init__(convert_charrefs=True)
        self.season = int(season)
        self.episodes: Dict[int, str] = {}
        self._card_depth = 0
        self._card_episode = 0
        self._episode_depth = 0
        self._date_depth = 0
        self._text: List[str] = []
        self._field = ""

    @staticmethod
    def _classes(attrs) -> Set[str]:
        return set(str(dict(attrs).get("class") or "").split())

    def _finish_card(self) -> None:
        if self._card_episode > 0 and self._text:
            raw = "".join(self._text).strip()
            match = re.search(r"(\d{4})\s*[年/-]\s*(\d{1,2})\s*[月/-]\s*(\d{1,2})", raw)
            if match:
                self.episodes[self._card_episode] = (
                    f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
                )
        self._card_episode = 0
        self._text = []
        self._field = ""

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        classes = self._classes(attrs)
        if tag == "div" and "card" in classes:
            if self._card_depth:
                self._finish_card()
            self._card_depth = 1
            url = str(attrs_dict.get("data-url") or "")
            match = re.search(r"/season/(\d+)/episode/(\d+)", url)
            self._card_episode = int(match.group(2)) if match and int(match.group(1)) == self.season else 0
            return
        if not self._card_depth:
            return
        if tag == "div":
            self._card_depth += 1
        if tag in {"span", "div"} and "episode_number" in classes:
            self._episode_depth = self._card_depth
            self._field = "episode"
            self._text = []
        elif tag in {"span", "div"} and "date" in classes:
            self._date_depth = self._card_depth
            self._field = "date"
            self._text = []

    def handle_endtag(self, tag):
        if not self._card_depth:
            return
        if self._field == "episode" and self._card_depth == self._episode_depth:
            try:
                self._card_episode = int("".join(self._text).strip())
            except ValueError:
                self._card_episode = 0
            self._field = ""
        elif self._field == "date" and self._card_depth == self._date_depth:
            self._field = ""
        if tag == "div":
            self._card_depth -= 1
            if not self._card_depth:
                self._finish_card()

    def handle_data(self, data):
        if self._field in {"episode", "date"}:
            self._text.append(data)

    def close(self):
        super().close()
        self._finish_card()


class SyncMetadataService(OwnerDelegator):
    """负责 TMDB 刮削、季页面解析、日历与 TMDBID 修复。"""

    @staticmethod
    def _calendar_date(value: Any) -> Optional[datetime.date]:
        normalized = str(value or "").strip()[:10]
        if not normalized:
            return None
        try:
            return datetime.date.fromisoformat(normalized)
        except ValueError:
            return None

    def _subscribe_defer_key(self, subscribe: Any) -> Tuple[Any, ...]:
        media_type = str(getattr(subscribe, "type", "") or "")
        is_tv = media_type == MediaType.TV.value
        return (
            int(getattr(subscribe, "id", 0) or 0),
            media_type,
            media_identity(subscribe),
            str(getattr(subscribe, "name", "") or ""),
            str(getattr(subscribe, "year", "") or ""),
            int(getattr(subscribe, "season", 1) or 1) if is_tv else 0,
            int(getattr(subscribe, "start_episode", 1) or 1) if is_tv else 0,
            int(getattr(subscribe, "total_episode", 0) or 0) if is_tv else 0,
            self._is_cloud_upgrade_subscribe(subscribe),
        )

    def defer_subscribe_until(
            self,
            subscribe: Any,
            defer_until: datetime.date,
            reason: str,
    ) -> bool:
        """缓存明确的未来上映/播出日期，日期到达后自动失效。"""
        if not defer_until or defer_until <= datetime.date.today():
            return False
        cache_key = normalize_platform_cache_key(
            self._subscribe_defer_key(subscribe)
        )
        with self._subscribe_defer_lock:
            self._subscribe_defer_cache.set(cache_key, {
                "defer_until": defer_until.isoformat(),
                "reason": str(reason or "尚未上映或播出"),
            })
        logger.debug(
            f"订阅已延期至 {defer_until.isoformat()}："
            f"{getattr(subscribe, 'name', '')}，{reason}"
        )
        return True

    def get_subscribe_defer(self, subscribe: Any) -> Optional[Dict[str, str]]:
        """返回仍有效的订阅延期信息；订阅范围变化或日期到达时立即失效。"""
        cache_key = normalize_platform_cache_key(
            self._subscribe_defer_key(subscribe)
        )
        today = datetime.date.today()
        with self._subscribe_defer_lock:
            entry = self._subscribe_defer_cache.get(cache_key)
            if entry:
                defer_until = self._calendar_date(entry.get("defer_until"))
                if defer_until and defer_until > today:
                    return dict(entry)
                self._subscribe_defer_cache.delete(cache_key)
        return None

    def _tmdb_season_web_episodes(self, tmdb_id: int, season: int) -> Dict[int, str]:
        """读取 TMDB 季网页的真实卡片，绕过 API/平台缓存的滞后。"""
        url = f"https://www.themoviedb.org/tv/{int(tmdb_id)}/season/{int(season)}"
        response = RequestUtils(
            proxies=settings.PROXY,
            timeout=5,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 Chrome/136.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
        ).get_res(url=url)
        status = int(getattr(response, "status_code", 0) or 0)
        if not response or status != 200:
            logger.debug(
                f"TMDB 季网页请求失败：S{season:02d}，HTTP {status or '-'}"
            )
            return {}
        parser = _TmdbSeasonPageParser(season)
        parser.feed(str(getattr(response, "text", "") or ""))
        parser.close()
        logger.debug(
            f"TMDB 季网页解析完成：TV {tmdb_id} S{season:02d}，"
            f"获取 {len(parser.episodes)} 集，最大集数 E{max(parser.episodes, default=0):02d}"
        )
        return parser.episodes

    def get_tv_subscribe_calendar(
            self,
            subscribe: Any,
            tmdb_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """读取 TMDB 季网页并缓存当前订阅目标集的播出状态。"""
        if str(getattr(subscribe, "type", "") or "") != MediaType.TV.value:
            return None
        tmdb_id = int(tmdb_id or tmdb_id_of(subscribe) or 0)
        season = int(getattr(subscribe, "season", 1) or 1)
        start_episode = int(getattr(subscribe, "start_episode", 1) or 1)
        total_episode = int(getattr(subscribe, "total_episode", 0) or 0)
        if tmdb_id <= 0 or total_episode < start_episode:
            return None

        cache_key = normalize_platform_cache_key(
            (*self._subscribe_defer_key(subscribe), tmdb_id)
        )
        today = datetime.date.today()
        checked_on = today.isoformat()
        with self._subscribe_defer_lock:
            entry = self._subscribe_calendar_cache.get(cache_key)
            if (
                    entry
                    and entry.get("checked_on") == checked_on
                    and entry.get("source") == "tmdb_web"
            ):
                return dict(entry)
            if entry:
                self._subscribe_calendar_cache.delete(cache_key)

        try:
            web_air_dates = self._timed_sync_call(
                "tmdb_season_web",
                self._tmdb_season_web_episodes,
                tmdb_id,
                season,
            )
        except Exception as error:
            logger.debug(
                f"{getattr(subscribe, 'name', '')} S{season:02d} "
                f"读取 TMDB 季网页失败：{error}"
            )
            return None
        if not web_air_dates:
            logger.debug(
                f"{getattr(subscribe, 'name', '')} S{season:02d} "
                "TMDB 季网页未解析到剧集播出日期，跳过播出过滤"
            )
            return None

        expected_episodes = set(range(start_episode, total_episode + 1))
        season_known_air_dates: Dict[int, str] = {}
        season_aired_episodes: Set[int] = set()
        known_air_dates: Dict[int, str] = {}
        aired_episodes: Set[int] = set()
        for episode_number, raw_air_date in web_air_dates.items():
            try:
                episode_number = int(episode_number)
            except (TypeError, ValueError):
                continue
            air_date = self._calendar_date(raw_air_date)
            if episode_number <= 0 or not air_date:
                continue
            season_known_air_dates[episode_number] = air_date.isoformat()
            if air_date <= today:
                season_aired_episodes.add(episode_number)
            if episode_number not in expected_episodes:
                continue
            known_air_dates[episode_number] = air_date.isoformat()
            if air_date <= today:
                aired_episodes.add(episode_number)

        future_air_dates = {
            episode: air_date
            for episode, value in known_air_dates.items()
            if (air_date := self._calendar_date(value)) and air_date > today
        }
        last_aired_episode = max(season_aired_episodes, default=0)
        future_boundary_episode = min(
            (
                episode
                for episode in future_air_dates
                if episode > last_aired_episode
            ),
            default=0,
        )
        # TMDB 只返回到当前已公布集数时，订阅总集数后面的未知尾部同样不能搜索。
        # 只在至少存在一条可靠播出日期时建立边界，避免 TMDB 整季无数据时误跳过。
        unreleased_boundary_episode = min(
            (
                episode
                for episode in expected_episodes
                if season_known_air_dates and episode > last_aired_episode
            ),
            default=0,
        )
        boundary_reason = ""
        if unreleased_boundary_episode:
            boundary_reason = (
                "future"
                if unreleased_boundary_episode in future_air_dates
                else "unknown_tail"
            )
        unreleased_episodes = {
            episode
            for episode in expected_episodes
            if episode in future_air_dates
               or (
                       unreleased_boundary_episode > 0
                       and episode >= unreleased_boundary_episode
               )
        }
        all_targets_future = bool(
            expected_episodes and unreleased_episodes == expected_episodes
        )
        next_air_date = min(future_air_dates.values(), default=None)
        defer_until = next_air_date if all_targets_future else None
        entry = {
            "source": "tmdb_web",
            "checked_on": checked_on,
            "known_air_dates": known_air_dates,
            "aired_episodes": sorted(aired_episodes),
            "aired_episode_air_dates": {
                episode: known_air_dates[episode]
                for episode in sorted(aired_episodes)
            },
            "unknown_episodes": sorted(expected_episodes - set(known_air_dates)),
            "unreleased_episodes": sorted(unreleased_episodes),
            "future_boundary_episode": future_boundary_episode,
            "unreleased_boundary_episode": unreleased_boundary_episode,
            "unreleased_boundary_reason": boundary_reason,
            "next_air_date": next_air_date.isoformat() if next_air_date else "",
            "all_targets_future": all_targets_future,
            "defer_until": defer_until.isoformat() if defer_until else "",
        }
        with self._subscribe_defer_lock:
            self._subscribe_calendar_cache.set(cache_key, entry)

        if defer_until:
            self.defer_subscribe_until(
                subscribe,
                defer_until,
                f"目标剧集最早于 {defer_until.isoformat()} 播出",
            )
        return dict(entry)

    @staticmethod
    def _tmdb_id_from_media(value: Any) -> int:
        raw_id = (
            value.get("id") or value.get("tmdb_id")
            if isinstance(value, dict)
            else getattr(value, "tmdb_id", None)
        )
        try:
            return max(0, int(raw_id or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _normalized_media_title(value: Any) -> str:
        return re.sub(r"[\W_]+", "", str(value or "").casefold())

    @classmethod
    def _tmdb_title_variants(cls, value: Any, media_type: MediaType) -> Set[str]:
        """生成可用于订阅回填的标题变体，去掉剧集季标记和常见宣传后缀。"""
        raw = str(value or "").strip()
        if not raw:
            return set()
        values = {raw}
        if media_type == MediaType.TV:
            values.add(re.sub(
                r"(?:\s*第\s*\d+\s*季|\s*第[一二三四五六七八九十百]+季|\s*season\s*\d+|\s*s\d{1,2})$",
                "",
                raw,
                flags=re.IGNORECASE,
            ).strip())
        return {
            cls._normalized_media_title(item)
            for item in values
            if cls._normalized_media_title(item)
        }

    def _match_tmdb_search_candidate(
            self,
            subscribe: Any,
            media_type: MediaType,
            candidates: List[Any],
    ) -> int:
        """按类型、年份和标题别名评分，只有最高分唯一时才回填。"""
        expected_titles = self._tmdb_title_variants(
            getattr(subscribe, "name", ""), media_type
        )
        expected_year = str(getattr(subscribe, "year", "") or "").strip()
        subscribe_meta = MetaInfo(str(getattr(subscribe, "name", "") or ""))
        season_specific_tv = (
                media_type == MediaType.TV
                and subscribe_meta.begin_season is not None
        )
        scores: Dict[int, int] = {}
        for candidate in candidates or []:
            candidate_type = getattr(candidate, "type", None)
            if candidate_type != media_type:
                continue
            candidate_year = str(getattr(candidate, "year", "") or "").strip()
            if (
                    not season_specific_tv
                    and expected_year
                    and candidate_year
                    and candidate_year != expected_year
            ):
                continue
            candidate_titles: Set[str] = set()
            for field in (
                    "title", "original_title", "en_title", "hk_title",
                    "tw_title", "sg_title", "original_name", "name",
            ):
                candidate_titles.update(
                    self._tmdb_title_variants(getattr(candidate, field, ""), media_type)
                )
            names = getattr(candidate, "names", None) or []
            for name in names:
                candidate_titles.update(self._tmdb_title_variants(name, media_type))
            if not expected_titles or not expected_titles.intersection(candidate_titles):
                continue
            if tmdb_id := self._tmdb_id_from_media(candidate):
                score = 3 if self._tmdb_title_variants(
                    getattr(candidate, "title", ""), media_type
                ).intersection(expected_titles) else 2
                scores[tmdb_id] = max(scores.get(tmdb_id, 0), score)
        if not scores:
            return 0
        best_score = max(scores.values())
        best_ids = [tmdb_id for tmdb_id, score in scores.items() if score == best_score]
        return best_ids[0] if len(best_ids) == 1 else 0

    def repair_subscribe_tmdb_id(self, subscribe: Any) -> bool:
        """在订阅收集阶段使用平台媒体链修复缺失的 TMDB ID。"""
        if tmdb_id_of(subscribe):
            return True

        media_type = {
            MediaType.MOVIE.value: MediaType.MOVIE,
            MediaType.TV.value: MediaType.TV,
        }.get(str(getattr(subscribe, "type", "") or ""))
        subscribe_id = int(getattr(subscribe, "id", 0) or 0)
        if not media_type or subscribe_id <= 0:
            return False

        tmdb_id = 0
        candidates: List[Any] = []
        # 同一豆瓣身份可能已有其他订阅完成 TMDB 回填，优先复用该稳定映射，
        # 避免被不同语言标题、季标题或年份差异误判为无匹配。
        source_douban_id = str(
            legacy_media_ids(subscribe).get("doubanid") or ""
        ).strip()
        if source_douban_id:
            for candidate in SubscribeOper().list() or []:
                if int(getattr(candidate, "id", 0) or 0) == subscribe_id:
                    continue
                candidate_douban_id = str(
                    legacy_media_ids(candidate).get("doubanid") or ""
                ).strip()
                if candidate_douban_id != source_douban_id:
                    continue
                candidate_type = str(getattr(candidate, "type", "") or "")
                if candidate_type != getattr(subscribe, "type", ""):
                    continue
                tmdb_id = self._tmdb_id_from_media({
                    "id": tmdb_id_of(candidate)
                })
                if tmdb_id:
                    logger.debug(
                        f"订阅复用同豆瓣身份的 TMDB 映射："
                        f"{getattr(subscribe, 'name', '')} -> {tmdb_id}"
                    )
                    break

        source_lookups = (
            (
                "doubanid",
                "get_tmdbinfo_by_doubanid",
                legacy_media_ids(subscribe).get("doubanid"),
            ),
            (
                "bangumiid",
                "get_tmdbinfo_by_bangumiid",
                legacy_media_ids(subscribe).get("bangumiid"),
            ),
        )
        for source_name, method_name, source_id in source_lookups:
            if tmdb_id:
                break
            lookup = getattr(self._chain, method_name, None)
            if not source_id or not callable(lookup):
                continue
            try:
                kwargs = (
                    {"doubanid": str(source_id), "mtype": media_type}
                    if source_name == "doubanid"
                    else {"bangumiid": int(source_id)}
                )
                result = self._timed_sync_call(
                    "subscribe_tmdb_repair", lookup, **kwargs
                )
                tmdb_id = self._tmdb_id_from_media(result)
            except Exception as error:
                logger.debug(
                    f"订阅 TMDB ID 自动修复的 {source_name} 映射失败："
                    f"{getattr(subscribe, 'name', '')} - {error}"
                )
            if tmdb_id:
                break

        if not tmdb_id:
            meta = MetaInfo(str(getattr(subscribe, "name", "") or ""))
            meta.year = getattr(subscribe, "year", None)
            meta.type = media_type
            try:
                search_metas = [meta]
                if meta.year:
                    relaxed_meta = MetaInfo(
                        str(getattr(subscribe, "name", "") or "")
                    )
                    relaxed_meta.type = media_type
                    search_metas.append(relaxed_meta)
                seen_ids = set()
                for search_meta in search_metas:
                    rows = self._timed_sync_call(
                        "subscribe_tmdb_repair",
                        search_medias,
                        self._chain,
                        meta=search_meta,
                        source="themoviedb",
                    ) or []
                    for row in rows:
                        row_id = self._tmdb_id_from_media(row)
                        if row_id and row_id not in seen_ids:
                            seen_ids.add(row_id)
                            candidates.append(row)
                tmdb_id = self._match_tmdb_search_candidate(
                    subscribe, media_type, candidates
                )
            except Exception as error:
                logger.debug(
                    f"订阅 TMDB ID 自动修复的标题查询失败："
                    f"{getattr(subscribe, 'name', '')} - {error}"
                )

        # 同步准备阶段可能早于平台搜索缓存建立；识别链是同一套平台
        # 能力，但会按标题/年份直接返回唯一 MediaInfo，作为最后兜底。
        if not tmdb_id:
            try:
                recognized = self._recognize_media_once(
                    (
                        "subscribe_tmdb_repair",
                        media_type.value,
                        getattr(subscribe, "name", ""),
                        getattr(subscribe, "year", None),
                    ),
                    meta=meta,
                    mtype=media_type,
                    tmdbid=None,
                    doubanid=legacy_media_ids(subscribe).get("doubanid"),
                    cache=True,
                )
                tmdb_id = self._tmdb_id_from_media(recognized)
            except Exception as error:
                logger.debug(
                    f"订阅 TMDB ID 自动修复的媒体识别失败："
                    f"{getattr(subscribe, 'name', '')} - {error}"
                )

        if not tmdb_id:
            logger.debug(
                f"订阅 TMDB ID 自动修复未找到安全匹配："
                f"{getattr(subscribe, 'name', '')} "
                f"({getattr(subscribe, 'year', '')})，"
                f"标题候选={len(candidates)}"
            )
            return False

        identity_update = tmdb_identity_update(subscribe, tmdb_id)
        try:
            updated = SubscribeOper().update(subscribe_id, identity_update)
        except Exception as error:
            logger.debug(
                f"订阅 TMDB ID 自动回填失败："
                f"{getattr(subscribe, 'name', '')} -> {tmdb_id} - {error}"
            )
            return False
        if not updated:
            logger.debug(f"订阅 TMDB ID 自动回填失败：订阅 {subscribe_id} 不存在")
            return False

        for field, value in identity_update.items():
            setattr(subscribe, field, value)
        logger.info(
            f"订阅 TMDB 身份已自动回填："
            f"{getattr(subscribe, 'name', '')} -> {tmdb_id}"
        )
        return True

    def _set_task_phase(
            self, subscribe: Any, phase: str, progress: int, **extra_kwargs
    ) -> None:
        """回写订阅任务的真实处理阶段。"""
        if self._task_update:
            task_id = (
                f"media:{self.subscription_budget_key(subscribe)}"
                if bool(getattr(subscribe, "_transient_target", False))
                   and hasattr(self, "subscription_budget_key")
                else f"subscribe:{getattr(subscribe, 'id', '')}"
            )
            self._task_update(
                task_id,
                phase=phase,
                progress=max(0, min(100, int(progress))),
                **extra_kwargs,
            )

    def _subscribe_mediainfo(
            self,
            subscribe: Any,
            media_type: MediaType,
            *,
            cache: bool = True,
    ) -> Optional[MediaInfo]:
        """优先复用订阅卡片信息，仅在关键字段缺失时回退媒体识别。"""
        title = str(getattr(subscribe, "name", "") or "").strip()
        try:
            tmdb_id = int(tmdb_id_of(subscribe) or 0)
        except (TypeError, ValueError):
            tmdb_id = 0
        media_category = str(
            getattr(subscribe, "media_category", "") or ""
        ).strip()
        if title and tmdb_id > 0 and media_category:
            try:
                mediainfo = MediaInfo(
                    type=media_type,
                    title=title,
                    year=getattr(subscribe, "year", None),
                    tmdb_id=tmdb_id,
                )
                apply_media_identity(mediainfo, "themoviedb", tmdb_id)
                for source_field, media_field in (
                        ("doubanid", "douban_id"),
                        ("bangumiid", "bangumi_id"),
                        ("anilistid", "anilist_id"),
                        ("original_title", "original_title"),
                        ("poster", "poster_path"),
                        ("backdrop", "backdrop_path"),
                        ("description", "overview"),
                        ("vote", "vote_average"),
                        ("release_date", "release_date"),
                        ("media_category", "category"),
                        ("episode_group", "episode_group"),
                ):
                    value = (
                        legacy_media_ids(subscribe).get(source_field)
                        if source_field in {
                            "doubanid", "bangumiid", "anilistid"
                        }
                        else getattr(subscribe, source_field, None)
                    )
                    if value in (None, "") or not hasattr(
                            mediainfo, media_field
                    ):
                        continue
                    try:
                        setattr(mediainfo, media_field, value)
                    except (AttributeError, TypeError, ValueError):
                        pass
                logger.debug(
                    f"复用订阅卡片媒体信息：{title}（TMDB={tmdb_id}，分类={media_category}）"
                )
                return mediainfo
            except (TypeError, ValueError) as error:
                logger.debug(
                    f"订阅卡片媒体信息无效，回退平台识别：{title} - {error}"
                )

        meta = MetaInfo(title)
        meta.year = getattr(subscribe, "year", None)
        meta.type = media_type
        season = (
            int(getattr(subscribe, "season", 0) or 1)
            if media_type == MediaType.TV else 0
        )
        if season:
            meta.begin_season = season
        source, media_id = media_identity(subscribe)
        legacy_ids = legacy_media_ids(subscribe)
        recognized = self._recognize_media_once(
            (
                "subscribe_fallback", media_type.value,
                source, media_id, title,
                getattr(subscribe, "year", None), season, bool(cache),
            ),
            meta=meta,
            mtype=media_type,
            media_source=source,
            media_id=media_id,
            **legacy_ids,
            cache=cache,
        )
        if recognized and hasattr(subscribe, "episode_group") and getattr(subscribe, "episode_group", None):
            try:
                recognized.episode_group = getattr(subscribe, "episode_group")
            except (AttributeError, TypeError, ValueError):
                pass
        return recognized

    def _recognize_media_once(self, key: Tuple[Any, ...], **kwargs: Any):
        cache_key = normalize_platform_cache_key(key)
        with self._media_recognition_lock:
            cached = self._media_recognition_cache.get(cache_key)
            if cached is not None:
                return cached
            future = self._media_recognition_inflight.get(key)
            owner = future is None
            if owner:
                future = Future()
                self._media_recognition_inflight[key] = future

        if not owner:
            return future.result()

        try:
            # 的媒体识别链包含非线程安全的远端客户端游标，不并发调用。
            with self._platform_media_recognition_lock:
                mediainfo = self._timed_sync_call(
                    "media_recognition", recognize_media, self._chain, **kwargs
                )
        except BaseException as error:
            future.set_exception(error)
            with self._media_recognition_lock:
                if self._media_recognition_inflight.get(key) is future:
                    self._media_recognition_inflight.pop(key, None)
            raise

        if mediainfo:
            with self._media_recognition_lock:
                self._media_recognition_cache.set(cache_key, mediainfo)
        future.set_result(mediainfo)
        with self._media_recognition_lock:
            if self._media_recognition_inflight.get(key) is future:
                self._media_recognition_inflight.pop(key, None)
        return mediainfo
