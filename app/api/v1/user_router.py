from fastapi import APIRouter, Depends, Path


from app.core.security.repeat_submit_guard import no_repeat_submit
from app.domain.schema.common import PageParams, PageResult, ApiResponse
from app.domain.schema.sys_user import SysUserRoleSchema
from app.domain.services.sys_user_role_service import SysUserRoleService
from app.infrastructure.datasource.database import get_session
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.repositories.sys_user_role_repo import SysUserRoleRepository

router = APIRouter(prefix="/user", tags=["用户模块"])


# 使用
@router.get("/page", response_model=ApiResponse[PageResult[SysUserRoleSchema]], dependencies=[Depends(no_repeat_submit(expire_seconds=5))])
async def list_users(page: PageParams = Depends(),
                     session: AsyncSession = Depends(get_session)
                     ):
    """简单查询、增删可直接调用repo层"""
    sys_user_repo = SysUserRoleRepository(session)
    return ApiResponse.ok(await sys_user_repo.paginate(page))



@router.get("/{user_id}", response_model=ApiResponse[list[SysUserRoleSchema]])
async def get_report(user_id: str = Path(description="用户id"), session: AsyncSession = Depends(get_session)):
    # from sqlmodel import select
    # sys_user_role = await session.exec(
    #     select(SysUserRole).where(SysUserRole.user_id == user_id)
    # )
    #
    # value = await redis_client.get("sys_dict:MODULE")
    # print(value)
    # raise BusinessException(code=4000, message="ceshi")
    sys_user_role_service = SysUserRoleService(session)
    return ApiResponse.ok(await sys_user_role_service.get_user_roles(user_id))


