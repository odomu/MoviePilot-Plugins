<template>
  <div class="cloud-subscribe-page">
    <v-card flat class="border rounded page-shell">
      <v-card-title class="page-header d-flex align-center ga-1 px-3 py-2 bg-primary-lighten-5">
        <v-icon icon="mdi-cloud-sync-outline" color="primary" size="small" class="mr-1" />
        <span class="page-title text-subtitle-1">网盘订阅助手</span>
        <v-spacer />
        <v-btn
          class="header-action"
          variant="text"
          size="small"
          prepend-icon="mdi-coffee-outline"
          title="赞赏支持"
          @click="rewardVisible = true">
          赞赏支持
        </v-btn>
        <v-btn
          class="header-action"
          variant="text"
          size="small"
          prepend-icon="mdi-cog-outline"
          title="配置"
          @click="emit('switch')">
          配置
        </v-btn>
        <v-btn
          class="header-action"
          variant="text"
          size="small"
          prepend-icon="mdi-close"
          title="关闭"
          @click="emit('close')">
          关闭
        </v-btn>
      </v-card-title>
      <v-divider />
      <div class="page-summary page-summary--compact">
        <div class="summary-heading">
          <div class="d-flex align-center ga-2">
            <v-icon icon="mdi-chart-box-outline" color="primary" size="small" />
            <span>转存概览</span>
          </div>
          <div class="page-actions">
            <v-btn
              color="secondary"
              variant="tonal"
              size="small"
              prepend-icon="mdi-link-variant-plus"
              @click="openManualDialog()">
              手动添加
            </v-btn>
            <v-btn
              v-if="offlineSupported"
              color="info"
              variant="tonal"
              size="small"
              prepend-icon="mdi-cloud-download-outline"
              @click="offlineVisible = true">
              离线任务
            </v-btn>
            <v-btn
              color="warning"
              variant="tonal"
              size="small"
              prepend-icon="mdi-cached"
              title="选择并清理缓存"
              @click="cacheVisible = true">
              清理缓存
            </v-btn>
            <v-btn
              :color="active ? 'warning' : 'primary'"
              variant="flat"
              size="small"
              :prepend-icon="active ? 'mdi-stop-circle-outline' : 'mdi-magnify'"
              :disabled="runtime.status === 'stopping'"
              :loading="runtime.status === 'starting' || searchStarting"
              :title="active ? '停止全部任务' : immediateSearchTitle"
              @click="active ? confirmStopSync() : openImmediateSearchConfirm()">
              {{ active ? "停止全部" : immediateSearchLabel }}
            </v-btn>
          </div>
        </div>
        <StatsGrid :stats="stats" class="summary-stats" />
      </div>
      <v-tabs v-model="mainTab" color="primary" class="page-tabs border-b" grow>
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
      <v-card-text class="pa-0 page-body">
        <div class="page-window">
          <div v-show="mainTab === 'tasks'" class="page-pane task-pane">
            <RuntimeCard class="runtime-tab-panel" :runtime="runtime" :active="active" @stop-task="confirmStopTask" />
          </div>
          <div v-show="mainTab === 'history'" class="page-pane history-pane">
            <div class="history-shell">
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
        </div>
      </v-card-text>
    </v-card>
    <OfflineTasksDialog v-if="offlineSupported" v-show="offlineVisible" v-model="offlineVisible" :api="api" />
    <ManualResourceDialog
      v-show="manualVisible"
      v-model="manualVisible"
      :api="api"
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
    <v-snackbar v-model="messageVisible" :color="messageType" location="top" timeout="3500" variant="elevated">
      {{ message }}
      <template #actions>
        <v-btn icon="mdi-close" size="small" variant="text" @click="messageVisible = false" />
      </template>
    </v-snackbar>
    <ConfirmDialog
      v-if="clearVisible"
      v-model="clearVisible"
      title="清空历史记录"
      icon="mdi-delete-sweep-outline"
      icon-color="error"
      body="确认清理本插件的转存历史？此操作不可逆。"
      :options="[
        { key: 'forceClear', label: '强制清理全部历史记录（含处理中）', color: 'error' },
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
    <RewardDialog v-model="rewardVisible" />
  </div>
</template>

<script setup>
import {computed, defineAsyncComponent, onMounted, onUnmounted, ref, watch} from "vue";
import RuntimeCard from "./dashboard/RuntimeCard.vue";
import HistoryTable from "./dashboard/HistoryTable.vue";
import StatsGrid from "./dashboard/StatsGrid.vue";
import {useHistoryPageData} from "../composables/usePageData.js";
import {useRuntimeData} from "../composables/useRuntimeData.js";
import {CACHE_CATEGORIES, useCacheActions} from "../composables/useCacheActions.js";

