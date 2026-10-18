"""monitoring.py 状态滞回、首次基线与用户进出检测测试。"""

from __future__ import annotations

import unittest

import _support  # noqa: F401

from astrbot_plugin_ts3_server_guard.ts_server_monitor.constants import SEPARATOR
from astrbot_plugin_ts3_server_guard.ts_server_monitor.models import (
    ClientInfo,
    DisplayOptions,
    ServerConfig,
    ServerSnapshot,
    ServerState,
)
from astrbot_plugin_ts3_server_guard.ts_server_monitor.monitoring import (
    build_initial_states,
    detect_join_leave,
    format_server_info,
    observe_status,
    sanitize_nickname,
    wrap_notice,
)

INTERVAL = 15.0


def make_server(name: str = "测试服", interval: int = 15) -> ServerConfig:
    return ServerConfig(
        key="srv",
        enabled=True,
        server_name=name,
        host="127.0.0.1",
        query_port=10011,
        query_user="u",
        query_password="p",
        virtual_server_id=1,
        join_leave_interval=interval,
    )


def make_snapshot(
    nicks: list[str] | None = None,
    *,
    clids: list[int] | None = None,
    status: str = "online",
    clients_online: int | None = None,
    roster_ok: bool = True,
    voice_port: int = 0,
) -> ServerSnapshot:
    nicks = list(nicks or [])
    if clids is None:
        clids = [i + 1 for i in range(len(nicks))]
    clients = [
        ClientInfo(clid=clid, nickname=n, database_id=100 + i, cid=1, client_type=0)
        for i, (clid, n) in enumerate(zip(clids, nicks))
    ]
    return ServerSnapshot(
        key="srv",
        name="测试服",
        host="127.0.0.1",
        query_port=10011,
        status=status,
        platform="Linux",
        version="3.13.7",
        clients_online=len(clients) if clients_online is None else clients_online,
        max_clients=32,
        channels_online=8,
        uptime=3661,
        clients=clients,
        roster_ok=roster_ok,
        voice_port=voice_port,
        update_time="2025-12-15 21:00:00",
    )


class BaselineTest(unittest.TestCase):
    def test_first_call_only_builds_baseline(self) -> None:
        state = ServerState()
        server = make_server()

        joins, leaves, message = detect_join_leave(
            server, state, make_snapshot(["Alice"]), now=0.0, debounce_seconds=3
        )
        self.assertEqual((joins, leaves, message), (set(), set(), None))
        self.assertTrue(state.join_leave_initialized)
        self.assertEqual(state.last_clients, {1: "Alice"})

    def test_empty_server_first_join_is_announced(self) -> None:
        """空服务器是合法基线：第一个人加入必须在防抖确认后播报。"""
        state = ServerState()
        server = make_server()

        detect_join_leave(server, state, make_snapshot([]), now=0.0, debounce_seconds=3)
        detect_join_leave(server, state, make_snapshot([]), now=15.0, debounce_seconds=3)
        # 第一个人出现在本轮的 pending 中，防抖未到期
        joins, _, message = detect_join_leave(
            server, state, make_snapshot(["Alice"]), now=30.0, debounce_seconds=3
        )
        self.assertEqual(joins, set())
        self.assertIsNone(message)
        # 下一轮仍在 → 确认
        joins, _, message = detect_join_leave(
            server, state, make_snapshot(["Alice"]), now=45.0, debounce_seconds=3
        )
        self.assertEqual(joins, {"Alice"})
        self.assertIn("Alice 加入了服务器", message or "")


class JoinLeaveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = ServerState()
        self.server = make_server()
        detect_join_leave(self.server, self.state, make_snapshot([]), now=0.0, debounce_seconds=3)

    def test_join_and_leave(self) -> None:
        detect_join_leave(self.server, self.state, make_snapshot(["A"]), now=15.0, debounce_seconds=3)
        joins, _, msg = detect_join_leave(
            self.server, self.state, make_snapshot(["A"]), now=30.0, debounce_seconds=3
        )
        self.assertEqual(joins, {"A"})
        self.assertIn("A 加入了服务器", msg or "")

        detect_join_leave(self.server, self.state, make_snapshot([]), now=45.0, debounce_seconds=3)
        _, leaves, msg = detect_join_leave(
            self.server, self.state, make_snapshot([]), now=60.0, debounce_seconds=3
        )
        self.assertEqual(leaves, {"A"})
        self.assertIn("A 离开了服务器", msg or "")

    def test_single_poll_jitter_is_dropped(self) -> None:
        """用户闪过一轮又消失：不播报。"""
        detect_join_leave(self.server, self.state, make_snapshot(["A"]), now=15.0, debounce_seconds=3)
        joins, _, msg = detect_join_leave(
            self.server, self.state, make_snapshot([]), now=30.0, debounce_seconds=3
        )
        self.assertEqual(joins, set())
        self.assertIsNone(msg)

    def test_debounce_zero_confirms_in_same_poll(self) -> None:
        joins, _, msg = detect_join_leave(
            self.server, self.state, make_snapshot(["Z"]), now=15.0, debounce_seconds=0
        )
        self.assertEqual(joins, {"Z"})
        self.assertIn("Z 加入了服务器", msg or "")

    def test_debounce_longer_than_interval_delays_confirmation(self) -> None:
        detect_join_leave(self.server, self.state, make_snapshot(["A"]), now=15.0, debounce_seconds=30)
        joins, _, _ = detect_join_leave(
            self.server, self.state, make_snapshot(["A"]), now=30.0, debounce_seconds=30
        )
        self.assertEqual(joins, set())
        joins, _, _ = detect_join_leave(
            self.server, self.state, make_snapshot(["A"]), now=45.0, debounce_seconds=30
        )
        self.assertEqual(joins, {"A"})

    def test_build_initial_states_matches_server_keys(self) -> None:
        servers = [make_server("A"), make_server("B")]
        servers[1].key = "srv2"
        states = build_initial_states(servers)
        self.assertEqual(set(states), {"srv", "srv2"})
        self.assertTrue(all(isinstance(s, ServerState) for s in states.values()))


class ObserveStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.state = ServerState()
        self.server = make_server()

    def observe(self, status: str, now: float):
        return observe_status(
            self.server,
            self.state,
            make_snapshot(status=status),
            confirm_online=2,
            confirm_offline=3,
        )

    def test_startup_online_emits_startup_once(self) -> None:
        kind, msg = self.observe("online", 0.0)
        self.assertEqual(kind, "startup")
        self.assertIn("监控已启动", msg or "")
        kind, msg = self.observe("online", 15.0)
        self.assertEqual((kind, msg), (None, None))

    def test_startup_offline_is_silent_then_up(self) -> None:
        self.assertEqual(self.observe("offline", 0.0), (None, None))
        self.assertEqual(self.state.health, "offline")
        self.assertEqual(self.observe("online", 15.0), (None, None))
        kind, msg = self.observe("online", 30.0)
        self.assertEqual(kind, "up")
        self.assertIn("已上线", msg or "")

    def test_single_failure_does_not_flip(self) -> None:
        self.observe("online", 0.0)
        self.assertEqual(self.observe("offline", 15.0), (None, None))
        self.assertEqual(self.observe("online", 30.0), (None, None))
        self.assertEqual(self.state.health, "online")
        self.assertEqual(self.state.contradict_probes, 0)

    def test_three_failures_flip_down_and_clear_roster(self) -> None:
        self.observe("online", 0.0)
        detect_join_leave(self.server, self.state, make_snapshot(["A"]), now=0.0, debounce_seconds=3)
        detect_join_leave(self.server, self.state, make_snapshot(["A"]), now=15.0, debounce_seconds=3)

        self.assertEqual(self.observe("offline", 15.0), (None, None))
        self.assertEqual(self.observe("offline", 30.0), (None, None))
        kind, msg = self.observe("offline", 45.0)
        self.assertEqual(kind, "down")
        self.assertIn("已离线", msg or "")
        self.assertEqual(self.state.last_clients, {})
        self.assertTrue(self.state.rebaseline_roster)
        # 哨兵不回退：恢复后仍然走正常流程
        self.assertTrue(self.state.join_leave_initialized)

    def test_two_successes_flip_up(self) -> None:
        self.observe("online", 0.0)
        self.observe("offline", 15.0)
        self.observe("offline", 30.0)
        kind, _ = self.observe("offline", 45.0)
        self.assertEqual(kind, "down")
        self.assertEqual(self.observe("online", 60.0), (None, None))
        kind, msg = self.observe("online", 75.0)
        self.assertEqual(kind, "up")
        self.assertIn("已上线", msg or "")


