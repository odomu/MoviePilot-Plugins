<template>
  <div class="history-table-root">
    <div class="history-toolbar">
      <div class="history-toolbar-head">
        <div class="history-filter-trigger">
          <v-menu
              v-model="filtersVisible"
              :close-on-content-click="false"
              location="bottom start"
              offset="6"
          >
            <template #activator="{ props: menuProps }">
              <v-badge
                  :content="activeFilterCount"
                  :model-value="activeFilterCount > 0"
                  color="primary"
              >
                <v-btn
                    v-bind="menuProps"
                    icon="mdi-filter-variant"
                    :color="activeFilterCount ? 'primary' : undefined"
                    variant="text"
                    size="small"
                    title="筛选历史记录"
                />
              </v-badge>
            </template>
            <v-card class="history-filter-menu" elevation="8">
              <v-card-title class="history-filter-menu-title">
                <span>筛选历史记录</span>
                <v-spacer/>
                <v-btn
                    variant="text"
                    size="small"
                    :disabled="!activeFilterCount"
                    @click="clearFilters"
                >清除
                </v-btn>
              </v-card-title>
              <v-divider/>
              <v-card-text class="history-filter-options">
                <div
                    v-if="resourceTypeOptions.length"
                    class="history-filter-group"
                >
                  <span class="history-filter-label">资源类型</span>
                  <v-chip-group
                      v-model="selectedResourceTypes"
                      multiple
                      selected-class="text-primary"
                  >
                    <v-chip
                        v-for="option in resourceTypeOptions"
                        :key="option.value"
                        :value="option.value"
                        size="small"
                        variant="tonal"
                        filter
                    >{{ option.title }}
                    </v-chip>
                  </v-chip-group>
                </div>
                <div v-if="sourceOptions.length" class="history-filter-group">
                  <span class="history-filter-label">来源</span>
                  <v-chip-group
                      v-model="selectedSources"
                      multiple
                      selected-class="text-primary"
                  >
                    <v-chip
                        v-for="option in sourceOptions"
                        :key="option.value"
                        :value="option.value"
                        size="small"
                        variant="tonal"
                        filter
                    >{{ option.title }}
                    </v-chip>
                  </v-chip-group>
                </div>
                <div class="history-filter-group">
                  <span class="history-filter-label">任务类型</span>
                  <v-chip-group
                      v-model="selectedTaskTypes"
                      multiple
                      selected-class="text-primary"
                  >
                    <v-chip
                        v-for="option in taskTypeOptions"
                        :key="option.value"
                        :value="option.value"
                        size="small"
                        variant="tonal"
                        filter
                    >{{ option.title }}
                    </v-chip>
                  </v-chip-group>
                </div>
                <div class="history-filter-group">
                  <span class="history-filter-label">状态</span>
                  <v-chip-group
                      v-model="selectedStatuses"
                      multiple
                      selected-class="text-primary"
                  >
                    <v-chip
                        v-for="status in statusOptions"
                        :key="status"
                        :value="status"
                        size="small"
                        variant="tonal"
                        filter
                    >{{ status }}
                    </v-chip>
                  </v-chip-group>
                </div>
              </v-card-text>
            </v-card>
          </v-menu>
        </div>
        <v-text-field
            v-if="!isMobile"
            v-model="keyword"
            class="history-search"
            placeholder="搜索标题或文件名"
            prepend-inner-icon="mdi-magnify"
            clearable
            density="compact"
            variant="outlined"
            hide-details
        />
        <div v-else class="history-search-trigger">
          <v-menu
              v-model="searchVisible"
              :close-on-content-click="false"
              location="bottom start"
              offset="6"
          >
            <template #activator="{ props: menuProps }">
              <v-btn
                  v-bind="menuProps"
                  icon="mdi-magnify"
                  :color="keyword ? 'primary' : undefined"
                  variant="text"
                  size="small"
                  title="搜索历史记录"
              />
            </template>
            <v-card class="history-search-menu" elevation="8">
              <v-card-text class="pa-2">
                <v-text-field
                    v-model="keyword"
                    placeholder="搜索标题或文件名"
                    prepend-inner-icon="mdi-magnify"
                    clearable
                    autofocus
                    density="compact"
                    variant="outlined"
                    hide-details
                />
              </v-card-text>
            </v-card>
          </v-menu>
        </div>
        <v-spacer/>
        <v-btn
            class="delete-selected-button"
            color="error"
            variant="text"
            size="small"
            :disabled="!deletableSelectedGroups.length"
            :loading="deletingKey === 'batch'"
            @click.stop="deleteSelected"
        >
          <v-icon icon="mdi-delete-outline" class="history-action-icon"/>
          <span class="history-action-label">删除所选</span>
        </v-btn>
        <div class="history-actions">
          <v-btn
              variant="text"
              size="small"
              :loading="loading"
              title="刷新历史记录"
              @click.stop="emit('refresh')"
          >
            <v-icon icon="mdi-refresh" class="history-action-icon"/>
            <span class="history-action-label">刷新</span>
          </v-btn>
          <v-btn
              color="error"
              variant="text"
              size="small"
              title="清空历史记录"
              @click.stop="emit('clear')"
          >
            <v-icon icon="mdi-delete-sweep" class="history-action-icon"/>
            <span class="history-action-label">清空历史</span>
          </v-btn>
        </div>
      </div>
      <v-divider/>
    </div>

    <v-data-table
        v-if="!isMobile && (loading || groupedItems.length)"
        :headers="headers"
        :items="groupedItems"
        v-model="selectedGroupKeys"
        v-model:expanded="expanded"
        :loading="loading"
        item-value="group_key"
        item-selectable="selectable"
        select-strategy="all"
        show-select
        show-expand
        density="compact"
        hover
        fixed-header
        :items-per-page="10"
        :items-per-page-options="pageSizes"
        items-per-page-text="每页"
        page-text="第 {0}-{1} 条，共 {2} 条"
        no-data-text="暂无符合条件的转存记录"
        loading-text="正在加载转存记录..."
        class="history-table"
        @click:row="toggleExpanded"
    >
      <template #item.media="{ item }">
        <div class="media-cell">
          <span class="media-title font-weight-medium">
            {{ item.title }}<span v-if="item.year"> ({{ item.year }})</span>
          </span>
          <v-btn
              v-if="mediaDetailLink(item)"
              icon="mdi-open-in-new"
              variant="text"
              size="x-small"
              color="primary"
              title="查看媒体详情"
              class="media-link"
              @click.stop="openMediaDetail(item)"
          />
        </div>
        <div class="text-caption text-medium-emphasis">
          {{
            item.type
          }}<span v-if="item.season_label"> · {{ item.season_label }}</span> ·
          {{ item.records.length }} 条记录
          <span> · {{ formatSize(item.total_size) }}</span>
        </div>
      </template>

      <template #item.resource_types="{ item }">
        <div v-if="item.resource_types.length === 1" class="summary-chips">
          <v-chip
              size="x-small"
              variant="tonal"
              :color="resourceTypeColor(item.resource_types[0])"
          >
            {{ resourceTypeLabel(item.resource_types[0]) }}
          </v-chip>
        </div>
        <div v-else class="mixed-resource-types">
          <v-chip
              size="x-small"
              variant="tonal"
              color="primary"
              prepend-icon="mdi-layers-triple-outline"
          >
            混合
          </v-chip>
        </div>
      </template>

      <template #item.sources="{ item }">
        <div class="source-summary">
          <v-chip
              v-if="item.source_items.length > 1"
              size="x-small"
              variant="tonal"
              color="primary"
              prepend-icon="mdi-source-branch"
          >
            多渠道
          </v-chip>
          <div v-else class="source-summary-links">
          <template v-for="source in item.source_items" :key="source.value">
            <a
                v-if="source.url"
                :href="source.url"
                target="_blank"
                rel="noopener noreferrer"
                class="source-link"
                @click.stop
            >
              {{ sourceLabel(source.value) }}
              <v-icon icon="mdi-open-in-new" size="x-small"/>
            </a>
            <span v-else>{{ sourceLabel(source.value) }}</span>
          </template>
          </div>
        </div>
      </template>

      <template #item.summary="{ item }">
        <div class="summary-counts">
          <span class="text-success">成功 {{ item.success_count }}</span>
          <span class="summary-separator">/</span>
          <span class="text-warning">待完成 {{ item.pending_count }}</span>
          <span class="summary-separator">/</span>
          <span class="text-error">失败 {{ item.failed_count }}</span>
        </div>
      </template>

      <template #item.latest_time="{ item }">
        <span class="latest-time">{{ item.latest_time || "-" }}</span>
      </template>

      <template #item.actions="{ item }">
        <div class="summary-actions">
          <v-btn
              v-if="canUpgradeGroup(item)"
              icon="mdi-auto-fix"
              color="warning"
              variant="text"
              size="x-small"
              :loading="upgradingKey === historyUpgradeGroupKey(item)"
              :title="item.type === '电影' ? '洗版此电影' : '洗版整个剧集列表'"
              @click.stop="upgradeGroup(item)"
          />
          <v-btn
              v-if="item.notification_record"
              icon="mdi-bell-ring-outline"
              color="primary"
              variant="text"
              size="x-small"
              :loading="notifyingKey === historyRetryKey(item.notification_record)"
              :title="'补发' + notificationSummaryTitle(item) + '汇总通知'"
              @click.stop="emit('notify', {
                record: item.notification_record,
                summaryTitle: notificationSummaryTitle(item),
              })"
          />
          <v-btn
              v-if="playItemId(item)"
              icon="mdi-play-circle-outline"
              color="success"
              variant="text"
              size="x-small"
              title="在 Emby 中播放"
              @click.stop="emit('play', playItemId(item))"
          />
        </div>
      </template>

      <template #expanded-row="{ columns, item }">
        <tr class="detail-row">
          <td :colspan="columns.length" class="pa-0">
            <v-table density="compact" class="detail-table">
              <thead>
              <tr>
                <th>文件 / 剧集</th>
                <th>资源类型</th>
                <th>来源</th>
                <th>大小</th>
                <th>状态</th>
                <th>时间</th>
                <th class="action-column">操作</th>
              </tr>
              </thead>
              <tbody>
              <tr
                  v-for="(record, index) in item.records"
                  :key="recordKey(record, index)"
              >
                <td>
                  <div class="d-flex align-center ga-1">
                      <span v-if="record.type !== '电影'" class="episode-label">
                        {{ episodeLabel(record) }}
                      </span>
                    <v-chip
                        v-if="record.upgrade"
                        size="x-small"
                        color="warning"
                        variant="tonal"
                    >
                      洗版
                      <v-tooltip activator="parent" location="top">
                        {{ record.upgrade_version_info }}
                      </v-tooltip>
                    </v-chip>
                    <v-chip
                        v-if="record.is_cross_transfer"
                        size="x-small"
                        color="info"
                        variant="tonal"
                    >
                      跨盘
                      <v-tooltip activator="parent" location="top">
                        {{ record.cross_transfer_title }}
                      </v-tooltip>
                    </v-chip>
                  </div>
                  <div class="text-caption text-medium-emphasis file-name">
                    {{ record.file_name || "-" }}
                  </div>
                </td>
                <td>
                  <v-chip
                      size="x-small"
                      variant="tonal"
                      :color="resourceTypeColor(resourceType(record))"
                  >
                    {{ resourceTypeLabel(resourceType(record)) }}
                  </v-chip>
                </td>
                <td>
                  <a
                      v-if="sourceLink(record)"
                      :href="sourceLink(record)"
                      target="_blank"
                      rel="noopener noreferrer"
                      class="source-link"
                      @click.stop
                  >
                    {{ sourceLabel(record.source) }}
                    <v-icon icon="mdi-open-in-new" size="x-small"/>
                  </a>
                  <span v-else>{{ sourceLabel(record.source) }}</span>
                </td>
                <td class="text-no-wrap">
                  {{ formatSize(record.file_size) }}
                </td>
                <td>
                  <v-chip
                      :color="statusColor(record.status)"
                      size="x-small"
                      variant="tonal"
                  >
                    {{ record.status }}
                  </v-chip>
                </td>
                <td class="text-no-wrap">{{ record.time || "-" }}</td>
                <td class="action-column">
                  <div class="action-buttons">
                    <v-btn
                        v-if="canUpgradeRecord(record)"
                        icon="mdi-auto-fix"
                        color="warning"
                        variant="text"
                        size="x-small"
                        :loading="upgradingKey === historyUpgradeRecordKey(record)"
                        title="洗版此集"
                        @click.stop="upgradeRecord(record)"
                    />
                    <v-btn
                        icon="mdi-reload"
                        color="primary"
                        variant="text"
                        size="x-small"
                        :disabled="!canRetryRecord(record)"
                        :loading="retryingKey === historyRetryKey(record)"
                        :title="retryTitle(record)"
                        @click.stop="emit('retry', record)"
                    />
                    <v-btn
                        icon="mdi-delete-outline"
                        color="error"
                        variant="text"
                        size="x-small"
                        :disabled="!canDeleteRecord(record)"
                        :loading="deletingKey === historyRetryKey(record)"
                        :title="deleteTitle(record)"
                        @click.stop="emit('delete', record)"
                    />
                  </div>
                </td>
              </tr>
              </tbody>
            </v-table>
          </td>
        </tr>
      </template>

    </v-data-table>

    <div
        v-else-if="!isMobile"
        class="history-empty text-body-2 text-medium-emphasis"
    >
      暂无符合条件的转存记录
    </div>

    <div v-else class="history-mobile-list">
      <v-progress-linear
          v-if="loading"
          indeterminate
          color="primary"
          height="2"
      />
      <div class="history-mobile-scroll">
        <div
            v-if="!loading && !groupedItems.length"
            class="history-mobile-empty text-body-2 text-medium-emphasis"
        >
          暂无符合条件的转存记录
        </div>
        <v-expansion-panels
            v-else
            v-model="expanded"
            multiple
            variant="accordion"
            class="history-mobile-panels"
        >
          <v-expansion-panel
              v-for="item in mobilePagedItems"
              :key="item.group_key"
              :value="item.group_key"
              :class="[
              'history-mobile-panel',
              { 'history-group-selected': isGroupSelected(item) },
            ]"
          >
            <v-expansion-panel-title class="history-mobile-title">
              <v-checkbox-btn
                  :model-value="isGroupSelected(item)"
                  :disabled="!item.selectable"
                  density="compact"
                  color="primary"
                  :aria-label="`选择 ${item.title}`"
                  @click.stop
                  @update:model-value="selectGroup(item, $event)"
              />
              <div class="history-mobile-summary">
                <div class="history-mobile-media-line">
                  <span class="history-mobile-media-title font-weight-medium">
                    {{
                      item.title
                    }}<span v-if="item.year" class="text-medium-emphasis">
                      ({{ item.year }})</span
                  >
                  </span>
                  <div class="history-mobile-summary-actions">
                    <v-btn
                        v-if="canUpgradeGroup(item)"
                        icon="mdi-auto-fix"
                        color="warning"
                        variant="text"
                        size="x-small"
                        :loading="upgradingKey === historyUpgradeGroupKey(item)"
                        :title="item.type === '电影' ? '洗版此电影' : '洗版整个剧集列表'"
                        @click.stop="upgradeGroup(item)"
                    />
                    <v-btn
                        v-if="item.notification_record"
                        icon="mdi-bell-ring-outline"
                        color="primary"
                        variant="text"
                        size="x-small"
                        :loading="notifyingKey === historyRetryKey(item.notification_record)"
                        :title="'补发' + notificationSummaryTitle(item) + '汇总通知'"
                        @click.stop="emit('notify', {
                          record: item.notification_record,
                          summaryTitle: notificationSummaryTitle(item),
                        })"
                    />
                    <v-btn
                        v-if="playItemId(item)"
                        icon="mdi-play-circle-outline"
                        color="success"
                        variant="text"
                        size="x-small"
                        title="在 Emby 中播放"
                        @click.stop="emit('play', playItemId(item))"
                    />
                    <v-btn
                        v-if="mediaDetailLink(item)"
                        icon="mdi-open-in-new"
                        variant="text"
                        size="x-small"
                        color="primary"
                        title="查看媒体详情"
                        @click.stop="openMediaDetail(item)"
                    />
                  </div>
                </div>
                <div class="history-mobile-summary-footer">
                  <div class="history-mobile-tags">
                    <v-chip
                        v-if="item.resource_types.length === 1"
                        :color="resourceTypeColor(item.resource_types[0])"
                        size="x-small"
                        variant="tonal"
                    >{{ resourceTypeLabel(item.resource_types[0]) }}
                    </v-chip>
                    <v-chip
                        v-else
                        color="primary"
                        size="x-small"
                        variant="tonal"
                        prepend-icon="mdi-layers-triple-outline"
                    >混合
                    </v-chip>
                    <span class="text-caption text-success">
                      成功 {{ item.success_count }} 条
                    </span>
                    <span
                        v-if="item.pending_count"
                        class="text-caption text-warning"
                    >
                      待完成 {{ item.pending_count }}
                    </span>
                    <span
                        v-if="item.failed_count"
                        class="text-caption text-error"
                    >
                      失败 {{ item.failed_count }}
                    </span>
                  </div>
                  <span
                      class="history-mobile-summary-time text-caption text-medium-emphasis"
                  >
                    {{ item.latest_time || "-" }}
                  </span>
                </div>
              </div>
            </v-expansion-panel-title>
            <v-expansion-panel-text class="history-mobile-details">
              <div
                  v-for="(record, index) in item.records"
                  :key="recordKey(record, index)"
                  class="history-mobile-record"
              >
                <div class="history-mobile-record-head">
                  <span v-if="record.type !== '电影'" class="episode-label">
                    {{ episodeLabel(record) }}
                  </span>
                  <v-chip
                      v-if="record.upgrade"
                      size="x-small"
                      color="warning"
                      variant="tonal"
                  >
                    洗版
                    <v-tooltip activator="parent" location="top">
                      {{ record.upgrade_version_info }}
                    </v-tooltip>
                  </v-chip>
                  <v-chip
                      v-if="record.is_cross_transfer"
                      size="x-small"
                      color="info"
                      variant="tonal"
                  >
                    跨盘
                    <v-tooltip activator="parent" location="top">
                      {{ record.cross_transfer_title }}
                    </v-tooltip>
                  </v-chip>
                  <v-chip
                      :color="statusColor(record.status)"
                      size="x-small"
                      variant="tonal"
                  >{{ record.status }}
                  </v-chip
                  >
                  <v-spacer/>
                  <div class="action-buttons history-mobile-actions">
                    <v-btn
                        v-if="canUpgradeRecord(record)"
                        icon="mdi-auto-fix"
                        color="warning"
                        variant="text"
                        size="x-small"
                        :loading="upgradingKey === historyUpgradeRecordKey(record)"
                        title="洗版此集"
                        @click.stop="upgradeRecord(record)"
                    />
                    <v-btn
                        icon="mdi-reload"
                        color="primary"
                        variant="text"
                        size="x-small"
                        :disabled="!canRetryRecord(record)"
                        :loading="retryingKey === historyRetryKey(record)"
                        :title="retryTitle(record)"
                        @click.stop="emit('retry', record)"
                    />
                    <v-btn
                        icon="mdi-delete-outline"
                        color="error"
                        variant="text"
                        size="x-small"
                        :disabled="!canDeleteRecord(record)"
                        :loading="deletingKey === historyRetryKey(record)"
                        :title="deleteTitle(record)"
                        @click.stop="emit('delete', record)"
                    />
                  </div>
                </div>
                <div class="history-mobile-file text-caption">
                  {{ record.file_name || "-" }}
                </div>
                <div class="history-mobile-record-footer">
                  <div
                      class="history-mobile-record-meta text-caption text-medium-emphasis"
                  >
                    <span>{{ resourceTypeLabel(resourceType(record)) }}</span>
                    <a
                        v-if="sourceLink(record)"
                        :href="sourceLink(record)"
                        target="_blank"
                        rel="noopener noreferrer"
                        class="source-link"
                        @click.stop
                    >{{ sourceLabel(record.source) }}</a
                    >
                    <span v-else>{{ sourceLabel(record.source) }}</span>
                    <span>{{ formatSize(record.file_size) }}</span>
                  </div>
                  <span
                      class="history-mobile-record-time text-caption text-medium-emphasis"
                  >{{ record.time || "-" }}</span
                  >
                </div>
              </div>
            </v-expansion-panel-text>
          </v-expansion-panel>
        </v-expansion-panels>
      </div>
      <v-pagination
          v-if="mobileTotalPages > 1"
          v-model="mobilePage"
          :length="mobileTotalPages"
          :total-visible="5"
          density="compact"
          class="history-mobile-pagination"
      />
    </div>
  </div>
