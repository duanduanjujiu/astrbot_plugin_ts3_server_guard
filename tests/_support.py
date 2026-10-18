"""测试引导模块：把插件父目录加入 sys.path，并安装 AstrBot stub。

插件已改为相对导入（``from .ts_server_monitor...``），因此测试必须把
**插件目录的父目录**加入 sys.path，再以 ``astrbot_plugin_ts3_server_guard``
包的形式导入，才能和 AstrBot 的加载方式（``data.plugins.<dir>.main``）一致。
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TESTS_DIR.parent
_PLUGIN_PARENT = _REPO_ROOT.parent

for _path in (str(_TESTS_DIR), str(_PLUGIN_PARENT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from astrbot_stub import install  # noqa: E402  (需要先修正 sys.path)

install()

PACKAGE = "astrbot_plugin_ts3_server_guard"
