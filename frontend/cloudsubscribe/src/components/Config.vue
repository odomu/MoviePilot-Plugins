<template>
  <div class="cloud-subscribe-config">
    <v-card flat class="border rounded config-shell">
      <v-card-title
          class="config-header d-flex align-center ga-1 px-3 py-2 bg-primary-lighten-5"
      >
        <v-icon
            icon="mdi-cloud-cog-outline"
            color="primary"
            size="small"
            class="mr-1"
        />
        <span class="config-title text-subtitle-1">网盘订阅助手</span>
        <v-spacer/>
        <v-btn
            v-if="showSwitch"
            class="config-header-action"
            variant="text"
            size="small"
            prepend-icon="mdi-arrow-left"
            title="返回详情"
            @click="emit('switch')"
        >返回详情
        </v-btn>
        <v-btn
            class="config-header-action"
            variant="text"
            size="small"
            prepend-icon="mdi-close"
            title="关闭"
            @click="emit('close')"
        >关闭
        </v-btn>
      </v-card-title>
      <v-card-text class="pa-0 config-body">
        <v-tabs
            v-model="activeTab"
            color="primary"
            density="compact"
            show-arrows
            class="config-tabs border-b"
        >
          <v-tab
              v-for="section in sections"
              :key="section.value"
              :value="section.value"
          >
            <v-icon :icon="section.icon" size="small" class="mr-2"/>
            {{ section.title }}
          </v-tab>
        </v-tabs>
        <div class="config-content-scroll">
          <div class="config-window">
            <section
                v-for="section in sections"
                :key="section.value"
                v-show="activeTab === section.value"
                class="config-window-section"
            >
              <ConfigSection
                  :section="section"
                  :config="config"
                  :refreshing-account="refreshingAccount"
                  :testing-source="testingSource"
                  :hdhive-oauth-action="hdhiveOauthAction"
                  @scan="openQrCode"
                  @browse-directory="openDirectoryPicker"
                  @test-source="openSourceTest"
                  @refresh-account="refreshAccount"
                  @hdhive-oauth-start="startHdhiveOAuth"
                  @hdhive-oauth-exchange="exchangeHdhiveOAuth"
                  @copy-text="copyText"
              />
            </section>
          </div>
        </div>
      </v-card-text>
      <v-divider/>
      <v-card-actions class="config-actions px-4 py-3">
        <v-progress-linear
            v-if="saving"
            class="save-progress"
            color="primary"
            indeterminate
        />
        <v-slide-y-transition>
          <div v-if="saving" class="save-state" aria-live="polite">
            <v-progress-circular
                indeterminate
                size="16"
                width="2"
                color="primary"
            />
            正在保存配置
          </div>
        </v-slide-y-transition>
        <v-spacer/>
        <v-btn
            color="primary"
            class="save-config-button"
            variant="flat"
            elevation="2"
            prepend-icon="mdi-content-save-check-outline"
            :loading="saving"
            @click="save"
        >保存配置
        </v-btn>
      </v-card-actions>
    </v-card>
    <QrCodeDialog
        v-show="qrVisible"
        v-model="qrVisible"
        :api="api"
        :provider="qrProvider"
        @success="handleQrSuccess"
    />
    <CloudDirectoryDialog
        v-show="directoryVisible"
        v-model="directoryVisible"
        :api="api"
        :provider="directoryProvider"
        :initial-path="directoryInitialPath"
        @select="selectDirectory"
    />
    <v-dialog
        v-model="sourceTestVisible"
        max-width="640"
        class="source-test-dialog"
    >
      <v-card
          class="source-test-card"
          :class="{ 'source-test-card--results': tmdbSearched || testSubmitted }"
      >
        <v-card-title class="source-test-header d-flex align-center ga-2">
          <v-icon icon="mdi-flask-outline" color="primary"/>
          {{ sourceNames[sourceTest.source] || "搜索渠道" }}测试
          <v-spacer/>
          <v-btn
              icon="mdi-close"
              size="small"
              variant="text"
              title="关闭"
              @click="sourceTestVisible = false"
          />
        </v-card-title>
        <v-form class="source-test-form" @submit.prevent="searchTmdbCandidates">
          <v-card-text class="source-test-body">
            <v-row dense class="source-test-fields">
              <v-col cols="12" sm="8">
                <v-text-field
                    v-model="sourceTest.title"
                    label="媒体名称"
                    maxlength="100"
                    autofocus
                    clearable
                    hide-details
                />
              </v-col>
              <v-col cols="12" sm="4">
                <v-text-field
                    v-model="sourceTest.season"
                    label="电视剧季号"
                    type="number"
                    min="1"
                    max="999"
                    hide-details
                />
              </v-col>
            </v-row>
            <v-alert
                v-if="testError"
                type="error"
                variant="tonal"
                density="compact"
                class="source-test-error mt-3"
            >
              {{ testError }}
              <span v-if="testElapsed != null">（耗时 {{ formatElapsed(testElapsed) }}）</span>
            </v-alert>
            <div
                v-if="tmdbSearched && !testSubmitted"
                class="source-test-tmdb mt-3"
            >
              <div class="text-caption text-medium-emphasis mb-2">
                选择 TMDB 条目后将立即测试 {{ sourceNames[sourceTest.source] }}
              </div>
              <div v-if="testingSource" class="source-test-loading">
                <v-progress-circular
                    indeterminate
                    color="primary"
                    size="42"
                    width="4"
                />
                <div class="text-body-2 mt-3">
                  正在读取 {{ sourceNames[sourceTest.source] }} 资源
                </div>
                <div class="text-caption text-medium-emphasis mt-1">
                  仅执行只读搜索，不会解锁、转存或处理文件
                </div>
              </div>
              <div v-else class="source-test-tmdb-scroll">
                <v-list
                    v-if="tmdbCandidates.length"
                    density="compact"
                    lines="two"
                >
                  <v-list-item
                      v-for="item in tmdbCandidates"
                      :key="`${item.media_type}-${item.tmdb_id}`"
                      :title="item.title"
                      :subtitle="tmdbCandidateSubtitle(item)"
                      :disabled="Boolean(testingSource)"
                      rounded="lg"
                      class="source-test-tmdb-item mb-1"
                      @click="testSource(item)"
                  >
                    <template #prepend>
                      <v-avatar rounded="lg" size="48" color="surface-variant">
                        <v-img v-if="item.poster" :src="item.poster" cover/>
                        <v-icon
                            v-else
                            :icon="
                            item.media_type === 'movie'
                              ? 'mdi-movie-open-outline'
                              : 'mdi-television-classic'
                          "
                        />
                      </v-avatar>
                    </template>
                    <template #append>
                      <v-progress-circular
                          v-if="selectedTmdbId === item.tmdb_id"
                          indeterminate
                          color="primary"
                          size="20"
                          width="2"
                      />
                      <v-chip
                          v-else
                          size="x-small"
                          variant="tonal"
                          color="primary"
                      >
                        TMDB {{ item.tmdb_id }}
                      </v-chip>
                    </template>
                  </v-list-item>
                </v-list>
                <v-alert v-else type="info" variant="tonal" density="compact">
                  TMDB 未找到匹配条目
                </v-alert>
              </div>
            </div>
            <div v-if="testSubmitted" class="source-test-result">
              <div class="source-test-summary">
                <div class="source-test-summary__line">
                  <span class="source-test-summary__context">
                    {{ testResult.media || "搜索结果" }}
                  </span>
                  <span>
                    本次获取 <strong>{{ testResult.count || 0 }}</strong> 个搜索结果
                  </span>
                  <span>
                    当前展示
                    <strong>{{ testResult.displayed_count ?? (testResult.items || []).length }}</strong>
                    个
                  </span>
                  <span v-if="testResult.elapsed_seconds != null">
                    耗时 <strong>{{ formatElapsed(testResult.elapsed_seconds) }}</strong>
                  </span>
                </div>
              </div>
              <div class="source-test-notice text-medium-emphasis mb-3">
                搜索测试固定最多获取并展示 {{ testResult.display_limit }} 个候选，不受正式搜索候选上限配置影响
              </div>
              <v-tabs
                  v-if="testResourceTabs.length > 1"
                  v-model="activeTestResourceType"
                  density="compact"
                  color="primary"
                  show-arrows
                  class="source-test-tabs mb-2"
              >
                <v-tab
                    v-for="tab in testResourceTabs"
                    :key="tab.value"
                    :value="tab.value"
                >
                  {{ tab.title }}
                  <v-badge
                      :content="tab.count"
                      inline
                      color="primary"
                      class="ml-1"
                  />
                </v-tab>
              </v-tabs>
              <div class="source-test-result-scroll">
                <v-list
                    v-if="filteredTestItems.length"
                    density="compact"
                    class="source-test-result-list"
                >
                  <v-list-item
                      v-for="(item, index) in filteredTestItems"
                      :key="`${item.title}-${index}`"
                      class="source-test-result-item"
                  >
                    <div class="source-test-item-content">
                      <div class="source-test-item-title">{{ item.title }}</div>
                      <div class="source-test-item-meta">
                        <v-chip
                            size="x-small"
                            variant="tonal"
                            color="primary"
                            :href="item.source_url || undefined"
                            :target="item.source_url ? '_blank' : undefined"
                            :rel="
                            item.source_url ? 'noopener noreferrer' : undefined
                          "
                            :title="item.source_url ? '打开来源资源页' : undefined"
                        >
                          {{
                            item.source_name ||
                            testResult.source_name ||
                            "未知来源"
                          }}
                          <v-icon
                              v-if="item.source_url"
                              icon="mdi-open-in-new"
                              size="12"
                              class="ml-1"
                          />
                        </v-chip>
                        <v-chip size="x-small" variant="tonal">
                          {{
                            item.resource_type_name ||
                            item.resource_type ||
                            "未知类型"
                          }}
                        </v-chip>
                        <v-chip
                            v-if="testItemStatus(item)"
                            size="x-small"
                            variant="tonal"
                            :color="testItemStatus(item).color"
                        >
                          {{ testItemStatus(item).label }}
                        </v-chip>
                        <span class="text-medium-emphasis">
                          {{ item.size || "大小未知" }}
                        </span>
                        <v-chip
                            v-for="tag in item.tags || []"
                            :key="`${item.title}-${tag}`"
                            size="x-small"
                            variant="outlined"
                        >
                          {{ tag }}
                        </v-chip>
                        <div class="source-test-item-actions">
                          <v-btn
                              v-if="canAccessHdhiveResource(item)"
                              icon="mdi-link-variant-plus"
                              size="x-small"
                              variant="text"
                              title="获取资源链接"
                              :loading="accessingResource === previewResourceKey(item)"
                              :disabled="Boolean(previewingUrl) || Boolean(accessingResource)"
                              @click="accessResource(item)"
                          />
                          <v-btn
                              v-if="canPreviewResource(item)"
                              icon="mdi-eye-outline"
                              size="x-small"
                              variant="text"
                              :title="String(item?.source || '').toLowerCase() === 'hdhive'
                                ? (item?.is_unlocked ? '预览已解锁分享内容' : '只读预览资源内容')
                                : '预览资源内容'"
                              :loading="previewingUrl === previewResourceKey(item)"
                              :disabled="Boolean(previewingUrl) || Boolean(accessingResource)"
                              @click="previewResource(item)"
                          />
                          <v-btn
                              v-if="item.url"
                              icon="mdi-content-copy"
                              size="x-small"
                              variant="text"
                              title="复制资源链接"
                              @click="copyText(item.url, '资源链接')"
                          />
                          <v-btn
                              v-if="item.need_unlock"
                              icon="mdi-lock-open-outline"
                              size="x-small"
                              variant="text"
                              color="warning"
                              :title="`确认消耗 ${Number(item.unlock_points || 0)} 积分解锁`"
                              @click="confirmUnlock(item)"
                          />
                        </div>
                      </div>
                    </div>
                  </v-list-item>
                </v-list>
                <v-alert v-else type="info" variant="tonal" density="compact">
                  当前渠道未找到候选资源
                </v-alert>
              </div>
            </div>
          </v-card-text>
          <v-card-actions class="source-test-actions">
            <v-spacer/>
            <v-btn
                type="submit"
                color="primary"
                variant="flat"
                prepend-icon="mdi-magnify"
                :loading="searchingTmdb"
                :disabled="
                Boolean(testingSource) ||
                searchingTmdb ||
                !String(sourceTest.title || '').trim()
              "
            >
              搜索资源
            </v-btn>
          </v-card-actions>
        </v-form>
      </v-card>
    </v-dialog>
    <v-dialog v-model="previewVisible" max-width="720">
      <v-card class="source-preview-card">
        <v-card-title class="d-flex align-center ga-2">
          <v-icon icon="mdi-file-tree-outline" color="primary"/>
          <span>资源内容预览</span>
          <v-spacer/>
          <a
              v-if="previewMeta.share_url"
              class="source-preview-header-link text-caption"
              :href="previewMeta.share_url"
              target="_blank"
              rel="noopener noreferrer"
              :title="previewMeta.share_url"
          >
            {{ previewMeta.share_url }}
          </a>
          <v-btn
              v-if="previewMeta.share_url"
              icon="mdi-content-copy"
              size="small"
              variant="text"
              title="复制网盘链接"
              @click="copyText(previewMeta.share_url, '网盘链接')"
          />
          <v-btn icon="mdi-close" size="small" variant="text" title="关闭" @click="previewVisible = false"/>
        </v-card-title>
        <v-card-text class="source-preview-body">
          <div
              v-if="previewMeta.display_name || previewMeta.info_hash || previewMeta.size || previewMeta.provider_name || previewMeta.share_url"
              class="source-preview-meta text-caption text-medium-emphasis">
            <span v-if="previewMeta.provider_name">网盘: {{ previewMeta.provider_name }}</span>
            <span v-if="previewMeta.resource_type_name">类型: {{ previewMeta.resource_type_name }}</span>
            <span v-if="previewMeta.display_name" class="source-preview-meta__title">标题: {{
                previewMeta.display_name
              }}</span>
            <span v-if="previewMeta.info_hash">Info Hash: {{ previewMeta.info_hash }}</span>
            <span v-if="previewMeta.size">总大小: {{ formatPreviewSize(previewMeta.size) }}</span>
            <span>当前层项目数: {{ previewItems.length }}</span>
          </div>
          <div v-if="previewBreadcrumbs.length > 1" class="source-preview-breadcrumbs">
            <template v-for="(breadcrumb, index) in previewBreadcrumbs" :key="`${breadcrumb.id}-${index}`">
              <v-icon v-if="index" icon="mdi-chevron-right" size="small"/>
              <v-btn size="small" variant="text" :disabled="previewLoading || index === previewBreadcrumbs.length - 1"
                     @click="openPreviewBreadcrumb(index)">
                {{ breadcrumb.name }}
              </v-btn>
            </template>
          </div>
          <div v-if="previewLoading" class="source-preview-loading">
            <v-progress-circular indeterminate color="primary" size="44" width="4"/>
          </div>
          <v-alert v-if="previewError" type="error" variant="tonal" density="compact" class="my-3">
            {{ previewError }}
          </v-alert>
          <div v-else-if="!previewLoading && previewItems.length" class="source-preview-list-scroll">
            <v-list density="compact" lines="one" class="source-preview-list">
              <template v-for="(file, index) in previewItems" :key="`${file.name}-${index}`">
                <v-list-item class="source-preview-file" :class="{'source-preview-file--directory': file.can_enter}"
                             @click="file.can_enter && openPreviewFolder(file)">
                  <template #prepend>
                    <v-icon :icon="previewFileIcon(file)"/>
                  </template>
                  <v-list-item-title class="source-preview-file-name" :title="file.name">
                    <span v-if="file.is_dir" class="source-preview-file-stem">{{ file.name }}</span>
                    <template v-else><span class="source-preview-file-stem">{{ previewFileStem(file.name) }}</span><span
                        class="source-preview-file-extension">{{ previewFileExtension(file.name) }}</span></template>
                  </v-list-item-title>
                  <template #append>
                <span v-if="formatPreviewSize(file.size)"
                      class="source-preview-file-size text-caption text-medium-emphasis">
                  {{ formatPreviewSize(file.size) }}
                </span>
                    <v-icon v-if="file.can_enter" icon="mdi-chevron-right" size="small" class="ml-2"/>
                  </template>
                </v-list-item>
                <v-divider v-if="index < previewItems.length - 1"/>
              </template>
            </v-list>
          </div>
          <v-alert v-else-if="!previewLoading && !previewError" type="info" variant="tonal" density="compact">
            当前目录为空
          </v-alert>
        </v-card-text>
      </v-card>
    </v-dialog>
    <v-dialog v-model="unlockVisible" max-width="440" :persistent="unlocking">
      <v-card>
        <v-card-title class="d-flex align-center ga-2">
          <v-icon icon="mdi-lock-open-outline" color="warning"/>
          解锁资源
        </v-card-title>
        <v-card-text>
          <p class="mb-3">确认消耗 {{ Number(unlockItem?.unlock_points || 0) }} 积分解锁此资源？</p>
          <div class="text-body-2 text-medium-emphasis text-truncate" :title="unlockItem?.title || ''">
            {{ unlockItem?.title || "未命名资源" }}
          </div>
          <v-alert v-if="unlockError" type="error" variant="tonal" density="compact" class="mt-4">
            {{ unlockError }}
          </v-alert>
        </v-card-text>
        <v-card-actions>
          <v-spacer/>
          <v-btn variant="text" :disabled="unlocking" @click="unlockVisible = false">取消</v-btn>
          <v-btn color="warning" variant="flat" prepend-icon="mdi-lock-open-outline" :loading="unlocking"
                 @click="unlockResource">确认解锁
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
    <v-snackbar
        v-model="messageVisible"
        :color="messageType"
        location="top end"
        timeout="3500"
        variant="elevated"
    >
      {{ message }}
      <template #actions>
        <v-btn
            icon="mdi-close"
            size="small"
            variant="text"
            @click="messageVisible = false"
        />
      </template>
    </v-snackbar>
  </div>
