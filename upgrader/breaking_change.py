"""Breaking-change detection stage for AzureRM acceptance tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

from upgrader import credentials, helpers

_PATCH_FILE = Path(__file__).resolve().parent / "breaking-change-detector.patch"
_PATCH_TARGETS = (
    "internal/acceptance/testcase.go",
    "internal/acceptance/breaking_change_test.go",
)
_TEST_TIMEOUT = "180m"
_BREAKING_CHANGE_LOG_MARKER = "ImportStateVerify attributes not equivalent"


class BreakingChangeError(RuntimeError):
    """A breaking-change preflight, patch, or cleanup step failed."""


def _git(repo: Path, *args: str,
         input_data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_data,
        capture_output=True,
        check=False,
    )


def _decode_output(output: bytes) -> str:
    return output.decode("utf-8", errors="replace").strip()


def _checked_git(repo: Path, *args: str, input_data: bytes | None = None) -> str:
    completed = _git(repo, *args, input_data=input_data)
    if completed.returncode:
        detail = _decode_output(completed.stderr or completed.stdout)
        raise BreakingChangeError(
            detail or f"git {' '.join(args)} failed with exit code {completed.returncode}")
    return _decode_output(completed.stdout)


def _repo_root(repo: Path) -> Path:
    root = _checked_git(repo, "rev-parse", "--show-toplevel")
    return Path(root)


def _require_clean_target(repo: Path) -> None:
    for target in _PATCH_TARGETS:
        for args in (("diff", "--quiet", "--", target),
                     ("diff", "--cached", "--quiet", "--", target)):
            completed = _git(repo, *args)
            if completed.returncode == 1:
                raise BreakingChangeError(
                    f"{target} has local changes; refusing to overwrite them")
            if completed.returncode:
                detail = _decode_output(completed.stderr or completed.stdout)
                raise BreakingChangeError(
                    detail or f"git {' '.join(args)} failed with exit code "
                    f"{completed.returncode}")


def run_breaking_change(*, repo: Path, run_dir: Path, provider_version: str,
                        service: str, test_regex: str, parallel: int = 11) -> bool:
    """Run selected acceptance tests against released and locally built providers."""
    run_dir.mkdir(parents=True, exist_ok=True)
    result_path = run_dir / "breaking-change.json"
    log_path = run_dir / "breaking-change.log"

    print(f"[DEBUG] running breaking-change detection for service {service} "
          f"against AzureRM {provider_version}")
    patch_applied = False
    failure: str | None = None
    completed: subprocess.CompletedProcess[bytes] | None = None
    breaking_change_detected: bool | None = None
    try:
        root = _repo_root(repo)
        _require_clean_target(root)
        patch_bytes = _PATCH_FILE.read_bytes().replace(b"\r", b"")
        _checked_git(root, "apply", "--check", "-", input_data=patch_bytes)
        _checked_git(root, "apply", "-", input_data=patch_bytes)
        patch_applied = True

        env = credentials.subprocess_env(*helpers.ACCTEST_REQUIRED_ENV)
        env.update({"TF_ACC": "1", "TF_ACC_PROVIDER_VERSION": provider_version})
        command = [
            "go", "test", "-v", f"./internal/services/{service}",
            "-run", test_regex, "-parallel", str(parallel), "-timeout", _TEST_TIMEOUT,
        ]
        with log_path.open("w", encoding="utf-8") as log:
            completed = subprocess.run(
                command,
                cwd=root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
    except (OSError, BreakingChangeError) as exc:
        failure = str(exc)
    finally:
        if patch_applied:
            try:
                _checked_git(root, "apply", "--reverse", "-", input_data=patch_bytes)
            except (OSError, BreakingChangeError) as exc:
                targets = ", ".join(_PATCH_TARGETS)
                failure = f"could not remove temporary changes from {targets}: {exc}"

    if completed is not None:
        try:
            log_output = log_path.read_text(encoding="utf-8", errors="replace")
            breaking_change_detected = _BREAKING_CHANGE_LOG_MARKER in log_output
        except OSError as exc:
            log_error = f"could not inspect breaking-change log {log_path}: {exc}"
            failure = f"{failure}; {log_error}" if failure else log_error

    if failure is not None:
        helpers.write_result(result_path, {
            "status": "failed",
            "provider_version": provider_version,
            "service": service,
            "test_regex": test_regex,
            "breaking_change_detected": breaking_change_detected,
            "error": failure,
            "log": str(log_path),
        })
        if breaking_change_detected:
            print(f"[DEBUG] breaking change detected; see {log_path}")
        print(f"[DEBUG] breaking-change detection failed: {failure}")
        return False
    if completed is None:
        raise RuntimeError("breaking-change test completed without a process result")

    try:
        succeeded = completed.returncode == 0 and not breaking_change_detected
        helpers.write_result(result_path, {
            "status": "success" if succeeded else "failed",
            "provider_version": provider_version,
            "service": service,
            "test_regex": test_regex,
            "breaking_change_detected": breaking_change_detected,
            "exit_code": completed.returncode,
            "log": str(log_path),
        })
        if breaking_change_detected:
            print(f"[DEBUG] breaking change detected; see {log_path}")
        else:
            print(f"[DEBUG] breaking-change detection "
                  f"{'passed' if succeeded else 'failed'}; see {log_path}")
        return succeeded
    except OSError as exc:
        print(f"[DEBUG] breaking-change result could not be written: {exc}")
        return False
