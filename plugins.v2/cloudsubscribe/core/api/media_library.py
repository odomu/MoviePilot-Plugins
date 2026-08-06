"""媒体库 Webhook、播放与内容查询 API。"""

from threading import RLock, Timer
from typing import Any, Dict, Optional

from app.chain.mediaserver import MediaServerChain
from app.db import SessionFactory
from app.db.models.mediaserver import MediaServerItem
from app.helper.mediaserver import MediaServerHelper
from app.log import logger

from .. import OwnerDelegator


class MediaLibraryApi(OwnerDelegator):
    _SYNC_DEBOUNCE_SECONDS = 2
    _PLATFORM_SYNC_EVENTS = {"library.new", "library.deleted"}

    def __init__(self, owner):
        super().__init__(owner)
        object.__setattr__(self, "_sync_timer_lock", RLock())
        object.__setattr__(self, "_sync_timers", {})

    def handle_platform_media_webhook(self, event_info: Any) -> bool:
        """消费已鉴权并标准化的媒体服务器 Webhook 事件。"""
        if not self._platform_media_sync_enabled or not event_info:
            return False
        event_name = str(getattr(event_info, "event", "") or "").strip().lower()
        channel = str(getattr(event_info, "channel", "") or "").strip().lower()
        server_name = str(
            getattr(event_info, "server_name", "") or ""
        ).strip()
        if channel != "emby" or event_name not in self._PLATFORM_SYNC_EVENTS:
            return False
        if not server_name:
            logger.debug("平台 Emby Webhook 缺少媒体服务器来源，跳过数据同步")
            return False
        service = MediaServerHelper().get_service(
            name=server_name,
            type_filter="emby",
        )
        if not service:
            logger.debug(
                f"平台 Emby Webhook 来源未匹配已配置服务：{server_name}"
            )
            return False
        scheduled = self._schedule_platform_media_sync(server_name)
        if scheduled:
            logger.debug(
                f"已接收平台 Emby Webhook：{server_name} - {event_name}"
            )
        return scheduled

    def _schedule_platform_media_sync(self, server_name: str) -> bool:
        """仅由有效 Emby Webhook 触发，并按媒体服务器合并短时重复事件。"""
        name = str(server_name or "").strip()
        if not name or not self._platform_media_sync_enabled:
            return False

        def trigger() -> None:
            with self._sync_timer_lock:
                self._sync_timers.pop(name, None)
            if not self._platform_media_sync_enabled:
                return
            try:
                MediaServerChain().sync(server=name)
                logger.info(f"Emby Webhook 触发媒体库数据同步完成：{name}")
            except Exception as error:
                logger.warning(
                    f"Emby Webhook 触发媒体库数据同步失败："
                    f"{name}，原因：{error}"
                )

        timer = Timer(self._SYNC_DEBOUNCE_SECONDS, trigger)
        timer.daemon = True
        with self._sync_timer_lock:
            previous = self._sync_timers.get(name)
            if previous:
                previous.cancel()
            self._sync_timers[name] = timer
            timer.start()
        logger.debug(f"Emby Webhook 已安排媒体库数据同步：{name}")
        return True

    def close(self) -> None:
        with self._sync_timer_lock:
            timers = list(self._sync_timers.values())
            self._sync_timers.clear()
        for timer in timers:
            timer.cancel()

    @staticmethod
    def _history_group_key(media_type: str, tmdb_id: Any) -> str:
        return f"tmdb:{media_type or '未知类型'}:{tmdb_id}"

    def _history_emby_play_items(self, history: list) -> Dict[str, str]:
        """一次数据库查询返回可在已启用 Emby 中播放的历史汇总项。"""
        services = MediaServerHelper().get_services(type_filter="emby")
        active_services = {
            name: service
            for name, service in services.items()
            if not service.instance.is_inactive()
        }
        if not active_services:
            return {}
        eligible = {
            (int(item.get("tmdb_id")), str(item.get("type") or ""))
            for item in history
            if item.get("tmdb_id")
               and str(item.get("status") or "") == "成功"
               and not item.get("finalize_key")
        }
        if not eligible:
            return {}
        tmdb_ids = {tmdb_id for tmdb_id, _ in eligible}
        with SessionFactory() as db:
            rows = db.query(MediaServerItem).filter(
                MediaServerItem.server == "emby",
                MediaServerItem.tmdbid.in_(tmdb_ids),
            ).all()
        result = {}
        for row in rows:
            identity = (int(row.tmdbid or 0), str(row.item_type or ""))
            if identity not in eligible or not row.item_id:
                continue
            result[self._history_group_key(identity[1], identity[0])] = str(
                row.item_id
            )
        return result

    def api_vue_emby_play(self, item_id: str) -> dict:
        """仅从已启用 Emby 实例解析播放链接。"""
        item_id = str(item_id or "").strip()
        if not item_id:
            return {"success": False, "message": "缺少 Emby 媒体条目ID"}
        services = MediaServerHelper().get_services(type_filter="emby")
        chain = MediaServerChain()
        for name, service in services.items():
            if service.instance.is_inactive():
                continue
            item = chain.iteminfo(server=name, item_id=item_id)
            if not item:
                continue
            play_url = chain.get_play_url(server=name, item_id=item_id)
            if play_url:
                return {"success": True, "data": {"url": play_url}}
        return {"success": False, "message": "未在已启用的 Emby 中找到播放地址"}

    def api_vue_media_server_content(
            self,
            server: str = "",
            keyword: str = "",
            tmdb_id: Optional[int] = None,
            media_type: str = "",
    ) -> dict:
        if not self._sync_handler:
            return {"success": False, "message": "同步处理器未初始化"}
        try:
            data = self._sync_handler.list_media_server_content(
                server=server,
                keyword=keyword,
                tmdb_id=tmdb_id,
                media_type=media_type,
            )
            return {
                "success": True,
                "message": f"已读取 {len(data.get('items') or [])} 个媒体库内容",
                "data": data,
            }
        except Exception as error:
            logger.warning(f"读取媒体服务器内容失败：{error}")
            return {"success": False, "message": str(error)}
