<template>
  <v-dialog
      :model-value="modelValue"
      :fullscreen="isMobile"
      max-width="680"
      class="manual-resource-dialog"
      @update:model-value="close"
  >
    <v-card rounded="lg" class="manual-resource-card">
      <v-card-title class="text-subtitle-1 d-flex align-center ga-2">
        <v-icon
            :icon="actionMode === 'upgrade' ? 'mdi-auto-fix' : 'mdi-link-variant-plus'"
            color="primary"
            size="small"
        />
        {{ actionMode === "upgrade" ? "媒体洗版" : "手动添加" }}
      </v-card-title>
      <v-card-text>
        <v-alert
            v-if="error"
            type="error"
            variant="tonal"
            density="compact"
            class="mb-3"
        >{{ error }}
        </v-alert>

        <v-btn-toggle
            v-model="actionMode"
            mandatory
            divided
            color="primary"
            density="compact"
            class="mb-4"
        >
          <v-btn value="links" prepend-icon="mdi-link-variant">添加资源</v-btn>
          <v-btn value="upgrade" prepend-icon="mdi-auto-fix">媒体洗版</v-btn>
        </v-btn-toggle>

        <template v-if="actionMode === 'links'">
          <v-btn-toggle
              v-if="!lockedInitialMedia"
              v-model="targetMode"
              mandatory
              divided
              density="compact"
              class="mb-3"
          >
            <v-btn value="subscribe">订阅卡片</v-btn>
            <v-btn value="tmdb">TMDB 媒体</v-btn>
          </v-btn-toggle>

          <v-text-field
              v-if="lockedInitialMedia"
              :model-value="lockedManualTargetLabel"
              :label="lockedSubscribe ? '订阅卡片' : '历史媒体'"
              variant="outlined"
              density="compact"
              readonly
              hide-details="auto"
              class="mb-3"
          />

          <v-autocomplete
              v-else-if="targetMode === 'subscribe'"
              v-model="subscribeId"
              v-model:search="subscribeSearch"
              :items="filteredSubscribes"
              item-title="title"
              item-value="value"
              :no-filter="true"
              label="指定订阅"
              placeholder="请选择电影或电视剧订阅"
              variant="outlined"
              density="compact"
              :loading="loadingOptions"
              :disabled="submitting"
              no-data-text="没有可用订阅，可切换到 TMDB 媒体"
              hide-details="auto"
              class="mb-3"
          />

          <template v-else>
            <div class="manual-search-row mb-3">
              <v-text-field
                  v-model="tmdbKeyword"
                  label="搜索 TMDB 媒体"
                  placeholder="输入电影或电视剧名称"
                  variant="outlined"
                  density="compact"
                  hide-details
                  :disabled="submitting"
                  @keyup.enter="searchTmdb"
              />
              <v-btn
                  color="primary"
                  variant="tonal"
                  :loading="searchingTmdb"
                  :disabled="!tmdbKeyword.trim() || submitting"
                  @click="searchTmdb"
              >搜索
              </v-btn>
            </div>
            <v-autocomplete
                v-model="selectedMedia"
                :items="tmdbCandidates"
                :item-title="candidateTitle"
                return-object
                label="选择 TMDB 结果"
                placeholder="请先搜索并选择准确媒体"
                variant="outlined"
                density="compact"
                :disabled="submitting"
                no-data-text="暂无 TMDB 候选"
                hide-details="auto"
                class="mb-3"
            />
            <div v-if="selectedMedia?.media_type === 'tv'" class="episode-range-fields mb-3">
              <v-text-field
                  v-model.number="season"
                  type="number"
                  min="1"
                  label="季"
                  variant="outlined"
                  density="compact"
                  hide-details
              />
              <v-text-field
                  v-model.number="episodeStart"
                  type="number"
                  min="1"
                  label="开始集"
                  variant="outlined"
                  density="compact"
                  hide-details
              />
              <v-text-field
                  v-model.number="episodeEnd"
                  type="number"
                  :min="episodeStart || 1"
                  label="结束集"
                  variant="outlined"
                  density="compact"
                  hide-details
              />
            </div>
          </template>

          <v-textarea
              v-model="resourceLinks"
              label="资源链接"
              placeholder="每行一个115分享、ED2K或Magnet链接"
              hint="支持单个或多个资源包，单次最多50条；无订阅时按所选 TMDB 媒体和剧集范围进入相同处理流程。"
              persistent-hint
              auto-grow
              rows="5"
              variant="outlined"
              density="compact"
              :disabled="submitting"
          />
          <v-checkbox
              v-model="manualUpgrade"
              label="将手动资源作为洗版候选"
              density="compact"
              hide-details
              class="mt-2"
              :disabled="submitting"
          />
        </template>

        <template v-else>
          <v-alert type="info" variant="tonal" density="compact" class="mb-3">
            在所选媒体服务器的全部媒体库中直接搜索，仅显示实际路径位于插件媒体根路径内的条目；也可通过 TMDB 快速精确匹配。
          </v-alert>
          <v-select
              v-model="mediaServer"
              :items="mediaServers"
              item-title="title"
              item-value="value"
              label="媒体服务器"
              placeholder="请选择 Emby 等媒体服务器"
              variant="outlined"
              density="compact"
              :loading="loadingMediaServers"
              :disabled="submitting"
              no-data-text="没有已启用的媒体服务器"
              hide-details="auto"
              class="mb-3"
          />
          <div v-if="lockedInitialMedia" class="manual-search-row mb-3">
            <v-text-field
                :model-value="lockedMediaLabel"
                label="历史媒体"
                variant="outlined"
                density="compact"
                readonly
                hide-details
            />
            <v-btn
                color="primary"
                variant="tonal"
                :loading="loadingMedia"
                :disabled="!mediaServer || submitting"
                @click="searchMediaContents(initialMedia)"
            >刷新
            </v-btn>
          </div>
          <div v-else class="manual-search-row mb-3">
            <v-text-field
                v-model="mediaKeyword"
                label="搜索媒体库内容"
                placeholder="输入电影或电视剧名称"
                variant="outlined"
                density="compact"
                hide-details
                :disabled="!mediaServer || submitting"
                @keyup.enter="searchMediaContents"
            />
            <v-btn
                color="primary"
                variant="tonal"
                :loading="loadingMedia"
                :disabled="!mediaServer || !mediaKeyword.trim() || submitting"
                @click="searchMediaContents"
            >搜索
            </v-btn>
            <v-btn
                color="secondary"
                variant="tonal"
                :loading="matchingUpgradeTmdb"
                :disabled="!mediaServer || !mediaKeyword.trim() || submitting"
                @click="matchUpgradeTmdb"
            >TMDB
            </v-btn>
          </div>
          <v-autocomplete
              v-if="!lockedInitialMedia && upgradeTmdbCandidates.length"
              v-model="selectedUpgradeTmdb"
              :items="upgradeTmdbCandidates"
              :item-title="candidateTitle"
              return-object
              label="TMDB 快速匹配"
              placeholder="选择准确条目后自动匹配所选服务器中的媒体"
              variant="outlined"
              density="compact"
              :disabled="submitting"
              no-data-text="没有 TMDB 候选"
              hide-details="auto"
              class="mb-3"
          />
          <v-card variant="outlined" class="media-selection-card">
            <div class="media-selection-header">
              <span class="text-body-2 font-weight-medium">选择媒体内容</span>
              <v-spacer/>
              <v-checkbox-btn
                  v-if="mediaContents.length"
                  :model-value="allMediaSelected"
                  :indeterminate="someMediaSelected && !allMediaSelected"
                  density="compact"
                  color="primary"
                  :disabled="submitting"
                  aria-label="全选媒体内容"
                  @update:model-value="toggleAllMediaItems"
              />
            </div>
            <v-divider/>
            <div v-if="loadingMedia" class="media-selection-state">
              <v-progress-circular indeterminate color="primary" size="28"/>
            </div>
            <v-list v-else-if="mediaContents.length" density="compact" class="media-selection-list">
              <v-list-item
                  v-for="item in mediaContents"
                  :key="mediaItemKey(item)"
                  :disabled="submitting"
                  @click="toggleMediaItem(item)"
              >
                <template #prepend>
                  <v-checkbox-btn
                      :model-value="isMediaItemSelected(item)"
                      density="compact"
                      color="primary"
                      tabindex="-1"
                      @click.stop="toggleMediaItem(item)"
                  />
                </template>
                <v-list-item-title>{{ mediaItemTitle(item) }}</v-list-item-title>
                <v-list-item-subtitle>{{ mediaItemSubtitle(item) }}</v-list-item-subtitle>
              </v-list-item>
            </v-list>
            <div v-else class="media-selection-state text-body-2 text-medium-emphasis">
              请先选择媒体服务器并搜索；不会跨服务器查询
            </div>
          </v-card>
          <div class="text-caption text-medium-emphasis mt-2">
            可勾选一个或多个电影、整季或单集；提交时会再次校验媒体服务器条目及实际路径。
          </div>
        </template>
      </v-card-text>
      <v-card-actions class="px-4 pb-3">
        <v-spacer/>
        <v-btn variant="text" :disabled="submitting" @click="close(false)">取消</v-btn>
        <v-btn
            color="primary"
            :prepend-icon="actionMode === 'upgrade' ? 'mdi-auto-fix' : 'mdi-play'"
            :loading="submitting"
            :disabled="active || !canSubmit"
            @click="requestSubmit"
        >{{ actionMode === "upgrade" ? "开始洗版" : "开始处理" }}
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
  <v-dialog v-model="confirmVisible" max-width="440" persistent>
    <v-card rounded="lg">
      <v-card-title class="text-subtitle-1">确认媒体洗版</v-card-title>
      <v-card-text>
        确认对所选 {{ selectedMediaItems.length }} 个媒体内容发起洗版？
        <v-alert type="warning" variant="tonal" density="compact" class="mt-3">
          将按插件洗版设置比较候选版本；符合条件时会进入替换或共存流程。
        </v-alert>
      </v-card-text>
      <v-card-actions>
        <v-spacer/>
        <v-btn variant="text" :disabled="submitting" @click="confirmVisible = false">
          取消
        </v-btn>
        <v-btn color="warning" :loading="submitting" @click="submit">
          确认洗版
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup>
import {computed, ref, watch} from "vue";
import {useDisplay} from "vuetify";

