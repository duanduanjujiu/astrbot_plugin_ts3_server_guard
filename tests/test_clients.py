"""clients.py 基础行为测试（不依赖 ts3 包与真实网络）。"""

from __future__ import annotations

import asyncio
import unittest

import _support  # noqa: F401

from astrbot_plugin_ts3_server_guard.ts_server_monitor.clients import (
    LocalStatusClient,
    _describe_error,
    _to_int,
    build_status_client,
)
from astrbot_plugin_ts3_server_guard.ts_server_monitor.models import (
    ServerConfig,
    ServerSnapshot,
)


def make_server() -> ServerConfig:
    return ServerConfig(
        key="srv",
        enabled=True,
        server_name="测试服",
        host="127.0.0.1",
        query_port=10011,
        query_user="u",
        query_password="p",
        virtual_server_id=1,
        join_leave_interval=15,
    )


class HelpersTest(unittest.TestCase):
    def test_to_int(self) -> None:
        self.assertEqual(_to_int("12"), 12)
        self.assertEqual(_to_int(None), 0)
        self.assertEqual(_to_int("abc"), 0)
        self.assertEqual(_to_int("abc", -1), -1)

    def test_describe_error_for_missing_dependency(self) -> None:
        text = _describe_error(ModuleNotFoundError("No module named 'ts3'"))
        self.assertIn("缺少 ts3 依赖", text)

    def test_describe_error_for_generic(self) -> None:
        self.assertIn("RuntimeError", _describe_error(RuntimeError("boom")))

    def test_snapshot_has_roster_ok_and_no_channels(self) -> None:
        snapshot = ServerSnapshot(
            key="k",
            name="n",
            host="h",
            query_port=10011,
            status="online",
            platform="Linux",
            version="3",
            clients_online=0,
            max_clients=32,
            channels_online=8,
            uptime=0,
        )
        self.assertTrue(snapshot.roster_ok)
        self.assertEqual(snapshot.voice_port, 0)
        self.assertFalse(hasattr(snapshot, "channels"))


class LocalStatusClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_closed_client_returns_none(self) -> None:
        client = LocalStatusClient(timeout=1.0)
        self.assertIsInstance(client, LocalStatusClient)
        await client.close()
        self.assertIsNone(await client.fetch_status(make_server(), "test"))

    async def test_close_is_idempotent(self) -> None:
        client = LocalStatusClient(timeout=1.0)
        await client.close()
        await client.close()

    def test_build_status_client(self) -> None:
        client = build_status_client()
        self.assertIsInstance(client, LocalStatusClient)
        asyncio.run(client.close())


if __name__ == "__main__":
    unittest.main()
