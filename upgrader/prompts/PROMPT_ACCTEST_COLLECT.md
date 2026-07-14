You are ONE turn of an automated acceptance-test triage loop with a FRESH context. State
lives on disk: trust only the files under the acctest run directory (triage.json, baseline.json,
run.json, logs/) and the code on disk. This is the COLLECT phase — the detached run has
finished. Do ONE bounded batch of triage, record it, and EXIT.

Inputs:
  service_name: <rp_name>
  test_regex: <test_regex>
  acctest_dir: <acctest_dir>
  test_phase: <test_phase>       # "smoke" (only _basic tests ran) or "full" (whole suite)
  phase_scope: <phase_scope>

Follow the rp-acctest-collect skill. Note: in the "smoke" test_phase only
<phase_scope> ran, so triage/fix only what is in run.json for this gate — a full-suite green
comes later in the "full" test_phase.

1. Confirm the run finished (run.pid process is gone or run.json has a final result line). If
   it is somehow still running, report a short tail and EXIT.

2. Reduce ONCE, then work in bounded batches with on-disk state:
   - failed set: jq -r 'select(.Action=="fail" and .Test!=null).Test' <acctest_dir>/run.json | sort -u
   - subtract baseline failures (comm -23 against <acctest_dir>/baseline.json) → <acctest_dir>/new_failures.txt
   - slice each NEW test's log once into <acctest_dir>/logs/<Test>.log; never reload run.json.
   - cluster by failure signature so one root cause fixes a whole group in one edit.
   - keep a <acctest_dir>/triage.json ledger (one row per NEW test: status ∈
     pending/fixed/breaking-change/api-bug/flake/needs-human, batch, file). Process ~5–10 tests THIS
     turn, update the ledger, and EXIT. Keep context flat: read only this batch's .log files.

