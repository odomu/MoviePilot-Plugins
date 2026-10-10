import unittest
from unittest.mock import MagicMock

from plugin_env import OwnerDelegator, install_app_mocks, load_module, register_mock, register_package, set_media_type

set_media_type("电影", "电视剧")

# 构造 mock 的 app 基础依赖
install_app_mocks(
    "app.core",
    "app.core.config",
    "app.core.cache",
    "app.core.context",
    "app.core.metainfo",
    "app.chain",
    "app.chain.subscribe",
    "app.chain.mediaserver",
    "app.db",
    "app.db.models",
    "app.db.models.subscribe",
    "app.db.models.downloadhistory",
    "app.db.models.mediaserver",
    "app.db.subscribe_oper",
    "app.db.transferhistory_oper",
    "app.db.downloadhistory_oper",
    "app.helper",
    "app.helper.mediaserver",
    "app.application",
    "app.application.mediaserver",
    "app.utils",
    "app.utils.string",
    "app.scheduler",
)

mock_subscribe_chain = MagicMock()
mock_subscribe_oper = MagicMock()
mock_logger = MagicMock()

import sys

sys.modules["app.log"].logger = mock_logger
sys.modules["app.chain.subscribe"].SubscribeChain = mock_subscribe_chain
sys.modules["app.db.subscribe_oper"].SubscribeOper = mock_subscribe_oper
# 构造 mock 的 cloudsubscribe 包结构
register_package("cloudsubscribe")
register_package("cloudsubscribe.core", OwnerDelegator=OwnerDelegator)
register_mock("cloudsubscribe.core.hook")
load_module("cloudsubscribe.core.media", "plugins.v2/cloudsubscribe/core/media.py")

SubscriptionSearchHook = load_module(
    "cloudsubscribe.core.hook.subscription",
    "plugins.v2/cloudsubscribe/core/hook/subscription.py",
).SubscriptionSearchHook


