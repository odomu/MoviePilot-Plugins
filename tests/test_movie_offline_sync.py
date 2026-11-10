"""测试电影离线资源处理及磁力链接过滤机制（Issue #11 修复验证）。"""

import importlib.util
import os
import sys
import types
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    ensure_package,
    install_app_mocks,
    set_media_type,
)

_SYNC_COMPONENTS = (
    "baseline", "cleanup", "dispatcher", "directory", "history", "matching",
    "metadata", "movie", "naming", "notify", "pipeline", "platform_history",
    "postprocess", "pt_upgrade", "queue", "resources", "retry", "rule_scoring",
    "state", "status", "subtitles", "television", "upgrade", "utils",
)
install_app_mocks(
    "app.core", "app.core.config", "app.core.context", "app.core.metainfo",
    "app.db", "app.db.subscribe_oper", "app.modules",
    "app.modules.filemanager", "app.modules.filemanager.transhandler",
    "app.utils", "app.utils.http", "app.helper", "app.application", "app.adapters",
    "cloudsubscribe", "cloudsubscribe.core", "cloudsubscribe.core.media",
    "cloudsubscribe.drive", "cloudsubscribe.drive.scanner",
    "cloudsubscribe.handlers", "cloudsubscribe.handlers.sync",
    "cloudsubscribe.handlers.notification", "cloudsubscribe.handlers.search",
    "cloudsubscribe.handlers.subscription", "cloudsubscribe.utils",
    "cloudsubscribe.utils.cache", "cloudsubscribe.search", "cloudsubscribe.search.types",
    *[f"cloudsubscribe.handlers.sync.{name}" for name in _SYNC_COMPONENTS],
)
MediaType = set_media_type(movie="movie", tv="tv")

_step_share_expiry = ensure_package("cloudsubscribe.utils.share_expiry")
_step_share_expiry.is_share_expired = lambda url: False
_step_share_expiry.mark_share_expired = lambda *args, **kwargs: None
_step_share_expiry.is_expired_share_error = lambda *args, **kwargs: False

mock_notif = ensure_package("cloudsubscribe.handlers.notification")
mock_notif.MediaServerNotifier = MagicMock()
mock_notif.MediaServerResolver = MagicMock()
ensure_package("cloudsubscribe.handlers.search").SearchHandler = MagicMock()
ensure_package("cloudsubscribe.handlers.subscription").SubscribeHandler = MagicMock()

mock_core = ensure_package("cloudsubscribe.core")
mock_core.OwnerDelegator = OwnerDelegator
mock_core.CloudDriveCapability = MagicMock()

mock_core_search = types.ModuleType("cloudsubscribe.core.search")
mock_core_search.SearchQuery = MagicMock()
sys.modules["cloudsubscribe.core.search"] = mock_core_search

# 加载 matching.py
matching_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../plugins.v2/cloudsubscribe/search/matching.py")
)
spec_match = importlib.util.spec_from_file_location("cloudsubscribe.search.matching", matching_path)
mod_match = importlib.util.module_from_spec(spec_match)
mod_match.__package__ = "cloudsubscribe.search"
sys.modules["cloudsubscribe.search.matching"] = mod_match
spec_match.loader.exec_module(mod_match)
resource_title_matches = mod_match.resource_title_matches
extract_season = mod_match.extract_season
# 加载 utils/file_parser.py
parser_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../plugins.v2/cloudsubscribe/utils/file_parser.py")
)
spec_p = importlib.util.spec_from_file_location("cloudsubscribe.utils.file_parser", parser_path)
mod_p = importlib.util.module_from_spec(spec_p)
spec_p.loader.exec_module(mod_p)
mock_utils = sys.modules["cloudsubscribe.utils"]
mock_utils.MediaFileParser = mod_p.MediaFileParser
sys.modules["cloudsubscribe.utils.file_parser"] = mod_p

# 加载 utils/magnet.py
utils_mag_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../plugins.v2/cloudsubscribe/utils/magnet.py")
)
spec_umag = importlib.util.spec_from_file_location("cloudsubscribe.utils.magnet", utils_mag_path)
mod_umag = importlib.util.module_from_spec(spec_umag)
mod_umag.__package__ = "cloudsubscribe.utils"
sys.modules["cloudsubscribe.utils.magnet"] = mod_umag
spec_umag.loader.exec_module(mod_umag)
mock_utils.parse_magnet_metadata = mod_umag.parse_magnet_metadata

# 加载 search/magnet.py
magnet_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../plugins.v2/cloudsubscribe/search/magnet.py")
)
spec_mag = importlib.util.spec_from_file_location("cloudsubscribe.search.magnet", magnet_path)
mod_mag = importlib.util.module_from_spec(spec_mag)
mod_mag.__package__ = "cloudsubscribe.search"
sys.modules["cloudsubscribe.search.magnet"] = mod_mag
spec_mag.loader.exec_module(mod_mag)
media_titles = mod_mag.media_titles