</template>

<script setup>
import {computed, ref, watch} from "vue";
import {useDisplay} from "vuetify";

const props = defineProps({
  items: {type: Array, default: () => []},
  embyPlayItems: {type: Object, default: () => ({})},
  loading: Boolean,
  retryingKey: {type: String, default: ""},
  deletingKey: {type: String, default: ""},
  notifyingKey: {type: String, default: ""},
  upgradingKey: {type: String, default: ""},
});
const emit = defineEmits([
  "refresh",
  "clear",
  "retry",
  "delete",
  "delete-groups",
  "selection-change",
  "notify",
  "upgrade",
  "play",
  "open-media",
]);
const display = useDisplay();
const isMobile = computed(() => display.xs.value);
const keyword = ref("");
const selectedResourceTypes = ref([]);
const selectedSources = ref([]);
const selectedTaskTypes = ref([]);
const selectedStatuses = ref([]);
const expanded = ref([]);
const selectedGroupKeys = ref([]);
const filtersVisible = ref(false);
const searchVisible = ref(false);
const mobilePage = ref(1);
const mobilePageSize = 10;

const sourceNames = {
  hdhive: "HDHive",
  pansou: "PanSou",
  manual: "手动添加",
  unknown: "未知",
};
const statusOptions = ["处理中", "下载中", "成功", "失败"];
const taskTypeOptions = [
  {title: "跨盘", value: "cross_transfer"},
  {title: "洗版", value: "upgrade"},
];
const pageSizes = [
  {value: 10, title: "10"},
  {value: 20, title: "20"},
  {value: 50, title: "50"},
  {value: -1, title: "全部"},
];
const headers = [
  {title: "媒体", key: "media", sortable: false, width: "26%"},
  {title: "类型", key: "resource_types", sortable: false, width: "14%"},
  {title: "来源", key: "sources", sortable: false, width: "12%"},
  {title: "汇总", key: "summary", sortable: false, width: "18%"},
  {title: "最新时间", key: "latest_time", width: 140},
  {title: "", key: "actions", sortable: false, width: 104},
  {title: "", key: "data-table-expand", width: 48},
];

