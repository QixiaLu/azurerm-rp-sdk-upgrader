"""Selectively load ai-assisted-development toolkit content into upgrade sessions.

Instead of installing the toolkit into the user's checkout (which pollutes their ``.github/`` and
makes the runtime auto-inject *everything*, including the review fleet), we read the toolkit's
content straight from the pinned ``submodule/aii`` **git submodule** and hand only an explicit
allow-list of files to the session:

- **instructions** -> concatenated into text that the upgrade prompt embeds (they are not skills,
  so ``skill_directories`` cannot load them),
- **skills** -> copied into a curated temp dir that is added to ``skill_directories``.

Only the files named in ``_INSTRUCTIONS`` / ``_SKILLS`` below are loaded. Nothing is written to
the user's checkout, so ``git status`` stays clean. When the submodule is absent (e.g. it wasn't
initialised) the loaders return empty/None and the upgrade proceeds without the toolkit.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

# The pinned toolkit git submodule (see .gitmodules; pinned to a release tag).
_PAYLOAD = Path(__file__).resolve().parent.parent / "submodule" / "aii" / ".github"

# Explicit allow-lists — only these are loaded into the upgrade session.
_INSTRUCTIONS = (
    "api-evolution-patterns.instructions.md",
    "azure-patterns.instructions.md",
    "error-patterns.instructions.md",
    "implementation-compliance-contract.instructions.md",
    "implementation-guide.instructions.md",
    "migration-guide.instructions.md",
)
_SKILLS = ("acceptance-testing",)

_curated_skills: Path | None = None


def instructions_text() -> str:
    """Concatenate the allow-listed ``*.instructions.md`` from the submodule, or ``""`` if absent."""
    d = _PAYLOAD / "instructions"
    if not d.is_dir():
        return ""
    parts: list[str] = []
    for name in _INSTRUCTIONS:
        f = d / name
        if f.is_file():
            parts.append(f"<!-- toolkit:instructions/{name} -->\n{f.read_text(encoding='utf-8')}")
    return "\n\n".join(parts)


def skills_dir() -> Path | None:
    """Curated dir holding the allow-listed submodule skills, for ``skill_directories`` (or None).

    Built once per process and cached; returns None when none of the allow-listed skills exist.
    """
    global _curated_skills
    if _curated_skills is not None:
        return _curated_skills
    src = _PAYLOAD / "skills"
    if not src.is_dir():
        return None
    dst = Path(tempfile.mkdtemp(prefix="aii-skills-"))
    copied = False
    for name in _SKILLS:
        if (src / name).is_dir():
            shutil.copytree(src / name, dst / name)
            copied = True
    if not copied:
        return None
    _curated_skills = dst
    return dst