# 加载 piratebay/service.py
mock_pb_client = types.ModuleType("cloudsubscribe.search.piratebay.client")
mock_pb_client.PirateBayClient = MagicMock()
mock_pb_client.PirateBayError = RuntimeError
sys.modules["cloudsubscribe.search.piratebay"] = types.ModuleType("cloudsubscribe.search.piratebay")
sys.modules["cloudsubscribe.search.piratebay"].__path__ = []
sys.modules["cloudsubscribe.search.piratebay.client"] = mock_pb_client

pb_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../plugins.v2/cloudsubscribe/search/piratebay/service.py")
)
spec_pb = importlib.util.spec_from_file_location("cloudsubscribe.search.piratebay.service", pb_path)
mod_pb = importlib.util.module_from_spec(spec_pb)
mod_pb.__package__ = "cloudsubscribe.search.piratebay"
sys.modules["cloudsubscribe.search.piratebay.service"] = mod_pb
spec_pb.loader.exec_module(mod_pb)
PirateBaySearchService = mod_pb.PirateBaySearchService

# 加载 handlers/sync/utils.py
utils_sync_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../plugins.v2/cloudsubscribe/handlers/sync/utils.py")
)
spec_u = importlib.util.spec_from_file_location("cloudsubscribe.handlers.sync.utils", utils_sync_path)
mod_u = importlib.util.module_from_spec(spec_u)
spec_u.loader.exec_module(mod_u)
sys.modules["cloudsubscribe.handlers.sync.utils"] = mod_u

# 加载 handlers/sync/service.py
service_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../plugins.v2/cloudsubscribe/handlers/sync/service.py")
)
spec_serv = importlib.util.spec_from_file_location("cloudsubscribe.handlers.sync.service", service_path)
mod_serv = importlib.util.module_from_spec(spec_serv)
mod_serv.__package__ = "cloudsubscribe.handlers.sync"
sys.modules["cloudsubscribe.handlers.sync.service"] = mod_serv
spec_serv.loader.exec_module(mod_serv)
SyncHandler = mod_serv.SyncHandler


class DummyMediaInfo:
    def __init__(self, title, year="2024", media_type=MediaType.MOVIE, original_title=""):
        self.title = title
        self.year = str(year)
        self.type = media_type
        self.original_title = original_title or title
        self.en_title = ""
        self.original_name = ""


class DummyOfflineSyncHandler:
    """模拟测试 _queue_magnet_package 的核心过滤与占位写入逻辑。"""

    def __init__(self):
        import threading
        self._offline_pending_lock = threading.Lock()
        self._cloud_transfer_path = "/downloads"
        self._pending_store = {}
        self._offline_download = MagicMock()
        self._offline_download.parse_magnet_link.return_value = {
            "metadata": {"metadata_available": True, "torrent_files": ["Coyote.mkv"]}
        }

    def _get_data(self, key):
        return self._pending_store.get(key, {})

    def _save_offline_pending(self, data):
        self._pending_store["pending_offline_strm"] = data

    def _save_data(self, key, data):
        self._pending_store[key] = data

    _OFFLINE_PENDING_KEY = "pending_offline_strm"

    def _offline_hash(self, share_url):
        return "A4F71D97B5F78C1DE0B904C542ECA2B86297C827"

    def _prepare_magnet_resource(self, resource, share_url):
        return resource.get("title", "")

    def _magnet_title_seasons(self, resource):
        title = resource.get("title", "")
        import re
        m = re.search(r"[Ss](\d{1,2})", title)
        if m:
            return {int(m.group(1))}
        return set()

    def _magnet_title_episodes(self, resource, season):
        title = resource.get("title", "")
        import re
        m = re.search(r"[Ee](\d{1,4})", title)
        if m:
            return {int(m.group(1))}
        return set()

    def _resource_preview_episodes(self, resource, season):
        return set()

    def _format_episode_ranges(self, episodes):
        return str(sorted(episodes))

    def _is_magnet_url(self, url):
        return str(url).startswith("magnet:?")

    def _serialize_mediainfo(self, mediainfo):
        return {"title": getattr(mediainfo, "title", "")}

    def _unreserved_episodes(self, *args, **kwargs):
        return []

    def _submit_offline_packages(self, contexts):
        return [ctx["pending_key"] for ctx in contexts]


DummyOfflineSyncHandler._queue_magnet_package = SyncHandler._queue_magnet_package