3. For each new failure: diagnose from the log first.

   - Test-side cause (attribute renamed in HCL, an assertion expecting a *server-computed*
     value that legitimately changed, import drift) → fix the *_test.go only. NOTE: a change
     to the provider's OWN schema (`Default`, `Computed`, `Required`, validation, type, or a
     property rename) is NOT test-side — it is a breaking change, handle it below.

   - Genuine breaking change (an INTENDED API-contract change confirmed against the target API
     via Microsoft Learn MCP + azure-rest-api-specs swagger) → MITIGATE it in the
     resource/schema code STRICTLY per contributing/topics/guide-breaking-changes.md. A
     mitigation that skips any of the mandatory rules below is NOT done — record it under
     needs_human instead. Mandatory rules:
     * Preserve current behavior by default. ANY change to a property's `Default`, `Computed`,
       `Required`, validation, type, or name MUST be gated behind `if !features.FivePointOh()
       { … }`, keeping the pre-upgrade behavior in the non-5.0 (default) path. Acctests run in
       the non-5.0 path, so current behavior — and current tests — must stay green WITHOUT
       weakening them.
     * Do NOT silence plan drift by flipping `Default`→`Computed`, dropping a `Default`, or
       changing a `Default` value unconditionally. The only approved mechanisms are (a)
       feature-flag gating the new behavior, or (b) marking the field `Required` (see the
       guide's "Updating Default Values").
     * Replace the WHOLE schema block inside the `!features.FivePointOh()` gate rather than
       inline single-field edits; no inline anonymous functions for defaults/validation.
     * Add the upgrade-guide entry under website/docs/5.0-upgrade-guide.markdown (alphabetical).
       Do NOT add "will do x in 5.0" notes to the resource/data-source docs.
     Then report it with evidence + the guide reference and the per-entry booleans defined in
     the result schema. If it cannot be safely mitigated in this scope, record it under
     needs_human with the required follow-up.

   - API-default absorption (NOT a breaking change): sending the provider's EXISTING default
     explicitly so a new Azure API default does not leak (no user-facing schema attribute
     changes) is allowed WITHOUT a feature flag. Report it with `schema_surface_changed=false`
     so it is not confused with a schema change.

   - API bug (the API errors or returns a shape/value that CONTRADICTS its own
     target-API-version spec — a defect on the Azure side, not an intended change) → DOCUMENT it
     under api_bugs with the failing test, error excerpt, and the spec/doc it contradicts, set
     status="blocked", and STOP: there is no correct provider-side change.

   - Suspected flake (quota/throttling/capacity 409/location 400/auth 401-403/eventual
     consistency/timeout) → isolate it: add it to <acctest_dir>/flakes.txt and retry ONLY that
     test ONCE in-place (-run=^<TestName>$). If it passes, drop it; if it still fails the same
     infra way, flag it as a suspected flake and move on. NEVER let a flake trigger a broader
     re-run or block the phase — flakes are environmental, not code, and re-running the whole
     suite just re-rolls them. Never edit go.mod/vendor.

4. Verify only fixes you applied by re-running just that test (-run=^<TestName>$). Flag tests
   that errored before destroy (possible leaked resources).

5. Leave all edits unstaged; never git commit/push/checkout. Update triage.json and write
   result.json, then EXIT.

Status rule: set status="done" ONLY when every row in triage.json is resolved (no "pending"):
each new test-side failure is fixed and passes on rerun, every genuine breaking change is
mitigated per the guide (or recorded under needs_human when it can't be) and reported with
evidence, and flakes/needs-human are flagged. NOTE: `needs-human` and `flake` are TERMINAL
(resolved) classifications, NOT "pending" — a row you have triaged into needs_human (quota, VM-size
or other subscription/environment limits, missing region capacity, or anything only a human/operator
can fix) is DONE from the loop's point of view. Do NOT set status="in-progress" to "wait" for a
human or environment fix: the loop cannot change the environment, so re-running just re-hits the
same wall and burns hours of real Azure runs. Set status="in-progress" ONLY when there are still
`pending` rows left to triage in a further batch (i.e. you stopped mid-triage to keep the turn
small). If nothing is `pending` — even if some rows are needs_human/flake and you changed no code —
set status="done". Set status="blocked" as soon as any API bug is found — document it under
api_bugs and stop (no provider-side fix exists).

Breaking-change gate: a breaking_changes entry may set `mitigated=true` ONLY if either
(a) `schema_surface_changed=false` (pure API-default absorption, no user-facing schema change),
or (b) `feature_flag_gated` AND `current_behavior_preserved` AND `upgrade_guide_updated` are ALL
true. Otherwise set `mitigated=false` and move the item to needs_human naming the missing step.
A schema-surface change with neither a feature flag nor an upgrade-guide entry is a REGRESSION,
not a fix — never mark it mitigated or done.

Round note: verify each fix per-test here (-run=^<TestName>$) before it counts as done — do NOT
relaunch the whole suite just to validate a fix (a full re-run also re-rolls the environmental
flakes). After smoke is green, the loop launches `full` once; after a `full` round that changed
any code — a test-side fix (listed in "fixed") OR a breaking-change mitigation (listed in
"breaking_changes" with mitigated=true) — it relaunches `full` ONCE to confirm the suite is
clean. Report empty "fixed" AND no mitigated breaking changes only when you changed no code at
all (a clean full round ends the loop early).

Loop action (REQUIRED — this is the control-flow signal the orchestrator obeys): set
`loop_action` to exactly one of the following. It, not the prose above, decides whether the
launch->wait->collect loop runs another round. You classify; the orchestrator only reads this
field.
- `reverify` — you changed code this round (a test-side fix in "fixed", or a breaking change
  with mitigated=true) OR triage is not finished (pending rows remain for a further batch). The
  loop will re-launch the suite to re-verify.
- `converged` — nothing left to change: every row is resolved (fixed/mitigated/flake/needs-human)
  and you changed no code this round. The loop stops as success.
- `api_bug` — at least one API bug found (Azure contradicts its own <target_api_version> spec).
  The loop stops; this is a real DETECTION, recorded under api_bugs.
- `needs_human` — only non-code failures remain (env/quota/capacity/VM-size/region limits). Re-running would just re-hit them, so the loop stops. This is NOT a tool failure.
- `tool_error` — the run or the agent itself failed (build broke, harness/infra error, unusable
  run.json). The loop stops; this is a RUN FAULT, not a finding.
Keep `status` consistent with it: `converged`->done, `reverify`->in-progress/done,
`api_bug`->blocked, `tool_error`->failed, `needs_human`->done. If you cannot determine one,
omit `loop_action` and the orchestrator will fall back to deriving it from status/api_bugs/fixed.

When finished, write a single JSON object to <result_path> with: "stage": "acctest",
"phase": "collect", "status" ("done"/"in-progress"/"blocked"/"failed"),
"loop_action" ("reverify"/"converged"/"api_bug"/"needs_human"/"tool_error"), "summary",
"counts" {total, passed, failed, new_failed, flaky}, "fixed" (list of {test, file}),
"breaking_changes" (list of {test, error, suspected_api_change, guide_ref, mitigation, files,
"feature_flag_gated" (bool: change gated behind !features.FivePointOh()),
"current_behavior_preserved" (bool: non-5.0 path unchanged and existing tests pass unedited),
"upgrade_guide_updated" (bool: entry added to website/docs/5.0-upgrade-guide.markdown),
"schema_surface_changed" (bool: false = API-default absorption with no user-facing schema change),
"mitigated" (bool: subject to the Breaking-change gate above)}),
"api_bugs" (list of {test, error, contradicts_spec}),
"needs_human" (list), "leaked_resource_risks" (list).
