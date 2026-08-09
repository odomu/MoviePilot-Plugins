"""搜索源测试与 TMDB 候选查询 API。"""

import ast
import re
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from app.core.config import settings
from app.core.metainfo import MetaInfo
from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType
from app.utils.string import StringUtils

from .. import OwnerDelegator
from ..cloud import CloudDriveCapability
from ..config import UIConfig
from ...search.hdhive import HDHIVE_DETAIL_RESOURCE_TYPES
from ...search.types import (
    PREVIEW_PROVIDER_KEYS,
    PREVIEW_RESOURCE_TYPES,
    RESOURCE_TYPE_PRIORITY,
    SUPPORTED_RESOURCE_TYPES,
    normalize_resource_type,
    resource_type_from_url,
    resource_type_name,
)
from ...utils import parse_magnet_metadata


class SearchApi(OwnerDelegator):
    _SEARCH_TEST_DISPLAY_LIMIT = 10
    _TEST_HDHIVE_CLIENT_LIMIT = 4
    _SEARCH_TEST_CONFIG_FIELDS = {
        "pansou": frozenset({
            "pansou_url", "pansou_username", "pansou_password",
            "pansou_auth_enabled", "pansou_channels", "pansou_plugins",
            "pansou_filter_include",
            "pansou_filter_exclude", "pansou_concurrency",
            "pansou_result_limit", "pansou_timeout",
        }),
        "hdhive": frozenset({
            "hdhive_base_url",
            "hdhive_query_mode", "hdhive_api_key", "hdhive_client_id",
            "hdhive_access_token", "hdhive_refresh_token",
            "hdhive_token_expires_at", "hdhive_username", "hdhive_password",
            "hdhive_candidate_limit", "hdhive_request_interval",
            "hdhive_unlocks_per_minute", "hdhive_torrentclaw_enabled",
            "hdhive_torrentclaw_subtitle_languages",
        }),
        "dian115": frozenset({
            "dian115_base_url",
            "dian115_email", "dian115_password", "dian115_candidate_limit",
            "dian115_request_interval", "dian115_unlocks_per_minute",
        }),
        "juying": frozenset({
            "juying_base_url",
            "juying_username", "juying_password", "juying_result_limit",
            "juying_request_interval",
        }),
        "seedhub": frozenset({"seedhub_result_limit"}),
        "butailing": frozenset({"butailing_result_limit"}),
        "pinglian": frozenset({
            "pinglian_username", "pinglian_password", "pinglian_result_limit",
            "pinglian_request_interval", "pinglian_timeout",
        }),
        "online_docs": frozenset({
            "online_docs", "online_docs_urls", "online_docs_resource_types"
        }),
    }

    def __init__(self, owner):
        super().__init__(owner)
        object.__setattr__(self, "_test_hdhive_clients_lock", threading.RLock())
        object.__setattr__(self, "_test_hdhive_clients", [])

    def close(self) -> None:
        """释放测试接口复用的 HDHive 认证连接。"""
        with self._test_hdhive_clients_lock:
            clients = list(self._test_hdhive_clients)
            self._test_hdhive_clients.clear()
        for client in clients:
            try:
                client.close()
            except Exception as error:
                logger.debug(f"关闭 HDHive 测试认证连接失败：{error}")

    def _get_test_hdhive_web_client(
            self,
            config: Dict[str, Any],
            proxy: Any,
    ) -> tuple:
        """按账号和网络配置复用测试连接及内存安全会话。"""
        from ...search.hdhive import HDHiveClient

        username = str(config.get("hdhive_username") or "")
        password = str(config.get("hdhive_password") or "")
        request_interval = float(
            config.get("hdhive_request_interval", 5) or 5
        )
        unlocks_per_minute = int(
            config.get("hdhive_unlocks_per_minute", 2) or 2
        )
        with self._test_hdhive_clients_lock:
            for client in self._test_hdhive_clients:
                if client.matches_config(
                        username,
                        password,
                        proxy,
                        request_interval,
                        unlocks_per_minute,
                ):
                    return client, False
            client = HDHiveClient(
                username=username,
                password=password,
                proxy=proxy,
                request_interval=request_interval,
                unlocks_per_minute=unlocks_per_minute,
            )
            if len(self._test_hdhive_clients) >= self._TEST_HDHIVE_CLIENT_LIMIT:
                logger.debug(
                    "HDHive 测试连接配置超过缓存上限，本次使用临时认证连接"
                )
                return client, True
            self._test_hdhive_clients.append(client)
            logger.debug("HDHive 测试接口已建立可复用认证连接")
            return client, False

    @staticmethod
    def _preview_error_message(error: Exception) -> str:
        """优先返回第三方异常携带的结构化错误信息。"""
        for value in reversed(getattr(error, "args", ())):
            if not isinstance(value, dict):
                continue
            message = value.get("message") or value.get("msg") or value.get("error")
            if message:
                return str(message)
        return str(error) or error.__class__.__name__

    @staticmethod
    def _preview_file(item: Any) -> Dict[str, Any]:
        if not isinstance(item, dict):
            item = getattr(item, "__dict__", {}) or {}
        name = next((str(item.get(key) or "").strip() for key in
                     ("name", "path", "file_name", "filename", "fileName")
                     if item.get(key)), "")
        size = next((item.get(key) for key in
                     ("size", "file_size", "fileSize")
                     if item.get(key) is not None), 0)
        is_dir = bool(item.get("is_dir") or item.get("is_folder")
                      or item.get("isDirectory") or item.get("type") == "folder")
        file_id = next((str(item.get(key) or "").strip() for key in
                        ("id", "file_id", "fileId", "fid", "cid")
                        if item.get(key) is not None), "")
        return {
            "id": file_id,
            "name": name or "未命名文件",
            "size": size or 0,
            "is_dir": is_dir,
            "can_enter": bool(is_dir and file_id),
        }

    def api_vue_preview_search_resource(self, payload: Dict[str, Any]) -> dict:
        """只读获取测试资源的文件列表。"""
        payload = dict(payload or {})
        source = str(payload.get("source") or "").strip().lower()
        juying_resource_id = str(
            payload.get("juying_resource_id") or ""
        ).strip()
        resource_type = normalize_resource_type(payload.get("resource_type"))
        url = str(payload.get("url") or "").strip()
        slug = str(payload.get("slug") or "").strip()
        is_unlocked = bool(payload.get("is_unlocked"))
        parent_id = str(payload.get("parent_id") or "").strip()
        pending_juying = source == "juying" and bool(juying_resource_id)
        valid_hdhive_url = bool(
            url and "\\" not in url
            and resource_type_from_url(url) == resource_type
        )
        pending_hdhive = (
                source == "hdhive"
                and bool(slug)
                and (not url or (is_unlocked and not valid_hdhive_url))
        )
        if (not url and not pending_juying and not pending_hdhive) or len(url) > 8192:
            return {"success": False, "message": "资源链接无效"}
        if pending_juying and (
                len(juying_resource_id) > 32
                or not juying_resource_id.isdigit()
        ):
            return {"success": False, "message": "聚影资源标识无效"}
        if len(parent_id) > 256:
            return {"success": False, "message": "目录标识无效"}
        if pending_hdhive and (
                resource_type not in HDHIVE_DETAIL_RESOURCE_TYPES
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", slug)
        ):
            return {"success": False, "message": "HDHive 资源标识或类型无效"}
        try:
            if pending_hdhive:
                url = ""
                if parent_id:
                    return {
                        "success": False,
                        "message": "HDHive file-list 不支持目录导航",
                    }
                handler = self._build_test_search_handler(
                    "hdhive",
                    self._test_search_config("hdhive", payload.get("config")),
                )
                try:
                    if is_unlocked:
                        url = handler.unlock_hdhive_resource(
                            slug=slug,
                            unlock_points=0,
                            resource_type=resource_type,
                            is_unlocked=True,
                            target_season=payload.get("target_season"),
                            target_episodes=payload.get("target_episodes"),
                            supports_file_preview=payload.get(
                                "supports_file_preview"
                            ),
                            detail_path=str(payload.get("detail_path") or ""),
                            search_label="测试已解锁预览",
                        )
                    else:
                        preview = handler.preview_hdhive_resource(
                            slug=slug,
                            resource_type=resource_type,
                            target_season=payload.get("target_season"),
                            target_episodes=payload.get("target_episodes"),
                            supports_file_preview=payload.get(
                                "supports_file_preview"
                            ),
                            detail_path=str(payload.get("detail_path") or ""),
                            search_label="测试只读预览",
                        )
                finally:
                    handler.close(release_cache=False)
                if is_unlocked:
                    url = str(url or "").strip()
                    if not url:
                        raise RuntimeError("HDHive 已解锁资源页未解析到分享链接")
                else:
                    files = [
                        {
                            **self._preview_file(item),
                            "can_enter": False,
                        }
                        for item in (preview.get("files") or [])
                    ][:500]
                    return {
                        "success": True,
                        "message": f"只读预览到 {len(files)} 个项目，未执行解锁",
                        "data": {
                            "items": files,
                            "count": len(files),
                            "provider_name": "HDHive",
                            "resource_type": resource_type,
                            "resource_type_name": resource_type_name(
                                resource_type, resource_type.upper()
                            ),
                            "share_url": "",
                            "parent_id": "",
                            "preview_episodes": preview.get("preview_episodes") or {},
                            "covers_target": preview.get("covers_target"),
                            "resource_validate_status": preview.get(
                                "resource_validate_status"
                            ) or "",
                            "resource_validate_message": preview.get(
                                "resource_validate_message"
                            ) or "",
                        },
                    }
            if pending_juying and not url:
                if parent_id:
                    return {"success": False, "message": "聚影资源链接已失效，请重新预览"}
                handler = self._build_test_search_handler(
                    "juying",
                    self._test_search_config("juying", payload.get("config")),
                )
                try:
                    resolved = handler.resolve_juying_resource(juying_resource_id)
                finally:
                    handler.close(release_cache=False)
                url = str(resolved.get("url") or "").strip()
                resource_type = normalize_resource_type(
                    resolved.get("resource_type")
                )
                if not url:
                    raise RuntimeError("聚影资源链接为空")
            if resource_type == "magnet":
                if parent_id:
                    return {"success": False, "message": "磁力链接不支持目录导航"}
                metadata = parse_magnet_metadata(url, fetch_info=True, timeout=12)
                files = [
                    {"name": str(entry.get("path") or entry.get("name") or "未命名文件"),
                     "size": int(entry.get("size") or 0), "is_dir": False}
                    for entry in (metadata.get("torrent_file_entries") or [])
                ]
                if not files:
                    raise RuntimeError("暂未获取到该磁力链接的 torrent 元数据")
                return {
                    "success": True,
                    "message": f"读取到 {len(files)} 个文件",
                    "data": {
                        "items": files, "count": len(files),
                        "provider_name": "",
                        "resource_type": "magnet",
                        "resource_type_name": resource_type_name("magnet"),
                        "share_url": url,
                        "parent_id": "",
                        "info_hash": metadata.get("info_hash"),
                        "display_name": metadata.get("display_name"),
                        "size": metadata.get("size") or 0
                    }
                }

            if "\\" in url or resource_type_from_url(url) != resource_type:
                return {"success": False, "message": "资源链接格式或类型无效"}

            provider_key = PREVIEW_PROVIDER_KEYS.get(resource_type)
            if not provider_key or not self._cloud_drive_registry:
                return {"success": False, "message": "当前资源类型暂不支持内容预览"}
            provider = self._cloud_drive_registry.get(provider_key)
            if not provider or not provider.supports(CloudDriveCapability.SHARE_TRANSFER):
                return {"success": False, "message": "对应网盘未配置或不支持分享预览"}
            service = provider.require(CloudDriveCapability.SHARE_TRANSFER)
            raw_files = service.list_share_directory(url, parent_id=parent_id) or []
            files = [self._preview_file(item) for item in raw_files][:500]
            return {
                "success": True,
                "message": f"读取到 {len(files)} 个项目",
                "data": {
                    "items": files, "count": len(files),
                    "provider_name": provider.name,
                    "resource_type": resource_type,
                    "resource_type_name": resource_type_name(
                        resource_type, provider.name
                    ),
                    "share_url": url,
                    "parent_id": parent_id,
                }
            }
        except Exception as error:
            message = self._preview_error_message(error)
            logger.warning(f"测试资源预览失败：{resource_type} - {message}")
            return {"success": False, "message": f"预览失败：{message}"}

    def api_vue_unlock_search_resource(self, payload: Dict[str, Any]) -> dict:
        payload = dict(payload or {})
        source = str(payload.get("source") or "").strip().lower()
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        try:
            points = max(0, int(item.get("unlock_points") or 0))
            free_hdhive_access = (
                    source == "hdhive"
                    and points == 0
                    and bool(item.get("need_access"))
                    and bool(item.get("is_free") or item.get("is_unlocked"))
            )
            zero_point_hdhive_unlock = (
                    source == "hdhive"
                    and points == 0
                    and bool(item.get("need_unlock"))
                    and bool(item.get("is_free"))
            )
            if points <= 0 and not (
                    free_hdhive_access or zero_point_hdhive_unlock
            ):
                return {"success": False, "message": "该资源不需要积分解锁"}
            slug = str(item.get("slug") or item.get("id") or "").strip()
            resource_type = normalize_resource_type(item.get("resource_type"))
            if source == "hdhive" and (
                    not slug or resource_type not in HDHIVE_DETAIL_RESOURCE_TYPES
            ):
                return {"success": False, "message": "HDHive 资源标识或类型无效"}
            handler = self._build_test_search_handler(
                source,
                self._test_search_config(source, payload.get("config")),
                confirmed_hdhive_unlock_points=(
                    points if source == "hdhive" else 0
                ),
            )
            try:
                if source == "hdhive":
                    url = handler.unlock_hdhive_resource(
                        slug, points, resource_type,
                        str(item.get("media_page_url") or ""),
                        is_unlocked=bool(item.get("is_unlocked")),
                        target_season=item.get("target_season"),
                        target_episodes=item.get("target_episodes"),
                        supports_file_preview=item.get("supports_file_preview"),
                        detail_path=str(item.get("detail_path") or ""),
                    )
                elif source == "dian115":
                    url = handler.unlock_dian115_resource(
                        int(item.get("share_id") or 0), int(item.get("resource_id") or 0), points,
                    )
                else:
                    return {"success": False, "message": "当前搜索源不支持积分解锁"}
            finally:
                handler.close(release_cache=False)
            if not url:
                message = "资源链接获取失败" if free_hdhive_access else "资源解锁失败"
                return {"success": False, "message": message}
            message = "资源链接已获取" if free_hdhive_access else "资源已解锁"
            return {"success": True, "message": message, "data": {"url": url}}
        except Exception as error:
            logger.warning(f"测试资源解锁失败：{source} - {error}")
            return {"success": False, "message": f"解锁失败：{error}"}

    def _test_search_config(
            self, source: str, overrides: Any
    ) -> Dict[str, Any]:
        """仅合并当前渠道测试真正需要的配置字段。"""
        base = dict(UIConfig.get_default_config())
        if isinstance(self._applied_config, dict):
            base.update(self._applied_config)
        allowed = self._SEARCH_TEST_CONFIG_FIELDS[source] | {"resource_type_order"}
        if isinstance(overrides, dict):
            base.update({
                key: value for key, value in overrides.items() if key in allowed
            })
        return {key: base.get(key) for key in allowed}

    def _build_test_search_handler(
            self,
            source: str,
            config: Dict[str, Any],
            deadline: Optional[float] = None,
            confirmed_hdhive_unlock_points: int = 0,
    ):
        """使用当前表单配置创建隔离搜索器，不修改已保存配置或运行中服务。"""
        from ...handlers.search import SearchHandler
        from ...search.butailing import ButailingClient
        from ...search.hdhive import HDHiveOpenAPIClient
        from ...search.juying import JuyingClient
        from ...search.pansou import PanSouClient
        from ...search.pinglian import PinglianClient
        from ...search.seedhub import SeedHubClient
        from ...search.online_docs import OnlineDocumentClient

        def as_list(value: Any) -> list:
            if isinstance(value, list):
                return list(value)
            if value is None:
                return []
            return [value]

        proxy = settings.PROXY
        hdhive_query_mode = str(config.get("hdhive_query_mode") or "web")
        if hdhive_query_mode not in {"api", "web"}:
            hdhive_query_mode = "web"
        hdhive_client = None
        if source == "hdhive" and hdhive_query_mode == "api":
            hdhive_client = HDHiveOpenAPIClient(
                app_secret=str(config.get("hdhive_api_key") or ""),
                client_id=str(config.get("hdhive_client_id") or ""),
                access_token=str(config.get("hdhive_access_token") or ""),
                refresh_token=str(config.get("hdhive_refresh_token") or ""),
                token_expires_at=float(
                    config.get("hdhive_token_expires_at") or 0
                ),
                proxy=proxy,
                request_interval=float(
                    config.get("hdhive_request_interval", 5) or 5
                ),
            )
        resource_type_order = list(dict.fromkeys(
            str(value).strip().lower()
            for value in as_list(config.get("resource_type_order"))
            if str(value).strip().lower()
            in {
                "115", "123", "quark", "guangya", "tianyi", "alipan",
                "ed2k", "magnet",
            }
        ))
        # 测试只验证渠道原始候选，不继承当前目标网盘的优先级或类型白名单。
        # 各渠道仍会自行识别真实资源类型，但不会因目标盘配置而丢弃候选。
        if source in {
            "hdhive", "dian115", "juying", "seedhub",
            "butailing", "pinglian", "pansou",
        }:
            resource_type_order = [
                "115", "123", "quark", "guangya", "tianyi", "alipan",
                "ed2k", "magnet",
            ]
        if not resource_type_order:
            raise ValueError("请至少选择一种资源类型")

        def require(*keys: str) -> None:
            if any(not str(config.get(key) or "").strip() for key in keys):
                raise ValueError("搜索渠道账号配置不完整")

        if source == "pansou":
            require("pansou_url")
            if bool(config.get("pansou_auth_enabled", False)):
                require("pansou_username", "pansou_password")
        elif source == "hdhive":
            if hdhive_query_mode == "api":
                require("hdhive_api_key", "hdhive_access_token")
            else:
                require("hdhive_username", "hdhive_password")
        elif source == "dian115":
            require("dian115_email", "dian115_password")
        elif source == "juying":
            require("juying_username", "juying_password")
        elif source == "pinglian":
            require("pinglian_username", "pinglian_password")

        hdhive_web_client = None
        hdhive_web_client_owned = True
        if (
                source == "hdhive"
                and hdhive_query_mode == "web"
                and deadline is None
        ):
            (
                hdhive_web_client,
                hdhive_web_client_owned,
            ) = self._get_test_hdhive_web_client(config, proxy)

        pansou_client = None
        pansou_timeout = 20
        if source == "pansou":
            pansou_url = str(config.get("pansou_url") or "").strip()
            if not pansou_url:
                raise ValueError("PanSou 服务地址为空")
            pansou_timeout = min(
                20, max(5, int(config.get("pansou_timeout", 30) or 30))
            )
            pansou_client = PanSouClient(
                base_url=pansou_url,
                username=str(config.get("pansou_username") or ""),
                password=str(config.get("pansou_password") or ""),
                auth_enabled=bool(config.get("pansou_auth_enabled", False)),
                proxy=proxy,
                search_timeout=pansou_timeout,
                get_data_func=self.get_data,
                save_data_func=self.save_data,
            )

        seedhub_client = SeedHubClient(base_url=str(config.get("seedhub_base_url") or ""),
                                       proxy=proxy) if source == "seedhub" else None
        butailing_client = (
            ButailingClient(base_url=str(config.get("butailing_base_url") or ""),
                            proxy=proxy) if source == "butailing" else None
        )
        juying_client = None
        if source == "juying":
            juying_client = JuyingClient(
                base_url=str(config.get("juying_base_url") or ""),
                username=str(config.get("juying_username") or ""),
                password=str(config.get("juying_password") or ""),
                proxy=proxy,
                request_interval=float(
                    config.get("juying_request_interval", 1) or 1
                ),
                get_data_func=self.get_data,
                save_data_func=self.save_data,
                cache_namespace="test-preview",
            )
        pinglian_client = None
        online_docs_client = OnlineDocumentClient(
            config.get("online_docs") or config.get("online_docs_urls") or [],
            config.get("online_docs_resource_types") or [],
            proxy=proxy,
        ) if source == "online_docs" else None
        if source == "pinglian":
            pinglian_client = PinglianClient(
                base_url=str(config.get("pinglian_base_url") or ""),
                username=str(config.get("pinglian_username") or ""),
                password=str(config.get("pinglian_password") or ""),
                proxy=proxy,
                request_timeout=int(config.get("pinglian_timeout", 30) or 30),
                request_interval=float(
                    config.get("pinglian_request_interval", 2) or 2
                ),
                get_data_func=self.get_data,
                save_data_func=self.save_data,
            )
        confirmed_hdhive_unlock_points = max(
            0, int(confirmed_hdhive_unlock_points or 0)
        )
        handler = SearchHandler(
            pansou_client=pansou_client,
            hdhive_client=hdhive_client,
            seedhub_client=seedhub_client,
            butailing_client=butailing_client,
            juying_client=juying_client,
            pinglian_client=pinglian_client,
            pansou_enabled=source == "pansou",
            hdhive_enabled=source == "hdhive",
            dian115_enabled=source == "dian115",
            seedhub_enabled=source == "seedhub",
            butailing_enabled=source == "butailing",
            juying_enabled=source == "juying",
            pinglian_enabled=source == "pinglian",
            online_docs_client=online_docs_client,
            hdhive_web_client=hdhive_web_client,
            hdhive_web_client_owned=hdhive_web_client_owned,
            hdhive_username=str(config.get("hdhive_username") or ""),
            hdhive_password=str(config.get("hdhive_password") or ""),
            hdhive_query_mode=hdhive_query_mode,
            # HDHive 测试使用独立只读路径；显式关闭自动解锁能力。
            hdhive_auto_unlock=False,
            hdhive_max_unlock_points=confirmed_hdhive_unlock_points,
            hdhive_max_points_per_sub=confirmed_hdhive_unlock_points,
            dian115_email=str(config.get("dian115_email") or ""),
            dian115_password=str(config.get("dian115_password") or ""),
            # 测试搜索不进入同步链；收费候选仅展示，不会消耗积分。
            dian115_auto_unlock=bool(
                config.get("dian115_auto_unlock", False)
            ),
            dian115_max_unlock_points=0,
            dian115_max_points_per_sub=0,
            pansou_channels=config.get("pansou_channels") or [],
            pansou_plugins=config.get("pansou_plugins") or [],
            pansou_cloud_types=resource_type_order,
            pansou_filter_include=config.get("pansou_filter_include") or [],
            pansou_filter_exclude=config.get("pansou_filter_exclude") or [],
            resource_type_order=resource_type_order,
            pansou_concurrency=config.get("pansou_concurrency") or None,
            pansou_result_limit=int(
                config.get("pansou_result_limit", 10) or 10
            ),
            pansou_refresh=False,
            pansou_timeout=pansou_timeout,
            seedhub_result_limit=int(
                config.get("seedhub_result_limit", 20) or 20
            ),
            butailing_result_limit=int(
                config.get("butailing_result_limit", 20) or 20
            ),
            juying_result_limit=int(
                config.get("juying_result_limit", 5) or 5
            ),
            pinglian_result_limit=int(
                config.get("pinglian_result_limit", 20) or 20
            ),
            search_source_order=[source],
            search_cache_enabled=False,
            search_concurrency=1,
            hdhive_candidate_limit=int(
                config.get("hdhive_candidate_limit", 4) or 4
            ),
            hdhive_request_interval=float(
                config.get("hdhive_request_interval", 5) or 5
            ),
            hdhive_unlocks_per_minute=int(
                config.get("hdhive_unlocks_per_minute", 2) or 2
            ),
            dian115_candidate_limit=int(
                config.get("dian115_candidate_limit", 4) or 4
            ),
            dian115_request_interval=float(
                config.get("dian115_request_interval", 1) or 1
            ),
            dian115_unlocks_per_minute=int(
                config.get("dian115_unlocks_per_minute", 6) or 6
            ),
            hdhive_torrentclaw_enabled=bool(
                config.get("hdhive_torrentclaw_enabled", False)
            ),
            hdhive_torrentclaw_subtitle_languages=as_list(
                config.get("hdhive_torrentclaw_subtitle_languages") or ["zh"]
            ),
            should_stop=(
                (lambda: time.monotonic() >= deadline) if deadline else None
            ),
        )
        handler.set_data_funcs(self.get_data, self.save_data)
        return handler

    def api_vue_search_tmdb_candidates(self, payload: Dict[str, Any]) -> dict:
        """按标题返回可供用户选择的 TMDB 电影和电视剧候选。"""
        title = str((payload or {}).get("title") or "").strip()
        if not title or len(title) > 100:
            return {"success": False, "message": "请输入 1 到 100 个字符的媒体名称"}
        try:
            meta = MetaInfo(title)
            candidates = self.chain.search_medias(
                meta=meta,
                source="themoviedb",
            ) or []
        except Exception as error:
            logger.warning(f"[{title}][TMDB] 媒体候选查询失败：{error}")
            return {"success": False, "message": f"TMDB 查询失败：{error}"}

        items = []
        seen = set()
        for candidate in candidates:
            candidate_type = getattr(candidate, "type", None)
            media_type = (
                "movie" if candidate_type == MediaType.MOVIE
                else "tv" if candidate_type == MediaType.TV
                else ""
            )
            try:
                tmdb_id = int(getattr(candidate, "tmdb_id", 0) or 0)
            except (TypeError, ValueError):
                tmdb_id = 0
            identity = (media_type, tmdb_id)
            if not media_type or tmdb_id <= 0 or identity in seen:
                continue
            seen.add(identity)
            items.append({
                "tmdb_id": tmdb_id,
                "media_type": media_type,
                "media_type_name": "电影" if media_type == "movie" else "电视剧",
                "title": str(getattr(candidate, "title", None) or title),
                "original_title": str(
                    getattr(candidate, "original_title", None) or ""
                ),
                "year": getattr(candidate, "year", None),
                "poster": str(getattr(candidate, "poster_path", None) or ""),
                "vote_average": getattr(candidate, "vote_average", None),
            })
            if len(items) >= 20:
                break
        return {
            "success": True,
            "message": f"TMDB 找到 {len(items)} 个候选",
            "data": {"items": items},
        }

    def api_vue_test_search_source(self, payload: Dict[str, Any]) -> dict:
        """使用页面输入执行隔离的单来源搜索，不触发下载、转存或历史写入。"""
        payload = dict(payload or {})
        source = str(payload.get("source") or "").strip().lower()
        source_names = {
            "hdhive": "HDHive",
            "dian115": "Dian115",
            "pansou": "PanSou",
            "juying": "聚影",
            "seedhub": "SeedHub",
            "butailing": "不太灵",
            "pinglian": "盘链",
            "online_docs": "在线文档",
        }
        if source not in source_names:
            return {"success": False, "message": "不支持的搜索渠道"}
        title = str(payload.get("title") or "").strip()
        if not title or len(title) > 100:
            return {"success": False, "message": "请输入 1 到 100 个字符的媒体名称"}
        tmdb_id_value = str(payload.get("tmdb_id") or "").strip()
        try:
            tmdb_id = int(tmdb_id_value)
        except (TypeError, ValueError):
            return {"success": False, "message": "请先选择 TMDB 媒体条目"}
        if not 1 <= tmdb_id <= 999999999:
            return {"success": False, "message": "请先选择 TMDB 媒体条目"}
        media_type_value = str(payload.get("media_type") or "tv").strip().lower()
        if media_type_value not in {"movie", "tv"}:
            return {"success": False, "message": "媒体类型仅支持电影或电视剧"}
        media_type = MediaType.MOVIE if media_type_value == "movie" else MediaType.TV
        try:
            year = int(payload.get("year")) if str(payload.get("year") or "").strip() else None
        except (TypeError, ValueError):
            return {"success": False, "message": "年份必须是整数"}
        if year is not None and not 1900 <= year <= 2100:
            return {"success": False, "message": "年份必须在 1900 到 2100 之间"}
        try:
            season = int(payload.get("season") or 1) if media_type == MediaType.TV else None
        except (TypeError, ValueError):
            return {"success": False, "message": "季号必须是整数"}
        if season is not None and not 1 <= season <= 999:
            return {"success": False, "message": "季号必须在 1 到 999 之间"}
        config = self._test_search_config(source, payload.get("config"))
        original_title = str(payload.get("original_title") or "").strip()[:200]
        mediainfo = MediaInfo(
            type=media_type,
            title=title,
            year=str(year) if year is not None else None,
        )
        mediainfo.tmdb_id = tmdb_id
        mediainfo.original_title = original_title

        test_started = time.monotonic()

        def run_test() -> list:
            handler = None
            try:
                handler = self._build_test_search_handler(
                    source, config
                )
                source_result_limit = handler.test_source_result_limit(source)
                results = handler.test_source(
                    source=source,
                    mediainfo=mediainfo,
                    media_type=media_type,
                    season=season,
                )
                return results, source_result_limit
            finally:
                if handler:
                    try:
                        handler.close(release_cache=False)
                    except Exception as close_error:
                        logger.debug(
                            f"[{source.upper()}] 测试搜索器关闭失败：{close_error}"
                        )

        try:
            logger.debug(
                f"[{title}{f' S{season:02d}' if season else ''}]"
                f"[{source.upper()}] 开始只读渠道测试："
                f"TMDB ID={tmdb_id}，类型={media_type_value}"
            )
            results, source_result_limit = run_test()
        except Exception as error:
            logger.warning(
                f"[{title}{f' S{season:02d}' if season else ''}]"
                f"[{source.upper()}] 渠道测试失败：{error}"
            )
            return {
                "success": False,
                "message": f"{source_names[source]} 测试失败：{error}",
                "data": {
                    "source": source,
                    "elapsed_seconds": round(time.monotonic() - test_started, 2),
                },
            }
        supported_results = []
        for result in results or []:
            if not isinstance(result, dict):
                continue
            resource_type = normalize_resource_type(
                result.get("resource_type") or result.get("pan_type") or ""
            )
            if resource_type not in SUPPORTED_RESOURCE_TYPES:
                continue
            if result.get("resource_type") != resource_type:
                result = {**result, "resource_type": resource_type}
            supported_results.append(result)
        results = supported_results
        total_result_count = len(results)
        results = self._balanced_test_results(
            results, self._SEARCH_TEST_DISPLAY_LIMIT
        )
        displayed_result_count = len(results or [])
        logger.debug(
            f"[{title}{f' S{season:02d}' if season else ''}]"
            f"[{source.upper()}] 只读渠道测试完成："
            f"候选总数={total_result_count}，展示={displayed_result_count}，"
            f"耗时={time.monotonic() - test_started:.2f}s"
        )

        items = []
        resource_type_counts: Dict[str, int] = {}

        def display_size(item: Dict[str, Any]) -> Any:
            human = str(item.get("size_human") or "").strip()
            if human:
                return human
            value = item.get("size")
            if not isinstance(value, (int, float)) or value <= 0:
                return value or 0
            return StringUtils.format_size(int(value))

        def display_tags(item: Dict[str, Any]) -> List[str]:
            values = [item.get("tags") or []]
            values.extend(
                item.get(key)
                for key in (
                    "resolution", "quality", "source_type", "codec",
                    "audio_codec", "hdr_type", "subtitle",
                )
                if item.get(key)
            )

            tags: List[str] = []
            seen_tags = set()

            def append_tag(value: Any) -> None:
                if isinstance(value, dict):
                    for nested in value.values():
                        append_tag(nested)
                    return
                if isinstance(value, (list, tuple, set)):
                    for nested in value:
                        append_tag(nested)
                    return

                text = str(value or "").strip()
                if not text:
                    return
                if text[:1] in ("[", "{") and text[-1:] in ("]", "}"):
                    try:
                        parsed = ast.literal_eval(text)
                    except (SyntaxError, ValueError):
                        parsed = None
                    if isinstance(parsed, (dict, list, tuple, set)):
                        append_tag(parsed)
                        return
                if text in seen_tags:
                    return
                seen_tags.add(text)
                tags.append(text)

            for value in values:
                append_tag(value)
            return tags

        for item in (results or [])[:self._SEARCH_TEST_DISPLAY_LIMIT]:
            source_url = ""
            for value in (item.get("source_url"), item.get("media_page_url")):
                candidate = str(value or "").strip()
                parsed = urlparse(candidate)
                if (parsed.scheme in {"http", "https"} and parsed.netloc
                        and (parsed.path.rstrip("/") or parsed.query)):
                    source_url = candidate
                    break
            resource_type = str(
                item.get("resource_type") or item.get("pan_type") or "unknown"
            ).strip().lower()
            try:
                unlock_points = max(0, int(item.get("unlock_points") or 0))
            except (TypeError, ValueError):
                unlock_points = 0
            items.append({
                "title": str(item.get("title") or "未命名资源"),
                "source": str(item.get("source") or source),
                "source_name": source_names.get(
                    str(item.get("source") or source), source_names[source]
                ),
                "resource_type": resource_type,
                "resource_type_name": resource_type_name(
                    resource_type, resource_type.upper() or "未知"
                ),
                "size": display_size(item),
                "size_bytes": item.get("size") or 0,
                "tags": display_tags(item),
                "description": str(item.get("description") or "").strip(),
                "source_url": source_url,
                "url": str(
                    item.get("url") or item.get("share_url")
                    or ""
                ).strip(),
                "slug": str(item.get("slug") or "").strip(),
                "share_id": item.get("share_id") or item.get("id") or 0,
                "resource_id": item.get("resource_id") or 0,
                "media_page_url": str(item.get("media_page_url") or "").strip(),
                "unlock_points": unlock_points,
                "need_unlock": bool(item.get("need_unlock")),
                "need_access": bool(item.get("need_access")),
                "is_unlocked": bool(item.get("is_unlocked")),
                "is_free": bool(item.get("is_free")),
                "target_season": item.get("target_season"),
                "target_episodes": item.get("target_episodes") or [],
                "preview_episodes": item.get("preview_episodes") or {},
                "juying_resource_id": str(
                    item.get("juying_resource_id") or ""
                ).strip(),
                "can_preview": resource_type in PREVIEW_RESOURCE_TYPES,
            })
            resource_type_counts[resource_type] = (
                    resource_type_counts.get(resource_type, 0) + 1
            )
        return {
            "success": True,
            "message": f"{source_names[source]} 测试完成，找到 {total_result_count} 个候选",
            "data": {
                "source": source,
                "source_name": source_names[source],
                "media": (
                    f"{getattr(mediainfo, 'title', None) or title}"
                    f"{f' ({getattr(mediainfo, 'year', None)})' if getattr(mediainfo, 'year', None) else ''}"
                    f"{f' S{season:02d}' if season else ''}"
                ),
                "count": total_result_count,
                "displayed_count": displayed_result_count,
                "result_limit": source_result_limit,
                "display_limit": self._SEARCH_TEST_DISPLAY_LIMIT,
                "elapsed_seconds": round(time.monotonic() - test_started, 2),
                "items": items,
                "resource_types": [
                    {
                        "value": resource_type,
                        "title": resource_type_name(
                            resource_type, resource_type.upper() or "未知"
                        ),
                        "count": count,
                    }
                    for resource_type, count in sorted(
                        resource_type_counts.items(),
                        key=lambda pair: (
                            RESOURCE_TYPE_PRIORITY.get(pair[0], 99),
                            pair[0],
                        ),
                    )
                ],
            },
        }

    @staticmethod
    def _balanced_test_results(
            results: Any, limit: int
    ) -> List[Dict[str, Any]]:
        """按资源类型轮询选取测试候选，避免单一类型占满展示额度。"""
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for item in results or []:
            if not isinstance(item, dict):
                continue
            resource_type = str(
                item.get("resource_type") or item.get("pan_type") or "unknown"
            ).strip().lower() or "unknown"
            groups.setdefault(resource_type, []).append(item)
        target = max(1, int(limit or 20))
        offsets = {resource_type: 0 for resource_type in groups}
        balanced = []
        while groups and len(balanced) < target:
            for resource_type in list(groups):
                rows = groups[resource_type]
                offset = offsets[resource_type]
                balanced.append(rows[offset])
                offset += 1
                offsets[resource_type] = offset
                if offset >= len(rows):
                    groups.pop(resource_type)
                    offsets.pop(resource_type, None)
                if len(balanced) >= target:
                    break
        return balanced
