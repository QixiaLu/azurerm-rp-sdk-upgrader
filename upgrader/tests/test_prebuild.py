"""Unit tests for local SDK prebuild mechanics and CLI validation."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from upgrader import cli, helpers, prebuild


class TestServiceConfig(unittest.TestCase):
    def test_adds_version_to_matching_service(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "resource-manager.hcl"
            config.write_text(
                'service "network" {\n'
                '  name      = "Network"\n'
                '  available = ["2025-05-01"]\n'
                '}\n',
                encoding="utf-8",
            )

            changed = prebuild.add_service_version(config, "Network", "2025-07-01")

            self.assertTrue(changed)
            self.assertIn(
                'available = ["2025-05-01", "2025-07-01"]',
                config.read_text(encoding="utf-8"),
            )

    def test_existing_version_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "resource-manager.hcl"
            original = (
                'service "network" {\n'
                '  name = "Network"\n'
                '  available = ["2025-07-01"]\n'
                '}\n'
            )
            config.write_text(original, encoding="utf-8")

            changed = prebuild.add_service_version(config, "network", "2025-07-01")

            self.assertFalse(changed)
            self.assertEqual(config.read_text(encoding="utf-8"), original)

    def test_missing_service_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "resource-manager.hcl"
            config.write_text(
                'service "compute" {\n  name = "Compute"\n  available = []\n}\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(prebuild.PrebuildError, "was not found"):
                prebuild.add_service_version(config, "Network", "2025-07-01")

    def test_ambiguous_service_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "resource-manager.hcl"
            block = 'service "network" {\n  name = "Network"\n  available = []\n}\n'
            config.write_text(block + block, encoding="utf-8")

            with self.assertRaisesRegex(prebuild.PrebuildError, "ambiguous"):
                prebuild.add_service_version(config, "Network", "2025-07-01")


class TestGeneratedOutput(unittest.TestCase):
    def test_finds_the_named_service(self):
        with tempfile.TemporaryDirectory() as temp:
            expected = Path(temp) / "resource-manager" / "network" / "2025-07-01"
            expected.mkdir(parents=True)
            self.assertEqual(
                prebuild.find_generated_version(Path(temp), "Network", "2025-07-01"), expected)

    def test_ignores_other_services_released_on_the_same_date(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "resource-manager"
            expected = root / "paloaltonetworks" / "2026-06-01"
            expected.mkdir(parents=True)
            (root / "mongocluster" / "2026-06-01").mkdir(parents=True)
            (root / "discovery" / "2026-06-01").mkdir(parents=True)
            self.assertEqual(
                prebuild.find_generated_version(Path(temp), "PaloAltoNetworks", "2026-06-01"),
                expected)

    def test_missing_service_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "resource-manager" / "other" / "2025-07-01").mkdir(parents=True)
            with self.assertRaisesRegex(prebuild.PrebuildError, "expected one"):
                prebuild.find_generated_version(Path(temp), "Network", "2025-07-01")


class TestRunPrebuild(unittest.TestCase):
    def test_missing_layout_fails_and_records_the_error(self):
        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "run"

            succeeded = prebuild.run_prebuild(
                repo=Path(temp) / "azurerm",
                run_dir=run_dir,
                target="2025-07-01",
                pandora_repo=Path(temp) / "pandora",
                go_azure_sdk_repo=Path(temp) / "go-azure-sdk",
                pandora_service="Network",
            )

            self.assertFalse(succeeded)
            result = helpers.read_result(run_dir / "prebuild.json")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["step"], "preflight")
            self.assertIn("missing", result["error"])


class TestCliArguments(unittest.TestCase):
    def _base(self) -> list[str]:
        # --env-file points at nothing so a real ./.env can't leak into the test process
        return ["network", "2025-07-01", "--repo", "azurerm",
                "--env-file", "no-such.env"]

    def test_prebuild_requires_all_local_arguments(self):
        with self.assertRaises(SystemExit) as raised:
            cli.main(self._base() + ["--stage", "prebuild", "--pandora-repo", "pandora"])
        self.assertEqual(raised.exception.code, 2)

    def test_local_arguments_require_prebuild_stage(self):
        with self.assertRaises(SystemExit) as raised:
            cli.main(self._base() + ["--pandora-service", "Network"])
        self.assertEqual(raised.exception.code, 2)

    @patch("upgrader.cli.run", return_value=True)
    def test_valid_prebuild_arguments_are_forwarded(self, run):
        result = cli.main(self._base() + [
            "--stage", "prebuild",
            "--pandora-repo", "pandora",
            "--go-azure-sdk-repo", "go-azure-sdk",
            "--pandora-service", "Network",
        ])

        self.assertEqual(result, 0)
        kwargs = run.call_args.kwargs
        self.assertTrue(kwargs["prebuild_local_sdk"])
        self.assertEqual(kwargs["pandora_service"], "Network")
        self.assertTrue(kwargs["pandora_repo"].is_absolute())
        self.assertTrue(kwargs["go_azure_sdk_repo"].is_absolute())

    def test_breaking_change_stage_requires_provider_version(self):
        with self.assertRaises(SystemExit) as raised:
            cli.main(self._base() + ["--stage", "breaking-change"])
        self.assertEqual(raised.exception.code, 2)

    @patch("upgrader.cli.run", return_value=True)
    def test_breaking_change_arguments_are_forwarded(self, run):
        result = cli.main(self._base() + [
            "--stage", "breaking-change",
            "--provider-version", "4.77.0",
        ])

        self.assertEqual(result, 0)
        kwargs = run.call_args.kwargs
        self.assertFalse(kwargs["run_execute"])
        self.assertFalse(kwargs["run_acctest"])
        self.assertEqual(kwargs["breaking_change_provider_version"], "4.77.0")

    @patch("upgrader.cli.run", return_value=True)
    def test_default_stages_are_upgrade_and_acctest(self, run):
        result = cli.main(self._base())

        self.assertEqual(result, 0)
        kwargs = run.call_args.kwargs
        self.assertTrue(kwargs["run_execute"])
        self.assertTrue(kwargs["run_acctest"])
        self.assertFalse(kwargs["prebuild_local_sdk"])
        self.assertIsNone(kwargs["breaking_change_provider_version"])

    @patch("upgrader.cli.run", return_value=True)
    def test_explicit_stages_replace_default_stages(self, run):
        result = cli.main(self._base() + [
            "--stage", "upgrade",
            "--stage", "breaking-change",
            "--provider-version", "4.77.0",
        ])

        self.assertEqual(result, 0)
        kwargs = run.call_args.kwargs
        self.assertTrue(kwargs["run_execute"])
        self.assertFalse(kwargs["run_acctest"])
        self.assertEqual(kwargs["breaking_change_provider_version"], "4.77.0")

    def test_stage_specific_arguments_require_their_stage(self):
        with self.assertRaises(SystemExit) as raised:
            cli.main(self._base() + ["--provider-version", "4.77.0"])
        self.assertEqual(raised.exception.code, 2)

    def test_bounded_work_options_must_be_positive(self):
        for option in ("--parallel", "--max-rounds"):
            with self.subTest(option=option), self.assertRaises(SystemExit) as raised:
                cli.main(self._base() + [option, "0"])
            self.assertEqual(raised.exception.code, 2)


class TestEnvFile(unittest.TestCase):
    def test_loads_pairs_without_overriding_the_real_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / ".env"
            env_file.write_text(
                "# comment\n"
                "\n"
                'UPGRADER_TEST_TOKEN="abc123"\n'
                "export UPGRADER_TEST_LOCATION = westeurope\n"
                "UPGRADER_TEST_PRESET=from-file\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"UPGRADER_TEST_PRESET": "from-environment"}, clear=False):
                for name in ("UPGRADER_TEST_TOKEN", "UPGRADER_TEST_LOCATION"):
                    os.environ.pop(name, None)
                cli.load_env(env_file)

                self.assertEqual(os.environ["UPGRADER_TEST_TOKEN"], "abc123")
                self.assertEqual(os.environ["UPGRADER_TEST_LOCATION"], "westeurope")
                self.assertEqual(os.environ["UPGRADER_TEST_PRESET"], "from-environment")

    def test_missing_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            cli.load_env(Path(temp) / "absent.env")


if __name__ == "__main__":
    unittest.main()
