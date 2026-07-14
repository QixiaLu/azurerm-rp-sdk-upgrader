"""Acceptance-test triage stage: a cheap smoke gate, then the full suite. Fresh-only.

To avoid burning many hours on a full run that a trivial regression would have caught, the
stage runs in two phases, each a launch -> wait -> collect round loop:

  1. SMOKE — only the representative tests (each resource's ``_basic``).
     Triage and fix here until green; this is fast and catches most upgrade breakage.
  2. FULL — the whole suite, run ONLY once smoke is green.

Each round re-runs its phase's suite (so prior-round fixes are re-verified), and the loop stops
early once a round finishes triage with no new fixes. State lives on disk; sessions never
re-attach to a prior run.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from upgrader.session import fill_prompt, read_result, run_session

LAUNCH_PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "PROMPT_ACCTEST_LAUNCH.md"
COLLECT_PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "PROMPT_ACCTEST_COLLECT.md"

# Detached acctest runs can take many hours; poll their PID at this cadence.
POLL_SECONDS = 60
# Emit a heartbeat line every Nth poll while waiting.
HEARTBEAT_EVERY = 5

LAUNCH_AGENT_NAME = "rp-acctest-launch"
COLLECT_AGENT_NAME = "rp-acctest-collect"

# Which tests make up the cheap smoke gate: every resource's basic-CRUD and requires-import
# tests. The launch agent intersects this with test_regex to build the smoke -run filter.
SMOKE_SELECTOR = "_basic tests (basic CRUD + import) for each resource"

# Launch agent: capture the TeamCity main baseline, then start a detached `make acctests` run
# and exit — it never waits for results (a suite can take many hours).
LAUNCH_AGENT_CONFIG = {
    "name": LAUNCH_AGENT_NAME,
    "display_name": "RP Acctest Launch",
    "description": "Capture the TeamCity baseline and start a detached acceptance-test run, then exit.",
    "skills": [LAUNCH_AGENT_NAME],
    "prompt": (
        "You are a senior Terraform provider engineer launching acceptance tests after an API "
        "upgrade. Never block — tests take hours. Capture the TeamCity main baseline (reuse it "
        "if already present), start a detached `make acctests` run for the chosen phase, record "
        "run.pid, and EXIT. Do not wait for results. Never modify go.mod/vendor."
    ),
}

# Collect agent: diff NEW failures vs the baseline, fix test-side breakages in *_test.go, and
# mitigate genuine breaking changes in resource/schema code strictly per the guide.
COLLECT_AGENT_CONFIG = {
    "name": COLLECT_AGENT_NAME,
    "display_name": "RP Acctest Collect",
    "description": "Triage a finished acceptance-test run: diff against baseline, fix test-side failures, mitigate breaking changes.",
    "skills": [COLLECT_AGENT_NAME],
    "prompt": (
        "You are a senior Terraform provider engineer triaging a finished acceptance-test run. "
        "Compute NEW failures = local FAIL minus the TeamCity baseline, fix test-side issues in "
        "*_test.go, and mitigate genuine breaking changes in resource/schema code STRICTLY per "
        "contributing/topics/guide-breaking-changes.md (feature-flag gated + upgrade-guide "
        "entry), reporting each with evidence. Never modify go.mod/vendor."
    ),
}


def _build_prompt(prompt_file: Path, rp_name: str, *, test_regex: str,
                  acctest_dir: Path, result_path: Path, phase: str, phase_scope: str) -> str:
    """Substitute placeholders into an acctest launch/collect prompt body."""
    return fill_prompt(prompt_file, {
        "<rp_name>": rp_name,
        "<test_regex>": test_regex,
        "<acctest_dir>": acctest_dir.as_posix(),
        "<result_path>": result_path.as_posix(),
        "<phase>": phase,
        "<phase_scope>": phase_scope,
    })


def _fixes_applied(result: dict[str, Any]) -> bool:
    """True if the last collect turn changed any code (test-side fix or breaking-change
    mitigation) and so warrants a fresh full-suite re-run to re-verify."""
    if result.get("fixed"):
        return True
    return any(bc.get("mitigated") for bc in result.get("breaking_changes", []))


# Phase outcomes. Two orthogonal axes are collapsed into one enum ON PURPOSE, but only along
# the control-flow axis (continue vs stop). The result/report axis stays richer and is NOT
# flattened: `api_bug` (a real detection — Azure contradicts its own spec) is kept distinct
# from `tool_error` (the agent/infra itself failed — not a finding). Per-test detail
# (fixed / breaking_changes / api_bugs / needs_human) always lives in the collect result.json.
FIXED = "fixed"              # code changed this round -> re-verify with another round (CONTINUE)
CLEAN = "clean"              # converged: suite green, nothing left to change (STOP, success)
API_BUG = "api-bug"          # >=1 blocked: Azure API bug detected -> STOP, report the finding
TOOL_ERROR = "tool-error"    # agent/infra failure -> STOP, not a finding
NEEDS_HUMAN = "needs-human"  # only non-code failures left (env/quota/flake/deferred) -> STOP
INCOMPLETE = "incomplete"    # used up max_rounds while still applying fixes -> STOP

# The collect agent declares its control-flow intent as ``loop_action`` (see
# PROMPT_ACCTEST_COLLECT.md). Python trusts that declaration and only maps it to an outcome;
# the verbose per-test reasoning that produced it stays in the agent/skill, not here.
_LOOP_ACTION_TO_OUTCOME = {
    "reverify": FIXED,        # applied fixes (or triage unfinished) -> run another round
    "converged": CLEAN,       # nothing left to change; suite green
    "api_bug": API_BUG,       # Azure API contradicts its own spec -> stop, it's a finding
    "tool_error": TOOL_ERROR,  # the run/agent itself failed -> stop, not a finding
    "needs_human": NEEDS_HUMAN,  # only env/quota/flake/deferred remain -> stop
}


def _outcome(result: dict[str, Any]) -> str:
    """Classify one finished collect round into a phase outcome — the single control-flow
    signal for the launch->wait->collect loop. Only ``FIXED`` continues the loop.

    The classification is OWNED BY THE COLLECT AGENT, which declares it explicitly as
    ``loop_action`` in its result (the agent understands the Go/Azure failure semantics;
    Python does not). This function just maps that declaration to a phase outcome. If the
    field is missing or unrecognized (older/partial result), it falls back to deriving the
    outcome from the individual signals so a dropped field never stalls the loop."""
    mapped = _LOOP_ACTION_TO_OUTCOME.get(result.get("loop_action"))
    if mapped is not None:
        return mapped
    return _fallback_outcome(result)


def _fallback_outcome(result: dict[str, Any]) -> str:
    """Defensive derivation of the phase outcome when the agent did not declare a usable
    ``loop_action`` — reconstructs it from status/api_bugs/fixed/breaking_changes."""
    status = result.get("status")
    if status == "blocked" or result.get("api_bugs"):
        return API_BUG
    if status == "failed":
        return TOOL_ERROR
    if _fixes_applied(result):
        return FIXED
    if status == "done":
        return CLEAN
    # Not done, but nothing changed and no hard stop: only non-actionable failures remain
    # (env/quota/flake) or breaking changes deferred to a human. Re-running the identical
    # suite would just reproduce them, so stop rather than burn another multi-hour run.
    return NEEDS_HUMAN


# --- detached-run watch (PID-based, fresh-only: never re-attaches) -------------------

def _read_pid(pid_file: Path) -> int | None:
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check that works on POSIX and Windows."""
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover - platform-specific
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return False
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A finished detached run that reparented to PID 1 (python) can linger as a
    # zombie that os.kill still "sees" until reaped — which never happens when
    # python is PID 1. Treat a zombie (/proc state 'Z') as not alive so the
    # watcher can move on to collect. Works with or without `docker run --init`.
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            data = fh.read()
        if data[data.rfind(")") + 2] == "Z":
            return False
    except (OSError, IndexError):
        pass
    return True


