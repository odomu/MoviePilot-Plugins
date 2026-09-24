<template>
  <v-dialog v-model="visible" :max-width="providerItems.length > 1 ? 760 : 560">
    <v-card class="directory-card rounded-xl overflow-hidden">
      <!-- 美化后的弹窗顶部 Header -->
      <div class="dialog-header d-flex align-center px-4 py-3 border-b">
        <div class="dialog-header-icon mr-2">
          <v-icon :icon="isLocalMode ? 'mdi-folder-cog-outline' : 'mdi-folder-network'" color="primary" size="20" />
        </div>
        <div class="flex-grow-1 overflow-hidden">
          <div class="text-subtitle-1 font-weight-bold text-truncate">{{ dialogTitle }}</div>
          <div class="text-caption text-medium-emphasis text-truncate">
            {{ isLocalMode ? "支持浏览本地存储并可直接新建子文件夹" : "支持浏览当前网盘转存目录并可新建文件夹" }}
          </div>
        </div>
        <v-btn
          icon="mdi-close"
          variant="text"
          size="small"
          class="dialog-close-btn ml-1 flex-shrink-0"
          title="关闭"
          @click="visible = false" />
      </div>

      <v-card-text class="px-4 py-3">
        <div class="directory-browser" :class="{ 'has-providers': providerItems.length > 1 }">
          <v-list v-if="providerItems.length > 1" nav density="compact" class="provider-list border rounded-lg">
            <v-list-subheader class="text-caption font-weight-bold">网盘服务商</v-list-subheader>
            <v-list-item
              v-for="item in providerItems"
              :key="item.value"
              :active="selectedProvider === item.value"
              color="primary"
              rounded="md"
              :disabled="loading"
              @click="selectProvider(item.value)">
              <template #prepend>
                <v-icon icon="mdi-cloud-outline" size="small" />
              </template>
              <v-list-item-title>{{ item.title }}</v-list-item-title>
              <v-list-item-subtitle>
                {{ item.value === (targetProvider || provider) ? "目标网盘" : "跨盘转存" }}
              </v-list-item-subtitle>
            </v-list-item>
          </v-list>
          <div class="directory-pane">
            <v-text-field
              v-model="currentPath"
              label="当前路径"
              variant="outlined"
              density="compact"
              class="mb-2"
              hide-details
              :disabled="loading"
              @keyup.enter="loadDirectories(currentPath)">
              <template #append-inner>
                <div class="d-flex align-center ga-1 mr-n1">
                  <v-btn
                    v-if="allowCreate"
                    icon="mdi-folder-plus-outline"
                    variant="text"
                    size="small"
                    density="comfortable"
                    color="primary"
                    title="新建文件夹"
                    :disabled="loading || createLoading"
                    @click.stop="openCreateDirectoryDialog" />
                  <v-btn
                    icon="mdi-refresh"
                    variant="text"
                    size="small"
                    density="comfortable"
                    color="medium-emphasis"
                    title="刷新"
                    :loading="loading"
                    :disabled="loading || createLoading"
                    @click.stop="refreshDirectories" />
                </div>
              </template>
            </v-text-field>
            <div v-if="loading && !treeRoot.loaded" class="directory-loading">
              <v-progress-circular indeterminate color="primary" />
            </div>
            <div v-else class="directory-list border rounded-lg">
              <DirectoryTreeNode
                :node="treeRoot"
                :selected-path="currentPath"
                :disabled="loading || createLoading"
                @select="selectTreeNode"
                @toggle="toggleTreeNode" />
            </div>
          </div>
        </div>
        <v-alert v-if="errorMessage" type="error" density="compact" variant="tonal" class="mt-2 text-caption">
          {{ errorMessage }}
        </v-alert>
      </v-card-text>
      <v-card-actions class="px-4 py-3 border-t bg-surface-variant-opacity">
        <span class="text-caption text-medium-emphasis text-truncate mr-2">
          选中：{{ currentPath }}
        </span>
        <v-spacer />
        <v-btn color="grey" variant="text" size="small" @click="visible = false">取消</v-btn>
        <v-btn color="primary" variant="flat" size="small" :disabled="!currentPath || loading" @click="selectDirectory">
          选择此目录
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>

  <v-dialog v-model="createDirectoryVisible" max-width="420" persistent>
    <v-card rounded="xl">
      <div class="dialog-header d-flex align-center px-4 py-3 border-b">
        <div class="dialog-header-icon mr-2">
          <v-icon icon="mdi-folder-plus-outline" color="primary" size="18" />
        </div>
        <div class="text-subtitle-1 font-weight-bold">新建文件夹</div>
      </div>
      <v-card-text class="pt-4">
        <v-text-field
          v-model="newDirectoryName"
          label="文件夹名称"
          placeholder="请输入文件夹名称"
          variant="outlined"
          density="compact"
          autofocus
          :disabled="createLoading"
          :error-messages="createDirectoryError"
          @keyup.enter="createDirectory" />
      </v-card-text>
      <v-card-actions class="px-4 pb-3">
        <v-spacer />
        <v-btn variant="text" :disabled="createLoading" @click="closeCreateDirectoryDialog">取消</v-btn>
        <v-btn color="primary" variant="flat" :loading="createLoading" :disabled="!newDirectoryName.trim()"
               @click="createDirectory">
          创建
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup>
import {computed, ref, watch} from "vue";
import DirectoryTreeNode from "./DirectoryTreeNode.vue";

