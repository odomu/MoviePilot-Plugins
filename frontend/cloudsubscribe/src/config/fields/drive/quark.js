export function createQuarkGroups(options) {
    return [
        {
            tab: "quark",
            title: "夸克账号",
            hideHeading: true,
            fields: [
                {
                    key: "quark_account_info",
                    type: "account",
                    accountKey: "drive:quark",
                    data: options.accounts?.quark || {},
                    cols: 12,
                },
                {
                    key: "quark_cookie",
                    label: "夸克 Cookie",
                    type: "password",
                    hint: "可直接填写或点击右侧二维码按钮扫码登录",
                    scanProvider: "quark",
                    cols: 12,
                },
                {
                    key: "quark_transfer_path",
                    label: "网盘转存路径",
                    type: "cloud-directory",
                    placeholder: "/",
                    driveProvider: "quark",
                    hint: "夸克分享先保存到此路径，之后按平台规则整理。",
                    cols: 6,
                },
                {
                    key: "quark_media_path",
                    label: "媒体库目录",
                    type: "cloud-directory",
                    placeholder: "/",
                    driveProvider: "quark",
                    hint: "最终媒体从此目录开始按平台规则分类；默认 / 表示网盘根目录。",
                    cols: 6,
                },
            ],
        },
        {
            tab: "quark",
            title: "请求超时",
            icon: "mdi-timer-cog-outline",
            hint: "作用于夸克网盘 HTTP 请求，范围 5-300 秒。",
            fields: [
                {
                    key: "quark_request_timeout",
                    label: "请求超时（秒）",
                    type: "number",
                    min: 5,
                    max: 300,
                    cols: 6,
                },
            ],
        },
    ];
}
