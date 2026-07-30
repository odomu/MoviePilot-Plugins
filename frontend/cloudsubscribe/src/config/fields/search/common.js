import {enabled} from "../helpers.js";

const SOURCE_ITEMS = [
    {title: "HDHive", value: "hdhive"},
    {title: "Dian115", value: "dian115"},
    {title: "PanSou", value: "pansou"},
    {title: "聚影", value: "juying"},
    {title: "SeedHub", value: "seedhub"},
    {title: "不太灵", value: "butailing"},
];

export function createCommonSearchGroups(resourceTypeItems) {
    return [
        {
            tab: "common",
            title: "搜索顺序",
            icon: "mdi-sort",
            hint: "按选择顺序依次查询；未选择但已启用的来源会排在末尾。",
            fields: [
                {
                    key: "search_source_order",
                    label: "搜索源优先级",
                    type: "select",
                    items: SOURCE_ITEMS,
                    multiple: true,
                    cols: 12,
                },
                {
                    key: "resource_type_order",
                    label: "资源类型优先级",
                    hint: "仅显示当前转存网盘支持的类型；未选择的类型不会获取或处理。",
                    type: "select",
                    items: resourceTypeItems,
                    multiple: true,
                    cols: 12,
                },
                {
                    key: "magnet_metadata_url_template",
                    label: "Magnet元数据地址模板",
                    hint: "必须包含 {info_hash}；返回内容会由torf解码并校验Info Hash。",
                    placeholder: "https://itorrents.org/torrent/{info_hash}.torrent",
                    cols: 12,
                },
            ],
        },
        {
            tab: "common",
            title: "搜索性能",
            icon: "mdi-speedometer",
            hint: "控制跨搜索源查询和本地缓存；115接口与积分解锁仍保持串行限速。",
            fields: [
                {
                    key: "search_cache_enabled",
                    label: "启用搜索缓存",
                    type: "switch",
                    cols: 4,
                },
                {
                    key: "search_cache_ttl_minutes",
                    label: "缓存时间（分钟）",
                    type: "number",
                    min: 1,
                    max: 1440,
                    cols: 4,
                    show: enabled("search_cache_enabled"),
                },
                {
                    key: "search_concurrency",
                    label: "搜索并发数",
                    hint: "1为逐源查询，建议2，最大5",
                    type: "number",
                    min: 1,
                    max: 5,
                    cols: 4,
                },
            ],
        },
    ];
}
