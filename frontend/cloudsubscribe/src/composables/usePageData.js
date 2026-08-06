import {computed, onMounted, onUnmounted, reactive, ref} from "vue";

export function usePageData(api, notify, pluginId = "CloudSubscribe") {
    const history = ref([]);
    const embyPlayItems = ref({});
    const offlineSupported = ref(false);
    const loading = ref(false);
    const runtime = reactive({
        status: "idle",
        task: "当前没有订阅处理任务",
        progress: 0,
        tasks: [],
    });

    const active = computed(
        () =>
            ["starting", "running", "stopping"].includes(runtime.status) ||
            (runtime.tasks || []).some((task) =>
                ["queued", "running", "stopping"].includes(task.status),
            ),
    );
    const stats = computed(() => {
        const today = new Date().toISOString().slice(0, 10);
        return [
            {
                title: "总转存",
                value: history.value.length,
                color: "primary",
                icon: "mdi-cloud-upload-outline",
            },
            {
                title: "今日转存",
                value: history.value.filter((item) =>
                    String(item.time || "").startsWith(today),
                ).length,
                color: "info",
                icon: "mdi-calendar-today",
            },
            {
                title: "成功",
                value: history.value.filter((item) => item.status === "成功").length,
                color: "success",
                icon: "mdi-check-circle-outline",
            },
            {
                title: "失败",
                value: history.value.filter((item) => item.status === "失败").length,
                color: "error",
                icon: "mdi-alert-circle-outline",
            },
        ];
    });

    let pageRequest = null;
    let runtimeTimer = null;
    let startRequestedUntil = 0;

    function hasActiveRuntime() {
        return (
            Date.now() < startRequestedUntil ||
            ["starting", "running", "stopping"].includes(runtime.status) ||
            (runtime.tasks || []).some((task) =>
                ["queued", "running", "stopping", "postprocessing"].includes(task.status),
            )
        );
    }

    function stopRuntimePolling() {
        if (runtimeTimer) {
            window.clearInterval(runtimeTimer);
            runtimeTimer = null;
        }
    }

    function ensureRuntimePolling() {
        if (!hasActiveRuntime()) {
            stopRuntimePolling();
            return;
        }
        if (runtimeTimer) return;
        runtimeTimer = window.setInterval(async () => {
            await loadRuntime();
            if (!hasActiveRuntime()) {
                stopRuntimePolling();
                await loadPage(false);
            }
        }, 5000);
    }

    async function loadPage(showLoading = true) {
        if (pageRequest) return pageRequest;
        if (showLoading) loading.value = true;
        pageRequest = (async () => {
            try {
                const result = await api.get(`plugin/${pluginId}/page_data`);
                if (!result?.success) throw new Error(result?.message || "加载失败");
                history.value = result.data?.history || [];
                embyPlayItems.value = result.data?.emby_play_items || {};
                offlineSupported.value = Boolean(result.data?.offline_supported);
                Object.assign(runtime, result.data?.runtime || {});
                ensureRuntimePolling();
            } catch (error) {
                if (showLoading) notify(error.message || "加载失败", "error");
            } finally {
                if (showLoading) loading.value = false;
                pageRequest = null;
            }
        })();
        return pageRequest;
    }

    async function loadRuntime() {
        try {
            const result = await api.get(`plugin/${pluginId}/runtime`);
            if (result?.success) {
                const nextRuntime = result.data || {};
                if (nextRuntime.status === "idle" && Date.now() < startRequestedUntil) {
                    Object.assign(runtime, {
                        ...nextRuntime,
                        status: "starting",
                        task: "正在准备订阅任务",
                    });
                } else {
                    Object.assign(runtime, nextRuntime);
                    if (nextRuntime.status === "running") startRequestedUntil = 0;
                }
                ensureRuntimePolling();
            }
        } catch (_) {
        }
    }

    async function startSync(selection = null) {
        try {
            const selectedCount = Math.max(0, Number(selection?.groupCount || 0));
            const payload = selectedCount
                ? {
                    selected_count: selectedCount,
                    subscribe_ids: Array.isArray(selection?.subscribeIds)
                        ? selection.subscribeIds
                        : [],
                    history_targets: Array.isArray(selection?.targets)
                        ? selection.targets
                        : [],
                }
                : {};
            const result = await api.post(`plugin/${pluginId}/sync/start`, payload);
            if (!result?.success) throw new Error(result?.message || "启动失败");
            const selectedScope = result?.data?.scope === "selected";
            const subscribeCount = Number(result?.data?.subscribe_count || 0);
            startRequestedUntil = Date.now() + 10000;
            Object.assign(runtime, {
                status: "starting",
                task: selectedScope
                    ? `正在准备所选 ${subscribeCount} 个订阅`
                    : "正在准备全部订阅",
                progress: 0,
                tasks: [],
            });
            notify(result.message || "订阅搜索任务已启动");
            await loadRuntime();
            ensureRuntimePolling();
            return true;
        } catch (error) {
            notify(error.message || "启动失败", "error");
            return false;
        }
    }


    async function stopSync() {
        try {
            runtime.status = "stopping";
            runtime.task = "正在停止当前任务";
            const result = await api.post(`plugin/${pluginId}/sync/stop`);
            if (!result?.success) throw new Error(result?.message || "停止失败");
            await loadRuntime();
            ensureRuntimePolling();
            return true;
        } catch (error) {
            notify(error.message || "停止失败", "error");
            await loadRuntime();
            return false;
        }
    }

    async function stopTask(taskId) {
        try {
            const task = (runtime.tasks || []).find((item) => item.id === taskId);
            if (task) {
                task.status = "stopping";
                task.phase = "等待安全停止";
            }
            const result = await api.post(`plugin/${pluginId}/sync/task/stop`, {
                task_id: taskId,
            });
            if (!result?.success) throw new Error(result?.message || "停止任务失败");
            await loadRuntime();
            ensureRuntimePolling();
            return true;
        } catch (error) {
            notify(error.message || "停止任务失败", "error");
            await loadRuntime();
            return false;
        }
    }

    async function clearHistory(force = false) {
        const result = await api.post(`plugin/${pluginId}/history/clear`, {
            force: Boolean(force),
        });
        if (!result?.success) throw new Error(result?.message || "清空失败");
        await loadPage(false);
        return result.message || "历史已清空";
    }

    async function deleteHistory(record, deleteLinkedFiles = false) {
        const result = await api.post(`plugin/${pluginId}/history/delete`, {
            time: record.time,
            share_url: record.share_url,
            file_name: record.file_name,
            tmdb_id: record.tmdb_id,
            season: record.season,
            episode: record.episode,
            delete_linked_files: Boolean(deleteLinkedFiles),
        });
        if (!result?.success) throw new Error(result?.message || "删除失败");
        await loadPage(false);
        return result.message || "历史记录已删除";
    }

    async function deleteHistoryBatch(records, deleteLinkedFiles = false) {
        const identities = records.map((record) => ({
            record_id: record.record_id,
            time: record.time,
            share_url: record.share_url,
            file_name: record.file_name,
            tmdb_id: record.tmdb_id,
            season: record.season,
            episode: record.episode,
        }));
        const result = await api.post(`plugin/${pluginId}/history/delete_batch`, {
            records: identities,
            delete_linked_files: Boolean(deleteLinkedFiles),
        });
        if (!result?.success) throw new Error(result?.message || "批量删除失败");
        await loadPage(false);
        return result.message || "所选历史记录已删除";
    }

    async function notifyHistory(record) {
        const result = await api.post(`plugin/${pluginId}/history/notify`, {
            time: record.time,
            share_url: record.share_url,
            file_name: record.file_name,
            tmdb_id: record.tmdb_id,
            season: record.season,
            episode: record.episode,
        });
        if (!result?.success) throw new Error(result?.message || "通知失败");
        return result.message || "通知已补发";
    }

    async function upgradeHistory(records, scope = "record") {
        const identities = (records || []).map((record) => ({
            record_id: record.record_id,
            time: record.time,
            share_url: record.share_url,
            file_name: record.file_name,
            tmdb_id: record.tmdb_id,
            season: record.season,
            episode: record.episode,
        }));
        const result = await api.post(`plugin/${pluginId}/history/upgrade`, {
            source: "history",
            scope,
            records: identities,
        });
        if (!result?.success) throw new Error(result?.message || "洗版任务提交失败");
        startRequestedUntil = Date.now() + 10000;
        Object.assign(runtime, {
            status: "starting",
            task: "正在准备洗版任务",
            progress: 0,
            tasks: [],
        });
        await loadRuntime();
        ensureRuntimePolling();
        return result.message || "洗版任务已提交";
    }

    async function clearCache(categories) {
        const result = await api.post(`plugin/${pluginId}/cache/clear`, {
            categories,
        });
        if (!result?.success) throw new Error(result?.message || "清理缓存失败");
        return result.message || "缓存已清理";
    }

    onMounted(() => loadPage());
    onUnmounted(stopRuntimePolling);

    return {
        history,
        embyPlayItems,
        offlineSupported,
        loading,
        runtime,
        active,
        stats,
        loadPage,
        startSync,
        stopSync,
        stopTask,
        clearHistory,
        deleteHistory,
        deleteHistoryBatch,
        notifyHistory,
        upgradeHistory,
        clearCache,
    };
}
