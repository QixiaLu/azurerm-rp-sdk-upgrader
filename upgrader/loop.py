"""Top-level orchestration for the upgrade pipeline and optional detector stage.

Each stage owns a fresh Copilot client for its run; disk is the shared state between
turns and between stages.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Awaitable, Callable
from uuid import uuid4

from upgrader import acctest, breaking_change, helpers, prebuild, upgrade


MANIFEST_FILE = "run.json"


def _write_manifest(run_dir: Path, manifest: dict) -> None:
    """Atomically persist the orchestrator-owned record for one invocation."""
    path = run_dir / MANIFEST_FILE
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def _repo_commit(repo: Path) -> str | None:
    """Return the checked-out commit when this is a Git worktree."""
    completed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                               capture_output=True, text=True, check=False)
    return completed.stdout.strip() if completed.returncode == 0 else None


async def _with_client(repo: Path, run: Callable[[object], Awaitable[bool]]) -> bool:
    """Start a fresh Copilot client rooted at ``repo``, run one stage on it, then stop it."""
    from copilot import CopilotClient  # type: ignore[import-not-found]

    client = CopilotClient(working_directory=str(repo))
    await client.start()
    try:
        return await run(client)
    finally:
        await client.stop()


def run(repo: Path, rp_name: str, target: str, old: str | None, *,
        model: str | None = None,
        run_acctest: bool = True, test_regex: str = "TestAcc",
        parallel: int = 11,
    max_rounds: int = 8,
    debug_tools_log: bool = False,
    prebuild_local_sdk: bool = False,
    run_execute: bool = True,
    breaking_change_provider_version: str | None = None,
    pandora_repo: Path | None = None,
    go_azure_sdk_repo: Path | None = None,
    pandora_service: str | None = None) -> bool:
    """Run the enabled stages in order and return overall success.

    Prebuild, execute, and breaking-change failures stop the pipeline. The acctest stage remains
    ADVISORY: it writes an investigate-and-report artifact but never changes the exit code.

    All work happens directly in ``repo``; the agent only edits files (the no-VCS guard keeps
    every change unstaged) so a human can review ``git -C <repo> diff`` afterwards.
    """
    work = repo
    print(f"[DEBUG] working directly in {work}")
    source = old or helpers.detect_current_api_version(work, rp_name)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    run_dir = work / ".upgrader" / rp_name / target / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "input": {
            "service": rp_name,
            "target_api_version": target,
            "old_api_version": source,
            "stages": [],
        },
        "stages": {},
    }
    if prebuild_local_sdk:
        manifest["input"]["stages"].append("prebuild")
    if run_execute:
        manifest["input"]["stages"].append("upgrade")
    if run_acctest:
        manifest["input"]["stages"].append("acctest")
    if breaking_change_provider_version:
        manifest["input"]["stages"].append("breaking-change")
    _write_manifest(run_dir, manifest)

    def finish(status: str) -> bool:
        result_path = run_dir / "result.json"
        result = helpers.read_result(result_path)
        if result:
            provenance = {
                "repository": str(work),
                "commit": _repo_commit(work),
                "source_api_version": source,
                "target_api_version": target,
                "command": {
                    "stages": manifest["input"]["stages"],
                    "test_regex": test_regex,
                    "parallel": parallel,
                    "provider_version": breaking_change_provider_version,
                },
            }
            helpers.write_result(result_path, helpers.finalize_result(result, provenance=provenance))
        manifest["status"] = status
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_manifest(run_dir, manifest)
        latest_dir = run_dir.parent.parent
        _write_manifest(latest_dir, {
            "run_id": run_id,
            "run": str(run_dir),
            "status": status,
        })
        (latest_dir / MANIFEST_FILE).replace(latest_dir / "latest.json")
        print(f"Run artifacts: {run_dir}")
        return status == "success"

    if prebuild_local_sdk:
        if pandora_repo is None or go_azure_sdk_repo is None or not pandora_service:
            raise ValueError("local SDK prebuild requires Pandora repo, SDK repo, and service")
        print(f"[DEBUG] prebuilding local SDK for Pandora service {pandora_service}")
        if not prebuild.run_prebuild(
                repo=work, run_dir=run_dir, target=target,
                pandora_repo=pandora_repo, go_azure_sdk_repo=go_azure_sdk_repo,
                pandora_service=pandora_service):
            manifest["stages"]["prebuild"] = {"status": "failed", "result": "prebuild.json"}
            return finish("failed")
        manifest["stages"]["prebuild"] = {"status": "success", "result": "prebuild.json"}
        _write_manifest(run_dir, manifest)

    if run_execute and not asyncio.run(_with_client(work, lambda client: upgrade.run_upgrade(
            client, run_dir, repo=work, rp_name=rp_name, target=target, old=source,
            model=model, max_rounds=max_rounds,
            debug_tools_log=debug_tools_log))):
        manifest["stages"]["upgrade"] = {"status": "failed", "result": "result.json"}
        return finish("failed")
    if run_execute:
        manifest["stages"]["upgrade"] = {"status": "success", "result": "result.json"}
        _write_manifest(run_dir, manifest)

    if run_acctest:
        print("[DEBUG] starting acctest investigation (advisory; does not affect the exit code).")
        acctest_dir = run_dir / "acctest"
        acctest_dir.mkdir(parents=True, exist_ok=True)
        acctest_clean = asyncio.run(_with_client(work, lambda client: acctest.run_acctest(
            client, run_dir, acctest_dir, repo=work, rp_name=rp_name,
            target_api_version=target, old_api_version=source,
            test_regex=test_regex, parallel=parallel, model=model,
            debug_tools_log=debug_tools_log)))
        manifest["stages"]["acctest"] = {
            "status": "success" if acctest_clean else "warning",
            "result": "result.json",
            "evidence": "acctest",
        }
        _write_manifest(run_dir, manifest)

    if breaking_change_provider_version and not breaking_change.run_breaking_change(
            repo=work, run_dir=run_dir,
            provider_version=breaking_change_provider_version,
            service=rp_name,
            test_regex=test_regex, parallel=parallel):
        manifest["stages"]["breaking-change"] = {
            "status": "failed", "result": "breaking-change.json"}
        return finish("failed")
    if breaking_change_provider_version:
        manifest["stages"]["breaking-change"] = {
            "status": "success", "result": "breaking-change.json"}

    return finish("success")