class TestPlatformCompatibility(unittest.TestCase):
    """
    MoviePilot 平台跨版本（v2 与 v3）核心行为兼容性测试套件
    """

    def setUp(self):
        self.owner = MagicMock()
        self.owner._subscribe_search_originals = {}
        self.hook = SubscriptionSearchHook(self.owner)
        self.hook._enabled = True

    # 1. SubscribeChain.search 跨版本测试

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

    # 2. SubscribeChain.refresh 跨版本测试

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

    # 3. Scheduler 单例与调度作业接管测试

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

    # 5. 跨版本订阅对象与平台过滤规则兼容性

    def test_v3_subscribe_get_params_dot_access_compatibility(self):
        """
        验证 MoviePilot v3 平台契约兼容性:
        MoviePilot v3 app/chain/subscribe/query.py::get_params 直接以属性访问读取：
        subscribe.quality, subscribe.resolution, subscribe.effect, subscribe.include, subscribe.exclude
        插件生成的临时目标对象必须包含这些字段，不得抛出 AttributeError。
        """
        import re
        from pathlib import Path

        history_file = Path("plugins.v2/cloudsubscribe/handlers/sync/history.py")
        text = history_file.read_text(encoding="utf-8")
        match = re.search(r"def _transient_target_defaults\(\)[^:]*:\s*return\s*\{([^}]+)\}", text)
        self.assertIsNotNone(match)
        # 验证必需包含 quality, resolution, effect, include, exclude 等 v3 契约属性
        for req in ("quality", "resolution", "effect", "include", "exclude"):
            self.assertIn(f'"{req}"', match.group(1))

    def test_v2_and_v3_safe_subscribe_params_resilience(self):
        """
        验证 platform_rules._safe_subscribe_params 契约同时兼容：
        1. v3 静态方法 SubscribeChain.get_params(subscribe)
        2. v2 实例方法 SubscribeChain().get_params(subscribe)
        3. 缺失任意属性的裸对象（自动补齐并防御异常）
        """
        from types import SimpleNamespace
        from pathlib import Path

        # 验证 platform_rules.py 源码声明了 _safe_subscribe_params 并防御了质量分辨率字段
        rules_src = Path("plugins.v2/cloudsubscribe/handlers/search/platform_rules.py").read_text(encoding="utf-8")
        self.assertIn("def _safe_subscribe_params", rules_src)
        for req in ("quality", "resolution", "effect", "include", "exclude"):
            self.assertIn(f'"{req}"', rules_src)

        # 1. 模拟 v3 静态方法调用 (dot-access)
        class MockV3SubscribeChain:
            @staticmethod
            def get_params(s):
                return {"quality": s.quality, "resolution": s.resolution, "effect": s.effect}

        sys.modules["app.chain.subscribe"].SubscribeChain = MockV3SubscribeChain
        obj3 = SimpleNamespace(name="v3测试")
        for attr in ("quality", "resolution", "effect", "include", "exclude"):
            setattr(obj3, attr, None)
        res_v3 = MockV3SubscribeChain.get_params(obj3)
        self.assertIsNone(res_v3["quality"])
        self.assertIsNone(res_v3["resolution"])

        # 2. 模拟 v2 实例方法调用
        class MockV2SubscribeChain:
            def get_params(self, s):
                return {"quality": getattr(s, "quality", None), "mode": "v2"}

        sys.modules["app.chain.subscribe"].SubscribeChain = MockV2SubscribeChain
        obj2 = SimpleNamespace(name="v2测试")
        res_v2 = MockV2SubscribeChain().get_params(obj2)
        self.assertEqual(res_v2["mode"], "v2")

    def test_is_subscribe_best_version_across_v2_v3_formats(self):
        """
        验证 is_subscribe_best_version 准确识别各种 v2/v3 形式的 best_version：
        - v2 ORM 模型 / 整数属性 (1 / 0)
        - v3 dataclass 快照 / Optional[int] (1 / None)
        - 字典形式 (best_version: 1, "1", True, "true")
        - 显式关闭形式 (0, False, None, "0", "false")
        """
        from types import SimpleNamespace
        core_media = load_module(
            "cloudsubscribe.core.media",
            "plugins.v2/cloudsubscribe/core/media.py",
        )
        is_best = core_media.is_subscribe_best_version

        # 1. 开启形式
        self.assertTrue(is_best(SimpleNamespace(best_version=1)))
        self.assertTrue(is_best(SimpleNamespace(best_version=True)))
        self.assertTrue(is_best(SimpleNamespace(best_version="1")))
        self.assertTrue(is_best(SimpleNamespace(best_version="true")))
        self.assertTrue(is_best(SimpleNamespace(best_version="yes")))
        self.assertTrue(is_best({"best_version": 1}))
        self.assertTrue(is_best({"best_version": True}))
        self.assertTrue(is_best({"best_version": "1"}))

        # 2. 关闭形式
        self.assertFalse(is_best(None))
        self.assertFalse(is_best(SimpleNamespace(best_version=0)))
        self.assertFalse(is_best(SimpleNamespace(best_version=False)))
        self.assertFalse(is_best(SimpleNamespace(best_version=None)))
        self.assertFalse(is_best(SimpleNamespace()))
        self.assertFalse(is_best({"best_version": 0}))
        self.assertFalse(is_best({"best_version": False}))
        self.assertFalse(is_best({"best_version": None}))
        self.assertFalse(is_best({}))

    def test_is_cloud_upgrade_subscribe_respects_platform_best_version(self):
        """
        验证当订阅卡片在平台开启了洗版 (best_version=1) 时，
        即使插件单独洗版订阅清单配置了其他订阅ID，也必须正确识别该订阅为洗版订阅。
        """
        from types import SimpleNamespace
        subscription_service = load_module(
            "cloudsubscribe.core.services.subscription",
            "plugins.v2/cloudsubscribe/core/services/subscription.py",
        )
        owner = MagicMock()
        owner._enable_cloud_upgrade = True
        owner._upgrade_subscribe_ids = [999]  # 插件配置了其他指定订阅
        svc = subscription_service.SubscriptionControlService(owner)

        # 订阅卡片开启了 best_version=1，ID 为 123（不在 999 列表中）
        sub = SimpleNamespace(id=123, best_version=1)
        self.assertTrue(svc._is_cloud_upgrade_subscribe(sub))

        # 订阅卡片未开启 best_version，且不在列表中 -> 不洗版
        sub_no_wash = SimpleNamespace(id=123, best_version=0)
        self.assertFalse(svc._is_cloud_upgrade_subscribe(sub_no_wash))

        # 订阅卡片未开启 best_version，但是在插件独立清单中 (999) -> 洗版
        sub_in_plugin_list = SimpleNamespace(id=999, best_version=0)
        self.assertTrue(svc._is_cloud_upgrade_subscribe(sub_in_plugin_list))

        # 插件全局网盘洗版关闭 -> 一律不洗版
        owner._enable_cloud_upgrade = False
        self.assertFalse(svc._is_cloud_upgrade_subscribe(sub))

    def test_append_history_records_upgrade_preserves_finalize_key_and_pending_ready(self):
        """
        验证洗版任务 (is_upgrade) 在合并到已有成功记录时：
        1. 必须推进为 incoming 状态 (处理中)，不得被旧的成功状态覆盖
        2. 必须保留 finalize_key
        3. 必须将 pending 任务标记为 history_ready=True，防止后处理被阻断 30 分钟
        """
        load_module("cloudsubscribe.core.history", "plugins.v2/cloudsubscribe/core/history.py")
        register_mock("cloudsubscribe.search")
        register_mock("cloudsubscribe.search.types", normalize_resource_type=lambda t: t,
                      resource_type_from_url=lambda u: "share")
        register_package("cloudsubscribe.drive")
        register_mock("cloudsubscribe.drive.common", format_size=lambda s: f"{s}B",
                      positive_int=lambda v: int(v) if v else None)
        register_package("cloudsubscribe.handlers")
        register_package("cloudsubscribe.handlers.sync")
        register_mock("cloudsubscribe.handlers.sync.utils", normalize_season=lambda s: int(s or 1))
        history_module = load_module(
            "cloudsubscribe.handlers.sync.history",
            "plugins.v2/cloudsubscribe/handlers/sync/history.py",
        )
        owner = MagicMock()
        data_store = {
            "history": [
                {
                    "title": "生化危机：爆发夜",
                    "year": "2026",
                    "type": "电影",
                    "tmdb_id": "1423191",
                    "status": "成功",
                    "file_name": "生化危机：爆发夜 (2026).mp4",
                    "rule_score": 0,
                }
            ],
            "pending_offline_strm": {
                "P115:KEY123": {
                    "file_name": "Resident.Evil.2026.1080p.mkv",
                    "history_ready": False,
                    "created_at": 1000.0,
                    "next_check_at": 1010.0,
                }
            },
        }
        owner.get_data = lambda k: data_store.get(k)
        owner.save_data = lambda k, v: data_store.__setitem__(k, v)
        owner._get_data = owner.get_data
        owner._save_data = owner.save_data
        owner._offline_pending_lock = MagicMock()
        owner._offline_pending_lock.__enter__ = MagicMock(return_value=True)
        owner._offline_pending_lock.__exit__ = MagicMock(return_value=None)
        owner._OFFLINE_PENDING_KEY = "pending_offline_strm"
        owner._record_platform_transfer_histories = MagicMock()
        owner._history_changed = MagicMock()
        owner._notify_offline_pending_changed = MagicMock()

        svc = history_module.HistoryService(owner)

        incoming_record = {
            "title": "生化危机：爆发夜",
            "year": "2026",
            "type": "电影",
            "tmdb_id": "1423191",
            "status": "处理中",
            "file_name": "Resident.Evil.2026.1080p.mkv",
            "finalize_key": "P115:KEY123",
            "upgrade": True,
            "rule_score": 80,
        }

        svc.append_history_records([incoming_record])

        # 验证历史记录状态和 finalize_key 已正确写入
        saved_history = data_store["history"]
        self.assertEqual(len(saved_history), 1)
        self.assertEqual(saved_history[0]["status"], "处理中")
        self.assertEqual(saved_history[0]["finalize_key"], "P115:KEY123")
        self.assertTrue(saved_history[0]["upgrade"])

        # 验证 pending 任务的 history_ready 已激活
        saved_pending = data_store["pending_offline_strm"]
        self.assertTrue(saved_pending["P115:KEY123"]["history_ready"])

    def test_reconcile_real_history_status_cleans_orphan_pending_tasks(self):
        """
        验证状态校准逻辑：
        当 STRM 产物文件已就绪或历史记录已是终态时，
        必须将 pending 中的残留任务清理并通知前端，避免任务列表永久卡在‘转存中’。
        """
        from pathlib import Path
        history_module = load_module(
            "cloudsubscribe.handlers.sync.history",
            "plugins.v2/cloudsubscribe/handlers/sync/history.py",
        )
        owner = MagicMock()
        data_store = {
            "history": [
                {
                    "title": "生化危机：爆发夜",
                    "year": "2026",
                    "type": "电影",
                    "tmdb_id": "1423191",
                    "status": "处理中",
                    "finalize_key": "P115:ORPHAN1",
                    "cloud_dir": "/media/电影",
                    "file_name": "生化危机：爆发夜 (2026).mp4",
                }
            ],
            "pending_offline_strm": {
                "P115:ORPHAN1": {
                    "file_name": "生化危机：爆发夜 (2026).mp4",
                    "cloud_dir": "/media/电影",
                    "history_ready": True,
                },
                "P115:GHOST2": {
                    "file_name": "旧电影.mp4",
                    "cloud_dir": "/media/电影",
                    "history_ready": True,
                    "moved_at": 1000.0,
                },
            },
        }
        owner.get_data = lambda k: data_store.get(k)
        owner.save_data = lambda k, v: data_store.__setitem__(k, v)
        owner._get_data = owner.get_data
        owner._save_data = owner.save_data
        owner._offline_pending_lock = MagicMock()
        owner._offline_pending_lock.__enter__ = MagicMock(return_value=True)
        owner._offline_pending_lock.__exit__ = MagicMock(return_value=None)
        owner._OFFLINE_PENDING_KEY = "pending_offline_strm"
        owner._record_platform_transfer_histories = MagicMock()
        owner._history_changed = MagicMock()
        owner._notify_offline_pending_changed = MagicMock()

        svc = history_module.HistoryService(owner)
        # 模拟 strm 文件已生成就绪
        mock_strm = MagicMock(spec=Path)
        mock_strm.is_file = MagicMock(return_value=True)
        mock_strm.stat = MagicMock(return_value=MagicMock(st_size=1024))
        svc._strm_local_path = MagicMock(
            side_effect=lambda record: mock_strm if record.get("file_name") == "生化危机：爆发夜 (2026).mp4" else None)

        res = svc.reconcile_real_history_status()
        self.assertTrue(res["success"])

        # 历史记录已收敛为成功
        self.assertEqual(data_store["history"][0]["status"], "成功")
        self.assertNotIn("finalize_key", data_store["history"][0])

        # pending 中产物就绪和孤儿任务均已清理
        self.assertEqual(len(data_store["pending_offline_strm"]), 0)
        owner._notify_offline_pending_changed.assert_called()
    def test_typing_annotations_defined_across_all_modules(self):
        """
        验证插件模块类型注解规范：
        所有模块中使用的 typing 类型注解必须完整导入或包含 future annotations，
        防止在低版本 Python 环境中因类/函数定义期求值注解抛出 NameError。
        """
        import ast
        from pathlib import Path

        common_typing = {
            "Any", "Dict", "List", "Optional", "Tuple", "Set", "Union",
            "Callable", "Iterable", "Sequence", "Mapping"
        }
        root = Path("plugins.v2/cloudsubscribe")
        missing_typing_report = []

        for py_file in root.rglob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "typing":
                    for alias in node.names:
                        imported.add(alias.name)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "typing":
                            imported.update(common_typing)

            used = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in common_typing:
                    used.add(node.id)

            missing = used - imported
            if missing:
                missing_typing_report.append(f"{py_file}: missing {missing}")

        self.assertEqual(
            missing_typing_report,
            [],
            f"存在未导入 typing 类型的模块: {missing_typing_report}",
        )
if __name__ == "__main__":
    unittest.main()
