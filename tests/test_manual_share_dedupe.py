"""手动提交分享链接的重复转存与洗版基线回归测试。

复现 Issue：无订阅绑定的手动提交（Emby 电影院卡片“转存入库”）在目标网盘已存在
整理好的集数时，旧实现会绕过去重与洗版逻辑，把整包分享再次转存成“xxx(1).mkv”。
公共 mock 与模块加载逻辑见 ``tests/plugin_env.py``。
"""

import sys
import types
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    ensure_package,
    install_app_mocks,
    load_module,
    set_media_type,
)

install_app_mocks(
    "app.core", "app.core.context", "app.core.metainfo", "app.db",
    "app.db.subscribe_oper", "app.utils", "app.utils.string", "app.modules",
    "app.modules.filemanager", "app.modules.filemanager.transhandler",
    "cloudsubscribe", "cloudsubscribe.core",
    "cloudsubscribe.handlers", "cloudsubscribe.handlers.sync",
    "cloudsubscribe.handlers.notification",
)
MediaType = set_media_type(movie="电影", tv="电视剧")

sys.modules["cloudsubscribe.core"].OwnerDelegator = OwnerDelegator

_notification = ensure_package("cloudsubscribe.handlers.notification")
_notification.MediaServerResolver = MagicMock()
_notification.MediaServerResolver.episode_numbers = MagicMock(return_value=(True, set()))
_notification.MediaServerNotifier = MagicMock()

# 真实加载 handlers/sync/utils.py（normalize_season/format_episode_ranges）
load_module(
    "cloudsubscribe.handlers.sync.utils",
    "plugins.v2/cloudsubscribe/handlers/sync/utils.py",
)
_television = load_module(
    "cloudsubscribe.handlers.sync.television",
    "plugins.v2/cloudsubscribe/handlers/sync/television.py",
)
_upgrade = load_module(
    "cloudsubscribe.handlers.sync.upgrade",
    "plugins.v2/cloudsubscribe/handlers/sync/upgrade.py",
)
TelevisionSyncProcessor = _television.TelevisionSyncProcessor
UpgradeService = _upgrade.UpgradeService


class DummyMediaInfo:
    def __init__(self):
        self.title = "兰香如故"
        self.year = "2026"
        self.type = MediaType.TV
        self.original_title = "Against the Current"

    @property
    def title_year(self):
        return f"{self.title} ({self.year})"


def _share_files(count=42):
    return [
        {
            "id": str(episode),
            "name": f"兰香如故.S01E{episode:02d}.mkv",
            "size": 5_200_000_000 + episode,
            "url": "https://115cdn.com/s/resource",
            "is_dir": False,
        }
        for episode in range(1, count + 1)
    ]


class FakeOwner:
    """提供 process_tv_subscribe 依赖的最小宿主体。"""

    def __init__(self, enable_cloud_upgrade, cloud_episodes):
        self._enable_cloud_upgrade = enable_cloud_upgrade
        self.upgrade_calls = 0
        self.transfer_items = []
        self.cloud_episodes = set(cloud_episodes)
        self._CLOUD_MEDIA_ROOT = "/"
        self._cloud_transfer_path = "/整理/待整理"
        self._organize_after_transfer = True
        self._skip_other_season_dirs = True
        self._chain = None
        self._subscribe_handler = MagicMock()
        self._search_handler = MagicMock()
        self._search_handler.get_enabled_sources.return_value = ["manual"]
        self._search_handler._search_label.return_value = "label"

    def _cloud_drive_name(self):
        return "115网盘"

    def _stop_requested(self):
        return False

    def __getattr__(self, name):
        # 未显式定义的宿主能力按空实现处理，避免测试因日志/格式化辅助方法失败。
        value = MagicMock()
        setattr(self, name, value)
        return value

    def _is_cloud_upgrade_subscribe(self, subscribe):
        return False

    def _process_tv_subscribe_upgrade(self, **kwargs):
        self.upgrade_calls += 1
        return kwargs.get("transferred_count", 0)

    def _set_task_phase(self, *args, **kwargs):
        return None

    def _timed_sync_call(self, name, func, *args, **kwargs):
        return func(*args, **kwargs)

    def subscription_budget_key(self, subscribe, media_type=None):
        return "budget-key"

    def _subscribe_mediainfo(self, subscribe, media_type, cache=True):
        return DummyMediaInfo()

    def _format_episode_ranges(self, episodes):
        return ",".join(str(value) for value in sorted(episodes))

    def _scan_cloud_resource_episodes(self, **kwargs):
        return True, set(self.cloud_episodes), "115网盘媒体路径"

    def _reconcile_subscribe_physical_episodes(self, **kwargs):
        return None

    def _build_transfer_resource_batches(self, sources, results):
        return [(source, list(results.get(source) or []), False) for source in sources]

    def _resource_input_label(self, url):
        return "分享"

    def _resource_log_reference(self, url):
        return url

    def _resolve_candidate_resource_url(self, candidates, index, resource, label, log_prefix=""):
        return resource.get("url")

    def _is_supported_resource(self, resource, url):
        return True

    def _is_offline_url(self, url):
        return False

    def _is_magnet_url(self, url):
        return False

    def _is_ed2k_url(self, url):
        return False

    def _validated_resource_files(self, url, resource_title="", target_season=None,
                                  log_prefix="", strict=False):
        return _share_files()

    def _summarize_share_episodes(self, files, season, mediainfo=None):
        return len(files), {int(item["id"]) for item in files}

    def _match_episode_files(self, files, mediainfo=None, subscribe=None, season=None,
                             episodes=None, require_media_match=False):
        by_episode = {int(item["id"]): item for item in files}
        return {
            int(episode): (by_episode[int(episode)], 10)
            for episode in (episodes or [])
            if int(episode) in by_episode
        }

    def _transfer_episode_items(self, items, *args, **kwargs):
        self.transfer_items.append(list(items))
        return []

    def _platform_target(self, root, subscribe, mediainfo, source_name,
                         season=None, episode=None):
        season_number = int(season or 1)
        directory = f"/电视剧/国剧/{subscribe.name} ({subscribe.year})/Season {season_number:02d}"
        return directory, source_name


