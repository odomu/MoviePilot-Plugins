"""Issue #33：完成通知不能阻塞订阅完成凭证。"""

import time
import unittest
from threading import Event
from types import SimpleNamespace

from plugin_env import (
    install_app_mocks,
    load_module,
    register_module,
    set_media_type,
)

install_app_mocks(
    "app.core",
    "app.core.config",
    "app.core.metainfo",
    "app.chain",
    "app.chain.subscribe",
    "app.db",
    "app.db.subscribe_oper",
    "app.db.systemconfig_oper",
    "app.helper",
    "app.helper.message",
    "app.schemas",
    "app.schemas.types",
)
set_media_type(movie="movie", tv="tv")

release = Event()
delivered = []


class _SlowSubscribeChain:
    """模拟通知渠道卡住数十秒的完成链。"""

    def post_message(self, **kwargs):
        release.wait(5)
        delivered.append(kwargs)


register_module(
    "app.chain.subscribe",
    SubscribeChain=_SlowSubscribeChain,
)

completion = load_module(
    "cloudsubscribe.handlers.subscription.completion",
    "plugins.v2/cloudsubscribe/handlers/subscription/completion.py",
)


class TestCompletionNotificationAsync(unittest.TestCase):
    def tearDown(self):
        release.set()

    def test_post_message_returns_before_channel_finishes(self):
        delivered.clear()
        release.clear()
        message = SimpleNamespace(title="已完成", text="", ctype=None)

        started = time.monotonic()
        completion.SubscribeCompletionChain().post_message(message=message)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.5)
        self.assertEqual(delivered, [])

        release.set()
        deadline = time.monotonic() + 2
        while not delivered and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(delivered), 1)
        self.assertIs(delivered[0]["message"], message)
