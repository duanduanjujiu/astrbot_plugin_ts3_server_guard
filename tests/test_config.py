"""config.py 服务器条目解析、唯一 key、端口/实例 ID 校验测试。"""

from __future__ import annotations

import unittest

import _support  # noqa: F401
from astrbot_plugin_ts3_server_guard.ts_server_monitor.config import (
    clean_secret,
    clean_text,
    load_servers_from_config,
    load_settings,
    parse_target_group,
    parse_umo,
    resolve_join_leave_interval,
    resolve_query_port,
    resolve_virtual_server_id,
)
from astrbot_plugin_ts3_server_guard.ts_server_monitor.constants import (
    DEFAULT_QUERY_PORT,
    DEFAULT_VIRTUAL_SERVER_ID,
)

BASE_ENTRY = {
    "alias": "main",
    "display_name": "主服务器",
    "enabled": True,
    "host": "192.168.1.10",
    "query_port": 10011,
    "query_user": "query",
    "query_password": "secret",
    "virtual_server_id": 1,
    "join_leave_interval": 15,
}


def entry(**overrides):
    data = dict(BASE_ENTRY)
    data.update(overrides)
    return data


class TextHelpersTest(unittest.TestCase):
    def test_clean_text(self) -> None:
        self.assertEqual(clean_text(None), "")
        self.assertEqual(clean_text("  x  "), "x")
        self.assertEqual(clean_text(["x"]), "")
        self.assertEqual(clean_text(3), "3")

    def test_clean_secret_keeps_spaces(self) -> None:
        self.assertEqual(clean_secret(" pass "), " pass ")
        self.assertEqual(clean_secret(None), "")
        self.assertEqual(clean_secret({"a": 1}), "")


