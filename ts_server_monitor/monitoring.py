"""格式化与变更检测。

仿 ``astrbot_plugin_minecraft_multi_monitor/minecraft_multi_monitor/monitoring.py`` 的拆分：
- ``format_server_info`` 负责把 snapshot 渲染成可读文本
- ``observe_status`` 负责带防抖地判定 上下线切换 + 首次基线
- ``detect_join_leave`` 负责按 clid diff 用户进出（带防抖）

样式约定：消息不带 emoji，用纯文本键值 + 头尾分隔线（区别于 minecraft 插件的
emoji 风格，分隔线常量见 :data:`ts_server_monitor.constants.SEPARATOR`）。
昵称由 TS3 用户自行设置，可能含控制字符、换行或超长文本，展示前统一经
:func:`sanitize_nickname` 清理并截断。

防抖设计：
    pending_joins / pending_leaves 是"待敲定"的事件 map（key 为 clid）：
    - 第一次轮询发现疑似 join/leave → 加入 pending
    - 第二次轮询还在 pending 中且仍符合条件 → 确认为有效事件，从 pending 删除
    - 第二次轮询 pending 中的事件已"消失"（用户又出现在线 / 又消失了）→ 从 pending 删除，丢弃该事件

身份识别：
    以 ``ClientInfo.clid`` 而非昵称作为身份主键：重名用户可以各自独立地加入/离开；
    同一 clid 只改昵称时静默更新基线昵称，不产生事件。

恢复重基线（见 :func:`observe_status`）：
    确认离线时在线名单已不可信，清空并置 ``rebaseline_roster``；恢复在线后的
    第一份在线快照只用于重建基线，避免把"离线期间一直在的人"误报为全员加入。

上下线防抖（v3.0.0 新增，见 :func:`observe_status`）：
    单次拉取失败（TS3 防洪 / 瞬时断网）不再直接判定服务器离线，
    需要连续 ``confirm_offline`` 个失败样本才翻转；恢复在线同理需要
    ``confirm_online`` 个成功样本。
"""

from __future__ import annotations

from .constants import (
    DEFAULT_STATUS_CONFIRM_OFFLINE,
    DEFAULT_STATUS_CONFIRM_ONLINE,
    SEPARATOR,
    format_duration,
)
from .models import DisplayOptions, ServerConfig, ServerSnapshot, ServerState


def status_text(status: str) -> str:
    """把状态枚举映射成中文纯文本（无 emoji）。"""
    return "在线" if status == "online" else "离线"


def sanitize_nickname(text: str, limit: int = 32) -> str:
    """清理客户端昵称用于展示：去掉控制字符/换行/制表符并截断。

    TS3 昵称可由用户任意设置，可能含换行、控制字符或超长文本，直接拼进
    通知会破坏排版或撑大消息。这里只保留可打印字符（空白保留），再按
    ``limit`` 截断；``limit <= 0`` 表示不截断。
    """
    cleaned = "".join(ch for ch in str(text) if ch.isprintable()).strip()
    if limit > 0 and len(cleaned) > limit:
        cleaned = cleaned[:limit]
    return cleaned


# 单条消息最多展示的昵称数；超出部分用"等共 N 人"概括，保证消息有界。
MAX_DISPLAY_NAMES = 20


def join_display_names(names: list[str], limit: int = MAX_DISPLAY_NAMES) -> str:
    """把昵称列表拼成一行展示文本，超过 ``limit`` 个时用"等共 N 人"概括。"""
    shown = "、".join(names[:limit])
    if len(names) > limit:
        shown += f" 等共 {len(names)} 人"
    return shown


def wrap_notice(title: str, *sections: str) -> str:
    """把通知消息包上统一的头尾分隔线。

    样式约定（区别于 minecraft 的 emoji 风格）：
    ::

        ━━━━━━━━━━━━━━━━━━
        <title>
        ━━━━━━━━━━━━━━━━━━
        <sections...>

        ━━━━━━━━━━━━━━━━━━

    Args:
        title: 标题行，例如 "TS3 用户进出"。
        *sections: 若干正文段（每段可以是多行字符串），段之间空一行。
    """
    parts = [SEPARATOR, title, SEPARATOR]
    if sections:
        parts.append("\n\n".join(s for s in sections if s))
    parts.append(SEPARATOR)
    return "\n".join(parts)


