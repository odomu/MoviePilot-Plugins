<template>
  <div class="config-tab-content">
    <div v-if="section.subtabs?.length" class="subtab-wrapper">
      <transition name="fade">
        <button
          v-if="canScrollLeft"
          type="button"
          class="subtab-arrow-btn"
          title="向左滚动"
          @click="scrollSubtabs(-180)">
          <v-icon icon="mdi-chevron-left" size="16" />
        </button>
      </transition>

      <div class="subtab-viewport-wrapper">
        <!-- 左侧截断渐变遮罩 -->
        <transition name="fade">
          <div v-show="canScrollLeft" class="subtab-fade-mask subtab-fade-mask--left" />
        </transition>

        <div
          ref="subtabScrollRef"
          class="subtab-scroll-container"
          @scroll.passive="onSubtabScroll"
          @wheel.passive="onSubtabWheel">
          <nav class="subtab-bar" aria-label="二级配置分类">
            <button
              v-for="tab in section.subtabs"
              :key="tab.value"
              type="button"
              class="subtab-item"
              :class="{ 'subtab-item--active': activeSubtab === tab.value }"
              @click="onSubtabClick(tab.value, $event)">
              <v-icon :icon="tab.icon" size="15" class="subtab-icon" />
              <span>{{ tab.title }}</span>
            </button>
          </nav>
        </div>

        <!-- 右侧截断渐变遮罩（明确提示右侧有内容被截断） -->
        <transition name="fade">
          <div v-show="canScrollRight" class="subtab-fade-mask subtab-fade-mask--right" />
        </transition>
      </div>

      <transition name="fade">
        <button
          v-if="canScrollRight"
          type="button"
          class="subtab-arrow-btn"
          title="向右滚动"
          @click="scrollSubtabs(180)">
          <v-icon icon="mdi-chevron-right" size="16" />
        </button>
      </transition>

      <!-- 全部分类/渠道下拉速选菜单（带未查看更多红点指示） -->
      <v-menu location="bottom end" :close-on-content-click="true">
        <template #activator="{ props }">
          <button
            v-bind="props"
            type="button"
            class="subtab-menu-trigger"
            :class="{ 'has-hidden': canScrollRight }"
            title="查看全部渠道与分类"
            aria-label="查看全部渠道与分类">
            <v-icon icon="mdi-format-list-bulleted" size="15" class="mr-1" />
            <span class="subtab-menu-text">更多 ({{ section.subtabs.length }})</span>
            <span v-if="canScrollRight" class="subtab-more-dot" />
          </button>
        </template>
        <div class="subtab-dropdown-menu rounded-lg">
          <div class="subtab-dropdown-header">
            <span>全部分类</span>
            <span class="subtab-dropdown-count">{{ section.subtabs.length }}</span>
          </div>
          <div class="subtab-dropdown-list">
            <button
              v-for="tab in section.subtabs"
              :key="tab.value"
              type="button"
              class="subtab-dropdown-item"
              :class="{ 'is-active': activeSubtab === tab.value }"
              @click="onSubtabClick(tab.value)">
              <v-icon :icon="tab.icon" size="15" class="subtab-dropdown-icon" />
              <span class="subtab-dropdown-title">{{ tab.title }}</span>
              <v-icon
                v-if="activeSubtab === tab.value"
                icon="mdi-check"
                size="15"
                class="subtab-dropdown-check" />
            </button>
          </div>
        </div>
      </v-menu>
    </div>
    <div class="config-section-scroll">
      <transition name="tab-fade" mode="out-in">
        <div :key="activeSubtab || 'main'" class="subtab-pane">
          <template v-for="(group, index) in visibleGroups" :key="group.title">
            <section class="config-group">
          <div v-if="!group.hideHeading" class="group-heading">
            <v-icon :icon="group.icon" color="primary" size="small" class="group-heading-icon" />
            <div class="group-heading-content">
              <span class="group-title text-subtitle-2 font-weight-medium">
                {{ group.title }}
              </span>
              <span v-if="group.hint" class="group-hint text-caption text-medium-emphasis">
                {{ group.hint }}
              </span>
            </div>
          </div>

              <v-row dense class="config-fields-row">
            <template v-for="field in group.fields" :key="field.key">
              <v-col
                v-if="evalShowCondition(field.show, config) && !isFieldEmbedded(group, field)"
                :cols="12"
                :sm="field.cols || 6"
                :md="field.cols || 6"
                class="config-field-col">
                <AccountInfo
                  v-if="field.type === 'account'"
                  :account="getAccountInfo(field)"
                  :compact="Boolean(field.compact)"
                  :loading="refreshingAccounts.includes(field.accountKey)"
                  :refreshable="Boolean(field.accountKey)"
                  @refresh="emit('refresh-account', field.accountKey)" />
                <v-alert v-else-if="field.type === 'info'" type="info" variant="tonal" density="compact">
                  <div class="text-body-2 font-weight-medium mb-1">
                    {{ field.label }}
                  </div>
                  <div v-for="line in field.lines || []" :key="line" class="text-caption mb-1">• {{ line }}</div>
                </v-alert>
                <div v-else-if="field.type === 'media-library-webhook'" class="media-library-webhook">
                  <v-alert
                    v-if="!(field.items || []).length"
                    type="warning"
                    variant="tonal"
                    density="compact"
                    text="尚未配置 Emby 媒体服务器，暂时无法生成通知地址。" />
                  <v-text-field
                    v-for="item in field.items || []"
                    v-else
                    :key="item.value"
                    :model-value="mediaLibraryWebhookUrl(field, item.value)"
                    :label="`${item.title || item.value} Webhook URL`"
                    readonly
                    density="compact"
                    variant="outlined"
                    hide-details="auto">
                    <template #append-inner>
                      <div class="media-library-webhook__actions">
                        <v-btn
                          icon="mdi-content-copy"
                          variant="text"
                          color="primary"
                          size="small"
                          title="复制 Webhook URL"
                          @click="emit('copy-text', mediaLibraryWebhookUrl(field, item.value))" />
                      </div>
                    </template>
                  </v-text-field>
                </div>
                <v-btn
                  v-else-if="field.type === 'test-source' && !isFieldEmbedded(group, field)"
                  color="primary"
                  variant="tonal"
                  prepend-icon="mdi-flask-outline"
                  :loading="testingSource === field.source"
                  :disabled="
                    !isTestSourceConfigured(field.source) || (Boolean(testingSource) && testingSource !== field.source)
                  "
                  :title="testSourceTitle(field.source)"
                  @click="emit('test-source', field.source)">
                  {{ field.label }}
                </v-btn>
                <v-btn
                  v-else-if="field.type === 'test-auto-subscribe'"
                  color="primary"
                  variant="tonal"
                  prepend-icon="mdi-flask-outline"
                  :loading="testingAutoSubscribe === field.provider"
                  :disabled="
                    !isAutoSubscribeConfigured(field.provider) ||
                    (Boolean(testingAutoSubscribe) && testingAutoSubscribe !== field.provider)
                  "
                  :title="autoSubscribeTestTitle(field.provider)"
                  @click="emit('test-auto-subscribe', field.provider)">
                  {{ field.label }}
                </v-btn>
                <div v-else-if="field.type === 'hdhive-oauth'" class="hdhive-oauth-panel">
                  <div class="d-flex align-center flex-wrap ga-2 mb-2">
                    <v-chip size="small" variant="tonal" :color="config.hdhive_access_token ? 'success' : 'warning'">
                      {{ config.hdhive_access_token ? "已获取用户 Token" : "尚未完成用户授权" }}
                    </v-chip>
                    <span class="text-caption text-medium-emphasis">授权范围：query unlock write</span>
                  </div>
                  <v-text-field
                    v-if="config.hdhive_response_mode !== 'postmessage'"
                    v-model="config.hdhive_oauth_callback"
                    label="授权完成后的完整回调 URL"
                    hint="请粘贴同时包含 code 和 state 的完整地址；插件会校验 state 后在服务端换取 Token。"
                    persistent-hint
                    clearable
                    density="compact"
                    variant="outlined"
                    hide-details="auto"
                    class="mb-2" />
                  <div class="d-flex flex-wrap ga-2">
                    <v-btn
                      color="primary"
                      variant="tonal"
                      prepend-icon="mdi-open-in-new"
                      :loading="hdhiveOauthAction === 'start'"
                      :disabled="Boolean(hdhiveOauthAction) && hdhiveOauthAction !== 'start'"
                      @click="emit('hdhive-oauth-start')">
                      打开 HDHive 授权页
                    </v-btn>
                    <v-btn
                      v-if="config.hdhive_response_mode !== 'postmessage'"
                      color="success"
                      variant="tonal"
                      prepend-icon="mdi-shield-check-outline"
                      :loading="hdhiveOauthAction === 'exchange'"
                      :disabled="Boolean(hdhiveOauthAction) || !String(config.hdhive_oauth_callback || '').trim()"
                      @click="emit('hdhive-oauth-exchange')">
                      校验回调并完成授权
                    </v-btn>
                  </div>
                </div>
                <CheckinTimeline
                  v-else-if="field.type === 'checkin-timeline'"
                  :api="api"
                  :providers="field.providers"
                  :config="config"
                  @result="emit('checkin-result', $event)" />
                <RegionMediaMapField
                  v-else-if="field.type === 'region-media-map' || field.type === 'priority-order'"
                  v-model="config[field.key]"
                  :field="field" />
                <VCronField
                  v-else-if="field.type === 'cron'"
                  v-model="config[field.key]"
                  :label="field.label"
                  :hint="field.hint"
                  :placeholder="field.placeholder"
                  :persistent-hint="Boolean(field.hint)"
                  density="compact" />
                <v-switch
                  v-else-if="field.type === 'switch'"
                  v-model="config[field.key]"
                  :label="field.label"
                  :hint="field.hint"
                  :persistent-hint="Boolean(field.hint)"
                  :disabled="Boolean(field.disabled?.(config))"
                  color="primary"
                  density="compact"
                  hide-details="auto"
                  class="config-switch" />
                <div v-else-if="field.type === 'online-documents'" class="online-documents">
                  <div v-if="field.hint" class="text-caption text-medium-emphasis mb-3">{{ field.hint }}</div>
                  <div
                    v-for="(document, documentIndex) in onlineDocuments(field.key)"
                    :key="documentIndex"
                    class="online-document-row">
                    <v-text-field
                      v-model="document.url"
                      label="文档地址"
                      placeholder="https://docs.qq.com/..."
                      density="compact"
                      variant="outlined"
                      hide-details="auto" />
                    <v-select
                      v-model="document.resource_types"
                      label="资源类型"
                      :items="onlineDocumentTypeItems(field)"
                      item-title="title"
                      item-value="value"
                      multiple
                      chips
                      closable-chips
                      density="compact"
                      variant="outlined"
                      hide-details="auto" />
                    <v-btn
                      icon="mdi-plus"
                      variant="text"
                      color="primary"
                      title="在下方添加文档"
                      @click="addOnlineDocument(field.key, documentIndex)" />
                    <v-btn
                      icon="mdi-delete-outline"
                      variant="text"
                      color="error"
                      title="删除此文档"
                      @click="removeOnlineDocument(field.key, documentIndex)" />
                  </div>
                </div>
                <MultiSelectDialogField
                  v-else-if="(field.type === 'select' || field.type === 'multi-select') && field.multiple"
                  v-model="config[field.key]"
                  :field="field"
                  :disabled="Boolean(field.disabled?.(config))"
                  @load-options="emit('load-options', $event)"
                  @refresh-options="emit('refresh-options', $event)" />
                <v-autocomplete
                  v-else-if="field.type === 'select' && field.searchable"
                  v-model="config[field.key]"
                  v-model:search="selectSearch[field.key]"
                  :label="field.label"
                  :items="filteredSelectItems(field)"
                  :hint="field.hint"
                  :persistent-hint="Boolean(field.hint)"
                  :disabled="Boolean(field.disabled?.(config))"
                  :no-filter="true"
                  no-data-text="没有匹配的订阅"
                  density="compact"
                  variant="outlined"
                  hide-details="auto" />
                <v-select
                  v-else-if="field.type === 'select'"
                  v-model="config[field.key]"
                  :label="field.label"
                  :items="field.items"
                  :hint="field.hint"
                  :persistent-hint="Boolean(field.hint)"
                  :disabled="Boolean(field.disabled?.(config))"
                  density="compact"
                  variant="outlined"
                  hide-details="auto" />
                <v-textarea
                  v-else-if="field.type === 'textarea'"
                  v-model="config[field.key]"
                  :label="field.label"
                  :hint="field.hint"
                  :placeholder="field.placeholder"
                  :persistent-hint="Boolean(field.hint)"
                  :disabled="Boolean(field.disabled?.(config))"
                  auto-grow
                  rows="2"
                  density="compact"
                  variant="outlined"
                  hide-details="auto" />
                <v-combobox
                  v-else-if="field.type === 'combobox'"
                  v-model="config[field.key]"
                  :label="field.label"
                  :items="field.items || []"
                  :hint="field.hint"
                  :persistent-hint="Boolean(field.hint)"
                  :loading="Boolean(field.loading)"
                  :multiple="field.multiple !== false"
                  :return-object="Boolean(field.returnObject)"
                  chips
                  closable-chips
                  clearable
                  density="compact"
                  variant="outlined"
                  hide-details="auto" />
                <v-text-field
                  v-else-if="field.type === 'cloud-directory' || field.type === 'local-directory'"
                  v-model="config[field.key]"
                  :label="field.label"
                  :hint="field.hint"
                  :placeholder="field.placeholder"
                  :persistent-hint="Boolean(field.hint)"
                  density="compact"
                  variant="outlined"
                  hide-details="auto">
                  <template #append-inner>
                    <v-btn
                      :icon="field.type === 'local-directory' ? 'mdi-folder-open-outline' : 'mdi-folder-search-outline'"
                      variant="text"
                      color="primary"
                      size="small"
                      :title="field.type === 'local-directory' ? '浏览并选择本地目录' : '浏览网盘目录'"
                      @click="emit('browse-directory', field.key, field.driveProvider, field.type === 'local-directory')" />
                  </template>
                </v-text-field>
                <v-text-field
                  v-else-if="field.type === 'proxy'"
                  v-model="config[field.key]"
                  :name="field.key"
                  autocomplete="off"
                  :label="field.label"
                  :hint="field.hint"
                  :placeholder="field.placeholder"
                  :persistent-hint="Boolean(field.hint)"
                  clearable
                  density="compact"
                  variant="outlined"
                  hide-details="auto">
                  <template #append-inner>
                    <v-btn
                      icon="mdi-lan-connect"
                      variant="text"
                      color="primary"
                      size="small"
                      title="测试代理连通性、延迟和出口"
                      :loading="testingProxy"
                      :disabled="testingProxy || !hasText(config[field.key])"
                      @click="emit('test-proxy')" />
                  </template>
                </v-text-field>
                <v-text-field
                  v-else-if="field.type === 'auto-subscribe-proxy'"
                  v-model="config[field.key]"
                  :name="field.key"
                  autocomplete="off"
                  :label="field.label"
                  :hint="field.hint"
                  :placeholder="field.placeholder"
                  :persistent-hint="Boolean(field.hint)"
                  clearable
                  density="compact"
                  variant="outlined"
                  hide-details="auto">
                  <template #append-inner>
                    <v-btn
                      icon="mdi-lan-connect"
                      variant="text"
                      color="primary"
                      size="small"
                      title="测试榜单代理连通性和延迟"
                      :loading="testingAutoSubscribeProxy"
                      :disabled="testingAutoSubscribeProxy || !hasText(config[field.key])"
                      @click="emit('test-auto-subscribe-proxy')" />
                  </template>
                </v-text-field>
                <v-text-field
                  v-else-if="field.type === 'number'"
                  v-model.number="config[field.key]"
                  :label="field.label"
                  type="number"
                  :min="field.min"
                  :max="field.max"
                  :suffix="field.suffix"
                  :hint="field.hint"
                  :placeholder="field.placeholder"
                  :clearable="field.clearable"
                  :persistent-hint="Boolean(field.hint)"
                  density="compact"
                  variant="outlined"
                  hide-details="auto" />
                <v-text-field
                  v-else
                  v-model="config[field.key]"
                  :label="field.label"
                  :type="field.type || 'text'"
                  :name="field.key"
                  :autocomplete="field.type === 'password' ? 'new-password' : field.autocomplete || 'off'"
                  :hint="field.hint"
                  :placeholder="field.placeholder"
                  :persistent-hint="Boolean(field.hint)"
                  :disabled="Boolean(field.disabled?.(config))"
                  density="compact"
                  variant="outlined"
                  hide-details="auto">
                  <template v-if="field.scanProvider || getAssociatedTestSource(group, field)" #append-inner>
                    <v-btn
                      v-if="field.scanProvider"
                      icon="mdi-qrcode-scan"
                      variant="text"
                      color="primary"
                      size="small"
                      title="扫码登录"
                      @click="emit('scan', field.scanProvider)" />
                    <v-btn
                      v-if="getAssociatedTestSource(group, field)"
                      icon="mdi-flask-outline"
                      variant="text"
                      color="primary"
                      size="small"
                      :loading="testingSource === getAssociatedTestSource(group, field).source"
                      :disabled="
                        !isTestSourceConfigured(getAssociatedTestSource(group, field).source) ||
                        (Boolean(testingSource) && testingSource !== getAssociatedTestSource(group, field).source)
                      "
                      :title="testSourceTitle(getAssociatedTestSource(group, field).source) || '测试搜索'"
                      @click="emit('test-source', getAssociatedTestSource(group, field).source)" />
                  </template>
                </v-text-field>
              </v-col>
            </template>
          </v-row>
          <v-divider v-if="index < visibleGroups.length - 1" class="group-divider" />
        </section>
      </template>
        </div>
      </transition>
    </div>
  </div>