def _finished(acctest_dir: Path) -> bool:
    """One non-blocking poll: has the detached `make acctests` run ended?"""
    pid = _read_pid(acctest_dir / "run.pid")
    if pid is None:
        # No PID yet means the launch agent hasn't recorded one; treat as not finished
        # unless a final run.json already exists.
        return (acctest_dir / "run.json").exists()
    return not _pid_alive(pid)


async def _wait(acctest_dir: Path, log) -> None:
    """Block-poll (async) until the detached run's PID is gone, logging a heartbeat."""
    polls = 0
    while not _finished(acctest_dir):
        if polls % HEARTBEAT_EVERY == 0:
            log(f"still waiting ({polls * POLL_SECONDS // 60}m elapsed)")
        polls += 1
        await asyncio.sleep(POLL_SECONDS)
    log("detached run finished")


async def _run_phase(client, run_dir: Path, acctest_dir: Path, *, rp_name: str,
                     test_regex: str, phase: str, phase_scope: str, max_rounds: int,
                     model: str | None, verbose: bool = False) -> str:
    """Run one phase's (launch -> wait -> collect/fix) round loop.

    Returns a phase outcome (see the module's outcome constants): ``CLEAN`` when the phase
    converged (suite green, nothing left to change), ``API_BUG`` / ``TOOL_ERROR`` /
    ``NEEDS_HUMAN`` for the distinct stop reasons, or ``INCOMPLETE`` when it ran out of rounds
    while still applying fixes. Only ``CLEAN`` is success.
    """
    launch_result = run_dir / f"acctest.{phase}.launch.result.json"
    collect_result = run_dir / f"acctest.{phase}.result.json"
    launch_prompt = _build_prompt(
        LAUNCH_PROMPT_FILE, rp_name, test_regex=test_regex, acctest_dir=acctest_dir,
        result_path=launch_result, phase=phase, phase_scope=phase_scope)
    collect_prompt = _build_prompt(
        COLLECT_PROMPT_FILE, rp_name, test_regex=test_regex, acctest_dir=acctest_dir,
        result_path=collect_result, phase=phase, phase_scope=phase_scope)

    for rnd in range(1, max_rounds + 1):
        print(f"=== acctest [{phase}] round {rnd}/{max_rounds}: launch ===")
        await run_session(client, launch_prompt, run_dir / f"acctest.{phase}.launch.r{rnd:02d}.log",
                          agent_config=LAUNCH_AGENT_CONFIG, agent_name=LAUNCH_AGENT_NAME, model=model,
                          verbose=verbose)

        print(f"=== acctest [{phase}] round {rnd}: waiting for detached run in {acctest_dir} ===")
        wait_fh = (run_dir / f"acctest.{phase}.wait.r{rnd:02d}.log").open("w", encoding="utf-8") if verbose else None
        try:
            def log(msg: str) -> None:
                line = f"[{time.strftime('%H:%M:%S')}] {msg}"
                print(line)
                if wait_fh is not None:
                    wait_fh.write(line + "\n")
                    wait_fh.flush()
            await _wait(acctest_dir, log)
        finally:
            if wait_fh is not None:
                wait_fh.close()

        print(f"=== acctest [{phase}] round {rnd}: collect/fix ===")
        await run_session(client, collect_prompt, run_dir / f"acctest.{phase}.collect.r{rnd:02d}.log",
                          agent_config=COLLECT_AGENT_CONFIG, agent_name=COLLECT_AGENT_NAME, model=model,
                          verbose=verbose)

        result = read_result(collect_result)
        outcome = _outcome(result)
        if outcome == FIXED:
            print(f"acctest [{phase}] round {rnd} applied fixes; re-running suite to verify.")
            continue
        if outcome == CLEAN:
            print(f"acctest [{phase}] clean after {rnd} round(s); logs in {run_dir}")
        else:
            print(f"acctest [{phase}] stopping at round {rnd} ({outcome}); see {collect_result}")
        return outcome

    print(f"acctest [{phase}] reached max_rounds={max_rounds} while still fixing; see {collect_result}")
    return INCOMPLETE


async def run_acctest(client, run_dir: Path, acctest_dir: Path, *, rp_name: str,
                      test_regex: str, max_rounds: int,
                      model: str | None, verbose: bool = False) -> bool:
    """Smoke gate, then full suite. The expensive full run happens only if smoke is green."""
    print("=== acctest: SMOKE phase (representative tests first) ===")
    smoke = await _run_phase(
        client, run_dir, acctest_dir, rp_name=rp_name, test_regex=test_regex,
        phase="smoke", phase_scope=SMOKE_SELECTOR, max_rounds=max_rounds, model=model,
        verbose=verbose)
    if smoke != CLEAN:
        print(f"smoke phase did not converge ({smoke}); NOT running the full suite. "
              f"see logs in {run_dir}")
        return False

    print("=== acctest: smoke green -> FULL suite ===")
    full = await _run_phase(
        client, run_dir, acctest_dir, rp_name=rp_name, test_regex=test_regex,
        phase="full", phase_scope="the entire suite matching test_regex",
        max_rounds=max_rounds, model=model, verbose=verbose)
    if full != CLEAN:
        print(f"full phase did not converge ({full}); see logs in {run_dir}")
    return full == CLEAN
