import unittest

from plugin_env import load_module

types = load_module(
    "cloudsubscribe_resource_types_test",
    "plugins.v2/cloudsubscribe/search/types.py",
)


class ResourceTypeDefinitionTest(unittest.TestCase):
    def test_aliases_and_hosts(self):
        self.assertEqual(types.normalize_resource_type("aliyun"), "alipan")
        self.assertEqual(types.normalize_resource_type("123pan"), "123")
        self.assertIn("pan.baidu.com", types.TYPE_HOSTS["baidu"])
        self.assertEqual(
            types.resource_type_from_url("https://pan.baidu.com/s/1abc"),
            "baidu",
        )
        self.assertEqual(
            types.resource_type_from_url("https://drive-pc.quark.cn/x"),
            "quark",
        )

    def test_text_and_panel_detection(self):
        self.assertEqual(types.resource_type_from_text("百度网盘资源"), "baidu")
        self.assertEqual(types.resource_type_from_text("阿里云盘分享"), "alipan")
        self.assertEqual(types.resource_type_from_text("magnet:?xt=urn:btih:x"), "magnet")
        self.assertEqual(types.resource_type_from_panel("百度网盘"), "baidu")
        self.assertEqual(types.resource_type_from_panel("other"), "")

    def test_derived_metadata_is_consistent(self):
        self.assertEqual(
            types.PREVIEW_PROVIDER_KEYS,
            {
                "115": "115",
                "123": "123",
                "quark": "quark",
                "uc": "uc",
                "alipan": "alipan",
                "baidu": "baidu",
                "tianyi": "tianyi",
                "guangya": "guangya",
                "yun139": "yun139",
            },
        )
        self.assertEqual(
            types.SHARE_PASSWORD_QUERY_KEYS,
            {
                "115": "password", "123": "pwd", "baidu": "pwd",
                "uc": "pwd", "guangya": "code",
            },
        )
        self.assertEqual(
            types.TEXT_PASSWORD_TYPES,
            frozenset({"quark", "alipan", "tianyi", "yun139"}),
        )
        self.assertNotIn("share", types.CANDIDATE_RESOURCE_TYPES)
        self.assertIn("baidu", types.CANDIDATE_RESOURCE_TYPES)
        self.assertNotIn("hosts", types.RESOURCE_TYPE_DISPLAY["baidu"])
        self.assertNotIn("password_param", types.RESOURCE_TYPE_DISPLAY["baidu"])

    def test_catalog_order_and_uniqueness(self):
        catalog = types.resource_type_catalog()
        values = [item["value"] for item in catalog]
        self.assertEqual(len(values), len(set(values)))
        self.assertEqual(values[:len(types.RESOURCE_TYPE_ORDER)], list(types.RESOURCE_TYPE_ORDER))
        self.assertIn("baidu", values)
        self.assertEqual(
            [item["value"] for item in catalog if item["value"] == "baidu"],
            ["baidu"],
        )

    def test_share_password_attachment(self):
        self.assertEqual(
            types.append_share_password(
                "baidu", "https://pan.baidu.com/s/1abc", "abcd"
            ),
            "https://pan.baidu.com/s/1abc?pwd=abcd",
        )
        self.assertEqual(
            types.append_share_password(
                "quark", "https://pan.quark.cn/s/abc", "abcd"
            ),
            "https://pan.quark.cn/s/abc 提取码: abcd",
        )
        self.assertEqual(
            types.append_share_password(
                "baidu", "https://pan.baidu.com/s/1abc?pwd=zzzz", "abcd"
            ),
            "https://pan.baidu.com/s/1abc?pwd=zzzz",
        )


if __name__ == "__main__":
    unittest.main()
