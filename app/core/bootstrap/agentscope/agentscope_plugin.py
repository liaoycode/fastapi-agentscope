import asyncio
import logging
import time

from fastapi import FastAPI

from app.core.bootstrap.abs_boot_plugin import AppPlugin
from app.core.common.logger import getLogger
from app.core.config.env_config import settings
from app.infrastructure.agentscope.sandbox.user_sandbox import get_manager

logger = getLogger()

# Workaround: agentscope==2.0.8 ships with broken resource paths in two
# places — both still point at the pre-refactor `agentscope.sandbox.*`
# packages while the actual files moved to `agentscope.workspace.*`.
# Without these repoints, every fresh-process DockerWorkspace.initialize()
# crashes when it loads templates/scripts via importlib.resources. Safe to
# remove once upstream fixes the paths.
from agentscope.workspace._docker import _make_dockerfile as _as_make_dockerfile
_as_make_dockerfile._TEMPLATE_PKG = "agentscope.workspace._docker"

import importlib.resources as _res
from agentscope.workspace import _utils as _as_utils

def _patched_read_gateway_script_bytes() -> bytes:
    return (
        _res.files("agentscope.workspace._mcp_gateway")
        .joinpath("_mcp_gateway_app.py")
        .read_bytes()
    )
_as_utils._read_gateway_script_bytes = _patched_read_gateway_script_bytes
# _make_dockerfile imports `_read_gateway_script_bytes` into its own module
# namespace, so re-patching `_utils` doesn't reach it. Override the local
# binding too.
_as_make_dockerfile._read_gateway_script_bytes = _patched_read_gateway_script_bytes

# Per-step timing on workspace initialize, so we can see where the seconds
# go. Wrap _sandboxed_base.SandboxedBase.initialize with a version that
# logs the wall time of each phase.
from agentscope.workspace import _sandboxed_base as _as_sandboxed_base
from agentscope._logging import logger as _as_logger

_SANDBOXED_INIT_PHASES = (
    "_provision_backend",
    "_restore_mcp_specs",
    "_ensure_workspace_layout",
    "_setup_mcp_gateway",
    "_migrate_skill_layout",
    "_setup_skills",
)


async def _timed_initialize(self):
    _as_logger.info(
        "Initialize sandbox (id=%s) from %s ...",
        self.workspace_id, self.__class__.__name__,
    )
    if self.is_alive:
        return

    timings: list[tuple[str, float]] = []
    overall_t0 = time.monotonic()

    t0 = time.monotonic()
    await self._provision_backend()
    timings.append(("_provision_backend", time.monotonic() - t0))
    assert (
        self._backend is not None
    ), "_provision_backend must set self._backend before returning"

    t0 = time.monotonic()
    self._mcp_specs = await self._restore_mcp_specs()
    timings.append(("_restore_mcp_specs", time.monotonic() - t0))

    t0 = time.monotonic()
    await self._ensure_workspace_layout()
    timings.append(("_ensure_workspace_layout", time.monotonic() - t0))

    t0 = time.monotonic()
    await self._setup_mcp_gateway()
    timings.append(("_setup_mcp_gateway", time.monotonic() - t0))

    t0 = time.monotonic()
    await self._migrate_skill_layout()
    timings.append(("_migrate_skill_layout", time.monotonic() - t0))

    t0 = time.monotonic()
    await self._setup_skills()
    timings.append(("_setup_skills", time.monotonic() - t0))

    self.is_alive = True

    breakdown = ", ".join(f"{name}={dt:.2f}s" for name, dt in timings)
    _as_logger.info(
        "Finished initializing sandbox (id=%s) from %s. timings: %s | total=%.2fs",
        self.workspace_id, self.__class__.__name__,
        breakdown, time.monotonic() - overall_t0,
    )


_as_sandboxed_base.SandboxedWorkspaceBase.initialize = _timed_initialize


class AgentscopePlugin(AppPlugin):
    """管理 per-user sandbox 生命周期:

    - 启动:扫一遍孤儿容器(前次进程强杀留下的),再后台预构建 sandbox 镜像,
      接着跑 reaper 回收闲置 sandbox
    - 关闭:停 reaper,关掉所有 sandbox
    """

    async def on_startup(self, app: FastAPI):
        if settings.agent_sandbox_mode != "docker":
            logger.warning(
                "AgentscopePlugin: AGENT_SANDBOX_MODE=%s, "
                "skipping docker init (sweep/prewarm/reaper)",
                settings.agent_sandbox_mode,
            )
            return
        manager = get_manager()
        await manager.sweep_orphans()
        asyncio.create_task(self._prewarm_image())
        manager.start_reaper()

    async def on_shutdown(self, app: FastAPI):
        if settings.agent_sandbox_mode != "docker":
            # local/none mode 下没有 docker 容器要关,get_manager() 也没人调过。
            return
        manager = get_manager()
        await manager.stop_reaper()
        await manager.close_all()

    async def _prewarm_image(self) -> None:
        """后台预构建 sandbox 镜像,首请求走 cache hit 跳过几分钟构建。

        镜像 tag 由 Dockerfile 内容 + copy files 哈希决定,不依赖
        workspace_id / host_workdir,所以临时 workspace 构建出来的镜像
        会被真正 sandbox 复用。容器启动 + MCP gateway 仍按需进行(只能
        在容器跑起来后做),但这两步通常只要几秒。
        """
        # 延迟 import:这个文件顶部 import HardenedDockerWorkspace 会触发
        # agentscope.workspace.__init__ 里更重的加载图。
        from app.infrastructure.agentscope.sandbox.user_workspace import (
            HardenedDockerWorkspace,
        )
        try:
            import aiodocker
        except ImportError:
            logger.warning("prewarm skipped: aiodocker not available")
            return

        workspace = HardenedDockerWorkspace(
            workspace_id="_prewarm_",
            host_workdir=None,
        )
        from app.infrastructure.agentscope.sandbox.docker_client import (
            make_docker_client,
        )
        workspace._client = make_docker_client()
        try:
            await workspace._build_or_reuse_image()
            logger.info("prewarm: image ready (%s)", workspace._image_tag)
        except Exception:
            logger.exception(
                "prewarm: image build failed (will rebuild on demand)",
            )
        finally:
            try:
                await workspace._client.close()
            except Exception:
                pass