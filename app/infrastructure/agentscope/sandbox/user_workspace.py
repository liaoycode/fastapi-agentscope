"""Per-user Docker sandbox, hardened.

`HardenedDockerWorkspace` 子类化 agentscope 的 `DockerWorkspace`,只覆写
`_provision_backend` 与 `_create_and_start_container`,做两件事:

1. 让 `_client` 走 ``make_docker_client()`` —— 注入 ``agent_docker_host``
   配置(默认 unix socket,远程沙箱主机时切 TCP),docker-in-docker / 远端
   daemon 部署都兼容。

2. 把沙箱的 `/sandbox` 从 bind mount(依赖 host 路径)换成 docker 命名卷。
   命名卷由 daemon 持有,不依赖任何 host 路径,跨容器销毁存活,
   沙箱主机迁移 / 容器重启零成本复用。卷名 = ``<volume_prefix>-<sanitized_uid>``,
   运维通过 ``docker volume ls | grep <volume_prefix>`` 即可定位所有用户卷。

其余行为(镜像构建、MCP gateway、skill sync 等)全部继承自基类;
`initialize` 仍覆写为精简版,跳过 sandbox 内 MCP gateway 与 skill 同步。

`SkillSyncedWorkspace` 是 per-user 门面:
  * `workspace_id=sanitized_uid`,容器名跨重启稳定(`as_ws_<user_id>`),
    孤儿清扫 / reaper 才能识别"自己人"。
  * `host_workdir` 现在只是"卷名种子"(已不再当 host 路径用),传给基类后
    在 `_create_and_start_container` 里拼出命名卷名。
  * `initialize` 在容器起来之后做两件事:
      1. ``_stage_and_push_skills``:把 public skill(从 app 镜像 baked-in 路径)
         + personal skill(从 PG 物化)打包,``put_archive`` 推进 ``/sandbox``。
      2. ``_restore_latest_snapshot``:从 PG 拉最近一份 workspace 快照 restore。
         skill 子目录会被 skill push 覆盖,所以 snapshot 阶段已经把这两个目录
         排除在外(见 WorkspaceSnapshotter.save)。

网络隔离注意: `--network=none` 会破坏容器内的 MCP gateway——后者通过
loopback 上的 `127.0.0.1:<gateway_port>` 自洽
(`agentscope/sandbox/_gateway_client.py:478,711`)。所以我们保留 `bridge`
网络,改用裁剪危险 cap 的方式。出站过滤属于宿主级问题(在 bridge 上配
iptables / nftables),本模块不做。

为什么没开 ReadonlyRootfs: agentscope 的 MCP gateway home 是
`/root/.agentscope`,镜像构建时把脚本和 venv 都烤进去了。若挂 Tmpfs
到 `/root` 会把构建产物遮蔽,导致 `file_exists` 探针失败;若不挂则
`put_archive` 又因为 rootfs 只读失败。`HardenedDockerWorkspace` 目前只
保留 cap_drop + no-new-privileges + 资源硬限 + tmpfs 这几项
ReadonlyRootfs 之外的加固,详见 `_create_and_start_container` 内注释。
"""
import asyncio
import io
import os
import re
import shutil
import tarfile
import time
from pathlib import Path
from typing import Any

from agentscope._logging import logger
from agentscope.workspace import DockerWorkspace

from app.core.config.env_config import settings
from app.infrastructure.agentscope.sandbox.docker_client import (
    make_docker_client,
)


# Docker 容器名只允许 `[a-zA-Z0-9_.-]`,其余字符全部归一成 `_`
_DOCKER_NAME_RE = re.compile(r"[^a-zA-Z0-9_.-]")


def sanitize_user_id(user_id: str) -> str:
    """Docker 容器名只接受 `[a-zA-Z0-9_.-]`,其他字符替换为 `_`。
    空 user_id 落到 `anon` 兜底,避免后续容器名校验失败。
    """
    if not user_id:
        return "anon"
    return _DOCKER_NAME_RE.sub("_", user_id)


