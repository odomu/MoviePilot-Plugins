"""订阅、入库与资源下载事件响应。"""

import io
from threading import Thread
from typing import Any, Dict, Optional, Tuple

from app.core.event import Event
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.event import ResourceDownloadEventData
from app.schemas.types import MediaType, NotificationType
from torf import Torrent, TorfError

from ...core import OwnerDelegator


class PluginEventHandler(OwnerDelegator):
    """处理 MoviePilot 事件总线回调。"""

    @staticmethod
    def _torrent_payload_to_magnet(payload: bytes) -> Tuple[str, Dict[str, Any]]:
        if not payload or len(payload) > 10 * 1024 * 1024:
            return "", {}
        try:
            torrent = Torrent.read_stream(io.BytesIO(payload), validate=True)
            magnet_url = str(torrent.magnet())
        except (OSError, TorfError, TypeError, ValueError):
            return "", {}
        files = [str(item) for item in torrent.files]
        if not files and torrent.name:
            files = [str(torrent.name)]
        return magnet_url, {
            "info_hash": str(torrent.infohash or "").upper(),
            "display_name": str(torrent.name or "").strip(),
            "size": int(torrent.size or 0),
            "torrent_files": files,
            "metadata_available": bool(torrent.name or files),
            "metadata_source": "moviepilot",
        }

    def _takeover_platform_download(
            self,
            event_data: ResourceDownloadEventData,
            subscribe: Any,
            season: int,
            episodes: list,
    ) -> bool:
        context = event_data.context
        torrent_info = context.torrent_info
        if "magnet" not in (self._resource_type_order or []):
            logger.debug("接管平台资源下载失败：资源类型优先级未启用 Magnet")
            return False

        from app.chain.download import DownloadChain

        payload, _, _ = DownloadChain().download_torrent(
            torrent=torrent_info,
            channel=event_data.channel,
            source=event_data.origin,
        )
        magnet_url = ""
        metadata: Dict[str, Any] = {}
        if isinstance(payload, str) and payload.lower().startswith("magnet:?"):
            magnet_url = payload
            parsed = self._sync_handler._offline_download.parse_magnet_link(
                magnet_url, fetch_metadata=True
            )
            metadata = dict((parsed or {}).get("metadata") or {})
        elif isinstance(payload, bytes):
            magnet_url, metadata = self._torrent_payload_to_magnet(payload)
        if not magnet_url:
            logger.warning(f"接管平台资源下载失败：无法读取种子元数据，{torrent_info.title}")
            return False

        resource = {
            "resource_type": "magnet",
            "pan_type": "magnet",
            "source": "moviepilot",
            "title": str(torrent_info.title or metadata.get("display_name") or "PT资源"),
            "url": magnet_url,
            "share_url": magnet_url,
            "magnet_metadata": metadata,
        }
        pending_key = self._sync_handler._queue_magnet_package(
            resource=resource,
            share_url=magnet_url,
            subscribe=subscribe,
            mediainfo=context.media_info,
            season=season,
            target_episodes=episodes,
            sub_key=f"pt:{getattr(subscribe, 'id', '')}",
        )
        if not pending_key:
            logger.error(f"接管平台资源下载失败：离线任务未创建，{torrent_info.title}")
            return False
        logger.debug(f"已接管平台资源下载：{torrent_info.title}")
        return True

    def _get_subscribe_id_from_event(self, event: Event) -> Optional[int]:
        if not event or not event.event_data:
            return None
        data = event.event_data or {}
        subscribe_id = data.get("subscribe_id") or data.get("id")
        if not subscribe_id and isinstance(data.get("subscribe"), dict):
            subscribe_id = data["subscribe"].get("id")
        try:
            return int(subscribe_id) if subscribe_id is not None else None
        except Exception:
            return None

    def on_subscribe_added(self, event: Event):
        """新增订阅由 MoviePilot 搜索调度钩子自动分流。"""
        sid = self._get_subscribe_id_from_event(event)
        if not sid:
            return
        if self._is_subscribe_excluded(sid):
            logger.debug(f"新增订阅不在插件处理范围：subscribe_id={sid}")
            return
        logger.debug(f"新增订阅等待搜索调度：subscribe_id={sid}")

    def on_subscribe_modified(self, event: Event):
        """ 用户手动修改订阅站点时，不自动覆盖用户操作 """
        sid = self._get_subscribe_id_from_event(event)
        if not sid:
            return
        logger.debug(f"订阅配置已修改，不改写站点：subscribe_id={sid}")
        return

    def _post_command_message(self, event_data: dict, title: str, text: str) -> None:
        self.post_message(
            mtype=NotificationType.Plugin,
            channel=event_data.get("channel"),
            title=title,
            text=text,
            userid=event_data.get("user"),
        )

    def _submit_remote_links(
            self,
            event_data: dict,
            subscribe_id: int,
            raw_links: str,
    ) -> None:
        result = self.submit_platform_links(subscribe_id, raw_links)
        self._post_command_message(
            event_data,
            "【网盘订阅】资源提交结果",
            str(result.get("message") or "资源提交失败"),
        )

    def on_plugin_action(self, event: Event):
        """处理通用远程命令，耗时校验交给后台线程。"""
        if not event or not self._enabled:
            return
        event_data = event.event_data or {}
        action = str(event_data.get("action") or "")
        if not action.startswith("cloudsubscribe_"):
            return

        if action == "cloudsubscribe_status":
            overview = self.get_platform_overview(0)
            stats = {item["title"]: item["value"] for item in overview["stats"]}
            runtime = overview["runtime"]
            self._post_command_message(
                event_data,
                "【网盘订阅】运行状态",
                (
                    f"状态：{runtime.get('task') or runtime.get('status')}\n"
                    f"任务：{len(runtime.get('tasks') or [])} 个\n"
                    f"转存：总计 {stats.get('总转存', 0)}，今日 {stats.get('今日转存', 0)}，"
                    f"成功 {stats.get('成功', 0)}，失败 {stats.get('失败', 0)}"
                ),
            )
            return

        if action == "cloudsubscribe_sync":
            result = self.start_platform_sync()
            self._post_command_message(
                event_data,
                "【网盘订阅】任务提交",
                str(result.get("message") or "任务启动失败"),
            )
            return

        if action == "cloudsubscribe_links":
            raw = str(event_data.get("arg_str") or "").strip()
            subscribe_text, separator, links_text = raw.partition(" ")
            if not separator or not subscribe_text.isdigit() or not links_text.strip():
                self._post_command_message(
                    event_data,
                    "【网盘订阅】参数错误",
                    "格式：/cloud_link 订阅ID 115分享、ED2K或Magnet链接",
                )
                return
            Thread(
                target=self._submit_remote_links,
                args=(dict(event_data), int(subscribe_text), links_text),
                daemon=True,
                name="cloudsubscribe-command-links",
            ).start()
            self._post_command_message(
                event_data,
                "【网盘订阅】正在校验资源",
                "链接已接收，正在校验并提交。",
            )
            return

        if action == "cloudsubscribe_cache_clear":
            result = self.api_vue_clear_cache()
            self._post_command_message(
                event_data,
                "【网盘订阅】缓存清理",
                str(result.get("message") or "缓存清理失败"),
            )

    def on_resource_download(self, event: Event):
        """接管或拦截 MoviePilot 即将创建的平台资源下载。"""
        if not event or not self._enabled:
            return
        if not self._sync_handler:
            return

        event_data: ResourceDownloadEventData = event.event_data
        if not event_data:
            return

        # 处理平台资源下载事件（PT、RSS、刷流等）。
        context = event_data.context
        if not context:
            return

        torrent = context.torrent_info
        media = context.media_info
        meta = context.meta_info
        if not torrent or not media or not meta:
            return

        tmdbid = media.tmdb_id
        if not tmdbid:
            return

        # 查找匹配的订阅；电影没有 season 字段，必须按 TMDB 全量查询。
        season_list = meta.season_list or [1]
        if media_type := getattr(getattr(media, "type", None), "value", getattr(media, "type", None)):
            if media_type == MediaType.MOVIE.value:
                all_subs = SubscribeOper().list_by_tmdbid(tmdbid, None)
            else:
                all_subs = []
                for season in season_list:
                    all_subs.extend(SubscribeOper().list_by_tmdbid(tmdbid, season))
        else:
            all_subs = []
            for season in season_list:
                all_subs.extend(SubscribeOper().list_by_tmdbid(tmdbid, season))

        if not all_subs:
            return

        # 接管时段内，本插件负责的订阅不允许 MoviePilot 同时提交 PT 下载。
        managed_subscribes = [
            subscribe for subscribe in all_subs
            if not self._is_subscribe_excluded(subscribe.id)
        ]
        all_plugin_managed = (
            bool(managed_subscribes)
            and self._is_takeover_active()
            and len(managed_subscribes) == len(all_subs)
        )
        episode_list = event_data.episodes or meta.episode_list or []

        is_tv = media_type == MediaType.TV.value
        can_match = bool(episode_list) if is_tv else True
        if (
                all_plugin_managed
                and self._takeover_platform_downloads
                and can_match
        ):
            subscribe = managed_subscribes[0]
            takeover_success = False
            try:
                takeover_success = self._takeover_platform_download(
                    event_data=event_data,
                    subscribe=subscribe,
                    season=int(season_list[0] or 1),
                    episodes=sorted({int(value) for value in episode_list}),
                )
            except Exception as error:
                logger.error(f"接管平台资源下载异常：{error}")
            event_data.cancel = True
            event_data.source = "CloudSubscribe-平台资源下载接管"
            event_data.reason = (
                "平台资源已提交插件离线下载"
                if takeover_success else "平台资源下载接管失败，已阻止平台下载"
            )
            return

        if all_plugin_managed and self._block_platform_downloads:
            sub_name = all_subs[0].name if all_subs else "未知"
            event_data.cancel = True
            event_data.source = "CloudSubscribe-平台资源下载拦截"
            event_data.reason = (
                f"订阅{sub_name}已由网盘订阅助手接管，"
                f"已拦截平台资源下载：{torrent.title}"
            )
            logger.debug(
                f"订阅接管已拦截平台资源下载：{sub_name}，{torrent.title}"
            )
            return
