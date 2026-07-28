Inputs:
  service_name: <rp_name>
  target_api_version: <target_api_version>
  old_api_version: <old_api_version>
  test_regex: <test_regex>
  acctest_dir: <acctest_dir>
  analysis_path: <analysis_path>
  result_path: <result_path>

The run is already reduced for you: read the pre-computed artifacts (analysis.json at
<analysis_path>; logs/<Test>.log and new_failures.txt under <acctest_dir>) and go straight to
investigation, exactly as your Test Agent role describes.

Deterministic Azure REST API specs old-vs-target diff (prepared before this turn). Use this first
to validate target-version contract drift; fetch extra swagger files only if needed:

<azure_rest_api_specs_diff>

Investigate & classify each NEW failure per your agent role (investigation only; STOP on the
first breaking change), then write result.json to <result_path> per its contract.
