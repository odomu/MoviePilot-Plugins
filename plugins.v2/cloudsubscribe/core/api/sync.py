"""订阅同步任务提交 API。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Lock, Thread
from typing import Any, Dict, Optional

from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper
from app.schemas.types import MediaType

from .. import CloudDriveCapability, OwnerDelegator
from ..services.runtime import sync_lock

_SYNC_SUBMIT_LOCK = Lock()


class SyncApi(OwnerDelegator):
    def _start_sync_thread(
            self,
            sync_kwargs: Dict[str, Any],
            *,
            thread_name: str,
            task_text: str,
    ) -> bool:
        """原子预占同步锁，避免多个启动请求都返回成功。"""
        with _SYNC_SUBMIT_LOCK:
            if self._sync_running or sync_lock.locked():
                return False
            if not sync_lock.acquire(blocking=False):
                return False
            self._set_sync_status("starting", task_text, 0, {})
            try:
                Thread(
                    target=self.sync_subscribes,
                    kwargs={**sync_kwargs, "lock_acquired": True},
                    daemon=True,
                    name=thread_name,
                ).start()
            except Exception:
                sync_lock.release()
                self._set_sync_status("idle", "当前没有订阅处理任务", 0, {})
                raise
        return True

    @staticmethod
    def _manual_resource_type(link: str, default: str) -> str:
        value = str(link or "").lower()
        for marker, resource_type in (
                ("quark", "quark"), ("189.cn", "tianyi"),
                ("cloud.189", "tianyi"), ("guangya", "guangya"),
                ("123pan", "123"), ("123.cn", "123"),
                ("123684.com", "123"), ("123865.com", "123"),
                ("alipan.com", "alipan"), ("aliyundrive.com", "alipan"),
        ):
            if marker in value:
                return resource_type
        return default

    def _manual_share_service(self, resource_type: str):
        registry = getattr(self, "_cloud_drive_registry", None)
        if registry:
            try:
                return registry.get({
                                        "189": "tianyi", "aliyun": "alipan"
                                    }.get(resource_type, resource_type)).require(
                    CloudDriveCapability.SHARE_TRANSFER
                )
            except (KeyError, RuntimeError):
                return None
        if self._cloud_drive and resource_type == self._cloud_drive.key:
            return self._share_transfer
        return None

    @staticmethod
    def _manual_share_info_valid(resource_type: str, share_info: Dict[str, Any]) -> bool:
        """分享提取码是可选字段，语法校验只要求可解析分享标识。"""
        return bool(share_info.get("share_code"))

    @staticmethod
    def _manual_resource_name(resource_type: str) -> str:
        return {
            "115": "115",
            "123": "123",
            "quark": "夸克",
            "guangya": "光鸭",
            "tianyi": "天翼",
            "aliyun": "阿里云盘",
        }.get(resource_type, resource_type.upper() or "未知网盘")

    def api_vue_start_sync(
            self,
            payload: Optional[Dict[str, Any]] = None,
            wait: bool = False,
    ) -> dict:
        if self._sync_running or sync_lock.locked():
            return {"success": False, "message": "已有订阅任务正在运行"}
        payload = payload or {}
        raw_subscribe_ids = payload.get("subscribe_ids") or []
        raw_targets = payload.get("history_targets") or []
        try:
            selected_count = max(0, int(payload.get("selected_count") or 0))
        except (TypeError, ValueError):
            selected_count = 0
        selection_requested = bool(
            selected_count or raw_subscribe_ids or raw_targets
        )
        if not isinstance(raw_subscribe_ids, list) or not isinstance(raw_targets, list):
            return {"success": False, "message": "立即搜索范围参数无效"}
        if len(raw_subscribe_ids) > 200 or len(raw_targets) > 200:
            return {"success": False, "message": "单次最多选择 200 个历史媒体"}

        subscribe_ids = set()
        if selection_requested:
            with SessionFactory() as db:
                subscribes = SubscribeOper(db=db).list() or []
            supported_types = {MediaType.TV.value, MediaType.MOVIE.value}
            subscriptions_by_id = {
                int(subscribe.id): subscribe
                for subscribe in subscribes
                if int(getattr(subscribe, "id", 0) or 0) > 0
                   and getattr(subscribe, "type", None) in supported_types
            }
            for value in raw_subscribe_ids:
                try:
                    subscribe_id = int(value or 0)
                except (TypeError, ValueError):
                    continue
                if subscribe_id in subscriptions_by_id:
                    subscribe_ids.add(subscribe_id)

            media_type_values = {
                "tv": MediaType.TV.value,
                "电视剧": MediaType.TV.value,
                "movie": MediaType.MOVIE.value,
                "电影": MediaType.MOVIE.value,
            }
            for target in raw_targets:
                if not isinstance(target, dict):
                    continue
                try:
                    tmdb_id = int(target.get("tmdb_id") or 0)
                except (TypeError, ValueError):
                    tmdb_id = 0
                media_type = media_type_values.get(
                    str(target.get("media_type") or "").strip().lower(), ""
                )
                title = " ".join(
                    str(target.get("title") or "").strip().casefold().split()
                )
                year = str(target.get("year") or "").strip()
                seasons = set()
                for value in target.get("seasons") or []:
                    try:
                        season = int(value or 0)
                    except (TypeError, ValueError):
                        continue
                    if season > 0:
                        seasons.add(season)

                for subscribe_id, subscribe in subscriptions_by_id.items():
                    if media_type and getattr(subscribe, "type", None) != media_type:
                        continue
                    try:
                        subscribe_tmdb_id = int(
                            getattr(subscribe, "tmdbid", 0) or 0
                        )
                    except (TypeError, ValueError):
                        subscribe_tmdb_id = 0
                    if tmdb_id > 0:
                        if subscribe_tmdb_id != tmdb_id:
                            continue
                    else:
                        subscribe_title = " ".join(
                            str(getattr(subscribe, "name", "") or "")
                            .strip().casefold().split()
                        )
                        subscribe_year = str(
                            getattr(subscribe, "year", "") or ""
                        ).strip()
                        if not title or subscribe_title != title:
                            continue
                        if year and subscribe_year and subscribe_year != year:
                            continue
                    if media_type == MediaType.TV.value and seasons:
                        try:
                            subscribe_season = int(
                                getattr(subscribe, "season", 1) or 1
                            )
                        except (TypeError, ValueError):
                            subscribe_season = 1
                        if subscribe_season not in seasons:
                            continue
                    subscribe_ids.add(subscribe_id)

            if not subscribe_ids:
                return {
                    "success": False,
                    "message": "所选历史记录未匹配到有效订阅，未启动全部搜索",
                }

        selected_ids = sorted(subscribe_ids) if selection_requested else None
        sync_kwargs = {"subscribe_ids": selected_ids}
        if wait:
            result: Dict[str, Any] = {}
            self.sync_subscribes(**sync_kwargs, result=result)
            data = dict(result.get("data") or {})
            data.update({
                "scope": "selected" if selection_requested else "all",
                "subscribe_count": len(selected_ids or []),
            })
            result["data"] = data
            return result
        if not self._start_sync_thread(
                sync_kwargs,
                thread_name="cloudsubscribe-subscribe-sync",
                task_text="正在准备订阅搜索任务",
        ):
            return {"success": False, "message": "已有订阅任务正在运行"}
        if selection_requested:
            message = f"已按所选历史记录启动 {len(selected_ids)} 个订阅的搜索"
            scope = "selected"
        else:
            message = "全部订阅搜索任务已启动"
            scope = "all"
        return {
            "success": True,
            "message": message,
            "data": {"scope": scope, "subscribe_count": len(selected_ids or [])},
        }

    def api_vue_start_manual_sync(
            self, payload: Dict[str, Any], wait: bool = False
    ) -> dict:
        """校验指定订阅和资源链接后进入现有转存流程。"""
        if self._sync_running or sync_lock.locked():
            return {"success": False, "message": "已有订阅任务正在运行"}
        try:
            subscribe_id = int((payload or {}).get("subscribe_id") or 0)
        except (TypeError, ValueError):
            subscribe_id = 0
        media_target = None
        if subscribe_id <= 0:
            raw_media = (payload or {}).get("media") or {}
            try:
                tmdb_id = int(raw_media.get("tmdb_id") or 0)
                media_type = str(raw_media.get("media_type") or "").strip().lower()
                title = str(raw_media.get("title") or "").strip()
                season = max(1, int(raw_media.get("season") or 1))
                episode_start = max(1, int(raw_media.get("episode_start") or 1))
                episode_end = max(
                    episode_start, int(raw_media.get("episode_end") or episode_start)
                )
            except (TypeError, ValueError):
                return {"success": False, "message": "TMDB 媒体范围格式错误"}
            if tmdb_id <= 0 or media_type not in {"movie", "tv"} or not title:
                return {"success": False, "message": "请选择订阅或有效的 TMDB 媒体"}
            media_target = {
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "title": title,
                "year": raw_media.get("year"),
                "season": season if media_type == "tv" else None,
                "episode_start": episode_start if media_type == "tv" else None,
                "episode_end": episode_end if media_type == "tv" else None,
            }

        raw_links = (payload or {}).get("resource_links") or []
        if isinstance(raw_links, str):
            raw_links = raw_links.splitlines()
        if not isinstance(raw_links, list):
            return {"success": False, "message": "资源链接格式错误"}

        links = []
        for value in raw_links:
            link = str(value or "").strip()
            if link and link not in links:
                links.append(link)
        if not links:
            return {"success": False, "message": "请至少填写一个资源链接"}
        if len(links) > 50:
            return {"success": False, "message": "单次最多处理 50 个资源链接"}

        if subscribe_id > 0:
            with SessionFactory() as db:
                subscribe = SubscribeOper(db=db).get(subscribe_id)
            if not subscribe:
                return {"success": False, "message": "指定订阅不存在"}
            if subscribe.type not in {MediaType.TV.value, MediaType.MOVIE.value}:
                return {"success": False, "message": "仅支持电影或电视剧订阅"}

        share_transfer = None
        offline_download = None
        if self._cloud_drive:
            if self._cloud_drive.supports(CloudDriveCapability.SHARE_TRANSFER):
                share_transfer = self._cloud_drive.require(
                    CloudDriveCapability.SHARE_TRANSFER
                )
            if self._cloud_drive.supports(CloudDriveCapability.OFFLINE_DOWNLOAD):
                offline_download = self._cloud_drive.require(
                    CloudDriveCapability.OFFLINE_DOWNLOAD
                )
        magnet_links = [
            link for link in links
            if offline_download and offline_download.is_magnet_url(link)
        ]
        magnet_info_by_url = {}
        if magnet_links:
            with ThreadPoolExecutor(
                    max_workers=min(3, len(magnet_links)),
                    thread_name_prefix="cloudsubscribe-magnet-metadata",
            ) as executor:
                results = executor.map(
                    lambda value: offline_download.parse_magnet_link(
                        value, fetch_metadata=True
                    ),
                    magnet_links,
                )
                magnet_info_by_url = dict(zip(magnet_links, results))

        resources = []
        invalid_links = []
        for index, link in enumerate(links, start=1):
            if offline_download and offline_download.is_ed2k_url(link):
                resource_type = "ed2k"
                valid = bool(offline_download.parse_ed2k_link(link))
            elif offline_download and offline_download.is_magnet_url(link):
                resource_type = "magnet"
                magnet_info = magnet_info_by_url.get(link)
                valid = bool(
                    magnet_info
                    and (magnet_info.get("metadata") or {}).get("metadata_available")
                )
            else:
                resource_type = self._manual_resource_type(link, self._cloud_drive.key)
                share_service = self._manual_share_service(resource_type)
                if not share_service:
                    invalid_links.append(
                        (
                            index,
                            f"{self._manual_resource_name(resource_type)}分享源尚未接入，"
                            "暂不支持手动转存",
                        )
                    )
                    continue
                share_info = share_service.extract_share_info(link)
                valid = self._manual_share_info_valid(resource_type, share_info)
            if not valid:
                reason = (
                    "Magnet 必须能解析出名称或完整文件元数据"
                    if resource_type == "magnet"
                    else (
                        f"无法解析有效的 {self._manual_resource_name(resource_type)}"
                        "分享链接"
                    )
                )
                invalid_links.append((index, reason))
                continue
            resources.append({
                "url": link,
                "title": f"手动添加 {index}",
                "resource_type": resource_type,
                "source": "manual",
                "source_url": link,
                "unlock_points": 0,
                **(
                    {"magnet_metadata": magnet_info["metadata"]}
                    if resource_type == "magnet" else {}
                ),
            })
        if invalid_links:
            return {
                "success": False,
                "message": "；".join(
                    f"第 {index} 行资源无效：{reason}"
                    for index, reason in invalid_links
                ),
            }

        order = {value: index for index, value in enumerate(self._resource_type_order)}
        resources.sort(key=lambda item: order.get(item["resource_type"], len(order)))
        sync_kwargs = {
            "subscribe_id": subscribe_id or None,
            "manual_resources": resources,
            "manual_target": media_target,
            "manual_upgrade": bool((payload or {}).get("manual_upgrade")),
        }
        if wait:
            result: Dict[str, Any] = {}
            self.sync_subscribes(**sync_kwargs, result=result)
            data = dict(result.get("data") or {})
            data["resource_count"] = len(resources)
            result["data"] = data
            return result
        if not self._start_sync_thread(
                sync_kwargs,
                thread_name="cloudsubscribe-manual-sync",
                task_text="正在准备手动添加任务",
        ):
            return {"success": False, "message": "已有订阅任务正在运行"}
        return {
            "success": True,
            "message": (
                f"手动添加任务已提交，共 {len(resources)} 条资源，开始自动处理"
                if subscribe_id
                else f"无订阅媒体任务已提交，共 {len(resources)} 条资源，开始自动处理"
            ),
        }

    def start_selected_resources(
            self,
            subscribe_id: int,
            resources: list[Dict[str, Any]],
    ) -> dict:
        """将智能体会话缓存中的原始候选直接送入现有同步链。"""
        if self._sync_running or sync_lock.locked():
            return {"success": False, "message": "已有订阅任务正在运行"}
        try:
            subscribe_id = int(subscribe_id or 0)
        except (TypeError, ValueError):
            subscribe_id = 0
        if subscribe_id <= 0:
            return {"success": False, "message": "请选择订阅"}
        with SessionFactory() as db:
            subscribe = SubscribeOper(db=db).get(subscribe_id)
        if not subscribe:
            return {"success": False, "message": "指定订阅不存在"}
        if subscribe.type not in {MediaType.TV.value, MediaType.MOVIE.value}:
            return {"success": False, "message": "仅支持电影或电视剧订阅"}

        selected = []
        for resource in list(resources or [])[:20]:
            item = dict(resource or {})
            resource_type = str(
                item.get("resource_type") or item.get("pan_type") or ""
            ).strip().lower()
            has_direct_url = bool(str(item.get("url") or "").strip())
            can_unlock = bool(
                item.get("need_unlock")
                and item.get("slug")
            )
            if not has_direct_url and not can_unlock:
                continue
            item["resource_type"] = resource_type
            selected.append(item)
        if not selected:
            return {"success": False, "message": "没有可处理的候选资源"}

        if not self._start_sync_thread(
                {
                "subscribe_id": subscribe_id,
                "manual_resources": selected,
            },
                thread_name="cloudsubscribe-agent-resource",
                task_text="正在准备候选资源任务",
        ):
            return {"success": False, "message": "已有订阅任务正在运行"}
        return {
            "success": True,
            "message": f"已提交 {len(selected)} 个候选资源，开始按现有规则处理",
            "data": {"submitted": len(selected)},
        }
