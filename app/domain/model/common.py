# domain/models/base.py
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import DateTime, Boolean, BigInteger, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import event

from app.core.utils.snowflake import generate_snowflake_id
from app.core.utils.serializer_util import SerializerUtil


class Base(DeclarativeBase):
    pass
    # """所有 Model 的基类"""
    # __abstract__ = True
    #
    # # 方法1：实例方法
    # def to_dict(self, exclude=None):
    #     return SerializerUtil.serialize(self, exclude)
    #
    # @classmethod
    # def to_dicts(cls, objects, exclude=None):
    #     return SerializerUtil.serialize_list(objects, exclude)
    #
    # # 方法2：类方法 - 批量
    # @classmethod
    # def from_dict(cls, data):
    #     return SerializerUtil.deserialize(cls, data)
    #
    # @classmethod
    # def from_dicts(cls, data_list):
    #     return SerializerUtil.deserialize_list(cls, data_list)



class SnowflakeIDMixin:
    """雪花 ID 主键"""
    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        default=generate_snowflake_id,
        comment="主键ID"
    )


class TimestampMixin:
    """时间戳"""
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        nullable=False,
        comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="更新时间"
    )


class SoftDeleteMixin:
    """软删除"""
    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        index=True,
        comment="是否已删除"
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        comment="删除时间"
    )


# ========== 全局事件监听器（只需一个！） ==========

@event.listens_for(Base, 'before_insert', propagate=True)
def before_insert_handler(mapper, connection, target):
    """
    插入前统一处理（所有继承 Base 的 Model 都会触发）

    职责：
    1. 雪花 ID 兜底（如果 default 没生效）
    2. 时间戳兜底（如果 server_default 没生效）
    3. 软删除默认值
    4. 业务编号生成
    5. 数据校验
    """
    # ----- 1. 雪花 ID -----
    if hasattr(target, 'id') and target.id is None:
        target.id = generate_snowflake_id()

    # ----- 2. 时间戳（应用层控制，统一 UTC 时区） -----
    # 注意：即使 server_default 已经设置了，这里可以覆盖为应用层时间
    if hasattr(target, 'created_at'):
        target.created_at = datetime.now(timezone.utc)
    if hasattr(target, 'updated_at'):
        target.updated_at = datetime.now(timezone.utc)

    # ----- 3. 软删除默认值 -----
    if hasattr(target, 'is_deleted') and target.is_deleted is None:
        target.is_deleted = False

    # ----- 4. 业务编号生成（如果有） -----
    # 示例：订单编号
    # if hasattr(target, 'order_no') and target.order_no is None:
    #     target.order_no = f"ORD{datetime.now().strftime('%Y%m%d')}{generate_snowflake_id()}"

    # ----- 5. 数据校验（可选） -----
    # if hasattr(target, 'email') and target.email:
    #     if '@' not in target.email:
    #         raise ValueError("Invalid email format")


@event.listens_for(Base, 'before_update', propagate=True)
def before_update_handler(mapper, connection, target):
    """
    更新前统一处理

    职责：
    1. 更新时间戳（应用层控制）
    2. 软删除时间自动设置
    """
    # ----- 1. 更新时间戳 -----
    if hasattr(target, 'updated_at'):
        target.updated_at = datetime.now(timezone.utc)

    # ----- 2. 软删除时间 -----
    if hasattr(target, 'is_deleted') and hasattr(target, 'deleted_at'):
        # 当 is_deleted 变为 True，自动设置 deleted_at
        # 注意：这里要检测变化，可以用 mapper 的 modified 属性
        # 但简化处理：只要 is_deleted 为 True 且 deleted_at 为空，就设置
        if target.is_deleted and target.deleted_at is None:
            target.deleted_at = datetime.now(timezone.utc)