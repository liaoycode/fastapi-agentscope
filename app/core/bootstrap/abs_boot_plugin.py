from fastapi import FastAPI
from abc import ABC, abstractmethod

class AppPlugin(ABC):
    @classmethod
    def is_enabled(cls) -> bool:
        """默认启用,子类可覆盖此方法来控制是否加载"""
        return True

    @abstractmethod
    async def on_startup(self, app: FastAPI): ...

    @abstractmethod
    async def on_shutdown(self, app: FastAPI): ...