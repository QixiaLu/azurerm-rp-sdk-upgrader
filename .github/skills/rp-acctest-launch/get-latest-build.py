#!/usr/bin/env python3
"""Capture the TeamCity `main` baseline for an RP acceptance-test build config.

Self-contained and **stdlib-only** (no pip install needed) so it runs in the
Go-based terraform-provider-azurerm checkout where this skill executes. It
mirrors the REST patterns used by tf-test-monitoring's ``teamcity.py``:

  1. find the latest build for a build config on a branch, then
  2. page through its test occurrences into a {test_name: status} map.

The output JSON is the ``baseline.json`` the triage skill diffs local runs
against (a NEW failure = local FAIL that is PASS/absent in this baseline).

Examples
--------
# Service short name -> TF_AzureRM_AZURERM_SERVICE_PUBLIC_RECOVERYSERVICES
python get-latest-build.py --service recoveryservices --output baseline.json

# Latest SUCCESS build on main, explicit build-config id
python get-latest-build.py \
    --build-type TF_AzureRM_AZURERM_SERVICE_PUBLIC_RECOVERYSERVICES \
    --status SUCCESS --output baseline.json

Auth
----
Set a TeamCity access token in either env var (both are accepted):
    TEAMCITY_TOKEN         (used by this skill)
    TEAMCITY_ACCESSTOKEN   (used by tf-test-monitoring)
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE_URL = os.environ.get(
    "TEAMCITY_SERVER_URL", "https://hashicorp.teamcity.com"
)
SERVICE_BUILDTYPE_PREFIX = "TF_AzureRM_AZURERM_SERVICE_{env}_{service}"
_PAGE_SIZE = 1000
_RETRY_STATUS = {502, 503, 504}
_MAX_RETRIES = 3


def _token():
    """Return the TeamCity token from either supported env var."""
    token = os.environ.get("TEAMCITY_TOKEN") or os.environ.get("TEAMCITY_ACCESSTOKEN")
    if not token:
        sys.exit(
            "[Error] Set TEAMCITY_TOKEN (or TEAMCITY_ACCESSTOKEN) to a TeamCity "
            "access token before running."
        )
    return token


def _get_json(url, token):
    """GET a URL with Bearer auth and return parsed JSON, retrying on 5xx."""
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    last_err = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in _RETRY_STATUS and attempt < _MAX_RETRIES:
                last_err = e
                time.sleep(2 ** attempt)
                continue
            raise
        except urllib.error.URLError as e:
            if attempt < _MAX_RETRIES:
                last_err = e
                time.sleep(2 ** attempt)
                continue
            raise
    raise last_err  # pragma: no cover


def _build_type_id(args):
    """Resolve the build config id from --build-type or --service."""
    if args.build_type:
        return args.build_type
    return SERVICE_BUILDTYPE_PREFIX.format(
        env=args.teamcity_env.upper(), service=args.service.upper()
    )


def get_latest_build(base_url, token, build_type_id, branch, status):
    """Return the latest build dict (id, number, status, webUrl) or None.

    Locator pattern (matches the triage skill):
        buildType:(id:<id>),branch:(name:refs/heads/main),status:SUCCESS,count:1
    """
    locator = [
        f"buildType:(id:{build_type_id})",
        f"branch:(name:{branch})",
        "count:1",
    ]
    if status:
        locator.append(f"status:{status}")
    url = (
        f"{base_url}/app/rest/builds"
        f"?locator={','.join(locator)}"
        f"&fields=build(id,number,status,branchName,webUrl,finishDate)"
    )
    builds = _get_json(url, token).get("build", [])
    return builds[0] if builds else None


def get_test_results(base_url, token, build_id):
    """Return a {test_name: status} map for a build, paginating as needed."""
    results = {}
    start = 0
    while True:
        url = (
            f"{base_url}/app/rest/testOccurrences"
            f"?fields=testOccurrence(name,status)"
            f"&locator=build:(id:{build_id}),start:{start},count:{_PAGE_SIZE}"
        )
        batch = _get_json(url, token).get("testOccurrence", [])
        for test in batch:
            results[test["name"]] = test["status"]
        if len(batch) < _PAGE_SIZE:
            break
        start += _PAGE_SIZE
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Fetch the latest main-branch build result from TeamCity."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--build-type",
        help="Full build config id, e.g. TF_AzureRM_AZURERM_SERVICE_PUBLIC_RECOVERYSERVICES",
    )
    target.add_argument(
        "--service",
        help="Service short name, e.g. recoveryservices (expands to the public build config id)",
    )
    parser.add_argument(
        "--teamcity-env",
        default="PUBLIC",
        help="Build-config env segment used with --service (default: PUBLIC)",
    )
    parser.add_argument(
        "--branch",
        default="refs/heads/main",
        help="Branch name locator (default: refs/heads/main)",
    )
    parser.add_argument(
        "--status",
        default=None,
        choices=["SUCCESS", "FAILURE", "ERROR"],
        help="Only consider builds with this status (default: latest of any status)",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"TeamCity server URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--output",
        help="Write the {test_name: status} baseline map to this JSON file",
    )
    parser.add_argument(
        "--failures-only",
        action="store_true",
        help="With --output, only persist tests whose status is not SUCCESS",
    )
    args = parser.parse_args()

    build_type_id = _build_type_id(args)
    token = _token()

    print(f"[Info] Build config: {build_type_id}")
    print(f"[Info] Branch:       {args.branch}")
    print(f"[Info] Status filter: {args.status or 'any'}")

    build = get_latest_build(
        args.base_url, token, build_type_id, args.branch, args.status
    )
    if not build:
        # Degraded mode: the triage skill treats this as an empty baseline.
        print("[Warning] No matching build found. Writing empty baseline if --output set.")
        if args.output:
            with open(args.output, "w") as f:
                json.dump({}, f, indent=2)
        sys.exit(2)

    build_id = build["id"]
    print(
        f"[Info] Latest build #{build.get('number')} (id={build_id}) "
        f"status={build.get('status')} -> {build.get('webUrl')}"
    )

    results = get_test_results(args.base_url, token, build_id)
    total = len(results)
    failed = sum(1 for s in results.values() if s != "SUCCESS")
    print(f"[Info] Test occurrences: {total} total, {failed} not-passing")

    if args.output:
        out = results
        if args.failures_only:
            out = {n: s for n, s in results.items() if s != "SUCCESS"}
        with open(args.output, "w") as f:
            json.dump(out, f, indent=2, sort_keys=True)
        print(f"[Info] Wrote {len(out)} entries to {args.output}")
    else:
        # No output file: print the not-passing tests to stdout.
        for name, status in sorted(results.items()):
            if status != "SUCCESS":
                print(f"  {status:8} {name}")


if __name__ == "__main__":
    main()
