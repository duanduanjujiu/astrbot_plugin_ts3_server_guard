"""AstrBot TeamSpeak 3 服务器监控插件。

v3.5.0（本轮优化，插件市场上架审查项）：
- **纯 asyncio ServerQuery 客户端**：移除 py-ts3 与 telnetlib 依赖，协议读写全部
  在事件循环内异步完成；不再压制任何第三方库日志，并且可运行在 Python 3.13+。
- 协议解析、TS3 转义、错误码（520/524/768/2568）提示、持久连接与重连语义保持不变。

v3.4.0（上一轮优化）：
- **持久连接**：每台服务器在客户端内复用一条 ServerQuery 连接（login 只做一次），
  空闲过久发送 keepalive，连接失效时重连一次；避免每 15s 重复 TCP+login 触发防洪。
- **并发轮询**：每轮用 ``asyncio.gather`` 并发执行所有服务器的轮询节拍，
  慢或离线服务器不再拖慢其它服务器；单台异常在协程内部隔离。
- **查询缓存与限频**：监控与手动查询都会写快照缓存，``/ts_query`` 在 TTL
  （``max(QUERY_CACHE_TTL_MIN, join_leave_interval*1.5)``）内直接复用缓存，
  同一会话 ``QUERY_COOLDOWN_SECONDS`` 内重复查询会被拒绝。
- **用户进出按 clid 识别**：同名用户可独立播报；同一 clid 改名静默更新基线；
  确认离线后恢复在线时静默重建名单，不再把全员补报为“加入”。
- **用户进出通知开关**：``enable_join_leave_notify=False`` 时只维护进出基线、
  不播报通知；其它通知开关语义不变。
- **展示优化**：离线状态人数/平台等显示 “—”；地址优先展示语音端口（括号标注
  ServerQuery 端口）；昵称清洗 + 截断，避免换行注入和超长消息。
- **恢复探测加速**：已确认离线的服务器降频探测定为 30s（其余失联场景仍 60s）。
- **旧版群号回退加固**：只在 AIOCQHTTP 上查找可用客户端，平台/客户端只解析
  一次即复用，重试 ``send_group_msg`` 不再重复扫描适配器。

v3.3.0（上一轮修复）：
- **包内相对导入**：改为 ``from .ts_server_monitor...`` 并移除 ``sys.path`` 注入，
  AstrBot 重载插件时子模块会随 ``data.plugins.<dir>.*`` 一并清理，不再残留旧代码。
- **推送目标解析收敛**：新增纯函数 :func:`resolve_target`，运行时覆盖优先于 WebUI
  配置且 UMO 与群号互斥，修复“WebUI 设了 target_umo 时，聊天下令设置的群号
  实际不生效、却仍回复设置成功”的问题。
- **先测试后保存**：``/ts_push_target`` 先向新目标发测试消息，发送成功才持久化
  并切换目标；失败不落盘、不改当前目标，并在回复中说明排查方向。
- **通知可靠投递**：通知先进入有界队列，由后台 worker 串行发送（超时 +
  指数退避重试），轮询循环不再被单次发送阻塞；短暂发送失败会自动重试。
- **名单可信门控**：``clientlist`` 拉取失败（``roster_ok=False``）时跳过用户
  进出 diff，避免误报“全员离开/加入”；上下线滞回检测不受影响。
- ``terminate()`` 额外取消通知 worker 并关闭状态查询客户端，保持无僵尸任务。

架构仿 ``astrbot_plugin_minecraft_multi_monitor`` 模块拆分；服务器配置 / 启停
通过 AstrBot WebUI（``_conf_schema.json``）管理。指令：``/ts_query``、
``/ts_push_target``、``/ts_push_test``（后两者仅管理员）。
"""

from __future__ import annotations

