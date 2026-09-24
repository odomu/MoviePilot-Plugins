<template>
  <div class="multi-select-dialog-field">
    <!-- 表单外层触发器输入框：严格单行不换行，杜绝标签过多撑破下边缘溢出 -->
    <v-text-field
      :model-value="selectedList.length > 0 ? ' ' : ''"
      :label="field.label"
      :hint="field.hint"
      :persistent-hint="Boolean(field.hint)"
      :placeholder="selectedList.length === 0 ? (field.placeholder || '点击选择...') : ''"
      :disabled="disabled"
      readonly
      density="compact"
      variant="outlined"
      hide-details="auto"
      class="multi-select-custom-field"
      :class="{ 'has-selected': selectedList.length > 0 }"
      :prepend-inner-icon="field.icon || 'mdi-checkbox-multiple-marked-outline'"
      @click="openDialog">
      <!-- 左侧已选项目胶囊预览（单行水平流，严格禁止换行溢出，超出用 +N 聚合） -->
      <template v-if="selectedList.length > 0" #default>
        <div class="d-flex align-center flex-nowrap ga-1 trigger-chips-wrapper" @click.stop="openDialog">
          <v-chip
            v-for="item in visibleChips"
            :key="String(item.value)"
            size="x-small"
            variant="tonal"
            color="primary"
            class="font-weight-medium my-0 flex-shrink-0 trigger-chip">
            {{ item.title }}
          </v-chip>
          <v-chip
            v-if="remainingCount > 0"
            size="x-small"
            variant="tonal"
            color="secondary"
            class="font-weight-medium my-0 flex-shrink-0 remaining-chip">
            +{{ remainingCount }}
          </v-chip>
        </div>
      </template>

      <!-- 右侧统一对齐展示：【清空 x 按钮】+【总数量徽标】+【下拉指示箭头】 -->
      <template #append-inner>
        <div class="d-flex align-center ga-1 trigger-append-actions">
          <!-- 清空 x 按钮 -->
          <button
            v-if="selectedList.length > 0 && !disabled"
            type="button"
            class="echo-clear-trigger"
            title="清空已选项"
            @click.stop="clearSelection">
            <v-icon icon="mdi-close" size="13" />
          </button>

          <!-- 总数量徽标 -->
          <span v-if="selectedList.length > 0" class="echo-count-pill">
            {{ selectedList.length }} 项
          </span>

          <!-- 下拉小箭头 -->
          <v-icon
            icon="mdi-chevron-down"
            size="16"
            class="echo-chevron-icon"
            :class="{ 'echo-chevron-icon--open': dialogVisible }" />
        </div>
      </template>
    </v-text-field>

    <!-- 弹窗多选选择器 -->
    <v-dialog v-model="dialogVisible" max-width="620" scrollable>
      <v-card class="multi-select-dialog rounded-xl overflow-hidden">
        <!-- 弹窗顶部 Header：高光渐变顶板、微阴影凸起图标底座、药丸徽标与细腻关闭按钮 -->
        <div class="dialog-header d-flex align-center px-4 py-3 border-b">
          <div class="dialog-header-icon mr-3">
            <v-icon
              :icon="field.icon || 'mdi-checkbox-multiple-marked-outline'"
              size="18"
              color="primary" />
          </div>
          <div class="flex-grow-1 overflow-hidden">
            <div class="dialog-header-title text-subtitle-1 font-weight-bold text-truncate">
              {{ field.label }}
            </div>
            <div class="dialog-header-subtitle text-caption text-medium-emphasis text-truncate">
              {{ field.hint || (field.allowCustom ? "支持勾选与回车自定义添加" : "支持多选，点击卡片直接勾选或取消") }}
            </div>
          </div>
          <v-chip size="small" variant="tonal" color="primary"
                  class="font-weight-medium px-2 mr-1 flex-shrink-0 dialog-count-badge">
            已选择 {{ tempSelection.length }} / {{ normalizedItems.length }} 项
          </v-chip>
          <v-btn
            icon="mdi-close"
            variant="text"
            size="small"
            class="dialog-close-btn ml-1 flex-shrink-0"
            title="关闭"
            @click="dialogVisible = false" />
        </div>

        <v-divider />

        <!-- 工具栏：搜索、自定义输入与批量操作（已移除冗余的顶栏生效标签，界面清爽直接） -->
        <div class="dialog-toolbar px-4 pt-3 pb-2 d-flex align-center justify-space-between ga-2">
          <!-- 搜索与自定义输入条 -->
          <div v-if="showSearch" class="search-box-wrapper flex-grow-1">
            <v-text-field
              v-model="searchKeyword"
              :placeholder="searchPlaceholder"
              density="compact"
              variant="solo-filled"
              flat
              rounded="pill"
              prepend-inner-icon="mdi-magnify"
              hide-details
              clearable
              class="compact-search-input"
              @keydown.enter.prevent="addCustomItem">
              <!-- 支持自定义添加按钮 -->
              <template v-if="canAddCustom" #append-inner>
                <v-btn
                  size="x-small"
                  variant="flat"
                  color="primary"
                  rounded="pill"
                  class="px-2 my-n1 font-weight-bold"
                  title="回车或点击添加为新项并自动勾选"
                  @click.stop="addCustomItem">
                  添加
                </v-btn>
              </template>
            </v-text-field>
          </div>
          <div v-else class="text-caption text-medium-emphasis">
            共 {{ normalizedItems.length }} 项可选
          </div>

          <!-- 批量操作微胶囊组 -->
          <div class="toolbar-actions d-flex align-center ga-1 flex-shrink-0">
            <v-btn
              v-if="field.dynamicOptions?.refreshable"
              icon="mdi-refresh"
              variant="text"
              size="small"
              :loading="Boolean(field.loading)"
              title="刷新选项列表"
              @click="refreshOptions" />
            <v-btn
              variant="tonal"
              size="small"
              rounded="pill"
              color="primary"
              class="action-btn px-2 text-caption font-weight-medium"
              prepend-icon="mdi-check-all"
              @click="selectAll">
              全选
            </v-btn>
            <v-btn
              variant="tonal"
              size="small"
              rounded="pill"
              color="primary"
              class="action-btn px-2 text-caption font-weight-medium"
              prepend-icon="mdi-swap-horizontal"
              @click="invertSelection">
              反选
            </v-btn>
            <v-btn
              variant="tonal"
              size="small"
              rounded="pill"
              color="error"
              class="action-btn action-btn--clear px-2 text-caption font-weight-medium"
              prepend-icon="mdi-trash-can-outline"
              @click="clearTemp">
              清空
            </v-btn>
          </div>
        </div>

        <!-- 选项列表主体（双列卡片网格布局，消除右侧空洞感） -->
        <v-card-text class="pt-1 pb-2 px-4">
          <!-- 空匹配状态 -->
          <div v-if="field.loading" class="text-center py-8 text-medium-emphasis">
            <v-progress-circular indeterminate color="primary" size="32" width="3" class="mb-2" />
            <div class="text-caption">正在加载选项</div>
          </div>
          <div v-else-if="filteredItems.length === 0" class="text-center py-8 text-medium-emphasis">
            <v-icon :icon="field.loadError ? 'mdi-alert-circle-outline' : 'mdi-file-search-outline'" size="36"
                    class="mb-2 opacity-40" />
            <div class="text-caption">
              {{ field.loadError || (field.allowCustom && searchKeyword ? `无匹配项，可按回车添加 "${formattedCustomInput}"` : "没有可选项")
              }}
            </div>
            <v-btn
              v-if="canAddCustom"
              size="small"
              variant="tonal"
              color="primary"
              prepend-icon="mdi-plus"
              class="mt-2"
              @click="addCustomItem">
              添加 "{{ formattedCustomInput }}" 并勾选
            </v-btn>
          </div>

          <!-- 双列自适应网格 -->
          <div v-else class="options-grid-container">
            <div
              v-for="item in filteredItems"
              :key="String(item.value)"
              class="option-card d-flex align-center px-3 py-2 rounded-lg cursor-pointer"
              :class="{ 'is-selected': isTempSelected(item.value) }"
              @click="toggleItem(item.value)">
              <!-- 精准对齐的勾选框图标 -->
              <v-icon
                :icon="isTempSelected(item.value) ? 'mdi-checkbox-marked' : 'mdi-checkbox-blank-outline'"
                :color="isTempSelected(item.value) ? 'primary' : 'medium-emphasis'"
                size="20"
                class="mr-2 flex-shrink-0 option-checkbox" />

              <div class="option-info flex-grow-1 overflow-hidden">
                <div class="d-flex align-center ga-1">
                  <div
                    class="option-title font-weight-medium text-body-2 text-truncate"
                    :class="{ 'text-primary font-weight-bold': isTempSelected(item.value) }">
                    {{ item.title }}
                  </div>
                  <v-chip
                    v-if="item.isCustom"
                    size="x-small"
                    variant="tonal"
                    color="secondary"
                    class="px-1 custom-tag">
                    自定义
                  </v-chip>
                </div>
                <div v-if="item.hint" class="option-hint text-caption text-medium-emphasis text-truncate">
                  {{ item.hint }}
                </div>
              </div>

              <!-- 自定义添加项支持从候选池彻底删除 -->
              <v-btn
                v-if="item.isCustom"
                icon="mdi-trash-can-outline"
                size="x-small"
                variant="text"
                color="error"
                class="ml-1 flex-shrink-0 custom-del-btn"
                title="删除此自定义项"
                @click.stop="removeCustomItem(item.value)" />
            </div>
          </div>
        </v-card-text>

        <v-divider />

        <!-- 底部操作栏 -->
        <v-card-actions class="px-4 py-2">
          <div class="text-caption text-medium-emphasis">
            已勾选 <span class="font-weight-bold text-primary">{{ tempSelection.length }}</span> 项
          </div>
          <v-spacer />
          <v-btn variant="text" size="small" class="px-4" @click="dialogVisible = false">
            取消
          </v-btn>
          <v-btn
            color="primary"
            variant="flat"
            size="small"
            class="px-5 font-weight-medium"
            @click="applySelection">
            确定
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </div>
</template>

