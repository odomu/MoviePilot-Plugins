"""STRM 播放代理契约测试：短链解析、标识短链、302 与错误码。

代理只由插件自有监听端口提供（短链 ``/d/<标识>?/<文件名>``），不依赖平台路由，
因此这里直接对传输无关的 ``handle_request`` 与 ``short_params`` 做断言。
"""

import unittest
import http.client
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from threading import Thread
from unittest.mock import patch, Mock
from types import SimpleNamespace

from plugin_env import (
    OwnerDelegator,
    install_app_mocks,
    load_module,
    register_mock,
    register_package,
)

install_app_mocks("app.log")
register_mock("cloudsubscribe")
_cloud = load_module(
    "cloudsubscribe.core.cloud",
    "plugins.v2/cloudsubscribe/core/cloud.py",
)
register_package(
    "cloudsubscribe.core",
    OwnerDelegator=OwnerDelegator,
    CloudDriveCapability=_cloud.CloudDriveCapability,
    CloudFile=_cloud.CloudFile,
)
register_package("cloudsubscribe.utils")
_strm = load_module(
    "cloudsubscribe.utils.strm",
    "plugins.v2/cloudsubscribe/utils/strm.py",
)
register_package("cloudsubscribe.core.api")
_proxy = load_module(
    "cloudsubscribe.core.api.strm_proxy",
    "plugins.v2/cloudsubscribe/core/api/strm_proxy.py",
)

StrmProxyApi = _proxy.StrmProxyApi
StrmProxyServer = _proxy.StrmProxyServer
StrmGenerator = _strm.StrmGenerator
CloudDriveCapability = _cloud.CloudDriveCapability
CloudFile = _cloud.CloudFile


class StubDrive:
    """只提供直链解析与播放标识声明的网盘替身。"""

    def __init__(self, url="http://cdn.example.com/a.mkv", error=None,
                 playback_variables=("pickcode",), proxy_identifier="pickcode"):
        self.url = url
        self.error = error
        self.playback = SimpleNamespace(
            template_variables=frozenset(playback_variables),
            proxy_identifier=proxy_identifier,
        )
        self.resolved = []

    def supports(self, capability):
        return capability in {
            CloudDriveCapability.FILE_DOWNLOAD,
            CloudDriveCapability.PLAYBACK_REFERENCE,
        }

    def require(self, capability):
        if capability == CloudDriveCapability.PLAYBACK_REFERENCE:
            return self.playback
        return self

    def resolve_download_link(self, file_item):
        self.resolved.append(file_item)
        if self.error:
            raise RuntimeError(self.error)
        return self.url, {}


def build_api(config=None, drive=None):
    owner = SimpleNamespace(
        _applied_config=config if config is not None else {"strm_proxy_enabled": True},
        _cloud_drive=drive if drive is not None else StubDrive(),
    )
    return StrmProxyApi(owner)


class TestStrmProxyContract(unittest.TestCase):
    """代理核心：启用开关、能力校验、302 语义与错误码。"""

    def test_disabled_returns_503(self):
        api = build_api(config={"strm_proxy_enabled": False})
        result = api.handle_request({"pickcode": "abc"})
        self.assertEqual(result.status_code, 503)

    def test_missing_download_capability_returns_503(self):
        api = build_api(drive=SimpleNamespace(supports=lambda capability: False))
        self.assertEqual(api.handle_request({"pickcode": "abc"}).status_code, 503)

    def test_missing_identifier_returns_404(self):
        api = build_api()
        self.assertEqual(api.handle_request({"name": "movie.mkv"}).status_code, 404)

    def test_resolve_failure_returns_502(self):
        api = build_api(drive=StubDrive(error="boom"))
        self.assertEqual(api.handle_request({"pickcode": "abc"}).status_code, 502)

    def test_redirect_contract(self):
        """按网盘标识短链解析出直链，并给出 302 与中文文件名下载头。"""
        drive = StubDrive(url="https://cdn.example.com/%E5%85%B0.mkv?sign=abc")
        api = build_api(drive=drive)

        result = api.handle_request({"pickcode": "abcdefghijklmnopq", "name": "兰香如故.S01E01.mkv"})

        self.assertEqual(result.status_code, 302)
        self.assertEqual(
            result.headers["Location"], "https://cdn.example.com/%E5%85%B0.mkv?sign=abc"
        )
        self.assertIn("filename*=UTF-8''", result.headers["Content-Disposition"])
        self.assertEqual(
            drive.resolved[0].playback_values, {"pickcode": "abcdefghijklmnopq"}
        )

    def test_head_returns_headers_without_body(self):
        api = build_api()
        result = api.handle_request({"pickcode": "abc", "name": "x.mkv"}, "HEAD")
        self.assertEqual(result.status_code, 302)
        self.assertEqual(result.body, b"")
        self.assertIn("Location", result.headers)

    def test_file_id_identifier_for_other_drives(self):
        """夸克/光雅等按 file_id 取链的网盘同样只依赖标识。"""
        drive = StubDrive(playback_variables=("file_id",), proxy_identifier="file_id")
        api = build_api(drive=drive)
        result = api.handle_request({"file_id": "abc123", "name": "movie.mkv"})
        self.assertEqual(result.status_code, 302)
        self.assertEqual(drive.resolved[0].id, "abc123")

    def test_unsupported_drive_ignores_path_params(self):
        """无标识能力的网盘（如 123）不提供短链代理，也不回退路径模式。"""
        drive = StubDrive(proxy_identifier="", playback_variables=("md5",))
        api = build_api(drive=drive)
        result = api.handle_request({"path": "/a/x.mkv", "name": "x.mkv"})
        self.assertEqual(result.status_code, 404)


