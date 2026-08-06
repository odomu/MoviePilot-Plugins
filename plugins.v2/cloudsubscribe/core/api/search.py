"""搜索源测试与 TMDB 候选查询 API。"""

import ast
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.metainfo import MetaInfo
from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType

from .. import OwnerDelegator
from ..config import UIConfig

_SEARCH_TEST_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="CloudSubscribe-SearchTest")
_SEARCH_TEST_TIMEOUT_SECONDS = 30


class SearchApi(OwnerDelegator):
    _SEARCH_TEST_RESULT_LIMIT = 20
    _SEARCH_TEST_CONFIG_FIELDS = {
        "pansou": frozenset({
            "pansou_url", "pansou_username", "pansou_password",
            "pansou_auth_enabled", "pansou_channels", "pansou_plugins",
            "pansou_filter_include",
            "pansou_filter_exclude", "pansou_concurrency",
            "pansou_result_limit", "pansou_timeout",
        }),
        "hdhive": frozenset({
            "hdhive_query_mode", "hdhive_api_key", "hdhive_client_id",
            "hdhive_access_token", "hdhive_refresh_token",
            "hdhive_token_expires_at", "hdhive_username", "hdhive_password",
            "hdhive_candidate_limit", "hdhive_request_interval",
            "hdhive_unlocks_per_minute", "hdhive_torrentclaw_enabled",
            "hdhive_torrentclaw_subtitle_languages",
        }),
        "dian115": frozenset({
            "dian115_email", "dian115_password", "dian115_candidate_limit",
            "dian115_request_interval", "dian115_unlocks_per_minute",
        }),
        "juying": frozenset({
            "juying_username", "juying_password", "juying_result_limit",
            "juying_request_interval", "juying_unlocks_per_minute",
        }),
        "seedhub": frozenset({"seedhub_result_limit"}),
        "butailing": frozenset({"butailing_result_limit"}),
        "pinglian": frozenset({
            "pinglian_username", "pinglian_password", "pinglian_result_limit",
            "pinglian_request_interval", "pinglian_timeout",
        }),
    }

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
    ):
        """使用当前表单配置创建隔离搜索器，不修改已保存配置或运行中服务。"""
        from ...handlers.search import SearchHandler
        from ...search.butailing import ButailingClient
        from ...search.hdhive import HDHiveOpenAPIClient
        from ...search.juying import JuyingClient
        from ...search.pansou import PanSouClient
        from ...search.pinglian import PinglianClient
        from ...search.seedhub import SeedHubClient

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

        seedhub_client = SeedHubClient(proxy=proxy) if source == "seedhub" else None
        butailing_client = (
            ButailingClient(proxy=proxy) if source == "butailing" else None
        )
        juying_client = None
        if source == "juying":
            juying_client = JuyingClient(
                username=str(config.get("juying_username") or ""),
                password=str(config.get("juying_password") or ""),
                proxy=proxy,
                request_interval=float(
                    config.get("juying_request_interval", 1) or 1
                ),
                unlocks_per_minute=int(
                    config.get("juying_unlocks_per_minute", 8) or 8
                ),
                get_data_func=self.get_data,
                save_data_func=self.save_data,
            )
        pinglian_client = None
        if source == "pinglian":
            pinglian_client = PinglianClient(
                username=str(config.get("pinglian_username") or ""),
                password=str(config.get("pinglian_password") or ""),
                proxy=proxy,
                request_timeout=int(config.get("pinglian_timeout", 30) or 30),
                request_interval=float(
                    config.get("pinglian_request_interval", 1) or 1
                ),
                get_data_func=self.get_data,
                save_data_func=self.save_data,
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
            hdhive_username=str(config.get("hdhive_username") or ""),
            hdhive_password=str(config.get("hdhive_password") or ""),
            hdhive_query_mode=hdhive_query_mode,
            # HDHive 测试使用独立只读路径；显式关闭自动解锁能力。
            hdhive_auto_unlock=False,
            hdhive_max_unlock_points=0,
            hdhive_max_points_per_sub=0,
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
            pansou_result_limit=min(
                10, int(config.get("pansou_result_limit", 10) or 10)
            ),
            pansou_refresh=False,
            pansou_timeout=pansou_timeout,
            seedhub_result_limit=min(
                5, int(config.get("seedhub_result_limit", 20) or 20)
            ),
            butailing_result_limit=min(
                10, int(config.get("butailing_result_limit", 20) or 20)
            ),
            juying_result_limit=min(
                5, int(config.get("juying_result_limit", 5) or 5)
            ),
            pinglian_result_limit=min(
                20, int(config.get("pinglian_result_limit", 20) or 20)
            ),
            search_source_order=[source],
            search_cache_enabled=False,
            search_concurrency=1,
            hdhive_candidate_limit=min(
                4, int(config.get("hdhive_candidate_limit", 4) or 4)
            ),
            hdhive_request_interval=float(
                config.get("hdhive_request_interval", 5) or 5
            ),
            hdhive_unlocks_per_minute=int(
                config.get("hdhive_unlocks_per_minute", 2) or 2
            ),
            dian115_candidate_limit=min(
                4, int(config.get("dian115_candidate_limit", 4) or 4)
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
        test_timeout = (
            120 if source == "dian115" else _SEARCH_TEST_TIMEOUT_SECONDS
        )
        test_deadline = test_started + test_timeout

        def run_test() -> list:
            handler = None
            try:
                handler = self._build_test_search_handler(
                    source, config, deadline=test_deadline
                )
                return handler.test_source(
                    source=source,
                    mediainfo=mediainfo,
                    media_type=media_type,
                    season=season,
                )
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
            future = _SEARCH_TEST_EXECUTOR.submit(run_test)
            results = future.result(timeout=test_timeout)
        except FutureTimeoutError:
            future.cancel()
            logger.warning(
                f"[{title}{f' S{season:02d}' if season else ''}]"
                f"[{source.upper()}] 渠道测试超过 "
                f"{test_timeout} 秒，已提前返回"
            )
            return {
                "success": False,
                "message": (
                    f"{source_names[source]} 测试超过 "
                    f"{test_timeout} 秒，请检查渠道服务或代理状态"
                ),
            }
        except Exception as error:
            logger.warning(
                f"[{title}{f' S{season:02d}' if season else ''}]"
                f"[{source.upper()}] 渠道测试失败：{error}"
            )
            return {
                "success": False,
                "message": f"{source_names[source]} 测试失败：{error}",
            }
        results = self._balanced_test_results(
            results, self._SEARCH_TEST_RESULT_LIMIT
        )
        logger.debug(
            f"[{title}{f' S{season:02d}' if season else ''}]"
            f"[{source.upper()}] 只读渠道测试完成："
            f"候选={len(results or [])}，耗时={time.monotonic() - test_started:.2f}s"
        )

        resource_type_names = {
            "115": "115",
            "123": "123云盘",
            "ed2k": "ED2K",
            "magnet": "Magnet",
            "quark": "夸克",
            "guangya": "光鸭",
            "aliyun": "阿里云盘",
            "alipan": "阿里云盘",
            "ali": "阿里云盘",
            "xunlei": "迅雷云盘",
            "189": "天翼云盘",
            "123pan": "123云盘",
        }
        items = []
        resource_type_counts: Dict[str, int] = {}

        def display_size(item: Dict[str, Any]) -> Any:
            human = str(item.get("size_human") or "").strip()
            if human:
                return human
            value = item.get("size")
            if not isinstance(value, (int, float)) or value <= 0:
                return value or 0
            units = ("B", "KB", "MB", "GB", "TB")
            number = float(value)
            unit = 0
            while number >= 1024 and unit < len(units) - 1:
                number /= 1024
                unit += 1
            return f"{number:.2f}{units[unit]}"

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
                if text not in tags:
                    tags.append(text)

            for value in values:
                append_tag(value)
            return tags

        for item in (results or [])[:100]:
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
                "resource_type_name": resource_type_names.get(
                    resource_type, resource_type.upper() or "未知"
                ),
                "size": display_size(item),
                "size_bytes": item.get("size") or 0,
                "tags": display_tags(item),
                "description": str(item.get("description") or "").strip(),
                "source_url": str(
                    item.get("source_url") or item.get("media_page_url") or ""
                ).strip(),
                "unlock_points": unlock_points,
                "need_unlock": bool(item.get("need_unlock")),
                "need_access": bool(item.get("need_access")),
                "is_unlocked": bool(item.get("is_unlocked")),
                "is_free": bool(item.get("is_free")),
            })
            resource_type_counts[resource_type] = (
                    resource_type_counts.get(resource_type, 0) + 1
            )
        return {
            "success": True,
            "message": f"{source_names[source]} 测试完成，找到 {len(results or [])} 个候选",
            "data": {
                "source": source,
                "source_name": source_names[source],
                "media": (
                    f"{getattr(mediainfo, 'title', None) or title}"
                    f"{f' ({getattr(mediainfo, 'year', None)})' if getattr(mediainfo, 'year', None) else ''}"
                    f"{f' S{season:02d}' if season else ''}"
                ),
                "count": len(results or []),
                "items": items,
                "resource_types": [
                    {
                        "value": resource_type,
                        "title": resource_type_names.get(
                            resource_type,
                            resource_type.upper() or "未知",
                        ),
                        "count": count,
                    }
                    for resource_type, count in sorted(
                        resource_type_counts.items(),
                        key=lambda pair: (
                            {
                                "115": 0,
                                "123": 1,
                                "quark": 2,
                                "guangya": 3,
                                "ed2k": 4,
                                "magnet": 5,
                            }.get(pair[0], 99),
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
        balanced = []
        while groups and len(balanced) < max(1, int(limit or 20)):
            for resource_type in list(groups):
                rows = groups[resource_type]
                balanced.append(rows.pop(0))
                if not rows:
                    groups.pop(resource_type)
                if len(balanced) >= max(1, int(limit or 20)):
                    break
        return balanced