</template>

<script setup>
import {computed, defineAsyncComponent, onBeforeUnmount, onMounted, reactive, ref, watch,} from "vue";
import ConfigSection from "./config/ConfigSection.vue";
import {createConfigSections} from "../config/fields.js";
import {createResourceTypeItems} from "../config/fields/helpers.js";

const QrCodeDialog = defineAsyncComponent(
    () => import("./dialogs/QrCodeDialog.vue"),
);
const CloudDirectoryDialog = defineAsyncComponent(
    () => import("./dialogs/CloudDirectoryDialog.vue"),
);

const props = defineProps({
  api: {type: [Object, Function], required: true},
  initialConfig: {type: Object, default: () => ({})},
  showSwitch: {type: Boolean, default: true},
});
const emit = defineEmits(["save", "close", "switch", "layout"]);
const api = props.api;
const config = reactive(JSON.parse(JSON.stringify(props.initialConfig || {})));
if (!Array.isArray(config.online_docs) || !config.online_docs.length) {
  const legacyUrls = Array.isArray(config.online_docs_urls)
      ? config.online_docs_urls
      : String(config.online_docs_urls || "").split(/[,，\n]+/);
  const legacyTypes = Array.isArray(config.online_docs_resource_types)
      ? config.online_docs_resource_types
      : [];
  config.online_docs = legacyUrls
      .map((url) => String(url || "").trim())
      .filter(Boolean)
      .map((url) => ({url, resource_types: [...legacyTypes]}));
}
if (!config.online_docs.length) {
  config.online_docs.push({url: "", resource_types: []});
}
config.online_docs_urls = [];
config.online_docs_resource_types = [];
const activeTab = ref("basic");
const qrVisible = ref(false),
    qrProvider = ref("115"),
    directoryVisible = ref(false),
    directoryField = ref(""),
    directoryInitialPath = ref("/"),
    directoryProvider = ref("115"),
    saving = ref(false),
    refreshingAccount = ref(""),
    hdhiveOauthAction = ref(""),
    testingSource = ref(""),
    searchingTmdb = ref(false),
    tmdbSearched = ref(false),
    tmdbCandidates = ref([]),
    selectedTmdbId = ref(0),
    activeTestResourceType = ref("all"),
    testResult = ref({}),
    sourceTestVisible = ref(false),
    testSubmitted = ref(false),
    testError = ref(""),
    testElapsed = ref(null),
    previewVisible = ref(false),
    previewingUrl = ref(""),
    accessingResource = ref(""),
    previewLoading = ref(false),
    previewError = ref(""),
    previewItems = ref([]),
    previewMeta = ref({}),
    previewBreadcrumbs = ref([]),
    previewResourceType = ref(""),
    previewShareUrl = ref(""),
    previewSource = ref(""),
    previewJuyingResourceId = ref(""),
    previewHdhiveSlug = ref(""),
    previewHdhiveUnlocked = ref(false),
    previewTargetSeason = ref(null),
    previewTargetEpisodes = ref([]),
    unlockVisible = ref(false),
    unlockItem = ref(null),
    unlocking = ref(false),
    unlockError = ref(""),
    message = ref(""),
    messageType = ref("success"),
    messageVisible = ref(false);
