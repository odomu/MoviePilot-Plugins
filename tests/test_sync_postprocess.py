"""后处理任务完整测试套件（覆盖 DirectoryFileIndex、快照同步、文件定位、重命名与移动状态机、重试退避及关闭自动整理契约）。"""

import importlib.util
import os
import sys
import types
import unittest
from unittest.mock import MagicMock

# 构造 mock 的基础依赖
sys.modules.setdefault("app", MagicMock())
sys.modules.setdefault("app.db", MagicMock())
sys.modules.setdefault("app.db.models", MagicMock())
sys.modules.setdefault("app.db.models.subscribe", MagicMock())
sys.modules.setdefault("app.db.subscribe_oper", MagicMock())
sys.modules.setdefault("app.log", MagicMock())
sys.modules.setdefault("app.schemas", MagicMock())
sys.modules.setdefault("app.schemas.types", MagicMock())

# 构造 cloudsubscribe 包结构
pkg = types.ModuleType("cloudsubscribe")
pkg.__path__ = []
sys.modules["cloudsubscribe"] = pkg

mock_core = types.ModuleType("cloudsubscribe.core")
mock_core.__path__ = []


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


mock_core.OwnerDelegator = OwnerDelegator
mock_core.CloudDriveCapability = MagicMock()
sys.modules["cloudsubscribe.core"] = mock_core

mock_search = types.ModuleType("cloudsubscribe.search")
mock_search.__path__ = []
mock_search.subs_filter = MagicMock()
sys.modules["cloudsubscribe.search"] = mock_search
sys.modules["cloudsubscribe.search.subs_filter"] = mock_search.subs_filter

# 加载 file_parser
parser_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../plugins.v2/cloudsubscribe/utils/file_parser.py"))
spec_p = importlib.util.spec_from_file_location("cloudsubscribe.utils.file_parser", parser_path)
mod_p = importlib.util.module_from_spec(spec_p)
spec_p.loader.exec_module(mod_p)
mock_utils = types.ModuleType("cloudsubscribe.utils")
mock_utils.MediaFileParser = mod_p.MediaFileParser
sys.modules["cloudsubscribe.utils"] = mock_utils
sys.modules["cloudsubscribe.utils.file_parser"] = mod_p

mock_handlers = types.ModuleType("cloudsubscribe.handlers")
mock_handlers.__path__ = []
sys.modules["cloudsubscribe.handlers"] = mock_handlers

mock_sync = types.ModuleType("cloudsubscribe.handlers.sync")
mock_sync.__path__ = []
sys.modules["cloudsubscribe.handlers.sync"] = mock_sync

utils_sync_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../plugins.v2/cloudsubscribe/handlers/sync/utils.py"))
spec_u = importlib.util.spec_from_file_location("cloudsubscribe.handlers.sync.utils", utils_sync_path)
mod_u = importlib.util.module_from_spec(spec_u)
spec_u.loader.exec_module(mod_u)
sys.modules["cloudsubscribe.handlers.sync.utils"] = mod_u

postprocess_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../plugins.v2/cloudsubscribe/handlers/sync/postprocess.py"))
spec_post = importlib.util.spec_from_file_location("cloudsubscribe.handlers.sync.postprocess", postprocess_path)
mod_post = importlib.util.module_from_spec(spec_post)
mod_post.__package__ = "cloudsubscribe.handlers.sync"
sys.modules["cloudsubscribe.handlers.sync.postprocess"] = mod_post
spec_post.loader.exec_module(mod_post)

DirectoryFileIndex = mod_post.DirectoryFileIndex
PostprocessBatchContext = mod_post.PostprocessBatchContext
PostprocessService = mod_post.PostprocessService


