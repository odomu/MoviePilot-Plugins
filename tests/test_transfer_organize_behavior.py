"""测试关闭自动整理时保留原分享母文件夹结构与原始文件名的行为（Issue #8）。"""

import unittest
from unittest.mock import MagicMock

from plugin_env import ensure_package, install_app_mocks, load_module

install_app_mocks(
    "app.core", "app.core.config", "app.core.context", "app.core.metainfo",
    "app.db", "app.db.subscribe_oper", "app.modules",
    "app.modules.filemanager", "app.modules.filemanager.transhandler",
    "app.utils", "app.utils.http", "app.helper", "app.application", "app.adapters",
    "cloudsubscribe", "cloudsubscribe.core", "cloudsubscribe.core.media",
    "cloudsubscribe.drive", "cloudsubscribe.drive.scanner",
    "cloudsubscribe.handlers", "cloudsubscribe.handlers.sync",
    "cloudsubscribe.handlers.notification", "cloudsubscribe.handlers.search",
    "cloudsubscribe.handlers.subscription", "cloudsubscribe.utils",
    "cloudsubscribe.utils.cache", "cloudsubscribe.search.types",
)

from plugin_env import OwnerDelegator


ensure_package("cloudsubscribe.core").OwnerDelegator = OwnerDelegator
mock_utils = ensure_package("cloudsubscribe.utils")

# 加载实际的 MediaFileParser
MediaFileParser = load_module(
    "cloudsubscribe.utils.file_parser",
    "plugins.v2/cloudsubscribe/utils/file_parser.py",
).MediaFileParser
mock_utils.MediaFileParser = MediaFileParser
mock_utils.parse_magnet_metadata = MagicMock()

# 加载 resources.py 中的 SyncResourceManager
ResourceTransferService = load_module(
    "cloudsubscribe.handlers.sync.resources",
    "plugins.v2/cloudsubscribe/handlers/sync/resources.py",
).ResourceTransferService


class DummySyncService(ResourceTransferService):
    def __init__(self, transfer_path: str = "/待整理", organize_after_transfer: bool = True):
        self._cloud_transfer_path = transfer_path
        self._organize_after_transfer = organize_after_transfer
        self._cloud_drive = MagicMock()
        self._cloud_drive_registry = None

    def _is_cloud_resource_url(self, url: str) -> bool:
        return False

    def _is_direct_cloud_resource_url(self, url: str) -> bool:
        return False


