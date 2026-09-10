"""AstrBot TeamSpeak 3 服务器监控插件。

v3.1.1（本轮改动）：
- 聊天指令全部加 ``ts`` 前缀，避免与 astrbot_plugin_mc_server_guard 等插件的
  同名指令冲突：``/ts_query``、``/ts_push_target``、``/ts_push_test``。

v3.1.0（通知开关拆分 + 删除定时通知）：
- **通知开关拆分**：``enable_status_push``（服务器 上线/下线 切换通知）与
  ``enable_startup_notify``（监控启动通知，“监控已启动”）现在是两个独立开关，
  可分别开关；``enable_startup_notify`` 默认关。
- **删除定时通知**：移除 ``enable_periodic_report``（定期状态快照）与
  ``global_status_interval`` / 单服务器 ``status_interval`` 配置。
  上下线/启动判定直接复用用户进出轮询（默认 15s）的采样，不再有每小时
  一次的多余状态轮询（也更不容易触发 TS3 防洪）。

v3.0.0（稳定性与 UMO 推送目标）：
- **上下线滞回判定**：单次拉取失败（TS3 防洪 / 瞬时断网）不再直接推“已离线”，
  需连续 ``confirm_offline`` 次失败采样才判定离线（恢复在线同理）。见
  ``ts_server_monitor.monitoring.observe_status``。
- **单轮询单连接**：用户进出与健康检测共用同一次 ServerQuery 拉取，消除每小时
  状态检查与 15s 用户轮询在同一秒连开两条连接导致的 TS3 防洪（error id 524）。
- **离线快照不参与用户 diff**：拉取失败产生的离线快照不再触发“全员离开/加入”。
- **连续失败降频**：服务器失联后用户轮询自动退避到 60s，减少无效连接与日志。
- **生命周期竞态修复**：延迟自动启动任务被持有并在 terminate 取消，杜绝
  “插件重载后旧实例残留 monitor 循环 → 双份通知 / 双倍防洪”。
- **推送目标支持 UMO**：``/ts_push_target <UMO>``（如 ``atri:GroupMessage:1092815819``）
  或 ``/ts_push_target <QQ群号>``、``/ts_push_target 本群``、``/ts_push_target 清除``；
  聊天下令设置的目标持久化于 ``data/plugin_data/.../relay_state.json``，
  优先级高于 WebUI 面板的 ``target_umo`` / ``target_group``。
- 发送统一走 ``context.send_message(umo, ...)``（官方主动消息 API）；
  UMO 为 ``GroupMessage`` + 纯数字群号时失败会自动回退旧版 ``send_group_msg``。

架构（v2.0.0 起沿用）仿 ``astrbot_plugin_minecraft_multi_monitor`` 模块拆分，
服务器配置 / 启停仍通过 AstrBot WebUI（``_conf_schema.json``）管理。
"""

from __future__ import annotations

import asyncio
import os
import sys

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from ts_server_monitor.clients import build_status_client
from ts_server_monitor.config import (
    load_servers_from_config,
    load_settings,
    parse_umo,
)
from ts_server_monitor.constants import (
    DEGRADE_AFTER_FAILURES,
    DEGRADED_POLL_INTERVAL,
    DEFAULT_STATUS_CONFIRM_OFFLINE,
    DEFAULT_STATUS_CONFIRM_ONLINE,
)
from ts_server_monitor.models import ServerConfig, ServerSnapshot, ServerState
from ts_server_monitor.monitoring import (
    build_initial_states,
    detect_join_leave,
    format_server_info,
    observe_status,
    wrap_notice,
)
from ts_server_monitor.services import describe_target, notify
from ts_server_monitor.state import (
    clear_runtime_target,
    load_runtime_target,
    save_runtime_target,
)


def _command_rest(event: AstrMessageEvent) -> str:
    """取 ``/命令`` 之后的参数字符串（不含命令本身）。"""
    raw = (getattr(event, "message_str", None) or "").strip()
    parts = raw.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


