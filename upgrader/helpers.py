"""Deterministic toolbelt for the upgrader stages (stdlib-only).

Every *mechanical* step lives here so the agents keep only the *judgement* steps.
Two groups of functions:

* **acctest analysis** (pure): parse a ``go test -json`` run, load the TeamCity
  baseline, compute the NEW-failure set, slice a single test's output, and cluster
  failures by signature.
* **upgrade mechanics** (subprocess against a checkout): ``go build`` with
  structured compiler errors and current API-version detection.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import base64
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

__all__ = [
    # result sidecar
    "read_result",
    "write_result",
    # acctest analysis
    "test_key",
    "parse_go_test_json",
    "load_baseline",
    "new_failures",
    "slice_test_output",
    "failure_signature",
    "cluster_failures",
    "analyze_run",
    # upgrade mechanics
    "detect_current_api_version",
    "run_go_build",
    # deterministic GitHub spec context
    "collect_azure_rest_api_specs_context",
    "format_azure_rest_api_specs_context",
    "format_azure_rest_api_specs_diff",
    # teamcity baseline
    "service_build_type_id",
    "capture_teamcity_baseline",
    # acctest launch
    "missing_acctest_env",
    "make_testargs",
    "launch_acctests",
]


# --------------------------------------------------------------------------- #
# result.json sidecar (canonical read/write)
# --------------------------------------------------------------------------- #

def read_result(result_path: str | Path) -> dict[str, Any]:
    """Read an agent's JSON result sidecar, or ``{}`` if missing/unparsable."""
    try:
        data = json.loads(Path(result_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_result(result_path: str | Path, obj: dict[str, Any]) -> None:
    """Write ``obj`` as canonical JSON (sorted keys, 2-space indent, trailing \\n).

    Centralising this keeps every stage's sidecar byte-identical in shape so
    downstream reads never have to cope with hand-assembled JSON.
    """
    text = json.dumps(obj, indent=2, sort_keys=True) + "\n"
    Path(result_path).write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# acctest analysis (pure)
# --------------------------------------------------------------------------- #

# A Go test identifier (plus any ``/subtest`` path). Used to derive a stable
# join key across the two sources whose names are formatted differently:
#   * ``go test -json`` emits the bare function name, e.g. ``TestAccKeyVault_basic``
#   * TeamCity test occurrences can carry a package/suite prefix, e.g.
#     ``azurerm: TestAccKeyVault_basic`` or ``pkg/path TestAccKeyVault_basic``.
# Extracting the ``Test...`` token from both makes the set difference exact.
_TEST_TOKEN = re.compile(r"Test[A-Za-z0-9_]+(?:/[^\s:]+)?")

# TeamCity marks a passing occurrence as ``SUCCESS``; anything else (FAILURE,
# ERROR, ...) counts as a known-failing baseline entry.
_BASELINE_PASS = "SUCCESS"

# First line that looks like the actual assertion / error, used to cluster
# many failing tests under one root cause.
_SIGNATURE_HINTS = (
    "Error:",
    "Error running",
    "panic:",
    "--- FAIL",
    "Step ",
    "expected",
    "unexpected",
)


def test_key(name: str) -> str:
    """Normalise a test name to a stable join key across go-test and TeamCity.

    Returns the ``Test...`` token (including any ``/subtest`` path) when present,
    else the stripped input. This lets :func:`new_failures` diff the two sources
    by identity even though TeamCity prefixes names with a package/suite.
    """
    if not name:
        return ""
    match = _TEST_TOKEN.search(name)
    return match.group(0) if match else name.strip()


def parse_go_test_json(source: str | Path) -> dict[str, Any]:
    """Parse a ``go test -json`` stream into per-test statuses + counts.

    ``source`` may be a path to the ``run.json`` file or the raw text. The stream
    is one JSON object per line (with occasional non-JSON noise from ``make``,
    which is skipped). A test's final status is the last ``pass``/``fail``/``skip``
    action recorded for it.

    Returns ``{"tests": {name: status}, "counts": {total, passed, failed,
    skipped}}`` where ``status`` is ``"pass"``/``"fail"``/``"skip"``. Only
    top-level tests (no ``/`` subtest separator) are counted so the numbers match
    what a human reads off the suite; subtests still appear in ``tests``.
    """
    text = _read_text(source)
    statuses: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        action = evt.get("Action")
        test = evt.get("Test")
        if not test or action not in ("pass", "fail", "skip"):
            continue
        statuses[test] = action

    top = {n: s for n, s in statuses.items() if "/" not in n}
    counts = {
        "total": len(top),
        "passed": sum(1 for s in top.values() if s == "pass"),
        "failed": sum(1 for s in top.values() if s == "fail"),
        "skipped": sum(1 for s in top.values() if s == "skip"),
    }
    return {"tests": statuses, "counts": counts}


def load_baseline(source: str | Path) -> dict[str, str]:
    """Load the TeamCity ``baseline.json`` ({test_name: STATUS}) map, or ``{}``.

    An absent/empty baseline (degraded mode) is a valid ``{}`` — every local
    failure is then treated as NEW, which is the intended fail-loud behaviour.
    """
    try:
        data = json.loads(_read_text(source))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def new_failures(local_tests: dict[str, str], baseline: dict[str, str]) -> list[str]:
    """Return the sorted NEW-failure set: local FAIL minus baseline failures.

    A test is NEW iff it FAILs locally and is *not* already failing on the
    TeamCity ``main`` baseline (i.e. baseline has it as ``SUCCESS`` or does not
    list it at all). Matching is by :func:`test_key`, so package-prefixed
    TeamCity names line up with bare go-test names.
    """
    baseline_failing = {
        test_key(name) for name, status in baseline.items() if status != _BASELINE_PASS
    }
    new: set[str] = set()
    for name, status in local_tests.items():
        if status != "fail" or "/" in name:  # top-level failures only
            continue
        if test_key(name) not in baseline_failing:
            new.add(name)
    return sorted(new)


def slice_test_output(source: str | Path, test_name: str) -> str:
    """Concatenate the ``Output`` chunks a ``go test -json`` stream emitted for one test.

    This is the deterministic replacement for the skill's per-test ``jq`` slice:
    the collect agent gets exactly one test's log without ever reloading the
    whole ``run.json``. Matches the test and any of its subtests.
    """
    text = _read_text(source)
    key = test_key(test_name)
    chunks: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("Action") != "output":
            continue
        etest = evt.get("Test")
        if not etest:
            continue
        ekey = test_key(etest)
        if ekey == key or ekey.startswith(key + "/"):  # the test and its subtests
            out = evt.get("Output")
            if out:
                chunks.append(out)
    return "".join(chunks)


def failure_signature(output: str) -> str:
    """Reduce a test's log to a one-line failure signature for clustering.

    Picks the first line that looks like the real assertion/error (``Error:``,
    ``panic:``, ``--- FAIL``, an ``expected``/``unexpected`` line, ...) and
    strips volatile tokens (resource names, GUIDs, timestamps, line numbers) so
    that many tests failing for the *same* reason collapse to one signature.
    Returns ``""`` when nothing error-like is found.
    """
    chosen = ""
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        if any(hint in line for hint in _SIGNATURE_HINTS):
            chosen = line
            break
    if not chosen:
        return ""
    return _normalize_signature(chosen)


def cluster_failures(outputs: dict[str, str]) -> dict[str, list[str]]:
    """Group ``{test_name: output}`` into ``{signature: [test_name, ...]}``.

    One shared signature usually means one root cause (e.g. a renamed attribute),
    so a human/agent can address a whole cluster with a single decision. Tests
    with no recognisable signature are grouped under ``""``.
    """
    clusters: dict[str, list[str]] = defaultdict(list)
    for test, output in outputs.items():
        clusters[failure_signature(output)].append(test)
    return {sig: sorted(tests) for sig, tests in sorted(clusters.items())}


def analyze_run(run_json: str | Path, baseline_json: str | Path) -> dict[str, Any]:
    """End-to-end deterministic reduction of a finished run.

    Parses ``run.json``, loads ``baseline.json``, computes the NEW-failure set,
    slices each NEW failure's log, clusters them by signature, and returns a
    ready-to-report structure. The agent then only has to *diagnose* each
    cluster (breaking change / test-side / api-bug / flake) — every mechanical
    step above is done here, deterministically.

    Returns::

        {
          "counts": {total, passed, failed, skipped, new_failed},
          "new_failures": [test, ...],
          "clusters": {signature: [test, ...]},
          "outputs": {test: log},   # NEW failures only
        }
    """
    parsed = parse_go_test_json(run_json)
    baseline = load_baseline(baseline_json)
    new = new_failures(parsed["tests"], baseline)
    outputs = {t: slice_test_output(run_json, t) for t in new}
    clusters = cluster_failures(outputs)
    counts = dict(parsed.get("counts", {}))
    counts["new_failed"] = len(new)
    return {
        "counts": counts,
        "new_failures": new,
        "clusters": clusters,
        "outputs": outputs,
    }


# --------------------------------------------------------------------------- #
# upgrade mechanics (subprocess against a checkout)
# --------------------------------------------------------------------------- #

# go-azure-sdk import path segment carrying the API version, e.g.
#   .../resource-manager/keyvault/2023-07-01/vaults
_API_VERSION_IN_IMPORT = re.compile(
    r"/resource-manager/[^/\"]+/(?P<version>\d{4}-\d{2}-\d{2}(?:-preview)?)/"
)

# A Go compiler diagnostic line: ``path/file.go:12:34: message``.
_GO_COMPILE_ERROR = re.compile(
    r"^(?P<file>\S+\.go):(?P<line>\d+):(?P<col>\d+):\s*(?P<msg>.*)$"
)

_HTTP_METHODS = ("get", "put", "post", "patch", "delete", "head", "options", "trace")

_GITHUB_SPEC_REPO = "Azure/azure-rest-api-specs"
_GITHUB_SPEC_REF = "main"
_GITHUB_SPEC_MAX_FILES = 4


def detect_current_api_version(repo: str | Path, rp_name: str) -> str | None:
    """Return the API version an RP currently imports, or ``None`` if undetectable.

    Scans ``internal/services/<rp_name>/**/*.go`` for go-azure-sdk import paths
    and returns the most common ``YYYY-MM-DD[-preview]`` segment. This is the
    deterministic version of "study how the SDK is used today" so the upgrade
    agent no longer has to eyeball imports to find the version it's moving from.
    """
    root = Path(repo) / "internal" / "services" / str(rp_name)
    if not root.is_dir():
        return None
    versions: Counter[str] = Counter()
    for go_file in root.rglob("*.go"):
        try:
            text = go_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in _API_VERSION_IN_IMPORT.finditer(text):
            versions[m.group("version")] += 1
    if not versions:
        return None
    return versions.most_common(1)[0][0]


def _parse_go_errors(text: str) -> list[dict[str, Any]]:
    """Extract ``file.go:line:col: msg`` compiler diagnostics from Go tool output."""
    return [
        {"file": m.group("file"), "line": int(m.group("line")),
         "col": int(m.group("col")), "msg": m.group("msg").strip()}
        for m in (_GO_COMPILE_ERROR.match(l) for l in text.splitlines())
        if m
    ]


def run_go_build(repo: str | Path, package: str = "./...",
                 timeout: int = 3600, include_tests: bool = True,
                 test_package: str = "./...") -> dict[str, Any]:
    """Compile ``<package>`` (and vet the tree) in ``repo`` and return a structured result.

    Returns ``{"passed": bool, "returncode": int, "errors": [{file, line, col,
    msg}], "raw_tail": str}``. ``errors`` is parsed from the compiler's
    ``file.go:line:col: msg`` diagnostics so the agent gets an objective,
    machine-checkable build signal instead of parsing prose.

    ``go build ./...`` deliberately skips ``*_test.go`` files, so an SDK API bump
    can leave the tree "green" while every ``_test.go`` in the RP fails to compile.
    With ``include_tests`` (default), a second pass runs ``go vet`` over
    ``test_package`` — vet type-checks ``*_test.go`` (surfacing test-file compile
    breakages ``go build`` misses) without linking or running any test binary.
    Diagnostics from both passes are unioned and de-duplicated.
    """
    proc = _run(["go", "build", package], repo, timeout)
    combined = (proc.stdout or "") + (proc.stderr or "")
    returncode = proc.returncode

    if include_tests:
        vet_proc = _run(["go", "vet", test_package], repo, timeout)
        combined += "\n" + (vet_proc.stdout or "") + (vet_proc.stderr or "")
        if returncode == 0:
            returncode = vet_proc.returncode

    seen: set[tuple[str, int, int, str]] = set()
    errors: list[dict[str, Any]] = []
    for err in _parse_go_errors(combined):
        key = (err["file"], err["line"], err["col"], err["msg"])
        if key not in seen:
            seen.add(key)
            errors.append(err)

    return {
        "passed": returncode == 0,
        "returncode": returncode,
        "errors": errors,
        "raw_tail": "\n".join(combined.splitlines()[-40:]),
    }


def _gh_api_json(endpoint: str, *, fields: dict[str, str] | None = None,
                 timeout: int = 120) -> Any:
    """Run ``gh api`` and return parsed JSON, raising on transport/API failures."""
    cmd = ["gh", "api", endpoint, "-H", "Accept: application/vnd.github+json"]
    for key, value in (fields or {}).items():
        cmd.extend(["-f", f"{key}={value}"])
    proc = _run(cmd, Path.cwd(), timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "gh api failed").strip())
    out = (proc.stdout or "").strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON from gh api {endpoint}: {exc}") from exc


def _gh_repo_file_text(repo: str, path: str, ref: str) -> str | None:
    """Fetch one repository file via ``gh api repos/<repo>/contents/<path>``."""
    data = _gh_api_json(f"/repos/{repo}/contents/{path}", fields={"ref": ref})
    if not isinstance(data, dict) or data.get("type") != "file":
        return None
    content = data.get("content")
    if not isinstance(content, str):
        return None
    if data.get("encoding") == "base64":
        try:
            return base64.b64decode(content.encode("ascii")).decode("utf-8", errors="ignore")
        except (ValueError, OSError):
            return None
    return content


def _search_azure_spec_paths(rp_name: str, target_api_version: str,
                             *, max_results: int = 20) -> list[str]:
    """Find likely swagger paths for ``rp_name`` + ``target_api_version`` in specs repo."""
    q_primary = (
        f"repo:{_GITHUB_SPEC_REPO} path:specification path:resource-manager "
        f"\"{rp_name}\" \"{target_api_version}\" in:path extension:json"
    )
    data = _gh_api_json("/search/code", fields={"q": q_primary, "per_page": str(max_results)})
    items = data.get("items", []) if isinstance(data, dict) else []
    paths = {
        str(i.get("path"))
        for i in items
        if isinstance(i, dict)
    }
    filtered = sorted(
        p for p in paths
        if p.endswith(".json") and f"/{target_api_version}/" in p and "/resource-manager/" in p
    )
    if filtered:
        return filtered

    q_fallback = (
        f"repo:{_GITHUB_SPEC_REPO} path:specification path:resource-manager "
        f"\"{target_api_version}\" in:path extension:json"
    )
    data = _gh_api_json("/search/code", fields={"q": q_fallback, "per_page": str(max_results)})
    items = data.get("items", []) if isinstance(data, dict) else []
    tokens = [t for t in re.split(r"[^a-z0-9]+", rp_name.lower()) if len(t) >= 4]
    ranked: list[tuple[int, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        if not path.endswith(".json") or f"/{target_api_version}/" not in path:
            continue
        score = sum(1 for t in tokens if t in path.lower())
        ranked.append((score, path))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]))
    return [p for _, p in ranked]


