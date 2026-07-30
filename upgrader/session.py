"""Shared Copilot-session plumbing used by every upgrader stage.

One fresh session per turn, every agent-output event streamed to stdout (no per-agent log
files — ``result.json`` is the durable structured record), and a no-VCS guard that denies git
subcommands so all work stays unstaged in the working tree.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

# An agent turn can run for hours; don't let the SDK's 60s default cut it off.
TURN_TIMEOUT_SECONDS = 24 * 60 * 60

# git subcommands the agent must never run — all work stays unstaged in the working tree.
_DENIED_GIT = (
    "git commit", "git push", "git checkout", "git switch", "git branch",
    "git reset", "git stash", "git rebase", "git merge", "git tag",
)

# Event types whose content is the agent's own output; everything else (system prompt, prompt
# echo, skill bodies, lifecycle) is skipped by the logger.
_LOGGED_EVENT_TYPES = frozenset({"assistant.message", "assistant.reasoning"})
_DEBUG_REDACT_KEYS = ("token", "secret", "password", "authorization", "api_key", "apikey", "key")
_DEBUG_MAX_STRING = 400
_STDOUT_PREFIX = "[upgrader]"


# Read-only MCP servers the research-capable agents (upgrade, acctest collect) may call when
# confirming API breaking changes. Shared here so every stage registers the same set.
MCPS: dict[str, Any] = {
    "microsoft-learn": {
        "type": "http",
        "url": "https://learn.microsoft.com/api/mcp",
        "tools": ["*"],
    },
    "github": {
        "type": "http",
        "url": "https://api.githubcopilot.com/mcp/x/repos/readonly",
        "tools": ["*"],
    },
}


def fill_prompt(prompt_file: Path, replacements: dict[str, str]) -> str:
    """Read a prompt body and substitute ``<placeholder>`` tokens."""
    text = prompt_file.read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def _debug_now() -> int:
    return int(time.time())


def _truncate(value: str, limit: int = _DEBUG_MAX_STRING) -> str:
    if len(value) <= limit:
        return value
    return value[:max(0, limit - 16)] + "...(truncated)"


def _sanitize_debug(value: Any, *, key_hint: str = "") -> Any:
    key_l = key_hint.lower()
    if any(k in key_l for k in _DEBUG_REDACT_KEYS):
        return "***"
    if isinstance(value, dict):
        return {str(k): _sanitize_debug(v, key_hint=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_debug(v, key_hint=key_hint) for v in value]
    if isinstance(value, str):
        return _truncate(value)
    return value


def _emit_debug(enabled: bool, payload: dict[str, Any]) -> None:
    if not enabled:
        return
    print(f"{_STDOUT_PREFIX} {json.dumps(payload, sort_keys=True)}", flush=True)


def _no_vcs_hooks(*, debug_tools_log: bool = False,
                  session_label: str = "") -> dict[str, Any]:
    log_enabled = bool(debug_tools_log)

    def on_pre_tool_use(hook_input: Any, _inv: Any):
        # The SDK passes a TypedDict (a plain dict at runtime), so read it by key.
        # Fall back to attribute access in case a future SDK sends an object instead.
        def _field(*names: str) -> Any:
            for name in names:
                if isinstance(hook_input, dict):
                    if name in hook_input:
                        return hook_input[name]
                else:
                    val = getattr(hook_input, name, None)
                    if val is not None:
                        return val
            return None

        tool_name = _field("toolName", "name") or "unknown"
        args = _field("toolArgs", "arguments") or {}
        blob = " ".join(str(v) for v in (args.values() if isinstance(args, dict) else [args])).lower()
        decision = "allow"
        reason = ""
        for sub in _DENIED_GIT:
            if sub in blob:
                decision = "deny"
                reason = f"{sub} forbidden"
                break

        sanitized_args = _sanitize_debug(args)
        _emit_debug(log_enabled, {
                "event": "tool_usage",
                "ts": int(time.time()),
                "session": session_label,
                "tool": str(tool_name),
                "decision": decision,
                "reason": reason,
                "args": sanitized_args,
            })

        if isinstance(args, dict) and isinstance(args.get("command"), str):
            _emit_debug(log_enabled, {
                "event": "command_execution",
                "ts": _debug_now(),
                "session": session_label,
                "tool": str(tool_name),
                "command": _truncate(args.get("command", "")),
                "goal": _truncate(str(args.get("goal", ""))),
                "isBackground": bool(args.get("isBackground", False)),
            })

        if decision == "deny":
            return {"permissionDecision": "deny", "permissionDecisionReason": reason}
        return {"permissionDecision": "allow"}
    return {"on_pre_tool_use": on_pre_tool_use}


def _event_logger(*, debug_tools_log: bool = False):
    """Return ``(on_event, state)``.

    * debug: stream the whole agent transcript live, each line prefixed.
    * non-debug: emit nothing live; instead capture only the LAST
      ``assistant.message`` in ``state["final"]`` so ``run_session`` can print
      just the final text afterwards (CLI ``-s/--silent`` equivalent).
    """
    state: dict[str, str] = {"final": ""}

    def on_event(event: Any) -> None:
        etype = getattr(getattr(event, "type", None), "value", None)
        if etype not in _LOGGED_EVENT_TYPES:
            return
        content = getattr(getattr(event, "data", None), "content", None)
        if not content:
            return
        if debug_tools_log:
            for line in content.rstrip("\n").splitlines() or [""]:
                print(f"{_STDOUT_PREFIX} {line}", flush=True)
            return
        if etype == "assistant.message": 
            state["final"] = content

    return on_event, state


async def run_session(client: Any, prompt: str, *,
                      agent_config: dict[str, Any], agent_name: str,
                      model: str | None,
                      mcp_servers: dict[str, Any] | None = None,
                      debug_tools_log: bool = False) -> None:
    """Run one fresh Copilot session for the given agent."""
    from copilot import PermissionHandler  # type: ignore[import-not-found]

    session_label = f"{agent_name}:{_debug_now()}"
    hooks = _no_vcs_hooks(
        debug_tools_log=debug_tools_log,
        session_label=session_label)

    on_event, event_state = _event_logger(debug_tools_log=debug_tools_log)
    kwargs: dict[str, Any] = {
        "on_permission_request": PermissionHandler.approve_all,
        "on_event": on_event,
        "hooks": hooks,
        "enable_host_git_operations": False,
        "custom_agents": [agent_config],
        "agent": agent_name,
    }
    if model:
        kwargs["model"] = model
    if mcp_servers:
        kwargs["mcp_servers"] = mcp_servers
    session = await client.create_session(**kwargs)
    try:
        await session.send_and_wait(prompt, timeout=TURN_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — record and move on
        print(f"[DEBUG]  session error: {exc!r}", file=sys.stderr)
    finally:
        await session.disconnect()
        if not debug_tools_log:
            final = event_state.get("final", "").rstrip("\n")
            if final:
                print(final, flush=True)
