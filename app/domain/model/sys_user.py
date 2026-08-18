from typing import Optional

from app.domain.model.common import Base, SnowflakeIDMixin, TimestampMixin, SoftDeleteMixin
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import (
    String,        # 字符串
    Integer,       # 整数
    BigInteger,    # 大整数（雪花 ID 用）
    Boolean,       # 布尔值
    DateTime,      # 日期时间
    Date,          # 日期
    Time,          # 时间
    Float,         # 浮点数
    Numeric,       # 精确小数
    Text,          # 长文本
    Enum,          # 枚举
    JSON,          # JSON 字段
    ForeignKey,    # 外键
    Index,         # 索引
    UniqueConstraint,  # 唯一约束
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

# class SysUserRole(Base, SnowflakeIDMixin, TimestampMixin, SoftDeleteMixin)
class SysUserRole(Base):
    __tablename__ = "sys_user_role"

    user_id: Mapped[str] = mapped_column(String(100), unique=False, nullable=False, primary_key=True, comment="用户唯一标识ID")
    role_id: Mapped[str] = mapped_column(String(100), unique=False, nullable=False, primary_key=True, comment="角色id")