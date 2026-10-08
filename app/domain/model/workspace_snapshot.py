"""WorkspaceSnapshot: 沙箱 workspace 字节级快照,跟命名卷互为冗余备份。

写入:UserSandboxManager.close() 在杀容器前 save 一份。
读取:SkillSyncedWorkspace.initialize() 在容器起来后 restore 最新一份。

tar_bytes 是 gzip 压缩的 tar 流,排除了 skills / personal_skills 两个子目录
(那两个目录在 sandbox 起来时由 skill push 重新填充,没必要占快照体积)。
"""
from datetime import datetime

from sqlalchemy import BigInteger, LargeBinary, Text, TIMESTAMP, func
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.model.common import Base


class WorkspaceSnapshot(Base):
    __tablename__ = "user_workspace_snapshot"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True,
        comment="BIGSERIAL",
    )
    user_id: Mapped[str] = mapped_column(
        Text, nullable=False, index=True,
    )
    label: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="auto-pre-close / manual / NULL",
    )
    tar_bytes: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False,
        comment="gzip tar of /sandbox, 排除 skills/personal_skills",
    )
    size_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False, server_default=func.now(),
    )


__all__ = ["WorkspaceSnapshot"]