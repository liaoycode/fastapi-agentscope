from pydantic import BaseModel, Field


class SysUserRoleSchema(BaseModel):
    user_id: str = Field(None, description="用户id")
    role_id: str = Field(None, description="角色id")
    # ... 其他字段

    model_config = {"from_attributes": True}  # 支持 ORM 对象转换