const resourceTypeOptions = computed(() =>
    uniqueOptions(props.items.map((item) => resourceType(item))).map((value) => ({
      title: resourceTypeLabel(value),
      value,
    })),
);
const sourceOptions = computed(() =>
    uniqueOptions(props.items.map((item) => normalizeSource(item.source))).map(
        (value) => ({title: sourceLabel(value), value}),
    ),
);
const filteredRecords = computed(() => {
  const query = String(keyword.value || "")
      .trim()
      .toLowerCase();
  return props.items.filter((item) => {
    const type = resourceType(item);
    const source = normalizeSource(item.source);
    if (
        selectedResourceTypes.value.length &&
        !selectedResourceTypes.value.includes(type)
    )
      return false;
    if (selectedSources.value.length && !selectedSources.value.includes(source))
      return false;
    if (
        selectedTaskTypes.value.length &&
        !selectedTaskTypes.value.some((value) =>
            (item.task_types || []).includes(value),
        )
    )
      return false;
    if (
        selectedStatuses.value.length &&
        !selectedStatuses.value.includes(item.status)
    )
      return false;
    if (!query) return true;
    return [
      item.title,
      item.file_name,
      item.type,
      item.tmdb_id,
      resourceTypeLabel(type),
      sourceLabel(source),
    ].some((value) =>
        String(value || "")
            .toLowerCase()
            .includes(query),
    );
  });
});

