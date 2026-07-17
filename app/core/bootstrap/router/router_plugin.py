# 注册路由
import importlib
import pkgutil

from fastapi import FastAPI

from app.core.bootstrap.abs_boot_plugin import AppPlugin

"""router注册"""

class RouterPlugin(AppPlugin):
    async def on_startup(self, app: FastAPI):
        _register_routers(app, "app.router")

    async def on_shutdown(self, app: FastAPI): ...


def _register_routers(app: FastAPI, package_name: str):
    """自动包含包中的所有路由器"""
    package = importlib.import_module(package_name)

    for _, name, is_pkg in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
        if not is_pkg:
            module = importlib.import_module(name)
            if hasattr(module, "router"):
                app.include_router(module.router)