<script setup>
import {computed, ref} from "vue";

const props = defineProps({
  modelValue: {
    type: [Array, String],
    default: () => [],
  },
  field: {
    type: Object,
    default: () => ({}),
  },
  disabled: {
    type: Boolean,
    default: false,
  },
});

const emit = defineEmits(["update:modelValue", "load-options", "refresh-options"]);

const dialogVisible = ref(false);
const searchKeyword = ref("");
const tempSelection = ref([]);
const customItems = ref([]);

// 规范化配置自带的候选项目为标准格式 [{ title, value, hint, isCustom: false }]
const baseItems = computed(() => {
  const items = props.field?.items || [];
  if (!Array.isArray(items)) return [];

  return items.map((item) => {
    if (typeof item === "string" || typeof item === "number") {
      return {title: String(item), value: item, hint: "", isCustom: false};
    }
    if (item && typeof item === "object") {
      const title = item.title ?? item.label ?? item.name ?? String(item.value ?? "");
      const value = item.value ?? item.title ?? item.label ?? "";
      const hint = item.hint ?? item.subtitle ?? item.desc ?? "";
      return {title, value, hint, isCustom: false};
    }
    return {title: String(item), value: item, hint: "", isCustom: false};
  });
});

// 合并配置候选项与用户添加的自定义项
const normalizedItems = computed(() => {
  const combined = [...baseItems.value];
  const seen = new Set(combined.map((i) => String(i.value).toLowerCase()));
  for (const item of customItems.value) {
    const key = String(item.value).toLowerCase();
    if (!seen.has(key)) {
      seen.add(key);
      combined.push(item);
    }
  }
  return combined;
});