</template>

<script setup>
import {computed, nextTick, onBeforeUnmount, onMounted, ref, watch} from "vue";
import AccountInfo from "./AccountInfo.vue";
import CheckinTimeline from "./CheckinTimeline.vue";
import RegionMediaMapField from "./RegionMediaMapField.vue";
import MultiSelectDialogField from "./MultiSelectDialogField.vue";

const props = defineProps({
  section: { type: Object, required: true },
  config: { type: Object, required: true },
  options: {type: Object, default: () => ({})},
  api: { type: [Object, Function], required: true },
  refreshingAccounts: { type: Array, default: () => [] },
  testingSource: { type: String, default: "" },
  testingAutoSubscribe: {type: String, default: ""},
  testingProxy: { type: Boolean, default: false },
  testingAutoSubscribeProxy: {type: Boolean, default: false},
  hdhiveOauthAction: { type: String, default: "" },
})

function getAccountInfo(field) {
  if (field.data && typeof field.data === "object" && Object.keys(field.data).length > 0) {
    return field.data;
  }
  const key = String(field.accountKey || "").trim();
  if (!key) return field.data || {};
  const [category, source] = key.split(":", 2);
  if (category === "drive") {
    return (
      props.options?.accounts?.[source] ||
      (props.options?.account && props.config?.cloud_drive === source ? props.options.account : null) ||
      {}
    );
  }
  if (category === "search") {
    return props.options?.searchAccounts?.[source] || {};
  }
  return field.data || {};
}
const emit = defineEmits([
  "scan",
  "browse-directory",
  "test-source",
  "test-auto-subscribe",
  "test-proxy",
  "test-auto-subscribe-proxy",
  "refresh-account",
  "hdhive-oauth-start",
  "hdhive-oauth-exchange",
  "checkin-result",
  "copy-text",
  "load-options",
  "refresh-options",
])

