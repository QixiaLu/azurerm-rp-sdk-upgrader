Inputs:
  rp_name: <rp_name>
  test_regex: <test_regex>
  parallel: <parallel>
  repo: <repo>
  upgrader_root: <upgrader_root>
  acctest_dir: <acctest_dir>
  result_path: <result_path>

Follow your Test Launch Agent role: resolve the real provider service directory + `-run` regex
for `rp_name` (it may differ from the product name — e.g. a `*backup*` RP lives under
`recoveryservices`), then delegate every mechanical step (check-env, baseline, detached launch)
to `python -m upgrader.helpers`. Run every helper command from `upgrader_root`; changing to `repo`
first can make the `upgrader` module unavailable. Inspect the provider using its absolute `repo`
path. This turn only LAUNCHES the suite detached and EXITS — never wait for results, never edit
code, never git commit/push/checkout.

Write result.json to <result_path> per your agent's contract.
