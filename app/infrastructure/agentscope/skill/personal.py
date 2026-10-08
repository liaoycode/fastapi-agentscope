"""PersonalSkillLoader: reads per-user skills from Postgres.

list_skills 返回 in-memory ``Skill``,**不写 disk**。``Skill.dir`` 指向
sandbox 内路径(``{container_dir}/{personal_subdir}/<name>/``)—— LLM 拿到这个
dir 可以直接 ``cd`` 进去、``ls scripts/`` 看到脚本。markdown 字段描述 skill
语义,LLM 据此决定调用方式。

materialize_all 是单独的"写盘"路径,只给 ``SkillSyncedWorkspace._stage_and_push_skills``
的 staging→put_archive 流水线用:把 SKILL.md / scripts/ 写到 staging 目录,
tar → put_archive 到 sandbox 命名卷。``list_skills`` 不依赖这条路径。
"""
import asyncio
import os
import time
from pathlib import Path

from sqlalchemy import text

from agentscope.skill import Skill, SkillLoaderBase
from agentscope._logging import logger

from app.core.config.env_config import settings
from app.infrastructure.datasource import database as db


_SKILL_QUERY = """
    SELECT skill_name, description, markdown, updated_at
    FROM personal_skill
    WHERE user_id = :user_id
"""

_SCRIPT_QUERY = """
    SELECT script_path, content
    FROM personal_skill_script
    WHERE user_id = :user_id AND skill_name = :skill_name
"""


class PersonalSkillLoader(SkillLoaderBase):
    """Loads per-user skills from Postgres.

    list_skills 不写 disk,纯内存构造 Skill。Skill.dir 指向 sandbox 路径,
    LLM 拿到后可导航到 sandbox 内的真实目录。

    materialize_all 写 disk,但写到调用方传入的 target_root(per-sandbox 隔离
    的 staging 目录),用于 sandbox push。
    """

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    @property
    def sandbox_personal_dir(self) -> str:
        """sandbox 内 personal skill 根目录。LLM 通过 Skill.dir 走到这里。"""
        return (
            f"{settings.agent_workspace_container_dir}"
            f"/{settings.agent_skill_personal_subdir}"
        )

    async def close(self) -> None:
        return None

    async def list_skills(self) -> list[Skill]:
        if db._session_factory is None:
            logger.warning(
                "PersonalSkillLoader: database not initialized. "
                "Falling back to public skills only.",
            )
            return []

        skills: list[Skill] = []
        try:
            async with db._session_factory() as session:
                rows = (
                    await session.execute(
                        text(_SKILL_QUERY),
                        {"user_id": self.user_id},
                    )
                ).mappings().all()
                for row in rows:
                    updated_at = row["updated_at"]
                    updated_epoch = (
                        updated_at.timestamp() if updated_at else time.time()
                    )
                    skills.append(
                        Skill(
                            name=row["skill_name"],
                            description=row["description"],
                            dir=f"{self.sandbox_personal_dir}/{row['skill_name']}/",
                            markdown=row["markdown"],
                            updated_at=updated_epoch,
                        ),
                    )
        except Exception as e:
            logger.warning(
                "PersonalSkillLoader: query failed (%s). "
                "Falling back to public skills only.",
                e,
            )
            return []

        return skills

    async def materialize_all(self, target_root: Path) -> int:
        """把 ``user_id`` 的 personal skill 物化到 ``target_root/<skill_name>/``
        (不嵌 user_id —— target_root 本身就是 per-sandbox 隔离的)。

        给 SkillSyncedWorkspace 的 staging→put_archive 流水线用。
        返回成功物化的 skill 数;DB 不可达时返回 0。
        """
        if db._session_factory is None:
            logger.warning(
                "PersonalSkillLoader.materialize_all: database not initialized",
            )
            return 0
        target_root.mkdir(parents=True, exist_ok=True)
        n = 0
        try:
            async with db._session_factory() as session:
                rows = (
                    await session.execute(
                        text(_SKILL_QUERY),
                        {"user_id": self.user_id},
                    )
                ).mappings().all()
                for row in rows:
                    if await self._materialize_to(session, row, target_root) is not None:
                        n += 1
        except Exception as e:
            logger.warning(
                "PersonalSkillLoader.materialize_all failed (%s)", e,
            )
            return n
        return n

    async def _materialize_to(
        self,
        session,
        row: dict,
        target_root: Path,
    ) -> Skill | None:
        """写 SKILL.md + scripts/ 到 target_root/<skill_name>/。失败返回 None。"""
        skill_name: str = row["skill_name"]
        description: str = row["description"]
        markdown: str = row["markdown"]
        updated_at = row["updated_at"]

        skill_dir = target_root / skill_name
        skill_md_path = skill_dir / "SKILL.md"
        scripts_dir = skill_dir / "scripts"

        try:
            skill_dir.mkdir(parents=True, exist_ok=True)
            scripts_dir.mkdir(exist_ok=True)

            skill_md_content = (
                f"---\n"
                f"name: {skill_name}\n"
                f"description: {description}\n"
                f"---\n\n"
                f"{markdown}\n"
            )

            updated_epoch = updated_at.timestamp() if updated_at else time.time()
            await self._write_if_newer(skill_md_path, skill_md_content, updated_epoch)

            scripts = (
                await session.execute(
                    text(_SCRIPT_QUERY),
                    {"user_id": self.user_id, "skill_name": skill_name},
                )
            ).mappings().all()
            for srow in scripts:
                rel = srow["script_path"]
                if not rel.startswith("scripts/"):
                    logger.warning(
                        "PersonalSkillLoader: refusing script path %r "
                        "(must start with 'scripts/')",
                        rel,
                    )
                    continue
                target = skill_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                await self._write_if_newer(target, srow["content"], updated_epoch)

            return Skill(
                name=skill_name,
                description=description,
                dir=str(skill_dir),
                markdown=markdown,
                updated_at=updated_epoch,
            )
        except OSError as e:
            logger.warning(
                "PersonalSkillLoader: failed to materialize skill %r: %s",
                skill_name, e,
            )
            return None

    @staticmethod
    async def _write_if_newer(
        path: Path,
        content: str,
        updated_epoch: float,
    ) -> None:
        """Write ``content`` to ``path`` only if DB updated_at is newer
        than the existing file's mtime."""
        try:
            current_mtime = path.stat().st_mtime
            if current_mtime >= updated_epoch:
                return
        except FileNotFoundError:
            pass

        await asyncio.to_thread(path.write_text, content, encoding="utf-8")
        os.utime(path, (updated_epoch, updated_epoch))