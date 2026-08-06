<template>
  <v-dialog v-model="visible" max-width="560">
    <v-card class="directory-card">
      <v-card-title class="directory-title px-3 py-2 bg-primary-lighten-5">
        <v-icon icon="mdi-folder-network" color="primary" class="mr-2"/>
        <span>选择网盘转存路径</span>
      </v-card-title>
      <v-card-text class="px-3 py-2">
        <div v-if="loading" class="directory-loading">
          <v-progress-circular indeterminate color="primary"/>
        </div>
        <div v-else>
          <v-text-field
              v-model="currentPath"
              label="当前路径"
              variant="outlined"
              density="compact"
              class="mb-2"
              hide-details
              @keyup.enter="loadDirectories(currentPath)"
          />
          <v-btn
              prepend-icon="mdi-folder-plus"
              variant="tonal"
              size="small"
              class="mb-2"
              :disabled="loading"
              @click="createDirectory"
          >新建文件夹
          </v-btn>
          <v-list class="directory-list border rounded">
            <v-list-item
                v-if="currentPath !== '/'"
                class="py-1"
                @click="loadDirectories(parentPath)"
            >
              <template #prepend>
                <v-icon icon="mdi-arrow-up" size="small" class="mr-2" color="grey"/>
              </template>
              <v-list-item-title class="text-body-2">上级目录</v-list-item-title>
              <v-list-item-subtitle>..</v-list-item-subtitle>
            </v-list-item>

            <v-list-item
                v-for="directory in directories"
                :key="directory.id || directory.path"
                class="py-1"
                @click="loadDirectories(directory.path)"
            >
              <template #prepend>
                <v-icon
                    icon="mdi-folder"
                    size="small"
                    class="mr-2"
                    color="amber-darken-2"
                />
              </template>
              <v-list-item-title class="text-body-2">{{ directory.name }}</v-list-item-title>
            </v-list-item>

            <v-list-item v-if="!directories.length" class="py-2 text-center">
              <v-list-item-title class="text-body-2 text-grey">
                该目录为空或没有子文件夹
              </v-list-item-title>
            </v-list-item>
          </v-list>
        </div>
        <v-alert
            v-if="errorMessage"
            type="error"
            density="compact"
            variant="tonal"
            class="mt-2 text-caption"
        >
          {{ errorMessage }}
        </v-alert>
      </v-card-text>
      <v-card-actions class="px-3 py-2">
        <v-spacer/>
        <v-btn
            color="primary"
            variant="text"
            size="small"
            :disabled="!currentPath || loading"
            @click="selectDirectory"
        >选择当前目录
        </v-btn
        >
        <v-btn
            color="grey"
            variant="text"
            size="small"
            @click="visible = false"
        >取消
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup>
import {computed, ref, watch} from "vue";

const props = defineProps({
  modelValue: {type: Boolean, default: false},
  api: {type: [Object, Function], required: true},
  provider: {type: String, default: ""},
  initialPath: {type: String, default: "/"},
  pluginId: {type: String, default: "CloudSubscribe"},
});
const emit = defineEmits(["update:modelValue", "select"]);
const currentPath = ref("/");
const directories = ref([]);
const loading = ref(false);
const errorMessage = ref("");
const lastRequestedPath = ref("");

const visible = computed({
  get: () => props.modelValue,
  set: (value) => emit("update:modelValue", value),
});
const parentPath = computed(() => {
  const parts = currentPath.value.split("/").filter(Boolean);
  parts.pop();
  return parts.length ? `/${parts.join("/")}` : "/";
});

function unwrap(raw) {
  if (raw?.data && typeof raw.data === "object" && "success" in raw.data)
    return raw.data;
  return raw || {};
}

async function loadDirectories(path) {
  if (loading.value) return;
  loading.value = true;
  errorMessage.value = "";
  try {
    const normalized = String(path || "/").trim() || "/";
    if (lastRequestedPath.value === normalized) return;
    lastRequestedPath.value = normalized;
    const query = new URLSearchParams({
      path: normalized,
      provider: props.provider || "",
    });
    const response = unwrap(
        await props.api.get(
            `plugin/${props.pluginId}/cloud/directories?${query}`,
        ),
    );
    if (response.success === false)
      throw new Error(response.message || "读取目录失败");
    const data = response.data?.data || response.data || response;
    currentPath.value = data.path || normalized;
    directories.value = Array.isArray(data.directories)
        ? [...data.directories].sort((left, right) =>
            String(left.name || "").localeCompare(String(right.name || ""), undefined, {
              numeric: true,
              sensitivity: "base",
            }),
        )
        : [];
  } catch (error) {
    directories.value = [];
    errorMessage.value = error.message || String(error);
    lastRequestedPath.value = "";
  } finally {
    loading.value = false;
  }
}

async function createDirectory() {
  const name = window.prompt("请输入文件夹名称", "");
  if (name === null) return;
  const folderName = name.trim();
  if (!folderName) {
    errorMessage.value = "文件夹名称不能为空";
    return;
  }
  loading.value = true;
  errorMessage.value = "";
  try {
    const response = unwrap(await props.api.post(
        `plugin/${props.pluginId}/cloud/directories/create`,
        {path: currentPath.value || "/", name: folderName, provider: props.provider || ""},
    ));
    if (response.success === false)
      throw new Error(response.message || "创建文件夹失败");
    loading.value = false;
    await loadDirectories(
        response.data?.path || `${currentPath.value.replace(/\/$/, "")}/${folderName}`,
    );
  } catch (error) {
    errorMessage.value = error.message || String(error);
    loading.value = false;
  }
}

function selectDirectory() {
  emit("select", currentPath.value || "/");
}

watch(
    () => props.modelValue,
    (value) => {
      if (value) {
        currentPath.value = String(props.initialPath || "/").trim() || "/";
        loadDirectories(currentPath.value);
      }
    },
    {immediate: true},
);
</script>

<style scoped>
.directory-title {
  display: flex;
  align-items: center;
}

.directory-card {
  min-height: min(560px, 72vh);
}

.directory-list {
  height: min(390px, calc(72vh - 168px));
  overflow-y: auto;
}

.directory-loading {
  display: flex;
  min-height: min(390px, calc(72vh - 168px));
  align-items: center;
  justify-content: center;
}
</style>