class TestPortCheck(unittest.TestCase):
    """端口占用检测：空闲端口可用、被占用端口给出原因。"""

    def test_free_port_is_available(self):
        import socket as socket_module

        with socket_module.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        result = _proxy.check_port(free_port)
        self.assertTrue(result["available"])

    def test_occupied_port_reports_unavailable(self):
        import socket as socket_module

        holder = socket_module.socket()
        holder.bind(("0.0.0.0", 0))
        holder.listen(1)
        try:
            busy_port = holder.getsockname()[1]
            result = _proxy.check_port(busy_port)
            self.assertFalse(result["available"])
            self.assertTrue(result["error"])
        finally:
            holder.close()

    def test_invalid_port(self):
        self.assertFalse(_proxy.check_port(0)["available"])
        self.assertFalse(_proxy.check_port(70000)["available"])


class TestShortLinkParsing(unittest.TestCase):
    """短链解析：/d/<标识> 与 CloudDrive2 风格 ?/<文件名>。"""

    def test_short_path_maps_identifier(self):
        api = build_api()
        self.assertEqual(
            api.short_params("/d/abcdefghijklmnopq", {}),
            {"pickcode": "abcdefghijklmnopq"},
        )

    def test_clouddrive_style_query_name(self):
        api = build_api()
        params = api.short_params("/d/abcdefghijklmnopq", {"/剧集.S01E01.mkv": ""})
        self.assertEqual(params["pickcode"], "abcdefghijklmnopq")
        self.assertEqual(params["name"], "剧集.S01E01.mkv")

    def test_plain_query_keeps_name(self):
        api = build_api()
        params = api.short_params("/d/abc", {"name": "movie.mkv"})
        self.assertEqual(params, {"name": "movie.mkv", "pickcode": "abc"})

    def test_end_to_end_short_link(self):
        """短链解析 + 解析直链一体化：等价于监听端口收到的请求。"""
        drive = StubDrive()
        api = build_api(drive=drive)
        params = api.short_params("/d/cg04ctinkn3aidbzc", {"/示例.S01E01.mkv": ""})
        result = api.handle_request(params)
        self.assertEqual(result.status_code, 302)
        self.assertEqual(drive.resolved[0].playback_values, {"pickcode": "cg04ctinkn3aidbzc"})


class TestProxyTemplateSelection(unittest.TestCase):
    """代理短链模板：仅 /d/<标识>?/<文件名>，无标识则不提供。"""

    def test_declared_identifier_wins(self):
        self.assertEqual(
            StrmGenerator.proxy_identifier_variable(
                SimpleNamespace(proxy_identifier="file_id", template_variables=frozenset({"md5"}))
            ),
            "file_id",
        )
        self.assertEqual(
            StrmGenerator.proxy_identifier_variable(
                SimpleNamespace(proxy_identifier="", template_variables=frozenset({"file_id", "md5"}))
            ),
            "file_id",
        )

    def test_identifier_fallback_by_variables(self):
        self.assertEqual(
            StrmGenerator.proxy_identifier_variable(None, {"pickcode", "file_id"}), "pickcode"
        )
        self.assertEqual(StrmGenerator.proxy_identifier_variable(None, {"md5"}), "")

    def test_short_template_only_form(self):
        _, template = StrmGenerator.build_proxy_template("http://mp:9527/", "pickcode")
        self.assertEqual(template, "{base_url}/d/{pickcode}?/{file_name}")
        _, unsupported = StrmGenerator.build_proxy_template("http://mp:9527", "")
        self.assertEqual(unsupported, "")

    def test_custom_route_segment(self):
        """路由可自定义：任意单段安全前缀都可用于短链模板与解析。"""
        _, template = StrmGenerator.build_proxy_template("http://mp:9527", "file_id", "strm")
        self.assertEqual(template, "{base_url}/strm/{file_id}?/{file_name}")
        api = build_api(config={"strm_proxy_enabled": True, "strm_proxy_route": "strm"})
        api._owner._cloud_drive = StubDrive(
            playback_variables=("file_id",), proxy_identifier="file_id"
        )
        params = api.short_params("/strm/abc123", {"/movie.mkv": ""})
        self.assertEqual(params["file_id"], "abc123")
        self.assertEqual(params["name"], "movie.mkv")

    def test_route_segment_sanitized(self):
        """非法路由（多段/特殊字符）回退默认 d，避免路径穿越。"""
        self.assertEqual(StrmGenerator.proxy_route_segment("../etc"), "etc")
        self.assertEqual(StrmGenerator.proxy_route_segment(""), "d")
        self.assertEqual(StrmGenerator.proxy_route_segment("a/b"), "b")

    def test_base_url_normalized(self):
        base, _ = StrmGenerator.build_proxy_template("http://mp:9527/", "pickcode")
        self.assertEqual(base, "http://mp:9527")