def format_server_info(
    snapshot: ServerSnapshot | None, display_options: DisplayOptions
) -> str:
    """把 snapshot 渲染为多行纯文本（无 emoji），供 /ts 查询 / 状态推送使用。

    离线快照拿不到可信的平台/版本/人数/频道/运行时间，这些栏目统一显示 "—"，
    避免 "未知 / 0 / 0分钟" 这类误导性数值；错误说明仍然保留。
    展示地址时优先用语音端口 ``voice_port``，并在与 ServerQuery 端口不同时标注后者。
    """
    if snapshot is None:
        return "获取服务器数据失败"

    online = snapshot.status == "online"
    st = status_text(snapshot.status)
    lines: list[str] = []

    if display_options.show_server_name:
        lines.append(f"服务器: {snapshot.name} [{st}]")
    else:
        lines.append(f"状态: {st}")

    if display_options.show_server_address:
        voice_port = snapshot.voice_port
        address = f"地址: {snapshot.host}:{voice_port or snapshot.query_port}"
        if voice_port > 0 and voice_port != snapshot.query_port:
            address += f"（ServerQuery {snapshot.query_port}）"
        lines.append(address)

    if display_options.show_server_platform:
        lines.append(f"平台: {snapshot.platform if online else '—'}")

    if display_options.show_server_version:
        lines.append(f"版本: {snapshot.version if online else '—'}")

    if display_options.show_online_clients:
        if online:
            lines.append(
                f"在线人数: {snapshot.clients_online}/{snapshot.max_clients}"
            )
        else:
            lines.append("在线人数: —")

    if display_options.show_channels:
        lines.append(f"频道数: {snapshot.channels_online if online else '—'}")

    if display_options.show_uptime:
        uptime = format_duration(snapshot.uptime) if online else "—"
        lines.append(f"运行时间: {uptime}")

    if display_options.show_client_list:
        # 昵称统一清理并截断；最多展示 MAX_DISPLAY_NAMES 个，超出部分用总人数概括。
        if snapshot.clients:
            names = [
                sanitize_nickname(client.nickname) or "未知"
                for client in snapshot.clients
            ]
            lines.append(f"在线用户: {join_display_names(names)}")
        else:
            lines.append("在线用户: 无")

    if display_options.show_update_time:
        lines.append(f"更新时间: {snapshot.update_time}")

    if snapshot.error and not online:
        lines.append(f"说明: {snapshot.error}")

    return "\n".join(lines)


# ----------------------------------------------------------------------
# 状态变更检测（上下线切换，带“连续采样确认”防抖）
# ----------------------------------------------------------------------


