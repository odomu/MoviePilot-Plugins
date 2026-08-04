import {enabled} from "./helpers.js";

export function createNotifySection(options) {
    return {
        value: "notify",
        title: "通知设置",
        icon: "mdi-bell-outline",
        groups: [
            {
                title: "入库通知",
                icon: "mdi-database-arrow-up-outline",
                hint: "STRM 写入成功后仅通知对应媒体项，不触发全库扫描；Emby 可在入库后继续提取媒体信息。",
                fields: [
                    {
                        key: "media_server_refresh_enabled",
                        label: "启用入库通知",
                        type: "switch",
                        cols: 6,
                    },
                    {
                        key: "emby_mediainfo_enabled",
                        label: "Emby 媒体信息提取",
                        type: "switch",
                        hint: "入库后通过 Emby PlaybackInfo 接口触发，不依赖其他服务。",
                        cols: 6,
                        show: enabled("media_server_refresh_enabled"),
                    },
                    {
                        key: "media_server_refresh_delay",
                        label: "延迟通知（秒）",
                        type: "number",
                        min: 0,
                        cols: 4,
                        show: enabled("media_server_refresh_enabled"),
                    },
                    {
                        key: "media_servers",
                        label: "通知媒体服务器",
                        type: "select",
                        items: options.mediaservers,
                        multiple: true,
                        cols: 8,
                        show: enabled("media_server_refresh_enabled"),
                    },
                    {
                        key: "media_server_path_mappings",
                        label: "媒体服务器路径映射",
                        type: "textarea",
                        placeholder: "/媒体服务器/strm#/strm",
                        hint: "每行：媒体服务器路径#本地路径；路径一致时留空",
                        cols: 12,
                        show: enabled("media_server_refresh_enabled"),
                    },
                ],
            },
            {
                title: "媒体库通知",
                icon: "mdi-server-network-outline",
                hint: "接收 Emby 媒体库变更事件后，同步 MoviePilot 内部媒体索引。",
                fields: [
                    {
                        key: "platform_media_sync_enabled",
                        label: "接收媒体库通知",
                        type: "switch",
                        cols: 12,
                    },
                    {
                        key: "media_library_webhook_urls",
                        label: "Emby 媒体库通知地址",
                        type: "media-library-webhook",
                        items: options.mediaservers.filter(
                            (item) => String(item.type || "").toLowerCase() === "emby",
                        ),
                        cols: 12,
                        show: enabled("platform_media_sync_enabled"),
                    },
                    {
                        key: "media_library_webhook_help",
                        type: "info",
                        label: "Emby Webhook 配置",
                        lines: [
                            "请求方法：POST；内容类型：application/json",
                            "将上方对应媒体服务器的完整地址复制到 Emby Webhook；固定 Key 不能手工修改",
                            "Emby 测试通知仅验证连接；正式事件只勾选媒体库新增、删除和更新",
                            "无效 Key、来源或事件会返回明确错误，不会返回假成功",
                        ],
                        cols: 12,
                        show: enabled("platform_media_sync_enabled"),
                    },
                ],
            },
            {
                title: "消息通知",
                icon: "mdi-message-badge-outline",
                fields: [
                    {key: "notify", label: "发送消息通知", type: "switch", cols: 4},
                    {
                        key: "notification_type",
                        label: "消息通知类型",
                        type: "select",
                        items: options.notificationTypes,
                        hint: "按 MoviePilot 消息类型分发到已启用的通知渠道。",
                        cols: 8,
                        show: enabled("notify"),
                    },
                ],
            },
            {
                title: "Webhook",
                icon: "mdi-webhook",
                fields: [
                    {
                        key: "webhook_enabled",
                        label: "启用 Webhook",
                        type: "switch",
                        cols: 4,
                    },
                    {
                        key: "webhook_url",
                        label: "Webhook 地址",
                        cols: 8,
                        show: enabled("webhook_enabled"),
                    },
                    {
                        key: "webhook_method",
                        label: "请求方法",
                        type: "select",
                        items: ["POST", "GET"],
                        cols: 4,
                        show: enabled("webhook_enabled"),
                    },
                    {
                        key: "webhook_timeout",
                        label: "超时（秒）",
                        type: "number",
                        min: 1,
                        max: 120,
                        cols: 4,
                        show: enabled("webhook_enabled"),
                    },
                ],
            },
        ],
    };
}
