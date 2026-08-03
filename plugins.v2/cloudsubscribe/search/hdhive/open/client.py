"""
HDHive OpenAPI 客户端
基于官方 Python SDK 适配：应用 Secret (X-API-Key) + OAuth 用户 Access Token (Bearer) 双层认证
参考文档: https://hdhive.com/docs/open
"""
import copy
import json
import secrets
import threading
import time
import urllib.parse
from typing import Any, Callable, Dict, Optional

from app.log import logger

from ...http_client import RequestGate, gated_request, normalize_proxies, requests


class HDHiveOpenAPIError(Exception):
    """HDHive OpenAPI 错误"""

    def __init__(self, code: str, message: str, description: str = "", status: int = 0):
        super().__init__(description or message or code)
        self.code = code
        self.message = message
        self.description = description
        self.status = status


class HDHiveOpenAPIClient:
    """
    HDHive OpenAPI 客户端

    认证模型:
    - 应用 Secret: 所有 /api/open/* 和 OAuth 接口都放在 X-API-Key 请求头
    - 用户 Access Token: 业务接口（资源查询/解锁等）附加 Authorization: Bearer
    - Access Token 过期时自动用 Refresh Token 刷新，并通过回调持久化新 Token
    """

    DEFAULT_SCOPE = "query unlock"
    _RESOURCE_CACHE_TTL = 10 * 60
    _RESOURCE_CACHE_LIMIT = 256
    _DETAIL_CACHE_TTL = 10 * 60
    _DETAIL_CACHE_LIMIT = 512
    _RISK_COOLDOWN_SECONDS = 60
    _SERVER_ERROR_COOLDOWN_SECONDS = 5

    def __init__(
            self,
            app_secret: str,
            client_id: str = "",
            access_token: str = "",
            refresh_token: str = "",
            token_expires_at: float = 0,
            base_url: str = "https://hdhive.com",
            proxy: Any = None,
            timeout: int = 30,
            request_interval: float = 1.0,
            on_token_update: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        """
        :param app_secret: OpenAPI 应用 Secret（X-API-Key）
        :param client_id: 应用公开 Client ID（用于生成授权链接）
        :param access_token: 用户 Access Token
        :param refresh_token: 用户 Refresh Token
        :param token_expires_at: Access Token 过期时间戳（秒），0 表示未知
        :param base_url: HDHive 站点地址
        :param proxy: 代理配置（字符串或 requests 格式字典）
        :param timeout: 请求超时秒数
        :param on_token_update: Token 刷新后的持久化回调，参数为
                                {"access_token", "refresh_token", "token_expires_at"}
        """
        self.app_secret = (app_secret or "").strip()
        self.client_id = (client_id or "").strip()
        self.access_token = (access_token or "").strip()
        self.refresh_token = (refresh_token or "").strip()
        self.token_expires_at = float(token_expires_at or 0)
        self.base_url = (base_url or "https://hdhive.com").rstrip("/")
        self.timeout = max(5, min(int(timeout or 30), 120))
        self.request_interval = max(
            0.2, min(float(request_interval or 1.0), 10.0)
        )
        self.on_token_update = on_token_update
        self._proxies = normalize_proxies(proxy)
        self._session = requests.Session(impersonate="chrome")
        self._resource_cache: Dict[tuple[str, str], tuple[float, Dict[str, Any]]] = {}
        self._detail_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
        self._resource_locks = tuple(threading.Lock() for _ in range(16))
        self._detail_locks = tuple(threading.Lock() for _ in range(32))
        self._lock = threading.RLock()
        self._request_gate = RequestGate(
            "HDHive OpenAPI",
            request_interval=self.request_interval,
            minimum_interval=0.2,
            risk_cooldown_seconds=self._RISK_COOLDOWN_SECONDS,
            server_error_cooldown_seconds=self._SERVER_ERROR_COOLDOWN_SECONDS,
        )

    @property
    def is_ready(self) -> bool:
        """应用 Secret 和用户 Token 均已配置，可调用业务接口"""
        return bool(self.app_secret and self.access_token)

    def close(self) -> None:
        with self._lock:
            self._session.close()
            self._resource_cache.clear()
            self._detail_cache.clear()

    def clear_cache(self) -> Dict[str, int]:
        """清空 OpenAPI 资源列表和分享详情缓存。"""
        with self._lock:
            counts = {
                "resources": len(self._resource_cache),
                "details": len(self._detail_cache),
            }
            self._resource_cache.clear()
            self._detail_cache.clear()
            return counts

    @staticmethod
    def _cached_copy(
            cache: Dict[Any, tuple[float, Dict[str, Any]]], key: Any
    ) -> Optional[Dict[str, Any]]:
        cached = cache.get(key)
        if not cached or cached[0] <= time.monotonic():
            cache.pop(key, None)
            return None
        return copy.deepcopy(cached[1])

    @staticmethod
    def _store_cache(
            cache: Dict[Any, tuple[float, Dict[str, Any]]],
            key: Any,
            value: Dict[str, Any],
            ttl: int,
            limit: int,
    ) -> None:
        now = time.monotonic()
        expired = [item for item, cached in cache.items() if cached[0] <= now]
        for item in expired:
            cache.pop(item, None)
        overflow = len(cache) - limit + 1
        if overflow > 0:
            for item in sorted(cache, key=lambda entry: cache[entry][0])[:overflow]:
                cache.pop(item, None)
        cache[key] = (now + ttl, copy.deepcopy(value))

    def build_authorize_url(
            self,
            redirect_uri: str,
            scope: str = "",
            state: str = "",
            response_mode: str = "redirect",
    ) -> str:
        """生成用户授权页 URL。state 必须由调用方保存并在回调时校验。"""
        if not self.client_id:
            raise HDHiveOpenAPIError("400", "缺少 OpenAPI Client ID")
        redirect_uri = str(redirect_uri or "").strip()
        parsed_redirect = urllib.parse.urlparse(redirect_uri)
        if (
                parsed_redirect.scheme not in {"http", "https"}
                or not parsed_redirect.netloc
                or parsed_redirect.fragment
        ):
            raise HDHiveOpenAPIError(
                "400", "OAuth Redirect URI 必须是无 fragment 的完整 HTTP/HTTPS 地址"
            )
        response_mode = str(response_mode or "redirect").strip().lower()
        if response_mode not in {"redirect", "postmessage"}:
            raise HDHiveOpenAPIError(
                "400", "当前插件仅支持 redirect 或 postmessage 授权回调"
            )
        state = str(state or "").strip() or secrets.token_urlsafe(32)
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": scope or self.DEFAULT_SCOPE,
            "state": state,
            "response_mode": response_mode,
        }
        return f"{self.base_url}/openapi/authorize?{urllib.parse.urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str) -> Dict[str, Any]:
        """
        授权码换取用户 Token
        :param code: 一次性授权码
        :param redirect_uri: 必须与发起授权时的回调地址完全一致
        :return: Token 数据（access_token/refresh_token/expires_in 等）
        """
        data = self._request_public(
            "POST",
            "/api/public/openapi/oauth/token",
            {
                "grant_type": "authorization_code",
                "code": (code or "").strip(),
                "redirect_uri": (redirect_uri or "").strip(),
            },
        )
        if not str(data.get("access_token") or "").strip():
            raise HDHiveOpenAPIError(
                "INVALID_TOKEN_RESPONSE", "HDHive OAuth Token 响应缺少 Access Token"
            )
        self._apply_token_set(data)
        return data

    def refresh_access_token(self) -> Dict[str, Any]:
        """
        使用 Refresh Token 刷新用户 Token
        刷新失败返回 OPENAPI_REAUTH_REQUIRED 时需要重新发起授权
        """
        if not self.refresh_token:
            raise HDHiveOpenAPIError("OPENAPI_REAUTH_REQUIRED", "缺少 Refresh Token，请重新授权")
        data = self._request_public(
            "POST",
            "/api/public/openapi/oauth/refresh",
            {"refresh_token": self.refresh_token},
        )
        if not str(data.get("access_token") or "").strip():
            raise HDHiveOpenAPIError(
                "INVALID_TOKEN_RESPONSE", "HDHive OAuth Refresh 响应缺少 Access Token"
            )
        self._apply_token_set(data)
        logger.info("HDHive OpenAPI: 用户 Access Token 刷新成功")
        return data

    def _apply_token_set(self, data: Dict[str, Any]):
        """保存 Token 并触发持久化回调"""
        if not isinstance(data, dict):
            return
        self.access_token = str(data.get("access_token") or self.access_token).strip()
        self.refresh_token = str(data.get("refresh_token") or self.refresh_token).strip()
        expires_in = int(data.get("expires_in", 0) or 0)
        self.token_expires_at = time.time() + expires_in if expires_in else 0
        if self.on_token_update:
            try:
                token_data = dict(data)
                token_data.update({
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "token_expires_at": self.token_expires_at,
                })
                self.on_token_update(token_data)
            except Exception as e:
                logger.error(f"HDHive OpenAPI: Token 持久化回调失败: {e}")

    def ping(self) -> Dict[str, Any]:
        """验证应用 Secret（仅需 X-API-Key）"""
        return self._request("GET", "/api/open/ping", with_user_token=False)

    def get_me(self) -> Dict[str, Any]:
        """获取当前授权用户基础信息"""
        return self._request("GET", "/api/open/me")

    def query_resources(
            self, media_type: str, tmdb_id: Any, force_refresh: bool = False
    ) -> Dict[str, Any]:
        """
        根据 TMDB ID 查询资源列表
        :param media_type: movie 或 tv
        """
        media_type = str(media_type or "").strip().lower()
        if media_type not in ("movie", "tv"):
            raise HDHiveOpenAPIError("400", f"不支持的媒体类型: {media_type}")
        normalized_tmdb_id = str(tmdb_id).strip()
        cache_key = (media_type, normalized_tmdb_id)
        with self._lock:
            cached = None if force_refresh else self._cached_copy(
                self._resource_cache, cache_key
            )
        if cached is not None:
            return cached
        path = "/api/open/resources/{}/{}".format(
            urllib.parse.quote(str(media_type), safe=""),
            urllib.parse.quote(normalized_tmdb_id, safe=""),
        )
        cache_lock = self._resource_locks[hash(cache_key) % len(self._resource_locks)]
        with cache_lock:
            with self._lock:
                cached = None if force_refresh else self._cached_copy(
                    self._resource_cache, cache_key
                )
            if cached is not None:
                return cached
            data = self._request("GET", path)
            with self._lock:
                self._store_cache(
                    self._resource_cache, cache_key, data,
                    self._RESOURCE_CACHE_TTL, self._RESOURCE_CACHE_LIMIT,
                )
            return data

    def get_share_details(
            self, slug: str, force_refresh: bool = False
    ) -> Dict[str, Any]:
        """查询单个分享对当前用户的实际积分和解锁状态，不返回原始链接。"""
        slug = str(slug or "").strip()
        if not slug:
            raise HDHiveOpenAPIError("400", "资源 slug 不能为空")
        with self._lock:
            cached = None if force_refresh else self._cached_copy(
                self._detail_cache, slug
            )
        if cached is not None:
            return cached
        cache_lock = self._detail_locks[hash(slug) % len(self._detail_locks)]
        with cache_lock:
            with self._lock:
                cached = None if force_refresh else self._cached_copy(
                    self._detail_cache, slug
                )
            if cached is not None:
                return cached
            data = self._request(
                "GET", f"/api/open/shares/{urllib.parse.quote(slug, safe='')}"
            )
            with self._lock:
                self._store_cache(
                    self._detail_cache, slug, data,
                    self._DETAIL_CACHE_TTL, self._DETAIL_CACHE_LIMIT,
                )
            return data

    def unlock_resource(
            self, slug: str, max_unlock_points: Optional[int] = None
    ) -> Dict[str, Any]:
        """解锁单个资源并获取分享链接"""
        slug = str(slug or "").strip()
        if not slug:
            raise HDHiveOpenAPIError("400", "资源 slug 不能为空")
        confirmed_points: Optional[int] = None
        if max_unlock_points is not None:
            detail_response = self.get_share_details(slug, force_refresh=True)
            detail = detail_response.get("data") or {}
            already_unlocked = bool(
                detail.get("is_unlocked") or detail.get("is_free_for_user")
            )
            try:
                current_points = max(0, int(
                    detail.get("actual_unlock_points")
                    if detail.get("actual_unlock_points") is not None
                    else detail.get("unlock_points") or 0
                ))
            except (TypeError, ValueError):
                current_points = 0
            confirmed_points = 0 if already_unlocked else current_points
            if not already_unlocked and current_points > int(max_unlock_points):
                raise HDHiveOpenAPIError(
                    "UNLOCK_BUDGET_EXCEEDED",
                    "HDHive 当前解锁价格超过预算",
                    f"需要 {current_points}，预算 {int(max_unlock_points)}",
                )
        data = self._request(
            "POST", "/api/open/resources/unlock", body={"slug": slug}
        )
        result = data.get("data") if isinstance(data, dict) else None
        result = result if isinstance(result, dict) else {}
        actual_points = 0
        point_sources = [result]
        if isinstance(result.get("unlock"), dict):
            point_sources.append(result["unlock"])
        point_sources.append(data)
        for source in point_sources:
            for key in (
                    "cost_points", "actual_unlock_points", "spent_points",
                    "points_cost", "actual_points",
            ):
                if source.get(key) is None:
                    continue
                try:
                    actual_points = max(0, int(source.get(key) or 0))
                except (TypeError, ValueError):
                    actual_points = 0
                break
            if actual_points > 0:
                break
        if actual_points <= 0 and confirmed_points is not None:
            actual_points = confirmed_points
        data["actual_points"] = actual_points
        with self._lock:
            self._detail_cache.pop(slug, None)
            self._resource_cache.clear()
        return data

    def _request_public(self, method: str, path: str, body: Optional[Dict] = None) -> Dict[str, Any]:
        """调用 OAuth 公共接口（仅应用 Secret，不带用户 Token），返回 data 部分"""
        if not self.app_secret:
            raise HDHiveOpenAPIError("MISSING_API_KEY", "未配置应用 Secret")
        headers = {
            "X-API-Key": self.app_secret,
            "Accept": "application/json",
        }
        data = self._do_request(method, path, headers, body)
        if isinstance(data, dict) and "data" in data:
            return data.get("data") or {}
        return data

    def _request(
            self,
            method: str,
            path: str,
            body: Optional[Dict] = None,
            with_user_token: bool = True,
            _retry: bool = True,
    ) -> Dict[str, Any]:
        """
        调用业务接口，返回完整响应 JSON（含 success/data/message）
        Access Token 过期时自动刷新并重试一次
        """
        if not self.app_secret:
            raise HDHiveOpenAPIError("MISSING_API_KEY", "未配置应用 Secret")

        if with_user_token:
            if not self.access_token:
                raise HDHiveOpenAPIError("OPENAPI_USER_REQUIRED", "未完成用户授权，缺少 Access Token")
            # 已知过期时间则提前刷新，避免无谓的 401 往返
            if self.refresh_token and self.token_expires_at and time.time() > self.token_expires_at - 60:
                try:
                    self.refresh_access_token()
                except HDHiveOpenAPIError as e:
                    logger.warning(f"HDHive OpenAPI: 预刷新 Token 失败（{e.code}），继续尝试当前 Token")

        headers = {
            "X-API-Key": self.app_secret,
            "Accept": "application/json",
        }
        if with_user_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        try:
            return self._do_request(method, path, headers, body)
        except HDHiveOpenAPIError as exc:
            if _retry and with_user_token and exc.code == "OPENAPI_REFRESH_REQUIRED" and self.refresh_token:
                self.refresh_access_token()
                return self._request(method, path, body, with_user_token, _retry=False)
            raise

    def _do_request(self, method: str, path: str, headers: Dict, body: Optional[Dict]) -> Dict[str, Any]:
        url = self.base_url + path
        try:
            with self._lock:
                resp = gated_request(
                    self._request_gate,
                    self._session.request,
                    method=method,
                    url=url,
                    headers=headers,
                    json=body if body is not None else None,
                    proxies=self._proxies,
                    timeout=self.timeout,
                )
        except requests.exceptions.RequestException as error:
            raise HDHiveOpenAPIError(
                "REQUEST_FAILED", "HDHive OpenAPI 请求失败", str(error)
            ) from error
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            raise HDHiveOpenAPIError(str(resp.status_code), f"响应解析失败 (HTTP {resp.status_code})",
                                     resp.text[:200], resp.status_code)
        if resp.status_code >= 400:
            raise HDHiveOpenAPIError(
                str(data.get("code", resp.status_code)),
                str(data.get("message", "")),
                str(data.get("description", "")),
                resp.status_code,
            )
        return data
