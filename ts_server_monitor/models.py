"""数据模型。

仿 ``astrbot_plugin_minecraft_multi_monitor/minecraft_multi_monitor/models.py``
的设计：所有运行时对象都用 ``@dataclass(slots=True)``。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class DisplayOptions:
    """控制状态通知 / 查询时展示哪些栏目。"""

    show_server_name: bool = True
    show_server_address: bool = False
    show_server_platform: bool = True
    show_server_version: bool = False
    show_online_clients: bool = True
    show_channels: bool = True
    show_uptime: bool = True
    show_client_list: bool = False
    show_update_time: bool = True


@dataclass(slots=True)
class PluginSettings:
    """插件全局设置（来自 _conf_schema.json 顶层字段）。"""

    target_group: str | None
    target_umo: str | None
    enable_auto_monitor: bool
    global_join_leave_interval: int
    global_join_leave_debounce: float
    enable_status_push: bool
    enable_startup_notify: bool
    # 用户进出通知开关（默认开）。关闭后仍持续检测并维护基线，
    # 只是不推送"加入/离开"消息，避免重新开启时补报历史用户。
    enable_join_leave_notify: bool = True
    display_options: DisplayOptions = field(default_factory=DisplayOptions)


@dataclass(slots=True)
class ServerConfig:
    """一台 TS3 服务器的配置（来自 _conf_schema.json 的 server_entries 项）。"""

    key: str
    enabled: bool
    server_name: str
    host: str
    query_port: int
    query_user: str
    query_password: str
    virtual_server_id: int
    join_leave_interval: int  # 秒；0 表示回退到 global_join_leave_interval


@dataclass(slots=True)
class ClientInfo:
    """TS3 在线客户端信息（来自 ServerQuery clientlist）。"""

    clid: int
    nickname: str
    database_id: int
    cid: int
    client_type: int


@dataclass(slots=True)
class ServerSnapshot:
    """某时刻 TS3 服务器的完整快照，用于显示和变更检测。"""

    key: str
    name: str
    host: str
    query_port: int
    status: str  # "online" | "offline"
    platform: str
    version: str
    clients_online: int
    max_clients: int
    channels_online: int
    uptime: int  # 秒
    # 语音端口（serverinfo 的 virtualserver_port）；0 表示未知，
    # 展示地址时回退到 query_port。clients.py 负责填充。
    voice_port: int = 0
    clients: list[ClientInfo] = field(default_factory=list)
    # clientlist 是否成功；False 表示名单不可用，clients_online 为 serverinfo 回退值。
    roster_ok: bool = True
    update_time: str = ""
    error: str | None = None


@dataclass(slots=True)
class ServerState:
    """单台服务器在监控循环中维护的运行时状态。

    字段按用途拆为四组：
    - "基线"：用户进出检测的上一次有效在线名单（clid -> 昵称，只记录在线快照）。
    - "待确认"：本次轮询刚发现但还没过防抖期的疑似进出事件（下一轮再次确认才"敲定"）。
    - "恢复"：确认离线后标记，恢复在线时静默重建基线，避免"全员加入"刷屏。
    - "健康"：上下线判定的滞回状态（见 monitoring.observe_status）。
    """

    # ---- 基线（用户进出检测用，仅在线快照会更新） ----
    # 「首次基线是否已建立」的专用哨兵，与名单是否为空解耦：
    # 空名单是服务器的合法状态（空服务器 / 全员离开），如果用 `not last_clients`
    # 当"尚未初始化"的判据，空服务器上第一个连入的用户会被反复当作基线吞掉，
    # 导致"第一个人加入不通知"。语义与 MC 插件的 `stable_status is None` 同构。
    join_leave_initialized: bool = False
    # clid -> 最近一次看到的昵称。以 clid 识别身份，重名用户不再互相遮盖；
    # 同一 clid 改名只静默更新这里的昵称，不产生加入/离开事件。
    last_clients: dict[int, str] = field(default_factory=dict)
    # 确认离线后置 True：恢复在线后的第一份在线快照只用于重建基线，
    # 不把"离线期间就在线的人"误报为加入。detect_join_leave 处理后置回 False。
    rebaseline_roster: bool = False

    # ---- 轮询时间戳 ----
    last_join_leave_check_ts: float = 0.0  # 用户进出/健康检测周期使用

    # ---- 待确认事件（防抖） ----
    # key 是 ClientInfo.clid（身份稳定，重名用户各自独立），
    # value 是 (首次观察到的时间戳, 昵称快照)：
    #   - pending_joins 的昵称取最近一次观察值；
    #   - pending_leaves 的昵称是离开前的最后已知值，便于离开后渲染。
    pending_joins: dict[int, tuple[float, str]] = field(default_factory=dict)
    pending_leaves: dict[int, tuple[float, str]] = field(default_factory=dict)

    # ---- 健康状态（上下线判定，见 monitoring.observe_status） ----
    # 已确认并推送过的稳定状态；None 表示尚未建立基线（插件刚启动）。
    health: str | None = None  # "online" | "offline"
    # 连续与 health 相反的采样数；达到阈值后翻转 health。
    contradict_probes: int = 0
    # 连续拉取失败次数（status == "offline" 的采样）；用于降频/降级。
    consecutive_failures: int = 0
    # 最近一次成功拉取的时间戳（loop.time()）。
    last_success_ts: float = 0.0