// 已选项目的对象列表
const selectedList = computed(() => {
  const current = Array.isArray(props.modelValue)
    ? props.modelValue
    : (props.modelValue ? [props.modelValue] : []);
  const map = new Map(normalizedItems.value.map((i) => [String(i.value).toLowerCase(), i]));
  return current.map((val) => {
    const s = String(val).toLowerCase();
    return map.get(s) || {title: String(val), value: val, isCustom: true};
  });
});

// 触发器中严格单行紧凑展示（最多 3 个），防止折行撑破外框
const maxVisible = computed(() => Math.min(Number(props.field?.maxChips) || 3, 3));
const visibleChips = computed(() => selectedList.value.slice(0, maxVisible.value));
const remainingCount = computed(() => Math.max(0, selectedList.value.length - maxVisible.value));

// 是否展示搜索/自定义输入栏
const showSearch = computed(() => {
  return (
    Boolean(props.field?.allowCustom) ||
    normalizedItems.value.length > 8 ||
    Boolean(props.field?.searchable)
  );
});

// 搜索栏 Placeholder
const searchPlaceholder = computed(() => {
  if (props.field?.customPlaceholder) return props.field.customPlaceholder;
  if (props.field?.allowCustom) {
    return props.field.isExtension
      ? "过滤选项或输入扩展名(如 .ts)回车添加..."
      : "过滤选项或输入回车直接添加自定义...";
  }
  return "快速过滤选项...";
});

