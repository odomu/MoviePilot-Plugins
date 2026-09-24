import importlib.util
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)


# 构造 mock 的 app 基础依赖
class MockModule(types.ModuleType):
    def __getattr__(self, name):
        val = MagicMock()
        setattr(self, name, val)
        return val


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
    m = MockModule(mod_name)
    m.__path__ = []
    sys.modules[mod_name] = m

mock_drive = types.ModuleType("cloudsubscribe.drive")
mock_drive.__path__ = []
mock_drive_common = MockModule("cloudsubscribe.drive.common")


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


mock_drive_common.safe_int = safe_int
mock_drive_common.iter_transfer_batches = iter_transfer_batches
mock_drive_common.extract_list = extract_list
mock_drive_common.CloudDriveFileServiceBase = object

core_cloud_path = os.path.join(plugins_v2_path, "cloudsubscribe/core/cloud.py")
spec_c = importlib.util.spec_from_file_location("cloudsubscribe.core.cloud", core_cloud_path)
mod_core_cloud = importlib.util.module_from_spec(spec_c)
mod_core_cloud.__package__ = "cloudsubscribe.core"
spec_c.loader.exec_module(mod_core_cloud)

mock_core = types.ModuleType("cloudsubscribe.core")
mock_core.__path__ = []

core_defs_path = os.path.join(plugins_v2_path, "cloudsubscribe/core/definitions.py")
spec_defs = importlib.util.spec_from_file_location("cloudsubscribe.core.definitions", core_defs_path)
mod_core_defs = importlib.util.module_from_spec(spec_defs)
mod_core_defs.__package__ = "cloudsubscribe.core"
sys.modules["cloudsubscribe.core.definitions"] = mod_core_defs
spec_defs.loader.exec_module(mod_core_defs)

mock_utils = types.ModuleType("cloudsubscribe.utils")
mock_utils.__path__ = []
mock_utils_cache = types.ModuleType("cloudsubscribe.utils.cache")
mock_utils_cache.create_platform_ttl_cache = MagicMock()

mock_search = types.ModuleType("cloudsubscribe.search")
mock_search.__path__ = []
mock_search_types = types.ModuleType("cloudsubscribe.search.types")
mock_search_types.RESOURCE_TYPE_DISPLAY = {
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
}

sys.modules["cloudsubscribe"] = MagicMock()
sys.modules["cloudsubscribe"].__path__ = []
sys.modules["cloudsubscribe.drive"] = mock_drive
sys.modules["cloudsubscribe.drive.common"] = mock_drive_common
sys.modules["cloudsubscribe.core"] = mock_core
sys.modules["cloudsubscribe.core.cloud"] = mod_core_cloud
sys.modules["cloudsubscribe.core.definitions"] = mod_core_defs
sys.modules["cloudsubscribe.core.transfer"] = MagicMock()
sys.modules["cloudsubscribe.utils"] = mock_utils
sys.modules["cloudsubscribe.utils.cache"] = mock_utils_cache
sys.modules["cloudsubscribe.search"] = mock_search
sys.modules["cloudsubscribe.search.types"] = mock_search_types

# 注册各驱动子包到 sys.modules 以支持相对导入
mod_pkg_tianyi = types.ModuleType("cloudsubscribe.drive.tianyi")
mod_pkg_tianyi.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribe/drive/tianyi")]
sys.modules["cloudsubscribe.drive.tianyi"] = mod_pkg_tianyi

mod_pkg_yun139 = types.ModuleType("cloudsubscribe.drive.yun139")
mod_pkg_yun139.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribe/drive/yun139")]
sys.modules["cloudsubscribe.drive.yun139"] = mod_pkg_yun139
sys.modules["cloudsubscribe.drive.yun139.client"] = MagicMock()

mod_pkg_guangya = types.ModuleType("cloudsubscribe.drive.guangya")
mod_pkg_guangya.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribe/drive/guangya")]
sys.modules["cloudsubscribe.drive.guangya"] = mod_pkg_guangya
sys.modules["cloudsubscribe.drive.guangya.client"] = MagicMock()