const hasText = (value) => Boolean(String(value || "").trim())

function isTestSourceConfigured(source) {
  // 通用原则：前端不耦合具体渠道配置，仅检查是否配置了资源类型优先级
  return Array.isArray(props.config.resource_type_order) && props.config.resource_type_order.length > 0;
}

function testSourceTitle(source) {
  return isTestSourceConfigured(source) ? "测试当前搜索渠道" : "请先在搜索顺序中选择至少一种资源类型";
}

function isAutoSubscribeConfigured(provider) {
  return Boolean(provider);
}

function autoSubscribeTestTitle(provider) {
  return "测试抓取最多 3 条榜单示例，仅验证连通性，不创建订阅";
}

const FALLBACK_ONLINE_DOC_TYPES = [
  {title: "115网盘", value: "115"},
  {title: "123网盘", value: "123"},
  {title: "夸克网盘", value: "quark"},
  {title: "阿里云盘", value: "alipan"},
  {title: "百度网盘", value: "baidu"},
  {title: "UC网盘", value: "uc"},
  {title: "天翼云盘", value: "tianyi"},
  {title: "移动云盘", value: "yun139"},
  {title: "光鸭网盘", value: "guangya"},
  {title: "迅雷网盘", value: "xunlei"},
  {title: "磁力链接", value: "magnet"},
  {title: "电驴链接", value: "ed2k"},
];

