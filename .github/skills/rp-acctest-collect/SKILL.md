---
name: rp-acctest-collect
description: Investigate a finished acceptance-test run for a Resource Provider - diff results against the TeamCity main baseline to find NEW failures and REPORT their root cause (in particular target-API breaking changes), without editing any code. Use after rp-acctest-launch once the detached run has finished.
---

# RP Acceptance Test Collect & Investigate Skill

## When to use

Use this skill when a detached full-suite acceptance-test run started by `rp-acctest-launch` has
finished and you need to:

- separate NEW failures from tests already failing on `main` (using the TeamCity baseline),
- diagnose the ROOT CAUSE of each new failure — in particular whether the target API version
  introduced a **breaking change** — and REPORT it with evidence,
- classify the rest (stale test-side expectation, API bug, or environmental flake).

This stage is **investigation only**: it NEVER edits code, tests, `go.mod`, or `vendor`, and it
NEVER re-runs tests. It **never weakens tests** — it does not skip, loosen, relax assertions,
add `ignore`/`ExpectNonEmptyPlan`, or otherwise reduce test coverage, and it never recommends
doing so. Every finding is a hand-off for a human to act on.

**Stop on the first breaking change.** The moment you confirm a target-API breaking change,
REPORT it and STOP — do not continue triaging the remaining NEW failures. A breaking change
blocks the upgrade and must be handed to a human before any further work; investigating the rest
is wasted effort until the breaking change is resolved.

The run is expensive (real Azure resources, many hours), so triage works entirely from the
on-disk `run.json`/`baseline.json` produced by the launch phase, in bounded batches.

## Required Inputs

- `service_name` (for example `recoveryservices`)

## Optional Inputs

- `test_regex` (default `TestAcc`)

## Required Environment

None. This stage only reads on-disk results and public docs/specs; it creates no resources and
runs no tests. (A `TEAMCITY_*` / `gh` token may help fetch a baseline or swagger, but is optional.)

## Procedure

1. Confirm completion: PID in `run.pid` is gone, or `run.json` has a final result line. If still running, report a short tail and exit (do not block).
2. Parse `run.json` (`go test -json`) into the failed-test set (read file, filter with `jq`/grep; never echo the whole file). Load `baseline.json`. NEW failures = local FAIL that are PASS/absent in baseline. Write the NEW set to `.acctest-run/<service_name>/new_failures.txt`.
   - **Scale (hundreds of results):** never carry the full log forward. Reduce once, then process in bounded batches with on-disk state:
     - failed set: `jq -r 'select(.Action=="fail" and .Test!=null).Test' run.json | sort -u`; subtract baseline failures (`comm -23`) → `new_failures.txt`.
     - slice each NEW test's log once into `.acctest-run/<service_name>/logs/<Test>.log` (`jq -r --arg t <Test> 'select(.Test==$t).Output // empty'`) so triage never reloads `run.json`.
     - cluster by failure signature (first `Error:`/assertion line) so one root cause (e.g. a renamed attribute) fixes a whole group in a single edit.
     - keep a `triage.json` ledger (one row per NEW test: `status` ∈ pending/breaking-change/test-side/api-bug/flake/needs-human, `batch`, `file`); process ~5–10 per turn, update the ledger, exit. This keeps turns small and resumable.
     - **Keep context flat:** batching bounds work but not context — reading every `.log` in one turn still accumulates it. Process **one batch per turn** (or delegate each batch to a stateless sub-agent that returns only test→status/file). The orchestrator holds **only `triage.json`**, never raw logs; a turn reads just its batch's `.log` files, then exits.
     - **Stop early:** the instant any test in a batch is confirmed as a breaking change, record it, write the report, and EXIT — leave the remaining NEW failures `pending` (a human resolves the breaking change first).
