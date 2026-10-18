"""clients.py 基础行为测试（纯辅助函数与小工具，不依赖网络与第三方包）。"""

from __future__ import annotations

import asyncio
import inspect
import unittest

import _support  # noqa: F401
from astrbot_plugin_ts3_server_guard.ts_server_monitor import clients as clients_module
from astrbot_plugin_ts3_server_guard.ts_server_monitor.clients import (
    LocalStatusClient,
    _build_command,
    _describe_error,
    _escape,
    _parse_client_list,
    _parse_item,
    _parse_records,
    _ProtocolError,
    _QueryError,
    _to_int,
    _unescape,
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

    def test_escape_all_special_characters(self) -> None:
        text = "a b|c\\d/e\n\r\t\v\f\a\b"
        self.assertEqual(
            _escape(text),
            "a\\sb\\pc\\\\d\\/e\\n\\r\\t\\v\\f\\a\\b",
        )

    def test_escape_unescape_roundtrip(self) -> None:
        for text in (
            "Alice Smith",
            "Bob|Dev",
            "back\\slash",
            "path/to",
            "line\nbreak",
            "空 格|服务",
            "",
        ):
            self.assertEqual(_unescape(_escape(text)), text)

    def test_unescape_known_sequences(self) -> None:
        self.assertEqual(_unescape(r"\s"), " ")
        self.assertEqual(_unescape(r"\p"), "|")
        self.assertEqual(_unescape(r"\/"), "/")
        self.assertEqual(_unescape(r"\\"), "\\")
        self.assertEqual(_unescape(r"a\sb"), "a b")

    def test_unescape_unknown_sequence_keeps_character(self) -> None:
        self.assertEqual(_unescape(r"\|"), "|")
        self.assertEqual(_unescape("plain"), "plain")
        self.assertEqual(_unescape("trailing\\"), "trailing\\")

    def test_build_command(self) -> None:
        self.assertEqual(_build_command("clientlist"), b"clientlist\n\r")
        self.assertEqual(
            _build_command(
                "login",
                {
                    "client_login_name": "a b",
                    "client_login_password": "p|w",
                },
            ),
            b"login client_login_name=a\\sb client_login_password=p\\pw\n\r",
        )
        self.assertEqual(
            _build_command("use", {"sid": 7}), b"use sid=7\n\r"
        )

    def test_parse_item(self) -> None:
        self.assertEqual(_parse_item("a=1 b= c=x=y"), {
            "a": "1",
            "b": "",
            "c": "x=y",
        })
        self.assertEqual(
            _parse_item(r"client_nickname=Alice\sSmith client_type=0"),
            {"client_nickname": "Alice Smith", "client_type": "0"},
        )

    def test_parse_records_multi_line_and_empty_items(self) -> None:
        records = _parse_records(
            ["clid=1 a=b|clid=2 c=d", "clid=3 e=f"]
        )
        self.assertEqual(
            records,
            [
                {"clid": "1", "a": "b"},
                {"clid": "2", "c": "d"},
                {"clid": "3", "e": "f"},
            ],
        )
        self.assertEqual(_parse_records([]), [])
        self.assertEqual(_parse_records(["", "|"]), [])

    def test_parse_client_list_filters_and_unescapes(self) -> None:
        records = [
            _parse_item(
                "clid=2 cid=1 client_database_id=10 "
                r"client_nickname=Alice\sSmith client_type=0"
            ),
            _parse_item(
                "clid=3 cid=1 client_database_id=11 "
                r"client_nickname=Bob\pDev client_type=0"
            ),
            _parse_item(
                "clid=4 cid=1 client_database_id=12 "
                "client_nickname=Query client_type=1"
            ),
        ]
        clients = _parse_client_list(records)
        self.assertEqual([c.clid for c in clients], [2, 3])
        self.assertEqual([c.nickname for c in clients], ["Alice Smith", "Bob|Dev"])
        self.assertEqual([c.database_id for c in clients], [10, 11])

    def test_parse_client_list_skips_malformed_rows(self) -> None:
        # 缺少 clid 的记录现在是协议错误；这里只覆盖"有 clid 但字段非法"的容错。
        clients = _parse_client_list(
            [
                {"client_type": "0", "clid": "bad"},
                {"client_type": "0", "clid": "5", "client_database_id": "x"},
            ]
        )
        self.assertEqual(clients, [])

    def test_parse_client_list_rejects_empty_response(self) -> None:
        # 真实 TS3 的 clientlist 必含本查询连接，空响应说明协议损坏。
        with self.assertRaises(_ProtocolError):
            _parse_client_list([])

    def test_parse_client_list_rejects_partially_misaligned_records(self) -> None:
        valid = {"clid": "2", "cid": "1", "client_type": "0"}
        serverinfo = {"virtualserver_name": "x", "virtualserver_port": "9987"}
        with self.assertRaises(_ProtocolError):
            _parse_client_list([valid, serverinfo])

    def test_protocol_error_retryable_flag(self) -> None:
        self.assertTrue(_ProtocolError("misaligned").retryable)
        self.assertFalse(_ProtocolError("bad error line", retryable=False).retryable)

    def test_parse_client_list_rejects_misaligned_response(self) -> None:
        with self.assertRaises(_ProtocolError):
            _parse_client_list(
                [{"virtualserver_name": "x", "virtualserver_port": "9987"}]
            )

    def test_query_error_message_and_hints(self) -> None:
        error = _QueryError(520, "invalid loginname or password")
        self.assertEqual(error.error_id, 520)
        self.assertEqual(error.message, "invalid loginname or password")
        self.assertIn("error id 520", _describe_error(error))
        self.assertIn("认证失败", _describe_error(error))
        self.assertIn("防洪", _describe_error(_QueryError(524, "flood")))
        self.assertIn(
            "virtual_server_id 无效", _describe_error(_QueryError(768, "bad sid"))
        )
        self.assertIn(
            "b_virtualserver_client_list",
            _describe_error(_QueryError(2568, "no permission")),
        )
        self.assertIn(
            "所需权限", _describe_error(_QueryError(2568, "no permission"))
        )
        unknown = _describe_error(_QueryError(999, "boom"))
        self.assertIn("error id 999", unknown)
        self.assertNotIn("（", unknown)

    def test_describe_error_categories(self) -> None:
        self.assertIn("超时", _describe_error(TimeoutError("slow")))
        self.assertIn("协议异常", _describe_error(_ProtocolError("bad line")))
        self.assertIn(
            "网络错误", _describe_error(ConnectionResetError("reset"))
        )

    def test_describe_error_for_generic(self) -> None:
        self.assertIn("RuntimeError", _describe_error(RuntimeError("boom")))

    def test_module_has_no_third_party_dependency_or_log_suppression(self) -> None:
        source = inspect.getsource(clients_module)
        self.assertNotIn("import ts3", source)
        self.assertNotIn("telnetlib", source)
        self.assertNotIn("logging.getLogger", source)
        self.assertNotIn("ThreadPoolExecutor", source)
        self.assertNotIn("import threading", source)

    def test_snapshot_has_roster_ok_voice_port_and_no_channels(self) -> None:
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
