"""TS3 ServerQuery 纯 asyncio 客户端。

v3.5.0 起不再依赖 py-ts3：协议读写直接用 ``asyncio.open_connection`` 实现，
因此没有线程池、没有阻塞式 socket 读取，也不再调整第三方库的日志级别
（插件市场审查项），并可运行在 Python 3.13+（旧依赖所用的 telnet 标准库
模块已被移除）。

通信模型：每台服务器缓存一条持久 ServerQuery 连接（TCP + 两次 greeting +
login + use），轮询时复用，避免每轮新建连接触发 TS3 防洪；只有连接失效、
协议错位或空闲过久保活失败时，才丢弃旧连接并重连一次。单次尝试共享一个
总 deadline（默认 ``timeout`` 秒）：连接、greeting、login、use、保活、
serverinfo、clientlist 的每个读写步骤都只拿到剩余时间，超时即失败返回。

只请求 ``serverinfo``（权威数据）与 ``clientlist``（在线名单，可选）：
频道数取自 serverinfo 的 ``virtualserver_channelsonline``，不再请求 channellist。
``clientlist`` 失败不影响在线判定，仅在快照上标记 ``roster_ok=False``。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, NamedTuple

from astrbot.api import logger

from .constants import CLIENT_TYPE_REGULAR, DEFAULT_NETWORK_TIMEOUT
from .models import ClientInfo, ServerConfig, ServerSnapshot

# 一次完整拉取（含一次重连）在 ``2 * timeout`` 之外的兜底余量。
_WAIT_FOR_GRACE = 2.0

# 连接空闲超过该秒数时发送轻量 version 命令保活，避免被服务端按空闲断开。
IDLE_KEEPALIVE_SECONDS = 240.0

# 保活命令的独立超时：即使服务端失联也不占满整次尝试的 deadline。
_KEEPALIVE_TIMEOUT = 5.0

# 单条响应行 / 单次响应数据的上限，防止异常服务端无限输出拖垮内存。
_STREAM_LIMIT = 4 * 1024 * 1024
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# 关闭连接时等待写缓冲收尾的上限。
_CLOSE_TIMEOUT = 1.0
_CLOSE_ALL_TIMEOUT = 2.0

_LINE_ENDING = b"\n\r"
_ERROR_PREFIX = "error "

# 常见 TS3 错误码 -> 可操作的排查提示。
_ERROR_ID_HINTS = {
    520: "认证失败，请检查 query_user / query_password",
    524: "触发 TS3 防洪，请把本机 IP 加入 query_ip_allowlist 或加大轮询间隔",
    768: "virtual_server_id 无效",
    2568: "查询账号缺少所需权限（clientlist 需要 b_virtualserver_client_list）",
}

# TS3 ServerQuery 转义表（服务端只接受这些转义形式）。
_ESCAPE_MAP = {
    "\\": "\\\\",
    "/": "\\/",
    " ": "\\s",
    "|": "\\p",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\v": "\\v",
    "\f": "\\f",
    "\a": "\\a",
    "\b": "\\b",
}
_UNESCAPE_MAP = {value[1:]: key for key, value in _ESCAPE_MAP.items()}


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


def _escape(value: Any) -> str:
    """按 TS3 ServerQuery 规则转义参数值。"""
    return "".join(_ESCAPE_MAP.get(char, char) for char in str(value))


def _unescape(value: str) -> str:
    """反转义 TS3 ServerQuery 字段值；未知转义按字符本身处理。"""
    result: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            escaped = value[index + 1]
            result.append(_UNESCAPE_MAP.get(escaped, escaped))
            index += 2
        else:
            result.append(char)
            index += 1
    return "".join(result)


def _build_command(
    command: str,
    common_parameters: dict[str, Any] | None = None,
) -> bytes:
    """拼装一条 ServerQuery 命令（含转义参数与行结束符）。"""
    parts = [command]
    for key, value in (common_parameters or {}).items():
        parts.append(f"{key}={_escape(value)}")
    return " ".join(parts).encode("utf-8") + _LINE_ENDING


def _parse_property(token: str) -> tuple[str, str]:
    """解析 ``key=value``；没有 ``=`` 时值为空，多个 ``=`` 归入值。"""
    key, separator, value = token.partition("=")
    if not separator:
        value = ""
    return _unescape(key), _unescape(value)


def _parse_item(item: str) -> dict[str, str]:
    """解析一条数据记录（空格分隔的 key=value 列表）。"""
    return dict(_parse_property(token) for token in item.split())


def _parse_records(data_lines: list[str]) -> list[dict[str, str]]:
    """解析响应数据行：记录以 ``|`` 分隔，多行结果依次拼接。"""
    records: list[dict[str, str]] = []
    for line in data_lines:
        for item in line.split("|"):
            if item.strip():
                records.append(_parse_item(item))
    return records


class _ProtocolError(Exception):
    """TS3 响应不符合协议（或响应与请求错位）。

    ``retryable`` 为 False 表示重新建立连接也无济于事（例如 error 行本身损坏），
    上层只丢弃连接、不再重连一次。
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class _QueryError(Exception):
    """TS3 服务端返回了非 0 error id。"""

    def __init__(self, error_id: int, message: str) -> None:
        self.error_id = error_id
        self.message = message
        super().__init__(f"error id {error_id}: {message}")


