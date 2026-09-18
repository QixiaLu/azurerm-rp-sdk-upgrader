"""Prebuild stage: generate the Pandora API Definitions for the target API version, build the
Go SDK from them, and point the AzureRM checkout at that local SDK.

Runs before the upgrade stage (see :mod:`upgrader.loop`) so the agent can compile against an
SDK that does not exist upstream yet. Every step is mechanical: progress is written to
``prebuild.json`` next to the run's other sidecars and all command output to ``prebuild.log``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO

from upgrader import helpers

_RESULT_FILE = "prebuild.json"
_LOG_FILE = "prebuild.log"
_DATA_API_PORT = 8080
_DATA_API_ENDPOINT = f"http://127.0.0.1:{_DATA_API_PORT}"
# large services such as Network take a long time to load from disk
_DATA_API_TIMEOUT_SECONDS = 2 * 60 * 60.0
_SPECS_BRANCH = "main"


class PrebuildError(RuntimeError):
    """A prebuild step failed."""


def _service_pattern(service_name: str) -> re.Pattern[str]:
    # service blocks in the config never nest braces, so `[^}]` can't run past the end of one
    return re.compile(
        r'service\s+"(?P<directory>[^"]+)"\s*\{[^}]*?name\s*=\s*"(?P<name>'
        + re.escape(service_name)
        + r')"[^}]*?available\s*=\s*\[(?P<available>[^]]*)\]',
        re.IGNORECASE,
    )


def _match_service(text: str, service_name: str) -> re.Match[str]:
    matches = list(_service_pattern(service_name).finditer(text))
    if not matches:
        raise PrebuildError(f'the service "{service_name}" was not found in the Pandora config')
    if len(matches) > 1:
        raise PrebuildError(
            f'the service "{service_name}" is ambiguous in the Pandora config '
            f"({len(matches)} matching blocks)")
    return matches[0]


def read_service(config_path: Path, service_name: str) -> tuple[str, str]:
    """Return the Swagger directory and the canonical name for a service."""
    match = _match_service(config_path.read_text(encoding="utf-8"), service_name)
    return match["directory"], match["name"]


def add_service_version(config_path: Path, service_name: str, api_version: str) -> bool:
    """Add ``api_version`` to a service's available list; return whether the file changed."""
    text = config_path.read_text(encoding="utf-8")
    match = _match_service(text, service_name)
    versions = re.findall(r'"([^"]+)"', match["available"])
    if api_version in versions:
        return False

    start, end = match.span("available")
    updated = ", ".join(f'"{version}"' for version in [*versions, api_version])
    config_path.write_text(text[:start] + updated + text[end:], encoding="utf-8")
    return True


def _spec_exists(specs: Path, directory: str, api_version: str) -> bool:
    service_specs = specs / "specification" / directory / "resource-manager"
    return service_specs.is_dir() and any(path.is_dir() for path in service_specs.rglob(api_version))


def find_generated_version(output_dir: Path, service_name: str, api_version: str) -> Path:
    """Find the generated directory for one service's ``api_version``.

    Scoped to ``service_name`` because an established go-azure-sdk checkout carries many
    unrelated services released on the same date.
    """
    wanted = service_name.lower()
    candidates = [path for path in (output_dir / "resource-manager").glob(f"*/{api_version}")
                  if path.is_dir() and path.parent.name.lower() == wanted]
    if len(candidates) != 1:
        rendered = ", ".join(str(path) for path in candidates) or "none"
        raise PrebuildError(
            f"expected one generated resource-manager directory for {service_name} "
            f"{api_version}; found {rendered}")
    return candidates[0]


def _format_command(argv: list[str]) -> str:
    return subprocess.list2cmdline(argv)


