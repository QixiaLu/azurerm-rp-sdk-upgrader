Inputs:
  rp_name: <rp_name>
  target_api_version: <target_api_version>
  old_api_version: <old_api_version>
  result_path: <result_path>
  plan_path: <plan_path>

<round_context>

Remaining compile errors from the orchestrator's LAST `go build ./...` (empty on the first round
or when the build was already clean) — the ground truth of what is still broken; start from the
most fundamental error and work outward:

<build_errors>

Deterministic Azure REST API specs old-vs-target diff (prepared before this turn). Use this first
as your evidence map; only fetch extra swagger files when this diff is insufficient:

<azure_rest_api_specs_diff>

Do one bounded chunk per your Upgrade Agent role (study the plan at <plan_path> and
internal/services/<rp_name>/ first). Do NOT run `go build` yourself — the orchestrator runs the
authoritative build after your turn. Update the plan at <plan_path>, then write result.json to
<result_path> per your contract and EXIT.
