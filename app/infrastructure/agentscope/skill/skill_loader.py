"""Public + personal skill loaders, with personal overriding public by name."""

import logging
import os

from dotenv import load_dotenv
from agentscope.skill import LocalSkillLoader, Skill, SkillLoaderBase

from app.core.common.logger import getLogger
from app.core.config.env_config import settings
from app.infrastructure.agentscope.skill.personal import PersonalSkillLoader

# Load .env from the project root on import so any caller
# (main2.py, main.py, scripts) gets PG* / PERSONAL_SKILLS_CACHE_DIR
# without having to remember to call load_dotenv(). Real system env vars
# already present at process start take precedence over .env values.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=False)


# Toolkit._get_available_skills() prints "Duplicate skill name ... overwriting
# it" once per agent loop iteration, so a single chat request can produce
# ~14 identical lines. We dedupe at the logger level: only the first occurrence
# of each (name, group) tuple surfaces; repeats become DEBUG.
_seen_duplicate_keys: set[tuple[str, str, str]] = set()


class _DedupDuplicateWarningFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "Duplicate skill name" not in msg and "Duplicate tool name" not in msg:
            return True
        # Parse "Duplicate skill name 'X' found in group 'Y', overwriting it."
        try:
            after_kind = msg.split("Duplicate ", 1)[1]
            kind, rest = after_kind.split(" name ", 1)
            quoted_name = rest.split("'", 2)[1]
            group = rest.split("group '", 1)[1].split("'", 1)[0]
            key = (kind, quoted_name, group)
        except (IndexError, ValueError):
            return True  # can't parse -> don't drop, just in case
        if key in _seen_duplicate_keys:
            return False  # drop repeat
        _seen_duplicate_keys.add(key)
        return True


for _name in ("as",):
    _lg = getLogger()
    _lg.addFilter(_DedupDuplicateWarningFilter())


CURRENT_USER_ID = "demo-user"

DEFAULT_PUBLIC_DIR = settings.agent_skill_public_dir


class SandboxLocalSkillLoader(LocalSkillLoader):
    """``LocalSkillLoader`` 子类,把 ``Skill.dir`` 从 host 路径改写到
    sandbox 路径(``{container_dir}/{public_subdir}/<name>/``)。

    父类仍然负责 host disk 上的 ``SKILL.md`` 读取;这里只是改返回值的 ``dir``
    字段。LLM 拿到的 ``<dir>`` 是 sandbox 内的真实路径,可以直接导航。

    缓存键仍然用 host ``skill_root``(父类原样),所以重复 ``list_skills()``
    不会重复读文件 —— 唯一多出来的开销是把 ``Skill`` 重新构造一次。
    """

    async def list_skills(self) -> list[Skill]:
        try:
            skills = await super().list_skills()
        except Exception:
            return []
        sandbox_root = (
            f"{settings.agent_workspace_container_dir}"
            f"/{settings.agent_skill_public_subdir}"
        )
        rewritten: list[Skill] = []
        for s in skills:
            try:
                rel = os.path.relpath(s.dir, self.directory)
            except ValueError:
                # Windows 跨盘符 relpath 会抛 ValueError,这里走不到
                rel = ""
            if rel in (".", ""):
                rewritten_dir = f"{sandbox_root}/"
            else:
                rewritten_dir = f"{sandbox_root}/{rel}/"
            rewritten.append(
                Skill(
                    name=s.name,
                    description=s.description,
                    dir=rewritten_dir,
                    markdown=s.markdown,
                    updated_at=s.updated_at,
                ),
            )
        return rewritten


def build_skill_loaders(
    public_dir: str = DEFAULT_PUBLIC_DIR,
    user_id: str = CURRENT_USER_ID,
) -> list[SkillLoaderBase]:
    """Build the loader list for Toolkit(skills_or_loaders=...).

    Order matters: public first, personal second. Toolkit dedupes by
    Skill.name and lets later loaders override earlier ones, so any
    personal skill whose name matches a public one wins.

    public skill 从 host ``public_dir`` 读 SKILL.md,但 ``Skill.dir`` 被
    SandboxLocalSkillLoader 改写成 sandbox 路径。LLM 看到的 dir 跟实际
    sandbox 内目录对齐 —— 这是命名约定。
    """
    final_user_id = user_id if user_id else CURRENT_USER_ID
    return [
        SandboxLocalSkillLoader(directory=public_dir, scan_subdir=True),
        PersonalSkillLoader(user_id=final_user_id),
    ]


__all__ = [
    "CURRENT_USER_ID",
    "DEFAULT_PUBLIC_DIR",
    "LocalSkillLoader",
    "SandboxLocalSkillLoader",
    "PersonalSkillLoader",
    "build_skill_loaders",
]