def _run(argv: list[str], *, cwd: Path, log: TextIO,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    log.write(f"$ (cd {cwd}) {_format_command(argv)}\n")
    log.flush()
    completed = subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.stdout:
        log.write(completed.stdout)
        if not completed.stdout.endswith("\n"):
            log.write("\n")
        log.flush()
    if completed.returncode:
        raise PrebuildError(
            f"command failed with exit code {completed.returncode}: {_format_command(argv)}")
    return completed


def _require_layout(pandora_repo: Path, repo: Path, sdk_repo: Path) -> None:
    required = [
        pandora_repo / "config" / "resource-manager.hcl",
        pandora_repo / "submodules" / "rest-api-specs",
        pandora_repo / "tools" / "importer-rest-api-specs",
        pandora_repo / "tools" / "data-api",
        pandora_repo / "tools" / "generator-go-sdk",
        repo / "go.mod",
        sdk_repo / "resource-manager" / "go.mod",
    ]
    # the Go SDK generator shells out to goimports and only logs when it's absent
    tools = ["git", "go", "goimports"]

    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise PrebuildError("required prebuild paths are missing: " + ", ".join(missing))
    missing_tools = [name for name in tools if shutil.which(name) is None]
    if missing_tools:
        raise PrebuildError("required executables are missing: " + ", ".join(missing_tools))


def _build_tool(directory: Path, name: str, log: TextIO) -> str:
    """Build a Pandora Go tool and return the path to its binary."""
    _run(["go", "build", "."], cwd=directory, log=log)
    binary = directory / (f"{name}.exe" if os.name == "nt" else name)
    if not binary.is_file():
        raise PrebuildError(f"go build did not produce {binary}")
    return str(binary)


def _wait_until_healthy(process: subprocess.Popen[bytes]) -> None:
    started = time.monotonic()
    deadline = started + _DATA_API_TIMEOUT_SECONDS
    next_update = started + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise PrebuildError(f"the Data API exited during startup (exit code {process.returncode})")
        with contextlib.suppress(OSError, urllib.error.URLError):
            urllib.request.urlopen(f"{_DATA_API_ENDPOINT}/v1/health", timeout=1).close()
            return
        if time.monotonic() >= next_update:
            print(f"[DEBUG] still waiting for the Data API ({int(time.monotonic() - started)}s)")
            next_update += 60
        time.sleep(0.5)
    raise PrebuildError(f"the Data API was not healthy at {_DATA_API_ENDPOINT} in time")


@contextlib.contextmanager
def serve_data_api(pandora_repo: Path, service_name: str, log: TextIO) -> Iterator[str]:
    """Run the Data API for the duration of the block, yielding its endpoint."""
    directory = pandora_repo / "tools" / "data-api"
    binary = _build_tool(directory, "data-api", log)
    process = subprocess.Popen(
        [binary, "serve", f"-services={service_name}", f"-port={_DATA_API_PORT}"],
        cwd=directory,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_until_healthy(process)
        yield _DATA_API_ENDPOINT
    finally:
        process.terminate()
        process.wait(timeout=30)


def update_rest_api_specs(pandora_repo: Path, directory: str, api_version: str,
                          log: TextIO) -> None:
    specs = pandora_repo / "submodules" / "rest-api-specs"
    _run(["git", "submodule", "update", "--init", "submodules/rest-api-specs"], cwd=pandora_repo,
         log=log)
    if not _spec_exists(specs, directory, api_version):
        _run(["git", "-C", str(specs), "fetch", "origin", _SPECS_BRANCH], cwd=pandora_repo, log=log)
        _run(["git", "-C", str(specs), "checkout", "--force", "FETCH_HEAD"], cwd=pandora_repo,
             log=log)
    if not _spec_exists(specs, directory, api_version):
        raise PrebuildError(
            f"API version {api_version} is absent from the rest-api-specs service {directory}")


def import_api_definitions(pandora_repo: Path, service_name: str, log: TextIO) -> Path:
    directory = pandora_repo / "tools" / "importer-rest-api-specs"
    importer = _build_tool(directory, "importer-rest-api-specs", log)
    _run([importer, "import", f"-services={service_name}"], cwd=directory, log=log)
    definitions = pandora_repo / "api-definitions" / "resource-manager" / service_name
    if not definitions.is_dir():
        raise PrebuildError(f"the importer did not generate {definitions}")
    return definitions


def generate_go_sdk(pandora_repo: Path, service_name: str, endpoint: str, output_dir: Path,
                    log: TextIO) -> None:
    directory = pandora_repo / "tools" / "generator-go-sdk"
    generator = _build_tool(directory, "generator-go-sdk", log)
    _run([generator, "resource-manager", "generate", f"-data-api={endpoint}",
          f"-output-dir={output_dir}", f"-services={service_name}"], cwd=directory, log=log)


def link_local_sdk(repo: Path, go_azure_sdk_repo: Path, service_name: str, api_version: str,
                   log: TextIO) -> Path:
    """Point the provider's ``go.mod`` at the freshly generated SDK; return that version's dir."""
    generated = find_generated_version(go_azure_sdk_repo, service_name, api_version)
    resource_manager = (go_azure_sdk_repo / "resource-manager").resolve()
    _run(["go", "mod", "edit",
          f"-replace=github.com/hashicorp/go-azure-sdk/resource-manager={resource_manager}"],
         cwd=repo, log=log)
    _verify_replace(repo, resource_manager, log)
    return generated


def _verify_replace(repo: Path, resource_manager: Path, log: TextIO) -> None:
    completed = _run(["go", "mod", "edit", "-json"], cwd=repo, log=log)
    try:
        module = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise PrebuildError("go mod edit -json returned invalid JSON") from exc
    expected = str(resource_manager)
    for replacement in module.get("Replace") or []:
        if (replacement.get("Old") or {}).get("Path") == \
                "github.com/hashicorp/go-azure-sdk/resource-manager":
            if (replacement.get("New") or {}).get("Path") == expected:
                return
    raise PrebuildError("AzureRM go.mod does not contain the expected local SDK replacement")


def run_prebuild(*, repo: Path, run_dir: Path, target: str, pandora_repo: Path,
                 go_azure_sdk_repo: Path, pandora_service: str) -> bool:
    """Import the API Definitions for ``target``, build the SDK, and link it into ``repo``.

    Returns whether the prebuild converged. Progress is mirrored into
    ``run_dir/prebuild.json`` so a resumed/failed run can be inspected, and every command's
    output is appended to ``run_dir/prebuild.log``.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    result_path = run_dir / _RESULT_FILE
    log_path = run_dir / _LOG_FILE
    state: dict[str, str] = {
        "status": "running",
        "step": "preflight",
        "error": "",
        "log": str(log_path),
    }
    helpers.write_result(result_path, state)

    def step(name: str) -> None:
        print(f"[DEBUG] prebuild: {name}")
        state["step"] = name
        helpers.write_result(result_path, state)

    try:
        with log_path.open("a", encoding="utf-8") as log:
            _require_layout(pandora_repo, repo, go_azure_sdk_repo)
            config_path = pandora_repo / "config" / "resource-manager.hcl"
            directory, service_name = read_service(config_path, pandora_service)

            step("update rest-api-specs")
            update_rest_api_specs(pandora_repo, directory, target, log)

            step("update Pandora config")
            add_service_version(config_path, service_name, target)

            step("import API definitions")
            definitions = import_api_definitions(pandora_repo, service_name, log)
            state["api_definitions"] = str(definitions)

            step("generate Go SDK")
            with serve_data_api(pandora_repo, service_name, log) as endpoint:
                generate_go_sdk(pandora_repo, service_name, endpoint, go_azure_sdk_repo, log)

            step("link the local SDK into AzureRM")
            generated = link_local_sdk(repo, go_azure_sdk_repo, service_name, target, log)

        state.update(status="success", step="complete", error="", sdk_dir=str(generated))
        helpers.write_result(result_path, state)
        print(f"[DEBUG] prebuild complete; {repo} now builds against {go_azure_sdk_repo}")
        return True
    except (OSError, PrebuildError, subprocess.SubprocessError) as exc:
        state.update(status="failed", error=str(exc))
        helpers.write_result(result_path, state)
        print(f"[DEBUG] prebuild failed during {state['step']}: {exc}")
        print(f"[DEBUG] see {result_path} and {log_path}")
        return False
