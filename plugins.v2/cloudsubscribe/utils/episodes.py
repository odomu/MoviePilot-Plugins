"""集数相关的共享工具：区间格式化与季总集数推断。"""

from typing import Any, Iterable, List, Optional

try:
    from app.domain.episode import format_ranges as platform_format_ranges
except Exception:  # pragma: no cover - 低版本核心没有该模块
    platform_format_ranges = None


def format_episode_ranges(episodes: Iterable[Any]) -> str:
    """把集数集合压缩为 E01-E03、E05 形式，优先使用平台原生 format_ranges。"""
    if not episodes:
        return "无"
    numbers = sorted({int(ep) for ep in episodes if str(ep).isdigit() or isinstance(ep, int)})
    if not numbers:
        return "无"
    if platform_format_ranges:
        try:
            result = platform_format_ranges(numbers)
            if result:
                return result
        except Exception:
            pass
    ranges: List[str] = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(f"E{start:02d}" if start == previous else f"E{start:02d}-E{previous:02d}")
        start = previous = number
    ranges.append(f"E{start:02d}" if start == previous else f"E{start:02d}-E{previous:02d}")
    return "、".join(ranges)


def season_total_episodes(mediainfo: Any, season: Optional[int]) -> int:
    """从识别结果推断该季总集数：优先季集清单，其次各季详情。"""
    if not mediainfo or season is None:
        return 0
    seasons = getattr(mediainfo, "seasons", None)
    if isinstance(seasons, dict):
        values = seasons.get(season) or seasons.get(str(season)) or []
        numbers = [int(value) for value in values if str(value).isdigit()]
        if numbers:
            return max(numbers)
    for item in getattr(mediainfo, "season_info", None) or []:
        if isinstance(item, dict):
            season_number = item.get("season_number")
            episode_count = item.get("episode_count")
        else:
            season_number = getattr(item, "season_number", None)
            episode_count = getattr(item, "episode_count", None)
        if int(season_number or 0) == int(season) and int(episode_count or 0) > 0:
            return int(episode_count)
    return 0