class TestRangeStreamFailure(unittest.TestCase):
    def make_stream(self, resolver=None, first_headers=None):
        first = Mock(headers=first_headers or {"ETag": '"v1"'})
        first.read.return_value = b"abcd"
        link = _proxy._LinkResolver("http://upstream/video", {}, resolver)
        stream = _proxy._RangeStream(first, link, 0, 3, 7, 8)
        self.assertEqual(stream.read(), b"abcd")
        return first, stream

    def test_changed_file_stops_and_closes_next_response(self):
        first, stream = self.make_stream()
        second = Mock(status=200, headers={})
        with patch.object(_proxy, "urlopen", return_value=second):
            with self.assertRaises(OSError):
                stream.read()
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_http_error_response_is_closed(self):
        _, stream = self.make_stream()
        from urllib.error import HTTPError
        error = HTTPError("http://upstream/video", 412, "changed", {}, Mock())
        with patch.object(_proxy, "urlopen", side_effect=error), patch.object(error, "close") as close:
            with self.assertRaises(OSError):
                stream.read()
            close.assert_called_once()

    def test_disconnect_does_not_fetch_more(self):
        first, stream = self.make_stream()
        stream.close()
        with patch.object(_proxy, "urlopen") as open_next:
            self.assertEqual(stream.read(), b"")
            open_next.assert_not_called()
        first.close.assert_called_once()

    def test_truncated_part_retries_then_raises(self):
        """分片提前 EOF：同窗口同偏移重试，耗尽后报提前结束。"""
        _, stream = self.make_stream()
        # 重开的窗口必须从同一偏移继续（offset=4，上一窗口 0-3 已读完）。
        after = Mock(status=206, headers={"Content-Range": "bytes 4-7/8"})
        after.read.return_value = b""
        with patch.object(_proxy, "urlopen", return_value=after) as open_next, \
                patch.object(_proxy.time, "sleep"):
            with self.assertRaises(OSError):
                stream.read()
        self.assertEqual(open_next.call_count, _proxy._RELAY_ATTEMPTS)
        self.assertTrue(all(
            call.args[0].get_header("Range") == "bytes=4-7"
            for call in open_next.call_args_list
        ))


class TestRangeStreamResume(unittest.TestCase):
    """分片中途被重置：从精确偏移续传，窗口不重叠。"""

    def test_midpart_reset_resumes_at_exact_offset(self):
        first = Mock(headers={"ETag": '"v1"'})
        first.read = Mock(side_effect=[b"ab", ConnectionResetError()])
        link = _proxy._LinkResolver("http://upstream/video", {}, None)
        stream = _proxy._RangeStream(first, link, 0, 3, 7, 8)
        resumed = Mock(status=206, headers={"Content-Range": "bytes 2-3/8"})
        resumed.read = Mock(side_effect=[b"cd"])
        following = Mock(status=206, headers={"Content-Range": "bytes 4-7/8"})
        following.read = Mock(side_effect=[b"ef", b"gh"])
        with patch.object(_proxy, "urlopen", side_effect=[resumed, following]) as open_next, \
                patch.object(_proxy.time, "sleep"):
            self.assertEqual(stream.read(size=2), b"ab")
            self.assertEqual(stream.read(size=2), b"cd")
            self.assertEqual(stream.read(size=2), b"ef")
            self.assertEqual(stream.read(size=2), b"gh")
            self.assertEqual(stream.read(size=2), b"")
        ranges = [call.args[0].get_header("Range") for call in open_next.call_args_list]
        self.assertEqual(ranges, ["bytes=2-3", "bytes=4-7"])