function notificationSummaryTitle(item) {
  const title = String(item?.title || "未知媒体").trim();
  const year = String(item?.year || "").trim();
  return year ? title + "（" + year + "）" : title;
}

const groupedItems = computed(() => {
  const groups = new Map();
  for (const record of filteredRecords.value) {
    const mediaType = record.type || "未知类型";
    const groupKey = record.tmdb_id
        ? `tmdb:${mediaType}:${record.tmdb_id}`
        : `legacy:${mediaType}:${record.title || ""}:${record.year || ""}`;
    if (!groups.has(groupKey)) {
      groups.set(groupKey, {
        group_key: groupKey,
        tmdb_id: record.tmdb_id || null,
        title: record.title || "未知媒体",
        year: record.year || "",
        type: mediaType,
        latest_time: record.time || "",
        records: [],
        resource_types: [],
        sources: [],
        success_count: 0,
        pending_count: 0,
        failed_count: 0,
        total_size: 0,
      });
    }
    const group = groups.get(groupKey);
    group.records.push(record);
    if (String(record.time || "") > group.latest_time)
      group.latest_time = record.time;
    if (record.status === "成功") group.success_count += 1;
    else if (["处理中", "下载中"].includes(record.status))
      group.pending_count += 1;
    else group.failed_count += 1;
    group.total_size += Number(record.file_size || 0);
  }
  return [...groups.values()]
      .map((group) => ({
        ...group,
        records: [...group.records].sort(compareTimeDesc),
        resource_types: uniqueOptions(
            group.records.map((record) => resourceType(record)),
        ),
        sources: uniqueOptions(
            group.records.map((record) => normalizeSource(record.source)),
        ),
        seasons: historySeasons(group.records),
        season_label: formatSeasonLabel(historySeasons(group.records)),
        source_items: uniqueOptions(
            group.records.map((record) => normalizeSource(record.source)),
        ).map((value) => {
          const record = group.records.find(
              (item) => normalizeSource(item.source) === value && sourceLink(item),
          );
          return {value, url: record ? sourceLink(record) : ""};
        }),
        notification_record:
            [...group.records]
                .filter(canNotifyRecord)
                .sort(compareTimeDesc)[0] || null,
        selectable: group.records.length > 0,
        deletable:
            group.records.length > 0 && group.records.every(canDeleteRecord),
      }))
      .sort((left, right) => compareTimeDesc(left, right, "latest_time"));
});
const activeFilterCount = computed(
    () =>
        selectedResourceTypes.value.length +
        selectedSources.value.length +
        selectedTaskTypes.value.length +
        selectedStatuses.value.length,
);
const mobileTotalPages = computed(() =>
    Math.max(1, Math.ceil(groupedItems.value.length / mobilePageSize)),
);
const mobilePagedItems = computed(() => {
  const start = (mobilePage.value - 1) * mobilePageSize;
  return groupedItems.value.slice(start, start + mobilePageSize);
});
const selectedGroups = computed(() => {
  const keys = new Set(selectedGroupKeys.value);
  return groupedItems.value.filter(
      (group) => keys.has(group.group_key),
  );
});
const deletableSelectedGroups = computed(() =>
    selectedGroups.value.filter((group) => group.deletable),
);
const groupedItemKeys = computed(
    () => new Set(groupedItems.value.map((group) => group.group_key)),
);