// 格式化用户输入的自定义值（若是扩展名自动规范为以点开头的全小写）
function formatCustomValue(raw) {
  let val = String(raw || "").trim();
  if (props.field?.isExtension) {
    if (!val.startsWith(".")) {
      val = "." + val;
    }
    val = val.toLowerCase();
  }
  return val;
}

const formattedCustomInput = computed(() => formatCustomValue(searchKeyword.value));

// 是否可触发自定义添加
const canAddCustom = computed(() => {
  if (!props.field?.allowCustom) return false;
  const val = formattedCustomInput.value;
  if (!val || (props.field?.isExtension && val === ".")) return false;
  return true;
});

// 过滤后的候选项目
const filteredItems = computed(() => {
  const kw = String(searchKeyword.value || "").trim().toLowerCase();
  if (!kw) return normalizedItems.value;
  return normalizedItems.value.filter((item) => {
    return (
      String(item.title || "").toLowerCase().includes(kw) ||
      String(item.value || "").toLowerCase().includes(kw) ||
      String(item.hint || "").toLowerCase().includes(kw)
    );
  });
});

function openDialog() {
  if (props.disabled) return;
  const current = Array.isArray(props.modelValue)
    ? props.modelValue
    : (props.modelValue ? [props.modelValue] : []);
  tempSelection.value = [...current];
  searchKeyword.value = "";

  // 自动从当前已选值中识别不在配置 baseItems 里的自定义项，补入 customItems 中以正常显示
  if (props.field?.allowCustom) {
    const baseSet = new Set(baseItems.value.map((i) => String(i.value).toLowerCase()));
    for (const val of current) {
      const sVal = String(val).trim();
      if (sVal && !baseSet.has(sVal.toLowerCase())) {
        if (!customItems.value.some((c) => String(c.value).toLowerCase() === sVal.toLowerCase())) {
          customItems.value.push({
            title: sVal,
            value: sVal,
            hint: "自定义扩展名",
            isCustom: true,
          });
        }
      }
    }
  }

  dialogVisible.value = true;
  if (props.field?.dynamicOptions) {
    emit("load-options", props.field.dynamicOptions);
  }
}

function refreshOptions() {
  if (props.field?.dynamicOptions) {
    emit("refresh-options", props.field.dynamicOptions);
  }
}

function isTempSelected(val) {
  const target = String(val).toLowerCase();
  return tempSelection.value.some((v) => String(v).toLowerCase() === target);
}

function toggleItem(val) {
  const target = String(val).toLowerCase();
  const index = tempSelection.value.findIndex((v) => String(v).toLowerCase() === target);
  if (index !== -1) {
    tempSelection.value.splice(index, 1);
  } else {
    tempSelection.value.push(val);
  }
}

function removeTempItem(val) {
  const target = String(val).toLowerCase();
  const index = tempSelection.value.findIndex((v) => String(v).toLowerCase() === target);
  if (index !== -1) {
    tempSelection.value.splice(index, 1);
  }
}

