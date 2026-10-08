from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.model.chat_session_state import ChatSessionState


class ChatSessionStateRepository:
    """ChatSessionState 仓储：load / upsert。

    调用方应通过 Depends(get_session) 注入 session，仓储只负责 SQL，
    不负责事务边界（get_session 已包好 commit / rollback）。
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_session_id(
        self, session_id: str
    ) -> ChatSessionState | None:
        result = await self.session.execute(
            select(ChatSessionState).where(
                ChatSessionState.session_id == session_id
            )
        )
        return result.scalar_one_or_none()

    async def list_by_user(
        self, user_id: str, limit: int = 100
    ) -> list[ChatSessionState]:
        result = await self.session.execute(
            select(ChatSessionState)
            .where(ChatSessionState.user_id == user_id)
            .order_by(ChatSessionState.updated_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def upsert(
        self,
        session_id: str,
        user_id: str,
        state_json: dict,
    ) -> None:
        """同 session_id 覆盖写；新建时 created_at/updated_at 都填当前时间。

        TimestampMixin 的 onupdate=func.now() 不会在 on_conflict_do_update
        里自动触发，所以这里显式写 updated_at；created_at 用 PG 的
        COALESCE 让 upsert 保留旧值。

        用 tz-naive UTC 对齐 TimestampMixin 的 DateTime 列（不带 timezone）。
        """
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        stmt = pg_insert(ChatSessionState).values(
            session_id=session_id,
            user_id=user_id,
            state_json=state_json,
            created_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ChatSessionState.session_id],
            set_={
                "state_json": stmt.excluded.state_json,
                "user_id": stmt.excluded.user_id,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await self.session.execute(stmt)