def _extract_swagger_ops(text: str) -> dict[str, Any]:
    """Extract compact operation context from one OpenAPI/Swagger JSON file."""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return {"title": "", "version": "", "operations": []}
    info = doc.get("info", {}) if isinstance(doc, dict) else {}
    paths = doc.get("paths", {}) if isinstance(doc, dict) else {}
    operations: list[dict[str, str]] = []
    if isinstance(paths, dict):
        for route, route_obj in paths.items():
            if not isinstance(route_obj, dict):
                continue
            for method in _HTTP_METHODS:
                op = route_obj.get(method)
                if not isinstance(op, dict):
                    continue
                operations.append({
                    "signature": f"{method.upper()} {route}",
                    "operation_id": str(op.get("operationId") or ""),
                })
    operations.sort(key=lambda row: row["signature"])
    title = str(info.get("title") or "") if isinstance(info, dict) else ""
    version = str(info.get("version") or "") if isinstance(info, dict) else ""
    return {"title": title, "version": version, "operations": operations}


def _diff_operation_signatures(old_ops: list[dict[str, str]],
                               new_ops: list[dict[str, str]]) -> dict[str, list[str]]:
    """Return added/removed operation signatures between old and new swagger contexts."""
    old_set = {str(o.get("signature")) for o in old_ops}
    new_set = {str(o.get("signature")) for o in new_ops}
    return {
        "added": sorted(new_set - old_set),
        "removed": sorted(old_set - new_set),
    }


