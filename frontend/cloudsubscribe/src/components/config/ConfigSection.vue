<template>
  <div class="config-tab-content">
    <template v-for="(group, index) in visibleGroups" :key="group.title">
      <v-tabs
          v-if="section.subtabs?.length && index === leadingGroupCount"
          v-model="activeSubtab"
          color="primary"
          density="compact"
          show-arrows
          class="section-tabs mb-4"
      >
        <v-tab v-for="tab in section.subtabs" :key="tab.value" :value="tab.value">
          <v-icon :icon="tab.icon" size="small" class="mr-2"/>
          {{ tab.title }}
        </v-tab>
      </v-tabs>
      <section class="config-group">
        <div v-if="!group.hideHeading" class="group-heading">
          <v-icon :icon="group.icon" color="primary" size="small"/>
          <div>
            <div class="text-subtitle-2 font-weight-medium">
              {{ group.title }}
            </div>
            <div v-if="group.hint" class="text-caption text-medium-emphasis">
              {{ group.hint }}
            </div>
          </div>
        </div>

        <v-row dense>
          <template v-for="field in group.fields" :key="field.key">
            <v-col
                v-if="!field.show || field.show(config)"
                cols="12"
                :md="field.cols || 6"
                class="config-field-col"
            >
              <AccountInfo
                  v-if="field.type === 'account'"
                  :account="field.data"
                  :compact="Boolean(field.compact)"
                  :loading="refreshingAccount === field.accountKey"
                  :refreshable="Boolean(field.accountKey)"
                  :disabled="Boolean(refreshingAccount) && refreshingAccount !== field.accountKey"
                  @refresh="emit('refresh-account', field.accountKey)"
              />
              <v-alert
                  v-else-if="field.type === 'info'"
                  type="info"
                  variant="tonal"
                  density="compact"
              >
                <div class="text-body-2 font-weight-medium mb-1">
                  {{ field.label }}
                </div>
                <div
                    v-for="line in field.lines || []"
                    :key="line"
                    class="text-caption mb-1"
                >
                  • {{ line }}
                  </div>
              </v-alert>
              <v-btn
                  v-else-if="field.type === 'test-source'"
                  color="primary"
                  variant="tonal"
                  prepend-icon="mdi-flask-outline"
                  :loading="testingSource === field.source"
                  :disabled="Boolean(testingSource) && testingSource !== field.source"
                  @click="emit('test-source', field.source)"
              >
                {{ field.label }}
              </v-btn>
              <v-switch
                  v-else-if="field.type === 'switch'"
                  v-model="config[field.key]"
                  :label="field.label"
                  :hint="field.hint"
                  :persistent-hint="Boolean(field.hint)"
                  :disabled="Boolean(field.disabled?.(config))"
                  color="success"
                  density="compact"
                  hide-details="auto"
                  class="config-switch"
              />
              <v-select
                  v-else-if="field.type === 'select'"
                  v-model="config[field.key]"
                  :label="field.label"
                  :items="field.items"
                  :multiple="field.multiple"
                  :hint="field.hint"
                  :persistent-hint="Boolean(field.hint)"
                  :disabled="Boolean(field.disabled?.(config))"
                  chips
                  closable-chips
                  density="compact"
                  variant="outlined"
                  hide-details="auto"
              />
              <v-textarea
                  v-else-if="field.type === 'textarea'"
                  v-model="config[field.key]"
                  :label="field.label"
                  :hint="field.hint"
                  :placeholder="field.placeholder"
                  :persistent-hint="Boolean(field.hint)"
                  :disabled="Boolean(field.disabled?.(config))"
                  auto-grow
                  rows="2"
                  density="compact"
                  variant="outlined"
                  hide-details="auto"
              />
              <v-combobox
                  v-else-if="field.type === 'combobox'"
                  v-model="config[field.key]"
                  :label="field.label"
                  :items="field.items || []"
                  :hint="field.hint"
                  :persistent-hint="Boolean(field.hint)"
                  multiple
                  chips
                  closable-chips
                  clearable
                  density="compact"
                  variant="outlined"
                  hide-details="auto"
              />
              <v-text-field
                  v-else-if="field.type === 'cloud-directory'"
                  v-model="config[field.key]"
                  :label="field.label"
                  :hint="field.hint"
                  :placeholder="field.placeholder"
                  :persistent-hint="Boolean(field.hint)"
                  density="compact"
                  variant="outlined"
                  hide-details="auto"
              >
                <template #append-inner>
                  <v-btn
                      icon="mdi-folder-search-outline"
                      variant="text"
                      color="primary"
                      size="small"
                      title="浏览网盘目录"
                      @click="emit('browse-directory', field.key, field.driveProvider)"
                  />
                </template>
              </v-text-field>
              <v-text-field
                  v-else-if="field.type === 'number'"
                  v-model.number="config[field.key]"
                  :label="field.label"
                  type="number"
                  :min="field.min"
                  :max="field.max"
                  :hint="field.hint"
                  :placeholder="field.placeholder"
                  :clearable="field.clearable"
                  :persistent-hint="Boolean(field.hint)"
                  density="compact"
                  variant="outlined"
                  hide-details="auto"
              />
              <v-text-field
                  v-else
                  v-model="config[field.key]"
                  :label="field.label"
                  :type="field.type || 'text'"
                  :hint="field.hint"
                  :placeholder="field.placeholder"
                  :persistent-hint="Boolean(field.hint)"
                  :disabled="Boolean(field.disabled?.(config))"
                  density="compact"
                  variant="outlined"
                  hide-details="auto"
              >
                <template v-if="field.scanProvider" #append-inner>
                  <v-btn
                      icon="mdi-qrcode-scan"
                      variant="text"
                      color="primary"
                      size="small"
                      title="扫码登录"
                      @click="emit('scan', field.scanProvider)"
                  />
                </template>
              </v-text-field>
            </v-col>
          </template>
        </v-row>
        <v-divider
            v-if="index < visibleGroups.length - 1"
            class="group-divider"
        />
      </section>
    </template>
  </div>
