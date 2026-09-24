<template>
  <div class="cloud-app-page">
    <header class="app-header">
      <div class="app-identity">
        <v-avatar color="primary" variant="tonal" size="42">
          <v-icon icon="mdi-cloud-sync-outline" size="23" />
        </v-avatar>
        <div>
          <h1>网盘订阅助手</h1>
          <p>{{ runtimeSummary }}</p>
        </div>
      </div>
      <div class="app-actions">
        <v-btn
          class="manual-action"
          variant="tonal"
          color="secondary"
          prepend-icon="mdi-link-variant-plus"
          title="手动添加"
          @click="openManualDialog()">
          <span class="action-label">手动添加</span>
        </v-btn>
        <v-btn
          v-if="offlineSupported"
          class="offline-action"
          variant="tonal"
          color="info"
          prepend-icon="mdi-cloud-download-outline"
          title="查看离线任务"
          @click="offlineVisible = true">
          <span class="action-label">离线任务</span>
        </v-btn>
        <v-btn
          class="cache-action"
          variant="tonal"
          color="warning"
          prepend-icon="mdi-cached"
          title="选择并清理缓存"
          @click="cacheVisible = true">
          <span class="action-label">清理缓存</span>
        </v-btn>
        <v-btn
          :class="['sync-action', { 'sync-action-active': active }]"
          :color="active ? 'warning' : 'primary'"
          variant="flat"
          :prepend-icon="active ? 'mdi-stop-circle-outline' : 'mdi-magnify'"
          :disabled="runtime.status === 'stopping'"
          :loading="runtime.status === 'starting' || searchStarting"
          :title="active ? '停止全部任务' : immediateSearchTitle"
          @click="active ? confirmStopSync() : openImmediateSearchConfirm()">
          <span class="action-label">{{ active ? "停止全部" : immediateSearchLabel }}</span>
        </v-btn>
        <v-btn
          class="settings-action"
          prepend-icon="mdi-cog-outline"
          variant="tonal"
          color="success"
          :loading="configLoading"
          title="配置"
          @click="openConfig">
          <span class="action-label">设置</span>
        </v-btn>
      </div>
    </header>

    <section class="overview-band" aria-label="转存概览">
      <div class="overview-title">
        <v-icon icon="mdi-chart-box-outline" color="primary" size="small" />
        <span>转存概览</span>
      </div>
      <div class="overview-metrics">
        <div
          v-for="stat in stats"
          :key="stat.title"
          :class="['overview-metric', { 'overview-metric--desktop-only': ['成功', '失败'].includes(stat.title) }]">
          <v-icon :icon="stat.icon" :color="stat.color" size="20" />
          <div>
            <span>{{ stat.title }}</span>
            <strong :class="`text-${stat.color}`">{{ stat.value }}</strong>
          </div>
        </div>
      </div>
    </section>

    <section :class="['workspace', { 'workspace-history': mainTab === 'history' }]">
      <div class="workspace-nav">
        <v-tabs v-model="mainTab" color="primary" class="workspace-tabs">
          <v-tab value="tasks">
            <v-icon icon="mdi-format-list-checks" size="small" class="mr-2" />
            订阅任务
            <v-chip v-if="activeTaskCount" size="x-small" class="ml-2">
              {{ activeTaskCount }}
            </v-chip>
          </v-tab>
          <v-tab value="history">
            <v-icon icon="mdi-history" size="small" class="mr-2" />
            历史记录
            <v-chip v-if="historyStats.total" size="x-small" class="ml-2">
              {{ historyStats.total }}
            </v-chip>
          </v-tab>
        </v-tabs>
      </div>

      <div class="workspace-window">
        <div v-show="mainTab === 'tasks'" class="workspace-pane task-pane">
          <RuntimeCard class="runtime-panel" :runtime="runtime" :active="active" @stop-task="confirmStopTask" />
        </div>
        <div v-show="mainTab === 'history'" class="workspace-pane history-pane">
          <HistoryTable
            :items="historyGroups"
            :page="historyPage.page"
            :page-size="historyPage.pageSize"
            :total="historyPage.total"
            :total-pages="historyPage.totalPages"
            :filter-options="historyPage.filterOptions"
            :emby-play-items="embyPlayItems"
            :loading="loading"
            :retrying-key="retryingHistoryKey"
            :deleting-key="deletingHistoryKey"
            :notifying-key="notifyingHistoryKey"
            :upgrading-key="upgradingHistoryKey"
            :enable-cloud-upgrade="historyPage.enableCloudUpgrade"
            @refresh="loadPage"
            @query-change="updateHistoryQuery"
            @clear="openClearHistory"
            @retry="confirmRetryHistory"
            @delete="confirmDeleteHistory"
            @delete-groups="confirmDeleteGroups"
            @selection-change="updateHistorySelection"
            @notify="confirmNotifyHistory"
            @upgrade="handleHistoryUpgrade"
            @play="playHistory"
            @open-media="openMediaDetail" />
        </div>
      </div>
    </section>

    <OfflineTasksDialog v-if="offlineSupported" v-show="offlineVisible" v-model="offlineVisible" :api="api" />
    <ManualResourceDialog
      v-show="manualVisible"
      v-model="manualVisible"
      :api="api"
      :plugin-id="pluginId"
      :initial-mode="manualInitialMode"
      :initial-media="manualInitialMedia"
      @started="manualStarted" />
    <ConfirmDialog
      v-model="searchConfirmVisible"
      :title="immediateSearchDialogTitle"
      :loading="searchStarting"
      :confirm-text="selectedHistoryCount ? '搜索所选' : '搜索全部'"
      persistent
      @confirm="confirmImmediateSearch">
      <template v-if="selectedHistoryCount">
        确认立即搜索所选 {{ selectedHistoryCount }} 个历史媒体？
        <v-alert type="info" variant="tonal" density="compact" class="mt-3">
          本次仅搜索所选历史记录，不会搜索其他订阅。
        </v-alert>
      </template>
      <template v-else>确认立即搜索全部订阅？</template>
    </ConfirmDialog>
    <ConfirmDialog
      v-model="retryVisible"
      title="恢复历史任务"
      alert-text="将重新检查目标文件、本地缓存和原分享，必要时重新执行跨盘转存及后处理。"
      confirm-text="确认恢复"
      :loading="Boolean(retryingHistoryKey)"
      persistent
      @confirm="retryHistory">
      确认恢复“{{ historyUpgradeLabel(retryingRecord ? {records: [retryingRecord]} : null) }}”的转存任务？
    </ConfirmDialog>
    <ConfirmDialog
      v-model="upgradeVisible"
      title="确认历史洗版"
      alert-text="将继续使用该条历史记录作为现有版本基线，并按当前洗版设置搜索和比较候选资源。"
      alert-type="warning"
      confirm-text="确认洗版"
      confirm-color="warning"
      :loading="Boolean(upgradingHistoryKey)"
      persistent
      @confirm="confirmHistoryUpgrade">
      确认洗版“{{ historyUpgradeLabel(upgradingPayload) }}”？
    </ConfirmDialog>
    <ConfirmDialog
      v-if="stopVisible"
      v-model="stopVisible"
      :title="stoppingTask ? '停止任务' : '停止全部任务'"
      icon="mdi-stop-circle-outline"
      icon-color="warning"
      alert-type="warning"
      :alert-text="['downloading', 'transferring', 'postprocessing'].includes(stoppingTask?.status)
        ? '将停止插件文件后处理；离线任务、已下载文件和STRM均会保留。'
        : '任务将在安全节点停止，已完成的处理不会回退。'"
      confirm-text="确认停止"
      confirm-color="warning"
      :loading="stopping"
      persistent
      @confirm="stopConfirmed">
      <div v-if="stoppingTask">确认停止“{{ stoppingTask.title || "此任务" }}”？</div>
      <div v-else-if="stoppableTaskCount > 0">确认停止当前 {{ stoppableTaskCount }} 个等待或运行中的任务？</div>
      <div v-else>确认停止当前订阅任务？</div>
    </ConfirmDialog>
    <v-dialog v-model="configVisible" attach="body" max-width="62rem" :fullscreen="isMobile" class="config-dialog">
      <Config
        v-if="configVisible"
        :api="api"
        :initial-config="configData"
        :show-switch="false"
        @close="configVisible = false" />
    </v-dialog>

    <ConfirmDialog
      v-if="clearVisible"
      v-model="clearVisible"
      title="清空历史记录"
      icon="mdi-delete-sweep-outline"
      icon-color="error"
      body="确认清理本插件的转存历史？此操作不可逆。"
      :options="[
        { key: 'forceClear', label: '同时终止并清理正在处理的记录', color: 'error' },
        { key: 'clearPoints', label: '同时清空 HDHive/Dian115 已花费积分记录', color: 'warning' },
      ]"
      confirm-text="确认清空"
      confirm-color="error"
      :loading="clearing"
      @confirm="clearHistory" />

    <ConfirmDialog
      v-if="cacheVisible"
      v-model="cacheVisible"
      title="清理缓存"
      icon="mdi-cached"
      icon-color="primary"
      :options="CACHE_CATEGORIES"
      :default-selected="['search']"
      show-select-all
      show-selected-count
      confirm-text="确认清理"
      :loading="clearingCache"
      @confirm="clearCache" />

    <ConfirmDialog
      v-if="deleteVisible"
      v-model="deleteVisible"
      title="删除历史记录"
      icon="mdi-delete-outline"
      icon-color="error"
      :body="deletingGroupCount
        ? `确认删除所选 ${deletingGroupCount} 个汇总项中的 ${deletingRecords.length} 条转存历史？`
        : `确认删除“${deletingRecord?.file_name || '此记录'}”的转存历史？`"
      :options="[{ key: 'deleteLinked', label: '同时删除关联的网盘文件和STRM', color: 'error' }]"
      confirm-text="确认删除"
      confirm-color="error"
      :loading="Boolean(deletingHistoryKey)"
      @confirm="deleteHistory" />

    <ConfirmDialog
      v-model="notifyVisible"
      title="补发汇总通知"
      confirm-text="确认发送"
      :error-text="notifyError"
      :loading="Boolean(notifyingHistoryKey)"
      @confirm="notifyHistory">
      将为“{{ notifyingSummaryTitle || notifyingRecord?.file_name || "此记录" }}”重新发送入库通知和Webhook。
    </ConfirmDialog>

    <v-snackbar v-model="fallbackVisible" :color="fallbackType" location="top" timeout="3000">
      {{ fallbackMessage }}
    </v-snackbar>
  </div>
