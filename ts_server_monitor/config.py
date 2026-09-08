"""配置加载与默认值处理。

仿 ``astrbot_plugin_minecraft_multi_monitor/minecraft_multi_monitor/config.py``。
"""

from __future__ import annotations

import re
from typing import Any

from astrbot.api import AstrBotConfig, logger

from .constants import (
    DEFAULT_JOIN_LEAVE_DEBOUNCE,
    DEFAULT_JOIN_LEAVE_INTERVAL,
    DEFAULT_QUERY_PORT,
    DEFAULT_VIRTUAL_SERVER_ID,
    MIN_JOIN_LEAVE_INTERVAL,
)
from .models import DisplayOptions, PluginSettings, ServerConfig


DISPLAY_DEFAULTS = DisplayOptions()


def safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return default


def safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_target_group(value: Any) -> str | None:
    """target_group 必须是纯数字的群号字符串。"""
    if value is None:
        return None
    text = str(value).strip()
    return text if text.isdigit() else None


def parse_umo(value: Any) -> str | None:
    """校验并规范化一个 UMO（Unified Message Origin）字符串。

    AstrBot 的 UMO 形如 ``<平台实例名>:<消息类型>:<会话ID>``，例如
    ``atri:GroupMessage:1092815819``。只做宽松校验：至少两段非空、以冒号分隔。
    详细的可达性校验交给 ``/推送目标`` 命令发送测试消息时完成。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    # 至少三段，且 平台前缀 / 消息类型 / 会话ID 三段都非空
    if len(parts) < 3 or any(not part.strip() for part in parts[:3]):
        return None
    return text


def slugify_server_key(text: str, fallback: str) -> str:
    """把显示名变成可作为 key 的 ASCII 串（失败时用 fallback）。"""
    normalized = re.sub(r"[^0-9a-zA-Z]+", "_", text.strip().lower()).strip("_")
    return normalized or fallback


def load_display_options(config: AstrBotConfig) -> DisplayOptions:
    raw = config.get("display_options", {})
    if not isinstance(raw, dict):
        logger.warning("配置项 display_options 不是对象，已使用默认展示设置。")
        raw = {}

    return DisplayOptions(
        show_server_name=safe_bool(
            raw.get("show_server_name"), DISPLAY_DEFAULTS.show_server_name
        ),
        show_server_address=safe_bool(
            raw.get("show_server_address"), DISPLAY_DEFAULTS.show_server_address
        ),
        show_server_platform=safe_bool(
            raw.get("show_server_platform"), DISPLAY_DEFAULTS.show_server_platform
        ),
        show_server_version=safe_bool(
            raw.get("show_server_version"), DISPLAY_DEFAULTS.show_server_version
        ),
        show_online_clients=safe_bool(
            raw.get("show_online_clients"), DISPLAY_DEFAULTS.show_online_clients
        ),
        show_channels=safe_bool(
            raw.get("show_channels"), DISPLAY_DEFAULTS.show_channels
        ),
        show_uptime=safe_bool(
            raw.get("show_uptime"), DISPLAY_DEFAULTS.show_uptime
        ),
        show_client_list=safe_bool(
            raw.get("show_client_list"), DISPLAY_DEFAULTS.show_client_list
        ),
        show_update_time=safe_bool(
            raw.get("show_update_time"), DISPLAY_DEFAULTS.show_update_time
        ),
    )


def load_settings(config: AstrBotConfig) -> PluginSettings:
    """从 AstrBot config 顶层字段构造 PluginSettings。"""
    global_join_leave_interval = safe_int(
        config.get("global_join_leave_interval", DEFAULT_JOIN_LEAVE_INTERVAL),
        DEFAULT_JOIN_LEAVE_INTERVAL,
    )
    if global_join_leave_interval < MIN_JOIN_LEAVE_INTERVAL:
        global_join_leave_interval = MIN_JOIN_LEAVE_INTERVAL

    global_join_leave_debounce = safe_float(
        config.get("global_join_leave_debounce", DEFAULT_JOIN_LEAVE_DEBOUNCE),
        DEFAULT_JOIN_LEAVE_DEBOUNCE,
    )
    if global_join_leave_debounce < 0:
        global_join_leave_debounce = 0.0

    return PluginSettings(
        target_group=parse_target_group(config.get("target_group")),
        target_umo=parse_umo(config.get("target_umo")),
        enable_auto_monitor=safe_bool(
            config.get("enable_auto_monitor", False), False
        ),
        global_join_leave_interval=global_join_leave_interval,
        global_join_leave_debounce=global_join_leave_debounce,
        # 上下线切换通知（默认开）
        enable_status_push=safe_bool(config.get("enable_status_push", True), True),
        # 监控启动通知（默认关，需要时在面板单独打开）
        enable_startup_notify=safe_bool(
            config.get("enable_startup_notify", False), False
        ),
        display_options=load_display_options(config),
    )


def resolve_join_leave_interval(value: Any, global_join_leave_interval: int) -> int:
    """单服务器 join_leave_interval 解析：≤0 → 全局值；强制 ≥ MIN_JOIN_LEAVE_INTERVAL。"""
    raw = safe_int(value, global_join_leave_interval)
    if raw <= 0:
        raw = global_join_leave_interval
    return max(MIN_JOIN_LEAVE_INTERVAL, raw)


def resolve_query_port(value: Any, global_query_port: int) -> int:
    """单服务器 query_port 解析：≤0 → 全局默认。"""
    raw = safe_int(value, 0)
    if raw <= 0:
        raw = global_query_port
    return raw


def load_servers_from_config(
    config: AstrBotConfig,
    global_join_leave_interval: int,
) -> list[ServerConfig]:
    """从 ``config['server_entries']`` 构造 ``ServerConfig`` 列表。

    - ``enabled=False`` 的条目被跳过；
    - 缺必填字段（alias/host/query_user/query_password）的条目被跳过并日志提示；
    - ``join_leave_interval`` ≤ 0 回退到 ``global_join_leave_interval``；
    - ``query_port`` ≤ 0 回退到全局默认 ``10011``；
    - alias 为空时，根据 ``server_name`` 自动 slugify 一个 key。
    """
    raw_servers = config.get("server_entries")
    if raw_servers is None:
        raw_servers = config.get("servers", [])  # 兼容旧版字段名
    if not isinstance(raw_servers, list):
        logger.warning("配置项 server_entries 不是列表，已忽略。")
        raw_servers = []

    servers: list[ServerConfig] = []
    for index, entry in enumerate(raw_servers, start=1):
        if not isinstance(entry, dict):
            logger.warning(f"server_entries 第 {index} 项不是对象，已跳过。")
            continue

        enabled = safe_bool(entry.get("enabled", True), True)
        if not enabled:
            continue

        raw_alias = str(entry.get("alias", "")).strip()
        raw_name = str(entry.get("display_name", "") or "").strip()
        server_name = raw_name or raw_alias or f"服务器{index}"

        host = str(entry.get("host", "")).strip()
        query_user = str(entry.get("query_user", "")).strip()
        query_password = str(entry.get("query_password", ""))

        if not host or not query_user or not query_password:
            logger.warning(
                f"server_entries 第 {index} 项 ({server_name}) 缺 host / query_user / "
                f"query_password，已跳过。"
            )
            continue

        query_port = resolve_query_port(entry.get("query_port"), DEFAULT_QUERY_PORT)
        virtual_server_id = safe_int(
            entry.get("virtual_server_id"), DEFAULT_VIRTUAL_SERVER_ID
        )
        join_leave_interval = resolve_join_leave_interval(
            entry.get("join_leave_interval"), global_join_leave_interval
        )

        key = raw_alias or slugify_server_key(server_name, f"server_{index}")

        servers.append(
            ServerConfig(
                key=key,
                enabled=enabled,
                server_name=server_name,
                host=host,
                query_port=query_port,
                query_user=query_user,
                query_password=query_password,
                virtual_server_id=virtual_server_id,
                join_leave_interval=join_leave_interval,
            )
        )

    return servers