3. INVESTIGATE each new failure — diagnose the root cause from the log, then CLASSIFY it. **Do not edit code, do not weaken tests, and do not re-run tests.** The moment a breaking change is confirmed, REPORT it and STOP (see step 2); do not classify the rest.
   - Extract the test's failure output from `run.json` (assertion/diff, Terraform/Azure error, failing step) and diagnose the root cause from the log alone.
   - **When a failure looks API-driven** (changed default/computed value, renamed/repurposed enum, new required field, altered response shape, or a create/read/update contract error), confirm what actually changed in the **target API version** — see *Researching an API change* below — before classifying.
   - **Breaking change** (the primary thing to find — and a STOP condition): an **intended** target-API contract change, confirmed in the docs/spec, that the provider or its tests relied on — including any change that would require touching the provider's OWN schema (`Default`, `Computed`, `Required`, validation, type, or a property rename). Report it under `breaking_changes` with the test, error excerpt, exactly what changed (old → new), the doc/spec evidence, whether it touches user-facing schema, and a recommended fix direction (e.g. feature-flag gate per `contributing/topics/guide-breaking-changes.md`, or API-default absorption). **Do NOT implement it.** As soon as this is confirmed, write the report and STOP — do not investigate the remaining NEW failures.
   - **Test-side expectation**: stale test data (attribute renamed in HCL, an assertion expecting a *server-computed* value that legitimately changed, import drift) with NO underlying schema/contract change. Report it under `test_side` with the cause and the suggested change. **Do NOT edit or weaken the test** — never propose skipping it, loosening an assertion, or adding an `ignore`/`ExpectNonEmptyPlan` to make it pass; only describe the stale data and the correct value for a human to update.
   - **API bug** (the API errors or returns a shape/value that **contradicts its own `<target_api_version>` spec** — a defect on the Azure side, not an intended change) → report under `api_bugs` with the failing test, error excerpt, and the spec/doc the behavior contradicts, and set `status: "blocked"`.
   - **Suspected infra flake** (quota/throttling/capacity/eventual-consistency/auth/timeout) → report under `flakes`. Do NOT re-run — this stage creates no resources; a human can retry.
   - Log insufficient to determine cause → report under `needs_human` with the excerpt and what is missing.
4. Flag any test that errored before destroy as a possible leaked resource under `leaked_resource_risks`.
5. Leave the tree exactly as you found it — you changed nothing. Write the report (see Deliverables) and exit.

## Assessing a breaking change (what to report, not fix)

Classify a NEW failure as a breaking change when an **intended** target-API contract change —
confirmed in the docs/spec — explains it, OR when the only conceivable fix would touch the
provider's OWN schema (`Default`, `Computed`, `Required`, validation, type, or a property
rename). You REPORT it; a human decides and implements the mitigation. For each, capture:

1. **What changed** — the property/enum/field and its old → new behavior in `<target_api_version>`.
2. **Evidence** — the `azure-rest-api-specs` swagger path proving
   the change (see *Researching an API change*).
3. **User-facing?** — set `schema_surface_changed=true` if it changes a user-facing schema
   attribute; `false` if it is pure API-default absorption (sending the provider's existing
   default so a new Azure default does not leak, with no schema change).
4. **Recommended fix direction** — e.g. "feature-flag gate the new behavior behind
   `!features.FivePointOh()` and add a `website/docs/5.0-upgrade-guide.markdown` entry per
   `contributing/topics/guide-breaking-changes.md`", or "absorb the API default (no flag needed)".
   This is a recommendation for the human — do not implement it here.

## Researching an API change (swagger)

Before classifying a failure as a genuine breaking change (vs. stale test data), confirm what
actually changed between the old and target API versions:

- **Swagger (`Azure/azure-rest-api-specs`)** — inspect the OpenAPI spec for the RP at the target
  version under `specification/<rp>/resource-manager/**/<target_api_version>/` and diff it against
  the old version for the affected property: `type`, `required`, `default`, `enum` values,
  `x-ms-enum` / `x-ms-mutability` and other `x-ms-*` extensions, and the response schema. Fetch a
  single file with `gh api repos/Azure/azure-rest-api-specs/contents/<path>?ref=main` (or its raw
  URL); do not clone or dump the whole spec tree.

Decision:
- an **intended** change confirmed in the docs/spec ⇒ **genuine breaking change** — REPORT it under
  `breaking_changes` with the doc/spec evidence and a recommended fix direction (per
  `contributing/topics/guide-breaking-changes.md`); do NOT implement it here.
