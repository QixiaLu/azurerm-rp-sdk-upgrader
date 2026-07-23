You are ONE iteration of an autonomous upgrade loop with a FRESH context. State lives on
disk, not in memory: each iteration restarts blank and trusts only IMPLEMENTATION_PLAN.md
plus the code on disk. Do one bounded chunk, validate, record, exit.

Inputs:
  rp_name: <rp_name>
  target_api_version: <target_api_version>
  old_api_version: <old_api_version>

0a. Study IMPLEMENTATION_PLAN.md to learn the state so far (done/blocked/next).
0b. Study internal/services/<rp_name>/ to see how the SDK is used today.
0c. Additional API-migration and provider-development guidance from the ai-assisted-development
   toolkit may be embedded at the END of this prompt (only when the run opts in via
   `--with-toolkit`). When present, treat it as authoritative, detailed rules for this bump; if it
   says "not requested" / "not available", the steps below are self-sufficient.

1. Pick the single most important unchecked task. Search the code to confirm it is really
   undone — never assume. Prefer fixing existing usage over rewrites.
2. Make surgical edits confined to the RP; widen only when transitive compile errors force
   it. Preserve schema/behavior unless the API genuinely changed. Run go mod tidy && go mod
   vendor if the SDK version changed.
3. Validate: focused `go build ./internal/services/<rp_name>/...` while iterating, then full
   `go build ./...` to confirm.
4. Update IMPLEMENTATION_PLAN.md NOW: tick finished tasks, append findings/blockers.
5. Once the upgrade is otherwise complete (build green, no tasks left), format the code from
   the repo root: `make fmt`, then ALWAYS run `make document-fix` — it refreshes the generated
   docs (including the API version referenced there), so run it even when you touched no docs
   by hand; never mark it "N/A". Skip formatting only while still mid-task. Never hand-format
   vendor/.
6. Never read/diff vendor/ or go.sum. Leave all edits unstaged; do not git commit/push/
   checkout. Write result.json and EXIT.

When finished, write a single JSON object to <result_path> with: "status"
("done"/"blocked"/"failed"/"in-progress"), "build_passed" (bool), "summary", "blockers",
"api_changes" (list of the notable differences the target API version introduced vs the old one
— e.g. newly added fields, removed/renamed properties, changed defaults or enums — each as
{"kind": "added"/"removed"/"renamed"/"default-changed"/"enum-changed"/"behavior", "symbol",
"detail"}; empty list if none). Base it on the old vs target SDK model diff plus the
Microsoft Learn / azure-rest-api-specs evidence you already gathered — do not re-investigate.
Set status="done" + build_passed=true only when full `go build ./...` is green.

---
# ai-assisted-development toolkit guidance (allow-listed instructions)
<toolkit_guidance>
