"""夸克和 UC 分享遍历须返回真实文件及转存所需令牌。"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1] / "plugins.v2/cloudsubscribe"
PREFIX = "_share_listing_regression"


class Item(dict):
    __getattr__ = dict.__getitem__


def package(name, **attrs):
    mod = types.ModuleType(name)
    mod.__path__ = []
    mod.__dict__.update(attrs)
    return mod


def load_services():
    dependencies = {name: package(name) for name in (
        PREFIX, PREFIX + ".drive", PREFIX + ".core", PREFIX + ".utils", "app",
    )}
    dependencies.update({
        "app.log": package("app.log", logger=MagicMock()),
        PREFIX + ".drive.common": package(
            PREFIX + ".drive.common", safe_int=lambda v: int(v or 0),
            should_skip_season_dir=lambda name, season: name == "Season 2" and season == 1,
        ),
        PREFIX + ".core.cloud": package(PREFIX + ".core.cloud", ShareLinkStatus=object),
        PREFIX + ".utils.cache": package(
            PREFIX + ".utils.cache", create_platform_ttl_cache=lambda *a, **kw: MagicMock(),
        ),
    })
    services = []
    for drive, class_name in [("quark", "QuarkShareService"), ("uc", "UCShareService")]:
        name = PREFIX + ".drive." + drive
        dependencies[name] = package(name)
        dependencies[name + ".files"] = package(
            name + ".files", cloud_file=lambda raw: Item(raw) if raw else None,
            list_data=lambda client, response: response["items"],
        )
        with patch.dict(sys.modules, dependencies):
            spec = importlib.util.spec_from_file_location(name + ".share", ROOT / "drive" / drive / "share.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            services.append((drive, getattr(mod, class_name)))
    return services


SERVICES = load_services()


def file(file_id, name, parent="0", directory=False):
    return {"id": file_id, "name": name, "is_directory": directory,
            "pdir_fid": parent, "share_fid_token": "token-" + file_id}


class TestShareListing(unittest.TestCase):
    def service(self, cls, pages, page_size=100):
        client = types.SimpleNamespace(is_success=lambda response: True)
        service = cls(client, types.SimpleNamespace(page_size=page_size))
        service._share_access = lambda url: ({"share_id": "share", "password": ""}, "access-token")
        service._get_share_files = MagicMock(
            side_effect=lambda sid, token, parent, page, size: {"items": pages.get((parent, page), [])},
        )
        return service

    def test_root_file_and_transfer_token(self):
        for drive, cls in SERVICES:
            with self.subTest(drive=drive):
                service = self.service(cls, {("0", 1): [file("video", "S01E01.mp4")]})
                rows = service.list_share_files("share-url")
                self.assertEqual([row["id"] for row in rows], ["video"])
                self.assertEqual(service._share_items["share"]["video"],
                                 {"token": "token-video", "parent_id": "0"})

    def test_nested_files_keep_paths_without_returning_directories(self):
        for drive, cls in SERVICES:
            with self.subTest(drive=drive):
                service = self.service(cls, {
                    ("0", 1): [file("show", "Show", directory=True)],
                    ("show", 1): [file("season", "Season 1", "show", directory=True)],
                    ("season", 1): [file("video", "S01E01.mp4", "season"),
                                      file("subtitle", "S01E01.srt", "season")],
                })
                rows = service.list_share_files("share-url", target_season=1)
                self.assertEqual({row["id"] for row in rows}, {"video", "subtitle"})
                for row in rows:
                    self.assertEqual(row["parent_path"], "Show/Season 1")
                    self.assertEqual(row["relative_path"], "Show/Season 1/" + row["name"])
                    self.assertEqual(service._share_items["share"][row["id"]]["parent_id"], "season")

    def test_pagination_keeps_files_from_every_page(self):
        for drive, cls in SERVICES:
            with self.subTest(drive=drive):
                service = self.service(cls, {
                    ("0", 1): [file("one", "E01.mp4"), file("two", "E02.mp4")],
                    ("0", 2): [file("three", "E03.mp4")],
                }, page_size=2)
                self.assertEqual(len(service.list_share_files("share-url")), 3)
                self.assertEqual(len(service._share_items["share"]), 3)
                self.assertEqual(service._get_share_files.call_count, 2)

    def test_other_season_directory_is_not_traversed(self):
        for drive, cls in SERVICES:
            with self.subTest(drive=drive):
                service = self.service(cls, {
                    ("0", 1): [file("wrong", "Season 2", directory=True), file("video", "S01E01.mp4")],
                    ("wrong", 1): [file("wrong-video", "S02E01.mp4", "wrong")],
                })
                rows = service.list_share_files("share-url", target_season=1)
                self.assertEqual([row["id"] for row in rows], ["video"])
                self.assertEqual(service._get_share_files.call_count, 1)

    def test_empty_share_returns_no_files(self):
        for drive, cls in SERVICES:
            with self.subTest(drive=drive):
                service = self.service(cls, {})
                self.assertEqual(service.list_share_files("share-url"), [])
                self.assertEqual(service._share_items["share"], {})


if __name__ == "__main__":
    unittest.main()