function addCustomItem() {
  if (!canAddCustom.value) return;
  const val = formattedCustomInput.value;

  const existsInBase = baseItems.value.some((i) => String(i.value).toLowerCase() === val.toLowerCase());
  const existsInCustom = customItems.value.some((i) => String(i.value).toLowerCase() === val.toLowerCase());

  if (!existsInBase && !existsInCustom) {
    customItems.value.push({
      title: val,
      value: val,
      hint: "自定义扩展名",
      isCustom: true,
    });
  }

  if (!tempSelection.value.some((v) => String(v).toLowerCase() === val.toLowerCase())) {
    tempSelection.value.push(val);
  }

  searchKeyword.value = "";
}

function removeCustomItem(val) {
  const target = String(val).toLowerCase();
  const cIndex = customItems.value.findIndex((i) => String(i.value).toLowerCase() === target);
  if (cIndex !== -1) {
    customItems.value.splice(cIndex, 1);
  }
  removeTempItem(val);
}

function selectAll() {
  const allValues = filteredItems.value.map((i) => i.value);
  const currentSet = new Set(tempSelection.value.map((v) => String(v).toLowerCase()));
  for (const val of allValues) {
    currentSet.add(String(val).toLowerCase());
  }
  tempSelection.value = normalizedItems.value
    .filter((i) => currentSet.has(String(i.value).toLowerCase()))
    .map((i) => i.value);
}

function invertSelection() {
  const visibleValues = new Set(filteredItems.value.map((i) => String(i.value).toLowerCase()));
  const currentSet = new Set(tempSelection.value.map((v) => String(v).toLowerCase()));
  const newSet = new Set();

  for (const v of tempSelection.value) {
    if (!visibleValues.has(String(v).toLowerCase())) {
      newSet.add(String(v).toLowerCase());
    }
  }
  for (const i of filteredItems.value) {
    const key = String(i.value).toLowerCase();
    if (!currentSet.has(key)) {
      newSet.add(key);
    }
  }

  tempSelection.value = normalizedItems.value
    .filter((i) => newSet.has(String(i.value).toLowerCase()))
    .map((i) => i.value);
}

function clearTemp() {
  tempSelection.value = [];
}

function applySelection() {
  emit("update:modelValue", [...tempSelection.value]);
  dialogVisible.value = false;
}

function clearSelection() {
  if (props.disabled) return;
  emit("update:modelValue", []);
}
</script>

<style scoped>
.multi-select-dialog-field {
  width: 100%;
}

/* 触发器输入框样式：继承 Vuetify 统一规范 */
.multi-select-custom-field {
  cursor: pointer;
}

.multi-select-custom-field :deep(.v-field) {
  cursor: pointer !important;
}

.multi-select-custom-field :deep(input) {
  cursor: pointer !important;
}

.multi-select-custom-field.has-selected :deep(input) {
  display: none !important;
  width: 0 !important;
  opacity: 0 !important;
  position: absolute !important;
  pointer-events: none !important;
}

.multi-select-custom-field :deep(.v-field__field) {
  overflow: hidden !important;
}

.multi-select-custom-field :deep(.v-field__input) {
  overflow: hidden !important;
  white-space: nowrap !important;
  flex-wrap: nowrap !important;
  padding-top: 0 !important;
  padding-bottom: 0 !important;
  display: flex !important;
  align-items: center !important;
}

.trigger-chips-wrapper {
  display: flex;
  align-items: center;
  flex-wrap: nowrap !important;
  overflow: hidden;
  white-space: nowrap;
  pointer-events: none;
  line-height: 1;
  max-width: calc(100% - 68px);
  gap: 4px;
}

.trigger-chip {
  max-width: 72px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.remaining-chip {
  opacity: 0.85;
  flex-shrink: 0;
}

.trigger-append-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-right: 2px;
}

.echo-clear-trigger {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  border: none;
  background-color: transparent;
  color: rgba(var(--v-theme-on-surface), 0.36);
  cursor: pointer;
  padding: 0;
  transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
  outline: none;
}

.echo-clear-trigger:hover {
  color: rgb(var(--v-theme-error));
  background-color: rgba(var(--v-theme-error), 0.12);
  transform: scale(1.15);
}

.echo-clear-trigger:active {
  transform: scale(0.92);
}

