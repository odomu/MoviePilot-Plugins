"""Dian115 门户登录、浏览器会话与受控请求客户端。"""

import asyncio
import base64
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Callable, Dict, Optional
from urllib.parse import unquote, urljoin, urlparse, urlsplit

from app.log import logger
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from ..http_client import (
    AccountActionGate,
    CURL_CFFI_AVAILABLE,
    RequestGate,
    gated_idempotent_request,
    normalize_proxies,
    requests,
)


class Dian115Error(RuntimeError):
    """Dian115 请求或协议错误。"""

    def __init__(self, message: str, code: str = "", status_code: int = 0):
        super().__init__(message)
        self.code = str(code or "")
        self.status_code = int(status_code or 0)


class Dian115Client:
    """维护登录 Cookie、浏览器证明和全接口统一限速。"""

    BASE_URL = "https://m.dian115.com"
    _IMPERSONATE = "chrome"
    _PROOF_MARGIN_SECONDS = 15
    _BROWSER_LOGIN_TIMEOUT_SECONDS = 90
    _RISK_COOLDOWN_SECONDS = 60
    _SERVER_ERROR_COOLDOWN_SECONDS = 5
    _PORTAL_COOKIES = ("__Host-portal_token", "__Host-portal_browser")
    _SESSION_DATA_KEY = "dian115_auth_session"
    _LOGIN_LOCK = threading.RLock()

    def __init__(
            self,
            email: str,
            password: str,
            base_url: str = BASE_URL,
            proxy: Any = None,
            request_interval: float = 1.0,
            unlocks_per_minute: int = 6,
            timeout: int = 30,
            get_data_func: Optional[Callable] = None,
            save_data_func: Optional[Callable] = None,
    ):
        if not CURL_CFFI_AVAILABLE:
            raise Dian115Error(
                "Dian115 需要 curl_cffi 请安装依赖后重启",
                code="curl_cffi_missing",
            )
        self._email = str(email or "").strip()
        self.base_url = str(base_url or self.BASE_URL).rstrip("/")
        self._password = str(password or "").strip()
        self._proxies = normalize_proxies(proxy)
        self._timeout = max(5, min(int(timeout or 30), 120))
        self._visitor_id = str(uuid.uuid4())
        self._session = requests.Session(impersonate=self._IMPERSONATE)
        self._proof: Optional[tuple[str, float]] = None
        self._browser_private_key = ec.generate_private_key(ec.SECP256R1())
        self._browser_session_expires_at = 0.0
        self._server_time_offset_ms = 0
        self._authenticated = False
        self._get_data_func = get_data_func
        self._save_data_func = save_data_func
        self._lock = threading.RLock()
        self._request_gate = RequestGate.shared(
            "Dian115",
            f"{self.base_url}|{self._email.casefold()}|{self._proxies}",
            request_interval=request_interval,
            minimum_interval=0.2,
            risk_cooldown_seconds=self._RISK_COOLDOWN_SECONDS,
            server_error_cooldown_seconds=self._SERVER_ERROR_COOLDOWN_SECONDS,
            challenge_detector=self._is_challenge_response,
        )
        self._unlock_gate = AccountActionGate.shared(
            "Dian115 解锁接口",
            f"dian115:{self._email.casefold()}",
            max_actions=unlocks_per_minute,
            maximum_actions=10,
        )
        self._restore_auth_cookie()

    @property
    def is_configured(self) -> bool:
        return bool(self._email and self._password)

    def matches_config(
            self, email: str, password: str, proxy: Any,
            request_interval: float, unlocks_per_minute: int,
    ) -> bool:
        return (
                self._email == str(email or "").strip()
                and self._password == str(password or "").strip()
                and self._proxies == normalize_proxies(proxy)
                and self._request_gate.request_interval
                == max(1.0, min(float(request_interval or 1.0), 10.0))
                and self._unlock_gate.max_actions
                == max(1, min(int(unlocks_per_minute or 6), 10))
        )

    def close(self) -> None:
        with self._lock:
            self._session.close()
            self._proof = None
            self._browser_session_expires_at = 0.0
            self._authenticated = False

    def _clear_portal_cookies(self) -> None:
        for name in self._PORTAL_COOKIES:
            self._session.cookies.delete(name)
        self._browser_session_expires_at = 0.0
        self._server_time_offset_ms = 0
        self._save_auth_cookie("")

    def _restore_auth_cookie(self) -> None:
        if not self._get_data_func:
            return
        try:
            data = self._get_data_func(self._SESSION_DATA_KEY) or {}
            if (
                    not isinstance(data, dict)
                    or str(data.get("email") or "").strip().lower()
                    != self._email.lower()
            ):
                return
            token = str(data.get("token") or "").strip()
            if not token:
                return
            self._session.cookies.set(
                "__Host-portal_token",
                token,
                domain="m.dian115.com",
                path="/",
            )
            self._authenticated = True
            logger.debug("Dian115 已恢复持久化登录状态")
        except Exception as error:
            logger.debug(f"Dian115 恢复持久化登录状态失败：{error}")

    def _save_auth_cookie(self, token: str = "") -> None:
        if not self._save_data_func:
            return
        try:
            value = str(token or "").strip()
            self._save_data_func(
                self._SESSION_DATA_KEY,
                {
                    "email": self._email,
                    "token": value,
                    "updated_at": int(time.time()),
                } if value else {},
            )
        except Exception as error:
            logger.debug(f"Dian115 持久化登录状态失败：{error}")

    def _headers(self, current_path: str) -> Dict[str, str]:
        """补齐站点 browser-challenge 校验所需的 UA Client Hints。"""
        path = current_path if str(current_path).startswith("/") else "/"
        return {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "priority": "u=1, i",
            "sec-ch-ua": (
                '"Not=A?Brand";v="99", "Google Chrome";v="151", '
                '"Chromium";v="151"'
            ),
            "sec-ch-ua-arch": '"x86"',
            "sec-ch-ua-bitness": '"64"',
            "sec-ch-ua-full-version": '"151.0.7922.72"',
            "sec-ch-ua-full-version-list": (
                '"Not=A?Brand";v="99.0.0.0", '
                '"Google Chrome";v="151.0.7922.72", '
                '"Chromium";v="151.0.7922.72"'
            ),
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-model": '""',
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-platform-version": '"19.0.0"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "x-portal-current-path": path,
            "x-portal-visitor-id": self._visitor_id,
            "x-requested-with": "XMLHttpRequest",
            "referer": urljoin(f"{self.BASE_URL}/", path.lstrip("/")),
        }

    @staticmethod
    def _is_challenge_response(response) -> bool:
        content_type = str(response.headers.get("content-type") or "").lower()
        cf_mitigated = str(
            response.headers.get("cf-mitigated") or ""
        ).strip().lower()
        return cf_mitigated == "challenge" or "text/html" in content_type

    def _raw_request(self, method: str, path: str, **kwargs):
        cooldown_remaining = self._request_gate.cooldown_remaining
        if cooldown_remaining > 0:
            status = self._request_gate.cooldown_status
            raise Dian115Error(
                f"Dian115 处于风控冷却期，跳过请求"
                f"（剩余 {int(cooldown_remaining + 0.999)} 秒）",
                code=("rate_limited" if status in {0, 403, 429}
                      else "server_cooldown"),
                status_code=status,
            )
        try:
            def request():
                return gated_idempotent_request(
                    self._request_gate,
                    self._session.request,
                    method,
                    urljoin(f"{self.base_url}/", path.lstrip("/")),
                    proxies=self._proxies,
                    timeout=self._timeout,
                    **kwargs,
                )

            if (
                    str(method or "").strip().upper() == "POST"
                    and path == "/api/portal/unlock"
            ):
                return self._unlock_gate.run(request)
            return request()
        except requests.exceptions.RequestException as error:
            raise Dian115Error(f"Dian115 请求失败：{error}") from error

    @staticmethod
    def _payload(response) -> Dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise Dian115Error(
                f"Dian115 返回非 JSON 响应，HTTP {response.status_code}",
                status_code=response.status_code,
            ) from error
        if not isinstance(payload, dict):
            raise Dian115Error("Dian115 返回结构异常")
        return payload

    @classmethod
    def _raise_response_error(cls, response, payload: Dict[str, Any]) -> None:
        code = str(payload.get("code") or "")
        message = str(
            payload.get("msg") or payload.get("message")
            or f"HTTP {response.status_code}"
        )
        raise Dian115Error(message, code=code, status_code=response.status_code)

    @staticmethod
    def _base64url(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    def _browser_proof(self, current_path: str, refresh: bool = False) -> str:
        now = time.time()
        cached = self._proof
        if not refresh and cached and cached[1] > now + self._PROOF_MARGIN_SECONDS:
            return cached[0]
        headers = self._headers(current_path)
        response = self._raw_request(
            "GET", "/api/portal/auth/browser-challenge", headers=headers
        )
        payload = self._payload(response)
        proof = str(payload.get("proof") or "")
        if response.status_code != 200 or payload.get("code") != "ok" or not proof:
            self._raise_response_error(response, payload)
        ttl = max(30, int(payload.get("ttl") or 600))
        self._proof = (proof, now + ttl)
        return proof

    def _public_jwk(self) -> Dict[str, str]:
        numbers = self._browser_private_key.public_key().public_numbers()
        return {
            "kty": "EC",
            "crv": "P-256",
            "x": self._base64url(numbers.x.to_bytes(32, "big")),
            "y": self._base64url(numbers.y.to_bytes(32, "big")),
        }

    def _ensure_browser_session(
            self, current_path: str, proof: str, refresh: bool = False
    ) -> None:
        now = time.time()
        if (
                not refresh
                and self._browser_session_expires_at
                > now + self._PROOF_MARGIN_SECONDS
        ):
            return
        headers = self._headers(current_path)
        headers.update({
            "content-type": "application/json",
            "x-portal-browser-proof": proof,
        })
        response = self._raw_request(
            "POST",
            "/api/portal/auth/browser-session",
            headers=headers,
            json={"public_jwk": self._public_jwk()},
        )
        payload = self._payload(response)
        if response.status_code != 200 or payload.get("code") not in {"ok", None}:
            self._raise_response_error(response, payload)
        if payload.get("enabled") is False:
            self._browser_session_expires_at = now + 1800
            return
        server_time_ms = payload.get("server_time_ms")
        try:
            self._server_time_offset_ms = int(server_time_ms) - round(now * 1000)
        except (TypeError, ValueError):
            self._server_time_offset_ms = 0
        ttl = max(60, int(payload.get("ttl") or 1800))
        expires_at = str(payload.get("expires_at") or "").strip()
        if expires_at:
            try:
                expiry = datetime.fromisoformat(
                    expires_at.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                expiry = now + ttl
        else:
            expiry = now + ttl
        self._browser_session_expires_at = max(now + 60, expiry)

    def _browser_signature(self, method: str, api_path: str) -> Dict[str, str]:
        timestamp = str(round(time.time() * 1000 + self._server_time_offset_ms))
        nonce = self._base64url(os.urandom(24))
        path = urlsplit(str(api_path or "/")).path or "/"
        canonical = (
            "portal-browser-request/v1\n"
            f"{str(method or 'GET').strip().upper()}\n"
            f"{path}\n{timestamp}\n{nonce}"
        ).encode("utf-8")
        der_signature = self._browser_private_key.sign(
            canonical, ec.ECDSA(hashes.SHA256())
        )
        r_value, s_value = decode_dss_signature(der_signature)
        signature = self._base64url(
            r_value.to_bytes(32, "big") + s_value.to_bytes(32, "big")
        )
        return {
            "x-portal-browser-ts": timestamp,
            "x-portal-browser-nonce": nonce,
            "x-portal-browser-sig": signature,
        }

    def _authorized_headers(
            self,
            method: str,
            api_path: str,
            current_path: str,
            refresh_proof: bool = False,
    ) -> Dict[str, str]:
        headers = self._headers(current_path)
        proof = self._browser_proof(
            current_path, refresh=refresh_proof
        )
        headers["x-portal-browser-proof"] = proof
        self._ensure_browser_session(
            current_path, proof, refresh=refresh_proof
        )
        headers.update(self._browser_signature(method, api_path))
        return headers

    def _browser_proxy(self) -> Optional[Dict[str, str]]:
        proxies = self._proxies or {}
        proxy = proxies.get("https") or proxies.get("http")
        if not proxy:
            return None
        parsed = urlparse(str(proxy))
        if not parsed.scheme or not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        server = f"{parsed.scheme}://{host}"
        if parsed.port:
            server += f":{parsed.port}"
        result = {"server": server}
        if parsed.username:
            result["username"] = unquote(parsed.username)
        if parsed.password:
            result["password"] = unquote(parsed.password)
        return result

    def _login_with_browser(self) -> None:
        executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="Dian115-BrowserLogin"
        )
        try:
            executor.submit(
                asyncio.run, self._login_with_browser_async()
            ).result()
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    async def _login_with_browser_async(self) -> None:
        try:
            from app.core.config import settings
            from cloakbrowser import launch_context_async
        except ImportError as error:
            raise Dian115Error(
                "Dian115 登录需要CloakBrowser，请先准备浏览器仿真环境",
                code="browser_unavailable",
            ) from error

        context = None
        page = None
        timeout_ms = self._BROWSER_LOGIN_TIMEOUT_SECONDS * 1000
        try:
            browser_options = {
                "headless": True,
                "proxy": self._browser_proxy(),
                "humanize": getattr(settings, "CLOAKBROWSER_HUMANIZE", True),
                # Dian115 Turnstile 在 default 预设下会提交后停留登录页。
                "human_preset": "careful",
            }
            context = await launch_context_async(**browser_options)
            page = await context.new_page()
            # 浏览器导航无法直接套同步 requests 门控，先占用同一账号请求槽。
            self._request_gate.run(lambda: None)
            await page.goto(
                f"{self.BASE_URL}/login",
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            await page.wait_for_selector("input[type='email']", timeout=30000)
            await page.fill("input[type='email']", self._email)
            await page.fill("input[type='password']", self._password)
            await page.wait_for_selector(
                "input[name='cf-turnstile-response']",
                state="attached",
                timeout=30000,
            )
            widget_rect = await page.evaluate(
                """() => {
                    const input = document.querySelector(
                        'input[name="cf-turnstile-response"]'
                    );
                    const rect = input?.parentElement?.getBoundingClientRect();
                    return rect && {
                        x: rect.x, y: rect.y,
                        width: rect.width, height: rect.height
                    };
                }"""
            )
            if widget_rect and widget_rect.get("width", 0):
                await page.mouse.click(
                    widget_rect["x"] + widget_rect["width"] / 2,
                    widget_rect["y"] + widget_rect["height"] / 2,
                )
            try:
                await page.wait_for_function(
                    """() => Boolean(
                        document.querySelector(
                            'input[name="cf-turnstile-response"]'
                        )?.value
                    )""",
                    timeout=60000,
                )
            except Exception as error:
                raise Dian115Error(
                    "Dian115 Turnstile 人机验证未通过，"
                    "请检查 CloakBrowser 网络和指纹",
                    code="turnstile_failed",
                ) from error
            await page.click("button[type='submit']")
            deadline = time.monotonic() + 30
            while "/login" in str(page.url or ""):
                if time.monotonic() >= deadline:
                    raise Dian115Error(
                        "Dian115 浏览器登录后未离开登录页",
                        code="browser_login_failed",
                    )
                await page.wait_for_timeout(250)
            token_cookie = next(
                (
                    cookie for cookie in await context.cookies()
                    if cookie.get("name") == "__Host-portal_token"
                       and cookie.get("value")
                ),
                None,
            )
            if not token_cookie:
                raise Dian115Error(
                    "Dian115 浏览器登录未返回认证 Cookie",
                    code="browser_login_failed",
                )
            self._session.cookies.set(
                token_cookie["name"],
                token_cookie["value"],
                domain="m.dian115.com",
                path="/",
            )
            self._save_auth_cookie(token_cookie["value"])
            self._authenticated = True
            logger.debug("Dian115 CloakBrowser 登录成功，已更新认证 Cookie")
        except Dian115Error:
            raise
        except Exception as error:
            raise Dian115Error(
                f"Dian115 浏览器登录失败：{error}",
                code="browser_login_failed",
            ) from error
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            if context:
                try:
                    await context.close()
                except Exception:
                    pass

    def _login(self, allow_browser_login: bool = True) -> None:
        if not self.is_configured:
            raise Dian115Error("Dian115 未配置邮箱或密码")
        with self._LOGIN_LOCK:
            self._restore_auth_cookie()
            if self._authenticated:
                return
            api_path = "/api/portal/auth/login"
            try:
                headers = self._authorized_headers(
                    "POST", api_path, "/login", refresh_proof=True
                )
                headers["content-type"] = "application/json"
                response = self._raw_request(
                    "POST",
                    api_path,
                    headers=headers,
                    json={"email": self._email, "password": self._password},
                )
                payload = self._payload(response)
                if response.status_code != 200 or payload.get("code") != "ok":
                    self._raise_response_error(response, payload)
                if not payload.get("user"):
                    raise Dian115Error("Dian115 登录成功响应缺少用户信息")
                self._save_auth_cookie(
                    self._session.cookies.get_dict().get("__Host-portal_token", "")
                )
                self._authenticated = True
                logger.debug("Dian115 HTTP 登录成功")
            except Dian115Error as error:
                is_cloudflare = (
                    error.code == "turnstile_failed"
                    or (error.status_code == 403 and not error.code)
                )
                if not is_cloudflare:
                    raise
                if not allow_browser_login:
                    raise Dian115Error(
                        "Dian115 HTTP 登录触发 Cloudflare，签到禁止使用浏览器回退",
                        code="browser_login_forbidden",
                        status_code=error.status_code,
                    ) from error
                logger.debug("Dian115 登录触发 Cloudflare，切换 CloakBrowser")
                self._clear_portal_cookies()
                self._login_with_browser()

    def _request_json(
            self,
            method: str,
            api_path: str,
            current_path: str,
            retry_login: bool = True,
            allow_browser_login: bool = True,
            **kwargs,
    ) -> Dict[str, Any]:
        with self._lock:
            if not self._authenticated:
                self._login(allow_browser_login=allow_browser_login)
            headers = self._authorized_headers(method, api_path, current_path)
            supplied_headers = dict(kwargs.pop("headers", {}) or {})
            headers.update(supplied_headers)
            response = self._raw_request(method, api_path, headers=headers, **kwargs)
            payload = self._payload(response)
            if (
                    response.status_code == 200
                    and payload.get("code") in {"ok", 0, "0", None}
            ):
                return payload
            code = str(payload.get("code") or "")
            if retry_login and (
                    response.status_code in {401, 403}
                    or code in {
                        "unauthorized", "auth_required", "browser_proof_invalid"
                    }
            ):
                logger.debug(
                    f"Dian115 登录态或访问证明失效，刷新后重试：{api_path}"
                )
                self._clear_portal_cookies()
                self._authenticated = False
                self._proof = None
                self._browser_session_expires_at = 0.0
                self._login(allow_browser_login=allow_browser_login)
                return self._request_json(
                    method,
                    api_path,
                    current_path,
                    retry_login=False,
                    allow_browser_login=allow_browser_login,
                    **kwargs,
                )
            self._raise_response_error(response, payload)

    def request_json(
            self,
            method: str,
            api_path: str,
            current_path: str,
            allow_browser_login: bool = True,
            **kwargs,
    ) -> Dict[str, Any]:
        """执行带登录态和浏览器证明的门户 JSON 请求。"""
        return self._request_json(
            method,
            api_path,
            current_path,
            allow_browser_login=allow_browser_login,
            **kwargs,
        )

    def get_account_info(
            self, allow_browser_login: bool = True
    ) -> Dict[str, Any]:
        """读取当前 Dian115 账户及可用积分。"""
        payload = self.request_json(
            "GET",
            "/api/portal/me",
            "/me",
            allow_browser_login=allow_browser_login,
        )
        user = payload.get("user") if isinstance(payload, dict) else None
        if not isinstance(user, dict) or "points" not in user:
            raise Dian115Error(
                "Dian115 账户接口缺少积分字段", code="schema_changed"
            )
        try:
            points = int(user.get("points") or 0)
        except (TypeError, ValueError) as error:
            raise Dian115Error(
                "Dian115 账户积分格式异常", code="schema_changed"
            ) from error
        return {
            "name": str(
                user.get("nickname") or user.get("username")
                or user.get("email") or "Dian115 用户"
            ),
            "email": str(user.get("email") or ""),
            "username": str(user.get("username") or ""),
            "avatar": str(user.get("avatar_url") or ""),
            "points": max(0, points),
            "role": str(user.get("role") or ""),
            "is_vip": bool(user.get("vip")),
            "vip_until": str(user.get("vip_until") or ""),
            "unlock_count": max(0, int(payload.get("unlock_count") or 0)),
            "consecutive_signin": max(
                0, int(user.get("consecutive_signin") or 0)
            ),
            "created_at": str(user.get("created_at") or ""),
            "last_login_at": str(user.get("last_login_at") or ""),
        }

    @staticmethod
    def _game_item(payload: Dict[str, Any], key: str) -> Dict[str, Any]:
        items = payload.get("items") if isinstance(payload, dict) else None
        item = items.get(key) if isinstance(items, dict) else None
        if not isinstance(item, dict):
            raise Dian115Error(
                f"Dian115 娱乐状态缺少 {key} 字段", code="schema_changed"
            )
        return item

    def get_game_status(self) -> Dict[str, Any]:
        """读取每日转盘次数；签到链路禁止触发浏览器登录。"""
        return self.request_json(
            "GET",
            "/api/portal/games/status",
            "/me/lottery",
            allow_browser_login=False,
        )

    def signin(self, mode: str = "normal") -> Dict[str, Any]:
        """通过门户签到接口执行普通或运气签到。"""
        normalized_mode = str(mode or "normal").strip().lower()
        if normalized_mode not in {"normal", "lucky"}:
            raise Dian115Error("Dian115 签到模式无效", code="invalid_mode")
        try:
            payload = self.request_json(
                "POST",
                "/api/portal/signin",
                "/me/signin",
                allow_browser_login=False,
                json={"mode": normalized_mode},
            )
        except Dian115Error as error:
            if error.code != "already_signed":
                raise
            return {
                "success": True,
                "already_checked_in": True,
                "status": "今日已签到",
                "message": "今日已签到",
                "mode": normalized_mode,
                "award_points": 0,
                "status_code": error.status_code,
                "error_code": error.code,
            }
        return {
            "success": True,
            "already_checked_in": False,
            "status": "签到成功",
            "message": str(payload.get("message") or "签到成功"),
            "mode": normalized_mode,
            "award_points": payload.get("award"),
            "new_balance": payload.get("new_balance"),
            "signin_days": payload.get("streak_after"),
            "lucky_tier": payload.get("lucky_tier"),
            "multiplier": payload.get("multiplier"),
            "status_code": 200,
            "error_code": "",
        }

    def run_lottery(self, target_count: int) -> Dict[str, Any]:
        """将幸运转盘补齐到当天目标次数，目标值硬限制为 20。"""
        target_plays = max(0, min(int(target_count or 0), 20))
        wheel_results = []
        wheel_error: Optional[Dian115Error] = None
        used_before = 0
        max_plays = 20
        play_count = 0
        if target_plays:
            wheel = self._game_item(self.get_game_status(), "daily_wheel")
            try:
                used_before = max(0, int(wheel.get("used_today") or 0))
                max_plays = max(
                    0, min(int(wheel.get("max_plays") or 0), 20)
                )
            except (TypeError, ValueError) as error:
                raise Dian115Error(
                    "Dian115 转盘次数格式异常", code="schema_changed"
                ) from error
            play_count = max(
                0, min(target_plays, max_plays) - used_before
            )
            for _ in range(play_count):
                try:
                    wheel_results.append(self.request_json(
                        "POST",
                        "/api/portal/lottery/wheel",
                        "/me/lottery",
                        allow_browser_login=False,
                    ))
                except Dian115Error as error:
                    wheel_error = error
                    break
        wheel_cost = 0
        wheel_award = 0
        wheel_vip_days = 0
        for item in wheel_results:
            prize = item.get("prize") if isinstance(item, dict) else None
            prize = prize if isinstance(prize, dict) else {}
            try:
                wheel_cost += max(0, int(item.get("cost") or 0))
                wheel_award += int(prize.get("points") or 0)
                wheel_vip_days += max(0, int(prize.get("vip_days") or 0))
            except (TypeError, ValueError):
                continue
        executed = len(wheel_results)
        success = wheel_error is None
        message = f"转盘 {executed}/{target_plays} 次"
        if target_plays and executed == 0 and used_before >= target_plays:
            message = f"今日转盘已完成 {used_before} 次"
        elif wheel_error:
            message = f"{message}，中断：{wheel_error}"
        balances = [
            item.get("new_balance")
            for item in wheel_results
            if isinstance(item, dict) and item.get("new_balance") is not None
        ]
        return {
            "success": success,
            "status": "转盘完成" if success else "转盘未完成",
            "message": message,
            "new_balance": balances[-1] if balances else None,
            "points_change": wheel_award - wheel_cost,
            "status_code": int(getattr(wheel_error, "status_code", 0) or 200),
            "error_code": str(getattr(wheel_error, "code", "") or ""),
            "target_count": target_plays,
            "max_plays": max_plays,
            "used_before": used_before,
            "planned": play_count,
            "executed": executed,
            "used_after": used_before + executed,
            "cost_points": wheel_cost,
            "award_points": wheel_award,
            "vip_days": wheel_vip_days,
        }
