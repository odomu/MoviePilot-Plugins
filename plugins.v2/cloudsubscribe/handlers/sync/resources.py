"""候选资源解析、校验与网盘 Provider 路由。"""

import copy
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from app.log import logger

from ...core import (
    CloudDriveCapability,
    CloudDriveProvider,
    CloudFile,
    OwnerDelegator,
)
from ...utils import MediaFileParser


class ResourceTransferService(OwnerDelegator):
    """统一搜索候选到网盘转存能力之间的适配。"""

    @staticmethod
    def _resource_preview_episodes(
            resource: Dict[str, Any], season: int
    ) -> Set[int]:
        """读取搜索阶段已取得的文件预览，不额外请求网盘接口。"""
        preview_episodes = resource.get("preview_episodes") or {}
        values = preview_episodes.get(
            str(season), preview_episodes.get(season, [])
        )
        episodes = set()
        for value in values or []:
            try:
                episodes.add(int(value))
            except (TypeError, ValueError):
                continue
        return episodes

    @staticmethod
    def _resource_history_meta(
            resource: Dict[str, Any], share_url: str
    ) -> Dict[str, Any]:
        source = str(resource.get("source") or "unknown").strip().lower()
        resource_type = str(
            resource.get("resource_type") or resource.get("pan_type") or ""
        ).strip().lower()
        if not resource_type:
            resource_type = (
                "ed2k"
                if str(share_url).lower().startswith("ed2k://")
                else "115"
            )
        points = resource.get("unlock_points")
        try:
            points = int(points) if points is not None else None
        except (TypeError, ValueError):
            points = None
        source_url = str(resource.get("source_url") or "").strip()
        result = {
            "resource_type": resource_type,
            "source": source,
            "points": points,
        }
        if source_url:
            result["source_url"] = source_url
        return result

    def _expand_resource_urls(
            self,
            resources: List[Dict[str, Any]],
            resource_index: int,
            resource: Dict[str, Any],
            value: Any,
    ) -> str:
        """展开列表或字符串中的多条离线链接，后续条目不重复计算积分。"""
        raw_values = value if isinstance(value, (list, tuple)) else [value]
        urls = []
        seen_urls = set()
        for raw_value in raw_values:
            text = str(raw_value or "").replace("｜", "|").strip()
            if not text:
                continue
            matches = list(self._OFFLINE_RESOURCE_URL_RE.finditer(text))
            extracted = [match.group(0).strip() for match in matches]
            remainder = self._OFFLINE_RESOURCE_URL_RE.sub("", text).strip()
            candidates = extracted if extracted and not remainder else [text]
            for url in candidates:
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                urls.append(url)
        if not urls:
            return ""

        resource["url"] = urls[0]
        resource["need_unlock"] = False
        resource["need_access"] = False
        if len(urls) > 1:
            expanded = []
            for url in urls[1:]:
                item = copy.deepcopy(resource)
                item["url"] = url
                item["unlock_points"] = 0
                expanded.append(item)
            resources[resource_index + 1:resource_index + 1] = expanded
            source_name = str(resource.get("source") or "资源源").upper()
            logger.debug(
                f"{source_name} 同一资源包含 {len(urls)} 条链接，"
                "已展开处理且积分仅计一次"
            )
        return urls[0]

    def _resolve_candidate_resource_url(
            self,
            resources: List[Dict[str, Any]],
            resource_index: int,
            resource: Dict[str, Any],
            search_label: str,
            log_prefix: str = "",
    ) -> str:
        """统一处理积分搜索源的延迟解锁，并展开一次返回的多条链接。"""
        share_url = str(resource.get("url") or "").strip()
        if share_url:
            return self._expand_resource_urls(
                resources, resource_index, resource, share_url
            )
        if not (
                resource.get("need_unlock") or resource.get("need_access")
        ):
            return ""
        slug = str(resource.get("slug") or "").strip()
        if not slug:
            return ""
        try:
            unlock_points = int(resource.get("unlock_points") or 0)
        except (TypeError, ValueError):
            unlock_points = 0
        prefix = f"{log_prefix} " if log_prefix else ""
        resource_title = str(resource.get("title") or "").strip()
        source = str(resource.get("source") or "").strip().lower()
        is_dian115 = source == "dian115"
        has_budget = (
            self._search_handler.has_dian115_unlock_budget(unlock_points)
            if is_dian115
            else self._search_handler.has_hdhive_unlock_budget(unlock_points)
        )
        source_label = "Dian115" if is_dian115 else "HDHive"
        if not has_budget:
            logger.debug(
                f"{prefix}跳过 {source_label} 资源 {resource_title}："
                f"需要 {unlock_points} 积分，当前预算不足"
            )
            return ""
        action_label = "获取免费资源链接" if unlock_points <= 0 else "消耗积分解锁"
        media_page_url = str(resource.get("media_page_url") or "").strip()
        media_page_suffix = f"，媒体页：{media_page_url}" if media_page_url else ""
        logger.info(
            f"{prefix}遇到尚未取得链接的 {source_label} 资源 {resource_title} "
            f"(slug: {slug})，尝试{action_label}{media_page_suffix}"
        )
        if is_dian115:
            unlocked = self._search_handler.unlock_dian115_resource(
                int(resource.get("dian115_share_id") or slug),
                int(resource.get("dian115_resource_id") or 0),
                unlock_points,
                search_label=search_label,
                tmdb_id=int(resource.get("dian115_tmdb_id") or 0),
                media_type=str(resource.get("dian115_media_type") or ""),
                season=int(resource.get("dian115_season") or 0),
            )
        else:
            unlocked = self._search_handler.unlock_hdhive_resource(
                slug,
                unlock_points,
                resource.get("resource_type"),
                media_page_url=media_page_url,
                search_label=search_label,
            )
        if self._stop_requested() or not unlocked:
            if not self._stop_requested():
                logger.error(
                    f"{prefix}未能取得 {source_label} 资源链接：{resource_title}"
                )
            return ""
        return self._expand_resource_urls(
            resources, resource_index, resource, unlocked
        )

    def _validate_resource_url(
            self,
            share_url: str,
            resource_label: str = "分享链接",
            log_prefix: str = "",
    ) -> bool:
        """使用对应 Provider 的统一能力校验资源链接。"""
        provider = self._resource_provider_for_url(share_url)
        share_service = (
            provider.require(CloudDriveCapability.SHARE_TRANSFER)
            if provider else self._share_transfer
        )
        if not share_service:
            return False
        status = self._timed_sync_call(
            "share_validation", share_service.check_share_status, share_url
        )
        if status.is_valid:
            return True
        prefix = f"{log_prefix} " if log_prefix else ""
        logger.debug(
            f"{prefix}{resource_label}无效："
            f"{self._resource_log_reference(share_url)}，原因：{status.status_text}"
        )
        return False

    def _validated_resource_files(
            self,
            share_url: str,
            resource_title: str = "",
            target_season: Optional[int] = None,
            log_prefix: str = "",
    ) -> List[Dict[str, Any]]:
        """校验分享并读取文件列表，供电影、剧集和洗版共同使用。"""
        if not self._validate_resource_url(
                share_url, resource_label="分享链接", log_prefix=log_prefix
        ):
            return []
        kwargs = {}
        if target_season is not None:
            kwargs["target_season"] = target_season
        if log_prefix:
            kwargs["log_prefix"] = log_prefix
        provider = self._resource_provider_for_url(share_url)
        share_service = (
            provider.require(CloudDriveCapability.SHARE_TRANSFER)
            if provider else self._share_transfer
        )
        if not share_service:
            return []
        files = self._timed_sync_call(
            "share_listing",
            share_service.list_share_files,
            share_url,
            **kwargs,
        ) or []
        files = list(MediaFileParser.iter_files(files))
        if not files:
            label = resource_title or self._resource_log_reference(share_url)
            logger.debug(
                f"{log_prefix + ' ' if log_prefix else ''}分享链接无内容：{label}"
            )
        return files

    def _transfer_history_status(self, success: bool, share_url: str) -> str:
        if not success:
            return "失败"
        return "下载中" if self._is_offline_url(share_url) else "成功"

    @staticmethod
    def _supported_resource_type(
            resource: Dict[str, Any], share_url: str
    ) -> str:
        resource_type = str(
            resource.get("resource_type") or resource.get("pan_type") or ""
        ).strip().lower()
        if resource_type:
            return resource_type
        normalized_url = str(share_url).lstrip().lower()
        if normalized_url.startswith("ed2k://"):
            return "ed2k"
        if normalized_url.startswith("magnet:?"):
            return "magnet"
        for marker, value in (
                ("quark", "quark"), ("189.cn", "tianyi"),
                ("cloud.189", "tianyi"), ("guangya", "guangya"),
                ("123pan", "123"), ("123.cn", "123"),
                ("123684.com", "123"), ("123865.com", "123"),
                ("alipan.com", "alipan"), ("aliyundrive.com", "alipan"),
        ):
            if marker in normalized_url:
                return value
        return "115"

    def _is_cross_drive_resource(
            self, resource: Dict[str, Any], share_url: str = ""
    ) -> bool:
        """判断候选是否必须经过跨盘下载上传。"""
        if not self._cloud_drive:
            return False
        actual_url = str(share_url or resource.get("url") or "").strip()
        if actual_url and not self._is_offline_url(actual_url):
            source = self._resource_provider_for_url(actual_url)
            if source:
                return source.key != self._cloud_drive.key
        resource_type = self._supported_resource_type(resource, actual_url)
        resource_type = {
            "189": "tianyi", "aliyun": "alipan",
        }.get(resource_type, resource_type)
        return not self._cloud_drive.supports_resource_type(resource_type)

    def _build_transfer_resource_batches(
            self,
            sources: List[str],
            source_results: Mapping[str, List[Dict[str, Any]]],
    ) -> List[Tuple[str, List[Dict[str, Any]], bool]]:
        """按目标盘直存优先、跨盘最后生成来源批次。"""
        direct_batches = []
        cross_batches = []
        direct_count = 0
        cross_count = 0
        for source in sources or []:
            direct_resources = []
            cross_resources = []
            for resource in source_results.get(source) or []:
                if self._is_cross_drive_resource(resource):
                    cross_resources.append(resource)
                else:
                    direct_resources.append(resource)
            if direct_resources:
                direct_count += len(direct_resources)
                direct_batches.append((source, direct_resources, False))
            if cross_resources:
                cross_count += len(cross_resources)
                cross_batches.append((source, cross_resources, True))
        if direct_count or cross_count:
            logger.debug(
                f"候选转存顺序：目标网盘直存 {direct_count} 个，"
                f"跨盘 {cross_count} 个（跨盘最后处理）"
            )
        return direct_batches + cross_batches

    def _resource_provider_for_url(
            self, share_url: str
    ) -> Optional[CloudDriveProvider]:
        if not self._cloud_drive_registry:
            return self._cloud_drive
        key = self._supported_resource_type({}, share_url)
        aliases = {"189": "tianyi", "aliyun": "alipan"}
        try:
            return self._cloud_drive_registry.get(aliases.get(key, key))
        except KeyError:
            return self._cloud_drive if key == "115" else None

    @staticmethod
    def _normalize_cross_transfer_media_type(value: Any) -> str:
        normalized = str(getattr(value, "name", value) or "").strip().lower()
        return {
            "电影": "movie",
            "电视剧": "tv",
            "mediatype.movie": "movie",
            "mediatype.tv": "tv",
        }.get(normalized, normalized)

    @staticmethod
    def _cloud_file_from_dict(item: Dict[str, Any]) -> CloudFile:
        return CloudFile(
            id=str(item.get("id") or ""),
            name=str(item.get("name") or ""),
            is_directory=False,
            size=int(item.get("size") or 0),
            sha1=str(item.get("sha1") or ""),
            md5=str(item.get("md5") or ""),
            native=item,
        )

    def _is_supported_resource(
            self, resource: Dict[str, Any], share_url: str
    ) -> bool:
        if not self._cloud_drive:
            return False
        resource_type = self._supported_resource_type(resource, share_url)
        if not self._cloud_drive.supports_resource_type(resource_type):
            if not self._cross_transfer_enabled:
                return False
            source = self._resource_provider_for_url(share_url)
            if not source or not source.supports(
                    CloudDriveCapability.SHARE_TRANSFER
            ):
                return False
            if not source.supports(CloudDriveCapability.FILE_QUERY):
                return False
            if not source.supports(CloudDriveCapability.FILE_DOWNLOAD):
                return False
            if not self._cloud_drive.supports(CloudDriveCapability.LOCAL_UPLOAD):
                return False
            if not self._cloud_drive.supports(CloudDriveCapability.FILE_QUERY):
                return False
        if resource_type in {"ed2k", "magnet"}:
            return self._offline_download is not None
        source = self._resource_provider_for_url(share_url)
        return bool(
            source and source.supports(CloudDriveCapability.SHARE_TRANSFER)
        )

    @classmethod
    def _format_resource_summary(
            cls, resources: List[Dict[str, Any]]
    ) -> str:
        labels = {"share": "网盘分享", "ed2k": "ED2K", "magnet": "Magnet"}
        summary_counts: Dict[str, Dict[str, int]] = {}
        seen = set()
        for resource in resources or []:
            resource_type = cls._supported_resource_type(
                resource, str(resource.get("url") or "")
            )
            label = labels.get(resource_type, resource_type.upper() or "未知")
            identity = str(
                resource.get("unlock_group") or resource.get("source_url")
                or resource.get("url") or resource.get("title") or ""
            )
            key = (resource_type, identity)
            if key in seen:
                continue
            seen.add(key)
            counts = summary_counts.setdefault(
                label, {"total": 0, "available": 0, "paid": 0, "official": 0}
            )
            counts["total"] += 1
            if resource.get("is_official"):
                counts["official"] += 1
            if resource.get("need_unlock"):
                counts["paid"] += 1
            else:
                counts["available"] += 1
        summaries = []
        for label, counts in summary_counts.items():
            statuses = [f"可用 {counts['available']}"]
            if counts["paid"]:
                statuses.append(f"待解锁 {counts['paid']}")
            if counts["official"]:
                statuses.append(f"官组 {counts['official']}")
            summaries.append(
                f"{label} {counts['total']}（{'，'.join(statuses)}）"
            )
        return f"共 {len(seen)} 个资源页：" + "；".join(summaries)
