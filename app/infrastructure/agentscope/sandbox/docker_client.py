"""统一的 aiodocker 客户端构造。

所有需要跟 docker daemon 通信的地方(prewarm、sweep_orphans、HardenedDockerWorkspace
的 `_provision_backend`)统一通过这里构造。

探测策略:
  1. ``settings.agent_docker_host`` 以 ``tcp://`` 开头(远程 daemon)
     → 不探测,直接用配置。
  2. 扫以下来源,按列表顺序返回第一个存在的 unix socket URL:
     - ``DOCKER_HOST`` 环境变量(Docker Desktop 安装时 CLI 自动 export,
       跨平台:macOS 指向 ``~/.docker/run/docker.sock``,Linux 指向
       ``~/.docker/desktop/docker.sock``,Windows WSL2 指向 ``/var/run/docker.sock``
       或 wsl 桥接路径)
     - ``~/.docker/desktop/docker.sock``(Linux Docker Desktop / 旧 macOS)
     - ``~/.docker/run/docker.sock``(macOS Docker Desktop 新版)
     - ``/var/run/docker.sock``(标准 daemon / 容器 mount)
  3. 都没命中 → fall back 到 ``settings.agent_docker_host``,让 aiodocker
     自己报错(连接错误向上层可见)。

Windows native(``npipe://``)走 ``DOCKER_HOST`` 环境变量路径,aiodocker 内部
支持,我们只读 env var 即可,不需要写 npipe 探测逻辑。
"""
from __future__ import annotations

import os
import ssl
from pathlib import Path

import aiodocker

from app.core.config.env_config import settings


def _candidate_unix_socket_urls() -> list[str]:
    """按优先级排序的候选 daemon URL 列表(已经过滤掉文件不存在的路径)。"""
    candidates: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        if url not in seen:
            candidates.append(url)
            seen.add(url)

    # 1. DOCKER_HOST env var(Docker Desktop 安装时 export,跨平台)
    docker_host = os.environ.get("DOCKER_HOST")
    if docker_host:
        add(docker_host)

    # 2. 平台常见路径(只保留文件实际存在的)
    plat_paths = [
        Path.home() / ".docker/desktop/docker.sock",  # Linux Docker Desktop / 旧 macOS
        Path.home() / ".docker/run/docker.sock",       # macOS Docker Desktop(新版)
        Path("/var/run/docker.sock"),                  # Linux 标准 daemon / 容器 mount
    ]
    for p in plat_paths:
        if p.exists():
            add(f"unix://{p}")

    return candidates


def make_docker_client() -> aiodocker.Docker:
    """构造 aiodocker 客户端:探测优先,settings 兜底。"""
    settings_url = settings.agent_docker_host

    ssl_ctx: ssl.SSLContext | None = None
    if settings.agent_docker_tls_verify:
        ssl_ctx = ssl.create_default_context()

    # 远程 daemon:不探测,直接用配置
    if settings_url.startswith("tcp://"):
        return aiodocker.Docker(url=settings_url, ssl_context=ssl_ctx)

    # unix socket / npipe / 任意:探测候选,拿第一个
    for url in _candidate_unix_socket_urls():
        return aiodocker.Docker(url=url, ssl_context=ssl_ctx)

    # 探测失败:兜底用 settings 配置(可能是 stale 路径,让 aiodocker 报错)
    return aiodocker.Docker(url=settings_url, ssl_context=ssl_ctx)


__all__ = ["make_docker_client"]