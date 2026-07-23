---
name: rp-acctest-launch
description: Capture the TeamCity main baseline for a Resource Provider and start a detached acceptance-test run, then exit. Use after upgrading an RP API version to kick off validation; pair with rp-acctest-collect to investigate and report the results once the run finishes.
---

# RP Acceptance Test Launch Skill

## When to use

Use this skill when you need to:

- start a service's acceptance tests locally after an API-version upgrade,
- capture the TeamCity `main` baseline that the later investigation diffs against.

Acceptance tests create real Azure resources and can take many hours, so this skill only
**launches** the run detached and exits — it never waits for results. Investigation happens later
with the `rp-acctest-collect` skill once the detached run finishes.

The launch runs the **whole suite** matching `test_regex` in one detached run; there is no smoke
gate. Investigation is report-only (report NEW failures and API breaking changes; never fix).

## Required Inputs

- `service_name` (for example `recoveryservices`)

## Optional Inputs

- `test_regex` (default `TestAcc`)
- `parallel` — max acctests to run together via `go test -parallel N` (default `11`)
- `teamcity_env` (default `PUBLIC`)
- `timeout` minutes for `TESTTIMEOUT` (default `0`, i.e. no Go test timeout)

## Required Environment

- `ARM_CLIENT_ID`, `ARM_CLIENT_SECRET`, `ARM_SUBSCRIPTION_ID`, `ARM_TENANT_ID`
- `ARM_TEST_LOCATION`, `ARM_TEST_LOCATION_ALT`, `ARM_TEST_LOCATION_ALT2`
- `TEAMCITY_TOKEN`, `TEAMCITY_SERVER_URL` (default `https://hashicorp.teamcity.com`)

If any are missing, stop and report before creating any resources.

## Procedure

1. Preflight: verify all required env vars; warn that real Azure resources will be created and the run may take hours. Create `.acctest-run/<service_name>/`.
2. Capture the TeamCity baseline of known failures ONCE per upgrade, then reuse it. If `.acctest-run/<service_name>/baseline.json` already exists (a prior launch wrote it), skip this step — the `main` baseline is a fixed reference and re-pulling it wastes a slow paginated TeamCity call and risks drift. Only when it is absent, run the bundled helper (stdlib-only Python 3; no pip install needed). It resolves the build config id, finds the latest `main` build, pages through all test occurrences, and writes the name→status map:
   ```bash
   python .github/skills/rp-acctest-launch/get-latest-build.py \
     --service '<service_name>' --teamcity-env '<teamcity_env>' \
     --output .acctest-run/<service_name>/baseline.json
   ```
   - It reads the token from `TEAMCITY_TOKEN` (or `TEAMCITY_ACCESSTOKEN`) and the server from `TEAMCITY_SERVER_URL` (default `https://hashicorp.teamcity.com`).
   - `--service` expands to `TF_AzureRM_AZURERM_SERVICE_<TEAMCITY_ENV_UPPER>_<SERVICE_UPPER>` (the `TF_AzureRM_` project prefix plus the `uniqueID` from `.teamcity/components/build_config_service.kt`). For a non-standard config, pass `--build-type <full_id>` instead.
   - Add `--status SUCCESS` to baseline only against the last green `main` build; omit it to use the latest build of any status.
   - If no build matches, the helper writes an empty baseline (`{}`) and exits `2` — note degraded mode (all failures treated as new) and continue.
3. Build the `-run` filter for the FULL suite: just `<test_regex>` (the whole suite matching the
   regex; the default `TestAcc` runs everything for the service). There is no smoke gate — run
   the whole suite once. Keep the filter as the RAW RE2 pattern (no shell quotes here); step 4
   adds the quoting.
4. Launch the run DETACHED with that `-run` filter, recording its PID so an external watcher can poll it.

   **Quoting is critical.** The `-run` pattern can contain `(`, `|`, and `$`, which are BOTH `make` and `/bin/sh` metacharacters. `make` expands `$(TESTARGS)` UNQUOTED into a `/bin/sh` (dash) command, so the pattern must carry its own quoting and escape `$` for `make`:
   - keep the whole `TESTARGS=...` in SINGLE quotes at your shell,
   - wrap the regex in DOUBLE quotes so dash treats `(` and `|` as literals,
   - write every literal `$` in the regex as `$$` so `make` emits a single `$` (an un-doubled `$` is silently eaten).

   So for the default full filter `TestAcc`, launch with (`-parallel <parallel>` caps how many
   tests run concurrently; default `11`):
   ```bash
   nohup make acctests SERVICE='<service_name>' \
     TESTARGS='-run="TestAcc" -parallel <parallel> -json' TESTTIMEOUT='<timeout>m' \
     > .acctest-run/<service_name>/run.json \
     2> .acctest-run/<service_name>/run.err &
   echo $! > .acctest-run/<service_name>/run.pid
   ```
   (If `<test_regex>` contains RE2 metacharacters like `(`, `|`, or `$`, the double-quote + `$$`
   rules above keep them intact through `make` → `/bin/sh`.)
   Record start time + the `-run` filter in `.acctest-run/<service_name>/meta.json`. Report and exit the turn — **do not wait**.
5. Leave all edits unstaged; never git commit/push/checkout. Write the result and EXIT.

## Command Hygiene (long/large output, real resources)

- Always redirect acctest output to a file; never echo full logs or poll repeatedly.
- Never block a turn on a multi-hour foreground command — launch detached and exit; investigate in a later turn with `rp-acctest-collect`.
- Keep scope to `service_name`; don't widen `-run` beyond need.
- Surface (don't hide) missing-credential / auth errors before creating any resources.

## Definition of Done

- All required env vars were verified present.
- The TeamCity `main` baseline exists at `.acctest-run/<service_name>/baseline.json` (or degraded mode is noted).
- The detached run is started, `run.pid` is written, and `meta.json` records the `-run` filter.
- The turn exited without waiting for the run.

## Deliverables

- service, `test_regex`, and the chosen `-run` filter,
- the `-parallel` value used,
- `baseline_mode` (`normal`/`degraded`),
- `pid_file` / `run_json` paths for the watcher,
- a clear "launched" (or "failed", with the missing prerequisite) status.
