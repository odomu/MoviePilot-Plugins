"""聚影搜索服务。"""

from typing import Iterable

from app.schemas.types import MediaType

from ...core.search import SearchQuery
from ..magnet import clear_cache, media_titles, normalize_magnets
from .client import JuyingClient
from .resource import JuyingResourceService


class JuyingSearchService:
    def __init__(
            self,
            client: JuyingClient,
            resources: JuyingResourceService,
            resource_types: Iterable[str],
            result_limit: int,
    ):
        self._client = client
        self._resources = resources
        self._resource_types = tuple(resource_types)
        self._result_limit = result_limit

    def search(self, query: SearchQuery):
        mediainfo = query.mediainfo
        subscribe = query.subscribe
        titles = media_titles(mediainfo)
        resources = self._resources.search(
            title=titles[0] if titles else "",
            alternative_titles=titles[1:],
            year=getattr(mediainfo, "year", None),
            media_type=(
                "tv" if query.media_type == MediaType.TV else "movie"
            ),
            tmdb_id=(
                    getattr(mediainfo, "tmdb_id", None)
                    or getattr(subscribe, "tmdbid", None)
            ),
            douban_id=(
                    getattr(mediainfo, "douban_id", None)
                    or getattr(subscribe, "doubanid", None)
            ),
            imdb_id=(
                    getattr(mediainfo, "imdb_id", None)
                    or getattr(subscribe, "imdbid", None)
            ),
            season=query.season,
            resource_type_order=self._resource_types,
            limit=(
                query.result_limit or self._result_limit
                if query.test_mode else self._result_limit
            ),
            test_mode=query.test_mode,
        )
        return normalize_magnets(resources, "juying")

    def resolve(self, **kwargs):
        return self._resources.resolve_resource(
            str(kwargs.get("resource_id") or "")
        )

    def clear_cache(self) -> int:
        return clear_cache(self._resources)