</template>

<script setup>
import {computed, defineAsyncComponent, onMounted, onUnmounted, ref, watch} from "vue";
import {useDisplay} from "vuetify";
import RuntimeCard from "./dashboard/RuntimeCard.vue";
import HistoryTable from "./dashboard/HistoryTable.vue";
import {useHistoryPageData} from "../composables/usePageData.js";
import {useRuntimeData} from "../composables/useRuntimeData.js";
import {CACHE_CATEGORIES, useCacheActions} from "../composables/useCacheActions.js";

const Config = defineAsyncComponent(() => import("./Config.vue"))
const ConfirmDialog = defineAsyncComponent(() => import("./dialogs/ConfirmDialog.vue"));
const ManualResourceDialog = defineAsyncComponent(() => import("./dialogs/ManualResourceDialog.vue"))
const OfflineTasksDialog = defineAsyncComponent(() => import("./dialogs/OfflineTasksDialog.vue"))

const props = defineProps({
  api: { type: Object, default: () => ({}) },
  pluginId: { type: String, default: "CloudSubscribe" },
  navKey: { type: String, default: "main" },
})

const api = props.api
const display = useDisplay()
const isMobile = computed(() => display.xs.value)
const fallbackVisible = ref(false)
const fallbackMessage = ref("")
const fallbackType = ref("success")
const mainTab = ref("tasks")
const offlineVisible = ref(false)
const manualVisible = ref(false)
const manualInitialMode = ref("links")
const manualInitialMedia = ref(null)
const stopVisible = ref(false)
const stoppingTask = ref(null)
const stopping = ref(false)
const configVisible = ref(false)
const configLoading = ref(false)
const configData = ref({})
const clearVisible = ref(false)
const clearing = ref(false)
const cacheVisible = ref(false)
const clearingCache = ref(false)
const retryingHistoryKey = ref("")
const retryVisible = ref(false)
const retryingRecord = ref(null)
const deleteVisible = ref(false)
const deletingRecord = ref(null)
const deletingRecords = ref([])
const deletingGroupCount = ref(0)
const deletingHistoryKey = ref("")
const notifyVisible = ref(false)
const notifyingRecord = ref(null)
const notifyingSummaryTitle = ref("")
const notifyingHistoryKey = ref("")
const upgradingHistoryKey = ref("")
const upgradeVisible = ref(false)
const upgradingPayload = ref(null)
const notifyError = ref("")
const searchConfirmVisible = ref(false)
const searchStarting = ref(false)
const historyDirty = ref(true)
const historySelection = ref({ groupCount: 0, subscribeIds: [], targets: [] })

