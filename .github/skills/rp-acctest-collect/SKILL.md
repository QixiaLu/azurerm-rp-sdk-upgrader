---
name: rp-acctest-collect
description: Triage a finished acceptance-test run for a Resource Provider - diff results against the TeamCity main baseline to find NEW failures, fix test-side failures, and mitigate genuine breaking changes strictly per contributing/topics/guide-breaking-changes.md. Use after rp-acctest-launch once the detached run has finished.
---

# RP Acceptance Test Collect & Triage Skill

## When to use

Use this skill when a detached acceptance-test run started by `rp-acctest-launch` has finished
and you need to:

- separate NEW failures from tests already failing on `main` (using the TeamCity baseline),
- fix failures that are caused by test code (HCL config, assertions, import paths),
- mitigate failures caused by a genuine API breaking change, following
  `contributing/topics/guide-breaking-changes.md`,
- document failures caused by an API bug (broken behavior that contradicts the API spec) and stop.

The run is expensive (real Azure resources, many hours), so triage works from the on-disk
`run.json`/`baseline.json` produced by the launch phase, in bounded batches, and only re-runs a
single test to verify a fix it applied.

## Required Inputs

- `service_name` (for example `recoveryservices`)

## Optional Inputs

- `test_regex` (default `TestAcc`)
- `test_phase` (`smoke` or `full`): which run is being triaged. In `smoke` only each resource's
  `_basic` tests ran, so triage only what is in `run.json` for that gate — a full-suite green
  comes later in the `full` phase.

## Required Environment

Only needed for the fix-verification reruns in step 7 (which create real resources):

- `ARM_CLIENT_ID`, `ARM_CLIENT_SECRET`, `ARM_SUBSCRIPTION_ID`, `ARM_TENANT_ID`
- `ARM_TEST_LOCATION`, `ARM_TEST_LOCATION_ALT`, `ARM_TEST_LOCATION_ALT2`

## Procedure

1. Confirm completion: PID in `run.pid` is gone, or `run.json` has a final result line. If still running, report a short tail and exit (do not block).
2. Parse `run.json` (`go test -json`) into the failed-test set (read file, filter with `jq`/grep; never echo the whole file). Load `baseline.json`. NEW failures = local FAIL that are PASS/absent in baseline. Write the NEW set to `.acctest-run/<service_name>/new_failures.txt`.
   - **Scale (hundreds of results):** never carry the full log forward. Reduce once, then process in bounded batches with on-disk state:
     - failed set: `jq -r 'select(.Action=="fail" and .Test!=null).Test' run.json | sort -u`; subtract baseline failures (`comm -23`) → `new_failures.txt`.
     - slice each NEW test's log once into `.acctest-run/<service_name>/logs/<Test>.log` (`jq -r --arg t <Test> 'select(.Test==$t).Output // empty'`) so triage never reloads `run.json`.
     - cluster by failure signature (first `Error:`/assertion line) so one root cause (e.g. a renamed attribute) fixes a whole group in a single edit.
     - keep a `triage.json` ledger (one row per NEW test: `status` ∈ pending/fixed/breaking-change/api-bug/flake/needs-human, `batch`, `file`); process ~5–10 per turn, update the ledger, exit. This keeps turns small and resumable.
     - **Keep context flat:** batching bounds work but not context — reading every `.log` in one turn still accumulates it. Process **one batch per turn** (or delegate each batch to a stateless sub-agent that returns only test→status/file). The orchestrator holds **only `triage.json`**, never raw logs; a turn reads just its batch's `.log` files, then exits.
3. Triage each new failure — analyze the log first, then fix (do NOT re-run to observe):
   - Extract the test's failure output from `run.json` (assertion/diff, Terraform/Azure error, failing step) and diagnose the root cause from the log. Re-running is reserved for verifying a fix (step 4).
   - **When a failure looks API-driven** (changed default/computed value, renamed/repurposed enum, new required field, altered response shape, or a create/read/update contract error), confirm what actually changed in the **target API version** before deciding — see *Researching an API change* below. An intended change in the docs/spec ⇒ genuine breaking change; no change ⇒ treat as test-side.
   - Test-side cause (attribute renamed in HCL, an assertion expecting a *server-computed* value that legitimately changed, import drift) → fix the `_test.go` based on the analysis. A change to the provider's OWN schema (`Default`, `Computed`, `Required`, validation, type, or a property rename) is NOT test-side — it is a breaking change and MUST follow the guide's flag-gating rules below.
   - Genuine breaking change (an **intended** API-contract change confirmed in the docs/spec) → **mitigate it in the resource/schema code** STRICTLY per `contributing/topics/guide-breaking-changes.md`, satisfying the *Breaking-change mitigation contract* below (all items). Then report it with evidence + the guide reference. If it cannot be safely mitigated in this scope, record it under needs-human with the required follow-up. Never edit `go.mod`/`vendor/`.
   - API bug (the API errors or returns a shape/value that **contradicts its own `<target_api_version>` spec** — a defect on the Azure side, not an intended change) → **document it in the result and break**: record the failing test, error excerpt, and the spec/doc the behavior contradicts, set `status: "blocked"`, and stop. There is no correct provider-side workaround; this needs an Azure/API fix.
   - Suspected infra flake (quota/throttling/capacity/eventual-consistency) → report as a suspected flake; do not re-run to confirm.
   - Log insufficient to determine cause → mark needs-human-review with the excerpt.
