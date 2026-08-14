#!/usr/bin/env python3
"""Smoke-test every scanner CLI: ``--help`` must exit 0 on all entrypoints.

Catches argparse regressions (renamed options, removed flags) before a real
scan run fails midway.  ``run_all --cli-smoke`` runs this first and aborts
the audit when any entrypoint misbehaves.

Modules that expose ``--version`` (``run_all``, ``adjudicate``) are also
smoke-tested with that flag so a missing ``__version__`` or a broken
``action="version"`` is caught here rather than at install-verify time.

Exit code 0: every entrypoint accepted ``--help`` and ``--version`` (where
applicable) and exited 0.
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
    "scan_regions",
    "scan_style",
)

#: Modules that expose ``--version`` via ``argparse action="version"``.
VERSION_MODULES = (
    "run_all",
    "adjudicate",
)


def _flag_smoke(modules: tuple[str, ...], flag: str) -> list[tuple[str, int]]:
    """Run *flag* on each module; return [(module, rc)] for failures."""
    failures: list[tuple[str, int]] = []
    for name in modules:
        module = importlib.import_module(name)
        try:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                rc = module.main([flag])
        except SystemExit as exc:
            rc = exc.code
        if rc != 0:
            failures.append((name, rc))
    return failures


def smoke(extra_modules: tuple[str, ...] = ()) -> list[tuple[str, int]]:
    """Run ``--help`` on each scanner; return [(module, rc)] for failures."""
    return _flag_smoke(SCANNERS + extra_modules, "--help")


def version_smoke(
    modules: tuple[str, ...] = VERSION_MODULES,
) -> list[tuple[str, int]]:
    """Run ``--version`` on each version-capable module; return failures."""
    return _flag_smoke(modules, "--version")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--scanner",
        action="append",
        default=[],
        help="extra module name to smoke-test (repeatable)",
    )
    args = ap.parse_args(argv)

    help_failures = smoke(tuple(args.scanner))
    version_failures = version_smoke()
    total_modules = len(SCANNERS) + len(args.scanner) + len(VERSION_MODULES)

    if help_failures or version_failures:
        for name, rc in help_failures:
            print(f"CLI_SMOKE FAIL {name} --help exited {rc}", file=sys.stderr)
        for name, rc in version_failures:
            print(f"CLI_SMOKE FAIL {name} --version exited {rc}", file=sys.stderr)
        failed = len(help_failures) + len(version_failures)
        print(
            f"CLI_SMOKE {failed}/{total_modules} "
            "entrypoints failed",
            file=sys.stderr,
        )
        return 2
    print(
        f"CLI_SMOKE ok entrypoints={total_modules}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