def collect_azure_rest_api_specs_context(rp_name: str, target_api_version: str,
                                         old_api_version: str | None = None, *,
                                         repo: str = _GITHUB_SPEC_REPO,
                                         ref: str = _GITHUB_SPEC_REF,
                                         max_files: int = _GITHUB_SPEC_MAX_FILES) -> dict[str, Any]:
    """Collect deterministic, compact swagger context via ``gh api`` for agent prompts.

    Returns ``{"mode": "normal"|"degraded", "reason", "entries": [...]}`` where each
    entry contains one target-version spec file's title/version/operations and, when available,
    an old-version operation diff from the corresponding old-version path.
    """
    out: dict[str, Any] = {
        "mode": "degraded",
        "reason": "",
        "repo": repo,
        "ref": ref,
        "rp_name": rp_name,
        "target_api_version": target_api_version,
        "old_api_version": old_api_version,
        "entries": [],
        "searched_paths": [],
    }
    try:
        paths = _search_azure_spec_paths(rp_name, target_api_version)
    except Exception as exc:  # noqa: BLE001 - deterministic degraded mode on tool failure
        out["reason"] = f"gh api search failed: {exc}"
        return out
    out["searched_paths"] = paths
    if not paths:
        out["reason"] = "no matching spec paths found"
        return out

    entries: list[dict[str, Any]] = []
    for path in paths[:max_files]:
        try:
            target_text = _gh_repo_file_text(repo, path, ref)
        except Exception:
            target_text = None
        if not target_text:
            continue
        current = _extract_swagger_ops(target_text)
        entry: dict[str, Any] = {
            "path": path,
            "title": current.get("title", ""),
            "version": current.get("version", ""),
            "operation_count": len(current.get("operations", [])),
            "operations": current.get("operations", []),
        }
        if old_api_version:
            old_path = path.replace(f"/{target_api_version}/", f"/{old_api_version}/", 1)
            if old_path != path:
                try:
                    old_text = _gh_repo_file_text(repo, old_path, ref)
                except Exception:
                    old_text = None
                if old_text:
                    old = _extract_swagger_ops(old_text)
                    diff = _diff_operation_signatures(
                        old.get("operations", []), current.get("operations", []))
                    entry.update({
                        "old_path": old_path,
                        "old_version": old.get("version", ""),
                        "old_operation_count": len(old.get("operations", [])),
                        "operation_diff": diff,
                    })
        entries.append(entry)

    out["entries"] = entries
    if not entries:
        out["reason"] = "found candidate paths, but could not fetch parseable spec files"
        return out
    out["mode"] = "normal"
    out["reason"] = ""
    return out


