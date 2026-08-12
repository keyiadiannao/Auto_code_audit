#!/usr/bin/env python3
"""Engine-owned deterministic acceptance gate (SKILL.md Phase 3).

The skill text said "check deterministically" after a patch; this script is
that check, owned by the engine instead of the reviewer. Given the verdicts
recorded before the patch and the report generated after it, the gate fails
when:

1. a code-action verdict (``true duplicate`` / ``compatibility debt``) still
   has its ``target_id`` in the new report — the finding was not removed;
2. a still-present finding's ``evidence_hash`` recomputes from the new
   report's detail — the evidence did not change;
3. (with ``--previous``) the patch scope gained a high/medium candidate that
   was not in the pre-patch report — a new risk appeared where the patch
   touched.

Exit code 0 means acceptance, 1 means rejection with the failing checks on
stderr, 2 means usage or I/O error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from run_all import __version__

CODE_ACTIONS = {"true duplicate", "compatibility debt"}
RISK_PRIORITIES = {"high", "medium"}


def _evidence_hash(scanner: str, target_id: str, detail: dict) -> str:
    """Same canonical evidence hash adjudicate.py uses."""
    payload = json.dumps(
        {"scanner": scanner, "target_id": target_id, "detail": detail},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _candidates(report: dict) -> dict[tuple[str, str], dict]:
    from run_all import _candidate_signatures

    out: dict[tuple[str, str], dict] = {}
    for scanner, triples in _candidate_signatures(
        report.get("scanners", {})
    ).items():
        for signature, _, detail in triples:
            out[(scanner, signature)] = detail
    return out


def _candidate_paths(detail: dict) -> set[str]:
    """Path fingerprints of a candidate, for scope filtering."""
    paths: set[str] = set()
    for key in ("path",):
        value = detail.get(key)
        if isinstance(value, str):
            paths.add(value)
    for key in ("left", "right", "local", "global"):
        value = detail.get(key)
        if isinstance(value, dict):
            for nested in ("path", "file", "source"):
                if isinstance(value.get(nested), str):
                    paths.add(value[nested])
    members = detail.get("members")
    if isinstance(members, list):
        for member in members:
            if isinstance(member, dict):
                for key in ("path", "file", "source"):
                    if isinstance(member.get(key), str):
                        paths.add(member[key])
    return paths


def _in_scope(detail: dict, scope: str | None) -> bool:
    if not scope:
        return True
    return any(scope in path for path in _candidate_paths(detail)) or not _candidate_paths(
        detail
    )


def _priority(detail: dict) -> str | None:
    value = detail.get("priority")
    return value if isinstance(value, str) else None


def _check_report(
    report: dict,
    verdicts: dict,
    previous: dict | None,
    scope: str | None,
) -> tuple[list[str], dict[str, Any]]:
    failures: list[str] = []
    stats: dict[str, Any] = {"code_action_verdicts": 0, "still_present": 0}
    candidates = _candidates(report)
    previous_candidates: set[tuple[str, str]] = (
        set(_candidates(previous)) if previous is not None else set()
    )

    for verdict in verdicts.get("verdicts", []):
        if verdict.get("disposition") not in CODE_ACTIONS:
            continue
        stats["code_action_verdicts"] += 1
        key = (verdict.get("scanner", ""), verdict.get("target_id", ""))
        if key not in candidates:
            continue
        stats["still_present"] += 1
        detail = candidates[key]
        failures.append(
            f"finding still present: {key[0]} {key[1]} "
            f"(verdict {verdict.get('disposition')})"
        )
        old_hash = verdict.get("evidence_hash")
        new_hash = _evidence_hash(key[0], key[1], detail)
        if old_hash and old_hash == new_hash:
            failures.append(
                f"evidence unchanged: {key[0]} {key[1]} "
                f"recomputes to the same hash after the patch"
            )

    if previous is not None:
        for key, detail in candidates.items():
            if key in previous_candidates:
                continue
            if not _in_scope(detail, scope):
                continue
            priority = _priority(detail)
            if priority in RISK_PRIORITIES:
                failures.append(
                    f"new {priority} candidate after patch: "
                    f"{key[0]} {key[1]} (not in previous report)"
                )
    return failures, stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--report", type=Path, required=True, help="post-fix report.json")
    ap.add_argument(
        "--verdicts", type=Path, required=True, help="verdicts.json from adjudicate.py"
    )
    ap.add_argument(
        "--previous",
        type=Path,
        default=None,
        help="pre-fix report.json; enables the new-candidate check",
    )
    ap.add_argument(
        "--scope",
        default=None,
        help="path substring the patch touched; new high/medium candidates "
        "outside it are not rejected",
    )
    ap.add_argument("--json", type=Path, default=None, help="write result JSON")
    args = ap.parse_args(argv)

    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        verdicts = json.loads(args.verdicts.read_text(encoding="utf-8"))
        previous = (
            json.loads(args.previous.read_text(encoding="utf-8"))
            if args.previous is not None
            else None
        )
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read inputs: {exc}", file=sys.stderr)
        return 2

    failures, stats = _check_report(report, verdicts, previous, args.scope)
    passed = not failures
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "passed": passed,
                    "failures": failures,
                    "stats": stats,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    for failure in failures:
        print(f"VERIFY FAIL {failure}", file=sys.stderr)
    print(
        f"VERIFY {'PASS' if passed else 'REJECT'} "
        f"code_action_verdicts={stats['code_action_verdicts']} "
        f"still_present={stats['still_present']} "
        f"failures={len(failures)}"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
