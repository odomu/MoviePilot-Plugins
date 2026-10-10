import {computed, onUnmounted, ref, watch} from "vue";
import {
  getChannelDefaultIcon,
  getNormalizedResourceType,
  getResourceTabIcon,
  getResourceTypeName,
  getResourceTypeRank,
  getSourceName,
  isPointUnlockResource,
  responseItems,
  unwrapApiResponse,
} from "./resourceUtils";

// 媒体渠道搜索结果的前端内存缓存（15分钟有效）
const mediaSearchMemoryCache = new Map();
const MEDIA_SEARCH_CACHE_TTL = 15 * 60 * 1000;

function getMediaCacheKey(media) {
  if (!media) return "";
  const id = media.tmdb_id || media.douban_id || media.media_id || media.imdb_id || "";
  const title = String(media.title || "").trim().toLowerCase();
  const year = String(media.year || "").trim();
  const type = String(media.media_type || "movie").trim();
  return `${type}:${id}:${title}:${year}`;
}

export function isCancellationError(error) {
  if (!error) return false;
  if (error.name === "AbortError" || error.name === "CanceledError") return true;
  if (error.code === "ERR_CANCELED" || error.__CANCEL__ || error.isAxiosCancel) return true;
  const msg = String(error.message || error || "").toLowerCase();
  return (
    msg.includes("cancel") ||
    msg.includes("abort") ||
    msg.includes("canceled") ||
    msg.includes("cancelled") ||
    msg.includes("http cancel") ||
    msg.includes("err_canceled") ||
    msg.includes("user aborted") ||
    msg.includes("请求已取消") ||
    msg.includes("操作已取消")
  );
}


