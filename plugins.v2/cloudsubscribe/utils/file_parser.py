"""媒体文件树展开与季集标记解析。"""

import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple


class MediaFileParser:
    """文件结构和命名解析"""

    DEFAULT_VIDEO_EXTENSIONS = frozenset({".mkv", ".mp4", ".avi", ".iso"})
    DEFAULT_SUBTITLE_EXTENSIONS = frozenset({
        ".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup", ".idx", ".smi", ".mks",
    })
    VIDEO_EXTENSIONS = set(DEFAULT_VIDEO_EXTENSIONS)
    SUBTITLE_EXTENSIONS = set(DEFAULT_SUBTITLE_EXTENSIONS)

    _SXEX_PATTERN = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,4})")
    _SEASON_EPISODE_PATTERN = re.compile(r"[Ss](\d{1,2})[Ee]")
    _CN_SEASON_PATTERN = re.compile(r"第\s*(\d{1,2})\s*季")
    _EN_SEASON_PATTERN = re.compile(r"[Ss]eason\s*(\d{1,2})", re.IGNORECASE)
    ANY_SEASON_PATTERN = re.compile(
        r"[Ss]\d+[Ee]|第\s*\d+\s*季|[Ss]eason\s*\d+", re.IGNORECASE
    )

    _EPISODE_MARKER_PATTERNS = (
        re.compile(r"(?i)(?:^|[^A-Za-z0-9])EP?0*(\d{1,4})(?!\d)"),
        re.compile(r"第\s*(\d{1,4})\s*[集话話]"),
        re.compile(r"(?:更新[到至]|更[到至])\s*(?:第\s*)?(\d{1,4})\s*[集话話]?"),
    )
    _TRAILING_EPISODE_PATTERN = re.compile(r"(?:^|[^0-9])(\d{1,4})\s*$")
    _NON_EPISODE_NUMBERS = frozenset({
        2160, 1080, 720, 576, 480, 360, 264, 265,
    })
    _SXEX_RANGE_PATTERN = re.compile(
        r"(?i)S(?P<season>\d{1,3})\s*E(?P<start>\d{1,4})"
        r"(?:\s*(?:-|~|—|至|到)\s*(?:S(?P<end_season>\d{1,3})\s*)?"
        r"E?(?P<end>\d{1,4}))?"
    )
    _TEXT_RANGE_PATTERN = re.compile(
        r"(?:第\s*)?(\d{1,4})\s*集?\s*(?:-|~|—|至|到)\s*"
        r"(?:第\s*)?(?:最新\s*)?(\d{1,4})\s*集"
    )
    _CN_ONE_UNTIL_PATTERN = re.compile(
        r"(?:第\s*)?(?:一|1)\s*集?\s*(?:-|~|—|至|到)\s*"
        r"(?:第\s*)?(?:最新\s*)?(\d{1,4})\s*集"
    )
    _UPDATE_TO_PATTERN = re.compile(
        r"(?:更新[到至]|更[到至]|最新)\s*(?:第\s*)?(\d{1,4})\s*集?(?![0-9A-Za-z])"
    )
    _TOTAL_PATTERN = re.compile(r"(?:全|共)\s*(\d{1,4})\s*集")
    _SINGLE_PATTERN = re.compile(r"(?<!\d)(\d{1,4})\s*单集")
    _EP_RANGE_PATTERN = re.compile(
        r"(?i)(?:^|[^A-Za-z0-9])E(\d{1,4})\s*(?:-|~|—|至|到)\s*E?(\d{1,4})(?![0-9])"
    )

    @classmethod
    def configure_extensions(
            cls,
            video_extensions: Optional[Any] = None,
            subtitle_extensions: Optional[Any] = None,
    ) -> Tuple[set, set]:
        """动态配置视频与字幕扩展名，自动标准化带点小写格式。"""
        if video_extensions is not None:
            cls.VIDEO_EXTENSIONS = cls._normalize_extensions(
                video_extensions, cls.DEFAULT_VIDEO_EXTENSIONS
            )
        if subtitle_extensions is not None:
            cls.SUBTITLE_EXTENSIONS = cls._normalize_extensions(
                subtitle_extensions, cls.DEFAULT_SUBTITLE_EXTENSIONS
            )
        return set(cls.VIDEO_EXTENSIONS), set(cls.SUBTITLE_EXTENSIONS)

    @staticmethod
    def _normalize_extensions(raw: Any, default_set: Any) -> set:
        if isinstance(raw, str):
            parts = re.split(r"[,，\n\s]+", raw)
        elif isinstance(raw, (list, set, tuple)):
            parts = list(raw)
        else:
            return set(default_set)
        result = set()
        for item in parts:
            text = str(item or "").strip().lower()
            if not text:
                continue
            if not text.startswith("."):
                text = f".{text}"
            result.add(text)
        return result or set(default_set)

    @classmethod
    def is_video(cls, file_name: str) -> bool:
        ext = Path(str(file_name or "")).suffix.lower()
        if not ext:
            return False
        return ext in cls.VIDEO_EXTENSIONS or ext.lstrip(".") in cls.VIDEO_EXTENSIONS

    @classmethod
    def is_subtitle(cls, file_name: str) -> bool:
        ext = Path(str(file_name or "")).suffix.lower()
        if not ext:
            return False
        return ext in cls.SUBTITLE_EXTENSIONS or ext.lstrip(".") in cls.SUBTITLE_EXTENSIONS

    @classmethod
    def contains_other_season(cls, file_name: str, target_season: int) -> bool:
        for pattern in (
                cls._SEASON_EPISODE_PATTERN,
                cls._CN_SEASON_PATTERN,
                cls._EN_SEASON_PATTERN,
        ):
            match = pattern.search(file_name)
            if match:
                return int(match.group(1)) != target_season
        return False

    @classmethod
    def matches_target_season(cls, file_name: str, target_season: int) -> bool:
        for pattern in (
                cls._SEASON_EPISODE_PATTERN,
                cls._CN_SEASON_PATTERN,
                cls._EN_SEASON_PATTERN,
        ):
            match = pattern.search(file_name)
            if match:
                return int(match.group(1)) == target_season
        return False

    @classmethod
    def extract_season_episode(
            cls, file_name: str
    ) -> Optional[Tuple[int, int]]:
        match = cls._SXEX_PATTERN.search(file_name)
        if not match:
            return None
        return int(match.group(1)), int(match.group(2))

    @classmethod
    def extract_episode_number(
            cls, file_name: str, fallback_season: int = 1
    ) -> Optional[Tuple[int, int]]:
        """解析文件名中的季集号；无季号时使用 fallback_season。

        依次尝试 ``SxxExx``、``E12/EP12``、``第12集``、``更新到12``，最后识别
        ``片名 - 12.mkv`` 这类只有结尾集数的文件。分辨率与年份不会被当成集数。
        """
        text = str(file_name or "").strip()
        if not text:
            return None
        match = cls._SXEX_PATTERN.search(text)
        if match:
            return int(match.group(1)), int(match.group(2))
        season = max(1, int(fallback_season or 1))
        for pattern in cls._EPISODE_MARKER_PATTERNS:
            match = pattern.search(text)
            if match and int(match.group(1)) > 0:
                return season, int(match.group(1))
        match = cls._TRAILING_EPISODE_PATTERN.search(Path(text).stem)
        if not match:
            return None
        episode = int(match.group(1))
        if not 0 < episode < 1900 or episode in cls._NON_EPISODE_NUMBERS:
            return None
        return season, episode

    @staticmethod
    def _bounded_episode_range(start: int, end: int) -> List[int]:
        if start <= 0 or end < start or end - start > 500:
            return []
        return list(range(start, end + 1))

    @classmethod
    def extract_episode_coverage(
            cls, text: str, fallback_season: int = 1
    ) -> Dict[str, List[int]]:
        """从资源标题/remark 提取季集覆盖范围，供解锁前预筛使用。

        支持 ``S01E01-E12``、``1-176集``、``第1集到最新176集``、``更新到176``、
        ``全176集``、``176单集`` 等常见写法；无法识别时返回空字典。
        """
        target = str(text or "").strip()
        if not target:
            return {}
        season = max(1, int(fallback_season or 1))
        season_marker = cls._CN_SEASON_PATTERN.search(target) or cls._EN_SEASON_PATTERN.search(target)
        if season_marker:
            season = int(season_marker.group(1))
        episodes: Dict[str, set] = {}

        def add(season_number: int, values: List[int]) -> None:
            if values:
                episodes.setdefault(str(season_number), set()).update(values)

        for match in cls._SXEX_RANGE_PATTERN.finditer(target):
            start_season = int(match.group("season"))
            end_season = int(match.group("end_season") or start_season)
            if start_season != end_season:
                continue
            add(start_season, cls._bounded_episode_range(
                int(match.group("start")),
                int(match.group("end") or match.group("start")),
            ))
        for match in cls._CN_ONE_UNTIL_PATTERN.finditer(target):
            add(season, cls._bounded_episode_range(1, int(match.group(1))))
        for match in cls._TEXT_RANGE_PATTERN.finditer(target):
            add(season, cls._bounded_episode_range(
                int(match.group(1)), int(match.group(2))
            ))
        for match in cls._EP_RANGE_PATTERN.finditer(target):
            add(season, cls._bounded_episode_range(
                int(match.group(1)), int(match.group(2))
            ))
        for match in cls._UPDATE_TO_PATTERN.finditer(target):
            add(season, cls._bounded_episode_range(1, int(match.group(1))))
        for match in cls._TOTAL_PATTERN.finditer(target):
            add(season, cls._bounded_episode_range(1, int(match.group(1))))
        for match in cls._SINGLE_PATTERN.finditer(target):
            episode = int(match.group(1))
            if episode > 0:
                add(season, [episode])
        return {
            key: sorted(values)
            for key, values in episodes.items()
            if values
        }

    @classmethod
    def iter_files(cls, files: list, parent_path: str = "") -> Iterator[Any]:
        for item in files or []:
            name = str(item.get("name") or item.get("file_name") or "").strip()
            relative_path = "/".join(
                value for value in (parent_path.strip("/"), name) if value
            )
            if item.get("is_dir"):
                yield from cls.iter_files(
                    item.get("children") or [], relative_path
                )
            else:
                if relative_path and isinstance(item, dict):
                    item.setdefault("_relative_path", relative_path)
                    if parent_path:
                        item.setdefault("parent_path", parent_path)
                    yield item
                elif relative_path:
                    enriched = dict(item)
                    enriched["_relative_path"] = relative_path
                    if parent_path:
                        enriched["parent_path"] = parent_path
                    yield enriched
                else:
                    if isinstance(item, dict) and item.get("relative_path"):
                        item.setdefault("_relative_path", item["relative_path"])
                    yield item
