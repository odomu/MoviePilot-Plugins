"""按媒体结构匹配实际文件候选。"""

import re
from typing import Any, Dict, List

from app.log import logger

from .file_parser import MediaFileParser


class FileMatcher:
    """电影或剧集候选"""

    @staticmethod
    def episode_candidates(
            files: list,
            season: int,
            episodes: List[int],
    ) -> Dict[int, List[Any]]:
        episode_list = list(dict.fromkeys(int(value) for value in episodes))
        if not episode_list:
            return {}

        target_episodes = set(episode_list)
        strict_matches = {episode: [] for episode in episode_list}
        loose_matches = {episode: [] for episode in episode_list}
        loosest_matches = {episode: [] for episode in episode_list}
        loose_patterns = {
            episode: (
                re.compile(rf"第\s*{episode}\s*集", re.IGNORECASE),
                re.compile(rf"[Ee][Pp]{episode}(?!\d)", re.IGNORECASE),
                re.compile(
                    rf"[\[\(\s\.\-_][Ee]0?{episode}[\]\)\s\.\-_]",
                    re.IGNORECASE,
                ),
            )
            for episode in episode_list
        }
        loosest_patterns = {
            episode: re.compile(
                rf"[\.\s\-_]0?{episode}[\.\s\-_]", re.IGNORECASE
            )
            for episode in episode_list
        }
        total_files = 0

        for item in MediaFileParser.iter_files(files):
            file_name = str(item.get("name") or "")
            total_files += 1
            if not MediaFileParser.is_video(file_name):
                continue
            if MediaFileParser.contains_other_season(file_name, season):
                continue

            season_episode = MediaFileParser.extract_season_episode(file_name)
            if season_episode:
                found_season, found_episode = season_episode
                if found_season == season and found_episode in target_episodes:
                    strict_matches[found_episode].append(item)
                continue

            matches_season = MediaFileParser.matches_target_season(file_name, season)
            has_season_marker = bool(
                MediaFileParser.ANY_SEASON_PATTERN.search(file_name)
            )
            for episode in episode_list:
                if any(pattern.search(file_name) for pattern in loose_patterns[episode]):
                    if season == 1 or matches_season or not has_season_marker:
                        loose_matches[episode].append(item)
                    continue
                if matches_season and loosest_patterns[episode].search(file_name):
                    loosest_matches[episode].append(item)

        results = {}
        for episode in episode_list:
            results[episode] = (
                    strict_matches[episode]
                    or loose_matches[episode]
                    or loosest_matches[episode]
            )
            if not results[episode] and total_files:
                logger.debug(f"S{season:02d}E{episode:02d} 未匹配到实际媒体文件")
        return results

    @staticmethod
    def movie_candidates(files: list, min_size_mb: int = 500) -> List[Any]:
        min_size = max(0, int(min_size_mb or 0)) * 1024 * 1024
        candidates = [
            item
            for item in MediaFileParser.iter_files(files)
            if MediaFileParser.is_video(str(item.get("name") or ""))
               and int(item.get("size") or 0) >= min_size
        ]
        candidates.sort(key=lambda item: int(item.get("size") or 0), reverse=True)
        return candidates
