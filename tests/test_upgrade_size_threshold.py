"""同评分洗版文件差异阈值回归测试。"""

import unittest
from types import SimpleNamespace

from plugin_env import (
    OwnerDelegator,
    ensure_package,
    install_app_mocks,
    load_module,
    set_media_type,
)

install_app_mocks(
    "app.log", "app.schemas", "app.schemas.types", "app.utils",
    "app.utils.string", "cloudsubscribe", "cloudsubscribe.core",
    "cloudsubscribe.core.media", "cloudsubscribe.drive",
)
set_media_type(movie="电影", tv="电视剧")
ensure_package("cloudsubscribe.core").OwnerDelegator = OwnerDelegator
ensure_package("cloudsubscribe.core.media")
ensure_package("cloudsubscribe.drive.common").format_size = (
    lambda size: f"{int(size or 0)} B"
)

_rule_scoring = load_module(
    "cloudsubscribe.handlers.sync.rule_scoring",
    "plugins.v2/cloudsubscribe/handlers/sync/rule_scoring.py",
)
RuleScoringService = _rule_scoring.UpgradeRuleScoringService


class UpgradeSizeThresholdTest(unittest.TestCase):
    """同评分洗版需达到文件差异阈值，避免小幅差异频繁替换。"""

    GIB = 1024 ** 3

    def _service(self, mode="largest", threshold=GIB):
        return RuleScoringService(SimpleNamespace(
            _upgrade_mode=mode,
            _upgrade_size_threshold=threshold,
        ))

    def test_largest_rejects_small_difference(self):
        should, reason = self._service()._should_upgrade_candidate(
            10, 10, 4 * self.GIB, 4 * self.GIB + 512 * 1024 ** 2
        )
        self.assertFalse(should)
        self.assertIn("未达到阈值", reason)

    def test_largest_accepts_difference_at_threshold(self):
        should, reason = self._service()._should_upgrade_candidate(
            10, 10, 4 * self.GIB, 5 * self.GIB
        )
        self.assertTrue(should)
        self.assertIn("差异", reason)

    def test_smallest_rejects_small_difference(self):
        should, reason = self._service("smallest")._should_upgrade_candidate(
            10, 10, 4 * self.GIB, 4 * self.GIB - 512 * 1024 ** 2
        )
        self.assertFalse(should)
        self.assertIn("未达到阈值", reason)

    def test_zero_threshold_keeps_any_difference(self):
        should, _ = self._service(threshold=0)._should_upgrade_candidate(
            10, 10, 4 * self.GIB, 4 * self.GIB + 1
        )
        self.assertTrue(should)


if __name__ == "__main__":
    unittest.main()