const props = defineProps({
  modelValue: Boolean,
  api: {type: [Object, Function], required: true},
  pluginId: {type: String, default: "CloudSubscribe"},
  active: Boolean,
  initialMode: {type: String, default: "links"},
  initialMedia: {type: Object, default: null},
});
const emit = defineEmits(["update:modelValue", "started"]);
const display = useDisplay();
const isMobile = computed(() => display.xs.value);
const subscribes = ref([]);
const subscribeSearch = ref("");
const subscribeId = ref(null);
const actionMode = ref("links");
const targetMode = ref("subscribe");
const resourceLinks = ref("");
const manualUpgrade = ref(false);
const tmdbKeyword = ref("");
const tmdbCandidates = ref([]);
const selectedMedia = ref(null);
const season = ref(1);
const episodeStart = ref(1);
const episodeEnd = ref(1);
const mediaServers = ref([]);
const mediaServer = ref(null);
const mediaKeyword = ref("");
const mediaContents = ref([]);
const selectedMediaItems = ref([]);
const upgradeTmdbCandidates = ref([]);
const selectedUpgradeTmdb = ref(null);
const loadingOptions = ref(false);
const loadingMediaServers = ref(false);
const loadingMedia = ref(false);
const searchingTmdb = ref(false);
const matchingUpgradeTmdb = ref(false);
const submitting = ref(false);
const confirmVisible = ref(false);
const error = ref("");

