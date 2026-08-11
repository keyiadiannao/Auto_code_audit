#!/usr/bin/env python3
"""Smoke-test every scanner CLI: ``--help`` must exit 0 on all entrypoints.

Catches argparse regressions (renamed options, removed flags) before a real
scan run fails midway.  ``run_all --cli-smoke`` runs this first and aborts
the audit when any entrypoint misbehaves.

Exit code 0: every entrypoint accepted ``--help`` and exited 0.
Exit code 2: one or more entrypoints failed; details are printed to stderr.
"""
from __future__ import annotations

import argparse
import importlib
import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SCANNERS = (
    "scan_capabilities",
    "scan_contracts",
    "scan_deadcode",
    "scan_duplicates",
    "scan_forks",
    "scan_hardcoded",
    "scan_style",
)


def smoke(extra_modules: tuple[str, ...] = ()) -> list[tuple[str, int]]:
    """Run ``--help`` on each module; return [(module, rc)] for failures."""
    failures: list[tuple[str, int]] = []
    for name in SCANNERS + extra_modules:
        module = importlib.import_module(name)
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                rc = module.main(["--help"])
        except SystemExit as exc:
            rc = exc.code
        if rc != 0:
            failures.append((name, rc))
    return failures


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--scanner",
        action="append",
        default=[],
        help="extra module name to smoke-test (repeatable)",
    )
    args = ap.parse_args(argv)

    failures = smoke(tuple(args.scanner))
    if failures:
        for name, rc in failures:
            print(f"CLI_SMOKE FAIL {name} --help exited {rc}", file=sys.stderr)
        print(
            f"CLI_SMOKE {len(failures)}/{len(SCANNERS) + len(args.scanner)} "
            "entrypoints failed",
            file=sys.stderr,
        )
        return 2
    print(
        f"CLI_SMOKE ok entrypoints={len(SCANNERS) + len(args.scanner)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
