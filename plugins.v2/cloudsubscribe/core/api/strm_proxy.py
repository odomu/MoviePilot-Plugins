"""STRM 播放代理：短链跳转，以及需要认证请求头的夸克流式中转。"""

import json
import re
import socket
import sys
import time
import types
from queue import Empty, Full, Queue
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import RLock, Thread
from typing import Any, Callable, Dict, Mapping, Optional, Tuple
from urllib.parse import quote, unquote, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.log import logger
from starlette import status

from .. import CloudDriveCapability, CloudFile, OwnerDelegator
from ...utils.strm import StrmGenerator


_RELAY_CHUNK_SIZE = 4 * 1024 * 1024
_RELAY_READ_SIZE = 256 * 1024
_RELAY_OPEN_TIMEOUT = 10            # 建连/单次 socket 操作超时（秒），避免独自吃光客户端超时
_RELAY_ATTEMPTS = 3                 # 同一偏移的首次尝试 + 重试次数
_RELAY_RETRY_BUDGET = 8.0           # 重试墙钟预算（秒），避免触顶播放器/ffprobe 超时
_RELAY_RETRY_BACKOFF = (0.5, 1.0, 2.0)
_RELAY_REFRESH_STATUS = frozenset({401, 403, 404, 410, 412})  # 直链失效：重新取链
_RELAY_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})    # 瞬时错误：退避重试
_RELAY_MAX_REFRESHS = 2             # 单条响应最多重新取链次数，避免重取链风暴
_RELAY_PREFETCH_ITEMS = 8           # 预取缓冲的队列深度（片），隔离上游抖动与客户端消费速度差
_KEEP_RESPONSE_HEADERS = frozenset({
    "content-type", "content-length", "content-range", "accept-ranges",
    "etag", "last-modified",
})
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")


class _RelayAbort(Exception):
    """上游分段请求的重试/预算耗尽（无响应对象可交回调用方）。"""


def _relay_status_kind(status: int) -> str:
    """把上游状态码归类：``refresh``（换链）/``retry``（退避重试）/``terminal``（终态）。"""
    if status in _RELAY_REFRESH_STATUS:
        return "refresh"
    if status in _RELAY_RETRY_STATUS:
        return "retry"
    return "terminal"


def _relay_backoff(deadline: float, attempt: int) -> None:
    """指数退避，总量不超过墙钟预算。"""
    remain = deadline - time.monotonic()
    if remain > 0:
        delay = _RELAY_RETRY_BACKOFF[min(attempt - 1, len(_RELAY_RETRY_BACKOFF) - 1)]
        time.sleep(min(delay, remain))


def _relay_request_headers(download_headers: Any) -> Dict[str, str]:
    """取链返回的认证头 → 发上游的请求头（仅认证信息，不携带客户端身份）。"""
    strip = {"host", "content-length", "accept-encoding", "range", "if-range"}
    headers = {k: v for k, v in dict(download_headers or {}).items()
               if str(k).lower() not in strip}
    headers["Accept-Encoding"] = "identity"
    return headers


class _LinkResolver:
    """直链源：收敛刷新次数；resolver 闭包（file_item/downloader）由调用方注入。"""

    def __init__(self, url: str, headers: Dict[str, str],
                 resolver: Optional[Callable[..., Tuple[str, Mapping[str, str]]]] = None):
        self._url = str(url or "").strip()
        self._headers = dict(headers or {})
        self._resolver = resolver
        self.refreshs = 0

    @property
    def url(self) -> str:
        return self._url

    @property
    def headers(self) -> Dict[str, str]:
        return dict(self._headers)

    def refresh(self) -> bool:
        """重新取链；无 resolver、次数达上限或失败时返回 False。"""
        if self._resolver is None or self.refreshs >= _RELAY_MAX_REFRESHS:
            return False
        try:
            new_url, new_headers = self._resolver()
        except Exception as error:  # noqa: BLE001 - 取链失败仅记录类型，避免泄漏凭据
            logger.warning(f"夸克播放中转重取直链失败：{type(error).__name__}")
            return False
        new_url = str(new_url or "").strip()
        if not new_url:
            return False
        self._url = new_url
        self._headers = _relay_request_headers(new_headers)
        self.refreshs += 1
        return True