const filteredSubscribes = computed(() => {
  const keyword = normalizeSearchText(subscribeSearch.value);
  if (!keyword) return subscribes.value;
  return subscribes.value.filter((item) =>
      normalizeSearchText(`${item?.title || ""} ${item?.value || ""}`).includes(keyword),
  );
});

const selectedMediaKeys = computed(() =>
    new Set(selectedMediaItems.value.map(mediaItemKey)),
);

const allMediaSelected = computed(() =>
    mediaContents.value.length > 0 &&
    mediaContents.value.every((item) => selectedMediaKeys.value.has(mediaItemKey(item))),
);

const someMediaSelected = computed(() =>
    mediaContents.value.some((item) => selectedMediaKeys.value.has(mediaItemKey(item))),
);

const lockedInitialMedia = computed(() =>
    props.initialMode === "upgrade" && Boolean(props.initialMedia?.title),
);

const lockedMediaLabel = computed(() => {
  if (!lockedInitialMedia.value) return "";
  const media = props.initialMedia || {};
  const title = `${media.title || "未知媒体"}${media.year ? ` (${media.year})` : ""}`;
  const type = media.media_type === "movie"
      ? "电影"
      : media.media_type === "tv"
          ? "电视剧"
          : "";
  return [title, type].filter(Boolean).join(" · ");
});

