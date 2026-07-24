Inputs:
  service_name: <rp_name>
  test_regex: <test_regex>
  parallel: <parallel>
  acctest_dir: <acctest_dir>

Follow the rp-acctest-launch skill:

1. Preflight: confirm the required env vars are present (ARM_CLIENT_ID, ARM_CLIENT_SECRET,
   ARM_SUBSCRIPTION_ID, ARM_TENANT_ID, ARM_TEST_LOCATION, ARM_TEST_LOCATION_ALT,
   ARM_TEST_LOCATION_ALT2, TEAMCITY_TOKEN, TEAMCITY_SERVER_URL). If any are missing, write a
   result with status="failed" explaining which, and EXIT before creating any resources.
   WARNING: this creates real, billable Azure resources and may run for hours.

2. Capture the TeamCity main baseline ONCE for the whole loop, then reuse it. If
   <acctest_dir>/baseline.json already exists (a prior launch wrote it), skip this step and
   reuse it as-is — do NOT re-fetch every round: the `main` baseline is a fixed reference for
   this upgrade, and re-pulling it wastes a slow paginated TeamCity call and risks drift. Only
   when it is absent, capture it with the bundled helper:
     python .github/skills/rp-acctest-launch/get-latest-build.py \
       --service '<rp_name>' --output <acctest_dir>/baseline.json
   If no build matches it writes an empty baseline and exits 2 — note degraded mode and continue.

3. Build the -run filter for the FULL suite: just `<test_regex>` (the whole suite). There is no
   smoke gate — run everything once. Keep the filter as the RAW RE2 pattern (no shell quotes);
   step 4 adds the quoting.

4. Launch the run DETACHED with that -run filter, recording its PID so an external watcher can
   poll it. Disable the Go test timeout (TESTTIMEOUT=0) so a long suite is never cut off.
   QUOTING IS CRITICAL: `make` expands $(TESTARGS) UNQUOTED into a `/bin/sh` (dash) command, and
   a `-run` pattern can contain `(`, `|`, and `$` (dash + make metacharacters). So keep TESTARGS
   in SINGLE quotes, wrap the regex in DOUBLE quotes (dash then treats `(`/`|` as literal), and
   double every literal `$` as `$$` (so `make` emits one `$` instead of eating it). Also cap the
   concurrency with `-parallel <parallel>` (how many tests run together; default 11). Substitute
   the pattern from step 3 for <run_filter> (writing any literal `$` as `$$`):
     nohup make acctests SERVICE='<rp_name>' \
       TESTARGS='-run="<run_filter>" -parallel <parallel> -json' TESTTIMEOUT='0' \
       > <acctest_dir>/run.json 2> <acctest_dir>/run.err &
     echo $! > <acctest_dir>/run.pid
   e.g. TESTARGS='-run="TestAcc" -parallel <parallel> -json'.
   Record start time + the -run filter in <acctest_dir>/meta.json. Then REPORT and EXIT — do not
   wait.

5. Leave all edits unstaged; never git commit/push/checkout. Write result.json and EXIT.

When finished, write a single JSON object to <result_path> with: "stage": "acctest",
"phase": "launch", "status" ("launched"/"failed"), "summary", "pid_file", "run_json",
"run_filter", "parallel", "baseline_mode" ("normal"/"degraded"), "test_regex".
Set status="launched" only once the detached run is started and run.pid is written; set
status="failed" (with the reason) if preflight fails or `make acctests` did not start.
