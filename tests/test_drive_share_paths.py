import os
import sys
import unittest
from unittest.mock import MagicMock, patch

from plugin_env import (
    ensure_package,
    load_module,
    register_mock,
    register_module,
    register_package,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)


def _plugin_dir(*parts):
    return os.path.join(plugins_v2_path, "cloudsubscribe", *parts)


# 构造 mock 的 app 基础依赖
for mod_name in [
    "app",
    "app.api",
    "app.api.endpoints",
    "app.api.endpoints.plugin",
    "app.core",
    "app.core.config",
    "app.core.cache",
    "app.core.event",
    "app.core.metainfo",
    "app.log",
    "app.plugins",
    "app.schemas",
    "app.schemas.types",
    "app.chain",
    "app.chain.subscribe",
    "app.db",
    "app.db.subscribe_oper",
    "app.scheduler",
    "app.utils",
    "app.utils.string",
]:
    ensure_package(mod_name)

register_package("cloudsubscribe.drive")
drive_common = ensure_package("cloudsubscribe.drive.common")


def iter_transfer_batches(values, batch_size, batch_interval, provider_limit):
    size = max(1, min(int(batch_size or 1), int(provider_limit or 1)))
    normalized = list(dict.fromkeys(str(v) for v in values))
    for offset in range(0, len(normalized), size):
        yield normalized[offset:offset + size]


