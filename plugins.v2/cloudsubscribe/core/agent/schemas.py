"""智能体工具参数模型。"""

from typing import List, Optional

from pydantic import BaseModel, Field


class CloudSubscribeStatusInput(BaseModel):
    include_recent: bool = Field(
        default=True,
        description="是否返回最近的转存记录摘要",
    )


class CloudSubscribeSyncInput(BaseModel):
    explanation: str = Field(
        default="执行网盘订阅搜索",
        description="本次触发订阅搜索的原因",
    )


class CloudSubscribeLinksInput(BaseModel):
    subscribe_id: int = Field(..., gt=0, description="要关联的 MoviePilot 订阅 ID")
    resource_links: List[str] = Field(
        ...,
        min_length=1,
        max_length=50,
        description="115分享、ED2K或Magnet链接列表",
    )


class CloudSubscribeResourceSearchInput(BaseModel):
    subscribe_id: Optional[int] = Field(
        default=None,
        gt=0,
        description="已有 MoviePilot 订阅 ID；已知媒体名称时可不传",
    )
    title: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="要搜索的媒体名称；与 subscribe_id 至少提供一项",
    )
    media_type: Optional[str] = Field(
        default=None,
        description="媒体类型，仅支持 movie（电影）或 tv（电视剧）；可由识别结果推断",
    )
    season: Optional[int] = Field(
        default=None,
        ge=1,
        le=999,
        description="电视剧季号；与 latest_season 不能同时使用",
    )
    latest_season: bool = Field(
        default=False,
        description="是否搜索电视剧最新季；用户说“最新季”时设为 true",
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=50,
        description="返回候选数量，默认20，最多50",
    )


class CloudSubscribeResourceSelectInput(BaseModel):
    search_id: str = Field(
        ...,
        min_length=1,
        max_length=32,
        description="cloudsubscribe_search_resources 返回的搜索 ID",
    )
    candidate_ids: List[str] = Field(
        ...,
        min_length=1,
        max_length=20,
        description="用户确认选择的候选 ID 列表，例如 r001、r003；只能使用最近一次搜索返回的 ID",
    )


class CloudSubscribeCacheClearInput(BaseModel):
    confirm: bool = Field(
        default=False,
        description="用户是否已明确确认清理网盘订阅助手缓存；只有明确要求清理时才传 true",
    )


class CloudSubscribePerformanceInput(BaseModel):
    include_tasks: bool = Field(
        default=True,
        description="是否返回当前运行任务的耗时、进度和吞吐信息",
    )


class CloudSubscribeConfigUpdateInput(BaseModel):
    show_sidebar_nav: Optional[bool] = Field(
        default=None,
        description="是否在 MoviePilot 左侧导航显示网盘订阅入口",
    )
    agent_enabled: Optional[bool] = Field(
        default=None,
        description="是否启用网盘订阅助手智能体工具",
    )
    notify: Optional[bool] = Field(
        default=None,
        description="是否发送插件执行通知",
    )
    search_cache_enabled: Optional[bool] = Field(
        default=None,
        description="是否启用搜索结果缓存",
    )
    search_cache_ttl_minutes: Optional[int] = Field(
        default=None,
        ge=1,
        le=1440,
        description="搜索结果缓存时间，单位分钟，范围 1 到 1440",
    )
    search_concurrency: Optional[int] = Field(
        default=None,
        ge=1,
        le=5,
        description="搜索源并发数，范围 1 到 5",
    )
    subscription_concurrency: Optional[int] = Field(
        default=None,
        ge=1,
        le=5,
        description="订阅任务并发数，范围 1 到 5",
    )
    pansou_result_limit: Optional[int] = Field(
        default=None,
        ge=1,
        le=100,
        description="PanSou 单次返回候选上限，范围 1 到 100",
    )
    hdhive_candidate_limit: Optional[int] = Field(
        default=None,
        ge=1,
        le=20,
        description="HDHive 单次候选上限，范围 1 到 20",
    )
