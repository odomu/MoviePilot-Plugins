import os
import sys
import unittest
from unittest.mock import MagicMock

# 构造 mock 的 app 基础依赖
mock_app = MagicMock()
mock_subscribe_chain = MagicMock()
mock_subscribe_oper = MagicMock()
mock_logger = MagicMock()

mock_app.chain.subscribe.SubscribeChain = mock_subscribe_chain
mock_app.db.subscribe_oper.SubscribeOper = mock_subscribe_oper
mock_app.log.logger = mock_logger

sys.modules["app"] = mock_app
sys.modules["app.chain"] = mock_app.chain
sys.modules["app.chain.subscribe"] = mock_app.chain.subscribe
sys.modules["app.db"] = mock_app.db
sys.modules["app.db.subscribe_oper"] = mock_app.db.subscribe_oper
sys.modules["app.log"] = mock_app.log
sys.modules["app.scheduler"] = mock_app.scheduler


# 定义与 delegation.py 一致的 OwnerDelegator
class OwnerDelegator:
    def __init__(self, owner):
        object.__setattr__(self, "_owner", owner)

    def __getattr__(self, name):
        return getattr(self._owner, name)

    def __setattr__(self, name, value):
        if name == "_owner":
            object.__setattr__(self, name, value)
            return
        setattr(self._owner, name, value)


mock_core = MagicMock()
mock_core.OwnerDelegator = OwnerDelegator

sys.modules["cloudsubscribe"] = MagicMock()
sys.modules["cloudsubscribe.core"] = mock_core
sys.modules["cloudsubscribe.core.hook"] = MagicMock()

import importlib.util

hook_file_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../plugins.v2/cloudsubscribe/core/hook/subscription.py"))
spec = importlib.util.spec_from_file_location("cloudsubscribe.core.hook.subscription", hook_file_path)
subscription_mod = importlib.util.module_from_spec(spec)
subscription_mod.__package__ = "cloudsubscribe.core.hook"
sys.modules["cloudsubscribe.core.hook.subscription"] = subscription_mod
spec.loader.exec_module(subscription_mod)

SubscriptionSearchHook = subscription_mod.SubscriptionSearchHook