class TestMovieOfflineSync(unittest.TestCase):
    """测试电影离线同步与磁力链接过滤。"""

    def test_resource_title_matches(self):
        # 英文电影标题匹配
        self.assertTrue(
            resource_title_matches(
                "Coyote vs Acme 2026 1080p AMZN WEB-DL DDP5 1 Atmos H 264-BYNDR",
                ["Coyote vs Acme"],
                expected_year="2026",
            )
        )
        self.assertTrue(
            resource_title_matches(
                "Coyote.vs.Acme.2026.1080p.DCPRIP.HEVC.x265.RMTeam",
                ["Coyote vs Acme"],
                expected_year="2026",
            )
        )
        # 中文电影标题匹配
        self.assertTrue(
            resource_title_matches(
                "出入平安.2024.1080p.WEB-DL.H264",
                ["出入平安"],
                expected_year="2024",
            )
        )
        # 不相关的电视剧标题不能匹配电影
        self.assertFalse(
            resource_title_matches(
                "The.Gentlemen.2024.S01.COMPLETE.1080p.NF.WEB.H264-NHTFS[TGx]",
                ["出入平安"],
                expected_year="2024",
            )
        )
        self.assertFalse(
            resource_title_matches(
                "Dark Matter 2024 S02E05 Love and Be Loved 1080p",
                ["出入平安"],
                expected_year="2024",
            )
        )

    def test_movie_queue_magnet_package_without_target_episodes(self):
        """验证电影提交离线包时 target_episodes 为 None 不会抛出 'NoneType' object is not iterable。"""
        handler = DummyOfflineSyncHandler()
        resource = {
            "title": "Coyote vs Acme 2026 1080p AMZN WEB-DL",
            "size": 1024 * 1024 * 1024,
            "magnet_metadata": {"metadata_available": True},
        }
        mediainfo = DummyMediaInfo("Coyote vs Acme", year="2026", media_type=MediaType.MOVIE)
        subscribe = types.SimpleNamespace(id=1, name="Coyote vs Acme")

        # 调用 _queue_magnet_package，此时 season 为 None，target_episodes 为 None
        pending_key = handler._queue_magnet_package(
            resource=resource,
            share_url="magnet:?xt=urn:btih:A4F71D97B5F78C1DE0B904C542ECA2B86297C827",
            subscribe=subscribe,
            mediainfo=mediainfo,
            season=None,
            target_episodes=None,
        )

        self.assertTrue(pending_key.startswith("magnet:A4F71D97B5F78C1DE0B904C542ECA2B86297C827:1"))
        pending = handler._get_data("pending_offline_strm")
        self.assertIn(pending_key, pending)
        self.assertEqual(pending[pending_key]["target_episodes"], [])

    def test_movie_queue_magnet_package_rejects_tv_season(self):
        """验证电影提交离线包时，若资源标题包含剧集季数则被预过滤拒绝。"""
        handler = DummyOfflineSyncHandler()
        resource = {
            "title": "The.Gentlemen.2024.S01.COMPLETE.1080p.NF.WEB.H264",
            "size": 1024 * 1024 * 1024,
            "magnet_metadata": {"metadata_available": True},
        }
        mediainfo = DummyMediaInfo("出入平安", year="2024", media_type=MediaType.MOVIE)
        subscribe = types.SimpleNamespace(id=1, name="出入平安")

        # 此时 season 为 None，但资源标题包含 S01
        pending_key = handler._queue_magnet_package(
            resource=resource,
            share_url="magnet:?xt=urn:btih:9DCEDB4DA9667EFA5AE2020F5EB70D57BFA5C5D9",
            subscribe=subscribe,
            mediainfo=mediainfo,
            season=None,
            target_episodes=None,
        )

        self.assertEqual(pending_key, "")

    def test_piratebay_keywords_filters_non_ascii(self):
        """海盗湾关键词应过滤纯中文，避免退化为纯年份检索。"""
        mediainfo_cn = DummyMediaInfo("出入平安", year="2024")
        keywords_cn = PirateBaySearchService._keywords(mediainfo_cn, MediaType.MOVIE, season=None)
        # 纯中文标题没有英文字母，不应生成纯年份或纯中文关键词
        self.assertEqual(keywords_cn, [])

        mediainfo_en = DummyMediaInfo("Coyote vs Acme", year="2026")
        keywords_en = PirateBaySearchService._keywords(mediainfo_en, MediaType.MOVIE, season=None)
        self.assertIn("Coyote vs Acme 2026", keywords_en)

    def test_piratebay_search_filters_tv_seasons_for_movies(self):
        """海盗湾电影检索时应过滤电视剧集并检查标题匹配。"""
        mock_client = MagicMock()
        mock_client.search.return_value = [
            {"info_hash": "hash1", "title": "The.Gentlemen.2024.S01.COMPLETE"},
            {"info_hash": "hash2", "title": "Coyote.vs.Acme.2024.1080p"},
        ]
        service = PirateBaySearchService(mock_client)
        mediainfo = DummyMediaInfo("Coyote vs Acme", year="2024", media_type=MediaType.MOVIE)
        query = types.SimpleNamespace(
            mediainfo=mediainfo,
            media_type=MediaType.MOVIE,
            season=None,
            result_limit=20,
        )

        results = service.search(query)
        titles = [r.get("title") for r in results]
        self.assertNotIn("The.Gentlemen.2024.S01.COMPLETE", titles)
        self.assertIn("Coyote.vs.Acme.2024.1080p", titles)


if __name__ == "__main__":
    unittest.main()