def _relay_open_segment(link: _LinkResolver, extra: Dict[str, str]) -> Tuple[Any, int]:
    """打开一条上游分段请求：状态分类 + 退避重试 + 重新取链（同偏移续传由调用方保证）。

    返回 ``(upstream, status)``：``206`` 与无法继续的终态原样交回调用方判断
    （如 ``416`` 透传、``200`` 视为版本变化）；传输层错误耗尽抛 ``_RelayAbort``。
    换链后丢弃 ``If-Range``（旧校验器对新直链无效），改由调用方做范围精确校验。
    日志只记录异常类型/状态码，不回显带签名的 URL。
    """
    deadline = time.monotonic() + _RELAY_RETRY_BUDGET
    last = "连接失败"
    upstream, status = None, None
    for attempt in range(1, _RELAY_ATTEMPTS + 1):
        if attempt > 1 and time.monotonic() >= deadline:
            break
        upstream, status = None, None
        try:
            headers = {**link.headers, **extra}
            if link.refreshs:
                headers.pop("If-Range", None)
            upstream = urlopen(Request(link.url, headers=headers), timeout=_RELAY_OPEN_TIMEOUT)
            status = upstream.status
        except HTTPError as error:
            upstream, status = error, error.code
        except Exception as error:  # noqa: BLE001 - 传输层异常统一退避重试
            last = type(error).__name__
            if attempt == _RELAY_ATTEMPTS or time.monotonic() >= deadline:
                break
            _relay_backoff(deadline, attempt)
            continue
        if status == 206:
            return upstream, 206
        kind = _relay_status_kind(status)
        done = attempt == _RELAY_ATTEMPTS or time.monotonic() >= deadline
        if kind == "refresh" and link.refresh():
            upstream.close()
            logger.info(f"夸克播放中转直链已刷新（第 {link.refreshs} 次）")
            continue
        if not done and kind == "retry":
            upstream.close()  # 瞬时错误只需重试，不透传中间响应
            last = str(status)
            _relay_backoff(deadline, attempt)
            continue
        last = str(status)
        break  # 终态 / 最后一次尝试：原样交回调用方
    if upstream is None:
        raise _RelayAbort(f"夸克分段请求失败：{last}")
    return upstream, status


class _RangeStream:
    """把连续的小范围响应拼成单一字节流：分片失败重试 + 断点续传 + 重新取链。"""

    def __init__(self, upstream: Any, link: _LinkResolver, start: int, part_end: int,
                 end: int, total: int, if_range: str = ""):
        self._upstream = upstream
        self._link = link
        self._if_range = if_range
        self._offset = start  # 绝对游标：[start, _offset) 已交付给调用方，绝不重发、绝不跳过
        self._window_end = part_end
        self._end = end
        self._total = total
        self._remaining = max(0, part_end - start + 1)
        self._closed = False
        self._failures = 0            # 连续无进展失败次数（交付字节后清零）
        self._retry_deadline: Optional[float] = None

    def _drop_upstream(self) -> None:
        if self._upstream is not None:
            self._upstream.close()
            self._upstream = None

    def _open_window(self) -> None:
        """打开当前窗口的分段；分片内重试时窗口保持不变，续传不重叠、不重发。"""
        window_end = min(self._window_end, self._end)
        self._drop_upstream()
        extra: Dict[str, str] = {"Range": f"bytes={self._offset}-{window_end}"}
        if self._link.refreshs == 0 and self._if_range:
            extra["If-Range"] = self._if_range
        upstream, status = _relay_open_segment(self._link, extra)
        match = (_CONTENT_RANGE.fullmatch(
            upstream.headers.get("Content-Range", "")) if status == 206 else None)
        if status != 206 or not match or tuple(map(int, match.groups())) != (
                self._offset, window_end, self._total):
            upstream.close()
            raise OSError("夸克分段响应范围或文件版本变化")
        self._upstream = upstream
        self._remaining = window_end - self._offset + 1

    def _retry(self, label: str) -> bool:
        """同一绝对偏移、同一窗口重试：退避后重开窗口。

        失败按“无进展”跨循环累计：分片能打开但内容 EOF 时不会无限循环，
        只有真正交付字节（``_offset`` 前进）才清零；重试共用同一条墙钟预算。
        """
        self._failures += 1
        if self._failures >= _RELAY_ATTEMPTS:
            logger.warning(f"夸克分段重试放弃：{label}")
            return False
        deadline = self._retry_deadline or (time.monotonic() + _RELAY_RETRY_BUDGET)
        self._retry_deadline = deadline
        if time.monotonic() >= deadline:
            return False
        try:
            _relay_backoff(deadline, self._failures)
            self._open_window()
        except (OSError, _RelayAbort) as error:
            logger.warning(f"夸克分段重试失败：{label} 第 {self._failures} 次：{error}")
            return False
        return time.monotonic() < deadline

    def read(self, size: int = _RELAY_READ_SIZE) -> bytes:
        if self._closed or size == 0:
            return b""
        while True:
            if self._upstream is None or not self._remaining:
                if not self._remaining:
                    if self._offset > self._end:
                        return b""  # 正常 EOF
                    if self._offset == self._window_end + 1:
                        # 上一个分片恰好读完：推进到下一个分片窗口（保持与起点对齐的网格）
                        self._window_end = min(self._window_end + _RELAY_CHUNK_SIZE, self._end)
                self._open_window()
            want = min(max(1, size), self._remaining, self._end - self._offset + 1)
            if want <= 0:
                return b""
            try:
                data = self._upstream.read(want)
            except Exception as error:  # noqa: BLE001 - 传输层异常统一重试
                self._drop_upstream()
                label = f"读取中断 offset={self._offset} {type(error).__name__}"
                logger.warning(f"夸克分段{label}，将重试")
                if not self._retry(label):
                    raise OSError("夸克分段响应提前结束") from None
                continue
            if not data:  # 提前 EOF（对端正常关闭）
                self._drop_upstream()
                label = f"响应提前结束 offset={self._offset}"
                logger.warning(f"夸克分段{label}，将重试")
                if not self._retry(label):
                    raise OSError("夸克分段响应提前结束") from None
                continue
            self._remaining -= len(data)
            self._offset += len(data)
            self._failures = 0
            self._retry_deadline = None
            return data

    def close(self) -> None:
        self._closed = True
        self._drop_upstream()


