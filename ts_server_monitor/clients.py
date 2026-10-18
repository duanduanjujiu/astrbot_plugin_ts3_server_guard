"""TS3 ServerQuery 异步客户端。

同步 ``ts3`` 库的所有网络读写都交给 ``LocalStatusClient`` 专属线程池执行，
避免阻塞事件循环。每台服务器在客户端内缓存一条持久 ServerQuery 连接
（TCP + login + use），轮询时复用；只有连接失效、或空闲过久发送 keepalive
后确认异常时，才丢弃旧连接并重连一次，避免每轮都新建连接触发 TS3 防洪。

单次查询（一次尝试）共享一个总超时 deadline：连接、两次 greeting、login、
use、keepalive、serverinfo、clientlist 的每个阻塞步骤都只拿到剩余时间，
因此任何一步卡住都会在 ``timeout`` 内失败返回，不会泄漏长期阻塞的线程。
重连尝试使用新的 deadline，故最坏耗时约为 ``2 * timeout``。

只请求 ``serverinfo``（权威数据）与 ``clientlist``（在线名单，可选）：
频道数取自 serverinfo 的 ``virtualserver_channelsonline``，不再请求 channellist。
``clientlist`` 失败不影响在线判定，仅在快照上标记 ``roster_ok=False``。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial
from typing import Any, NamedTuple

from astrbot.api import logger

from .constants import CLIENT_TYPE_REGULAR, DEFAULT_NETWORK_TIMEOUT
from .models import ClientInfo, ServerConfig, ServerSnapshot

# 单次尝试在总 deadline 之外额外留给线程收尾（发 quit、关闭连接）的余量。
_WAIT_FOR_GRACE = 2.0

# 连接空闲超过该秒数时发送 keepalive，避免被服务端按空闲连接断开。
IDLE_KEEPALIVE_SECONDS = 240.0

# 读取 keepalive 响应的最长等待；真实 TS3 立即回 error 行，不回包的服务端
# 也不至于把本次查询的 deadline 全耗光。
_KEEPALIVE_DRAIN_TIMEOUT = 0.5

# 线程池上限：每台服务器同一时刻只占一个线程（同连接内串行），不同服务器可并发。
_EXECUTOR_MAX_WORKERS = 8

# ts3 库是可选依赖：惰性导入，避免插件在缺包时整体加载失败。
_ts3_query: Any = None
_telnetlib: Any = None
_bounded_connection_cls: Any = None

# 常见 TS3 错误码 -> 可操作的排查提示。
_ERROR_ID_HINTS = {
    520: "认证失败，请检查 query_user / query_password",
    524: "触发 TS3 防洪，请把本机 IP 加入 query_ip_allowlist 或加大轮询间隔",
    768: "virtual_server_id 无效",
    2568: "查询账号缺少 clientlist 权限（b_virtualserver_client_list）",
}


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_int(value: Any, default: int = 0) -> int:
    """尽力把 ServerQuery 返回的字符串字段转成 int。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _remaining(deadline: float) -> float:
    """返回距离总 deadline 的剩余秒数；已超时则抛 TimeoutError。"""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("TS3 ServerQuery 查询总超时")
    return remaining


