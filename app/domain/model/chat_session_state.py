from sqlalchemy import String, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.model.common import Base, TimestampMixin


class ChatSessionState(Base, TimestampMixin):
    """按 session_id 持久化 AgentScope 的 AgentState（含 context / summary / tool cache 等）。

    写入时机：每轮 chat_stream 结束后；读取时机：每轮进入前。
    命中 upsert 语义，同 session_id 第二次写覆盖第一次（state_json 全量替换）。
    """

    __tablename__ = "chat_session_state"

    session_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        comment="会话唯一标识，由调用方传入（uuid.hex 或业务侧 ID）",
    )

    user_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="所属用户，便于按用户排查；当前不强制隔离",
    )

    state_json: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        comment="AgentState.model_dump(mode='json') 的全量快照",
    )

    __table_args__ = (
        Index("ix_chat_session_user_updated", "user_id", "updated_at"),
    )