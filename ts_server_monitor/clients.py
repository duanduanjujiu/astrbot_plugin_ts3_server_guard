"""TS3 ServerQuery 异步客户端。

仿 ``astrbot_plugin_minecraft_multi_monitor/minecraft_multi_monitor/clients.py``
的风格：在异步函数内通过 ``asyncio.to_thread`` 把同步的 ``ts3`` 库调用
放进线程池，避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import asdict
from datetime import datetime
from typing import Any

from astrbot.api import logger

from .constants import (
    CLIENT_TYPE_REGULAR,
    DEFAULT_NETWORK_TIMEOUT,
)
from .models import ChannelInfo, ClientInfo, ServerConfig, ServerSnapshot


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _is_ip_literal(value: str) -> bool:
    if not value:
        return False
    try:
        socket.inet_pton(socket.AF_INET, value)
        return True
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, value)
        return True
    except OSError:
        pass
    return False


# -------- 底层同步 ts3 客户端（线程池内执行） --------


class _SyncTS3Client:
    """同步 TS3 ServerQuery 客户端（仅在 ``asyncio.to_thread`` 内调用）。"""

    def __init__(
        self,
        host: str,
        query_port: int,
        query_user: str,
        query_password: str,
        virtual_server_id: int = 1,
        timeout: float = DEFAULT_NETWORK_TIMEOUT,
    ) -> None:
        self.host = host
        self.query_port = query_port
        self.query_user = query_user
        self.query_password = query_password
        self.virtual_server_id = virtual_server_id
        self.timeout = timeout
        self._connection: Any = None

    @property
    def is_connected(self) -> bool:
        return self._connection is not None

    def connect(self) -> None:
        import ts3  # 局部 import，便于在缺包时优雅降级

        self._connection = ts3.query.TS3Connection()
        self._connection.open(self.host, self.query_port, timeout=self.timeout)
        # 连接建立后强制 socket 超时，防止后续 login/use/send 在防洪或网络黑洞下
        # 永久阻塞线程（wait_for 只能救回协程，救不回泄漏的线程）。
        try:
            sock = getattr(self._connection, "conn", None)
            if sock is not None:
                sock.settimeout(self.timeout)
        except Exception:
            pass
        self._connection.login(
            client_login_name=self.query_user,
            client_login_password=self.query_password,
        )
        self._connection.use(sid=self.virtual_server_id)

    def quit(self) -> None:
        if self._connection is not None:
            try:
                self._connection.quit()
            except Exception:
                pass
            finally:
                self._connection = None

    def fetch_server_info(self) -> dict[str, str] | None:
        if self._connection is None:
            return None
        resp = self._connection.send("serverinfo", timeout=self.timeout)
        if not resp.parsed:
            return None
        return resp.parsed[0]

    def fetch_client_list(self) -> list[ClientInfo]:
        if self._connection is None:
            return []
        resp = self._connection.send("clientlist", timeout=self.timeout)
        clients: list[ClientInfo] = []
        for entry in resp.parsed or []:
            try:
                ctype = int(entry.get("client_type", 0))
            except (TypeError, ValueError):
                ctype = CLIENT_TYPE_REGULAR
            if ctype != CLIENT_TYPE_REGULAR:
                continue
            try:
                clid = int(entry.get("clid", 0))
                dbid = int(entry.get("client_database_id", 0))
                cid = int(entry.get("cid", 0))
            except (TypeError, ValueError):
                continue
            clients.append(
                ClientInfo(
                    clid=clid,
                    nickname=str(entry.get("client_nickname", "Unknown")),
                    database_id=dbid,
                    cid=cid,
                    client_type=ctype,
                )
            )
        return clients

    def fetch_channel_list(self) -> list[ChannelInfo]:
        if self._connection is None:
            return []
        resp = self._connection.send("channellist", timeout=self.timeout)
        channels: list[ChannelInfo] = []
        for entry in resp.parsed or []:
            try:
                cid = int(entry.get("cid", 0))
                total = int(entry.get("total_clients", 0))
            except (TypeError, ValueError):
                continue
            channels.append(
                ChannelInfo(
                    cid=cid,
                    name=str(entry.get("channel_name", "Unknown")),
                    total_clients=total,
                )
            )
        return channels


# -------- 异步客户端工厂 --------


class LocalStatusClient:
    """异步 TS3 ServerQuery 客户端（与 minecraft 的 LocalStatusClient 对应）。"""

    def __init__(self, timeout: float = DEFAULT_NETWORK_TIMEOUT) -> None:
        self.timeout = timeout

    async def fetch_status(
        self, server: ServerConfig, source: str
    ) -> ServerSnapshot | None:
        """拉取一台 TS3 服务器的状态。失败返回 None。

        所有同步 ts3 调用通过 ``asyncio.to_thread`` 放进线程池，
        避免阻塞事件循环。
        """
        loop = asyncio.get_running_loop()

        def _do() -> ServerSnapshot:
            client = _SyncTS3Client(
                host=server.host,
                query_port=server.query_port,
                query_user=server.query_user,
                query_password=server.query_password,
                virtual_server_id=server.virtual_server_id,
                timeout=self.timeout,
            )
            try:
                client.connect()
                info = client.fetch_server_info() or {}
                clients = client.fetch_client_list()
                channels = client.fetch_channel_list()
            finally:
                client.quit()

            try:
                max_clients = int(info.get("virtualserver_maxclients", 0) or 0)
            except (TypeError, ValueError):
                max_clients = 0
            try:
                channels_online = int(
                    info.get("virtualserver_channelsonline", 0) or 0
                )
            except (TypeError, ValueError):
                channels_online = 0
            try:
                uptime = int(info.get("virtualserver_uptime", 0) or 0)
            except (TypeError, ValueError):
                uptime = 0

            return ServerSnapshot(
                key=server.key,
                name=server.server_name,
                host=server.host,
                query_port=server.query_port,
                status="online",
                platform=str(info.get("virtualserver_platform", "Unknown")),
                version=str(info.get("virtualserver_version", "Unknown")),
                clients_online=len(clients),  # 过滤后的真实数量
                max_clients=max_clients,
                channels_online=channels_online,
                uptime=uptime,
                clients=clients,
                channels=channels,
                update_time=_now_text(),
            )

        # 关键：必须用 wait_for 包裹 to_thread，否则底层 ts3 同步调用若永久阻塞
        # （网络黑洞 / TS3 防洪挂起连接），await 永不返回，monitor_loop 会被整个卡死。
        # 超时后返回 None，由上层构造 offline 快照继续下一轮轮询。
        timeout = self.timeout + 5  # 在连接层 timeout 基础上多留 5s 余量
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_do), timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[{source}] [{server.server_name}] TS3 查询超时 "
                f"({timeout:.0f}s)，已跳过本轮"
            )
            return None
        except Exception as exc:
            logger.warning(
                f"[{source}] [{server.server_name}] TS3 查询失败: {exc}"
            )
            return None


def build_status_client() -> LocalStatusClient:
    """工厂函数：构造异步状态查询客户端。"""
    return LocalStatusClient()


__all__ = [
    "LocalStatusClient",
    "build_status_client",
    "ClientInfo",
    "ChannelInfo",
    "ServerSnapshot",
    # 同时暴露 asdict 给需要序列化 snapshot 的代码使用
    "asdict",
]