class _PrefetchReader:
    """单生产者预取缓冲：在上游流外侧加一层有界队列，吸收播放器消费的节奏抖动。

    生产者线程持续按固定片读取上游，消费者 ``read`` 从队列取片；一个片即一次
    上游读取的结果（可能比请求长度短），调用方按片消费即可。异常对象作为哨兵
    入队，由消费端原样抛出；``b""`` 为正常 EOF 哨兵。``close`` 立即停止生产并
    关闭底层流，生产者卡在 ``put`` 时靠超时轮询 ``_closed`` 自行退出（守护线程）。
    """

    _PUT_POLL = 0.5   # 生产者 put 轮询 _closed 的间隔（秒）
    _GET_POLL = 0.5   # 消费者 get 轮询 _closed 的间隔（秒）

    def __init__(self, upstream: Any):
        self._upstream = upstream
        self._queue: Queue = Queue(maxsize=_RELAY_PREFETCH_ITEMS)
        self._closed = False
        self._worker = Thread(target=self._produce, name="strm-prefetch", daemon=True)

    @classmethod
    def wrap(cls, upstream: Any) -> "_PrefetchReader":
        """包装上游流并启动生产者线程。"""
        reader = cls(upstream)
        reader._worker.start()
        return reader

    def _produce(self) -> None:
        # 生产者只读不关：关闭统一由 close() 收尾，避免与消费端竞态。
        while not self._closed:
            try:
                chunk = self._upstream.read(_RELAY_READ_SIZE)
            except Exception as error:  # noqa: BLE001 - 传输层异常原样转交消费端
                if not self._closed:
                    self._put(error)
                return
            self._put(chunk if chunk else b"")
            if not chunk:
                return

    def _put(self, item: Any) -> None:
        while not self._closed:
            try:
                self._queue.put(item, timeout=self._PUT_POLL)
                return
            except Full:
                continue

    def read(self, size: int = _RELAY_READ_SIZE) -> bytes:
        while True:
            if self._closed:
                return b""
            try:
                item = self._queue.get(timeout=self._GET_POLL)
            except Empty:
                continue
            if isinstance(item, BaseException):
                raise item
            return item  # b"" 即 EOF

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._upstream.close()
        except Exception as error:  # noqa: BLE001 - 关闭失败不影响响应收尾
            logger.debug(f"STRM 预取缓冲关闭上游异常：{error}")
        # 清空未消费的缓冲，让卡在 put 的生产者立即退出
        while True:
            try:
                self._queue.get_nowait()
            except Empty:
                break


