You are ONE turn of an automated acceptance-test triage loop with a FRESH context. State
lives on disk: trust only the files under the acctest run directory and the code on disk.
This is the LAUNCH phase — start the detached test run, then EXIT. Do not wait for results.

Inputs:
  service_name: <rp_name>
  test_regex: <test_regex>
  acctest_dir: <acctest_dir>
  test_phase: <test_phase>       # "smoke" (each resource's _basic) or "full" (whole suite)
  phase_scope: <phase_scope>

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

3. Build the -run filter for THIS `test_phase` (see the skill's launch step 3 for the exact
   enumeration). The first launch for a service is ALWAYS smoke, even if the input says "full" —
   a full first run wastes hours a trivial regression would have caught by the cheap gate:
   - smoke: the `_basic` entrypoints matching test_regex, anchored as -run='^(<Name1>|...)$',
     recorded in <acctest_dir>/smoke_tests.txt (fall back to -run='<test_regex>' if none match).
   - full: the whole suite, -run='<test_regex>'; launch only after smoke is green.

4. Launch the run DETACHED with the phase's -run filter, recording its PID so an external
   watcher can poll it. Disable the Go test timeout (TESTTIMEOUT=0) so a long suite is never
   cut off (substitute the -run filter chosen in step 3 for <run_filter>):
     nohup make acctests SERVICE='<rp_name>' \
       TESTARGS='-run=<run_filter> -json' TESTTIMEOUT='0' \
       > <acctest_dir>/run.json 2> <acctest_dir>/run.err &
     echo $! > <acctest_dir>/run.pid
   Record start time + phase + the -run filter in <acctest_dir>/meta.json. Then REPORT and
   EXIT — do not wait.

5. Leave all edits unstaged; never git commit/push/checkout. Write result.json and EXIT.

When finished, write a single JSON object to <result_path> with: "stage": "acctest",
"phase": "launch", "test_phase" ("smoke"/"full"), "status" ("launched"/"failed"),
"summary", "pid_file", "run_json", "run_filter", "baseline_mode" ("normal"/"degraded"),
"test_regex". "test_phase" is MANDATORY — it is how the collect turn knows which gate ran; set it
to the phase actually launched (which is "smoke" on the first launch regardless of input).
Set status="launched" only once the detached run is started and run.pid is written.