class TestPrefetchReader(unittest.TestCase):
    """预取缓冲：顺序交付、EOF、异常重抛与 close 停止生产。"""

    def wrap(self, upstream):
        stream = _proxy._PrefetchReader.wrap(upstream)
        self.addCleanup(stream.close)
        return stream

    def test_sequential_order_and_eof(self):
        upstream = Mock()
        upstream.read = Mock(side_effect=[b"ab", b"cd", b""])
        stream = self.wrap(upstream)
        self.assertEqual(stream.read(1), b"ab")
        self.assertEqual(stream.read(1), b"cd")
        self.assertEqual(stream.read(1), b"")
        stream._worker.join(timeout=2)
        self.assertEqual(upstream.read.call_count, 3)  # EOF 后生产者立即退出

    def test_exception_is_reraised_to_consumer(self):
        error = RuntimeError("gap")
        upstream = Mock()
        upstream.read = Mock(side_effect=[b"ab", error])
        stream = self.wrap(upstream)
        self.assertEqual(stream.read(1), b"ab")
        with self.assertRaises(RuntimeError) as context:
            stream.read(1)
        self.assertIs(context.exception, error)  # 抛出的正是生产者遇到的那个异常对象
        stream._worker.join(timeout=2)

    def test_short_chunks_are_delivered_asis(self):
        upstream = Mock()
        upstream.read = Mock(side_effect=[b"a", b"b", b""])
        stream = self.wrap(upstream)
        self.assertEqual([stream.read(8), stream.read(8), stream.read(8)],
                         [b"a", b"b", b""])

    def test_close_stops_producer(self):
        state = SimpleNamespace(reads=0)
        import time as _time

        class EndlessUpstream:
            def read(self, size):
                state.reads += 1
                return b"c" * size

            def close(self):
                state.closed = True

        stream = self.wrap(EndlessUpstream())
        # 等队列填满（生产者卡在 put 轮询）
        for _ in range(100):
            if stream._queue.qsize() >= _proxy._RELAY_PREFETCH_ITEMS:
                break
            _time.sleep(0.02)
        stream.close()
        reads_before = state.reads
        self.assertEqual(stream.read(8), b"")       # close 后 read 立即 EOF
        _time.sleep(0.2)
        self.assertEqual(state.reads, reads_before)  # 不再生产
        self.assertTrue(state.closed)


