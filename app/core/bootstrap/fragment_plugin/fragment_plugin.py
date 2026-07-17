from fastapi import FastAPI

from app.core.bootstrap.abs_boot_plugin import AppPlugin
from app.core.common.logger import setup_logger
from app.core.exceptions.exception_handler import setup_exception_handlers


class FragmentPlugin(AppPlugin):
    async def on_startup(self, app: FastAPI):
        # 日志
        # setup_logger()
        pass


    async def on_shutdown(self, app: FastAPI):
        pass