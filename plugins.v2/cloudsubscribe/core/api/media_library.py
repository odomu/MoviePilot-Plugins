"""媒体库 Webhook、播放与内容查询 API。"""

import secrets
from threading import RLock, Timer
from typing import Any, Dict, Optional

from app.chain.mediaserver import MediaServerChain
from app.db import SessionFactory
from app.db.models.mediaserver import MediaServerItem
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from fastapi import HTTPException, Request

from .page import clear_ui_options_cache
from .. import OwnerDelegator
from ..config import UIConfig


class MediaLibraryApi(OwnerDelegator):
    _SYNC_DEBOUNCE_SECONDS = 2

    def __init__(self, owner):
        super().__init__(owner)
        object.__setattr__(self, "_sync_timer_lock", RLock())
        object.__setattr__(self, "_sync_timers", {})

    async def api_media_library_webhook(
            self,
            request: Request,
            key: str,
            source: str = "",
    ) -> dict:
        """使用插件固定 Key 接收 Emby application/json 媒体库事件。"""
        content_type = str(request.headers.get("content-type") or "").strip()
        query_source = str(source or "").strip()
        logger.debug(
            "媒体库 Webhook 请求进入插件："
            f"content_type={content_type or 'empty'}，"
            f"query_source={query_source or 'empty'}"
        )
        configured_key = str(self._media_library_webhook_key or "").strip()
        if (
                len(configured_key) < 16
                or not secrets.compare_digest(str(key or ""), configured_key)
        ):
            logger.debug("媒体库 Webhook 请求拒绝：固定 Key 校验失败")
            raise HTTPException(status_code=401, detail="Webhook Key 错误")
        try:
            payload = await request.json()
        except Exception as error:
            logger.debug(
                "媒体库 Webhook 请求拒绝：JSON 解析失败，"
                f"content_type={content_type or 'empty'}，"
                f"error={type(error).__name__}"
            )
            raise HTTPException(
                status_code=400,
                detail="请求体必须是 application/json",
            ) from error
        if not isinstance(payload, dict):
            logger.debug(
                "媒体库 Webhook 请求拒绝：JSON 顶层不是对象，"
                f"payload_type={type(payload).__name__}"
            )
            raise HTTPException(status_code=400, detail="Webhook JSON 格式错误")

        event_name = str(payload.get("Event") or "").strip().lower()
        server_payload = payload.get("Server")
        payload_server_name = str(
            server_payload.get("Name")
            if isinstance(server_payload, dict) else ""
        ).strip()
        logger.debug(
            "媒体库 Webhook 请求已解析："
            f"event={event_name or 'empty'}，"
            f"query_source={query_source or 'empty'}，"
            f"payload_server={payload_server_name or 'empty'}，"
            f"server_type={type(server_payload).__name__}"
        )
        test_events = {"system.webhooktest", "system.notificationtest"}
        if event_name not in test_events and not event_name.startswith("library."):
            logger.debug(
                "媒体库 Webhook 请求拒绝："
                f"不支持的事件 event={event_name or 'empty'}"
            )
            raise HTTPException(
                status_code=422,
                detail=f"不支持的 Emby 事件：{event_name or 'empty'}",
            )
        server_name = query_source or payload_server_name
        if not server_name:
            logger.debug("媒体库 Webhook 请求拒绝：缺少媒体服务器来源")
            raise HTTPException(
                status_code=422,
                detail="缺少 source 参数或 Server.Name",
            )
        service = MediaServerHelper().get_service(
            name=server_name,
            type_filter="emby",
        )
        if not service:
            logger.debug(
                "媒体库 Webhook 请求拒绝："
                f"MoviePilot 未找到 Emby 服务 source={server_name}"
            )
            raise HTTPException(
                status_code=404,
                detail=f"MoviePilot 中不存在 Emby 媒体服务器：{server_name}",
            )
        if not self._platform_media_sync_enabled:
            logger.debug("媒体库 Webhook 请求拒绝：接收媒体库通知未启用")
            raise HTTPException(status_code=409, detail="平台 Emby 数据同步未启用")
        if event_name in test_events:
            logger.info(f"Emby 媒体库 Webhook 连接测试成功：{server_name}")
            return {
                "success": True,
                "message": "Webhook 连接测试成功",
                "data": {"server": server_name, "event": event_name},
            }
        if not self._schedule_platform_media_sync(server_name):
            logger.debug(
                "媒体库 Webhook 请求拒绝："
                f"平台媒体同步提交失败 source={server_name}"
            )
            raise HTTPException(status_code=503, detail="媒体库同步任务提交失败")
        logger.info(
            f"已接收 Emby 媒体库 Webhook：{server_name} - {event_name}"
        )
        return {
            "success": True,
            "message": "媒体库通知已接收，平台 Emby 数据同步已排队",
            "data": {"server": server_name, "event": event_name},
        }

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
                logger.info(f"Emby Webhook 触发 MoviePilot 媒体库数据同步完成：{name}")
            except Exception as error:
                logger.warning(
                    f"Emby Webhook 触发 MoviePilot 媒体库数据同步失败："
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
        logger.debug(f"Emby Webhook 已安排 MoviePilot 媒体库数据同步：{name}")
        return True

    def close(self) -> None:
        with self._sync_timer_lock:
            timers = list(self._sync_timers.values())
            self._sync_timers.clear()
        for timer in timers:
            timer.cancel()

    def api_vue_refresh_media_library_webhook_key(self) -> dict:
        """由服务端轮换固定 Key，并让旧 Webhook 地址立即失效。"""
        new_key = secrets.token_hex(8)
        payload = dict(self._applied_config or UIConfig.get_default_config())
        payload["media_library_webhook_key"] = new_key
        if not self.update_config(payload):
            return {"success": False, "message": "Webhook Key 持久化失败"}

        self._media_library_webhook_key = new_key
        self._applied_config = dict(payload)
        with self._pending_config_lock:
            if self._pending_config is not None:
                self._pending_config["media_library_webhook_key"] = new_key
        clear_ui_options_cache()
        logger.info("媒体库 Webhook 固定 Key 已刷新，旧地址已失效")
        return {
            "success": True,
            "message": "Webhook Key 已刷新，旧地址已失效",
            "data": {"media_library_webhook_key": new_key},
        }

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