function onlineDocumentTypeItems(field) {
  if (Array.isArray(props.options?.resourceTypes) && props.options.resourceTypes.length > 0) {
    return props.options.resourceTypes.map((item) => ({
      title: item.name || item.title || item.value,
      value: item.value,
    }));
  }
  if (Array.isArray(field?.items) && field.items.length > 0) {
    return field.items;
  }
  return FALLBACK_ONLINE_DOC_TYPES;
}

function onlineDocuments(key) {
  if (!Array.isArray(props.config[key])) props.config[key] = []
  return props.config[key]
}

function addOnlineDocument(key, index) {
  onlineDocuments(key).splice(index + 1, 0, { url: "", resource_types: [] })
}

function removeOnlineDocument(key, index) {
  const documents = onlineDocuments(key)
  if (documents.length <= 1) {
    documents.splice(0, 1, { url: "", resource_types: [] })
    return
  }
  documents.splice(index, 1)
}

function getAssociatedTestSource(group, field) {
  if (!group?.fields?.length || !field) return null;
  const isAddressField =
    field.key?.endsWith("_base_url") ||
    (field.key?.endsWith("_url") && String(field.label || "").includes("地址")) ||
    field.testSource;
  if (!isAddressField) return null;

  const sourceKey = field.testSource || group.fields.find((f) => f.type === "test-source")?.source;
  if (!sourceKey) return null;
  return {source: sourceKey};
}