def format_azure_rest_api_specs_context(context: dict[str, Any], *,
                                        max_ops_per_file: int = 8,
                                        max_delta_per_file: int = 6) -> str:
    """Render :func:`collect_azure_rest_api_specs_context` output into prompt-safe text."""
    if context.get("mode") != "normal":
        reason = context.get("reason") or "unavailable"
        return f"(Azure REST API specs context unavailable: {reason})"

    lines = [
        f"source: {context.get('repo')}@{context.get('ref')}",
        f"rp_name: {context.get('rp_name')}",
        f"target_api_version: {context.get('target_api_version')}",
    ]
    if context.get("old_api_version"):
        lines.append(f"old_api_version: {context.get('old_api_version')}")
    for entry in context.get("entries", []):
        path = entry.get("path", "")
        lines.append(f"- file: {path}")
        lines.append(f"  info: title={entry.get('title', '')} version={entry.get('version', '')}")
        lines.append(f"  operation_count: {entry.get('operation_count', 0)}")
        for op in entry.get("operations", [])[:max_ops_per_file]:
            op_id = op.get("operation_id") or "(no operationId)"
            lines.append(f"    - {op.get('signature')} :: {op_id}")
        diff = entry.get("operation_diff")
        if isinstance(diff, dict):
            added = diff.get("added", [])[:max_delta_per_file]
            removed = diff.get("removed", [])[:max_delta_per_file]
            lines.append(f"  old_file: {entry.get('old_path', '')}")
            lines.append(f"  old_operation_count: {entry.get('old_operation_count', 0)}")
            if added:
                lines.append(f"  added_ops: {', '.join(added)}")
            if removed:
                lines.append(f"  removed_ops: {', '.join(removed)}")
    return "\n".join(lines)