class FormatTest(unittest.TestCase):
    def test_offline_snapshot_has_error_note(self) -> None:
        snapshot = make_snapshot(status="offline", clients_online=0)
        snapshot.error = "TS3 ServerQuery 查询失败"
        text = format_server_info(snapshot, DisplayOptions())
        self.assertIn("[离线]", text)
        self.assertIn("说明: TS3 ServerQuery 查询失败", text)

    def test_empty_snapshot(self) -> None:
        self.assertEqual(format_server_info(None, DisplayOptions()), "获取服务器数据失败")

    def test_wrap_notice_contains_title_and_separator(self) -> None:
        text = wrap_notice("标题", "正文")
        self.assertIn("标题", text)
        self.assertIn("正文", text)
        self.assertEqual(text.count(SEPARATOR), 3)


class IdentityTest(unittest.TestCase):
    """按 clid 识别用户：同名用户不再互相吞事件。"""

    def setUp(self) -> None:
        self.state = ServerState()
        self.server = make_server()
        detect_join_leave(self.server, self.state, make_snapshot([]), now=0.0, debounce_seconds=3)

    def test_duplicate_nicknames_are_independent(self) -> None:
        snapshot = make_snapshot(["User", "User"], clids=[1, 2])
        detect_join_leave(self.server, self.state, snapshot, now=15.0, debounce_seconds=3)
        joins, _, msg = detect_join_leave(
            self.server, self.state, snapshot, now=30.0, debounce_seconds=3
        )
        self.assertEqual(joins, {"User"})  # 集合按昵称去重
        self.assertEqual((msg or "").count("User"), 2)  # 消息按 clid 逐个渲染
        self.assertEqual(self.state.last_clients, {1: "User", 2: "User"})

        # 其中一个离开，另一个还在：必须播报离开
        one_left = make_snapshot(["User"], clids=[1])
        detect_join_leave(self.server, self.state, one_left, now=45.0, debounce_seconds=3)
        _, leaves, msg = detect_join_leave(
            self.server, self.state, one_left, now=60.0, debounce_seconds=3
        )
        self.assertEqual(leaves, {"User"})
        self.assertIn("User 离开了服务器", msg or "")
        self.assertEqual(self.state.last_clients, {1: "User"})

    def test_rename_on_same_clid_is_silent(self) -> None:
        detect_join_leave(self.server, self.state, make_snapshot(["A"]), now=15.0, debounce_seconds=3)
        detect_join_leave(self.server, self.state, make_snapshot(["A"]), now=30.0, debounce_seconds=3)
        self.assertEqual(self.state.last_clients, {1: "A"})

        renamed = make_snapshot(["B"])
        joins, leaves, msg = detect_join_leave(
            self.server, self.state, renamed, now=45.0, debounce_seconds=3
        )
        self.assertEqual((joins, leaves, msg), (set(), set(), None))
        self.assertEqual(self.state.last_clients, {1: "B"})


