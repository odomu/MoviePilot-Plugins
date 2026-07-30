export function createBasicSection(cloudDriveItems) {
    return {
        value: "basic",
        title: "基础设置",
        icon: "mdi-cog-outline",
        groups: [
            {
                title: "运行设置",
                icon: "mdi-play-circle-outline",
                fields: [
                    {key: "enabled", label: "启用插件", type: "switch", cols: 4},
                    {
                        key: "show_sidebar_nav",
                        label: "启用左侧导航",
                        type: "switch",
                        hint: "默认显示“网盘订阅”全页入口；关闭后刷新主界面生效。",
                        cols: 4,
                    },
                    {
                        key: "agent_enabled",
                        label: "启用智能体工具",
                        type: "switch",
                        hint: "允许智能体查询状态与性能、搜索推荐资源、清理缓存、修改白名单配置，以及提交用户确认的候选。",
                        cols: 4,
                    },
                    {
                        key: "platform_transfer_history_enabled",
                        label: "写入整理历史",
                        type: "switch",
                        cols: 4,
                    },
                    {
                        key: "takeover_new_subscribes",
                        label: "拦截新增订阅",
                        type: "switch",
                        hint: "新增订阅的搜索调度交由本插件处理，不修改原有真实站点配置。接管态仍遵循订阅过滤规则。",
                        cols: 8,
                    },
                    {
                        key: "cron",
                        label: "定时执行 Cron",
                        placeholder: "30 2,10,18 * * *",
                        cols: 8,
                    },
                ],
            },
            {
                title: "订阅接管时段",
                icon: "mdi-clock-outline",
                hint: "接管时段内符合过滤规则的订阅由本插件搜索；排除或开放时段的订阅仍由 MoviePilot 原生处理。开启始终接管后不再按下面时段切换。",
                fields: [
                    {
                        key: "block_system_subscribe",
                        label: "始终接管系统订阅",
                        hint: "订阅全天由插件接管",
                        type: "switch",
                        cols: 4,
                    },
                    {
                        key: "block_platform_downloads",
                        label: "拦截平台资源下载",
                        type: "switch",
                        hint: "接管时段内阻止 MoviePilot 为插件管理的订阅创建下载任务；与资源下载接管独立，关闭后平台可继续下载。",
                        cols: 4,
                    },
                    {
                        key: "takeover_platform_downloads",
                        label: "接管平台资源下载",
                        type: "switch",
                        hint: "接管时段内将 MoviePilot 选中的平台资源转换为 Magnet 并提交网盘离线下载，同时阻止平台重复创建下载；需启用 Magnet，接管失败时仍会阻止平台下载。",
                        cols: 4,
                    },
                    {
                        key: "block_start_time",
                        label: "接管开始",
                        type: "time",
                        cols: 2,
                        show: (config) => !config.block_system_subscribe,
                    },
                    {
                        key: "block_end_time",
                        label: "接管结束",
                        type: "time",
                        cols: 2,
                        show: (config) => !config.block_system_subscribe,
                    },
                    {
                        key: "unblock_start_time",
                        label: "开放开始",
                        type: "time",
                        cols: 2,
                        show: (config) => !config.block_system_subscribe,
                    },
                    {
                        key: "unblock_end_time",
                        label: "开放结束",
                        type: "time",
                        cols: 2,
                        show: (config) => !config.block_system_subscribe,
                    },
                ],
            },
            {
                title: "网盘提供方",
                icon: "mdi-cloud-outline",
                fields: [
                    {
                        key: "cloud_drive",
                        label: "当前转存网盘",
                        type: "select",
                        items: cloudDriveItems,
                        disabled: () => cloudDriveItems.length <= 1,
                        cols: 4,
                    },
                ],
            },
        ],
    };
}
