"""upgrader CLI: upgrade one RP with a Ralph loop."""

from __future__ import annotations

import argparse
from pathlib import Path

from upgrader import __version__, credentials
from upgrader.loop import run


def load_env(explicit: Path | None = None) -> None:
    """Load ``explicit`` if given, else the ``.env`` in the cwd and in this checkout's root.

    Direct runs have no ``docker compose`` to inject ``.env``. Secrets are held in-process
    rather than exported — see :mod:`upgrader.credentials`.
    """
    credentials.load(explicit)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="upgrader", description="Ralph-loop RP API-version upgrade.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("rp_name", help="Service short name (e.g. keyvault)")
    p.add_argument("target_api_version", help="Target API version (e.g. 2023-07-01)")
    p.add_argument("--repo", required=True, type=Path, help="terraform-provider-azurerm path")
    p.add_argument("--old-api-version", default=None,
                   help="Current API version being replaced; auto-detected from the RP's imports "
                        "if omitted.")
    p.add_argument("--model", default=None)
    p.add_argument("--skip-acctest", action="store_true",
                   help="Stop after the build is green; do not run acceptance-test investigation.")
    p.add_argument("--test-regex", default="TestAcc",
                   help="Acctest -run filter (default: TestAcc).")
    p.add_argument("--parallel", type=int, default=11,
                   help="Max acctests to run together via `go test -parallel` (default: 11).")
    p.add_argument("--max-rounds", type=int, default=8,
                   help="Max upgrade rounds; each is a fresh session gated by a real `go build ./...` "
                        "(default: 8).")
    p.add_argument("--prebuild-local-sdk", action="store_true",
                   help="Generate the target SDK from a local Pandora checkout before upgrading.")
    p.add_argument("--pandora-repo", type=Path,
                   help="Local Pandora checkout (required with --prebuild-local-sdk).")
    p.add_argument("--go-azure-sdk-repo", type=Path,
                   help="Local go-azure-sdk checkout (required with --prebuild-local-sdk).")
    p.add_argument("--pandora-service",
                   help="Pandora service name used by SERVICES, e.g. Network "
                        "(required with --prebuild-local-sdk).")
    p.add_argument("--env-file", type=Path, default=None,
                   help="Load credentials from this KEY=VALUE file instead of the default "
                        "./.env (real environment variables always win).")
    p.add_argument("--debug", action="store_true",
                   help="If set, print JSON tool-usage records to stdout for every agent session.")
    args = p.parse_args(argv)

    load_env(args.env_file)

    local_sdk_args = (args.pandora_repo, args.go_azure_sdk_repo, args.pandora_service)
    if args.prebuild_local_sdk and not all(local_sdk_args):
        p.error("--prebuild-local-sdk requires --pandora-repo, --go-azure-sdk-repo, "
                "and --pandora-service")
    if not args.prebuild_local_sdk and any(local_sdk_args):
        p.error("--pandora-repo, --go-azure-sdk-repo, and --pandora-service require "
                "--prebuild-local-sdk")

    ok = run(args.repo.resolve(), args.rp_name, args.target_api_version,
             args.old_api_version, model=args.model,
             run_acctest=not args.skip_acctest, test_regex=args.test_regex,
             parallel=args.parallel, max_rounds=args.max_rounds,
             debug_tools_log=args.debug,
             prebuild_local_sdk=args.prebuild_local_sdk,
             pandora_repo=args.pandora_repo.resolve() if args.pandora_repo else None,
             go_azure_sdk_repo=(args.go_azure_sdk_repo.resolve()
                                if args.go_azure_sdk_repo else None),
             pandora_service=args.pandora_service)
    print("[DEBUG] " + (f"upgrade complete; review `git -C {args.repo} diff`."
                        if ok else "did not converge; see IMPLEMENTATION_PLAN.md"))
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
