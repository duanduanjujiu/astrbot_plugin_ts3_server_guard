"""main.py 目标解析、命令行为与通知队列测试（不启动 AstrBot）。"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

import _support  # noqa: F401
from astrbot_plugin_ts3_server_guard import main as plugin_module
from astrbot_plugin_ts3_server_guard.ts_server_monitor.models import (
    DisplayOptions,
    PluginSettings,
    ServerConfig,
    ServerSnapshot,
    ServerState,
)


class FakeEvent:
    def __init__(self, message_str: str, umo: str = "robot:GroupMessage:100") -> None:
        self.message_str = message_str
        self.unified_msg_origin = umo

    def plain_result(self, text: str):
        return ("plain", text)


def make_server(key: str = "srv", interval: int = 15) -> ServerConfig:
    return ServerConfig(
        key=key,
        enabled=True,
        server_name=f"服务器-{key}",
        host="127.0.0.1",
        query_port=10011,
        query_user="u",
        query_password="p",
        virtual_server_id=1,
        join_leave_interval=interval,
    )


def make_snapshot(status: str = "online") -> ServerSnapshot:
    return ServerSnapshot(
        key="srv",
        name="测试服",
        host="127.0.0.1",
        query_port=10011,
        status=status,
        platform="Linux",
        version="3",
        clients_online=1,
        max_clients=32,
        channels_online=8,
        uptime=60,
        update_time="2025-12-15 21:00:00",
    )


def make_plugin(
    *,
    target_umo: str | None = None,
    target_group: str | None = None,
    umo_override: str | None = None,
    group_override: str | None = None,
) -> plugin_module.MyPlugin:
    plugin = plugin_module.MyPlugin.__new__(plugin_module.MyPlugin)
    plugin.context = None
    plugin.config = {}
    plugin.settings = PluginSettings(
        target_group=target_group,
        target_umo=target_umo,
        enable_auto_monitor=False,
        global_join_leave_interval=15,
        global_join_leave_debounce=3.0,
        enable_status_push=True,
        enable_startup_notify=False,
        enable_join_leave_notify=True,
        display_options=DisplayOptions(),
    )
    plugin.servers = []
    plugin.server_states = {}
    plugin.server_locks = {}
    plugin.status_client = None
    plugin.task = None
    plugin._delayed_start_task = None
    plugin._terminated = True
    plugin._notify_queue = asyncio.Queue(maxsize=50)
    plugin._notify_task = None
    plugin._roster_warned = set()
    plugin._snapshots = {}
    plugin._query_cooldown = {}
    plugin._umo_override = umo_override
    plugin._group_override = group_override
    return plugin


async def collect(agen) -> list:
    return [item async for item in agen]


class ResolveTargetTest(unittest.TestCase):
    def test_truth_table(self) -> None:
        resolve = plugin_module.resolve_target
        self.assertEqual(resolve("u1", None, "u2", "g2"), ("u1", None))
        self.assertEqual(resolve("u1", "g1", "u2", "g2"), ("u1", None))
        self.assertEqual(resolve(None, "g1", "u2", "g2"), (None, "g1"))
        self.assertEqual(resolve(None, None, "u2", "g2"), ("u2", None))
        self.assertEqual(resolve(None, None, None, "g2"), (None, "g2"))
        self.assertEqual(resolve(None, None, None, None), (None, None))

    def test_effective_target(self) -> None:
        plugin = make_plugin(target_umo="robot:GroupMessage:1", group_override="333")
        self.assertEqual(plugin._effective_target(), (None, "333"))
        self.assertEqual(plugin._target_desc(), "QQ群: 333")

        plugin = make_plugin(target_umo="robot:GroupMessage:1")
        self.assertEqual(plugin._effective_target(), ("robot:GroupMessage:1", None))


class CommandRestTest(unittest.TestCase):
    def test_command_rest(self) -> None:
        self.assertEqual(plugin_module._command_rest(FakeEvent("ts_push_target")), "")
        self.assertEqual(
            plugin_module._command_rest(FakeEvent("ts_push_target 本群")), "本群"
        )
        self.assertEqual(
            plugin_module._command_rest(FakeEvent("ts_push_target\t123")), "123"
        )


class SetPushTargetTest(unittest.IsolatedAsyncioTestCase):
    async def test_group_target_saved_after_success_even_with_config_umo(self) -> None:
        """P0-1 回归：WebUI 配了 target_umo 时，群号命令必须真正切到群号。"""
        plugin = make_plugin(target_umo="robot:GroupMessage:999")
        sent: list[tuple] = []

        async def fake_send_test(*, umo, group_id, cause):
            sent.append((umo, group_id, cause))
            return True

        plugin._send_test = fake_send_test
        event = FakeEvent("ts_push_target 123456789")

        with mock.patch.object(plugin_module, "save_runtime_target") as save:
            results = await collect(plugin.set_push_target(event))

        self.assertEqual(sent, [(None, "123456789", "输入 QQ 群号")])
        save.assert_called_once_with(group_id="123456789")
        self.assertIsNone(plugin._umo_override)
        self.assertEqual(plugin._group_override, "123456789")
        self.assertEqual(plugin._effective_target(), (None, "123456789"))
        self.assertIn("设置成功", results[0][1])

    async def test_failed_test_does_not_save_or_switch(self) -> None:
        plugin = make_plugin(target_umo="robot:GroupMessage:999")
        plugin._group_override = None

        async def fake_send_test(*, umo, group_id, cause):
            return False

        plugin._send_test = fake_send_test
        event = FakeEvent("ts_push_target 123456789")

        with mock.patch.object(plugin_module, "save_runtime_target") as save:
            results = await collect(plugin.set_push_target(event))

        save.assert_not_called()
        self.assertIsNone(plugin._group_override)
        self.assertEqual(plugin._effective_target(), ("robot:GroupMessage:999", None))
        self.assertIn("未保存", results[0][1])

    async def test_umo_target_saved(self) -> None:
        plugin = make_plugin(target_umo="robot:GroupMessage:999")
        sent: list[tuple] = []

        async def fake_send_test(*, umo, group_id, cause):
            sent.append((umo, group_id, cause))
            return True

        plugin._send_test = fake_send_test
        event = FakeEvent("ts_push_target robot:GroupMessage:777")

        with mock.patch.object(plugin_module, "save_runtime_target") as save:
            results = await collect(plugin.set_push_target(event))

        self.assertEqual(sent, [("robot:GroupMessage:777", None, "输入 UMO")])
        save.assert_called_once_with(umo="robot:GroupMessage:777")
        self.assertEqual(plugin._effective_target(), ("robot:GroupMessage:777", None))
        self.assertIn("设置成功", results[0][1])

    async def test_clear_target(self) -> None:
        plugin = make_plugin(
            target_umo="robot:GroupMessage:999", group_override="333"
        )
        event = FakeEvent("ts_push_target 清除")

        with mock.patch.object(plugin_module, "clear_runtime_target") as clear:
            results = await collect(plugin.set_push_target(event))

        clear.assert_called_once()
        self.assertIsNone(plugin._umo_override)
        self.assertIsNone(plugin._group_override)
        self.assertEqual(plugin._effective_target(), ("robot:GroupMessage:999", None))
        self.assertIn("已清除", results[0][1])


class NotifyQueueTest(unittest.IsolatedAsyncioTestCase):
    async def test_enqueue_captures_target(self) -> None:
        plugin = make_plugin(umo_override="robot:GroupMessage:1")
        plugin._enqueue_notification("hello")
        text, umo, group = plugin._notify_queue.get_nowait()
        self.assertEqual((text, umo, group), ("hello", "robot:GroupMessage:1", None))

    async def test_deliver_retries_then_succeeds(self) -> None:
        plugin = make_plugin()
        calls: list[str] = []

        async def fake_send_text(text, umo, group_id):
            calls.append(text)
            return len(calls) >= 3

        plugin._send_text = fake_send_text

        with mock.patch.object(
            plugin_module.asyncio, "sleep", new=mock.AsyncMock()
        ) as sleep:
            ok = await plugin._deliver_notification("hi", None, "123")

        self.assertTrue(ok)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.await_count, 2)

    async def test_deliver_gives_up_after_max_attempts(self) -> None:
        plugin = make_plugin()

        async def always_fail(text, umo, group_id):
            return False

        plugin._send_text = always_fail

        with mock.patch.object(
            plugin_module.asyncio, "sleep", new=mock.AsyncMock()
        ):
            ok = await plugin._deliver_notification("hi", None, "123")

        self.assertFalse(ok)

    async def test_queue_full_drops_oldest(self) -> None:
        plugin = make_plugin(umo_override="robot:GroupMessage:1")
        plugin._notify_queue = asyncio.Queue(maxsize=1)
        plugin._enqueue_notification("old")
        plugin._enqueue_notification("new")
        text, _, _ = plugin._notify_queue.get_nowait()
        self.assertEqual(text, "new")


class FakeStatusClient:
    def __init__(self, snapshot: ServerSnapshot | None) -> None:
        self.snapshot = snapshot
        self.calls = 0

    async def fetch_status(self, server, source):
        self.calls += 1
        return self.snapshot


class QueryCacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_cache_hit_avoids_live_fetch(self) -> None:
        plugin = make_plugin()
        server = make_server()
        plugin.servers = [server]
        snapshot = make_snapshot()
        plugin._snapshots[server.key] = (snapshot, 100.0)
        fake = FakeStatusClient(snapshot)
        plugin.status_client = fake

        got = await plugin._get_query_snapshot(server, now=101.0)

        self.assertIs(got, snapshot)
        self.assertEqual(fake.calls, 0)

    async def test_expired_cache_fetches_and_updates(self) -> None:
        plugin = make_plugin()
        server = make_server()
        plugin.servers = [server]
        snapshot = make_snapshot()
        plugin._snapshots[server.key] = (snapshot, 0.0)
        fake = FakeStatusClient(snapshot)
        plugin.status_client = fake

        got = await plugin._get_query_snapshot(server, now=100.0)

        self.assertIs(got, snapshot)
        self.assertEqual(fake.calls, 1)
        self.assertIn(server.key, plugin._snapshots)

    async def test_query_cooldown_per_session(self) -> None:
        plugin = make_plugin()

        self.assertEqual(plugin._register_query("umo-a", 100.0), 0)
        self.assertGreaterEqual(plugin._register_query("umo-a", 101.0), 1)
        self.assertEqual(plugin._register_query("umo-b", 101.0), 0)  # 不同会话
        self.assertEqual(plugin._register_query("umo-a", 106.0), 0)  # 冷却结束

    async def test_ts_query_command_rejects_within_cooldown(self) -> None:
        plugin = make_plugin()
        event = FakeEvent("ts_query")

        first = await collect(plugin.query_server_status(event))
        second = await collect(plugin.query_server_status(event))

        self.assertIn("当前没有启用", first[0][1])
        self.assertIn("查询过于频繁", second[0][1])


class JoinSwitchTest(unittest.IsolatedAsyncioTestCase):
    async def test_switch_off_keeps_baseline_but_skips_notice(self) -> None:
        plugin = make_plugin()
        server = make_server()
        state = ServerState()
        snapshot = make_snapshot()

        plugin.settings.enable_join_leave_notify = False
        with mock.patch.object(
            plugin_module,
            "detect_join_leave",
            return_value=({"Alice"}, set(), "Alice 加入了服务器"),
        ) as detect:
            await plugin._run_join_leave(server, state, snapshot, now=100.0)

        detect.assert_called_once()
        self.assertTrue(plugin._notify_queue.empty())

    async def test_switch_on_enqueues_notice(self) -> None:
        plugin = make_plugin()
        plugin.settings.enable_join_leave_notify = True
        server = make_server()
        state = ServerState()

        with mock.patch.object(
            plugin_module,
            "detect_join_leave",
            return_value=({"Alice"}, set(), "Alice 加入了服务器"),
        ):
            await plugin._run_join_leave(server, state, make_snapshot(), now=100.0)

        text, _, _ = plugin._notify_queue.get_nowait()
        self.assertIn("Alice 加入了服务器", text)


class ConcurrencyTest(unittest.IsolatedAsyncioTestCase):
    async def test_tick_round_runs_servers_concurrently(self) -> None:
        plugin = make_plugin()
        servers = [make_server("a"), make_server("b"), make_server("c")]
        plugin.servers = servers
        plugin.server_states = {}
        plugins_called: list[str] = []

        async def fake_tick(server, state):
            plugins_called.append(server.key)
            await asyncio.sleep(0.1)

        plugin._monitor_tick = fake_tick
        loop = asyncio.get_running_loop()
        started = loop.time()
        await plugin._run_tick_round()
        elapsed = loop.time() - started

        self.assertLess(elapsed, 0.2)  # 并发：约 0.1s，而不是 0.3s
        self.assertEqual(set(plugins_called), {"a", "b", "c"})


class QueryCooldownTest(unittest.IsolatedAsyncioTestCase):
    async def test_register_query_prunes_large_dict(self) -> None:
        plugin = make_plugin()
        for i in range(200):
            plugin._register_query(f"umo-{i}", now=100.0 + i * 0.01)
        self.assertLessEqual(len(plugin._query_cooldown), 129)


if __name__ == "__main__":
    unittest.main()