let hdhiveOauthWindow = null;
let previewRequestId = 0;
const options = reactive({
  subscribes: [],
  mediaservers: [],
  mediaLibraryWebhookUrls: {},
  notificationTypes: [],
  cloudDrives: [],
  account: {},
  accounts: {},
  searchAccounts: {},
  pansou: {},
});
const sections = computed(() => createConfigSections(options, config));
const testResourceTabs = computed(() => {
  const types = Array.isArray(testResult.value?.resource_types)
      ? testResult.value.resource_types
      : [];
  if (!types.length) return [];
  const displayedCount = Number(
      testResult.value?.displayed_count ?? testResult.value?.items?.length ?? 0,
  );
  return [
    {
      value: "all",
      title: "全部",
      count: displayedCount,
    },
    ...types,
  ];
});
const filteredTestItems = computed(() => {
  const items = Array.isArray(testResult.value?.items)
      ? testResult.value.items
      : [];
  if (activeTestResourceType.value === "all") return items;
  return items.filter(
      (item) => item.resource_type === activeTestResourceType.value,
  );
});
const sourceNames = {
  pansou: "PanSou",
  hdhive: "HDHive",
  dian115: "Dian115",
  juying: "聚影",
  seedhub: "SeedHub",
  butailing: "不太灵",
  pinglian: "盘链",
  online_docs: "在线文档",
};
const sourceTestConfigKeys = {
  pansou: [
    "pansou_url",
    "pansou_username",
    "pansou_password",
    "pansou_auth_enabled",
    "pansou_channels",
    "pansou_plugins",
    "pansou_filter_include",
    "pansou_filter_exclude",
    "pansou_concurrency",
    "pansou_result_limit",
    "pansou_timeout",
  ],
  hdhive: [
    "hdhive_query_mode",
    "hdhive_api_key",
    "hdhive_client_id",
    "hdhive_access_token",
    "hdhive_refresh_token",
    "hdhive_token_expires_at",
    "hdhive_username",
    "hdhive_password",
    "hdhive_candidate_limit",
    "hdhive_request_interval",
    "hdhive_unlocks_per_minute",
    "hdhive_torrentclaw_enabled",
    "hdhive_torrentclaw_subtitle_languages",
  ],
  dian115: [
    "dian115_email",
    "dian115_password",
    "dian115_candidate_limit",
    "dian115_request_interval",
    "dian115_unlocks_per_minute",
  ],
  juying: [
    "juying_username",
    "juying_password",
    "juying_result_limit",
    "juying_request_interval",
  ],
  seedhub: ["seedhub_result_limit"],
  butailing: ["butailing_result_limit"],
  pinglian: [
    "pinglian_username",
    "pinglian_password",
    "pinglian_result_limit",
    "pinglian_request_interval",
    "pinglian_timeout",
  ],
  online_docs: ["online_docs"],
};
const sourceTest = reactive({
  source: "",
  title: "",
  season: 1,
});

