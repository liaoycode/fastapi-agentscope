from fastapi import APIRouter, Depends, Path
from sqlmodel import select, func
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.common.repeat_submit_guard import no_repeat_submit
from app.domain.schema.api_response import ApiResponse
from app.domain.model.sys_user import SysUserRole
from app.domain.schema.page_schema import PageRequest, PageResponse
from app.infrastructure.datasource.database import get_session
from app.infrastructure.redis.redis_client import redis_client

router = APIRouter(prefix="/user", tags=["用户模块"])


# 使用
@router.get("/page", response_model=ApiResponse[PageResponse[SysUserRole]], dependencies=[Depends(no_repeat_submit(expire_seconds=5))])
async def list_users(page: PageRequest = Depends(),
                     session: AsyncSession = Depends(get_session)
                     ):
    total = await session.scalar(select(func.count(SysUserRole.user_id)))
    users = await session.exec(select(SysUserRole).offset(page.skip).limit(page.size))
    return ApiResponse.ok(PageResponse.of(users.all(), total, page))


@router.get("/{user_id}", response_model=ApiResponse[list[SysUserRole]])
async def get_report(user_id: str = Path(description="用户id"), session: AsyncSession = Depends(get_session)):
    from sqlmodel import select
    sys_user_role = await session.exec(
        select(SysUserRole).where(SysUserRole.user_id == user_id)
    )

    value = await redis_client.get("sys_dict:MODULE")
    print(value)
    # raise BusinessException(code=4000, message="ceshi")
    return ApiResponse.ok(sys_user_role.all())


