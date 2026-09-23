"""Breaking-change detection stage for AzureRM acceptance tests."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from upgrader import credentials, helpers


def _bash_executable() -> str:
    """Return Git Bash on Windows, avoiding WSL's Linux Go toolchain."""
    if os.name != "nt":
        return "bash"

    candidates = []
    git = shutil.which("git")
    if git:
        candidates.append(Path(git).resolve().parent.parent / "bin" / "bash.exe")
    program_files = os.environ.get("ProgramFiles")
    if program_files:
        candidates.append(Path(program_files) / "Git" / "bin" / "bash.exe")

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise OSError("Git Bash is required for breaking-change detection on Windows")


def run_breaking_change(*, repo: Path, run_dir: Path, provider_version: str,
                        service: str, test_regex: str, parallel: int = 11) -> bool:
    """Run selected acceptance tests against released and locally built providers."""
    run_dir.mkdir(parents=True, exist_ok=True)
    result_path = run_dir / "breaking-change.json"
    log_path = run_dir / "breaking-change.log"
    script = Path(__file__).resolve().parent / "breaking-change-detector.sh"

    print(f"[DEBUG] running breaking-change detection for service {service} "
          f"against AzureRM {provider_version}")
    try:
        command = [
            _bash_executable(), "-s", "--", provider_version, "--",
            "go", "test", "-v", f"./internal/services/{service}",
            "-run", test_regex, "-parallel", str(parallel), "-timeout", "180m",
        ]
        script_input = script.read_text(encoding="utf-8").replace("\r", "").encode("utf-8")
        with log_path.open("w", encoding="utf-8") as log:
            completed = subprocess.run(
                command, cwd=repo, input=script_input,
                stdout=log, stderr=subprocess.STDOUT, check=False,
                env=credentials.subprocess_env(*helpers.ACCTEST_REQUIRED_ENV))
        succeeded = completed.returncode == 0
        helpers.write_result(result_path, {
            "status": "success" if succeeded else "failed",
            "provider_version": provider_version,
            "service": service,
            "test_regex": test_regex,
            "exit_code": completed.returncode,
            "log": str(log_path),
        })
        print(f"[DEBUG] breaking-change detection "
              f"{'passed' if succeeded else 'failed'}; see {log_path}")
        return succeeded
    except OSError as exc:
        helpers.write_result(result_path, {
            "status": "failed",
            "provider_version": provider_version,
            "service": service,
            "test_regex": test_regex,
            "error": str(exc),
            "log": str(log_path),
        })
        print(f"[DEBUG] breaking-change detection could not start: {exc}")
        return False