# MoviePilot-Plugins

> [!NOTE]
> MoviePilot 第三方插件仓库。

## 使用说明

在 MoviePilot 的插件市场中添加以下仓库地址：

```text
https://github.com/odomu/MoviePilot-Plugins
```

插件版本、MoviePilot 最低版本及更新记录以 [package.v2.json](package.v2.json) 为准。

## 插件列表

### 订阅与网盘

- [网盘订阅助手](docs/cloudsubscribe.md)：结合 MoviePilot 订阅功能，自动搜索网盘资源并同步缺失的电影和剧集；支持资源推荐、115
  转存与离线下载、STRM、洗版、智能体、工作流和侧边栏页面。源码目录说明见[插件 README](plugins.v2/cloudsubscribe/README.md)。

## 仓库结构

```text
MoviePilot-Plugins/
├── frontend/       # 插件前端源码
├── icons/          # 插件图标
├── plugins.v2/     # MoviePilot v2 插件
└── package.v2.json # 插件市场清单
```

## 许可证

本仓库根据 [GNU General Public License v3.0](LICENSE) 许可证进行许可。
