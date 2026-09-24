<template>
  <v-dialog v-model="visible" max-width="460" :persistent="persistent">
    <v-card rounded="xl" class="confirm-dialog-card">
      <!-- 统一顶部标题栏 -->
      <v-card-title class="dialog-header d-flex align-center justify-space-between px-5 pt-4 pb-2">
        <div class="d-flex align-center ga-2 min-w-0">
          <v-icon v-if="icon" :icon="icon" :color="iconColor" size="20" class="flex-shrink-0" />
          <span class="text-subtitle-1 font-weight-bold text-truncate">{{ title }}</span>
        </div>
        <v-btn
          v-if="!persistent"
          icon="mdi-close"
          variant="text"
          size="small"
          density="comfortable"
          :disabled="loading"
          @click="visible = false" />
      </v-card-title>

      <!-- 主体内容 -->
      <v-card-text class="px-5 py-3">
        <div v-if="body" class="text-body-2 mb-3 text-medium-emphasis">{{ body }}</div>
        <slot />

        <!-- 选项列表（支持带描述、全选和自定义高亮色） -->
        <div v-if="options.length" class="d-flex flex-column ga-2 mt-3 mb-2">
          <div
            v-for="opt in options"
            :key="opt.key"
            class="dialog-option-item"
            :class="isOptionActive(opt.key) ? `dialog-option-item--active-${opt.color || 'primary'}` : ''"
            @click="toggleOption(opt.key)">
            <v-icon
              :icon="isOptionActive(opt.key) ? 'mdi-checkbox-marked' : 'mdi-checkbox-blank-outline'"
              :color="isOptionActive(opt.key) ? (opt.color || 'primary') : 'default'"
              size="20"
              class="flex-shrink-0" />
            <div class="ml-3 flex-grow-1 min-w-0">
              <div class="text-body-2 font-weight-medium">{{ opt.label }}</div>
              <div v-if="opt.desc" class="text-caption text-medium-emphasis mt-1">{{ opt.desc }}</div>
            </div>
          </div>
        </div>

        <!-- 提示信息 -->
        <v-alert
          v-if="alertText"
          :type="alertType"
          variant="tonal"
          density="compact"
          class="mt-3 text-caption rounded-lg">
          {{ alertText }}
        </v-alert>
        <v-alert
          v-if="errorText"
          type="error"
          variant="tonal"
          density="compact"
          class="mt-3 text-caption rounded-lg">
          {{ errorText }}
        </v-alert>
      </v-card-text>

      <v-divider />

      <!-- 统一底部操作栏 -->
      <v-card-actions class="px-5 py-3 d-flex align-center">
        <v-btn
          v-if="showSelectAll && options.length"
          variant="text"
          size="small"
          color="primary"
          :disabled="loading"
          @click="toggleSelectAll">
          {{ allSelected ? "取消全选" : "全选" }}
        </v-btn>
        <v-spacer />
        <v-btn variant="text" size="small" :disabled="loading" @click="visible = false">取消</v-btn>
        <v-btn
          :color="confirmColor"
          variant="flat"
          size="small"
          rounded="pill"
          class="px-4 font-weight-medium"
          :loading="loading"
          :disabled="isConfirmDisabled"
          @click="handleConfirm">
          {{ displayConfirmText }}
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup>
import {computed, ref, watch} from "vue";

const props = defineProps({
  modelValue: Boolean,
  /** 弹窗标题 */
  title: {type: String, default: "确认"},
  /** 左侧图标 */
  icon: {type: String, default: ""},
  /** 图标颜色 */
  iconColor: {type: String, default: "primary"},
  /** 正文文字（也可用默认插槽覆盖） */
  body: {type: String, default: ""},
  /**
   * 选项列表
   * [{ key: string, label: string, desc?: string, color?: string }]
   */
  options: {type: Array, default: () => []},
  /** 默认选中的 keys 数组 */
  defaultSelected: {type: Array, default: () => []},
  /** 是否显示全选/取消全选按钮 */
  showSelectAll: {type: Boolean, default: false},
  /** 是否在确认按钮上显示选中项数量 */
  showSelectedCount: {type: Boolean, default: false},
  /** 内嵌 alert 文字 */
  alertText: {type: String, default: ""},
  /** alert 类型 */
  alertType: {type: String, default: "info"},
  /** 错误提示文字 */
  errorText: {type: String, default: ""},
  /** 确认按钮文字 */
  confirmText: {type: String, default: "确认"},
  /** 确认按钮颜色 */
  confirmColor: {type: String, default: "primary"},
  /** 强制禁用确认按钮 */
  confirmDisabled: {type: Boolean, default: false},
  /** 是否 loading */
  loading: {type: Boolean, default: false},
  /** persistent（点击外部不关闭） */
  persistent: {type: Boolean, default: false},
});

const emit = defineEmits(["update:modelValue", "confirm"]);

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});

/** 当前选中的 key 列表 */
const selectedKeys = ref([]);

function isOptionActive(key) {
  return selectedKeys.value.includes(key);
}

function toggleOption(key) {
  if (selectedKeys.value.includes(key)) {
    selectedKeys.value = selectedKeys.value.filter((k) => k !== key);
  } else {
    selectedKeys.value = [...selectedKeys.value, key];
  }
}

const allSelected = computed(() => {
  return props.options.length > 0 && selectedKeys.value.length === props.options.length;
});

function toggleSelectAll() {
  if (allSelected.value) {
    selectedKeys.value = [];
  } else {
    selectedKeys.value = props.options.map((opt) => opt.key);
  }
}

/** 弹窗打开时重置选项 */
watch(
  () => props.modelValue,
  (val) => {
    if (val) {
      selectedKeys.value = [...(props.defaultSelected || [])];
    }
  },
  {immediate: true},
);

const isConfirmDisabled = computed(() => {
  if (props.confirmDisabled) return true;
  if (props.showSelectAll && props.options.length && !selectedKeys.value.length) return true;
  return false;
});

const displayConfirmText = computed(() => {
  if (props.showSelectedCount && selectedKeys.value.length > 0) {
    return `${props.confirmText} (${selectedKeys.value.length})`;
  }
  return props.confirmText;
});

function handleConfirm() {
  if (props.options.length) {
    const map = {};
    for (const opt of props.options) {
      map[opt.key] = selectedKeys.value.includes(opt.key);
    }
    // 同时派发 map 字典和选中的 keys 数组，完美适配两类接收方式
    emit("confirm", map, [...selectedKeys.value]);
  } else {
    emit("confirm");
  }
}
</script>

<style scoped>
.confirm-dialog-card {
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.16) !important;
}

.dialog-option-item {
  display: flex;
  align-items: center;
  padding: 10px 14px;
  border-radius: 12px;
  border: 1px solid rgba(var(--v-border-color), 0.12);
  background: transparent;
  cursor: pointer;
  transition: all 0.15s ease;
  user-select: none;
}

.dialog-option-item:hover {
  background: rgba(var(--v-theme-primary), 0.04);
  border-color: rgba(var(--v-theme-primary), 0.2);
}

.dialog-option-item--active-primary {
  background: rgba(var(--v-theme-primary), 0.08);
  border-color: rgba(var(--v-theme-primary), 0.35);
}

.dialog-option-item--active-error {
  background: rgba(var(--v-theme-error), 0.06);
  border-color: rgba(var(--v-theme-error), 0.35);
}

.dialog-option-item--active-warning {
  background: rgba(var(--v-theme-warning), 0.06);
  border-color: rgba(var(--v-theme-warning), 0.35);
}
</style>
