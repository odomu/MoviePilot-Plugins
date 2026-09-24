"""HDHaven 统一网络请求、鉴权会话与风控客户端。"""

import threading
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import urljoin

from app.log import logger

from .security import HDHavenTurnstile
from ..cloudflare import is_cloudflare_challenge
from ..http_client import (
    RequestGate,
    RequestGateCancelled,
    RequestGateCooldown,
    gated_idempotent_request,
    normalize_proxies,
    requests,
)


class HDHavenError(RuntimeError):
    """HDHaven 请求、业务或鉴权错误。"""

    def __init__(self, message: str, code: str = "", status_code: int = 0):
        super().__init__(message)
        self.code = str(code or "")
        self.status_code = int(status_code or 0)


class HDHavenClient:
    """HDHaven 接口请求与用户鉴权客户端。"""

    DEFAULT_BASE_URL = "https://hdhaven.com"
    _SESSION_DATA_KEY = "hdhaven_auth_session"

    def __init__(
            self,
            username: str = "",
            password: str = "",
            base_url: str = "",
            proxy: Any = None,
            request_interval: float = 1.0,
            get_data_func: Optional[Callable] = None,
            save_data_func: Optional[Callable] = None,
    ):
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.username = str(username or "").strip()
        self.password = str(password or "").strip()
        self.token = ""
        self.proxy = proxy
        self.request_interval = max(0.5, float(request_interval or 1.0))
        self._get_data_func = get_data_func
        self._save_data_func = save_data_func

        self._proxies = normalize_proxies(proxy)
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": f"{self.base_url}/",
            "Origin": self.base_url,
        })

        self._lock = threading.RLock()
        self._turnstile_solver: Optional[HDHavenTurnstile] = None
        self._turnstile_site_key: str = ""

        session_key = f"hdhaven:{self.username or 'default'}"
        self._session_key = session_key
        self._request_gate = RequestGate.shared(
            "HDHaven API",
            session_key,
            request_interval=self.request_interval,
            minimum_interval=1.0,
            risk_cooldown_seconds=60,
            server_error_cooldown_seconds=30,
            challenge_detector=lambda resp: is_cloudflare_challenge(
                resp.text if resp is not None else "",
                getattr(resp, "status_code", 0),
                getattr(resp, "headers", {}),
            ),
            serial_requests=True,
            max_requests_per_window=30,
            request_window_seconds=60.0,
        )
        self._restore_session()

    def _restore_session(self) -> None:
        """从数据库恢复登录 Cookie 和 Token。"""
        if not self._get_data_func:
            return
        try:
            data = self._get_data_func(self._SESSION_DATA_KEY) or {}
            if (
                    not isinstance(data, dict)
                    or str(data.get("username") or "").strip() != self.username
                    or str(data.get("base_url") or "").rstrip("/") != self.base_url
            ):
                return
            cookies = data.get("cookies") or {}
            now = time.time()
            valid = False
            for name, info in (cookies.items() if isinstance(cookies, dict) else {}):
                value = str(info.get("value") or "") if isinstance(info, dict) else str(info or "")
                expires = float(info.get("expires") or 0) if isinstance(info, dict) else 0
                if expires > 0 and expires <= now:
                    continue
                if value:
                    self._session.cookies.set(name, value)
                    valid = True
            if valid:
                self.token = data.get("token") or "restored"
                logger.debug("HDHaven 已恢复持久化登录状态")
        except Exception as error:
            logger.debug(f"HDHaven 恢复登录状态失败：{error}")

    def _save_session(self, token: str = "") -> None:
        """将登录 Cookie 和 Token 持久化到数据库。"""
        if not self._save_data_func:
            return
        try:
            cookies: Dict[str, Any] = {}
            for cookie in self._session.cookies.jar:
                cookies[cookie.name] = {
                    "value": cookie.value,
                    "expires": int(cookie.expires or 0),
                }
            self._save_data_func(
                self._SESSION_DATA_KEY,
                {
                    "username": self.username,
                    "base_url": self.base_url,
                    "token": str(token or self.token or ""),
                    "cookies": cookies,
                    "updated_at": int(time.time()),
                } if cookies else {},
            )
        except Exception as error:
            logger.debug(f"HDHaven 持久化登录状态失败：{error}")

    @property
    def cooldown_remaining(self) -> float:
        return self._request_gate.cooldown_remaining

    def login(self, force: bool = False) -> str:
        if self.token and not force:
            return self.token
        if not self.username or not self.password:
            raise HDHavenError("HDHaven 登录缺少用户名或密码", code="missing_credentials")

        remaining = self.cooldown_remaining
        if remaining > 0:
            raise HDHavenError(
                f"HDHaven 处于风控冷却中（剩余 {remaining:.0f}s），暂缓登录",
                code="rate_limited",
            )

        if not self._turnstile_site_key:
            try:
                conf = self.request("GET", "/api/auth/registration-config", auto_login=False)
                if conf.status_code == 200:
                    self._turnstile_site_key = str(
                        (conf.json().get("data") or {}).get("turnstile_site_key") or ""
                    ).strip()
            except Exception as err:
                logger.debug(f"HDHaven 获取 registration-config 失败：{err}")

        if not self._turnstile_site_key:
            raise HDHavenError("HDHaven 无法获取 Cloudflare site key", code="site_key_not_found")

        if self._turnstile_solver is None:
            self._turnstile_solver = HDHavenTurnstile(base_url=self.base_url, proxy=self.proxy)

        try:
            cf_token = self._turnstile_solver.token(site_key=self._turnstile_site_key, action="login", timeout=40)
        except Exception as err:
            self._request_gate.activate_cooldown(30, reason="Turnstile求解失败")
            raise HDHavenError(f"HDHaven 人机验证获取失败：{err}", code="turnstile_failed") from err

        payload = {
            "username": self.username,
            "password": self.password,
            "turnstile_token": cf_token,
        }

        try:
            resp = gated_idempotent_request(
                self._request_gate,
                self._session.request,
                "POST",
                urljoin(self.base_url, "/api/auth/login"),
                json=payload,
                proxies=self._proxies,
                timeout=15,
                retry_connection_errors=False,
            )
        except Exception as err:
            raise HDHavenError(f"HDHaven 登录请求网络异常：{err}") from err

        if is_cloudflare_challenge(resp.text, resp.status_code, resp.headers):
            self._request_gate.activate_cooldown(120, status=resp.status_code, reason="登录触发CF防护")
            raise HDHavenError("HDHaven 登录触发 Cloudflare 安全验证", code="cf_blocked")

        try:
            data = resp.json()
        except ValueError as err:
            raise HDHavenError("HDHaven 登录响应数据非有效 JSON", status_code=resp.status_code) from err

        if resp.status_code not in (200, 201) or not data.get("success"):
            msg = data.get("message") or "用户名或密码错误"
            raise HDHavenError(f"HDHaven 登录失败：{msg}", code="login_failed", status_code=resp.status_code)

        session_cookie = self._session.cookies.get("hdh_session")
        user_info = (data.get("data") or {}).get("user") or {}
        token = session_cookie or str(user_info.get("id") or "logged_in")
        self.token = token
        self._session.headers.pop("Authorization", None)
        self._save_session(token)
        logger.debug(f"HDHaven 登录成功：user={user_info.get('nickname') or user_info.get('id') or self.username}")
        return token

    def request(
            self,
            method: str,
            path: str,
            params: Optional[Dict[str, Any]] = None,
            json_data: Optional[Dict[str, Any]] = None,
            headers: Optional[Dict[str, str]] = None,
            timeout: int = 15,
            auto_login: bool = True,
    ) -> requests.Response:
        remaining = self.cooldown_remaining
        if remaining > 0:
            status = self._request_gate.cooldown_status
            status_label = f"HTTP {status}" if status else "风险保护"
            raise HDHavenError(
                f"HDHaven 处于{status_label}冷却期，跳过请求（剩余 {int(remaining + 0.999)} 秒）",
                code="rate_limited",
                status_code=status,
            )

        if not self.token and auto_login and self.username and self.password:
            self.login()

        full_url = urljoin(self.base_url, path)
        req_headers = dict(headers or {})

        try:
            resp = gated_idempotent_request(
                self._request_gate,
                self._session.request,
                method.upper(),
                full_url,
                params=params,
                json=json_data,
                headers=req_headers,
                proxies=self._proxies,
                timeout=timeout,
                retry_connection_errors=True,
            )
        except RequestGateCooldown as err:
            raise HDHavenError(f"HDHaven 处于冷却期：{err}", code="rate_limited", status_code=err.status) from err
        except RequestGateCancelled as err:
            raise HDHavenError("HDHaven 请求已取消", code="cancelled") from err
        except Exception as err:
            raise HDHavenError(f"HDHaven 请求网络失败：{err}") from err

        if is_cloudflare_challenge(resp.text, resp.status_code, resp.headers):
            self._request_gate.activate_cooldown(120, status=resp.status_code, reason="接口触发Cloudflare质询")
            raise HDHavenError("HDHaven 触发 Cloudflare 质询拦截", code="cf_blocked", status_code=resp.status_code)

        if resp.status_code == 401 and auto_login and self.username and self.password:
            logger.debug("HDHaven 会话令牌过期，尝试重新登录刷新")
            self.login(force=True)
            return self.request(
                method, path, params, json_data, headers, timeout, auto_login=False
            )

        if resp.status_code == 429:
            self._request_gate.activate_cooldown(60, status=429, reason="接口触发429限流")
            raise HDHavenError("HDHaven 访问过于频繁（429 Too Many Requests）", code="rate_limited", status_code=429)

        return resp

    def get_account_info(self) -> Dict[str, Any]:
        resp = self.request("GET", "/api/auth/me")
        if resp.status_code != 200:
            raise HDHavenError(f"获取个人信息失败：HTTP {resp.status_code}", status_code=resp.status_code)
        try:
            payload = resp.json() or {}
        except Exception as err:
            raise HDHavenError(f"解析个人信息失败：{err}") from err

        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        level_obj = data.get("level") if isinstance(data.get("level"), dict) else {}
        retention_obj = data.get("retention") if isinstance(data.get("retention"), dict) else {}
        level_name = str(level_obj.get("name") or level_obj.get("key") or "")
        is_member = bool(data.get("is_member"))

        return {
            "id": data.get("id"),
            "name": data.get("nickname") or data.get("username") or data.get("email"),
            "email": data.get("email"),
            "avatar": data.get("avatar_url") or "",
            "points": int(data.get("points") or 0),
            "level": "VIP" if is_member else (level_name or "普通用户"),
            "level_name": level_name,
            "is_vip": is_member,
            "checkin_today": bool(data.get("checkin_today")),
            "last_checkin": data.get("last_checkin"),
            "user_number": data.get("user_number"),
            "retention_deadline": retention_obj.get("deadline"),
            "status": "active" if retention_obj.get("state") == "active" else "normal",
        }

    def checkin(self, mode: str = "normal") -> Dict[str, Any]:
        is_gambler = str(mode or "normal").strip().lower() == "gambler"
        endpoint = "/api/account/checkin/gamble" if is_gambler else "/api/account/checkin"
        mode_text = "赌狗签到" if is_gambler else "普通签到"
        try:
            resp = self.request("POST", endpoint)
            if resp.status_code == 409:
                return {
                    "success": True,
                    "already_checked_in": True,
                    "status": "今日已签到",
                    "message": f"今日已{mode_text}，无需重复签到",
                    "signin_points": 0,
                    "points_change": 0,
                    "signin_days": None,
                    "status_code": 200,
                }
            payload = resp.json() if resp.status_code in (200, 201) else {}
            if not payload.get("success"):
                msg = payload.get("message") or f"{mode_text}失败"
                raise HDHavenError(msg, status_code=resp.status_code)
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            gain = int(data.get("earned") if data.get("earned") is not None else (
                        data.get("reward") or data.get("points") or 5))
            points_after = int(data.get("points")) if data.get("points") is not None else None
            outcome = str(data.get("outcome") or "").strip()
            if gain < 0:
                msg = f"{mode_text}完成：{outcome}（积分 {gain}）" if outcome else f"{mode_text}完成，积分 {gain}"
                status_text = f"{mode_text}完成"
            else:
                msg = f"{mode_text}成功：{outcome}（获得 {gain} 积分）" if outcome else f"{mode_text}成功，获得 {gain} 积分"
                status_text = f"{mode_text}成功"
            return {
                "success": True,
                "already_checked_in": False,
                "status": status_text,
                "message": msg,
                "signin_points": gain,
                "points_change": gain,
                "points_after": points_after,
                "signin_days": None,
                "status_code": 200,
            }
        except HDHavenError as err:
            if err.status_code == 409 or "今日已签到" in str(err):
                return {
                    "success": True,
                    "already_checked_in": True,
                    "status": "今日已签到",
                    "message": f"今日已{mode_text}，无需重复签到",
                    "signin_points": 0,
                    "points_change": 0,
                    "signin_days": None,
                    "status_code": 200,
                }
            raise

    def close(self) -> None:
        if self._turnstile_solver:
            self._turnstile_solver.close()
            self._turnstile_solver = None
        try:
            self._session.close()
        except Exception:
            pass
