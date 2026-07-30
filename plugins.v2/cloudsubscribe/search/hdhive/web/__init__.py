"""HDHive WebAPI 客户端。"""

from .client import HDHiveClient, HDHiveWebError
from .resource import (
    HDHIVE_DETAIL_RESOURCE_TYPES,
    HDHIVE_RESOURCE_TYPES,
    HDHiveResourceService,
)

__all__ = [
    "HDHiveClient",
    "HDHiveResourceService",
    "HDHiveWebError",
    "HDHIVE_DETAIL_RESOURCE_TYPES",
    "HDHIVE_RESOURCE_TYPES",
]
