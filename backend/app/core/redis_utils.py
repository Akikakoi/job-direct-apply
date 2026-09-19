"""Redis 懒加载客户端（挂账销项：限流/缓存的统一入口）。

约定：
- settings.redis_url 为空 → 返回 None，调用方走降级路径（DB 间隔守卫 / 无缓存）；
- 连接失败/超时 → 返回 None 并静默降级，绝不因 Redis 抖动拖垮采集主流程；
- decode_responses=True：所有命令收发 str，调用方直接 json.loads；
- lru_cache 单例：进程内复用连接池；测试 monkeypatch 本函数即可隔离。
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import settings


@lru_cache(maxsize=1)
def get_redis():
    """返回可用的 redis 客户端；不可用一律返回 None（调用方降级）。"""
    if not settings.redis_url:
        return None
    try:
        import redis  # celery 依赖自带 redis-py，无需额外安装

        client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=True,
        )
        client.ping()
        return client
    except Exception:
        return None
