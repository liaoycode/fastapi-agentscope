from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils.serializer_util import SerializerUtil
from app.domain.model.sys_user import SysUserRole
from app.infrastructure.redis.redis_client import redis_client
from app.infrastructure.repositories.sys_user_role_repo import SysUserRoleRepository

"""
service中编排各种业务逻辑
"""
class SysUserRoleService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.user_repo = SysUserRoleRepository(session)


    async def get_user_roles(self, user_id):
        user_roles = await redis_client.get(f"sys_dict:MODULE")
        if user_roles:
            print(user_roles)


        user_roles = await self.user_repo.get_by_id(user_id)
        print(user_roles)
        print(SerializerUtil.serialize_list(user_roles))
        return user_roles