def format_azure_rest_api_specs_diff(context: dict[str, Any], *,
                                     max_delta_per_file: int = 10,
                                     char_budget: int = 8000) -> str:
    """Render a compact old-vs-target diff summary for direct prompt injection."""
    if context.get("mode") != "normal":
        reason = context.get("reason") or "unavailable"
        return f"(Azure REST API specs diff unavailable: {reason})"

    lines = [
        f"source: {context.get('repo')}@{context.get('ref')}",
        f"rp_name: {context.get('rp_name')}",
        f"target_api_version: {context.get('target_api_version')}",
        f"old_api_version: {context.get('old_api_version') or '(not provided)'}",
    ]

    for entry in context.get("entries", []):
        path = entry.get("path", "")
        old_path = entry.get("old_path", "")
        diff = entry.get("operation_diff") if isinstance(entry.get("operation_diff"), dict) else {}
        added = list(diff.get("added", []))[:max_delta_per_file] if isinstance(diff, dict) else []
        removed = list(diff.get("removed", []))[:max_delta_per_file] if isinstance(diff, dict) else []

        lines.append(f"- file: {path}")
        if old_path:
            lines.append(f"  old_file: {old_path}")
        lines.append(
            f"  operation_count: old={entry.get('old_operation_count', 0)} new={entry.get('operation_count', 0)}"
        )
        if added:
            lines.append(f"  added_ops: {', '.join(added)}")
        if removed:
            lines.append(f"  removed_ops: {', '.join(removed)}")
        if not added and not removed:
            lines.append("  op_delta: none")

    text = "\n".join(lines)
    if len(text) <= char_budget:
        return text
    trimmed = text[:max(0, char_budget - 48)].rstrip()
    return trimmed + "\n...(truncated by char budget)"


# --------------------------------------------------------------------------- #
# TeamCity baseline (deterministic capture, orchestrator-side)
# --------------------------------------------------------------------------- #
#
# Resolve the build-config id, find the latest `main` build, and page through its
# test occurrences to build the {test_name: status} baseline the collect phase
# diffs local runs against.

_TEAMCITY_DEFAULT_URL = "https://hashicorp.teamcity.com"
_TEAMCITY_BUILDTYPE_PREFIX = "TF_AzureRM_AZURERM_SERVICE_{env}_{service}"
_TEAMCITY_PAGE_SIZE = 1000
_TEAMCITY_RETRY_STATUS = {502, 503, 504}
_TEAMCITY_MAX_RETRIES = 3


def _teamcity_token() -> str | None:
    """Return the TeamCity token from either supported env var, or ``None``."""
    return os.environ.get("TEAMCITY_TOKEN") or os.environ.get("TEAMCITY_ACCESSTOKEN")


def _teamcity_base_url() -> str:
    return os.environ.get("TEAMCITY_SERVER_URL", _TEAMCITY_DEFAULT_URL)