class TestOpenSegmentRetry(unittest.TestCase):
    """分段打开器：传输层重试、状态码分类与重新取链。"""

    def setUp(self):
        patcher = patch.object(_proxy.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def ok_response(content_range="bytes 0-7/8"):
        return Mock(status=206, headers={"Content-Range": content_range})

    def test_transport_error_retries_same_arguments(self):
        from urllib.error import URLError

        link = _proxy._LinkResolver("http://up/video", {"Cookie": "s"}, None)
        good = self.ok_response()
        with patch.object(_proxy, "urlopen",
                          side_effect=[URLError("reset"), URLError("again"), good]) as open_next:
            upstream, status = _proxy._relay_open_segment(link, {"Range": "bytes=0-7"})
        self.assertIs(upstream, good)
        self.assertEqual(status, 206)
        self.assertEqual(open_next.call_count, 3)
        for call in open_next.call_args_list:
            self.assertEqual(call.args[0].full_url, "http://up/video")
            self.assertEqual(call.args[0].get_header("Range"), "bytes=0-7")

    def test_transport_error_exhausted_raises_abort(self):
        from urllib.error import URLError

        link = _proxy._LinkResolver("http://up/video", {}, None)
        with patch.object(_proxy, "urlopen", side_effect=URLError("down")) as open_next:
            with self.assertRaises(_proxy._RelayAbort):
                _proxy._relay_open_segment(link, {})
        self.assertEqual(open_next.call_count, 3)

    def test_retry_status_backs_off_then_succeeds(self):
        from urllib.error import HTTPError

        link = _proxy._LinkResolver("http://up/video", {}, None)
        good = self.ok_response()
        limited = HTTPError("http://up/video", 429, "too many", {}, None)
        with patch.object(_proxy, "urlopen", side_effect=[limited, good]) as open_next:
            upstream, status = _proxy._relay_open_segment(link, {})
        self.assertEqual(status, 206)
        self.assertEqual(open_next.call_count, 2)
        good.close.assert_not_called()

    def test_refresh_status_without_resolver_is_terminal(self):
        from urllib.error import HTTPError

        link = _proxy._LinkResolver("http://up/video", {}, None)
        denied = HTTPError("http://up/video", 403, "forbidden", {}, None)
        with patch.object(_proxy, "urlopen", side_effect=denied) as open_next:
            upstream, status = _proxy._relay_open_segment(link, {})
        self.assertIs(upstream, denied)
        self.assertEqual(status, 403)
        self.assertEqual(open_next.call_count, 1)

    def test_403_refreshes_download_link(self):
        from urllib.error import HTTPError

        resolved = []

        def resolver():
            resolved.append(True)
            return "http://new/video", {"Cookie": "s2"}

        link = _proxy._LinkResolver("http://up/video", {"Cookie": "s"}, resolver)
        good = self.ok_response()
        denied = HTTPError("http://up/video", 403, "forbidden", {}, None)
        with patch.object(_proxy, "urlopen", side_effect=[denied, good]) as open_next:
            upstream, status = _proxy._relay_open_segment(
                link, {"Range": "bytes=0-7", "If-Range": '"v9"'})
        self.assertEqual(status, 206)
        self.assertEqual(resolved, [True])
        self.assertEqual(link.refreshs, 1)
        self.assertEqual(open_next.call_count, 2)
        self.assertEqual(open_next.call_args_list[1].args[0].full_url, "http://new/video")
        self.assertIsNone(open_next.call_args_list[1].args[0].get_header("If-Range"))

    def test_refresh_is_capped_per_response(self):
        from urllib.error import HTTPError

        resolved = []

        def resolver():
            resolved.append(True)
            return "http://new/video", {}

        link = _proxy._LinkResolver("http://up/video", {}, resolver)
        denied = HTTPError("http://up/video", 403, "forbidden", {}, None)
        with patch.object(_proxy, "urlopen", side_effect=denied) as open_next:
            upstream, status = _proxy._relay_open_segment(link, {})
        self.assertEqual(status, 403)
        self.assertEqual(resolved, [True] * _proxy._RELAY_MAX_REFRESHS)
        self.assertEqual(link.refreshs, _proxy._RELAY_MAX_REFRESHS)
        self.assertEqual(open_next.call_count, 1 + _proxy._RELAY_MAX_REFRESHS)

    def test_resolver_failure_falls_back_to_terminal(self):
        from urllib.error import HTTPError

        def resolver():
            raise RuntimeError("sleepy")

        link = _proxy._LinkResolver("http://up/video", {}, resolver)
        denied = HTTPError("http://up/video", 403, "forbidden", {}, None)
        with patch.object(_proxy, "urlopen", side_effect=denied) as open_next:
            upstream, status = _proxy._relay_open_segment(link, {})
        self.assertEqual(status, 403)
        self.assertEqual(open_next.call_count, 1)


class TestRelayErrorSplit(unittest.TestCase):
    """_relay 读写异常分离：写侧客户端断开静默；读侧失败才记“读取中断”。"""

    def setUp(self):
        self.logger = logging.Logger("strm-relay-test", level=logging.WARNING)
        self.logger.propagate = False
        patcher = patch.object(_proxy, "logger", self.logger)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_stub(self, upstream, wfile=None):
        handler_cls = _proxy.StrmProxyServer._make_handler(SimpleNamespace())
        stub = object.__new__(handler_cls)
        stub.request_version = "HTTP/1.1"
        stub.command = "GET"
        stub.path = "/d/x"
        stub.requestline = "GET /d/x HTTP/1.1"
        stub.close_connection = False
        stub.wfile = wfile if wfile is not None else BytesIO()
        return stub

    def relay(self, upstream, wfile=None):
        self.make_stub(upstream, wfile)._relay(
            _proxy.ProxyResult(status_code=206, upstream=upstream), "GET")
        upstream.close.assert_called_once()  # finally 必收尾

    def test_write_disconnect_is_silent(self):
        upstream = Mock()
        upstream.read = Mock(side_effect=[b"chunk-1", b"chunk-2"])
        wfile = Mock()
        wfile.write.side_effect = [None, ConnectionResetError()]  # 首次写头部成功，随后直言正文失败
        self.relay(upstream, wfile)
        self.assertEqual(upstream.read.call_count, 1)  # 断开后不再读上游

    def test_header_phase_disconnect_is_silent(self):
        upstream = Mock()
        wfile = Mock()
        wfile.write.side_effect = BrokenPipeError()
        self.relay(upstream, wfile)
        upstream.read.assert_not_called()

    def test_read_failure_only_logs_read_line(self):
        upstream = Mock()
        upstream.read = Mock(side_effect=OSError("exhausted"))
        with self.assertLogs(_proxy.logger, level="WARNING") as captured:
            self.relay(upstream)
        self.assertEqual(len(captured.output), 1)
        self.assertIn("夸克播放中转读取中断：OSError", captured.output[0])


class TestQuarkRelay(unittest.TestCase):
    """真实 HTTP 链路验证：认证只发上游，Range/HEAD 与错误码保持一致。"""

    def setUp(self):
        # 缩短重试退避的等待：用例只关心行为，不关心真实退避时长
        self._sleeps = patch.object(_proxy.time, "sleep")
        self._sleeps.start()
        self.addCleanup(self._sleeps.stop)
        self.payload = b"0123456789abcdefghijklmnopqrstuvwxyz"
        self.truncated_range = None
        self.received = []
        test = self

        class Upstream(BaseHTTPRequestHandler):
            def do_GET(self):
                self.respond()

            def do_HEAD(self):
                self.respond()

            def respond(self):
                test.received.append(dict(self.headers))
                if self.headers.get("Cookie") != "session=secret" or self.headers.get("User-Agent") != "Quark-PC":
                    self.send_response(412)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = test.payload
                range_value = self.headers.get("Range")
                code = 200
                if range_value:
                    low, high = range_value.removeprefix("bytes=").split("-", 1)
                    start = int(low) if low else max(0, len(data) - int(high))
                    end = min(int(high), len(data) - 1) if low and high else len(data) - 1
                    code = 206 if start < len(data) else 416
                    self.send_response(code)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}" if code == 206 else f"bytes */{len(data)}")
                    data = data[start:end + 1] if code == 206 else b""
                    if code == 206 and range_value == test.truncated_range:
                        # 模拟 CDN 连接在分片中途提前断开
                        data = data[:4]
                else:
                    self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("ETag", '"version-one"')
                self.send_header("Set-Cookie", "do-not-expose=secret")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        drive = StubDrive(url=f"http://127.0.0.1:{self.upstream.server_port}/video",
                          playback_variables=("file_id",), proxy_identifier="file_id")
        drive.resolve_download_link = lambda item: (drive.url, {"Cookie": "session=secret", "User-Agent": "Quark-PC"})
        self.owner = SimpleNamespace(_applied_config={"strm_proxy_enabled": True, "cloud_drive": "quark"},
                                     _cloud_drive=drive)
        server = StrmProxyServer(self.owner, 0)
        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), server._make_handler())
        self.threads = []
        for httpd in [self.upstream, self.proxy]:
            thread = Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)

    def tearDown(self):
        for server in [self.proxy, self.upstream]:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)

    def fetch(self, method="GET", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=5)
        try:
            conn.request(method, "/d/file-id", headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_get_uses_private_headers_and_streams_body(self):
        code, headers, body = self.fetch(headers={"Cookie": "attacker", "User-Agent": "TV-client"})
        self.assertEqual((code, body), (200, self.payload))
        self.assertNotIn("Set-Cookie", headers)
        self.assertNotIn("Location", headers)
        self.assertNotIn("secret", str(headers))
        self.assertEqual(self.received[0]["Accept-Encoding"], "identity")

    def test_range_and_if_range_are_forwarded(self):
        for value, expected in [("bytes=2-5", self.payload[2:6]), ("bytes=8-", self.payload[8:]),
                                ("bytes=-5", self.payload[-5:])]:
            with self.subTest(range=value):
                code, headers, body = self.fetch(headers={"Range": value, "If-Range": '"version-one"'})
                self.assertEqual((code, body), (206, expected))
                self.assertEqual(int(headers["Content-Length"]), len(expected))
                self.assertEqual(headers["Accept-Ranges"], "bytes")
                self.assertIn("Content-Range", headers)
                self.assertEqual(self.received[-1]["If-Range"], '"version-one"')

    def test_head_returns_upstream_length_without_body(self):
        code, headers, body = self.fetch("HEAD")
        self.assertEqual((code, body), (200, b""))
        self.assertEqual(int(headers["Content-Length"]), len(self.payload))

    def test_segmented_requests_preserve_complete_client_range(self):
        for value, start, end in [(None, 0, 35), ("bytes=2-", 2, 35),
                                  ("bytes=3-25", 3, 25)]:
            with self.subTest(range=value), patch.object(_proxy, "_RELAY_CHUNK_SIZE", 8):
                self.received.clear()
                code, headers, body = self.fetch(headers={"Range": value} if value else {})
                self.assertEqual(code, 206 if value else 200)
                self.assertEqual(body, self.payload[start:end + 1])
                self.assertEqual(int(headers["Content-Length"]), end - start + 1)
                if value:
                    self.assertEqual(headers["Content-Range"], f"bytes {start}-{end}/36")
                else:
                    self.assertNotIn("Content-Range", headers)
                expected = [f"bytes={low}-{min(low + 7, end)}"
                            for low in range(start, end + 1, 8)]
                self.assertEqual([h["Range"] for h in self.received], expected)
                for request in self.received[1:]:
                    self.assertEqual(request["If-Range"], '"version-one"')


    def test_unsatisfiable_range_keeps_416(self):
        code, headers, body = self.fetch(headers={"Range": "bytes=999-"})
        self.assertEqual((code, body), (416, b""))
        self.assertEqual(headers["Content-Range"], f"bytes */{len(self.payload)}")

    def test_rejected_upstream_is_not_reported_as_success(self):
        self.owner._cloud_drive.resolve_download_link = lambda item: (self.owner._cloud_drive.url, {})
        code, _, body = self.fetch()
        self.assertEqual((code, body), (412, b""))

    def test_truncated_upstream_resumes_mid_part(self):
        """上游在分片中途断开（11-05 的 URLError/截断场景）：同窗口断点续传，字节完整。"""
        first_range = f"bytes=8-{8 + _proxy._RELAY_CHUNK_SIZE - 1}"
        self.truncated_range = first_range
        code, headers, body = self.fetch(headers={"Range": "bytes=8-"})
        self.assertEqual(code, 206)
        self.assertEqual(body, self.payload[8:])
        ranges = [h["Range"] for h in self.received]
        self.assertEqual(ranges, [first_range, "bytes=12-35"])
        self.assertTrue(int(headers["Content-Length"]) > 0)
        self.assertEqual(self.received[-1]["If-Range"], '"version-one"')

    def test_tail_cache_hits_without_upstream_or_link_resolve(self):
        """尾部响应读完一次后，同范围请求零上游、零取链直接命中。"""
        cache = _proxy._tail_cache()
        cache.clear()
        self.addCleanup(cache.clear)
        self.payload = b"x" * (64 * 1024)
        tail_start, tail_size = 48 * 1024, 16 * 1024
        calls = []
        original = self.owner._cloud_drive.resolve_download_link

        def counting(item):
            calls.append(item)
            return original(item)

        self.owner._cloud_drive.resolve_download_link = counting
        with patch.object(_proxy, "_RELAY_TAIL_CACHE", tail_size), \
                patch.object(_proxy, "_RELAY_TAIL_TRIGGER", 32 * 1024):
            first = self.fetch(headers={"Range": f"bytes={tail_start}-"})
            self.assertEqual((first[0], len(first[2])), (206, tail_size))
            self.assertEqual(first[2], self.payload[tail_start:])
            received_after_first = len(self.received)
            calls_after_first = len(calls)
            second = self.fetch(headers={"Range": f"bytes={tail_start}-"})
            self.assertEqual((second[0], second[2]), (206, self.payload[tail_start:]))
            self.assertEqual(second[1]["Content-Range"], f"bytes {tail_start}-65535/65536")
            self.assertEqual(len(self.received), received_after_first)   # 零上游
            self.assertEqual(len(calls), calls_after_first)              # 零取链

    def test_tail_cache_switch_can_be_turned_off(self):
        """部署配置默认启用；把开关置 0 即完全旁路，一键回到第一批行为。"""
        cache = _proxy._tail_cache()
        cache.clear()
        self.addCleanup(cache.clear)
        self.assertGreater(_proxy._RELAY_TAIL_CACHE, 0)
        with patch.object(_proxy, "_RELAY_TAIL_CACHE", 0):
            stream = Mock()
            self.assertIs(_proxy._wrap_tail(stream, ("quark", "fid"), "GET",
                                            1024, 2047, 4096, '"v1"'), stream)
            self.assertIsNone(cache.hit(("quark", "fid"), "GET", {"Range": "bytes=1024-"}))

    def spy_logger(self):
        logger = logging.Logger("strm-relay-e2e", level=logging.WARNING)
        logger.propagate = False
        patcher = patch.object(_proxy, "logger", logger)
        patcher.start()
        self.addCleanup(patcher.stop)
        return logger

    def test_upstream_read_failure_logs_read_line(self):
        """上游谎报范围：续传总失败，客户端收到截断响应，日志只记“读取中断”。"""
        logger = self.spy_logger()
        # 模拟 CDN 对每次请求都声称同一范围、只给 4 字节正文：首发通过校验，
        # 续传偏移与声称范围不匹配 → 重试耗尽 → OSError。
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.bind(("127.0.0.1", 0))
        raw.listen(5)
        raw.settimeout(0.5)
        stop = threading.Event()

        def serve():
            while not stop.is_set():
                try:
                    conn, _ = raw.accept()
                except (socket.timeout, OSError):
                    continue
                try:
                    try:
                        conn.recv(65536)
                    except OSError:
                        pass
                    response = (b"HTTP/1.1 206 Partial Content\r\n"
                                b"Content-Range: bytes 0-37/38\r\n"
                                b"Content-Length: 4\r\n"
                                b"ETag: \"version-one\"\r\n"
                                b"Content-Type: video/mp4\r\n"
                                b"Accept-Ranges: bytes\r\n\r\n0123")
                    conn.sendall(response)
                except OSError:
                    pass
                finally:
                    conn.close()
            raw.close()

        worker = Thread(target=serve, daemon=True)
        worker.start()
        self.owner._cloud_drive.resolve_download_link = lambda item: (
            f"http://127.0.0.1:{raw.getsockname()[1]}/video",
            {"Cookie": "session=secret", "User-Agent": "Quark-PC"})
        self.addCleanup(stop.set)
        self.addCleanup(worker.join, timeout=2)
        with self.assertLogs(logger, level="WARNING") as captured:
            with self.assertRaises(http.client.IncompleteRead):
                self.fetch()
        self.assertTrue(any("夸克播放中转读取中断" in m for m in captured.output))

    def test_client_disconnect_is_silent(self):
        """播放器停止/跳播断开连接：写侧静默，无任何 WARNING 日志。"""
        logger = self.spy_logger()
        self.payload = b"0" * (32 * 1024 * 1024)
        conn = http.client.HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=10)
        try:
            conn.request("GET", "/d/file-id")
            conn.getresponse().read(4096)  # 读完头部后立即放弃
        finally:
            conn.close()
        with self.assertNoLogs(logger, level="WARNING"):
            time.sleep(2.0)  # 给服务端循环留出发送/触错的时间

    def test_connection_error_returns_502_without_private_url(self):
        with patch.object(_proxy, "urlopen", side_effect=OSError("private-signed-url")):
            code, _, body = self.fetch()
        self.assertEqual(code, 502)
        self.assertNotIn(b"private-signed-url", body)


