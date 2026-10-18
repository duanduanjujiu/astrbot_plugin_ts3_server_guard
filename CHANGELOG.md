# 更新日志

本文档记录 AstrBot TeamSpeak 3 服务器监控插件的版本更新历史。

---

## [3.5.0] - 2026-10

插件市场上架审查整改：移除 py-ts3 依赖，改为纯 asyncio 客户端。

### 🔍 审查项 1：不再压制第三方库日志

审查意见指出 `ts_server_monitor/clients.py` 使用
`logging.getLogger("ts3.query").setLevel(logging.WARNING)` 压制第三方库日志，
不符合日志规范。

**整改**：直接移除 `py-ts3` 依赖，协议读写全部用 `asyncio.open_connection` 实现。
插件不再触碰任何第三方 logger，所有日志统一走 `astrbot.api.logger`。

### 🐍 审查项 2：移除 telnetlib 依赖，支持 Python 3.13+

`py-ts3 1.0.11` 依赖 Python 标准库 `telnetlib`，而其在 Python 3.13 中已被移除。
新实现不再使用 `telnetlib` / 线程池 / 阻塞 socket，因此：

- 无第三方运行依赖（`requirements.txt` / `pyproject.toml` 同步清空）；
- 支持 Python 3.10 ~ 3.13+；
- 事件循环内无阻塞调用，无 `ThreadPoolExecutor`、无长时间泄漏线程。

### 🔌 内置 ServerQuery 协议实现

- 连接握手：校验 `TS3` / `Welcome` 两行 greeting，所有读写受单次尝试的总 deadline 约束。
- 命令与响应：`\n\r` 分帧，读到 `error id=… msg=…` 行为止；非 0 错误抛
  `_QueryError`，保留 520（认证）/524（防洪）/768（sid）/2568（clientlist 权限）排查提示。
- 完整 TS3 转义/反转义表（`\s \p \\ \/ \n \r \t \v \f \a \b`），昵称/密码中的
  空格、竖线、反斜杠、换行均可正确往返。
- 记录解析：`|` 分隔多条记录、空格分隔字段、空值兼容；未订阅的 `notify` 行防御性忽略。
- 协议错位（serverinfo 缺 `virtualserver_name`、clientlist 缺 `clid`）按失败处理，
  绝不会把错位数据当成“在线快照”；超长响应（>8MB）直接断开。
- 行为保持不变：每服务器持久连接 + 空闲 keepalive（`version` 命令）、失效重连一次、
  clientlist 权限失败降级 `roster_ok=False` + serverinfo 人数回退、`voice_port` 展示。

### 🛡️ 独立协议审计后的加固

发布前由独立审计对照真实 TS3 服务器与 py-ts3 语义验证，并补了以下防护：

- **空/部分错位的 clientlist 不再被信任**：真实 TS3 的 clientlist 一定包含本查询
  连接，因此空响应或任一记录缺少 `clid` 一律按协议错误处理（`roster_ok=False`），
  避免“全员离开”误报。
- **服务端查询错误不再重连重登**：serverinfo 遇到 524 防洪 / 2568 权限等
  `_QueryError` 时保留已对齐连接，不再二次登录加剧防洪；只有网络/超时/协议失效
  才重连一次。
- **error 行严格解析**：缺少合法 `id` 的 error 行按不可重试的协议错误处理，
  不再伪装成 `id=-1` 触发重连。
- **多行 serverinfo 合并**：拆成多行返回时逐条合并，避免字段丢失。
- **日志去重**：clientlist 持续失败只在每段故障期告警一次，恢复后重置；
  超时日志不再误报单次超时秒数。
- **测试补强**：假服务器记录原始命令行，新增登录转义端到端断言、chunked
  greeting/response、notify 行、多行 clientlist/serverinfo、同 key 目标变更重连、
  keepalive 失败重连、空/混合 clientlist、524 不重登等回归用例。

### ✅ 测试

新增 `tests/fake_ts3_server.py`（asyncio 行协议假服务器）与
`tests/test_clients_protocol.py`（31 项协议/集成测试）。测试总数 **76 → 122**，
覆盖连接复用、断线重连一次、静默服务器超时、clientlist 2568 降级、524 防洪、
认证/sid 错误、转义、协议错位、close 幂等、无第三方依赖扫描等。
测试在 Python 3.11 / 3.12 / 3.13.16（官方镜像）与真实 AstrBot 4.29.1 容器中均通过；
另用真实 TS3 服务器完成只读联通验证（serverinfo/clientlist、语音端口、版本、人数）。

---

## [3.4.0] - 2026-10

性能与体验优化（batch-2）。

### ⚡ 持久连接 + keepalive

每台服务器在 `LocalStatusClient` 内缓存一条 ServerQuery 连接（TCP + login + use），
轮询复用；连接空闲超过 240s 发送 keepalive，连接失效/超时只重连一次并重试
serverinfo，纯 connect/auth 失败不重复重连。实测：连续两次轮询只产生 **1 次**
TCP accept + login（此前每 15s 一次），大幅降低 TS3 防洪风险与日志噪声。
`close()` 会尽力退出所有缓存连接（最多等 1s 锁后并发 quit），不会因挂起服务器
拖住插件卸载。