watch(
    [keyword, selectedResourceTypes, selectedSources, selectedTaskTypes, selectedStatuses],
    () => {
      mobilePage.value = 1;
      expanded.value = [];
      selectedGroupKeys.value = [];
    },
    {deep: true},
);

watch(groupedItemKeys, (keys) => {
  selectedGroupKeys.value = selectedGroupKeys.value.filter((key) =>
      keys.has(key),
  );
});

watch(
    selectedGroups,
    (groups) => {
      emit("selection-change", {
        groupCount: groups.length,
        subscribeIds: uniqueOptions(
            groups.flatMap((group) =>
                group.records.map((record) => Number(record.subscribe_id || 0)),
            ).filter((value) => value > 0),
        ),
        targets: groups.map((group) => ({
          tmdb_id: Number(group.tmdb_id || 0),
          media_type: group.type || "",
          title: group.title || "",
          year: group.year || "",
          seasons: group.seasons || [],
        })),
      });
    },
    {immediate: true},
);

function compareTimeDesc(left, right, key = "time") {
  return String(right[key] || "").localeCompare(String(left[key] || ""));
}

function uniqueOptions(values) {
  return [...new Set(values.filter(Boolean))];
}

function historySeasons(records) {
  return [
    ...new Set(
        records
            .filter((record) => record.type !== "电影")
            .map((record) => Number(record.season))
            .filter((season) => Number.isInteger(season) && season > 0),
    ),
  ].sort((left, right) => left - right);
}

function formatSeasonLabel(seasons) {
  if (!seasons.length) return "";
  return seasons.map((season) => `S${pad(season)}`).join("、");
}

