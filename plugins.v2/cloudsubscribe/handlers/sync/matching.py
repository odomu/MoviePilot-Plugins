"""网盘分享文件的电影/剧集匹配。"""

from pathlib import Path
from typing import Dict, List

from app.schemas import MediaInfo

from ...core import OwnerDelegator
from ...utils import FileMatcher


class FileMatchingService(OwnerDelegator):
    """只负责从已读取的文件树中选择媒体文件。"""

    def _match_episode_files(
            self,
            files: list,
            mediainfo: MediaInfo,
            subscribe,
            season: int,
            episodes: List[int],
            require_media_match: bool = True,
    ) -> Dict[int, tuple]:
        """按结构收集剧集候选，再使用规则组选择文件。"""
        episode_list = list(dict.fromkeys(int(value) for value in episodes))
        candidates = FileMatcher.episode_candidates(
            files,
            season,
            episode_list,
            mediainfo=mediainfo if require_media_match else None,
        )
        return {
            episode: self._search_handler.select_file_candidate(
                candidates.get(episode) or [], mediainfo, subscribe
            )
            for episode in episode_list
        }

    def _match_movie_file(
            self,
            files: list,
            mediainfo: MediaInfo,
            subscribe,
            resource_title: str = "",
            require_media_match: bool = True,
    ) -> tuple:
        """按媒体文件结构收集电影候选，再使用规则组选择。"""
        matched = self._search_handler.select_file_candidate(
            FileMatcher.movie_candidates(
                files,
                mediainfo=mediainfo if require_media_match else None,
            ),
            mediainfo,
            subscribe,
        )
        if matched[0] or not require_media_match:
            return matched if matched[0] else (None, 0)

        # 跨盘文件名被网盘混淆时，仅允许“资源标题匹配 + 唯一大视频”兜底。
        fallback = FileMatcher.movie_candidates(files)
        if (
                len(fallback) != 1
                or not FileMatcher.media_name_matches(resource_title, mediainfo)
        ):
            return None, 0
        actual = fallback[0]
        scoring_item = dict(actual)
        scoring_item["name"] = (
            f"{str(resource_title).strip()}{Path(str(actual.get('name') or '')).suffix}"
        )
        selected, score = self._search_handler.select_file_candidate(
            [scoring_item], mediainfo, subscribe
        )
        return (actual, score) if selected else (None, 0)
