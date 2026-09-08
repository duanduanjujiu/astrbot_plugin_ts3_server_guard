# 更新日志

本文档记录 AstrBot TeamSpeak 3 服务器监控插件的版本更新历史。

---

## [3.1.0] - 2026-09

### 🔀 通知开关拆分 + 移除定时通知

- **开关拆分**：原 `enable_status_push` 一开关同时管 上下线切换 + 首次监控启动，
  现拆为两个独立开关：
  - `enable_status_push`：仅推送 服务器上线 / 下线 切换通知（默认开）
  - `enable_startup_notify`（新增）：仅推送「监控已启动」通知（默认关）
  - `monitoring.observe_status` 现在返回事件类型（startup/up/down），
    由主循环按对应开关决定是否发送
- **移除定时通知**：删除 `enable_periodic_report`（定期状态快照）相关配置与代码，
  同步移除 顶层的 `global_status_interval` 与单服务器的 `status_interval` 配置项
- **轮询简化**：不再有独立的每小时状态轮询；上下线 / 启动事件直接复用
  用户进出轮询（默认 15s，失联自动退避）的采样即时判定，
  每台服务器每节拍仍至多一条 ServerQuery 连接
- 升级到 3.1.0（metadata.yaml / pyproject.toml / @register 版本号同步）

---

## [3.0.0] - 2026-09

### 🛡️ 稳定性重构 + UMO 动态推送目标（本地 fork）

#### 新功能

- **UMO 推送目标**：推送目标不再只能是 WebUI 里的纯数字 `target_group`，
  新增 UMO 形态（如 `atri:GroupMessage:1092815819`），管理员可用新命令动态设置：
  - `/推送目标 <UMO>`、`/推送目标 <QQ群号>`、`/推送目标 本群`、`/推送目标 清除`、`/推送目标`（查看）
  - `/推送测试`：向当前目标发送测试消息
  - 聊天下令设置的推送目标持久化在 `data/plugin_data/astrbot_plugin_tsserver_relay/relay_state.json`，
    优先级：聊天下令覆盖 > WebUI `target_umo` > WebUI `target_group`
- **`_conf_schema.json` 新增 `target_umo`** 顶层字段（WebUI 面板也可直接填 UMO）
- 自动监控不再要求配置里预先填好群号：只要启用了服务器即可运行，推送目标可运行期再设置

#### 稳定性修复

- **上下线滞回判定**（`monitoring.observe_status`）：连续 `confirm_offline=3` 次失败采样才报
  “已离线”、连续 `confirm_online=2` 次成功才报“已上线”，杜绝单次防洪 / 瞬时断网
  触发“已离线→已上线”误报刷屏；首次采样即离线时静默建基线
- **单轮询单连接**：用户进出 + 状态检测共用同一轮询节拍的同一条 ServerQuery 连接，
  消除“每小时状态检查与 15s 用户轮询同秒双连接”触发的 TS3 防洪（error id 524）
- **离线快照不参与用户进出 diff**：拉取失败不再可能触发“全员离开 / 加入”假通知；
  判定离线时清空用户进出基线，恢复后重新建基线
- **连续失败降频**：失联服务器用户轮询退避到 60s，降低无效连接 / 登录 / 日志量
- **生命周期竞态修复**：延迟自动启动协程被持有并在 `terminate` 中取消，杜绝插件重载
  （<5s 内）后旧实例残留僵尸 monitor 任务造成双份通知 / 双倍防洪
- 单台服务器轮询异常隔离：不会拖垮整轮循环
- 群消息发送失败自动重试（最多 3 次，指数退避）；UMO 发送失败若目标是 QQ 群
  （`GroupMessage` + 纯数字群号）自动回退旧版 `send_group_msg` 路径
- 通知发送统一优先走官方 `context.send_message(umo, chain)`，适配任意平台

#### 其它

- 主监控循环保留 1s 心跳；`/查询` 行为不变
- 旧版纯群号 `target_group` 完全兼容（无 UMO 时行为与 v2 一致）

---

## [2.0.0] - 2025-12-15

### 🏗️ 架构重构（仿 astrbot_plugin_minecraft_multi_monitor）

- **完全模块化**：核心代码迁入 `ts_server_monitor/` 包，按职责拆分为
  `config.py / models.py / clients.py / monitoring.py / services.py / constants.py`。
  与 minecraft 监控插件的目录结构一一对应。
- **通知模型改为单目标群**：移除每个服务器独立的订阅者列表
  （`subscriptions[server][umo]`），改为所有事件推送到配置中的 `target_group` 一个固定群。