def _load_ts3() -> Any:
    """惰性导入 ts3，并构造带总超时的连接类（进程内只构造一次）。"""
    global _ts3_query, _telnetlib, _bounded_connection_cls
    if _bounded_connection_cls is not None:
        return _bounded_connection_cls

    import telnetlib

    import ts3
    import ts3.query

    # 库内部每次建连都会打 INFO，统一压到 WARNING（只做一次）。
    logging.getLogger("ts3.query").setLevel(logging.WARNING)

    class _BoundedTS3Connection(ts3.query.TS3Connection):
        """TS3Connection 子类：所有阻塞读都受同一个 deadline 约束。

        父类 ``open()`` 的两次 greeting 读取使用 ``read_until(timeout=None)``，
        服务器接受 TCP 却迟迟不发欢迎信息时会永久阻塞，故在此重写。
        """

        def open(
            self,
            host,
            port=10011,
            timeout=None,
            deadline=None,
        ):
            if self.is_connected():
                raise OSError("The client is already connected.")
            if deadline is None:
                if timeout is None:
                    timeout = DEFAULT_NETWORK_TIMEOUT
                deadline = time.monotonic() + timeout

            self._telnet_conn = _telnetlib.Telnet(
                host, port, timeout=_remaining(deadline)
            )
            try:
                # 两次 greeting（b"TS3\n\r" 与 Welcome 行）都必须读到数据。
                for _ in range(2):
                    greeting = self._telnet_conn.read_until(
                        b"\n\r", timeout=_remaining(deadline)
                    )
                    if not greeting:
                        raise TimeoutError("读取 TS3 ServerQuery 欢迎信息超时")
            except BaseException:
                self._telnet_conn.close()
                self._telnet_conn = None
                raise

            self._telnet_queue = []
            self._num_pending_queries = 0
            self._event_queue = []
            return None

    _ts3_query = ts3.query
    _telnetlib = telnetlib
    _bounded_connection_cls = _BoundedTS3Connection
    return _bounded_connection_cls


def _describe_error(exc: BaseException) -> str:
    """把 ts3 / 网络异常归类成可日志输出的一句话。"""
    if isinstance(exc, ImportError):
        return (
            "TS3 查询失败: 缺少 ts3 依赖（请执行 "
            "pip install -r requirements.txt 后重载插件）"
        )
    if _ts3_query is not None and isinstance(exc, _ts3_query.TS3QueryError):
        error = getattr(getattr(exc, "resp", None), "error", None) or {}
        error_id = str(error.get("id", "?"))
        message = str(error.get("msg", "unknown"))
        hint = _ERROR_ID_HINTS.get(_to_int(error_id, -1))
        text = f"TS3 查询失败: error id {error_id}: {message}"
        return f"{text}（{hint}）" if hint else text
    if isinstance(exc, TimeoutError):
        return "TS3 查询失败: 超时（总时限内未完成连接或响应）"
    if isinstance(exc, (OSError, EOFError)):
        return f"TS3 查询失败: 网络错误 {type(exc).__name__}: {exc}"
    return f"TS3 查询失败: {type(exc).__name__}: {exc}"


def _parse_client_list(entries: list[dict[str, str]]) -> list[ClientInfo]:
    """把 clientlist 响应解析成 ClientInfo 列表（只保留真实客户端）。"""
    clients: list[ClientInfo] = []
    for entry in entries:
        client_type = _to_int(
            entry.get("client_type"), CLIENT_TYPE_REGULAR
        )
        if client_type != CLIENT_TYPE_REGULAR:
            continue
        try:
            clid = int(entry.get("clid", ""))
            database_id = int(entry.get("client_database_id", ""))
            cid = int(entry.get("cid", ""))
        except (TypeError, ValueError):
            continue
        clients.append(
            ClientInfo(
                clid=clid,
                nickname=str(entry.get("client_nickname", "Unknown")),
                database_id=database_id,
                cid=cid,
                client_type=client_type,
            )
        )
    return clients


# -------- 底层同步 ts3 客户端（线程池内执行） --------


