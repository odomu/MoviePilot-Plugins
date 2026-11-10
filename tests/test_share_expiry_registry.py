"""通用分享失效登记：跨网盘的过期/失效识别与 24 小时拉黑。"""

import unittest
from unittest.mock import patch

from plugin_env import install_app_mocks, load_module, register_mock, register_package

install_app_mocks(
    "app.core",
    "app.core.cache",
    "app.log",
)
register_package("cloudsubscribe.utils", paths=[])


class FakeTTLCache:
    def __init__(self, *args, **kwargs):
        self._data = {}

    def __contains__(self, key):
        return key in self._data

    def __setitem__(self, key, value):
        self._data[key] = value

    def get(self, key, default=None, **kwargs):
        return self._data.get(key, default)

    def clear(self):
        self._data.clear()


register_mock(
    "cloudsubscribe.utils.cache",
    create_platform_ttl_cache=lambda *args, **kwargs: FakeTTLCache(),
)
share_expiry = load_module(
    "cloudsubscribe.utils.share_expiry",
    "plugins.v2/cloudsubscribe/utils/share_expiry.py",
)


class TestShareExpiryRegistry(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(
            share_expiry,
            "create_platform_ttl_cache",
            lambda *args, **kwargs: FakeTTLCache(),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        share_expiry._registry = None
        share_expiry.clear_expired_shares()

    def tearDown(self):
        share_expiry._registry = None

    def test_normalize_strips_query_and_aliases_hosts(self):
        self.assertEqual(
            share_expiry.normalize_share_url(
                "https://115.com/s/swxabc?password=pwd#"
            ),
            "https://115.com/s/swxabc",
        )
        self.assertEqual(
            share_expiry.normalize_share_url("https://115cdn.com/s/swxabc/"),
            "https://115.com/s/swxabc",
        )

    def test_mark_and_query_expiry(self):
        url = "https://pan.quark.cn/s/quark-id?pwd=1234"
        print("REGISTRY", type(share_expiry._registry), getattr(share_expiry._registry, "_data", None))
        self.assertFalse(share_expiry.is_share_expired(url))
        share_expiry.mark_share_expired(url, "分享已过期")
        print("REGISTRY2", getattr(share_expiry._registry, "_data", None))
        self.assertTrue(share_expiry.is_share_expired(url))
        # 同一分享换查询参数/末尾斜杠仍命中
        self.assertTrue(
            share_expiry.is_share_expired(
                "https://pan.quark.cn/s/quark-id/"
            )
        )

    def test_error_classification_is_generic(self):
        cases = [
            (4100018, ""),
            (None, "链接已过期"),
            (None, "分享已失效"),
            (None, "该分享不存在或已取消"),
            (None, "share expired"),
            (None, "分享文件已删除"),
        ]
        for code, message in cases:
            with self.subTest(code=code, message=message):
                self.assertTrue(
                    share_expiry.is_expired_share_error(code, message)
                )

    def test_rate_limit_and_file_errors_are_not_expiry(self):
        self.assertFalse(
            share_expiry.is_expired_share_error(None, "访问过于频繁，请稍后再试")
        )
        self.assertFalse(
            share_expiry.is_expired_share_error(None, "文件不存在")
        )
