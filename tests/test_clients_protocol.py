"""clients.py 协议层集成测试：asyncio 假 TS3 服务端，不依赖第三方包。"""

from __future__ import annotations

import asyncio
import time
import unittest

import _support  # noqa: F401
from astrbot_plugin_ts3_server_guard.ts_server_monitor import (  # noqa: E402
    clients as clients_module,
)
from astrbot_plugin_ts3_server_guard.ts_server_monitor.clients import (  # noqa: E402
    LocalStatusClient,
    _escape,
)
from astrbot_plugin_ts3_server_guard.ts_server_monitor.models import (  # noqa: E402
    ServerConfig,
)
from fake_ts3_server import FakeTS3Server  # noqa: E402


def make_server(
    port: int,
    key: str = "srv",
    *,
    username: str = "serveradmin",
    password: str = "p@ss word|1",
) -> ServerConfig:
    return ServerConfig(
        key=key,
        enabled=True,
        server_name="测试服",
        host="127.0.0.1",
        query_port=port,
        query_user=username,
        query_password=password,
        virtual_server_id=1,
        join_leave_interval=15,
    )


class _RecordingLogger:
    """替换 clients 模块的 logger，避免测试依赖具体 AstrBot 日志实现。"""

    def __init__(self) -> None:
        self.debugs: list[str] = []
        self.infos: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def debug(self, message: object, *args, **kwargs) -> None:
        self.debugs.append(str(message))

    def info(self, message: object, *args, **kwargs) -> None:
        self.infos.append(str(message))

    def warning(self, message: object, *args, **kwargs) -> None:
        self.warnings.append(str(message))

    def error(self, message: object, *args, **kwargs) -> None:
        self.errors.append(str(message))


class ServerTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # 替换 clients 模块的 logger，stub 环境与真实 AstrBot 环境行为一致。
        self._original_logger = clients_module.logger
        self.recorder = _RecordingLogger()
        clients_module.logger = self.recorder
        self.client: LocalStatusClient | None = None
        self.servers: list[FakeTS3Server] = []

    async def asyncTearDown(self) -> None:
        if self.client is not None:
            await self.client.close()
        for server in self.servers:
            await server.stop()
        clients_module.logger = self._original_logger

    async def start_server(self, **kwargs) -> FakeTS3Server:
        server = FakeTS3Server(**kwargs)
        await server.start()
        self.servers.append(server)
        return server

    def make_client(self, timeout: float = 1.0) -> LocalStatusClient:
        self.client = LocalStatusClient(timeout=timeout)
        return self.client

    @property
    def logs(self) -> str:
        return "\n".join(self.recorder.warnings)

    @property
    def debug_logs(self) -> str:
        return "\n".join(self.recorder.debugs)

    async def wait_for_closed_connections(
        self, server: FakeTS3Server, count: int = 1
    ) -> None:
        for _ in range(50):
            if server.closed_connection_count >= count:
                return
            await asyncio.sleep(0.02)
        self.fail(f"期望至少 {count} 条连接被关闭")