function notify(text, type = "success") {
  const method = ["success", "info", "warning", "error"].includes(type) ? type : "success"
  fallbackMessage.value = text
  fallbackType.value = method
  fallbackVisible.value = true
}

const {
  historyGroups,
  historyPage,
  historyStats,
  embyPlayItems,
  loading,
  stats,
  loadPage,
  loadSummary,
  updateHistoryQuery,
  clearHistory: clearHistoryRequest,
  deleteHistory: deleteHistoryRequest,
  deleteHistoryBatch: deleteHistoryBatchRequest,
  notifyHistory: notifyHistoryRequest,
} = useHistoryPageData(api, notify, props.pluginId)
const { clearCache: clearCacheRequest } = useCacheActions(api, props.pluginId)
let historyRefreshTimer = null

function scheduleHistoryRefresh() {
  historyDirty.value = true
  if (historyRefreshTimer !== null) {
    window.clearTimeout(historyRefreshTimer)
  }
  historyRefreshTimer = window.setTimeout(async () => {
    historyRefreshTimer = null
    await loadSummary(false)
    if (mainTab.value === "history" && historyDirty.value) {
      historyDirty.value = false
      await loadPage(false)
    }
  }, 180)
}

const {
  offlineSupported,
  runtime,
  active,
  startSync,
  stopSync: stopSyncRequest,
  stopTask: stopTaskRequest,
  upgradeHistory: upgradeHistoryRequest,
} = useRuntimeData(api, notify, props.pluginId, {
  onSettled: scheduleHistoryRefresh,
  onHistoryChanged: scheduleHistoryRefresh,
})

