from fastapi import FastAPI

from app.core.bootstrap.abs_boot_plugin import AppPlugin
from app.core.bootstrap.nacos.nacos_registry import NacosRegistry
from app.core.common.logger import getLogger
from app.core.config.env_config import settings

"""naocs注册"""
logger = getLogger()

class NacosPlugin(AppPlugin):
    @classmethod
    def is_enabled(cls) -> bool:
        return settings.nacos_enable if settings.nacos_enable is not None else True

    def __init__(self):
        self.registry = NacosRegistry(
            server_addresses=settings.nacos_server,
            namespace=settings.nacos_namespace,
            username=settings.nacos_username,
            password=settings.nacos_password,
        )

    async def on_startup(self, app: FastAPI):
        if not settings.nacos_enable:
            logger.info("nacos is disabled")
            return

        logger.info("nacos is enabled")
        self.registry.register(
            service_name=settings.nacos_server_name,
            port=settings.app_port,
            group=settings.nacos_group,
            metadata={"version": "1.0.0"},
        )
        self.registry.start_heartbeat(interval=5)

    async def on_shutdown(self, app: FastAPI):
        if not settings.nacos_enable:
            return

        self.registry.stop_heartbeat()
        self.registry.deregister()