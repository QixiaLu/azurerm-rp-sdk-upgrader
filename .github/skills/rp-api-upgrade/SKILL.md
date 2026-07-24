---
name: rp-api-upgrade
description: Upgrade one Azure Resource Provider SDK API version in terraform-provider-azurerm and iterate until go build succeed. Use when asked to upgrade an RP to a specific API version.
---

# RP API Upgrade Skill

## When to use

Use this skill when you need to:

- upgrade an RP SDK package from one API version to another,
- repair compile-time breakages caused by model/API changes,
- identify and mitigate user-facing breaking changes caused by API upgrades,
- validate the upgrade via `go build ./...`.

## Provider standards — defer to the toolkit

This skill owns the *upgrade workflow*, not the provider's coding standards. The
ai-assisted-development toolkit is the authoritative owner of provider-development standards — Go/Azure
implementation patterns, error-message formatting, schema/validation placement, PATCH/residual-state
handling, transitional deprecation and `features`-flag gating, and acceptance-test shape. When its
guidance is available to this session (allow-listed `*.instructions.md` embedded in the run, and/or
toolkit skills such as `acceptance-testing` offered alongside this one), treat that guidance as
authoritative and defer to it for *how* edits are written.

## Required Inputs

- `rp_name`
- `target_api_version`

## Optional Inputs

- `old_api_version`
- `scope_paths` (resource/data source files, clients, helpers)

## Procedure

1. Discover all versioned SDK imports and usage in `internal/services/<rp_name>/` (note this repo mixes versions per SDK sub-package).
2. Verify the target API version is shipped by the **currently pinned** `go-azure-sdk` (check the module, NOT `vendor/`, which only holds already-imported versions):
   - `go list -mod=mod github.com/hashicorp/go-azure-sdk/resource-manager/<rp_name>/<target_api_version>/<subpackage>` (or inspect `"$(go env GOMODCACHE)/github.com/hashicorp/go-azure-sdk/resource-manager@<pinned-version>/<rp_name>/<target_api_version>"`).
   - **If present in the pinned module:** no `go.mod` bump needed; continue to step 3.
   - **If NOT in the pinned module:** check upstream `gh api repos/hashicorp/go-azure-sdk/contents/resource-manager/<rp_name>/<target_api_version> --silent`.
     - exists upstream → `go get github.com/hashicorp/go-azure-sdk@<minimum-version>` (the minimum release that ships it; vendor sync happens in step 4).
     - not upstream → stop and report that the target API version has not yet been published in `hashicorp/go-azure-sdk`.
3. Update imports to the target API version, per sub-package — the client (`client/client.go`) and resource/data-source files. Preserve intentional overrides under `azuresdkhacks/`.
4. Sync the vendor directory: `go mod tidy` then `go mod vendor` so the now-imported target sub-package is pulled into `vendor/` (and any step-2 bump is applied). Long-running; see command hygiene. Confirm with a single path check.
5. Update impacted symbols.
6. Run a breaking-change assessment. **When the toolkit's breaking-change guidance is available to
   this session, follow it as authoritative for classifying and mitigating changes** — the
   `migration-guide` instructions ("Breaking Change Patterns" / "Breaking Change Guardrails") and the
   `api-evolution-patterns` instructions own that policy (what counts as breaking, major-release
   `features` gating to avoid silent minor-release breakage, upgrade-guide/doc requirements). The
   bullets below are the swagger-evidence step this skill owns plus a self-sufficient fallback for
   when the toolkit is not loaded; where they overlap the toolkit's guidance, the toolkit wins.
   - confirm suspected changes against the **target API version** — diff the **swagger** in
     `Azure/azure-rest-api-specs` for the affected property (`type`/`required`/`default`/`enum`/
     `x-ms-*`/response shape) so changed defaults/enums/required-fields are caught even when they
     compile cleanly,
   - as a complementary semantics check, when the Microsoft Learn MCP tools
     (`microsoft_docs_search` / `microsoft_docs_fetch`) are available to the session, search the
     target RP and `target_api_version` for documented breaking changes the code diff alone will not
     reveal — changed or newly added default values, deprecations/soft-removals, removed or renamed
     properties, and behavior notes. Use this to *surface intent* (e.g. "is this new default
     intentional?"), then confirm every lead against the swagger before classifying — MS Learn is a
     prose/reference aid, so the `azure-rest-api-specs` swagger remains the authoritative evidence,
   - classify changes: resource/data source removal, schema rename/type/default/validation
     changes, behavior/default-value changes,
   - mitigate as instructed in the repo's `contributing/topics/guide-breaking-changes.md`; for
     unavoidable changes apply transitional patterns (deprecation messaging, `features.FivePointOh()`
     gating, conditional registration, staged tests),
   - add required docs follow-up (upgrade guide and resource/data source docs) when behavior is
     user-visible.
7. Compile and iterate:
   - `go build ./internal/services/<rp_name>/...`
   - fix errors until the RP package compiles.
8. Validate full provider build:
   - `go build ./...`

## Heuristics

- Act as a senior provider engineer: aim for **zero behavioral regressions**, not just a green build. Azure APIs often ship **undocumented breaking changes** (changed defaults, nil/pointer semantics, renamed/repurposed enums, altered response shapes) that compile but silently change behavior.
- Diff old vs new SDK models/enums/constants; treat any change to defaults, required/optional, `Computed`, nil-handling, or enum values as a suspected breaking change until proven safe.
- Confirm whether a model/enum/default change is intended (not just an SDK-generation artifact) by checking the target API version directly: the `azure-rest-api-specs` swagger for the property's `type`/`required`/`default`/`enum`/`x-ms-*`/response shape.
- Trace data flow (expand/flatten + schema), not just the compile error, when a field changes.
- Prefer adapting local mapping/expand/flatten helpers over broad refactors.
- Keep edits minimal and localized to target RP unless transitive compile errors require wider changes.

## Command Hygiene (large/long output)

- You may bump the `go-azure-sdk` version in `go.mod` only when the target API is not in the pinned module but exists upstream (see step 2). Use `go get` with the minimum version, not latest. If it is already in the pinned module, do not change `go.mod` — just update imports and re-vendor.
- `go mod tidy`/`go mod vendor` are slow and rewrite many files: run with a generous timeout and pipe output through `tail`.
- Use `gh api repos/hashicorp/go-azure-sdk/contents/...` to check upstream package availability — not a directory dump.
- Treat `vendor/` and `go.sum` as generated: never `git diff`/`git status`/`cat` them. Verify success via build/vet exit codes.
- Check the target vendored package exists with a single path check, not a directory dump.
- Read the output of long commands once; do not repeatedly poll.

## Definition of Done

- All RP references use `target_api_version` where intended.
- No unresolved compile errors from the upgrade.
- Breaking changes are either mitigated according to `contributing/topics/guide-breaking-changes.md` or captured as explicit follow-up actions.
- `go build ./...` return exit code `0`.
- If `go-azure-sdk` was bumped, the new version is the minimum that ships the target API (noted in deliverables); any `vendor/` changes are from that bump only.

## Deliverables

- concise change summary,
- API-version delta: the notable differences the target API version introduced vs the old one (newly added fields, removed/renamed properties, changed defaults/enums, behavior changes), from the old-vs-target SDK model diff and the breaking-change evidence already gathered,
- modified file list (summarize `vendor/` sync as a count; note whether `go-azure-sdk` was bumped and to which version),
- breaking-change assessment and mitigation/deprecation plan,
- final build/vet commands + outcome,
- blockers (if any) with exact next steps.