- the API **contradicts its own spec** (errors, or returns a shape/value the `<target_api_version>`
  spec does not allow) ⇒ **API bug** — document it in the result with the contradicting evidence and
  set `status: "blocked"`; there is no correct provider-side change.
- **No** such change ⇒ the expectation was merely stale ⇒ report it under `test_side` with the
  suggested change (do NOT edit the `_test.go`).

## Heuristics

- Common test-side signatures after an API upgrade: attribute renamed in HCL, a `Computed` value changed so an assertion's expected value drifts, an enum/string value changed, or `ImportStep()` needs an `ignore` for a now-Computed field.
- When a post-upgrade diff might be intended, confirm it against the target API version via the `azure-rest-api-specs` swagger (see *Researching an API change*) before classifying test-side vs breaking change — never infer intent from the log alone.
- If the only plausible fix would require touching resource/schema code, classify it as a breaking change (report it), NOT as test-side.
- The instant a breaking change is confirmed, REPORT it and STOP — do not triage the remaining NEW failures; a human must resolve the breaking change before the upgrade can continue.
- Never weaken a test: do not skip it, loosen or delete an assertion, or add an `ignore`/`ExpectNonEmptyPlan`, and never recommend doing so. A failing assertion is signal, not noise — report the real cause instead of masking it.
- If the API itself is broken (contradicts its own `<target_api_version>` spec), it is an API bug — report it and set `status: "blocked"`.
- Diagnose from the failing log; never re-run a test and never edit code — this stage only reports. Suspected env/quota/auth issues are reported as flakes. If the cause is unclear, report under needs-human.

## Command Hygiene (long/large output)

- Always read `run.json` with `jq`/grep; never echo full logs or poll repeatedly.
- Never re-run tests, never edit code, and never weaken a test — this stage only reads results and reports.
- Stop as soon as a breaking change is confirmed; leave the remaining NEW failures `pending`.
- Keep scope to `service_name`.
- Surface (don't hide) quota/auth/leaked-resource errors in the report.

## Definition of Done

- The finished run's `run.json` is parsed and NEW failures are isolated from baseline failures.
- EITHER a breaking change was confirmed — in which case it is reported and triage STOPPED (remaining NEW failures may stay `pending`) — OR every NEW failure is classified (test-side / api-bug / flake / needs-human) and reported with evidence.
- Every suspected breaking change is reported with what changed (old → new), the doc/spec evidence, whether it touches user-facing schema, and a recommended fix direction — WITHOUT implementing it.
- Any API bug is reported with contradicting evidence and `status: "blocked"`.
- Leaked-resource risks are flagged.
- No code, tests, `go.mod`, or `vendor` were changed, no test was weakened, and no tests were re-run.

## Status semantics

This stage does not loop and does not act — it produces one report. Set `status` to exactly one:

- `done` — investigation finished with NO breaking change: every NEW failure is classified
  (test-side / api-bug / flake / needs-human) and reported. Use this even when findings exist —
  reporting them IS the deliverable.
- `blocked` — investigation STOPPED because at least one breaking change was confirmed, OR at least
  one API bug was found (Azure contradicts its own `<target_api_version>` spec). Report the finding
  under `breaking_changes` / `api_bugs`; there is no provider-side fix here and a human must act
  before the upgrade continues. Remaining NEW failures may be left `pending`.
- `failed` — the run or the agent itself failed (unusable `run.json`, harness/infra error) so the
  report is incomplete.

There is no `loop_action` and no re-verify: findings are handed to a human, not fixed here.

## Deliverables

- service and `test_regex`,
- counts: total / passed / failed / new-failed / flaky,
- table of NEW failures → category (breaking-change / test-side / api-bug / flake / needs-human),
- per breaking change: test, error excerpt, what changed (old → new), doc/spec evidence,
  `schema_surface_changed`, and a recommended fix direction (not implemented),
- per API bug: test, error excerpt, and the spec/doc the behavior contradicts (evidence); `status: "blocked"`,
- resource-cleanup notes and next actions for a human.