const lockedSubscribe = computed(() => {
  if (!lockedInitialMedia.value || !subscribeId.value) return null;
  return subscribes.value.find((item) => item.value === subscribeId.value) || null;
});

const lockedManualTargetLabel = computed(() =>
    lockedSubscribe.value?.title || lockedMediaLabel.value,
);

const canSubmit = computed(() => {
  if (submitting.value) return false;
  if (actionMode.value === "upgrade") return selectedMediaItems.value.length > 0;
  if (!resourceLinks.value.trim()) return false;
  if (targetMode.value === "subscribe") return Boolean(subscribeId.value);
  if (!selectedMedia.value?.tmdb_id) return false;
  if (selectedMedia.value.media_type !== "tv") return true;
  return Number(season.value) > 0 && Number(episodeStart.value) > 0 &&
      Number(episodeEnd.value) >= Number(episodeStart.value);
});

function normalizeSearchText(value) {
  return String(value ?? "")
      .normalize("NFKC")
      .toLocaleLowerCase()
      .replace(/\s+/g, "");
}

function unwrap(result) {
  return result?.data && typeof result.data === "object" && "success" in result.data
      ? result.data
      : result || {};
}

function candidateTitle(item) {
  if (!item) return "";
  const year = item.year ? ` (${item.year})` : "";
  return `${item.title || "未知媒体"}${year} · ${item.media_type_name || ""}`;
}

function mediaItemKey(item) {
  return [item?.server, item?.item_id, item?.kind, item?.season, item?.episode]
      .map((value) => String(value ?? ""))
      .join(":");
}

function mediaItemTitle(item) {
  const title = String(item?.title || "未知媒体");
  const season = Number(item?.season || 0);
  const episode = Number(item?.episode || 0);
  if (item?.kind === "season") return `${title} S${String(season).padStart(2, "0")} · 整季`;
  if (item?.kind === "episode") {
    return `${title} S${String(season).padStart(2, "0")}E${String(episode).padStart(2, "0")}`;
  }
  return item?.year ? `${title} (${item.year})` : title;
}

function mediaItemSubtitle(item) {
  const details = [];
  if (item?.kind !== "movie" && item?.year) details.push(String(item.year));
  if (item?.path) details.push(String(item.path));
  return details.join(" · ");
}

function isMediaItemSelected(item) {
  return selectedMediaKeys.value.has(mediaItemKey(item));
}

function toggleMediaItem(item) {
  const key = mediaItemKey(item);
  selectedMediaItems.value = isMediaItemSelected(item)
      ? selectedMediaItems.value.filter((selected) => mediaItemKey(selected) !== key)
      : [...selectedMediaItems.value, item];
}

function toggleAllMediaItems(selected) {
  selectedMediaItems.value = selected ? [...mediaContents.value] : [];
}

