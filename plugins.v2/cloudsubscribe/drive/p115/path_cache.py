"""115 目录路径缓存。"""

from typing import Optional

from app.core.cache import TTLCache


class PathCache:
    """保存路径到目录 ID 的短期映射。"""

    def __init__(
            self,
            default_ttl: int = 3600,
            max_entries: int = 2000,
            region: str = "cloudsubscribe:p115_paths",
    ):
        self.default_ttl = max(1, int(default_ttl or 3600))
        self.max_entries = max(16, int(max_entries or 2000))
        self._cache = TTLCache(
            region=region,
            maxsize=self.max_entries,
            ttl=self.default_ttl,
        )

    def get(self, path: str) -> Optional[int]:
        cached = self._cache.get(path)
        return int(cached) if cached is not None else None

    def set(self, path: str, cid: int) -> None:
        self._cache[path] = int(cid)

    def invalidate(self, path: str) -> None:
        self._cache.delete(path)

    def clear(self) -> None:
        self._cache.clear()

    def close(self) -> None:
        pass

    def stats(self) -> dict:
        return {
            "entries": len(list(self._cache.items())),
            "limit": self.max_entries,
            "ttl_seconds": self.default_ttl,
        }

    def __contains__(self, path: str) -> bool:
        return self.get(path) is not None
