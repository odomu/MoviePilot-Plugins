"""Issue #32：转存确认失效的分享要拉黑，不能每轮重复解锁转存。"""

import os
import unittest
from threading import RLock
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import (
    ensure_package,
    install_app_mocks,
    load_module,
    register_mock,
    register_package,
)

install_app_mocks(
    "app.core",
    "app.core.config",
    "app.core.cache",
    "app.log",
    "app.utils",
    "app.utils.string",
    "app.schemas",
    "app.schemas.types",
)

_marked_shares = set()


def _normalize(url):
    return str(url or "").split("?")[0].rstrip("/")


def _mock_is_expired(url):
    return _normalize(url) in _marked_shares


def _mock_mark_expired(url, *args, **kwargs):
    _marked_shares.add(_normalize(url))


def _mock_expired_error(error_code=None, message="", *args, **kwargs):
    text = str(message or "")
    return (
        str(error_code or "") == "4100018"
        or "过期" in text
        or "失效" in text
    )


register_mock(
    "cloudsubscribe.utils.share_expiry",
    is_share_expired=_mock_is_expired,
    mark_share_expired=_mock_mark_expired,
    is_expired_share_error=_mock_expired_error,
)

_plugin_root = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "plugins.v2",
    "cloudsubscribe",
)

load_module("cloudsubscribe.core.cloud", "plugins.v2/cloudsubscribe/core/cloud.py")
load_module(
    "cloudsubscribe.core.delegation",
    "plugins.v2/cloudsubscribe/core/delegation.py",
)
ensure_package("cloudsubscribe.core")
register_mock(
    "cloudsubscribe.drive.common",
    DRIVE_RETRY_EXCEPTIONS=(Exception,),
    should_skip_season_dir=lambda *args, **kwargs: False,
)
register_package("cloudsubscribe.drive.p115", paths=[os.path.join(_plugin_root, "drive", "p115")])
share_module = load_module(
    "cloudsubscribe.drive.p115.share",
    "plugins.v2/cloudsubscribe/drive/p115/share.py",
)

_SHARE_URL = "https://115.com/s/swxabc?password=pwd"


class TestP115ExpiredShare(unittest.TestCase):
    def setUp(self):
        _marked_shares.clear()
        owner = SimpleNamespace(
            _share_cache_lock=RLock(),
            _share_info_cache={},
            _share_status_cache={},
            _share_file_cache={},
            client=MagicMock(),
            DEFAULT_MAX_RETRIES=0,
            _login_checked=True,
            is_vip=True,
        )
        owner.is_offline_url = lambda url: False
        owner._rate_limited_call = lambda func, *args, **kwargs: func(*args)
        owner._ios_request_kwargs = lambda **kwargs: {}
        self.owner = owner
        self.service = share_module.ShareService(owner)

    def tearDown(self):
        _marked_shares.clear()

    def test_transfer_expiry_marks_share_url(self):
        self.owner.client.share_receive.return_value = {
            "state": False,
            "errno": 4100018,
            "error": "链接已过期",
        }

        result = self.service._do_transfer(
            share_code="swxabc",
            receive_code="pwd",
            file_id="1",
            parent_id=1,
            save_path="/媒体",
            share_url=_SHARE_URL,
        )

        self.assertFalse(result)
        self.assertTrue(_mock_is_expired(_SHARE_URL))
        self.assertEqual(self.owner.client.share_receive.call_count, 1)

    def test_expired_share_skips_remote_transfer(self):
        _mock_mark_expired(_SHARE_URL)

        result = self.service._do_transfer(
            share_code="swxabc",
            receive_code="pwd",
            file_id="1",
            parent_id=1,
            save_path="/媒体",
            share_url=_SHARE_URL,
        )

        self.assertFalse(result)
        self.owner.client.share_receive.assert_not_called()

    def test_share_status_reports_expired_before_snapshot(self):
        _mock_mark_expired(_SHARE_URL)

        status = self.service.check_share_status(_SHARE_URL)

        self.assertFalse(status.is_valid)
        self.assertTrue(status.is_expired)
        self.owner.client.share_snap.assert_not_called()

    def test_error_classification(self):
        self.assertTrue(self.service._is_share_expired_error(4100018, ""))
        self.assertTrue(self.service._is_share_expired_error(0, "分享已失效"))
        self.assertFalse(self.service._is_share_expired_error(990001, "请求过于频繁"))