def _subscribe(total_episode=0):
    return types.SimpleNamespace(
        id=-1,
        name="兰香如故",
        year="2026",
        type="电视剧",
        season=1,
        start_episode=1,
        total_episode=total_episode,
        lack_episode=0,
        note=[],
        custom_words=None,
        tmdbid=282326,
    )


def _manual_resource():
    return {
        "url": "https://115cdn.com/s/resource",
        "title": "兰香如故 2160p",
        "source": "hdhive",
        "resource_type": "115",
    }


class ManualShareDedupeTest(unittest.TestCase):
    def _run(self, owner, transient_target, total_episode=0):
        processor = TelevisionSyncProcessor(owner)
        return processor.process_tv_subscribe(
            subscribe=_subscribe(total_episode=total_episode),
            history=[],
            transfer_details=[],
            transferred_count=0,
            exclude_ids=set(),
            manual_resources=[_manual_resource()],
            manual_upgrade=False,
            transient_target=transient_target,
        )

    def test_transient_manual_routes_to_upgrade_when_enabled(self):
        """无订阅手动提交且启用网盘洗版时，必须先建立真实网盘基线再决定替换/跳过。"""
        owner = FakeOwner(enable_cloud_upgrade=True, cloud_episodes=range(1, 43))
        self._run(owner, transient_target=True)
        self.assertEqual(owner.upgrade_calls, 1)
        self.assertEqual(owner.transfer_items, [])

    def test_transient_manual_skips_existing_when_upgrade_disabled(self):
        """洗版关闭时，手动提交分享中已存在的集数不得重复转存。"""
        owner = FakeOwner(enable_cloud_upgrade=False, cloud_episodes=range(1, 43))
        self._run(owner, transient_target=True)
        self.assertEqual(owner.upgrade_calls, 0)
        self.assertEqual(owner.transfer_items, [])

    def test_transient_manual_transfers_missing_episodes(self):
        """目标网盘为空的分享仍按原逻辑整包含集转存。"""
        owner = FakeOwner(enable_cloud_upgrade=False, cloud_episodes=set())
        self._run(owner, transient_target=True)
        self.assertEqual(len(owner.transfer_items), 1)
        self.assertEqual(len(owner.transfer_items[0]), 42)

    def test_subscription_manual_not_force_upgraded(self):
        """订阅绑定的手动提交沿用订阅洗版策略，不被强制切换为洗版基线。"""
        owner = FakeOwner(enable_cloud_upgrade=True, cloud_episodes=set())
        self._run(owner, transient_target=False, total_episode=42)
        self.assertEqual(owner.upgrade_calls, 0)
        self.assertEqual(len(owner.transfer_items), 1)
        self.assertEqual(len(owner.transfer_items[0]), 42)


class ManualUpgradeScopeTest(unittest.TestCase):
    """验证手动分享在洗版基线流程中按实际集数匹配（含基线外的新集）。"""

    def test_manual_pending_episodes_include_share_episodes(self):
        captured = {}

        class UpgradeOwner(FakeOwner):
            def __init__(self):
                super().__init__(enable_cloud_upgrade=True, cloud_episodes=set())
                self._upgrade_mode = "largest"

            def _read_ep_priority(self, subscribe):
                return {}

            def _build_episode_baseline(self, subscribe, mediainfo, season, include_saved=True):
                return {}

            def _get_mp_rule_score(self, *args, **kwargs):
                return 20

            def _timed_sync_call(self, name, func, *args, **kwargs):
                old_file = types.SimpleNamespace(
                    name="兰香如故.S01E05.mkv", size=5_000_000_000, id="old-5"
                )
                return True, {5: old_file}, "/电视剧/国剧/兰香如故 (2026)/Season 01"

            def _validated_resource_files(self, url, resource_title="",
                                          target_season=None, log_prefix="",
                                          strict=False):
                return [
                    {"id": "5", "name": "兰香如故.S01E05.mkv", "size": 5_100_000_000},
                    {"id": "6", "name": "兰香如故.S01E06.mkv", "size": 5_100_000_000},
                ]

            def _summarize_share_episodes(self, files, season, mediainfo=None):
                return len(files), {5, 6}

            def _match_episode_files(self, files, mediainfo=None, subscribe=None,
                                     season=None, episodes=None, require_media_match=False):
                captured["episodes"] = tuple(episodes or ())
                return {}

        owner = UpgradeOwner()
        UpgradeService(owner)._process_tv_subscribe_upgrade(
            subscribe=_subscribe(total_episode=0),
            history=[],
            transfer_details=[],
            transferred_count=0,
            exclude_ids=set(),
            manual_upgrade=False,
            manual_resources=[_manual_resource()],
        )
        self.assertEqual(captured.get("episodes"), (5, 6))


if __name__ == "__main__":
    unittest.main()