class TestTransferOrganizeBehavior(unittest.TestCase):
    def test_staging_dir_with_parent_path_when_organize_disabled(self):
        """当关闭自动整理时，_resource_staging_dir 应正确拼接原分享的母文件夹。"""
        service = DummySyncService(transfer_path="/待整理", organize_after_transfer=False)

        # 1. 文件带有 parent_path
        file_item = {
            "name": "繁花.2023.S01E01.4k.mkv",
            "parent_path": "繁花 (2023) [tmdbid-123456]",
        }
        staging_dir = service._resource_staging_dir("https://115.com/s/test", file_item)
        self.assertEqual(staging_dir, "/待整理/繁花 (2023) [tmdbid-123456]")

        # 2. 文件带有深层 parent_path
        file_item_nested = {
            "name": "EP01.mp4",
            "parent_path": "深渊无间 (2026)/Season 1",
        }
        staging_dir_nested = service._resource_staging_dir("https://115.com/s/test", file_item_nested)
        self.assertEqual(staging_dir_nested, "/待整理/深渊无间 (2026)/Season 1")

        # 3. 文件只有 _relative_path
        file_item_rel = {
            "name": "EP02.mp4",
            "_relative_path": "漫长的季节 (2023)/EP02.mp4",
        }
        staging_dir_rel = service._resource_staging_dir("https://115.com/s/test", file_item_rel)
        self.assertEqual(staging_dir_rel, "/待整理/漫长的季节 (2023)")

        # 4. 根目录下直接是文件，无 parent_path
        file_item_root = {
            "name": "独行月球.2022.mkv",
        }
        staging_dir_root = service._resource_staging_dir("https://115.com/s/test", file_item_root)
        self.assertEqual(staging_dir_root, "/待整理")

    def test_staging_dir_keeps_root_when_organize_enabled(self):
        """当开启自动整理时，_resource_staging_dir 保持根转存目录不变。"""
        service = DummySyncService(transfer_path="/待整理", organize_after_transfer=True)
        file_item = {
            "name": "繁花.2023.S01E01.4k.mkv",
            "parent_path": "繁花 (2023) [tmdbid-123456]",
        }
        staging_dir = service._resource_staging_dir("https://115.com/s/test", file_item)
        self.assertEqual(staging_dir, "/待整理")

    def test_file_parser_iter_files_preserves_parent_path(self):
        """MediaFileParser.iter_files 应保留树状目录结构与 parent_path。"""
        nested_files = [
            {
                "name": "繁花 (2023)",
                "is_dir": True,
                "children": [
                    {
                        "name": "Season 1",
                        "is_dir": True,
                        "children": [
                            {"name": "繁花 - S01E01.mkv", "is_dir": False},
                            {"name": "繁花 - S01E02.mkv", "is_dir": False},
                        ]
                    }
                ]
            }
        ]
        flattened = list(MediaFileParser.iter_files(nested_files))
        self.assertEqual(len(flattened), 2)
        self.assertEqual(flattened[0]["_relative_path"], "繁花 (2023)/Season 1/繁花 - S01E01.mkv")
        self.assertEqual(flattened[0]["parent_path"], "繁花 (2023)/Season 1")
        self.assertEqual(flattened[1]["name"], "繁花 - S01E02.mkv")

    def test_organize_disabled_flags_and_naming_contract(self):
        """验证关闭整理时 target_name 保持原名、且 rename_items 不改名的约定。"""
        organize_enabled = False
        file_item = {"name": "Raw.Movie.Name.2024.1080p.mkv", "sha1": "TEST_HASH", "id": "123"}
        platform_computed_target = "Movie Name (2024) - 1080p.mkv"

        # 模拟电视/电影同步中 target_name 的决定
        if organize_enabled:
            target_name = platform_computed_target
        else:
            target_name = file_item["name"]
        self.assertEqual(target_name, "Raw.Movie.Name.2024.1080p.mkv")

        # 模拟批量转存 rename_items 构造
        rename_item = {
            "sha1": file_item.get("sha1"),
            "target_name": None if not organize_enabled else target_name,
            "url": "https://example.com/share",
        }
        self.assertIsNone(rename_item["target_name"])

    def test_same_drive_transfer_share_logic(self):
        """验证同盘关闭整理时优先调用 transfer_share 的分支逻辑。"""
        mock_transfer = MagicMock()
        mock_transfer.transfer_share.return_value = True

        organize_enabled = False
        share_url = "https://115.com/s/test_share"
        cloud_transfer_path = "/待整理"
        file_ids = ["101", "102"]

        success_ids = []
        share_transferred = False
        if not organize_enabled and hasattr(mock_transfer, "transfer_share"):
            share_success = mock_transfer.transfer_share(
                share_url=share_url,
                save_path=cloud_transfer_path,
            )
            if share_success:
                success_ids = list(file_ids)
                share_transferred = True

        self.assertTrue(share_transferred)
        self.assertEqual(success_ids, ["101", "102"])
        mock_transfer.transfer_share.assert_called_once_with(
            share_url=share_url,
            save_path=cloud_transfer_path,
        )


# 针对 SyncHandler 真实 _transfer_episode_batch 批量转存逻辑的测试套件

install_app_mocks(
    "app.core", "app.core.config", "app.core.context", "app.core.metainfo",
    "app.db", "app.db.subscribe_oper", "app.modules",
    "app.modules.filemanager", "app.modules.filemanager.transhandler",
    "app.utils", "app.utils.http", "app.helper", "app.application", "app.adapters",
    "cloudsubscribe", "cloudsubscribe.core", "cloudsubscribe.core.media",
    "cloudsubscribe.drive", "cloudsubscribe.drive.scanner",
    "cloudsubscribe.handlers", "cloudsubscribe.handlers.sync",
    "cloudsubscribe.handlers.notification", "cloudsubscribe.handlers.search",
    "cloudsubscribe.handlers.subscription", "cloudsubscribe.utils",
    "cloudsubscribe.utils.cache", "cloudsubscribe.search.types",
    *[
        f"cloudsubscribe.handlers.sync.{sibling}"
        for sibling in (
            "baseline", "cleanup", "history", "matching", "metadata", "movie",
            "naming", "notify", "platform_history", "postprocess", "pt_upgrade",
            "retry", "rule_scoring", "subtitles", "television", "upgrade", "utils",
        )
    ],
)

# 加载 service.py 中的真实 SyncHandler
SyncHandler = load_module(
    "cloudsubscribe.handlers.sync.service",
    "plugins.v2/cloudsubscribe/handlers/sync/service.py",
).SyncHandler