def _teamcity_get_json(url: str, token: str) -> Any:
    """GET a URL with bearer auth and return parsed JSON, retrying on 5xx."""
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    last_err: Exception | None = None
    for attempt in range(_TEAMCITY_MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in _TEAMCITY_RETRY_STATUS and attempt < _TEAMCITY_MAX_RETRIES:
                last_err = exc
                time.sleep(2 ** attempt)
                continue
            raise
        except urllib.error.URLError as exc:
            if attempt < _TEAMCITY_MAX_RETRIES:
                last_err = exc
                time.sleep(2 ** attempt)
                continue
            raise
    raise last_err  # pragma: no cover


def service_build_type_id(rp_name: str, teamcity_env: str = "PUBLIC") -> str:
    """Expand an RP short name to its standard public build-config id.

    ``recoveryservices`` -> ``TF_AzureRM_AZURERM_SERVICE_PUBLIC_RECOVERYSERVICES``.
    """
    return _TEAMCITY_BUILDTYPE_PREFIX.format(
        env=teamcity_env.upper(), service=rp_name.upper())


def _teamcity_latest_build(base_url: str, token: str, build_type_id: str,
                           branch: str, status: str | None) -> dict | None:
    """Return the latest build dict (id, number, status, webUrl) or ``None``."""
    locator = [f"buildType:(id:{build_type_id})", f"branch:(name:{branch})", "count:1"]
    if status:
        locator.append(f"status:{status}")
    url = (f"{base_url}/app/rest/builds"
           f"?locator={','.join(locator)}"
           f"&fields=build(id,number,status,branchName,webUrl,finishDate)")
    builds = _teamcity_get_json(url, token).get("build", [])
    return builds[0] if builds else None


def _teamcity_test_results(base_url: str, token: str, build_id: Any) -> dict[str, str]:
    """Return a ``{test_name: status}`` map for a build, paginating as needed."""
    results: dict[str, str] = {}
    start = 0
    while True:
        url = (f"{base_url}/app/rest/testOccurrences"
               f"?fields=testOccurrence(name,status)"
               f"&locator=build:(id:{build_id}),start:{start},count:{_TEAMCITY_PAGE_SIZE}")
        batch = _teamcity_get_json(url, token).get("testOccurrence", [])
        for test in batch:
            results[test["name"]] = test["status"]
        if len(batch) < _TEAMCITY_PAGE_SIZE:
            break
        start += _TEAMCITY_PAGE_SIZE
    return results


def capture_teamcity_baseline(rp_name: str, output: str | Path, *,
                              teamcity_env: str = "PUBLIC",
                              build_type: str | None = None,
                              branch: str = "refs/heads/main",
                              status: str | None = None,
                              base_url: str | None = None) -> dict[str, Any]:
    """Capture the TeamCity ``main`` baseline for an RP and write it to ``output``.

    Returns ``{"mode", "reason", "build", "counts"}`` where ``mode`` is:

    * ``"normal"``   — a build was found; ``output`` holds its {name: status} map.
    * ``"degraded"`` — no build/token matched; an empty baseline (``{}``) was
      written so the collect phase treats every failure as new.

    Never raises for the ordinary "no build" / "no token" cases and never calls
    ``sys.exit`` — the orchestrator decides what to do with a degraded baseline.
    Genuine network/HTTP errors propagate to the caller (which downgrades them).
    """
    output = Path(output)
    build_type_id = build_type or service_build_type_id(rp_name, teamcity_env)
    token = _teamcity_token()
    base_url = base_url or _teamcity_base_url()

    def _degraded(reason: str) -> dict[str, Any]:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({}, indent=2) + "\n", encoding="utf-8")
        return {"mode": "degraded", "reason": reason, "build": None,
                "counts": {"total": 0, "not_passing": 0}}

    if not token:
        return _degraded("no TEAMCITY_TOKEN / TEAMCITY_ACCESSTOKEN in env")

    build = _teamcity_latest_build(base_url, token, build_type_id, branch, status)
    if not build:
        return _degraded(f"no build matched buildType {build_type_id} on {branch}")

    results = _teamcity_test_results(base_url, token, build["id"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    not_passing = sum(1 for s in results.values() if s != "SUCCESS")
    return {"mode": "normal", "reason": "", "build": build,
            "counts": {"total": len(results), "not_passing": not_passing}}


# --------------------------------------------------------------------------- #
# acctest launch (deterministic, orchestrator-side)
# --------------------------------------------------------------------------- #
#
# Check the ARM env, build the `-run`/TESTARGS string with the exact make/sh
# quoting, and start `make acctests` detached. The fragile `$$`/quote rules are
# exactly what an LLM gets wrong and Python gets right, so this stays in Python.

# ARM credentials/locations the suite needs before it creates real resources.
ACCTEST_REQUIRED_ENV = (
    "ARM_CLIENT_ID", "ARM_CLIENT_SECRET", "ARM_SUBSCRIPTION_ID", "ARM_TENANT_ID",
    "ARM_TEST_LOCATION", "ARM_TEST_LOCATION_ALT", "ARM_TEST_LOCATION_ALT2",
)


def missing_acctest_env(env: dict[str, str] | None = None) -> list[str]:
    """Return the required ARM_* vars that are absent/empty (empty list = all present)."""
    src = os.environ if env is None else env
    return [k for k in ACCTEST_REQUIRED_ENV if not src.get(k)]


def make_testargs(test_regex: str, parallel: int) -> str:
    """Build the ``TESTARGS`` value for ``make acctests``, escaped to survive two hops.

    ``make`` expands ``$(TESTARGS)`` into a ``/bin/sh`` (dash) recipe command, so the
    value must carry its own quoting:

    * every literal ``$`` in the RE2 regex is doubled (``$$``) so ``make``'s own
      variable expansion emits a single ``$`` (an un-doubled ``$`` is eaten), and
    * the regex is wrapped in DOUBLE quotes so dash treats ``(`` and ``|`` as literals.

    We pass this as one argv element to ``make`` (no host shell), so no further
    shell-level quoting is needed here.
    """
    safe = test_regex.replace("$", "$$")
    return f'-run="{safe}" -parallel {int(parallel)} -json'


def launch_acctests(repo: str | Path, acctest_dir: str | Path, *, rp_name: str,
                    test_regex: str = "TestAcc", parallel: int = 11,
                    timeout_min: int = 0) -> dict[str, Any]:
    """Start ``make acctests`` DETACHED in ``repo``; write artifacts under ``acctest_dir``.

    Returns ``{"status", "pid", "reason", "run_filter", "run_json", "pid_file"}`` where
    ``status`` is ``"launched"`` (a live PID was recorded) or ``"failed"`` (with a reason).
    The command is passed as an argv list (no host shell parses it), and detached into its
    own session so it outlives the orchestrator; the orchestrator then polls ``run.pid``.
    """
    repo = Path(repo)
    acctest_dir = Path(acctest_dir)
    acctest_dir.mkdir(parents=True, exist_ok=True)
    run_json = acctest_dir / "run.json"
    run_err = acctest_dir / "run.err"
    pid_file = acctest_dir / "run.pid"
    meta_file = acctest_dir / "meta.json"
    testargs = make_testargs(test_regex, parallel)
    argv = ["make", "acctests", f"SERVICE={rp_name}",
            f"TESTARGS={testargs}", f"TESTTIMEOUT={int(timeout_min)}m"]

    def _failed(reason: str) -> dict[str, Any]:
        return {"status": "failed", "pid": None, "reason": reason,
                "run_filter": testargs, "run_json": str(run_json), "pid_file": str(pid_file)}

    try:
        out = run_json.open("w", encoding="utf-8")
        err = run_err.open("w", encoding="utf-8")
    except OSError as exc:
        return _failed(f"cannot open output files: {exc}")

    popen_kwargs: dict[str, Any] = {
        "cwd": str(repo), "stdout": out, "stderr": err, "stdin": subprocess.DEVNULL,
    }
    if os.name == "posix":  # detach into its own session so it survives orchestrator exit
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, **popen_kwargs)  # noqa: S603 — fixed argv, no shell
    except (OSError, ValueError) as exc:
        out.close()
        err.close()
        return _failed(f"failed to start make acctests: {exc}")
    finally:
        out.close()  # child inherited the fds; drop the parent's copies
        err.close()

    pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")
    meta_file.write_text(json.dumps({
        "service": rp_name, "run_filter": testargs, "parallel": int(parallel),
        "started": time.time(),
    }, indent=2) + "\n", encoding="utf-8")
    return {"status": "launched", "pid": proc.pid, "reason": "",
            "run_filter": testargs, "run_json": str(run_json), "pid_file": str(pid_file)}


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #

def _read_text(source: str | Path) -> str:
    """Return the file's text when ``source`` is a ``Path``, else ``source`` itself.

    Production callers always pass a ``Path`` (read from disk); tests pass the raw
    ``go test -json`` / baseline text as a ``str`` (used verbatim).
    """
    if isinstance(source, Path):
        return source.read_text(encoding="utf-8", errors="ignore")
    return source


_VOLATILE = (
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "<guid>"),
    (re.compile(r"\bacctest[A-Za-z0-9]+\b"), "<name>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}T[\d:.]+Z?\b"), "<ts>"),
    (re.compile(r":\d+:\d+:"), ":<pos>:"),
    (re.compile(r"\b\d{3,}\b"), "<n>"),
)