const props = defineProps({
  modelValue: {type: Boolean, default: false},
  api: {type: [Object, Function], required: true},
  mode: {type: String, default: "cloud"}, // "cloud" 或 "local"
  provider: {type: String, default: ""},
  targetProvider: {type: String, default: ""},
  initialPath: {type: String, default: "/"},
  pluginId: {type: String, default: "CloudSubscribe"},
  title: {type: String, default: ""},
  allowCreate: {type: Boolean, default: true},
  providers: {type: Array, default: () => []},
})
const emit = defineEmits(["update:modelValue", "select"]);
const isLocalMode = computed(() => props.mode === "local");
const dialogTitle = computed(() => {
  if (props.title) return props.title;
  return isLocalMode.value ? "选择本地存储目录" : "选择网盘转存路径";
});
const currentPath = ref("/");
const selectedProvider = ref("");
const loading = ref(false);
const errorMessage = ref("");
const createDirectoryVisible = ref(false);
const newDirectoryName = ref("");
const createDirectoryError = ref("");
const createLoading = ref(false);

function directoryNode(source = {}, fallbackPath = "/") {
  const rootLabel = isLocalMode.value ? "本地根目录" : "网盘根目录";
  return {
    id: String(source.id || source.path || fallbackPath),
    name: String(source.name || (fallbackPath === "/" ? rootLabel : fallbackPath.split("/").pop())),
    path: String(source.path || fallbackPath),
    children: [],
    expanded: fallbackPath === "/",
    loaded: false,
    loading: false,
  }
}

const treeRoot = ref(directoryNode({}, "/"));

const visible = computed({
  get: () => props.modelValue,
  set: (value) => emit("update:modelValue", value),
})
const providerItems = computed(() => {
  if (isLocalMode.value) return [];
  if (Array.isArray(props.providers) && props.providers.length) return props.providers;
  return props.provider ? [{title: props.provider, value: props.provider}] : [];
})

function unwrap(raw) {
  if (raw?.data && typeof raw.data === "object" && "success" in raw.data) return raw.data;
  return raw || {};
}

function normalizePath(path) {
  const parts = String(path || "/")
    .replace(/\\/g, "/")
    .split("/")
    .filter(Boolean);
  return parts.length ? `/${parts.join("/")}` : "/";
}

function findTreeNode(path, node = treeRoot.value) {
  const normalized = normalizePath(path);
  if (node.path === normalized) return node;
  for (const child of node.children) {
    const matched = findTreeNode(normalized, child);
    if (matched) return matched;
  }
  return null;
}

function ensureTreePath(path) {
  const normalized = normalizePath(path);
  let node = treeRoot.value;
  let current = "";
  for (const part of normalized.split("/").filter(Boolean)) {
    current = `${current}/${part}`;
    let child = node.children.find((item) => item.path === current);
    if (!child) {
      child = directoryNode({name: part, path: current}, current);
      node.children.push(child);
    }
    node.expanded = true;
    node = child;
  }
  return node;
}

async function loadTreeNode(node, force = false) {
  if (!node || node.loading || (!force && node.loaded)) return;
  loading.value = true;
  node.loading = true;
  errorMessage.value = "";
  try {
    const query = new URLSearchParams({
      path: node.path,
    })
    if (!isLocalMode.value) {
      query.set("provider", selectedProvider.value || props.provider || "");
    }
    if (force) query.set("refresh", "true");
    const endpoint = isLocalMode.value ? "local/directories" : "cloud/directories";
    const response = unwrap(await props.api.get(`plugin/${props.pluginId}/${endpoint}?${query}`));
    if (response.success === false) throw new Error(response.message || "读取目录失败");
    const data = response.data?.data || response.data || response;
    const previous = new Map(node.children.map((item) => [item.path, item]));
    node.children = (Array.isArray(data.directories) ? data.directories : [])
      .sort((left, right) =>
        String(left.name || "").localeCompare(String(right.name || ""), undefined, {
          numeric: true,
          sensitivity: "base",
        }),
      )
      .map((item) => previous.get(item.path) || directoryNode(item, item.path))
    node.loaded = true;
  } catch (error) {
    errorMessage.value = error.message || String(error);
  } finally {
    node.loading = false;
    loading.value = false;
  }
}

