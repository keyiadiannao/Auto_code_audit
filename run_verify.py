#!/usr/bin/env python3
"""Engine-owned deterministic acceptance gate (SKILL.md Phase 3).

The skill text said "check deterministically" after a patch; this script is
that check, owned by the engine instead of the reviewer. Given the verdicts
recorded before the patch and the report generated after it, the gate fails
when:

1. a code-action verdict still has its ``target_id`` in the new report —
   the finding was not removed;
2. a still-present finding's ``finding_evidence_hash`` recomputes from the
   new report's detail — the evidence did not change;
3. (with ``--previous``) the patch scope gained a high/medium candidate that
   was not in the pre-patch report — a new risk appeared where the patch
   touched;
4. a code-action verdict was recorded without a test gate — pass
   ``--test-command`` to run the target project's tests or ``--no-tests`` to
   declare that tests are verified outside this gate;
5. ``--test-command`` exits non-zero.

Verdict vocabulary: the gate speaks the skill-first protocol
(``disposition: true_finding`` + ``recommended_action``) *and* the legacy
adjudicate.py dispositions (``true duplicate`` / ``compatibility debt``,
accepted as the legacy importer).  ``--verdicts`` accepts either an
aggregated ``verdicts.json`` or a directory of per-case protocol verdict
files (SKILL Phase 2).  Stale-evidence binding uses the canonical
``finding_evidence_hash`` (run_all.finding_evidence_hash); per-case protocol
files additionally carry ``case_hash`` (the protocol case digest), which
binds commit and snippets and is *not* used for stale detection.

Exit code 0 means acceptance, 1 means rejection with the failing checks on
stderr, 2 means usage or I/O error.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from run_all import __version__, finding_evidence_hash

#: Legacy adjudicate.py dispositions that require a code change.
LEGACY_CODE_ACTIONS = {"true duplicate", "compatibility debt"}
#: Protocol recommended_action values that require a code change.
CODE_ACTION_ACTIONS = {
    "delete_dead_code",
    "extract_shared_component",
    "reuse_existing",
    "fix_contract_drift",
    "replace_with_library",
    "externalize_config",
}
RISK_PRIORITIES = {"high", "medium"}


def _verdict_hash(verdict: dict) -> str | None:
    """The finding-evidence hash recorded on a verdict entry.

    Prefers the canonical ``finding_evidence_hash``; falls back to the
    legacy ``evidence_hash`` key written by older adjudicate.py versions.
    Per-case protocol files keep their case digest under ``evidence_hash``
    as well, so this fallback is only safe for aggregated entries.
    """
    for key in ("finding_evidence_hash", "evidence_hash"):
        value = verdict.get(key)
        if isinstance(value, str):
            return value
    return None


def _is_code_action(verdict: dict) -> bool:
    """True when the verdict obliges a code change that the gate must see
    removed from the next report."""
    disposition = verdict.get("disposition")
    if disposition in LEGACY_CODE_ACTIONS:
        return True
    if disposition == "true_finding":
        return verdict.get("recommended_action") in CODE_ACTION_ACTIONS
    return False


def _load_verdicts(path: Path) -> tuple[list[dict], list[str]]:
    """Load verdict entries from an aggregated verdicts.json or a directory
    of per-case protocol verdict files (SKILL Phase 2).

    Per-case files are only usable by the gate when they carry
    ``scanner``/``target_id`` (the bridge fields); pre-bridge files are
    skipped with a warning.  Returns ``(entries, warnings)``.
    """
    entries: list[dict] = []
    warnings: list[str] = []
    if path.is_dir():
        for verdict_path in sorted(path.glob("*.json")):
            try:
                payload = json.loads(verdict_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                warnings.append(f"{verdict_path.name}: unreadable ({exc})")
                continue
            case = payload.get("verdict")
            scanner = payload.get("scanner")
            target_id = payload.get("target_id")
            if not isinstance(case, dict):
                warnings.append(f"{verdict_path.name}: not a per-case verdict file")
                continue
            if not (isinstance(scanner, str) and isinstance(target_id, str)):
                warnings.append(
                    f"{verdict_path.name}: pre-bridge verdict file without "
                    "scanner/target_id, skipped"
                )
                continue
            entries.append(
                {
                    "scanner": scanner,
                    "target_id": target_id,
                    "finding_evidence_hash": payload.get("finding_evidence_hash"),
                    "disposition": case.get("disposition"),
                    "recommended_action": case.get("recommended_action"),
                    "case_hash": payload.get("case_hash")
                    or payload.get("evidence_hash"),
                }
            )
        return entries, warnings
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("verdicts file must contain a JSON object")
    verdicts_list = payload.get("verdicts")
    if not isinstance(verdicts_list, list):
        raise ValueError("verdicts file must contain a 'verdicts' list")
    for verdict in verdicts_list:
        if isinstance(verdict, dict):
            entries.append(verdict)
    return entries, warnings


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


def _risk_level(detail: dict) -> str | None:
    """Unified risk severity across scanner detail schemas.

    Falls back ``priority`` (duplicates, regions) → ``severity``
    (hardcoded) → deadcode's ``status`` (a new DEAD module after a patch is
    the drift the gate exists to catch).
    """
    for key in ("priority", "severity"):
        value = detail.get(key)
        if isinstance(value, str) and value.lower() in RISK_PRIORITIES:
            return value.lower()
    if detail.get("status") == "DEAD":
        return "high"
    return None


def _check_report(
    report: dict,
    verdicts: dict,
    previous: dict | None,
    scope: str | None,
    test_gate: str = "not_run",
) -> tuple[list[str], dict[str, Any]]:
    failures: list[str] = []
    stats: dict[str, Any] = {
        "code_action_verdicts": 0,
        "still_present": 0,
        "test_gate": test_gate,
    }
    candidates = _candidates(report)
    previous_candidates: set[tuple[str, str]] = (
        set(_candidates(previous)) if previous is not None else set()
    )

    for verdict in verdicts.get("verdicts", []):
        if not _is_code_action(verdict):
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
        old_hash = _verdict_hash(verdict)
        new_hash = finding_evidence_hash(key[0], key[1], detail)
        if old_hash and old_hash == new_hash:
            failures.append(
                f"evidence unchanged: {key[0]} {key[1]} "
                f"recomputes to the same hash after the patch"
            )

    if test_gate == "failed":
        failures.append("test command failed (non-zero exit)")
    elif test_gate == "not_run" and stats["code_action_verdicts"] > 0:
        failures.append(
            "code-action verdicts present but no test gate: "
            "pass --test-command or --no-tests"
        )

    if previous is not None:
        for key, detail in candidates.items():
            if key in previous_candidates:
                continue
            if not _in_scope(detail, scope):
                continue
            priority = _risk_level(detail)
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
        "--verdicts",
        type=Path,
        required=True,
        help="verdicts.json from adjudicate.py, or a directory of per-case "
        "protocol verdict files (SKILL Phase 2)",
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
    ap.add_argument(
        "--test-command",
        default=None,
        help="shell command that runs the target project's tests; a non-zero "
        "exit rejects the gate",
    )
    ap.add_argument(
        "--no-tests",
        action="store_true",
        help="explicitly skip the test gate (tests are verified outside "
        "run_verify)",
    )
    ap.add_argument("--json", type=Path, default=None, help="write result JSON")
    args = ap.parse_args(argv)

    if args.test_command and args.no_tests:
        print(
            "error: --test-command and --no-tests are mutually exclusive",
            file=sys.stderr,
        )
        return 2

    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        verdicts = _load_verdicts(args.verdicts)
        previous = (
            json.loads(args.previous.read_text(encoding="utf-8"))
            if args.previous is not None
            else None
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: cannot read inputs: {exc}", file=sys.stderr)
        return 2
    verdict_entries, warnings = verdicts
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if args.test_command:
        result = subprocess.run(args.test_command, shell=True)
        test_gate = "passed" if result.returncode == 0 else "failed"
    elif args.no_tests:
        test_gate = "skipped"
    else:
        test_gate = "not_run"

    failures, stats = _check_report(
        report, {"verdicts": verdict_entries}, previous, args.scope, test_gate
    )
    passed = not failures
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "schema_version": 2,
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
        f"test_gate={stats['test_gate']} "
        f"failures={len(failures)}"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
