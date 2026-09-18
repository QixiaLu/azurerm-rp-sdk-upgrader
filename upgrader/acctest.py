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
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from upgrader import helpers
from upgrader.agents import load_agent
from upgrader.helpers import read_result
from upgrader.session import MCPS, fill_prompt, run_session

# The upgrade stage's report; acctest findings are merged into it so a run has ONE report.
FINAL_RESULT_FILE = "result.json"

LAUNCH_PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "PROMPT_ACCTEST_LAUNCH.md"
COLLECT_PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "PROMPT_ACCTEST_COLLECT.md"

# Detached acctest runs can take many hours; poll their PID at this cadence.
POLL_SECONDS = 60
# Emit a heartbeat line every Nth poll while waiting.
HEARTBEAT_EVERY = 5

# Two acctest agents, each a single judgement role:
#   * LAUNCH — map the product RP name to the real provider SERVICE dir + test regex (which a
#     deterministic mapping gets wrong, e.g. `recoveryservicesbackup` -> `recoveryservices`),
#     then shell out to `python -m upgrader.helpers` for the mechanics.
#   * COLLECT — investigate a FINISHED run and report root causes.
# Waiting out the run and reducing its results stay deterministic (orchestrator-side).
LAUNCH_AGENT_NAME = "rp-test-launch"

LAUNCH_AGENT_CONFIG = {
    "name": LAUNCH_AGENT_NAME,
    "display_name": "RP Acceptance Test Launch",
    "description": "Resolve the RP's provider service dir + test regex, capture the TeamCity "
                   "baseline, and start the acceptance-test suite detached. Never edits code.",
    "prompt": load_agent("test-launch"),
}

TEST_AGENT_NAME = "rp-test"

TEST_AGENT_CONFIG = {
    "name": TEST_AGENT_NAME,
    "display_name": "RP Acceptance Test",
    "description": "Investigate a finished RP acceptance-test run: diff NEW failures vs the "
                   "TeamCity baseline and report API breaking changes with evidence. Never edits code.",
    "prompt": load_agent("test"),
}


def _build_launch_prompt(rp_name: str, *, test_regex: str, parallel: int, repo: Path,
                         acctest_dir: Path, result_path: Path) -> str:
    """Substitute placeholders into the acctest launch prompt body."""
    return fill_prompt(LAUNCH_PROMPT_FILE, {
        "<rp_name>": rp_name,
        "<test_regex>": test_regex,
        "<parallel>": str(parallel),
        "<repo>": repo.as_posix(),
        "<upgrader_root>": Path(__file__).resolve().parent.parent.as_posix(),
        "<acctest_dir>": acctest_dir.as_posix(),
        "<result_path>": result_path.as_posix(),
    })


def _build_prompt(prompt_file: Path, rp_name: str, *, test_regex: str,
                  acctest_dir: Path, result_path: Path,
                  analysis_path: Path | None = None,
                  target_api_version: str = "",
                  old_api_version: str | None = None,
                  specs_diff: str = "") -> str:
    """Substitute placeholders into the acctest collect prompt body."""
    return fill_prompt(prompt_file, {
        "<rp_name>": rp_name,
        "<test_regex>": test_regex,
        "<acctest_dir>": acctest_dir.as_posix(),
        "<result_path>": result_path.as_posix(),
        "<analysis_path>": (analysis_path.as_posix() if analysis_path is not None else ""),
        "<target_api_version>": target_api_version,
        "<old_api_version>": old_api_version or "(detect current)",
        "<azure_rest_api_specs_diff>": specs_diff or "(not available)",
    })


