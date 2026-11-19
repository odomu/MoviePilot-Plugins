"""电影洗版以媒体服务器/整理历史为基线的回归测试。STRM 库中已有媒体服务器条目、115 网盘源文件已删除时，
旧实现会把已算出的媒体服务器基线清零（``movie_history_score = -1``），
导致电影洗版永远无法比对候选。
"""

import types
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    ensure_package,
    install_app_mocks,
    load_module,
    set_media_type,
)

install_app_mocks(
    "app.core", "app.core.context", "app.core.metainfo", "app.db",
    "app.db.transferhistory_oper", "app.utils", "app.utils.string",
    "cloudsubscribe", "cloudsubscribe.core", "cloudsubscribe.drive",
    "cloudsubscribe.core.media", "cloudsubscribe.handlers",
    "cloudsubscribe.handlers.sync", "cloudsubscribe.handlers.notification",
)
MediaType = set_media_type(movie="电影", tv="电视剧")

ensure_package("cloudsubscribe.core").OwnerDelegator = OwnerDelegator
_media = ensure_package("cloudsubscribe.core.media")
_media.get_transfer_history_by = MagicMock(return_value=[])
_media.media_identity = MagicMock(return_value=(None, None))
_media.tmdb_id_of = lambda subscribe: getattr(subscribe, "tmdbid", None)
ensure_package("cloudsubscribe.drive.common").format_size = (
    lambda size: f"{int(size or 0)} B"
)

load_module(
    "cloudsubscribe.handlers.sync.utils",
    "plugins.v2/cloudsubscribe/handlers/sync/utils.py",
)
_movie = load_module(
    "cloudsubscribe.handlers.sync.movie",
    "plugins.v2/cloudsubscribe/handlers/sync/movie.py",
)
MovieSyncProcessor = _movie.MovieSyncProcessor


class DummyMediaInfo:
    def __init__(self):
        self.title = "绝境反击"
        self.year = "2026"
        self.type = MediaType.MOVIE
        self.tmdb_id = 1311031
        self.release_date = None

    @property
    def title_year(self):
        return f"{self.title} ({self.year})"


def _subscribe():
    return types.SimpleNamespace(
        id=1,
        name="绝境反击",
        year="2026",
        type="电影",
        tmdbid=1311031,
        season=None,
        start_episode=None,
        total_episode=None,
    )


def _media_item():
    return {
        "path": "/媒体库/电影/绝境反击 (2026)/绝境反击.2026.2160p.DV.strm",
        "size": 0,
        "rule_title": "绝境反击.2026.2160p.WEB-DL.DV.H.265",
    }


def _candidate():
    return {
        "url": "https://115cdn.com/s/resource",
        "title": "绝境反击 2160p",
        "source": "hdhive",
        "resource_type": "115",
        "size": 40 * 1024 ** 3,
    }


def _share_file():
    return {
        "id": "file-1",
        "name": "绝境反击.2026.2160p.WEB-DL.DV.H.265.mkv",
        "size": 40 * 1024 ** 3,
    }