def _normalize_signature(line: str) -> str:
    """Strip volatile tokens so equivalent failures share one signature."""
    for pattern, repl in _VOLATILE:
        line = pattern.sub(repl, line)
    return " ".join(line.split())[:200]


def _run(cmd: list[str], cwd: str | Path, timeout: int) -> subprocess.CompletedProcess:
    """Run a subprocess capturing text output; never raise on non-zero exit."""
    try:
        return subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(cmd, returncode=127, stdout="", stderr=str(exc))


# --------------------------------------------------------------------------- #
# CLI: expose the mechanical acctest steps so the launch AGENT can invoke them
# --------------------------------------------------------------------------- #
#
# The agent decides the judgement bit a deterministic mapping gets wrong (the real
# provider SERVICE dir + regex for a product RP name, e.g. `recoveryservicesbackup`
# lives under `internal/services/recoveryservices`) and shells out for the mechanics:
#
#   python -m upgrader.helpers check-env
#   python -m upgrader.helpers baseline --service <dir> --output <path> [--build-type ID]
#   python -m upgrader.helpers launch --repo <p> --acctest-dir <p> --service <dir> \
#       --test-regex <re> --parallel <n>
#
# Each subcommand prints a single JSON object to stdout; exit code 0 = ok.

def _cli_check_env() -> int:
    missing = missing_acctest_env()
    print(json.dumps({"missing": missing, "ok": not missing}))
    return 0 if not missing else 1