onMounted(() => void loadSummary())
onUnmounted(() => {
  if (historyRefreshTimer !== null) window.clearTimeout(historyRefreshTimer)
})

watch(mainTab, (tab) => {
  if (tab !== "history" || !historyDirty.value) return
  historyDirty.value = false
  void loadPage()
})

const activeTaskCount = computed(
  () =>
    (runtime.tasks || []).filter((task) => ["queued", "running", "stopping", "downloading", "transferring", "postprocessing"].includes(task.status))
      .length,
)
const stoppableTaskCount = computed(
  () => (runtime.tasks || []).filter((task) => ["queued", "running"].includes(task.status)).length,
)
const selectedHistoryCount = computed(() => Math.max(0, Number(historySelection.value.groupCount || 0)))
const immediateSearchLabel = computed(() =>
  selectedHistoryCount.value ? `搜索所选（${selectedHistoryCount.value}）` : "搜索全部",
)
const immediateSearchTitle = computed(() =>
  selectedHistoryCount.value ? `立即搜索所选 ${selectedHistoryCount.value} 个历史媒体` : "立即搜索全部订阅",
)
const immediateSearchDialogTitle = computed(() => (selectedHistoryCount.value ? "搜索所选历史记录" : "搜索全部订阅"))
const runtimeSummary = computed(() => {
  if (runtime.status === "starting") return "正在准备订阅任务"
  if (runtime.status === "stopping") return "正在停止当前任务"
  if (active.value) return runtime.task || "正在处理订阅任务"
  return "统一管理订阅搜索、资源转存与运行记录"
})