class FakeOwner:
    """提供 process_movie_subscribe 依赖的最小宿主体。"""

    def __init__(self, media_items, candidate_score):
        self.media_items = list(media_items)
        self.candidate_score = candidate_score
        self.match_calls = []
        self.upgrade_checks = []
        self.transfers = []
        self.strm_kwargs = []
        self._CLOUD_MEDIA_ROOT = "/"
        self._cloud_transfer_path = "/整理/待整理"
        self._organize_after_transfer = True
        self._enable_cloud_upgrade = True
        self._upgrade_mode = "largest"
        self._strm_generate_enabled = False
        self._media_server_resolver = types.SimpleNamespace(
            movie_media=lambda chain, mediainfo: list(self.media_items)
        )
        self._media_server_notifier = MagicMock()
        self._subscribe_handler = MagicMock()
        self._search_handler = MagicMock()
        self._search_handler.get_enabled_sources.return_value = ["hdhive"]
        self._search_handler.search_sources.return_value = {"hdhive": [_candidate()]}
        self._search_handler._search_label.return_value = "label"

    def _stop_requested(self):
        return False

    def subscription_budget_key(self, subscribe, media_type=None):
        return "budget-key"

    def _is_cloud_upgrade_subscribe(self, subscribe):
        return True

    def _subscribe_mediainfo(self, subscribe, media_type, cache=True):
        return DummyMediaInfo()

    def _get_mp_rule_score(self, filename, filesize, subscribe, season, mediainfo=None):
        return 100

    def _calendar_date(self, value):
        return None

    def _set_task_phase(self, *args, **kwargs):
        return None

    def _timed_sync_call(self, name, func, *args, **kwargs):
        return func(*args, **kwargs)

    def _find_cloud_movie_file(self, subscribe, mediainfo):
        return None

    def _generate_strm(self, *args, **kwargs):
        return None

    def _scrape_metadata(self, *args, **kwargs):
        return None

    def _build_transfer_resource_batches(self, sources, results):
        return [
            (source, list(results.get(source) or []), False)
            for source in sources
        ]

    def _resolve_candidate_resource_url(self, candidates, index, resource, label, log_prefix=""):
        return resource.get("url")

    def _is_supported_resource(self, resource, url):
        return True

    def _is_cloud_resource_url(self, url):
        return True

    def _is_direct_cloud_resource_url(self, url):
        return False

    def _is_offline_url(self, url):
        return False

    def _is_magnet_url(self, url):
        return False

    def _resource_input_label(self, url):
        return "分享"

    def _resource_log_reference(self, url):
        return url

    def _resource_size_bytes(self, value):
        return int(value or 0)

    def _validated_resource_files(self, url, resource_title="", strict=False, **kwargs):
        return [_share_file()]

    def _is_cross_drive_resource(self, resource, url):
        return False

    def _match_movie_file(self, files, mediainfo, subscribe, resource_title,
                          require_media_match=True):
        self.match_calls.append(resource_title)
        return files[0], self.candidate_score

    def _should_upgrade_candidate(self, old_score, new_score, old_size=0,
                                  new_size=0, has_existing=True,
                                  suppress_when_unparsed=False):
        self.upgrade_checks.append((old_score, new_score, old_size, new_size))
        if new_score > old_score:
            return True, f"评分提升 {old_score}→{new_score}"
        return False, f"评分未提升 {old_score}→{new_score}"

    def _platform_target(self, root, subscribe, mediainfo, source_name,
                         season=None, episode=None):
        return "/电影/绝境反击 (2026)", source_name

    def _coexist_target_name(self, target_name, source_name, file_size=0, source_sha1=""):
        return f"{target_name} [共存]"

    def _transfer_file(self, *args, **kwargs):
        self.transfers.append(args)
        return True

    def _transfer_companion_subtitles(self, **kwargs):
        return []

    def _transfer_history_status(self, success, share_url):
        return "成功" if success else "失败"

    def _build_transfer_history_item(self, **kwargs):
        return dict(kwargs)

    def _generate_or_queue_strm(self, *args, **kwargs):
        self.strm_kwargs.append(kwargs)
        return "/strm/绝境反击.strm", None

    def _record_download_history(self, **kwargs):
        return None

    def __getattr__(self, name):
        value = MagicMock()
        setattr(self, name, value)
        return value


class MovieUpgradeBaselineTest(unittest.TestCase):
    def _run(self, owner):
        processor = MovieSyncProcessor(owner)
        return processor.process_movie_subscribe(
            subscribe=_subscribe(),
            history=[],
            transfer_details=[],
            transferred_count=0,
        )

    def test_media_server_baseline_survives_missing_cloud_file(self):
        """网盘无实体文件时不得丢弃媒体服务器基线，低分候选应直接跳过。"""
        owner = FakeOwner(media_items=[_media_item()], candidate_score=90)
        self._run(owner)
        self.assertEqual(owner.upgrade_checks, [(100, 90, 0, 40 * 1024 ** 3)])
        self.assertEqual(owner.transfers, [])

    def test_media_server_baseline_allows_upgrade_without_cloud_file(self):
        """基线成立时高分候选按洗版转存，且不携带不存在的旧网盘文件信息。"""
        owner = FakeOwner(media_items=[_media_item()], candidate_score=110)
        self._run(owner)
        self.assertEqual(len(owner.transfers), 1)
        self.assertEqual(len(owner.strm_kwargs), 1)
        kwargs = owner.strm_kwargs[0]
        self.assertTrue(kwargs.get("upgrade"))
        self.assertEqual(kwargs.get("upgrade_old_cloud_dir"), "")
        self.assertEqual(kwargs.get("upgrade_old_file_name"), "")

    def test_no_baseline_still_transfers_as_fresh_install(self):
        """媒体服务器与历史都为空时保持原行为：按普通转存处理，不做洗版比对。"""
        owner = FakeOwner(media_items=[], candidate_score=110)
        self._run(owner)
        self.assertEqual(owner.upgrade_checks, [])
        self.assertEqual(len(owner.transfers), 1)
        self.assertFalse(owner.strm_kwargs[0].get("upgrade"))


if __name__ == "__main__":
    unittest.main()