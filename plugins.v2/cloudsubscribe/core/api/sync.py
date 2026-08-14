"""订阅同步任务提交 API。"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper
from app.schemas.types import MediaType

from .. import CloudDriveCapability, OwnerDelegator


class SyncApi(OwnerDelegator):
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

    @staticmethod
    def _positive_ints(values: Any) -> List[int]:
        result = set()
        if isinstance(values, dict):
            values = values.keys()
        elif not isinstance(values, (list, tuple, set)):
            values = []
        for value in values:
            try:
                normalized = int(value or 0)
            except (TypeError, ValueError):
                continue
            if normalized > 0:
                result.add(normalized)
        return sorted(result)

    @classmethod
    def _normalize_history_search_targets(
            cls, targets: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """将历史媒体组展开为同步链可直接处理的电影或单季电视剧目标。"""
        media_type_values = {
            "tv": MediaType.TV.value,
            "电视剧": MediaType.TV.value,
            "movie": MediaType.MOVIE.value,
            "电影": MediaType.MOVIE.value,
        }
        normalized: Dict[tuple, Dict[str, Any]] = {}
        for target in targets:
            if not isinstance(target, dict):
                continue
            try:
                tmdb_id = int(target.get("tmdb_id") or 0)
            except (TypeError, ValueError):
                tmdb_id = 0
            title = str(target.get("title") or "").strip()
            media_type = media_type_values.get(
                str(target.get("media_type") or "").strip().lower(), ""
            )
            if tmdb_id <= 0 or not title or not media_type:
                continue

            base = {
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "title": title,
                "year": target.get("year") or "",
            }
            if media_type == MediaType.MOVIE.value:
                normalized.setdefault((media_type, tmdb_id, 0), base)
                continue

            season_episodes = target.get("season_episodes") or {}
            if not isinstance(season_episodes, dict):
                season_episodes = {}
            seasons = set(cls._positive_ints(target.get("seasons") or []))
            seasons.update(cls._positive_ints(season_episodes.keys()))
            try:
                single_season = int(target.get("season") or 0)
            except (TypeError, ValueError):
                single_season = 0
            if single_season > 0:
                seasons.add(single_season)
            fallback_episodes = cls._positive_ints(target.get("episodes") or [])
            for season in sorted(seasons):
                episodes = cls._positive_ints(
                    season_episodes.get(str(season))
                    or season_episodes.get(season)
                    or (fallback_episodes if len(seasons) == 1 else [])
                )
                if not episodes:
                    continue
                key = (media_type, tmdb_id, season)
                existing = normalized.get(key)
                if existing:
                    existing["episodes"] = sorted(
                        set(existing.get("episodes") or []) | set(episodes)
                    )
                    continue
                normalized[key] = {
                    **base,
                    "season": season,
                    "episodes": episodes,
                }
        return list(normalized.values())

    def api_vue_start_sync(
            self,
            payload: Optional[Dict[str, Any]] = None,
            wait: bool = False,
    ) -> dict:
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
        history_search_targets: List[Dict[str, Any]] = []
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

            normalized_targets = self._normalize_history_search_targets(raw_targets)
            if len(normalized_targets) > 500:
                return {"success": False, "message": "所选历史记录包含的媒体季数过多"}
            for target in normalized_targets:
                tmdb_id = int(target["tmdb_id"])
                media_type = str(target["media_type"])
                title = " ".join(
                    str(target.get("title") or "").strip().casefold().split()
                )
                year = str(target.get("year") or "").strip()
                season = int(target.get("season") or 0)
                matched_ids = set()
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
                    if media_type == MediaType.TV.value:
                        try:
                            subscribe_season = int(
                                getattr(subscribe, "season", 1) or 1
                            )
                        except (TypeError, ValueError):
                            subscribe_season = 1
                        if subscribe_season != season:
                            continue
                    matched_ids.add(subscribe_id)

                if matched_ids:
                    subscribe_ids.update(matched_ids)
                else:
                    history_search_targets.append(target)

            if not subscribe_ids and not history_search_targets:
                return {
                    "success": False,
                    "message": "所选历史记录缺少可搜索的媒体或集数信息",
                }

        selected_ids = sorted(subscribe_ids) if selection_requested else None
        sync_kwargs = {
            "subscribe_ids": selected_ids,
            "history_search_targets": history_search_targets or None,
        }
        history_target_count = len(history_search_targets)
        media_count = len(selected_ids or []) + history_target_count
        if wait:
            result: Dict[str, Any] = {}
            future, _ = self._submit_sync_operation(
                {**sync_kwargs, "result": result},
                "页面订阅搜索",
            )
            future.result()
            data = dict(result.get("data") or {})
            data.update({
                "scope": "selected" if selection_requested else "all",
                "subscribe_count": len(selected_ids or []),
                "history_target_count": history_target_count,
                "media_count": media_count,
            })
            result["data"] = data
            return result
        queue_position = self.queue_sync_operation(
            sync_kwargs,
            "页面订阅搜索",
        )
        if selection_requested:
            parts = []
            if selected_ids:
                parts.append(f"{len(selected_ids)} 个订阅")
            if history_target_count:
                parts.append(f"{history_target_count} 个历史媒体目标")
            message = f"已按所选历史记录提交{'和'.join(parts)}的搜索"
            scope = "selected"
        else:
            message = "全部订阅搜索任务已提交"
            scope = "all"
        return {
            "success": True,
            "message": message,
            "data": {
                "scope": scope,
                "subscribe_count": len(selected_ids or []),
                "history_target_count": history_target_count,
                "media_count": media_count,
                "queue_position": queue_position,
            },
        }

    def api_vue_start_manual_sync(
            self, payload: Dict[str, Any], wait: bool = False
    ) -> dict:
        """校验指定订阅和资源链接后进入现有转存流程。"""
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
            except (TypeError, ValueError):
                return {"success": False, "message": "TMDB 媒体信息格式错误"}
            if tmdb_id <= 0 or media_type not in {"movie", "tv"} or not title:
                return {"success": False, "message": "请选择订阅或有效的 TMDB 媒体"}
            seasons = []
            if media_type == "tv":
                raw_seasons = raw_media.get("seasons")
                if raw_seasons is None:
                    raw_seasons = []
                elif not isinstance(raw_seasons, (list, tuple, set)):
                    return {"success": False, "message": "季数格式错误"}
                try:
                    seasons = sorted({
                        int(value) for value in raw_seasons
                        if int(value) > 0
                    })
                except (TypeError, ValueError):
                    return {"success": False, "message": "季数格式错误"}
                if not seasons:
                    seasons = self._resolve_tmdb_seasons(raw_media)
                if not seasons:
                    return {"success": False, "message": "未查询到 TMDB 真实季信息"}
                if seasons[-1] > 999:
                    return {"success": False, "message": "请选择 1 到 999 之间的季"}
            media_target = {
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "title": title,
                "year": raw_media.get("year"),
                "seasons": seasons,
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
        if len(links) > 50:
            return {"success": False, "message": "单次最多处理 50 个资源链接"}

        raw_cloud_path = str((payload or {}).get("cloud_path") or "").strip()
        cloud_path = ""
        cloud_provider = ""
        cloud_drive = None
        target_provider = ""
        if raw_cloud_path:
            cloud_parts = [
                part for part in raw_cloud_path.replace("\\", "/").split("/")
                if part
            ]
            if any(part in {".", ".."} for part in cloud_parts):
                return {"success": False, "message": "网盘资源路径格式错误"}
            cloud_path = str(PurePosixPath(
                "/" + "/".join(cloud_parts)
            ))
            target_provider = str(
                getattr(self._cloud_drive, "key", "") or ""
            ).strip().lower()
            cloud_provider = str(
                (payload or {}).get("cloud_provider") or target_provider
            ).strip().lower()
            try:
                cloud_drive = (
                    self._cloud_drive_registry.get(cloud_provider)
                    if self._cloud_drive_registry and cloud_provider
                    else self._cloud_drive
                )
            except KeyError:
                return {"success": False, "message": "所选网盘提供方不存在"}
            if not cloud_drive or not cloud_drive.supports(
                    CloudDriveCapability.DIRECTORY_READ
            ):
                return {"success": False, "message": "所选网盘不支持目录浏览"}
            if cloud_provider != target_provider:
                cross_ready = bool(
                    self._cross_transfer_enabled
                    and cloud_drive.supports(CloudDriveCapability.FILE_QUERY)
                    and cloud_drive.supports(CloudDriveCapability.FILE_DOWNLOAD)
                    and self._cloud_drive
                    and self._cloud_drive.supports(CloudDriveCapability.LOCAL_UPLOAD)
                    and self._cloud_drive.supports(CloudDriveCapability.FILE_QUERY)
                )
                if not cross_ready:
                    return {"success": False, "message": "所选网盘未满足跨盘转存条件"}
            directory_service = cloud_drive.require(
                CloudDriveCapability.DIRECTORY_READ
            )
            try:
                lookup = directory_service.resolve_directory(cloud_path)
            except Exception as error:
                return {
                    "success": False,
                    "message": f"读取网盘资源路径失败：{error}",
                }
            if not lookup.checked:
                return {"success": False, "message": "读取网盘资源路径失败"}
            if lookup.directory_id is None:
                return {"success": False, "message": "所选网盘资源路径不存在"}
        if not links and not cloud_path:
            return {"success": False, "message": "请填写资源链接或选择网盘路径"}

        subscribe = None
        if subscribe_id > 0:
            with SessionFactory() as db:
                subscribe = SubscribeOper(db=db).get(subscribe_id)
            if not subscribe:
                return {"success": False, "message": "指定订阅不存在"}
            if subscribe.type not in {MediaType.TV.value, MediaType.MOVIE.value}:
                return {"success": False, "message": "仅支持电影或电视剧订阅"}
        if cloud_path and cloud_provider != target_provider:
            requested_media_type = (
                str(media_target.get("media_type") or "").strip().lower()
                if media_target
                else {
                    MediaType.MOVIE.value: "movie",
                    MediaType.TV.value: "tv",
                }.get(getattr(subscribe, "type", None), "")
            )
            allowed_cross_types = {
                str(value or "").strip().lower()
                for value in getattr(self, "_cross_transfer_media_types", set())
            }
            if requested_media_type not in allowed_cross_types:
                return {
                    "success": False,
                    "message": "当前媒体类型未启用跨盘转存",
                }

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
        skip_history = bool((payload or {}).get("skip_history"))
        if cloud_path:
            provider_name = str(getattr(cloud_drive, "name", cloud_provider) or cloud_provider)
            resources.append({
                "url": f"cloud://{cloud_provider}{quote(cloud_path, safe='/')}",
                "title": f"{provider_name}路径 {cloud_path}",
                "resource_type": "cloud",
                "source": "manual",
                "cloud_path": cloud_path,
                "cloud_provider": cloud_provider,
                "unlock_points": 0,
                "skip_history": skip_history,
            })
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
                "unlock_points": 0,
                "skip_history": skip_history,
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
        queue_media = media_target or {
            "title": getattr(subscribe, "name", "") if subscribe else "",
            "media_type": (
                "tv"
                if getattr(subscribe, "type", None) == MediaType.TV.value
                else "movie"
            ),
            "seasons": (
                [int(getattr(subscribe, "season", 1) or 1)]
                if subscribe
                   and getattr(subscribe, "type", None) == MediaType.TV.value
                else []
            ),
        }
        queue_title = str(queue_media.get("title") or "").strip()
        queue_seasons = self._positive_ints(queue_media.get("seasons") or [])
        season_text = (
            " " + "/".join(f"S{value:02d}" for value in queue_seasons)
            if queue_seasons else ""
        )
        queue_label = (
            f"手动添加资源：{queue_title}{season_text}"
            if queue_title else "手动添加资源"
        )
        if wait:
            result: Dict[str, Any] = {}
            future, _ = self._submit_sync_operation(
                {**sync_kwargs, "result": result},
                queue_label,
            )
            future.result()
            data = dict(result.get("data") or {})
            data["resource_count"] = len(resources)
            result["data"] = data
            return result
        queue_position = self.queue_sync_operation(
            sync_kwargs,
            queue_label,
        )
        return {
            "success": True,
            "message": (
                f"手动添加任务已排队，共 {len(resources)} 条资源"
                if subscribe_id
                else f"无订阅媒体任务已排队，共 {len(resources)} 条资源"
            ),
            "data": {"queue_position": queue_position},
        }

    def start_selected_resources(
            self,
            subscribe_id: int,
            resources: list[Dict[str, Any]],
    ) -> dict:
        """将智能体会话缓存中的原始候选直接送入现有同步链。"""
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
                and item.get("resource_ref")
            )
            if not has_direct_url and not can_unlock:
                continue
            item["resource_type"] = resource_type
            selected.append(item)
        if not selected:
            return {"success": False, "message": "没有可处理的候选资源"}

        queue_position = self.queue_sync_operation(
            {
                "subscribe_id": subscribe_id,
                "manual_resources": selected,
            },
            f"候选资源处理：{subscribe.name}",
        )
        return {
            "success": True,
            "message": f"已提交 {len(selected)} 个候选资源，开始按现有规则处理",
            "data": {"submitted": len(selected), "queue_position": queue_position},
        }