class TestDirectoryFileIndex(unittest.TestCase):
    """测试 DirectoryFileIndex 目录索引与多算法哈希/季集反查功能。"""

    def test_basic_name_and_hash_lookup(self):
        file1 = types.SimpleNamespace(name="Blossoms.S01E01.mkv", sha1="A1B2C3D4", md5="MD5HASH1")
        file2 = {"name": "Blossoms.S01E02.mkv", "sha1": "E5F6A7B8", "md5": "MD5HASH2"}
        index = DirectoryFileIndex([file1, file2])

        # 文件名查找
        self.assertIn("Blossoms.S01E01.mkv", index)
        self.assertIn("Blossoms.S01E02.mkv", index)
        self.assertEqual(index["Blossoms.S01E01.mkv"], file1)

        # 哈希大小写不敏感反查
        self.assertEqual(index.get_by_hash("a1b2c3d4"), file1)
        self.assertEqual(index.get_by_sha1("A1B2C3D4"), file1)
        self.assertEqual(index.get_by_hash("MD5HASH2"), file2)

        # 季集提取与反查
        self.assertEqual(index.get_by_season_episode(1, 1), file1)
        self.assertEqual(index.get_by_season_episode(1, 2), file2)
        self.assertIsNone(index.get_by_season_episode(1, 3))

    def test_add_and_remove_file(self):
        index = DirectoryFileIndex()
        file_obj = types.SimpleNamespace(name="Movie.2024.S02E05.mp4", sha1="TESTSHA1")
        index.add_file(file_obj)

        self.assertIn("Movie.2024.S02E05.mp4", index)
        self.assertEqual(index.get_by_sha1("TESTSHA1"), file_obj)
        self.assertEqual(index.get_by_season_episode(2, 5), file_obj)

        # 删除后，名称、哈希与季集索引均应清除
        index.remove_file(file_obj)
        self.assertNotIn("Movie.2024.S02E05.mp4", index)
        self.assertIsNone(index.get_by_sha1("TESTSHA1"))
        self.assertIsNone(index.get_by_season_episode(2, 5))


class TestPostprocessBatchContext(unittest.TestCase):
    """测试 PostprocessBatchContext 目录快照状态机与变更同步。"""

    def setUp(self):
        self.ctx = PostprocessBatchContext(
            pending={},
            pending_snapshot={},
            due_keys=["key1"],
            now=1000.0,
            monitor_token="token_123",
            task_map={},
            tasks_valid=True,
            subscribe_cache={},
        )
        self.file1 = types.SimpleNamespace(name="old_name.mkv", sha1="HASH123")
        self.index_src = DirectoryFileIndex([self.file1])
        self.index_dst = DirectoryFileIndex([])
        self.ctx.directory_snapshots["/src"] = (True, self.index_src)
        self.ctx.directory_snapshots["/dst"] = (True, self.index_dst)

    def test_sync_rename(self):
        new_file = types.SimpleNamespace(name="new_name.mkv", sha1="HASH123")
        self.ctx.sync_rename("/src", self.file1, new_file)

        # 检查快照索引
        self.assertNotIn("old_name.mkv", self.index_src)
        self.assertIn("new_name.mkv", self.index_src)
        self.assertEqual(self.index_src.get_by_sha1("HASH123"), new_file)

    def test_sync_move(self):
        self.ctx.sync_move("/src", "/dst", self.file1)

        # 源目录应移除，目标目录应新增
        self.assertNotIn("old_name.mkv", self.index_src)
        self.assertIn("old_name.mkv", self.index_dst)
        self.assertEqual(self.index_dst.get_by_sha1("HASH123"), self.file1)


class DummyPostprocessService(PostprocessService):
    """用于测试 PostprocessService 各独立逻辑的派生类。"""

    def __init__(self, organize_after_transfer: bool = True):
        owner = types.SimpleNamespace()
        super().__init__(owner)
        self._organize_after_transfer = organize_after_transfer
        self._upgrade_mode = "replace"
        self._FINALIZE_MAX_FAILURES = 3
        self._OFFLINE_CHECK_DELAYS = [60, 120, 300, 600]
        self._OFFLINE_TIMEOUT = 3600
        self._cloud_batch_mutations = MagicMock()
        self._offline_pending_lock = MagicMock()
        self._offline_pending_lock.__enter__ = MagicMock(return_value=None)
        self._offline_pending_lock.__exit__ = MagicMock(return_value=None)
        self._get_data_store = {}
        self._cloud_drive = MagicMock()
        self._postprocess_task_update = False
        self._FINALIZE_DEAD_LOG = "文件后处理失败超出重试限制：{file_name}"

    def _notify_offline_pending_changed(self, count):
        pass

    def _supported_hash_algorithms(self):
        return frozenset({"sha1", "md5"})

    def _cloud_directory_snapshot(self, path, cache=None):
        if cache and path in cache:
            return cache[path]
        return True, DirectoryFileIndex()

    def _get_data(self, key):
        return self._get_data_store.get(key, {})

    def _save_offline_pending(self, data):
        self._get_data_store[self._OFFLINE_PENDING_KEY] = data


