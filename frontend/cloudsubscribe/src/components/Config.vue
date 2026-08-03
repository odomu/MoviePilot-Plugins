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
        v-model="qrVisible"
        :api="api"
        :provider="qrProvider"
        @success="handleQrSuccess"
    />
    <CloudDirectoryDialog
        v-model="directoryVisible"
        :api="api"
        :provider="directoryProvider"
        :initial-path="directoryField ? config[directoryField] : '/'"
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
                    counter="100"
                    autofocus
                    clearable
                    hide-details="auto"
                />
              </v-col>
              <v-col cols="12" sm="4">
                <v-text-field
                    v-model="sourceTest.season"
                    label="电视剧季号"
                    type="number"
                    min="1"
                    max="999"
                    hint="电影会自动忽略"
                    hide-details="auto"
                />
              </v-col>
            </v-row>
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
            <v-alert
                v-if="testError"
                type="error"
                variant="tonal"
                density="compact"
                class="mt-3"
            >
              {{ testError }}
            </v-alert>
            <div v-if="testSubmitted" class="source-test-result">
              <v-divider class="my-4"/>
              <div class="text-body-2 mb-3">
                {{ testChannelSummary }} · {{ testResult.media }} ·
                {{ testResult.count || 0 }} 个候选
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
import {computed, onBeforeUnmount, onMounted, reactive, ref, watch,} from "vue";
import QrCodeDialog from "./dialogs/QrCodeDialog.vue";
import CloudDirectoryDialog from "./dialogs/CloudDirectoryDialog.vue";
import ConfigSection from "./config/ConfigSection.vue";
import {createConfigSections} from "../config/fields.js";

const props = defineProps({
  api: {type: [Object, Function], required: true},
  initialConfig: {type: Object, default: () => ({})},
  showSwitch: {type: Boolean, default: true},
});
const emit = defineEmits(["save", "close", "switch", "layout"]);
const api = props.api;
const config = reactive(JSON.parse(JSON.stringify(props.initialConfig || {})));
const activeTab = ref("basic");
const qrVisible = ref(false),
    qrProvider = ref("115"),
    directoryVisible = ref(false),
    directoryField = ref(""),
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
    message = ref(""),
    messageType = ref("success"),
    messageVisible = ref(false);
let hdhiveOauthWindow = null;
const options = reactive({
  subscribes: [],
  mediaservers: [],
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
  return [
    {
      value: "all",
      title: "全部",
      count: Number(testResult.value?.count || 0),
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
const testChannelSummary = computed(() => {
  const source = testResult.value?.source_name || "搜索渠道";
  const types = Array.isArray(testResult.value?.resource_types)
      ? testResult.value.resource_types
      : [];
  if (types.length <= 1) return source;
  return `${source} · 多渠道（${types.map((item) => item.title).join(" / ")}）`;
});
const sourceNames = {
  pansou: "PanSou",
  hdhive: "HDHive",
  dian115: "Dian115",
  juying: "聚影",
  seedhub: "SeedHub",
  butailing: "不太灵",
};
const sourceTestConfigKeys = {
  pansou: [
    "pansou_url",
    "pansou_username",
    "pansou_password",
    "pansou_auth_enabled",
    "pansou_channels",
    "pansou_plugins",
    "pansou_cloud_types",
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
  ],
  juying: [
    "juying_username",
    "juying_password",
    "juying_result_limit",
    "juying_request_interval",
  ],
  seedhub: ["seedhub_result_limit"],
  butailing: ["butailing_result_limit"],
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
  [
    "pansou_channels",
    "pansou_plugins",
    "pansou_cloud_types",
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
  const credentials = payload?.credentials;
  if (credentials && typeof credentials === "object") {
    Object.assign(config, credentials);
  }
  try {
    await loadOptions();
  } catch (error) {
    notify(`账号信息刷新失败：${error.message || error}`, "warning");
  }
}

function openDirectoryPicker(fieldKey, provider) {
  directoryField.value = fieldKey;
  directoryProvider.value = String(provider || config.cloud_drive || "115");
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
      throw new Error(response.message || "搜索渠道测试失败");
    }
    testResult.value = response.data?.data || response.data || {};
    activeTestResourceType.value = "all";
    testSubmitted.value = true;
    notify(response.message || "搜索渠道测试完成");
  } catch (e) {
    const status = Number(e?.response?.status || 0);
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
  window.removeEventListener("message", handleHdhiveOAuthMessage);
  if (hdhiveOauthWindow && !hdhiveOauthWindow.closed) hdhiveOauthWindow.close();
});

watch(
    () => props.initialConfig,
    (value) => Object.assign(config, JSON.parse(JSON.stringify(value || {}))),
    {deep: true},
);

watch(
    () => config.cloud_drive,
    (provider, previous) => {
      if (!previous || provider === previous) return;
      const drive = options.cloudDrives.find((item) => item.value === provider);
      if (!drive?.resource_types) return;
      const supported = new Set(drive.resource_types);
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
  flex-wrap: wrap;
  gap: 6px;
  padding-top: 4px;
  padding-bottom: 2px;
  line-height: 1.4;
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
