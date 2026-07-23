# upgrader

A minimal CLI that upgrades one Azure Resource Provider's
**SDK API version** in `terraform-provider-azurerm`, then runs its acceptance tests once and
**reports** what broke:

```
upgrade  →  acctest investigation (advisory)
(rp-api-upgrade)  (rp-acctest-launch + rp-acctest-collect)
```

It runs all work **directly in the provided checkout** and **never** commits, pushes,
switches the branch, or opens a PR — every change is left unstaged in the working tree for
human review.

The two stages have deliberately different roles. The **upgrade** stage does the fix work end
to end against fast, objective feedback (`go build`). The **acctest** stage is
**diagnose-only**: it runs the full suite once, diffs NEW failures against the TeamCity `main`
baseline, and writes a categorized report (in particular, suspected **API breaking changes**)
for a human to act on. It never edits code, never re-runs tests, and is **advisory** — a slow
or flaky acceptance run never fails a run whose upgrade converged.

## How it works

- Each stage runs as a series of fresh **GitHub Copilot SDK sessions** (one per turn) via
  the [Copilot SDK for Python](https://github.com/github/copilot-sdk). Disk is the only
  shared state between turns and between stages.
- Every session's working directory is the `--repo` checkout; all agent edits land there,
  unstaged, for human review.
- The bundled agents read skills from `.github/skills/<name>`, registered with the SDK by
  absolute path.
- A **no-VCS guard** (`_no_vcs_hooks` in [session.py](upgrader/session.py)) denies any tool
  call containing a forbidden `git` subcommand (`git commit`, `git push`, `git checkout`, …)
  so all work stays unstaged.
- Every turn streams its events to a per-turn log under the checkout's
  `.upgrader/<rp>/<version>/`, and each agent writes a small **`result.json`** sidecar so the
  loops advance on a deterministic pass/fail signal rather than by parsing prose.

### The two stages

| Stage | Flow | "Done" means |
| ----- | ---- | ------------ |
| **upgrade** ([upgrade.py](upgrader/upgrade.py)) | single session that edits the RP until `result.json` reports a green `go build ./...` | build is green |
| **acctest** ([acctest.py](upgrader/acctest.py)) | single pass: *launch full suite → watch detached PID → investigate & report* (no loop, no code edits) | the run finished and every NEW-vs-baseline failure is classified in a report |

Only the **upgrade** decides success. The acctest stage is **advisory**: it writes its report
as an artifact and never changes the exit code.

## Install

The supported way to run is the bundled sandbox image — it ships Python, Go, `make`, `git`,
the `gh` CLI, and Terraform, so the only host requirement is Docker:

```pwsh
docker build -t upgrader-sandbox .
```

<details>
<summary>Run on the host instead of Docker</summary>

```pwsh
python -m pip install -e .
# or run without installing:
$env:PYTHONPATH="."; python -m upgrader --help
```

Requires Python ≥ 3.9 and, for real runs, these tools on `PATH`: `copilot`, `go`, `make`,
`gh`, `git`.

</details>

## The ai-assisted-development toolkit (opt-in)

The upgrade agent can enrich its context with the community
[`terraform-azurerm-ai-assisted-development`](https://github.com/WodansSon/terraform-azurerm-ai-assisted-development)
toolkit — **without installing it into your checkout**. It's **off by default**; pass
`--with-toolkit` to enable it. The toolkit rides along as a pinned git submodule at
`third_party/aii` (currently `v3.7.0`), and when enabled [toolkit.py](upgrader/toolkit.py) loads
an **explicit allow-list** into the upgrade session only: six migration/implementation instruction
files (embedded into the prompt) and the `acceptance-testing` skill (via `skill_directories`).
Nothing is written to your `--repo`, so `git status` stays clean and none of the toolkit's
review/docs fleet pollutes the session. Without `--with-toolkit` (or when the submodule isn't
initialised) the upgrade proceeds on the agent's own self-contained `rp-api-upgrade` skill.

Clone with submodules (or run `git submodule update --init` afterwards):

```pwsh
git clone --recurse-submodules <this-repo>
# bump the toolkit later: git -C third_party/aii checkout vX.Y.Z && git add third_party/aii && git commit
```

## Usage

Mount your `terraform-provider-azurerm` checkout at `/workspace/azurerm` and pass it as
`--repo`. Put `GH_TOKEN` (and the acctest vars) in a `.env` file consumed by `--env-file`.

```pwsh
# Upgrade only (stop once the build is green):
docker run --rm --env-file .env `
  -v "C:\Repos\terraform-provider-azurerm:/workspace/azurerm" `
  upgrader-sandbox `
  keyvault 2023-07-01 --repo /workspace/azurerm --skip-acctest

# Full run: upgrade, then run the acctest suite once and report:
docker run --rm --env-file .env `
  -v "C:\Repos\terraform-provider-azurerm:/workspace/azurerm" `
  upgrader-sandbox `
  keyvault 2023-07-01 --repo /workspace/azurerm `
  --old-api-version 2023-02-01 --test-regex 'TestAccKeyVault_'
```

Or via Docker Compose (set `AZURERM_REPO` to your checkout):

```pwsh
$env:AZURERM_REPO="C:\Repos\terraform-provider-azurerm"
docker compose run --rm upgrader keyvault 2023-07-01 --repo /workspace/azurerm --skip-acctest
```

### Flags

| Flag | Purpose |
| ---- | ------- |
| `--repo` (required) | Path to the `terraform-provider-azurerm` working tree. |
| `--old-api-version` | Current version to upgrade from (default: detect). |
| `--skip-acctest` | Stop after the build is green; skip acceptance-test investigation. |
| `--test-regex` | Acctest `-run` filter for the full suite (default: `TestAcc`). |
| `--model` | Copilot model (e.g. `claude-sonnet-4.5`). |
| `--with-toolkit` | Inject the allow-listed ai-assisted-development toolkit content (migration instructions + `acceptance-testing` skill). Off by default. |
| `-v`, `--verbose` | Write per-turn event logs to disk (otherwise only `result.json` is kept). |

## Required environment

- **Copilot auth:** `GH_TOKEN` or `GITHUB_TOKEN`
- **Azure (acctest):** `ARM_CLIENT_ID`, `ARM_CLIENT_SECRET`, `ARM_SUBSCRIPTION_ID`,
  `ARM_TENANT_ID`, `ARM_TEST_LOCATION`, `ARM_TEST_LOCATION_ALT`, `ARM_TEST_LOCATION_ALT2`
- **TeamCity baseline (acctest):** `TEAMCITY_TOKEN` (or `TEAMCITY_ACCESSTOKEN`),
  optional `TEAMCITY_SERVER_URL`

## Module map

| Module | Responsibility |
| ------ | -------------- |
| [cli.py](upgrader/cli.py) | argparse surface; single `run` entry. |
| [loop.py](upgrader/loop.py) | Chains the upgrade and acctest stages; owns a Copilot client per stage. |
| [session.py](upgrader/session.py) | Fresh Copilot session per turn, no-VCS guard, per-turn event log. |
| [toolkit.py](upgrader/toolkit.py) | Loads an explicit allow-list from the `third_party/aii` toolkit submodule (6 instruction files embedded in the prompt + the `acceptance-testing` skill via `skill_directories`); never writes to the checkout. |
| [upgrade.py](upgrader/upgrade.py) | Upgrade stage: one session that edits the RP until `go build ./...` is green. |
| [acctest.py](upgrader/acctest.py) | Acctest investigation: launch full suite → watch detached PID → investigate & report (advisory, no code edits). |

Prompt bodies live under [upgrader/prompts/](upgrader/prompts) as `PROMPT.md`,
`PROMPT_ACCTEST_LAUNCH.md`, and `PROMPT_ACCTEST_COLLECT.md`. See
[OVERVIEW.md](upgrader/OVERVIEW.md) for the high-level
picture and roadmap (breaking-change detection, Pandora PR awareness).

## Constraints (by design)

- No `git commit`/`push`/branch switching/PR — enforced by the no-VCS deny-list in
  [session.py](upgrader/session.py). The orchestrator performs no VCS actions of its own.
- All edits are left unstaged in the `--repo` checkout so a human reviews
  `git -C <repo> diff` before anything is committed.
- Volume of output is not the goal — human review is the bottleneck.
