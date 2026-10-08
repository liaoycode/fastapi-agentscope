from fastapi import FastAPI

from app.core.bootstrap.abs_boot_plugin import AppPlugin
from app.core.config.env_config import settings
from app.domain.model.common import Base
from app.infrastructure.datasource.database import init_db, close_db

# 触发各 Model 类的注册（Base.metadata 需要看见所有子类才能 create_all）
from app.domain.model import chat_session_state  # noqa: F401
from app.domain.model import sys_user  # noqa: F401
from app.domain.model import workspace_snapshot  # noqa: F401


"""数据库连接池初始化 + 启动时建表"""


class DatabasePlugin(AppPlugin):
    @classmethod
    def is_enabled(cls) -> bool:
        return settings.db_enable if settings.db_enable is not None else True

    async def on_startup(self, app: FastAPI):
        init_db()
        # 用 run_sync 调同步的 create_all（async engine 必备写法）
        from app.infrastructure.datasource.database import _engine  # noqa: WPS437
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def on_shutdown(self, app: FastAPI):
        await close_db()
