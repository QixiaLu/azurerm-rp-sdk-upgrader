# upgrader

A minimal CLI that upgrades one Azure Resource Provider's **SDK API version** in
`terraform-provider-azurerm`, then runs its acceptance tests once and reports what broke:

```
upgrade  →  acctest investigation (advisory)
(Upgrade agent)   (orchestrator launches; Test agent investigates)
```

All work happens **directly in the provided checkout**; nothing is committed, pushed, or
branched — every change is left unstaged for human review. Only the **upgrade** decides success;
the acctest stage is advisory and never changes the exit code.

## How it works

- Each stage runs as fresh **GitHub Copilot SDK sessions** (one per turn) via the
  [Copilot SDK for Python](https://github.com/github/copilot-sdk); disk is the only shared
  state between turns and stages.
- Each role is a single **agent** under [upgrader/agents/](upgrader/agents) —
  `upgrade.md`, `test-launch.md`, and `test.md` — loaded verbatim as the agent's system prompt;
  per-turn task prompts under `prompts/` carry only the dynamic inputs. All mechanical work
  (version detection, the TeamCity baseline, the `make acctests` quoting/spawn, the finished-run
  reduction, `go build`) is deterministic Python in [helpers.py](upgrader/helpers.py); the launch
  agent shells out to it via `python -m upgrader.helpers`.
- Each agent writes a small **`result.json`** sidecar so stages advance on a deterministic
  pass/fail signal rather than by parsing prose; the agent's transcript streams to stdout
  (no per-agent log files — redirect stdout if you want to keep it).

### The two stages

| Stage | Flow | "Done" means |
| ----- | ---- | ------------ |
| **upgrade** ([upgrade.py](upgrader/upgrade.py)) | bounded round loop: each round is a fresh session that fixes a chunk, then the **orchestrator** runs `go build ./...` as the authoritative convergence gate and feeds the remaining compile errors into the next round (up to `--max-rounds`) | the orchestrator's `go build ./...` is green |
| **acctest** ([acctest.py](upgrader/acctest.py)) | a **launch agent** resolves the RP's provider service dir + regex and starts the suite (via the helpers CLI); the orchestrator waits out the detached run and reduces it; then a **Test agent** investigates & reports the NEW-vs-baseline failures (no code edits) | the run finished and every NEW-vs-baseline failure is classified in a report |

## Prerequisites

- **Docker** — the only hard requirement for the supported (sandboxed) path; the image bundles
  Python, Go, `make`, `git`, the `gh` CLI, and Terraform.
- **Git** — to clone this repo and to mount/review your
  `terraform-provider-azurerm` checkout.
- **A GitHub token** (`GH_TOKEN`) with Copilot access, passed via `--env-file`.
- **Azure + TeamCity credentials** — only for acctest runs (skip with `--skip-acctest`); see
  [Required environment](#required-environment).

## Local Usage

End-to-end: clone this repo, then run it against your checkout with Docker Compose (which
builds the image, mounts your checkout, loads `.env`, and caches Go modules for you).

**1. Clone**:

```pwsh
git clone <this-repo>
cd ai-api-upgrade
```

**2. Configure** — put `GH_TOKEN` (and, for acctest runs, the Azure/TeamCity vars) in a `.env`
file, and point `AZURERM_REPO` at your provider checkout:

```pwsh
$env:AZURERM_REPO = "C:\Repos\terraform-provider-azurerm"
```

**3. Run it** — everything after `upgrader` is passed straight to the CLI. Compose builds the
image on first use (`--build` forces a rebuild):

```pwsh
# Upgrade only (stop once the build is green):
docker compose run --rm upgrader `
  keyvault 2023-07-01 --repo /workspace/azurerm --skip-acctest

# Full run: upgrade, then run the acctest suite once and report:
docker compose run --rm upgrader `
  keyvault 2023-07-01 --repo /workspace/azurerm `
  --old-api-version 2023-02-01 --test-regex 'TestAccKeyVault_'
```

### Flags

| Flag | Purpose |
| ---- | ------- |
| `--repo` (required) | Path to the `terraform-provider-azurerm` working tree. |
| `--old-api-version` | Current version to upgrade from (default: detect). |
| `--skip-acctest` | Stop after the build is green; skip acceptance-test investigation. |
| `--test-regex` | Acctest `-run` filter for the full suite (default: `TestAcc`). |
| `--max-rounds` | Max upgrade rounds; each is a fresh session gated by a real `go build ./...` (default: 8). |
| `--model` | Copilot model (e.g. `claude-sonnet-4.5`). |

## Required environment

- **Copilot auth:** `GH_TOKEN`
- **Azure (acctest):** `ARM_CLIENT_ID`, `ARM_CLIENT_SECRET`, `ARM_SUBSCRIPTION_ID`,
  `ARM_TENANT_ID`, `ARM_TEST_LOCATION`, `ARM_TEST_LOCATION_ALT`, `ARM_TEST_LOCATION_ALT2`
- **TeamCity baseline (acctest):** `TEAMCITY_TOKEN` (or `TEAMCITY_ACCESSTOKEN`),
  optional `TEAMCITY_SERVER_URL`

## Module map

| Module | Responsibility |
| ------ | -------------- |
| [cli.py](upgrader/cli.py) | argparse surface; single `run` entry. |
| [loop.py](upgrader/loop.py) | Chains the upgrade and acctest stages; owns a Copilot client per stage. |
| [session.py](upgrader/session.py) | Fresh Copilot session per turn, no-VCS guard, transcript streamed to stdout. |
| [helpers.py](upgrader/helpers.py) | Deterministic toolbelt (stdlib-only): current-version detection, `go build` mechanics, finished-run reduction (`analyze_run`), TeamCity baseline capture (`capture_teamcity_baseline`), and the detached suite launch (`launch_acctests`, with the correct `make`/`sh` quoting). Also exposes these as a CLI (`python -m upgrader.helpers check-env`/`baseline`/`launch`) the launch agent shells out to. |
| [agents/](upgrader/agents) | One agent per role — `upgrade.md`, `test-launch.md`, `test.md` — loaded as the agent system prompt via `load_agent()`. Per-turn task prompts live under [upgrader/prompts/](upgrader/prompts). |
| [upgrade.py](upgrader/upgrade.py) | Upgrade stage: one session that edits the RP until `go build ./...` is green. |
| [acctest.py](upgrader/acctest.py) | Acctest investigation: a launch agent resolves the RP's provider service dir + regex and starts the suite (via the helpers CLI), the orchestrator watches the detached PID + reduces the run, then a Test agent investigates & reports (advisory, no code edits). |

## Constraints (by design)

- No `git commit`/`push`/branch switching/PR — enforced by the no-VCS deny-list in
  [session.py](upgrader/session.py). The orchestrator performs no VCS actions of its own.
- All edits are left unstaged in the `--repo` checkout so a human reviews
  `git -C <repo> diff` before anything is committed.
- Volume of output is not the goal — human review is the bottleneck.
