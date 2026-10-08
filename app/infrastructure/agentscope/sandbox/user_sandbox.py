"""Per-user sandbox manager + LazyBash tool wrapper.

`UserSandboxManager` 每个 `user_id` 一份 `UserSandbox`(sandbox + bash_tool +
末次活跃时间),惰性创建,per-user `asyncio.Lock` 防同用户并发首请求双开容器,
后台 reaper 定期回收闲置 sandbox,启动时 sweep 清掉任何带 agentscope 标签的
孤儿容器。

`LazyBash` 是 chat-side 的懒代理:LLM 在 chat 进来时看到的是一个 ToolBase
(name / description / input_schema 全填好),真正的 bash 实例 + docker 容器
要等第一次 `call()` 才拉起——纯聊天的用户不占容器。详见类内注释。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

from agentscope.permission import PermissionBehavior, PermissionDecision
from agentscope.tool import Bash, ToolBase
from agentscope.tool._response import ToolChunk

from app.core.common.logger import getLogger
from app.core.config.env_config import settings
from app.infrastructure.agentscope.middware.hostpath_guard_middware import HostPathGuardMiddleware
from app.infrastructure.agentscope.sandbox.user_workspace import (
    SkillSyncedWorkspace,
    sanitize_user_id,
)
from app.infrastructure.agentscope.sandbox.docker_client import (
    make_docker_client as _make_docker_client,
)

logger = getLogger()


@dataclass
class UserSandbox:
    """一个 user_id 对应一份 sandbox + bash 工具。"""
    user_id: str
    workspace: SkillSyncedWorkspace
    bash_tool: Bash
    last_active_at: float = field(default_factory=time.monotonic)


class UserSandboxManager:
    """全局唯一的 per-user 沙箱管理器,模块级单例。"""

    WORKSPACE_LABEL = "agentscope.sandbox"
    WORKSPACE_ID_LABEL = "agentscope.sandbox.id"

    def __init__(self) -> None:
        self._sandboxes: dict[str, UserSandbox] = {}
        # per-user 锁:防同 user_id 并发首请求创建两份 sandbox
        self._locks: dict[str, asyncio.Lock] = {}
        self._dict_lock = asyncio.Lock()
        self._reaper_task: asyncio.Task | None = None

    # ── lookup / lifecycle ────────────────────────────────────────

    async def get_or_create(self, user_id: str) -> UserSandbox:
        """取 sandbox,缺则惰性创建。并发首请求安全(per-user lock)。"""
        sb = self._sandboxes.get(user_id)
        if sb is not None:
            return sb

        lock = await self._get_lock(user_id)
        async with lock:
            sb = self._sandboxes.get(user_id)
            if sb is not None:
                return sb
            sb = await self._create(user_id)
            self._sandboxes[user_id] = sb
            return sb

    async def touch(self, user_id: str) -> None:
        sb = self._sandboxes.get(user_id)
        if sb is not None:
            sb.last_active_at = time.monotonic()

    async def close(self, user_id: str) -> None:
        sb = self._sandboxes.pop(user_id, None)
        # 锁也清掉,避免 dict 越来越大
        self._locks.pop(user_id, None)
        if sb is None:
            return
        # snapshot save 钩子在 SkillSyncedWorkspace.close() 里 —— 直接走
        # ws.close() 的路径(测试、admin 工具)也能拍到,不依赖 manager。
        try:
            await sb.workspace.close()
        except Exception:
            logger.exception("failed closing sandbox for user=%s", user_id)

    async def close_all(self) -> None:
        # 复制 keys,因为 close() 会改 dict
        for uid in list(self._sandboxes.keys()):
            await self.close(uid)

    # ── orphan sweep ──────────────────────────────────────────────

    async def sweep_orphans(self) -> int:
        """启动时调一次:把所有带 agentscope 标签但不在活跃集合的容器删掉。

        启动阶段 `_sandboxes` 一定是空的,所以会把所有同标签的容器
        一刀切清掉,这就是预期行为(每次启动都从干净状态开始)。

        返回被清理的容器数量。
        """
        try:
            import aiodocker
        except ImportError:
            logger.warning("aiodocker not available; skip orphan sweep")
            return 0

        # client 生命周期用 try/finally 统一管 — 之前几个早 return 路径
        # 会漏掉 close(),导致连接泄漏("Unclosed connector")。
        client = _make_docker_client()
        try:
            try:
                containers = await client.containers.list(
                    filters={"label": [f"{self.WORKSPACE_LABEL}=true"]},
                    all=True,
                )
            except Exception:
                logger.exception("orphan sweep failed (daemon unreachable?)")
                return 0

            if not containers:
                logger.info("orphan sweep: no labeled containers found")
                return 0

            killed = 0
            active_ids = set(self._sandboxes.keys())
            for c in containers:
                wid = await _label_value_async(c, self.WORKSPACE_ID_LABEL)
                if wid in active_ids:
                    continue
                try:
                    await c.kill()
                except Exception:
                    pass
                try:
                    await c.delete(force=True)
                    killed += 1
                    logger.info("orphan sweep: removed container id=%s ws=%s",
                                c.id[:12], wid)
                except Exception:
                    logger.exception("orphan sweep: failed deleting %s", wid)

            logger.info("orphan sweep done: %d container(s) removed", killed)
            return killed
        finally:
            # 全部容器操作做完再关 client — container 对象持有 client 引用,
            # 提前 close 会导致后续 delete 报 "Session is closed"。
            try:
                await client.close()
            except Exception:
                pass

    # ── idle reaper ───────────────────────────────────────────────

    async def reap_idle(self, ttl_seconds: int) -> int:
        """关闭所有 `last_active_at` 超过 ttl 的 sandbox。返回关闭数。"""
        now = time.monotonic()
        stale = [
            uid for uid, sb in self._sandboxes.items()
            if now - sb.last_active_at > ttl_seconds
        ]
        for uid in stale:
            logger.info("reaping idle sandbox user=%s (idle %.0fs)",
                        uid, now - self._sandboxes[uid].last_active_at)
            await self.close(uid)
        return len(stale)

    def start_reaper(
        self,
        interval_seconds: int | None = None,
        ttl_seconds: int | None = None,
    ) -> asyncio.Task:
        interval = interval_seconds or settings.agent_workspace_reaper_interval_seconds
        ttl = ttl_seconds or settings.agent_workspace_idle_ttl_seconds
        if self._reaper_task is not None and not self._reaper_task.done():
            return self._reaper_task

        async def _loop():
            logger.info("sandbox reaper started: interval=%ds ttl=%ds", interval, ttl)
            while True:
                try:
                    await asyncio.sleep(interval)
                    n = await self.reap_idle(ttl)
                    if n:
                        logger.info("reaper closed %d idle sandbox(es)", n)
                except asyncio.CancelledError:
                    logger.info("sandbox reaper stopped")
                    return
                except Exception:
                    logger.exception("reaper iteration failed; will retry")

        self._reaper_task = asyncio.create_task(_loop(), name="sandbox-reaper")
        return self._reaper_task

    async def stop_reaper(self) -> None:
        if self._reaper_task is None:
            return
        self._reaper_task.cancel()
        try:
            await self._reaper_task
        except asyncio.CancelledError:
            pass
        self._reaper_task = None

    # ── internals ─────────────────────────────────────────────────

    async def _get_lock(self, user_id: str) -> asyncio.Lock:
        async with self._dict_lock:
            lock = self._locks.get(user_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[user_id] = lock
            return lock

    async def _create(self, user_id: str) -> UserSandbox:
        logger.info("creating sandbox for user=%s", user_id)
        workspace = SkillSyncedWorkspace(user_id=user_id)
        await workspace.initialize()
        bash_tool = Bash(
            backend=workspace._workspace._backend,
            cwd=workspace.workdir,
            middlewares=[HostPathGuardMiddleware(workspace.host_workdir)],
        )
        # Bash 工具的环境说明文本由 LazyBash 在注册期静态拼好,
        # 这里不再覆写 description,避免每次 _create 都做无意义的字符串拼接。
        return UserSandbox(
            user_id=user_id,
            workspace=workspace,
            bash_tool=bash_tool,
            last_active_at=time.monotonic(),
        )


def _label_value(container_info: dict[str, Any], label: str) -> str:
    """同步取值:从 aiodocker 的 container.show() 返回结构里取 Labels。
    aiodocker 的 inspect 把标签放在 `Config.Labels`。
    """
    cfg = container_info.get("Config") or {}
    labels = cfg.get("Labels") or {}
    val = labels.get(label) or labels.get(label.replace(".", "_"))
    return val if isinstance(val, str) else ""


async def _label_value_async(container, label: str) -> str:
    """异步取值:`container.show()` 一次 inspect,再走同步取值路径。"""
    try:
        info = await container.show()
        return _label_value(info, label)
    except Exception:
        return ""


def _augment_bash_description(bash_tool: Bash, workspace: SkillSyncedWorkspace, sanitized: str) -> str:
    """兼容旧调用方的薄壳——env_block 跟 LazyBash 共用,语义不变。
    新代码应该用 LazyBash 而不是再走这条路径。
    """
    return _LAZY_BASH_DESCRIPTION


# 模块级常量:跟 Bash.description 拼成完整的工具说明。
# 之所以放在模块级而不是 LazyBash 实例属性,是因为这段 env_block 完全静态——
# 不依赖运行中的容器、不依赖 sanitized user_id,在 import 期一次性算好即可。
_LAZY_BASH_DESCRIPTION: str = Bash.description + """