</template>

<script setup>
import {computed, ref} from "vue";
import AccountInfo from "./AccountInfo.vue";

const props = defineProps({
  section: {type: Object, required: true},
  config: {type: Object, required: true},
  refreshingAccount: {type: String, default: ""},
  testingSource: {type: String, default: ""},
});
const emit = defineEmits([
  "scan",
  "browse-directory",
  "test-source",
  "refresh-account",
]);
const activeSubtab = ref(props.section.subtabs?.[0]?.value || "");
const availableGroups = computed(() => props.section.groups.filter(
    (group) => !group.show || group.show(props.config),
));
const leadingGroupCount = computed(() => availableGroups.value.filter(
    (group) => group.beforeTabs,
).length);
const visibleGroups = computed(() => {
  const leadingGroups = availableGroups.value.filter((group) => group.beforeTabs);
  const tabGroups = availableGroups.value.filter(
      (group) => !group.beforeTabs && (
          !props.section.subtabs?.length ||
          !group.tab ||
          group.tab === activeSubtab.value
      ),
  );
  return [...leadingGroups, ...tabGroups];
});
</script>

<style scoped>
.config-tab-content {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  overflow-x: hidden;
}

.config-group {
  width: 100%;
  max-width: 100%;
  min-width: 0;
}

.section-tabs {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  border-bottom: 1px solid rgba(var(--v-border-color), var(--v-border-opacity));
}

.section-tabs :deep(.v-tab) {
  min-width: 112px;
  text-transform: none;
}

.group-heading {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-bottom: 10px;
}

.group-divider {
  margin: 16px 0;
}

.config-field-col {
  max-width: 100%;
  min-width: 0;
}

.config-field-col :deep(.v-input),
.config-field-col :deep(.v-field) {
  width: 100%;
  max-width: 100%;
  min-width: 0;
}

.config-switch {
  min-width: 0;
  min-height: 40px;
  margin: 0;
  padding-inline: 4px;
}

.config-switch :deep(.v-selection-control) {
  min-height: 40px;
}

.config-switch :deep(.v-label) {
  min-width: 0;
  line-height: 1.35;
  white-space: normal;
  overflow-wrap: anywhere;
}

.config-switch :deep(.v-input__details) {
  padding-inline: 4px;
}
</style>