### 🚀 并发轮询

`monitor_loop` 每轮用 `asyncio.gather` 并发执行全部服务器的 tick；单台慢/离线
服务器不再拖慢其它服务器，单台异常在协程内部隔离。连接受每服务器 asyncio.Lock
保护，超时后仍在收尾的 worker 不会被下一轮重复使用。

### 🔎 `/ts_query` 缓存 + 限频

监控与手动查询都会写入快照缓存；`/ts_query` 在
`max(QUERY_CACHE_TTL_MIN, join_leave_interval*1.5)` 内直接复用缓存，不再每次调用
都新建 N 条查询连接。同一会话 5s 内重复查询直接回复“查询过于频繁”，不发网络请求。
冷缓存仍会实时拉取并回写。

### 👥 按 clid 识别用户

`detect_join_leave` 从“昵称集合 diff”改为按 `ClientInfo.clid` 识别：
同名用户可以各自播报加入/离开；同一 clid 改名只静默更新基线，不再误报
“离开 + 加入”。确认离线后恢复在线时静默重建名单（`rebaseline_roster`），
不再把此时在线的所有人补报为“加入”。

### 🔕 新开关：`enable_join_leave_notify`

默认开启；关闭后只维护进出基线与防抖状态、不播报加入/离开消息，
重新开启不会补发关闭期间的事件。

### 🎨 展示与安全

- 离线快照的平台/版本/人数/频道/运行时间统一显示 `—`，不再出现 `0/0`、`0分钟` 等误导值。
- `show_server_address` 优先显示 serverinfo 返回的语音端口，并在与 ServerQuery 端口
  不同时括号标注查询端口。
- 昵称清洗：去除控制字符/换行/制表符并截断到 32 字符；单条消息最多展示 20 个昵称
  （超出以“等共 N 人”概括），避免换行注入与超长消息。

### 🔧 其它

- 确认离线后的恢复探测间隔从 60s 缩短到 30s，更快发现服务器恢复。
- 旧版群号回退只在 AIOCQHTTP 上解析一次平台/客户端并复用，重试不再重复扫描适配器；
  `get_client()` 为 None 或缺少 `api` 时安全跳过。
- 删除未使用的遗留常量；新增可观测的 debug 日志（连接复用/重连/keepalive）。
- 测试增至 **76 项**（新增 clid 身份、恢复重建、清洗/展示、缓存限频、并发轮询等）。
- `logo.png` 压缩到 ~97KB / 256×256（AstrBot 只识别小写 `logo.png`）。
- 版本号统一升级到 3.4.0。

---

## [3.3.0] - 2026-10

可靠性修复（基于对线上 AstrBot 4.29.1 部署与 py-ts3 1.0.11 源码的审计）。

### 🐛 推送目标优先级错误（聊天设置群号不生效）

**现象**：WebUI 配了 `target_umo` 时，管理员执行 `/ts_push_target <QQ群号>`，命令回复
“✅ 设置成功”，但测试消息与后续通知实际仍发到旧 UMO。

**根因**：`_effective_umo()` 把运行期覆盖与 WebUI 配置做“或”运算，群号分支只清了
`_umo_override`，`notify()` 又总是 UMO 优先。

**修复**：新增纯函数 `resolve_target()`，运行期覆盖决定目标类型，UMO 与群号严格互斥；
`_send_test()` 改为对新目标**显式发送**并返回 bool，`/ts_push_target` 只有测试成功才
`save_runtime_target()` 并切换目标，失败不落盘、不改当前目标并回复 ❌。

### 🛡️ TS3 查询不再可能永久阻塞 / 泄漏线程

- 删除无效的 `.conn.settimeout()`（py-ts3 1.0.11 无 `conn` 属性，实际是 `telnet_conn`）。
- 重写连接建立：两次 greeting 使用带超时的 `read_until()`；`login`/`use` 改用
  `send(..., timeout=remaining)`（库的便捷方法内部 `timeout=None`，可能永久阻塞）。
- 一次查询共享一个总 deadline（默认 30s），连接、greeting、login/use、serverinfo、
  clientlist 都只能使用剩余时间；外层 `wait_for` 仅作兜底。
- 改用 `LocalStatusClient` 专属 `ThreadPoolExecutor`，并在 `terminate()` 中
  `shutdown`，避免默认线程池被卡死任务耗尽。
- 移除从未被渲染、却每轮多打一条命令的 `channellist` 请求（频道数取 serverinfo）。
- 将 `ts3.query` 的 INFO 连接日志压到 WARNING，避免每 15s 一条的日志噪音。

### 🐛 clientlist 失败不再把在线服务器误判为离线

`serverinfo` 成功即视为在线；`clientlist` 单独 `try/except`，失败时快照
`roster_ok=False`、跳过用户进出 diff，人数回退 `virtualserver_clientsonline - 1`。
错误码 520/524/768/2568 会输出可操作的排查提示。

