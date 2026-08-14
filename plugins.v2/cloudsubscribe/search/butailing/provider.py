"""不太灵搜索能力声明。"""

from typing import Any, Mapping

from ...core.search import (
    SearchCapability,
    SearchPolicy,
    SearchProvider,
)


def create_butailing_provider(
        service: Any,
        cache_context: Mapping[str, Any],
) -> SearchProvider:
    return SearchProvider(
        key="butailing",
        name="不太灵",
        resource_types=frozenset({"magnet"}),
        services={
            SearchCapability.RESOURCE_SEARCH: service,
            SearchCapability.CACHE_MAINTENANCE: service,
        },
        policy=SearchPolicy(cache_context=cache_context),
    )