# 当前环境(沙箱内,不是宿主机)
- Bash 在隔离的 Docker 容器里跑,所有命令都在沙箱内执行
- 沙箱外的绝对路径(`/home/...`、`~/.local/...` 等)都不可访问,误用会被中间件直接拦截
- 你和其他用户不共享容器——你的容器里只有你自己的文件

# 工作目录(相对路径的根)
- 所有"找文件"操作从这里出发:`ls`、`find .` 即可
- 公共 skill 目录:`./skills/<name>/scripts/...`
- 个人 skill 目录:`./personal_skills/<name>/scripts/...`

# 找 skill 的方法
1. `ls` 看顶层目录
2. `ls ./skills` 或 `find . -name SKILL.md` 列出可用 skill
3. 拼成相对路径 `./skills/<name>/scripts/<script>.py` 再 `python <path>`

# 重要
- **任何情况下都不要在回复里向用户暴露文件路径**——包括 cwd、个人/公共 skill 路径、容器路径
- 用户问你"有哪些 skill",只回答 skill 名称和功能描述;不要带路径
"""


async def _bash_auto_allow(tool_input, context):
    """Bash 永远 ALLOW,不弹确认。从 agent.py 移过来是为了让 LazyBash._ensure
    也能复用——agent.py 不再持有这个函数的副本。
    """
    return PermissionDecision(
        behavior=PermissionBehavior.ALLOW,
        message="bash auto-allowed",
    )


class LazyBash(ToolBase):
    """懒创建 sandbox 的 Bash 代理。

    注册期就把 name / description / input_schema 填好——LLM 看到的工具
    形态跟真实 Bash 完全一致。但底层 Bash 实例 + Docker 容器要等到
    第一次 `call()` 才拉起。

    为什么需要它:大多数 chat 轮次不调 bash(规划、提问、读结果、review)。
    在 chat 进来就 `get_or_create` 等于让几百个纯聊天用户在沙箱池里
    留满空容器,白白吃 cgroup / docker daemon 的开销。改成首次脚本
    调用才创建,reaper 也只看真正活跃的 user。

    关键不变量:
      - description 静态算好:只描述环境,不泄露路径/容器信息,所以
        在容器启动之前就能给 LLM 看
      - 同 user 首次并发调用被本类的 `_lock` 串行化,manager 内部还有
        一层 per-user asyncio.Lock,双重保险
      - `_ensure` 拿到 bash 后顺手装上 `check_permissions = ALLOW`,
        避免原 agent.py 那种每次 chat 重复赋值的写法
      - `is_concurrency_safe = False`:并发 bash 调用由 Bash 自己内部
        序列化(lock on the same backend),跟父类语义一致
    """

    name: str = "Bash"
    description: str = _LAZY_BASH_DESCRIPTION
    input_schema: dict[str, Any] = Bash.input_schema
    is_mcp: bool = False
    is_read_only: bool = False
    is_concurrency_safe: bool = False
    is_external_tool: bool = False
    is_state_injected: bool = False

    def __init__(self, manager: "UserSandboxManager", user_id: str) -> None:
        super().__init__()
        self._manager = manager
        self._user_id = user_id
        self._bash: Bash | None = None
        self._lock = asyncio.Lock()

    async def check_permissions(self, tool_input, context):
        """Bash 永远 ALLOW,不弹确认。本身覆盖了 ToolBase 的抽象方法,
        所以即使底层 sandbox 还没拉起,permission 引擎也能直接拿决策。
        """
        return await _bash_auto_allow(tool_input, context)

    async def _ensure(self) -> Bash:
        if self._bash is not None:
            return self._bash
        async with self._lock:
            if self._bash is not None:
                return self._bash
            sandbox = await self._manager.get_or_create(self._user_id)
            await self._manager.touch(self._user_id)
            bash = sandbox.bash_tool
            bash.check_permissions = _bash_auto_allow
            self._bash = bash
            logger.info(
                "lazy sandbox materialized user=%s (first bash call)",
                self._user_id,
            )
            return bash

    async def call(
        self,
        command: str,
        description: str = "",
        timeout: int = 120000,
    ) -> AsyncGenerator[ToolChunk, None]:
        bash = await self._ensure()
        async for chunk in bash.call(
            command=command,
            description=description,
            timeout=timeout,
        ):
            yield chunk


_manager: UserSandboxManager | None = None


def get_manager() -> UserSandboxManager:
    """全局单例。惰性创建避免模块导入期就触发 asyncio loop。"""
    global _manager
    if _manager is None:
        _manager = UserSandboxManager()
    return _manager


__all__ = ["UserSandbox", "UserSandboxManager", "get_manager", "LazyBash"]