def _describe_error(exc: BaseException) -> str:
    """把协议 / 网络异常归类成可日志输出的一句话。"""
    if isinstance(exc, _QueryError):
        hint = _ERROR_ID_HINTS.get(exc.error_id)
        text = f"TS3 查询失败: error id {exc.error_id}: {exc.message}"
        return f"{text}（{hint}）" if hint else text
    if isinstance(exc, _ProtocolError):
        return f"TS3 查询失败: 协议异常 {exc}"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "TS3 查询失败: 超时（总时限内未完成连接或响应）"
    if isinstance(exc, (OSError, EOFError)):
        return f"TS3 查询失败: 网络错误 {type(exc).__name__}: {exc}"
    return f"TS3 查询失败: {type(exc).__name__}: {exc}"


def _parse_client_list(records: list[dict[str, str]]) -> list[ClientInfo]:
    """把 clientlist 记录解析成 ClientInfo 列表（只保留真实客户端）。

    真实 TS3 的 clientlist 一定包含本查询连接（client_type=1），所以空响应或
    任意一条记录缺少 clid 都说明响应损坏/错位，必须报协议错误：否则会把
    空名单当成合法 roster，让上层误判为“全员离开”。
    """
    if not records:
        raise _ProtocolError("clientlist 响应为空，连接可能已错位")
    if not all("clid" in record for record in records):
        raise _ProtocolError("clientlist 响应含缺少 clid 的记录，连接可能已错位")
    clients: list[ClientInfo] = []
    for entry in records:
        client_type = _to_int(entry.get("client_type"), CLIENT_TYPE_REGULAR)
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


def _connection_target(server: ServerConfig) -> tuple[Any, ...]:
    return (
        server.host,
        server.query_port,
        server.query_user,
        server.query_password,
        server.virtual_server_id,
    )


# -------- 底层持久连接 --------


