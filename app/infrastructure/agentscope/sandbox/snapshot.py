"""WorkspaceSnapshotter:沙箱 workspace 的 save / restore。

save 路径
---------
容器内 ``tar --exclude=skills --exclude=personal_skills -czf - -C /sandbox .``
→ exec 的 stdout 直接是 gzip tar 字节流 → 写 PG bytea。

为什么不先写 ``/tmp/snap.tgz`` 再 ``get_archive``:
本机 docker daemon 28.x 对 tmpfs 路径的 get_archive 返回 404(只支持
容器镜像层 / 命名卷路径)。直接走 exec stdout 更稳,少一次中转。

restore 路径
-----------
PG bytea → gzip 解压 → ``container.put_archive('/sandbox', tar_bytes)``
→ daemon 把 tar 解到 /sandbox。

大小限制
--------
exec 默认没设 timeout,大 workspace 跑 tar 几分钟没问题,但要小心 tar 本身
对 /tmp(tmpfs 100m)没占用 —— 我们走 stdout 不落盘,所以这个限制解除了。
"""
from __future__ import annotations

import gzip
import logging

from aiodocker.containers import DockerContainer

from app.infrastructure.datasource import database as db
from app.infrastructure.repositories.workspace_snapshot_repo import (
    WorkspaceSnapshotRepo,
)


logger = logging.getLogger(__name__)


_SAVE_CMD = (
    "tar --exclude='skills' --exclude='personal_skills' "
    "-czf - -C /sandbox ."
)


async def _exec_collect_stdout(
    container: DockerContainer, cmd: str,
) -> bytes:
    """exec 一条 sh 命令,把 stdout 全部字节收回来。

    aiodocker 的 exec.start(detach=False) 返回 Stream,read_out() 拿到
    ``Message(stream, data)`` 的 NamedTuple(stream=1 stdout / 2 stderr,
    data=bytes),EofStream 时返回 None。
    """
    ex = await container.exec(["sh", "-c", cmd])
    stream = ex.start(detach=False)
    pieces: list[bytes] = []
    while True:
        msg = await stream.read_out()
        if msg is None:
            break
        if msg.stream == 1:  # stdout
            pieces.append(msg.data)
    return b"".join(pieces)


class WorkspaceSnapshotter:
    """负责容器 workspace ↔ PG 字节流的搬运。"""

    def __init__(self) -> None:
        # 不持 session;每次操作自己起 session,免得跟外层事务耦合
        self._session_factory = db._session_factory

    async def save(
        self,
        container: DockerContainer,
        user_id: str,
        label: str | None = None,
    ) -> int | None:
        """exec tar 到 stdout → 写 PG bytea。失败返回 None。"""
        if self._session_factory is None:
            logger.warning("snapshot save skipped: database not initialized")
            return None
        try:
            data = await _exec_collect_stdout(container, _SAVE_CMD)
            if not data:
                logger.warning(
                    "snapshot save: tar produced empty output user=%s",
                    user_id,
                )
                return None
        except Exception:
            logger.exception(
                "snapshot save: tar exec failed user=%s", user_id,
            )
            return None

        try:
            async with self._session_factory() as session:
                repo = WorkspaceSnapshotRepo(session)
                if label == "auto-pre-close":
                    # 滚动:auto-pre-close 留最近 (N-1) 份,新存的算第 N 份。
                    # 留 N-1 而不是 N 是因为下面 insert 完之后总数还是 N。
                    from app.core.config.env_config import settings
                    await repo.purge_auto_keep_n(
                        user_id=user_id,
                        n=max(0, settings.agent_snapshot_max_auto_per_user - 1),
                    )
                snap_id = await repo.insert(
                    user_id=user_id,
                    label=label,
                    tar_bytes=data,
                    size_bytes=len(data),
                )
                await session.commit()
            logger.info(
                "snapshot saved: user=%s id=%d bytes=%d label=%s",
                user_id, snap_id, len(data), label,
            )
            return snap_id
        except Exception:
            logger.exception(
                "snapshot save: PG write failed user=%s", user_id,
            )
            return None

    async def restore(
        self,
        container: DockerContainer,
        snapshot_id: int,
    ) -> bool:
        """从 PG 取一份快照,gzip 解压,put_archive 进 /sandbox。
        失败返回 False(上层决定要不要继续)。"""
        if self._session_factory is None:
            logger.warning("snapshot restore skipped: database not initialized")
            return False
        try:
            async with self._session_factory() as session:
                repo = WorkspaceSnapshotRepo(session)
                snap = await repo.fetch(snapshot_id)
            if snap is None:
                logger.warning(
                    "snapshot restore: id=%d not found", snapshot_id,
                )
                return False
            # PG 里存的是 gzip 压缩的 tar,put_archive 要的是裸 tar
            tar_bytes = gzip.decompress(snap.tar_bytes)
            await container.put_archive("/sandbox", tar_bytes)
            logger.info(
                "snapshot restored: id=%d user=%s bytes=%d",
                snap.id, snap.user_id, len(tar_bytes),
            )
            return True
        except Exception:
            logger.exception(
                "snapshot restore failed snapshot_id=%d", snapshot_id,
            )
            return False

    async def restore_latest(self, container: DockerContainer, user_id: str) -> bool:
        """拿这个 user 最近的一份快照 restore。找不到或失败返回 False。"""
        if self._session_factory is None:
            return False
        try:
            async with self._session_factory() as session:
                repo = WorkspaceSnapshotRepo(session)
                snap = await repo.fetch_latest(user_id)
                snap_id = snap.id if snap is not None else None
        except Exception:
            logger.exception(
                "snapshot restore_latest: query failed user=%s", user_id,
            )
            return False
        if snap_id is None:
            return False
        return await self.restore(container, snap_id)


__all__ = ["WorkspaceSnapshotter"]