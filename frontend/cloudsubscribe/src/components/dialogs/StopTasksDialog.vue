<template>
  <v-dialog :model-value="modelValue" max-width="420" persistent>
    <v-card rounded="lg">
      <v-card-title class="text-subtitle-1">
        {{ task ? "停止任务" : "停止全部任务" }}
      </v-card-title>
      <v-card-text>
        <div v-if="task">确认停止“{{ task.title || "此任务" }}”？</div>
        <div v-else-if="taskCount > 0">
          确认停止当前 {{ taskCount }} 个等待或运行中的任务？
        </div>
        <div v-else>确认停止当前订阅任务？</div>
        <v-alert type="warning" variant="tonal" density="compact" class="mt-3">
          任务将在安全节点停止，已完成的处理不会回退。
        </v-alert>
      </v-card-text>
      <v-card-actions>
        <v-spacer/>
        <v-btn variant="text" :disabled="loading" @click="close">取消</v-btn>
        <v-btn color="warning" :loading="loading" @click="emit('confirm')">
          确认停止
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup>
const props = defineProps({
  modelValue: Boolean,
  task: {type: Object, default: null},
  taskCount: {type: Number, default: 0},
  loading: Boolean,
});
const emit = defineEmits(["update:modelValue", "confirm"]);

function close() {
  if (!props.loading) emit("update:modelValue", false);
}
</script>