function isFieldEmbedded(group, field) {
  if (field.type !== "test-source") return false;
  return Boolean(
    group.fields?.some(
      (f) =>
        f.key?.endsWith("_base_url") ||
        (f.key?.endsWith("_url") && String(f.label || "").includes("地址")) ||
        f.testSource,
    ),
  );
}

const activeSubtab = ref(props.section.subtabs?.[0]?.value || "")
const subtabScrollRef = ref(null);
const canScrollLeft = ref(false);
const canScrollRight = ref(false);

function updateScrollState() {
  const el = subtabScrollRef.value;
  if (!el) return;
  canScrollLeft.value = el.scrollLeft > 6;
  canScrollRight.value = el.scrollLeft + el.clientWidth < el.scrollWidth - 6;
}

function onSubtabScroll() {
  updateScrollState();
}

function scrollSubtabs(delta) {
  if (subtabScrollRef.value) {
    subtabScrollRef.value.scrollBy({left: delta, behavior: "smooth"});
    setTimeout(updateScrollState, 260);
  }
}

function onSubtabWheel(event) {
  if (subtabScrollRef.value && event.deltaY !== 0) {
    subtabScrollRef.value.scrollLeft += event.deltaY;
    updateScrollState();
  }
}

function onSubtabClick(val, event) {
  activeSubtab.value = val;
  nextTick(() => {
    updateScrollState();
    if (event?.currentTarget) {
      event.currentTarget.scrollIntoView({behavior: "smooth", inline: "center", block: "nearest"});
    } else if (subtabScrollRef.value) {
      const activeEl = subtabScrollRef.value.querySelector(".subtab-item--active");
      activeEl?.scrollIntoView({behavior: "smooth", inline: "center", block: "nearest"});
    }
  });
}

onMounted(() => {
  nextTick(() => {
    updateScrollState();
    window.addEventListener("resize", updateScrollState);
  });
});

onBeforeUnmount(() => {
  window.removeEventListener("resize", updateScrollState);
});

watch(
  () => props.section,
  (newSection) => {
    if (newSection?.subtabs?.length) {
      if (!newSection.subtabs.some((t) => t.value === activeSubtab.value)) {
        activeSubtab.value = newSection.subtabs[0].value;
      }
      nextTick(updateScrollState);
    }
  },
  {immediate: true},
);

const selectSearch = ref({})

function normalizeSearchText(value) {
  return String(value ?? "")
    .normalize("NFKC")
    .toLocaleLowerCase()
    .replace(/\s+/g, "")
}

function filteredSelectItems(field) {
  const items = Array.isArray(field.items) ? field.items : []
  const keyword = normalizeSearchText(selectSearch.value[field.key])
  if (!keyword) return items
  const selected = new Set(
    (Array.isArray(props.config[field.key]) ? props.config[field.key] : [props.config[field.key]])
      .filter((value) => value !== undefined && value !== null)
      .map((value) => String(value)),
  )
  return items.filter(
    (item) =>
      selected.has(String(item?.value ?? "")) ||
      normalizeSearchText(`${item?.title || ""} ${item?.value || ""}`).includes(keyword),
  )
}

