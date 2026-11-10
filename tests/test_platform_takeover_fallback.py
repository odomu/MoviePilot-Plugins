"""Issue #34：平台资源接管失败时必须放行平台下载。"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from plugin_env import (
    OwnerDelegator,
    ensure_package,
    install_app_mocks,
    load_module,
    set_media_type,
)

install_app_mocks(
    "app.core",
    "app.core.config",
    "app.core.event",
    "app.core.metainfo",
    "app.db",
    "app.db.subscribe_oper",
    "app.log",
    "app.schemas",
    "app.schemas.event",
    "app.schemas.types",
    "app.chain",
    "app.chain.subscribe",
    "app.chain.download",
)
MediaType = set_media_type(movie="movie", tv="tv")

core = ensure_package("cloudsubscribe.core")
core.OwnerDelegator = OwnerDelegator
_media_module = ensure_package("cloudsubscribe.core.media")
if not hasattr(_media_module, "list_subscribes_by_tmdb_id"):
    _media_module.list_subscribes_by_tmdb_id = lambda *args, **kwargs: []
_search_types = ensure_package("cloudsubscribe.search.types")
if not hasattr(_search_types, "resource_type_from_url"):
    _search_types.resource_type_from_url = lambda *args, **kwargs: ""
if not hasattr(_search_types, "resource_type_name"):
    _search_types.resource_type_name = lambda *args, **kwargs: ""
ensure_package("cloudsubscribe.core.hook")

events = load_module(
    "cloudsubscribe.core.hook.events",
    "plugins.v2/cloudsubscribe/core/hook/events.py",
)


def _download_event(episodes):
    torrent = SimpleNamespace(title="测试资源")
    media = SimpleNamespace(tmdb_id=123, type=MediaType.TV)
    meta = SimpleNamespace(
        season_list=[1],
        episode_list=list(episodes),
    )
    context = SimpleNamespace(
        torrent_info=torrent,
        media_info=media,
        meta_info=meta,
    )
    event_data = SimpleNamespace(
        context=context,
        episodes=list(episodes),
        channel="",
        origin="",
        cancel=False,
        source="",
        reason="",
    )
    return SimpleNamespace(event_data=event_data)


class TestPlatformTakeoverFallback(unittest.TestCase):
    """接管失败不能拦截平台下载，否则订阅彻底没有下载来源。"""

    def setUp(self):
        self.owner = SimpleNamespace(
            _enabled=True,
            _sync_handler=object(),
            _platform_download_policy="cloud",
            _is_takeover_active=lambda: True,
            _is_subscribe_excluded=lambda subscribe_id: False,
        )
        self.handler = events.PluginEventHandler(self.owner)
        patcher = patch.object(
            events,
            "list_subscribes_by_tmdb_id",
            return_value=[SimpleNamespace(id=1, name="测试订阅")],
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_failed_takeover_allows_platform_download(self):
        with patch.object(
                events.PluginEventHandler,
                "_takeover_platform_download",
                return_value=False,
        ) as takeover:
            event = _download_event([1])
            self.handler.on_resource_download(event)

        self.assertFalse(event.event_data.cancel)
        self.assertEqual(event.event_data.source, "")
        takeover.assert_called_once()

    def test_successful_takeover_cancels_platform_download(self):
        with patch.object(
                events.PluginEventHandler,
                "_takeover_platform_download",
                return_value=True,
        ):
            event = _download_event([1])
            self.handler.on_resource_download(event)

        self.assertTrue(event.event_data.cancel)
        self.assertEqual(
            event.event_data.reason, "平台资源已提交插件离线下载"
        )

    def test_tv_without_episodes_allows_platform_download(self):
        with patch.object(
                events.PluginEventHandler,
                "_takeover_platform_download",
                return_value=False,
        ) as takeover:
            event = _download_event([])
            self.handler.on_resource_download(event)

        self.assertFalse(event.event_data.cancel)
        takeover.assert_not_called()