class TestPlatformCompatibility(unittest.TestCase):
    """
    MoviePilot 平台跨版本（v2 与 v3）核心行为兼容性测试套件
    """

    def setUp(self):
        self.owner = MagicMock()
        self.owner._subscribe_search_originals = {}
        self.hook = SubscriptionSearchHook(self.owner)
        self.hook._enabled = True

    # ------------------ 1. SubscribeChain.search 跨版本测试 ------------------

    def test_v2_search_signature_and_execution(self):
        """
        验证 MoviePilot v2 平台：
        - 签名: search(self, sid=None, state='N', manual=False, progress_callback=None) -> None
        - 不支持 sids 和 scheduled_interval
        - 返回值为 None
        """
        self.hook._is_takeover_active = MagicMock(return_value=False)
        self.hook._is_subscribe_excluded = MagicMock(return_value=False)

        recorded_calls = []

        class MockV2SubscribeChain:
            def search(self, sid=None, state='N', manual=False, progress_callback=None):
                recorded_calls.append({
                    "sid": sid,
                    "state": state,
                    "manual": manual,
                    "progress_callback": progress_callback,
                })
                return None  # v2 返回 None

        mock_subscribe_chain.return_value = MockV2SubscribeChain()

        # 模拟调度器带 v3 属性的调用调用到插件，插件安全降级传给 v2 SubscribeChain
        res = self.hook._dispatch_subscribe_search(
            state="R",
            scheduled_interval=24,
            extra_scheduler_kw="test",
        )
        self.assertIsNone(res)
        self.assertEqual(len(recorded_calls), 1)
        self.assertEqual(recorded_calls[0]["sid"], None)
        self.assertEqual(recorded_calls[0]["state"], "R")

    def test_v2_search_with_sids_batch_split(self):
        """
        验证 MoviePilot v2 平台：
        - 未接管时传入批量 sids，由于 v2 原生不支持批量，插件自动将其拆分为逐个 sid 调用
        """
        self.hook._is_takeover_active = MagicMock(return_value=False)
        self.hook._is_subscribe_excluded = MagicMock(return_value=False)

        called_sids = []

        class MockV2SubscribeChain:
            def search(self, sid=None, state='N', manual=False, progress_callback=None):
                called_sids.append(sid)
                return None

        mock_subscribe_chain.return_value = MockV2SubscribeChain()

        res = self.hook._dispatch_subscribe_search(sids=(201, 202, 203), state="R", manual=True)
        self.assertEqual(called_sids, [201, 202, 203])
        self.assertIsNone(res)

    def test_v3_search_signature_and_execution(self):
        """
        验证 MoviePilot v3 平台：
        - 签名: search(self, sid=None, state='N', manual=False, progress_callback=None, sids=None, scheduled_interval=None) -> Optional[str]
        - 完整支持 sids 与 scheduled_interval
        - 返回值为队列任务批次 batch_id
        """
        self.hook._is_takeover_active = MagicMock(return_value=False)
        self.hook._is_subscribe_excluded = MagicMock(return_value=False)

        recorded_kwargs = {}

        class MockV3SubscribeChain:
            def search(self, sid=None, state="N", manual=False, progress_callback=None, sids=None,
                       scheduled_interval=None):
                recorded_kwargs.update({
                    "sid": sid,
                    "state": state,
                    "manual": manual,
                    "progress_callback": progress_callback,
                    "sids": sids,
                    "scheduled_interval": scheduled_interval,
                })
                return "batch-uuid-12345"  # v3 返回任务批次 ID

        mock_subscribe_chain.return_value = MockV3SubscribeChain()

        res = self.hook._dispatch_subscribe_search(
            state="R",
            sids=(301, 302),
            scheduled_interval=12,
        )
        self.assertEqual(res, "batch-uuid-12345")
        self.assertEqual(recorded_kwargs["state"], "R")
        self.assertEqual(recorded_kwargs["sids"], (301, 302))
        self.assertEqual(recorded_kwargs["scheduled_interval"], 12)

    # ------------------ 2. SubscribeChain.refresh 跨版本测试 ------------------

    def test_v2_refresh_signature_compatibility(self):
        """
        验证 MoviePilot v2 平台：
        - 签名: refresh(self, progress_callback=None) -> None
        - 不支持 mtype 关键字参数
        """
        self.hook._is_takeover_active = MagicMock(return_value=False)

        called = []

        class MockV2RefreshChain:
            def refresh(self, progress_callback=None):
                called.append(progress_callback)
                return None

        mock_subscribe_chain.return_value = MockV2RefreshChain()

        # 传入带有 mtype 及额外 kwargs 的调用
        res = self.hook._dispatch_subscribe_refresh(mtype="movie", future_arg=True)
        self.assertIsNone(res)
        self.assertEqual(len(called), 1)

    def test_v3_refresh_signature_compatibility(self):
        """
        验证 MoviePilot v3 平台：
        - 签名: refresh(self, progress_callback=None, *, mtype=None) -> None
        - 原生支持 mtype
        """
        self.hook._is_takeover_active = MagicMock(return_value=False)

        received_mtype = []

        class MockV3RefreshChain:
            def refresh(self, progress_callback=None, *, mtype=None):
                received_mtype.append(mtype)
                return None

        mock_subscribe_chain.return_value = MockV3RefreshChain()

        self.hook._dispatch_subscribe_refresh(mtype="tv")
        self.assertEqual(received_mtype, ["tv"])

    # ------------------ 3. Scheduler 单例与调度作业接管测试 ------------------

    def test_v2_scheduler_singleton_takeover(self):
        """
        验证 MoviePilot v2 平台：
        - Scheduler 类为单例（通过 Scheduler() 实例化），无 get_existing_instance 方法
        """

        class MockV2Scheduler:
            _instance = None

            def __new__(cls):
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._jobs = {
                        "subscribe_search": {"func": lambda: None},
                        "new_subscribe_search": {"func": lambda: None},
                        "subscribe_refresh": {"func": lambda: None},
                    }
                return cls._instance

        sys.modules["app.scheduler"].Scheduler = MockV2Scheduler

        self.hook._install_platform_search_block = MagicMock()
        self.hook._install_subscribe_chain_takeover = MagicMock()

        self.hook._install_subscribe_search_takeover()
        sched = MockV2Scheduler()
        self.assertEqual(sched._jobs["subscribe_search"]["func"], self.hook._dispatch_subscribe_search)
        self.assertEqual(sched._jobs["new_subscribe_search"]["func"], self.hook._dispatch_subscribe_search)
        self.assertEqual(sched._jobs["subscribe_refresh"]["func"], self.hook._dispatch_subscribe_refresh)

    def test_v3_scheduler_get_existing_instance_takeover(self):
        """
        验证 MoviePilot v3 平台：
        - Scheduler 提供了 get_existing_instance() 类方法
        """

        class MockV3Scheduler:
            _jobs = {
                "subscribe_search": {"func": lambda: None},
                "new_subscribe_search": {"func": lambda: None},
                "subscribe_refresh": {"func": lambda: None},
            }

            @classmethod
            def get_existing_instance(cls):
                return cls

        sys.modules["app.scheduler"].Scheduler = MockV3Scheduler

        self.hook._install_platform_search_block = MagicMock()
        self.hook._install_subscribe_chain_takeover = MagicMock()

        self.hook._install_subscribe_search_takeover()
        self.assertEqual(MockV3Scheduler._jobs["subscribe_search"]["func"], self.hook._dispatch_subscribe_search)


if __name__ == "__main__":
    unittest.main()