def observe_status(
    server: ServerConfig,
    state: ServerState,
    snapshot: ServerSnapshot,
    *,
    confirm_online: int = DEFAULT_STATUS_CONFIRM_ONLINE,
    confirm_offline: int = DEFAULT_STATUS_CONFIRM_OFFLINE,
) -> tuple[str | None, str | None]:
    """观测一次服务器状态采样，返回 ``(event_kind, message)``。

    事件类型（供调用方按不同开关分别决定是否推送）：
    - ``"startup"``：首次采样且在线 → “监控已启动[，当前有用户在线]”
    - ``"up"`` / ``"down"``：稳定态翻转 → “已上线” / “已离线”
    - 无事件时返回 ``(None, None)``

    背景：TS3 防洪、瞬时断网等会让单次拉取失败。如果一失败就判离线、一成功就判
    上线，群聊会被“已离线/已上线”刷屏（v2.x 实测出现过）。这里引入滞回：
    只有连续 ``confirm_offline`` 次离线采样才把基线翻到离线；只有连续
    ``confirm_online`` 次在线采样才翻回在线。启动即离线的服务器静默建立基线，
    不产生启动事件，恢复在线时按 ``"up"`` 处理。

    确认离线时在线名单已不可信：清空 ``last_clients`` 与待确认事件，并置
    ``state.rebaseline_roster = True``；恢复在线后的第一份在线快照只用于静默
    重建基线，因此不会出现"全员加入"刷屏，"已上线"通知仍按滞回正常发出。
    ``join_leave_initialized`` 哨兵不回退（首次基线只建立一次）。

    Args:
        server: 当前服务器配置（用于生成消息文本）。
        state: 运行时状态（持有 health / contradict_probes 等字段）。
        snapshot: 本次轮询的快照（``status`` 为 "online"/"offline"）。
        confirm_online: 判定“已上线”需要的连续在线采样数。
        confirm_offline: 判定“已离线”需要的连续离线采样数。
    """
    observed = snapshot.status

    # 首次建立基线：只可能在“在线”时产生 startup 事件
    if state.health is None:
        state.contradict_probes = 0
        if observed == "online":
            state.health = "online"
            if snapshot.clients_online > 0:
                return "startup", f"{server.server_name} 监控已启动，当前有用户在线"
            return "startup", f"{server.server_name} 监控已启动"
        # 启动时服务器就不可达：静默建立离线基线，恢复在线后再通知
        state.health = "offline"
        return None, None

    # 与基线一致 → 清掉反向累计，维持现状
    if observed == state.health:
        state.contradict_probes = 0
        return None, None

    # 与基线相反：累计“反方向”采样数，达到阈值才翻转
    state.contradict_probes += 1
    required = confirm_offline if observed == "offline" else confirm_online
    if state.contradict_probes < required:
        return None, None

    state.health = observed
    state.contradict_probes = 0

    if observed == "offline":
        # 已确认离线：离线期间拉不到 clientlist，在线名单已不可信，清空基线
        # 与待确认事件；置 rebaseline_roster 让恢复后的第一份在线快照只重建基线，
        # 否则会把"离线期间一直在的人"全部误报为加入。
        # 注意：不要重置 join_leave_initialized 哨兵——这里只是丢弃不可信数据，
        # 该哨兵保证"首次基线"只建立一次。
        state.last_clients.clear()
        state.pending_joins.clear()
        state.pending_leaves.clear()
        state.rebaseline_roster = True
        return "down", f"{server.server_name} 已离线"

    return "up", f"{server.server_name} 已上线"


# ----------------------------------------------------------------------
# 用户进出检测（join_leave_interval 节奏，快，带防抖）
# ----------------------------------------------------------------------