# AstrBot 4.27.5 (PyPI wheel): `register` lives in `astrbot.api.star`, not in
# `astrbot.api.event.filter`. The decorator is marked deprecated but still works.
@register(
    "astrbot_plugin_ts3_server_guard",
    "duanduanjujiu",
    "TeamSpeak 3 多服务器监控插件",
    "3.1.1",
    repo="https://github.com/duanduanjujiu/astrbot_plugin_ts3_server_guard",
)
class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}

        self.settings = load_settings(self.config)
        self.task: asyncio.Task | None = None
        self._delayed_start_task: asyncio.Task | None = None
        self._terminated = False

        # ---- 推送目标（聊天下令的运行时覆盖 > WebUI 面板配置）----
        runtime_target = load_runtime_target()
        self._umo_override: str | None = runtime_target.umo
        self._group_override: str | None = runtime_target.group_id

        self.servers: list[ServerConfig] = load_servers_from_config(
            self.config,
            self.settings.global_join_leave_interval,
        )
        self.server_states = build_initial_states(self.servers)
        self.server_locks = {server.key: asyncio.Lock() for server in self.servers}
        self.status_client = build_status_client()

        logger.info(
            "TS3 多服务器监控插件已加载，"
            f"推送目标: {self._target_desc()}, 自动启动: {self.settings.enable_auto_monitor}, "
            f"服务器数: {len(self.servers)}"
        )

        # 只要配置了服务器就自动启动监控：推送目标可在运行期用
        # /ts_push_target <UMO|群号> 动态设置，无需再要求配置里先填好群号。
        if self.settings.enable_auto_monitor and self.servers:
            self._delayed_start_task = asyncio.create_task(self._delayed_auto_start())

    async def initialize(self) -> None:
        logger.info("TS3 多服务器监控插件初始化完成")

    async def terminate(self) -> None:
        """停止插件：取消延迟启动 + 主监控任务，避免残留僵尸协程。"""
        self._terminated = True
        pending = [
            t
            for t in (self._delayed_start_task, self.task)
            if t is not None and not t.done()
        ]
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - 兜底，不影响框架卸载
                logger.warning(f"TS3 任务终止时异常: {exc!r}")
        logger.info("TS3 插件已停止（监控任务已取消）")

    # ------------------------------------------------------------------
    # 推送目标解析
    # ------------------------------------------------------------------

    def _effective_umo(self) -> str | None:
        """运行时（聊天下令）覆盖 > WebUI target_umo。"""
        return self._umo_override or self.settings.target_umo or None

    def _effective_group_id(self) -> str | None:
        """运行时（聊天下令）覆盖 > WebUI target_group。"""
        return self._group_override or self.settings.target_group or None

    def _target_desc(self) -> str:
        return describe_target(
            umo=self._effective_umo(), group_id=self._effective_group_id()
        )

    # ------------------------------------------------------------------
    # 生命周期 / 自动启动
    # ------------------------------------------------------------------

    async def _delayed_auto_start(self) -> None:
        """延迟几秒再启动，避免与 AstrBot 平台适配器初始化抢资源。"""
        try:
            await asyncio.sleep(5)
            if self._terminated:
                return  # 插件已被重载/卸载，放弃启动（防僵尸 monitor）
            if self.task is None or self.task.done():
                self.task = asyncio.create_task(self.monitor_loop())
                # 给 task 挂一个完成回调，任何未捕获异常都能打日志（否则静默消失）
                self.task.add_done_callback(self._on_monitor_task_done)
                logger.info("TS3 多服务器监控已自动启动")
        except asyncio.CancelledError:
            logger.info("TS3 延迟自动启动被取消")
            raise
        except Exception as exc:
            logger.error(f"TS3 自动启动失败: {exc}", exc_info=True)

    def _on_monitor_task_done(self, fut: asyncio.Task) -> None:
        """monitor task 结束时回调：正常取消不打日志，异常则打完整堆栈。"""
        if fut.cancelled():
            logger.info("TS3 monitor 任务被取消")
            return
        try:
            exc = fut.exception()
        except asyncio.CancelledError:
            logger.info("TS3 monitor 任务被取消")
            return
        if exc is not None:
            logger.error(
                f"TS3 monitor 任务异常退出: {exc!r}",
                exc_info=exc,
            )

    # ------------------------------------------------------------------
    # 数据获取与变更检测
    # ------------------------------------------------------------------

    async def _fetch_server_data(
        self, server: ServerConfig, source: str
    ) -> ServerSnapshot | None:
        """拉取单台服务器的状态；拉取失败时返回 ``status='offline'`` 的快照。"""
        from datetime import datetime

        lock = self.server_locks.setdefault(server.key, asyncio.Lock())
        async with lock:
            snapshot = await self.status_client.fetch_status(server, source)
            if snapshot is not None:
                return snapshot

            now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return ServerSnapshot(
                key=server.key,
                name=server.server_name,
                host=server.host,
                query_port=server.query_port,
                status="offline",
                platform="未知",
                version="未知",
                clients_online=0,
                max_clients=0,
                channels_online=0,
                uptime=0,
                update_time=now_text,
                error="TS3 ServerQuery 查询失败（超时或凭据错误）",
            )

    async def get_all_server_status_text(self) -> str:
        if not self.servers:
            return "当前没有启用的服务器配置"

        blocks: list[str] = []
        for index, server in enumerate(self.servers, start=1):
            snapshot = await self._fetch_server_data(server, source="manual")
            blocks.append(
                f"[{index}] {server.server_name}\n"
                f"{format_server_info(snapshot, self.settings.display_options)}"
            )
        return "\n\n".join(blocks)

    async def monitor_loop(self) -> None:
        """主监控循环。

        对每台服务器：每个轮询节拍（默认 15s，失联自动退避）至多建立一条
        ServerQuery 连接，快照同时喂给三件事（v3.1.0 起不再有独立的每小时
        状态轮询 / 定时通知）：
        1. 用户进出检测（带防抖；离线快照**不参与用户 diff**）；
        2. 上下线切换检测（带滞回，受 ``enable_status_push`` 控制）；
        3. 监控启动通知（仅在监控刚启动且服务器在线时发一次，
           受 ``enable_startup_notify`` 控制，默认关）。

        一条拉取 = 一次 ServerQuery 连接（serverinfo + clientlist + channellist）。
        """
        logger.info("TS3 monitor_loop 已启动")
        try:
            while True:
                await asyncio.sleep(1)
                if not self.servers:
                    continue
                for server in list(self.servers):
                    state = self.server_states.setdefault(
                        server.key, ServerState()
                    )
                    try:
                        await self._monitor_tick(server, state)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # 单台服务器故障不影响其它服务器
                        logger.error(
                            f"[{server.server_name}] 单服务器轮询异常: {exc!r}",
                            exc_info=True,
                        )
        except asyncio.CancelledError:
            logger.info("TS3 监控循环已取消")
            raise
        except Exception as exc:
            logger.error(f"TS3 监控循环异常: {exc!r}", exc_info=True)

    async def _monitor_tick(self, server: ServerConfig, state: ServerState) -> None:
        """单台服务器的一个轮询节拍：至多拉取一次快照并分发到各检测器。"""
        loop = asyncio.get_running_loop()
        now = loop.time()

        # 用户进出轮询（失联降频：连续失败后退避，减少无效连接）
        join_interval: float = float(server.join_leave_interval)
        if state.consecutive_failures >= DEGRADE_AFTER_FAILURES:
            join_interval = max(join_interval, DEGRADED_POLL_INTERVAL)
        if now - state.last_join_leave_check_ts < join_interval:
            return
        # 先记账再拉取：拉取耗时（超时）不拖慢下次轮询节奏
        state.last_join_leave_check_ts = now

        snapshot = await self._fetch_server_data(server, source="join_leave")
        if snapshot is None:
            return

        # 健康统计：任何一次拉取的成功/失败都计入（用于降频与上下线判定）
        if snapshot.status == "online":
            state.consecutive_failures = 0
            state.last_success_ts = now
        else:
            state.consecutive_failures += 1

        # 上下线 / 启动事件（带滞回）—— 只要任一相关开关开启就维持健康基线
        change_msg: str | None = None
        status_observing = (
            self.settings.enable_status_push or self.settings.enable_startup_notify
        )
        if status_observing:
            kind, msg = observe_status(
                server,
                state,
                snapshot,
                confirm_online=DEFAULT_STATUS_CONFIRM_ONLINE,
                confirm_offline=DEFAULT_STATUS_CONFIRM_OFFLINE,
            )
            if msg:
                # 按事件类型分别受两个开关控制
                if kind in ("up", "down") and not self.settings.enable_status_push:
                    msg = None
                elif kind == "startup" and not self.settings.enable_startup_notify:
                    msg = None
            change_msg = msg

        # 用户进出：只有确认服务器在线（快照可信）才做 diff
        if snapshot.status == "online":
            await self._run_join_leave(server, state, snapshot, now)

        if change_msg:
            logger.info(
                f"[status] [{server.server_name}] 检测到状态变化: {change_msg}"
            )
            await self._push_status_change(snapshot, change_msg)

    async def _run_join_leave(
        self,
        server: ServerConfig,
        state: ServerState,
        snapshot: ServerSnapshot,
        now: float,
    ) -> None:
        """用户进出检测（快照必须为 online，由调用方保证）。"""
        joined, left, message = detect_join_leave(
            server,
            state,
            snapshot,
            now=now,
            debounce_seconds=self.settings.global_join_leave_debounce,
        )
        if joined:
            logger.info(
                f"[join_leave] [{server.server_name}] 确认加入: {sorted(joined)}"
            )
        if left:
            logger.info(
                f"[join_leave] [{server.server_name}] 确认离开: {sorted(left)}"
            )
        if not message:
            return

        detail = format_server_info(snapshot, self.settings.display_options)
        final_message = wrap_notice(
            "TS3 用户进出",
            message,
            "当前状态：\n" + detail,
        )
        sent = await self._send_notification(final_message)
        if not sent:
            logger.error(
                f"[join_leave] [{server.server_name}] 用户变动已识别，但群消息发送失败"
            )

    # ------------------------------------------------------------------
    # 通知发送
    # ------------------------------------------------------------------

    async def _push_status_change(
        self, snapshot: ServerSnapshot, change_msg: str
    ) -> None:
        detail = format_server_info(snapshot, self.settings.display_options)
        final_message = wrap_notice(
            "TS3 服务器状态",
            change_msg,
            "当前状态：\n" + detail,
        )
        await self._send_notification(final_message)

    async def _send_notification(self, final_text: str) -> bool:
        """把通知文本发到当前生效的推送目标（UMO 优先，其次 QQ 群号）。"""
        umo = self._effective_umo()
        group_id = self._effective_group_id()
        sent = await notify(
            self.context, final_text, umo=umo, group_id=group_id
        )
        if not sent:
            logger.error(f"通知发送失败，当前推送目标: {describe_target(umo=umo, group_id=group_id)}")
        return sent

    async def _send_test(
        self, *, umo: str | None, group_id: str | None, cause: str
    ) -> str:
        """设置目标后立即发一条测试消息，返回给用户看的回执文本。"""
        if umo:
            dest = f"UMO: {umo}"
        elif group_id:
            dest = f"QQ群: {group_id}"
        else:
            return "❌ 目标为空，设置失败。"
        final_message = wrap_notice(
            "TS3 推送测试",
            f"推送目标已设置（来源：{cause}）。\n当前目标: {dest}\n这是一条测试消息。",
        )
        sent = await self._send_notification(final_message)
        if sent:
            return f"✅ 设置成功，已向新目标发送测试消息（请查收）。\n当前推送目标: {dest}"
        return (
            f"⚠️ 目标已保存，但测试消息发送失败（请查看 AstrBot 日志）。\n"
            f"当前推送目标: {dest}\n"
            f"请确认 UMO/群号正确、机器人已连接该平台、且机器人未被该群禁言。"
        )

    # ------------------------------------------------------------------
    # 聊天命令
    # ------------------------------------------------------------------

    @filter.command("ts_query")
    async def query_server_status(self, event: AstrMessageEvent):
        """立即拉取所有启用服务器的状态（不推送群，只在当前会话返回文本）。"""
        text = await self.get_all_server_status_text()
        yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ts_push_target")
    async def set_push_target(self, event: AstrMessageEvent):
        """查看 / 设置通知推送目标。支持输入 UMO 或 QQ 群号。"""
        rest = _command_rest(event)

        if not rest:
            yield event.plain_result(self._target_help_text())
            return

        lowered = rest.lower()
        if lowered in {"清除", "clear", "reset", "删除", "取消"}:
            clear_runtime_target()
            self._umo_override = None
            self._group_override = None
            yield event.plain_result(
                "已清除聊天下令设置的推送目标，恢复使用 WebUI 面板配置 "
                "(target_umo / target_group)。\n\n"
                f"当前生效推送目标: {self._target_desc()}"
            )
            return

        if lowered in {"本群", "当前", "这里", "当前会话", "here"}:
            umo = (getattr(event, "unified_msg_origin", None) or "").strip()
            if not umo:
                yield event.plain_result(
                    "❌ 无法获取当前会话的 UMO（该平台可能不支持主动消息），"
                    "请改用 /ts_push_target <UMO> 手动指定。"
                )
                return
            save_runtime_target(umo=umo)
            self._umo_override = umo
            self._group_override = None
            reply = await self._send_test(umo=umo, group_id=None, cause="绑定当前会话")
            yield event.plain_result(reply)
            return

        umo = parse_umo(rest)
        if umo:
            save_runtime_target(umo=umo)
            self._umo_override = umo
            self._group_override = None
            reply = await self._send_test(umo=umo, group_id=None, cause="输入 UMO")
            yield event.plain_result(reply)
            return

        if rest.isdigit():
            save_runtime_target(group_id=rest)
            self._group_override = rest
            self._umo_override = None
            reply = await self._send_test(umo=None, group_id=rest, cause="输入 QQ 群号")
            yield event.plain_result(reply)
            return

        yield event.plain_result(
            "❌ 无法识别的参数。\n\n" + self._target_usage_text()
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ts_push_test")
    async def push_test(self, event: AstrMessageEvent):
        """向当前推送目标发送一条测试消息。"""
        umo = self._effective_umo()
        group_id = self._effective_group_id()
        if not umo and not group_id:
            yield event.plain_result(
                "❌ 当前未配置推送目标。\n"
                "请先使用 /ts_push_target <UMO 或 QQ群号> 设置。"
            )
            return
        final_message = wrap_notice(
            "TS3 推送测试",
            "这是一条来自 TS3 监控插件的测试消息。\n"
            f"当前推送目标: {self._target_desc()}",
        )
        sent = await self._send_notification(final_message)
        if sent:
            yield event.plain_result("✅ 测试消息已发送，请到目标会话查收。")
        else:
            yield event.plain_result(
                "❌ 测试消息发送失败（请查看 AstrBot 日志）。\n"
                f"当前推送目标: {self._target_desc()}"
            )

    # ------------------------------------------------------------------
    # 命令辅助文案
    # ------------------------------------------------------------------

    def _target_usage_text(self) -> str:
        return (
            "【用法】（管理员）\n"
            "/ts_push_target <UMO>        按 UMO 设置，例如：\n"
            "                             /ts_push_target atri:GroupMessage:1092815819\n"
            "/ts_push_target <QQ群号>     兼容旧版纯群号，例如：/ts_push_target 123456789\n"
            "/ts_push_target 本群         把当前会话设为推送目标\n"
            "/ts_push_target 清除         恢复使用 WebUI 面板配置\n"
            "/ts_push_test                 向当前目标发送测试消息"
        )

    def _target_help_text(self) -> str:
        return (
            f"当前生效推送目标: {self._target_desc()}\n\n"
            + self._target_usage_text()
        )


__all__ = ["MyPlugin"]