async function loadDirectories(path, force = false) {
  const normalized = normalizePath(path);
  if (!treeRoot.value.loaded) await loadTreeNode(treeRoot.value);
  const node = findTreeNode(normalized) || ensureTreePath(normalized);
  currentPath.value = normalized;
  node.expanded = true;
  await loadTreeNode(node, force);
}

function refreshDirectories() {
  const node = findTreeNode(currentPath.value) || treeRoot.value;
  loadTreeNode(node, true);
}

function selectTreeNode(node) {
  currentPath.value = normalizePath(node?.path);
}

async function toggleTreeNode(node) {
  if (!node || node.loading) return;
  node.expanded = !node.expanded;
  if (node.expanded) await loadTreeNode(node);
}

function selectProvider(provider) {
  const value = String(provider || "").trim();
  if (!value || value === selectedProvider.value || loading.value) return;
  selectedProvider.value = value;
  currentPath.value = "/";
  treeRoot.value = directoryNode({}, "/");
  loadDirectories("/");
}

function openCreateDirectoryDialog() {
  newDirectoryName.value = "";
  createDirectoryError.value = "";
  createDirectoryVisible.value = true;
}

function closeCreateDirectoryDialog() {
  if (createLoading.value) return;
  createDirectoryVisible.value = false;
}

async function createDirectory() {
  if (createLoading.value) return;
  const folderName = newDirectoryName.value.trim();
  if (!folderName) {
    createDirectoryError.value = "文件夹名称不能为空";
    return;
  }
  createLoading.value = true;
  createDirectoryError.value = "";
  const directoryPath = currentPath.value || "/";
  try {
    const endpoint = isLocalMode.value ? "local/directories/create" : "cloud/directories/create";
    const payload = {
      path: directoryPath,
      name: folderName,
    };
    if (!isLocalMode.value) {
      payload.provider = selectedProvider.value || props.provider || "";
    }
    const response = unwrap(
      await props.api.post(`plugin/${props.pluginId}/${endpoint}`, payload),
    )
    if (response.success === false) throw new Error(response.message || "创建文件夹失败");
    createDirectoryVisible.value = false;
    newDirectoryName.value = "";
    await loadDirectories(directoryPath, true);
  } catch (error) {
    createDirectoryError.value = error.message || String(error);
  } finally {
    createLoading.value = false;
  }
}

function selectDirectory() {
  emit("select", currentPath.value || "/", selectedProvider.value || props.provider || "");
}

watch(
  () => props.modelValue,
  (value) => {
    if (value) {
      selectedProvider.value = String(props.provider || providerItems.value[0]?.value || "").trim();
      currentPath.value = String(props.initialPath || "/").trim() || "/";
      treeRoot.value = directoryNode({}, "/");
      loadDirectories(currentPath.value);
    } else {
      createDirectoryVisible.value = false;
      newDirectoryName.value = "";
      createDirectoryError.value = "";
    }
  },
  {immediate: true},
)
</script>

<style scoped>
/* 美化弹窗顶部 Header */
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

.dialog-close-btn {
  display: inline-flex !important;
  align-items: center !important;
  justify-content: center !important;
  width: 30px !important;
  height: 30px !important;
  min-width: 30px !important;
  border-radius: 50% !important;
  color: rgba(var(--v-theme-on-surface), 0.55) !important;
  transition: all 0.2s ease !important;
}

.dialog-close-btn:hover {
  color: rgba(var(--v-theme-on-surface), 0.95) !important;
  background-color: rgba(var(--v-theme-on-surface), 0.08) !important;
  transform: rotate(90deg);
}

.directory-browser {
  display: flex;
  gap: 12px;
  min-height: 340px;
}

.directory-pane {
  flex: 1 1 auto;
  min-width: 0;
}

.provider-list {
  width: 140px;
  flex: 0 0 140px;
  overflow-y: auto;
}

.directory-list {
  max-height: 300px;
  overflow-y: auto;
  padding: 6px 4px;
}

.directory-loading {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 200px;
}
</style>