def detect_join_leave(
    server: ServerConfig,
    state: ServerState,
    snapshot: ServerSnapshot,
    *,
    now: float,
    debounce_seconds: float,
) -> tuple[set[str], set[str], str | None]:
    """按 clid diff 用户进出，带防抖，返回 ``(confirmed_joins, confirmed_leaves, message)``。

    身份主键是 ``ClientInfo.clid``（昵称不唯一且可随时修改）：
    - 出现新 clid → 进入 pending join，过防抖期后仍在才确认；
    - clid 消失 → 进入 pending leave（记住最后昵称），过防抖期后仍缺失才确认；
    - 同一 clid 只改昵称 → 静默更新基线昵称，不产生事件；
    - 重名用户各自独立统计，不会互相遮盖或互相抵消。

    首次基线的判据是 ``state.join_leave_initialized`` 哨兵（**不是** ``last_clients``
    是否为空）：空名单是合法状态，用它判空会导致空服务器上第一个连入的用户被反复
    当作基线吞掉。若 ``state.rebaseline_roster`` 为 True（确认离线后恢复），
    本函数只静默重建基线，不报告任何事件。

    Args:
        server: 当前服务器配置
        state: 运行时状态（持有 last_clients 与 pending_*）
        snapshot: 本次轮询的快照
        now: 当前时间戳（loop.time()）
        debounce_seconds: 防抖时长（秒）。在该时间内，疑似 join/leave 必须再出现一次
            才被确认为有效；否则视为抖动丢弃。

    Returns:
        (confirmed_joins, confirmed_leaves, message)：
        - ``confirmed_joins`` / ``confirmed_leaves`` 是新确认事件的昵称集合（按昵称去重，
          事件本身按 clid 统计；重名时集合只有一份，消息里逐个渲染）
        - ``message`` 是渲染好的通知文本（无变更时为 None）
    """
    # 昵称只用于展示：统一清理截断，空昵称用占位符避免消息出现空档。
    current: dict[int, str] = {
        client.clid: sanitize_nickname(client.nickname) or "未知"
        for client in snapshot.clients
    }

    # 恢复重基线：离线期间名单不可信，恢复后的第一份在线快照只重建基线。
    if state.rebaseline_roster:
        state.rebaseline_roster = False
        state.join_leave_initialized = True
        state.last_clients = dict(current)
        state.pending_joins.clear()
        state.pending_leaves.clear()
        return set(), set(), None

    # 首次基线建立：把当前在线用户直接记为 last；不报告任何 join/leave。
    #
    # 判据用专用哨兵 join_leave_initialized，**不能**用 `not last_clients`：
    # 空名单是合法状态（空服务器、全员离开），若拿它当"尚未初始化"的判据，
    # 则空服务器上第一个连入的人会被再次当作基线吞掉。
    if not state.join_leave_initialized:
        state.join_leave_initialized = True
        state.last_clients = dict(current)
        state.pending_joins.clear()
        state.pending_leaves.clear()
        return set(), set(), None

    last = state.last_clients

    # ---- join 防抖（按 clid） ----
    for clid, nick in current.items():
        if clid in last:
            # 同一用户改名：静默同步基线昵称，不算加入
            last[clid] = nick
            continue
        state.pending_joins.setdefault(clid, (now, nick))

    confirmed_join_names: list[str] = []
    for clid in list(state.pending_joins):
        first_seen, _nick = state.pending_joins[clid]
        current_nick = current.get(clid)
        if current_nick is None:
            # 进 pending 后又消失 → 抖动，丢弃
            del state.pending_joins[clid]
        elif now - first_seen >= debounce_seconds:
            del state.pending_joins[clid]
            last[clid] = current_nick
            confirmed_join_names.append(current_nick)
        else:
            # 未到防抖期：继续观察，同时记下最新昵称
            state.pending_joins[clid] = (first_seen, current_nick)

    # ---- leave 防抖（按 clid） ----
    for clid, nick in last.items():
        if clid not in current:
            # 记住最后已知昵称，用户离开后仍能报出名字
            state.pending_leaves.setdefault(clid, (now, nick))

    confirmed_leave_names: list[str] = []
    for clid in list(state.pending_leaves):
        first_seen, nick = state.pending_leaves[clid]
        if clid in current:
            # 进 pending 后又重新出现 → 短断连抖动，丢弃
            del state.pending_leaves[clid]
        elif now - first_seen >= debounce_seconds:
            del state.pending_leaves[clid]
            last.pop(clid, None)
            confirmed_leave_names.append(nick)
        # else: 还没到防抖期，继续观察

    # 消息按 clid 逐个渲染昵称（重名会重复出现）；返回集合天然去重，仅用于日志。
    confirmed_joins = set(confirmed_join_names)
    confirmed_leaves = set(confirmed_leave_names)

    # 渲染消息（纯文本，不带 emoji）；昵称逐个渲染但总数有上限，保证消息有界。
    lines: list[str] = []
    if confirmed_join_names:
        names = join_display_names(sorted(confirmed_join_names))
        lines.append(f"{server.server_name}: {names} 加入了服务器")
    if confirmed_leave_names:
        names = join_display_names(sorted(confirmed_leave_names))
        lines.append(f"{server.server_name}: {names} 离开了服务器")

    return confirmed_joins, confirmed_leaves, ("\n".join(lines) if lines else None)


def build_initial_states(servers: list[ServerConfig]) -> dict[str, ServerState]:
    """给每台服务器初始化一个空的 ServerState。"""
    return {server.key: ServerState() for server in servers}
