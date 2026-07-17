from fastapi import FastAPI

from app.core.bootstrap.abs_boot_plugin import AppPlugin
from app.infrastructure.redis.redis_client import init_pool, close_pool


class RedisPlugin(AppPlugin):
    async def on_startup(self, app: FastAPI):
        init_pool()

    async def on_shutdown(self, app: FastAPI):
        await close_pool()