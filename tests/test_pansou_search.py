"""PanSou 返回大量其他网盘结果时，目标网盘仍应进入订阅候选。"""

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1] / "plugins.v2/cloudsubscribe"
PREFIX = "_pansou_regression"
logger = MagicMock()
http = MagicMock()
http.exceptions.Timeout = TimeoutError


def module(name, **attrs):
    result = types.ModuleType(name)
    result.__path__ = []
    result.__dict__.update(attrs)
    return result


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


dependencies = {
    name: module(name) for name in (
        PREFIX, PREFIX + ".search", PREFIX + ".search.pansou", PREFIX + ".utils",
        "app", "app.schemas",
    )
}
dependencies.update({
    "app.log": module("app.log", logger=logger),
    "app.schemas.types": module("app.schemas.types", MediaType=types.SimpleNamespace(TV="tv", MOVIE="movie")),
    PREFIX + ".core": module(PREFIX + ".core", SearchQuery=object),
    PREFIX + ".search.magnet": module(PREFIX + ".search.magnet", clear_cache=lambda: None,
                                     normalize_magnets=lambda rows, source: rows),
    PREFIX + ".search.matching": module(PREFIX + ".search.matching", normalize_season=lambda value: value),
    PREFIX + ".utils.http_client": module(
        PREFIX + ".utils.http_client", requests=http, RequestGate=MagicMock(),
        normalize_proxies=lambda value: value,
        gated_request=lambda gate, func, *args, **kwargs: func(*args, **kwargs),
    ),
})
dependencies["app.schemas"].MediaInfo = object
with patch.dict(sys.modules, dependencies):
    load(PREFIX + ".search.types", "search/types.py")
    Client = load(PREFIX + ".search.pansou.client", "search/pansou/client.py").PanSouClient
    Service = load(PREFIX + ".search.pansou.service", "search/pansou/service.py").PanSouSearchService


def row(index, drive="quark", **extra):
    return {
        "title": "嫁金钗 (2026) 4K SDR",
        "links": [{"type": drive, "url": f"https://example.com/{index}", "password": "1234"}],
        **extra,
    }


class TestPanSouSearch(unittest.TestCase):
    def setUp(self):
        http.reset_mock()
        http.post.side_effect = None
        logger.reset_mock()
        self.client = Client("https://example.com", auth_enabled=False)

    def request(self, data, **kwargs):
        response = MagicMock(status_code=200)
        response.json.return_value = {"code": 0, "data": data}
        http.post.return_value = response
        return self.client.request_search("嫁金钗", **kwargs)

    def test_target_after_first_hundred_reaches_media_candidates(self):
        target = row(101, "guangya", description="2160p HEVC SDR", tags=["tv"])
        result = self.request({"results": [row(i) for i in range(100)] + [target]}, cloud_types=["guangya"])
        self.assertEqual((result["raw_count"], result["filtered_count"], result["processed_count"]), (101, 1, 1))
        groups = Service._normalize_results(result["results"], "嫁金钗", ["嫁金钗"], 2026, ["guangya"], 10)
        candidate = next(iter(groups.values()))[0]
        self.assertEqual(candidate["url"], target["links"][0]["url"])
        self.assertEqual(candidate["description"], "2160p HEVC SDR")
        self.assertEqual(candidate["tags"], ["tv"])
        self.assertEqual(candidate["password"], "1234")

    def test_mixed_row_preserves_metadata_and_does_not_mutate_response(self):
        original = row(1, description="DoVi 2160p", tags=["tv"], datetime="2026-10-30")
        original["links"].append({"type": "guangya", "url": "target", "password": "5678"})
        result = self.request({"results": [original]}, cloud_types=["guangya"])
        filtered = result["results"][0]
        self.assertEqual(filtered["links"], [original["links"][1]])
        self.assertEqual(filtered["description"], original["description"])
        self.assertEqual(filtered["datetime"], original["datetime"])
        self.assertEqual(len(original["links"]), 2)

    def test_no_matching_links_returns_empty(self):
        result = self.request({"results": [row(1), None, {"links": [None]}]}, cloud_types=["guangya"])
        self.assertEqual(result["results"], [])
        self.assertEqual(result["filtered_count"], 0)

    def test_no_type_filter_retains_multiple_drives_with_cap(self):
        result = self.request({"results": [row(i, "guangya" if i % 2 else "quark") for i in range(120)]})
        self.assertEqual(result["filtered_count"], 120)
        self.assertEqual(len(result["results"]), 100)
        self.assertEqual({x["links"][0]["type"] for x in result["results"]}, {"quark", "guangya"})

    def test_target_rows_still_have_limit(self):
        result = self.request({"results": [row(i, "guangya") for i in range(120)]}, cloud_types=["guangya"])
        self.assertEqual(result["filtered_count"], 120)
        self.assertEqual(result["processed_count"], 100)

    def test_aliases_use_existing_type_mapping(self):
        for requested, returned, expected in [("alipan", "aliyun", "aliyun"), ("aliyun", "alipan", "aliyun"),
                                               ("yun139", "139", "yun139")]:
            with self.subTest(requested=requested, returned=returned):
                result = self.request({"results": [row(1, returned)]}, cloud_types=[requested])
                self.assertEqual(result["results"][0]["links"][0]["type"], expected)
                self.assertEqual(http.post.call_args.kwargs["json"]["cloud_types"], [expected])

    def test_merge_filters_before_round_robin_and_cap(self):
        result = self.request({"merged_by_type": {
            "quark": [{"url": str(i)} for i in range(150)],
            "guangya": [{"url": "target", "note": "嫁金钗 4K", "password": "1234"}],
        }}, cloud_types=["guangya"], response_mode="merge")
        self.assertEqual((result["raw_count"], result["filtered_count"], result["processed_count"]), (151, 1, 1))
        self.assertEqual(result["results"][0]["links"][0]["url"], "target")
        self.assertEqual(result["results"][0]["title"], "嫁金钗 4K")

    def test_merge_without_filter_remains_fair(self):
        result = self.request({"merged_by_type": {
            "magnet": [{"url": str(i)} for i in range(150)],
            "guangya": [{"url": "target"}],
        }}, response_mode="merge")
        self.assertEqual(result["processed_count"], 100)
        self.assertEqual(result["results"][1]["links"][0]["url"], "target")

    def test_merge_request_with_legacy_results_response_filters_first(self):
        result = self.request({"results": [row(i) for i in range(110)] + [row(111, "guangya")]},
                              cloud_types=["guangya"], response_mode="merge")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["links"][0]["type"], "guangya")

    def test_service_logs_counts_at_info_and_errors_at_warning(self):
        media = types.SimpleNamespace(title="嫁金钗", original_title="", cn_name="", en_name="", year=2026)
        query = types.SimpleNamespace(mediainfo=media, media_type="tv", season=1, subscribe=None,
                                      resource_list_mode=False, result_limit=None)
        client = MagicMock(spec=["request_search"])
        client.request_search.return_value = {"results": [row(1, "guangya")], "raw_count": 101,
                                             "filtered_count": 1, "processed_count": 1}
        service = Service(client=client, resource_types=["guangya"])
        self.assertEqual(len(service.search(query)), 1)
        summary = logger.info.call_args.args[0]
        for expected in ["原始条目=101", "网盘筛选后=1", "截断后=1", "有效返回=1"]:
            self.assertIn(expected, summary)
        client.request_search.return_value = {"error": "搜索请求超时"}
        self.assertEqual(service.search(query), [])
        self.assertIn("搜索请求超时", logger.warning.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