def _reduce_run(acctest_dir: Path) -> dict:
    """Deterministically reduce the finished run BEFORE the collect agent sees it.

    Parses ``run.json``, diffs the TeamCity baseline, and slices per-test logs in Python so the
    collect agent is left only the *judgement* (root-cause classification). Writes:

    * ``analysis.json`` — counts, NEW-failure list, and signature clusters
    * ``new_failures.txt`` — one NEW failure per line
    * ``logs/<Test>.log`` — each NEW failure's sliced output

    Returns the analysis dict (``{}`` if ``run.json`` is missing/unusable).
    """
    run_json = acctest_dir / "run.json"
    baseline_json = acctest_dir / "baseline.json"
    if not run_json.is_file():
        print(f"[DEBUG]   reduce: no run.json at {run_json}; collect will fall back to raw parsing.")
        return {}
    analysis = helpers.analyze_run(run_json, baseline_json)

    logs_dir = acctest_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    for test, output in analysis.get("outputs", {}).items():
        (logs_dir / f"{test}.log").write_text(output, encoding="utf-8")
    (acctest_dir / "new_failures.txt").write_text(
        "\n".join(analysis.get("new_failures", [])) + "\n", encoding="utf-8")
    # Persist counts + clusters (drop the bulky per-test outputs — they live in logs/).
    helpers.write_result(acctest_dir / "analysis.json", {
        "counts": analysis.get("counts", {}),
        "new_failures": analysis.get("new_failures", []),
        "clusters": analysis.get("clusters", {}),
    })
    counts = analysis.get("counts", {})
    print(f"[DEBUG]   reduce: {counts.get('new_failed', 0)} NEW failure(s) across "
          f"{len(analysis.get('clusters', {}))} signature cluster(s); "
          f"analysis.json + logs/ written to {acctest_dir}")
    return analysis


# --- detached-run watch (PID-based, fresh-only: never re-attaches) -------------------