.echo-count-pill {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 22px;
  padding: 0 8px;
  border-radius: 11px;
  background-color: rgba(var(--v-theme-primary), 0.08);
  border: 1px solid rgba(var(--v-theme-primary), 0.18);
  color: rgb(var(--v-theme-primary));
  font-size: 0.72rem;
  font-weight: 600;
  letter-spacing: -0.01em;
  white-space: nowrap;
  user-select: none;
  line-height: 1;
}

.echo-chevron-icon {
  color: rgba(var(--v-theme-on-surface), 0.35);
  transition: transform 0.22s cubic-bezier(0.4, 0, 0.2, 1), color 0.2s ease;
}

.echo-chevron-icon--open {
  transform: rotate(180deg);
  color: rgb(var(--v-theme-primary));
}

/* 弹窗头部 Header 美化 */
.dialog-header {
  background: linear-gradient(180deg, rgba(var(--v-theme-primary), 0.05) 0%, rgba(var(--v-theme-surface), 0.8) 100%);
  min-height: 56px;
}

.dialog-header-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border-radius: 8px;
  background: rgba(var(--v-theme-primary), 0.1);
  box-shadow: 0 1px 3px rgba(var(--v-theme-primary), 0.1);
  flex-shrink: 0;
}

.dialog-count-badge {
  border-radius: 12px !important;
}

.dialog-close-btn {
  display: inline-flex !important;
  align-items: center !important;
  justify-content: center !important;
  width: 30px !important;
  height: 30px !important;
  min-width: 30px !important;
  border-radius: 50% !important;
  color: rgba(var(--v-theme-on-surface), 0.55) !important;
  transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1) !important;
}

.dialog-close-btn:hover {
  color: rgba(var(--v-theme-on-surface), 0.95) !important;
  background-color: rgba(var(--v-theme-on-surface), 0.08) !important;
  transform: rotate(90deg);
}

.dialog-close-btn:active {
  transform: rotate(90deg) scale(0.92);
}

/* 搜索框与操作工具栏 */
.compact-search-input :deep(.v-field) {
  height: 32px !important;
  min-height: 32px !important;
  font-size: 0.8rem;
  background-color: rgba(var(--v-theme-on-surface), 0.05) !important;
}

.compact-search-input :deep(.v-field__input) {
  min-height: 32px !important;
  padding-top: 0 !important;
  padding-bottom: 0 !important;
}

.compact-search-input :deep(.v-field__prepend-inner) {
  padding-top: 0 !important;
  align-items: center;
}

.compact-search-input :deep(.v-field__append-inner) {
  padding-top: 0 !important;
  align-items: center;
}

.action-btn {
  height: 28px !important;
  min-height: 28px !important;
}

.action-btn--clear {
  color: rgb(var(--v-theme-error)) !important;
  background-color: rgba(var(--v-theme-error), 0.1) !important;
}

.action-btn--clear:hover {
  color: rgb(var(--v-theme-error)) !important;
  background-color: rgba(var(--v-theme-error), 0.18) !important;
}

/* 选项双列网格布局：紧凑饱满、告别单列空洞 */
.options-grid-container {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
  max-height: 380px;
  overflow-y: auto;
  padding: 4px 2px 8px 2px;
}

@media (max-width: 520px) {
  .options-grid-container {
    grid-template-columns: 1fr;
  }
}

.option-card {
  border: 1px solid rgba(var(--v-border-color), 0.16);
  background-color: rgb(var(--v-theme-surface));
  min-height: 42px;
  box-sizing: border-box;
  transition: all 0.18s cubic-bezier(0.4, 0, 0.2, 1);
  user-select: none;
}

.option-card:hover {
  border-color: rgba(var(--v-theme-primary), 0.45);
  background-color: rgba(var(--v-theme-primary), 0.03);
  transform: translateY(-1px);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
}

.option-card.is-selected {
  border-color: rgba(var(--v-theme-primary), 0.75);
  background-color: rgba(var(--v-theme-primary), 0.07);
}

.option-checkbox {
  transition: transform 0.15s ease;
}

.option-card:active .option-checkbox {
  transform: scale(0.9);
}

.custom-tag {
  font-size: 0.65rem !important;
  height: 18px !important;
}

.custom-del-btn {
  opacity: 0.6;
  transition: opacity 0.2s ease;
}

.custom-del-btn:hover {
  opacity: 1;
}
</style>