class _TS3Connection:
    """一条持久 TS3 ServerQuery 连接（纯 asyncio，仅事件循环内使用）。"""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        target: tuple[Any, ...],
    ) -> None:
        self._reader = reader
        self._writer: asyncio.StreamWriter | None = writer
        self.target = target
        self.closed = False
        self._last_used = time.monotonic()

    @classmethod
    async def open(
        cls, server: ServerConfig, deadline: float
    ) -> "_TS3Connection":
        """在 deadline 内建立连接并完成 greeting / login / use。"""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    server.host,
                    server.query_port,
                    limit=_STREAM_LIMIT,
                ),
                timeout=_remaining(deadline),
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise TimeoutError("连接 TS3 ServerQuery 超时") from exc

        connection = cls(reader, writer, _connection_target(server))
        try:
            await connection._read_greeting(deadline)
            await connection.query(
                "login",
                common_parameters={
                    "client_login_name": server.query_user,
                    "client_login_password": server.query_password,
                },
                deadline=deadline,
            )
            await connection.query(
                "use",
                common_parameters={"sid": server.virtual_server_id},
                deadline=deadline,
            )
        except BaseException:
            await connection.close()
            raise
        connection._last_used = time.monotonic()
        return connection

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_used

    async def _read_line(self, deadline: float) -> bytes:
        """读取一行（不含 ``\\n\\r``），超时/断连会标记连接失效。"""
        try:
            timeout = _remaining(deadline)
        except TimeoutError:
            self.closed = True
            raise
        try:
            raw = await asyncio.wait_for(
                self._reader.readuntil(_LINE_ENDING),
                timeout=timeout,
            )
        except asyncio.IncompleteReadError as exc:
            self.closed = True
            raise ConnectionError("TS3 服务端关闭了连接") from exc
        except asyncio.LimitOverrunError as exc:
            self.closed = True
            raise _ProtocolError("TS3 响应行过长") from exc
        except (asyncio.TimeoutError, TimeoutError) as exc:
            self.closed = True
            raise TimeoutError("等待 TS3 响应超时") from exc
        except OSError:
            self.closed = True
            raise
        return raw[: -len(_LINE_ENDING)]

    async def _write(self, data: bytes, deadline: float) -> None:
        writer = self._writer
        if self.closed or writer is None:
            raise ConnectionError("TS3 连接已关闭")
        try:
            timeout = _remaining(deadline)
        except TimeoutError:
            self.closed = True
            raise
        try:
            writer.write(data)
            await asyncio.wait_for(writer.drain(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            self.closed = True
            raise TimeoutError("发送 TS3 查询超时") from exc
        except OSError:
            self.closed = True
            raise

    async def _read_greeting(self, deadline: float) -> None:
        first = await self._read_line(deadline)
        if first.strip() != b"TS3":
            raise _ProtocolError("TS3 欢迎信息不正确")
        second = await self._read_line(deadline)
        if b"Welcome" not in second:
            raise _ProtocolError("TS3 欢迎信息缺少 Welcome")

    async def _read_response(self, deadline: float) -> list[dict[str, str]]:
        """读到 ``error `` 行为止，返回其前面的数据记录。"""
        data_lines: list[str] = []
        buffered = 0
        while True:
            line = await self._read_line(deadline)
            text = line.decode("utf-8", "replace").strip()
            if not text:
                continue
            if text.startswith("notify"):
                # 未订阅事件时不应出现；出现则防御性忽略。
                continue
            if text.startswith(_ERROR_PREFIX):
                fields = _parse_item(text)
                raw_id = fields.get("id")
                try:
                    error_id = int(raw_id)  # type: ignore[arg-type]
                except (TypeError, ValueError) as exc:
                    # error 行本身损坏：重连也无济于事，标记为不可重试。
                    raise _ProtocolError(
                        f"TS3 error 行缺少合法 id: {text}",
                        retryable=False,
                    ) from exc
                if error_id != 0:
                    raise _QueryError(error_id, fields.get("msg", "unknown"))
                return _parse_records(data_lines)
            data_lines.append(text)
            buffered += len(text)
            if buffered > _MAX_RESPONSE_BYTES:
                self.closed = True
                raise _ProtocolError("TS3 响应数据过大")

    async def query(
        self,
        command: str,
        common_parameters: dict[str, Any] | None = None,
        *,
        deadline: float,
    ) -> list[dict[str, str]]:
        """执行一条命令并返回数据记录；非 0 error id 抛 ``_QueryError``。"""
        await self._write(_build_command(command, common_parameters), deadline)
        records = await self._read_response(deadline)
        self._last_used = time.monotonic()
        return records

    async def fetch_server_info(self, deadline: float) -> dict[str, str]:
        """serverinfo 是权威数据；支持多行响应，缺关键字段按协议错位处理。"""
        records = await self.query("serverinfo", deadline=deadline)
        if not records:
            raise _ProtocolError("serverinfo 响应为空")
        # 某些服务端/代理会把一条 serverinfo 拆成多行返回，逐条合并后再校验。
        info: dict[str, str] = {}
        for record in records:
            info.update(record)
        if "virtualserver_name" not in info:
            raise _ProtocolError("serverinfo 响应缺少 virtualserver_name")
        return info

    async def fetch_client_list(self, deadline: float) -> list[ClientInfo]:
        records = await self.query("clientlist", deadline=deadline)
        return _parse_client_list(records)

    async def close(self) -> None:
        """尽力关闭连接，最多等待 ``_CLOSE_TIMEOUT`` 秒；可重复调用。"""
        self.closed = True
        writer = self._writer
        self._writer = None
        if writer is None:
            return
        try:
            writer.close()
        except Exception:  # noqa: BLE001 - 关闭失败无需上抛
            pass
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=_CLOSE_TIMEOUT)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        except Exception:  # noqa: BLE001 - 连接可能已被对端重置
            pass


class _FetchOutcome(NamedTuple):
    """一次成功拉取的结果：在线快照 + clientlist 的错误描述（None 表示正常）。"""

    snapshot: ServerSnapshot
    roster_error: str | None


# -------- 异步客户端 --------


class LocalStatusClient:
    """异步 TS3 ServerQuery 客户端（每台服务器复用一条持久连接）。

    连接按 ``server.key`` 缓存，同一 key 用一把 ``asyncio.Lock`` 串行化，
    不同服务器可并发。每次尝试（含重连后的第二次尝试）各自使用新的总
    deadline，因此最坏耗时约为 ``2 * timeout``。
    """

    def __init__(self, timeout: float = DEFAULT_NETWORK_TIMEOUT) -> None:
        self.timeout = timeout
        self._closed = False
        self._connections: dict[str, _TS3Connection] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # 已提示过 clientlist 不可用的 server.key：每个失败周期只 warning 一次，
        # 名单恢复后移除，避免每轮轮询重复刷日志（main.py 另有整段状态提示）。
        self._roster_warned: set[str] = set()

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def _drop_connection(self, key: str) -> None:
        connection = self._connections.pop(key, None)
        if connection is not None:
            await connection.close()

    async def _open_and_cache(
        self, server: ServerConfig, deadline: float
    ) -> _TS3Connection:
        connection = await _TS3Connection.open(server, deadline)
        if self._closed:
            # close() 与建连并发：立即关掉，避免留下无人管理的 socket。
            await connection.close()
            raise ConnectionError("TS3 客户端已关闭")
        self._connections[server.key] = connection
        return connection

    async def _fetch_once(
        self, server: ServerConfig
    ) -> _FetchOutcome | None:
        """一次拉取：复用连接、失败重连一次、clientlist 降级。"""
        key = server.key
        connection = self._connections.get(key)
        if connection is not None and (
            connection.target != _connection_target(server) or connection.closed
        ):
            # 配置变化或连接已断开：丢弃旧连接，本轮按新建连接处理。
            await self._drop_connection(key)
            connection = None

        reused = connection is not None
        connected = False
        deadline = time.monotonic() + self.timeout
        active_deadline = deadline
        try:
            if connection is None:
                connection = await self._open_and_cache(server, deadline)
                connected = True
                logger.debug(f"[{server.server_name}] 建立 TS3 查询连接")
            else:
                logger.debug(f"[{server.server_name}] 复用 TS3 查询连接")
                if connection.idle_seconds() > IDLE_KEEPALIVE_SECONDS:
                    logger.debug(
                        f"[{server.server_name}] 连接空闲 "
                        f"{connection.idle_seconds():.0f}s，发送 keepalive"
                    )
                    await connection.query(
                        "version",
                        deadline=min(
                            deadline,
                            time.monotonic() + _KEEPALIVE_TIMEOUT,
                        ),
                    )
            info = await connection.fetch_server_info(active_deadline)
        except asyncio.CancelledError:
            await self._drop_connection(key)
            raise
        except Exception as first_exc:
            if self._closed:
                await self._drop_connection(key)
                return None
            if not reused and not connected:
                # 纯连接/认证失败：重试只会再失败一次，直接判离线。
                await self._drop_connection(key)
                raise
            if isinstance(first_exc, _QueryError):
                # 服务端拒绝查询（如防洪 524）：连接仍是对齐的，重连只会再次
                # 登录、加剧防洪，故保留连接，由 fetch_status 记录并判离线。
                logger.debug(
                    f"[{server.server_name}] 服务端拒绝查询，保留连接: "
                    f"{_describe_error(first_exc)}"
                )
                raise
            if isinstance(first_exc, _ProtocolError) and not first_exc.retryable:
                # error 行本身损坏：连接不可信，丢弃但不在本轮重连。
                await self._drop_connection(key)
                raise
            logger.debug(
                f"[{server.server_name}] TS3 连接失效，重连一次: "
                f"{_describe_error(first_exc)}"
            )
            await self._drop_connection(key)
            active_deadline = time.monotonic() + self.timeout
            try:
                connection = await self._open_and_cache(
                    server, active_deadline
                )
                info = await connection.fetch_server_info(active_deadline)
            except asyncio.CancelledError:
                await self._drop_connection(key)
                raise
            except Exception:
                await self._drop_connection(key)
                raise
            logger.debug(f"[{server.server_name}] TS3 连接重连成功")

        roster_error: str | None = None
        clients: list[ClientInfo] = []
        try:
            clients = await connection.fetch_client_list(active_deadline)
        except asyncio.CancelledError:
            await self._drop_connection(key)
            raise
        except _QueryError as exc:
            # 查询级错误（如 2568 权限不足）不影响连接，继续复用。
            roster_error = _describe_error(exc)
        except Exception as exc:
            # 网络/超时/协议错误：连接状态不可信，本轮降级并丢弃连接。
            roster_error = _describe_error(exc)
            await self._drop_connection(key)

        if self._closed:
            await self._drop_connection(key)
            return None

        if roster_error is None:
            clients_online = len(clients)
        else:
            # clientlist 不可用时回退 serverinfo。注意该计数含所有 ServerQuery
            # 连接（不只是一个），有多个查询客户端时会高估在线人数；没有名单
            # 就无法区分，这里只保证是保守的上界估算（减 1 仅覆盖本连接）。
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

    # ---- 公开异步接口 ----

    async def fetch_status(
        self, server: ServerConfig, source: str
    ) -> ServerSnapshot | None:
        """拉取一台 TS3 服务器的状态；失败或超时返回 None，从不抛异常。

        返回 None 时由上层（main.py）构造 offline 快照继续轮询。
        """
        if self._closed:
            return None

        lock = self._lock_for(server.key)
        async with lock:
            if self._closed:
                return None
            try:
                outcome = await asyncio.wait_for(
                    self._fetch_once(server),
                    timeout=2 * self.timeout + _WAIT_FOR_GRACE,
                )
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError):
                if self._closed:
                    return None
                logger.warning(
                    f"[{source}] [{server.server_name}] "
                    f"TS3 查询超时（已按总时限放弃），已跳过本轮"
                )
                await self._drop_connection(server.key)
                return None
            except Exception as exc:
                if self._closed:
                    return None
                logger.warning(
                    f"[{source}] [{server.server_name}] {_describe_error(exc)}"
                )
                return None

        if self._closed or outcome is None:
            return None

        if outcome.roster_error is not None:
            if server.key in self._roster_warned:
                logger.debug(
                    f"[{source}] [{server.server_name}] clientlist 仍不可用: "
                    f"{outcome.roster_error}"
                )
            else:
                self._roster_warned.add(server.key)
                logger.warning(
                    f"[{source}] [{server.server_name}] clientlist 查询失败，"
                    f"本轮在线名单不可用，人数回退 serverinfo: "
                    f"{outcome.roster_error}"
                )
        else:
            self._roster_warned.discard(server.key)
        return outcome.snapshot

    async def close(self) -> None:
        """关闭客户端：尽力关闭所有缓存连接，最多阻塞约 2 秒；可重复调用。"""
        if self._closed:
            return
        self._closed = True
        connections = list(self._connections.values())
        self._connections.clear()
        if not connections:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *(connection.close() for connection in connections),
                    return_exceptions=True,
                ),
                timeout=_CLOSE_ALL_TIMEOUT,
            )
        except (asyncio.TimeoutError, TimeoutError):
            pass


def build_status_client() -> LocalStatusClient:
    """工厂函数：构造异步状态查询客户端。"""
    return LocalStatusClient()