- **聊天命令简化**：仅保留 `/查询` 一个命令；删除 `/ts ls / sub / unsub / mysub / status / start / stop / 重置 / 全部管理员命令`。
- **`_conf_schema.json` 重写**：仿 minecraft 添加顶层 `target_group / global_status_interval / enable_auto_monitor / display_options`，
  `server_entries` 仍为 `template_list`，每条服务器字段精简为 `alias / display_name / enabled / host / query_port / query_user / query_password / virtual_server_id / status_interval`。
- **`display_options` 9 项开关**：与 minecraft 对齐，分别控制通知 / 查询里展示哪些栏目。
- **数据持久化**：移除 `data/plugin_data/astrbot_plugin_tsserver_relay/ts3_data.json`；
  配置由 AstrBot 写入 `data/config/astrbot_plugin_tsserver_relay_config.json`。
- **异步客户端**：`ts_server_monitor.clients.LocalStatusClient` 用
  `asyncio.to_thread` 把同步 ts3 库调用放进线程池，不阻塞事件循环。
- **`monitor_loop` 单协程驱动**：与 minecraft 一致，每台服务器独立 `status_interval` 控制推送频率。
- **`detect_changes`**：处理四类变更（首次基线、上下线切换、用户加入、用户离开）。
- **`format_server_info`**：仿 minecraft 用 `DisplayOptions` 控制渲染。

### ⚠️ 破坏性变更

- **删除聊天管理员命令**：所有服务器增删改查、监控启停、显示项配置均改为在 WebUI 完成。
- **删除订阅模型**：v1.x 的 `subscriptions` JSON 不再被读取；若 `plugin_data/.../ts3_data.json` 里仍残留旧订阅数据，可手动删除（插件代码不再引用）。
- **存储位置变更**：AstrBot 配置路径仍是 `data/config/<plugin>_config.json`，但 schema 不同，需要在 WebUI 重新填写。

---

## [1.1.0] - 2025-12-15

### ⚙️ WebUI 配置面板化（AstrBot 4.27.5 原生方案，参考 `astrbot_plugin_minecraft_multi_monitor`）

- **`_conf_schema.json` 改为 `template_list`**：与 `astrbot_plugin_minecraft_multi_monitor`
  对齐使用 `type: template_list` + `templates.ts_server`。每条服务器条目必带
  `__template_key=ts_server`，dashboard 才能渲染"添加 / 删除服务器"按钮。
- **服务器条目字段**（与 minecraft 风格一致）：
  `alias / display_name / enabled / host / query_port / query_user / query_password /
  virtual_server_id / status_interval`。其中 `display_name` 用于通知展示，回退到 `alias`；
  `enabled=False` 时跳过该服务器（保留配置但停监控）。
- **新增顶层配置项 `default_status_interval` / `default_query_port`**：
  当单服务器 `status_interval` / `query_port` 留空或填 0 时，回退到全局默认值。
- **服务器信息权威源切换到 AstrBot 配置面板**：存储路径为
  `data/config/astrbot_plugin_tsserver_relay_config.json`，由 `Main.__init__(context, config=...)`
  直接以 dict 形式注入。
- **聊天管理员命令移除**：`/ts add`、`/ts del`、`/ts join`、`/ts leave`、`/ts interval`、
  `/ts atall`、`/ts restart` 均已下线；保留 5 个普通用户命令
  `/ts ls`、`/ts sub`、`/ts unsub`、`/ts mysub`、`/ts status`。
- **热重载由 AstrBot dashboard 自动触发**：WebUI 保存配置后，
  `dashboard.services.config_service.save_plugin_configs` 会调用
  `plugin_manager.reload(plugin_name)`，整个插件（`terminate` → `__init__` → `initialize`）
  会被重新加载。本插件无需实现 `on_config_change` 钩子。
- **v1.0.x 数据迁移**：旧 `ts3_data.json` 中的 `server_info` 字段会作为 `legacy_server_info`
  保留在 `DataManager` 内存中；若 `config['server_entries']` 为空，插件会在日志中打印
  带 `__template_key` 的待写入 JSON，请管理员在 AstrBot WebUI 粘贴一次即可触发自动 reload 与热加载。
- **`status_interval` 防御加固**：`_sanitize_status_interval` 把任意输入规范化为
  ≥ 10 分钟的整数；与全局默认值回退结合，避免误填过小值触发 TS3 防洪。
- **`ts3_data.json` 不再持久化 `server_info`**：磁盘文件只保存订阅数据。

---

