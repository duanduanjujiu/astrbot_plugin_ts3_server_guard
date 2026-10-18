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


def clean_text(value: Any) -> str:
    """把配置值规整成字符串。

    JSON 的 null 会变成 Python 的 None，直接 ``str()`` 会得到 "None" 并骗过必填校验；
    这里统一：None → ""，字符串去首尾空白，其他标量转字符串，列表/字典等非法值 → ""。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()


def clean_secret(value: Any) -> str:
    """密码类字段：不做 strip（密码首尾空格可能是有效字符）。"""
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    return value if isinstance(value, str) else str(value)


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
    ``atri:GroupMessage:1092815819``。只做宽松校验：必须是字符串、至少三段、
    且各段都非空（拒绝内部空段）。详细的可达性校验交给
    ``/ts_push_target`` 命令发送测试消息时完成。
    """
    if not isinstance(value, str):
        # 列表/字典等会 str() 成 "['a:b:c']" 之类的假 UMO，直接判非法。
        return None
    text = value.strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) < 3:
        return None
    if any(not part.strip() for part in parts):
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
            config.get("enable_auto_monitor"), True
        ),
        global_join_leave_interval=global_join_leave_interval,
        global_join_leave_debounce=global_join_leave_debounce,
        # 上下线切换通知（默认开）
        enable_status_push=safe_bool(config.get("enable_status_push", True), True),
        # 监控启动通知（默认关，需要时在面板单独打开）
        enable_startup_notify=safe_bool(
            config.get("enable_startup_notify", False), False
        ),
        # 用户进出通知（默认开）；关闭后仍维护基线，只是不推送消息
        enable_join_leave_notify=safe_bool(
            config.get("enable_join_leave_notify", True), True
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
    """单服务器 query_port 解析：≤0/缺省 → 全局默认；非法 → 默认端口并告警。"""
    fallback = safe_int(global_query_port, DEFAULT_QUERY_PORT)
    if not 1 <= fallback <= 65535:
        fallback = DEFAULT_QUERY_PORT

    text = clean_text(value)
    if not text:
        return fallback

    raw = safe_int(text, 0)
    if 1 <= raw <= 65535:
        return raw
    if raw <= 0:
        return fallback
    logger.warning(f"query_port={text} 超出合法范围 1-65535，已回退到 {fallback}。")
    return fallback


def resolve_virtual_server_id(value: Any) -> int:
    """virtual_server_id 解析：缺省/空串用默认值；非 ≥1 的整数回退并告警。"""
    text = clean_text(value)
    if not text:
        return DEFAULT_VIRTUAL_SERVER_ID

    raw = safe_int(text, 0)
    if raw < 1:
        logger.warning(
            f"virtual_server_id={text} 非法（必须是 ≥1 的整数），"
            f"已回退到默认值 {DEFAULT_VIRTUAL_SERVER_ID}。"
        )
        return DEFAULT_VIRTUAL_SERVER_ID
    return raw


def load_servers_from_config(
    config: AstrBotConfig,
    global_join_leave_interval: int,
) -> list[ServerConfig]:
    """从 ``config['server_entries']`` 构造 ``ServerConfig`` 列表。

    - ``enabled=False`` 的条目被跳过；
    - 缺必填字段（host/query_user/query_password）的条目被跳过并日志提示；
    - ``join_leave_interval`` ≤ 0 回退到 ``global_join_leave_interval``；
    - ``query_port`` 非法或 ≤ 0 回退到全局默认 ``10011``；
    - ``virtual_server_id`` 非 ≥1 的整数时回退到默认值；
    - alias 为空时，根据 ``server_name`` 自动 slugify 一个 key；
    - key 全局唯一：重复条目保留第一个，后续追加 ``_2``、``_3`` 后缀（不丢服务器）。
    """
    raw_servers = config.get("server_entries", [])
    if not isinstance(raw_servers, list):
        logger.warning("配置项 server_entries 不是列表，已忽略。")
        raw_servers = []

    servers: list[ServerConfig] = []
    used_keys: set[str] = set()
    for index, entry in enumerate(raw_servers, start=1):
        if not isinstance(entry, dict):
            logger.warning(f"server_entries 第 {index} 项不是对象，已跳过。")
            continue

        enabled = safe_bool(entry.get("enabled", True), True)
        if not enabled:
            continue

        raw_alias = clean_text(entry.get("alias"))
        raw_name = clean_text(entry.get("display_name"))
        server_name = raw_name or raw_alias or f"服务器{index}"

        host = clean_text(entry.get("host"))
        query_user = clean_text(entry.get("query_user"))
        query_password = clean_secret(entry.get("query_password"))

        if not host or not query_user or not query_password:
            logger.warning(
                f"server_entries 第 {index} 项 ({server_name}) 缺 host / query_user / "
                f"query_password，已跳过。"
            )
            continue

        query_port = resolve_query_port(entry.get("query_port"), DEFAULT_QUERY_PORT)
        virtual_server_id = resolve_virtual_server_id(entry.get("virtual_server_id"))
        join_leave_interval = resolve_join_leave_interval(
            entry.get("join_leave_interval"), global_join_leave_interval
        )

        base_key = raw_alias or slugify_server_key(server_name, f"server_{index}")
        key = base_key
        suffix = 2
        while key in used_keys:
            key = f"{base_key}_{suffix}"
            suffix += 1
        if key != base_key:
            logger.warning(
                f"server_entries 第 {index} 项 ({server_name}) 的 key「{base_key}」"
                f"与其他服务器重复，已改为「{key}」；"
                f"请修改别名（alias）以免状态互相覆盖。"
            )
        used_keys.add(key)

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
