"""规则评分。"""

import hashlib
import json
from pathlib import Path
from typing import Optional

from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType
from app.utils.string import StringUtils

from ...core import OwnerDelegator
from ...core.media import apply_media_identity, tmdb_id_of
from ...drive.common import format_size


class UpgradeRuleScoringService(OwnerDelegator):
    @staticmethod
    def _resource_size_bytes(value: object) -> int:
        """将搜索卡片的大小展示值转换为字节。"""
        return max(0, int(StringUtils.num_filesize(value) or 0))

    def _should_upgrade_candidate(
            self,
            old_score: int,
            new_score: int,
            old_size: int = 0,
            new_size: int = 0,
            has_existing: bool = True,
            suppress_when_unparsed: bool = False,
    ) -> tuple[bool, str]:
        """按评分与洗版模式判断候选可否转存；suppress_when_unparsed 时对无版本集保守跳过。"""
        if not has_existing:
            if suppress_when_unparsed:
                return False, "目标季存在无法解析集号的既有文件，保守跳过转存"
            return True, "无现有版本"
        old_score = int(old_score or 0)
        new_score = int(new_score or 0)
        old_size = max(0, int(old_size or 0))
        new_size = max(0, int(new_size or 0))
        if new_score > old_score:
            return True, f"评分提升 {old_score}→{new_score}"
        if new_score < old_score:
            return False, f"评分降低 {old_score}→{new_score}"

        mode = self._upgrade_mode
        threshold = max(
            0, int(getattr(self, "_upgrade_size_threshold", 0) or 0)
        )
        if mode == "largest":
            if old_size and new_size and new_size > old_size:
                diff = new_size - old_size
                if threshold and diff < threshold:
                    return False, (
                        f"同评分文件差异 {format_size(diff)} "
                        f"未达到阈值 {format_size(threshold)}"
                    )
                return True, (
                    f"同评分保留更大文件 {format_size(old_size)}→"
                    f"{format_size(new_size)}（差异 {format_size(diff)}）"
                )
            return False, "同评分且候选文件未更大"
        if mode == "smallest":
            if old_size and new_size and new_size < old_size:
                diff = old_size - new_size
                if threshold and diff < threshold:
                    return False, (
                        f"同评分文件差异 {format_size(diff)} "
                        f"未达到阈值 {format_size(threshold)}"
                    )
                return True, (
                    f"同评分保留更小文件 {format_size(old_size)}→"
                    f"{format_size(new_size)}（差异 {format_size(diff)}）"
                )
            return False, "同评分且候选文件未更小"
        return False, "同评分不执行替换"

    @staticmethod
    def _coexist_target_name(
            target_name: str,
            source_name: str,
            file_size: int = 0,
            source_sha1: str = "",
    ) -> str:
        """共存模式生成稳定的独立目标名，避免与现有版本同名冲突。"""
        target = Path(str(target_name or source_name))
        identity = str(source_sha1 or "").strip().upper() or hashlib.sha1(
            f"{source_name}|{int(file_size or 0)}".encode("utf-8")
        ).hexdigest()
        return f"{target.stem} [共存-{identity[:8]}]{target.suffix}"

    def _get_mp_rule_score(
            self,
            filename: str,
            filesize: int,
            subscribe,
            season: int,
            mediainfo: Optional[MediaInfo] = None,
    ) -> int:
        """返回规则组的 ``pri_order``，未匹配时为 0。"""
        try:
            rule_mediainfo = mediainfo or MediaInfo(
                type=MediaType.TV,
                title=getattr(subscribe, "name", None),
                year=getattr(subscribe, "year", None),
                tmdb_id=tmdb_id_of(subscribe),
            )
            apply_media_identity(
                rule_mediainfo, "themoviedb", tmdb_id_of(subscribe)
            )
            selected, priority = self._search_handler.select_file_candidate(
                [{"name": filename, "size": filesize or 0}],
                rule_mediainfo,
                subscribe,
            )
            return int(priority or 0) if selected else 0
        except Exception as error:
            logger.warning(f"MoviePilot 规则评分失败：{error}")
            return 0

    @staticmethod
    def _read_ep_priority(subscribe) -> dict:
        """读取订阅已保存的逐集 ``pri_order``。"""
        raw = getattr(subscribe, "episode_priority", None) or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = {}
        result = {}
        if not isinstance(raw, dict):
            return result
        for key, value in raw.items():
            try:
                result[key] = int(
                    value.get("score", 0) if isinstance(value, dict) else value or 0
                )
            except (TypeError, ValueError):
                result[key] = 0
        return result