function matchInitialSubscribe() {
  subscribeId.value = null;
  if (!props.initialMedia) return;
  const media = props.initialMedia;
  const targetTmdbId = Number(media.tmdb_id || 0);
  const targetSeason = Number(media.season || 0);
  const targetType = String(media.media_type || "");
  const targetTitle = normalizeSearchText(media.title);
  const targetYear = String(media.year || "").trim();
  const matches = subscribes.value.filter((item) => {
    const itemType = String(item.media_type || "");
    if (targetType && itemType && itemType !== targetType) return false;
    if (targetTmdbId && Number(item.tmdb_id || 0) === targetTmdbId) return true;
    return normalizeSearchText(item.name) === targetTitle &&
        (!targetYear || !item.year || String(item.year) === targetYear);
  });
  const seasonMatch = targetSeason > 0
      ? matches.find((item) => Number(item.season || 0) === targetSeason)
      : null;
  subscribeId.value = (seasonMatch || matches[0])?.value || null;
  if (subscribeId.value) {
    targetMode.value = "subscribe";
    selectedMedia.value = null;
  } else {
    targetMode.value = "tmdb";
    selectedMedia.value = {
      ...media,
      media_type_name: media.media_type === "movie" ? "电影" : "电视剧",
    };
  }
}

async function resolveInitialMediaFallback() {
  if (
      !lockedInitialMedia.value ||
      subscribeId.value ||
      selectedMedia.value?.tmdb_id ||
      !props.initialMedia?.title
  ) return;
  const media = props.initialMedia;
  try {
    const result = unwrap(await props.api.post(`plugin/${props.pluginId}/search/tmdb`, {
      title: media.title,
    }));
    if (result.success === false) throw new Error(result.message || "TMDB 匹配失败");
    const candidates = Array.isArray(result.data?.items) ? result.data.items : [];
    const targetTitle = normalizeSearchText(media.title);
    const targetYear = String(media.year || "").trim();
    const targetType = String(media.media_type || "");
    const matched = candidates.find((item) =>
        (!targetType || item.media_type === targetType) &&
        (!targetYear || !item.year || String(item.year) === targetYear) &&
        normalizeSearchText(item.title) === targetTitle,
    ) || candidates.find((item) =>
        (!targetType || item.media_type === targetType) &&
        (!targetYear || !item.year || String(item.year) === targetYear),
    );
    if (!matched?.tmdb_id) throw new Error("未找到对应的 TMDB 媒体");
    selectedMedia.value = {
      ...matched,
      ...media,
      tmdb_id: matched.tmdb_id,
      media_type: media.media_type || matched.media_type,
    };
  } catch (e) {
    error.value = e.message || "历史媒体自动匹配失败";
  }
}

async function loadOptions() {
  loadingOptions.value = true;
  error.value = "";
  try {
    const result = unwrap(await props.api.get(`plugin/${props.pluginId}/ui_options`));
    if (result.success === false) throw new Error(result.message || "加载订阅失败");
    const data = result.data?.data || result.data || result;
    subscribes.value = Array.isArray(data.subscribes) ? data.subscribes : [];
  } catch (e) {
    error.value = e.message || "加载订阅失败";
  } finally {
    loadingOptions.value = false;
  }
}

async function loadMediaServers() {
  if (loadingMediaServers.value || mediaServers.value.length) return;
  loadingMediaServers.value = true;
  error.value = "";
  try {
    const result = unwrap(await props.api.get(`plugin/${props.pluginId}/media/content`));
    if (result.success === false) throw new Error(result.message || "加载媒体服务器失败");
    mediaServers.value = result.data?.servers || [];
    if (mediaServers.value.length === 1) mediaServer.value = mediaServers.value[0].value;
  } catch (e) {
    error.value = e.message || "加载媒体服务器失败";
  } finally {
    loadingMediaServers.value = false;
  }
}

