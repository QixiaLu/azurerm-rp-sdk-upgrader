"""upgrader CLI: upgrade one RP with a Ralph loop."""

from __future__ import annotations

import argparse
from pathlib import Path

from upgrader import __version__
from upgrader.loop import run


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="upgrader", description="Ralph-loop RP API-version upgrade.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("rp_name", help="Service short name (e.g. keyvault)")
    p.add_argument("target_api_version", help="Target API version (e.g. 2023-07-01)")
    p.add_argument("--repo", required=True, type=Path, help="terraform-provider-azurerm path")
    p.add_argument("--old-api-version", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--skip-acctest", action="store_true",
                   help="Stop after the build is green; do not run acceptance-test investigation.")
    p.add_argument("--test-regex", default="TestAcc",
                   help="Acctest -run filter (default: TestAcc).")
    p.add_argument("--parallel", type=int, default=11,
                   help="Max acctests to run together via `go test -parallel` (default: 11).")
    p.add_argument("--with-toolkit", action="store_true",
                   help="Inject the allow-listed ai-assisted-development toolkit content "
                        "(migration instructions + acceptance-testing skill) into the upgrade. "
                        "Off by default.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Write per-turn event logs to disk; otherwise only result.json is kept.")
    args = p.parse_args(argv)

    ok = run(args.repo.resolve(), args.rp_name, args.target_api_version,
             args.old_api_version, model=args.model,
             run_acctest=not args.skip_acctest, test_regex=args.test_regex,
             parallel=args.parallel,
             with_toolkit=args.with_toolkit,
             verbose=args.verbose)
    print("upgrade complete; review the working tree." if ok else "did not converge; see IMPLEMENTATION_PLAN.md")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