def safe_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def extract_list(data, keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


drive_common.safe_int = safe_int
drive_common.iter_transfer_batches = iter_transfer_batches
drive_common.extract_list = extract_list
drive_common.CloudDriveFileServiceBase = object

# 加载真实 cloud.py / definitions.py
load_module("cloudsubscribe.core.cloud", "plugins.v2/cloudsubscribe/core/cloud.py")
load_module("cloudsubscribe.core.definitions", "plugins.v2/cloudsubscribe/core/definitions.py")

register_package("cloudsubscribe.core")
register_mock("cloudsubscribe.core.transfer")

register_package("cloudsubscribe.utils")
register_mock("cloudsubscribe.utils.cache", create_platform_ttl_cache=MagicMock())
register_mock(
    "cloudsubscribe.utils.share_expiry",
    is_share_expired=lambda url: False,
    mark_share_expired=lambda *args, **kwargs: None,
    is_expired_share_error=lambda *args, **kwargs: False,
)

register_package("cloudsubscribe.search")
register_module("cloudsubscribe.search.types", RESOURCE_TYPE_DISPLAY={
    "115": {"name": "115网盘"},
    "123": {"name": "123云盘"},
    "quark": {"name": "夸克网盘"},
    "alipan": {"name": "阿里云盘"},
    "baidu": {"name": "百度网盘"},
    "uc": {"name": "UC网盘"},
    "tianyi": {"name": "天翼云盘"},
    "yun139": {"name": "中国移动云盘"},
    "guangya": {"name": "光鸭网盘"},
    "xunlei": {"name": "迅雷云盘"},
    "magnet": {"name": "磁力链接"},
    "ed2k": {"name": "电驴链接"},
})

# 注册各驱动子包以支持相对导入，并加载真实 share service
register_package("cloudsubscribe.drive.tianyi", paths=[_plugin_dir("drive", "tianyi")])
register_package("cloudsubscribe.drive.yun139", paths=[_plugin_dir("drive", "yun139")])
register_mock("cloudsubscribe.drive.yun139.client")

register_package("cloudsubscribe.drive.guangya", paths=[_plugin_dir("drive", "guangya")])
register_mock("cloudsubscribe.drive.guangya.client")

register_package("cloudsubscribe.search.onlinedocs", paths=[_plugin_dir("search", "onlinedocs")])
register_mock("cloudsubscribe.search.onlinedocs.client")
register_mock("cloudsubscribe.search.onlinedocs.provider")

TianyiShareService = load_module(
    "cloudsubscribe.drive.tianyi.share",
    "plugins.v2/cloudsubscribe/drive/tianyi/share.py",
).TianyiShareService
Yun139ShareService = load_module(
    "cloudsubscribe.drive.yun139.share",
    "plugins.v2/cloudsubscribe/drive/yun139/share.py",
).Yun139ShareService
GuangyaShareService = load_module(
    "cloudsubscribe.drive.guangya.share",
    "plugins.v2/cloudsubscribe/drive/guangya/share.py",
).GuangyaShareService

register_package("cloudsubscribe.drive.alipan", paths=[_plugin_dir("drive", "alipan")])
AliPanShareService = load_module(
    "cloudsubscribe.drive.alipan.share",
    "plugins.v2/cloudsubscribe/drive/alipan/share.py",
).AliPanShareService

OnlineDocsSourceDefinition = load_module(
    "cloudsubscribe.search.onlinedocs.definition",
    "plugins.v2/cloudsubscribe/search/onlinedocs/definition.py",
).OnlineDocsSourceDefinition


class TestTianyiShareRelativePath(unittest.TestCase):
    """测试天翼云盘递归遍历时的 parent_path 与 relative_path。"""

    def setUp(self):
        self.client = MagicMock()
        self.files = MagicMock()
        self.service = TianyiShareService(self.client, self.files)

    def test_tianyi_multi_depth_paths(self):
        with patch.object(self.service, "_share_info", return_value={"shareId": "s123", "fileId": "-11"}):
            def mock_list_dir(info, folder_id):
                if folder_id == "-11":
                    return [], [{"id": "f_season1", "name": "Season 1"}]
                elif folder_id == "f_season1":
                    return [{"id": "ep1", "name": "Episode 01.mkv", "size": 1000}], []
                return [], []

            with patch.object(self.service, "_list_directory", side_effect=mock_list_dir):
                files = self.service.list_share_files("https://cloud.189.cn/t/abcdef")
                self.assertEqual(len(files), 1)
                item = files[0]
                self.assertEqual(item["name"], "Episode 01.mkv")
                self.assertEqual(item.get("parent_path"), "Season 1")
                self.assertEqual(item.get("relative_path"), "Season 1/Episode 01.mkv")


class TestYun139ShareRelativePath(unittest.TestCase):
    """测试移动云盘递归遍历时的 parent_path 与 relative_path。"""

    def setUp(self):
        self.client = MagicMock()
        self.files = MagicMock()
        self.upload = MagicMock()
        self.service = Yun139ShareService(self.client, self.files, self.upload)

    def test_yun139_multi_depth_paths(self):
        with patch.object(self.service, "_require_share", return_value=("link1", "pwd1")):
            def mock_list_node(link_id, password, node_id="root"):
                if node_id == "root":
                    return [{"caId": "d1", "caName": "庆余年.S01"}], []
                elif node_id == "d1":
                    return [], [{"coId": "f1", "coName": "QYN.S01E01.mp4", "coSize": 2000}]
                return [], []

            with patch.object(self.service, "_list_node", side_effect=mock_list_node):
                files = self.service.list_share_files("https://share-kd-njs.yun.139.com/s/abcdef")
                self.assertEqual(len(files), 1)
                item = files[0]
                self.assertEqual(item["name"], "QYN.S01E01.mp4")
                self.assertEqual(item.get("parent_path"), "庆余年.S01")
                self.assertEqual(item.get("relative_path"), "庆余年.S01/QYN.S01E01.mp4")

    def test_yun139_transfer_share_with_parent_path(self):
        # 测试 transfer_share 按 parent_path 转存到子目录
        files = [
            {"id": "f1", "name": "ep1.mp4", "parent_path": "Season 1"},
            {"id": "f2", "name": "root.mp4", "parent_path": ""},
        ]
        with patch.object(self.service, "list_share_files", return_value=files), \
                patch.object(self.service, "transfer_file", return_value=True) as mock_transfer:
            res = self.service.transfer_share("https://share-kd-njs.yun.139.com/s/abcdef", "/待整理")
            self.assertTrue(res)
            # 验证 Season 1 文件转存到 /待整理/Season 1，根目录文件转存到 /待整理
            mock_transfer.assert_any_call("https://share-kd-njs.yun.139.com/s/abcdef", "f1", "/待整理/Season 1",
                                          target_name="ep1.mp4")
            mock_transfer.assert_any_call("https://share-kd-njs.yun.139.com/s/abcdef", "f2", "/待整理",
                                          target_name="root.mp4")


class TestGuangyaShareRelativePath(unittest.TestCase):
    """测试光鸭网盘递归遍历时的 parent_path 与 relative_path。"""

    def setUp(self):
        self.client = MagicMock()
        self.client.is_success.return_value = True
        self.client.data.side_effect = lambda resp: resp.get("data") if isinstance(resp, dict) else resp
        self.files = MagicMock()
        self.files.page_size = 100
        self.offline = MagicMock()
        self.offline.is_ed2k_url.return_value = False
        self.offline.is_magnet_url.return_value = False
        self.service = GuangyaShareService(self.client, self.files, self.offline)

    def test_guangya_multi_depth_paths(self):
        with patch.object(self.service, "_share_access", return_value=({}, "token123")):
            def mock_share_files(token, parent_id="", page=1, page_size=100):
                if parent_id == "":
                    return {
                        "code": 0,
                        "data": [{"fileId": "dir1", "fileName": "特工任务.S01", "isDir": True}],
                    }
                elif parent_id == "dir1":
                    return {
                        "code": 0,
                        "data": [{"fileId": "file1", "fileName": "E01.mkv", "fileSize": 3000, "isDir": False}],
                    }
                return {"code": 0, "data": []}

            with patch.object(self.service, "_share_files", side_effect=mock_share_files):
                files = self.service.list_share_files("https://guangyapan.com/s/123456")
                self.assertEqual(len(files), 1)
                item = files[0]
                self.assertEqual(item["name"], "E01.mkv")
                self.assertEqual(item.get("parent_path"), "特工任务.S01")
                self.assertEqual(item.get("relative_path"), "特工任务.S01/E01.mkv")


class TestOnlineDocsDefinition(unittest.TestCase):
    """测试在线文档配置规范声明。"""

    def test_onlinedocs_field_spec(self):
        groups = OnlineDocsSourceDefinition.get_config_groups()
        self.assertEqual(len(groups), 1)
        fields = groups[0].fields
        docs_field = next(f for f in fields if f.key == "onlinedocs")
        self.assertEqual(docs_field.type, "online-documents")
        self.assertEqual(docs_field.cols, 12)
        # 验证已简化定义，不再包含冗余静态 items
        self.assertIsNone(docs_field.items)


class TestAlipanShareRelativePath(unittest.TestCase):
    """测试阿里云盘转存时根据 parent_path 保持子目录结构。"""

    def setUp(self):
        self.client = MagicMock()
        self.files = MagicMock()
        self.service = AliPanShareService(self.client, self.files)

    def test_alipan_transfer_share_with_parent_path(self):
        files = [
            {"id": "file1", "name": "EP01.mp4", "parent_path": "Season 1"},
            {"id": "file2", "name": "cover.jpg", "parent_path": ""},
        ]
        with patch.object(self.service, "list_share_files", return_value=files), \
                patch.object(self.service, "transfer_file", return_value=True) as mock_transfer:
            res = self.service.transfer_share("https://www.alipan.com/s/12345", "/media/tv")
            self.assertTrue(res)
            mock_transfer.assert_any_call("https://www.alipan.com/s/12345", "file1", "/media/tv/Season 1", "EP01.mp4")
            mock_transfer.assert_any_call("https://www.alipan.com/s/12345", "file2", "/media/tv", "cover.jpg")


if __name__ == "__main__":
    unittest.main()
