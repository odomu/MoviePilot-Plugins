"""Vue 页面需要的配置默认值和选项查询。"""

from typing import Any, Dict, List

from app.db import SessionFactory
from app.db.site_oper import SiteOper
from app.db.subscribe_oper import SubscribeOper
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.schemas.types import MediaType


class UIConfig:
    """提供 Vue 配置页所需的数据，不再保留旧 iframe/Vuetify 表单。"""

    @staticmethod
    def get_default_config() -> Dict[str, Any]:
        return {
            "enabled": False,
            "show_sidebar_nav": True,
            "agent_enabled": True,
            "notify": True,
            "notification_type": "Plugin",
            "webhook_enabled": False,
            "webhook_url": "",
            "webhook_method": "POST",
            "webhook_timeout": 10,
            "onlyonce": False,
            "cron": "30 2,10,18 * * *",
            "cookies": "",
            "p123_token": "",
            "p123_request_timeout": 30,
            "quark_cookie": "",
            "quark_request_timeout": 30,
            "guangya_access_token": "",
            "guangya_refresh_token": "",
            "guangya_client_id": "",
            "guangya_device_id": "",
            "guangya_request_timeout": 30,
            "tianyi_cookie": "",
            "tianyi_access_token": "",
            "tianyi_refresh_token": "",
            "tianyi_request_timeout": 60,
            "alipan_access_token": "",
            "alipan_refresh_token": "",
            "alipan_request_timeout": 60,
            "cloud_drive": "115",
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
            "pansou_timeout": 30,
            "seedhub_result_limit": 20,
            "butailing_result_limit": 20,
            "juying_username": "",
            "juying_password": "",
            "juying_result_limit": 5,
            "juying_request_interval": 1.0,
            "juying_unlocks_per_minute": 8,
            "pinglian_username": "",
            "pinglian_password": "",
            "pinglian_result_limit": 20,
            "pinglian_request_interval": 1.0,
            "pinglian_timeout": 30,
            "hdhive_query_mode": "web",
            "hdhive_api_key": "",
            "hdhive_client_id": "",
            "hdhive_redirect_uri": "",
            "hdhive_response_mode": "redirect",
            "hdhive_auth_code": "",
            "hdhive_access_token": "",
            "hdhive_refresh_token": "",
            "hdhive_token_expires_at": 0,
            "hdhive_token_file": "",
            "hdhive_auto_unlock": False,
            "hdhive_max_unlock_points": 50,
            "hdhive_max_points_per_sub": 20,
            "hdhive_username": "",
            "hdhive_password": "",
            "dian115_email": "",
            "dian115_password": "",
            "dian115_auto_unlock": False,
            "dian115_max_unlock_points": 50,
            "dian115_max_points_per_sub": 20,
            "search_source_order": ["pansou"],
            "search_cache_enabled": True,
            "search_cache_ttl_minutes": 30,
            "search_concurrency": 2,
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
            "max_transfer_per_sync": 50,
            "cross_transfer_enabled": False,
            "cross_transfer_media_types": ["movie", "tv"],
            "cross_transfer_download_path": "",
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
            "cloud_transfer_path": "/",
            "p123_transfer_path": "/",
            "quark_transfer_path": "/",
            "guangya_transfer_path": "/",
            "tianyi_transfer_path": "/",
            "alipan_transfer_path": "/",
            "cloud_media_path": "/",
            "p123_media_path": "/",
            "quark_media_path": "/",
            "guangya_media_path": "/",
            "tianyi_media_path": "/",
            "alipan_media_path": "/",
            "self_heal_interval": 10,
        }

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
                    "tmdb_id": subscribe.tmdbid,
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