function evalShowCondition(condition, config) {
  if (condition === undefined || condition === null || condition === "") return true;
  if (typeof condition === "function") {
    try {
      return Boolean(condition(config));
    } catch {
      return true;
    }
  }
  if (typeof condition === "boolean") return condition;
  if (typeof condition === "string") {
    try {
      return Boolean(new Function("config", `"use strict"; return Boolean(${condition});`)(config));
    } catch {
      return true;
    }
  }
  return true;
}

const availableGroups = computed(() =>
  (props.section?.groups || []).filter((group) => evalShowCondition(group.show, props.config)),
);
const visibleGroups = computed(() => {
  const leadingGroups = availableGroups.value.filter((group) => group.beforeTabs)
  const tabGroups = availableGroups.value.filter(
    (group) => !group.beforeTabs && (!props.section.subtabs?.length || !group.tab || group.tab === activeSubtab.value),
  )
  return [...leadingGroups, ...tabGroups]
})

function mediaLibraryWebhookUrl(field, serverName) {
  const relativeUrl = String(field?.urls?.[serverName] || "").trim()
  return relativeUrl ? new URL(relativeUrl, window.location.origin).toString() : ""
}
</script>

<style scoped>
.config-tab-content {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.config-section-scroll {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  min-height: 0;
  flex: 1 1 0;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
  touch-action: pan-y;
  scrollbar-width: none;
}

.config-section-scroll::-webkit-scrollbar {
  display: none;
  width: 0;
  height: 0;
}

.config-group {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  margin-bottom: 26px;
}

.config-group:last-child {
  margin-bottom: 10px;
}

/* 现代连贯一体化二级 Tab 选项卡（Line Tab）设计 */
.subtab-wrapper {
  position: relative;
  display: flex;
  align-items: center;
  width: 100%;
  margin-bottom: 16px;
  flex: 0 0 auto;
  border-bottom: 1px solid rgba(var(--v-border-color), 0.12);
  gap: 2px;
}

.subtab-arrow-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border-radius: 50%;
  border: none;
  background: transparent;
  color: rgba(var(--v-theme-on-surface), 0.55);
  cursor: pointer;
  flex-shrink: 0;
  transition: all 0.2s ease;
  margin-bottom: 2px;
}

.subtab-arrow-btn:hover:not(.is-disabled) {
  color: rgb(var(--v-theme-primary));
  background: rgba(var(--v-theme-primary), 0.08);
  transform: scale(1.06);
}

.subtab-arrow-btn.is-disabled {
  opacity: 0.22;
  cursor: default;
  pointer-events: none;
}

.subtab-viewport-wrapper {
  position: relative;
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
}

.subtab-fade-mask {
  position: absolute;
  top: 0;
  bottom: 1px;
  width: 28px;
  pointer-events: none;
  z-index: 2;
  transition: opacity 0.2s ease;
}

.subtab-fade-mask--left {
  left: 0;
  background: linear-gradient(to right, rgb(var(--v-theme-surface)), transparent);
}

.subtab-fade-mask--right {
  right: 0;
  background: linear-gradient(to left, rgb(var(--v-theme-surface)), transparent);
}

.subtab-menu-trigger {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 30px;
  padding: 0 9px;
  border-radius: 6px;
  border: 1px solid rgba(var(--v-border-color), 0.18);
  background: rgba(var(--v-theme-on-surface), 0.03);
  color: rgba(var(--v-theme-on-surface), 0.75);
  cursor: pointer;
  flex-shrink: 0;
  transition: all 0.2s ease;
  margin-left: 4px;
  margin-bottom: 2px;
  white-space: nowrap;
}

.subtab-menu-text {
  font-size: 0.8125rem;
  font-weight: 500;
  line-height: 1;
}

.subtab-menu-trigger:hover,
.subtab-menu-trigger.has-hidden {
  color: rgb(var(--v-theme-primary));
  border-color: rgba(var(--v-theme-primary), 0.35);
  background: rgba(var(--v-theme-primary), 0.06);
}

.subtab-more-dot {
  position: absolute;
  top: -2px;
  right: -2px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background-color: rgb(var(--v-theme-primary));
  box-shadow: 0 0 0 2px rgb(var(--v-theme-surface));
  animation: pulse-dot 2s infinite ease-in-out;
}

@keyframes pulse-dot {
  0%, 100% {
    transform: scale(1);
    opacity: 0.9;
  }
  50% {
    transform: scale(1.3);
    opacity: 1;
  }
}

.subtab-dropdown-menu {
  max-height: 380px;
  overflow-y: auto;
  width: max-content;
  min-width: 0;
  padding: 5px;
  background-color: rgb(var(--v-theme-surface));
  border: 1px solid rgba(var(--v-border-color), 0.18);
  border-radius: 10px !important;
  box-shadow: 0 10px 28px -4px rgba(0, 0, 0, 0.14), 0 2px 6px -1px rgba(0, 0, 0, 0.06);
}

.subtab-dropdown-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 4px 8px 6px;
  font-size: 0.75rem;
  font-weight: 600;
  color: rgba(var(--v-theme-on-surface), 0.52);
  border-bottom: 1px solid rgba(var(--v-border-color), 0.08);
  margin-bottom: 4px;
  user-select: none;
  white-space: nowrap;
}

