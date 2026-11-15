import threading
import types
import unittest
from unittest.mock import MagicMock, patch

from plugin_env import OwnerDelegator, install_app_mocks, load_module, register_package

install_app_mocks("cloudsubscribe", "cloudsubscribe.drive", "cloudsubscribe.drive.p115")
register_package("cloudsubscribe.core", OwnerDelegator=OwnerDelegator)
register_package("cloudsubscribe.utils", parse_magnet_metadata=MagicMock())
offline = load_module(
    "cloudsubscribe.drive.p115.offline", "plugins.v2/cloudsubscribe/drive/p115/offline.py",
)


class TestP115TaskListBound(unittest.TestCase):
    def make_service(self):
        lock = threading.RLock()
        owner = types.SimpleNamespace(
            client=object(), _offline_task_lock=lock,
            _offline_task_condition=threading.Condition(lock),
            _offline_task_cache=[], _offline_task_cache_time=0,
            _offline_task_status={}, _offline_task_cache_revision=0,
            _offline_task_refreshing=False, _offline_task_refresh_ok=False,
            OFFLINE_TASK_CACHE_TTL=600,
            rate_limiter=types.SimpleNamespace(call=lambda callback: callback()),
            _ios_request_kwargs=lambda **kwargs: {},
        )
        return offline.OfflineDownloadService(owner)

    def test_stops_consuming_at_existing_cache_limit(self):
        consumed = []
        requests = []

        def iterator(client, **kwargs):
            requests.append(kwargs)
            for index in range(899):
                consumed.append(index)
                yield {"info_hash": str(index), "status": 2}

        service = self.make_service()
        with patch.object(offline, "clouddownload_iter", iterator, create=True):
            tasks = service.get_offline_tasks(force=True)
        self.assertEqual(len(tasks), 500)
        self.assertEqual(len(consumed), 500)
        self.assertEqual(requests[0].get("page_size"), 100)
        self.assertTrue(service._offline_task_refresh_ok)
        self.assertEqual(len(service.get_offline_tasks()), 500)

    def test_short_list_remains_complete(self):
        service = self.make_service()
        rows = [{"info_hash": "A", "status": 1}, {"info_hash": "B", "status": 2}]
        with patch.object(offline, "clouddownload_iter", return_value=iter(rows), create=True):
            tasks = service.get_offline_tasks(force=True)
        self.assertEqual([task["id"] for task in tasks], ["A", "B"])


if __name__ == "__main__":
    unittest.main()
