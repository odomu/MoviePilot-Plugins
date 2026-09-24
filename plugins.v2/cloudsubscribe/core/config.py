"""Vue 页面需要的配置默认值和选项查询。"""

import datetime
from typing import Any, Dict, List
from urllib.parse import urlsplit, urlunsplit

from app.db import SessionFactory
from app.db.site_oper import SiteOper
from app.db.subscribe_oper import SubscribeOper

try:
    from app.helper.mediaserver import MediaServerHelper
except ImportError:
    from app.application.mediaserver import MediaServerHelper
from app.log import logger
from app.schemas.types import MediaType

from .media import tmdb_id_of

DEFAULT_AUTO_SUBSCRIBE_USERNAME = "网盘订阅助手"


class UIConfig:
    """提供 Vue 配置页所需的数据，不再保留旧 iframe/Vuetify 表单。"""

    @staticmethod
    def get_default_config() -> Dict[str, Any]:
        current_year = datetime.datetime.now().year
        current_month = datetime.datetime.now().month
        return {
            "enabled": False,
            "show_sidebar_nav": True,
            "agent_enabled": True,
            "direct_transfer_enabled": True,
            "notify": True,
            "notification_type": "Plugin",
            "webhook_enabled": False,
            "webhook_url": "",
            "webhook_method": "POST",
            "webhook_timeout": 10,
            "cron": "30 2,10,18 * * *",
            "auto_subscribe_enabled": False,
            "auto_subscribe_onlyonce": False,
            "auto_subscribe_cron": "0 8 * * *",
            "auto_subscribe_username": DEFAULT_AUTO_SUBSCRIBE_USERNAME,
            "auto_subscribe_notify": False,
            "auto_subscribe_skip_subscribed": True,
            "auto_subscribe_skip_history": True,
            "auto_subscribe_skip_library": True,
            "auto_subscribe_skip_season_zero": True,
            "auto_subscribe_proxy": "",
            "auto_subscribe_proxy_username": "",
            "auto_subscribe_proxy_password": "",
            "auto_subscribe_douban_enabled": False,
            "auto_subscribe_douban_ranks": ["movie-showing", "movie-hot"],
            "auto_subscribe_douban_rsshub_base": "https://rsshub.app",
            "auto_subscribe_douban_rss_urls": [],
            "auto_subscribe_douban_proxy": False,
            "auto_subscribe_douban_limit": 30,
            "auto_subscribe_douban_min_vote": 6,
            "auto_subscribe_douban_min_year": current_year,
            "auto_subscribe_douban_min_month": current_month,
            "auto_subscribe_douban_media_type": "all",
            "auto_subscribe_tmdb_enabled": False,
            "auto_subscribe_tmdb_ranks": ["trending", "movies", "tvs"],
            "auto_subscribe_tmdb_proxy": False,
            "auto_subscribe_tmdb_limit": 20,
            "auto_subscribe_tmdb_min_vote": 6,
            "auto_subscribe_tmdb_min_year": current_year,
            "auto_subscribe_tmdb_min_month": current_month,
            "auto_subscribe_tmdb_media_type": "all",
            "auto_subscribe_bangumi_enabled": False,
            "auto_subscribe_bangumi_proxy": False,
            "auto_subscribe_bangumi_limit": 50,
            "auto_subscribe_bangumi_min_vote": 6,
            "auto_subscribe_bangumi_min_year": current_year,
            "auto_subscribe_bangumi_min_month": current_month,
            "auto_subscribe_anilist_enabled": False,
            "auto_subscribe_anilist_ranks": ["popular_this_season", "trending"],
            "auto_subscribe_anilist_proxy": False,
            "auto_subscribe_anilist_limit": 30,
            "auto_subscribe_anilist_min_vote": 6,
            "auto_subscribe_anilist_min_year": current_year,
            "auto_subscribe_anilist_min_month": current_month,
            "auto_subscribe_maoyan_enabled": False,
            "auto_subscribe_maoyan_base_url": "https://piaofang.maoyan.com",
            "auto_subscribe_maoyan_movie_box": True,
            "auto_subscribe_maoyan_web_platform_map": {"all": ["tv"]},
            "auto_subscribe_maoyan_platforms": ["all"],
            "auto_subscribe_maoyan_categories": ["tv"],
            "auto_subscribe_maoyan_limit": 10,
            "auto_subscribe_maoyan_proxy": False,
            "auto_subscribe_maoyan_min_vote": 6,
            "auto_subscribe_maoyan_min_year": current_year,
            "auto_subscribe_maoyan_min_month": current_month,
            "auto_subscribe_maoyan_media_type": "all",
            "auto_subscribe_netflix_enabled": False,
            "auto_subscribe_netflix_base_url": "https://www.netflix.com",
            "auto_subscribe_netflix_global": True,
            "auto_subscribe_netflix_global_dataset": "weekly",
            "auto_subscribe_netflix_global_media_types": [
                "Films (English)", "Films (Non-English)",
                "TV (English)", "TV (Non-English)",
            ],
            "auto_subscribe_netflix_country_selections": {},
            "auto_subscribe_netflix_limit": 10,
            "auto_subscribe_netflix_proxy": False,
            "auto_subscribe_netflix_min_vote": 6,
            "auto_subscribe_netflix_min_year": current_year,
            "auto_subscribe_netflix_min_month": current_month,
            "auto_subscribe_netflix_rich_metadata": False,
            "auto_subscribe_netflix_max_workers": 4,
            "auto_subscribe_netflix_use_cache": True,
            "auto_subscribe_mikan_enabled": False,
            "auto_subscribe_mikan_year": current_year,
            "auto_subscribe_mikan_season": "当前",
            "auto_subscribe_mikan_resolve_bangumi_id": True,
            "auto_subscribe_mikan_proxy": False,
            "auto_subscribe_mikan_limit": 100,
            "auto_subscribe_mikan_min_vote": 6,
            "auto_subscribe_mikan_min_year": current_year,
            "auto_subscribe_mikan_min_month": current_month,
            "auto_subscribe_mikan_base_urls": [
                "https://mikanani.me", "https://mikanime.tv"
            ],
            "cookies": "",
            "p115_checkin_enabled": False,
            "p115_checkin_mode": "normal",
            "p123_token": "",
            "p123_request_timeout": 30,
            "quark_cookie": "",
            "quark_checkin_enabled": False,
            "quark_checkin_url": "",
            "quark_checkin_mode": "normal",
            "quark_request_timeout": 30,
            "guangya_access_token": "",
            "guangya_refresh_token": "",
            "guangya_client_id": "",
            "guangya_device_id": "",
            "guangya_request_timeout": 30,
            "tianyi_cookie": "",
            "tianyi_access_token": "",
            "tianyi_refresh_token": "",
            "tianyi_session_key": "",
            "tianyi_request_timeout": 60,
            "yun139_authorization": "",
            "yun139_request_timeout": 60,
            "alipan_access_token": "",
            "alipan_refresh_token": "",
            "alipan_request_timeout": 60,
            "cloud_drive": "115",
            "organize_after_transfer": True,
            "organize_subtitles": True,
            "subtitle_traditional_to_simplified": False,
            "video_extensions": [".mkv", ".mp4", ".avi", ".iso"],
            "subtitle_extensions": [
                ".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup", ".idx", ".smi", ".mks",
            ],
            "anime_pack_preferred": True,
            "offline_timeout": 30,
            "strm_generate_enabled": True,
            "nfo_scrape_enabled": False,
            "image_scrape_enabled": False,
            "strm_base_url": "http://172.17.0.1:9527",
            "strm_url_template": "{base_url}/d/{pickcode}?/{file_name}",
            "media_server_refresh_enabled": False,
            "media_servers": [],
            "media_server_path_mappings": "",
            "media_server_refresh_delay": 0,
            "emby_mediainfo_enabled": False,
            "platform_media_sync_enabled": False,
            "platform_deep_delete_enabled": False,
            "platform_transfer_history_enabled": False,
            "timeout_enabled": True,
            "timeout_default_connect": 30,
            "timeout_default_pool": 15,
            "timeout_default_read": 60,
            "timeout_default_write": 60,
            "timeout_slow_connect": 30,
            "timeout_slow_pool": 15,
            "timeout_slow_read": 300,
            "timeout_slow_write": 300,
            "pansou_url": "https://so.252035.xyz/",
            "hdhive_base_url": "https://re0.me",
            "dian115_base_url": "https://m.dian115.com",
            "hdhaven_base_url": "https://hdhaven.com",
            "juying_base_url": "https://www.jying.top",
            "seedhub_base_url": "https://www.seedhub.cc",
            "piratebay_base_url": "https://apibay.org",
            "uindex_base_url": "https://uindex.org",
            "pinglian_base_url": "https://pinglian.lol",
            "online_docs_urls": [],
            "online_docs_resource_types": ["115", "123", "quark", "alipan"],
            "online_docs": [{"url": "", "resource_types": []}],
            "pansou_username": "",
            "pansou_password": "",
            "pansou_auth_enabled": False,
            "pansou_channels": [],
            "pansou_plugins": [],
            "pansou_filter_include": [],
            "pansou_filter_exclude": [],
            "resource_type_order": ["115", "ed2k"],
            "magnet_metadata_url_template": "https://itorrents.org/torrent/{info_hash}.torrent",
            "pansou_concurrency": None,
            "pansou_result_limit": 10,
            "pansou_refresh": True,
            "pansou_timeout": 60,
            "seedhub_result_limit": 20,
            "seedhub_request_interval": 1.0,
            "seedhub_timeout": 60,
            "piratebay_result_limit": 20,
            "piratebay_request_interval": 1.0,
            "piratebay_timeout": 60,
            "mikan_base_url": "https://mikanani.me",
            "mikan_result_limit": 10,
            "mikan_request_interval": 2.0,
            "mikan_timeout": 60,
            "mikan_fansub_order": [
                "LoliHouse",
                "VCB-Studio",
                "喵萌奶茶|Nekomoe",
                "Nix-Raws",
                r"\bANI\b|ANi",
            ],
            "mikan_fansub_exclude": "",
            "mikan_exclude_re": "720[pP]|480[pP]|特别篇|特別篇|\\b(?:SP|OVA|OAD)\\d*|\\b\\d+\\s*-\\s*\\d+\\b",
            "mikan_no_subs_re": "无字幕|無字幕|无字版|無字版|生肉|\\b(?:unsubbed|no[ ._-]*subs?|subtitle[ ._-]*free)\\b",
            "mikan_chinese_re": "简[体體繁中]|簡[体體繁中]|繁[体體简簡中]|中[日英双雙文]|[简簡繁]日|\\b(?:CHS|CHT|BIG5|GB|SC|TC|ZH|CHI|ZHO)(?:\\b|_)",
            "animegarden_base_url": "https://animes.garden/",

            "animegarden_fansub_order": [
                "LoliHouse",
                "VCB-Studio",
                "喵萌奶茶|Nekomoe",
                "Nix-Raws",
                r"\bANI\b|ANi",
            ],
            "animegarden_exclude_re": "720[pP]|480[pP]|特别篇|特別篇|\\b(?:SP|OVA|OAD)\\d*|\\b\\d+\\s*-\\s*\\d+\\b",
            "animegarden_no_subs_re": "无字幕|無字幕|无字版|無字版|生肉|\\b(?:unsubbed|no[ ._-]*subs?|subtitle[ ._-]*free)\\b",
            "animegarden_chinese_re": "简[体體繁中]|簡[体體繁中]|繁[体體简簡中]|中[日英双雙文]|[简簡繁]日|\\b(?:CHS|CHT|BIG5|GB|SC|TC|ZH|CHI|ZHO)(?:\\b|_)",
            "animegarden_result_limit": 10,
            "animegarden_request_interval": 1.0,
            "animegarden_timeout": 60,
            "uindex_result_limit": 20,
            "uindex_request_interval": 1.0,
            "uindex_timeout": 60,
            "juying_username": "",
            "juying_password": "",
            "juying_checkin_enabled": False,
            "juying_result_limit": 5,
            "juying_request_interval": 1.0,
            "pinglian_username": "",
            "pinglian_password": "",
            "pinglian_result_limit": 20,
            "pinglian_request_interval": 1.0,
            "pinglian_timeout": 60,
            "hdhive_query_mode": "web",
            "hdhive_api_key": "",
            "hdhive_client_id": "",
            "hdhive_redirect_uri": "",
            "hdhive_response_mode": "redirect",
            "hdhive_auth_code": "",
            "hdhive_access_token": "",
            "hdhive_refresh_token": "",
            "hdhive_token_expires_at": 0,
            "hdhive_auto_unlock": False,
            "hdhive_max_unlock_points": 50,
            "hdhive_max_points_per_sub": 20,
            "hdhive_username": "",
            "hdhive_password": "",
            "hdhive_checkin_enabled": False,
            "hdhive_checkin_mode": "normal",
            "checkin_cron": "0 8 * * *",
            "checkin_auto_retry": True,
            "checkin_retry_count": 2,
            "dian115_email": "",
            "dian115_password": "",
            "dian115_checkin_enabled": False,
            "dian115_checkin_mode": "normal",
            "dian115_lottery_enabled": False,
            "dian115_lottery_count": 1,
            "dian115_auto_unlock": False,
            "dian115_max_unlock_points": 50,
            "dian115_max_points_per_sub": 20,
            "hdhaven_username": "",
            "hdhaven_password": "",
            "hdhaven_auto_unlock": True,
            "hdhaven_max_unlock_points": 50,
            "hdhaven_max_points_per_sub": 20,
            "hdhaven_checkin_enabled": False,
            "hdhaven_checkin_mode": "gambler",
            "hdhaven_candidate_limit": 4,
            "hdhaven_request_interval": 2.0,
            "hdhaven_unlocks_per_minute": 5,
            "hdhaven_timeout": 60,
            "hdhaven_magnet_enabled": False,
            "search_source_order": ["pansou"],
            "search_proxy": "",
            "search_proxy_username": "",
            "search_proxy_password": "",
            "search_cache_enabled": True,
            "search_cache_ttl_minutes": 30,
            "search_concurrency": 2,
            "search_source_timeout": 60,
            "search_circuit_breaker_enabled": True,
            "search_circuit_breaker_threshold": 3,
            "search_circuit_breaker_cooldown": 60,
            "hdhive_timeout": 60,
            "dian115_timeout": 60,
            "juying_timeout": 60,
            "hdhive_candidate_limit": 4,
            "hdhive_request_interval": 5,
            "hdhive_unlocks_per_minute": 2,
            "dian115_candidate_limit": 4,
            "dian115_request_interval": 1,
            "dian115_unlocks_per_minute": 6,
            "hdhive_torrentclaw_enabled": False,
            "hdhive_torrentclaw_subtitle_languages": ["zh"],
            "subscribe_filter_mode": "exclude",
            "exclude_subscribes": [],
            "include_subscribes": [],
            "block_system_subscribe": False,
            "takeover_new_subscribes": False,
            "platform_download_policy": "block",
            "block_start_time": "18:00",
            "block_end_time": "23:59",
            "transfer_task_batch_size": 50,
            "cross_transfer_enabled": False,
            "cross_transfer_media_types": ["movie", "tv"],
            "cross_transfer_download_path": "/tmp",
            "cross_transfer_download_threads": 5,
            "cross_transfer_max_concurrent": 2,
            "subscription_concurrency": 2,
            "batch_size": 20,
            "batch_interval": 3,
            "transfer_risk_cooldown": 1800,
            "skip_other_season_dirs": True,
            "enable_cloud_upgrade": False,
            "enable_pt_upgrade": False,
            "upgrade_mode": "largest",
            "upgrade_subscribe_ids": [],
            "local_resource_path": "",
            "p115_transfer_path": "/",
            "p123_transfer_path": "/",
            "quark_transfer_path": "/",
            "guangya_transfer_path": "/",
            "tianyi_transfer_path": "/",
            "yun139_transfer_path": "/",
            "alipan_transfer_path": "/",
            "p115_media_path": "/",
            "p123_media_path": "/",
            "quark_media_path": "/",
            "guangya_media_path": "/",
            "tianyi_media_path": "/",
            "yun139_media_path": "/",
            "alipan_media_path": "/",
            "self_heal_interval": 10,
            "anime_pack_preferred": True,
            "offline_timeout": 30,
        }

    @staticmethod
    def normalize_auto_subscribe_dates(config: Dict[str, Any]) -> None:
        current_year = datetime.datetime.now().year
        current_month = datetime.datetime.now().month
        for key in (
                "auto_subscribe_douban_min_year",
                "auto_subscribe_tmdb_min_year",
                "auto_subscribe_bangumi_min_year",
                "auto_subscribe_anilist_min_year",
                "auto_subscribe_maoyan_min_year",
                "auto_subscribe_netflix_min_year",
                "auto_subscribe_mikan_year",
                "auto_subscribe_mikan_min_year",
        ):
            try:
                if int(config.get(key) or 0) == 0:
                    config[key] = current_year
            except (TypeError, ValueError):
                config[key] = current_year
        for provider_id in (
                "douban", "tmdb", "bangumi", "anilist", "maoyan", "netflix", "mikan"
        ):
            key = f"auto_subscribe_{provider_id}_min_month"
            try:
                month = int(config.get(key) or current_month)
            except (TypeError, ValueError):
                month = current_month
            config[key] = month if 1 <= month <= 12 else current_month

    @staticmethod
    def _normalize_rsshub_instance_url(value: Any) -> str:
        """规范公告中的实例地址，拒绝维护者主页、查询参数和本地地址。"""
        try:
            parsed = urlsplit(str(value or "").strip())
            if parsed.scheme.lower() not in {"http", "https"}:
                return ""
            if (
                    not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.query
                    or parsed.fragment
            ):
                return ""
            port = parsed.port
        except ValueError:
            return ""
        hostname = parsed.hostname.rstrip(".").lower()
        if (
                "." not in hostname
                or hostname == "localhost"
                or hostname.endswith(".local")
                or any(character.isspace() for character in parsed.path)
        ):
            return ""
        netloc = hostname if port is None else f"{hostname}:{port}"
        path = parsed.path.rstrip("/")
        return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))

    @staticmethod
    def _subscribes() -> list:
        try:
            with SessionFactory() as db:
                return SubscribeOper(db=db).list("N,R") or []
        except Exception as error:
            logger.error(f"获取订阅列表失败: {error}")
            return []

    @staticmethod
    def get_subscribe_options() -> List[Dict[str, Any]]:
        items = []
        for subscribe in UIConfig._subscribes():
            prefix = "[剧]" if subscribe.type == MediaType.TV.value else "[影]"
            suffix = f" ({subscribe.year})" if subscribe.year else ""
            season = f" S{subscribe.season or 1}" if subscribe.type == MediaType.TV.value else ""
            items.append({"title": f"{prefix} {subscribe.name}{suffix}{season}", "value": subscribe.id})
        return items

    @staticmethod
    def get_subscribe_options_grouped() -> List[Dict[str, Any]]:
        items = []
        for subscribe in UIConfig._subscribes():
            is_movie = subscribe.type == MediaType.MOVIE.value
            group = "电影订阅" if is_movie else "电视剧订阅"
            prefix = "[电影]" if is_movie else "[电视剧]"
            suffix = f" ({subscribe.year})" if subscribe.year else ""
            season = f" S{subscribe.season or 1}" if subscribe.type == MediaType.TV.value else ""
            items.append(
                {
                    "title": f"{prefix} {subscribe.name}{suffix}{season}",
                    "value": subscribe.id,
                    "group": group,
                    "name": subscribe.name,
                    "year": subscribe.year,
                    "media_type": "movie" if is_movie else "tv",
                    "tmdb_id": tmdb_id_of(subscribe),
                    "season": subscribe.season if not is_movie else None,
                }
            )
        return items

    @staticmethod
    def get_site_name_options() -> List[Dict[str, Any]]:
        try:
            with SessionFactory() as db:
                sites = SiteOper(db=db).list() or []
            names = sorted({str(site.name) for site in sites if site.name})
            return [{"title": name, "value": name} for name in names]
        except Exception as error:
            logger.error(f"获取站点列表失败: {error}")
            return []

    @staticmethod
    def get_media_server_options() -> List[Dict[str, Any]]:
        try:
            return [
                {"title": config.name, "value": config.name, "type": config.type}
                for config in MediaServerHelper().get_configs().values()
            ]
        except Exception as error:
            logger.error(f"获取媒体服务器列表失败: {error}")
            return []

    @staticmethod
    def normalize_config(target: Dict[str, Any]) -> Dict[str, Any]:
        """集中处理配置清洗、赋初值、字符串与数组拆分转换，解耦前端。"""
        if not isinstance(target, dict):
            return {}
        import re
        current_year = datetime.datetime.now().year
        current_month = datetime.datetime.now().month

        if not str(target.get("auto_subscribe_username") or "").strip():
            target["auto_subscribe_username"] = DEFAULT_AUTO_SUBSCRIBE_USERNAME

        year_keys = [
            "auto_subscribe_douban_min_year",
            "auto_subscribe_maoyan_min_year",
            "auto_subscribe_netflix_min_year",
            "auto_subscribe_mikan_year",
            "auto_subscribe_mikan_min_year",
            "auto_subscribe_tmdb_min_year",
            "auto_subscribe_bangumi_min_year",
            "auto_subscribe_anilist_min_year",
        ]
        for key in year_keys:
            try:
                val = int(target.get(key) or 0)
            except (TypeError, ValueError):
                val = 0
            if val <= 0:
                target[key] = current_year

        month_keys = [
            "auto_subscribe_douban_min_month",
            "auto_subscribe_maoyan_min_month",
            "auto_subscribe_netflix_min_month",
            "auto_subscribe_mikan_min_month",
            "auto_subscribe_tmdb_min_month",
            "auto_subscribe_bangumi_min_month",
            "auto_subscribe_anilist_min_month",
        ]
        for key in month_keys:
            try:
                val = int(target.get(key) or 0)
            except (TypeError, ValueError):
                val = 0
            if not 1 <= val <= 12:
                target[key] = current_month

        rss_urls = target.get("auto_subscribe_douban_rss_urls")
        if isinstance(rss_urls, str):
            target["auto_subscribe_douban_rss_urls"] = [
                u.strip() for u in re.split(r"[\n,，]+", rss_urls) if u.strip()
            ]
        elif not isinstance(rss_urls, list):
            target["auto_subscribe_douban_rss_urls"] = []

        mikan_urls = target.get("auto_subscribe_mikan_base_urls")
        if isinstance(mikan_urls, str):
            target["auto_subscribe_mikan_base_urls"] = [
                u.strip() for u in re.split(r"[\n,，]+", mikan_urls) if u.strip()
            ]
        elif not isinstance(mikan_urls, list) or not mikan_urls:
            target["auto_subscribe_mikan_base_urls"] = ["https://mikanani.me", "https://mikanime.tv"]

        default_fansub_order = ["LoliHouse", "VCB-Studio", "喵萌奶茶|Nekomoe", "Nix-Raws", r"\bANI\b|ANi"]
        if not isinstance(target.get("mikan_fansub_order"), list) or not target["mikan_fansub_order"]:
            target["mikan_fansub_order"] = list(default_fansub_order)
        if not isinstance(target.get("animegarden_fansub_order"), list) or not target["animegarden_fansub_order"]:
            target["animegarden_fansub_order"] = list(default_fansub_order)

        default_no_subs_re = r"无字幕|無字幕|无字版|偏字版|生肉|\b(?:unsubbed|no[ ._-]*subs?|subtitle[ ._-]*free)\b"
        default_chinese_re = r"简[体體繁中]|簡[体體繁中]|繁[体體简簡中]|中[日英双雙文]|[简簡繁]日|\b(?:CHS|CHT|BIG5|GB|SC|TC|ZH|CHI|ZHO)(?:\b|_)"
        default_exclude_re = r"720[pP]|480[pP]|特别篇|特別篇|\b(?:SP|OVA|OAD)\d*|\b\d+\s*-\s*\d+\b"

        if not target.get("mikan_no_subs_re"):
            target["mikan_no_subs_re"] = default_no_subs_re
        if not target.get("mikan_chinese_re"):
            target["mikan_chinese_re"] = default_chinese_re
        if not target.get("mikan_exclude_re"):
            target["mikan_exclude_re"] = default_exclude_re

        if not target.get("animegarden_no_subs_re"):
            target["animegarden_no_subs_re"] = default_no_subs_re
        if not target.get("animegarden_chinese_re"):
            target["animegarden_chinese_re"] = default_chinese_re
        if not target.get("animegarden_exclude_re"):
            target["animegarden_exclude_re"] = default_exclude_re

        if not str(target.get("cross_transfer_download_path") or "").strip():
            target["cross_transfer_download_path"] = "/tmp"

        online_docs = target.get("online_docs")
        if not isinstance(online_docs, list) or not online_docs:
            legacy_urls = target.get("online_docs_urls") or []
            if isinstance(legacy_urls, str):
                legacy_urls = [u.strip() for u in re.split(r"[,，\n]+", legacy_urls) if u.strip()]
            legacy_types = target.get("online_docs_resource_types") or []
            if not isinstance(legacy_types, list):
                legacy_types = []
            target["online_docs"] = [
                {"url": url, "resource_types": list(legacy_types)}
                for url in legacy_urls if url
            ]
        if not target["online_docs"]:
            target["online_docs"].append({"url": "", "resource_types": []})
        target["online_docs_urls"] = []
        target["online_docs_resource_types"] = []

        for key in ("search_source_order", "pansou_channels", "pansou_plugins", "pansou_filter_include",
                    "pansou_filter_exclude"):
            val = target.get(key)
            if isinstance(val, str):
                target[key] = [v.strip() for v in re.split(r"[,，\n]+", val) if v.strip()]
            elif not isinstance(val, list):
                target[key] = []

        def _clean_ext_list(raw_val, defaults):
            if isinstance(raw_val, str):
                items = re.split(r"[,，\n\s]+", raw_val)
            elif isinstance(raw_val, (list, set, tuple)):
                items = list(raw_val)
            else:
                return list(defaults)
            result = []
            seen = set()
            for item in items:
                ext = str(item or "").strip().lower()
                if not ext:
                    continue
                if not ext.startswith("."):
                    ext = f".{ext}"
                if ext not in seen:
                    seen.add(ext)
                    result.append(ext)
            return result if result else list(defaults)

        target["video_extensions"] = _clean_ext_list(
            target.get("video_extensions"), [".mkv", ".mp4", ".avi", ".iso"]
        )
        target["subtitle_extensions"] = _clean_ext_list(
            target.get("subtitle_extensions"),
            [".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup", ".idx", ".smi", ".mks"],
        )

        return target
