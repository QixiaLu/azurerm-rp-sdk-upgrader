# upgrader

A minimal CLI that runs a **"Ralph loop"** to upgrade one Azure Resource Provider's
**SDK API version** in `terraform-provider-azurerm`, then triages its acceptance tests:

```
upgrade  →  acctest triage
(rp-api-upgrade)  (rp-acctest-launch + rp-acctest-collect)
```

It runs all work **directly in the provided checkout** and **never** commits, pushes,
switches the branch, or opens a PR — every change is left unstaged in the working tree for
human review.

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

| Stage | Loop | "Done" means |
| ----- | ---- | ------------ |
| **upgrade** ([upgrade.py](upgrader/upgrade.py)) | fresh session per iteration until `result.json` reports a green `go build ./...` | build is green |
| **acctest** ([acctest.py](upgrader/acctest.py)) | rounds of *launch → watch detached PID → collect/fix*, re-running the full suite after each fix | a round finishes triage with no new fixes |

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

## Usage

Mount your `terraform-provider-azurerm` checkout at `/workspace/azurerm` and pass it as
`--repo`. Put `GH_TOKEN` (and the acctest vars) in a `.env` file consumed by `--env-file`.

```pwsh
# Upgrade only (stop once the build is green):
docker run --rm --env-file .env `
  -v "C:\Repos\terraform-provider-azurerm:/workspace/azurerm" `
  upgrader-sandbox `
  keyvault 2023-07-01 --repo /workspace/azurerm --skip-acctest

# Full run: upgrade, then acctest triage:
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
| `--max-iterations` | Upgrade-loop cap (default: 15). |
| `--skip-acctest` | Stop after the build is green. |
| `--test-regex` | Acctest `-run` filter (default: `TestAcc`). |
| `--acctest-rounds` | Max trigger→fix→rerun rounds (default: 3). |
| `--model` | Copilot model (e.g. `claude-sonnet-4.5`). |

## Required environment

- **Copilot auth:** `GH_TOKEN` or `GITHUB_TOKEN`
- **Azure (acctest):** `ARM_CLIENT_ID`, `ARM_CLIENT_SECRET`, `ARM_SUBSCRIPTION_ID`,
  `ARM_TENANT_ID`, `ARM_TEST_LOCATION`, `ARM_TEST_LOCATION_ALT`, `ARM_TEST_LOCATION_ALT2`
- **TeamCity baseline (acctest):** `TEAMCITY_TOKEN` (or `TEAMCITY_ACCESSTOKEN`),
  optional `TEAMCITY_SERVER_URL`

> Acceptance tests provision **real Azure resources** that cost money and can run for hours.

## Module map

| Module | Responsibility |
| ------ | -------------- |
| [cli.py](upgrader/cli.py) | argparse surface; single `run` entry. |
| [loop.py](upgrader/loop.py) | Chains the upgrade and acctest stages; owns a Copilot client per stage. |
| [session.py](upgrader/session.py) | Fresh Copilot session per turn, no-VCS guard, per-turn event log. |
| [upgrade.py](upgrader/upgrade.py) | Upgrade Ralph loop until `go build ./...` is green. |
| [acctest.py](upgrader/acctest.py) | Acctest triage: launch → watch detached PID → collect/fix rounds. |

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
