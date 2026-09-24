export const CACHE_CATEGORIES = [
  {
    key: "search",
    label: "搜索资源缓存",
    desc: "搜索结果、候选详情、文件预览和 Magnet 元数据",
  },
  {
    key: "cloud",
    label: "网盘数据缓存",
    desc: "分享信息、文件列表、路径、离线任务和账户容量",
  },
  {
    key: "sync",
    label: "同步计算缓存",
    desc: "媒体识别、季目录、播出日历、延期和洗版基线",
  },
  {
    key: "interface",
    label: "页面选项缓存",
    desc: "订阅、站点、媒体服务器和配置下拉选项",
  },
  {
    key: "platform",
    label: "概览与智能体缓存",
    desc: "概览统计和智能体候选资源",
  },
];

export function useCacheActions(api, pluginId = "CloudSubscribe") {
  async function clearCache(categories) {
    const list = Array.isArray(categories)
      ? categories
      : Object.keys(categories || {}).filter((k) => categories[k]);
    const result = await api.post(`plugin/${pluginId}/cache/clear`, {
      categories: list,
    })
    if (!result?.success) throw new Error(result?.message || "清理缓存失败")
    return result.message || "缓存已清理"
  }

  return { clearCache }
}