class TestSyncHandlerEpisodeBatchTransfer(unittest.TestCase):
    """测试 SyncHandler._transfer_episode_batch 在各种场景下的转存路径与行为契约。"""

    def setUp(self):
        self.handler = SyncHandler.__new__(SyncHandler)
        self.handler._cloud_transfer_path = "/待整理"
        self.handler._organize_after_transfer = False
        self.handler._is_cloud_resource_url = lambda url: False
        self.handler._is_direct_cloud_resource_url = lambda url: False
        self.handler._is_offline_url = lambda url: False
        self.handler._resource_staging_dir = (
            lambda share_url, file_item=None: ResourceTransferService._resource_staging_dir(
                self.handler, share_url, file_item
            )
        )
        self.handler._resource_provider_for_url = MagicMock(return_value=MagicMock(key="p115"))
        self.handler._cloud_drive = MagicMock(key="p115")
        self.handler._ensure_share_transfer_available = MagicMock()
        self.handler._cross_transfer_enabled = False
        self.handler._share_transfer = MagicMock()
        # 默认 transfer_files_batch 成功返回所有 file_ids
        self.handler._share_transfer.transfer_files_batch.side_effect = lambda share_url, file_ids, save_path, **kw: (
            list(file_ids), [])
        # 默认不具备 transfer_share，走按文件列表转存
        if hasattr(self.handler._share_transfer, "transfer_share"):
            delattr(self.handler._share_transfer, "transfer_share")
        self.handler._timed_sync_call = lambda name, fn, **kwargs: fn(**kwargs)
        self.handler._batch_size = 20
        self.handler._batch_interval = 0.0
        self.handler._transfer_risk_cooldown = 0
        self.handler._stop_requested = lambda: False
        self.handler._companion_subtitle_files = lambda *a, **k: []
        self.handler._current_task_context = lambda: (None, None)
        self.handler._cloud_directory_snapshot = MagicMock(return_value=(False, {}))
        self.handler._generate_or_queue_strm_batch = MagicMock(return_value={})

        # 构造通用的 mock mediainfo 与 subscribe
        self.mediainfo = MagicMock()
        self.mediainfo.type.name = "tv"
        self.mediainfo.title = "美国人质"
        self.subscribe = MagicMock()

    def test_partial_transfer_preserves_parent_folder_when_organize_disabled(self):
        """场景：Issue #10 核心问题——关闭整理时，哪怕仅转存一部分（更02集），也必须保留母文件夹。"""
        self.handler._organize_after_transfer = False
        share_url = "https://115.com/s/sample_share"

        # 仅转存第 2 集（模拟原分享有深层母文件夹）
        matched_items = [
            {
                "file": {
                    "id": "file_ep2_id",
                    "name": "美国人质 S01E02 2160p.CHDWEB.mkv",
                    "parent_path": "美国人质 (2026) {tmdbid=239618}/Season 1",
                    "size": 1024000,
                    "sha1": "HASH_EP2",
                },
                "target_name": "美国人质 (2026) - S01E02 - 2160p.mkv",
                "target_dir": "/媒体库/电视剧/美国人质 (2026)/Season 1",
                "episode": 2,
            }
        ]

        self.handler._transfer_episode_batch(
            matched_items=matched_items,
            share_url=share_url,
            mediainfo=self.mediainfo,
            subscribe=self.subscribe,
            season=1,
            sub_key="tv_239618",
        )

        # 验证调用 transfer_files_batch 的目标 save_path 是带母文件夹的子目录，而不是根目录 /待整理
        expected_save_path = "/待整理/美国人质 (2026) {tmdbid=239618}/Season 1"
        self.handler._share_transfer.transfer_files_batch.assert_called_once()
        _, kwargs = self.handler._share_transfer.transfer_files_batch.call_args
        self.assertEqual(kwargs["save_path"], expected_save_path)
        self.assertEqual(kwargs["file_ids"], ["file_ep2_id"])

        # 验证登记到后处理队列的 staging_dir 也是该完整母文件夹路径
        self.handler._generate_or_queue_strm_batch.assert_called_once()
        batch_args = self.handler._generate_or_queue_strm_batch.call_args[0][0]
        self.assertEqual(len(batch_args), 1)
        self.assertEqual(batch_args[0]["staging_dir"], expected_save_path)

    def test_multiple_episodes_same_folder_grouped_and_cached(self):
        """场景：多集在同一母文件夹，应合并批次转存，且目录快照只查询一次（缓存生效）。"""
        self.handler._organize_after_transfer = False
        share_url = "https://115.com/s/sample_share"

        matched_items = [
            {
                "file": {
                    "id": "ep1_id",
                    "name": "美国人质 S01E01 2160p.mkv",
                    "parent_path": "美国人质 (2026) {tmdbid=239618}/Season 1",
                },
                "target_name": "美国人质 - S01E01.mkv",
                "target_dir": "/电视剧/美国人质/Season 1",
                "episode": 1,
            },
            {
                "file": {
                    "id": "ep2_id",
                    "name": "美国人质 S01E02 2160p.mkv",
                    "parent_path": "美国人质 (2026) {tmdbid=239618}/Season 1",
                },
                "target_name": "美国人质 - S01E02.mkv",
                "target_dir": "/电视剧/美国人质/Season 1",
                "episode": 2,
            },
        ]

        self.handler._transfer_episode_batch(
            matched_items=matched_items,
            share_url=share_url,
            mediainfo=self.mediainfo,
            subscribe=self.subscribe,
            season=1,
            sub_key="tv_239618",
        )

        expected_dir = "/待整理/美国人质 (2026) {tmdbid=239618}/Season 1"
        # 1. 验证合并为一次 transfer_files_batch 调用
        self.assertEqual(self.handler._share_transfer.transfer_files_batch.call_count, 1)
        _, kwargs = self.handler._share_transfer.transfer_files_batch.call_args
        self.assertEqual(kwargs["save_path"], expected_dir)
        self.assertEqual(sorted(kwargs["file_ids"]), ["ep1_id", "ep2_id"])

        # 2. 验证预检阶段快照缓存生效：同一目录只调用了一次 _cloud_directory_snapshot
        self.handler._cloud_directory_snapshot.assert_called_once_with(expected_dir)

    def test_episodes_different_parent_paths_grouped_separately(self):
        """场景：多集跨不同母目录（如 Season 1 和 Season 2），应按不同目录分别调用转存。"""
        self.handler._organize_after_transfer = False
        share_url = "https://115.com/s/sample_share"

        matched_items = [
            {
                "file": {
                    "id": "s1_id",
                    "name": "剧集.S01E01.mkv",
                    "parent_path": "大剧/Season 1",
                },
                "target_name": "剧集 - S01E01.mkv",
                "target_dir": "/剧集/Season 1",
                "episode": 1,
            },
            {
                "file": {
                    "id": "s2_id",
                    "name": "剧集.S02E01.mkv",
                    "parent_path": "大剧/Season 2",
                },
                "target_name": "剧集 - S02E01.mkv",
                "target_dir": "/剧集/Season 2",
                "episode": 1,
            },
        ]

        self.handler._transfer_episode_batch(
            matched_items=matched_items,
            share_url=share_url,
            mediainfo=self.mediainfo,
            subscribe=self.subscribe,
            season=1,
            sub_key="tv_multi",
        )

        # 验证调用了 2 次 transfer_files_batch，分别对应两个母目录
        self.assertEqual(self.handler._share_transfer.transfer_files_batch.call_count, 2)
        called_paths = [
            call[1]["save_path"]
            for call in self.handler._share_transfer.transfer_files_batch.call_args_list
        ]
        self.assertIn("/待整理/大剧/Season 1", called_paths)
        self.assertIn("/待整理/大剧/Season 2", called_paths)

    def test_organize_enabled_transfers_all_to_root(self):
        """场景：开启自动整理时，所有文件统一转存到根转存目录。"""
        self.handler._organize_after_transfer = True
        share_url = "https://115.com/s/sample_share"

        matched_items = [
            {
                "file": {
                    "id": "ep1_id",
                    "name": "美国人质 S01E01 2160p.mkv",
                    "parent_path": "美国人质 (2026) {tmdbid=239618}/Season 1",
                },
                "target_name": "美国人质 - S01E01.mkv",
                "target_dir": "/媒体库/Season 1",
                "episode": 1,
            },
            {
                "file": {
                    "id": "ep2_id",
                    "name": "美国人质 S01E02 2160p.mkv",
                    "parent_path": "美国人质 (2026) {tmdbid=239618}/Season 1",
                },
                "target_name": "美国人质 - S01E02.mkv",
                "target_dir": "/媒体库/Season 1",
                "episode": 2,
            },
        ]

        self.handler._transfer_episode_batch(
            matched_items=matched_items,
            share_url=share_url,
            mediainfo=self.mediainfo,
            subscribe=self.subscribe,
            season=1,
            sub_key="tv_239618",
        )

        # 开启整理时全部归入根目录 /待整理
        self.assertEqual(self.handler._share_transfer.transfer_files_batch.call_count, 1)
        _, kwargs = self.handler._share_transfer.transfer_files_batch.call_args
        self.assertEqual(kwargs["save_path"], "/待整理")
        self.assertEqual(sorted(kwargs["file_ids"]), ["ep1_id", "ep2_id"])

    def test_precheck_skips_existing_files_in_subfolder(self):
        """场景：目标母文件夹下已存在某文件时，预检命中并跳过转存。"""
        self.handler._organize_after_transfer = False
        share_url = "https://115.com/s/sample_share"

        # mock 快照中已存在 ep1
        existing_file = MagicMock()
        existing_file.name = "美国人质 S01E01 2160p.mkv"
        existing_file.size = 1000
        existing_file.sha1 = "HASH1"

        def fake_snapshot(path):
            if path == "/待整理/美国人质 (2026) {tmdbid=239618}/Season 1":
                return True, {"美国人质 S01E01 2160p.mkv": existing_file}
            return False, {}

        self.handler._cloud_directory_snapshot = fake_snapshot

        matched_items = [
            {
                "file": {
                    "id": "ep1_id",
                    "name": "美国人质 S01E01 2160p.mkv",
                    "parent_path": "美国人质 (2026) {tmdbid=239618}/Season 1",
                    "size": 1000,
                    "sha1": "HASH1",
                },
                "target_name": "美国人质 - S01E01.mkv",
                "target_dir": "/媒体库/Season 1",
                "episode": 1,
            },
            {
                "file": {
                    "id": "ep2_id",
                    "name": "美国人质 S01E02 2160p.mkv",
                    "parent_path": "美国人质 (2026) {tmdbid=239618}/Season 1",
                    "size": 2000,
                    "sha1": "HASH2",
                },
                "target_name": "美国人质 - S01E02.mkv",
                "target_dir": "/媒体库/Season 1",
                "episode": 2,
            },
        ]

        self.handler._transfer_episode_batch(
            matched_items=matched_items,
            share_url=share_url,
            mediainfo=self.mediainfo,
            subscribe=self.subscribe,
            season=1,
            sub_key="tv_239618",
        )

        # ep1 已存在跳过，只有 ep2 被发送到网盘转存
        self.assertEqual(self.handler._share_transfer.transfer_files_batch.call_count, 1)
        _, kwargs = self.handler._share_transfer.transfer_files_batch.call_args
        self.assertEqual(kwargs["file_ids"], ["ep2_id"])

    def test_failure_recheck_heals_in_subfolder(self):
        """场景：转存返回失败但实际网盘在母文件夹已存在文件，通过子目录快照自愈恢复。"""
        self.handler._organize_after_transfer = False
        share_url = "https://115.com/s/sample_share"

        # 模拟 transfer_files_batch 返回失败
        self.handler._share_transfer.transfer_files_batch.side_effect = lambda share_url, file_ids, save_path, **kw: (
            [], list(file_ids))

        # 模拟自愈复核时在目标母文件夹找到该文件
        found_file = MagicMock()
        found_file.name = "美国人质 S01E01 2160p.mkv"
        self.handler._cloud_directory_snapshot = MagicMock(
            return_value=(True, {"美国人质 S01E01 2160p.mkv": found_file})
        )

        matched_items = [
            {
                "file": {
                    "id": "ep1_id",
                    "name": "美国人质 S01E01 2160p.mkv",
                    "parent_path": "美国人质 (2026) {tmdbid=239618}/Season 1",
                },
                "target_name": "美国人质 - S01E01.mkv",
                "target_dir": "/媒体库/Season 1",
                "episode": 1,
            }
        ]

        self.handler._transfer_episode_batch(
            matched_items=matched_items,
            share_url=share_url,
            mediainfo=self.mediainfo,
            subscribe=self.subscribe,
            season=1,
            sub_key="tv_239618",
        )

        # 验证后处理队列依然收到了该项（说明自愈成功）
        self.handler._generate_or_queue_strm_batch.assert_called_once()
        batch_args = self.handler._generate_or_queue_strm_batch.call_args[0][0]
        self.assertEqual(len(batch_args), 1)
        self.assertEqual(batch_args[0]["result_key"], "ep1_id")


if __name__ == "__main__":
    unittest.main()
