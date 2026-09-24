import importlib.util
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# 构造 mock 的基础依赖
mock_app = MagicMock()
mock_logger = MagicMock()
mock_app.log.logger = mock_logger

sys.modules["app"] = mock_app
sys.modules["app.log"] = mock_app.log
sys.modules["app.core"] = MagicMock()
mock_config = MagicMock()
mock_config.settings = MagicMock()
mock_config.settings.TZ = "Asia/Shanghai"
sys.modules["app.core.config"] = mock_config


class OwnerDelegator:
    def __init__(self, owner=None):
        object.__setattr__(self, "_owner", owner)

    def __getattr__(self, name):
        return getattr(self._owner, name) if self._owner else None

    def __setattr__(self, name, value):
        if name == "_owner":
            object.__setattr__(self, name, value)
            return
        if self._owner:
            setattr(self._owner, name, value)


mock_core = MagicMock()
mock_core.__path__ = []
mock_core.OwnerDelegator = OwnerDelegator

sys.modules["cloudsubscribe"] = MagicMock()
sys.modules["cloudsubscribe"].__path__ = []
sys.modules["cloudsubscribe.core"] = mock_core
sys.modules["cloudsubscribe.core.checkin_manager"] = MagicMock()
sys.modules["cloudsubscribe.core.definitions"] = MagicMock()
sys.modules["cloudsubscribe.core.search"] = MagicMock()
sys.modules["cloudsubscribe.core.services"] = MagicMock()
sys.modules["cloudsubscribe.core.services"].__path__ = []
sys.modules["cloudsubscribe.search"] = MagicMock()
sys.modules["cloudsubscribe.search"].__path__ = []
sys.modules["cloudsubscribe.search.hdhaven"] = MagicMock()
sys.modules["cloudsubscribe.search.hdhaven"].__path__ = []
sys.modules["cloudsubscribe.search.hdhaven.security"] = MagicMock()
sys.modules["cloudsubscribe.search.cloudflare"] = MagicMock()
sys.modules["cloudsubscribe.search.http_client"] = MagicMock()

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")

# 加载 hdhaven client
hdhaven_client_path = os.path.join(plugins_v2_path, "cloudsubscribe/search/hdhaven/client.py")
spec_h = importlib.util.spec_from_file_location("cloudsubscribe.search.hdhaven.client", hdhaven_client_path)
mod_h = importlib.util.module_from_spec(spec_h)
mod_h.__package__ = "cloudsubscribe.search.hdhaven"
sys.modules["cloudsubscribe.search.hdhaven.client"] = mod_h
spec_h.loader.exec_module(mod_h)
HDHavenClient = mod_h.HDHavenClient
HDHavenError = mod_h.HDHavenError

# 加载 checkin service
checkin_service_path = os.path.join(plugins_v2_path, "cloudsubscribe/core/services/checkin.py")
spec_c = importlib.util.spec_from_file_location("cloudsubscribe.core.services.checkin", checkin_service_path)
mod_c = importlib.util.module_from_spec(spec_c)
mod_c.__package__ = "cloudsubscribe.core.services"
sys.modules["cloudsubscribe.core.services.checkin"] = mod_c
mod_c.settings = MagicMock()
mod_c.settings.TZ = "Asia/Shanghai"
spec_c.loader.exec_module(mod_c)
CheckinService = mod_c.CheckinService