import asyncio
import math

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .ts_server_monitor.clients import build_status_client
from .ts_server_monitor.config import (
    load_servers_from_config,
    load_settings,
    parse_umo,
)
from .ts_server_monitor.constants import (
    DEFAULT_STATUS_CONFIRM_OFFLINE,
    DEFAULT_STATUS_CONFIRM_ONLINE,
    DEGRADE_AFTER_FAILURES,
    DEGRADED_OFFLINE_PROBE_INTERVAL,
    DEGRADED_POLL_INTERVAL,
    NOTIFY_MAX_ATTEMPTS,
    NOTIFY_QUEUE_MAX,
    NOTIFY_SEND_TIMEOUT,
    QUERY_CACHE_TTL_MIN,
    QUERY_COOLDOWN_SECONDS,
)
from .ts_server_monitor.models import ServerConfig, ServerSnapshot, ServerState
from .ts_server_monitor.monitoring import (
    build_initial_states,
    detect_join_leave,
    format_server_info,
    observe_status,
    wrap_notice,
)
from .ts_server_monitor.services import describe_target, notify
from .ts_server_monitor.state import (
    clear_runtime_target,
    load_runtime_target,
    save_runtime_target,
)


def resolve_target(
    umo_override: str | None,
    group_override: str | None,
    config_umo: str | None,
    config_group: str | None,
) -> tuple[str | None, str | None]:
    """解析当前生效的推送目标（纯函数，不读写任何状态）。

    优先级：运行时 UMO 覆盖 > 运行时群号覆盖 > WebUI ``target_umo`` >
    WebUI ``target_group``；UMO 与群号互斥，至多一个非空。

    Returns:
        ``(umo, group_id)``，两者的组合中至多一个非空。
    """
    if umo_override:
        return umo_override, None
    if group_override:
        return None, group_override
    if config_umo:
        return config_umo, None
    return None, config_group or None


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
    "3.5.0",
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

        # 通知队列：轮询循环只负责入队，实际发送交给 _notify_worker。
        self._notify_queue: asyncio.Queue = asyncio.Queue(
            maxsize=NOTIFY_QUEUE_MAX
        )
        self._notify_task: asyncio.Task | None = None
        # 已提示过“名单不可信”的服务器 key，名单恢复后移除以便下次再提示。
        self._roster_warned: set[str] = set()

        # /ts_query 快照缓存：server.key -> (最新快照, 采集时刻 loop.time())。
        # 监控轮询与手动查询都会写入，查询在 TTL 内直接复用，避免重复建连。
        self._snapshots: dict[str, tuple[ServerSnapshot, float]] = {}
        # /ts_query 按会话 UMO 限频：umo -> 上次查询时刻 loop.time()。
        self._query_cooldown: dict[str, float] = {}

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
        """停止插件：取消延迟启动 + 主监控 + 通知 worker，并关闭查询客户端。"""
        self._terminated = True
        pending = [
            t
            for t in (self._delayed_start_task, self.task, self._notify_task)
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

        close = getattr(self.status_client, "close", None)
        if close is not None:
            try:
                await close()
            except Exception as exc:  # noqa: BLE001 - 关闭失败不阻塞卸载
                logger.warning(f"TS3 状态客户端关闭失败: {exc!r}")

        logger.info("TS3 插件已停止（监控任务已取消）")

    # ------------------------------------------------------------------
    # 推送目标解析
    # ------------------------------------------------------------------

    def _effective_target(self) -> tuple[str | None, str | None]:
        """当前生效的 ``(umo, group_id)``，两者至多一个非空。"""
        return resolve_target(
            self._umo_override,
            self._group_override,
            self.settings.target_umo,
            self.settings.target_group,
        )

    def _effective_umo(self) -> str | None:
        """当前生效的 UMO（没有 UMO 目标时为 None）。"""
        return self._effective_target()[0]

    def _effective_group_id(self) -> str | None:
        """当前生效的 QQ 群号（没有群号目标时为 None）。"""
        return self._effective_target()[1]

    def _target_desc(self) -> str:
        umo, group_id = self._effective_target()
        return describe_target(umo=umo, group_id=group_id)

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
                # 通知 worker 与监控循环同时启动；后续重试由 _deliver 内部完成。
                if self._notify_task is None or self._notify_task.done():
                    self._notify_task = asyncio.create_task(self._notify_worker())
                    self._notify_task.add_done_callback(self._on_notify_task_done)
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

    def _on_notify_task_done(self, fut: asyncio.Task) -> None:
        """通知 worker 结束时回调：正常取消不打日志，异常则打完整堆栈。"""
        if fut.cancelled():
            return
        try:
            exc = fut.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.error(
                f"TS3 通知 worker 异常退出: {exc!r}",
                exc_info=exc,
            )

    # ------------------------------------------------------------------
    # 数据获取与变更检测
    # ------------------------------------------------------------------

    async def _fetch_server_data(
        self, server: ServerConfig, source: str
    ) -> ServerSnapshot | None:
        """拉取单台服务器的状态。

        拉取失败时返回 ``status='offline'`` 的兜底快照；调用方负责把返回的
        快照写入 ``self._snapshots``，供 TTL 内的 ``/ts_query`` 复用。
        """
        from datetime import datetime

        lock = self.server_locks.setdefault(server.key, asyncio.Lock())
        async with lock:
            snapshot = await self.status_client.fetch_status(server, source)

        if snapshot is None:
            now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            snapshot = ServerSnapshot(
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
                roster_ok=False,
            )

        return snapshot

    def _remember_snapshot(self, server_key: str, snapshot: ServerSnapshot) -> None:
        """记录快照及采集时刻（monotonic），供 /ts_query 在 TTL 内复用。"""
        self._snapshots[server_key] = (
            snapshot,
            asyncio.get_running_loop().time(),
        )

    async def get_all_server_status_text(self) -> str:
        """渲染所有启用服务器的状态（TTL 内优先用缓存，减少查询连接数）。"""
        if not self.servers:
            return "当前没有启用的服务器配置"

        loop = asyncio.get_running_loop()
        blocks: list[str] = []
        for index, server in enumerate(self.servers, start=1):
            snapshot = await self._get_query_snapshot(server, loop.time())
            blocks.append(
                f"[{index}] {server.server_name}\n"
                f"{format_server_info(snapshot, self.settings.display_options)}"
            )
        return "\n\n".join(blocks)

    async def _get_query_snapshot(
        self, server: ServerConfig, now: float
    ) -> ServerSnapshot | None:
        """取一次查询用快照：缓存未过期直接用，否则实时拉取并写回缓存。"""
        ttl = max(
            QUERY_CACHE_TTL_MIN,
            float(server.join_leave_interval) * 1.5,
        )
        cached = self._snapshots.get(server.key)
        if cached is not None and now - cached[1] <= ttl:
            return cached[0]
        snapshot = await self._fetch_server_data(server, source="manual")
        if snapshot is not None:
            self._remember_snapshot(server.key, snapshot)
        return snapshot

    async def monitor_loop(self) -> None:
        """主监控循环。

        对每台服务器：每个轮询节拍（默认 15s，失联自动退避）至多建立一条
        ServerQuery 连接，快照同时喂给三件事（v3.1.0 起不再有独立的每小时
        状态轮询 / 定时通知）：
        1. 用户进出检测（带防抖；离线或名单不可信的快照**不参与用户 diff**）；
        2. 上下线切换检测（带滞回，受 ``enable_status_push`` 控制）；
        3. 监控启动通知（仅在监控刚启动且服务器在线时发一次，
           受 ``enable_startup_notify`` 控制，默认关）。

        一条拉取 = 一次 ServerQuery 连接（serverinfo + clientlist）。
        通知只入队，由 ``_notify_worker`` 串行发送，不阻塞本循环；每轮所有
        服务器并发轮询，慢服务器不会拖住其它服务器。
        """
        logger.info("TS3 monitor_loop 已启动")
        try:
            while True:
                await asyncio.sleep(1)
                if not self.servers:
                    continue
                await self._run_tick_round()
        except asyncio.CancelledError:
            logger.info("TS3 监控循环已取消")
            raise
        except Exception as exc:
            logger.error(f"TS3 监控循环异常: {exc!r}", exc_info=True)

    async def _run_tick_round(self) -> None:
        """并发执行一轮全部服务器的轮询，等待全部完成后再进入下一轮。"""
        coros = []
        for server in list(self.servers):
            state = self.server_states.setdefault(server.key, ServerState())
            coros.append(self._run_server_tick(server, state))
        if coros:
            # 单台服务器的异常在 _run_server_tick 内部隔离，不会中断整轮。
            await asyncio.gather(*coros)

    async def _run_server_tick(
        self, server: ServerConfig, state: ServerState
    ) -> None:
        """单台服务器轮询包装：异常隔离，取消信号继续向上传播。"""
        try:
            await self._monitor_tick(server, state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 单台服务器故障不影响其它服务器
            logger.error(
                f"[{server.server_name}] 单服务器轮询异常: {exc!r}",
                exc_info=True,
            )

    async def _monitor_tick(self, server: ServerConfig, state: ServerState) -> None:
        """单台服务器的一个轮询节拍：至多拉取一次快照并分发到各检测器。"""
        loop = asyncio.get_running_loop()
        now = loop.time()

        # 用户进出轮询（失联降频：连续失败后退避，减少无效连接）
        join_interval: float = float(server.join_leave_interval)
        if state.consecutive_failures >= DEGRADE_AFTER_FAILURES:
            # 已确认离线时用更短的恢复探测间隔，尽快发现服务器恢复在线；
            # 其余情况维持 60s 低频轮询，减少对失联/被防洪服务器的压力。
            degraded_interval = (
                DEGRADED_OFFLINE_PROBE_INTERVAL
                if state.health == "offline"
                else DEGRADED_POLL_INTERVAL
            )
            join_interval = max(join_interval, degraded_interval)
        if now - state.last_join_leave_check_ts < join_interval:
            return
        # 先记账再拉取：拉取耗时（超时）不拖慢下次轮询节奏
        state.last_join_leave_check_ts = now

        snapshot = await self._fetch_server_data(server, source="join_leave")
        if snapshot is None:
            return
        # 每份快照（在线/离线）都写入查询缓存，供 /ts_query 在 TTL 内复用。
        self._remember_snapshot(server.key, snapshot)

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

        # 先入队状态变化，再入队用户进出，保证恢复在线时“已上线”先于“用户加入”。
        if change_msg:
            logger.info(
                f"[status] [{server.server_name}] 检测到状态变化: {change_msg}"
            )
            await self._push_status_change(snapshot, change_msg)

        # 用户进出：只有确认服务器在线、且 clientlist 拉取成功（名单可信）才做 diff
        if snapshot.status == "online":
            if snapshot.roster_ok:
                self._roster_warned.discard(server.key)
                await self._run_join_leave(server, state, snapshot, now)
            elif server.key not in self._roster_warned:
                self._roster_warned.add(server.key)
                logger.warning(
                    f"[{server.server_name}] clientlist 拉取失败，客户端名单不可信，"
                    "本轮跳过用户进出检测"
                )

    async def _run_join_leave(
        self,
        server: ServerConfig,
        state: ServerState,
        snapshot: ServerSnapshot,
        now: float,
    ) -> None:
        """用户进出检测（快照必须为 online 且名单可信，由调用方保证）。"""
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

        # 通知开关只决定是否播报；基线/防抖状态始终由 detect_join_leave 维护，
        # 因此重新打开开关不会补发关闭期间的事件。
        if not self.settings.enable_join_leave_notify:
            return

        detail = format_server_info(snapshot, self.settings.display_options)
        final_message = wrap_notice(
            "TS3 用户进出",
            message,
            "当前状态：\n" + detail,
        )
        self._enqueue_notification(final_message)

    # ------------------------------------------------------------------
    # 通知发送（队列 + 后台 worker）
    # ------------------------------------------------------------------

    def _enqueue_notification(self, final_text: str) -> None:
        """把通知放进发送队列；目标在入队时确定，避免排队期间目标切换。"""
        umo, group_id = self._effective_target()
        item = (final_text, umo, group_id)
        try:
            self._notify_queue.put_nowait(item)
            return
        except asyncio.QueueFull:
            pass

        # 队列满：丢弃最早的一条，优先保证最新事件（如“已上线”）送达。
        try:
            dropped = self._notify_queue.get_nowait()
        except asyncio.QueueEmpty:
            dropped = None
        if dropped is not None:
            self._notify_queue.task_done()
            logger.warning("TS3 通知队列已满，已丢弃最早的一条通知")
        try:
            self._notify_queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.error("TS3 通知队列已满，最新通知仍未入队，已丢弃")

    async def _notify_worker(self) -> None:
        """后台通知 worker：串行消费队列，失败按指数退避重试。"""
        logger.info("TS3 通知队列 worker 已启动")
        try:
            while True:
                text, umo, group_id = await self._notify_queue.get()
                try:
                    await self._deliver_notification(text, umo, group_id)
                finally:
                    self._notify_queue.task_done()
        except asyncio.CancelledError:
            logger.info("TS3 通知队列 worker 已取消")
            raise

    async def _deliver_notification(
        self, final_text: str, umo: str | None, group_id: str | None
    ) -> bool:
        """发送单条通知：最多 ``NOTIFY_MAX_ATTEMPTS`` 次，失败按 1s、2s... 退避。"""
        for attempt in range(1, NOTIFY_MAX_ATTEMPTS + 1):
            try:
                sent = await asyncio.wait_for(
                    self._send_text(final_text, umo, group_id),
                    timeout=NOTIFY_SEND_TIMEOUT,
                )
                if sent:
                    return True
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.debug(
                    f"TS3 通知发送超时（第 {attempt}/{NOTIFY_MAX_ATTEMPTS} 次）"
                )
            except Exception as exc:  # noqa: BLE001 - 重试后仍失败才报错
                logger.debug(
                    f"TS3 通知发送异常（第 {attempt}/{NOTIFY_MAX_ATTEMPTS} 次）: "
                    f"{exc!r}"
                )
            if attempt < NOTIFY_MAX_ATTEMPTS:
                await asyncio.sleep(2 ** (attempt - 1))

        logger.error(
            f"TS3 通知发送失败（已尝试 {NOTIFY_MAX_ATTEMPTS} 次），"
            f"目标: {describe_target(umo=umo, group_id=group_id)}"
        )
        return False

    async def _push_status_change(
        self, snapshot: ServerSnapshot, change_msg: str
    ) -> None:
        """上下线切换通知：只入队，不等待实际发送。"""
        detail = format_server_info(snapshot, self.settings.display_options)
        final_message = wrap_notice(
            "TS3 服务器状态",
            change_msg,
            "当前状态：\n" + detail,
        )
        self._enqueue_notification(final_message)

    async def _send_text(
        self, text: str, umo: str | None, group_id: str | None
    ) -> bool:
        """用**显式**目标发送文本（不做当前生效目标解析）。"""
        return await notify(self.context, text, umo=umo, group_id=group_id)

    async def _send_test(
        self, *, umo: str | None, group_id: str | None, cause: str
    ) -> bool:
        """向显式的新目标发送一条测试消息，返回是否发送成功（不改任何状态）。"""
        dest = describe_target(umo=umo, group_id=group_id)
        final_message = wrap_notice(
            "TS3 推送测试",
            f"推送目标设置测试（来源：{cause}）。\n"
            f"测试目标: {dest}\n"
            f"这是一条测试消息。",
        )
        return await self._send_text(final_message, umo, group_id)

    # ------------------------------------------------------------------
    # 聊天命令
    # ------------------------------------------------------------------

    @filter.command("ts_query")
    async def query_server_status(self, event: AstrMessageEvent):
        """返回所有启用服务器状态（TTL 内复用缓存，并按会话限频）。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        now = asyncio.get_running_loop().time()
        remaining = self._register_query(umo, now)
        if remaining:
            yield event.plain_result(f"❌ 查询过于频繁，请 {remaining} 秒后再试")
            return
        text = await self.get_all_server_status_text()
        yield event.plain_result(text)

    def _register_query(self, umo: str, now: float) -> int:
        """登记一次查询；仍在冷却期时返回需等待的整数秒，否则返回 0。"""
        last = self._query_cooldown.get(umo)
        if last is not None:
            elapsed = now - last
            if elapsed < QUERY_COOLDOWN_SECONDS:
                return max(1, math.ceil(QUERY_COOLDOWN_SECONDS - elapsed))
        self._query_cooldown[umo] = now
        if len(self._query_cooldown) > 128:
            # 防止长期运行后按会话累积的字典无限增长：先剔除冷却期已过的
            # 会话；若仍超限则只保留最近 128 个，避免大量会话同时查询时膨胀。
            recent = {
                key: ts
                for key, ts in self._query_cooldown.items()
                if now - ts < QUERY_COOLDOWN_SECONDS
            }
            if len(recent) > 128:
                recent = dict(
                    sorted(recent.items(), key=lambda item: item[1])[-128:]
                )
            self._query_cooldown = recent
        return 0

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
            if not await self._send_test(
                umo=umo, group_id=None, cause="绑定当前会话"
            ):
                yield event.plain_result(
                    self._target_not_saved_text(umo=umo, group_id=None)
                )
                return
            save_runtime_target(umo=umo)
            self._umo_override = umo
            self._group_override = None
            yield event.plain_result(self._target_saved_text())
            return

        umo = parse_umo(rest)
        if umo:
            if not await self._send_test(umo=umo, group_id=None, cause="输入 UMO"):
                yield event.plain_result(
                    self._target_not_saved_text(umo=umo, group_id=None)
                )
                return
            save_runtime_target(umo=umo)
            self._umo_override = umo
            self._group_override = None
            yield event.plain_result(self._target_saved_text())
            return

        if rest.isdigit():
            if not await self._send_test(
                umo=None, group_id=rest, cause="输入 QQ 群号"
            ):
                yield event.plain_result(
                    self._target_not_saved_text(umo=None, group_id=rest)
                )
                return
            save_runtime_target(group_id=rest)
            self._group_override = rest
            self._umo_override = None
            yield event.plain_result(self._target_saved_text())
            return

        yield event.plain_result(
            "❌ 无法识别的参数。\n\n" + self._target_usage_text()
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ts_push_test")
    async def push_test(self, event: AstrMessageEvent):
        """向当前推送目标发送一条测试消息。"""
        umo, group_id = self._effective_target()
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
        sent = await self._send_text(final_message, umo, group_id)
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

    def _target_saved_text(self) -> str:
        return (
            "✅ 设置成功，已向新目标发送测试消息（请查收）。\n"
            f"当前推送目标: {self._target_desc()}"
        )

    def _target_not_saved_text(
        self, *, umo: str | None, group_id: str | None
    ) -> str:
        return (
            "❌ 测试消息发送失败，推送目标未保存（仍使用原目标）。\n"
            f"测试目标: {describe_target(umo=umo, group_id=group_id)}\n"
            f"当前推送目标: {self._target_desc()}\n"
            "请确认 UMO/群号正确、机器人已连接对应平台，且机器人未被该群禁言。"
        )

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
