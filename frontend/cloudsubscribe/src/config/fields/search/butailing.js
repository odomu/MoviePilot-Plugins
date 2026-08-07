export function createButailingGroups() {
    return [
        {
            tab: "butailing",
            title: "不太灵",
            icon: "mdi-magnet",
            fields: [
                {
                    key: "butailing_base_url",
                    label: "服务地址",
                    cols: 12
                },
                {
                    key: "test_butailing",
                    label: "测试搜索",
                    type: "test-source",
                    source: "butailing",
                    cols: 12,
                },
                {
                    key: "butailing_result_limit",
                    label: "候选上限",
                    type: "number",
                    min: 1,
                    max: 80,
                    cols: 6,
                },
            ],
        }
    ];
}
