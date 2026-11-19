"""已解锁资源仍可读取链接的回归测试。

站点把资源标记为已解锁、但搜索卡片没有内嵌分享链接时，旧实现会因
``need_unlock``/``need_access`` 均为 False 直接跳过，导致每轮都无法转存新集。
"""

import re
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    ensure_package,
    install_app_mocks,
    load_module,
)

install_app_mocks(
    "app.core", "app.core.context", "app.db", "app.utils", "app.utils.string",
    "cloudsubscribe", "cloudsubscribe.core", "cloudsubscribe.drive",
    "cloudsubscribe.search", "cloudsubscribe.utils",
    "cloudsubscribe.handlers", "cloudsubscribe.handlers.sync",
)
core = ensure_package("cloudsubscribe.core")
core.OwnerDelegator = OwnerDelegator
core.CloudDriveCapability = MagicMock()
core.CloudDriveProvider = MagicMock()
core.CloudFile = MagicMock()

ensure_package("cloudsubscribe.drive.common").format_size = (
    lambda size: f"{int(size or 0)} B"
)
ensure_package("cloudsubscribe.utils").MediaFileParser = MagicMock()
ensure_package("cloudsubscribe.utils").parse_magnet_metadata = MagicMock()
ensure_package("cloudsubscribe.utils.share_expiry").is_share_expired = (
    MagicMock(return_value=False)
)
ensure_package("cloudsubscribe.search.types")

load_module(
    "cloudsubscribe.handlers.sync.utils",
    "plugins.v2/cloudsubscribe/handlers/sync/utils.py",
)
_resources = load_module(
    "cloudsubscribe.handlers.sync.resources",
    "plugins.v2/cloudsubscribe/handlers/sync/resources.py",
)
ResourceTransferService = _resources.ResourceTransferService


class FakeSearchHandler:
    def __init__(self, unlocked_url):
        self.unlocked_url = unlocked_url
        self.unlock_calls = []
        self.budget_points = []

    def can_unlock(self, source):
        return True

    def has_unlock_budget(self, source, points):
        self.budget_points.append(int(points or 0))
        return int(points or 0) <= 0

    def source_name(self, source):
        return str(source or "").upper()

    def unlock_resource(self, source, resource, search_label=""):
        self.unlock_calls.append(dict(resource))
        return self.unlocked_url


class FakeOwner:
    def __init__(self, unlocked_url="https://115cdn.com/s/unlocked"):
        self._search_handler = FakeSearchHandler(unlocked_url)
        self.expanded = []
        self._OFFLINE_RESOURCE_URL_RE = re.compile(r"$^")

    def _stop_requested(self):
        return False

    def _is_resource_ref_blacklisted(self, resource_ref):
        return False

    def _drop_expired_resource_link(self, share_url, resource_ref, log_prefix=""):
        return False

    def _is_ed2k_url(self, url):
        return False

    def _is_expired_share_link(self, share_url):
        return False


def _resource(**overrides):
    resource = {
        "url": "",
        "title": "遮天 更新到176集",
        "source": "hdhive",
        "resource_ref": "abc123",
        "is_unlocked": True,
        "unlock_points": 50,
        "target_season": 1,
        "target_episodes": [175, 176],
    }
    resource.update(overrides)
    return resource


class UnlockedResourceLinkTest(unittest.TestCase):
    def _resolve(self, owner, resource):
        service = ResourceTransferService(owner)
        return service._resolve_candidate_resource_url(
            [resource], 0, resource, "label", log_prefix="[TEST]"
        )

    def test_unlocked_resource_without_url_reads_link_without_points(self):
        owner = FakeOwner()
        self.assertEqual(
            self._resolve(owner, _resource()),
            "https://115cdn.com/s/unlocked",
        )
        self.assertEqual(len(owner._search_handler.unlock_calls), 1)
        payload = owner._search_handler.unlock_calls[0]
        self.assertEqual(payload.get("unlock_points"), 0)
        self.assertTrue(payload.get("need_access"))
        self.assertEqual(owner._search_handler.budget_points, [0])

    def test_unlocked_resource_without_ref_is_skipped(self):
        owner = FakeOwner()
        self.assertEqual(self._resolve(owner, _resource(resource_ref="")), "")
        self.assertEqual(owner._search_handler.unlock_calls, [])

    def test_locked_resource_without_flags_is_not_unlocked(self):
        owner = FakeOwner()
        resource = _resource(is_unlocked=False)
        self.assertEqual(self._resolve(owner, resource), "")
        self.assertEqual(owner._search_handler.unlock_calls, [])


if __name__ == "__main__":
    unittest.main()