import {enabled} from "./helpers.js";

export function createTransferSection(options) {
    return {
        value: "transfer",
        title: "转存设置",
        icon: "mdi-cloud-download-outline",
        groups: [
            {
                title: "订阅范围",
                icon: "mdi-filter-outline",
                fields: [
                    {
                        key: "subscribe_filter_mode",
                        label: "订阅筛选模式",
                        type: "select",
                        items: [
                            {title: "排除所选订阅", value: "exclude"},
                            {title: "仅处理所选订阅", value: "include"},
                        ],
                        cols: 4,
                    },
                    {
                        key: "exclude_subscribes",
                        label: "排除订阅",
                        type: "select",
                        items: options.subscribes,
                        multiple: true,
                        cols: 8,
                        show: (config) => config.subscribe_filter_mode === "exclude",
                    },
                    {
                        key: "include_subscribes",
                        label: "指定订阅",
                        type: "select",
                        items: options.subscribes,
                        multiple: true,
                        cols: 8,
                        show: (config) => config.subscribe_filter_mode === "include",
                    },
                ],
            },
            {
                title: "转存控制",
                icon: "mdi-tune-variant",
                fields: [
                    {
                        key: "max_transfer_per_sync",
                        label: "单次最大转存文件数",
                        type: "number",
                        min: 1,
                        cols: 3,
                    },
                    {
                        key: "subscription_concurrency",
                        label: "订阅并发数",
                        type: "number",
                        min: 1,
                        max: 5,
                        cols: 3,
                    },
                    {
                        key: "skip_other_season_dirs",
                        label: "跳过其他季目录",
                        type: "switch",
                        cols: 12,
                    },
                    {
                        key: "batch_size",
                        label: "批量转存数量",
                        type: "number",
                        min: 1,
                        cols: 3,
                    },
                ],
            },
            {
                title: "本地路径与 STRM",
                icon: "mdi-folder-play-outline",
                hint: "配置本地媒体根路径及转存后的 STRM 生成方式。",
                fields: [
                    {
                        key: "local_resource_path",
                        label: "本地媒体根路径",
                        placeholder: "/strm",
                        cols: 12,
                    },
                    {
                        key: "strm_generate_enabled",
                        label: "转存后直接生成 STRM",
                        type: "switch",
                        cols: 4,
                    },
                    {
                        key: "nfo_scrape_enabled",
                        type: "switch",
                        label: "刮削生成 NFO",
                        cols: 4,
                    },
                    {
                        key: "image_scrape_enabled",
                        type: "switch",
                        label: "刮削生成图片",
                        cols: 4,
                    },
                    {
                        key: "strm_base_url",
                        label: "STRM 基础地址",
                        placeholder: "http://172.17.0.1:9527",
                        cols: 8,
                        show: enabled("strm_generate_enabled"),
                    },
                    {
                        key: "strm_url_template",
                        label: "STRM URL 模板",
                        type: "textarea",
                        hint: "115变量：{pickcode}；夸克变量：{file_id}；光鸭变量：{file_id}、{gcid}；123变量：{file_id}、{md5}、{size}、{s3_key_flag}；通用变量：{base_url}、{file_name}、{file_path}。插件仅按模板生成 STRM，不代理播放请求。",
                        cols: 12,
                        show: enabled("strm_generate_enabled"),
                    },
                ],
            },
        ]
    }
}
