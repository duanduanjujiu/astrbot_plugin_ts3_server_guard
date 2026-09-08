<div align="center">
  <img src="LOGO.png" alt="AstrBot TeamSpeak Plugin Logo" width="160" />
</div>

# <div align="center">TeamSpeak 3 Server Guard</div>

<div align="center">
  <strong>AstrBot TeamSpeak 3 多服务器监控插件（AI 修改版 · v3.1.0）</strong>
</div>

<br>

<div align="center">
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/version-v3.1.0-9644F4?style=for-the-badge" alt="Version"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0-E53935?style=for-the-badge" alt="License"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python"></a>
  <a href="https://github.com/AstrBotDevs/AstrBot"><img src="https://img.shields.io/badge/AstrBot-Compatible-00BFA5?style=for-the-badge&logo=robot&logoColor=white" alt="AstrBot Compatible"></a>
</div>

<br>

<div align="center">
  <a href="#插件简介">插件简介</a> •
  <a href="#来源与-ai-生成声明">来源与 AI 声明</a> •
  <a href="#功能特性">功能特性</a> •
  <a href="#配置说明">配置说明</a> •
  <a href="#命令">命令</a> •
  <a href="#faq">FAQ</a>
</div>
  <a href="#功能特性">功能特性</a> •
  <a href="#配置说明">配置说明</a> •
  <a href="#命令">命令</a> •
  <a href="#faq">FAQ</a>
</div>

## 来源与 AI 生成声明

本仓库 **`astrbot_plugin_ts3_server_guard`** 是 **由 AI（DeepSeek 编码助手）辅助修改生成** 的分叉/改版插件，基于以下项目修改而来：

