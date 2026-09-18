# AzureRM API Upgrader

A GitHub Copilot-powered CLI for upgrading one Azure Resource Provider SDK API
version in `terraform-provider-azurerm`.

It edits the supplied checkout, resolves compile failures, assesses user-facing
API changes as breaking or non-breaking, and can investigate acceptance-test
failures. Acceptance tests are advisory; upgrade success is determined by
`go build ./...`.

All changes remain unstaged for review. The tool never commits, pushes, switches
branches, or opens pull requests.

## Requirements

- Python 3.9+, Git, and the Go toolchain required by the provider checkout.
- `GH_TOKEN` with GitHub Copilot access.
- For acceptance tests: `make`, Terraform, Azure
  credentials, and `TEAMCITY_TOKEN` (or `TEAMCITY_ACCESSTOKEN`).
- For local SDK generation: `goimports`, Pandora, and `go-azure-sdk` checkouts.

## Install

```bash
git clone <this-repo>
cd ai-api-upgrade
python3 -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Git Bash: source .venv/Scripts/activate
# PowerShell: .\.venv\Scripts\Activate.ps1
pip install -e .
```

Set `GH_TOKEN` in the environment or `.env`. Environment variables take
precedence; `--env-file` loads a different file.

## Usage

Upgrade without acceptance tests:

```bash
python -m upgrader keyvault 2023-07-01 \
  --repo ~/repos/terraform-provider-azurerm \
  --skip-acctest
```

Upgrade and investigate acceptance-test failures:

```bash
python -m upgrader keyvault 2023-07-01 \
  --repo ~/repos/terraform-provider-azurerm \
  --old-api-version 2023-02-01 \
  --test-regex 'TestAccKeyVault_'
```

The installed `upgrader` command is equivalent to `python -m upgrader`.

### Unpublished SDK versions

Generate the target SDK from local repositories when it is not published:

```bash
python -m upgrader network 2025-09-01 \
  --repo ~/repos/terraform-provider-azurerm \
  --prebuild-local-sdk \
  --pandora-repo ~/repos/pandora \
  --go-azure-sdk-repo ~/repos/go-azure-sdk \
  --pandora-service Network \
  --skip-acctest
```

`--pandora-service` is the service `name` in Pandora's
`config/resource-manager.hcl`; it may differ from the provider directory name.

## Options

| Option | Description |
| --- | --- |
| `--repo` | Provider checkout to edit (required). |
| `--old-api-version` | Existing API version; detected when omitted. |
| `--skip-acctest` | Skip acceptance-test investigation. |
| `--test-regex` | Acceptance-test `-run` filter (default: `TestAcc`). |
| `--parallel` | Maximum parallel acceptance tests (default: 11). |
| `--max-rounds` | Maximum upgrade rounds (default: 8). |
| `--model` | Copilot model to use. |
| `--env-file` | Alternative credentials file. |
| `--debug` | Print agent tool-usage records. |
| `--prebuild-local-sdk` | Generate and link a local SDK. |
| `--pandora-repo` | Pandora checkout used for generation. |
| `--go-azure-sdk-repo` | `go-azure-sdk` checkout used for generation. |
| `--pandora-service` | Pandora service name used for generation. |

Run `upgrader --help` for the authoritative CLI reference.

## Output

The provider checkout contains the upgraded Go files and any generated dependency
changes. Run artifacts are written to:

```text
<provider-repo>/.upgrader/<rp>/<target-version>/
```

The main files are:

- `IMPLEMENTATION_PLAN.md`: completed work and remaining follow-ups.
- `result.json`: build result, API changes, breaking-change assessment, and
  acceptance-test findings when tests were run.
- `prebuild.json` and `prebuild.log`: local SDK generation status and logs, only
  when `--prebuild-local-sdk` was used.

Review changes with:

```bash
git -C <provider-repo> diff
```

## Docker

```pwsh
$env:AZURERM_REPO = "C:\Repos\terraform-provider-azurerm"
docker compose run --rm upgrader keyvault 2023-07-01 --repo /workspace/azurerm --skip-acctest
```

Compose loads `.env`. Rebuild the image after source changes with
`docker compose build upgrader`.