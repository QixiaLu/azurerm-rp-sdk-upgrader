"""Per-role agent definitions.

Each role is defined by a single markdown agent file (``<role>.md``) that is loaded
verbatim as the agent's system prompt — the same "one agent per role" shape
proven out by the agent-assist squad. The agent file owns the durable role: who the
agent is, its full procedure, its boundaries, and its ``result.json`` contract.
The per-turn task prompt (``prompts/*.md``) carries only the dynamic inputs for
one turn (rp/version/build-errors/phase), exactly as agent-assist's loop prompt
is separate from its agents.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_DIR = Path(__file__).resolve().parent

__all__ = ["load_agent", "AGENT_DIR"]

AGENT_DIR = _DIR


@lru_cache(maxsize=None)
def load_agent(name: str) -> str:
    """Return the agent markdown for ``name`` (e.g. ``"upgrade"``, ``"test"``)."""
    return (_DIR / f"{name}.md").read_text(encoding="utf-8")
