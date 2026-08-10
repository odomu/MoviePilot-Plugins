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
    """处理事件总线回调。"""

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

    def _has_pending_cloud_target(
            self,
            subscribes: list,
            seasons: list,
            episodes: list,
    ) -> bool:
        """判断平台资源是否与正在处理的网盘订阅目标重叠。"""
        subscribe_ids = {
            int(getattr(subscribe, "id", 0) or 0) for subscribe in subscribes
        }
        subscribe_ids.discard(0)
        if not subscribe_ids or not self._sync_handler:
            return False
        season_set = {int(value) for value in (seasons or []) if int(value) > 0}
        episode_set = {int(value) for value in (episodes or []) if int(value) > 0}
        for item in self._sync_handler.get_pending_finalize_tasks():
            if int(item.get("subscribe_id") or 0) not in subscribe_ids:
                continue
            pending_season = int(item.get("season") or 0)
            if season_set and pending_season and pending_season not in season_set:
                continue
            pending_episodes = {
                int(value)
                for value in (
                        item.get("target_episodes")
                        or item.get("success_episodes")
                        or item.get("notification_episodes")
                        or ([item.get("episode")] if item.get("episode") else [])
                )
                if int(value) > 0
            }
            if not episode_set or not pending_episodes or episode_set & pending_episodes:
                return True
        return False

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
        """新增订阅由搜索调度钩子自动分流。"""
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

    def on_transfer_complete(self, event: Event):
        """PT 整理完成后异步进入网盘洗版上传。"""
        if (
                not event
                or not self._enabled
                or not self._enable_pt_upgrade
                or not self._sync_handler
        ):
            return
        event_data = event.event_data or {}
        if not event_data.get("downloader") or not event_data.get("download_hash"):
            return
        Thread(
            target=self._sync_handler.process_pt_upgrade,
            args=(dict(event_data),),
            daemon=True,
            name="cloudsubscribe-pt-upgrade",
        ).start()

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
            subscribe_id: int = 0,
            raw_links: str = "",
            title: str = "",
            media_type: str = "",
            selection_id: str = "",
            tmdb_id: Optional[int] = None,
    ) -> None:
        result = self.submit_platform_links(
            subscribe_id=subscribe_id,
            resource_links=raw_links,
            title=title,
            media_type=media_type,
            selection_id=selection_id,
            tmdb_id=tmdb_id,
            selection_scope=self._command_selection_scope(event_data),
        )
        self._post_command_message(
            event_data,
            "【网盘订阅】资源提交结果",
            self._format_remote_link_result(result),
        )

    @staticmethod
    def _command_selection_scope(event_data: dict) -> str:
        return f"telegram:{event_data.get('channel')}:{event_data.get('user')}"

    @staticmethod
    def _format_remote_link_result(result: dict) -> str:
        data = result.get("data") or {}
        if not data.get("selection_required"):
            return str(result.get("message") or "资源提交失败")
        selection_id = str(data.get("selection_id") or "")
        lines = [str(result.get("message") or "请选择 TMDB 媒体")]
        for item in list(data.get("candidates") or [])[:10]:
            year = f" ({item.get('year')})" if item.get("year") else ""
            media_type = item.get("media_type_name") or item.get("media_type") or ""
            lines.append(
                f"{item.get('media_type')}:{item.get('tmdb_id')}｜"
                f"{item.get('title') or '未知媒体'}{year} · {media_type}"
            )
        lines.append(f"选择格式：/cloud_link_select {selection_id} movie:TMDB_ID")
        return "\n".join(lines)

    def _run_remote_checkin(
            self,
            event_data: dict,
            provider: str,
            mode: str,
            confirm_gambler: bool,
    ) -> None:
        result = self.run_quick_checkin(
            provider=provider,
            mode=mode,
            confirm_gambler=confirm_gambler,
        )
        data = result.get("data") or {}
        lines = [str(result.get("message") or "签到失败")]
        for item in data.get("items") or []:
            record = item.get("data") or {}
            details = [
                str(record.get("status") or item.get("message") or "签到失败"),
            ]
            if record.get("points_change") is not None:
                details.append(f"积分 {int(record.get('points_change') or 0):+d}")
            if record.get("points_after") is not None:
                details.append(f"当前 {record.get('points_after')}")
            lines.append(f"{item.get('provider_name') or item.get('provider')}：{' · '.join(details)}")
        self._post_command_message(
            event_data,
            "【网盘订阅】签到结果",
            "\n".join(lines),
        )

    @staticmethod
    def _format_checkin_history(result: dict) -> str:
        if not result.get("success"):
            return str(result.get("message") or "签到详情查询失败")
        channels = (result.get("data") or {}).get("channels") or []
        lines = []
        trigger_names = {
            "scheduled": "定时执行",
            "retry": "异常重试",
            "manual": "手动执行",
        }
        for channel in channels:
            lines.append(
                f"【{channel.get('provider_name') or channel.get('provider')}】"
                f"共 {channel.get('total') or 0} 条"
            )
            records = channel.get("items") or []
            if not records:
                lines.append("暂无签到记录")
                continue
            for record in records:
                executed_at = str(record.get("executed_at") or "").replace("T", " ")
                mode = "赌狗签到" if record.get("mode") == "gambler" else "普通签到"
                details = [
                    trigger_names.get(record.get("trigger"), "手动执行"),
                    str(record.get("status") or "签到失败"),
                    mode,
                ]
                if record.get("points_change") is not None:
                    details.append(f"积分 {int(record.get('points_change') or 0):+d}")
                if record.get("points_after") is not None:
                    details.append(f"当前 {record.get('points_after')}")
                if record.get("signin_days") is not None:
                    details.append(f"累计 {record.get('signin_days')} 天")
                lines.append(f"{executed_at}｜{' · '.join(details)}")
        return "\n".join(lines) if lines else "暂无签到记录"

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
            links = self.extract_resource_links(raw)
            if not links:
                self._post_command_message(
                    event_data,
                    "【网盘订阅】参数错误",
                    "格式：/cloud_link [订阅ID或媒体名称] 115分享、ED2K或Magnet链接",
                )
                return
            target_text = raw
            for link in links:
                target_text = target_text.replace(link, " ")
            target_parts = target_text.split()
            subscribe_id = int(target_parts.pop(0)) if target_parts and target_parts[0].isdigit() else 0
            title = " ".join(target_parts).strip()
            Thread(
                target=self._submit_remote_links,
                kwargs={
                    "event_data": dict(event_data),
                    "subscribe_id": subscribe_id,
                    "raw_links": "\n".join(links),
                    "title": title,
                },
                daemon=True,
                name="cloudsubscribe-command-links",
            ).start()
            self._post_command_message(
                event_data,
                "【网盘订阅】正在校验资源",
                "链接已接收，正在校验并提交。",
            )
            return

        if action == "cloudsubscribe_link_select":
            args = str(event_data.get("arg_str") or "").strip().split()
            selection_type, separator, tmdb_text = (
                args[1].lower().partition(":") if len(args) == 2 else ("", "", "")
            )
            if (
                    len(args) != 2
                    or not separator
                    or selection_type not in {"movie", "tv"}
                    or not tmdb_text.isdigit()
            ):
                self._post_command_message(
                    event_data,
                    "【网盘订阅】参数错误",
                    "格式：/cloud_link_select 选择ID movie:TMDB_ID",
                )
                return
            Thread(
                target=self._submit_remote_links,
                kwargs={
                    "event_data": dict(event_data),
                    "selection_id": args[0],
                    "media_type": selection_type,
                    "tmdb_id": int(tmdb_text),
                },
                daemon=True,
                name="cloudsubscribe-command-link-select",
            ).start()
            self._post_command_message(
                event_data,
                "【网盘订阅】正在提交资源",
                "已收到 TMDB 选择，正在继续完整转存流程。",
            )
            return

        if action == "cloudsubscribe_checkin":
            args = str(event_data.get("arg_str") or "").strip().lower().split()
            confirm_gambler = any(value in {"confirm", "确认"} for value in args)
            args = [value for value in args if value not in {"confirm", "确认"}]
            mode_aliases = {"normal": "normal", "普通": "normal", "gambler": "gambler", "赌狗": "gambler"}
            mode = ""
            provider = ""
            for value in args:
                if value in mode_aliases and not mode:
                    mode = mode_aliases[value]
                elif not provider:
                    provider = value
                else:
                    self._post_command_message(
                        event_data,
                        "【网盘订阅】参数错误",
                        "格式：/cloud_checkin [渠道] [normal|gambler] [confirm]",
                    )
                    return
            Thread(
                target=self._run_remote_checkin,
                args=(dict(event_data), provider, mode, confirm_gambler),
                daemon=True,
                name="cloudsubscribe-command-checkin",
            ).start()
            self._post_command_message(
                event_data,
                "【网盘订阅】正在签到",
                "签到请求已接收，完成后将发送结果。",
            )
            return

        if action == "cloudsubscribe_checkin_history":
            args = str(event_data.get("arg_str") or "").strip().lower().split()
            if len(args) > 2:
                self._post_command_message(
                    event_data,
                    "【网盘订阅】参数错误",
                    "格式：/cloud_checkin_history [渠道] [数量]",
                )
                return
            provider = ""
            limit = 10
            for value in args:
                if value.isdigit():
                    limit = max(1, min(int(value), 60))
                elif not provider:
                    provider = value
                else:
                    self._post_command_message(
                        event_data,
                        "【网盘订阅】参数错误",
                        "格式：/cloud_checkin_history [渠道] [数量]",
                    )
                    return
            result = self.list_checkin_details(provider=provider, limit=limit)
            self._post_command_message(
                event_data,
                "【网盘订阅】签到详情",
                self._format_checkin_history(result),
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
        """接管或拦截即将创建的平台资源下载。"""
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

        # 接管时段内，按平台下载策略处理本插件负责的订阅。
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
        policy = self._platform_download_policy

        if all_plugin_managed and policy == "allow":
            if self._has_pending_cloud_target(
                    managed_subscribes, season_list, episode_list
            ):
                event_data.cancel = True
                event_data.source = "CloudSubscribe-重复资源拦截"
                event_data.reason = "同一订阅季集已有网盘任务正在处理，已阻止重复下载"
                logger.debug(f"已阻止平台重复下载：{torrent.title}")
            return

        is_tv = media_type == MediaType.TV.value
        can_match = bool(episode_list) if is_tv else True
        if all_plugin_managed and policy == "cloud":
            subscribe = managed_subscribes[0]
            takeover_success = False
            if can_match:
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

        if all_plugin_managed and policy == "block":
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