function normalizeSource(value) {
  const normalized =
      String(value || "unknown")
          .trim()
          .toLowerCase() || "unknown";
  return ["manual", "手动添加", "手动资源"].includes(normalized)
      ? "manual"
      : normalized;
}

function sourceLabel(value) {
  const normalized = normalizeSource(value);
  return sourceNames[normalized] || normalized;
}

function resourceType(item) {
  if (typeof item === "string") return item.trim().toLowerCase();
  const configured = String(item.resource_type || "")
      .trim()
      .toLowerCase();
  if (configured) return configured;
  const link = String(item.share_url || "").toLowerCase();
  if (link.startsWith("ed2k://")) return "ed2k";
  if (link.startsWith("magnet:?")) return "magnet";
  return "115";
}

function resourceTypeLabel(value) {
  const normalized = resourceType(value);
  return {
    "115": "115网盘",
    "123": "123网盘",
    quark: "夸克网盘",
    guangya: "光鸭网盘",
    tianyi: "天翼云盘",
    alipan: "阿里云盘",
    aliyun: "阿里云盘",
    ed2k: "ED2K",
    magnet: "Magnet",
  }[normalized] || normalized.toUpperCase();
}

function resourceTypeColor(value) {
  const normalized = resourceType(value);
  return normalized === "ed2k"
      ? "warning"
      : normalized === "magnet"
          ? "purple"
          : "info";
}

function sourceLink(item) {
  const link = String(item.source_url || item.share_url || "").trim();
  return /^(https?:|ed2k:|magnet:)/i.test(link) ? link : "";
}

function mediaDetailLink(item) {
  if (!item?.tmdb_id) return "";
  return [
    `#/media?mediaid=tmdb:${encodeURIComponent(String(item.tmdb_id))}`,
    `title=${encodeURIComponent(item.title || "")}`,
    `year=${encodeURIComponent(item.year || "")}`,
    `type=${encodeURIComponent(item.type || "")}`,
  ].join("&");
}

function openMediaDetail(item) {
  const link = mediaDetailLink(item);
  if (link) emit("open-media", link);
}

function playItemId(item) {
  return String(props.embyPlayItems?.[item?.group_key] || "");
}

function statusColor(status) {
  return status === "成功"
      ? "success"
      : ["处理中", "下载中"].includes(status)
          ? "info"
          : "error";
}

function formatSize(bytes) {
  let value = Number(bytes || 0);
  if (!value) return "-";
  for (const unit of ["B", "KB", "MB", "GB", "TB"]) {
    if (value < 1024 || unit === "TB")
      return `${value.toFixed(value >= 100 ? 0 : 1)} ${unit}`;
    value /= 1024;
  }
  return "-";
}

function toggleExpanded(event, row) {
  if (event?.target?.closest("a, button, input, .v-selection-control")) return;
  const item = row?.item?.raw || row?.item || row;
  const key = item?.group_key;
  if (!key) return;
  expanded.value = expanded.value.includes(key)
      ? expanded.value.filter((value) => value !== key)
      : [...expanded.value, key];
}

function recordKey(record, index) {
  return [
    record.time,
    record.file_name,
    record.season,
    record.episode,
    index,
  ].join(":");
}

function episodeLabel(record) {
  const season = `S${pad(record.season)}`;
  const targetEpisodes = Array.isArray(record.target_episodes)
      ? record.target_episodes
      : String(record.target_episodes || "")
          .split(/[^0-9]+/)
          .filter(Boolean);
  const episodes = [
    ...new Set(
        targetEpisodes
            .map((value) => Number(value))
            .filter((value) => Number.isInteger(value) && value > 0),
    ),
  ].sort((left, right) => left - right);
  if (episodes.length > 1)
    return `${season}E${pad(episodes[0])}-E${pad(episodes[episodes.length - 1])}`;
  const episode = episodes[0] || Number(record.episode || 0);
  return episode > 0 ? `${season}E${pad(episode)}` : season;
}

function historyRetryKey(record) {
  return [record.time, record.share_url, record.file_name].join("|");
}

function historyUpgradeGroupKey(group) {
  return `group:${group.group_key}`;
}

function historyUpgradeRecordKey(record) {
  return `record:${historyRetryKey(record)}`;
}

function canUpgradeRecord(record) {
  return (
      record?.type !== "电影" &&
      record?.status === "成功" &&
      !record?.finalize_key &&
      Number(record?.episode || 0) > 0
  );
}

function canUpgradeGroup(group) {
  return (group?.records || []).some(
      (record) => record?.status === "成功" && !record?.finalize_key,
  );
}

function upgradeRecord(record) {
  if (!canUpgradeRecord(record)) return;
  emit("upgrade", {
    scope: "record",
    records: [record],
    key: historyUpgradeRecordKey(record),
  });
}

function upgradeGroup(group) {
  if (!canUpgradeGroup(group)) return;
  emit("upgrade", {
    scope: "group",
    media: {
      title: group.title,
      year: group.year,
      tmdb_id: group.tmdb_id,
      media_type: group.type === "电影" ? "movie" : "tv",
      season: group.seasons.length === 1 ? group.seasons[0] : null,
    },
    records: group.records.filter(
        (record) => record?.status === "成功" && !record?.finalize_key,
    ),
    key: historyUpgradeGroupKey(group),
  });
}

function isGroupSelected(group) {
  return selectedGroupKeys.value.includes(group.group_key);
}

function selectGroup(group, selected) {
  if (!group.selectable) return;
  const keys = new Set(selectedGroupKeys.value);
  if (selected) keys.add(group.group_key);
  else keys.delete(group.group_key);
  selectedGroupKeys.value = [...keys];
}

function deleteSelected() {
  if (!deletableSelectedGroups.value.length) return;
  emit("delete-groups", {
    groupCount: deletableSelectedGroups.value.length,
    records: deletableSelectedGroups.value.flatMap((group) => group.records),
  });
}

function clearFilters() {
  selectedResourceTypes.value = [];
  selectedSources.value = [];
  selectedTaskTypes.value = [];
  selectedStatuses.value = [];
}