const ConfirmDialog = defineAsyncComponent(() => import("./dialogs/ConfirmDialog.vue"));
const ManualResourceDialog = defineAsyncComponent(() => import("./dialogs/ManualResourceDialog.vue"))
const OfflineTasksDialog = defineAsyncComponent(() => import("./dialogs/OfflineTasksDialog.vue"))
const RewardDialog = defineAsyncComponent(() => import("./dialogs/RewardDialog.vue"));

const props = defineProps({
  api: { type: [Object, Function], required: true },
})
const emit = defineEmits(["close", "switch", "action"])
const api = props.api
const rewardVisible = ref(false);
const message = ref(""),
  messageType = ref("success"),
  messageVisible = ref(false),
  mainTab = ref("tasks"),
  offlineVisible = ref(false),
  manualVisible = ref(false),
  manualInitialMode = ref("links"),
  manualInitialMedia = ref(null),
  stopVisible = ref(false),
  stoppingTask = ref(null),
  stopping = ref(false),
  clearVisible = ref(false),
  clearing = ref(false),
  forceClearHistory = ref(false),
  clearPointsHistory = ref(false),
  cacheVisible = ref(false),
  clearingCache = ref(false),
  retryingHistoryKey = ref(""),
  retryVisible = ref(false),
  retryingRecord = ref(null),
  deleteVisible = ref(false),
  deletingRecord = ref(null),
  deletingRecords = ref([]),
  deletingGroupCount = ref(0),
  deleteLinkedFiles = ref(false),
  deletingHistoryKey = ref(""),
  notifyVisible = ref(false),
  notifyingRecord = ref(null),
  notifyingSummaryTitle = ref(""),
  notifyingHistoryKey = ref(""),
  upgradingHistoryKey = ref(""),
  upgradeVisible = ref(false),
  upgradingPayload = ref(null),
  notifyError = ref(""),
  searchConfirmVisible = ref(false),
  searchStarting = ref(false),
  historyDirty = ref(true),
  historySelection = ref({ groupCount: 0, subscribeIds: [], targets: [] })

