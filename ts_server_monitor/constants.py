"""默认值与时长格式化。"""

from __future__ import annotations

# ServerQuery 默认端口
DEFAULT_QUERY_PORT = 10011

# 虚拟服务器默认 ID
DEFAULT_VIRTUAL_SERVER_ID = 1

# 用户进出检测间隔（秒）
DEFAULT_JOIN_LEAVE_INTERVAL = 15

# 用户进出检测间隔最小值（秒）
# 注意：太短会触发 TS3 防洪。务必把 AstrBot IP 加进 query_ip_allowlist.txt。
MIN_JOIN_LEAVE_INTERVAL = 5

# 用户进出防抖时间（秒）
# 在两次轮询之间某用户暂时消失又重新出现时，丢弃这次抖动以避免误报。
DEFAULT_JOIN_LEAVE_DEBOUNCE = 3

# TS3Client 网络操作超时（秒）
DEFAULT_NETWORK_TIMEOUT = 30.0

# ServerQuery 客户端类型枚举（来自 ts3 协议）
CLIENT_TYPE_REGULAR = 0
CLIENT_TYPE_SERVERQUERY = 1

# 消息分隔线（推送样式：去 emoji，头尾加 ━━ 分隔线以区别于 minecraft 插件）
SEPARATOR = "━━━━━━━━━━━━━━━━━━"

# ---------------------------------------------------------------------------
# 稳定性（v3.0.0 新增）
# ---------------------------------------------------------------------------

# 上下线状态切换前需要的“连续同向采样”次数。
#
# 背景：TS3 防洪（error id 524）、瞬时断网等都会让某一次拉取失败。
# 若不确认次数直接翻转，会把“单次抖动”误报成服务器 已离线/已上线 骚扰群聊。
# 拉取样本来自 用户进出轮询 + 状态轮询 的合并结果（见 main.monitor_loop），
# 正常节奏下约每 15s 一个样本，因此：
#   - 离线需连续 3 个失败样本（约 45s 持续失联）才判定离线；
#   - 恢复在线需连续 2 个成功样本，避免刚恢复时的抖动又被打回离线。
DEFAULT_STATUS_CONFIRM_ONLINE = 2
DEFAULT_STATUS_CONFIRM_OFFLINE = 3

# 连续拉取失败达到该次数后，用户进出轮询降频，降低对“已失联 / 被防洪”
# 服务器的高频连接压力（连接数、登录数、日志量）。
DEGRADE_AFTER_FAILURES = 3

# 已确认离线服务器的恢复探测间隔（秒）。比 DEGRADED_POLL_INTERVAL 短，
# 避免恢复在线后要等满 60s 才被发现。
DEGRADED_OFFLINE_PROBE_INTERVAL = 30.0

# 在线的失联服务器轮询间隔（秒）：保持较低负载，等待上下线滞回判定。
DEGRADED_POLL_INTERVAL = 60.0

# 单次通知发送失败时的重试次数（QQ 群号路径）
SEND_MAX_ATTEMPTS = 3

# 等待可用平台适配器出现的超时（秒）
PLATFORM_WAIT_TIMEOUT = 12.0

# ---------------------------------------------------------------------------
# 通知发送队列（v3.3.0 新增）
# ---------------------------------------------------------------------------

# 通知队列上限；满时丢弃最早的一条，避免无限堆积。
NOTIFY_QUEUE_MAX = 50

# 单次通知发送超时（秒）；超时视为本次尝试失败并进入重试。
NOTIFY_SEND_TIMEOUT = 30.0

# 单条通知的最大发送尝试次数（失败后按 1s、2s... 指数退避）。
NOTIFY_MAX_ATTEMPTS = 3

# ---------------------------------------------------------------------------
# 查询缓存与限频（batch-2 新增）
# ---------------------------------------------------------------------------

# /ts_query 复用监控快照的最小缓存时长（秒）；实际 TTL 还会参考轮询间隔。
QUERY_CACHE_TTL_MIN = 5.0

# /ts_query 同一会话的最短查询间隔（秒），避免刷屏与重复建立查询连接。
QUERY_COOLDOWN_SECONDS = 5.0


def format_duration(seconds: int) -> str:
    """将秒数格式化为 "X天Y小时Z分钟" 形式。

    用于状态通知里展示 uptime 等信息。
    """
    if seconds <= 0:
        return "0分钟"

    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60

    parts: list[str] = []
    if days > 0:
        parts.append(f"{days}天")
    if hours > 0:
        parts.append(f"{hours}小时")
    if minutes > 0 or not parts:
        parts.append(f"{minutes}分钟")

    return "".join(parts)
