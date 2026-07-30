"""Dian115 门户搜索客户端。"""

from .client import Dian115Client, Dian115Error
from .protocol import encode_resource_key, resource_path, share_path
from .resource import Dian115ResourceService

__all__ = [
    "Dian115Client",
    "Dian115Error",
    "Dian115ResourceService",
    "encode_resource_key",
    "resource_path",
    "share_path",
]