.subtab-dropdown-count {
  font-size: 0.6875rem;
  padding: 0 5px;
  height: 16px;
  line-height: 16px;
  border-radius: 4px;
  background: rgba(var(--v-theme-on-surface), 0.06);
  color: rgba(var(--v-theme-on-surface), 0.58);
  font-weight: 600;
}

.subtab-dropdown-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: max-content;
  min-width: 100%;
}

.subtab-dropdown-item {
  position: relative;
  display: flex;
  align-items: center;
  width: 100%;
  height: 32px;
  padding: 0 8px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: rgba(var(--v-theme-on-surface), 0.78);
  cursor: pointer;
  text-align: left;
  user-select: none;
  transition: all 0.15s cubic-bezier(0.4, 0, 0.2, 1);
  font-size: 0.84rem;
  white-space: nowrap;
}

.subtab-dropdown-item:hover {
  background: rgba(var(--v-theme-on-surface), 0.05);
  color: rgb(var(--v-theme-on-surface));
}

.subtab-dropdown-item.is-active {
  background: rgba(var(--v-theme-primary), 0.09) !important;
  color: rgb(var(--v-theme-primary)) !important;
  font-weight: 600;
}

.subtab-dropdown-icon {
  margin-right: 8px;
  flex-shrink: 0;
  opacity: 0.72;
}

.subtab-dropdown-item.is-active .subtab-dropdown-icon {
  opacity: 1;
  color: rgb(var(--v-theme-primary));
}

.subtab-dropdown-title {
  white-space: nowrap;
  margin-right: 12px;
}

.subtab-dropdown-check {
  margin-left: auto;
  flex-shrink: 0;
  color: rgb(var(--v-theme-primary));
}

.subtab-scroll-container {
  flex: 1 1 auto;
  min-width: 0;
  overflow-x: auto;
  scrollbar-width: none;
  -webkit-overflow-scrolling: touch;
  scroll-behavior: smooth;
}

.subtab-scroll-container::-webkit-scrollbar {
  display: none;
}

.subtab-bar {
  display: inline-flex;
  align-items: flex-end;
  gap: 2px;
  padding: 0;
  background: transparent !important;
  border: none !important;
}

.subtab-item {
  position: relative;
  display: inline-flex;
  align-items: center;
  height: 34px;
  padding: 0 13px;
  border: none !important;
  border-radius: 6px 6px 0 0;
  background: transparent;
  color: rgba(var(--v-theme-on-surface), 0.7);
  font-size: 0.875rem;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
  user-select: none;
  transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
  flex-shrink: 0;
  margin-bottom: -1px;
}

.subtab-item:hover {
  color: rgb(var(--v-theme-on-surface));
  background: rgba(var(--v-theme-on-surface), 0.04);
}

.subtab-item--active {
  color: rgb(var(--v-theme-primary)) !important;
  font-weight: 600;
  background: rgba(var(--v-theme-primary), 0.06) !important;
}

.subtab-item--active::after {
  content: "";
  position: absolute;
  left: 6px;
  right: 6px;
  bottom: 0;
  height: 2px;
  border-radius: 2px 2px 0 0;
  background: rgb(var(--v-theme-primary));
}

.subtab-icon {
  margin-right: 6px;
  color: inherit;
  opacity: 0.85;
}

/* 二级 Tab 切换淡入淡出动画 */
.tab-fade-enter-active,
.tab-fade-leave-active {
  transition: opacity 0.18s cubic-bezier(0.4, 0, 0.2, 1), transform 0.18s cubic-bezier(0.4, 0, 0.2, 1);
}

.tab-fade-enter-from {
  opacity: 0;
  transform: translateY(4px);
}

.tab-fade-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

.group-heading {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  padding-bottom: 2px;
}

.group-heading-icon {
  flex-shrink: 0;
}

.group-heading-content {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}

.group-title {
  flex-shrink: 0;
  color: rgb(var(--v-theme-on-surface));
  font-size: 0.875rem;
  font-weight: 600;
}

.group-hint {
  font-size: 0.78rem;
  line-height: 1.45;
  color: rgba(var(--v-theme-on-surface), 0.58);
}

.subtab-pane {
  width: 100%;
  max-width: 100%;
  min-width: 0;
}

.config-fields-row {
  width: 100%;
  margin-inline: -4px;
}

.config-field-col {
  min-width: 0;
  margin-bottom: 8px;
}

/* 统一所有输入框规范：严格保证宽高、圆角、内边距绝对一致 */
.config-field-col :deep(.v-input) {
  width: 100%;
  min-width: 0;
}

.config-field-col :deep(.v-field) {
  width: 100%;
  min-width: 0;
  height: 40px !important;
  min-height: 40px !important;
  border-radius: 8px !important;
}

.config-field-col :deep(.v-field__input) {
  height: 40px !important;
  min-height: 40px !important;
  padding-top: 0 !important;
  padding-bottom: 0 !important;
  display: flex;
  align-items: center;
}

.config-field-col :deep(.v-field__append-inner),
.config-field-col :deep(.v-field__prepend-inner),
.config-field-col :deep(.v-field__clearable) {
  padding-top: 0 !important;
  display: flex;
  align-items: center;
}