mod_pkg_online_docs = types.ModuleType("cloudsubscribe.search.online_docs")
mod_pkg_online_docs.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribe/search/online_docs")]
sys.modules["cloudsubscribe.search.online_docs"] = mod_pkg_online_docs
sys.modules["cloudsubscribe.search.online_docs.client"] = MagicMock()
sys.modules["cloudsubscribe.search.online_docs.provider"] = MagicMock()

# 加载 tianyi share service
tianyi_share_path = os.path.join(plugins_v2_path, "cloudsubscribe/drive/tianyi/share.py")
spec_t = importlib.util.spec_from_file_location("cloudsubscribe.drive.tianyi.share", tianyi_share_path)
mod_t = importlib.util.module_from_spec(spec_t)
mod_t.__package__ = "cloudsubscribe.drive.tianyi"
spec_t.loader.exec_module(mod_t)
TianyiShareService = mod_t.TianyiShareService
sys.modules["cloudsubscribe.drive.tianyi.share"] = mod_t

# 加载 yun139 share service
yun139_share_path = os.path.join(plugins_v2_path, "cloudsubscribe/drive/yun139/share.py")
spec_y = importlib.util.spec_from_file_location("cloudsubscribe.drive.yun139.share", yun139_share_path)
mod_y = importlib.util.module_from_spec(spec_y)
mod_y.__package__ = "cloudsubscribe.drive.yun139"
spec_y.loader.exec_module(mod_y)
Yun139ShareService = mod_y.Yun139ShareService
sys.modules["cloudsubscribe.drive.yun139.share"] = mod_y

# 加载 guangya share service
guangya_share_path = os.path.join(plugins_v2_path, "cloudsubscribe/drive/guangya/share.py")
spec_g = importlib.util.spec_from_file_location("cloudsubscribe.drive.guangya.share", guangya_share_path)
mod_g = importlib.util.module_from_spec(spec_g)
mod_g.__package__ = "cloudsubscribe.drive.guangya"
spec_g.loader.exec_module(mod_g)
GuangyaShareService = mod_g.GuangyaShareService
sys.modules["cloudsubscribe.drive.guangya.share"] = mod_g

# 注册并加载 alipan share service
mod_pkg_alipan = types.ModuleType("cloudsubscribe.drive.alipan")
mod_pkg_alipan.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribe/drive/alipan")]
sys.modules["cloudsubscribe.drive.alipan"] = mod_pkg_alipan

alipan_share_path = os.path.join(plugins_v2_path, "cloudsubscribe/drive/alipan/share.py")
spec_a = importlib.util.spec_from_file_location("cloudsubscribe.drive.alipan.share", alipan_share_path)
mod_a = importlib.util.module_from_spec(spec_a)
mod_a.__package__ = "cloudsubscribe.drive.alipan"
spec_a.loader.exec_module(mod_a)
AliPanShareService = mod_a.AliPanShareService
sys.modules["cloudsubscribe.drive.alipan.share"] = mod_a

# 加载 online_docs definition
online_docs_def_path = os.path.join(plugins_v2_path, "cloudsubscribe/search/online_docs/definition.py")
spec_o = importlib.util.spec_from_file_location("cloudsubscribe.search.online_docs.definition", online_docs_def_path)
mod_o = importlib.util.module_from_spec(spec_o)
mod_o.__package__ = "cloudsubscribe.search.online_docs"
spec_o.loader.exec_module(mod_o)
OnlineDocsSourceDefinition = mod_o.OnlineDocsSourceDefinition
sys.modules["cloudsubscribe.search.online_docs.definition"] = mod_o


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

    def test_online_docs_field_spec(self):
        groups = OnlineDocsSourceDefinition.get_config_groups()
        self.assertEqual(len(groups), 1)
        fields = groups[0].fields
        docs_field = next(f for f in fields if f.key == "online_docs")
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
