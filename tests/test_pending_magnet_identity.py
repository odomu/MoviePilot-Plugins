import time
import unittest
from unittest.mock import MagicMock

from test_sync_postprocess import DummyPostprocessService, PostprocessBatchContext


class TestPendingMagnetIdentity(unittest.TestCase):
    def setUp(self):
        self.service = DummyPostprocessService()
        self.service._OFFLINE_PENDING_KEY = "pending"
        self.service._OFFLINE_MONITOR_LEASE_SECONDS = 900
        self.service._OFFLINE_TASK_TYPES = {"magnet"}
        self.service._cloud_directories = MagicMock()
        self.service._cloud_query = MagicMock()
        self.service._cloud_mutations = MagicMock()

    def test_subscription_ids_share_the_correct_native_task(self):
        native_id = "A" * 40
        other_id = "B" * 40
        ids = [f"magnet:{native_id.lower()}:12", f"magnet:{native_id}:13", other_id]
        self.service._get_data_store["pending"] = {
            task_id: {"task_id": task_id, "task_type": "magnet", "history_ready": True}
            for task_id in ids
        }
        tasks = [
            {"id": native_id, "completed": True, "file_id": "target-folder"},
            {"id": other_id, "completed": False},
        ]
        ctx = self.service._prepare_postprocess_context(
            force=True, offline_tasks=tasks, offline_tasks_valid=True,
        )
        self.assertIs(ctx.task_map[ids[0].upper()], tasks[0])
        self.assertIs(ctx.task_map[ids[1].upper()], tasks[0])
        self.assertIs(ctx.task_map[other_id], tasks[1])

    def test_known_task_status_does_not_scan_shared_staging(self):
        for completed in (True, False):
            with self.subTest(completed=completed):
                task = {"completed": completed}
                now = time.time()
                item = {"task_id": "TASK", "task_type": "magnet", "created_at": now}
                ctx = PostprocessBatchContext(
                    pending={"key": item}, pending_snapshot={}, due_keys=["key"],
                    now=now, monitor_token="test", task_map={"TASK": task},
                    tasks_valid=True, subscribe_cache={},
                )
                snapshot = MagicMock(side_effect=AssertionError("shared scan"))
                object.__setattr__(self.service, "_snapshot", snapshot)
                object.__setattr__(self.service, "_schedule_finalize_retry", MagicMock())
                object.__setattr__(self.service, "_update_postprocess_progress", MagicMock())
                actual_task, ready = self.service._poll_offline_ready(item, "key", ctx)
                self.assertIs(actual_task, task)
                self.assertEqual(ready, completed)
                snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
