from sqlalchemy import select, func
from typing import List, Any

from app.domain.model.sys_user import SysUserRole
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.schema.common import PageParams, PageResult

"""
repo中实现各种数据库操作
"""
class SysUserRoleRepository:
    def __init__(self, session: AsyncSession):
        self.session = session


    async def get_by_id(self, user_id)-> List[SysUserRole]:
        result = await self.session.execute(
            select(SysUserRole).where(SysUserRole.user_id == user_id)
        )

        return list(result.scalars().all())

    async def paginate(
            self,
            page: PageParams,
            **filters
    ) -> PageResult[Any]:

        stmt = select(SysUserRole)


        # 等值过滤
        for key, value in filters.items():
            if value is not None and hasattr(SysUserRole, key):
                stmt = stmt.where(getattr(SysUserRole, key) == value)

        # 查询总数
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = await self.session.scalar(count_stmt)

        # 分页
        stmt = stmt.offset(page.skip).limit(page.size)
        result = await self.session.execute(stmt)
        items = result.scalars().all()

        return PageResult.of(items, total, page)
