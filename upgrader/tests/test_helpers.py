"""Unit tests for the deterministic acctest helpers (stdlib ``unittest``).

Run with::

    python -m unittest upgrader.tests.test_helpers

These cover the *pure* reduction functions — the mechanical work that used to
live as ``jq``/``comm`` prose in the collect skill — so a regression there is
caught without a Copilot session or a live acctest run.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from argparse import Namespace

from upgrader import helpers, upgrade


def _jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events)


# A tiny but representative ``go test -json`` stream: one pass, one fail (with a
# subtest), one skip, plus some non-JSON ``make`` noise the parser must ignore.
SAMPLE_RUN = _jsonl(
    {"Action": "run", "Test": "TestAccKeyVault_basic"},
    {"Action": "output", "Test": "TestAccKeyVault_basic", "Output": "=== RUN\n"},
    {"Action": "pass", "Test": "TestAccKeyVault_basic"},
    {"Action": "run", "Test": "TestAccKeyVault_update"},
    {"Action": "output", "Test": "TestAccKeyVault_update",
     "Output": "    Error: expected soft_delete_enabled to be true\n"},
    {"Action": "output", "Test": "TestAccKeyVault_update/step2",
     "Output": "    Step 2/3 error: attribute renamed\n"},
    {"Action": "fail", "Test": "TestAccKeyVault_update/step2"},
    {"Action": "fail", "Test": "TestAccKeyVault_update"},
    {"Action": "run", "Test": "TestAccKeyVault_legacy"},
    {"Action": "skip", "Test": "TestAccKeyVault_legacy"},
) + "\nmake: *** [acctests] Error 1\n"


class TestKey(unittest.TestCase):
    def test_bare_go_name(self):
        self.assertEqual(helpers.test_key("TestAccKeyVault_basic"), "TestAccKeyVault_basic")

    def test_teamcity_prefixed(self):
        self.assertEqual(
            helpers.test_key("azurerm: TestAccKeyVault_basic"), "TestAccKeyVault_basic")
        self.assertEqual(
            helpers.test_key("pkg/path TestAccKeyVault_update"), "TestAccKeyVault_update")

    def test_subtest_path_kept(self):
        self.assertEqual(
            helpers.test_key("TestAccKeyVault_update/step2"), "TestAccKeyVault_update/step2")

    def test_empty(self):
        self.assertEqual(helpers.test_key(""), "")


class TestParseGoTestJson(unittest.TestCase):
    def setUp(self):
        self.parsed = helpers.parse_go_test_json(SAMPLE_RUN)

    def test_top_level_counts(self):
        self.assertEqual(self.parsed["counts"],
                         {"total": 3, "passed": 1, "failed": 1, "skipped": 1})

    def test_subtest_recorded_but_not_counted(self):
        self.assertEqual(self.parsed["tests"]["TestAccKeyVault_update/step2"], "fail")

    def test_ignores_non_json_noise(self):
        self.assertEqual(self.parsed["tests"]["TestAccKeyVault_basic"], "pass")


class TestNewFailures(unittest.TestCase):
    def setUp(self):
        self.local = helpers.parse_go_test_json(SAMPLE_RUN)["tests"]

    def test_absent_baseline_is_new(self):
        self.assertEqual(helpers.new_failures(self.local, {}), ["TestAccKeyVault_update"])

    def test_known_failing_baseline_is_not_new(self):
        baseline = {"TestAccKeyVault_update": "FAILURE"}
        self.assertEqual(helpers.new_failures(self.local, baseline), [])

    def test_baseline_success_still_new(self):
        baseline = {"TestAccKeyVault_update": "SUCCESS"}
        self.assertEqual(helpers.new_failures(self.local, baseline), ["TestAccKeyVault_update"])

    def test_teamcity_prefixed_baseline_matches(self):
        baseline = {"azurerm: TestAccKeyVault_update": "FAILURE"}
        self.assertEqual(helpers.new_failures(self.local, baseline), [])


class TestSliceAndSignature(unittest.TestCase):
    def test_slice_includes_subtest_output(self):
        out = helpers.slice_test_output(SAMPLE_RUN, "TestAccKeyVault_update")
        self.assertIn("expected soft_delete_enabled", out)
        self.assertIn("attribute renamed", out)

    def test_slice_isolates_test(self):
        out = helpers.slice_test_output(SAMPLE_RUN, "TestAccKeyVault_basic")
        self.assertIn("=== RUN", out)
        self.assertNotIn("attribute renamed", out)

    def test_signature_picks_error_line(self):
        sig = helpers.failure_signature("noise\n    Error: expected foo to be bar\nmore")
        self.assertEqual(sig, "Error: expected foo to be bar")

    def test_signature_normalizes_volatile_tokens(self):
        a = helpers.failure_signature("Error: resource acctestRG231007 missing")
        b = helpers.failure_signature("Error: resource acctestRG990101 missing")
        self.assertEqual(a, b)

    def test_signature_empty_when_no_error(self):
        self.assertEqual(helpers.failure_signature("all good\ndone"), "")


class TestCluster(unittest.TestCase):
    def test_groups_same_signature(self):
        outputs = {
            "TestA": "Error: attribute soft_delete renamed",
            "TestB": "Error: attribute soft_delete renamed",
            "TestC": "Error: something else entirely",
        }
        clusters = helpers.cluster_failures(outputs)
        sig_ab = helpers.failure_signature(outputs["TestA"])
        self.assertEqual(clusters[sig_ab], ["TestA", "TestB"])
        self.assertEqual(len(clusters), 2)


class TestAnalyzeRun(unittest.TestCase):
    def test_end_to_end(self):
        out = helpers.analyze_run(SAMPLE_RUN, "{}")
        self.assertEqual(out["new_failures"], ["TestAccKeyVault_update"])
        self.assertEqual(out["counts"]["new_failed"], 1)
        self.assertEqual(out["counts"]["failed"], 1)
        self.assertIn("TestAccKeyVault_update", out["outputs"])
        clustered = [t for tests in out["clusters"].values() for t in tests]
        self.assertEqual(clustered, ["TestAccKeyVault_update"])


class TestResultSidecar(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "result.json")
            helpers.write_result(p, {"status": "done", "build_passed": True})
            self.assertEqual(helpers.read_result(p),
                             {"status": "done", "build_passed": True})

    def test_read_missing_is_empty(self):
        self.assertEqual(helpers.read_result("/no/such/file.json"), {})


class TestUpgradeProgress(unittest.TestCase):
    def test_format_no_errors(self):
        self.assertIn("none", upgrade._format_build_errors([]))

    def test_format_errors(self):
        errs = [{"file": "a.go", "line": 1, "col": 2, "msg": "undefined: X"}]
        self.assertEqual(upgrade._format_build_errors(errs), "a.go:1:2: undefined: X")

    def test_format_caps(self):
        errs = [{"file": f"f{i}.go", "line": 1, "col": 1, "msg": "e"} for i in range(60)]
        out = upgrade._format_build_errors(errs, cap=50)
        self.assertIn("and 10 more", out)

    def test_merge_green_marks_converged_done(self):
        merged = upgrade._merge_progress({"status": "in_progress", "summary": "s"},
                                         3, {"passed": True, "errors": []})
        self.assertTrue(merged["converged"])
        self.assertTrue(merged["build_passed"])
        self.assertEqual(merged["status"], "done")
        self.assertEqual(merged["rounds"], 3)
        self.assertEqual(merged["summary"], "s")  # LLM fields preserved

    def test_merge_corrects_false_done_claim(self):
        # Model claimed done, but the orchestrator's build still has errors.
        merged = upgrade._merge_progress({"status": "done"}, 2,
                                         {"passed": False, "errors": [{"file": "a.go"}]})
        self.assertFalse(merged["converged"])
        self.assertFalse(merged["build_passed"])
        self.assertEqual(merged["status"], "in_progress")
        self.assertEqual(merged["build_error_count"], 1)

    def test_merge_is_restart_safe_round_counter(self):
        merged = upgrade._merge_progress({"rounds": 4}, 5, {"passed": False, "errors": []})
        self.assertEqual(merged["rounds"], 5)


class TeamCityBaselineTests(unittest.TestCase):
    def test_service_build_type_id_expands_service(self):
        self.assertEqual(
            helpers.service_build_type_id("recoveryservices"),
            "TF_AzureRM_AZURERM_SERVICE_PUBLIC_RECOVERYSERVICES")
        self.assertEqual(
            helpers.service_build_type_id("keyvault", "PRIVATE"),
            "TF_AzureRM_AZURERM_SERVICE_PRIVATE_KEYVAULT")

    def test_capture_degrades_and_writes_empty_baseline_without_token(self):
        # No TEAMCITY token in env -> degraded mode, empty baseline on disk, no network call.
        saved = {k: os.environ.pop(k) for k in ("TEAMCITY_TOKEN", "TEAMCITY_ACCESSTOKEN")
                 if k in os.environ}
        try:
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "baseline.json")
                result = helpers.capture_teamcity_baseline("recoveryservices", out)
                self.assertEqual(result["mode"], "degraded")
                self.assertIn("TEAMCITY", result["reason"])
                with open(out, encoding="utf-8") as fh:
                    self.assertEqual(json.load(fh), {})
        finally:
            os.environ.update(saved)


class AcctestLaunchTests(unittest.TestCase):
    def test_make_testargs_default_regex(self):
        self.assertEqual(helpers.make_testargs("TestAcc", 11),
                         '-run="TestAcc" -parallel 11 -json')

    def test_make_testargs_doubles_dollar_and_keeps_metachars(self):
        # `$` must be doubled for make; `(`/`|` stay literal inside the double quotes.
        self.assertEqual(helpers.make_testargs("TestAccFoo(Bar|Baz)$", 5),
                         '-run="TestAccFoo(Bar|Baz)$$" -parallel 5 -json')

    def test_make_testargs_multiple_dollars(self):
        self.assertEqual(helpers.make_testargs("A$|B$", 1),
                         '-run="A$$|B$$" -parallel 1 -json')

    def test_missing_acctest_env_lists_absent_vars(self):
        self.assertEqual(helpers.missing_acctest_env({}), list(helpers.ACCTEST_REQUIRED_ENV))
        full = {k: "x" for k in helpers.ACCTEST_REQUIRED_ENV}
        self.assertEqual(helpers.missing_acctest_env(full), [])
        partial = dict(full)
        del partial["ARM_TENANT_ID"]
        self.assertEqual(helpers.missing_acctest_env(partial), ["ARM_TENANT_ID"])


class DetectVersionTests(unittest.TestCase):
    def test_detects_most_common_version_from_imports(self):
        with tempfile.TemporaryDirectory() as d:
            svc = os.path.join(d, "internal", "services", "keyvault")
            os.makedirs(svc)
            with open(os.path.join(svc, "client.go"), "w", encoding="utf-8") as fh:
                fh.write('import "github.com/hashicorp/go-azure-sdk/'
                         'resource-manager/keyvault/2023-07-01/vaults"\n')
            with open(os.path.join(svc, "resource.go"), "w", encoding="utf-8") as fh:
                fh.write('import "github.com/hashicorp/go-azure-sdk/'
                         'resource-manager/keyvault/2023-07-01/keys"\n')
            self.assertEqual(helpers.detect_current_api_version(d, "keyvault"), "2023-07-01")

    def test_returns_none_when_no_service_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(helpers.detect_current_api_version(d, "nope"))


class AzureSpecsContextTests(unittest.TestCase):
    def test_extract_swagger_ops_parses_operations(self):
        doc = json.dumps({
            "info": {"title": "Vaults", "version": "2024-01-01"},
            "paths": {
                "/subscriptions/{id}/providers/Microsoft.KeyVault/vaults": {
                    "get": {"operationId": "Vaults_List"},
                    "put": {"operationId": "Vaults_CreateOrUpdate"},
                }
            },
        })
        out = helpers._extract_swagger_ops(doc)
        self.assertEqual(out["title"], "Vaults")
        self.assertEqual(out["version"], "2024-01-01")
        self.assertEqual(len(out["operations"]), 2)
        self.assertEqual(out["operations"][0]["signature"],
                         "GET /subscriptions/{id}/providers/Microsoft.KeyVault/vaults")

    def test_collect_context_uses_old_new_diff(self):
        target_path = (
            "specification/keyvault/resource-manager/Microsoft.KeyVault/stable/"
            "2024-01-01/vaults.json"
        )
        old_path = target_path.replace("/2024-01-01/", "/2023-07-01/")
        old_doc = json.dumps({
            "info": {"title": "Vaults", "version": "2023-07-01"},
            "paths": {
                "/vaults": {"get": {"operationId": "Vaults_Get"}}
            },
        })
        new_doc = json.dumps({
            "info": {"title": "Vaults", "version": "2024-01-01"},
            "paths": {
                "/vaults": {
                    "get": {"operationId": "Vaults_Get"},
                    "put": {"operationId": "Vaults_CreateOrUpdate"},
                }
            },
        })

        original_search = helpers._search_azure_spec_paths
        original_fetch = helpers._gh_repo_file_text
        try:
            helpers._search_azure_spec_paths = lambda *_args, **_kwargs: [target_path]

            def fake_fetch(_repo: str, path: str, _ref: str) -> str | None:
                if path == target_path:
                    return new_doc
                if path == old_path:
                    return old_doc
                return None

            helpers._gh_repo_file_text = fake_fetch
            ctx = helpers.collect_azure_rest_api_specs_context(
                "keyvault", "2024-01-01", "2023-07-01")
        finally:
            helpers._search_azure_spec_paths = original_search
            helpers._gh_repo_file_text = original_fetch

        self.assertEqual(ctx["mode"], "normal")
        self.assertEqual(len(ctx["entries"]), 1)
        diff = ctx["entries"][0]["operation_diff"]
        self.assertIn("PUT /vaults", diff["added"])

    def test_format_context_degraded(self):
        out = helpers.format_azure_rest_api_specs_context(
            {"mode": "degraded", "reason": "gh api search failed"})
        self.assertIn("unavailable", out)
        self.assertIn("gh api search failed", out)

    def test_format_diff_context_contains_operation_delta(self):
        ctx = {
            "mode": "normal",
            "repo": "Azure/azure-rest-api-specs",
            "ref": "main",
            "rp_name": "keyvault",
            "target_api_version": "2024-01-01",
            "old_api_version": "2023-07-01",
            "entries": [{
                "path": "new.json",
                "old_path": "old.json",
                "operation_count": 10,
                "old_operation_count": 8,
                "operation_diff": {
                    "added": ["PUT /vaults"],
                    "removed": ["DELETE /legacy"],
                },
            }],
        }
        out = helpers.format_azure_rest_api_specs_diff(ctx)
        self.assertIn("old_file: old.json", out)
        self.assertIn("added_ops: PUT /vaults", out)
        self.assertIn("removed_ops: DELETE /legacy", out)

    def test_format_diff_context_respects_char_budget(self):
        ctx = {
            "mode": "normal",
            "repo": "r",
            "ref": "main",
            "rp_name": "kv",
            "target_api_version": "2024-01-01",
            "old_api_version": "2023-07-01",
            "entries": [{
                "path": "a.json",
                "operation_count": 2,
                "old_operation_count": 1,
                "operation_diff": {"added": ["PUT /" * 50], "removed": []},
            }],
        }
        out = helpers.format_azure_rest_api_specs_diff(ctx, char_budget=120)
        self.assertIn("truncated by char budget", out)

    def test_cli_specs_context_writes_output(self):
        original_collect = helpers.collect_azure_rest_api_specs_context
        original_format = helpers.format_azure_rest_api_specs_context
        try:
            helpers.collect_azure_rest_api_specs_context = lambda *_a, **_k: {
                "mode": "normal", "reason": "", "entries": [{"path": "p"}]
            }
            helpers.format_azure_rest_api_specs_context = lambda *_a, **_k: "ctx"
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "specs.txt")
                code = helpers._cli_specs_context(Namespace(
                    service="keyvault",
                    target_api_version="2024-01-01",
                    old_api_version="2023-07-01",
                    repo="Azure/azure-rest-api-specs",
                    ref="main",
                    max_files=2,
                    max_ops_per_file=3,
                    max_delta_per_file=2,
                    output=out,
                    raw_json=False,
                ))
                self.assertEqual(code, 0)
                with open(out, encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), "ctx\n")
        finally:
            helpers.collect_azure_rest_api_specs_context = original_collect
            helpers.format_azure_rest_api_specs_context = original_format


if __name__ == "__main__":
    unittest.main()
