import importlib.util
import os
import unittest

# 加载 search/types.py
types_file = os.path.abspath(os.path.join(os.path.dirname(__file__), "../plugins.v2/cloudsubscribe/search/types.py"))
spec = importlib.util.spec_from_file_location("cloudsubscribe.search.types", types_file)
types_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(types_mod)

resource_type_from_url = types_mod.resource_type_from_url
resource_type_from_text = types_mod.resource_type_from_text
normalize_resource_type = types_mod.normalize_resource_type
resource_type_name = types_mod.resource_type_name
SUPPORTED_RESOURCE_TYPES = types_mod.SUPPORTED_RESOURCE_TYPES


class TestSearchResourceTypes(unittest.TestCase):
    """
    资源类型识别与分类模块测试用例
    """

    def test_resource_type_from_url(self):
        # 协议链接
        self.assertEqual(resource_type_from_url("magnet:?xt=urn:btih:abcdef123456"), "magnet")
        self.assertEqual(resource_type_from_url("ed2k://|file|test.mkv|1000|abcdef|/"), "ed2k")

        # 网盘域名识别
        self.assertEqual(resource_type_from_url("https://115.com/s/sw3e4r5t"), "115")
        self.assertEqual(resource_type_from_url("https://anxia.com/s/sw3e4r5t"), "115")
        self.assertEqual(resource_type_from_url("https://www.123pan.com/s/abc-xyz"), "123")
        self.assertEqual(resource_type_from_url("https://pan.quark.cn/s/123456789"), "quark")
        self.assertEqual(resource_type_from_url("https://www.alipan.com/s/abcdef"), "alipan")
        self.assertEqual(resource_type_from_url("https://www.aliyundrive.com/s/abcdef"), "alipan")
        self.assertEqual(resource_type_from_url("https://cloud.189.cn/web/share?code=123"), "tianyi")
        self.assertEqual(resource_type_from_url("https://yun.139.com/w/#/detail/123"), "yun139")
        self.assertEqual(resource_type_from_url("https://caiyun.feixin.10086.cn/detail"), "yun139")

        # 未知/不支持的域名
        self.assertEqual(resource_type_from_url("https://example.com/share"), "")

    def test_normalize_resource_type(self):
        self.assertEqual(normalize_resource_type("115pan"), "115")
        self.assertEqual(normalize_resource_type("123pan"), "123")
        self.assertEqual(normalize_resource_type("ali"), "alipan")
        self.assertEqual(normalize_resource_type("aliyun"), "alipan")
        self.assertEqual(normalize_resource_type("189"), "tianyi")
        self.assertEqual(normalize_resource_type("139"), "yun139")
        self.assertEqual(normalize_resource_type("magnetlink"), "magnet")
        self.assertEqual(normalize_resource_type("quark"), "quark")

    def test_resource_type_from_text(self):
        # 通过别名
        self.assertEqual(resource_type_from_text("aliyun"), "alipan")
        self.assertEqual(resource_type_from_text("189"), "tianyi")

        # 通过文本内容包含关键词
        self.assertEqual(resource_type_from_text("【夸克网盘】庆余年 第二季 全集"), "quark")
        self.assertEqual(resource_type_from_text("天翼云盘高速下载地址"), "tianyi")
        self.assertEqual(resource_type_from_text("中国移动云盘 4K 原盘"), "yun139")
        self.assertEqual(resource_type_from_text("115网盘独家首发"), "115")

    def test_resource_type_display_name(self):
        self.assertEqual(resource_type_name("115"), "115网盘")
        self.assertEqual(resource_type_name("123"), "123网盘")
        self.assertEqual(resource_type_name("quark"), "夸克网盘")
        self.assertEqual(resource_type_name("alipan"), "阿里云盘")
        self.assertEqual(resource_type_name("tianyi"), "天翼云盘")
        self.assertEqual(resource_type_name("yun139"), "移动云盘")
        self.assertEqual(resource_type_name("magnet"), "磁力链接")
        self.assertEqual(resource_type_name("custom", fallback="自定义"), "自定义")


if __name__ == "__main__":
    unittest.main()
