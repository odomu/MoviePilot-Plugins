"""Dian115 资源搜索、客户端生命周期与积分解锁。"""

import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from app.core.config import settings
from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType

from ...core import OwnerDelegator
from ...search.dian115 import (
    Dian115Client,
    Dian115Error,
    Dian115ResourceService,
)
from ...search.matching import unique_texts
from ...utils.file_parser import MediaFileParser


class Dian115SearchService(OwnerDelegator):
    """提供 Dian115 curl_cffi 搜索与按需解锁能力。"""

    _HISTORY_KEY = "dian115_sub_points_history"

    def _get_dian115_resources(self) -> Dian115ResourceService:
        """复用唯一认证客户端，返回独立的资源服务。"""
        proxy = settings.PROXY
        with self._dian115_client_lock:
            client = self._dian115_client
            if client is None or not client.matches_config(
                    self._dian115_email,
                    self._dian115_password,
                    proxy,
                    self._dian115_request_interval,
            ):
                if client:
                    client.close()
                client = Dian115Client(
                    email=self._dian115_email,
                    password=self._dian115_password,
                    proxy=proxy,
                    request_interval=self._dian115_request_interval,
                    get_data_func=self._dian115_get_data_func,
                    save_data_func=self._dian115_save_data_func,
                )
                self._dian115_client = client
                self._dian115_resources = None
            resources = self._dian115_resources
            if resources is None or not resources.matches_client(client):
                resources = Dian115ResourceService(client)
                self._dian115_resources = resources
            return resources

    def close(self) -> None:
        with self._dian115_client_lock:
            client = self._dian115_client
            self._dian115_client = None
            self._dian115_resources = None
        if client:
            client.close()

    def clear_cache(self) -> int:
        with self._dian115_client_lock:
            return (
                self._dian115_resources.clear_cache()
                if self._dian115_resources else 0
            )

    @staticmethod
    def _episode_values(value: Any) -> List[int]:
        numbers = []
        for part in re.split(r"[,，\s]+", str(value or "").strip()):
            if not part:
                continue
            match = re.fullmatch(r"(\d+)(?:\s*[-~至]\s*(\d+))?", part)
            if not match:
                continue
            start = int(match.group(1))
            end = int(match.group(2) or start)
            if 0 < start <= end <= 10000:
                numbers.extend(range(start, end + 1))
        return sorted(set(numbers))

    @staticmethod
    def _seasons(share: Dict[str, Any]) -> List[int]:
        values = Dian115SearchService._episode_values(
            share.get("seasons_csv") or share.get("seasons")
        )
        if values:
            return values
        try:
            season = int(share.get("season") or 0)
        except (TypeError, ValueError):
            season = 0
        return [season] if season >= 0 else []

    @staticmethod
    def _resource_type(share: Dict[str, Any]) -> str:
        if str(share.get("share_kind") or "").strip().lower() != "offline":
            return "115"
        resource_type = str(share.get("offline_type") or "").strip().lower()
        return resource_type if resource_type in {"ed2k", "magnet"} else ""

    @staticmethod
    def _share_url(share: Dict[str, Any]) -> str:
        resource_type = Dian115SearchService._resource_type(share)
        if resource_type in {"ed2k", "magnet"}:
            return str(share.get("url") or "").strip()
        direct = str(share.get("url_115") or share.get("url") or "").strip()
        if direct:
            return direct
        share_code = str(share.get("share_code") or "").strip()
        receive_code = str(share.get("receive_code") or "").strip()
        if share_code and receive_code:
            return (
                f"https://115.com/s/{share_code}?"
                f"{urlencode({'password': receive_code})}"
            )
        return ""

    @staticmethod
    def _unlock_payload_url(payload: Dict[str, Any]) -> str:
        data = payload.get("payload") or {}
        if not isinstance(data, dict):
            return ""
        return Dian115SearchService._share_url(data)

    def _normalize_share(
            self,
            share: Dict[str, Any],
            resource: Dict[str, Any],
            resource_key: str,
            resource_path: str,
            media_type: str,
            tmdb_id: int,
            target_season: Optional[int],
            test_mode: bool = False,
    ) -> Optional[Dict[str, Any]]:
        resource_type = self._resource_type(share)
        if not resource_type or (
                not test_mode and resource_type not in self._resource_type_order_config
        ):
            return None
        if str(share.get("status") or "active").strip().lower() != "active":
            return None
        seasons = self._seasons(share)
        if (not test_mode and media_type == "tv" and target_season is not None
                and int(target_season) not in seasons):
            return None

        share_id = int(share.get("id") or 0)
        if share_id <= 0:
            return None
        resource_id = int(share.get("resource_id") or 0)
        url = self._share_url(share)
        is_unlocked = bool(share.get("is_unlocked")) or bool(url)
        file_list = [
            str(value).strip()
            for value in (share.get("file_list") or [])
            if str(value or "").strip()
        ]
        try:
            unlock_points = max(0, int(share.get("unlock_cost") or 0))
        except (TypeError, ValueError):
            unlock_points = 0

        episodes = self._episode_values(
            share.get("episodes") or share.get("episodes_csv")
        )
        file_episodes: Dict[int, List[int]] = {}
        for file_name in file_list:
            parsed = MediaFileParser.extract_season_episode(file_name)
            if not parsed:
                continue
            file_season, file_episode = parsed
            file_episodes.setdefault(int(file_season), []).append(int(file_episode))
        if not episodes and file_episodes:
            episodes = sorted({
                episode for values in file_episodes.values() for episode in values
            })
        preview_episodes = {}
        for season in seasons or ([int(target_season)] if target_season is not None else []):
            preview_episodes[str(season)] = sorted(set(
                file_episodes.get(int(season), episodes)
            ))

        tag = share.get("tag_decoded") or {}
        if not isinstance(tag, dict):
            tag = {}
        tag_values = [
            tag.get("resolution"), tag.get("source"), tag.get("video_codec"),
            tag.get("audio_codec"), tag.get("hdr"), tag.get("frame_rate"),
            "中字" if tag.get("chn_sub") else "",
            str(share.get("subtitle_label") or "").strip(),
            str(share.get("file_extension") or "").strip().upper(),
        ]
        tags = unique_texts(tag_values)
        title = str(
            share.get("offline_title")
            or share.get("title_override")
            or share.get("file_name")
            or share.get("resource_title")
            or resource.get("title")
            or f"Dian115 分享 {share_id}"
        ).strip()
        page_url = f"{Dian115Client.BASE_URL}{resource_path}"
        return {
            "url": url,
            "title": title,
            "description": str(share.get("file_name") or "").strip(),
            "size": int(share.get("total_size_bytes") or 0),
            "size_human": str(share.get("total_size_human") or "").strip(),
            "file_list": file_list,
            "file_count": len(file_list),
            "episode_count": max(0, int(share.get("episode_count") or 0)),
            "tags": tags,
            "tag_decoded": dict(tag),
            "resource_type": resource_type,
            "pan_type": resource_type,
            "source": "dian115",
            "source_url": page_url,
            "media_page_url": page_url,
            "detail_path": resource_path,
            "slug": str(share_id),
            "unlock_group": f"dian115:share:{share_id}",
            "need_unlock": not is_unlocked and unlock_points > 0,
            "need_access": not is_unlocked and unlock_points <= 0,
            "unlock_points": unlock_points,
            "is_unlocked": is_unlocked,
            "is_free": unlock_points <= 0,
            "preview_episodes": preview_episodes,
            "resolution": str(tag.get("resolution") or ""),
            "codec": str(tag.get("video_codec") or ""),
            "audio_codec": str(tag.get("audio_codec") or ""),
            "source_type": str(tag.get("source") or ""),
            "hdr_type": str(tag.get("hdr") or ""),
            "subtitle": str(share.get("subtitle_label") or ""),
            "update_time": share.get("created_at"),
            "dian115_share_id": share_id,
            "dian115_resource_id": resource_id,
            "dian115_resource_key": resource_key,
            "dian115_tmdb_id": tmdb_id,
            "dian115_media_type": media_type,
            "dian115_season": int(target_season or 0),
        }

    def _search_dian115(
            self,
            mediainfo: MediaInfo,
            media_type: MediaType,
            season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            subscribe: Any = None,
            test_mode: bool = False,
    ) -> Optional[List[Dict[str, Any]]]:
        tmdb_id = mediainfo.tmdb_id or getattr(subscribe, "tmdbid", None)
        search_label = self._search_label(mediainfo, media_type, season)
        prefix = f"[{search_label}][DIAN115]"
        if not tmdb_id:
            logger.debug(f"{prefix} 缺少 TMDB ID，跳过查询")
            return []
        if not self._dian115_email or not self._dian115_password:
            logger.warning("Dian115 已启用但未配置邮箱或密码")
            return []
        normalized_type = "movie" if media_type == MediaType.MOVIE else "tv"
        target_season = int(season or 0) if normalized_type == "tv" else 0
        started = time.monotonic()
        try:
            resources = self._get_dian115_resources()
            detail = resources.resource_detail(
                int(tmdb_id), normalized_type, target_season
            )
            resource = detail.get("resource") or {}
            candidates = []
            shares = detail.get("shares") or []
            restored_count = 0
            normalized_count = 0
            auto_unlock_skipped = 0
            inaccessible_skipped = 0
            for share in shares:
                if not isinstance(share, dict):
                    continue
                candidate = self._normalize_share(
                    share,
                    resource,
                    str(detail.get("resource_key") or ""),
                    str(detail.get("resource_path") or ""),
                    normalized_type,
                    int(tmdb_id),
                    season if normalized_type == "tv" else None,
                    test_mode=test_mode,
                )
                if not candidate:
                    continue
                normalized_count += 1
                # 免费或历史已解锁但当前详情未直接带链接时，调用 /unlock 只取回
                # 已有访问数据；服务端返回 already=true 或 cost_points=0，不消耗积分。
                if not test_mode and not candidate["url"] and not candidate["need_unlock"]:
                    unlocked = resources.unlock_share(
                        candidate["dian115_share_id"],
                        candidate["dian115_resource_id"],
                        max_unlock_points=0,
                        tmdb_id=candidate["dian115_tmdb_id"],
                        media_type=candidate["dian115_media_type"],
                        season=candidate["dian115_season"],
                    )
                    candidate["url"] = self._unlock_payload_url(unlocked)
                    restored_count += bool(candidate["url"])
                    candidate["need_access"] = not bool(candidate["url"])
                    candidate["is_unlocked"] = bool(candidate["url"])
                if not test_mode and candidate["need_unlock"] and not self._dian115_auto_unlock:
                    auto_unlock_skipped += 1
                    continue
                if not test_mode and not candidate["url"] and not candidate["need_unlock"]:
                    inaccessible_skipped += 1
                    continue
                candidates.append(candidate)

            before_limit_count = len(candidates)
            if test_mode:
                candidates = candidates[:100]
            else:
                candidates = self._prefilter_resource_order(
                    candidates,
                    season=season,
                    target_episodes=target_episodes,
                )[:self._dian115_candidate_limit]
            logger.debug(
                f"{prefix} WebAPI 查询完成：站点分享={len(shares)}，"
                f"规范化={normalized_count}，候选={len(candidates)}，"
                f"待积分解锁 {sum(bool(item.get('need_unlock')) for item in candidates)} 个，"
                f"恢复已有访问链接 {restored_count} 个，"
                f"跳过（自动解锁关闭={auto_unlock_skipped}，"
                f"无可用链接={inaccessible_skipped}，"
                f"预筛/上限={max(0, before_limit_count - len(candidates))}），"
                f"总耗时={time.monotonic() - started:.2f}s"
            )
            return candidates
        except Dian115Error as error:
            logger.error(
                f"{prefix} 查询失败："
                f"[{error.code or error.status_code or 'request'}] {error}"
            )
            return None
        except Exception as error:
            logger.error(f"{prefix} 查询异常：{error}")
            return None

    def set_data_funcs(self, get_func, save_func) -> None:
        """注入订阅积分历史的持久化读写函数。"""
        with self._dian115_budget_lock:
            self._dian115_get_data_func = get_func
            self._dian115_save_data_func = save_func

    def _load_dian115_points_history(self) -> Dict[str, int]:
        if not self._dian115_get_data_func:
            return {}
        data = self._dian115_get_data_func(self._HISTORY_KEY) or {}
        return data if isinstance(data, dict) else {}

    def _save_dian115_points_history(self, data: Dict[str, int]) -> None:
        if self._dian115_save_data_func:
            self._dian115_save_data_func(self._HISTORY_KEY, data)

    def reset_task_spent_points(self) -> None:
        with self._dian115_budget_lock:
            self._dian115_current_spent_points = 0
            self._dian115_sub_spent_points = 0
            self._dian115_current_sub_key = ""
        if self._dian115_enabled and self._dian115_auto_unlock:
            logger.debug("Dian115 任务积分账本已初始化")

    def reset_sub_spent_points(self, sub_key: str = "") -> None:
        with self._dian115_budget_lock:
            self._dian115_current_sub_key = sub_key
            history = self._load_dian115_points_history() if sub_key else {}
            self._dian115_sub_spent_points = int(history.get(sub_key, 0) or 0)
            spent_points = self._dian115_sub_spent_points
        if sub_key and self._dian115_enabled and self._dian115_auto_unlock:
            if spent_points > 0:
                logger.debug(
                    f"Dian115 订阅 {sub_key} 历史已花费 {spent_points} 积分，"
                    f"剩余预算 {max(0, self._dian115_max_points_per_sub - spent_points)}"
                )
            else:
                logger.debug(f"Dian115 订阅 {sub_key} 尚无积分消费记录")

    def clear_sub_points(self, sub_key: str) -> None:
        with self._dian115_budget_lock:
            history = self._load_dian115_points_history()
            if sub_key in history:
                del history[sub_key]
                self._save_dian115_points_history(history)
                logger.debug(f"Dian115 已清除订阅 {sub_key} 的历史积分记录")

    def has_dian115_unlock_budget(self, unlock_points: int) -> bool:
        try:
            points = max(0, int(unlock_points or 0))
        except (TypeError, ValueError):
            return False
        with self._dian115_budget_lock:
            return (
                    self._dian115_current_spent_points + points
                    <= self._dian115_max_unlock_points
                    and self._dian115_sub_spent_points + points
                    <= self._dian115_max_points_per_sub
            )

    def unlock_dian115_resource(
            self,
            share_id: int,
            resource_id: int,
            unlock_points: int,
            search_label: str = "",
            tmdb_id: int = 0,
            media_type: str = "",
            season: int = 0,
    ) -> Optional[str]:
        prefix = f"[{search_label}][DIAN115]" if search_label else "[DIAN115]"
        with self._dian115_budget_lock:
            if not self.has_dian115_unlock_budget(unlock_points):
                logger.warning(
                    f"{prefix} 积分预算不足：share_id={share_id}，"
                    f"需要 {unlock_points} 积分"
                )
                return None
            try:
                result = self._get_dian115_resources().unlock_share(
                    share_id,
                    resource_id,
                    max_unlock_points=unlock_points,
                    tmdb_id=tmdb_id,
                    media_type=media_type,
                    season=season,
                )
                actual_points = max(0, int(result.get("actual_points") or 0))
                self._dian115_current_spent_points += actual_points
                self._dian115_sub_spent_points += actual_points
                if self._dian115_current_sub_key:
                    history = self._load_dian115_points_history()
                    history[self._dian115_current_sub_key] = (
                        self._dian115_sub_spent_points
                    )
                    self._save_dian115_points_history(history)
                url = self._unlock_payload_url(result)
                if not url:
                    logger.error(
                        f"{prefix} 解锁响应未返回可用链接：share_id={share_id}，"
                        f"已按服务端结果记录 {actual_points} 积分"
                    )
                    return None
                if actual_points > unlock_points:
                    logger.error(
                        f"{prefix} 实际扣费高于搜索时价格："
                        f"预计={unlock_points}，实际={actual_points}"
                    )
                logger.info(
                    f"{prefix} 已取得分享链接：share_id={share_id}，"
                    f"消耗 {actual_points} 积分；"
                    f"任务剩余 {max(0, self._dian115_max_unlock_points - self._dian115_current_spent_points)}，"
                    f"当前订阅剩余 {max(0, self._dian115_max_points_per_sub - self._dian115_sub_spent_points)}"
                )
                return url
            except Dian115Error as error:
                logger.error(
                    f"{prefix} 解锁失败："
                    f"[{error.code or error.status_code or 'request'}] {error}"
                )
                return None
