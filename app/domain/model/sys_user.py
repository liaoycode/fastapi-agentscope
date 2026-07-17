from typing import Optional
from sqlmodel import SQLModel, Field

class SysUserRole(SQLModel, table=True):
    __tablename__ = "sys_user_role"

    user_id: str = Field(primary_key=True, description="用户id")
    role_id: str = Field(description="角色id")