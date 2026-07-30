<template>
  <div class="runtime-panel">
    <div v-if="tasks.length" class="runtime-header">
      <div class="d-flex align-center ga-2 min-width-0">
        <v-icon icon="mdi-format-list-checks" color="primary" size="small"/>
        <v-chip size="x-small" variant="tonal">
          共 {{ tasks.length }} · 运行 {{ runningCount }} · 等待
          {{ queuedCount }} · 后处理 {{ postprocessingCount }}
        </v-chip>
      </div>
    </div>

    <div v-if="tasks.length" class="task-list">
      <div v-for="task in tasks" :key="task.id" class="task-row">
        <v-icon
            :icon="
            task.media_type === '电影'
              ? 'mdi-movie-outline'
              : 'mdi-television-classic'
          "
            :color="taskColor(task.status)"
            size="small"
        />
        <div class="task-content">
          <div class="task-line">
            <span class="task-name text-body-2 font-weight-medium">{{
                task.title
              }}</span>
            <span v-if="task.season" class="text-caption text-medium-emphasis">
              S{{ String(task.season).padStart(2, "0") }}
            </span>
            <v-chip
                :color="taskColor(task.status)"
                size="x-small"
                variant="tonal"
            >
              {{ taskStatus(task.status) }}
            </v-chip>
          </div>
          <div class="task-meta">
            <span class="task-phase text-caption text-medium-emphasis">{{
                task.status === "failed"
                    ? task.message || task.phase || "处理失败"
                    : task.phase || "等待调度"
              }}</span>
          </div>
          <v-progress-linear
              class="task-progress"
              :model-value="Number(task.progress || 0)"
              :indeterminate="['running', 'stopping', 'postprocessing'].includes(task.status)"
              :color="taskColor(task.status)"
              height="5"
              rounded
          />
        </div>
        <v-btn
            v-if="canStop(task.status)"
            icon="mdi-stop-circle-outline"
            color="warning"
            variant="text"
            size="x-small"
            :loading="task.status === 'stopping'"
            title="停止此任务"
            @click="emit('stop-task', task.id)"
        />
        <v-btn
            v-else-if="task.status === 'postprocessing'"
            icon="mdi-folder-sync-outline"
            color="primary"
            variant="text"
            size="x-small"
            title="管理后处理任务"
            @click="emit('manage-postprocessing')"
        />
        <v-icon
            v-else
            :icon="resultIcon(task.status)"
            :color="taskColor(task.status)"
            size="small"
        />
      </div>
    </div>
    <div v-else class="idle-state text-medium-emphasis">
      <v-progress-circular
          v-if="active"
          indeterminate
          size="44"
          width="3"
          color="primary"
      />
      <v-icon
          v-else
          icon="mdi-check-circle-outline"
          color="success"
          size="44"
      />
      <div class="text-subtitle-2 font-weight-medium mt-3">
        {{ active ? runtime.task || "正在准备订阅任务" : "当前没有订阅任务" }}
      </div>
      <div class="text-caption mt-1">
        {{
          active ? "正在加载订阅任务列表" : runtime.task || "等待下一次订阅搜索"
        }}
      </div>
    </div>
  </div>
</template>
<script setup>
import {computed} from "vue";

const props = defineProps({
  runtime: {type: Object, required: true},
  active: Boolean,
});
const emit = defineEmits(["stop-task", "manage-postprocessing"]);
const active = computed(() => props.active);
const tasks = computed(() =>
    (props.runtime.tasks || []).filter((task) =>
        ["queued", "running", "stopping", "postprocessing"].includes(task.status),
    ),
);
const runningCount = computed(
    () =>
        tasks.value.filter((task) => ["running", "stopping"].includes(task.status))
            .length,
);
const queuedCount = computed(
    () => tasks.value.filter((task) => task.status === "queued").length,
);
const postprocessingCount = computed(
    () => tasks.value.filter((task) => task.status === "postprocessing").length,
);

function canStop(status) {
  return ["queued", "running", "stopping"].includes(status);
}

function taskStatus(status) {
  return (
      {
        queued: "等待",
        running: "运行中",
        stopping: "停止中",
        postprocessing: "后处理中",
        completed: "完成",
        failed: "失败",
        stopped: "已停止",
      }[status] || "未知"
  );
}

function taskColor(status) {
  return (
      {
        queued: "secondary",
        running: "info",
        stopping: "warning",
        postprocessing: "primary",
        completed: "success",
        failed: "error",
        stopped: "warning",
      }[status] || "secondary"
  );
}

function resultIcon(status) {
  return status === "postprocessing"
      ? "mdi-cog-sync-outline"
      : status === "completed"
      ? "mdi-check-circle"
      : status === "failed"
          ? "mdi-alert-circle"
          : "mdi-stop-circle";
}
</script>

<style scoped>
.runtime-panel {
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: rgb(var(--v-theme-surface));
}

.runtime-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 12px;
}

.min-width-0,
.task-content {
  min-width: 0;
}

.task-list {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  border-top: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.task-row {
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr) 30px;
  align-items: center;
  gap: 8px;
  min-height: 54px;
  padding: 7px 12px;
}

.task-row + .task-row {
  border-top: 1px solid rgba(var(--v-border-color), 0.08);
}

.task-line,
.task-meta {
  display: flex;
  align-items: center;
  gap: 7px;
}

.task-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.runtime-header > .d-flex {
  min-width: 0;
}

.task-meta {
  margin-top: 4px;
  justify-content: space-between;
}

.task-phase {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-progress {
  margin-top: 5px;
}

.idle-state {
  display: flex;
  flex: 1 1 auto;
  min-height: 220px;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 24px;
  text-align: center;
}

@media (max-width: 600px) {
  .runtime-header > .d-flex {
    width: 100%;
    white-space: nowrap;
  }

  .task-line {
    flex-wrap: wrap;
  }
}
</style>
