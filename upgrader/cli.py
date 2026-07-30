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
    p.add_argument("--debug", action="store_true",
                   help="If set, print JSON tool-usage records to stdout for every agent session.")
    args = p.parse_args(argv)

    ok = run(args.repo.resolve(), args.rp_name, args.target_api_version,
             args.old_api_version, model=args.model,
             run_acctest=not args.skip_acctest, test_regex=args.test_regex,
             parallel=args.parallel, max_rounds=args.max_rounds,
             debug_tools_log=args.debug)
    print("[DEBUG] " + ("upgrade complete; review the branch." if ok else "did not converge; see IMPLEMENTATION_PLAN.md"))
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
