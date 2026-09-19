"""Redis 令牌桶限流（挂账销项）。

场景：beat 与手动 CLI 可能并发触发采集，DB 间隔守卫读 fetch_log 有竞态窗口
（两个进程同时看到"最近无成功记录"然后双双抓取）。Redis 令牌桶作为第二层
跨进程限流；Redis 不可用时返回 None，调用方沿用 DB 间隔守卫（原有行为不变）。

桶参数：容量 = rate_per_window（默认 1），速率 = rate_per_window / window_s，
即每个采集窗口补 1 个令牌；连续采集自然被摊平。
"""

from __future__ import annotations

import time

from app.core.redis_utils import get_redis

# 原子令牌桶：HMGET 状态 → 按流逝时间补币 → 尝试取币 → 回写 + EXPIRE
TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil or ts == nil then
    tokens = rate
    ts = now
end
local elapsed = now - ts
if elapsed > 0 then
    tokens = math.min(rate, tokens + elapsed / window * rate)
end
local allowed = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, math.ceil(window * 2))
return allowed
"""


def acquire(key: str, rate_per_window: int = 1, window_s: int = 360) -> bool | None:
    """尝试取一个令牌。

    返回：
      True  — 允许（已扣减令牌）
      False — 被限流
      None  — Redis 不可用，调用方应回退 DB 守卫
    """
    client = get_redis()
    if client is None:
        return None
    try:
        result = client.eval(
            TOKEN_BUCKET_LUA, 1, f"ratelimit:{key}", rate_per_window, window_s, time.time()
        )
        return bool(int(result))
    except Exception:
        return None