function unwrapResponse(raw) {
  if (raw?.data && typeof raw.data === "object" && "success" in raw.data) {
    return raw.data;
  }
  return raw || {};
}

function applyOptions(data) {
  Object.entries(data.defaults || {}).forEach(([key, value]) => {
    if (!(key in config)) config[key] = value;
  });
  options.subscribes = Array.isArray(data.subscribes) ? data.subscribes : [];
  options.mediaservers = Array.isArray(data.mediaservers)
      ? data.mediaservers
      : [];
  options.mediaLibraryWebhookUrls =
      data.media_library_webhook_urls &&
      typeof data.media_library_webhook_urls === "object"
          ? data.media_library_webhook_urls
          : {};
  options.notificationTypes = Array.isArray(data.notification_types)
      ? data.notification_types
      : [];
  options.cloudDrives = Array.isArray(data.cloud_drives)
      ? data.cloud_drives
      : [];
  options.account =
      data.account && typeof data.account === "object" ? data.account : {};
  options.accounts =
      data.accounts && typeof data.accounts === "object" ? data.accounts : {};
  options.searchAccounts =
      data.search_accounts && typeof data.search_accounts === "object"
      ? data.search_accounts
      : {};
  options.pansou =
      data.pansou && typeof data.pansou === "object" ? data.pansou : {};
  const configuredSources = Array.isArray(config.search_source_order)
      ? config.search_source_order.filter(Boolean)
      : String(config.search_source_order || "")
          .split(/[,，\n]+/)
          .map((value) => value.trim())
          .filter(Boolean);
  config.search_source_order = configuredSources;
  [
    "pansou_channels",
    "pansou_plugins",
    "pansou_filter_include",
    "pansou_filter_exclude",
  ].forEach((key) => {
    if (Array.isArray(config[key])) return;
    config[key] = String(config[key] || "")
        .split(/[,，\n]+/)
        .map((value) => value.trim())
        .filter(Boolean);
  });
}

function notify(text, type = "success") {
  message.value = text;
  messageType.value = type;
  messageVisible.value = true;
}

async function copyText(value, label = "Webhook URL") {
  const text = String(value || "");
  if (!text) return;
  try {
    let copied = false;
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        copied = true;
      } catch (_) {
        // 局域网 HTTP 页面可能暴露 API 但拒绝调用，继续使用兼容方式。
      }
    }
    if (!copied) {
      const input = document.createElement("textarea");
      input.value = text;
      input.setAttribute("readonly", "");
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.appendChild(input);
      input.select();
      const copied = document.execCommand("copy");
      document.body.removeChild(input);
      if (!copied) throw new Error("浏览器拒绝访问剪贴板");
    }
    notify(`${label}已复制`);
  } catch (error) {
    notify(`复制失败：${error.message || error}`, "error");
  }
}

