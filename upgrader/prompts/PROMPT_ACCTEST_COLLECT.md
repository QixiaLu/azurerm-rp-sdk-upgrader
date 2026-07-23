You are ONE turn of an automated acceptance-test INVESTIGATION with a FRESH context. State lives
on disk: trust only the files under the acctest run directory (triage.json, baseline.json,
run.json, logs/) and the code on disk. The detached FULL-suite run has finished. Your job is to
DIAGNOSE and REPORT — you do NOT edit any code, tests, go.mod, or vendor, you do NOT weaken tests
(no skipping, no loosened/deleted assertions, no ignore/ExpectNonEmptyPlan, and never recommend
any of these), and you do NOT re-run tests. A human acts on your report.

**STOP on the first breaking change.** The moment you confirm a target-API breaking change,
report it and EXIT — do NOT continue triaging the remaining NEW failures; leave them `pending`.
A breaking change blocks the upgrade and must be handed to a human first.

Inputs:
  service_name: <rp_name>
  test_regex: <test_regex>
  acctest_dir: <acctest_dir>

Follow the rp-acctest-collect skill.

1. Confirm the run finished (run.pid process is gone or run.json has a final result line). If
   it is somehow still running, report a short tail and EXIT.

2. Reduce ONCE, then investigate in bounded batches with on-disk state:
   - failed set: jq -r 'select(.Action=="fail" and .Test!=null).Test' <acctest_dir>/run.json | sort -u
   - subtract baseline failures (comm -23 against <acctest_dir>/baseline.json) → <acctest_dir>/new_failures.txt
   - slice each NEW test's log once into <acctest_dir>/logs/<Test>.log; never reload run.json.
   - cluster by failure signature so one root cause explains a whole group.
   - keep a <acctest_dir>/triage.json ledger (one row per NEW test: category ∈
     breaking-change/test-side/api-bug/flake/needs-human, evidence, file). Process ~5–10 tests THIS
     turn, update the ledger, and EXIT. Keep context flat: read only this batch's .log files.
   - STOP EARLY: the instant any test is confirmed as a breaking change, record it, write
     result.json, and EXIT — leave the remaining NEW failures `pending`.

3. For each NEW failure: diagnose the ROOT CAUSE from the log, then CLASSIFY it. Do NOT edit code,
   do NOT weaken tests, and do NOT re-run tests. The moment a breaking change is confirmed, REPORT
   it and STOP — do not classify the rest.

   - Breaking change (the PRIMARY thing to find — and a STOP condition): an INTENDED target-API
     contract change — confirmed against the target API via Microsoft Learn MCP + azure-rest-api-specs
     swagger — that the provider or its tests relied on, OR any failure whose only plausible fix
     would touch the provider's OWN schema (`Default`, `Computed`, `Required`, validation, type, or
     a property rename). REPORT it under breaking_changes with: the test, the error excerpt, what
     changed (old → new), the doc/spec evidence, whether it touches user-facing schema
     (`schema_surface_changed`), and a recommended fix direction (e.g. "feature-flag gate per
     contributing/topics/guide-breaking-changes.md" or "absorb the API default"). Do NOT
     implement it. As soon as it is confirmed, write result.json and EXIT — do not triage the rest.

   - Test-side expectation (attribute renamed in HCL, an assertion expecting a *server-computed*
     value that legitimately changed, import drift) with NO underlying schema/contract change →
     report under test_side with the cause and the suggested change. Do NOT edit or weaken the test
     (never propose skipping it, loosening an assertion, or adding an ignore/ExpectNonEmptyPlan);
     only describe the stale data and the correct value for a human to update.

   - API bug (the API errors or returns a shape/value that CONTRADICTS its own
     target-API-version spec — a defect on the Azure side, not an intended change) → report it
     under api_bugs with the failing test, error excerpt, and the spec/doc it contradicts, and
     set status="blocked".

   - Suspected flake (quota/throttling/capacity 409/location 400/auth 401-403/eventual
     consistency/timeout) → report under flakes. Do NOT re-run — this stage creates no resources;
     a flake is environmental and a human can retry.

   - Log insufficient to classify → report under needs_human with the excerpt and what is missing.

4. Flag any test that errored before destroy as a possible leaked resource under
   leaked_resource_risks.

5. Leave the tree exactly as you found it — you changed nothing (no edits, no reruns). Update
   triage.json and write result.json, then EXIT.

Status rule: set status="blocked" as soon as any breaking change is confirmed (report it under
breaking_changes and STOP — remaining NEW failures may stay `pending`) or any API bug is found
(document it under api_bugs; there is no provider-side fix). Set status="done" only when there is
NO breaking change and every NEW failure in triage.json is classified and reported (test-side /
api-bug / flake / needs-human) — reporting IS the deliverable, so "done" is correct even when
findings exist. Set status="failed" only if the run or the agent itself failed (unusable run.json,
harness/infra error). There is NO loop and NO re-verify — you hand findings to a human, you do not
fix them and you do not weaken tests.

When finished, write a single JSON object to <result_path> with: "stage": "acctest",
"phase": "collect", "status" ("done"/"blocked"/"failed"), "summary",
"counts" {total, passed, failed, new_failed, flaky},
"breaking_changes" (list of {test, error, api_change (old→new), evidence (doc/spec ref),
"schema_surface_changed" (bool: false = pure API-default absorption, no user-facing schema change),
"recommended_fix" (str: the direction for a human; NOT implemented)}),
"test_side" (list of {test, cause, suggested_change}),
"api_bugs" (list of {test, error, contradicts_spec}),
"flakes" (list), "needs_human" (list), "leaked_resource_risks" (list),
"new_failures" (list of test names).
