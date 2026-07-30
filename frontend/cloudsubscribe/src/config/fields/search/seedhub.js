import {enabled} from "../helpers.js";

export function createSeedhubGroups() {
    return [
        {
            tab: "seedhub",
            title: "SeedHub",
            icon: "mdi-seed-outline",
            fields: [
                {
                    key: "seedhub_enabled",
                    label: "启用 SeedHub",
                    type: "switch",
                    cols: 6,
                },
                {
                    key: "test_seedhub",
                    label: "测试搜索",
                    type: "test-source",
                    source: "seedhub",
                    cols: 12,
                    show: enabled("seedhub_enabled"),
                },
                {
                    key: "seedhub_result_limit",
                    label: "候选上限",
                    type: "number",
                    min: 1,
                    max: 80,
                    cols: 6,
                    show: enabled("seedhub_enabled"),
                },
            ],
        },
    ];
}