function canRetryRecord(record) {
  return Boolean(record?.can_retry);
}

function retryTitle(record) {
  return record?.retry_title || "当前记录无需重试";
}

function canDeleteRecord(record) {
  return ["成功", "失败"].includes(record?.status) || Boolean(record?.finalize_key);
}

function deleteTitle(record) {
  if (!canDeleteRecord(record)) return "当前记录不能删除";
  return record?.finalize_key ? "删除此条后处理记录" : "删除此条历史记录";
}

function canNotifyRecord(record) {
  return record?.status === "成功" && !record?.finalize_key;
}

const pad = (value) => String(Number(value || 0)).padStart(2, "0");
</script>

<style scoped>
.history-table-root {
  display: flex;
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
  background: rgb(var(--v-theme-surface));
}

.history-toolbar {
  flex: 0 0 auto;
  background: rgb(var(--v-theme-surface));
}

.history-toolbar-head {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 4px;
  padding: 6px 8px;
}

.history-filter-trigger {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
}

.history-search {
  width: min(260px, 28vw);
  min-width: 180px;
  flex: 0 1 260px;
}

.history-search-trigger {
  display: flex;
  align-items: center;
}

.history-search-menu {
  width: min(340px, calc(100vw - 24px));
}

.history-filter-menu {
  width: min(420px, calc(100vw - 24px));
  max-height: min(520px, calc(100vh - 32px));
  overflow-y: auto;
}

.history-filter-menu-title {
  display: flex;
  align-items: center;
  padding: 8px 12px;
  font-size: 0.9rem;
  font-weight: 600;
}

.history-filter-options {
  padding: 10px 12px !important;
}

.history-filter-group + .history-filter-group {
  margin-top: 8px;
}

.history-filter-label {
  display: block;
  margin-bottom: 2px;
  color: rgba(var(--v-theme-on-surface), 0.78);
  font-size: 0.8rem;
  font-weight: 600;
}

.history-filter-group :deep(.v-chip-group) {
  margin: 0;
}

.history-filter-group :deep(.v-slide-group__content) {
  gap: 4px;
  padding: 0;
}

.history-actions {
  display: flex;
  align-items: center;
  flex: 0 0 auto;
  gap: 2px;
}

.history-actions :deep(.v-btn) {
  white-space: nowrap;
}

.history-action-icon {
  margin-right: 6px;
}

.history-toolbar :deep(.v-field) {
  --v-input-control-height: 36px;
  --v-field-input-padding-top: 4px;
  --v-field-input-padding-bottom: 4px;
}

