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
4. a code-action verdict was recorded without test evidence — pass
   ``--test-command`` to run the target project's tests inside the gate,
   ``--test-result`` to consume a machine-readable external test artifact
   (e.g. a CI result file), or ``--no-tests`` to declare that behavioral
   verification is delegated outside this gate (never machine-checked);
5. ``--test-command`` exits non-zero or ``--test-result`` reports a failure;
6. ``--test-result`` is unreadable, not a valid artifact, or its ``git_head``
   does not bind to the post-fix report's git head (provenance.git.head) or
   its ``source_tree_sha256`` to the report's audited source-tree fingerprint
   — the tests must have run against the exact code state the report
   describes, not merely the same commit;
7. a verdict artifact is protocol-invalid — a file the Layer 2 validator
   would reject cannot be skipped out of the gate's input, it rejects the
   gate (fail closed, not fail open);
8. ``--previous`` was given but the pre-fix report is not comparable to the
   post-fix report (schema / package / profile / scanner-set /
   audit-config-hash / scanner-bundle-hash mismatch) — the new-risk check
   would be meaningless;
9. the live source tree under ``--root`` no longer hashes to the report's
   ``provenance.source_tree_sha256`` — the code changed after the audit, so
   neither the test evidence nor the new-risk check applies to the current
   state; re-run the audit before verifying.

Test-gate vocabulary in the stats: ``passed`` (internal run), ``failed``
(internal run rejected), ``external_passed`` / ``external_failed`` /
``external_invalid`` (artifact consumed), ``external_unverified``
(``--no-tests``: delegated, not machine-checked), ``skipped`` (declared via
``--no-tests`` in older callers), ``not_run``.  ``fully_verified`` is true
only when the gate passes, the test evidence is machine-checked (``passed``
or ``external_passed``), **and** a comparable pre-patch report was provided
(``--previous``) so the gate can prove the patch introduced no new blocking
candidate; a ``--no-tests`` acceptance is never ``fully_verified``, and
neither is an acceptance without a comparable baseline.

Verdict vocabulary: the gate speaks the skill-first protocol
(``disposition: true_finding`` + ``recommended_action``) *and* the legacy
adjudicate.py dispositions (``true duplicate`` / ``compatibility debt``,
accepted as the legacy importer).  ``--verdicts`` accepts either an
aggregated ``verdicts.json`` or a directory of per-case protocol verdict
files (SKILL Phase 2); per-case files run the shared full protocol
validator (``_verdict_files.load_protocol_verdict``) — a verdict the Layer 2
validator would reject *rejects this gate* (see check 7).  Stale-evidence
binding uses the canonical ``finding_evidence_hash``
(run_all.finding_evidence_hash); per-case protocol files additionally carry
``case_hash`` (the protocol case digest), which binds commit and snippets
and is *not* used for stale detection.

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
from _scanner_common import atomic_write_text

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

#: Accepted ``status`` values in a ``--test-result`` external artifact.
ARTIFACT_STATUSES = {"passed", "failed"}


