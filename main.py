# main.py
import importlib
import inspect
import pkgutil
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.bootstrap.abs_boot_plugin import AppPlugin
from app.core.common.logger import getLogger, setup_logger
from app.core.config.env_config import settings
import app.core.bootstrap as bootstrap_pkg
from app.core.exceptions.exception_handler import setup_exception_handlers


setup_logger()
logger = getLogger()

def load_plugins() -> list[AppPlugin]:
    plugins = []
    for _, module_name, _ in pkgutil.walk_packages(  # iter_modules → walk_packages
        path=bootstrap_pkg.__path__,
        prefix=bootstrap_pkg.__name__ + ".",
    ):
        module = importlib.import_module(module_name)
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, AppPlugin) and cls is not AppPlugin:
                if not cls.is_enabled():
                    logger.info(f"plugin {name} is disabled, skip loading")
                    continue
                logger.info(f"plugin {name} is enabled, loading")
                plugins.append(cls())
    return plugins

def create_app() -> FastAPI:
    plugins = load_plugins()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for plugin in plugins:
            await plugin.on_startup(app)
        yield
        for plugin in reversed(plugins):
            await plugin.on_shutdown(app)

    app = FastAPI(
        title=settings.app_name,
        description="统一报告",
        lifespan=lifespan
    )

    setup_exception_handlers(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # app.mount("/static", StaticFiles(directory="app/static"), name="static")

    return app


app = create_app()

@app.get("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.app_port,
        workers=1,
        limit_concurrency=100,  # 总并发限制
        limit_max_requests=1000  # 最大请求数
    )