def content_disposition(file_name: str) -> str:
    """按 RFC 5987 生成兼容中文文件名的下载头。"""
    name = str(file_name or "").strip() or "download"
    try:
        name.encode("ascii")
        return f'attachment; filename="{name}"'
    except UnicodeEncodeError:
        return f"attachment; filename*=UTF-8''{quote(name, safe='')}"


@dataclass(frozen=True)
class ProxyResult:
    """代理响应：跳转、错误 JSON，或需要调用方关闭的上游流。"""

    status_code: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    upstream: Any = field(default=None, repr=False, compare=False)


def error_result(message: str, status_code: int) -> ProxyResult:
    body = json.dumps({"code": -1, "msg": message, "data": None}, ensure_ascii=False)
    return ProxyResult(status_code=status_code, body=body.encode("utf-8"))


class StrmProxyApi(OwnerDelegator):
    """解析 STRM 请求目标并返回网盘直链（插件路由与独立监听共用）。"""

    _NAME_PARAMS = ("name", "file_name", "filename")
    _VIDEO_SUFFIXES = (".mkv", ".mp4", ".ts", ".iso", ".avi", ".m2ts", ".mov", ".flv", ".webm")

    def route_segment(self) -> str:
        """短链路由段（可配置，默认 ``d``）。"""
        config = getattr(self, "_applied_config", None) or {}
        return StrmGenerator.proxy_route_segment(config.get("strm_proxy_route", ""))

    @property
    def _proxy_enabled(self) -> bool:
        config = getattr(self, "_applied_config", None) or {}
        return bool(config.get("strm_proxy_enabled", False))

    def api_strm_port_check(self, port: int = 0) -> Dict[str, Any]:
        """配置页即时检测：端口是否可用、是否为本插件监听占用。"""
        result = check_port(port)
        status = getattr(self, "_strm_proxy_status", None) or {}
        logger.debug(
            f"STRM 代理端口检测：port={port} available={result['available']} ours={result['ours']}"
        )
        return {
            "success": True,
            "data": {
                "port": int(port or 0),
                "available": bool(result["available"]),
                "error": str(result["error"] or ""),
                "ours": bool(result["ours"]),
                "running": bool(status.get("running")),
                "running_port": int(status.get("port", 0) or 0),
                "running_error": str(status.get("error") or ""),
            },
        }

    def handle_request(
            self, params: Mapping[str, Any], method: str = "GET",
            request_headers: Optional[Mapping[str, str]] = None,
    ) -> ProxyResult:
        """按参数解析文件；夸克保留取链认证头，其余网盘继续跳转。"""
        if not self._proxy_enabled:
            return error_result("STRM 播放代理未启用", status.HTTP_503_SERVICE_UNAVAILABLE)
        downloader = self._file_downloader()
        if not downloader:
            return error_result("当前网盘不支持直链解析", status.HTTP_503_SERVICE_UNAVAILABLE)
        file_item = self._resolve_file(params)
        if not file_item:
            return error_result("未找到目标文件", status.HTTP_404_NOT_FOUND)
        try:
            url, download_headers = downloader.resolve_download_link(file_item)
        except Exception as error:
            logger.warning(f"STRM 播放代理解析直链失败：{file_item.name} - {error}")
            return error_result(
                f"解析直链失败：{error}", status.HTTP_502_BAD_GATEWAY
            )
        url = str(url or "").strip()
        if not url:
            return error_result("网盘未返回可用直链", status.HTTP_502_BAD_GATEWAY)
        drive_key = str(getattr(self, "_cloud_drive_key", "") or
                        (getattr(self, "_applied_config", None) or {}).get("cloud_drive", ""))
        if drive_key == "quark":
            def resolver(_downloader=downloader, _item=file_item):
                return _downloader.resolve_download_link(_item)
            return self._relay_result(url, download_headers, method, request_headers, resolver)
        logger.info(f"STRM 播放代理跳转：{file_item.name} -> {url.split('?')[0]}")
        return self._redirect_result(url, file_item.name, method)

    def _relay_result(self, url: str, download_headers: Mapping[str, str], method: str,
                      request_headers: Optional[Mapping[str, str]],
                      resolver: Optional[Callable[..., Tuple[str, Mapping[str, str]]]] = None
                      ) -> ProxyResult:
        """有界内存转发，认证只发给上游，允许播放器定位字节范围。"""
        method = str(method).upper()
        if method not in {"GET", "HEAD"}:
            return ProxyResult(status_code=405, headers={"Allow": "GET, HEAD"})
        link = _LinkResolver(url, _relay_request_headers(download_headers), resolver)
        client_headers = {k.lower(): v for k, v in (request_headers or {}).items()}
        range_value = client_headers.get("range", "")
        range_match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_value.strip(), re.IGNORECASE)
        segmented = method == "GET" and (not range_value or bool(range_match))
        start, requested_end, part_end = 0, None, 0
        if segmented:
            if range_match:
                start = int(range_match.group(1))
                requested_end = int(range_match.group(2)) if range_match.group(2) else None
            if requested_end is not None and requested_end < start:
                segmented = False  # 无效范围交由上游按原样返回 416
            else:
                part_end = start + _RELAY_CHUNK_SIZE - 1
                if requested_end is not None:
                    part_end = min(part_end, requested_end)
        extra: Dict[str, str] = {}
        if client_headers.get("if-range"):
            extra["If-Range"] = client_headers["if-range"]
        if range_value:
            extra["Range"] = range_value
        if segmented:
            extra["Range"] = f"bytes={start}-{part_end}"
            try:
                upstream, status = _relay_open_segment(link, extra)
            except _RelayAbort as error:
                logger.warning(f"夸克播放中转连接失败：{error}")
                return error_result("夸克播放中转连接失败", 502)
        else:
            headers = {**link.headers, **extra}
            try:
                upstream = urlopen(Request(url, headers=headers, method=method), timeout=30)
                status = upstream.status
            except HTTPError as error:
                upstream, status = error, error.code
            except Exception as error:  # noqa: BLE001 - 异常文本可能含带签名 URL，不回显
                logger.warning(f"夸克播放中转连接失败：{type(error).__name__}")
                return error_result("夸克播放中转连接失败", 502)
        response_headers = {k: v for k, v in upstream.headers.items()
                            if k.lower() in _KEEP_RESPONSE_HEADERS}
        response_headers["Cache-Control"] = "no-store"
        if segmented and status == 206:
            match = _CONTENT_RANGE.fullmatch(upstream.headers.get("Content-Range", ""))
            if not match:
                upstream.close()
                return error_result("夸克分段响应缺少有效范围", 502)
            actual_start, actual_end, total = map(int, match.groups())
            end = min(requested_end, total - 1) if requested_end is not None else total - 1
            if actual_start != start or actual_end != min(part_end, total - 1) or end < start:
                upstream.close()
                return error_result("夸克分段响应范围不匹配", 502)
            response_headers = {k: v for k, v in response_headers.items()
                                if k.lower() not in {"content-length", "content-range"}}
            response_headers["Content-Length"] = str(end - start + 1)
            if range_value:
                response_headers["Content-Range"] = f"bytes {start}-{end}/{total}"
            validator = upstream.headers.get("ETag", "")
            if not validator or validator.startswith("W/"):
                validator = upstream.headers.get("Last-Modified")
            stream = _PrefetchReader.wrap(_RangeStream(
                upstream, link, start, actual_end, end, total,
                if_range=validator or ""))
            return ProxyResult(status_code=206 if range_value else 200,
                               headers=response_headers, upstream=stream)
        return ProxyResult(status_code=status, headers=response_headers, upstream=upstream)

    def short_params(self, path: str, query: Any = None) -> Dict[str, str]:
        """把短链 ``/d/<标识>``（含 CloudDrive2 风格 ``?/<文件名>``）解析为查询参数。"""
        params = {str(key): str(value) for key, value in dict(query or {}).items()}
        raw = unquote(urlparse(str(path or "")).path).strip("/")
        segment = self.route_segment()
        if raw == segment:
            raw = ""
        elif raw.startswith(f"{segment}/"):
            raw = raw[len(segment) + 1:]
        identifier = self._proxy_identifier()
        if identifier and raw and "/" not in raw and identifier not in params:
            params[identifier] = raw
        if not self._param(params, self._NAME_PARAMS):
            name = self._name_from_query_keys(params)
            if name:
                params["name"] = name
        return params

    @classmethod
    def _name_from_query_keys(cls, params: Mapping[str, str]) -> str:
        """从 ``?/文件名`` 形式（键为路径、值为空）中恢复文件名。"""
        for key, value in params.items():
            candidate = key if "/" in key or key.lower().endswith(cls._VIDEO_SUFFIXES) else ""
            if candidate and not value:
                return candidate.lstrip("/")
        return ""

    def _resolve_file(self, params: Any) -> Optional[CloudFile]:
        """按网盘标识定位文件（短链代理仅支持标识直取链的网盘）。"""
        return self._fast_file(params, self._param(params, self._NAME_PARAMS))

    def _fast_file(self, params: Any, name: str) -> Optional[CloudFile]:
        """按网盘声明的播放标识（如 115 的 pickcode）免目录扫描解析。"""
        identifier = self._proxy_identifier()
        value = str(params.get(identifier) or "").strip() if identifier else ""
        if not value:
            return None
        return CloudFile(
            id=value,
            name=name or value,
            is_directory=False,
            playback_values={identifier: value},
        )

    @staticmethod
    def _param(params: Any, keys: Tuple[str, ...]) -> str:
        for key in keys:
            value = str(params.get(key) or "").strip()
            if value:
                return value
        return ""

    def _proxy_identifier(self) -> str:
        """当前网盘可用于短链定位的播放标识变量（无则按路径定位）。"""
        reference = self._capability(CloudDriveCapability.PLAYBACK_REFERENCE)
        return str(getattr(reference, "proxy_identifier", "") or "").strip()

    def _capability(self, capability: CloudDriveCapability) -> Any:
        """从当前网盘提供方取能力对象，插件级缓存不可用时也能工作。"""
        drive = getattr(self, "_cloud_drive", None)
        if not drive or not drive.supports(capability):
            return None
        try:
            return drive.require(capability)
        except Exception as error:
            logger.debug(f"获取网盘能力 {capability.value} 失败：{error}")
            return None

    def _file_downloader(self) -> Any:
        return self._capability(CloudDriveCapability.FILE_DOWNLOAD)

    @staticmethod
    def _redirect_result(url: str, file_name: str, method: str = "GET") -> ProxyResult:
        headers = {
            "Location": quote(url, safe=":/?#[]@!$&'()*+,;=%"),
            "Cache-Control": "no-store",
            "Content-Disposition": content_disposition(file_name),
        }
        body = b"" if str(method).upper() == "HEAD" else json.dumps(
            {"status": "redirecting", "file_name": file_name, "url": url},
            ensure_ascii=False,
        ).encode("utf-8")
        return ProxyResult(status_code=status.HTTP_302_FOUND, headers=headers, body=body)


