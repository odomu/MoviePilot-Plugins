"""聚影网页资源搜索客户端。"""

from .client import JuyingClient, JuyingError
from .resource import JuyingResourceService

__all__ = ["JuyingClient", "JuyingError", "JuyingResourceService"]
