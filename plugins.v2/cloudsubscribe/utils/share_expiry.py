"""分享失效登记表：转存阶段确认失效的分享在 TTL 内跳过解锁与重试。

与具体网盘解耦：键为规范化后的分享链接，任何驱动只需在确认失效时调用
``mark_share_expired``，转存/解锁链路统一用 ``is_share_expired`` 判断。
"""

from threading import RLock
from typing import Any, Optional
from urllib.parse import urlsplit

from app.log import logger

from .cache import create_platform_ttl_cache

_SHARE_TTL = 24 * 60 * 60
_EXPIRED_CODE_115 = 4100018
# 同一分享的常见域名写法归一到同一键，避免换域名后重新解锁
_HOST_ALIASES = {
    "115cdn.com": "115.com",
    "anxia.com": "115.com",
}
_EXPIRED_MARKERS = (
    "过期",
    "失效",
    "取消",
    "删除",
    "不存在",
    "expired",
    "invalid",
    "not found",
    "not exist",
    "cancel",
    "deleted",
)
_SHARE_CONTEXT = ("分享", "链接", "share", "link")

_registry_lock = RLock()
_registry = None


def _registry_cache():
    global _registry
    with _registry_lock:
        if _registry is None:
            _registry = create_platform_ttl_cache(
                "share_expiry",
                maxsize=4096,
                ttl=_SHARE_TTL,
            )
        return _registry


def normalize_share_url(share_url: Any) -> str:
    """把分享链接规范化为不含查询参数/片段的稳定键。"""
    text = str(share_url or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        return text.lower()
    if not parsed.scheme or not parsed.netloc:
        return text.lower()
    host = parsed.netloc.lower()
    host = _HOST_ALIASES.get(host, host)
    path = (parsed.path or "/").rstrip("/") or "/"
    return f"{parsed.scheme.lower()}://{host}{path}".lower()


def mark_share_expired(
        share_url: Any,
        reason: str = "",
        error_code: Optional[Any] = None,
) -> bool:
    """登记已确认失效的分享，返回是否成功写入。"""
    key = normalize_share_url(share_url)
    if not key:
        return False
    cache = _registry_cache()
    if key in cache:
        return True
    cache[key] = {
        "reason": str(reason or ""),
        "error_code": error_code,
    }
    logger.info(
        f"🚫 分享已加入失效黑名单（24小时）：{key}，原因：{reason or error_code or '未知'}"
    )
    return True


def is_share_expired(share_url: Any) -> bool:
    """判断分享是否在有效期内被登记为失效。"""
    key = normalize_share_url(share_url)
    return bool(key and key in _registry_cache())


def clear_expired_shares() -> None:
    """测试/热重载：清空失效登记。"""
    _registry_cache().clear()


def is_expired_share_error(error_code: Any = None, message: Any = "") -> bool:
    """识别各网盘“分享已过期/失效/取消/删除”类错误。"""
    try:
        if int(error_code or 0) == _EXPIRED_CODE_115:
            return True
    except (TypeError, ValueError):
        pass
    text = str(message or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if not any(marker in lowered for marker in _SHARE_CONTEXT):
        return False
    return any(marker in lowered for marker in _EXPIRED_MARKERS)


def share_error_text(error: Any) -> str:
    """从异常/响应中提取可判定的错误文本。"""
    if error is None:
        return ""
    if isinstance(error, bytes):
        try:
            return error.decode("utf-8", errors="ignore")
        except Exception:
            return ""
    return str(error)


__all__ = [
    "normalize_share_url",
    "mark_share_expired",
    "is_share_expired",
    "clear_expired_shares",
    "is_expired_share_error",
    "share_error_text",
]