class TestPostprocessServiceHelpers(unittest.TestCase):
    """测试任务 ID、洗版备份命名、到期判断等辅助函数。"""

    def test_postprocess_task_id(self):
        # 具有 subscribe_id
        self.assertEqual(
            PostprocessService._postprocess_task_id({"subscribe_id": 123}),
            "subscribe:123"
        )
        # 具有 sub_key
        self.assertEqual(
            PostprocessService._postprocess_task_id({"sub_key": "sub_xyz"}),
            "media:sub_xyz"
        )
        # 均无
        self.assertEqual(
            PostprocessService._postprocess_task_id({}),
            ""
        )

    def test_upgrade_backup_name(self):
        backup_name = PostprocessService._upgrade_backup_name("Movie.Name.2024.1080p.mkv", "task-abc123xyz")
        self.assertTrue(backup_name.startswith("Movie.Name.2024.1080p-"))
        self.assertTrue(backup_name.endswith(".mkv"))
        self.assertIn("taskabc123", backup_name)

    def test_due_pending_keys(self):
        now = 1000.0
        pending = {
            "due_item": {"next_check_at": 900.0, "_monitor_until": 950.0},
            "future_item": {"next_check_at": 1100.0, "_monitor_until": 950.0},
            "monitored_item": {"next_check_at": 900.0, "_monitor_until": 1200.0},
        }
        due = PostprocessService._due_pending_keys(pending, now)
        self.assertEqual(due, ["due_item"])

        # 强制到期测试
        forced_due = PostprocessService._due_pending_keys(pending, now, force=True)
        self.assertIn("due_item", forced_due)
        self.assertIn("future_item", forced_due)
        self.assertNotIn("monitored_item", forced_due)

    def test_media_context_key(self):
        sub_item = {"subscribe_id": 42}
        self.assertEqual(PostprocessService._media_context_key(sub_item), ("subscribe", 42))

        key_item = {"sub_key": "my_sub_key"}
        self.assertEqual(PostprocessService._media_context_key(key_item), ("sub_key", "my_sub_key"))

        media_item = {
            "mediainfo": {"tmdb_id": 999, "type": "tv"},
            "season": 2,
        }
        self.assertEqual(PostprocessService._media_context_key(media_item), ("media", "tv", "999", 2))


class TestRetryAndLocateMechanism(unittest.TestCase):
    """测试重试退避调度、死任务判断与文件定位算法。"""

    def setUp(self):
        self.service = DummyPostprocessService()

    def test_schedule_finalize_retry(self):
        item = {"file_name": "Test.mkv", "check_index": 0}
        now = 1000.0
        self.service._schedule_finalize_retry(item, now)
        self.assertEqual(item["check_index"], 1)
        self.assertEqual(item["next_check_at"], 1000.0 + self.service._OFFLINE_CHECK_DELAYS[1])

    def test_finalize_failure_and_dead_letter(self):
        item = {"file_name": "DeadTask.mkv", "fail_count": 0, "task_type": "share"}
        # 阈值为 2
        is_dead = self.service._finalize_failure(item, "pending_key")
        self.assertFalse(is_dead)
        self.assertEqual(item["fail_count"], 1)

        is_dead = self.service._finalize_failure(item, "pending_key")
        self.assertTrue(is_dead)
        self.assertEqual(item["fail_count"], 2)
        self.assertTrue(item.get("finalize_dead"))

    def test_locate_cloud_file_strategies(self):
        target_file = types.SimpleNamespace(name="Episode.S01E03.mkv", sha1="MATCH_SHA1")
        file_index = DirectoryFileIndex([target_file])
        ctx = PostprocessBatchContext(
            pending={}, pending_snapshot={}, due_keys=[], now=0.0,
            monitor_token="", task_map={}, tasks_valid=True, subscribe_cache={}
        )
        ctx.directory_snapshots["/staging"] = (True, file_index)

        # 策略 1: 精确文件名匹配
        found1 = self.service._locate_cloud_file("/staging", ["Episode.S01E03.mkv"], "", ctx)
        self.assertEqual(found1, target_file)

        # 策略 2: 基于 SHA1 哈希反查匹配
        found2 = self.service._locate_cloud_file("/staging", ["wrong_name.mkv"], "MATCH_SHA1", ctx)
        self.assertEqual(found2, target_file)

        # 策略 3: 基于前缀/后缀模糊匹配
        found3 = self.service._locate_cloud_file("/staging", ["Episode.S01E03.1080p.mkv"], "", ctx)
        self.assertEqual(found3, target_file)