function formatPreviewSize(value) {
  const size = Number(value || 0);
  if (!Number.isFinite(size) || size <= 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1);
  return `${(size / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function formatElapsed(value) {
  const seconds = Number(value);
  return Number.isFinite(seconds) ? `${seconds.toFixed(2)} 秒` : "未知";
}

function previewFileIcon(file) {
  if (file?.is_dir) return "mdi-folder-outline";
  const name = String(file?.name || "").toLowerCase();
  if (/\.(mkv|mp4|avi|ts|m2ts|mov|wmv|webm)$/.test(name)) return "mdi-filmstrip";
  if (/\.(srt|ass|ssa|sup|vtt)$/.test(name)) return "mdi-subtitles-outline";
  if (/\.(zip|rar|7z|tar|gz)$/.test(name)) return "mdi-archive-outline";
  return "mdi-file-outline";
}

function previewFileExtension(value) {
  const name = String(value || "未命名文件");
  const extensionMatch = name.match(/(\.[^./\\\s]{1,12})$/);
  return extensionMatch ? extensionMatch[1] : "";
}

function previewFileStem(value) {
  const name = String(value || "未命名文件");
  const extension = previewFileExtension(name);
  const slash = Math.max(name.lastIndexOf("/"), name.lastIndexOf("\\"));
  const basename = slash >= 0 ? name.slice(slash + 1) : name;
  return extension ? basename.slice(0, -extension.length) : basename;
}

function canPreviewResource(item) {
  const source = String(item?.source || "").toLowerCase();
  const hdhivePreview = source === "hdhive" && Boolean(
      item?.slug && item?.resource_type,
  );
  return Boolean(
      item?.can_preview && (
          item?.url ||
          (source === "juying" && item?.juying_resource_id) ||
          hdhivePreview
      ),
  );
}

function canAccessHdhiveResource(item) {
  return Boolean(
      String(item?.source || "").toLowerCase() === "hdhive" &&
      item?.need_access &&
      !item?.url &&
      Number(item?.unlock_points || 0) === 0 &&
      (item?.is_free || item?.is_unlocked),
  );
}

function previewResourceKey(item) {
  const source = String(item?.source || "").toLowerCase();
  const resourceId = String(item?.juying_resource_id || "");
  if (source === "juying" && resourceId) return `${source}:${resourceId}`;
  return String(
      item?.url ||
      `${source}:${item?.resource_type || ""}:${item?.slug || item?.id || ""}`,
  );
}

async function requestResourceUrl(item) {
  const response = unwrapResponse(await api.post("plugin/CloudSubscribe/search/unlock", {
    source: item.source || sourceTest.source,
    item,
    config: sourceTestConfig(item.source || sourceTest.source),
  }));
  if (response.success === false) throw new Error(response.message || "资源链接获取失败");
  const data = response.data?.data || response.data || {};
  if (!data.url) throw new Error(response.message || "资源链接获取失败");
  item.url = data.url;
  item.need_access = false;
  item.need_unlock = false;
  item.is_unlocked = true;
  return response.message || "资源链接已获取";
}

async function accessResource(item) {
  if (!canAccessHdhiveResource(item)) return;
  const resourceKey = previewResourceKey(item);
  if (accessingResource.value || previewingUrl.value) return;
  accessingResource.value = resourceKey;
  try {
    const message = await requestResourceUrl(item);
    notify(`${message}，现在可以预览或复制`);
  } catch (error) {
    notify(
        error?.response?.data?.message || error.message || String(error),
        "error",
    );
  } finally {
    accessingResource.value = "";
  }
}

async function previewResource(item) {
  if (!canPreviewResource(item)) return;
  if (accessingResource.value || previewingUrl.value) return;
  const requestId = ++previewRequestId;
  previewingUrl.value = previewResourceKey(item);
  const shareUrl = String(item.url || "");
  previewVisible.value = true;
  previewLoading.value = false;
  previewError.value = "";
  previewItems.value = [];
  previewResourceType.value = String(item.resource_type || "").toLowerCase();
  previewShareUrl.value = shareUrl;
  previewSource.value = String(item.source || "").toLowerCase();
  previewJuyingResourceId.value = String(item.juying_resource_id || "");
  previewHdhiveSlug.value = String(item.slug || "");
  previewHdhiveUnlocked.value = Boolean(item.is_unlocked);
  previewTargetSeason.value = item.target_season ?? null;
  previewTargetEpisodes.value = Array.isArray(item.target_episodes)
      ? [...item.target_episodes]
      : [];
  previewMeta.value = {
    provider_name: "",
    resource_type_name: String(
        item.resource_type_name || item.resource_type || "",
    ),
    share_url: previewShareUrl.value,
  };
  const breadcrumbs = [{id: "", name: "根目录"}];
  previewBreadcrumbs.value = breadcrumbs;
  await loadPreviewDirectory("", breadcrumbs, requestId);
  if (requestId === previewRequestId && previewShareUrl.value) {
    item.url = previewShareUrl.value;
    item.need_access = false;
    item.need_unlock = false;
    item.is_unlocked = true;
  }
  if (requestId === previewRequestId) previewingUrl.value = "";
}

async function loadPreviewDirectory(parentId, breadcrumbs, requestId = ++previewRequestId) {
  const pendingJuying = previewSource.value === "juying" && previewJuyingResourceId.value;
  const pendingHdhive = previewSource.value === "hdhive" &&
      previewHdhiveSlug.value && !previewShareUrl.value;
  if (!previewShareUrl.value && !pendingJuying && !pendingHdhive) return;
  const resourceType = previewResourceType.value;
  const shareUrl = previewShareUrl.value;
  previewLoading.value = true;
  previewError.value = "";
  try {
    const response = unwrapResponse(await api.post("plugin/CloudSubscribe/search/preview", {
      resource_type: resourceType,
      url: shareUrl,
      parent_id: parentId || "",
      source: previewSource.value,
      juying_resource_id: previewJuyingResourceId.value,
      slug: previewHdhiveSlug.value,
      is_unlocked: previewHdhiveUnlocked.value,
      target_season: previewTargetSeason.value,
      target_episodes: previewTargetEpisodes.value,
      config: pendingJuying
          ? sourceTestConfig("juying")
          : pendingHdhive ? sourceTestConfig("hdhive") : undefined,
    }));
    if (requestId !== previewRequestId || !previewVisible.value) return;
    if (response.success === false) throw new Error(response.message || "资源预览失败");
    const data = response.data?.data || response.data || {};
    if (data.resource_type) {
      previewResourceType.value = String(data.resource_type).toLowerCase();
    }
    if (data.share_url) previewShareUrl.value = String(data.share_url);
    previewItems.value = Array.isArray(data.items) ? data.items : [];
    previewMeta.value = {
      provider_name: String(data.provider_name || ""),
      resource_type_name: String(data.resource_type_name || ""),
      display_name: String(data.display_name || ""),
      info_hash: String(data.info_hash || ""),
      size: Number(data.size || 0),
      share_url: String(data.share_url || shareUrl),
    };
    previewBreadcrumbs.value = breadcrumbs;
  } catch (error) {
    if (requestId !== previewRequestId || !previewVisible.value) return;
    previewItems.value = [];
    previewError.value = error?.response?.data?.message || error.message || String(error);
  } finally {
    if (requestId === previewRequestId) previewLoading.value = false;
  }
}

function openPreviewFolder(file) {
  if (!file?.can_enter || previewLoading.value) return;
  loadPreviewDirectory(String(file.id || ""), [
    ...previewBreadcrumbs.value,
    {id: String(file.id || ""), name: String(file.name || "未命名目录")},
  ]);
}

function openPreviewBreadcrumb(index) {
  const breadcrumb = previewBreadcrumbs.value[index];
  if (!breadcrumb || previewLoading.value) return;
  loadPreviewDirectory(
      String(breadcrumb.id || ""),
      previewBreadcrumbs.value.slice(0, index + 1),
  );
}

function confirmUnlock(item) {
  unlockItem.value = item || null;
  unlockError.value = "";
  unlockVisible.value = Boolean(item);
}

async function unlockResource() {
  const item = unlockItem.value;
  if (!item || unlocking.value) return;
  unlocking.value = true;
  unlockError.value = "";
  let shouldPreview = false;
  try {
    const message = await requestResourceUrl(item);
    unlockVisible.value = false;
    shouldPreview = canPreviewResource(item);
    notify(shouldPreview ? `${message}，正在打开预览` : `${message}，现在可以复制`);
  } catch (error) {
    unlockError.value = error?.response?.data?.message || error.message || String(error);
  } finally {
    unlocking.value = false;
  }
  if (shouldPreview) await previewResource(item);
}

function testItemStatus(item) {
  if (item?.is_unlocked) return {label: "已解锁", color: "success"};
  if (item?.need_unlock) {
    return {
      label: `${Number(item.unlock_points || 0)} 积分`,
      color: "warning",
    };
  }
  if (item?.is_free) return {label: "免费", color: "success"};
  if (item?.need_access) return {label: "待获取", color: "info"};
  return null;
}

function tmdbCandidateSubtitle(item) {
  const values = [item.media_type_name, item.year];
  if (item.original_title && item.original_title !== item.title) {
    values.push(item.original_title);
  }
  if (Number(item.vote_average) > 0) {
    values.push(`评分 ${Number(item.vote_average).toFixed(1)}`);
  }
  return values.filter(Boolean).join(" · ");
}

function sourceTestConfig(source) {
  const keys = ["resource_type_order", ...(sourceTestConfigKeys[source] || [])];
  return Object.fromEntries(
      keys
          .filter((key) => key in config && config[key] !== undefined)
          .map((key) => [key, JSON.parse(JSON.stringify(config[key]))]),
  );
}

function openQrCode(provider) {
  qrProvider.value = String(provider || "115");
  qrVisible.value = true;
}

async function handleQrSuccess(payload) {
  const provider = String(payload?.provider || qrProvider.value || "")
      .trim()
      .toLowerCase();
  const credentials = payload?.credentials;
  if (credentials && typeof credentials === "object") {
    Object.assign(config, credentials);
  }
  try {
    if (provider) {
      await refreshAccount(`drive:${provider}`, {silent: true});
    }
    await loadOptions();
  } catch (error) {
    notify(`账号信息刷新失败：${error.message || error}`, "warning");
  }
}

function openDirectoryPicker(fieldKey, provider) {
  directoryField.value = fieldKey;
  directoryProvider.value = String(provider || config.cloud_drive || "115");
  directoryInitialPath.value = String(config[fieldKey] || "/").trim() || "/";
  directoryVisible.value = true;
}

function selectDirectory(path) {
  if (directoryField.value) config[directoryField.value] = path || "/";
  directoryVisible.value = false;
}

async function loadOptions() {
  const response = unwrapResponse(
      await api.get("plugin/CloudSubscribe/ui_options"),
  );
  if (response.success === false) {
    throw new Error(response.message || "加载配置选项失败");
  }
  applyOptions(response.data?.data || response.data || response);
}

async function refreshAccount(accountKey, {silent = false} = {}) {
  const normalizedKey = String(accountKey || "")
      .trim()
      .toLowerCase();
  if (!normalizedKey || refreshingAccount.value) return;
  refreshingAccount.value = normalizedKey;
  try {
    const response = unwrapResponse(
        await api.post("plugin/CloudSubscribe/account/refresh", {
          key: normalizedKey,
        }),
    );
    if (response.success === false) {
      throw new Error(response.message || "账户信息刷新失败");
    }
    const data = response.data?.data || response.data || {};
    const [category, source] = String(data.key || normalizedKey).split(":", 2);
    const account =
        data.account && typeof data.account === "object" ? data.account : {};
    if (category === "drive") {
      options.accounts = {...options.accounts, [source]: account};
      if (String(config.cloud_drive || "") === source) {
        options.account = account;
      }
    } else if (category === "search") {
      options.searchAccounts = {...options.searchAccounts, [source]: account};
    }
    if (!silent) {
      notify(
          response.message ||
          (data.limited
              ? "刷新过于频繁，已显示最近一次账户信息"
              : "账户信息已刷新"),
          data.limited ? "warning" : "success",
      );
    }
  } catch (error) {
    if (!silent) {
      notify(`账户信息刷新失败：${error.message || error}`, "error");
    }
  } finally {
    refreshingAccount.value = "";
  }
}

async function save() {
  if (saving.value) return;
  saving.value = true;
  messageVisible.value = false;
  try {
    const payload = JSON.parse(JSON.stringify(config));
    delete payload.hdhive_oauth_callback;
    const response = unwrapResponse(
        await api.post("plugin/CloudSubscribe/config/save", payload),
    );
    if (response.success === false) {
      throw new Error(response.message || "保存配置失败");
    }
    const savedConfig = response.data?.data || response.data;
    if (savedConfig && typeof savedConfig === "object") {
      Object.assign(config, JSON.parse(JSON.stringify(savedConfig)));
    }
    await loadOptions();
    notify(response.message || "配置已保存");
  } catch (e) {
    notify(`保存配置失败：${e.message || e}`, "error");
  } finally {
    saving.value = false;
  }
}

function hdhiveOAuthPayload() {
  return {
    client_id: String(config.hdhive_client_id || "").trim(),
    app_secret: String(config.hdhive_api_key || "").trim(),
    redirect_uri: String(config.hdhive_redirect_uri || "").trim(),
    response_mode: String(config.hdhive_response_mode || "redirect").trim(),
    scope: "query unlock",
  };
}

async function startHdhiveOAuth() {
  if (hdhiveOauthAction.value) return;
  const oauthPopup = window.open(
      "about:blank",
      "hdhive-openapi-oauth",
      "width=560,height=760,noopener=no",
  );
  hdhiveOauthAction.value = "start";
  try {
    const response = unwrapResponse(
        await api.post(
            "plugin/CloudSubscribe/hdhive/oauth/start",
            hdhiveOAuthPayload(),
        ),
    );
    if (response.success === false) {
      throw new Error(response.message || "生成 HDHive 授权链接失败");
    }
    const data = response.data?.data || response.data || {};
    if (!data.authorize_url) throw new Error("HDHive 授权链接为空");
    if (oauthPopup) {
      oauthPopup.location.href = data.authorize_url;
      hdhiveOauthWindow = oauthPopup;
    } else {
      window.open(data.authorize_url, "_blank", "noopener,noreferrer");
    }
    notify(response.message || "HDHive 授权页已打开");
  } catch (error) {
    if (oauthPopup && !oauthPopup.closed) oauthPopup.close();
    notify(`发起 HDHive 授权失败：${error.message || error}`, "error");
  } finally {
    hdhiveOauthAction.value = "";
  }
}

async function exchangeHdhiveOAuth(callbackData = {}) {
  if (hdhiveOauthAction.value) return;
  hdhiveOauthAction.value = "exchange";
  try {
    const response = unwrapResponse(
        await api.post("plugin/CloudSubscribe/hdhive/oauth/exchange", {
          ...hdhiveOAuthPayload(),
          callback: String(config.hdhive_oauth_callback || "").trim(),
          code: String(callbackData.code || "").trim(),
          state: String(callbackData.state || "").trim(),
        }),
    );
    if (response.success === false) {
      throw new Error(response.message || "HDHive 用户授权失败");
    }
    const data = response.data?.data || response.data || {};
    config.hdhive_access_token = data.access_token || "";
    config.hdhive_refresh_token = data.refresh_token || "";
    config.hdhive_token_expires_at = Number(data.token_expires_at || 0);
    config.hdhive_auth_code = "";
    config.hdhive_oauth_callback = "";
    if (hdhiveOauthWindow && !hdhiveOauthWindow.closed)
      hdhiveOauthWindow.close();
    hdhiveOauthWindow = null;
    notify(
        data.warning || "HDHive OpenAPI 授权成功，请保存配置使 Token 生效",
        data.warning ? "warning" : "success",
    );
  } catch (error) {
    notify(`完成 HDHive 授权失败：${error.message || error}`, "error");
  } finally {
    hdhiveOauthAction.value = "";
  }
}

function handleHdhiveOAuthMessage(event) {
  if (event.origin !== "https://hdhive.com") return;
  const payload = event.data;
  if (
      !payload ||
      payload.source !== "hdhive-openapi" ||
      payload.type !== "authorization_response" ||
      String(payload.client_id || "") !== String(config.hdhive_client_id || "")
  )
    return;
  exchangeHdhiveOAuth(payload);
}

function openSourceTest(source) {
  if (testingSource.value || !sourceNames[source]) return;
  sourceTest.source = source;
  tmdbCandidates.value = [];
  tmdbSearched.value = false;
  selectedTmdbId.value = 0;
  activeTestResourceType.value = "all";
  testResult.value = {};
  testSubmitted.value = false;
  testError.value = "";
  testElapsed.value = null;
  sourceTestVisible.value = true;
}

async function searchTmdbCandidates() {
  if (searchingTmdb.value || testingSource.value) return;
  const title = String(sourceTest.title || "").trim();
  if (!title) {
    testError.value = "请输入媒体名称";
    return;
  }
  searchingTmdb.value = true;
  tmdbSearched.value = false;
  tmdbCandidates.value = [];
  selectedTmdbId.value = 0;
  activeTestResourceType.value = "all";
  testResult.value = {};
  testSubmitted.value = false;
  testError.value = "";
  try {
    const response = unwrapResponse(
        await api.post("plugin/CloudSubscribe/search/tmdb", {title}),
    );
    if (response.success === false) {
      throw new Error(response.message || "TMDB 查询失败");
    }
    const data = response.data?.data || response.data || {};
    tmdbCandidates.value = Array.isArray(data.items) ? data.items : [];
    tmdbSearched.value = true;
  } catch (e) {
    testError.value = e?.response?.data?.message || e.message || String(e);
  } finally {
    searchingTmdb.value = false;
  }
}

async function testSource(candidate) {
  if (testingSource.value || !candidate?.tmdb_id) return;
  testingSource.value = sourceTest.source;
  selectedTmdbId.value = Number(candidate.tmdb_id || 0);
  testError.value = "";
  testElapsed.value = null;
  testSubmitted.value = false;
  messageVisible.value = false;
  try {
    const response = unwrapResponse(
        await api.post("plugin/CloudSubscribe/search/test", {
          source: sourceTest.source,
          title: candidate.title,
          original_title: candidate.original_title || "",
          year: candidate.year || null,
          tmdb_id: candidate.tmdb_id,
          media_type: candidate.media_type,
          season: candidate.media_type === "tv" ? sourceTest.season : null,
          config: sourceTestConfig(sourceTest.source),
        }),
    );
    if (response.success === false) {
      testElapsed.value = response.data?.elapsed_seconds ?? null;
      throw new Error(response.message || "搜索渠道测试失败");
    }
    testResult.value = response.data?.data || response.data || {};
    testElapsed.value = testResult.value.elapsed_seconds ?? null;
    activeTestResourceType.value = "all";
    testSubmitted.value = true;
    notify(response.message || "搜索渠道测试完成");
  } catch (e) {
    const status = Number(e?.response?.status || 0);
    const errorData = e?.response?.data?.data || e?.response?.data || {};
    testElapsed.value = errorData.elapsed_seconds ?? testElapsed.value;
    testError.value =
        status === 502
        ? `${sourceNames[sourceTest.source] || "搜索渠道"} 测试请求被网关中断（HTTP 502），请检查渠道服务状态及反向代理超时`
        : e?.response?.data?.message || e.message || String(e);
  } finally {
    testingSource.value = "";
    selectedTmdbId.value = 0;
  }
}

onMounted(async () => {
  window.addEventListener("message", handleHdhiveOAuthMessage);
  emit("layout", {maxWidth: "62rem"});
  try {
    await loadOptions();
  } catch (e) {
    notify(`加载配置选项失败：${e.message || e}`, "warning");
  }
});

onBeforeUnmount(() => {
  previewRequestId += 1;
  window.removeEventListener("message", handleHdhiveOAuthMessage);
  if (hdhiveOauthWindow && !hdhiveOauthWindow.closed) hdhiveOauthWindow.close();
});

watch(
    previewVisible,
    (visible) => {
      if (visible) return;
      previewRequestId += 1;
      previewingUrl.value = "";
      previewLoading.value = false;
      previewError.value = "";
      previewItems.value = [];
      previewMeta.value = {};
      previewBreadcrumbs.value = [];
      previewResourceType.value = "";
      previewShareUrl.value = "";
      previewSource.value = "";
      previewJuyingResourceId.value = "";
      previewHdhiveSlug.value = "";
      previewHdhiveUnlocked.value = false;
      previewTargetSeason.value = null;
      previewTargetEpisodes.value = [];
    },
);

watch(
    () => props.initialConfig,
    (value) => Object.assign(config, JSON.parse(JSON.stringify(value || {}))),
    {deep: true},
);

watch(
    [
      () => config.cloud_drive,
      () => config.cross_transfer_enabled,
      () => options.cloudDrives,
    ],
    (
        [provider, crossTransfer, drives],
        [previousProvider, previousCrossTransfer, previousDrives],
    ) => {
      if (
          provider === previousProvider &&
          crossTransfer === previousCrossTransfer &&
          drives === previousDrives
      ) return;
      const supported = new Set(
          createResourceTypeItems(options.cloudDrives, config)
              .map((item) => item.value),
      );
      config.resource_type_order = (config.resource_type_order || []).filter(
          (value) => supported.has(value),
      );
    },
);
</script>

<style scoped>
.cloud-subscribe-config {
  display: flex;
  width: min(62rem, calc(100vw - 32px));
  max-width: min(62rem, 100%);
  min-width: 0;
  height: auto;
  max-height: min(800px, calc(100dvh - 64px));
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
}

:global(.v-overlay__content:has(.cloud-subscribe-config)) {
  overflow: hidden !important;
}

:global(.v-overlay__content:has(.cloud-subscribe-config) > *) {
  min-height: 0;
  max-height: 100%;
  overflow: hidden !important;
}

.config-shell {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  height: auto;
  max-height: 100%;
  min-height: 0;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.config-header {
  min-width: 0;
  flex-wrap: nowrap;
}

.config-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.config-body {
  width: 100%;
  max-width: 100%;
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.cloud-subscribe-config :deep(.v-field),
.cloud-subscribe-config :deep(.v-selection-control) {
  font-size: 0.875rem;
}

.cloud-subscribe-config :deep(.v-field) {
  --v-input-control-height: 38px;
}

.config-tabs :deep(.v-tab) {
  min-width: 132px;
  text-transform: none;
}

.config-tabs {
  flex: 0 0 auto;
}

.config-content-scroll {
  flex: 1 1 0;
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
  touch-action: pan-y;
  scrollbar-width: none !important;
}

.config-content-scroll::-webkit-scrollbar {
  display: none !important;
  width: 0 !important;
  height: 0 !important;
}

.config-actions {
  position: relative;
  min-height: 68px;
}

.save-progress {
  position: absolute;
  inset: 0 0 auto;
}

.save-state {
  display: flex;
  align-items: center;
  gap: 8px;
  color: rgb(var(--v-theme-primary));
  font-size: 0.875rem;
}

.save-config-button {
  min-width: 132px;
  height: 42px;
  font-weight: 600;
  letter-spacing: 0;
  box-shadow: 0 3px 8px rgba(var(--v-theme-primary), 0.24) !important;
}

.source-test-card {
  max-height: min(600px, calc(100dvh - 48px));
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.source-test-card--results {
  height: min(600px, calc(100dvh - 48px));
}

.source-test-header,
.source-test-actions {
  flex: 0 0 auto;
}

.source-test-form {
  min-height: 0;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.source-test-body {
  min-height: 0;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.source-test-fields {
  flex: 0 0 auto;
}

.source-test-error {
  flex: 0 0 auto;
}

.source-test-tmdb {
  min-height: 0;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
}

.source-test-tmdb-scroll {
  min-height: 0;
  flex: 1 1 auto;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.source-test-loading {
  min-height: 220px;
  flex: 1 1 auto;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-direction: column;
  text-align: center;
}

.source-test-tmdb-item {
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.source-test-result {
  min-height: 0;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  margin-top: 8px;
}

.source-test-summary {
  flex: 0 0 auto;
  padding: 10px 12px;
  margin-bottom: 8px;
  border-left: 3px solid rgb(var(--v-theme-primary));
  background: rgba(var(--v-theme-primary), 0.07);
}

.source-test-summary__context {
  color: rgba(var(--v-theme-on-surface), 0.72);
}

.source-test-summary__line {
  display: flex;
  align-items: baseline;
  flex-wrap: nowrap;
  gap: 14px;
  overflow-x: auto;
  white-space: nowrap;
  color: rgba(var(--v-theme-on-surface), 0.86);
  font-size: 0.8125rem;
  line-height: 1.45;
  scrollbar-width: none;
}

.source-test-summary__line::-webkit-scrollbar {
  display: none;
}

.source-test-summary__line strong {
  color: rgb(var(--v-theme-primary));
  font-size: 0.9375rem;
}

.source-test-notice {
  font-size: 0.81rem;
  line-height: 1.4;
}

.source-test-result-scroll {
  min-height: 0;
  flex: 1 1 auto;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.source-test-tabs {
  flex: 0 0 auto;
  border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.source-test-tabs :deep(.v-btn__content) {
  text-transform: none;
}

.source-test-result-list {
  padding: 0;
}

.source-test-result-item {
  min-height: 0 !important;
  height: auto !important;
  padding-top: 6px;
  padding-bottom: 6px;
}

.source-test-result-item :deep(.v-list-item__content) {
  align-self: stretch;
  min-width: 0;
}

.source-test-item-content {
  width: 100%;
  min-width: 0;
  position: relative;
  padding-right: 82px;
}

.source-test-item-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  line-height: 1.45;
}

.source-test-item-meta {
  display: flex;
  min-width: 0;
  align-items: center;
  flex-wrap: nowrap;
  gap: 6px;
  overflow: hidden;
  padding-top: 4px;
  padding-bottom: 2px;
  line-height: 1.4;
}

.source-test-item-meta > :deep(.v-chip),
.source-test-item-meta > span {
  flex: 0 0 auto;
}

.source-test-item-actions {
  position: absolute;
  top: 22px;
  right: 0;
  display: flex;
  align-items: center;
  gap: 2px;
}

.source-preview-card {
  max-height: min(78vh, 720px);
}

.source-preview-body {
  display: flex;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
}

.source-preview-loading {
  min-height: 120px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.source-preview-list-scroll {
  min-height: 0;
  max-height: min(58vh, 520px);
  overflow-y: auto;
}

.source-preview-list {
  font-size: 0.86rem;
}

.source-preview-list :deep(.v-list-item-title) {
  font-size: 0.86rem;
  line-height: 1.35rem;
}

.source-preview-meta {
  padding-bottom: 8px;
  overflow-wrap: anywhere;
  line-height: 1.55;
}

.source-preview-meta > span {
  margin-right: 16px;
}

.source-preview-meta__title {
  white-space: normal;
}

.source-preview-header-link {
  display: block;
  max-width: min(44%, 320px);
  min-width: 0;
  overflow: hidden;
  color: rgb(var(--v-theme-primary));
  text-overflow: ellipsis;
  white-space: nowrap;
}

.source-preview-breadcrumbs {
  display: flex;
  min-height: 32px;
  align-items: center;
  overflow-x: auto;
  padding-bottom: 6px;
  white-space: nowrap;
}

.source-preview-file--directory {
  cursor: pointer;
}

.source-preview-file--directory:hover {
  background: rgba(var(--v-theme-primary), 0.06);
}

.source-preview-file :deep(.v-list-item__prepend) {
  width: 36px;
  min-width: 36px;
}

.source-preview-file :deep(.v-list-item__content) {
  min-width: 0;
  overflow: hidden;
}

.source-preview-file-name {
  display: flex;
  flex-direction: row;
  justify-content: flex-start;
  min-width: 0;
  overflow: hidden;
  direction: ltr;
  text-align: left;
  white-space: nowrap;
}

.source-preview-file-stem {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  direction: ltr;
  text-align: left;
}

.source-preview-file-extension {
  flex: 0 0 auto;
}

.source-preview-file-size {
  width: 76px;
  flex: 0 0 76px;
  text-align: right;
  white-space: nowrap;
}

.config-window {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  min-height: 0;
  overflow: visible;
}

.config-window-section {
  box-sizing: border-box;
  width: 100%;
  max-width: 100%;
  min-width: 0;
}

@media (min-width: 601px) {
  .config-shell,
  .config-body,
  .config-content-scroll {
    flex: 0 1 auto;
  }

  .config-window-section {
    padding: 12px 14px 20px;
  }
}

@media (max-width: 600px) {
  :global(.v-overlay__content:has(.cloud-subscribe-config)) {
    width: 100vw !important;
    max-width: 100vw !important;
    height: 100dvh !important;
    max-height: 100dvh !important;
    margin: 0 !important;
  }

  .cloud-subscribe-config {
    width: 100%;
    max-width: 100%;
    height: 100%;
    max-height: 100%;
  }

  .config-shell {
    width: 100%;
    height: 100%;
    max-height: 100%;
    min-height: 0;
    border-radius: 0 !important;
  }

  .config-body,
  .config-content-scroll {
    height: 0;
  }

  .config-content-scroll {
    overflow-y: auto;
    overscroll-behavior-y: contain;
    scroll-behavior: auto;
  }

  .config-header {
    padding-left: 10px !important;
    padding-right: 10px !important;
  }

  .config-header-action {
    flex: 0 0 34px;
    min-width: 34px !important;
    width: 34px;
    padding: 0 !important;
  }

  .config-header-action :deep(.v-btn__content) {
    display: none;
  }

  .config-header-action :deep(.v-btn__prepend) {
    margin: 0;
  }

  .config-actions {
    min-height: 64px;
    padding-inline: 12px !important;
  }

  .save-state {
    font-size: 0.8125rem;
  }

  .config-tabs :deep(.v-tab) {
    min-width: 112px;
  }

  .config-window-section {
    padding: 10px 12px 20px;
  }
}
</style>