function updateHistorySelection(selection) {
  historySelection.value = {
    groupCount: Math.max(0, Number(selection?.groupCount || 0)),
    subscribeIds: Array.isArray(selection?.subscribeIds) ? [...selection.subscribeIds] : [],
    targets: Array.isArray(selection?.targets) ? [...selection.targets] : [],
  }
}

function openImmediateSearchConfirm() {
  searchConfirmVisible.value = true
}

async function confirmImmediateSearch() {
  if (searchStarting.value) return
  searchStarting.value = true
  try {
    const success = await startSync(historySelection.value)
    if (success) searchConfirmVisible.value = false
  } finally {
    searchStarting.value = false
  }
}

async function manualStarted(text) {
  notify(text)
  await loadSummary(false)
  if (mainTab.value === "history") await loadPage(false)
  else historyDirty.value = true
}

function openManualDialog(mode = "links", media = null) {
  manualInitialMode.value = mode === "upgrade" ? "upgrade" : "links"
  manualInitialMedia.value = media ? { ...media } : null
  manualVisible.value = true
}

function confirmStopSync() {
  stoppingTask.value = null
  stopVisible.value = true
}

function confirmStopTask(taskId) {
  const task = (runtime.tasks || []).find((item) => item.id === taskId)
  if (!task) return
  stoppingTask.value = task
  stopVisible.value = true
}

async function stopConfirmed() {
  if (stopping.value) return
  stopping.value = true
  try {
    const success = stoppingTask.value ? await stopTaskRequest(stoppingTask.value.id) : await stopSyncRequest()
    if (success) {
      stopVisible.value = false
      stoppingTask.value = null
    }
  } finally {
    stopping.value = false
  }
}

async function openConfig() {
  if (configLoading.value) return
  configLoading.value = true
  try {
    const response = await api.get(`plugin/form/${props.pluginId}`)
    const data = response?.data?.model ? response.data : response
    if (!data?.model || typeof data.model !== "object") {
      throw new Error("未能读取当前插件配置")
    }
    configData.value = JSON.parse(JSON.stringify(data.model))
    configVisible.value = true
  } catch (error) {
    notify(error.message || "加载配置失败", "error")
  } finally {
    configLoading.value = false
  }
}