def parse_mem_limit(spec: str) -> int:
    """`"256m"` / `"1g"` 这种 Docker 内存规格字符串 → 字节数(int)。
    Docker API 的 `Memory` 字段只接受整数。
    """
    s = spec.strip().lower()
    if not s:
        return 0
    units = {"k": 1024, "m": 1024**2, "g": 1024**3}
    if s and s[-1] in units:
        return int(float(s[:-1]) * units[s[-1]])
    return int(s)


class HardenedDockerWorkspace(DockerWorkspace):
    """覆写 `_provision_backend` 与 `_create_and_start_container`。

    - `_provision_backend`:构造 `_client` 时走 ``make_docker_client()``,
      这样 ``agent_docker_host`` / TLS 配置生效。
    - `_create_and_start_container`:把容器 workdir 从 bind mount 换成命名卷
      ``<volume_prefix>-<workspace_id>``,host 路径无关;HostConfig 的加固项
      保留。
    - `__init__` 接受 ``container_workdir`` 参数覆盖 ``self.workdir``(默认走
      ``settings.agent_workspace_container_dir`` = ``/sandbox``)。这条路径是
      命名卷挂载点 / WorkingDir / PersonalSkillLoader 拼 Skill.dir 的共同依据。
    """

    def __init__(
        self,
        workspace_id: str,
        host_workdir: str | None = None,
        container_workdir: str | None = None,
    ) -> None:
        super().__init__(
            workspace_id=workspace_id,
            host_workdir=host_workdir,
        )
        if container_workdir is not None:
            self.workdir = container_workdir

    async def initialize(self):
        """精简版 initialize:只跑容器创建,跳过 MCP gateway / skill sync。

        父类完整流程还要走 `_restore_mcp_specs` / `_ensure_workspace_layout` /
        `_setup_mcp_gateway` / `_migrate_skill_layout` / `_setup_skills`。其中
        `_setup_mcp_gateway` 单步占 ~6.7s —— 在容器内启动 MCP gateway server
        并轮询 /health 等它 ready。

        当前项目只用 `Bash` tool + agent 层 `PersonalSkillLoader`,不依赖
        sandbox 内的 MCP gateway 也不依赖容器内 skill 同步(我们 skill 是
        push 进命名卷的),所以这几步都可以省。

        需要 sandbox 内 MCP 工具时,把方法体换成
        `return await super().initialize()` 即可恢复完整流程。
        """
        if self.is_alive:
            return
        logger.info(
            "Initialize sandbox (id=%s) from %s ...",
            self.workspace_id, self.__class__.__name__,
        )
        t0 = time.monotonic()
        await self._provision_backend()
        logger.info(
            "[timing] _provision_backend: %.2fs",
            time.monotonic() - t0,
        )
        assert (
            self._backend is not None
        ), "_provision_backend must set self._backend before returning"
        self.is_alive = True
        logger.info(
            "Finished initializing sandbox (id=%s) from %s.",
            self.workspace_id, self.__class__.__name__,
        )

    async def _provision_backend(self) -> None:
        """与父类同名方法等价,只是 ``_client`` 走 ``make_docker_client()``。

        父类的实现是 ``self._client = aiodocker.Docker()``,硬编码 unix socket,
        无法走 settings 里的 ``agent_docker_host``。这里覆盖一次,
        所有用到 docker daemon 的地方都从同一个口子出。
        """
        self._client = make_docker_client()
        await self._build_or_reuse_image()
        await self._create_and_start_container()

    async def _create_and_start_container(self) -> None:
        """HostConfig 带加固项 + `/sandbox` 走命名卷。"""
        config: dict[str, Any] = {
            "Image": self._image_tag,
            "Cmd": ["sleep", "infinity"],
            "WorkingDir": self.workdir,
            "Labels": {
                "agentscope.sandbox": "true",
                "agentscope.sandbox.id": self.workspace_id,
            },
        }
        # 沙箱无 TTY + sandbox 镜像模板未设 PYTHONUNBUFFERED,默认注入 1
        # 让 print 行缓冲,docker logs 能实时看到。开关走 settings。
        # 调用方在 self.env 里显式传同名 key 可覆盖(比如调试想关掉)。
        merged_env = dict(self.env or {})
        if settings.agent_workspace_python_unbuffered:
            merged_env.setdefault("PYTHONUNBUFFERED", "1")
        if merged_env:
            config["Env"] = [f"{k}={v}" for k, v in merged_env.items()]

        # 在默认容器配置之上叠加加固项
        host_config: dict[str, Any] = {
            "Memory": parse_mem_limit(settings.agent_workspace_mem_limit),
            "PidsLimit": settings.agent_workspace_pids_limit,
            "CpuShares": settings.agent_workspace_cpu_shares,
            # 只裁掉最危险的几个 cap,其他保留以免破坏 Python / pip 的常规能力
            "CapDrop": ["NET_RAW", "NET_ADMIN", "SYS_ADMIN", "SYS_PTRACE"],
            "SecurityOpt": ["no-new-privileges:true"],
            # ReadonlyRootfs 没开:与 agentscope 的 MCP gateway 不兼容。
            # gateway_home(/root/.agentscope)在镜像构建时由 Dockerfile 创建,
            # 运行时若挂 Tmpfs 到 /root 会把构建产物遮蔽,put_archive 又因
            # rootfs 只读失败。后续可以考虑通过修改 gateway_home 路径或
            # bind-mount 它来重新打开 ReadonlyRootfs——目前先保留 cap_drop
            # + no-new-privileges + 资源硬限作为主要加固手段。
            # /tmp 给一般 scratch 用,/run 给守护进程的杂项 socket
            "Tmpfs": {
                "/tmp": "size=100m,mode=1777",
                "/run": "size=10m,mode=0755",
            },
        }
        if self.host_workdir is not None:
            # 语义变化:host_workdir 不再是 host 路径,而是命名卷的"种子"。
            # 命名卷由 docker daemon 自动按需创建,跨容器销毁存活。
            volume_name = (
                f"{settings.agent_user_volume_prefix}-{self.host_workdir}"
            )
            host_config["Binds"] = [f"{volume_name}:{self.workdir}:rw"]
        config["HostConfig"] = host_config

        self._container = await self._client.containers.create_or_replace(
            name=f"as_ws_{self.workspace_id}",
            config=config,
        )
        await self._container.start()
        # 延迟导入避免模块加载期就拉起 backend
        from agentscope.workspace._docker._docker_backend import DockerBackend
        self._backend = DockerBackend(self._container, self.workdir)