class TestTailCache(unittest.TestCase):
    """尾部缓存：纯内存命中、版本失效、If-Range 校验与 LRU 上限。"""

    def setUp(self):
        self.cache = _proxy._TailCache()
        self.file_key = ("quark", "fid-1")
        self.base = {"Content-Type": "video/mp4", "ETag": '"v1"',
                     "Accept-Ranges": "bytes"}
        self.cache.note_size(self.file_key, 1000, '"v1"', self.base)
        patcher = patch.object(_proxy, "_RELAY_TAIL_CACHE", 64)
        patcher.start()
        self.addCleanup(patcher.stop)
        trigger = patch.object(_proxy, "_RELAY_TAIL_TRIGGER", 128)
        trigger.start()
        self.addCleanup(trigger.stop)

    def key(self, total=1000, validator='"v1"'):
        return self.cache._entry_key(self.file_key, total, validator)

    def test_publish_then_hit_serves_exact_bytes(self):
        key = self.key()
        self.cache.begin(key)
        self.cache.publish(key, b"0123456789", start=990)
        result = self.cache.hit(self.file_key, "GET", {"Range": "bytes=992-999"})
        self.assertIsNotNone(result)
        self.assertEqual(result.status_code, 206)
        self.assertEqual(result.headers["Content-Range"], "bytes 992-999/1000")
        self.assertEqual(result.headers["Content-Length"], "8")
        self.assertEqual(result.upstream.read(), b"23456789")

    def test_hit_outside_cached_window_misses(self):
        self.cache.publish(self.key(), b"0123456789", start=990)
        self.assertIsNone(self.cache.hit(self.file_key, "GET", {"Range": "bytes=980-"}))

    def test_if_range_mismatch_misses(self):
        self.cache.publish(self.key(), b"0123456789", start=990)
        self.assertIsNone(self.cache.hit(
            self.file_key, "GET", {"Range": "bytes=995-", "If-Range": '"v2"'}))

    def test_file_version_change_drops_old_bytes(self):
        self.cache.publish(self.key(), b"0123456789", start=990)
        self.cache.note_size(self.file_key, 1000, '"v2"', self.base)
        self.assertIsNone(self.cache.hit(self.file_key, "GET", {"Range": "bytes=995-"}))

    def test_pending_request_waits_for_warm_to_finish(self):
        key = self.key()
        self.cache.begin(key)

        def finish():
            time.sleep(0.05)
            self.cache.publish(key, b"0123456789", start=990)

        worker = Thread(target=finish, daemon=True)
        worker.start()
        result = self.cache.hit(self.file_key, "GET", {"Range": "bytes=995-"})
        worker.join(timeout=2)
        self.assertIsNotNone(result)
        self.assertEqual(result.upstream.read(), b"56789")

    def test_lru_caps_cached_files(self):
        with patch.object(_proxy, "_RELAY_TAIL_CACHE_FILES", 2):
            for number in range(3):
                self.cache.publish(("quark", f"f{number}", 1000, '"v1"'),
                                   b"0123456789", start=990)
            self.assertEqual(len(self.cache._entries), 2)
            self.assertNotIn(("quark", "f0", 1000, '"v1"'), self.cache._entries)