4. Verify fixes only: after applying a test-side fix, re-run just that test (`-run=^<TestName>$`) to confirm it passes; iterate analyze→fix→verify until green. Ensure no new regressions among touched tests.
5. Report (see Deliverables) and flag tests that errored before destroy (possible leaked resources).

## Breaking-change mitigation contract

A breaking-change mitigation is only "done" when ALL of the following hold. If any is missing,
set `mitigated=false`, move the item to needs-human naming the missing step, and do NOT report
it as fixed:

1. **Current behavior preserved by default.** The new behavior is gated behind
   `if !features.FivePointOh() { … }`; the pre-upgrade behavior remains the default (non-5.0)
   path. Acctests run in the non-5.0 path, so they must stay green without being weakened.
2. **No unconditional schema-surface change.** Never flip `Default`→`Computed`, drop a
   `Default`, change a `Default` value, or promote a field to `Required` outside the flag. The
   only approved mechanisms are feature-flag gating or (where the guide allows) `Required`.
3. **Whole schema block replaced inside the flag**, not inline single-field edits; no inline
   anonymous functions for defaults/validation (per the guide's note).
4. **Upgrade guide updated.** Add an alphabetical entry under
   `website/docs/5.0-upgrade-guide.markdown`. Do NOT add "in 5.0" notes to resource/data-source
   docs.
5. **Existing acctests still pass** in the non-5.0 path without weakening them; if the new
   behavior needs coverage, gate the test config per the guide's "Update the test
   configurations" example.
6. **Report cites evidence** — the doc/spec reference and the exact guide section applied — and
   sets the per-entry booleans (`feature_flag_gated`, `current_behavior_preserved`,
   `upgrade_guide_updated`, `schema_surface_changed`, `mitigated`) in the result.

**Exception — API-default absorption (no flag needed):** sending the provider's EXISTING default
explicitly so a new Azure API default does not leak, with NO change to any user-facing schema
attribute, is not a breaking change. Report it with `schema_surface_changed=false` and it may be
`mitigated=true` without a feature flag or upgrade-guide entry. This is the ONLY case where a
mitigation is done without items 1–4.

## Researching an API change (Microsoft Learn MCP + swagger)

Before classifying a failure as a genuine breaking change (vs. stale test data), confirm what
actually changed between the old and target API versions:

- **Microsoft Learn MCP** — run `microsoft_docs_search`, then `microsoft_docs_fetch` on the best
  result, for the RP's REST API reference and any "what's new" / changelog covering
  `<target_api_version>`. Look for changed defaults, renamed or repurposed enum values, newly
  required fields, or altered response shapes for the failing attribute.
- **Swagger (`Azure/azure-rest-api-specs`)** — inspect the OpenAPI spec for the RP at the target
  version under `specification/<rp>/resource-manager/**/<target_api_version>/` and diff it against
  the old version for the affected property: `type`, `required`, `default`, `enum` values,
  `x-ms-enum` / `x-ms-mutability` and other `x-ms-*` extensions, and the response schema. Fetch a
  single file with `gh api repos/Azure/azure-rest-api-specs/contents/<path>?ref=main` (or its raw
  URL); do not clone or dump the whole spec tree.

Decision:
- an **intended** change confirmed in the docs/spec ⇒ **genuine breaking change** — mitigate it in
  the resource/schema code per `contributing/topics/guide-breaking-changes.md` (or record it under
  needs-human when it can't be safely mitigated), citing the doc/spec reference; do not mask it by
  weakening the test.
- the API **contradicts its own spec** (errors, or returns a shape/value the `<target_api_version>`
  spec does not allow) ⇒ **API bug** — document it in the result with the contradicting evidence and
  break (`status: "blocked"`); there is no correct provider-side change.
- **No** such change ⇒ the expectation was merely stale ⇒ fix the `_test.go`.

## Heuristics

- Common test-side fixes after an API upgrade: attribute renamed in HCL, a `Computed` value changed so an assertion's expected value drifts, an enum/string value changed, or `ImportStep()` needs an `ignore` for a now-Computed field.
- When a post-upgrade diff might be intended, confirm it against the target API version via Microsoft Learn MCP and the `azure-rest-api-specs` swagger (see *Researching an API change*) before deciding test-side vs breaking change — never infer intent from the log alone.
- Prefer the smallest assertion/config change that reflects the new (intended) behavior; do not weaken a test just to make it pass.
- If a "fix" would require touching resource/schema code, it is a genuine breaking change — mitigate it per `contributing/topics/guide-breaking-changes.md` (or record needs-human when it can't be safely mitigated), not by weakening the test.
- If the API itself is broken (contradicts its own `<target_api_version>` spec), it is an API bug, not a provider fix — document it in the result and break (`status: "blocked"`).
- Diagnose from the failing log; re-run a test only to verify a fix you applied. Suspected env/quota/auth flakes are reported, not re-run. If the cause is unclear, mark needs-human-review.

## Command Hygiene (long/large output, real resources)

- Always read `run.json` with `jq`/grep; never echo full logs or poll repeatedly.
- Only re-run a single test (`-run=^<TestName>$`) to verify a fix you applied — never re-run the whole suite to observe.
- Keep scope to `service_name`; don't widen `-run` beyond need.
- Surface (don't hide) quota/auth/leaked-resource errors.

## Definition of Done

- The finished run's `run.json` is parsed and NEW failures are isolated from baseline failures.
- Every new test-side failure is fixed and passes on rerun.
- Every genuine breaking change is mitigated per `contributing/topics/guide-breaking-changes.md` (or recorded under needs-human when it can't be safely mitigated) and reported with evidence and a guide reference; `go.mod`/`vendor/` are never changed.
- Every breaking-change mitigation satisfies the *Breaking-change mitigation contract*: it is feature-flag gated (keeping current behavior in the non-5.0 path) AND has an entry in `website/docs/5.0-upgrade-guide.markdown`, OR is a pure API-default absorption with `schema_surface_changed=false`. A schema-surface change with neither a flag nor an upgrade-guide entry is a regression, not a fix.
- Any API bug is documented in the result with contradicting evidence, and the run is stopped (`status: "blocked"`).
- Leaked-resource risks are flagged.

## Status semantics (drives the loop)

You OWN the control-flow decision — declare it explicitly in the result as **`loop_action`**; the
orchestrator obeys that field and does not re-derive it from prose. Set exactly one of:

- `reverify` — you changed code this round (a test-side fix, or a breaking change with
  `mitigated=true`) OR triage is unfinished (`pending` rows remain). The loop re-launches the suite
  to re-verify.
- `converged` — every row is resolved and you changed no code this round. The loop stops (success).
- `api_bug` — at least one API bug found (Azure contradicts its own `<target_api_version>` spec).
  The loop stops; this is a real **detection**, recorded under `api_bugs`.
- `needs_human` — only non-code failures remain (subscription/quota/capacity/VM-size/region limits,
  flakes, or breaking changes safely deferred to a human). Re-running would just re-hit them, so the
  loop stops. **Not** a tool failure.
- `tool_error` — the run or the agent itself failed (build broke, harness/infra error, unusable
  `run.json`). The loop stops; this is a **run fault**, not a finding.

Keep `status` consistent with `loop_action`: `converged`→done, `reverify`→in-progress/done,
`api_bug`→blocked, `tool_error`→failed, `needs_human`→done. Notes that still hold:

- `needs-human` and `flake` are **terminal** classifications, not `pending`. A failure only a human/operator can fix — subscription/quota limits, a disallowed VM size in the test region, missing capacity, or any non-code environment issue — is triaged into `needs_human` and is **done** from the loop's perspective (`loop_action=needs_human`).
- Set `status: "in-progress"` **only** when `pending` rows still remain to triage in a later batch (you stopped mid-triage to keep the turn small); pair it with `loop_action=reverify`. Never use `in-progress` to "wait" for a human or an environment change: the loop cannot alter the environment, so re-running just re-hits the same wall and burns hours of real Azure runs.
- When nothing is `pending` — even if some rows are `needs-human`/`flake` and you changed no code this turn — set `status: "done"` with `loop_action=converged` or `needs_human` as appropriate.
- If you truly cannot determine `loop_action`, omit it; the orchestrator falls back to deriving the outcome from `status`/`api_bugs`/`fixed`. Prefer to set it.

## Deliverables

- service, `test_regex`, and which `test_phase` was triaged,
- counts: total / passed / failed / new-failed / flaky,
- table of new failures → fixed-via-test (file) vs breaking-change-mitigated (files) vs api-bug (blocked),
- per breaking change: test, error excerpt, suspected API change, guide reference, and the mitigation applied (or the needs-human reason if it couldn't be safely mitigated),
- per API bug: test, error excerpt, and the spec/doc the behavior contradicts (evidence); set `status: "blocked"` to stop the run,
- resource-cleanup notes and exact next actions for anything unresolved.
