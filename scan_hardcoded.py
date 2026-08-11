#!/usr/bin/env python3
"""Find known inline patterns that may drift from shared implementations.

Hits are review candidates. In particular, identical syntax does not imply an
identical hashing contract, intervention boundary, or readout semantics.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

import _audit_config

# Pattern list: tune per project. `exclude_paths` ships empty — canonical
# implementations that legitimately own the pattern belong there (or in
# ignore.json under `hardcoded`).
PATTERNS = [
    {
        "name": "readout_pool_then_ln_inline",
        "regex": r"norm_f\(\s*[A-Za-z_][A-Za-z0-9_]*\.mean\(dim=1\)\s*\)",
        "suggestion": (
            "Possible hard-coded pool-then-LN readout. Check whether this is a "
            "deliberate intervention boundary; otherwise use the shared readout "
            "implementation."
        ),
        "severity": "high",
        "exclude_paths": set(),
    },
    {
        "name": "manual_sha256",
        "regex": r"hashlib\.sha256\(",
        "suggestion": (
            "Manual SHA-256 call. Compare the serialization contract before reuse; "
            "file, JSON, NumPy-array, and protocol fingerprints are not interchangeable."
        ),
        "severity": "medium",
        "exclude_paths": set(),
    },
    {
        "name": "mean_pool_readout_tail",
        "regex": r"\.mean\(dim=1\)",
        "suggestion": (
            "Two-token mean pooling. Determine whether this is an intermediate "
            "statistic or a final readout that should use the shared readout "
            "implementation."
        ),
        "severity": "low",
        "exclude_paths": set(),
    },
]

DEFAULT_EXCLUDE_PARTS = {"frozen_source", "tests", "self-audit", "__pycache__"}


def _load_ignore(path: Path | None) -> dict:
    if path and path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def _candidate_id(pattern: str, path: str, line: int, code: str) -> str:
    raw = f"{pattern}\n{path}\n{line}\n{code}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def _write_json(path: Path | None, payload: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repo root (default: script's repo)",
    )
    ap.add_argument("--package", default="src")
    ap.add_argument("--subdirs", nargs="*", default=None)
    ap.add_argument("--pattern", default=None)
    ap.add_argument("--all-patterns", action="store_true")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--ignore", type=Path, default=None)
    args = ap.parse_args(argv)

    repo = args.root.resolve()
    pkg = (repo / args.package).resolve()
    if not pkg.is_dir() or pkg != repo / args.package:
        print(f"error: package dir not found or escapes root: {pkg}", file=sys.stderr)
        return 2

    cfg = _audit_config.load_config(repo)
    hard_cfg = cfg.get("hardcoded", {})
    subdirs = _audit_config.pick(
        args.subdirs,
        cfg,
        "subdirs",
        ["lib", "experiments", "mechanism", "audit", "verify", "figures"],
    )
    exclude_parts = set(
        _audit_config.as_string_list(
            hard_cfg.get("exclude_parts"), sorted(DEFAULT_EXCLUDE_PARTS)
        )
    )

    patterns = PATTERNS
    config_patterns = hard_cfg.get("patterns")
    if isinstance(config_patterns, list):
        patterns = config_patterns  # config replaces module defaults wholesale
        for item in patterns:
            if isinstance(item.get("exclude_paths"), list):
                item["exclude_paths"] = set(item["exclude_paths"])
    if args.pattern:
        patterns = [item for item in patterns if item["name"] == args.pattern]
        if not patterns:
            print(f"error: unknown pattern {args.pattern!r}", file=sys.stderr)
            return 2
    if not args.all_patterns:
        patterns = [item for item in patterns if item["severity"] != "low"]
    compiled = {item["name"]: re.compile(item["regex"]) for item in patterns}

    ignore = _load_ignore(args.ignore)
    ignored_pairs = {
        (entry.get("path", ""), entry.get("pattern", ""))
        for entry in ignore.get("hardcoded", [])
    }
    ignored_ids = {
        entry.get("id", "") for entry in ignore.get("hardcoded", []) if entry.get("id")
    }

    files: set[Path] = set()
    for subdir_name in subdirs:
        subdir = pkg / subdir_name
        if not subdir.is_dir():
            continue
        for path in subdir.rglob("*.py"):
            if any(part in exclude_parts for part in path.relative_to(pkg).parts):
                continue
            files.add(path)

    hits: dict[str, list[dict]] = {item["name"]: [] for item in patterns}
    ignored_hits = []
    for path in sorted(files):
        rel = path.relative_to(pkg).as_posix()
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        lines = text.splitlines()
        for pattern in patterns:
            if rel in pattern.get("exclude_paths", set()):
                continue
            for match in compiled[pattern["name"]].finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                code = lines[line - 1].strip()[:160] if lines else ""
                candidate_id = _candidate_id(pattern["name"], rel, line, code)
                hit = {
                    "id": candidate_id,
                    "path": rel,
                    "line": line,
                    "code": code,
                    "severity": pattern["severity"],
                    "suggestion": pattern["suggestion"],
                }
                if (
                    candidate_id in ignored_ids
                    or (rel, pattern["name"]) in ignored_pairs
                ):
                    ignored_hits.append(hit)
                else:
                    hits[pattern["name"]].append(hit)

    payload = {
        "scanner": "hardcoded",
        "schema_version": 1,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "package": args.package,
        "files_scanned": len(files),
        "patterns_run": [item["name"] for item in patterns],
        "hits": {name: values for name, values in hits.items() if values},
        "ignored": ignored_hits,
    }
    _write_json(args.json, payload)

    hit_count = sum(len(values) for values in hits.values())
    print(
        f"HARDCODED_SCAN package={args.package} files={len(files)} "
        f"hits={hit_count} ignored={len(ignored_hits)}"
    )
    for pattern in patterns:
        values = hits[pattern["name"]]
        print(f"  [{pattern['severity']}] {pattern['name']}: {len(values)} hits")
        for hit in values[:12]:
            print(f"      {hit['path']}:{hit['line']} {hit['code']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
