"""HDHive 网页安全握手与请求签名协议。"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


class HDHiveSecurityProtocol:
    """等价实现站点 hdh/v1 安全模块，不依赖 WASM 运行时。"""

    KID = "1"
    INFO = b"hdh/v1"
    SESSION_RETRY_CODES = frozenset({
        "invalid_session",
        "missing_signature",
        "signature_invalid",
        "session_user_mismatch",
    })
    SIGNED_RESPONSE_PATHS = frozenset({
        "/api/customer/user/current",
        "/api/customer/points-logs",
    })

    def __init__(self):
        self._private_key: Optional[X25519PrivateKey] = None
        self._cid = ""
        self._request_key = b""
        self._response_key = b""
        self._expires_at = 0.0
        self._clock_offset_ms = 0

    @property
    def cid(self) -> str:
        return self._cid

    def ready(self, margin_seconds: int = 60) -> bool:
        return bool(
            self._cid
            and self._request_key
            and self._expires_at - max(0, int(margin_seconds or 0)) > time.time()
        )

    def invalidate(self) -> None:
        self._private_key = None
        self._cid = ""
        self._request_key = b""
        self._response_key = b""
        self._expires_at = 0.0

    def begin_handshake(self) -> bytes:
        self._private_key = X25519PrivateKey.generate()
        self._cid = ""
        self._request_key = b""
        self._response_key = b""
        self._expires_at = 0.0
        return self._private_key.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw
        )

    def handshake_body(
            self,
            user_agent: str,
            languages: str,
            bind_token: str,
    ) -> bytes:
        public_key = self.begin_handshake()
        fingerprint = hashlib.sha256(
            f"{user_agent}|{languages}".encode("utf-8")
        ).hexdigest()
        return json.dumps({
            "client_pub": base64.b64encode(public_key).decode("ascii"),
            "ua_fingerprint": fingerprint,
            "ts": self.timestamp_ms(),
            "bind_token": str(bind_token or ""),
        }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def finalize_handshake(self, cid: str, server_public_key: bytes) -> None:
        if not self._private_key:
            raise ValueError("HDHive 安全握手尚未开始")
        normalized_cid = str(cid or "").strip()
        if not normalized_cid:
            raise ValueError("HDHive 安全握手缺少 cid")
        if len(server_public_key) != 32:
            raise ValueError("HDHive 服务端公钥长度无效")
        shared_secret = self._private_key.exchange(
            X25519PublicKey.from_public_bytes(server_public_key)
        )
        key_material = HKDF(
            algorithm=hashes.SHA256(),
            length=64,
            salt=normalized_cid.encode("utf-8"),
            info=self.INFO,
        ).derive(shared_secret)
        self._cid = normalized_cid
        self._request_key = key_material[32:]
        # hdh/v1 当前版本的请求与响应 HMAC 都使用扩展结果后 32 字节。
        self._response_key = self._request_key

    def accept_handshake(self, data: Dict[str, Any]) -> None:
        server_public_key = base64.b64decode(str(data.get("server_pub") or ""))
        self.finalize_handshake(str(data.get("cid") or ""), server_public_key)
        self._expires_at = float(data.get("expires_at") or 0)

    def sync_time(self, server_time_ms: Any) -> None:
        self._clock_offset_ms = int(server_time_ms) - int(time.time() * 1000)

    def timestamp_ms(self) -> int:
        return int(time.time() * 1000) + self._clock_offset_ms

    def request_headers(
            self,
            method: str,
            path: str,
            body: bytes,
            user_id: str,
    ) -> Dict[str, str]:
        timestamp = str(self.timestamp_ms())
        nonce = self.nonce()
        return {
            "X-HDH-Cid": self.cid,
            "X-HDH-TS": timestamp,
            "X-HDH-Nonce": nonce,
            "X-HDH-Sig": self.sign_request(
                method, path, timestamp, nonce, body, user_id
            ),
            "X-HDH-Kid": self.KID,
        }

    @classmethod
    def is_unlock_path(cls, path: str) -> bool:
        normalized = urlsplit(path).path
        return bool(re.fullmatch(
            r"/api/customer/(?:resources|music_resources)/[^/]+/unlock",
            normalized,
        ) or re.fullmatch(
            r"/api/customer/tv-follow/packs/[^/]+/unlock", normalized
        ))

    @classmethod
    def requires_signed_response(cls, path: str) -> bool:
        normalized = urlsplit(path).path
        return (
                normalized in cls.SIGNED_RESPONSE_PATHS
                or cls.is_unlock_path(normalized)
        )

    @staticmethod
    def response_error_code(response: Any) -> str:
        if int(getattr(response, "status_code", 0) or 0) != 401:
            return ""
        try:
            payload = response.json()
        except (AttributeError, ValueError):
            return ""
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("code") or payload.get("error_code") or "")

    @classmethod
    def retry_action(cls, error_code: str) -> str:
        if error_code in cls.SESSION_RETRY_CODES:
            return "handshake"
        if error_code == "stale_ts":
            return "clock"
        if error_code == "replay":
            return "retry"
        return ""

    def sign_request(
            self,
            method: str,
            path: str,
            timestamp: str,
            nonce: str,
            body: bytes,
            user_id: str,
    ) -> str:
        if not self._request_key or not self._cid:
            raise ValueError("HDHive 安全会话未就绪")
        canonical = "\n".join((
            str(method or "GET").upper(),
            str(path or "/"),
            str(timestamp or ""),
            str(nonce or ""),
            hashlib.sha256(body or b"").hexdigest(),
            self._cid,
            str(user_id or "0"),
            self.KID,
        )).encode("utf-8")
        return hmac.new(self._request_key, canonical, hashlib.sha256).hexdigest()

    def verify_response(
            self,
            path: str,
            status_code: int,
            response_timestamp: str,
            body: bytes,
            signature: str,
    ) -> bool:
        if not self._response_key:
            return False
        canonical = "|".join((
            str(path or "/"),
            str(int(status_code or 0)),
            str(response_timestamp or ""),
            hashlib.sha256(body or b"").hexdigest(),
        )).encode("utf-8")
        expected = hmac.new(
            self._response_key, canonical, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, str(signature or ""))

    @staticmethod
    def nonce() -> str:
        return secrets.token_hex(16)