.config-field-col :deep(.v-input__details) {
  padding-inline: 4px;
  min-height: 18px;
}

.inner-test-btn {
  transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1) !important;
}

.inner-test-btn:hover {
  transform: scale(1.1);
  background-color: rgba(var(--v-theme-primary), 0.12) !important;
}

/* 拟物立体弹出开关（Pop-out Tactile Switch）全新设计 */
.config-switch {
  min-width: 0;
  min-height: 38px;
  margin: 0;
  padding-inline: 4px;
  cursor: pointer;
  user-select: none;
}

.config-switch :deep(.v-selection-control) {
  min-height: 38px;
}

/* 开关轨道：内凹立体微阴影 + 柔和胶囊轮廓 */
.config-switch :deep(.v-switch__track) {
  height: 22px !important;
  width: 40px !important;
  border-radius: 11px !important;
  opacity: 1 !important;
  background-color: rgba(var(--v-theme-on-surface), 0.18) !important;
  box-shadow: inset 0 1.5px 3px rgba(0, 0, 0, 0.2), 0 0 0 1px rgba(var(--v-border-color), 0.12) !important;
  transition: all 0.28s cubic-bezier(0.4, 0, 0.2, 1) !important;
}

/* 开启状态轨道：漫溢品牌色柔和光晕 */
.config-switch :deep(.v-selection-control--dirty .v-switch__track) {
  background-color: rgb(var(--v-theme-primary)) !important;
  box-shadow: inset 0 1px 2px rgba(0, 0, 0, 0.15), 0 2px 8px rgba(var(--v-theme-primary), 0.35) !important;
}

/* 滑块：微立体凸起（Pop-out 质感） + 物理回弹弹簧曲线 */
.config-switch :deep(.v-switch__thumb) {
  width: 16px !important;
  height: 16px !important;
  border-radius: 50% !important;
  background: #ffffff !important;
  box-shadow: 0 2px 5px rgba(0, 0, 0, 0.25), 0 0 0 0.5px rgba(255, 255, 255, 0.9) !important;
  transition: transform 0.32s cubic-bezier(0.34, 1.56, 0.64, 1),
  box-shadow 0.24s ease !important;
}

/* 悬停微弹出感：滑块轻微向外凸出放大 */
.config-switch:hover :deep(.v-switch__thumb) {
  transform: scale(1.08);
  box-shadow: 0 3px 8px rgba(0, 0, 0, 0.28), 0 0 0 1px rgba(255, 255, 255, 0.95) !important;
}

/* 开启态滑块质感 */
.config-switch :deep(.v-selection-control--dirty .v-switch__thumb) {
  background: #ffffff !important;
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.25), 0 0 2px 1px rgba(255, 255, 255, 0.8) !important;
}

/* 点击按压蓄力反馈：滑块微扁微缩 */
.config-switch:active :deep(.v-switch__thumb) {
  transform: scale(0.92) scaleX(1.12);
}

.config-switch :deep(.v-label) {
  min-width: 0;
  font-size: 0.875rem;
  line-height: 1.35;
  white-space: normal;
  overflow-wrap: anywhere;
  color: rgb(var(--v-theme-on-surface));
}

.config-switch :deep(.v-input__details) {
  padding-inline: 4px;
}

.hdhive-oauth-panel {
  padding: 12px;
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 8px;
  background: rgba(var(--v-theme-primary), 0.035);
}

.media-library-webhook {
  display: grid;
  gap: 10px;
  min-width: 0;
}

.media-library-webhook__actions {
  display: flex;
  align-items: center;
  gap: 2px;
}

.online-documents {
  min-width: 0;
}

.region-media-map {
  display: grid;
  gap: 8px;
  min-width: 0;
}

.region-media-map__panels {
  margin-top: 8px;
}

.grouped-select {
  display: grid;
  gap: 8px;
  min-width: 0;
}

.grouped-select__panels {
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 8px;
  overflow: hidden;
}

.online-document-row {
  display: grid;
  grid-template-columns: minmax(220px, 1.2fr) minmax(220px, 1fr) 40px 40px;
  align-items: start;
  gap: 10px;
  margin-bottom: 10px;
}

@media (max-width: 720px) {
  .subtab-arrow-btn {
    display: none !important;
  }

  .subtab-wrapper {
    gap: 0;
    margin-bottom: 12px;
  }

  .subtab-scroll-container {
    -webkit-overflow-scrolling: touch;
    touch-action: pan-x;
    overscroll-behavior-x: contain;
  }

  .subtab-item {
    height: 31px;
    padding: 0 12px;
    font-size: 0.8125rem;
  }

  .online-document-row {
    grid-template-columns: minmax(0, 1fr) 40px 40px;
  }

  .online-document-row :deep(.v-select) {
    grid-column: 1;
  }
}

:deep(input:-webkit-autofill),
:deep(input:-webkit-autofill:hover),
:deep(input:-webkit-autofill:focus),
:deep(input:-webkit-autofill:active) {
  -webkit-box-shadow: 0 0 0 1000px rgb(var(--v-theme-surface)) inset !important;
  -webkit-text-fill-color: rgb(var(--v-theme-on-surface)) !important;
  transition: background-color 5000s ease-in-out 0s;
}
</style>
