"""格式化与变更检测。

仿 ``astrbot_plugin_minecraft_multi_monitor/minecraft_multi_monitor/monitoring.py`` 的拆分：
- ``format_server_info`` 负责把 snapshot 渲染成可读文本
- ``observe_status`` 负责带防抖地判定 上下线切换 + 首次基线
- ``detect_join_leave`` 负责 diff 用户进出（带防抖）

样式约定：消息不带 emoji，用纯文本键值 + 头尾分隔线（区别于 minecraft 插件的
emoji 风格，分隔线常量见 :data:`ts_server_monitor.constants.SEPARATOR`）。

防抖设计：
    pending_joins / pending_leaves 是"待敲定"的事件 map：
    - 第一次轮询发现疑似 join/leave → 加入 pending
    - 第二次轮询还在 pending 中且仍符合条件 → 确认为有效事件，从 pending 删除
    - 第二次轮询 pending 中的事件已"消失"（用户又出现在线 / 又消失了）→ 从 pending 删除，丢弃该事件

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
    """把 snapshot 渲染为多行纯文本（无 emoji），供 /ts 查询 / 状态推送使用。"""
    if snapshot is None:
        return "获取服务器数据失败"

    st = status_text(snapshot.status)
    lines: list[str] = []

    if display_options.show_server_name:
        lines.append(f"服务器: {snapshot.name} [{st}]")
    else:
        lines.append(f"状态: {st}")

    if display_options.show_server_address:
        lines.append(f"地址: {snapshot.host}:{snapshot.query_port}")

    if display_options.show_server_platform:
        lines.append(f"平台: {snapshot.platform}")

    if display_options.show_server_version:
        lines.append(f"版本: {snapshot.version}")

    if display_options.show_online_clients:
        lines.append(
            f"在线人数: {snapshot.clients_online}/{snapshot.max_clients}"
        )

    if display_options.show_channels:
        lines.append(f"频道数: {snapshot.channels_online}")

    if display_options.show_uptime:
        lines.append(f"运行时间: {format_duration(snapshot.uptime)}")

    if display_options.show_client_list:
        if snapshot.clients:
            names = [c.nickname for c in snapshot.clients[:10]]
            line = f"在线用户: {'、'.join(names)}"
            if len(snapshot.clients) > 10:
                line += f" 等共 {len(snapshot.clients)} 人"
            lines.append(line)
        else:
            lines.append("在线用户: 无")

    if display_options.show_update_time:
        lines.append(f"更新时间: {snapshot.update_time}")

    if snapshot.error and snapshot.status != "online":
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
        # 已确认离线：清空用户进出基线 / 待确认事件，
        # 否则离线期间拉不到 clientlist，恢复后会把整段时间误报成“全员离开”。
        state.last_client_nicknames = []
        state.last_clients_online = None
        state.pending_joins.clear()
        state.pending_leaves.clear()
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
    """diff 用户进出，带防抖，返回 ``(confirmed_joins, confirmed_leaves, message)``。

    Args:
        server: 当前服务器配置
        state: 运行时状态（持有 last_* 与 pending_*）
        snapshot: 本次轮询的快照
        now: 当前时间戳（loop.time()）
        debounce_seconds: 防抖时长（秒）。在该时间内，疑似 join/leave 必须再出现一次
            才被确认为有效；否则视为抖动丢弃。

    Returns:
        (confirmed_joins, confirmed_leaves, message)：
        - ``confirmed_joins`` / ``confirmed_leaves`` 是新确认的事件（已从 pending 移除）
        - ``message`` 是渲染好的通知文本（无变更时为 None）
    """
    current_nicks = {c.nickname for c in snapshot.clients}

    # 首次基线建立：把当前在线用户直接记为 last；不报告任何 join/leave
    if not state.last_client_nicknames:
        state.last_client_nicknames = sorted(current_nicks)
        state.last_clients_online = snapshot.clients_online
        return set(), set(), None

    last_set = set(state.last_client_nicknames)

    # ---- join 防抖 ----
    new_joins = current_nicks - last_set  # 当前轮询看到的新用户
    for nick in new_joins:
        state.pending_joins.setdefault(nick, now)

    # 上一轮已在 pending 的 join：若用户仍在 current_nicks 中 → 确认
    confirmed_joins: set[str] = set()
    for nick in list(state.pending_joins.keys()):
        in_still = nick in current_nicks
        elapsed = now - state.pending_joins[nick]
        if in_still and elapsed >= debounce_seconds:
            confirmed_joins.add(nick)
            del state.pending_joins[nick]
        elif not in_still:
            # 用户进 pending 后又消失了 → 抖动，丢弃
            del state.pending_joins[nick]
        # else: 还没到防抖期，继续观察

    # ---- leave 防抖 ----
    new_leaves = last_set - current_nicks  # 当前轮询看不到的旧用户
    for nick in new_leaves:
        state.pending_leaves.setdefault(nick, now)

    confirmed_leaves: set[str] = set()
    for nick in list(state.pending_leaves.keys()):
        still_missing = nick not in current_nicks
        elapsed = now - state.pending_leaves[nick]
        if still_missing and elapsed >= debounce_seconds:
            confirmed_leaves.add(nick)
            del state.pending_leaves[nick]
        elif not still_missing:
            # 用户进 pending 后又重新出现 → 抖动（短暂断连），丢弃
            del state.pending_leaves[nick]
        # else: 还没到防抖期，继续观察

    # 更新基线（只把 confirmed 事件纳入基线，避免误删抖动用户）
    next_nicks = sorted(
        (last_set | confirmed_joins) - confirmed_leaves
    )
    state.last_client_nicknames = next_nicks
    state.last_clients_online = snapshot.clients_online

    # 渲染消息（纯文本，不带 emoji）
    lines: list[str] = []
    if confirmed_joins:
        lines.append(
            f"{server.server_name}: {'、'.join(sorted(confirmed_joins))} 加入了服务器"
        )
    if confirmed_leaves:
        lines.append(
            f"{server.server_name}: {'、'.join(sorted(confirmed_leaves))} 离开了服务器"
        )

    return confirmed_joins, confirmed_leaves, ("\n".join(lines) if lines else None)


def build_initial_states(servers: list[ServerConfig]) -> dict[str, ServerState]:
    """给每台服务器初始化一个空的 ServerState。"""
    return {server.key: ServerState() for server in servers}
