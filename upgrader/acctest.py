"""Acceptance-test stage: run the full suite ONCE, then investigate & report. Advisory only.

This stage never edits code and never loops. After an API-version upgrade it launches the full
acceptance-test suite, waits for the detached run to finish, then runs a single collect turn
that diffs NEW failures against the TeamCity `main` baseline and INVESTIGATES each one — in
particular whether the target API version introduced a breaking change — and writes a
categorized report for a human to act on. It never mitigates or fixes anything, and its result
is ADVISORY: the upgrade's green `go build` alone decides the pipeline's success.

State lives on disk; the session is fresh and never re-attaches to a prior run.
"""

from __future__ import annotations

import asyncio
import os
import shutil
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

# Launch agent: capture the TeamCity main baseline, then start a detached full-suite
# `make acctests` run and exit — it never waits for results (a suite can take many hours).
LAUNCH_AGENT_CONFIG = {
    "name": LAUNCH_AGENT_NAME,
    "display_name": "RP Acctest Launch",
    "description": "Capture the TeamCity baseline and start a detached full acceptance-test run, then exit.",
    "skills": [LAUNCH_AGENT_NAME],
    "prompt": (
        "You are a senior Terraform provider engineer launching the full acceptance-test suite "
        "after an API upgrade. Never block — tests take hours. Capture the TeamCity main "
        "baseline (reuse it if already present), start a detached `make acctests` run for the "
        "whole suite, record run.pid, and EXIT. Do not wait for results. Never modify "
        "go.mod/vendor."
    ),
}

# Collect agent: diff NEW failures vs the baseline and INVESTIGATE each — is it a target-API
# breaking change, a test-side expectation, an API bug, or a flake? REPORT only; never edit code.
COLLECT_AGENT_CONFIG = {
    "name": COLLECT_AGENT_NAME,
    "display_name": "RP Acctest Collect",
    "description": "Investigate a finished acceptance-test run: diff against baseline and report NEW failures and API breaking changes. Never edits code.",
    "skills": [COLLECT_AGENT_NAME],
    "prompt": (
        "You are a senior Terraform provider engineer investigating a finished acceptance-test "
        "run after an API upgrade. Compute NEW failures = local FAIL minus the TeamCity "
        "baseline, and for each diagnose the root cause — in particular whether the target API "
        "version introduced a breaking change (confirm against Microsoft Learn + "
        "azure-rest-api-specs). REPORT every finding with evidence. Do NOT edit any code, tests, "
        "go.mod, or vendor — this is investigation only."
    ),
}


def _build_prompt(prompt_file: Path, rp_name: str, *, test_regex: str,
                  acctest_dir: Path, result_path: Path, parallel: int | None = None) -> str:
    """Substitute placeholders into an acctest launch/collect prompt body."""
    return fill_prompt(prompt_file, {
        "<rp_name>": rp_name,
        "<test_regex>": test_regex,
        "<parallel>": str(parallel if parallel is not None else 11),
        "<acctest_dir>": acctest_dir.as_posix(),
        "<result_path>": result_path.as_posix(),
    })


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


async def run_acctest(client, run_dir: Path, acctest_dir: Path, *, rp_name: str,
                      test_regex: str, parallel: int = 11, model: str | None = None,
                      verbose: bool = False) -> bool:
    """Launch the full suite once, wait for it, then run ONE investigate-and-report collect turn.

    Returns True when the report is clean (no NEW failures and no suspected breaking changes /
    API bugs). The return is ADVISORY — the caller does NOT gate the pipeline exit code on it;
    the upgrade's green build alone decides success. It is surfaced only for visibility.
    """
    launch_result = run_dir / "acctest.launch.result.json"
    collect_result = run_dir / "acctest.result.json"
    launch_prompt = _build_prompt(LAUNCH_PROMPT_FILE, rp_name, test_regex=test_regex,
                                  acctest_dir=acctest_dir, result_path=launch_result,
                                  parallel=parallel)
    collect_prompt = _build_prompt(COLLECT_PROMPT_FILE, rp_name, test_regex=test_regex,
                                   acctest_dir=acctest_dir, result_path=collect_result)

    print("=== acctest: launching the FULL suite (detached) ===")
    await run_session(client, launch_prompt, run_dir / "acctest.launch.log",
                      agent_config=LAUNCH_AGENT_CONFIG, agent_name=LAUNCH_AGENT_NAME, model=model,
                      verbose=verbose)

    # Guard: only wait if the launch actually started a run. A launch that failed (bad env,
    # tests that don't compile, a shell-quoting error) leaves no live PID; without this check
    # the watcher would "finish" instantly and collect would just report an empty/failed run.
    launch = read_result(launch_result)
    if launch.get("status") != "launched" or _read_pid(acctest_dir / "run.pid") is None:
        print(f"acctest launch did not start a run (status={launch.get('status', 'unknown')}); "
              f"see {launch_result} and {acctest_dir}/run.err — skipping collect.")
        return False

    print(f"=== acctest: waiting for detached run in {acctest_dir} ===")
    wait_fh = (run_dir / "acctest.wait.log").open("w", encoding="utf-8") if verbose else None
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

    print("=== acctest: investigate & report (no code changes) ===")
    await run_session(client, collect_prompt, run_dir / "acctest.collect.log",
                      agent_config=COLLECT_AGENT_CONFIG, agent_name=COLLECT_AGENT_NAME, model=model,
                      verbose=verbose)

    clean = _summarize(read_result(collect_result), collect_result)
    if not verbose:
        _cleanup_intermediate(acctest_dir)
    return clean


def _cleanup_intermediate(acctest_dir: Path) -> None:
    """Drop collect scratch files; keep the report and baseline."""
    for name in ("new_failures.txt", "triage.json", "meta.json", "run.pid", "run.err", "logs"):
        path = acctest_dir / name
        try:
            shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)
        except OSError:
            pass


def _summarize(result: dict[str, Any], collect_result: Path) -> bool:
    """Print a human-facing summary of the collect report; return True if it found nothing to act
    on. Purely advisory — this stage changes no code, so any finding is a hand-off to a human."""
    counts = result.get("counts", {})
    new_failed = counts.get("new_failed")
    if new_failed is None:
        new_failed = len(result.get("new_failures", []))
    breaking = result.get("breaking_changes", [])
    api_bugs = result.get("api_bugs", [])
    status = result.get("status", "unknown")

    print(f"acctest report ({status}): {new_failed} new failure(s), "
          f"{len(breaking)} suspected breaking change(s), {len(api_bugs)} API bug(s). "
          f"full report: {collect_result}")
    if status == "failed":
        print("  note: the run or the collect agent itself failed; the report may be incomplete.")

    clean = status != "failed" and new_failed == 0 and not breaking and not api_bugs
    if clean:
        print("  no NEW failures or breaking changes detected.")
    else:
        print("  review the report; this stage does NOT change code — a human must act on it.")
    return clean