function notify(text, type = "success") {
  message.value = text
  messageType.value = type
  messageVisible.value = true
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
} = useHistoryPageData(api, notify)
const { clearCache: clearCacheRequest } = useCacheActions(api)
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
} = useRuntimeData(api, notify, "CloudSubscribe", {
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

function openClearHistory() {
  clearVisible.value = true
}

function openMediaDetail(link) {
  emit("close")
  window.setTimeout(() => {
    window.location.hash = String(link).replace(/^#/, "")
  }, 0)
}

async function playHistory(itemId) {
  const popup = window.open("", "_blank")
  try {
    const result = await api.get(`plugin/CloudSubscribe/history/play/${encodeURIComponent(itemId)}`)
    const url = result?.data?.url
    if (!result?.success || !url) throw new Error(result?.message || "未找到播放地址")
    if (popup) popup.location.href = url
    else window.open(url, "_blank", "noopener,noreferrer")
  } catch (error) {
    if (popup) popup.close()
    notify(error.message || "打开 Emby 失败", "error")
  }
}

async function clearHistory({forceClear, clearPoints}) {
  clearing.value = true
  try {
    const resultMessage = await clearHistoryRequest(forceClear, clearPoints);
    clearVisible.value = false
    notify(resultMessage)
  } catch (e) {
    notify(e.message || "清空失败", "error")
  } finally {
    clearing.value = false
  }
}

async function clearCache(categories) {
  clearingCache.value = true
  try {
    const resultMessage = await clearCacheRequest(categories)
    cacheVisible.value = false
    notify(resultMessage)
  } catch (e) {
    notify(e.message || "清理缓存失败", "error")
  } finally {
    clearingCache.value = false
  }
}

function confirmRetryHistory(record) {
  retryingRecord.value = record
  retryVisible.value = true
}

async function retryHistory() {
  const record = retryingRecord.value
  if (!record || retryingHistoryKey.value) return
  retryingHistoryKey.value = [record.time, record.share_url, record.file_name].join("|")
  try {
    const result = await api.post("plugin/CloudSubscribe/history/retry", {
      time: record.time,
      share_url: record.share_url,
      file_name: record.file_name,
    })
    if (!result?.success) throw new Error(result?.message || "重试失败")
    await Promise.all([loadPage(false), loadSummary(false)])
    retryVisible.value = false
    retryingRecord.value = null
  } catch (error) {
    notify(error.message || "重试失败", "error")
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
    const resultMessage = await upgradeHistoryRequest(records, payload?.scope || "record")
    upgradeVisible.value = false
    upgradingPayload.value = null
    notify(resultMessage)
  } catch (error) {
    notify(error.message || "洗版任务提交失败", "error")
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
  deletingHistoryKey.value = batch ? "batch" : [record.time, record.share_url, record.file_name].join("|")
  try {
    const resultMessage = batch
      ? await deleteHistoryBatchRequest(deletingRecords.value, deleteLinked)
      : await deleteHistoryRequest(record, deleteLinked)
    deleteVisible.value = false
    deletingRecord.value = null
    deletingRecords.value = []
    deletingGroupCount.value = 0
    notify(resultMessage)
  } catch (error) {
    notify(error.message || "删除失败", "error")
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
  notifyingHistoryKey.value = [record.time, record.share_url, record.file_name].join("|")
  try {
    const resultMessage = await notifyHistoryRequest(record)
    notifyVisible.value = false
    notifyingRecord.value = null
    notifyingSummaryTitle.value = ""
    notifyError.value = ""
    notify(resultMessage)
  } catch (error) {
    notifyError.value = error.message || "通知失败"
  } finally {
    notifyingHistoryKey.value = ""
  }
}
</script>

<style scoped>
.cloud-subscribe-page {
  width: 100%;
  height: 100%;
  min-height: 0;
  overflow: hidden;
}

.page-shell {
  height: 100%;
  max-height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

:global(html[data-theme="transparent"]) .page-shell,
:global(html[data-theme="glass"]) .page-shell,
:global(html[data-theme-preference="transparent"]) .page-shell,
:global(html[class*="transparent-glass"]) .page-shell,
:global(html[data-glass-appearance]) .page-shell,
:global(.v-theme--transparent) .page-shell {
  background-color: rgba(var(--v-theme-surface), var(--transparent-opacity-heavy, 0.78)) !important;
  backdrop-filter: blur(var(--transparent-blur-heavy, 16px)) saturate(140%) !important;
  -webkit-backdrop-filter: blur(var(--transparent-blur-heavy, 16px)) saturate(140%) !important;
  border-color: rgba(var(--v-border-color), 0.16) !important;
}

.page-header {
  min-width: 0;
  flex-wrap: nowrap;
}

.page-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.page-body {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

:global(.v-overlay__content:has(.cloud-subscribe-page)) {
  width: min(72rem, calc(100vw - 48px)) !important;
  max-width: 72rem !important;
  height: min(760px, calc(100dvh - 48px)) !important;
  max-height: min(760px, calc(100dvh - 48px)) !important;
  overflow: hidden !important;
}

.page-tabs {
  flex: 0 0 auto;
}

.page-tabs :deep(.v-tab) {
  min-width: 0;
  text-transform: none;
}

.page-window,
.page-pane {
  height: 100%;
  min-height: 0;
}

.page-window {
  display: flex;
  flex: 1 1 auto;
}

.page-pane {
  box-sizing: border-box;
  width: 100%;
  min-width: 0;
  flex: 1 1 auto;
  overflow-x: hidden;
  padding: 12px;
}

.task-pane {
  display: flex;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.runtime-tab-panel {
  flex: 1 1 auto;
  min-height: 100%;
  margin: 0 !important;
}

.page-summary {
  flex: 0 0 auto;
  padding: 10px 12px 0;
}

.summary-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
  color: rgb(var(--v-theme-on-surface));
  font-size: 0.875rem;
  font-weight: 600;
}

.page-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}

.summary-stats {
  margin-bottom: 0 !important;
}

.history-shell {
  display: flex;
  height: 100%;
  width: 100%;
  max-width: 100%;
  min-height: 0;
  min-width: 0;
  overflow: hidden;
}

.history-pane {
  display: flex;
  overflow: hidden;
}

:global(.v-dialog > .v-overlay__content:has(.cloud-subscribe-page) > .v-card) {
  height: 100%;
  max-height: 100%;
  min-height: 0;
  overflow: hidden !important;
}

.dialog-card {
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.16) !important;
}



@media (max-width: 600px) {
  :global(.v-overlay__content:has(.cloud-subscribe-page)) {
    width: 100vw !important;
    max-width: 100vw !important;
    height: 100dvh !important;
    max-height: 100dvh !important;
    margin: 0 !important;
  }

  .page-header {
    padding-left: 10px !important;
    padding-right: 10px !important;
  }

  .page-shell {
    width: 100%;
    height: 100%;
    max-height: 100%;
    min-height: 0;
    border-radius: 0 !important;
  }

  .header-action {
    min-width: 34px !important;
    width: 34px;
    padding: 0 !important;
  }

  .header-action :deep(.v-btn__content) {
    display: none;
  }

  .header-action :deep(.v-btn__prepend) {
    margin: 0;
  }

  .page-body {
    padding: 0 !important;
  }

  .page-tabs :deep(.v-tab) {
    padding-inline: 8px;
    font-size: 0.875rem;
  }

  .page-pane {
    padding: 10px;
  }

  .page-summary {
    padding: 10px 10px 0;
  }

  .summary-heading {
    align-items: flex-start;
    flex-direction: column;
    gap: 8px;
    margin-bottom: 8px;
  }

  .page-actions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    width: 100%;
    gap: 8px;
  }

  .page-actions :deep(.v-btn) {
    min-width: 0;
    height: 34px;
    padding-inline: 10px;
    font-size: 13px;
    border-radius: 8px;
  }
}
</style>