/** 管理详情弹窗的数据补全、并发取消和生命周期。 */
export function useMediaDetail({api, pluginId, pluginConfig, showMessage}) {
  const detailVisible = ref(false);
  const detailLoading = ref(false);
  const activeMedia = ref(null);
  const activeDetailSeason = ref(1);

  function normalizeChannels(sources) {
    if (!Array.isArray(sources)) return [];
    return sources
      .map((source) => {
        const key = (typeof source === "object" && source?.key ? source.key : String(source)).toLowerCase();
        const name = typeof source === "object" && source?.name ? source.name : getSourceName(key);
        const icon = (typeof source === "object" && source?.icon) ? source.icon : getChannelDefaultIcon(key);
        return { key, name, icon };
      })
      .filter((channel) => Boolean(channel.key));
  }

  // 渠道与网盘列表均由后端下发（available_sources / available_drives）
  const availableChannels = ref([]);
  const availableDrives = ref([]);
  const activeChannelTab = ref("pansou");
  const activeResourceTab = ref("");
  const resourceSearchQuery = ref("");
  const selectedResourceSpecs = ref([]);
  const channelResults = ref({});
  const channelLoading = ref({});
  const channelSearched = ref({});
  const channelElapsed = ref({});
  let requestToken = 0;
  const channelAbortControllers = new Map();

  function abortAllActiveSearches() {
    for (const controller of channelAbortControllers.values()) {
      try {
        controller.abort();
      } catch (_) {
      }
    }
    channelAbortControllers.clear();
  }



  function getItemFansub(item) {
    if (item?.fansub) return String(item.fansub).trim();
    const title = String(item?.title || "").trim();
    const match = title.match(/^[\[【]([^\]】]+)[\]】]/);
    return match ? match[1].trim() : "其他";
  }

  const currentChannelResources = computed(() => channelResults.value[activeChannelTab.value] || []);
  const currentChannelResourceTabs = computed(() => {
    const list = currentChannelResources.value;
    const channel = String(activeChannelTab.value || "").toLowerCase();
    const isAnimeBtChannel = channel === "mikan" || channel === "animegarden";

    if (isAnimeBtChannel) {
      if (!list.length) return [];
      const counts = {};
      for (const item of list) {
        const fs = getItemFansub(item);
        counts[fs] = (counts[fs] || 0) + 1;
      }
      const sorted = Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
      return [
        {
          value: "all",
          title: "全部",
          count: list.length,
          icon: "mdi-account-group-outline",
        },
        ...sorted.map((fs) => ({
          value: fs,
          title: fs,
          count: counts[fs],
          icon: "mdi-subtitles-outline",
        })),
      ];
    }

    const counts = {};
    for (const item of list) {
      const type = getNormalizedResourceType(item);
      counts[type] = (counts[type] || 0) + 1;
    }
    return Object.keys(counts)
      .sort((a, b) => {
        const rankDiff = getResourceTypeRank(a) - getResourceTypeRank(b);
        return rankDiff !== 0 ? rankDiff : counts[b] - counts[a];
      })
      .map((type) => ({
        value: type,
        title: getResourceTypeName(type),
        count: counts[type],
        icon: getResourceTabIcon(type),
      }));
  });

  const activeResourceFilterCount = computed(() => {
    let count = 0;
    if (String(resourceSearchQuery.value || "").trim()) count += 1;
    if (Array.isArray(selectedResourceSpecs.value)) count += selectedResourceSpecs.value.length;
    return count;
  });

  function resetResourceFilters() {
    resourceSearchQuery.value = "";
    selectedResourceSpecs.value = [];
  }

  const currentChannelFilteredResources = computed(() => {
    const list = currentChannelResources.value;
    const selected = activeResourceTab.value;
    // 1. 过滤当前选中的子 tab
    const availableTabs = currentChannelResourceTabs.value;
    const effectiveSelected = availableTabs.some((tab) => tab.value === selected)
      ? selected
      : (availableTabs[0]?.value || "");

    const channel = String(activeChannelTab.value || "").toLowerCase();
    const isAnimeBtChannel = channel === "mikan" || channel === "animegarden";

    let tabFiltered = list;
    if (isAnimeBtChannel) {
      if (effectiveSelected && effectiveSelected !== "all") {
        tabFiltered = list.filter((item) => getItemFansub(item) === effectiveSelected);
      }
    } else {
      tabFiltered = !effectiveSelected
        ? list
        : list.filter((item) => getNormalizedResourceType(item) === effectiveSelected);
    }

    // 2. 搜索当前子 tab 列表的文本
    const query = String(resourceSearchQuery.value || "").trim().toLowerCase();
    let searched = tabFiltered;
    if (query) {
      searched = searched.filter((item) => {
        const title = String(item?.title || "").toLowerCase();
        const desc = String(item?.description || "").toLowerCase();
        const fileName = String(item?.file_name || "").toLowerCase();
        const tags = (item?.tags || []).map((t) => String(t || "").toLowerCase()).join(" ");
        const fansub = String(item?.fansub || "").toLowerCase();
        return title.includes(query) || desc.includes(query) || fileName.includes(query) || tags.includes(query) || fansub.includes(query);
      });
    }

    // 3. 规格快筛过滤
    const specs = selectedResourceSpecs.value || [];
    if (specs.length > 0) {
      searched = searched.filter((item) => {
        const itemTags = (item?.tags || []).map((t) => String(t).toUpperCase());
        const titleUpper = String(item?.title || "").toUpperCase();

        return specs.every((spec) => {
          const specUpper = String(spec).toUpperCase();
          if (specUpper === "免费") {
            return isPointUnlockResource(item) && Number(item?.unlock_points || 0) === 0;
          }
          if (specUpper === "4K") {
            return itemTags.includes("4K") || titleUpper.includes("4K") || titleUpper.includes("2160P");
          }
          if (specUpper === "1080P") {
            return itemTags.includes("1080P") || titleUpper.includes("1080P");
          }
          if (specUpper === "原盘") {
            return itemTags.some((t) => t.includes("原盘") || t.includes("REMUX") || t.includes("BLURAY") || t.includes("BDMV"))
              || /原盘|REMUX|BLURAY|BDMV/i.test(titleUpper);
          }
          if (specUpper === "HDR") {
            return itemTags.some((t) => t.includes("HDR")) || /HDR/i.test(titleUpper);
          }
          if (specUpper === "杜比视界" || specUpper === "DV") {
            return itemTags.some((t) => t.includes("杜比") || t.includes("DV") || t.includes("DOVI"))
              || /杜比视界|\bDV\b|DOVI|DOLBY\s*VISION/i.test(titleUpper);
          }
          return itemTags.includes(specUpper) || titleUpper.includes(specUpper);
        });
      });
    }

    return searched
      .map((item, index) => ({item, index}))
      .sort((a, b) => resourceTagCount(b.item) - resourceTagCount(a.item) || a.index - b.index)
      .map(({item}) => item);
  });

  function resourceTagCount(resource) {
    return Array.isArray(resource?.tags) ? resource.tags.length : 0;
  }

  async function loadMediaDetail(item, token = requestToken) {
    const response = await api.value.post(`plugin/${pluginId.value}/resource/detail`, item);
    const result = unwrapApiResponse(response);
    const detail = result?.data?.item || result?.item;
    if (result?.success === false || !detail || typeof detail !== "object") {
      throw new Error(result?.message || "获取媒体详情失败");
    }
    if (token !== requestToken) return false;
    activeMedia.value = {...activeMedia.value, ...detail};
    return true;
  }

  async function openMedia(item) {
    const token = ++requestToken;
    activeMedia.value = {...item};
    detailLoading.value = true;
    detailVisible.value = true;
    try {
      await loadMediaDetail(item, token);
    } finally {
      if (token === requestToken) detailLoading.value = false;
    }
    return token === requestToken && detailVisible.value;
  }

  function resetChannelState() {
    channelResults.value = {};
    channelLoading.value = {};
    channelSearched.value = {};
    channelElapsed.value = {};
    activeResourceTab.value = "";
    resourceSearchQuery.value = "";
    selectedResourceSpecs.value = [];
  }

  function syncAvailableChannels(sources) {
    const list = normalizeChannels(sources);
    if (!list.length) return;
    availableChannels.value = list;
  }

  async function searchChannel(channelKey, force = false) {
    if (!channelKey || !activeMedia.value || (!force && channelSearched.value[channelKey])) return;
    if (!detailVisible.value) return;
    if (force) {
      const mKey = getMediaCacheKey(activeMedia.value);
      if (mKey && mediaSearchMemoryCache.has(mKey)) {
        const entry = mediaSearchMemoryCache.get(mKey);
        delete entry.results?.[channelKey];
        delete entry.searched?.[channelKey];
        delete entry.elapsed?.[channelKey];
      }
    }

    // 中止该渠道之前的检索，避免重复堆叠请求
    if (channelAbortControllers.has(channelKey)) {
      try {
        channelAbortControllers.get(channelKey).abort();
      } catch (_) {
      }
      channelAbortControllers.delete(channelKey);
    }
    const controller = new AbortController();
    channelAbortControllers.set(channelKey, controller);

    channelLoading.value = {...channelLoading.value, [channelKey]: true};
    try {
      const media = activeMedia.value;
      const response = await api.value.post(
        `plugin/${pluginId.value}/resource/search_resources`,
        {
          source: channelKey,
          force: Boolean(force),
          force_refresh: Boolean(force),
          title: media.title,
          original_title: media.original_title || "",
          year: media.year || "",
          media_type: media.media_type || "movie",
          tmdb_id: media.tmdb_id || 0,
          imdb_id: media.imdb_id || "",
          tvdb_id: media.tvdb_id || 0,
          douban_id: media.douban_id || 0,
          bangumi_id: media.bangumi_id || 0,
          anilist_id: media.anilist_id || 0,
          anidb_id: media.anidb_id || 0,
          media_source: media.media_source || "",
          media_id: media.media_id || "",
        },
        {
          signal: controller.signal,
        },
      );
      if (!detailVisible.value) return;
      const result = unwrapApiResponse(response);
      channelResults.value = {...channelResults.value, [channelKey]: result?.success ? responseItems(result) : []};
      channelElapsed.value = {...channelElapsed.value, [channelKey]: result?.data?.elapsed ?? null};
      if (result?.success) {
        syncAvailableChannels(result.data?.available_sources);
        if (Array.isArray(result.data?.available_drives) && result.data.available_drives.length) {
          availableDrives.value = result.data.available_drives;
        }
        if (result.data?.main_cloud_drive && pluginConfig?.value)
          pluginConfig.value.cloud_drive = result.data.main_cloud_drive;

        // 缓存该媒体的渠道搜索结果
        const mKey = getMediaCacheKey(activeMedia.value);
        if (mKey) {
          const entry = mediaSearchMemoryCache.get(mKey) || {results: {}, searched: {}, elapsed: {}, time: Date.now()};
          entry.results[channelKey] = channelResults.value[channelKey];
          entry.searched[channelKey] = true;
          entry.elapsed[channelKey] = channelElapsed.value[channelKey];
          entry.time = Date.now();
          mediaSearchMemoryCache.set(mKey, entry);
        }
      } else {
        if (detailVisible.value) {
          showMessage?.(result?.message || `${getSourceName(channelKey)} 检索失败`, "warning");
        }
      }
    } catch (error) {
      channelResults.value = {...channelResults.value, [channelKey]: []};
      if (!detailVisible.value || isCancellationError(error)) {
        // 弹窗已关闭或主动取消请求：静默处理，避免弹出无意义的 http cancel 错误提示
        return;
      }
      const unavailable = error?.response?.status === 502 || String(error?.message || "").includes("502");
      showMessage?.(
        unavailable
          ? `${getSourceName(channelKey)} 渠道服务暂不可用 (502)，请稍后重试`
          : `${getSourceName(channelKey)} 搜索异常: ${error?.message || error}`,
        "warning",
      );
    } finally {
      channelAbortControllers.delete(channelKey);
      channelLoading.value = {...channelLoading.value, [channelKey]: false};
      if (detailVisible.value) {
        channelSearched.value = {...channelSearched.value, [channelKey]: true};
      }
    }
  }
  async function openMediaDetail(item) {
    abortAllActiveSearches();
    const key = getMediaCacheKey(item);
    const cached = mediaSearchMemoryCache.get(key);
    if (cached && Date.now() - cached.time < MEDIA_SEARCH_CACHE_TTL) {
      channelResults.value = {...(cached.results || {})};
      channelLoading.value = {};
      channelSearched.value = {...(cached.searched || {})};
      channelElapsed.value = {...(cached.elapsed || {})};
      activeResourceTab.value = "";
      resourceSearchQuery.value = "";
      selectedResourceSpecs.value = [];
    } else {
      resetChannelState();
    }
    try {
      const stillOpen = await openMedia(item);
      if (!stillOpen) return;
    } catch (error) {
      console.warn("[网盘资源] 获取媒体详情失败，使用榜单媒体信息继续", error);
    }
    if (!detailVisible.value) return;
    const seasons = activeMedia.value?.seasons || [];
    const defaultSeason =
      seasons.find((season) => {
        const episodes = Array.isArray(season?.episodes) ? season.episodes : [];
        return !episodes.length || episodes.some((episode) => !episode?.in_library);
      }) || seasons[0];
    activeDetailSeason.value = defaultSeason?.season_number || 1;
    const firstChannel = availableChannels.value[0]?.key || "";
    activeChannelTab.value = firstChannel;
    if (firstChannel && !channelSearched.value[firstChannel]) {
      await searchChannel(firstChannel);
    }
  }

  function onChannelTabChange(channelKey) {
    activeResourceTab.value = "";
    resourceSearchQuery.value = "";
    if (!channelSearched.value[channelKey] && !channelLoading.value[channelKey]) searchChannel(channelKey);
  }

  function getChannelCount(channelKey) {
    return channelKey === activeChannelTab.value
      ? currentChannelResources.value.length
      : (channelResults.value[channelKey] || []).length;
  }

  function closeMediaDetail() {
    detailVisible.value = false;
    abortAllActiveSearches();
    channelLoading.value = {};
  }

  watch(detailVisible, (visible) => {
    if (visible) return;
    requestToken += 1;
    detailLoading.value = false;
    abortAllActiveSearches();
    channelLoading.value = {};
  });

  onUnmounted(() => {
    abortAllActiveSearches();
    channelLoading.value = {};
  });

  watch(
    currentChannelResourceTabs,
    (tabs) => {
      const first = tabs[0]?.value || "";
      if (!tabs.length) {
        activeResourceTab.value = "";
      } else if (!tabs.some((tab) => tab.value === activeResourceTab.value)) {
        activeResourceTab.value = first;
      }
    },
    {immediate: true},
  );

  watch(
    availableChannels,
    (channels) => {
      const first = channels[0]?.key || "";
      if (!channels.some((channel) => channel.key === activeChannelTab.value)) {
        activeChannelTab.value = first;
      }
    },
    {immediate: true},
  );

  return {
    detailVisible,
    detailLoading,
    activeMedia,
    activeDetailSeason,
    availableChannels,
    availableDrives,
    activeChannelTab,
    activeResourceTab,
    resourceSearchQuery,
    selectedResourceSpecs,
    activeResourceFilterCount,
    resetResourceFilters,
    channelResults,
    channelLoading,
    channelSearched,
    channelElapsed,
    currentChannelResources,
    currentChannelResourceTabs,
    currentChannelFilteredResources,
    openMediaDetail,
    searchChannel,
    onChannelTabChange,
    getChannelCount,
    closeMediaDetail,
    syncAvailableChannels,
  };
}
