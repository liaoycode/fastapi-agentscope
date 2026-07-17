import nacos
import socket
import asyncio
from typing import Optional

from app.core.common.logger import getLogger

logger = getLogger()

class NacosRegistry:
    def __init__(
        self,
        server_addresses: str,       # "127.0.0.1:8848"
        namespace: str,
        username: str ,
        password: str,
    ):
        self.client = nacos.NacosClient(
            server_addresses,
            namespace=namespace,
            username=username,
            password=password,
        )
        self._heartbeat_task: Optional[asyncio.Task] = None

    def get_local_ip(self) -> str:
        """获取本机 IP"""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()

    def register(
        self,
        service_name: str,
        port: int,
        ip: Optional[str] = None,
        group: str = "DEFAULT_GROUP",
        metadata: dict = None,
        weight: float = 1.0,
    ):
        ip = ip or self.get_local_ip()
        self.client.add_naming_instance(
            service_name,
            ip,
            port,
            group_name=group,
            weight=weight,
            metadata=metadata or {},
            healthy=True,
            ephemeral=True,   # 临时实例，断连自动下线
        )
        logger.info(f"[Nacos] 注册成功: {service_name} -> {ip}:{port}")
        self._ip = ip
        self._port = port
        self._service_name = service_name
        self._group = group

    def deregister(self):
        self.client.remove_naming_instance(
            self._service_name,
            self._ip,
            self._port,
            group_name=self._group,
        )
        logger.info(f"[Nacos] 注销成功: {self._service_name}")

    async def _send_heartbeat(self, interval: int = 5):
        """定时发送心跳，保持实例存活"""
        while True:
            try:
                self.client.send_heartbeat(
                    self._service_name,
                    self._ip,
                    self._port,
                    group_name=self._group,
                )
            except Exception as e:
                logger.warning(f"[Nacos] 心跳失败: {e}")
            await asyncio.sleep(interval)

    def start_heartbeat(self, interval: int = 5):
        loop = asyncio.get_event_loop()
        self._heartbeat_task = loop.create_task(
            self._send_heartbeat(interval)
        )

    def stop_heartbeat(self):
        if self._heartbeat_task:
            self._heartbeat_task.cancel()