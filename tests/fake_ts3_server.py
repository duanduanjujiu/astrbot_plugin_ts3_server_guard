"""测试用的 asyncio 假 TS3 ServerQuery 服务端。

只实现 clients.py 用到的协议子集：两次 greeting、login/use、serverinfo、
clientlist、version（保活）、quit。记录原始命令行、连接数与命令次数供断言。

可配置行为：
- ``silent_greeting``：接受 TCP 但不发 greeting（验证连接超时）；
- ``flood``：serverinfo 返回 error id=524；
- ``serverinfo_denied``：serverinfo 返回 error id=2568；
- ``misaligned_serverinfo``：serverinfo 返回 clientlist 数据（缺关键字段，
  验证协议错位不会生成假在线快照）；
- ``serverinfo_split``：serverinfo 拆成多行返回（验证多行合并）；
- ``notify_noise``：serverinfo 响应中夹一条 notify 行（验证被忽略）；
- ``malformed_error_id`` / ``malformed_error_no_id``：error 行本身损坏；
- ``empty_clientlist``：clientlist 只回 error id=0、无数据行（协议损坏）；
- ``mixed_clientlist``：clientlist 混入缺少 clid 的 serverinfo 记录；
- ``split_clientlist``：clientlist 记录拆成多行返回；
- ``clientlist_denied``：clientlist 返回 error id=2568；
- ``clientlist_flood``：clientlist 返回 error id=524；
- ``version_denied``：保活 version 返回 error id=524；
- ``auth_error``：login 返回 error id=520；
- ``invalid_sid``：use 返回 error id=768；
- ``close_after_commands=N``：回完第 N 条命令后主动关闭连接（验证重连）；
- ``serverinfo_delay``：延迟 serverinfo 响应（验证 deadline）；
- ``chunk_size``：按小块写响应（验证半包/分片读取）；
- ``clientlist_fail_count=N``：前 N 次 clientlist 返回 2568，之后正常。
"""

from __future__ import annotations

import asyncio

OK_LINE = b"error id=0 msg=ok\n\r"
ERROR_520 = b"error id=520 msg=invalid\\sloginname\\sor\\spassword\n\r"
ERROR_524 = b"error id=524 msg=server\\sis\\sflooding\n\r"
ERROR_768 = b"error id=768 msg=invalid\\sserverID\n\r"
ERROR_2568 = (
    b"error id=2568 msg=insufficient\\spermissions\\sto\\sperform\\s"
    b"clientlist\n\r"
)
MALFORMED_ERROR_ID = b"error id=abc msg=boom\n\r"
MALFORMED_ERROR_NO_ID = b"error msg=ok\n\r"
VERSION_LINE = b"version=3.13.7\n\r"
SERVERINFO_LINE = (
    b"virtualserver_name=Test\\sServer "
    b"virtualserver_platform=Linux "
    b"virtualserver_version=3.13.7 "
    b"virtualserver_clientsonline=3 "
    b"virtualserver_maxclients=32 "
    b"virtualserver_channelsonline=8 "
    b"virtualserver_port=9987 "
    b"virtualserver_uptime=3661\n\r"
)
# serverinfo 拆成两行：后面的记录覆盖/补充前面的字段。
SPLIT_SERVERINFO_LINES = (
    b"virtualserver_name=Test\\sServer virtualserver_port=9987 "
    b"virtualserver_clientsonline=3\n\r",
    b"virtualserver_platform=Linux virtualserver_version=3.13.7 "
    b"virtualserver_maxclients=32 virtualserver_channelsonline=8 "
    b"virtualserver_uptime=3661\n\r",
)
NOTIFY_LINE = b"notifycliententerview clid=9 client_nickname=Ghost\n\r"
# 昵称里带转义：\s 表示空格，\p 表示 |；client_type=1 的是 ServerQuery 自身。
CLIENTLIST_LINE = (
    b"clid=2 cid=1 client_database_id=10 client_nickname=Alice\\sSmith "
    b"client_type=0|"
    b"clid=3 cid=1 client_database_id=11 client_nickname=Bob\\pDev "
    b"client_type=0|"
    b"clid=4 cid=1 client_database_id=12 client_nickname=Query "
    b"client_type=1\n\r"
)
# 拆成多行返回：每行是一条（或几条）记录。
SPLIT_CLIENTLIST_LINES = (
    b"clid=2 cid=1 client_database_id=10 client_nickname=Alice\\sSmith "
    b"client_type=0\n\r",
    b"clid=3 cid=1 client_database_id=11 client_nickname=Bob\\pDev "
    b"client_type=0|"
    b"clid=4 cid=1 client_database_id=12 client_nickname=Query "
    b"client_type=1\n\r",
)
# 混入一条缺少 clid 的记录：真实 TS3 不可能出现，应被判定为协议错位。
MIXED_CLIENTLIST_LINE = (
    b"clid=2 cid=1 client_database_id=10 client_nickname=Alice\\sSmith "
    b"client_type=0|virtualserver_name=Test\\sServer\n\r"
)