function openMediaDetail(link) {
  window.location.hash = String(link).replace(/^#/, "")
}

async function playHistory(itemId) {
  const popup = window.open("", "_blank")
  try {
    const result = await api.get(`plugin/${props.pluginId}/history/play/${encodeURIComponent(itemId)}`)
    const url = result?.data?.url
    if (!result?.success || !url) throw new Error(result?.message || "未找到播放地址")
    if (popup) popup.location.href = url
    else window.open(url, "_blank", "noopener,noreferrer")
  } catch (error) {
    if (popup) popup.close()
    notify(error.message || "打开 Emby 失败", "error")
  }
}

function openClearHistory() {
  clearVisible.value = true
}

async function clearHistory({forceClear, clearPoints}) {
  clearing.value = true
  try {
    const message = await clearHistoryRequest(forceClear, clearPoints);
    clearVisible.value = false
    notify(message)
  } catch (e) {
    notify(e.message || "清空失败", "error")
  } finally {
    clearing.value = false
  }
}

async function clearCache(categories) {
  clearingCache.value = true
  try {
    const message = await clearCacheRequest(categories)
    cacheVisible.value = false
    notify(message)
  } catch (e) {
    notify(e.message || "清理缓存失败", "error")
  } finally {
    clearingCache.value = false
  }
}

function historyKey(record) {
  return [record.time, record.share_url, record.file_name].join("|")
}

function confirmRetryHistory(record) {
  retryingRecord.value = record
  retryVisible.value = true
}

async function retryHistory() {
  const record = retryingRecord.value
  if (!record || retryingHistoryKey.value) return
  retryingHistoryKey.value = historyKey(record)
  try {
    const result = await api.post(`plugin/${props.pluginId}/history/retry`, {
      time: record.time,
      share_url: record.share_url,
      file_name: record.file_name,
    })
    if (!result?.success) throw new Error(result?.message || "重试失败")
    await Promise.all([loadPage(false), loadSummary(false)])
    retryVisible.value = false
    retryingRecord.value = null
    notify(result.message || "已重新提交处理")
  } catch (e) {
    notify(e.message || "重试失败", "error")
  } finally {
    retryingHistoryKey.value = ""
  }
}

function handleHistoryUpgrade(payload) {
  if (payload?.scope === "group") {
    openManualDialog("upgrade", payload.media || null)
    return
  }
  if (!(payload?.records || []).length) return
  upgradingPayload.value = payload
  upgradeVisible.value = true
}

function historyUpgradeLabel(payload) {
  const record = payload?.records?.[0] || {}
  const title = String(record.title || record.file_name || "此记录").trim()
  const season = Number(record.season || 0)
  const episode = Number(record.episode || 0)
  if (season > 0 && episode > 0) {
    return `${title} S${String(season).padStart(2, "0")}E${String(episode).padStart(2, "0")}`
  }
  return title
}

async function confirmHistoryUpgrade() {
  const payload = upgradingPayload.value
  const records = payload?.records || []
  if (!records.length) return
  upgradingHistoryKey.value = String(payload?.key || "")
  try {
    const message = await upgradeHistoryRequest(records, payload?.scope || "record")
    upgradeVisible.value = false
    upgradingPayload.value = null
    notify(message)
  } catch (e) {
    notify(e.message || "洗版任务提交失败", "error")
  } finally {
    upgradingHistoryKey.value = ""
  }
}

function confirmDeleteHistory(record) {
  if (!["成功", "失败"].includes(record?.status) && !record?.finalize_key) {
    notify("任务仍在处理，完成后才能删除历史记录", "warning")
    return
  }
  deletingRecord.value = record
  deletingRecords.value = []
  deletingGroupCount.value = 0
  deleteVisible.value = true
}

function confirmDeleteGroups({ records, groupCount }) {
  const deletableRecords = (records || []).filter(
    (record) => ["成功", "失败"].includes(record?.status) || Boolean(record?.finalize_key),
  )
  if (!deletableRecords.length) {
    notify("所选汇总项没有可删除的历史记录", "warning")
    return
  }
  deletingRecord.value = null
  deletingRecords.value = deletableRecords
  deletingGroupCount.value = Number(groupCount || 0)
  deleteVisible.value = true
}

async function deleteHistory({deleteLinked}) {
  const batch = deletingGroupCount.value > 0
  const record = deletingRecord.value
  if (!batch && !record) return
  deletingHistoryKey.value = batch ? "batch" : historyKey(record)
  try {
    const message = batch
      ? await deleteHistoryBatchRequest(deletingRecords.value, deleteLinked)
      : await deleteHistoryRequest(record, deleteLinked)
    deleteVisible.value = false
    deletingRecord.value = null
    deletingRecords.value = []
    deletingGroupCount.value = 0
    notify(message)
  } catch (e) {
    notify(e.message || "删除失败", "error")
  } finally {
    deletingHistoryKey.value = ""
  }
}

function confirmNotifyHistory(payload) {
  const record = payload?.record || payload
  if (record?.status !== "成功" || record?.finalize_key) {
    notify("文件尚未成功完成，不能发送通知", "warning")
    return
  }
  notifyingRecord.value = record
  notifyingSummaryTitle.value = String(payload?.summaryTitle || record?.title || record?.file_name || "此记录")
  notifyError.value = ""
  notifyVisible.value = true
}

async function notifyHistory() {
  const record = notifyingRecord.value
  if (!record) return
  notifyingHistoryKey.value = historyKey(record)
  try {
    const message = await notifyHistoryRequest(record)
    notifyVisible.value = false
    notifyingRecord.value = null
    notifyingSummaryTitle.value = ""
    notifyError.value = ""
    notify(message)
  } catch (e) {
    notifyError.value = e.message || "通知失败"
  } finally {
    notifyingHistoryKey.value = ""
  }
}
</script>

<style scoped>
.cloud-app-page {
  display: flex;
  box-sizing: border-box;
  min-width: 0;
  height: calc(100dvh - 64px);
  max-height: calc(100dvh - 64px);
  min-height: 0;
  flex-direction: column;
  gap: 16px;
  overflow: hidden;
  padding: 16px;
  color: rgb(var(--v-theme-on-background));
}

.app-header,
.app-identity,
.app-actions,
.overview-title,
.overview-metric {
  display: flex;
  align-items: center;
}

.app-header {
  flex: 0 0 auto;
  justify-content: space-between;
  gap: 16px;
  min-width: 0;
}

.app-identity {
  min-width: 0;
  gap: 12px;
}

.app-identity > div {
  min-width: 0;
}

.app-identity h1 {
  margin: 0;
  font-size: 1.15rem;
  font-weight: 600;
  line-height: 1.3;
  letter-spacing: 0;
}

.app-identity p {
  margin: 2px 0 0;
  color: rgba(var(--v-theme-on-surface), 0.62);
  font-size: 0.875rem;
  line-height: 1.4;
}

.app-actions {
  flex: 0 0 auto;
  gap: 6px;
}

.app-actions :deep(.v-btn) {
  min-height: 34px;
  padding-inline: 10px;
  font-size: 0.8rem;
}

.overview-band {
  display: flex;
  flex: 0 0 auto;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}

.overview-title {
  gap: 8px;
  padding-inline: 2px;
  font-size: 0.875rem;
  font-weight: 600;
}

.overview-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  overflow: hidden;
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 8px;
  background: rgb(var(--v-theme-surface));
}