async function searchMediaContents(tmdb = null) {
  if (!mediaServer.value || (!tmdb?.tmdb_id && !mediaKeyword.value.trim()) || loadingMedia.value) return;
  const selectedServer = mediaServer.value;
  loadingMedia.value = true;
  error.value = "";
  try {
    const params = {server: mediaServer.value};
    if (tmdb?.tmdb_id) {
      params.tmdb_id = String(tmdb.tmdb_id);
      params.media_type = tmdb.media_type || "";
    } else {
      params.keyword = mediaKeyword.value.trim();
    }
    const query = new URLSearchParams(params);
    const result = unwrap(
        await props.api.get(`plugin/${props.pluginId}/media/content?${query}`),
    );
    if (mediaServer.value !== selectedServer) return;
    if (result.success === false) throw new Error(result.message || "搜索媒体库失败");
    mediaServers.value = result.data?.servers || mediaServers.value;
    mediaContents.value = result.data?.items || [];
    selectedMediaItems.value = [];
  } catch (e) {
    error.value = e.message || "搜索媒体库失败";
  } finally {
    loadingMedia.value = false;
  }
}

async function matchUpgradeTmdb() {
  if (!mediaServer.value || !mediaKeyword.value.trim() || matchingUpgradeTmdb.value) return;
  const selectedServer = mediaServer.value;
  matchingUpgradeTmdb.value = true;
  error.value = "";
  try {
    const result = unwrap(await props.api.post(`plugin/${props.pluginId}/search/tmdb`, {
      title: mediaKeyword.value.trim(),
    }));
    if (mediaServer.value !== selectedServer) return;
    if (result.success === false) throw new Error(result.message || "TMDB 匹配失败");
    upgradeTmdbCandidates.value = result.data?.items || [];
    selectedUpgradeTmdb.value = upgradeTmdbCandidates.value.length === 1
        ? upgradeTmdbCandidates.value[0]
        : null;
  } catch (e) {
    error.value = e.message || "TMDB 匹配失败";
  } finally {
    matchingUpgradeTmdb.value = false;
  }
}

async function searchTmdb() {
  if (!tmdbKeyword.value.trim() || searchingTmdb.value) return;
  searchingTmdb.value = true;
  error.value = "";
  try {
    const result = unwrap(await props.api.post(`plugin/${props.pluginId}/search/tmdb`, {
      title: tmdbKeyword.value.trim(),
    }));
    if (result.success === false) throw new Error(result.message || "TMDB 搜索失败");
    tmdbCandidates.value = result.data?.items || [];
    selectedMedia.value = tmdbCandidates.value.length === 1 ? tmdbCandidates.value[0] : null;
  } catch (e) {
    error.value = e.message || "TMDB 搜索失败";
  } finally {
    searchingTmdb.value = false;
  }
}

function close(value) {
  if (submitting.value) return;
  confirmVisible.value = false;
  emit("update:modelValue", Boolean(value));
}

function requestSubmit() {
  if (!canSubmit.value) return;
  if (actionMode.value === "upgrade") {
    confirmVisible.value = true;
    return;
  }
  submit();
}

async function submit() {
  if (!canSubmit.value) return;
  submitting.value = true;
  error.value = "";
  try {
    let result;
    if (actionMode.value === "upgrade") {
      result = unwrap(await props.api.post(`plugin/${props.pluginId}/history/upgrade`, {
        source: "media_server",
        items: selectedMediaItems.value.map((item) => ({
          server: item.server,
          item_id: item.item_id,
          kind: item.kind,
          season: item.season,
          episode: item.episode,
        })),
      }));
    } else {
      const media = selectedMedia.value ? {
        ...selectedMedia.value,
        season: Number(season.value),
        episode_start: Number(episodeStart.value),
        episode_end: Number(episodeEnd.value),
      } : null;
      result = unwrap(await props.api.post(`plugin/${props.pluginId}/sync/manual`, {
        subscribe_id: targetMode.value === "subscribe" ? subscribeId.value : null,
        media: targetMode.value === "tmdb" ? media : null,
        resource_links: resourceLinks.value.split(/\r?\n/),
        manual_upgrade: manualUpgrade.value,
      }));
    }
    if (result.success === false) throw new Error(result.message || "提交失败");
    emit("started", result.message || "任务已启动");
    resourceLinks.value = "";
    manualUpgrade.value = false;
    selectedMediaItems.value = [];
    confirmVisible.value = false;
    emit("update:modelValue", false);
  } catch (e) {
    error.value = e.message || "提交失败";
  } finally {
    submitting.value = false;
  }
}