def _external_test_gate(
    path: Path, report_head: str | None, report_tree: str | None
) -> tuple[str, str]:
    """Read a machine-readable external test artifact and map it to a
    test-gate value.

    The artifact is a JSON object with strong provenance requirements, so a
    hand-written ``{"status": "passed"}`` cannot be full verification
    evidence.  Required fields: ``status`` (``passed`` or ``failed``),
    ``exit_code`` (int, consistent with ``status``), ``git_head`` (40-char
    hex commit), ``source_tree_sha256`` (64-char hex content fingerprint of
    the tree the tests ran against), and a runner identity via ``tool`` or
    ``command`` (non-empty string).  The artifact's ``git_head`` must equal
    the post-fix report's ``provenance.git.head`` and its
    ``source_tree_sha256`` the report's ``provenance.source_tree_sha256`` —
    the test evidence must come from the same commit *and the same code
    state* the report was scanned at.  Returns ``(gate, reason)`` where
    ``gate`` is ``external_passed``, ``external_failed``, or
    ``external_invalid``; ``reason`` is a short diagnostic for the invalid
    case.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return "external_invalid", f"artifact unreadable: {exc}"
    if not isinstance(payload, dict):
        return "external_invalid", "artifact must be a JSON object"
    status = payload.get("status")
    if status not in ARTIFACT_STATUSES:
        return (
            "external_invalid",
            f"artifact status must be one of {sorted(ARTIFACT_STATUSES)}: "
            f"{status!r}",
        )
    exit_code = payload.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        return "external_invalid", "artifact exit_code must be an integer"
    if (status == "passed") != (exit_code == 0):
        return (
            "external_invalid",
            f"artifact status {status!r} contradicts exit_code {exit_code}",
        )
    git_head = payload.get("git_head")
    if not isinstance(git_head, str) or len(git_head) != 40 or any(
        char not in "0123456789abcdef" for char in git_head
    ):
        return "external_invalid", "artifact git_head must be a 40-char hex commit"
    if report_head is None:
        return (
            "external_invalid",
            "artifact git_head cannot be bound: the report carries no git "
            "provenance (not a git checkout)",
        )
    if git_head != report_head:
        return (
            "external_invalid",
            f"artifact git_head {git_head[:12]} != report git head "
            f"{report_head[:12]}",
        )
    tree_hash = payload.get("source_tree_sha256")
    if not isinstance(tree_hash, str) or len(tree_hash) != 64 or any(
        char not in "0123456789abcdef" for char in tree_hash
    ):
        return (
            "external_invalid",
            "artifact source_tree_sha256 must be a 64-char hex digest",
        )
    if report_tree is None:
        return (
            "external_invalid",
            "artifact source_tree_sha256 cannot be bound: the report carries "
            "no source tree hash (stale report; re-run the audit)",
        )
    if tree_hash != report_tree:
        return (
            "external_invalid",
            f"artifact source_tree_sha256 {tree_hash[:12]} != report "
            f"source tree hash {report_tree[:12]}: the tests did not run "
            "against the audited code state",
        )
    if not any(
        isinstance(payload.get(key), str) and payload[key]
        for key in ("tool", "command")
    ):
        return (
            "external_invalid",
            "artifact must name the runner via 'tool' or 'command'",
        )
    return f"external_{status}", ""


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

    Per-case files run the full shared protocol pipeline —
    ``_verdict_files.load_protocol_verdict``: schema-version check, the
    filename -> evidence_hash -> case_hash binding, the bridge fields, and
    ``validate_verdict`` with its cross-field rules.  A file that fails any
    step is *reported*, not skipped: the acceptance gate fails closed, so a
    verdict artifact the Layer 2 validator would reject can never silently
    disappear from the gate's input.  Returns ``(entries, invalid)`` where
    ``invalid`` lists the per-file diagnostics that must reject the gate.
    """
    entries: list[dict] = []
    invalid: list[str] = []
    if path.is_dir():
        from _verdict_files import load_protocol_verdict

        for verdict_path in sorted(path.glob("*.json")):
            try:
                entries.append(load_protocol_verdict(verdict_path))
            except (OSError, ValueError) as exc:
                invalid.append(f"{verdict_path.name}: {exc}")
                continue
        return entries, invalid
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("verdicts file must contain a JSON object")
    verdicts_list = payload.get("verdicts")
    if not isinstance(verdicts_list, list):
        raise ValueError("verdicts file must contain a 'verdicts' list")
    for index, verdict in enumerate(verdicts_list):
        if isinstance(verdict, dict):
            entries.append(verdict)
        else:
            invalid.append(f"verdict entry #{index} is not a JSON object")
    return entries, invalid


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
    scope_parts = tuple(p for p in scope.replace("\\", "/").split("/") if p)
    paths = _candidate_paths(detail)
    if not paths:
        return True  # path-less candidates cannot be scoped out
    for path in paths:
        parts = tuple(p for p in path.replace("\\", "/").split("/") if p)
        if parts[: len(scope_parts)] == scope_parts:
            return True
    return False


def _risk_level(scanner: str, detail: dict) -> str | None:
    """Unified risk severity for the new-risk gate.

    Delegates to ``run_all.finding_severity`` — one fallback chain across
    scanner schemas (``priority`` → ``severity`` → deadcode ``status`` →
    contracts ``_channel``) — so the gate and every other consumer agree on
    what a new high/medium candidate is.
    """
    from run_all import finding_severity

    return finding_severity(scanner, detail)


