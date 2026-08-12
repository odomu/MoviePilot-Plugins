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
                    cols: 12,
                },
                {
                    key: "test_butailing",
                    label: "测试搜索",
                    type: "test-source",
                    source: "butailing",
                    cols: 12,
                },
            ],
        },
        {
            tab: "butailing",
            title: "搜索与风控",
            icon: "mdi-shield-search",
            hint: "列表和详情接口共享请求限速。",
            fields: [
                {
                    key: "butailing_result_limit",
                    label: "候选上限",
                    type: "number",
                    min: 1,
                    max: 80,
                    cols: 4,
                },
                {
                    key: "butailing_request_interval",
                    label: "请求间隔",
                    type: "number",
                    min: 1,
                    max: 10,
                    step: 0.1,
                    suffix: "秒",
                    cols: 4,
                },
                {
                    key: "butailing_timeout",
                    label: "请求超时",
                    type: "number",
                    min: 5,
                    max: 60,
                    suffix: "秒",
                    cols: 4,
                },
            ],
        },
    ];
}
