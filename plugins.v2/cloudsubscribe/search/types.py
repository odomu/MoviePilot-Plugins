"""搜索渠道共享的资源类型定义。"""

TYPE_ALIASES = {
    "115": "115",
    "123": "123",
    "quark": "quark",
    "uc": "uc",
    "mobile": "mobile",
    "pikpak": "pikpak",
    "xunlei": "xunlei",
    "aliyun": "alipan",
    "alipan": "alipan",
    "tianyi": "tianyi",
    "guangya": "guangya",
    "baidu": "baidu",
}
TYPE_HOSTS = {
    "115": {"115.com", "115cdn.com"},
    "123": {"123pan.com", "123pan.cn", "123684.com", "123865.com"},
    "quark": {"quark.cn"},
    "alipan": {"alipan.com", "aliyundrive.com", "aliyundrive.net"},
    "tianyi": {"cloud.189.cn"},
    "guangya": {"guangyapan.com"},
    "baidu": {"pan.baidu.com"},
}
SUPPORTED_CLOUD_TYPES = tuple(TYPE_ALIASES)
TYPE_NAMES = {
    "115": "115网盘",
    "123": "123云盘",
    "quark": "夸克",
    "alipan": "阿里云盘",
    "uc": "UC网盘",
    "mobile": "移动云盘",
    "pikpak": "PikPak",
    "xunlei": "迅雷云盘",
    "magnet": "磁力链接",
    "ed2k": "电驴链接",
    "tianyi": "天翼云盘",
    "guangya": "光鸭云盘",
    "baidu": "百度网盘",
}