def _tar_dir_to_bytes(src_dir: Path) -> bytes:
    """把 ``src_dir`` 整个打成 gzip tar,返回 bytes。

    用 ``tarfile`` 标准库 + ``BytesIO``,典型几十 MB workspace 内存毫无压力;
    真大到撑爆就改成流式(分块 ``put_archive`` / chunked upload to PG)。
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # arcname 用相对路径,这样解到 /sandbox 时是干净的子目录
        tar.add(str(src_dir), arcname=".")
    return buf.getvalue()


class SkillSyncedWorkspace:
    """Per-user 沙箱门面。

    容器起来之后做两件事:
      1. ``_stage_and_push_skills`` —— 把 public + personal skill 物化到 app
         容器内 staging 目录,打成 tar,``put_archive`` 推进 ``/sandbox``。
      2. ``_restore_latest_snapshot`` —— 从 PG 拉最近一份 workspace 快照
         restore。skill 子目录会被 skill push 覆盖,所以 snapshot save 时
         已经把它们排除(见 WorkspaceSnapshotter._SAVE_CMD)。

    staging 路径 ``/tmp/agent-skills-<workspace_id>`` 在 app 容器内,
    跟 host 路径完全无关 —— docker-in-docker / 远端 daemon 部署都适用。
    """

    def __init__(
        self,
        user_id: str,
        skill_sources: list[str] | None = None,
    ):
        self.user_id = user_id
        sanitized = sanitize_user_id(user_id)
        # skill_sources 字段保留签名兼容,实际不再用(public 走 host dir,
        # personal 走 PG)
        self._skill_sources = skill_sources
        # container_workdir 默认走 settings —— LLM 看到的 Skill.dir、容器
        # WorkingDir、命名卷挂载点全都来自同一个值,改一处全生效。
        self._workspace = HardenedDockerWorkspace(
            workspace_id=sanitized,
            host_workdir=sanitized,  # 现在是卷名种子
            container_workdir=settings.agent_workspace_container_dir,
        )

    @property
    def host_workdir(self) -> str:
        return self._workspace.host_workdir

    @property
    def workdir(self) -> str:
        return self._workspace.workdir

    @property
    def _container(self):
        """暴露 aiodocker 容器给上层做 put_archive / exec / get_archive。"""
        return self._workspace._container

    async def initialize(self):
        await self._workspace.initialize()
        # 容器起来后再推 skill 和 restore snapshot —— 这时 _backend 已就绪,
        # _container 可用,put_archive 才能写进命名卷。
        await self._stage_and_push_skills(self._workspace._container)
        if settings.agent_snapshot_enabled:
            await self._restore_latest_snapshot(self._workspace._container)

    async def close(self):
        # 先 save snapshot 再关容器 —— 命名卷保留,snapshot 是冗余备份。
        # 钩子放在这里而不是 UserSandboxManager.close 是因为:
        # 1. 这里有 self.user_id,manager 那条路径传 user_id 进来其实就是绕一圈
        # 2. 直接调 ws.close() 不走 manager 的场景(测试、admin 工具)也能拍快照
        if settings.agent_snapshot_enabled:
            try:
                container = self._workspace._container
                if container is not None:
                    from app.infrastructure.agentscope.sandbox.snapshot import (
                        WorkspaceSnapshotter,
                    )
                    await WorkspaceSnapshotter().save(
                        container=container,
                        user_id=self.user_id,
                        label="auto-pre-close",
                    )
            except Exception:
                logger.exception(
                    "snapshot save failed during ws.close user=%s",
                    self.user_id,
                )
        return await self._workspace.close()

    async def _stage_and_push_skills(self, container) -> None:
        """物化 → tar → put_archive 一次,推到 ``/sandbox``。

        staging 在 app 容器内 ``/tmp/agent-skills-<workspace_id>``,每次
        sandbox 创建覆盖;命名卷会被 put_archive 写穿。

        push 完之后做一步清理:sandbox 内 ``/sandbox/skills/`` 和
        ``/sandbox/personal_skills/`` 下,凡是 host/PG 期望列表里没有的子目录
        直接 ``rm -rf``。``put_archive`` 只覆盖不删除,这一补刀是为了让
        "host 删了一个 skill → 重启后 sandbox 也跟着干净"。
        """
        workspace_id = self._workspace.workspace_id
        staging = Path(f"/tmp/agent-skills-{workspace_id}")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)

        # public skills:从 host disk 上的 agent_skill_public_dir 拷过来。
        # 注意 staging 子目录名必须跟 agent_skill_public_subdir 一致,
        # put_archive 写到 self.workdir 时会把 staging 内容原样映射到
        # sandbox 内对应子目录。LLM 通过 SandboxLocalSkillLoader 看到的
        # Skill.dir 也是 {workdir}/{public_subdir}/<name>/,三处对齐。
        public_src = Path(settings.agent_skill_public_dir)
        public_staging = staging / settings.agent_skill_public_subdir
        if public_src.is_dir():
            shutil.copytree(public_src, public_staging)
            logger.info(
                "skill staging: public from %s -> %s",
                public_src, public_staging,
            )

        # personal skills:走 PG。target_root 不嵌 user_id —— staging 本身
        # 已 per-sandbox 隔离。同上,目录名跟 agent_skill_personal_subdir 对齐。
        await _materialize_personal_skills(
            user_id=self.user_id,
            target_root=staging / settings.agent_skill_personal_subdir,
        )

        # tar 一次,put_archive 一次(单次 RTT)
        tar_bytes = await asyncio.to_thread(_tar_dir_to_bytes, staging)
        if tar_bytes:
            await container.put_archive(
                path=self._workspace.workdir,  # /sandbox
                data=tar_bytes,
            )
            logger.info(
                "skill sync pushed: workspace=%s bytes=%d",
                workspace_id, len(tar_bytes),
            )

        # 清理 sandbox 内已不存在的 skill 残留。put_archive 不删文件,
        # 所以这一步是补刀 —— host 删了 skill,或 PG 删了 personal skill,
        # 重启时 sandbox 也要跟着干净。
        await self._prune_stale_skills(container)

    async def _prune_stale_skills(self, container) -> None:
        """sandbox 内 ``skills/`` 和 ``personal_skills/`` 下,sandbox 存在但
        host/PG 期望列表里没有的子目录 → ``rm -rf``。

        期望列表:
          - public:host disk ``agent_skill_public_dir`` 下的直接子目录
          - personal:PG ``personal_skill`` 表里 ``user_id=self.user_id`` 的
            ``skill_name``
        """
        from app.infrastructure.agentscope.sandbox.snapshot import (
            _exec_collect_stdout,
        )
        workdir = self._workspace.workdir
        public_subdir = settings.agent_skill_public_subdir
        personal_subdir = settings.agent_skill_personal_subdir

        # public 期望集:host 上 agent_skill_public_dir 的直接子目录
        public_src = Path(settings.agent_skill_public_dir)
        if public_src.is_dir():
            expected_public = {
                p.name for p in public_src.iterdir() if p.is_dir()
            }
        else:
            expected_public = set()

        # personal 期望集:直接 query PG,不走 materialize(避免重复物化)
        expected_personal = await _list_personal_skill_names(self.user_id)

        for subdir, expected in (
            (public_subdir, expected_public),
            (personal_subdir, expected_personal),
        ):
            sandbox_dir = f"{workdir}/{subdir}"
            actual_bytes = await _exec_collect_stdout(
                container,
                f"cd {sandbox_dir} && ls -1 2>/dev/null",
            )
            actual = set(actual_bytes.decode().split()) if actual_bytes else set()
            stale = sorted(actual - expected)
            if not stale:
                continue
            # 用单条 sh -c 把多个 rm 串起来,一次 RTT。注意 stale 来自
            # sandbox 内 ls,理论上名字安全(都是我们 put_archive 写过的),
            # 但仍加引号防止万一含 shell 元字符。
            rm_cmd = " && ".join(
                f"rm -rf '{sandbox_dir}/{n}'" for n in stale
            )
            await _exec_collect_stdout(container, rm_cmd)
            logger.info(
                "skill prune: %s removed %d stale entries: %s",
                sandbox_dir, len(stale), stale,
            )

    async def _restore_latest_snapshot(self, container) -> None:
        """从 PG 拉最近一份 workspace 快照,put_archive 灌进 ``/sandbox``。

        skill push 已经在前一步完成,snapshot 不含 skill 子目录,所以
        restore 不会冲掉 skill。
        """
        from app.infrastructure.agentscope.sandbox.snapshot import (
            WorkspaceSnapshotter,
        )
        ok = await WorkspaceSnapshotter().restore_latest(
            container=container, user_id=self.user_id,
        )
        if ok:
            logger.info(
                "snapshot restored into workspace=%s user=%s",
                self._workspace.workspace_id, self.user_id,
            )


async def _materialize_personal_skills(
    user_id: str, target_root: Path,
) -> int:
    """把 ``user_id`` 的 personal skill 物化到 ``target_root/<skill_name>``。

    target_root 已经是 per-sandbox 隔离的(staging 目录),不用嵌 user_id。
    """
    from app.infrastructure.agentscope.skill.personal import PersonalSkillLoader
    loader = PersonalSkillLoader(user_id=user_id)
    return await loader.materialize_all(target_root)


async def _list_personal_skill_names(user_id: str) -> set[str]:
    """从 PG 拿 ``user_id`` 的 personal skill name 集合,不走物化。

    给 ``_prune_stale_skills`` 用:对比 sandbox 端实际列表,差集就是要清掉的。
    """
    from sqlalchemy import text
    from app.infrastructure.datasource import database as db
    if db._session_factory is None:
        return set()
    try:
        async with db._session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT skill_name FROM personal_skill "
                        "WHERE user_id = :user_id",
                    ),
                    {"user_id": user_id},
                )
            ).scalars().all()
        return set(rows)
    except Exception:
        logger.exception(
            "_list_personal_skill_names: PG query failed user=%s", user_id,
        )
        return set()