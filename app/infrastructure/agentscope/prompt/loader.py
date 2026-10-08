"""Agent system prompt loader.

Each prompt is a markdown file under ``prompts/`` with YAML frontmatter and
the body becomes the ``system_prompt`` for the agent.

File format::

    ---
    name: complaint_analyst
    display_name: 投诉分析专家
    description: ...
    version: 1
    ---

    你是投诉分析专家...

The ``name`` in frontmatter (used as the lookup key for ``load_prompt()``)
is independent of the filename. A mismatch only emits a logger warning, so
``AGENT_SYSTEM_PROMPT=default_system`` (filename) and ``name: complaint_analyst``
(identifier) can coexist — the filename is what callers pass to
``load_prompt``, while ``Prompt.name`` reflects the frontmatter.

Loaded prompts are cached in-process — they are static after deploy, so we
do not re-read the file on every agent assembly. Call ``clear_cache()`` if
you need to pick up edits during development.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from app.core.common.logger import getLogger


@dataclass(frozen=True)
class Prompt:
    name: str
    display_name: str
    description: str
    content: str
    version: int


class PromptNotFoundError(FileNotFoundError):
    """Raised when a requested prompt file is missing."""


_PROMPTS_DIR = Path(__file__).parent / "prompts"

_cache: dict[str, Prompt] = {}

logger = getLogger()


def _parse(path: Path) -> Prompt:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"prompt file {path} missing YAML frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError(f"prompt file {path} has malformed frontmatter")
    fm = yaml.safe_load(parts[1]) or {}
    body = parts[2].strip()
    frontmatter_name = fm.get("name")
    if frontmatter_name and str(frontmatter_name) != path.stem:
        logger.warning(
            "prompt file %s: frontmatter name=%r differs from filename stem "
            "%r — callers should look up the file by filename",
            path,
            frontmatter_name,
            path.stem,
        )
    name = str(frontmatter_name or path.stem)
    return Prompt(
        name=name,
        display_name=str(fm.get("display_name") or name),
        description=str(fm.get("description") or ""),
        content=body,
        version=int(fm.get("version") or 1),
    )


def load_prompt(name: str) -> Prompt:
    """Load a prompt by name. Cached after first call."""
    cached = _cache.get(name)
    if cached is not None:
        return cached
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise PromptNotFoundError(
            f"prompt {name!r} not found at {path} "
            f"(available: {[p.stem for p in _PROMPTS_DIR.glob('*.md')]})",
        )
    parsed = _parse(path)
    _cache[name] = parsed
    return parsed


def list_prompts() -> list[Prompt]:
    """List all available prompts in the prompts directory."""
    return [
        load_prompt(p.stem)
        for p in sorted(_PROMPTS_DIR.glob("*.md"))
    ]


def clear_cache() -> None:
    """Drop the in-process cache. Useful for dev hot-reload or tests."""
    _cache.clear()