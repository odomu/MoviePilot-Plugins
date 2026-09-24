import os
import sys
import unittest
from unittest.mock import MagicMock

# 构造 mock 的 app 模块依赖
mock_app = MagicMock()
mock_message_chain = MagicMock()
mock_logger = MagicMock()

mock_app.chain.message.MessageChain = mock_message_chain
mock_app.log.logger = mock_logger

sys.modules["app"] = mock_app
sys.modules["app.chain"] = mock_app.chain
sys.modules["app.chain.message"] = mock_app.chain.message
sys.modules["app.log"] = mock_app.log


# mock OwnerDelegator
class OwnerDelegator:
    def __init__(self, owner):
        object.__setattr__(self, "_owner", owner)

    def __getattr__(self, name):
        return getattr(self._owner, name)

    def __setattr__(self, name, value):
        if name == "_owner":
            object.__setattr__(self, name, value)
            return
        setattr(self._owner, name, value)


mock_core = MagicMock()
mock_core.OwnerDelegator = OwnerDelegator
sys.modules["cloudsubscribe"] = MagicMock()
sys.modules["cloudsubscribe.core"] = mock_core
sys.modules["cloudsubscribe.core.delegation"] = mock_core
sys.modules["cloudsubscribe.search"] = MagicMock()
sys.modules["cloudsubscribe.search.types"] = MagicMock()


# mock resource_type_from_url
def fake_resource_type_from_url(url: str) -> str:
    if "115.com" in url:
        return "115"
    if "quark.cn" in url:
        return "quark"
    if url.startswith("magnet:?"):
        return "magnet"
    return ""


sys.modules["cloudsubscribe.search.types"].resource_type_from_url = fake_resource_type_from_url

import importlib.util

hook_file = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../plugins.v2/cloudsubscribe/core/hook/message.py"))
spec = importlib.util.spec_from_file_location("cloudsubscribe.core.hook.message", hook_file)
mod = importlib.util.module_from_spec(spec)
mod.__package__ = "cloudsubscribe.core.hook"
spec.loader.exec_module(mod)

MessageRoutingHook = mod.MessageRoutingHook


class TestMessageRoutingHook(unittest.TestCase):
    """
    平台消息路由 Hook 单元测试
    """

    def setUp(self):
        self.owner = MagicMock()
        self.hook = MessageRoutingHook(self.owner)
        self.hook._enabled = True
        self.hook._direct_transfer_enabled = True

        # mock extract_resource_links
        self.hook.extract_resource_links = lambda text: [
            w for w in text.split() if "http" in w or w.startswith("magnet:?")
        ]

    def test_message_payload_disabled(self):
        self.hook._direct_transfer_enabled = False
        payload = self.hook._message_payload({"text": "https://115.com/s/123"})
        self.assertIsNone(payload)

    def test_message_payload_ignore_commands(self):
        # 忽略斜杠命令和回调
        self.assertIsNone(self.hook._message_payload({"text": "/search 权力的游戏"}))
        self.assertIsNone(self.hook._message_payload({"text": "CALLBACK:action"}))

    def test_message_payload_ignore_media_attachments(self):
        # 排除包含媒体附件的消息
        self.assertIsNone(self.hook._message_payload({
            "text": "https://115.com/s/123",
            "images": ["image.jpg"],
        }))
        self.assertIsNone(self.hook._message_payload({
            "text": "https://115.com/s/123",
            "files": ["file.zip"],
        }))

    def test_message_payload_extraction(self):
        args = {
            "channel": "telegram",
            "text": "分享资源 https://115.com/s/123 和 https://pan.quark.cn/s/456",
            "userid": "user-001",
            "username": "tester",
            "original_chat_id": 999,
        }
        payload = self.hook._message_payload(args)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["channel"], "telegram")
        self.assertEqual(payload["userid"], "user-001")
        self.assertEqual(payload["chat_id"], 999)
        self.assertEqual(len(payload["links"]), 2)
        self.assertIn("https://115.com/s/123", payload["links"])
        self.assertIn("https://pan.quark.cn/s/456", payload["links"])

    def test_message_hook_install_and_close(self):
        # 模拟 MessageChain._handle_message_core
        orig_func = MagicMock()
        mock_message_chain._handle_message_core = orig_func

        self.hook.install()
        self.assertNotEqual(mock_message_chain._handle_message_core, orig_func)

        self.hook.close()
        self.assertEqual(mock_message_chain._handle_message_core, orig_func)


if __name__ == "__main__":
    unittest.main()
