

# RP Upgrade Orchestration — Design

End-to-end flow: one dispatch `(rp_name, target_api_version)` chains the workers,

## High-level flow

```text
Manual trigger (custom prompt) -> api upgrade agent -> acceptance tester (investigate & report) -> report
```

## Notes
- Once the api upgrade agent finishes with a green build, the orchestrator invokes the acceptance tester.
- The acceptance tester launches the full suite once (it does NOT fix anything), then a watch
  script polls the detached run's PID in the background; when it finishes, the tester is invoked
  to look into the results.
- The tester diffs NEW failures against the TeamCity `main` baseline and writes a categorized
  report — in particular, suspected **API breaking changes** with evidence — for a human to act
  on. It never edits code and never re-runs tests.
- The acctest stage is **advisory**: only the upgrade's green build decides success, so a slow
  or flaky test run never fails a run whose upgrade converged.
