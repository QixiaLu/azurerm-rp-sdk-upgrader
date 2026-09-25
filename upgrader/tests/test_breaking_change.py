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
        run_process.return_value = subprocess.CompletedProcess([], 0)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "azurerm"
            repo.mkdir()

            succeeded = breaking_change.run_breaking_change(
                repo=repo,
                run_dir=root / "run",
                provider_version="4.77.0",
                service="keyvault",
                test_regex="TestAccKeyVault_",
                parallel=5,
            )

            self.assertTrue(succeeded)
            command = run_process.call_args.args[0]
            self.assertEqual(command[:4], [
                breaking_change._bash_executable(), "-s", "--", "4.77.0",
            ])
            self.assertEqual(command[4:], [
                "--", "go", "test", "-v",
                "./internal/services/keyvault", "-run", "TestAccKeyVault_",
                "-parallel", "5", "-timeout", "180m",
            ])
            self.assertEqual(run_process.call_args.kwargs["cwd"], repo)
            self.assertTrue(
                run_process.call_args.kwargs["input"].startswith(b"#!/usr/bin/env bash\n"))
            self.assertNotIn(b"\r", run_process.call_args.kwargs["input"])
            self.assertNotIn("text", run_process.call_args.kwargs)
            subprocess_env.assert_called_once_with(*helpers.ACCTEST_REQUIRED_ENV)
            self.assertEqual(
                run_process.call_args.kwargs["env"], {"ARM_CLIENT_ID": "test-client"})
            result = helpers.read_result(root / "run" / "breaking-change.json")
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["exit_code"], 0)

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