"""NFO 写入兼容 MoviePilot v2（bytes）与 v3（str）刮削器返回值。"""

import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from plugin_env import (
    install_app_mocks,
    load_module,
    register_module,
    set_media_type,
)

install_app_mocks()
MediaType = set_media_type()
register_module(
    "app.core.config",
    settings=types.SimpleNamespace(PROXY=None, NORMAL_USER_AGENT="ua"),
)
register_module(
    "app.core.metainfo",
    MetaInfoPath=lambda path: types.SimpleNamespace(
        begin_season=None, begin_episode=None
    ),
)
register_module("app.modules.themoviedb.scraper", TmdbScraper=MagicMock())
register_module("app.schemas", MediaInfo=object)
register_module("app.utils.http", RequestUtils=MagicMock())
register_module("app.log", logger=MagicMock())

_scraper = load_module(
    "cloudsubscribe.core.scraper",
    "plugins.v2/cloudsubscribe/core/scraper.py",
)
MediaScraper = _scraper.MediaScraper


class FakeTmdbScraper:
    """按 MoviePilot 目标版本返回 bytes 或 str 的 NFO。"""

    def __init__(self, nfo):
        self._nfo = nfo

    def get_metadata_nfo(self, *_args, **_kwargs):
        return self._nfo

    def get_metadata_img(self, *_args, **_kwargs):
        return {}


class TestNfoWrite(unittest.TestCase):
    def _scrape(self, nfo):
        with tempfile.TemporaryDirectory() as tmp:
            media_path = Path(tmp) / "Show" / "Show.S01E01.mkv"
            scraper = MediaScraper(nfo_enabled=True, image_enabled=False)
            scraper._scraper = FakeTmdbScraper(nfo)
            mediainfo = types.SimpleNamespace(
                type=MediaType.MOVIE, tmdb_id=1, title_year="Show (2024)"
            )
            created = scraper.scrape_batch(
                [{"media_path": media_path, "season": 1, "episode": 1}],
                mediainfo,
            )
            nfo_path = media_path.with_suffix(".nfo")
            self.assertEqual(created, 1)
            return nfo_path.read_bytes()

    def test_str_nfo_from_moviepilot_v3(self):
        self.assertEqual(self._scrape("<movie>中文标题</movie>"), "<movie>中文标题</movie>".encode("utf-8"))

    def test_bytes_nfo_from_moviepilot_v2(self):
        self.assertEqual(self._scrape(b"<movie>title</movie>"), b"<movie>title</movie>")


if __name__ == "__main__":
    unittest.main()
