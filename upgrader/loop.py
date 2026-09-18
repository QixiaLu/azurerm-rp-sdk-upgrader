"""Top-level orchestration: run the upgrade stage, then (optionally) acctest triage.

Each stage owns a fresh Copilot client for its run; disk is the shared state between
turns and between stages.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

from upgrader import acctest, prebuild, upgrade


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
    pandora_repo: Path | None = None,
    go_azure_sdk_repo: Path | None = None,
    pandora_service: str | None = None) -> bool:
    """Upgrade the RP, then (optionally) run acctest investigation. Returns overall success.

    Success is decided by the UPGRADE alone (a green ``go build``). The acctest stage is
    ADVISORY: it runs the full suite once and writes an investigate-and-report artifact for a
    human, but never changes the exit code — slow/flaky acceptance tests must not fail a run
    whose upgrade converged.

    All work happens directly in ``repo``; the agent only edits files (the no-VCS guard keeps
    every change unstaged) so a human can review ``git -C <repo> diff`` afterwards.
    """
    work = repo
    print(f"[DEBUG] working directly in {work}")

    run_dir = work / ".upgrader" / rp_name / target
    run_dir.mkdir(parents=True, exist_ok=True)

    # if prebuild_local_sdk:
    #     if pandora_repo is None or go_azure_sdk_repo is None or not pandora_service:
    #         raise ValueError("local SDK prebuild requires Pandora repo, SDK repo, and service")
    #     print(f"[DEBUG] prebuilding local SDK for Pandora service {pandora_service}")
    #     if not prebuild.run_prebuild(
    #             repo=work,
    #             run_dir=run_dir,
    #             target=target,
    #             pandora_repo=pandora_repo,
    #             go_azure_sdk_repo=go_azure_sdk_repo,
    #             pandora_service=pandora_service):
    #         return False

    # upgraded = asyncio.run(_with_client(work, lambda client: upgrade.run_upgrade(
    #     client, run_dir, repo=work, rp_name=rp_name, target=target, old=old,
    #     model=model, max_rounds=max_rounds,
    #     debug_tools_log=debug_tools_log)))

    # if upgraded and run_acctest:
    print("[DEBUG] starting acctest investigation (advisory; does not affect the exit code).")
    acctest_dir = work / ".acctest-run" / rp_name
    acctest_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(_with_client(work, lambda client: acctest.run_acctest(
        client, run_dir, acctest_dir, repo=work, rp_name=rp_name,
        target_api_version=target, old_api_version=old,
        test_regex=test_regex, parallel=parallel, model=model,
        debug_tools_log=debug_tools_log)))

    # return upgraded
    return True