def _check_report(
    report: dict,
    verdicts: dict,
    previous: dict | None,
    scope: str | None,
    test_gate: str = "not_run",
    previous_comparable: bool | None = None,
    verdict_invalid: list[str] | None = None,
    mismatches: list[str] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Evaluate the gate; ``previous_comparable`` is the run_all comparator's
    verdict (None when no ``--previous`` was given; an incompatible baseline
    rejects the gate because its new-risk check would be meaningless),
    ``verdict_invalid`` are protocol-invalid verdict artifacts, which reject
    the gate fail-closed instead of being dropped from the input, and
    ``mismatches`` are the messages produced when the live source tree or
    audit inputs no longer match the report's recorded fingerprints."""
    failures: list[str] = []
    stats: dict[str, Any] = {
        "code_action_verdicts": 0,
        "still_present": 0,
        "test_gate": test_gate,
        "fully_verified": False,
    }
    for message in mismatches or []:
        failures.append(message)
    for message in verdict_invalid or []:
        failures.append(f"invalid verdict artifact: {message}")
    if previous is not None and previous_comparable is not True:
        failures.append(
            "previous report is not comparable to the post-fix report; "
            "the new-risk check cannot be trusted"
        )
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
    elif test_gate == "external_failed":
        failures.append("external test artifact reports status 'failed'")
    elif test_gate == "external_invalid":
        failures.append(
            "external test artifact is invalid or unreadable; "
            "pass a valid --test-result or use --test-command/--no-tests"
        )
    elif test_gate == "not_run" and stats["code_action_verdicts"] > 0:
        failures.append(
            "code-action verdicts present but no test evidence: "
            "pass --test-command, --test-result, or --no-tests"
        )

    if previous is not None and previous_comparable is not False:
        for key, detail in candidates.items():
            if key in previous_candidates:
                continue
            if not _in_scope(detail, scope):
                continue
            priority = _risk_level(key[0], detail)
            if priority in RISK_PRIORITIES:
                failures.append(
                    f"new {priority} candidate after patch: "
                    f"{key[0]} {key[1]} (not in previous report)"
                )
    stats["fully_verified"] = (
        not failures
        and test_gate in ("passed", "external_passed")
        and previous_comparable is True
    )
    return failures, stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument(
        "--root",
        type=Path,
        default=Path("."),
        help="repo root the report was generated from (default: current "
        "directory); the live source tree is fingerprinted here and must "
        "match the report's provenance.source_tree_sha256",
    )
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
        help="package-relative path prefix the patch touched; new high/medium "
        "candidates outside it are not rejected (matched by path prefix, "
        "not substring)",
    )
    ap.add_argument(
        "--test-command",
        default=None,
        help="shell command that runs the target project's tests inside the "
        "gate; a non-zero exit rejects the gate",
    )
    ap.add_argument(
        "--test-result",
        type=Path,
        default=None,
        help="machine-readable external test artifact (JSON with required "
        "'status', 'exit_code', 'git_head', 'source_tree_sha256', and "
        "'tool'/'command'; git_head and source_tree_sha256 must equal the "
        "report's provenance values); machine-checked, unlike --no-tests",
    )
    ap.add_argument(
        "--no-tests",
        action="store_true",
        help="declare that behavioral verification is delegated outside this "
        "gate (test_gate 'external_unverified'; never fully_verified)",
    )
    ap.add_argument(
        "--allow-incomplete-analysis",
        action="store_true",
        help="permit the gate to pass even when the report marks its analysis "
        "incomplete (scanner parse failures); off by default — an audit that "
        "could not parse some files must not silently PASS",
    )
    ap.add_argument("--json", type=Path, default=None, help="write result JSON")
    args = ap.parse_args(argv)

    test_flags = [args.test_command, args.test_result, args.no_tests]
    if sum(flag is not None and flag is not False for flag in test_flags) > 1:
        print(
            "error: --test-command, --test-result, and --no-tests are "
            "mutually exclusive",
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
    analysis = report.get("analysis") or {}
    if not analysis.get("complete", True) and not args.allow_incomplete_analysis:
        print(
            "error: report analysis is incomplete (scanner parse failures); "
            "the audit could not analyze every file, so a PASS would be "
            "unverifiable. Fix the parse failures and re-run, or pass "
            "--allow-incomplete-analysis to accept the gap explicitly.",
            file=sys.stderr,
        )
        return 2
    verdict_entries, invalid_verdicts = verdicts
    for message in invalid_verdicts:
        print(f"warning: invalid verdict artifact: {message}", file=sys.stderr)

    previous_comparable: bool | None = None
    if previous is not None:
        from run_all import (
            DEFAULT_PROFILE,
            _diff_previous,
            audit_config_hash,
        )

        comparison = _diff_previous(
            previous,
            report.get("scanners", {}),
            report.get("package"),
            (report.get("configuration") or {}).get("profile", DEFAULT_PROFILE),
            audit_config_hash(report.get("configuration") or {}),
            ((report.get("provenance") or {}).get("scanner_bundle_hash")),
        )
        previous_comparable = bool(comparison and comparison.get("comparable"))
        if not previous_comparable:
            print(
                f"warning: previous report not comparable: "
                f"{(comparison or {}).get('reason', 'unknown')}",
                file=sys.stderr,
            )

    provenance = report.get("provenance") or {}
    report_tree = provenance.get("source_tree_sha256")
    mismatches: list[str] = []
    if report_tree:
        from _scanner_common import source_tree_sha256

        configuration = report.get("configuration") or {}
        live_tree = source_tree_sha256(
            args.root.resolve(),
            report.get("package"),
            bool(configuration.get("all_py")),
            configuration.get("subdirs"),
        )
        if live_tree != report_tree:
            mismatches.append(
                f"live source tree does not match the report's audited tree "
                f"(tree {live_tree[:12]} != report {report_tree[:12]}); "
                f"the code changed after the audit, or --root does not point "
                f"at the tree the audit was run against — re-run the audit "
                f"before verifying"
            )
    report_inputs = provenance.get("audit_inputs_sha256")
    if report_inputs:
        from _scanner_common import audit_inputs_sha256

        configuration = report.get("configuration") or {}
        inputs_meta = provenance.get("audit_inputs") or {}
        live_inputs = audit_inputs_sha256(
            args.root.resolve(),
            report.get("package"),
            bool(configuration.get("all_py")),
            bool(configuration.get("document_channel", True)),
            configuration.get("profile", "research"),
            inputs_meta.get("doc_dirs"),
            inputs_meta.get("doc_exclude"),
            inputs_meta.get("tex_dir"),
            inputs_meta.get("tex_exclude"),
            inputs_meta.get("subdirs", configuration.get("subdirs")),
        )
        if live_inputs != report_inputs:
            mismatches.append(
                f"live audit inputs (Python scope, document channel, TeX) do "
                f"not match the report's recorded inputs "
                f"(inputs {live_inputs[:12]} != report {report_inputs[:12]}); "
                f"a scanned input file changed after the audit — re-run the "
                f"audit before verifying"
            )

    artifact_reason = ""
    if args.test_command:
        result = subprocess.run(args.test_command, shell=True)
        test_gate = "passed" if result.returncode == 0 else "failed"
        if test_gate == "passed" and (report_tree or report_inputs):
            # TOCTOU guard: the test command may rewrite source or scanned
            # non-code inputs (codegen, golden files, formatters, docs/TeX),
            # so re-fingerprint both after it ran — a passing suite against
            # mutated state must not verify.
            from _scanner_common import audit_inputs_sha256, source_tree_sha256

            configuration = report.get("configuration") or {}
            if report_tree:
                post_test_tree = source_tree_sha256(
                    args.root.resolve(),
                    report.get("package"),
                    bool(configuration.get("all_py")),
                    configuration.get("subdirs"),
                )
                if post_test_tree != report_tree:
                    mismatches.append(
                        f"live source tree changed while running --test-command "
                        f"(tree {post_test_tree[:12]} != report {report_tree[:12]}); "
                        f"the tests modified the audited source — a passing suite "
                        f"against rewritten code cannot verify; re-run the audit "
                        f"before verifying"
                    )
            if report_inputs:
                inputs_meta = provenance.get("audit_inputs") or {}
                post_test_inputs = audit_inputs_sha256(
                    args.root.resolve(),
                    report.get("package"),
                    bool(configuration.get("all_py")),
                    bool(configuration.get("document_channel", True)),
                    configuration.get("profile", "research"),
                    inputs_meta.get("doc_dirs"),
                    inputs_meta.get("doc_exclude"),
                    inputs_meta.get("tex_dir"),
                    inputs_meta.get("tex_exclude"),
                    inputs_meta.get("subdirs", configuration.get("subdirs")),
                )
                if post_test_inputs != report_inputs:
                    mismatches.append(
                        f"live audit inputs changed while running --test-command "
                        f"(inputs {post_test_inputs[:12]} != report {report_inputs[:12]}); "
                        f"the tests modified a scanned input (document channel "
                        f"or TeX) — a passing suite against rewritten inputs "
                        f"cannot verify; re-run the audit before verifying"
                    )
    elif args.test_result:
        report_head = ((provenance.get("git") or {})).get("head")
        test_gate, artifact_reason = _external_test_gate(
            args.test_result, report_head, report_tree
        )
    elif args.no_tests:
        test_gate = "external_unverified"
    else:
        test_gate = "not_run"

    failures, stats = _check_report(
        report,
        {"verdicts": verdict_entries},
        previous,
        args.scope,
        test_gate,
        previous_comparable,
        invalid_verdicts,
        mismatches,
    )
    if artifact_reason:
        print(f"test artifact: {artifact_reason}", file=sys.stderr)
    passed = not failures
    if args.json is not None:
        atomic_write_text(
            args.json,
            json.dumps(
                {
                    "schema_version": 5,
                    "passed": passed,
                    "failures": failures,
                    "stats": stats,
                },
                indent=2,
            )
            + "\n",
        )
    for failure in failures:
        print(f"VERIFY FAIL {failure}", file=sys.stderr)
    print(
        f"VERIFY {'PASS' if passed else 'REJECT'} "
        f"code_action_verdicts={stats['code_action_verdicts']} "
        f"still_present={stats['still_present']} "
        f"test_gate={stats['test_gate']} "
        f"fully_verified={stats['fully_verified']} "
        f"failures={len(failures)}"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
