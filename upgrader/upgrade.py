"""Upgrade stage: one Copilot session that bumps one RP's SDK API version toward a green build.

A single fresh session does one bounded chunk of the upgrade; disk is the shared state
(IMPLEMENTATION_PLAN.md + result.json). Success is reported only when result.json records a
green `go build ./...`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from upgrader import helpers
from upgrader.agents import load_agent
from upgrader.helpers import read_result
from upgrader.session import MCPS, fill_prompt, run_session

PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "PROMPT.md"
PLAN_FILE = "IMPLEMENTATION_PLAN.md"
RESULT_FILE = "result.json"
PREBUILD_FILE = "prebuild.json"

AGENT_NAME = "rp-api-upgrade"

# Ceiling on upgrade rounds. Each round is a fresh session that does one bounded chunk of the
# fix; after each, the orchestrator runs `go build ./...` as the authoritative convergence gate.
# A run that hasn't gone green by here is handed to a human (see IMPLEMENTATION_PLAN.md).
DEFAULT_MAX_ROUNDS = 8

# Cap how many compile errors are echoed into the next round's prompt (keep the turn focused;
# fixing the first few usually clears many downstream ones).
_BUILD_ERROR_CAP = 50


# The durable role (identity, procedure, boundaries, result.json contract) lives in
# agents/upgrade.md and is loaded verbatim as the system prompt; PROMPT.md carries only the
# per-round dynamic inputs.
AGENT_CONFIG = {
    "name": AGENT_NAME,
    "display_name": "RP API Upgrade",
    "description": "Upgrade one Azure RP SDK API version in terraform-provider-azurerm until go build succeeds.",
    "prompt": load_agent("upgrade"),
}


def build_prompt(rp_name: str, target: str, old: str | None, result_path: Path,
                 plan_path: Path,
                 *, round_no: int = 1,
                 max_rounds: int = DEFAULT_MAX_ROUNDS,
                 build_errors: list[dict[str, Any]] | None = None,
                 specs_diff: str = "",
                 local_sdk: str = "") -> str:
    plan_posix = plan_path.as_posix()
    round_context = (
        f"This is upgrade round {round_no} of at most {max_rounds}. Prior rounds' progress is in "
        f"{plan_posix} and result.json — continue from there; do not restart from scratch."
    )
    return fill_prompt(PROMPT_FILE, {
        "<rp_name>": rp_name,
        "<target_api_version>": target,
        "<old_api_version>": old or "(detect current)",
        "<local_sdk>": local_sdk or "(none — resolve the target version from a published release)",
        "<result_path>": result_path.as_posix(),
        "<plan_path>": plan_posix,
        "<round_context>": round_context,
        "<build_errors>": _format_build_errors(build_errors or []),
        "<azure_rest_api_specs_diff>": specs_diff or "(not available)",
    })


def _local_sdk_dir(run_dir: Path) -> str:
    """Where a successful ``--prebuild-local-sdk`` put the generated SDK, else ``""``.

    A prebuilt version is deliberately absent upstream, so the agent must skip the
    "is it published?" check that would otherwise abort the run.
    """
    prebuild = read_result(run_dir / PREBUILD_FILE)
    if prebuild.get("status") != "success":
        return ""
    return str(prebuild.get("sdk_dir") or "")


def _format_build_errors(errors: list[dict[str, Any]], cap: int = _BUILD_ERROR_CAP) -> str:
    """Render structured ``go build`` errors as ``file:line:col: msg`` lines for the prompt."""
    if not errors:
        return "(none — the orchestrator's last build was clean or this is the first round)"
    lines = [f"{e.get('file')}:{e.get('line')}:{e.get('col')}: {e.get('msg')}" for e in errors[:cap]]
    if len(errors) > cap:
        lines.append(f"... and {len(errors) - cap} more")
    return "\n".join(lines)


def plan_seed(rp_name: str, target: str, old: str | None) -> str:
    return "\n".join([
        f"# Upgrade plan: {rp_name} {old or '(current)'} -> {target}",
        "",
        "Task list for this upgrade session; tick items as you complete them.",
        "",
        "- [ ] Confirm target API version is published; bump go-azure-sdk if needed.",
        "- [ ] Update imports/clients/models/enums to target version.",
        "- [ ] go mod tidy && go mod vendor.",
        "- [ ] Assess every user-facing API change as breaking or non-breaking; record evidence and user impact.",
        "- [ ] Fix compile errors (resource AND `*_test.go` files) until `go build ./...` + test compile are green.",
        "",
        "## Notes",
        "- (append findings here)",
    ])


def _merge_progress(prior: dict[str, Any], round_no: int,
                    build: dict[str, Any]) -> dict[str, Any]:
    """Merge one round's AUTHORITATIVE build outcome over the LLM's sidecar (pure, restart-safe).

    The LLM writes ``status``/``summary``/``blockers``/``api_changes`` into result.json; those are
    preserved. The orchestrator then overwrites the fields that must be trustworthy — the real
    ``go build`` verdict and the round tally — so a resumed run reads a truthful progress record
    regardless of what the model claimed, and tags each ``api_changes`` entry with its
    ``schema_impact`` so schema-surface changes are separable from SDK-only churn.
    """
    merged = dict(prior)
    merged["rounds"] = round_no
    merged["build_passed"] = bool(build.get("passed"))
    merged["converged"] = bool(build.get("passed"))
    merged["build_errors"] = build.get("errors", [])
    merged["build_error_count"] = len(build.get("errors", []))
    if merged.get("api_changes"):
        # Separate real schema-surface changes from SDK-only plumbing (package moves,
        # client/method renames, operations the provider never calls) so a reviewer
        # isn't wading through churn a green build already settled.
        changes = helpers.tag_api_changes_schema_impact(merged["api_changes"])
        merged["api_changes"] = changes
        merged["schema_impacting_change_count"] = len(helpers.schema_impacting_changes(changes))
    if build.get("passed"):
        merged["status"] = "done"
    elif merged.get("status") == "done":
        merged["status"] = "in_progress"
    return merged


def _record_progress(result_path: Path, round_no: int, build: dict[str, Any]) -> None:
    """Persist the merged, restart-safe progress record."""
    helpers.write_result(result_path, _merge_progress(read_result(result_path), round_no, build))


def _print_api_changes(result_path: Path) -> None:
    """Surface the target API version's notable changes (new fields, etc.) from result.json.

    Schema-surface changes print first and in full — they're the ones needing a human
    decision; the SDK-only plumbing is collapsed to a count.
    """
    changes = read_result(result_path).get("api_changes") or []
    if not changes:
        return
    tagged = helpers.tag_api_changes_schema_impact(changes)
    schema = [c for c in tagged if isinstance(c, dict) and c.get("schema_impact") == "schema"]
    sdk_only = len(tagged) - len(schema)

    print(f"[DEBUG] API-version changes ({len(tagged)}); "
          f"{len(schema)} touch the provider schema:")
    for c in schema:
        kind, symbol, detail = c.get("kind", "?"), c.get("symbol", ""), c.get("detail", "")
        print(f"[DEBUG]   - [{kind}] {symbol}: {detail}".rstrip(": "))
    for c in tagged:
        if not isinstance(c, dict):
            print(f"[DEBUG]   - {c}")
    if sdk_only:
        print(f"[DEBUG]   (+{sdk_only} SDK-only change(s) — package/client/method churn, "
              "no schema impact; see result.json)")


async def run_upgrade(client, run_dir: Path, *, repo: Path, rp_name: str, target: str,
                      old: str | None, model: str | None,
                      max_rounds: int = DEFAULT_MAX_ROUNDS,
                      debug_tools_log: bool = False) -> bool:
    """Drive the upgrade as a bounded round loop; return True iff `go build ./...` is green.

    Each round is a fresh session that does one bounded chunk of the fix. After every round the
    ORCHESTRATOR runs ``go build ./...`` itself (``helpers.run_go_build``) — that deterministic
    verdict, not the model's self-report, is the convergence gate, and its remaining compile
    errors are fed verbatim into the next round. Progress (round counter + authoritative build
    state) is merged into result.json each round so an interrupted run resumes truthfully.
    """
    result_path = run_dir / RESULT_FILE
    plan_path = run_dir / PLAN_FILE

    if not old:
        detected = helpers.detect_current_api_version(repo, rp_name)
        if detected:
            old = detected
            print(f"[DEBUG] detected current API version for {rp_name}: {old}")

    if not plan_path.exists():
        plan_path.write_text(plan_seed(rp_name, target, old), encoding="utf-8")

    specs_diff = helpers.format_azure_rest_api_specs_diff(
        helpers.collect_azure_rest_api_specs_context(rp_name, target, old)
    )

    local_sdk = _local_sdk_dir(run_dir)
    if local_sdk:
        print(f"[DEBUG] local SDK prebuild detected at {local_sdk}; "
              "the agent will skip the upstream-publication check")

    prior_rounds = int(read_result(result_path).get("rounds", 0) or 0)
    total_round_limit = prior_rounds + max_rounds

    build: dict[str, Any] = {"passed": False, "errors": []}
    for i in range(max_rounds):
        round_no = prior_rounds + i + 1
        print(f"=== upgrade round {round_no}/{total_round_limit} "
              f"({len(build['errors'])} compile error(s) to clear) ===")
        prompt = build_prompt(
            rp_name,
            target,
            old,
            result_path,
            plan_path,
            round_no=round_no,
            max_rounds=total_round_limit,
            build_errors=build["errors"],
            specs_diff=specs_diff,
            local_sdk=local_sdk,
        )
        await run_session(client, prompt,
                          agent_config=AGENT_CONFIG, agent_name=AGENT_NAME, model=model,
                          mcp_servers=MCPS,
                          debug_tools_log=debug_tools_log)
        build = helpers.run_go_build(repo)  # AUTHORITATIVE convergence check
        _record_progress(result_path, round_no, build)
        if build["passed"]:
            print(f"[DEBUG] build green after round {round_no}; result.json in {run_dir}")
            _print_api_changes(result_path)
            return True
        print(f"[DEBUG] round {round_no}: build still failing "
              f"({len(build['errors'])} error(s)); continuing.")

    print(f"[DEBUG] did not converge after {max_rounds} round(s); "
          "see IMPLEMENTATION_PLAN.md and result.json.")
    return False
