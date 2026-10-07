"""Unit tests for the breaking-change detection step."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from upgrader import breaking_change, helpers
from upgrader.loop import run


class TestBreakingChangeStage(unittest.TestCase):
    @patch("upgrader.breaking_change.credentials.subprocess_env",
           return_value={"ARM_CLIENT_ID": "test-client"})
    @patch("upgrader.breaking_change.subprocess.run")
    def test_runs_detector_and_records_success(self, run_process, subprocess_env):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "azurerm"
            repo.mkdir()
            run_process.side_effect = [
                subprocess.CompletedProcess([], 0, stdout=f"{repo}\n".encode(), stderr=b""),
                *[
                    subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
                    for _ in range(8)
                ],
            ]

            succeeded = breaking_change.run_breaking_change(
                repo=repo,
                run_dir=root / "run",
                provider_version="4.77.0",
                service="keyvault",
                test_regex="TestAccKeyVault_",
                parallel=5,
            )

            self.assertTrue(succeeded)
            commands = [call.args[0] for call in run_process.call_args_list]
            self.assertEqual(commands, [
                ["git", "rev-parse", "--show-toplevel"],
                ["git", "diff", "--quiet", "--", "internal/acceptance/testcase.go"],
                ["git", "diff", "--cached", "--quiet", "--",
                 "internal/acceptance/testcase.go"],
                ["git", "diff", "--quiet", "--",
                 "internal/acceptance/breaking_change_test.go"],
                ["git", "diff", "--cached", "--quiet", "--",
                 "internal/acceptance/breaking_change_test.go"],
                ["git", "apply", "--check", "-"],
                ["git", "apply", "-"],
                [
                    "go", "test", "-v",
                    "./internal/services/keyvault", "-run", "TestAccKeyVault_",
                    "-parallel", "5", "-timeout", "180m",
                ],
                ["git", "apply", "--reverse", "-"],
            ])
            self.assertEqual(run_process.call_args_list[7].kwargs["cwd"], repo)
            self.assertEqual(
                run_process.call_args_list[7].kwargs["env"],
                {
                    "ARM_CLIENT_ID": "test-client",
                    "TF_ACC": "1",
                    "TF_ACC_PROVIDER_VERSION": "4.77.0",
                },
            )
            self.assertEqual(run_process.call_args_list[5].kwargs["input"],
                             run_process.call_args_list[6].kwargs["input"])
            self.assertEqual(run_process.call_args_list[6].kwargs["input"],
                             run_process.call_args_list[8].kwargs["input"])
            self.assertNotIn(b"\r", run_process.call_args_list[5].kwargs["input"])
            subprocess_env.assert_called_once_with(*helpers.ACCTEST_REQUIRED_ENV)
            result = helpers.read_result(root / "run" / "breaking-change.json")
            self.assertEqual(result["status"], "success")
            self.assertFalse(result["breaking_change_detected"])
            self.assertEqual(result["exit_code"], 0)

    @patch("upgrader.breaking_change.subprocess.run")
    def test_refuses_local_changes_to_patch_target(self, run_process):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "azurerm"
            repo.mkdir()
            run_process.side_effect = [
                subprocess.CompletedProcess([], 0, stdout=f"{repo}\n".encode(), stderr=b""),
                subprocess.CompletedProcess([], 1, stdout=b"", stderr=b""),
            ]

            succeeded = breaking_change.run_breaking_change(
                repo=repo,
                run_dir=root / "run",
                provider_version="4.77.0",
                service="keyvault",
                test_regex="TestAccKeyVault_",
            )

            self.assertFalse(succeeded)
            self.assertEqual(run_process.call_count, 2)
            result = helpers.read_result(root / "run" / "breaking-change.json")
            self.assertEqual(result["status"], "failed")
            self.assertIn("local changes", result["error"])

    @patch("upgrader.breaking_change.credentials.subprocess_env",
           return_value={"ARM_CLIENT_ID": "test-client"})
    @patch("upgrader.breaking_change.subprocess.run")
    def test_reverts_patch_when_tests_fail(self, run_process, subprocess_env):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "azurerm"
            repo.mkdir()
            run_process.side_effect = [
                subprocess.CompletedProcess([], 0, stdout=f"{repo}\n".encode(), stderr=b""),
                *[
                    subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
                    for _ in range(6)
                ],
                subprocess.CompletedProcess([], 1, stdout=b"", stderr=b""),
                subprocess.CompletedProcess([], 0, stdout=b"", stderr=b""),
            ]

            succeeded = breaking_change.run_breaking_change(
                repo=repo,
                run_dir=root / "run",
                provider_version="4.77.0",
                service="keyvault",
                test_regex="TestAccKeyVault_",
            )

            self.assertFalse(succeeded)
            self.assertEqual(
                run_process.call_args_list[-1].args[0],
                ["git", "apply", "--reverse", "-"],
            )
            result = helpers.read_result(root / "run" / "breaking-change.json")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["exit_code"], 1)
            self.assertFalse(result["breaking_change_detected"])

    @patch("upgrader.breaking_change.credentials.subprocess_env",
           return_value={"ARM_CLIENT_ID": "test-client"})
    @patch("upgrader.breaking_change.subprocess.run")
    def test_reports_breaking_change_when_marker_is_in_log(
            self, run_process, subprocess_env):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "azurerm"
            repo.mkdir()

            def run_and_log_marker(command, **kwargs):
                if command[:2] == ["git", "rev-parse"]:
                    return subprocess.CompletedProcess(
                        command, 0, stdout=f"{repo}\n".encode(), stderr=b"")
                if command[0] == "go":
                    kwargs["stdout"].write(
                        "Error: ImportStateVerify attributes not equivalent\n")
                    return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"")
                return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

            run_process.side_effect = run_and_log_marker

            succeeded = breaking_change.run_breaking_change(
                repo=repo,
                run_dir=root / "run",
                provider_version="4.77.0",
                service="keyvault",
                test_regex="TestAccKeyVault_",
            )

            self.assertFalse(succeeded)
            result = helpers.read_result(root / "run" / "breaking-change.json")
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["breaking_change_detected"])
            self.assertEqual(result["exit_code"], 1)

    @patch("upgrader.loop.breaking_change.run_breaking_change", return_value=True)
    @patch("upgrader.loop._with_client")
    def test_breaking_change_can_run_without_execute(self, with_client, run_breaking_change):
        with tempfile.TemporaryDirectory() as temp:
            succeeded = run(
                Path(temp), "keyvault", "2023-07-01", None,
                run_execute=False, run_acctest=False,
                breaking_change_provider_version="4.77.0",
            )
            root = Path(temp)
            self.assertTrue(succeeded)
            latest = helpers.read_result(
                root / ".upgrader" / "keyvault" / "2023-07-01" / "latest.json")
            manifest = helpers.read_result(Path(latest["run"]) / "run.json")
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["status"], "success")
            self.assertEqual(manifest["input"]["stages"], ["breaking-change"])
            self.assertEqual(manifest["stages"]["breaking-change"]["status"], "success")

        with_client.assert_not_called()
        run_breaking_change.assert_called_once()


if __name__ == "__main__":
    unittest.main()