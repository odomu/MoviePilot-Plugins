"""HDHive 官方开放 API 与 WebAPI 客户端。"""

from .open import (
    HDHiveOpenAPIClient,
    HDHiveOpenAPIError,
    HDHiveTokenStore,
    HDHiveTokenStoreError,
)
from .web import (
    HDHIVE_DETAIL_RESOURCE_TYPES,
    HDHIVE_RESOURCE_TYPES,
    HDHiveClient,
    HDHiveResourceService,
    HDHiveWebError,
)

__all__ = [
    "HDHiveClient",
    "HDHiveResourceService",
    "HDHiveWebError",
    "HDHiveOpenAPIClient",
    "HDHiveOpenAPIError",
    "HDHiveTokenStore",
    "HDHiveTokenStoreError",
    "HDHIVE_DETAIL_RESOURCE_TYPES",
    "HDHIVE_RESOURCE_TYPES",
]