# 进程级注册表（存 sys.modules，热重载后仍可先关旧监听再重新监听）
_REGISTRY_KEY = "cloudsubscribe.strm_proxy.listeners"


def _listener_registry() -> Any:
    holder = sys.modules.get(_REGISTRY_KEY)
    if holder is None:
        holder = types.ModuleType(_REGISTRY_KEY)
        holder.listeners = {}
        holder.lock = RLock()
        sys.modules[_REGISTRY_KEY] = holder
    return holder


def check_port(port: int) -> Dict[str, Any]:
    """检测端口可用性：能绑定即空闲；无法绑定则给出系统错误原因。"""
    try:
        port_number = int(port or 0)
    except (TypeError, ValueError):
        port_number = 0
    if port_number <= 0 or port_number > 65535:
        return {"available": False, "error": "端口不合法", "ours": False}
    registry = _listener_registry()
    with registry.lock:
        ours = registry.listeners.get(port_number)
    if ours is not None and getattr(ours, "running", False):
        return {"available": True, "error": "", "ours": True}
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port_number))
        return {"available": True, "error": "", "ours": False}
    except OSError as error:
        return {"available": False, "error": str(error) or "端口被占用", "ours": False}
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


class StrmProxyServer:
    """可自定义端口的 STRM 代理监听：仅服务短链 ``/<路由>/<标识>``。"""

    last_error: str = ""

    def __init__(self, owner: Any, port: int, host: str = "0.0.0.0"):
        self._api = StrmProxyApi(owner)
        self._port = int(port or 0)
        self._host = host
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[Thread] = None
        self._lock = RLock()

    @classmethod
    def acquire(cls, owner: Any, port: int) -> Optional["StrmProxyServer"]:
        """获取指定端口的监听：先关掉重载前遗留的旧监听，再按最新配置重新监听。"""
        if port <= 0:
            return None
        registry = _listener_registry()
        with registry.lock:
            previous = registry.listeners.pop(port, None)
        if previous is not None:
            try:
                previous.stop()
            except Exception as error:
                logger.debug(f"关闭旧代理监听异常：{error}")
        server = cls(owner, port)
        if not server.start():
            return None
        with registry.lock:
            registry.listeners[port] = server
        return server

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> bool:
        """启动监听；端口不可用时记录告警并返回 False（插件路由仍可用）。"""
        with self._lock:
            if self._httpd is not None or self._port <= 0:
                return self._httpd is not None
            handler = self._make_handler()
            try:
                self._httpd = ThreadingHTTPServer((self._host, self._port), handler)
            except OSError as error:
                StrmProxyServer.last_error = str(error)
                logger.error(f"STRM 播放代理监听启动失败（{self._host}:{self._port}）：{error}")
                self._httpd = None
                return False
            self._thread = Thread(
                target=self._httpd.serve_forever, name="strm-proxy", daemon=True
            )
            self._thread.start()
            StrmProxyServer.last_error = ""
            logger.info(
                f"STRM 播放代理监听已启动：http://{self._host}:{self._port}"
                f"/{self._api.route_segment()}/<标识>"
            )
            return True

    def stop(self) -> None:
        with self._lock:
            httpd, self._httpd = self._httpd, None
            thread, self._thread = self._thread, None
            if not httpd:
                return
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception as error:
                logger.debug(f"STRM 播放代理监听关闭异常：{error}")
            if thread:
                thread.join(timeout=3)
            logger.info(f"STRM 播放代理监听已停止：端口 {self._port}")
        registry = _listener_registry()
        with registry.lock:
            if registry.listeners.get(self._port) is self:
                registry.listeners.pop(self._port, None)

    def _make_handler(self):
        server = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "CloudSubscribeStrmProxy"

            def do_GET(self) -> None:
                self._dispatch("GET")

            def do_HEAD(self) -> None:
                self._dispatch("HEAD")

            def do_POST(self) -> None:
                self._dispatch("POST")

            def _dispatch(self, method: str) -> None:
                parsed = urlparse(self.path)
                segment = server._api.route_segment()
                first = parsed.path.strip("/").split("/", 1)[0]
                if first == segment:
                    result = server._api.handle_request(
                        server._api.short_params(parsed.path, _query_pairs(parsed.query)),
                        method,
                        self.headers,
                    )
                else:
                    result = error_result("未找到目标文件", status.HTTP_404_NOT_FOUND)
                self._respond(result, method)

            def _respond(self, result: ProxyResult, method: str) -> None:
                if result.upstream is not None:
                    self._relay(result, method)
                    return
                body = b"" if method == "HEAD" else result.body
                self.send_response(result.status_code)
                for key, value in result.headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _relay(self, result: ProxyResult, method: str) -> None:
                # 上游无长度时以关闭连接结束响应，禁止错误地复用连接。
                self.close_connection = True
                try:
                    self.send_response(result.status_code)
                    for key, value in result.headers.items():
                        self.send_header(key, value)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if method != "HEAD":
                        while True:
                            try:
                                chunk = result.upstream.read(256 * 1024)
                                if not chunk:
                                    break
                            except Exception as error:  # noqa: BLE001 - 上游读取失败（含重试耗尽）
                                # 上游彻底失败才说话；客户端断开不算事故。
                                logger.warning(f"夸克播放中转读取中断：{type(error).__name__}")
                                return
                            try:
                                self.wfile.write(chunk)
                            except (BrokenPipeError, ConnectionResetError):
                                return  # 播放器探测、跳播或停止时正常断开。
                except (BrokenPipeError, ConnectionResetError):
                    return  # 响应头阶段客户端断开，同样属正常。
                except Exception as error:
                    logger.warning(f"夸克播放中转响应异常：{type(error).__name__}")
                finally:
                    result.upstream.close()

            def log_message(self, fmt: str, *args: Any) -> None:
                logger.debug(f"STRM 播放代理：{fmt % args}")

        return _Handler


def _query_pairs(query: str) -> Dict[str, Any]:
    """解析查询串；保留 ``?/文件名`` 这类以路径为键的形式。"""
    pairs: Dict[str, Any] = {}
    for chunk in str(query or "").split("&"):
        if not chunk:
            continue
        key, _, value = chunk.partition("=")
        pairs[unquote(key)] = unquote(value)
    return pairs