.history-table > :deep(.v-table__wrapper) {
  flex: 1 1 auto;
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.history-table {
  display: flex;
  flex: 1 1 auto;
  min-height: 0;
  flex-direction: column;
}

.history-empty {
  display: flex;
  flex: 1 1 auto;
  min-height: 0;
  align-items: center;
  justify-content: center;
  text-align: center;
}

.history-table :deep(.v-data-table-footer) {
  justify-content: flex-end;
  min-height: 56px;
  padding: 6px 16px;
  flex: 0 0 auto;
  background: rgb(var(--v-theme-surface));
  border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.history-table :deep(table) {
  width: 100%;
  min-width: 0;
  table-layout: fixed;
}

.summary-counts {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px;
  font-size: 0.75rem;
  font-weight: 600;
}

.summary-actions,
.history-mobile-summary-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 2px;
}

.summary-separator {
  color: rgba(var(--v-theme-on-surface), 0.38);
  font-weight: 400;
}

.history-table :deep(thead tr),
.history-table :deep(thead th) {
  height: 34px !important;
}

.history-table :deep(tbody > tr:not(.detail-row)) {
  cursor: pointer;
}

.history-table :deep(thead th) {
  padding-top: 0 !important;
  padding-bottom: 0 !important;
}

.summary-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.source-summary {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
  min-width: 0;
}

.source-summary-links {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px 8px;
  min-width: 0;
}

.mixed-resource-types {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.detail-row > td {
  background: rgb(var(--v-theme-surface));
}

.detail-row > td::after {
  background: transparent !important;
  opacity: 0 !important;
}

.detail-table :deep(table) {
  width: 100%;
  min-width: 0;
  table-layout: fixed;
}

.detail-table :deep(.v-table__wrapper) {
  overflow: visible !important;
}

.detail-table :deep(tbody tr:hover > td) {
  background: transparent !important;
}

.detail-table :deep(tbody tr > td::after) {
  background: transparent !important;
  opacity: 0 !important;
}

.detail-table :deep(th) {
  height: 32px !important;
  font-size: 0.75rem;
  color: rgb(var(--v-theme-on-surface-variant));
}

.detail-table :deep(td:first-child) {
  width: 28%;
  min-width: 0;
}

.action-column {
  width: 128px;
  min-width: 128px;
  text-align: center;
  white-space: nowrap;
}

.action-buttons {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 2px;
  width: 100%;
}

.latest-time {
  display: inline-block;
  min-width: 0;
  line-height: 1.35;
  white-space: normal;
}

.episode-label {
  font-weight: 600;
}

.file-name {
  max-width: 260px;
  overflow-wrap: anywhere;
}

.media-cell {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 2px;
}

.media-title {
  min-width: 0;
  overflow-wrap: anywhere;
}

.media-link {
  flex: 0 0 auto;
}

.history-mobile-list {
  display: flex;
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  overflow: hidden;
  border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.history-mobile-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
}

.history-mobile-empty {
  display: flex;
  min-height: 100%;
  align-items: center;
  justify-content: center;
  padding: 32px 16px;
  text-align: center;
}

.history-mobile-panels {
  border-radius: 0;
}

.history-mobile-panel {
  background: rgb(var(--v-theme-surface));
}

.history-mobile-panel + .history-mobile-panel {
  border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.history-mobile-title {
  min-height: 0 !important;
  padding: 8px 10px !important;
}

.history-mobile-summary {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  column-gap: 8px;
  min-width: 0;
  width: auto;
  flex: 1 1 0;
  overflow: hidden;
}

.history-mobile-summary-footer {
  display: grid;
  grid-column: 1 / -1;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: end;
  min-width: 0;
  width: 100%;
  margin-top: 4px;
  gap: 8px;
}

.history-mobile-summary-time {
  justify-self: end;
  white-space: nowrap;
}

.history-group-selected {
  box-shadow: inset 3px 0 0 rgb(var(--v-theme-primary));
}

.history-mobile-media-line,
.history-mobile-tags,
.history-mobile-record-head,
.history-mobile-record-meta {
  display: flex;
  align-items: center;
  min-width: 0;
  gap: 6px;
}

.history-mobile-media-line {
  padding-right: 4px;
}

.history-mobile-summary-actions {
  flex: 0 0 auto;
  margin-left: auto;
}

.history-mobile-media-title {
  display: block;
  min-width: 0;
  flex: 1 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.history-mobile-record-meta {
  flex-wrap: wrap;
  margin-top: 4px;
}

.history-mobile-tags {
  max-width: 100%;
  flex-wrap: nowrap;
  overflow: hidden;
}

.history-mobile-tags > :deep(.v-chip),
.history-mobile-tags > span {
  flex: 0 0 auto;
  white-space: nowrap;
}

.history-mobile-record-footer {
  display: flex;
  align-items: flex-end;
  flex-wrap: wrap;
  min-width: 0;
  gap: 8px;
}

.history-mobile-record-meta {
  flex: 1 1 0;
  overflow: hidden;
}

.history-mobile-record-time {
  flex: 0 0 auto;
  margin-left: auto;
  white-space: nowrap;
}

.history-mobile-record-meta > * + *::before {
  margin-right: 6px;
  color: rgba(var(--v-theme-on-surface), 0.38);
  content: "·";
}

.history-mobile-details :deep(.v-expansion-panel-text__wrapper) {
  padding: 0 !important;
}

.history-mobile-record {
  padding: 7px 10px;
  background: rgba(var(--v-theme-on-surface), 0.025);
  border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.history-mobile-actions {
  flex: 0 0 auto;
  width: auto;
  justify-content: flex-end;
}

.history-mobile-file {
  margin-top: 2px;
  line-height: 1.35;
  overflow-wrap: anywhere;
}

.history-mobile-pagination {
  flex: 0 0 auto;
  padding: 8px 4px;
  background: rgb(var(--v-theme-surface));
  border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

@media (min-width: 601px) {
  .history-table {
    font-size: 0.875rem;
  }

  .history-table :deep(thead th) {
    padding: 0 12px !important;
    font-size: 0.8125rem;
  }

  .history-table :deep(tbody > tr:not(.detail-row)) {
    height: 56px !important;
  }

  .history-table :deep(tbody > tr:not(.detail-row) > td) {
    padding: 10px 12px !important;
    overflow: hidden;
  }

  .detail-table :deep(td) {
    padding: 8px 10px !important;
    font-size: 0.8125rem;
  }
}

@media (max-width: 600px) {
  .history-toolbar-head {
    display: flex;
    align-items: center;
    gap: 2px;
    padding: 4px 8px;
  }

  .history-toolbar-head > .v-spacer {
    display: block;
    min-width: 0;
    flex: 1 1 auto;
  }

  .history-search {
    width: auto;
    min-width: 0;
    flex: none;
  }

  .history-actions {
    display: flex;
    flex: 0 0 auto;
    gap: 2px;
    padding: 0;
  }

  .history-actions :deep(.v-btn) {
    min-width: 32px;
    width: 32px;
    height: 32px;
    padding: 0;
  }

  .history-action-icon {
    margin: 0;
  }

  .history-action-label {
    display: none;
  }

  .delete-selected-button {
    min-width: 32px !important;
    width: 32px;
    height: 32px;
    flex: 0 0 32px;
  }

  .history-mobile-title :deep(.v-selection-control) {
    align-self: flex-start;
    flex: 0 0 32px;
    margin-top: -2px;
  }

  .history-toolbar :deep(.v-field) {
    --v-input-control-height: 44px;
    min-height: 44px;
  }

  .history-search :deep(.v-input),
  .history-search :deep(.v-input__control),
  .history-search :deep(.v-field),
  .history-search :deep(.v-field__input) {
    min-height: 32px !important;
    height: 32px !important;
    max-height: 32px !important;
  }

  .history-search :deep(.v-field) {
    padding-inline: 8px;
  }

  .history-search :deep(.v-field__input) {
    padding-top: 0 !important;
    padding-bottom: 0 !important;
  }

  .history-search :deep(.v-input__details) {
    display: none;
  }

  .history-mobile-scroll {
    scrollbar-width: none;
    touch-action: pan-y;
  }

  .history-mobile-scroll::-webkit-scrollbar {
    display: none;
  }

  .history-filter-group,
  .history-filter-group + .history-filter-group {
    display: flex;
    align-items: center;
    min-width: 0;
    margin-top: 4px;
    gap: 6px;
  }

  .history-filter-label {
    flex: 0 0 58px;
    margin: 0;
    color: rgba(var(--v-theme-on-surface), 0.82) !important;
    font-size: 0.8rem;
    font-weight: 700;
    opacity: 1 !important;
  }

  .history-filter-group :deep(.v-chip-group) {
    min-width: 0;
    flex: 1 1 auto;
    overflow-x: auto;
  }

  .history-filter-group :deep(.v-slide-group__content) {
    flex-wrap: nowrap;
    min-width: max-content;
  }
}

.source-link {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  color: rgb(var(--v-theme-primary));
  text-decoration: none;
}

.source-link:hover {
  text-decoration: underline;
}

.media-link {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: rgb(var(--v-theme-primary));
  text-decoration: none;
}

.media-link:hover {
  text-decoration: none;
}
</style>
