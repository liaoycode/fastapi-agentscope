
from fastapi import FastAPI

from app.core.bootstrap.abs_boot_plugin import AppPlugin
from app.core.config.env_config import settings
from app.infrastructure.datasource.database import init_db, close_db

"""数据库连接池初始化"""

class DatabasePlugin(AppPlugin):
    @classmethod
    def is_enabled(cls) -> bool:
        return settings.db_enable if settings.db_enable is not None else True

    async def on_startup(self, app: FastAPI):
        init_db()

    async def on_shutdown(self, app: FastAPI):
        await close_db()