### 📨 通知可靠投递

通知先进入有界队列（上限 50），由后台 worker 串行发送：单次超时 30s、最多 3 次、
1s/2s 退避；轮询循环不再被发送阻塞，短暂失败不再直接丢事件。队列满时丢弃最早一条。
状态变化先于用户进出入队，恢复在线时“已上线”先于“用户加入”。

### 🧩 配置健壮性

- JSON `null` 不再被 `str()` 成 `"None"` 骗过必填校验；密码字段不做 strip。
- `query_port` 仅接受 1-65535；`virtual_server_id` 必须 ≥ 1，非法值告警并回退。
- 重复 alias / 自动 key 冲突不再让第二台服务器“消失”：自动追加 `_2`、`_3` 后缀并告警。

### 🔁 热重载修复

移除 `sys.path` 注入，子模块改为包内相对导入（`from .ts_server_monitor...`）。
AstrBot 重载插件时会清理 `data.plugins.<dir>.*`，子模块不再残留旧代码，改代码后
重载即可生效（无需重启进程）。

### ✅ 测试

新增 `tests/`（unittest + AstrBot stub，不依赖真实 AstrBot / TS3）：
覆盖配置解析、滞回矩阵、进出防抖、目标优先级、先测试后保存、通知队列重试、
状态持久化等。运行：`python3 -m unittest discover -s tests -v`。

---

## [3.2.1] - 2026-09

### 🐛 修复「第一个连入服务器的用户不通知」

**现象**（用户报告）：空服务器上，自己作为第一个连入的人不会被通知；但之后第二个人连入、
或自己下线却会收到通知。

**根因**：`detect_join_leave` 用 `if not state.last_client_nicknames:` 作为
「首次基线尚未建立」的判据。但**空名单是服务器的合法状态**：

| 步骤 | `current_nicks` | `last_client_nicknames` | 判空结果 | 后果 |
| --- | --- | --- | --- | --- |
| T0 插件加载，0 人 | `{}` | `[]` | True | 首次基线，赋值为 `[]`（**仍是空**） |
| T1 第一人连入 | `{我}` | `[]` | **True** | **再次误判为首次基线 → 加入事件被吞** |
| T2 第二人连入 | `{我,B}` | `[我]` | False | 终于走正常 diff |

同一个哨兵失效点在两处触发：
1. 插件启动时服务器为空 → 第一个人连入被吞；
2. `observe_status` 确认离线时清空 `last_client_nicknames = []` → 服务器恢复在线后，
   在线用户再次被吞。

**修复**：新增专用哨兵字段 `ServerState.join_leave_initialized: bool`，首次基线判据
改为 `if not state.join_leave_initialized:`，与 `last_client_nicknames` 是否为空彻底解耦。
语义与 MC 插件的 `stable_status is None` 哨兵同构——哨兵只在真正的第一次置位，
之后永不回退；`observe_status` 清空名单时**不**重置哨兵。

**验证**（用两个插件的真实检测函数跑对照实验）：
- 空服务器 → 我第一个连入 → 第二人连入 → 我下线 → 第二人下线：全部正确播报
- 离线 → 恢复在线（名单被清空过）：在线用户被正确报为加入
- 空服务器连续 3 轮轮询后第一个人连入：正确通知

> 注：TS3 的用户进出检测带防抖（`global_join_leave_debounce`，默认 3s），
> 因此 join/leave 事件会在下一轮轮询才播报，这是设计如此（避免抖动误报）。

---

## [3.2.0] - 2026-09

### 📦 维护性重构（按 AstrBot 官方开发文档合规化）

- **命令名去空格**：`ts查询` 等含空格的命令被 AstrBot 解析成 `ts` + `查询` 两个 token，注册不上。已全部改为下划线：
  - `/ts查询` → `/ts_query`
  - `/ts推送目标` → `/ts_push_target`
  - `/ts推送测试` → `/ts_push_test`
  - 用户提示文本 / README / CHANGELOG / `_conf_schema.json` hint 同步
- **`enable_auto_monitor` 默认值修复**：schema 默认 `true`，但 `load_settings` 兜底 `false`——静默不一致。现统一为 `True`。
- **删除未文档化的 `servers` 字段兜底**：config.py 不再回退读取 `servers`，只支持 `server_entries`。
- **`metadata.yaml` 补 `short_desc` + `support_platforms`**：声明支持 `aiocqhttp` / `qq_official` / `satori`。
- **作者归属**：author / repo URL 从 `pengjinrui` 改为 `duanduanjujiu`。

---

## [3.1.1] - 2026-09

### 🔀 聊天指令加 ts 前缀（避免与 astrbot_plugin_mc_server_guard 冲突）

- `/查询` → `/ts_query`
- `/推送目标` → `/ts_push_target`
- `/推送测试` → `/ts_push_test`
- 旧指令名不再生效，请改用带前缀的新指令。

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