watch(actionMode, (value) => {
  error.value = "";
  if (value === "upgrade") loadMediaServers();
});

watch(mediaServer, () => {
  mediaContents.value = [];
  selectedMediaItems.value = [];
  upgradeTmdbCandidates.value = [];
  selectedUpgradeTmdb.value = null;
  if (
      props.modelValue &&
      actionMode.value === "upgrade" &&
      mediaServer.value &&
      lockedInitialMedia.value
  ) {
    searchMediaContents(props.initialMedia?.tmdb_id ? props.initialMedia : null);
  }
});

watch(mediaKeyword, () => {
  upgradeTmdbCandidates.value = [];
  selectedUpgradeTmdb.value = null;
});

watch(selectedUpgradeTmdb, (value) => {
  if (value?.tmdb_id) searchMediaContents(value);
});

watch(
    () => props.modelValue,
    async (visible) => {
      if (visible) {
        actionMode.value = props.initialMode === "upgrade" ? "upgrade" : "links";
        targetMode.value = "subscribe";
        mediaKeyword.value = String(props.initialMedia?.title || "").trim();
        tmdbKeyword.value = "";
        tmdbCandidates.value = [];
        selectedMedia.value = null;
        mediaContents.value = [];
        selectedMediaItems.value = [];
        upgradeTmdbCandidates.value = [];
        selectedUpgradeTmdb.value = null;
        confirmVisible.value = false;
        subscribeSearch.value = "";
        subscribeId.value = null;
        await loadOptions();
        matchInitialSubscribe();
        await resolveInitialMediaFallback();
        if (actionMode.value === "upgrade") {
          await loadMediaServers();
          if (mediaServer.value && lockedInitialMedia.value && !loadingMedia.value) {
            await searchMediaContents(
                props.initialMedia?.tmdb_id ? props.initialMedia : null,
            );
          }
        }
      } else {
        error.value = "";
        confirmVisible.value = false;
      }
    },
    {immediate: true},
);
</script>

<style scoped>
.media-selection-card {
  overflow: hidden;
}

.manual-resource-card {
  max-height: min(760px, calc(100dvh - 32px));
}

.manual-search-row,
.episode-range-fields {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.media-selection-header {
  display: flex;
  min-height: 42px;
  align-items: center;
  padding: 4px 10px 4px 14px;
}

.media-selection-list {
  max-height: min(320px, 42vh);
  overflow-y: auto;
  overscroll-behavior: contain;
}

.media-selection-state {
  display: flex;
  min-height: 96px;
  align-items: center;
  justify-content: center;
  padding: 16px;
  text-align: center;
}

@media (max-width: 600px) {
  .manual-resource-card {
    width: 100%;
    height: 100dvh;
    max-height: 100dvh;
    border-radius: 0 !important;
  }

  .manual-resource-card > :deep(.v-card-text) {
    min-height: 0;
    flex: 1 1 auto;
    overflow-y: auto;
    padding: 12px;
  }

  .manual-resource-card > :deep(.v-card-title) {
    padding: 12px;
  }

  .manual-resource-card > :deep(.v-card-actions) {
    flex: 0 0 auto;
    padding: 8px 12px 12px !important;
  }

  .manual-resource-card :deep(.v-btn-toggle) {
    display: grid;
    width: 100%;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .manual-resource-card :deep(.v-btn-toggle .v-btn) {
    min-width: 0;
  }

  .manual-search-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
  }

  .manual-search-row > :deep(.v-text-field) {
    grid-column: 1 / -1;
  }

  .manual-search-row > :deep(.v-btn) {
    width: 100%;
  }

  .episode-range-fields {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .media-selection-list {
    max-height: min(42vh, 360px);
  }

  .media-selection-list :deep(.v-list-item) {
    padding-inline: 8px;
  }

  .media-selection-list :deep(.v-list-item-subtitle) {
    white-space: normal;
    overflow-wrap: anywhere;
  }
}
</style>
