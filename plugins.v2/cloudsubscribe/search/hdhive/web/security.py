"""HDHive 网页安全握手与请求签名协议。"""

import hashlib
import hmac
import secrets
from typing import Optional

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

    def __init__(self):
        self._private_key: Optional[X25519PrivateKey] = None
        self._cid = ""
        self._request_key = b""
        self._response_key = b""

    @property
    def cid(self) -> str:
        return self._cid

    def begin_handshake(self) -> bytes:
        self._private_key = X25519PrivateKey.generate()
        self._cid = ""
        self._request_key = b""
        self._response_key = b""
        return self._private_key.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw
        )

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