class _SyncTS3Client:
    """同步 TS3 ServerQuery 客户端（持久连接，只在专用线程池内调用）。

    由 ``LocalStatusClient`` 按 ``server.key`` 缓存复用。``lock`` 保证同一
    连接上的操作（含 close 时的 quit）不会并发进入 socket；连接建立、查询
    与退出都在该锁内完成。
    """

    def __init__(
        self, server: ServerConfig, timeout: float = DEFAULT_NETWORK_TIMEOUT
    ) -> None:
        self.timeout = timeout
        self.lock = threading.Lock()
        self._connection: Any = None
        self._deadline = 0.0
        self._last_used = 0.0
        self._target: tuple[Any, ...] = ()
        self.configure(server)

    def configure(self, server: ServerConfig) -> bool:
        """同步连接参数；参数变化时丢弃旧连接并返回 True。"""
        target = (
            server.host,
            server.query_port,
            server.query_user,
            server.query_password,
            server.virtual_server_id,
        )
        if target == self._target:
            return False
        self.quit()
        (
            self.host,
            self.query_port,
            self.query_user,
            self.query_password,
            self.virtual_server_id,
        ) = target
        self._target = target
        return True

    def is_connected(self) -> bool:
        connection = self._connection
        return connection is not None and connection.is_connected()

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_used

    def begin_attempt(self, deadline: float) -> None:
        """开始一次查询尝试：刷新本次尝试的总 deadline（复用连接时同样需要）。"""
        self._deadline = deadline

    def connect(self, deadline: float) -> None:
        """在给定 deadline 内建立连接并完成 login / use；失败时关闭连接。"""
        connection_cls = _load_ts3()
        self.quit()
        self.begin_attempt(deadline)
        connection = connection_cls()
        self._connection = connection
        try:
            connection.open(
                self.host,
                self.query_port,
                timeout=self.timeout,
                deadline=deadline,
            )
            # 不用库的 login()/use() 便捷方法：它们调用 send() 时 timeout=None，
            # 在防洪或网络黑洞下可能永久阻塞。
            self._send(
                "login",
                common_parameters={
                    "client_login_name": self.query_user,
                    "client_login_password": self.query_password,
                },
            )
            self._send(
                "use",
                common_parameters={"sid": self.virtual_server_id},
            )
        except BaseException:
            self.quit()
            raise

    def _send(
        self,
        command: str,
        common_parameters: dict[str, Any] | None = None,
    ) -> Any:
        if not self.is_connected():
            raise ConnectionError("TS3 连接已断开")
        resp = self._connection.send(
            command,
            common_parameters=common_parameters,
            timeout=_remaining(self._deadline),
        )
        self._last_used = time.monotonic()
        return resp

    def send_keepalive(self) -> None:
        """发送空查询保活，并在短超时内消费服务端响应。

        py-ts3 的 ``send_keepalive`` 不把空查询计入 ``_num_pending_queries``，
        若服务端回了 ``error`` 行又被 ``_recv`` 读到，计数会变成 -1，导致后续
        查询断言失败。这里显式读掉该响应并把计数复位，保证连接可继续复用。
        """
        if not self.is_connected():
            raise ConnectionError("TS3 连接已断开")
        drain_timeout = min(_remaining(self._deadline), _KEEPALIVE_DRAIN_TIMEOUT)
        connection = self._connection
        connection.send_keepalive()
        try:
            connection._recv(timeout=drain_timeout)
        except Exception:
            # 部分服务端不响应空查询（或回包超时）；没有待处理响应，连接仍可复用。
            pass
        finally:
            connection._num_pending_queries = 0
        self._last_used = time.monotonic()

    def quit(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            connection.quit()
        except Exception:
            pass

    def fetch_server_info(self) -> dict[str, str] | None:
        """serverinfo 是权威数据；失败直接抛异常，由上层判定离线或重连。"""
        resp = self._send("serverinfo")
        if not resp.parsed:
            return None
        info = resp.parsed[0]
        # 响应错位（读到别的命令的数据）时字段对不上，按失败处理并重连。
        if "virtualserver_name" not in info:
            return None
        return info

    def fetch_client_list(self) -> tuple[list[ClientInfo], str | None]:
        """拉取在线名单；失败时返回空名单和归类后的错误描述，不抛异常。

        TS3QueryError（如缺少 clientlist 权限）不影响连接，继续复用；
        超时/网络错误会让协议状态不可信，此时丢弃连接，下一轮重新建立。
        """
        try:
            resp = self._send("clientlist")
        except Exception as exc:
            error = _describe_error(exc)
            if isinstance(exc, TimeoutError) or not self.is_connected():
                self.quit()
            return [], error
        entries = resp.parsed or []
        if entries and not any("clid" in entry for entry in entries):
            # 有数据行却没有一个 clid：读到的是上一条响应，协议已错位。
            self.quit()
            return [], "TS3 查询失败: clientlist 响应错位，已丢弃连接"
        return _parse_client_list(entries), None


class _FetchOutcome(NamedTuple):
    """一次成功拉取的结果：在线快照 + clientlist 的错误描述（None 表示正常）。"""

    snapshot: ServerSnapshot
    roster_error: str | None


# -------- 异步客户端 --------


class LocalStatusClient:
    """异步 TS3 ServerQuery 客户端（每台服务器复用一条持久连接）。

    每个实例持有专属线程池，同步 ts3 调用通过 ``loop.run_in_executor`` 执行；
    单次查询由工作线程内的总 deadline 兜底，外层再用 ``wait_for`` 防止下界：
    即使工作线程卡在不可中断的系统调用，协程也会按时返回。同一 server.key
    用一把 ``asyncio.Lock`` 串行化，避免并发调用同时操作一条 socket。
    """

    def __init__(self, timeout: float = DEFAULT_NETWORK_TIMEOUT) -> None:
        self.timeout = timeout
        self._executor = ThreadPoolExecutor(
            max_workers=_EXECUTOR_MAX_WORKERS,
            thread_name_prefix="ts3-query",
        )
        self._closed = False
        self._sync_clients: dict[str, _SyncTS3Client] = {}
        self._server_locks: dict[str, asyncio.Lock] = {}
        # 每个 server.key 最近一次提交给线程池的查询，用于避免超时后旧线程
        # 仍在收尾时又开新线程操作同一条连接。
        self._inflight: dict[str, asyncio.Future] = {}

    # ---- 线程池工作函数 ----

    def _get_sync_client(self, server: ServerConfig) -> _SyncTS3Client:
        client = self._sync_clients.get(server.key)
        if client is None:
            client = _SyncTS3Client(server, timeout=self.timeout)
            self._sync_clients[server.key] = client
        return client

    def _forget_inflight(
        self, key: str, future: asyncio.Future
    ) -> None:
        if self._inflight.get(key) is future:
            del self._inflight[key]
        if not future.cancelled():
            # 消费异常，避免 asyncio 对"超时后才完成"的 future 打印
            # "Future exception was never retrieved"。
            future.exception()

    def _fetch_status_sync(
        self, server: ServerConfig, client: _SyncTS3Client
    ) -> _FetchOutcome | None:
        """线程池内完成一次拉取：复用连接、失败重连一次并构造快照。"""
        with client.lock:
            if self._closed:
                return None
            if client.configure(server):
                logger.debug(
                    f"[{server.server_name}] 服务器连接参数变化，重建 TS3 查询连接"
                )

            reused = False
            connected = False
            try:
                # 每次尝试都刷新 deadline：复用连接时也要重新计时，否则空闲
                # 超过 timeout 后保活/serverinfo 会直接判超时。
                attempt_deadline = time.monotonic() + self.timeout
                client.begin_attempt(attempt_deadline)
                if client.is_connected():
                    reused = True
                    logger.debug(f"[{server.server_name}] 复用 TS3 查询连接")
                    idle = client.idle_seconds()
                    if idle > IDLE_KEEPALIVE_SECONDS:
                        logger.debug(
                            f"[{server.server_name}] 连接空闲 {idle:.0f}s，"
                            f"发送 keepalive"
                        )
                        client.send_keepalive()
                else:
                    client.connect(attempt_deadline)
                    connected = True
                    logger.debug(f"[{server.server_name}] 建立 TS3 查询连接")
                info = client.fetch_server_info()
                if not info:
                    raise RuntimeError("serverinfo 返回空数据")
            except BaseException as first_exc:
                if self._closed:
                    # close() 正在关闭：放弃重连，尽快让出线程与连接。
                    client.quit()
                    return None
                if not reused and not connected:
                    # 本次尝试连连接都没建起来：直接判离线，不再重试。
                    client.quit()
                    raise
                logger.debug(
                    f"[{server.server_name}] TS3 连接失效，重连一次: "
                    f"{_describe_error(first_exc)}"
                )
                client.quit()
                try:
                    client.connect(time.monotonic() + self.timeout)
                    info = client.fetch_server_info()
                except BaseException:
                    client.quit()
                    raise
                if not info:
                    client.quit()
                    raise RuntimeError("serverinfo 返回空数据")
                logger.debug(f"[{server.server_name}] TS3 连接重连成功")

            clients, roster_error = client.fetch_client_list()

            if roster_error is None:
                clients_online = len(clients)
            else:
                # clientlist 不可用时回退 serverinfo；该计数含 ServerQuery 自身连接。
                clients_online = max(
                    0, _to_int(info.get("virtualserver_clientsonline")) - 1
                )

            snapshot = ServerSnapshot(
                key=server.key,
                name=server.server_name,
                host=server.host,
                query_port=server.query_port,
                voice_port=_to_int(info.get("virtualserver_port")),
                status="online",
                platform=str(info.get("virtualserver_platform") or "Unknown"),
                version=str(info.get("virtualserver_version") or "Unknown"),
                clients_online=clients_online,
                max_clients=_to_int(info.get("virtualserver_maxclients")),
                channels_online=_to_int(info.get("virtualserver_channelsonline")),
                uptime=_to_int(info.get("virtualserver_uptime")),
                clients=clients,
                roster_ok=roster_error is None,
                update_time=_now_text(),
            )
            return _FetchOutcome(snapshot=snapshot, roster_error=roster_error)

    def _close_all_sync(self) -> None:
        """线程池内退出并丢弃所有缓存连接（close() 调用）。

        关闭时不无限等待正在查询的工作线程：最多等 1 秒锁，拿不到就直接
        并发 quit，让阻塞中的 socket 读尽快失败退出。这样插件卸载不会被
        一台挂起服务器拖住几十秒。
        """
        clients = list(self._sync_clients.values())
        self._sync_clients.clear()
        for client in clients:
            acquired = client.lock.acquire(timeout=1.0)
            try:
                client.quit()
            finally:
                if acquired:
                    client.lock.release()

    # ---- 异步接口 ----

    async def fetch_status(
        self, server: ServerConfig, source: str
    ) -> ServerSnapshot | None:
        """拉取一台 TS3 服务器的状态；失败或超时返回 None。

        返回 None 时由上层（main.py）构造 offline 快照继续轮询。
        """
        if self._closed:
            return None

        loop = asyncio.get_running_loop()
        lock = self._server_locks.setdefault(server.key, asyncio.Lock())
        async with lock:
            if self._closed:
                return None

            pending = self._inflight.get(server.key)
            if pending is not None and not pending.done():
                # 上一轮查询已超时但工作线程还没收尾：本轮跳过，避免两个线程
                # 同时操作同一条连接。
                logger.debug(
                    f"[{source}] [{server.server_name}] 上一轮查询仍在收尾，"
                    f"跳过本轮"
                )
                return None

            client = self._get_sync_client(server)
            future = loop.run_in_executor(
                self._executor, self._fetch_status_sync, server, client
            )
            self._inflight[server.key] = future
            future.add_done_callback(
                partial(self._forget_inflight, server.key)
            )
            try:
                # 每次尝试最多 self.timeout；重连一次，故总预算约 2*timeout。
                outcome = await asyncio.wait_for(
                    asyncio.shield(future),
                    timeout=2 * (self.timeout + _WAIT_FOR_GRACE),
                )
            except asyncio.CancelledError:
                if self._closed:
                    return None
                raise
            except (asyncio.TimeoutError, TimeoutError):
                logger.warning(
                    f"[{source}] [{server.server_name}] TS3 查询超时 "
                    f"({self.timeout:.0f}s)，已跳过本轮"
                )
                return None
            except Exception as exc:
                logger.warning(
                    f"[{source}] [{server.server_name}] {_describe_error(exc)}"
                )
                return None

        if self._closed or outcome is None:
            return None

        if outcome.roster_error is not None:
            logger.warning(
                f"[{source}] [{server.server_name}] clientlist 查询失败，"
                f"本轮在线名单不可用，人数回退 serverinfo: "
                f"{outcome.roster_error}"
            )
        return outcome.snapshot

    async def close(self) -> None:
        """关闭客户端：先退出所有缓存连接，再关闭线程池；可重复调用。

        使用独立线程执行 quit（不复用本客户端的查询线程池），避免查询线程
        全部挂起时 close 还要排队；整体耗时上限约为 ``连接数 * 1s``。
        """
        if self._closed:
            return
        self._closed = True
        try:
            await asyncio.to_thread(self._close_all_sync)
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)


def build_status_client() -> LocalStatusClient:
    """工厂函数：构造异步状态查询客户端。"""
    return LocalStatusClient()
