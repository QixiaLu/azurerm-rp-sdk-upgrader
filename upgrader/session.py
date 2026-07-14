"""Shared Copilot-session plumbing used by every upgrader stage.

One fresh session per turn, every event streamed to a per-turn log file, and a no-VCS
guard that denies git subcommands so all work stays unstaged in the working tree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# An agent turn can run for hours; don't let the SDK's 60s default cut it off.
TURN_TIMEOUT_SECONDS = 24 * 60 * 60

# Bundled skills the agents read: .github/skills/<name> lives at the repo root.
SKILLS_DIR = Path(__file__).resolve().parent.parent / ".github" / "skills"

# git subcommands the agent must never run — all work stays unstaged in the working tree.
_DENIED_GIT = (
    "git commit", "git push", "git checkout", "git switch", "git branch",
    "git reset", "git stash", "git rebase", "git merge", "git tag",
)


def read_result(result_path: Path) -> dict[str, Any]:
    """Read an agent's JSON result sidecar, or {} if it is missing/unparsable."""
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def fill_prompt(prompt_file: Path, replacements: dict[str, str]) -> str:
    """Read a prompt body and substitute ``<placeholder>`` tokens."""
    text = prompt_file.read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def _no_vcs_hooks() -> dict[str, Any]:
    def on_pre_tool_use(hook_input: Any, _inv: Any):
        args = getattr(hook_input, "toolArgs", None) or {}
        blob = " ".join(str(v) for v in (args.values() if isinstance(args, dict) else [args])).lower()
        for sub in _DENIED_GIT:
            if sub in blob:
                return {"permissionDecision": "deny", "permissionDecisionReason": f"{sub} forbidden"}
        return {"permissionDecision": "allow"}
    return {"on_pre_tool_use": on_pre_tool_use}


def _event_logger(log_path: Path | None):
    """Open a per-turn log file and return (on_event, write, close) recording every event.

    When ``log_path`` is None (non-verbose runs), nothing is written to disk and all three
    returned callables are no-ops — only the agents' ``result.json`` sidecars are kept.
    """
    if log_path is None:
        return (lambda _event: None), (lambda _text: None), (lambda: None)

    log_fh = log_path.open("w", encoding="utf-8")

    def write(text: str) -> None:
        log_fh.write(text if text.endswith("\n") else text + "\n")
        log_fh.flush()

    def on_event(event: Any) -> None:
        data = getattr(event, "data", None)
        content = getattr(data, "content", None)
        if content:
            write(content)

    return on_event, write, log_fh.close


async def run_session(client: Any, prompt: str, log_path: Path, *,
                      agent_config: dict[str, Any], agent_name: str,
                      model: str | None, verbose: bool = False) -> None:
    """Run one fresh Copilot session for the given agent.

    When ``verbose`` is True, every event is streamed to ``log_path``; otherwise no log file
    is written and only the agent's ``result.json`` sidecar is kept.
    """
    from copilot import PermissionHandler  # type: ignore[import-not-found]

    on_event, write, close = _event_logger(log_path if verbose else None)
    kwargs: dict[str, Any] = {
        "on_permission_request": PermissionHandler.approve_all,
        "on_event": on_event,
        "hooks": _no_vcs_hooks(),
        "enable_host_git_operations": False,
        "custom_agents": [agent_config],
        "agent": agent_name,
    }
    if SKILLS_DIR.is_dir():
        kwargs["skill_directories"] = [str(SKILLS_DIR)]
    if model:
        kwargs["model"] = model
    session = await client.create_session(**kwargs)
    try:
        await session.send_and_wait(prompt, timeout=TURN_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — record and move on
        write(f"[exception] {exc!r}")
        # Also surface it on the console; otherwise a turn that fails immediately (auth, model
        # id, empty/unmounted repo) is invisible and the loop silently burns iterations.
        hint = f" (see {log_path})" if verbose else ""
        print(f"  session error: {exc!r}{hint}")
    finally:
        await session.disconnect()
        close()