def _cli_baseline(args: Any) -> int:
    """Capture the TeamCity baseline, degrading (empty baseline) on any failure."""
    out = Path(args.output)
    try:
        result = capture_teamcity_baseline(
            args.service, out, teamcity_env=args.teamcity_env,
            build_type=args.build_type)
        print(json.dumps({"mode": result["mode"], "reason": result["reason"],
                          "counts": result["counts"]}))
        return 0
    except Exception as exc:  # noqa: BLE001 — network/HTTP failure -> degrade, don't crash
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("{}\n", encoding="utf-8")
        print(json.dumps({"mode": "degraded", "reason": repr(exc),
                          "counts": {"total": 0, "not_passing": 0}}))
        return 0


def _cli_launch(args: Any) -> int:
    result = launch_acctests(args.repo, args.acctest_dir, rp_name=args.service,
                             test_regex=args.test_regex, parallel=args.parallel,
                             timeout_min=args.timeout_min)
    print(json.dumps(result))
    return 0 if result["status"] == "launched" else 1


def _cli_specs_context(args: Any) -> int:
    """Build deterministic Azure REST API specs context and print/save it as requested."""
    context = collect_azure_rest_api_specs_context(
        rp_name=args.service,
        target_api_version=args.target_api_version,
        old_api_version=args.old_api_version,
        repo=args.repo,
        ref=args.ref,
        max_files=args.max_files,
    )
    text = format_azure_rest_api_specs_context(
        context,
        max_ops_per_file=args.max_ops_per_file,
        max_delta_per_file=args.max_delta_per_file,
    )

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")

    if args.raw_json:
        payload = {
            "mode": context.get("mode"),
            "reason": context.get("reason", ""),
            "entries": len(context.get("entries", [])),
            "output": str(args.output) if args.output else "",
            "text": text,
        }
        print(json.dumps(payload))
    else:
        print(text)

    return 0


def _main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="upgrader.helpers",
                                description="Mechanical acctest steps for the launch agent.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check-env", help="Report missing ARM_* env vars as JSON.")

    b = sub.add_parser("baseline", help="Capture the TeamCity main baseline as {test: status}.")
    b.add_argument("--service", required=True,
                   help="Provider service dir / RP name used to resolve the build-config id.")
    b.add_argument("--output", required=True, help="Path to write baseline.json.")
    b.add_argument("--build-type", default=None,
                   help="Full TeamCity build-config id (overrides the --service-derived default).")
    b.add_argument("--teamcity-env", default="PUBLIC")

    l = sub.add_parser("launch", help="Start `make acctests` detached; record run.pid.")
    l.add_argument("--repo", required=True, help="terraform-provider-azurerm checkout path.")
    l.add_argument("--acctest-dir", required=True, help="Where run.json/run.pid/meta.json go.")
    l.add_argument("--service", required=True,
                   help="Provider service dir for `make acctests SERVICE=<dir>` (NOT the product "
                        "RP name if they differ).")
    l.add_argument("--test-regex", default="TestAcc")
    l.add_argument("--parallel", type=int, default=11)
    l.add_argument("--timeout-min", type=int, default=0)

    s = sub.add_parser(
        "specs-context",
        help="Collect deterministic Azure REST API specs context via gh api.")
    s.add_argument("--service", required=True,
                   help="RP/service short name used for spec path discovery.")
    s.add_argument("--target-api-version", required=True,
                   help="Target API version, e.g. 2024-01-01.")
    s.add_argument("--old-api-version", default=None,
                   help="Old API version for old-vs-target operation diff.")
    s.add_argument("--repo", default=_GITHUB_SPEC_REPO,
                   help="GitHub repo in owner/name form (default Azure specs repo).")
    s.add_argument("--ref", default=_GITHUB_SPEC_REF,
                   help="Git ref to query (default: main).")
    s.add_argument("--max-files", type=int, default=_GITHUB_SPEC_MAX_FILES,
                   help="Max spec files to include (default: 4).")
    s.add_argument("--max-ops-per-file", type=int, default=8,
                   help="Max operation signatures rendered per file.")
    s.add_argument("--max-delta-per-file", type=int, default=6,
                   help="Max added/removed operation signatures rendered per file.")
    s.add_argument("--output", default=None,
                   help="Optional path to write rendered context text.")
    s.add_argument("--raw-json", action="store_true",
                   help="Print metadata JSON with rendered text instead of plain text.")

    args = p.parse_args(argv)
    if args.cmd == "check-env":
        return _cli_check_env()
    if args.cmd == "baseline":
        return _cli_baseline(args)
    if args.cmd == "launch":
        return _cli_launch(args)
    if args.cmd == "specs-context":
        return _cli_specs_context(args)
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
