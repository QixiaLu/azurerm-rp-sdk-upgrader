"""upgrader CLI: upgrade one RP with a Ralph loop."""

from __future__ import annotations

import argparse
from pathlib import Path

from upgrader import __version__, credentials
from upgrader.loop import run


STAGES = ("prebuild", "upgrade", "acctest", "breaking-change")
DEFAULT_STAGES = ("upgrade", "acctest")


def positive_int(value: str) -> int:
    """Parse a strictly positive integer for bounded-work CLI options."""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def load_env(explicit: Path | None = None) -> None:
    """Load ``explicit`` if given, else the ``.env`` in the cwd and in this checkout's root.

    Direct runs have no ``docker compose`` to inject ``.env``. Secrets are held in-process
    rather than exported — see :mod:`upgrader.credentials`.
    """
    credentials.load(explicit)


def build_parser() -> argparse.ArgumentParser:
    """Build the public command-line interface."""
    parser = argparse.ArgumentParser(
        prog="upgrader",
        description="Upgrade an AzureRM provider service to a target API version.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("rp_name", metavar="SERVICE",
                        help="AzureRM provider service name, for example keyvault")
    parser.add_argument("target_api_version", metavar="TARGET_API_VERSION",
                        help="target API version, for example 2023-07-01")

    workflow = parser.add_argument_group("workflow options")
    workflow.add_argument("--repo", required=True, type=Path, metavar="PATH",
                          help="path to the terraform-provider-azurerm checkout")
    workflow.add_argument("--stage", action="append", choices=STAGES,
                          help="stage to run; repeat for multiple stages "
                               "(default: upgrade, acctest)")
    workflow.add_argument("--old-api-version", metavar="VERSION",
                          help="current API version (auto-detected when omitted)")
    workflow.add_argument("--max-rounds", type=positive_int, default=8, metavar="COUNT",
                          help="maximum upgrade rounds (default: 8)")
    workflow.add_argument("--provider-version", metavar="VERSION",
                          help="released AzureRM version used by breaking-change")

    tests = parser.add_argument_group("acceptance test options")
    tests.add_argument("--test-regex", default="TestAcc", metavar="REGEX",
                       help="value passed to go test -run (default: TestAcc)")
    tests.add_argument("--parallel", type=positive_int, default=11, metavar="COUNT",
                       help="maximum tests run concurrently by go test (default: 11)")

    local_sdk = parser.add_argument_group("local SDK options")
    local_sdk.add_argument("--pandora-repo", type=Path, metavar="PATH",
                           help="local Pandora checkout")
    local_sdk.add_argument("--go-azure-sdk-repo", type=Path, metavar="PATH",
                           help="local go-azure-sdk checkout")
    local_sdk.add_argument("--pandora-service", metavar="SERVICE",
                           help="Pandora service name used by SERVICES, for example Network")

    runtime = parser.add_argument_group("runtime options")
    runtime.add_argument("--model", metavar="MODEL", help="Copilot model to use")
    runtime.add_argument("--env-file", type=Path, metavar="PATH",
                         help="credentials file to load instead of ./.env")
    runtime.add_argument("--debug", action="store_true",
                         help="print JSON tool-usage records for agent sessions")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    stages = frozenset(args.stage or DEFAULT_STAGES)
    local_sdk_args = (args.pandora_repo, args.go_azure_sdk_repo, args.pandora_service)
    if "prebuild" in stages and not all(local_sdk_args):
        parser.error("stage 'prebuild' requires --pandora-repo, --go-azure-sdk-repo, "
                     "and --pandora-service")
    if "prebuild" not in stages and any(local_sdk_args):
        parser.error("--pandora-repo, --go-azure-sdk-repo, and --pandora-service require "
                     "--stage prebuild")
    if "breaking-change" in stages and not args.provider_version:
        parser.error("stage 'breaking-change' requires --provider-version")
    if "breaking-change" not in stages and args.provider_version:
        parser.error("--provider-version requires --stage breaking-change")

    load_env(args.env_file)

    ok = run(args.repo.resolve(), args.rp_name, args.target_api_version,
             args.old_api_version, model=args.model,
             run_acctest="acctest" in stages, test_regex=args.test_regex,
             parallel=args.parallel, max_rounds=args.max_rounds,
             debug_tools_log=args.debug,
             prebuild_local_sdk="prebuild" in stages,
             run_execute="upgrade" in stages,
             breaking_change_provider_version=(args.provider_version
                                                if "breaking-change" in stages else None),
             pandora_repo=args.pandora_repo.resolve() if args.pandora_repo else None,
             go_azure_sdk_repo=(args.go_azure_sdk_repo.resolve()
                                if args.go_azure_sdk_repo else None),
             pandora_service=args.pandora_service)
    print(f"requested pipeline {'complete' if ok else 'failed'}; "
          f"review `git -C {args.repo} diff` and the run artifacts above.")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