class FetchIntegrationTest(ServerTestBase):
    async def test_normal_fetch_fields(self) -> None:
        server = await self.start_server()
        client = self.make_client()
        snapshot = await client.fetch_status(make_server(server.port), "test")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.status, "online")
        self.assertTrue(snapshot.roster_ok)
        self.assertEqual(snapshot.platform, "Linux")
        self.assertEqual(snapshot.version, "3.13.7")
        self.assertEqual(snapshot.voice_port, 9987)
        self.assertEqual(snapshot.max_clients, 32)
        self.assertEqual(snapshot.channels_online, 8)
        self.assertEqual(snapshot.uptime, 3661)
        self.assertEqual(snapshot.clients_online, 2)
        self.assertEqual([c.clid for c in snapshot.clients], [2, 3])
        self.assertEqual(
            [c.nickname for c in snapshot.clients], ["Alice Smith", "Bob|Dev"]
        )
        self.assertEqual([c.database_id for c in snapshot.clients], [10, 11])
        self.assertEqual(snapshot.error, None)
        self.assertNotIn("channellist", server.commands)
        self.assertEqual(server.connection_count, 1)
        self.assertEqual(self.logs, "")

    async def test_two_fetches_reuse_one_connection(self) -> None:
        server = await self.start_server()
        client = self.make_client()
        first = await client.fetch_status(make_server(server.port), "test")
        second = await client.fetch_status(make_server(server.port), "test")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(second.clients_online, 2)
        self.assertEqual(server.connection_count, 1, "应复用连接")
        self.assertEqual(server.command_counts.get("login"), 1)
        self.assertEqual(server.command_counts.get("use"), 1)
        self.assertEqual(server.command_counts.get("serverinfo"), 2)
        self.assertEqual(server.command_counts.get("clientlist"), 2)

    async def test_reconnect_once_when_server_closes_connection(self) -> None:
        # 每条连接的命令序列：login, use, serverinfo, clientlist。
        server = await self.start_server(close_after_commands=4)
        client = self.make_client()
        first = await client.fetch_status(make_server(server.port), "test")
        second = await client.fetch_status(make_server(server.port), "test")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(server.connection_count, 2, "第二次应重连一次")
        self.assertEqual(server.command_counts.get("login"), 2)

    async def test_silent_greeting_returns_none_within_timeout(self) -> None:
        server = await self.start_server(mode="silent_greeting")
        client = self.make_client(timeout=1.0)
        started = time.monotonic()
        snapshot = await client.fetch_status(make_server(server.port), "test")
        elapsed = time.monotonic() - started
        self.assertIsNone(snapshot)
        self.assertLess(elapsed, 3.0, f"耗时 {elapsed:.2f}s 超出预期")
        self.assertIn("查询超时", self.logs)
        self.assertEqual(server.connection_count, 1)

    async def test_clientlist_2568_degrades_and_keeps_connection(self) -> None:
        server = await self.start_server(mode="clientlist_denied")
        client = self.make_client()
        first = await client.fetch_status(make_server(server.port), "test")
        second = await client.fetch_status(make_server(server.port), "test")
        for snapshot in (first, second):
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot.status, "online")
            self.assertFalse(snapshot.roster_ok)
            self.assertEqual(snapshot.clients, [])
            # serverinfo 的 3 个在线连接含 ServerQuery 自身 -> 回退为 2
            self.assertEqual(snapshot.clients_online, 2)
        self.assertEqual(server.connection_count, 1, "查询级错误不应重连")
        self.assertIn("error id 2568", self.logs)
        self.assertIn("所需权限", self.logs)
        self.assertIn("b_virtualserver_client_list", self.logs)

    async def test_clientlist_flood_keeps_connection(self) -> None:
        server = await self.start_server(mode="clientlist_flood")
        client = self.make_client()
        first = await client.fetch_status(make_server(server.port), "test")
        second = await client.fetch_status(make_server(server.port), "test")
        for snapshot in (first, second):
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertFalse(snapshot.roster_ok)
            self.assertEqual(snapshot.clients_online, 2)
        self.assertEqual(server.connection_count, 1)
        self.assertIn("error id 524", self.logs)
        self.assertIn("防洪", self.logs)

    async def test_clientlist_warning_once_per_failing_streak(self) -> None:
        server = await self.start_server(mode="clientlist_denied")
        client = self.make_client()
        for _ in range(3):
            snapshot = await client.fetch_status(
                make_server(server.port), "test"
            )
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertFalse(snapshot.roster_ok)
        self.assertEqual(len(self.recorder.warnings), 1, "同一失败周期只 warning 一次")
        self.assertIn("仍不可用", self.debug_logs)

    async def test_clientlist_warning_resets_after_recovery(self) -> None:
        server = await self.start_server(clientlist_fail_count=1)
        client = self.make_client()
        first = await client.fetch_status(make_server(server.port), "test")
        assert first is not None
        self.assertFalse(first.roster_ok)
        self.assertEqual(len(self.recorder.warnings), 1)
        # 第 2 次 clientlist 恢复正常：roster_ok 恢复且清除告警状态。
        second = await client.fetch_status(make_server(server.port), "test")
        assert second is not None
        self.assertTrue(second.roster_ok)
        self.assertEqual(len(self.recorder.warnings), 1)
        # 再次失败：应重新告警一次（说明恢复时已清除）。
        server.clientlist_fail_count = 10**6
        third = await client.fetch_status(make_server(server.port), "test")
        assert third is not None
        self.assertFalse(third.roster_ok)
        self.assertEqual(len(self.recorder.warnings), 2)

    async def test_flood_524_returns_none_without_second_login(self) -> None:
        server = await self.start_server(mode="flood")
        client = self.make_client()
        first = await client.fetch_status(make_server(server.port), "test")
        second = await client.fetch_status(make_server(server.port), "test")
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(server.connection_count, 1, "服务端级错误不应重连")
        self.assertEqual(server.command_counts.get("login"), 1)
        self.assertIn("error id 524", self.logs)
        self.assertIn("防洪", self.logs)

    async def test_serverinfo_2568_returns_none_without_retry(self) -> None:
        server = await self.start_server(mode="serverinfo_denied")
        client = self.make_client()
        first = await client.fetch_status(make_server(server.port), "test")
        second = await client.fetch_status(make_server(server.port), "test")
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(server.connection_count, 1)
        self.assertEqual(server.command_counts.get("login"), 1)
        self.assertIn("error id 2568", self.logs)
        self.assertIn("所需权限", self.logs)

    async def test_protocol_misalignment_does_not_fake_online(self) -> None:
        server = await self.start_server(mode="misaligned_serverinfo")
        client = self.make_client()
        snapshot = await client.fetch_status(make_server(server.port), "test")
        self.assertIsNone(snapshot)
        self.assertIn("协议异常", self.logs)
        self.assertEqual(server.connection_count, 2)

    async def test_slow_serverinfo_bounded_by_deadline(self) -> None:
        server = await self.start_server(serverinfo_delay=2.0)
        client = self.make_client(timeout=1.0)
        started = time.monotonic()
        snapshot = await client.fetch_status(make_server(server.port), "test")
        elapsed = time.monotonic() - started
        self.assertIsNone(snapshot)
        self.assertLess(elapsed, 4.0, f"耗时 {elapsed:.2f}s 超出预期")
        self.assertIn("查询超时", self.logs)

    async def test_auth_error_returns_none_without_retry(self) -> None:
        server = await self.start_server(mode="auth_error")
        client = self.make_client()
        snapshot = await client.fetch_status(make_server(server.port), "test")
        self.assertIsNone(snapshot)
        self.assertIn("error id 520", self.logs)
        self.assertIn("认证失败", self.logs)
        self.assertEqual(server.connection_count, 1, "认证失败不应重连")

    async def test_invalid_sid_returns_none_without_retry(self) -> None:
        server = await self.start_server(mode="invalid_sid")
        client = self.make_client()
        snapshot = await client.fetch_status(make_server(server.port), "test")
        self.assertIsNone(snapshot)
        self.assertIn("error id 768", self.logs)
        self.assertIn("virtual_server_id 无效", self.logs)
        self.assertEqual(server.connection_count, 1)

    async def test_malformed_error_id_is_protocol_error(self) -> None:
        server = await self.start_server(mode="malformed_error_id")
        client = self.make_client()
        snapshot = await client.fetch_status(make_server(server.port), "test")
        self.assertIsNone(snapshot)
        self.assertIn("协议异常", self.logs)
        self.assertEqual(server.connection_count, 1, "error 行损坏不应重连")
        await self.wait_for_closed_connections(server)

    async def test_malformed_error_without_id_is_protocol_error(self) -> None:
        server = await self.start_server(mode="malformed_error_no_id")
        client = self.make_client()
        snapshot = await client.fetch_status(make_server(server.port), "test")
        self.assertIsNone(snapshot)
        self.assertIn("协议异常", self.logs)
        self.assertEqual(server.connection_count, 1)
        await self.wait_for_closed_connections(server)

    async def test_multiline_serverinfo_is_merged(self) -> None:
        server = await self.start_server(mode="serverinfo_split")
        client = self.make_client()
        snapshot = await client.fetch_status(make_server(server.port), "test")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.platform, "Linux")
        self.assertEqual(snapshot.version, "3.13.7")
        self.assertEqual(snapshot.voice_port, 9987)
        self.assertEqual(snapshot.max_clients, 32)
        self.assertEqual(snapshot.channels_online, 8)
        self.assertEqual(snapshot.uptime, 3661)
        self.assertEqual(server.connection_count, 1)

    async def test_multiline_clientlist_is_parsed(self) -> None:
        server = await self.start_server(mode="split_clientlist")
        client = self.make_client()
        snapshot = await client.fetch_status(make_server(server.port), "test")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertTrue(snapshot.roster_ok)
        self.assertEqual([c.clid for c in snapshot.clients], [2, 3])
        self.assertEqual(
            [c.nickname for c in snapshot.clients], ["Alice Smith", "Bob|Dev"]
        )

    async def test_notify_line_is_ignored(self) -> None:
        server = await self.start_server(mode="notify_noise")
        client = self.make_client()
        snapshot = await client.fetch_status(make_server(server.port), "test")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.max_clients, 32)
        self.assertEqual(snapshot.clients_online, 2)
        self.assertTrue(snapshot.roster_ok)

    async def test_chunked_greeting_and_response(self) -> None:
        server = await self.start_server(chunk_size=3, chunk_delay=0.002)
        client = self.make_client(timeout=3.0)
        snapshot = await client.fetch_status(make_server(server.port), "test")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.platform, "Linux")
        self.assertEqual(snapshot.clients_online, 2)
        self.assertEqual(server.connection_count, 1)

    async def test_empty_clientlist_is_not_trusted_as_roster(self) -> None:
        server = await self.start_server(mode="empty_clientlist")
        client = self.make_client()
        first = await client.fetch_status(make_server(server.port), "test")
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first.status, "online")
        self.assertFalse(first.roster_ok, "空 clientlist 必须视为协议错误")
        self.assertEqual(first.clients, [])
        self.assertEqual(first.clients_online, 2)
        self.assertIn("协议异常", self.logs)
        await self.wait_for_closed_connections(server)
        # 连接已被丢弃，下一轮重新建立（不会把空名单当成合法基线）。
        second = await client.fetch_status(make_server(server.port), "test")
        self.assertIsNotNone(second)
        assert second is not None
        self.assertFalse(second.roster_ok)
        self.assertEqual(server.connection_count, 2)

    async def test_mixed_clientlist_is_not_trusted_as_roster(self) -> None:
        server = await self.start_server(mode="mixed_clientlist")
        client = self.make_client()
        snapshot = await client.fetch_status(make_server(server.port), "test")
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertFalse(snapshot.roster_ok)
        self.assertEqual(snapshot.clients, [])
        self.assertEqual(snapshot.clients_online, 2)
        self.assertIn("协议异常", self.logs)
        await self.wait_for_closed_connections(server)

    async def test_login_escaping_end_to_end(self) -> None:
        server = await self.start_server()
        client = self.make_client()
        username = "q u|x"
        password = "a b|c\\d\n"
        snapshot = await client.fetch_status(
            make_server(server.port, username=username, password=password),
            "test",
        )
        self.assertIsNotNone(snapshot)
        expected_login = (
            f"login client_login_name={_escape(username)} "
            f"client_login_password={_escape(password)}"
        )
        self.assertEqual(server.raw_commands[0], expected_login)
        self.assertEqual(server.raw_commands[1], "use sid=1")

    async def test_same_key_target_change_reconnects(self) -> None:
        server_a = await self.start_server()
        server_b = await self.start_server()
        client = self.make_client()
        first = await client.fetch_status(
            make_server(server_a.port, "same"), "test"
        )
        second = await client.fetch_status(
            make_server(server_b.port, "same"), "test"
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(server_a.connection_count, 1)
        self.assertEqual(server_b.connection_count, 1)
        self.assertEqual(server_b.command_counts.get("login"), 1)
        await self.wait_for_closed_connections(server_a)

    async def test_keepalive_sends_version_and_reuses_connection(self) -> None:
        original = clients_module.IDLE_KEEPALIVE_SECONDS
        clients_module.IDLE_KEEPALIVE_SECONDS = -1.0
        try:
            server = await self.start_server()
            client = self.make_client()
            first = await client.fetch_status(make_server(server.port), "test")
            second = await client.fetch_status(make_server(server.port), "test")
        finally:
            clients_module.IDLE_KEEPALIVE_SECONDS = original
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(server.command_counts.get("version"), 1)
        self.assertEqual(server.connection_count, 1, "保活不应导致重连")

    async def test_keepalive_transport_failure_reconnects_once(self) -> None:
        original = clients_module.IDLE_KEEPALIVE_SECONDS
        clients_module.IDLE_KEEPALIVE_SECONDS = -1.0
        try:
            server = await self.start_server(close_after_commands=4)
            client = self.make_client()
            first = await client.fetch_status(make_server(server.port), "test")
            second = await client.fetch_status(make_server(server.port), "test")
        finally:
            clients_module.IDLE_KEEPALIVE_SECONDS = original
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        # 第一条连接已被服务端关闭，保活失败后应重连一次再成功。
        self.assertEqual(server.connection_count, 2)
        self.assertEqual(server.command_counts.get("login"), 2)

    async def test_keepalive_query_error_keeps_connection(self) -> None:
        original = clients_module.IDLE_KEEPALIVE_SECONDS
        clients_module.IDLE_KEEPALIVE_SECONDS = -1.0
        try:
            server = await self.start_server(mode="version_denied")
            client = self.make_client()
            first = await client.fetch_status(make_server(server.port), "test")
            second = await client.fetch_status(make_server(server.port), "test")
            third = await client.fetch_status(make_server(server.port), "test")
        finally:
            clients_module.IDLE_KEEPALIVE_SECONDS = original
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertIsNone(third)
        # 保活被服务端拒绝属于服务端级错误：保留连接，不重复登录。
        self.assertEqual(server.connection_count, 1)
        self.assertEqual(server.command_counts.get("login"), 1)
        self.assertIn("error id 524", self.logs)


class CloseIntegrationTest(ServerTestBase):
    async def test_close_is_idempotent_and_blocks_fetch(self) -> None:
        server = await self.start_server()
        client = self.make_client()
        snapshot = await client.fetch_status(make_server(server.port), "test")
        self.assertIsNotNone(snapshot)
        await client.close()
        await client.close()
        self.assertIsNone(
            await client.fetch_status(make_server(server.port), "test")
        )
        await self.wait_for_closed_connections(server)

    async def test_close_finishes_within_two_seconds(self) -> None:
        server = await self.start_server()
        client = self.make_client()
        await client.fetch_status(make_server(server.port), "test")
        started = time.monotonic()
        await client.close()
        elapsed = time.monotonic() - started
        # close 内部对单连接 wait_closed 的上限是 1s，整体上限 2s；阈值放宽到 2.5s。
        self.assertLess(elapsed, 2.5, f"close 耗时 {elapsed:.2f}s 超出预期")


class RobustnessTest(ServerTestBase):
    async def test_connection_refused_returns_none(self) -> None:
        probe = await asyncio.start_server(
            lambda _r, _w: None, "127.0.0.1", 0
        )
        sockets = probe.sockets or []
        port = sockets[0].getsockname()[1]
        probe.close()
        await probe.wait_closed()
        client = self.make_client()
        snapshot = await client.fetch_status(make_server(port), "test")
        self.assertIsNone(snapshot)
        self.assertIn("网络错误", self.logs)

    async def test_different_servers_use_separate_connections(self) -> None:
        server_a = await self.start_server()
        server_b = await self.start_server()
        client = self.make_client()
        results = await asyncio.gather(
            client.fetch_status(make_server(server_a.port, "a"), "a"),
            client.fetch_status(make_server(server_b.port, "b"), "b"),
        )
        self.assertTrue(all(r is not None for r in results))
        self.assertEqual(server_a.connection_count, 1)
        self.assertEqual(server_b.connection_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
