"""Upgrade stage: one Copilot session that bumps one RP's SDK API version toward a green build.

A single fresh session does one bounded chunk of the upgrade; disk is the shared state
(IMPLEMENTATION_PLAN.md + result.json). Success is reported only when result.json records a
green `go build ./...`.
"""

from __future__ import annotations

from pathlib import Path

from upgrader import toolkit
from upgrader.session import fill_prompt, read_result, run_session

PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "PROMPT.md"
PLAN_FILE = "IMPLEMENTATION_PLAN.md"
RESULT_FILE = "result.json"

AGENT_NAME = "rp-api-upgrade"

# Microsoft Learn MCP: read-only, public, no auth. Gives the breaking-change assessment
# (SKILL.md step 6) a semantics search over published Learn docs to surface candidate changes
# (default-value/enum/deprecation shifts). Subordinate to swagger — leads must still be
# confirmed against Azure/azure-rest-api-specs before being classified as breaking.
MCPS = {
    "microsoft-learn": {
        "type": "http",
        "url": "https://learn.microsoft.com/api/mcp",
        "tools": ["*"],
    },
}

# The upgrade agent: bump one RP's SDK API version until `go build ./...` is green.
AGENT_CONFIG = {
    "name": AGENT_NAME,
    "display_name": "RP API Upgrade",
    "description": "Upgrade one Azure RP SDK API version in terraform-provider-azurerm until go build succeeds.",
    "skills": [AGENT_NAME],
    "prompt": (
        "You are a senior Terraform provider engineer. Upgrade one Azure RP's SDK API "
        "version in terraform-provider-azurerm. Follow the rp-api-upgrade skill: locate "
        "current SDK usage under internal/services/<rp_name>/, verify the target version in "
        "the pinned go-azure-sdk, address breaking changes, run go mod tidy && "
        "go mod vendor, fix compile errors, and confirm go build ./... passes. Make surgical "
        "edits only and never read/diff vendor/ or go.sum."
    ),
}


def build_prompt(rp_name: str, target: str, old: str | None, result_path: Path,
                 with_toolkit: bool = False) -> str:
    if with_toolkit:
        guidance = toolkit.instructions_text() or "(ai-assisted-development toolkit not available)"
    else:
        guidance = "(toolkit guidance not requested; run with --with-toolkit to include it)"
    return fill_prompt(PROMPT_FILE, {
        "<rp_name>": rp_name,
        "<target_api_version>": target,
        "<old_api_version>": old or "(detect current)",
        "<result_path>": result_path.as_posix(),
        "<toolkit_guidance>": guidance,
    })


def plan_seed(rp_name: str, target: str, old: str | None) -> str:
    return "\n".join([
        f"# Upgrade plan: {rp_name} {old or '(current)'} -> {target}",
        "",
        "Task list for this upgrade session; tick items as you complete them.",
        "",
        "- [ ] Confirm target API version is published; bump go-azure-sdk if needed.",
        "- [ ] Update imports/clients/models/enums to target version.",
        "- [ ] go mod tidy && go mod vendor.",
        "- [ ] Assess and address breaking changes per `contributing/topics/guide-breaking-changes.md`.",
        "- [ ] Fix compile errors until `go build ./...` is green.",
        "",
        "## Notes",
        "- (append findings here)",
    ])


def build_passed(result_path: Path) -> bool:
    data = read_result(result_path)
    return data.get("status") == "done" and data.get("build_passed") is True


def _print_api_changes(result_path: Path) -> None:
    """Surface the target API version's notable changes (new fields, etc.) from result.json."""
    changes = read_result(result_path).get("api_changes") or []
    if not changes:
        return
    print(f"API-version changes ({len(changes)}):")
    for c in changes:
        if isinstance(c, dict):
            kind, symbol, detail = c.get("kind", "?"), c.get("symbol", ""), c.get("detail", "")
            print(f"  - [{kind}] {symbol}: {detail}".rstrip(": "))
        else:
            print(f"  - {c}")


async def run_upgrade(client, run_dir: Path, *, rp_name: str, target: str, old: str | None,
                      model: str | None, verbose: bool = False,
                      with_toolkit: bool = False) -> bool:
    """Run one upgrade session on an already-started client; return True iff the build is green.

    ``with_toolkit`` opts into injecting the allow-listed ai-assisted-development toolkit content
    (instructions embedded in the prompt, skills via ``skill_directories``). Off by default.
    """
    result_path = run_dir / RESULT_FILE
    plan_path = run_dir / PLAN_FILE
    if not plan_path.exists():
        plan_path.write_text(plan_seed(rp_name, target, old), encoding="utf-8")
    prompt = build_prompt(rp_name, target, old, result_path, with_toolkit=with_toolkit)

    extra_skill_dirs = None
    if with_toolkit:
        skills = toolkit.skills_dir()
        extra_skill_dirs = [str(skills)] if skills else None
    await run_session(client, prompt, run_dir / "upgrade.log",
                        agent_config=AGENT_CONFIG, agent_name=AGENT_NAME, model=model,
                        verbose=verbose, extra_skill_dirs=extra_skill_dirs,
                        mcp_servers=MCPS)
    if build_passed(result_path):
        print(f"build green; logs in {run_dir}")
        _print_api_changes(result_path)
        return True
    else:
        print("build failed;")
        return False