.overview-metric {
  min-width: 0;
  gap: 10px;
  padding: 12px 16px;
}

.overview-metric + .overview-metric {
  border-left: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.overview-metric > div,
.overview-metric span,
.overview-metric strong {
  display: block;
  min-width: 0;
}

.overview-metric span {
  color: rgba(var(--v-theme-on-surface), 0.62);
  font-size: 0.75rem;
}

.overview-metric strong {
  margin-top: 2px;
  font-size: 1.15rem;
  line-height: 1.2;
}

.workspace {
  display: flex;
  min-width: 0;
  min-height: 0;
  flex: 1 1 auto;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
  border-radius: 8px;
  background: rgb(var(--v-theme-surface));
}

.workspace-nav {
  display: flex;
  align-items: center;
  min-width: 0;
  flex: 0 0 auto;
  border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.workspace-tabs {
  min-width: 0;
  flex: 1 1 auto;
}

.workspace-window,
.workspace-pane {
  min-height: 0;
  height: 100%;
}

.workspace-window {
  display: flex;
  flex: 1 1 auto;
}

.workspace-pane {
  min-width: 0;
  flex: 1 1 auto;
  overflow: hidden;
}

.task-pane {
  display: flex;
  padding: 12px;
}

.runtime-panel {
  flex: 1 1 auto;
  min-height: 100%;
}

.history-pane {
  display: flex;
}

:global(html:has(.cloud-app-page)),
:global(body:has(.cloud-app-page)) {
  overflow: hidden !important;
  overscroll-behavior: none;
}

@media (max-width: 959px) {
  .cloud-app-page {
    gap: 12px;
    padding: 12px;
  }

  .app-header {
    align-items: flex-start;
  }

  .app-actions {
    display: grid;
    grid-template-columns: repeat(4, 40px) auto;
    gap: 4px;
  }

  .app-actions :deep(.v-btn) {
    min-width: 40px;
    width: 40px;
    padding-inline: 0;
  }

  .app-actions :deep(.v-btn__prepend) {
    margin: 0;
  }

  .action-label {
    display: none;
  }

  .app-actions :deep(.settings-action) {
    width: auto;
    min-width: 64px;
    padding-inline: 8px;
  }

  .app-actions :deep(.settings-action .v-btn__prepend) {
    margin-inline-end: 4px;
  }
}

.dialog-card {
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.16) !important;
}


@media (max-width: 600px) {
  .overview-metric--desktop-only {
    display: none;
  }

  .cloud-app-page {
    overflow-x: hidden;
    overflow-y: auto;
    overscroll-behavior-y: contain;
    padding: 10px;
  }

  .app-header {
    align-items: center;
  }

  .app-identity {
    flex: 1 1 auto;
    gap: 9px;
    overflow: hidden;
  }

  .app-identity :deep(.v-avatar) {
    width: 36px !important;
    height: 36px !important;
  }

  .app-identity h1,
  .app-identity p {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .app-identity h1 {
    font-size: 1.05rem;
  }

  .app-identity p {
    display: none;
  }

  .app-actions {
    grid-template-columns: repeat(5, 34px);
  }

  .app-actions :deep(.v-btn) {
    min-width: 34px;
    width: 34px;
    height: 34px;
    padding: 0 !important;
    border-radius: 50%;
    background: transparent !important;
    box-shadow: none !important;
  }

  .app-actions :deep(.v-btn__overlay),
  .app-actions :deep(.v-btn__underlay) {
    opacity: 0 !important;
  }

  .app-actions :deep(.sync-action) {
    color: rgb(var(--v-theme-primary)) !important;
  }

  .app-actions :deep(.sync-action-active) {
    color: rgb(var(--v-theme-warning)) !important;
  }

  .app-actions :deep(.settings-action) {
    min-width: 34px;
    width: 34px;
    padding-inline: 0;
  }

  .app-actions :deep(.settings-action .v-btn__prepend) {
    margin: 0;
  }

  .settings-action .action-label {
    display: none;
  }

  .overview-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .workspace {
    min-height: max(460px, calc(100dvh - 210px));
    flex: 0 0 auto;
  }

  .workspace-window,
  .workspace-pane {
    height: auto;
    overflow: visible;
  }

  .task-pane :deep(.runtime-panel),
  .task-pane :deep(.task-list),
  .workspace-history .history-pane :deep(.history-table-root),
  .workspace-history .history-pane :deep(.history-mobile-list),
  .workspace-history .history-pane :deep(.history-mobile-scroll) {
    min-height: 0;
    flex: 0 0 auto;
    overflow: visible;
    overscroll-behavior: auto;
    touch-action: pan-y;
  }

  .workspace-history .history-pane :deep(.history-table-root),
  .workspace-history .history-pane :deep(.history-mobile-list) {
    width: 100%;
    max-width: 100%;
  }

  .task-pane :deep(.runtime-panel) {
    display: flex;
    width: 100%;
    min-height: 360px;
  }

  .overview-metric {
    padding: 9px 12px;
  }

  .workspace-tabs :deep(.v-tab) {
    min-width: 0;
    flex: 1 1 50%;
    padding-inline: 8px;
    text-transform: none;
  }

  .task-pane {
    padding: 8px;
  }
}
</style>
