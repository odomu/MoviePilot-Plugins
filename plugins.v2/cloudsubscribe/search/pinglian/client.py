"""盘链网页登录、资源查询与分享链接解析。"""

import html
import re
import threading
import time
from html.parser import HTMLParser
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.log import logger

from ..http_client import (
    RequestGate,
    gated_idempotent_request,
    gated_request,
    normalize_proxies,
    requests,
)
from ..matching import extract_year, title_matches, unique_texts


class PinglianError(RuntimeError):
    """盘链登录、查询或链接解析失败。"""

    def __init__(self, message: str, code: str = "pinglian_error"):
        super().__init__(message)
        self.code = code


class _ProfileParser(HTMLParser):
    """只读取个人中心账户卡片中的稳定 class 字段。"""

    _DIRECT_FIELDS = {
        "pf-username": "name",
        "vip-badge": "level",
        "pf-reg-date": "registered_at",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.result: Dict[str, Any] = {"details": {}}
        self._capture = ""
        self._capture_tag = ""
        self._buffer: List[str] = []
        self._card_depth = 0
        self._card: Dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        classes = set(str(values.get("class") or "").split())
        if tag == "div" and "stat-card" in classes and not self._card_depth:
            self._card_depth = 1
            self._card = {}
        elif tag == "div" and self._card_depth:
            self._card_depth += 1

        capture = next((value for key, value in self._DIRECT_FIELDS.items()
                        if key in classes), "")
        if values.get("id") == "profileCoinCount":
            capture = "points"
        if self._card_depth and "stat-label" in classes:
            capture = "card_label"
        elif self._card_depth and "stat-value" in classes:
            capture = "card_value"
        if capture and not self._capture:
            self._capture = capture
            self._capture_tag = tag
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capture and tag == self._capture_tag:
            value = re.sub(r"\s+", " ", " ".join(self._buffer)).strip()
            if self._capture.startswith("card_"):
                self._card[self._capture.removeprefix("card_")] = value
            elif value and not self.result.get(self._capture):
                self.result[self._capture] = value
            self._capture = ""
            self._capture_tag = ""
            self._buffer = []
        if tag == "div" and self._card_depth:
            self._card_depth -= 1
            if not self._card_depth:
                label = self._card.get("label", "")
                value = self._card.get("value", "")
                if label and value:
                    self.result["details"][label] = value
                self._card = {}


class _RedirectParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.target = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag != "a" or self.target:
            return
        values = dict(attrs)
        if values.get("id") == "jumpBtn":
            self.target = str(values.get("href") or "").strip()


class PinglianClient:
    BASE_URL = "https://pinglian.lol"
    _SESSION_DATA_KEY = "pinglian_auth_session"
    _LOGIN_LOCK = threading.RLock()
    _TYPE_ALIASES = {
        "115": "115",
        "123": "123",
        "quark": "quark",
        "aliyun": "alipan",
        "alipan": "alipan",
        "tianyi": "tianyi",
        "guangya": "guangya",
        "baidu": "baidu",
    }
    _TYPE_HOSTS = {
        "115": {"115.com", "115cdn.com"},
        "123": {"123pan.com", "123pan.cn", "123684.com", "123865.com"},
        "quark": {"quark.cn"},
        "alipan": {"alipan.com", "aliyundrive.com"},
        "tianyi": {"cloud.189.cn"},
        "guangya": {"guangyapan.com"},
        "baidu": {"pan.baidu.com"},
    }
    _HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
    }

    def __init__(
            self,
            username: str,
            password: str,
            proxy: Any = None,
            request_timeout: int = 30,
            request_interval: float = 1.0,
            get_data_func: Optional[Callable] = None,
            save_data_func: Optional[Callable] = None,
    ):
        self.base_url = self.BASE_URL
        self.username = str(username or "").strip()
        self.password = str(password or "")
        self._proxies = normalize_proxies(proxy)
        self._request_timeout = max(5, min(int(request_timeout or 30), 120))
        self._session = requests.Session(impersonate="chrome")
        self._session.headers.update(self._HEADERS)
        self._request_gate = RequestGate.shared(
            "盘链",
            f"{self.base_url}|{self.username.casefold()}|{self._proxies}",
            request_interval=request_interval, minimum_interval=0.5
        )
        self._get_data_func = get_data_func
        self._save_data_func = save_data_func
        self._lock = threading.RLock()
        self._authenticated = False
        self._restore_session()

    @property
    def is_configured(self) -> bool:
        return bool(self.username and self.password)

    @staticmethod
    def _is_json(response) -> bool:
        return "application/json" in str(
            response.headers.get("content-type") or ""
        ).casefold()

    def _restore_session(self) -> None:
        if not self._get_data_func:
            return
        try:
            data = self._get_data_func(self._SESSION_DATA_KEY) or {}
            if (
                    not isinstance(data, dict)
                    or str(data.get("username") or "").strip() != self.username
            ):
                return
            cookies = data.get("cookies") or {}
            if isinstance(cookies, dict):
                for name, value in cookies.items():
                    if str(name or "").strip() and str(value or ""):
                        self._session.cookies.set(str(name), str(value))
                self._authenticated = bool(cookies)
                if cookies:
                    logger.debug("盘链已恢复持久化登录状态")
        except Exception as error:
            logger.debug(f"盘链恢复持久化登录状态失败：{error}")

    def _save_session(self) -> None:
        if not self._save_data_func:
            return
        try:
            cookies = self._session.cookies.get_dict()
            self._save_data_func(
                self._SESSION_DATA_KEY,
                {
                    "username": self.username,
                    "cookies": cookies,
                    "updated_at": int(time.time()),
                } if cookies else {},
            )
        except Exception as error:
            logger.debug(f"盘链持久化登录状态失败：{error}")

    def _clear_session(self) -> None:
        self._authenticated = False
        self._session.cookies.clear()
        self._save_session()

    def _login(self, force: bool = False) -> None:
        if not self.is_configured:
            raise PinglianError("盘链账号或密码未配置", "pinglian_not_configured")
        if self._authenticated and not force:
            return
        with self._LOGIN_LOCK:
            if self._authenticated and not force:
                return
            if force:
                self._clear_session()
            try:
                response = gated_request(
                    self._request_gate,
                    self._session.post,
                    f"{self.base_url}/api/login.php",
                    data={
                        "username": self.username,
                        "password": self.password,
                        "remember": "on",
                    },
                    headers={
                        "Origin": self.base_url,
                        "Referer": f"{self.base_url}/pages/login.php",
                    },
                    proxies=self._proxies,
                    timeout=(8, self._request_timeout),
                )
            except requests.exceptions.RequestException as error:
                raise PinglianError(
                    f"盘链登录失败：{type(error).__name__}", "pinglian_login_failed"
                ) from error
            if response.status_code != 200 or not self._is_json(response):
                raise PinglianError(
                    f"盘链登录失败（HTTP {response.status_code}）",
                    "pinglian_login_failed",
                )
            try:
                payload = response.json()
            except ValueError as error:
                raise PinglianError(
                    "盘链登录响应格式异常", "pinglian_schema_changed"
                ) from error
            if not isinstance(payload, dict) or not payload.get("success"):
                raise PinglianError(
                    str((payload or {}).get("message") or "盘链账号或密码错误"),
                    "pinglian_login_failed",
                )
            self._authenticated = True
            self._save_session()

    def _request(self, method: str, path: str, retry_auth: bool = True, **kwargs):
        self._login()
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Referer", f"{self.base_url}/")
        try:
            response = gated_idempotent_request(
                self._request_gate,
                self._session.request,
                method,
                f"{self.base_url}{path}",
                retry_connection_errors=False,
                headers=headers,
                proxies=self._proxies,
                timeout=(8, self._request_timeout),
                **kwargs,
            )
        except requests.exceptions.RequestException as error:
            raise PinglianError(
                f"盘链请求失败：{type(error).__name__}", "pinglian_request_failed"
            ) from error
        auth_failed = response.status_code == 401
        payload = None
        if self._is_json(response):
            try:
                payload = response.json()
            except ValueError:
                payload = None
            auth_failed = auth_failed or (
                    isinstance(payload, dict)
                    and str(payload.get("code") or "") == "-1"
            )
        else:
            response_path = str(urlparse(str(response.url or "")).path or "")
            auth_failed = auth_failed or response_path.endswith("/pages/login.php")
        if auth_failed and retry_auth:
            self._clear_session()
            self._login(force=True)
            return self._request(method, path, retry_auth=False, **kwargs)
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after") or ""
            try:
                cooldown = max(60, min(600, int(float(retry_after))))
            except (TypeError, ValueError):
                cooldown = 60
            self._request_gate.activate_cooldown(
                cooldown, status=429, reason="盘链 HTTP 429"
            )
            raise PinglianError("盘链请求过于频繁，请稍后重试", "pinglian_rate_limited")
        if response.status_code >= 400:
            raise PinglianError(
                f"盘链请求失败（HTTP {response.status_code}）",
                "pinglian_request_failed",
            )
        return response, payload

    def request_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict:
        response, payload = self._request("GET", path, params=params)
        if not self._is_json(response) or not isinstance(payload, dict):
            raise PinglianError(
                "盘链返回了非 JSON 页面，接口可能已改版", "pinglian_schema_changed"
            )
        return payload

    @staticmethod
    def _video_title(row: Dict[str, Any]) -> str:
        """移除盘链候选标题末尾与 vod_year 重复的年份。"""
        name = str(row.get("vod_name") or "").strip()
        row_year = extract_year(row.get("vod_year"))
        if not name or not row_year:
            return name
        normalized = re.sub(
            rf"[\s（(【\[]*{re.escape(row_year)}[）)】\]]*$", "", name
        ).strip()
        return normalized or name

    @staticmethod
    def _select_video(
            rows: Iterable[Dict[str, Any]], titles: List[str], year: str
    ) -> Optional[Dict[str, Any]]:
        best = None
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            name = PinglianClient._video_title(row)
            if not title_matches(name, titles):
                continue
            row_year = extract_year(row.get("vod_year"))
            if year and row_year and row_year != year:
                continue
            exact = any(name.casefold() == title.casefold() for title in titles)
            score = (200 if exact else 100) + (50 if year and row_year == year else 0)
            ranked = (score, -index, row)
            if best is None or (score, -index) > (best[0], best[1]):
                best = ranked
        return best[2] if best is not None else None

    @staticmethod
    def _host_matches(host: str, domains: Iterable[str]) -> bool:
        return any(host == domain or host.endswith(f".{domain}") for domain in domains)

    @classmethod
    def _target_type(cls, target: str) -> str:
        host = str(urlparse(target).hostname or "").casefold()
        for resource_type, domains in cls._TYPE_HOSTS.items():
            if cls._host_matches(host, domains):
                return resource_type
        return ""

    @staticmethod
    def _append_password(resource_type: str, target: str, password: str) -> str:
        password = str(password or "").strip()
        if not password:
            return target
        parsed = urlparse(target)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        key = {
            "115": "password", "123": "pwd", "guangya": "code", "baidu": "pwd"
        }.get(resource_type)
        if key and key not in query:
            query[key] = password
            return urlunparse(parsed._replace(query=urlencode(query)))
        if resource_type in {"quark", "alipan", "tianyi"}:
            return f"{target} 提取码: {password}"
        return target

    def _resolve_token(self, token: str, expected_type: str) -> str:
        response, _ = self._request(
            "GET", "/api/go.php", params={"t": token}, allow_redirects=False
        )
        target = str(response.headers.get("location") or "").strip()
        if not target and response.status_code == 200:
            text = str(response.text or "")
            parser = _RedirectParser()
            parser.feed(text)
            target = parser.target
            if not target:
                match = re.search(r'\btargetUrl\s*=\s*["\']([^"\']+)', text, re.I)
                target = match.group(1) if match else ""
            target = html.unescape(target).replace(r"\/", "/")
        if not target:
            raise PinglianError("盘链资源令牌未返回跳转链接", "pinglian_empty_link")
        actual_type = self._target_type(target)
        if actual_type != expected_type:
            raise PinglianError("盘链资源跳转类型异常", "pinglian_invalid_link")
        return target

    def get_account_info(self) -> Dict[str, Any]:
        """从个人中心读取账户、会员和金币信息。"""
        response, _ = self._request("GET", "/pages/profile.php")
        parser = _ProfileParser()
        parser.feed(str(response.text or ""))
        profile = parser.result
        details = profile.get("details") or {}
        name = str(profile.get("name") or "").strip()
        if not name:
            raise PinglianError("盘链个人中心缺少账户字段", "pinglian_schema_changed")
        points_text = str(profile.get("points") or "0")
        points_match = re.search(r"\d+", points_text)
        return {
            "name": name,
            "email": details.get("邮箱", ""),
            "level": details.get("会员等级") or profile.get("level", ""),
            "points": int(points_match.group()) if points_match else 0,
            "expires_at": details.get("VIP 到期", ""),
            "registered_at": str(profile.get("registered_at") or "")
            .removeprefix("注册于 ").strip(),
            "invite_count": details.get("已邀请用户", ""),
        }

    def search(
            self,
            title: str,
            alternative_titles: Iterable[str],
            year: Any,
            resource_type_order: Iterable[str],
            limit: int = 20,
            test_mode: bool = False,
    ) -> List[Dict[str, Any]]:
        titles = unique_texts((title, *alternative_titles))
        if not titles:
            return []
        allowed = list(dict.fromkeys(
            str(value).strip().casefold() for value in resource_type_order
            if str(value).strip().casefold() in self._TYPE_HOSTS
        ))
        if not allowed:
            return []
        expected_year = extract_year(year)
        with self._lock:
            video = None
            selected_keyword = ""
            for keyword in titles:
                payload = self.request_json(
                    "/api/search_suggestions.php", params={"q": keyword}
                )
                rows = payload.get("data") if payload.get("success") else []
                video = self._select_video(rows, titles, expected_year)
                if not video:
                    payload = self.request_json(
                        "/api/get_videos.php", params={"wd": keyword, "pg": 1}
                    )
                    video = self._select_video(
                        payload.get("list") or [], titles, expected_year
                    )
                if video:
                    selected_keyword = keyword
                    break
            if not video:
                return []
            vod_id = video.get("vod_id")
            payload = self.request_json(
                "/api/search_pan_links.php",
                params={"keyword": selected_keyword, "vod_id": vod_id, "_t": int(time.time() * 1000)},
            )
            groups = payload.get("data") if payload.get("success") else None
            if not isinstance(groups, dict):
                return []
            type_order = {value: index for index, value in enumerate(allowed)}
            candidates = []
            for group in groups.values():
                for row in (group.get("links") or []) if isinstance(group, dict) else []:
                    if not isinstance(row, dict):
                        continue
                    resource_type = self._TYPE_ALIASES.get(
                        str(row.get("type") or "").strip().casefold(), ""
                    )
                    token = str(row.get("token") or "").strip()
                    if resource_type not in type_order or not token:
                        continue
                    candidates.append((type_order[resource_type], row, resource_type, token))

            def user_tier(item) -> int:
                try:
                    return int(item[1].get("user_tier") or 0)
                except (TypeError, ValueError):
                    return 0

            candidates.sort(
                key=lambda item: (item[0], -user_tier(item))
            )
            if test_mode:
                grouped = {}
                for candidate in candidates:
                    grouped.setdefault(candidate[2], []).append(candidate)
                candidates = []
                offsets = {resource_type: 0 for resource_type in grouped}
                while grouped:
                    for resource_type in list(grouped):
                        rows = grouped[resource_type]
                        offset = offsets[resource_type]
                        candidates.append(rows[offset])
                        offset += 1
                        offsets[resource_type] = offset
                        if offset >= len(rows):
                            grouped.pop(resource_type)
                            offsets.pop(resource_type, None)
            results = []
            seen = set()
            for _, row, resource_type, token in candidates:
                if len(results) >= max(1, min(int(limit or 20), 80)):
                    break
                key = (resource_type, str(row.get("title") or "").strip(), token)
                if key in seen:
                    continue
                seen.add(key)
                source_url = f"{self.base_url}/pages/video.php?id={vod_id}"
                target = ""
                if not test_mode:
                    try:
                        target = self._resolve_token(token, resource_type)
                    except PinglianError as error:
                        logger.debug(f"[PINGLIAN] 跳过不可用资源：{error.code}")
                        continue
                    target = self._append_password(
                        resource_type, target, row.get("password")
                    )
                results.append({
                    "title": str(row.get("title") or "盘链资源").strip(),
                    "description": str(row.get("source") or "").strip(),
                    "url": target,
                    "resource_type": resource_type,
                    "pan_type": resource_type,
                    "update_time": str(row.get("time") or ""),
                    "source": "pinglian",
                    "source_service": "pinglian",
                    "source_url": source_url,
                    "pinglian_resource_id": str(row.get("id") or ""),
                })
            return results

    def clear_cache(self) -> Dict[str, int]:
        return {"session": int(self._authenticated)}

    def close(self) -> None:
        with self._lock:
            self._session.close()