class ParseUmoTest(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(parse_umo("robot:GroupMessage:123"), "robot:GroupMessage:123")
        self.assertEqual(
            parse_umo("  robot:GroupMessage:123  "), "robot:GroupMessage:123"
        )

    def test_invalid(self) -> None:
        for value in (
            None,
            123,
            ["robot:GroupMessage:123"],
            "",
            "robot:GroupMessage",
            "robot::123",
            "robot:GroupMessage:123:",
        ):
            with self.subTest(value=value):
                self.assertIsNone(parse_umo(value))


class ParseTargetGroupTest(unittest.TestCase):
    def test_valid(self) -> None:
        self.assertEqual(parse_target_group("123456789"), "123456789")

    def test_invalid(self) -> None:
        self.assertIsNone(parse_target_group("abc"))
        self.assertIsNone(parse_target_group("12 34"))
        self.assertIsNone(parse_target_group(None))


class IntervalAndPortTest(unittest.TestCase):
    def test_interval_floor(self) -> None:
        self.assertEqual(resolve_join_leave_interval(0, 15), 15)
        self.assertEqual(resolve_join_leave_interval(-5, 15), 15)
        self.assertEqual(resolve_join_leave_interval(5, 15), 5)
        self.assertEqual(resolve_join_leave_interval(1, 15), 5)
        self.assertEqual(resolve_join_leave_interval(60, 15), 60)

    def test_port_validation(self) -> None:
        self.assertEqual(resolve_query_port(0, 10011), 10011)
        self.assertEqual(resolve_query_port(None, 10011), 10011)
        self.assertEqual(resolve_query_port(9987, 10011), 9987)
        self.assertEqual(resolve_query_port(65535, 10011), 65535)
        self.assertEqual(resolve_query_port(65536, 10011), DEFAULT_QUERY_PORT)
        self.assertEqual(resolve_query_port(70000, 10011), DEFAULT_QUERY_PORT)
        self.assertEqual(resolve_query_port("abc", 10011), DEFAULT_QUERY_PORT)

    def test_virtual_server_id_validation(self) -> None:
        self.assertEqual(resolve_virtual_server_id(None), DEFAULT_VIRTUAL_SERVER_ID)
        self.assertEqual(resolve_virtual_server_id(0), DEFAULT_VIRTUAL_SERVER_ID)
        self.assertEqual(resolve_virtual_server_id(-1), DEFAULT_VIRTUAL_SERVER_ID)
        self.assertEqual(resolve_virtual_server_id(2), 2)
        self.assertEqual(resolve_virtual_server_id("3"), 3)


class LoadServersTest(unittest.TestCase):
    def test_normal_entry(self) -> None:
        servers = load_servers_from_config({"server_entries": [entry()]}, 15)
        self.assertEqual(len(servers), 1)
        server = servers[0]
        self.assertEqual(server.key, "main")
        self.assertEqual(server.server_name, "主服务器")
        self.assertEqual(server.query_port, 10011)
        self.assertEqual(server.join_leave_interval, 15)

    def test_duplicate_alias_gets_unique_keys(self) -> None:
        config = {
            "server_entries": [
                entry(alias="same", host="h1"),
                entry(alias="same", host="h2"),
            ]
        }
        servers = load_servers_from_config(config, 15)
        self.assertEqual([s.key for s in servers], ["same", "same_2"])
        self.assertEqual([s.host for s in servers], ["h1", "h2"])

    def test_slug_collision_gets_unique_keys(self) -> None:
        config = {
            "server_entries": [
                entry(alias="", display_name="TS 1", host="h1"),
                entry(alias="", display_name="TS#1", host="h2"),
            ]
        }
        servers = load_servers_from_config(config, 15)
        self.assertEqual(len(servers), 2)
        self.assertEqual(len({s.key for s in servers}), 2)

    def test_null_required_field_is_skipped(self) -> None:
        config = {"server_entries": [entry(host=None), entry(alias="ok")]}
        servers = load_servers_from_config(config, 15)
        self.assertEqual([s.key for s in servers], ["ok"])

    def test_null_optional_fields_are_empty(self) -> None:
        config = {
            "server_entries": [entry(display_name=None, join_leave_interval=None)]
        }
        servers = load_servers_from_config(config, 15)
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0].server_name, "main")  # display_name 回退 alias
        self.assertEqual(servers[0].join_leave_interval, 15)

    def test_disabled_and_non_dict_entries_are_skipped(self) -> None:
        config = {
            "server_entries": [
                entry(alias="off", enabled=False),
                "not-a-dict",
                entry(alias="on"),
            ]
        }
        servers = load_servers_from_config(config, 15)
        self.assertEqual([s.key for s in servers], ["on"])

    def test_invalid_port_and_sid_fall_back(self) -> None:
        config = {"server_entries": [entry(query_port=70000, virtual_server_id=0)]}
        server = load_servers_from_config(config, 15)[0]
        self.assertEqual(server.query_port, DEFAULT_QUERY_PORT)
        self.assertEqual(server.virtual_server_id, DEFAULT_VIRTUAL_SERVER_ID)


class LoadSettingsTest(unittest.TestCase):
    def test_defaults(self) -> None:
        settings = load_settings({})
        self.assertIsNone(settings.target_group)
        self.assertIsNone(settings.target_umo)
        self.assertTrue(settings.enable_auto_monitor)
        self.assertTrue(settings.enable_status_push)
        self.assertFalse(settings.enable_startup_notify)
        self.assertTrue(settings.enable_join_leave_notify)
        self.assertEqual(settings.global_join_leave_interval, 15)
        self.assertEqual(settings.global_join_leave_debounce, 3.0)

    def test_join_leave_notify_switch(self) -> None:
        self.assertFalse(load_settings({"enable_join_leave_notify": False}).enable_join_leave_notify)
        self.assertFalse(load_settings({"enable_join_leave_notify": "false"}).enable_join_leave_notify)
        self.assertTrue(load_settings({"enable_join_leave_notify": True}).enable_join_leave_notify)

    def test_interval_minimum_and_negative_debounce(self) -> None:
        settings = load_settings(
            {"global_join_leave_interval": 1, "global_join_leave_debounce": -1}
        )
        self.assertEqual(settings.global_join_leave_interval, 5)
        self.assertEqual(settings.global_join_leave_debounce, 0.0)


if __name__ == "__main__":
    unittest.main()