- **上游插件**：[astrbot_plugin_tsserver_relay](https://github.com/GEMILUXVII/astrbot_plugin_tsserver_relay)（作者：GEMILUXVII，AGPL-3.0）
- **架构参考**：[astrbot_plugin_minecraft_multi_monitor](https://github.com/XiaolongYang-HZAU/astrbot_plugin_minecraft_multy_monitor)

主要修改内容（v3.0 / v3.1，均为 AI 辅助完成）：

- 上下线通知滞回判定（连续失败采样确认，避免防洪/抖动误报）；
- 单轮询单连接，消除同秒双连接触发的 TS3 防洪；失联自动降频；
- 修复插件重载可能残留僵尸监控任务的问题；
- 推送目标支持 UMO（`/推送目标 <UMO|群号|本群>`），可动态切换推送群；
- 通知开关拆分：`enable_status_push`（上下线切换）与 `enable_startup_notify`（监控启动）相互独立；
- 移除定时通知（`enable_periodic_report`）与 `status_interval` 相关配置。

> 保留上游 AGPL-3.0 许可证与版权声明（见 `LICENSE`）。如上游有更新，本仓库不会自动跟随；
> 使用前请自行评估。AstrBot 插件名（metadata `name`）与本仓库名均为 `astrbot_plugin_ts3_server_guard`，
> 与原插件的 `astrbot_plugin_tsserver_relay` 不同，可并存安装。

## 插件简介

AstrBot TeamSpeak 3 多服务器监控插件。同时监控多台 TS3 服务器，通过 ServerQuery 协议
定时拉取服务器状态、用户进出、运行时间等信息；状态发生变化时把变更推送到指定的 QQ 群。

v2.0.0 起架构仿造 [`astrbot_plugin_minecraft_multi_monitor`](https://github.com/XiaolongYang-HZAU/astrbot_plugin_minecraft_multy_monitor)：
模块化拆包、统一的 `display_options` 开关、`template_list` 配置、单一目标群推送。

v3.0.0 起为稳定性与 UMO 特性（AI 修改版）：
- 推送目标支持 **UMO**（`atri:GroupMessage:1092815819`），聊天里用 `/推送目标 <UMO>` 即可动态设置/切换，无需改 WebUI；
- 上下线通知带**滞回确认**，单次防洪 / 断网抖动不再误报“已离线/已上线”；
- 每个轮询节拍只建一条 ServerQuery 连接（消除每小时状态检查与 15s 用户轮询
  同秒双连接触发的 TS3 防洪），失败自动降频；修复插件重载可能残留僵尸监控任务的问题。

v3.1.0（本轮改动，AI 修改版）：
- **开关拆分**：`enable_status_push`（上线/下线切换通知）与新增的 `enable_startup_notify`
  （“监控已启动”通知，默认关）互相独立，可分别开关；
- **定时通知已删除**：移除 `enable_periodic_report`（定期状态快照）与
  `global_status_interval` / 单服务器 `status_interval` 配置，不再有每小时状态轮询；
  上下线/启动事件直接复用用户进出轮询的采样（默认 15s）即时判定。

消息样式与 minecraft 插件刻意区分：**推送文案为纯文本（无 emoji），带 `━━━` 头尾分隔线**。

## 功能特性

- **多服务器监控**：同时监控多台 TS3 服务器，互不干扰
- **用户进出检测**：默认每 15 秒轮询 clientlist，diff 出新加入 / 离开的用户（带防抖）
- **上下线 / 启动通知独立开关（v3.1.0）**：`enable_status_push` 控制服务器 上线 / 下线 切换通知；
  `enable_startup_notify` 单独控制「监控已启动」通知（默认关），互不影响
- **已移除定时通知（v3.1.0）**：不再有定期状态快照
- **可配置展示项**：通过 `display_options` 控制通知里展示哪些栏目
- **自动启动 / 手动控制**：`enable_auto_monitor` 配置项控制插件加载后是否自动启动监控循环
- **UMO 动态推送目标**：`/推送目标 <UMO|群号|本群>` 聊天内即可设置/切换/清除推送目标，无需改 WebUI（持久化）
- **上下线滞回判定（v3）**：连续失败采样确认后才报“已离线”，避免防洪 / 抖动误报
- **数据持久化**：服务器配置由 AstrBot 面板管理；推送目标运行时设置存 `plugin_data`，重启 / 重载不丢失

## 配置说明

打开 **AstrBot 控制台 → 插件管理 → astrbot_plugin_ts3_server_guard → 设置**。

### 顶层字段

| 字段 | 说明 | 默认值 |
| --- | --- | --- |
| `target_group` | 接收通知的 QQ 群号（纯数字字符串） | `""` |
| `target_umo` | 推送目标 UMO（如 `atri:GroupMessage:1092815819`），**优先于** `target_group` | `""` |
| `global_join_leave_interval` | 全局默认用户进出检测间隔（秒，最小 5）；上下线切换也复用此采样节奏 | `15` |
| `global_join_leave_debounce` | 全局默认用户进出防抖时长（秒） | `3` |
| `enable_auto_monitor` | 插件加载后是否自动启动监控 | `true` |
| `enable_status_push` | 是否推送服务器 上线 / 下线 切换通知（默认开） | `true` |
| `enable_startup_notify` | 是否推送「监控已启动」通知（默认关） | `false` |
| `display_options` | 通知 / 查询展示项开关 | 详见下表 |
| `server_entries` | TS3 服务器列表 | `[]` |

### `display_options` 子字段

| 字段 | 说明 | 默认值 |
| --- | --- | --- |
| `show_server_name` | 展示服务器名称（含 `[在线]` / `[离线]` 状态） | `true` |
| `show_server_address` | 展示服务器地址 | `false` |
| `show_server_platform` | 展示服务器平台 | `true` |
| `show_server_version` | 展示服务器版本 | `false` |
| `show_online_clients` | 展示在线人数 | `true` |
| `show_channels` | 展示频道数 | `true` |
| `show_uptime` | 展示运行时间 | `true` |
| `show_client_list` | 展示在线用户列表 | `false` |
| `show_update_time` | 展示更新时间 | `true` |

### `server_entries[].ts_server` 单条服务器

| 字段 | 说明 | 默认值 |
| --- | --- | --- |
| `alias` | 唯一标识（推荐英文 / 数字 / 下划线） | 必填 |
| `display_name` | 通知里展示的名字；空 → `alias` | `""` |
| `enabled` | 是否启用 | `true` |
| `host` | TS3 服务器地址 | 必填 |
| `query_port` | ServerQuery 端口；填 `0` 用全局默认 | `10011` |
| `query_user` | ServerQuery 用户名 | 必填 |
| `query_password` | ServerQuery 密码（明文存储） | 必填 |
| `virtual_server_id` | 虚拟服务器 ID | `1` |
| `join_leave_interval` | 单服务器用户进出检测间隔（秒）；≤ 0 → 全局值；< 5 → 强制 5 | `0` |

示例条目：

```json
{
  "__template_key": "ts_server",
  "alias": "main",
  "display_name": "主服务器",
  "enabled": true,
  "host": "192.168.1.100",
  "query_port": 10011,
  "query_user": "serveradmin",
  "query_password": "MyPassword123",
  "virtual_server_id": 1,
  "join_leave_interval": 15
}
```

## 通知样例

消息为**纯文本 + `━━━` 头尾分隔线**，无 emoji。

### 用户进出通知

```
━━━━━━━━━━━━━━━━━━
TS3 用户进出
━━━━━━━━━━━━━━━━━━
主服务器: Alice、Bob 加入了服务器

当前状态：
服务器: 主服务器 [在线]
地址: 192.168.1.100:10011
平台: Linux
在线人数: 2/32
频道数: 8
运行时间: 1天2小时30分钟
更新时间: 2025-12-15 21:00:00
━━━━━━━━━━━━━━━━━━
```

### 服务器状态变化通知（上线 / 下线 切换，需开启 `enable_status_push`）

```
━━━━━━━━━━━━━━━━━━
TS3 服务器状态
━━━━━━━━━━━━━━━━━━
主服务器 已离线

当前状态：
服务器: 主服务器 [离线]
地址: 192.168.1.100:10011
在线人数: 0/32
说明: TS3 ServerQuery 查询失败（超时或凭据错误）
━━━━━━━━━━━━━━━━━━
```

「监控已启动」通知（需开启 `enable_startup_notify`）文本为：
`主服务器 监控已启动`（服务器在线且有用户时带后缀 `，当前有用户在线`）。

## 命令

聊天命令如下（`推送目标` / `推送测试` 仅管理员可用）：

| 命令 | 说明 |
| --- | --- |
| `/查询` | 立即拉取所有启用服务器的状态（不推送群，只在私聊 / 群聊里返回文本） |
| `/推送目标` | 查看当前推送目标与用法 |
| `/推送目标 <UMO>` | 按 UMO 设置推送目标，如 `/推送目标 atri:GroupMessage:1092815819`，并发送测试消息 |
| `/推送目标 <QQ群号>` | 兼容旧版：按纯数字群号设置（如 `/推送目标 123456789`） |
| `/推送目标 本群` | 把当前会话（群/私聊）设为推送目标 |
| `/推送目标 清除` | 清除聊天下令设置，恢复使用 WebUI 面板的 `target_umo` / `target_group` |
| `/推送测试` | 向当前生效目标发送一条测试消息 |

推送目标生效优先级：**聊天下令设置（持久化） > WebUI `target_umo` > WebUI `target_group`**。
服务器增删改、监控启动 / 停止、显示项配置等仍走 WebUI。

### UMO 是什么？

UMO（Unified Message Origin）是 AstrBot 记录的“会话唯一 ID”，形如
`<平台实例名>:<消息类型>:<会话ID>`，例如 QQ 群为 `atri:GroupMessage:1092815819`。
发到 UMO 即发到对应平台对应会话，比“纯群号 + send_group_msg”更通用，
也支持把通知发到私聊或其它平台。

## 数据存储

v2.0.0 起不再使用 `data/plugin_data/astrbot_plugin_ts3_server_guard/` 目录保存服务器配置。
所有服务器 / 显示项配置由 AstrBot 写入 `data/config/astrbot_plugin_ts3_server_guard_config.json`。

v3.0.0 起聊天下令设置的推送目标（UMO / 群号）持久化在
`data/plugin_data/astrbot_plugin_ts3_server_guard/relay_state.json`，跨插件重载与进程重启保留。

## FAQ

### Q: 提示 "ts3 库未安装" / 升级 AstrBot 后依赖丢失

插件运行依赖已写入插件根目录的 `requirements.txt`（仅 `ts3`）。
升级 / 重建 AstrBot（如更换 docker 镜像）后若报缺库，在 AstrBot 容器内执行一次：

```bash
pip install -r /AstrBot/data/plugins/astrbot_plugin_ts3_server_guard/requirements.txt
```

然后重载插件；或在 WebUI 里对该插件执行一次重装 / 更新，让 AstrBot 按
`requirements.txt` 自动安装依赖。

### Q: 用户进出提示很慢 / 被防洪拦截

默认用户进出检测每 15 秒一次，ServerQuery 连接比较频繁。请务必：

- 把 AstrBot 所在机器 IP 加入 TS3 服务器的 `query_ip_allowlist.txt`（修改后**重启 TS3 服务**才生效）
- 若 TS3 服务器不在你控制之下或防洪严格，把 `global_join_leave_interval` 调到 30-60 秒

### Q: 无法连接到 TS3 服务器

请检查：

- `host` 是否正确（IP 或域名），`query_port` 是否对应 ServerQuery 端口（默认 10011，非客户端端口 9987）
- `query_user` / `query_password` 是否正确
- 是否已在 TS3 服务器上为该账号授予 ServerQuery 权限（`serveradmin` 通常自带）
- TS3 服务器是否将本机 IP 加入 `query_ip_allowlist.txt`（否则会被防洪拦截）

### Q: 推送不到目标群 / 私聊

请检查：

- 用 `/推送目标` 查看当前生效目标；`target_group` 必须是纯数字的群号，`target_umo` 需形如 `atri:GroupMessage:1092815819`
- AstrBot 必须已连接到目标平台（NapCat / 官方机器人 / 其它），机器人必须在目标群内且未被禁言
- 使用 `/推送测试` 直接验证；失败时查看 AstrBot 日志中的具体报错
- 注意：部分平台可能不支持“主动消息”，此时只能把推送目标设在机器人能收到消息的会话

### Q: 如何用 UMO 设置推送群？

管理员在任意会话输入：

- `/推送目标 atri:GroupMessage:1092815819` —— 按完整 UMO 设置（推荐）
- `/推送目标 本群` —— 在目标群里直接绑定当前会话
- `/推送目标 123456789` —— 兼容旧版纯群号

设置后插件会自动向新目标发送一条测试消息确认可达；目标会持久化，插件重载 / AstrBot 重启后仍生效。

### Q: 服务器明明在线，却偶尔收到“已离线”又“已上线”？

这是 v2.x 的老问题：单次 ServerQuery 拉取失败（例如 TS3 防洪 error id 524、瞬时断网）会被直接当成服务器离线。
v3.0.0 起上下线判定带滞回——需**连续 3 次**失败采样才报“已离线”，恢复在线也需连续 2 次成功采样；
v3.1.0 起不再有独立的每小时状态轮询（定时通知已移除），每个轮询节拍只建一条连接。
若仍频繁出现，请把 AstrBot 所在机器 IP 加入 TS3 `query_ip_allowlist.txt`（修改后**重启 TS3 服务**才生效），并适当调大
`global_join_leave_interval`。

### Q: 想看用户列表

在 `display_options.show_client_list` 设为 `true` 即可。

## 相关链接

- [AstrBot 官方文档](https://astrbot.app/)
- [AstrBot 插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)
- [TeamSpeak 官网](https://teamspeak.com/)
- [ts3 Python 库](https://github.com/benediktschmitt/py-ts3)
- [参考插件 astrbot_plugin_minecraft_multi_monitor](https://github.com/XiaolongYang-HZAU/astrbot_plugin_minecraft_multy_monitor)

## 许可证

AGPL-3.0 — Copyright (C) 2025 GEMILUXVII