class RecoveryTest(unittest.TestCase):
    """恢复在线不应把所有人重新播报为加入。"""

    def test_recovery_rebaselines_without_join_burst(self) -> None:
        state = ServerState()
        server = make_server()
        detect_join_leave(server, state, make_snapshot([]), now=0.0, debounce_seconds=3)
        detect_join_leave(server, state, make_snapshot(["A"]), now=15.0, debounce_seconds=3)
        detect_join_leave(server, state, make_snapshot(["A"]), now=30.0, debounce_seconds=3)

        # 先建立在线健康基线，再确认离线
        observe_status(
            server,
            state,
            make_snapshot([]),
            confirm_online=2,
            confirm_offline=3,
        )
        for _ in (45.0, 60.0, 75.0):
            observe_status(
                server,
                state,
                make_snapshot(status="offline"),
                confirm_online=2,
                confirm_offline=3,
            )
        self.assertEqual(state.health, "offline")
        self.assertTrue(state.rebaseline_roster)

        # 恢复后的第一轮：静默重建基线，不报加入
        offline = make_snapshot(["A", "B"])
        joins, leaves, msg = detect_join_leave(
            server, state, offline, now=90.0, debounce_seconds=3
        )
        self.assertEqual((joins, leaves, msg), (set(), set(), None))
        self.assertFalse(state.rebaseline_roster)
        self.assertEqual(state.last_clients, {1: "A", 2: "B"})

        # 之后正常检测新的离开
        detect_join_leave(server, state, make_snapshot(["A"]), now=105.0, debounce_seconds=3)
        _, leaves, _ = detect_join_leave(
            server, state, make_snapshot(["A"]), now=120.0, debounce_seconds=3
        )
        self.assertEqual(leaves, {"B"})


class SanitizeAndFormatTest(unittest.TestCase):
    def test_sanitize_nickname(self) -> None:
        self.assertEqual(sanitize_nickname("bad\nname\t"), "badname")
        self.assertEqual(len(sanitize_nickname("x" * 100)), 32)
        self.assertEqual(sanitize_nickname("  Alice  "), "Alice")
        self.assertEqual(sanitize_nickname("\n\t"), "")

    def test_control_chars_do_not_forge_notice_lines(self) -> None:
        state = ServerState()
        server = make_server()
        detect_join_leave(server, state, make_snapshot([]), now=0.0, debounce_seconds=3)
        evil = make_snapshot(["evil\nTS3 服务器状态\n主服 已离线"])
        detect_join_leave(server, state, evil, now=15.0, debounce_seconds=3)
        _, _, msg = detect_join_leave(server, state, evil, now=30.0, debounce_seconds=3)
        self.assertNotIn("\nTS3 服务器状态", msg or "")
        self.assertIn("evil", msg or "")

    def test_offline_format_uses_dash(self) -> None:
        snapshot = make_snapshot(status="offline", clients_online=0)
        snapshot.error = "TS3 ServerQuery 查询失败"
        text = format_server_info(snapshot, DisplayOptions())
        self.assertIn("在线人数: —", text)
        self.assertIn("平台: —", text)
        self.assertNotIn("0/0", text)
        self.assertIn("说明: TS3 ServerQuery 查询失败", text)

    def test_voice_port_address(self) -> None:
        snapshot = make_snapshot(voice_port=9987)
        text = format_server_info(
            snapshot, DisplayOptions(show_server_address=True)
        )
        self.assertIn("地址: 127.0.0.1:9987", text)
        self.assertIn("ServerQuery 10011", text)

    def test_address_falls_back_to_query_port(self) -> None:
        snapshot = make_snapshot(voice_port=0)
        text = format_server_info(
            snapshot, DisplayOptions(show_server_address=True)
        )
        self.assertIn("地址: 127.0.0.1:10011", text)
        self.assertNotIn("ServerQuery", text)

    def test_client_list_is_sanitized_and_capped(self) -> None:
        nicks = [f"User{i}" for i in range(25)]
        snapshot = make_snapshot(nicks, clids=list(range(1, 26)))
        text = format_server_info(
            snapshot, DisplayOptions(show_client_list=True)
        )
        self.assertIn("等共 25 人", text)
        self.assertNotIn("User24", text)


class LargeBurstTest(unittest.TestCase):
    def test_join_burst_message_is_capped(self) -> None:
        state = ServerState()
        server = make_server()
        detect_join_leave(server, state, make_snapshot([]), now=0.0, debounce_seconds=3)
        nicks = [f"User{i}" for i in range(25)]
        clids = list(range(1, 26))
        snapshot = make_snapshot(nicks, clids=clids)
        detect_join_leave(server, state, snapshot, now=15.0, debounce_seconds=3)
        _, _, msg = detect_join_leave(
            server, state, snapshot, now=30.0, debounce_seconds=3
        )
        self.assertIn("等共 25 人", msg or "")
        self.assertLessEqual(len(msg or ""), 200)


if __name__ == "__main__":
    unittest.main()