class TestBatchMutationsAndOrganizeContract(unittest.TestCase):
    """测试批量重命名与移动状态机，重点验证开启与关闭自动整理（Issue #8）的行为契约。"""

    def test_execute_batch_cloud_mutations_when_organize_enabled(self):
        """当开启自动整理时，待整理文件应被纳入批量重命名与批量移动队列。"""
        service = DummyPostprocessService(organize_after_transfer=True)
        source_file = types.SimpleNamespace(name="Source.EP01.mp4", sha1="FILEHASH")
        index = DirectoryFileIndex([source_file])

        ctx = PostprocessBatchContext(
            pending={
                "task_1": {
                    "pending_key": "task_1",
                    "file_name": "Target.S01E01.mp4",
                    "staging_name": "Source.EP01.mp4",
                    "staging_dir": "/staging",
                    "cloud_dir": "/media/tv",
                    "task_type": "share",
                }
            },
            pending_snapshot={},
            due_keys=["task_1"],
            now=1000.0,
            monitor_token="tok",
            task_map={},
            tasks_valid=True,
            subscribe_cache={},
        )
        ctx.directory_snapshots["/staging"] = (True, index)

        renamed_target = types.SimpleNamespace(name="Target.S01E01.mp4", sha1="FILEHASH")
        service._cloud_batch_mutations.rename_files.return_value = {"task_1": renamed_target}

        service._execute_batch_cloud_mutations(ctx)

        # 应调用批量重命名
        service._cloud_batch_mutations.rename_files.assert_called_once()
        # 且在移动组中存在目标目录 /media/tv
        self.assertEqual(ctx.pending["task_1"]["staging_name"], "Target.S01E01.mp4")

    def test_execute_batch_cloud_mutations_when_organize_disabled(self):
        """当关闭自动整理时（Issue #8），文件直接加入 prepared_files 与 moved_files，跳过重命名与跨目录移动。"""
        service = DummyPostprocessService(organize_after_transfer=False)
        source_file = types.SimpleNamespace(name="Source.EP01.mp4", sha1="FILEHASH")
        index = DirectoryFileIndex([source_file])

        ctx = PostprocessBatchContext(
            pending={
                "task_1": {
                    "pending_key": "task_1",
                    "file_name": "Source.EP01.mp4",
                    "staging_name": "Source.EP01.mp4",
                    "staging_dir": "/staging/繁花 (2023)",
                    "cloud_dir": "/media/tv",
                    "task_type": "share",
                }
            },
            pending_snapshot={},
            due_keys=["task_1"],
            now=1000.0,
            monitor_token="tok",
            task_map={},
            tasks_valid=True,
            subscribe_cache={},
        )
        ctx.directory_snapshots["/staging/繁花 (2023)"] = (True, index)

        service._execute_batch_cloud_mutations(ctx)

        # 绝不调用网盘批量重命名
        service._cloud_batch_mutations.rename_files.assert_not_called()

        # 文件应直接完成就绪与原位落盘
        self.assertEqual(ctx.prepared_files["task_1"], source_file)
        self.assertEqual(ctx.moved_files["task_1"], source_file)
        self.assertEqual(ctx.pending["task_1"]["cloud_dir"], "/staging/繁花 (2023)")
        self.assertEqual(ctx.pending["task_1"]["file_name"], "Source.EP01.mp4")
        self.assertIsNotNone(ctx.pending["task_1"].get("moved_at"))


class TestReconcilePendingState(unittest.TestCase):
    """测试状态调和持久化逻辑。"""

    def test_reconcile_removes_completed_items(self):
        service = DummyPostprocessService()
        service._OFFLINE_PENDING_KEY = "offline_pending"

        # 初始持久化存储中包含 task_completed 和 task_still_pending
        service._get_data_store["offline_pending"] = {
            "task_completed": {
                "created_at": 100.0, "share_url": "url1", "file_name": "f1", "task_type": "share",
                "_monitor_token": "token_A"
            },
            "task_still_pending": {
                "created_at": 200.0, "share_url": "url2", "file_name": "f2", "task_type": "share",
                "_monitor_token": "token_A", "fail_count": 0
            }
        }

        ctx = PostprocessBatchContext(
            pending={
                # task_completed 已经处理完成，从 ctx.pending 中移除（不在 pending 字典里）
                "task_still_pending": {
                    "created_at": 200.0, "share_url": "url2", "file_name": "f2", "task_type": "share",
                    "fail_count": 1
                }
            },
            pending_snapshot={
                "task_completed": {
                    "created_at": 100.0, "share_url": "url1", "file_name": "f1", "task_type": "share"
                },
                "task_still_pending": {
                    "created_at": 200.0, "share_url": "url2", "file_name": "f2", "task_type": "share"
                }
            },
            due_keys=["task_completed", "task_still_pending"],
            now=1000.0,
            monitor_token="token_A",
            task_map={},
            tasks_valid=True,
            subscribe_cache={}
        )

        result = service._reconcile_pending_state(ctx)

        saved = service._get_data("offline_pending")
        # task_completed 应已从持久化中移除
        self.assertNotIn("task_completed", saved)
        # task_still_pending 应保留且 fail_count 已更新为 1
        self.assertIn("task_still_pending", saved)
        self.assertEqual(saved["task_still_pending"]["fail_count"], 1)
        self.assertEqual(result["pending"], 1)


if __name__ == "__main__":
    unittest.main()
