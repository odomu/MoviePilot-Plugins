import {enabled} from "../helpers.js";

export function createHdhiveGroups(options = {}) {
    return [
        {
            tab: "hdhive",
            title: "HDHive 接入",
            icon: "mdi-hexagon-multiple-outline",
            hint: "WebAPI 模式完成登录、搜索和资源获取。",
            fields: [
                {
                    key: "hdhive_account_info",
                    type: "account",
                    accountKey: "search:hdhive",
                    data: options.searchAccounts?.hdhive || {},
                    compact: true,
                    cols: 12,
                    show: enabled("hdhive_enabled"),
                },
                {
                    key: "hdhive_enabled",
                    label: "启用 HDHive",
                    type: "switch",
                    cols: 6,
                },
                {
                    key: "hdhive_query_mode",
                    label: "查询模式",
                    type: "select",
                    items: [
                        {title: "OpenAPI", value: "api"},
                        {title: "WebAPI", value: "web"},
                    ],
                    cols: 6,
                    show: enabled("hdhive_enabled"),
                },
                {
                    key: "hdhive_api_key",
                    label: "API Key / 应用 Secret",
                    type: "password",
                    cols: 6,
                    show: (config) =>
                        config.hdhive_enabled && config.hdhive_query_mode === "api",
                },
                {
                    key: "hdhive_client_id",
                    label: "Client ID",
                    cols: 6,
                    show: (config) =>
                        config.hdhive_enabled && config.hdhive_query_mode === "api",
                },
                {
                    key: "hdhive_redirect_uri",
                    label: "OAuth Redirect URI",
                    cols: 6,
                    show: (config) =>
                        config.hdhive_enabled && config.hdhive_query_mode === "api",
                },
                {
                    key: "hdhive_auth_code",
                    label: "一次性授权码",
                    cols: 6,
                    show: (config) =>
                        config.hdhive_enabled && config.hdhive_query_mode === "api",
                },
                {
                    key: "hdhive_access_token",
                    label: "Access Token",
                    type: "password",
                    cols: 6,
                    show: (config) =>
                        config.hdhive_enabled && config.hdhive_query_mode === "api",
                },
                {
                    key: "hdhive_refresh_token",
                    label: "Refresh Token",
                    type: "password",
                    cols: 6,
                    show: (config) =>
                        config.hdhive_enabled && config.hdhive_query_mode === "api",
                },
                {
                    key: "hdhive_token_file",
                    label: "Token 挂载文件",
                    cols: 12,
                    show: (config) =>
                        config.hdhive_enabled && config.hdhive_query_mode === "api",
                },
                {
                    key: "hdhive_username",
                    label: "HDHive 用户名",
                    cols: 6,
                    show: (config) =>
                        config.hdhive_enabled &&
                        config.hdhive_query_mode === "web",
                },
                {
                    key: "hdhive_password",
                    label: "HDHive 密码",
                    type: "password",
                    cols: 6,
                    show: (config) =>
                        config.hdhive_enabled &&
                        config.hdhive_query_mode === "web",
                },
                {
                    key: "test_hdhive",
                    label: "测试搜索",
                    type: "test-source",
                    source: "hdhive",
                    cols: 12,
                    show: enabled("hdhive_enabled"),
                },
            ],
        },
        {
            tab: "hdhive",
            title: "HDHive 积分解锁",
            icon: "mdi-ticket-confirmation-outline",
            hint: "默认不解锁收费资源；开启后仅在候选被实际采用且双重预算充足时扣费。",
            show: enabled("hdhive_enabled"),
            fields: [
                {
                    key: "hdhive_auto_unlock",
                    label: "允许积分解锁",
                    type: "switch",
                    cols: 4,
                    show: enabled("hdhive_enabled"),
                },
                {
                    key: "hdhive_max_unlock_points",
                    label: "单次积分总预算",
                    hint: "限制一次同步任务内 HDHive 的累计解锁积分。",
                    type: "number",
                    min: 0,
                    cols: 4,
                    show: (config) =>
                        config.hdhive_enabled && config.hdhive_auto_unlock,
                },
                {
                    key: "hdhive_max_points_per_sub",
                    label: "单订阅解锁预算",
                    hint: "按订阅累计并持久化；订阅完成后清除对应积分账本。",
                    type: "number",
                    min: 0,
                    cols: 4,
                    show: (config) =>
                        config.hdhive_enabled && config.hdhive_auto_unlock,
                },
            ],
        },
        {
            tab: "hdhive",
            title: "HDHive 搜索与风控",
            icon: "mdi-shield-search",
            hint: "资源缓存减少重复访问；认证、资源查询和解锁共用限速与风控冷却。",
            show: enabled("hdhive_enabled"),
            fields: [
                {
                    key: "hdhive_candidate_limit",
                    label: "HDHive 候选上限",
                    hint: "按平台规则排序后再截取",
                    type: "number",
                    min: 1,
                    max: 20,
                    cols: 6,
                    show: enabled("hdhive_enabled"),
                },
                {
                    key: "hdhive_request_interval",
                    label: "请求访问间隔",
                    hint: "OpenAPI 与 WebAPI 的认证、查询和解锁共用统一限速，并自动加入随机抖动",
                    type: "number",
                    min: 0.5,
                    max: 10,
                    step: 0.5,
                    suffix: "秒",
                    cols: 6,
                    show: enabled("hdhive_enabled"),
                },
                {
                    key: "hdhive_torrentclaw_enabled",
                    label: "获取 TorrentClaw Magnet",
                    hint: "仅在资源类型优先级中选择 Magnet 时生效。",
                    type: "switch",
                    cols: 12,
                    show: (config) => enabled("hdhive_enabled")(config)
                        && config.hdhive_query_mode === "web",
                },
                {
                    key: "hdhive_torrentclaw_subtitle_languages",
                    label: "Magnet字幕语言筛选",
                    hint: "按选择顺序优先匹配，默认中文。存在匹配项时过滤其他资源，无匹配时回退全部。",
                    type: "select",
                    items: [
                        {title: "中文", value: "zh"},
                        {title: "英语", value: "en"},
                        {title: "日语", value: "ja"},
                        {title: "韩语", value: "ko"},
                        {title: "葡萄牙语（巴西）", value: "pt-BR"},
                        {title: "葡萄牙语（葡萄牙）", value: "pt-PT"},
                        {title: "西班牙语", value: "es"},
                        {title: "法语", value: "fr"},
                        {title: "德语", value: "de"},
                        {title: "意大利语", value: "it"},
                        {title: "俄语", value: "ru"},
                        {title: "阿拉伯语", value: "ar"},
                        {title: "泰语", value: "th"},
                        {title: "土耳其语", value: "tr"},
                        {title: "印地语", value: "hi"},
                        {title: "泰米尔语", value: "ta"},
                        {title: "泰卢固语", value: "te"},
                    ],
                    multiple: true,
                    cols: 12,
                    show: (config) => enabled("hdhive_enabled")(config)
                        && config.hdhive_query_mode === "web"
                        && enabled("hdhive_torrentclaw_enabled")(config),
                },
            ]
        }
    ]
}
