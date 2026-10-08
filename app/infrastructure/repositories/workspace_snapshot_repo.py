"""WorkspaceSnapshot 仓储。

写入:UserSandboxManager.close() 在关容器前调 insert。
读取:SkillSyncedWorkspace.initialize() 调 fetch_latest 决定要不要 restore。
滚动:save 之前调 purge_auto_keep_n,只清 auto-pre-close,manual 留着不动。

注意 bytea 在 asyncpg 下要显式用 LargeBinary 列;sa 1.4+ 会自动用 bytea,
无需 .dialect 相关特殊处理。
"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.model.workspace_snapshot import WorkspaceSnapshot


class WorkspaceSnapshotRepo:
    """按 user_id / label 维度的快照 CRUD。

    调用方应通过 Depends(get_session) 注入 session,仓储只负责 SQL,
    不负责事务边界(get_session 已包好 commit / rollback)。
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def insert(
        self,
        user_id: str,
        label: str | None,
        tar_bytes: bytes,
        size_bytes: int,
    ) -> int:
        row = WorkspaceSnapshot(
            user_id=user_id,
            label=label,
            tar_bytes=tar_bytes,
            size_bytes=size_bytes,
        )
        self.session.add(row)
        await self.session.flush()
        return row.id

    async def fetch(self, snapshot_id: int) -> WorkspaceSnapshot | None:
        result = await self.session.execute(
            select(WorkspaceSnapshot).where(WorkspaceSnapshot.id == snapshot_id),
        )
        return result.scalar_one_or_none()

    async def fetch_latest(self, user_id: str) -> WorkspaceSnapshot | None:
        """拿这个 user 最近的快照,任何 label 都算(不分 auto / manual)。"""
        result = await self.session.execute(
            select(WorkspaceSnapshot)
            .where(WorkspaceSnapshot.user_id == user_id)
            .order_by(WorkspaceSnapshot.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def fetch_latest_with_label(
        self, user_id: str, label: str,
    ) -> WorkspaceSnapshot | None:
        result = await self.session.execute(
            select(WorkspaceSnapshot)
            .where(WorkspaceSnapshot.user_id == user_id)
            .where(WorkspaceSnapshot.label == label)
            .order_by(WorkspaceSnapshot.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_by_user(
        self, user_id: str, limit: int = 20,
    ) -> list[WorkspaceSnapshot]:
        result = await self.session.execute(
            select(WorkspaceSnapshot)
            .where(WorkspaceSnapshot.user_id == user_id)
            .order_by(WorkspaceSnapshot.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def purge_auto_keep_n(self, user_id: str, n: int) -> int:
        """保留同一 user 的 ``auto-pre-close`` 最近 n 条,删更早的。

        之所以只针对 ``auto-pre-close``:manual 是用户主动存的存档,不应该被
        自动清理(用户预期 manual 是有意识的快照,丢了会骂街)。其它 label
        (NULL 等)目前没有写入路径,以后要加再扩。

        n=0 表示全删;n 不为 0 时按 created_at desc 取前 n,删剩下的。
        """
        if n < 0:
            n = 0
        # 先找出要保留的 id 集合
        keep_result = await self.session.execute(
            select(WorkspaceSnapshot.id)
            .where(WorkspaceSnapshot.user_id == user_id)
            .where(WorkspaceSnapshot.label == "auto-pre-close")
            .order_by(WorkspaceSnapshot.created_at.desc())
            .limit(n)
        )
        keep_ids = {row[0] for row in keep_result.all()}
        if not keep_ids and n > 0:
            # 全部都保留,没东西删
            return 0

        # 删不在 keep_ids 里的 auto-pre-close
        from sqlalchemy import and_
        stmt = delete(WorkspaceSnapshot).where(
            and_(
                WorkspaceSnapshot.user_id == user_id,
                WorkspaceSnapshot.label == "auto-pre-close",
            )
        )
        if keep_ids:
            stmt = stmt.where(WorkspaceSnapshot.id.notin_(keep_ids))
        result = await self.session.execute(stmt)
        return result.rowcount or 0


__all__ = ["WorkspaceSnapshotRepo"]