class TestTailCapture(unittest.TestCase):
    """客户端断开后由后台线程把同一条响应读完，失败静默、不改变播放语义。"""

    def setUp(self):
        patcher = patch.object(_proxy, "_RELAY_TAIL_CACHE", 8)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cache = _proxy._TailCache()
        self.key = ("quark", "fid", 1000, '"v1"')

    def test_disconnect_keeps_reading_same_response(self):
        class Upstream:
            def __init__(self):
                self.parts = iter([b"abcd", b"efgh", b""])

            def read(self, size):
                return next(self.parts)

            def close(self):
                pass

        capture = _proxy._TailCapture(Upstream(), self.cache, self.key, 990, 8)
        self.assertEqual(capture.read(), b"abcd")       # 客户端只拿到一半就断开
        capture.close()                                 # 模拟 _relay 的 finally
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.key not in self.cache._entries:
            time.sleep(0.01)
        self.assertIn(self.key, self.cache._entries)
        self.assertEqual(self.cache._entries[self.key][0], b"abcdefgh")

    def test_failure_is_silent_and_does_not_publish(self):
        upstream = Mock()
        upstream.read.side_effect = OSError("upstream reset")
        capture = _proxy._TailCapture(upstream, self.cache, self.key, 990, 8)
        with self.assertRaises(OSError):
            capture.read()
        capture.close()
        self.assertNotIn(self.key, self.cache._entries)


class TestSharedPlaybackReference(unittest.TestCase):
    """以文件 ID 取链的网盘播放引用（阿里云盘/天翼云盘/移动云盘共用）。"""

    @classmethod
    def setUpClass(cls):
        register_mock("app.core")
        register_mock("app.core.cache")
        register_mock("app.utils")
        register_mock("app.utils.string")
        register_mock("app.log")
        register_mock("cloudsubscribe.utils.cache")
        register_mock("cloudsubscribe.drive")
        cls.common = load_module(
            "cloudsubscribe.drive.common",
            "plugins.v2/cloudsubscribe/drive/common.py",
        )

    def test_variables_and_identifier(self):
        reference = self.common.FileIdPlaybackReference()
        self.assertEqual(reference.template_variables, frozenset({"file_id"}))
        self.assertEqual(reference.proxy_identifier, "file_id")

    def test_reference_values_fall_back_to_file_id(self):
        file_item = CloudFile(id="fid-1", name="movie.mkv", is_directory=False)
        values = dict(self.common.FileIdPlaybackReference().reference_values(file_item))
        self.assertEqual(values, {"file_id": "fid-1"})


if __name__ == "__main__":
    unittest.main()