class TestHDHavenClientCheckin(unittest.TestCase):
    """测试 HDHaven 客户端签到天数返回值是否彻底去除硬编码 1。"""

    def setUp(self):
        self.client = HDHavenClient(
            username="testuser",
            password="testpassword",
            base_url="https://hdhaven.org",
        )

    def test_hdhaven_checkin_success_no_hardcoded_days(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "success": True,
            "data": {
                "earned": 10,
                "points": 100,
                "outcome": "抽中大奖",
            },
        }

        with patch.object(self.client, "request", return_value=mock_resp):
            res = self.client.checkin(mode="normal")
            self.assertTrue(res["success"])
            self.assertEqual(res["signin_points"], 10)
            self.assertIsNone(res["signin_days"], "HDHaven 签到成功未下发天数时 signin_days 应为 None 而非写死 1")

    def test_hdhaven_checkin_409_already_checked_in(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 409

        with patch.object(self.client, "request", return_value=mock_resp):
            res = self.client.checkin(mode="normal")
            self.assertTrue(res["success"])
            self.assertTrue(res["already_checked_in"])
            self.assertIsNone(res["signin_days"], "HDHaven 409 已签到时 signin_days 应为 None")

    def test_hdhaven_checkin_error_with_already_message(self):
        with patch.object(
                self.client,
                "request",
                side_effect=HDHavenError("今日已签到", status_code=400),
        ):
            res = self.client.checkin(mode="normal")
            self.assertTrue(res["success"])
            self.assertTrue(res["already_checked_in"])
            self.assertIsNone(res["signin_days"], "HDHaven 异常包含今日已签到时 signin_days 应为 None")


class TestCheckinDaysCalculation(unittest.TestCase):
    """测试 CheckinService._calculate_signin_days 的统计与融合算法。"""

    def test_empty_history_and_no_remote(self):
        res = CheckinService._calculate_signin_days(history=[], raw_signin_days=None)
        self.assertIsNone(res)

    def test_local_distinct_dates_counting(self):
        # 6 天成功打卡历史，其中一天有 2 次记录（重试或多次打卡）
        history = [
            {"executed_at": "2026-09-19 08:00:00", "success": True},
            {"executed_at": "2026-09-20 08:00:00", "success": True},
            {"executed_at": "2026-09-20 12:00:00", "success": True},  # 同一天多次打卡
            {"executed_at": "2026-09-21 08:00:00", "success": True},
            {"executed_at": "2026-09-22 08:00:00", "success": True},
            {"executed_at": "2026-09-23 08:00:00", "success": True},
            {"executed_at": "2026-09-24 08:00:00", "success": True},
            {"executed_at": "2026-09-18 08:00:00", "success": False},  # 失败记录不计入
        ]
        # 本地成功日期共 6 天：9/19, 9/20, 9/21, 9/22, 9/23, 9/24
        res = CheckinService._calculate_signin_days(history=history, raw_signin_days=None)
        self.assertEqual(res, 6)

    def test_p115_leaf_reset_use_local_days(self):
        # 115 枫叶周期重置为 2 天，但本地历史连续签到了 7 天
        history = [
            {"executed_at": f"2026-09-{day:02d} 09:00:00", "success": True, "signin_days": day}
            for day in range(18, 25)
        ]
        # 此时本地有 7 天打卡成功，而本次 115 官方返回枫叶周期天数 2
        res = CheckinService._calculate_signin_days(
            history=history,
            raw_signin_days=2,
            provider_key="p115",
        )
        self.assertEqual(res, 7, "115 网盘在本地签到 7 天而远端枫叶返回 2 天时，应展示 7 天")

    def test_remote_larger_days_preserved(self):
        # 聚影 / Dian115 远端累计天数大于本地打卡天数
        history = [
            {"executed_at": f"2026-09-{day:02d} 09:00:00", "success": True}
            for day in range(18, 25)
        ]
        # 本地 7 天，聚影返回 56 天
        res = CheckinService._calculate_signin_days(
            history=history,
            raw_signin_days=56,
            provider_key="juying",
        )
        self.assertEqual(res, 56, "远端累计天数 56 大于本地 7 天时，应保留 56 天")

    def test_hdhaven_legacy_one_day_filtered(self):
        # HDHaven 历史记录中遗留了硬编码的 signin_days: 1
        history = [
            {"executed_at": f"2026-09-{day:02d} 09:00:00", "success": True, "signin_days": 1}
            for day in range(19, 25)
        ]
        # 本地 6 天，上报/历史遗留值为 1
        res = CheckinService._calculate_signin_days(
            history=history,
            raw_signin_days=1,
            provider_key="hdhaven",
        )
        self.assertEqual(res, 6, "HDHaven 历史遗留的硬编码 1 应被过滤，正确采用本地累计的 6 天")

    def test_current_execution_increment(self):
        history = [
            {"executed_at": "2026-09-23 09:00:00", "success": True},
        ]
        # 今天 2026-09-24 成功签到
        res = CheckinService._calculate_signin_days(
            history=history,
            current_date_key="2026-09-24",
            current_success=True,
            raw_signin_days=None,
        )
        self.assertEqual(res, 2, "今日成功签到后累计天数应从 1 递增为 2")


class TestCheckinReturnStructure(unittest.TestCase):
    """测试 get_checkin_history 与 list_checkin_details 的返回结构。"""

    def test_get_checkin_history_contains_signin_days(self):
        owner = MagicMock()
        service = CheckinService(owner)
        history = [
            {"executed_at": f"2026-09-{day:02d} 09:00:00", "success": True}
            for day in range(20, 25)
        ]
        with patch.object(CheckinService, "_load_history", return_value=history), \
                patch.object(CheckinService, "_resolve_provider", return_value=MagicMock(key="hdhaven")):
            res = service.get_checkin_history("hdhaven")
            self.assertIsNotNone(res)
            self.assertEqual(res.get("signin_days"), 5)

    def test_list_checkin_details_contains_signin_days(self):
        owner = MagicMock()
        service = CheckinService(owner)
        provider = MagicMock(key="p115", name="115 网盘", points_label="枫叶")
        history = [
            {"executed_at": f"2026-09-{day:02d} 09:00:00", "success": True}
            for day in range(18, 25)
        ]
        with patch.object(CheckinService, "_enabled_providers", return_value=[provider]), \
                patch.object(CheckinService, "_checkin_histories", return_value={"p115": history}):
            res = service.list_checkin_details()
            self.assertTrue(res["success"])
            channels = res["data"]["channels"]
            self.assertEqual(len(channels), 1)
            self.assertEqual(channels[0]["signin_days"], 7)


if __name__ == "__main__":
    unittest.main()
