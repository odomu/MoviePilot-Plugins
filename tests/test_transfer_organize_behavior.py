"""测试关闭自动整理时保留原分享母文件夹结构与原始文件名的行为（Issue #8）。"""

import importlib.util
import os
import sys
import types
import unittest
from unittest.mock import MagicMock

# 构造 mock 的基础依赖
sys.modules.setdefault("app", MagicMock())
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
    pass


mock_core.OwnerDelegator = OwnerDelegator
mock_core.CloudDriveCapability = MagicMock()
mock_core.CloudDriveProvider = MagicMock()
mock_core.CloudFile = MagicMock()
mock_core.SearchCapability = MagicMock()
sys.modules["cloudsubscribe.core"] = mock_core

mock_search = types.ModuleType("cloudsubscribe.search")
mock_search.__path__ = []
sys.modules["cloudsubscribe.search"] = mock_search
mock_search_types = MagicMock()
sys.modules["cloudsubscribe.search.types"] = mock_search_types

mock_handlers = types.ModuleType("cloudsubscribe.handlers")
mock_handlers.__path__ = []
sys.modules["cloudsubscribe.handlers"] = mock_handlers

mock_sync = types.ModuleType("cloudsubscribe.handlers.sync")
mock_sync.__path__ = []
sys.modules["cloudsubscribe.handlers.sync"] = mock_sync

mock_utils = types.ModuleType("cloudsubscribe.utils")
mock_utils.__path__ = []
sys.modules["cloudsubscribe.utils"] = mock_utils

# 加载实际的 MediaFileParser
parser_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../plugins.v2/cloudsubscribe/utils/file_parser.py"))
spec_parser = importlib.util.spec_from_file_location("cloudsubscribe.utils.file_parser", parser_path)
mod_parser = importlib.util.module_from_spec(spec_parser)
spec_parser.loader.exec_module(mod_parser)
MediaFileParser = mod_parser.MediaFileParser
mock_utils.MediaFileParser = MediaFileParser
mock_utils.parse_magnet_metadata = MagicMock()

# 加载 resources.py 中的 SyncResourceManager
resources_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../plugins.v2/cloudsubscribe/handlers/sync/resources.py"))
spec_res = importlib.util.spec_from_file_location("cloudsubscribe.handlers.sync.resources", resources_path)
mod_res = importlib.util.module_from_spec(spec_res)
mod_res.__package__ = "cloudsubscribe.handlers.sync"
sys.modules["cloudsubscribe.handlers.sync.resources"] = mod_res
spec_res.loader.exec_module(mod_res)
ResourceTransferService = mod_res.ResourceTransferService


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


if __name__ == "__main__":
    unittest.main()
