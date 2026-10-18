"""state.py 运行时推送目标持久化测试（重定向到临时目录）。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _support  # noqa: F401
from astrbot_plugin_ts3_server_guard.ts_server_monitor import state as state_module


class RuntimeTargetStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "relay_state.json"
        self._orig_state_path = state_module._state_path
        state_module._state_path = lambda: self.path

    def tearDown(self) -> None:
        state_module._state_path = self._orig_state_path

    def test_roundtrip_umo(self) -> None:
        state_module.save_runtime_target(umo="robot:GroupMessage:123")
        target = state_module.load_runtime_target()
        self.assertEqual(target.umo, "robot:GroupMessage:123")
        self.assertIsNone(target.group_id)

    def test_roundtrip_group(self) -> None:
        state_module.save_runtime_target(group_id="456")
        target = state_module.load_runtime_target()
        self.assertEqual(target.group_id, "456")
        self.assertIsNone(target.umo)

    def test_clear(self) -> None:
        state_module.save_runtime_target(umo="robot:GroupMessage:123")
        state_module.clear_runtime_target()
        target = state_module.load_runtime_target()
        self.assertTrue(target.is_empty)

    def test_malformed_json_returns_empty(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        self.assertTrue(state_module.load_runtime_target().is_empty)

    def test_missing_file_returns_empty(self) -> None:
        self.assertTrue(state_module.load_runtime_target().is_empty)

    def test_both_fields_in_file_are_loaded(self) -> None:
        """防御性文档：历史上可能同时写入两个字段，加载时需要能读到。"""
        self.path.write_text(
            json.dumps({"umo": "robot:GroupMessage:123", "group_id": "456"}),
            encoding="utf-8",
        )
        target = state_module.load_runtime_target()
        self.assertEqual(target.umo, "robot:GroupMessage:123")
        self.assertEqual(target.group_id, "456")


if __name__ == "__main__":
    unittest.main()
