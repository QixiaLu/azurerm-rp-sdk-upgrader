---
name: rp-acctest-launch
description: Capture the TeamCity main baseline for a Resource Provider and start a detached acceptance-test run, then exit. Use after upgrading an RP API version to kick off validation; pair with rp-acctest-collect to triage the results once the run finishes.
---

# RP Acceptance Test Launch Skill

## When to use

Use this skill when you need to:

- start a service's acceptance tests locally after an API-version upgrade,
- capture the TeamCity `main` baseline that the later triage diffs against,
- run a cheap **smoke gate** (each resource's `_basic`) before committing to the full suite.

Acceptance tests create real Azure resources and can take many hours, so this skill only
**launches** the run detached and exits — it never waits for results. Triage happens later with
the `rp-acctest-collect` skill once the detached run finishes.

To avoid spending many hours on a full run that a trivial regression would have caught, launch a
**smoke gate first**: only the representative tests (each resource's `_basic`), and only launch
the full suite once smoke is green. A `test_phase` input distinguishes `smoke` (restricted
`-run`) from `full`.

## Required Inputs

- `service_name` (for example `recoveryservices`)

## Optional Inputs

- `test_regex` (default `TestAcc`)
- `teamcity_env` (default `PUBLIC`)
- `test_phase` (`smoke` or `full`, default `smoke`): `smoke` runs only each resource's
  `_basic` tests as a cheap gate; `full` runs the whole `test_regex` suite and must only be
  launched after smoke is green.
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
3. Choose the tests for this `test_phase` and build the `-run` filter. The first launch for a service is ALWAYS smoke, even if the input says `full` — a full first run wastes hours a trivial regression would have caught by the cheap gate:
   - `full` → run the whole suite: `-run=<test_regex>`.
   - `smoke` → enumerate top-level `func TestAcc…(t *testing.T)` in
     `internal/services/<service_name>/*_test.go` matching `test_regex`, keep only names ending
     in `_basic`, and build an anchored filter
     `-run='^(<Name1>|<Name2>|...)$'` (record them in `.acctest-run/<service_name>/smoke_tests.txt`).
     Fall back to `-run=<test_regex>` if none match.
4. Launch the run DETACHED with the phase's `-run` filter, recording its PID so an external watcher can poll it:
   ```bash
   nohup make acctests SERVICE='<service_name>' \
     TESTARGS='-run=<run_filter> -json' TESTTIMEOUT='<timeout>m' \
     > .acctest-run/<service_name>/run.json \
     2> .acctest-run/<service_name>/run.err &
   echo $! > .acctest-run/<service_name>/run.pid
   ```
   Record start time + phase + the `-run` filter in `.acctest-run/<service_name>/meta.json`. Report and exit the turn — **do not wait**.
5. Leave all edits unstaged; never git commit/push/checkout. Write the result and EXIT.

## Command Hygiene (long/large output, real resources)

- Always redirect acctest output to a file; never echo full logs or poll repeatedly.
- Never block a turn on a multi-hour foreground command — launch detached and exit; triage in a later turn with `rp-acctest-collect`.
- Keep scope to `service_name`; don't widen `-run` beyond need.
- Surface (don't hide) missing-credential / auth errors before creating any resources.

## Definition of Done

- All required env vars were verified present.
- The TeamCity `main` baseline exists at `.acctest-run/<service_name>/baseline.json` (or degraded mode is noted).
- The detached run is started, `run.pid` is written, and `meta.json` records the phase + `-run` filter.
- The turn exited without waiting for the run.

## Deliverables

- service, `test_regex`, and the chosen `test_phase` + `-run` filter,
- `baseline_mode` (`normal`/`degraded`),
- `pid_file` / `run_json` paths for the watcher,
- a clear "launched" (or "failed", with the missing prerequisite) status.