class FakeTS3Server:
    """asyncio 行协议假服务端；用 ``await server.start()`` 启动。"""

    def __init__(
        self,
        mode: str = "normal",
        *,
        serverinfo_delay: float = 0.0,
        close_after_commands: int | None = None,
        chunk_size: int | None = None,
        chunk_delay: float = 0.01,
        clientlist_fail_count: int = 0,
    ) -> None:
        self.mode = mode
        self.serverinfo_delay = serverinfo_delay
        self.close_after_commands = close_after_commands
        self.chunk_size = chunk_size
        self.chunk_delay = chunk_delay
        self.clientlist_fail_count = clientlist_fail_count
        self.port = 0
        self.connection_count = 0
        self.closed_connection_count = 0
        self.raw_commands: list[str] = []
        self.commands: list[str] = []
        self.command_counts: dict[str, int] = {}
        self._clientlist_requests = 0
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self) -> "FakeTS3Server":
        self._server = await asyncio.start_server(
            self._accept, "127.0.0.1", 0
        )
        sockets = self._server.sockets or []
        self.port = sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        # 先关闭连接并取消处理任务，再等 listen socket 收尾：Python 3.12 的
        # Server.wait_closed() 会等所有连接结束，顺序反了会被挂起的 handler 卡住。
        for writer in list(self._writers):
            writer.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=1.0)
            except (asyncio.TimeoutError, TimeoutError):
                pass

    async def _send(
        self, writer: asyncio.StreamWriter, data: bytes
    ) -> None:
        """按 ``chunk_size`` 分片写出，模拟半包/分片到达。"""
        if not self.chunk_size or self.chunk_size <= 0:
            writer.write(data)
            await writer.drain()
            return
        for offset in range(0, len(data), self.chunk_size):
            writer.write(data[offset : offset + self.chunk_size])
            await writer.drain()
            await asyncio.sleep(self.chunk_delay)

    def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.create_task(self._handle(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self.connection_count += 1
        self._writers.add(writer)
        try:
            if self.mode == "silent_greeting":
                await asyncio.sleep(3600)
                return
            await self._send(writer, b"TS3\n\r")
            await self._send(
                writer,
                b"Welcome to the TeamSpeak 3 ServerQuery interface.\n\r",
            )
            buffer = b""
            command_index = 0
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                buffer += chunk
                while b"\n\r" in buffer:
                    raw, buffer = buffer.split(b"\n\r", 1)
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    command_index += 1
                    if not await self._handle_line(line, writer, command_index):
                        return
        except (ConnectionError, asyncio.CancelledError):
            return
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001 - 测试清理，忽略关闭异常
                pass
            self._writers.discard(writer)
            self.closed_connection_count += 1

    async def _handle_line(
        self, line: str, writer: asyncio.StreamWriter, command_index: int
    ) -> bool:
        self.raw_commands.append(line)
        command = line.split()[0]
        self.commands.append(command)
        self.command_counts[command] = self.command_counts.get(command, 0) + 1
        if command == "quit":
            return False
        if command == "login":
            if self.mode == "auth_error":
                await self._send(writer, ERROR_520)
            else:
                await self._send(writer, OK_LINE)
        elif command == "use":
            if self.mode == "invalid_sid":
                await self._send(writer, ERROR_768)
            else:
                await self._send(writer, OK_LINE)
        elif command == "version":
            if self.mode == "version_denied":
                await self._send(writer, ERROR_524)
            else:
                await self._send(writer, VERSION_LINE + OK_LINE)
        elif command == "serverinfo":
            if self.serverinfo_delay:
                await asyncio.sleep(self.serverinfo_delay)
            await self._send_serverinfo(writer)
        elif command == "clientlist":
            await self._send_clientlist(writer)
        else:
            await self._send(
                writer, b"error id=256 msg=unknown\\scommand\n\r"
            )
        if (
            self.close_after_commands is not None
            and command_index >= self.close_after_commands
        ):
            return False
        return True

    async def _send_serverinfo(self, writer: asyncio.StreamWriter) -> None:
        mode = self.mode
        if mode == "flood":
            await self._send(writer, ERROR_524)
        elif mode == "serverinfo_denied":
            await self._send(writer, ERROR_2568)
        elif mode == "misaligned_serverinfo":
            await self._send(writer, CLIENTLIST_LINE + OK_LINE)
        elif mode == "serverinfo_split":
            for line in SPLIT_SERVERINFO_LINES:
                await self._send(writer, line)
            await self._send(writer, OK_LINE)
        elif mode == "notify_noise":
            await self._send(writer, SERVERINFO_LINE)
            await self._send(writer, NOTIFY_LINE)
            await self._send(writer, OK_LINE)
        elif mode == "malformed_error_id":
            await self._send(writer, MALFORMED_ERROR_ID)
        elif mode == "malformed_error_no_id":
            await self._send(writer, MALFORMED_ERROR_NO_ID)
        else:
            await self._send(writer, SERVERINFO_LINE + OK_LINE)

    async def _send_clientlist(self, writer: asyncio.StreamWriter) -> None:
        self._clientlist_requests += 1
        mode = self.mode
        if mode == "clientlist_denied":
            await self._send(writer, ERROR_2568)
        elif mode == "clientlist_flood":
            await self._send(writer, ERROR_524)
        elif mode == "empty_clientlist":
            await self._send(writer, OK_LINE)
        elif mode == "mixed_clientlist":
            await self._send(writer, MIXED_CLIENTLIST_LINE + OK_LINE)
        elif mode == "split_clientlist":
            for line in SPLIT_CLIENTLIST_LINES:
                await self._send(writer, line)
            await self._send(writer, OK_LINE)
        elif (
            self.clientlist_fail_count > 0
            and self._clientlist_requests <= self.clientlist_fail_count
        ):
            await self._send(writer, ERROR_2568)
        else:
            await self._send(writer, CLIENTLIST_LINE + OK_LINE)
