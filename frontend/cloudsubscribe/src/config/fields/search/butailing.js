import {enabled} from "../helpers.js";

export function createButailingGroups() {
    return [
        {
            tab: "butailing",
            title: "不太灵",
            icon: "mdi-magnet",
            fields: [
                {
                    key: "butailing_enabled",
                    label: "启用不太灵",
                    type: "switch",
                    cols: 6,
                },
                {
                    key: "test_butailing",
                    label: "测试搜索",
                    type: "test-source",
                    source: "butailing",
                    cols: 12,
                    show: enabled("butailing_enabled"),
                },
                {
                    key: "butailing_result_limit",
                    label: "候选上限",
                    type: "number",
                    min: 1,
                    max: 80,
                    cols: 6,
                    show: enabled("butailing_enabled"),
                },
            ],
        }
    ];
}
