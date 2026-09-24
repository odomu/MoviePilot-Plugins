<template>
  <v-dialog v-model="visible" max-width="360" class="reward-dialog">
    <v-card class="reward-card text-center pa-5" elevation="8" rounded="xl">
      <div class="d-flex align-center justify-space-between mb-2">
        <div class="d-flex align-center ga-2">
          <v-avatar color="red-lighten-5" size="32">
            <v-icon icon="mdi-heart" color="red-accent-2" size="18" />
          </v-avatar>
          <span class="text-subtitle-1 font-weight-bold">赞赏支持</span>
        </div>
        <v-btn icon="mdi-close" size="x-small" variant="text" @click="visible = false" />
      </div>
      <div class="reward-img-container mx-auto mb-3">
        <v-img
          :src="rewardImageSrc"
          alt="微信赞赏码"
          width="250"
          height="250"
          aspect-ratio="1"
          class="reward-qrcode rounded-lg elevation-2 mx-auto" />
      </div>
      <div class="text-caption text-medium-emphasis">
        微信扫一扫 · 感谢您的支持与鼓励 ❤️
      </div>
    </v-card>
  </v-dialog>
</template>

<script setup>
import {computed} from "vue";
import {REWARD_IMAGE_SRC} from "../../assets/rewardImage.js";

const rewardImageSrc = REWARD_IMAGE_SRC;

const props = defineProps({
  modelValue: {
    type: Boolean,
    default: false,
  },
});

const emit = defineEmits(["update:modelValue"]);

const visible = computed({
  get: () => props.modelValue,
  set: (val) => emit("update:modelValue", val),
});
</script>

<style scoped>
.reward-card {
  background-color: rgb(var(--v-theme-surface)) !important;
  border: 1px solid rgba(var(--v-border-color), 0.12);
}

.reward-img-container {
  display: flex;
  justify-content: center;
  align-items: center;
}

.reward-qrcode {
  background-color: #ffffff !important;
  padding: 8px;
  border: 1px solid rgba(var(--v-border-color), 0.15);
}
</style>
