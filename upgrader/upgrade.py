"""Upgrade stage: the Ralph loop that bumps one RP's SDK API version until the build is green.

Fresh Copilot session per iteration; disk is the shared state (IMPLEMENTATION_PLAN.md +
result.json). The loop stops as soon as result.json reports a green `go build ./...`.
"""

from __future__ import annotations

from pathlib import Path

from upgrader.session import fill_prompt, read_result, run_session

PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "PROMPT.md"
PLAN_FILE = "IMPLEMENTATION_PLAN.md"
RESULT_FILE = "result.json"

AGENT_NAME = "rp-api-upgrade"

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


def build_prompt(rp_name: str, target: str, old: str | None, result_path: Path) -> str:
    return fill_prompt(PROMPT_FILE, {
        "<rp_name>": rp_name,
        "<target_api_version>": target,
        "<old_api_version>": old or "(detect current)",
        "<result_path>": result_path.as_posix(),
    })


def plan_seed(rp_name: str, target: str, old: str | None) -> str:
    return "\n".join([
        f"# Upgrade plan: {rp_name} {old or '(current)'} -> {target}",
        "",
        "Shared task list across iterations. Keep current; future iterations start fresh.",
        "",
        "- [ ] Confirm target API version is published; bump go-azure-sdk if needed.",
        "- [ ] Update imports/clients/models/enums to target version.",
        "- [ ] go mod tidy && go mod vendor.",
        "- [ ] Assess and Address breaking change as instructed in `contributing/topics/guide_breaking_change.md`",
        "- [ ] Fix compile errors until `go build ./...` is green.",
        "",
        "## Notes",
        "- (iterations append findings here)",
    ])


def build_passed(result_path: Path) -> bool:
    data = read_result(result_path)
    return data.get("status") == "done" and data.get("build_passed") is True


async def run_upgrade(client, run_dir: Path, *, rp_name: str, target: str, old: str | None,
                      max_iterations: int, model: str | None, verbose: bool = False) -> bool:
    """Ralph loop on an already-started client: iterate until the build is green."""
    result_path = run_dir / RESULT_FILE
    plan_path = run_dir / PLAN_FILE
    if not plan_path.exists():
        plan_path.write_text(plan_seed(rp_name, target, old), encoding="utf-8")
    prompt = build_prompt(rp_name, target, old, result_path)

    for i in range(1, max_iterations + 1):
        print(f"=== upgrade iteration {i}/{max_iterations} ===")
        await run_session(client, prompt, run_dir / f"upgrade.iter-{i:02d}.log",
                          agent_config=AGENT_CONFIG, agent_name=AGENT_NAME, model=model,
                          verbose=verbose)
        if build_passed(result_path):
            print(f"build green after {i} iteration(s); logs in {run_dir}")
            return True
    print(f"reached max_iterations={max_iterations} without a green build")
    return build_passed(result_path)