## [1.0.8] - 2025-12-10

### 🛡️ 可靠性改进

- **长睡眠模式**：监控重连失败达上限后不再永久停止，改为等待 30 分钟后自动重试
- **异常堆栈跟踪**：所有异常日志添加 `exc_info=True`，便于调试
- **上下文管理器安全**：`TS3Client.__enter__` 连接失败时抛出 `ConnectionError`，避免返回无效对象
- **防抖时间调整**：用户离开确认时间从 5 秒缩短至 2 秒

---

## [1.0.7] - 2025-12-10

### 🛡️ 网络超时保护

- **添加 timeout 参数**：`TS3Client` 新增 `timeout` 参数（默认 30 秒），所有网络操作（连接、查询）均会在超时后抛出异常，避免因网络问题导致监控线程无限阻塞

---

## [1.0.6] - 2025-12-10

### ⚡ 线程停止响应优化

- **使用 `threading.Event` 替代 `time.sleep`**：监控线程的所有睡眠调用改用 `Event.wait(timeout)`，调用 `stop()` 时可立即唤醒线程并退出，解决了之前最长需等待 30 秒的停止延迟问题

---

## [1.0.5] - 2025-12-10

### 🛠️ 代码质量改进

- **Monitor 首次连接容错**：首次连接失败后不再直接退出，而是进入重试循环（30秒间隔，最多5次）
- **Notifier 代码去重**：时长格式化改用 `utils.format_duration()`，消除重复代码
- **常量引用统一**：`ServerInfo` 模型使用 `utils.constants` 中的默认值常量

---

## [1.0.4] - 2025-12-10

### ⚡ 异步优化

- **使用 asyncio.Queue 替代 queue.Queue**：通知队列现在使用 `asyncio.Queue`，实现零延迟异步处理，消除了 1 秒轮询间隔
- **使用 call_soon_threadsafe**：监控线程通过 `call_soon_threadsafe` 安全地将通知放入队列，避免了直接调用协程的开销

---

## [1.0.3] - 2025-12-10

### 🔒 线程安全 & ⚡ 效率优化

- **DataManager 线程安全**：添加 `RLock` 锁保护所有数据访问，防止主线程与监控线程并发访问导致的竞态条件
- **优化状态推送**：`_on_status_tick` 现在直接复用 Monitor 已有的连接获取状态，避免每次推送时创建新连接

---

## [1.0.2] - 2025-12-10

### ⚡ 性能优化

- **修复同步网络 I/O 阻塞事件循环问题**：`/ts add` 和 `/ts status` 命令现在使用 `asyncio.to_thread()` 在线程池中执行同步网络操作，避免阻塞 asyncio 事件循环
- **修复插件卸载时的阻塞问题**：`terminate` 方法使用 `run_in_executor()` 调用 `monitor.stop()`，避免 `thread.join()` 阻塞主线程

---

## [1.0.1] - 2025-12-10

### 🐛 Bug 修复

- **修复在线人数显示不正确的问题**：改用实际过滤后的客户端列表长度计算在线人数，而非依赖服务器报告的数值（该数值可能包含多个 ServerQuery 连接）

---

## [1.0.0] - 2025-12-10

### 🎉 首次发布

#### 新增功能

- **多服务器监控**：支持同时监控多个 TS3 服务器
- **实时进出通知**：用户加入/离开服务器时自动推送通知
- **定时状态推送**：可配置间隔（默认 60 分钟）自动推送服务器状态
- **订阅管理**：用户可自主订阅/取消订阅
- **@全体成员**：支持状态推送时 @全体成员
- **抗抖动机制**：5 秒确认期，避免网络抖动导致的误报
- **数据持久化**：JSON 格式存储，重启不丢失

#### 命令列表

**管理员命令**

- `/ts add` - 添加服务器
- `/ts del` - 删除服务器
- `/ts join` - 切换加入通知
- `/ts leave` - 切换离开通知
- `/ts interval` - 设置状态推送间隔
- `/ts atall` - 设置 @全体成员
- `/ts restart` - 重启监控

**普通用户命令**

- `/ts ls` - 查看监控列表
- `/ts sub` - 订阅通知
- `/ts unsub` - 取消订阅
- `/ts mysub` - 查看我的订阅
- `/ts status` - 查看服务器状态

---

[1.0.1]: https://github.com/GEMILUXVII/astrbot_plugin_tsserver_relay/releases/tag/v1.0.1
[1.0.0]: https://github.com/GEMILUXVII/astrbot_plugin_tsserver_relay/releases/tag/v1.0.0
