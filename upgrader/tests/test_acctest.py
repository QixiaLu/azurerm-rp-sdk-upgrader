"""Unit tests for acceptance-test orchestration."""

from __future__ import annotations

import unittest
import os
import subprocess
import sys
from pathlib import Path

from upgrader import acctest


class TestLaunchPrompt(unittest.TestCase):
    def test_helper_commands_are_anchored_to_upgrader_root(self):
        prompt = acctest._build_launch_prompt(
            "paloalto",
            test_regex="TestAccPaloAlto",
            parallel=4,
            repo=Path("C:/repos/terraform-provider-azurerm"),
            acctest_dir=Path("C:/tmp/acctest"),
            result_path=Path("C:/tmp/result.json"),
        )

        upgrader_root = Path(acctest.__file__).resolve().parent.parent.as_posix()
        self.assertIn(f"upgrader_root: {upgrader_root}", prompt)
        self.assertIn("Run every helper command from `upgrader_root`", prompt)
        self.assertNotIn("<upgrader_root>", prompt)


class TestPidAlive(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows-specific process probe")
    def test_windows_process_liveness(self):
        self.assertTrue(acctest._pid_alive(os.getpid()))

        process = subprocess.Popen([sys.executable, "-c", "pass"])
        self.assertEqual(process.wait(), 0)
        self.assertFalse(acctest._pid_alive(process.pid))


if __name__ == "__main__":
    unittest.main()