def _read_pid(pid_file: Path) -> int | None:
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """Return whether a process is alive on the local platform."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        error_access_denied = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, wintypes.LPDWORD]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == error_access_denied
        try:
            exit_code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and \
                exit_code.value == still_active
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
        # No PID yet means the orchestrator hasn't recorded one; treat as not finished
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


async def run_acctest(client, run_dir: Path, acctest_dir: Path, *, repo: Path, rp_name: str,
                      target_api_version: str, old_api_version: str | None,
                      test_regex: str, parallel: int = 11, model: str | None = None,
                      debug_tools_log: bool = False) -> bool:
    """Launch the full suite once, wait for it, then run ONE investigate-and-report collect turn.

    Two agent turns bracket deterministic orchestrator work: a LAUNCH turn resolves the RP's real
    provider service dir + test regex and starts the suite (via the helpers CLI), then the
    orchestrator waits out the run and reduces it in Python, then a COLLECT turn classifies the
    NEW failures. Waiting and reduction never involve an LLM.

    Returns True when the report is clean (no NEW failures and no suspected breaking changes /
    API bugs). The return is ADVISORY — the caller does NOT gate the pipeline exit code on it;
    the upgrade's green build alone decides success. It is surfaced only for visibility.
    """
    collect_result = run_dir / "acctest.result.json"
    launch_result = run_dir / "acctest.launch.result.json"
    analysis_path = acctest_dir / "analysis.json"

    # Cheap orchestrator short-circuit: if the ARM creds are absent, skip the whole stage
    # (and the launch session) rather than spin an agent that can only fail the preflight.
    missing = helpers.missing_acctest_env()
    if missing:
        print(f"[DEBUG] acctest: missing required env {', '.join(missing)}; "
              f"skipping acctest (no Azure resources created).")
        return False

    # LAUNCH is an agent turn: it maps the product RP name to the real provider SERVICE dir +
    # test regex (which a deterministic mapping gets wrong), captures the baseline, and starts
    # `make acctests` detached — all via the `python -m upgrader.helpers` CLI, which owns the
    # mechanical TeamCity/quoting/spawn work. The agent never waits.
    acctest_dir.mkdir(parents=True, exist_ok=True)
    print("=== acctest: launching the FULL suite (detached, launch agent) ===")
    launch_prompt = _build_launch_prompt(rp_name, test_regex=test_regex, parallel=parallel,
                                          repo=repo, acctest_dir=acctest_dir,
                                          result_path=launch_result)
    await run_session(client, launch_prompt,
                      agent_config=LAUNCH_AGENT_CONFIG, agent_name=LAUNCH_AGENT_NAME,
                      model=model, debug_tools_log=debug_tools_log)

    launch = read_result(launch_result)
    if launch.get("status") != "launched" or _read_pid(acctest_dir / "run.pid") is None:
        print(f"[DEBUG] acctest launch did not start a run ({launch.get('summary', 'unknown')}); "
              f"see {acctest_dir}/run.err — skipping collect.")
        return False
    print(f"[DEBUG]   launched against service '{launch.get('service', '?')}' "
          f"(filter {launch.get('run_filter', '?')}, baseline {launch.get('baseline_mode', '?')}).")

    print(f"=== acctest: waiting for detached run in {acctest_dir} ===")

    def log(msg: str) -> None:
        print(f"[DEBUG] [{time.strftime('%H:%M:%S')}] {msg}")
    await _wait(acctest_dir, log)

    # Deterministic reduction: parse run.json, diff the baseline, slice per-test logs, and
    # cluster by signature IN PYTHON — so the collect agent only classifies root cause.
    print("=== acctest: reducing the finished run (deterministic) ===")
    analysis = _reduce_run(acctest_dir)
    specs_diff = helpers.format_azure_rest_api_specs_diff(
        helpers.collect_azure_rest_api_specs_context(
            rp_name, target_api_version, old_api_version))
    collect_prompt = _build_prompt(COLLECT_PROMPT_FILE, rp_name, test_regex=test_regex,
                                   acctest_dir=acctest_dir, result_path=collect_result,
                                   analysis_path=analysis_path,
                                   target_api_version=target_api_version,
                                   old_api_version=old_api_version,
                                   specs_diff=specs_diff)

    print("=== acctest: investigate & report (no code changes) ===")
    await run_session(client, collect_prompt,
                      agent_config=TEST_AGENT_CONFIG, agent_name=TEST_AGENT_NAME, model=model,
                      mcp_servers=MCPS,
                      debug_tools_log=debug_tools_log)

    report = read_result(collect_result)
    if analysis.get("counts"):
        report["counts"] = analysis["counts"]
    merged = _merge_into_final_report(run_dir / FINAL_RESULT_FILE, report)

    counts = report.get("counts", {})
    new_failed = counts.get("new_failed")
    if new_failed is None:
        new_failed = len(report.get("new_failures", []))
    breaking = report.get("breaking_changes", [])
    api_bugs = report.get("api_bugs", [])
    status = report.get("status", "unknown")
    clean = status != "failed" and new_failed == 0 and not breaking and not api_bugs

    _cleanup_intermediate(acctest_dir)
    launch_result.unlink(missing_ok=True)  # launch details already served their purpose
    if merged:  # findings now live in result.json; drop the standalone sidecar
        collect_result.unlink(missing_ok=True)
    return clean


def _merge_into_final_report(final_result: Path, acctest_report: dict[str, Any]) -> bool:
    """Attach the acctest findings to the upgrade's result.json under an ``acctest`` key and
    return True on success."""
    final = read_result(final_result)
    final["acctest"] = acctest_report
    try:
        final_result.write_text(json.dumps(final, indent=2, sort_keys=True) + "\n",
                                encoding="utf-8")
        return True
    except OSError as exc:
        print(f"  warning: could not merge acctest findings into {final_result} ({exc}); "
              f"see the standalone report instead.", file=sys.stderr)
        return False


def _cleanup_intermediate(acctest_dir: Path) -> None:
    """Drop collect scratch files; keep the report and baseline."""
    for name in ("new_failures.txt", "analysis.json", "triage.json", "meta.json", "run.pid", "run.err", "baseline.json", "run.json", "logs"):
        path = acctest_dir / name
        try:
            shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)
        except OSError:
            pass
