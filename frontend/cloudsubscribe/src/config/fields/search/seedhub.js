export function createSeedhubGroups() {
    return [
        {
            tab: "seedhub",
            title: "SeedHub",
            icon: "mdi-seed-outline",
            fields: [
                {
                    key: "seedhub_base_url",
                    label: "服务地址",
                    cols: 12
                },
                {
                    key: "test_seedhub",
                    label: "测试搜索",
                    type: "test-source",
                    source: "seedhub",
                    cols: 12,
                },
                {
                    key: "seedhub_result_limit",
                    label: "候选上限",
                    type: "number",
                    min: 1,
                    max: 80,
                    cols: 6,
                },
            ],
        },
    ];
}
