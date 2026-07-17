# app/infrastructure/cache/redis_client.py
from typing import Any, Optional
from redis.asyncio import Redis, ConnectionPool
from app.core.config.env_config import settings

_pool: ConnectionPool = None


def init_pool():
    global _pool
    if _pool is not None:
        return
    _pool = ConnectionPool.from_url(
        settings.redis_url,
        max_connections=settings.redis_max_connections,
        decode_responses=True,
    )


async def close_pool():
    global _pool
    if _pool:
        await _pool.disconnect()


def _get_redis() -> Redis:
    if _pool is None:
        raise RuntimeError("Redis未初始化，请检查 RedisPlugin 是否正常启动")
    return Redis(connection_pool=_pool)


class RedisClient:
    # ========== String ==========
    async def get(self, key: str) -> Optional[str]:
        async with _get_redis() as r:
            return await r.get(key)

    async def set(self, key: str, value: Any, nx: bool=None, ex: int = None) -> bool:
        async with _get_redis() as r:
            return await r.set(key, value, nx=nx, ex=ex)

    async def delete(self, *keys: str) -> int:
        async with _get_redis() as r:
            return await r.delete(*keys)

    async def exists(self, key: str) -> bool:
        async with _get_redis() as r:
            return await r.exists(key) > 0

    async def expire(self, key: str, seconds: int) -> bool:
        async with _get_redis() as r:
            return await r.expire(key, seconds)

    async def ttl(self, key: str) -> int:
        async with _get_redis() as r:
            return await r.ttl(key)

    # ========== Hash ==========
    async def hget(self, name: str, key: str) -> Optional[str]:
        async with _get_redis() as r:
            return await r.hget(name, key)

    async def hset(self, name: str, mapping: dict) -> int:
        async with _get_redis() as r:
            return await r.hset(name, mapping=mapping)

    async def hgetall(self, name: str) -> dict:
        async with _get_redis() as r:
            return await r.hgetall(name)

    async def hdel(self, name: str, *keys: str) -> int:
        async with _get_redis() as r:
            return await r.hdel(name, *keys)

    # ========== List ==========
    async def lpush(self, key: str, *values: Any) -> int:
        async with _get_redis() as r:
            return await r.lpush(key, *values)

    async def rpop(self, key: str) -> Optional[str]:
        async with _get_redis() as r:
            return await r.rpop(key)

    async def lrange(self, key: str, start: int, end: int) -> list:
        async with _get_redis() as r:
            return await r.lrange(key, start, end)

    # ========== Set ==========
    async def sadd(self, key: str, *values: Any) -> int:
        async with _get_redis() as r:
            return await r.sadd(key, *values)

    async def smembers(self, key: str) -> set:
        async with _get_redis() as r:
            return await r.smembers(key)

    async def sismember(self, key: str, value: Any) -> bool:
        async with _get_redis() as r:
            return await r.sismember(key, value)

    # ========== 原子操作 ==========
    async def incr(self, key: str, amount: int = 1) -> int:
        async with _get_redis() as r:
            return await r.incr(key, amount)

    async def setnx(self, key: str, value: Any) -> bool:
        """不存在才设置，常用于分布式锁"""
        async with _get_redis() as r:
            return await r.setnx(key, value)


redis_client = RedisClient()