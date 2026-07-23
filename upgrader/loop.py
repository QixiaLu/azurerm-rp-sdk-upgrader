"""Top-level orchestration: run the upgrade stage, then (optionally) acctest triage.

Each stage owns a fresh Copilot client for its run; disk is the shared state between
iterations and between stages.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

from upgrader import acctest, upgrade


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
        with_toolkit: bool = False,
        verbose: bool = False) -> bool:
    """Upgrade the RP, then (optionally) run acctest investigation. Returns overall success.

    Success is decided by the UPGRADE alone (a green ``go build``). The acctest stage is
    ADVISORY: it runs the full suite once and writes an investigate-and-report artifact for a
    human, but never changes the exit code — slow/flaky acceptance tests must not fail a run
    whose upgrade converged.

    All work happens directly in ``repo``; the agent only edits files (the no-VCS guard keeps
    every change unstaged) so a human can review ``git -C <repo> diff`` afterwards.

    ``with_toolkit`` opts into injecting the allow-listed ai-assisted-development toolkit content
    into the upgrade stage (off by default).
    """
    work = repo
    print(f"working directly in {work}")

    run_dir = work / ".upgrader" / rp_name / target
    run_dir.mkdir(parents=True, exist_ok=True)

    upgraded = asyncio.run(_with_client(work, lambda client: upgrade.run_upgrade(
        client, run_dir, rp_name=rp_name, target=target, old=old,
        model=model, verbose=verbose, with_toolkit=with_toolkit)))

    if upgraded and run_acctest:
        print("starting acctest investigation (advisory; does not affect the exit code).")
        acctest_dir = work / ".acctest-run" / rp_name
        acctest_dir.mkdir(parents=True, exist_ok=True)
        asyncio.run(_with_client(work, lambda client: acctest.run_acctest(
            client, run_dir, acctest_dir, rp_name=rp_name, test_regex=test_regex,
            parallel=parallel, model=model, verbose=verbose)))

    return upgraded

