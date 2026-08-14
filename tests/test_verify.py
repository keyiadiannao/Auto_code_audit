"""Tests for the engine-owned acceptance gate (run_verify.py)."""
from __future__ import annotations

import json

from run_all import finding_evidence_hash
from run_verify import (
    _check_report,
    _in_scope,
    _is_code_action,
    _load_verdicts,
    _risk_level,
)


def _cluster_report(clusters: list[dict]) -> dict:
    return {"scanners": {"duplicates": {"clusters": clusters}}}


def _cluster(cluster_id: str, priority: str = "high") -> dict:
    return {
        "id": cluster_id,
        "priority": priority,
        "members": [{"path": "lib/a.py", "qualname": "f", "start_line": 1}],
    }


def _verdicts(entries: list[dict]) -> dict:
    return {"verdicts": entries}


def _legacy_verdict(scanner: str, target_id: str, detail: dict) -> dict:
    return {
        "scanner": scanner,
        "target_id": target_id,
        "finding_evidence_hash": finding_evidence_hash(scanner, target_id, detail),
        "disposition": "true duplicate",
    }


def test_verify_rejects_finding_still_present() -> None:
    cluster = _cluster("abc")
    report = _cluster_report([cluster])
    verdicts = _verdicts([_legacy_verdict("duplicates", "cluster/abc", cluster)])
    failures, stats = _check_report(report, verdicts, None, None, "skipped")
    assert stats["code_action_verdicts"] == 1
    assert stats["still_present"] == 1
    assert any("finding still present" in failure for failure in failures)


def test_verify_passes_when_finding_removed() -> None:
    verdicts = _verdicts([_legacy_verdict("duplicates", "cluster/abc", _cluster("abc"))])
    failures, stats = _check_report(_cluster_report([]), verdicts, None, None, "skipped")
    assert failures == []
    assert stats["still_present"] == 0


def test_verify_ignores_false_positive_verdicts() -> None:
    report = _cluster_report([_cluster("abc")])
    verdicts = _verdicts(
        [
            {
                "scanner": "duplicates",
                "target_id": "cluster/abc",
                "finding_evidence_hash": "x",
                "disposition": "false positive",
            }
        ]
    )
    failures, stats = _check_report(report, verdicts, None, None)
    assert failures == []
    assert stats["code_action_verdicts"] == 0


def test_verify_detects_unchanged_evidence() -> None:
    cluster = _cluster("abc")
    report = _cluster_report([cluster])
    old_hash = finding_evidence_hash("duplicates", "cluster/abc", cluster)
    verdicts = _verdicts(
        [
            {
                "scanner": "duplicates",
                "target_id": "cluster/abc",
                "finding_evidence_hash": old_hash,
                "disposition": "true duplicate",
            }
        ]
    )
    failures, _ = _check_report(report, verdicts, None, None, "skipped")
    assert any("evidence unchanged" in failure for failure in failures)


def test_verify_rejects_new_high_candidate_in_scope() -> None:
    previous = _cluster_report([])
    report = _cluster_report([_cluster("new1", "high")])
    failures, _ = _check_report(
        report, _verdicts([]), previous, None, previous_comparable=True
    )
    assert any("new high candidate" in failure for failure in failures)


def test_verify_scope_filters_new_candidates() -> None:
    previous = _cluster_report([])
    report = _cluster_report([_cluster("new1", "high")])
    failures, _ = _check_report(
        report, _verdicts([]), previous, "experiments/", previous_comparable=True
    )
    assert failures == []


def test_in_scope_matches_path_prefix_not_substring() -> None:
    # Regression: scope was a raw substring match, so `--scope lib` matched
    # `liberal/x.py` (a candidate the patch never touched), and `--scope src/lib`
    # matched nothing because candidate paths are package-relative.
    assert _in_scope({"path": "lib/a.py"}, "lib") is True
    assert _in_scope({"path": "lib/a.py"}, "lib/") is True
    assert _in_scope({"path": "liberal/x.py"}, "lib") is False
    assert _in_scope({"path": "experiments/e01.py"}, "lib") is False
    assert _in_scope({"path": "lib/a.py"}, None) is True  # no scope = in scope
    assert _in_scope({}, "lib") is True  # path-less candidates stay in scope


def test_verify_ignores_new_low_candidate() -> None:
    previous = _cluster_report([])
    report = _cluster_report([_cluster("new1", "low")])
    failures, _ = _check_report(
        report, _verdicts([]), previous, None, previous_comparable=True
    )
    assert failures == []


def test_evidence_hash_tracks_detail() -> None:
    first = finding_evidence_hash("duplicates", "cluster/abc", {"id": "abc", "size": 1})
    second = finding_evidence_hash("duplicates", "cluster/abc", {"id": "abc", "size": 2})
    assert first != second
    assert first == finding_evidence_hash(
        "duplicates", "cluster/abc", {"id": "abc", "size": 1}
    )


def test_verify_recognizes_protocol_true_finding() -> None:
    """The skill-first protocol disposition is a code action: verify must
    reject a still-present true_finding."""
    cluster = _cluster("abc")
    report = _cluster_report([cluster])
    verdicts = _verdicts(
        [
            {
                "scanner": "duplicates",
                "target_id": "cluster/abc",
                "finding_evidence_hash": finding_evidence_hash(
                    "duplicates", "cluster/abc", cluster
                ),
                "disposition": "true_finding",
                "recommended_action": "extract_shared_component",
            }
        ]
    )
    failures, stats = _check_report(report, verdicts, None, None, "skipped")
    assert stats["code_action_verdicts"] == 1
    assert stats["still_present"] == 1
    assert any("finding still present" in failure for failure in failures)
    assert _is_code_action(verdicts["verdicts"][0])


def test_verify_protocol_false_positive_and_investigate_not_code_actions() -> None:
    cluster = _cluster("abc")
    report = _cluster_report([cluster])
    verdicts = _verdicts(
        [
            {
                "scanner": "duplicates",
                "target_id": "cluster/abc",
                "disposition": "false_positive",
                "recommended_action": "none",
            },
            {
                "scanner": "duplicates",
                "target_id": "cluster/abc",
                "disposition": "true_finding",
                "recommended_action": "investigate",
            },
        ]
    )
    failures, stats = _check_report(report, verdicts, None, None)
    assert failures == []
    assert stats["code_action_verdicts"] == 0


def test_verify_requires_test_gate_for_code_actions() -> None:
    """A code-action verdict without a test gate is not acceptable."""
    verdicts = _verdicts([_legacy_verdict("duplicates", "cluster/abc", _cluster("abc"))])
    failures, _ = _check_report(_cluster_report([]), verdicts, None, None, "not_run")
    assert any("no test evidence" in failure for failure in failures)

    failures, _ = _check_report(_cluster_report([]), verdicts, None, None, "skipped")
    assert failures == []

    failures, _ = _check_report(_cluster_report([]), verdicts, None, None, "failed")
    assert any("test command failed" in failure for failure in failures)


def test_verify_no_test_gate_required_without_code_actions() -> None:
    failures, _ = _check_report(_cluster_report([]), _verdicts([]), None, None)
    assert failures == []


def test_verify_risk_level_falls_back_to_severity() -> None:
    """hardcoded candidates carry ``severity`` instead of ``priority``."""
    report = {
        "scanners": {
            "hardcoded": {
                "hits": {
                    "sha256": [
                        {
                            "path": "lib/runner.py",
                            "line": 3,
                            "severity": "medium",
                            "code": "a1b2",
                        }
                    ]
                }
            }
        }
    }
    detail = report["scanners"]["hardcoded"]["hits"]["sha256"][0]
    assert _risk_level("hardcoded", detail) == "medium"
    failures, _ = _check_report(
        report, _verdicts([]), _cluster_report([]), None, previous_comparable=True
    )
    assert any("new medium candidate" in failure for failure in failures)


def test_verify_risk_level_maps_dead_status_to_high() -> None:
    """A new DEAD module after a patch is the drift the gate exists to catch."""
    report = {
        "scanners": {
            "deadcode": {
                "candidates": [{"path": "lib/unused.py", "status": "DEAD"}]
            }
        }
    }
    detail = report["scanners"]["deadcode"]["candidates"][0]
    assert _risk_level("deadcode", detail) == "high"
    failures, _ = _check_report(
        report, _verdicts([]), _cluster_report([]), None, previous_comparable=True
    )
    assert any("new high candidate" in failure for failure in failures)


def test_verify_risk_level_maps_contracts_channels() -> None:
    """Contracts drift channels enter the new-risk gate via the unified
    severity function."""
    from run_all import finding_severity

    assert (
        finding_severity("contracts", {"_channel": "defensive_param_loosening"})
        == "high"
    )
    assert finding_severity("contracts", {"_channel": "env_written_not_read"}) == "high"
    assert (
        finding_severity("contracts", {"_channel": "generation_path_without_env"})
        == "medium"
    )
    # Non-risk contracts channels and non-contracts details stay unranked.
    assert finding_severity("contracts", {"_channel": "forwarding_wrappers"}) is None
    assert finding_severity("duplicates", {"_channel": "env_written_not_read"}) is None


def test_verify_new_contracts_candidate_rejects_with_previous() -> None:
    """A patch that adds an env write without a read is a new high risk."""
    from run_all import _candidate_signatures, finding_evidence_hash

    report = {
        "scanners": {
            "contracts": {
                "env_written_not_read": [
                    {"var": "E01_MODE", "path": "lib/runner.py", "line": 4}
                ]
            }
        }
    }
    signature = next(
        iter(_candidate_signatures(report.get("scanners", {}))["contracts"])
    )
    scanner, target_id, detail = signature
    verdicts = {"verdicts": []}
    failures, _ = _check_report(
        report, verdicts, _cluster_report([]), None, previous_comparable=True
    )
    assert any("new high candidate" in failure for failure in failures)
    # And the finding hash canonicalizes the channel-injected detail.
    assert finding_evidence_hash(scanner, target_id, detail)


def test_verify_loads_per_case_verdict_directory(tmp_path) -> None:
    """A directory of SKILL Phase 2 per-case verdict files feeds the gate.

    The shared ``load_protocol_verdict`` pipeline validates every file: the
    triple binding (filename == evidence_hash == case_hash), the bridge
    fields, and the full protocol validator.  A file that fails any step is
    reported as an invalid verdict artifact, and the gate rejects fail-closed
    — an invalid verdict can never silently disappear from the input.
    """
    verdicts_dir = tmp_path / "verdicts"
    verdicts_dir.mkdir()
    cluster = _cluster("abc")
    digest = "d" * 64
    (verdicts_dir / f"{digest}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "evidence_hash": digest,
                "case_hash": digest,
                "scanner": "duplicates",
                "target_id": "cluster/abc",
                "finding_evidence_hash": finding_evidence_hash(
                    "duplicates", "cluster/abc", cluster
                ),
                "adapter": "agent",
                "verdict": {
                    "disposition": "true_finding",
                    "confidence": 0.9,
                    "reason": "identical env parsing in three shells",
                    "recommended_action": "extract_shared_component",
                    "required_verification": ["re_audit"],
                },
            }
        ),
        encoding="utf-8",
    )
    # Correct binding but a verdict the protocol validator rejects (a
    # false_positive with a non-'none' action violates the cross-field rules).
    bad_digest = "e" * 64
    (verdicts_dir / f"{bad_digest}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "evidence_hash": bad_digest,
                "case_hash": bad_digest,
                "scanner": "duplicates",
                "target_id": "cluster/xyz",
                "verdict": {
                    "disposition": "false_positive",
                    "confidence": 0.8,
                    "reason": "public API surface",
                    "recommended_action": "extract_shared_component",
                },
            }
        ),
        encoding="utf-8",
    )
    # Pre-bridge files carry no scanner/target_id and are categorically
    # unusable by the gate.
    (verdicts_dir / "prebridge.json").write_text(
        json.dumps(
            {"schema_version": 1, "verdict": {"disposition": "true_finding"}}
        ),
        encoding="utf-8",
    )
    entries, invalid = _load_verdicts(verdicts_dir)
    assert len(entries) == 1
    assert entries[0]["scanner"] == "duplicates"
    assert entries[0]["target_id"] == "cluster/abc"
    assert entries[0]["case_hash"] == digest
    assert len(invalid) == 2
    assert any("pre-bridge" in message for message in invalid)
    assert any("must set recommended_action" in message for message in invalid)

    failures, stats = _check_report(
        _cluster_report([cluster]),
        {"verdicts": entries},
        None,
        None,
        "skipped",
        verdict_invalid=invalid,
    )
    assert stats["code_action_verdicts"] == 1
    assert any("finding still present" in failure for failure in failures)
    # The invalid artifacts reject the gate fail-closed: acceptance is not
    # possible while a verdict file the Layer 2 validator would reject is
    # sitting in the input directory.
    assert any("invalid verdict artifact" in failure for failure in failures)


def test_verify_rejects_case_hash_binding_mismatch(tmp_path) -> None:
    """A verdict whose case_hash contradicts the filename cannot enter the gate."""
    verdicts_dir = tmp_path / "verdicts"
    verdicts_dir.mkdir()
    digest = "f" * 64
    (verdicts_dir / f"{digest}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "evidence_hash": digest,
                "case_hash": "0" * 64,
                "scanner": "duplicates",
                "target_id": "cluster/abc",
                "verdict": {
                    "disposition": "true_finding",
                    "confidence": 0.9,
                    "reason": "identical env parsing in three shells",
                    "recommended_action": "extract_shared_component",
                    "required_verification": ["re_audit"],
                },
            }
        ),
        encoding="utf-8",
    )
    entries, invalid = _load_verdicts(verdicts_dir)
    assert entries == []
    assert any("case_hash" in message for message in invalid)


def test_verify_cli_end_to_end(tmp_path) -> None:
    """Full CLI round-trip: report + verdicts in, exit codes out."""
    import subprocess
    import sys

    cluster = _cluster("abc")
    report_path = tmp_path / "report.json"
    verdicts_path = tmp_path / "verdicts.json"
    report_path.write_text(
        json.dumps(_cluster_report([cluster])), encoding="utf-8"
    )
    verdicts_path.write_text(
        json.dumps(
            _verdicts([_legacy_verdict("duplicates", "cluster/abc", cluster)])
        ),
        encoding="utf-8",
    )
    failed = subprocess.run(
        [
            sys.executable, "-m", "run_verify",
            "--report", str(report_path),
            "--verdicts", str(verdicts_path),
            "--no-tests",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode == 1
    assert "VERIFY REJECT" in failed.stderr + failed.stdout

    report_path.write_text(json.dumps(_cluster_report([])), encoding="utf-8")
    passed = subprocess.run(
        [
            sys.executable, "-m", "run_verify",
            "--report", str(report_path),
            "--verdicts", str(verdicts_path),
            "--no-tests",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert passed.returncode == 0
    assert "VERIFY PASS" in passed.stderr + passed.stdout


def test_verify_cli_rejects_without_test_gate(tmp_path) -> None:
    """A code-action verdict with no test gate fails even when the finding
    is gone."""
    import subprocess
    import sys

    cluster = _cluster("abc")
    report_path = tmp_path / "report.json"
    verdicts_path = tmp_path / "verdicts.json"
    report_path.write_text(json.dumps(_cluster_report([])), encoding="utf-8")
    verdicts_path.write_text(
        json.dumps(
            _verdicts([_legacy_verdict("duplicates", "cluster/abc", cluster)])
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable, "-m", "run_verify",
            "--report", str(report_path),
            "--verdicts", str(verdicts_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "no test evidence" in result.stderr + result.stdout


#: The tree fingerprint of a scope that does not exist (no *.py files): the
#: CLI tests run from the repo root where ``pkg/`` is absent, so a report
#: with this hash still matches the live tree the gate recomputes.
_EMPTY_TREE = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def _full_report(
    clusters: list[dict], head: str, tree: str = _EMPTY_TREE
) -> dict:
    """A report shaped like run_all's payload: comparable metadata plus git
    provenance the test artifact's git_head can bind to and a source-tree
    fingerprint the artifact's source_tree_sha256 must match.  The
    audit-config hash is computed the same way run_verify recomputes it, so
    baseline and post-fix reports compare equal."""
    from run_all import SCHEMA_VERSION, audit_config_hash

    configuration = {"profile": "code", "all_py": False}
    return {
        "schema_version": SCHEMA_VERSION,
        "package": "pkg",
        "configuration": configuration,
        "provenance": {
            "git": {"head": head},
            "source_tree_sha256": tree,
            "audit_config_hash": audit_config_hash(configuration),
            "scanner_bundle_hash": "2" * 64,
        },
        "scanners": {"duplicates": {"clusters": clusters}},
    }


def test_verify_external_artifact_passed_is_fully_verified(tmp_path) -> None:
    """A machine-readable external test artifact with status 'passed', bound
    to the report's git head and backed by a comparable pre-patch report, is
    machine-checked evidence: the gate can be fully verified."""
    import subprocess
    import sys

    head = "a" * 40
    report_path = tmp_path / "report.json"
    previous_path = tmp_path / "previous.json"
    verdicts_path = tmp_path / "verdicts.json"
    artifact_path = tmp_path / "ci-result.json"
    report_path.write_text(
        json.dumps(_full_report([], head)), encoding="utf-8"
    )
    previous_path.write_text(
        json.dumps(_full_report([], head)), encoding="utf-8"
    )
    verdicts_path.write_text(json.dumps(_verdicts([])), encoding="utf-8")
    artifact_path.write_text(
        json.dumps(
            {
                "status": "passed",
                "tool": "pytest",
                "command": "pytest -q",
                "summary": "42 passed",
                "exit_code": 0,
                "git_head": head,
                "source_tree_sha256": _EMPTY_TREE,
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable, "-m", "run_verify",
            "--report", str(report_path),
            "--verdicts", str(verdicts_path),
            "--previous", str(previous_path),
            "--test-result", str(artifact_path),
            "--json", str(tmp_path / "verify.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "test_gate=external_passed" in result.stderr + result.stdout
    assert "fully_verified=True" in result.stderr + result.stdout
    payload = json.loads((tmp_path / "verify.json").read_text(encoding="utf-8"))
    assert payload["stats"]["test_gate"] == "external_passed"
    assert payload["stats"]["fully_verified"] is True


def test_verify_external_artifact_passed_without_previous_is_not_fully_verified(
    tmp_path,
) -> None:
    """Without a comparable pre-patch report the gate cannot prove the patch
    introduced no new candidate: accepted, but never fully_verified."""
    import subprocess
    import sys

    head = "b" * 40
    report_path = tmp_path / "report.json"
    verdicts_path = tmp_path / "verdicts.json"
    artifact_path = tmp_path / "ci-result.json"
    report_path.write_text(json.dumps(_full_report([], head)), encoding="utf-8")
    verdicts_path.write_text(json.dumps(_verdicts([])), encoding="utf-8")
    artifact_path.write_text(
        json.dumps(
            {
                "status": "passed",
                "tool": "pytest",
                "exit_code": 0,
                "git_head": head,
                "source_tree_sha256": _EMPTY_TREE,
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable, "-m", "run_verify",
            "--report", str(report_path),
            "--verdicts", str(verdicts_path),
            "--test-result", str(artifact_path),
            "--json", str(tmp_path / "verify.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "fully_verified=False" in result.stderr + result.stdout
    payload = json.loads((tmp_path / "verify.json").read_text(encoding="utf-8"))
    assert payload["stats"]["fully_verified"] is False


def test_verify_external_artifact_failure_rejects(tmp_path) -> None:
    """An artifact reporting failure rejects the gate."""
    import subprocess
    import sys

    head = "c" * 40
    report_path = tmp_path / "report.json"
    verdicts_path = tmp_path / "verdicts.json"
    artifact_path = tmp_path / "ci-result.json"
    report_path.write_text(json.dumps(_full_report([], head)), encoding="utf-8")
    verdicts_path.write_text(json.dumps(_verdicts([])), encoding="utf-8")
    artifact_path.write_text(
        json.dumps(
            {
                "status": "failed",
                "tool": "pytest",
                "exit_code": 3,
                "git_head": head,
                "source_tree_sha256": _EMPTY_TREE,
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable, "-m", "run_verify",
            "--report", str(report_path),
            "--verdicts", str(verdicts_path),
            "--test-result", str(artifact_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "status 'failed'" in result.stderr + result.stdout


def test_verify_external_artifact_invalid_rejects(tmp_path) -> None:
    """An unreadable, malformed, under-provenanced, or unbound artifact
    cannot self-approve the gate."""
    import subprocess
    import sys

    report_path = tmp_path / "report.json"
    verdicts_path = tmp_path / "verdicts.json"
    report_path.write_text(json.dumps(_full_report([], "d" * 40)), encoding="utf-8")
    verdicts_path.write_text(json.dumps(_verdicts([])), encoding="utf-8")
    for name, content in (
        ("garbage.json", "not json at all"),
        ("bad-status.json", '{"status": "skipped"}'),
        # Provenance is mandatory: {"status": "passed"} alone is not full
        # verification evidence.
        ("no-provenance.json", '{"status": "passed"}'),
        ("bad-exit.json", '{"status": "passed", "exit_code": 1, "git_head": "' + "d" * 40 + '", "tool": "pytest"}'),
        ("missing-head.json", '{"status": "passed", "exit_code": 0, "tool": "pytest"}'),
        ("no-runner.json", '{"status": "passed", "exit_code": 0, "git_head": "' + "d" * 40 + '"}'),
        # A bare git_head mismatch between artifact and report rejects.
        ("wrong-head.json", '{"status": "passed", "exit_code": 0, "git_head": "' + "e" * 40 + '", "tool": "pytest"}'),
        # The tree fingerprint is mandatory and must match the report's.
        ("missing-tree.json", '{"status": "passed", "exit_code": 0, "git_head": "' + "d" * 40 + '", "tool": "pytest"}'),
        ("bad-tree.json", '{"status": "passed", "exit_code": 0, "git_head": "' + "d" * 40 + '", "tool": "pytest", "source_tree_sha256": "xyz"}'),
        ("wrong-tree.json", '{"status": "passed", "exit_code": 0, "git_head": "' + "d" * 40 + '", "tool": "pytest", "source_tree_sha256": "' + "f" * 64 + '"}'),
    ):
        artifact_path = tmp_path / name
        artifact_path.write_text(content, encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable, "-m", "run_verify",
                "--report", str(report_path),
                "--verdicts", str(verdicts_path),
                "--test-result", str(artifact_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1, name
        assert "invalid" in result.stderr + result.stdout, name


def test_verify_live_tree_mismatch_rejects(tmp_path) -> None:
    """The gate fingerprints the live tree under --root and rejects when it
    no longer matches the report's audited tree: code changed after the
    audit, so no evidence applies to the current state."""
    import subprocess
    import sys

    from _scanner_common import source_tree_sha256

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("x = 1\n", encoding="utf-8")
    tree = source_tree_sha256(tmp_path, "pkg")

    head = "g" * 40
    report_path = tmp_path / "report.json"
    verdicts_path = tmp_path / "verdicts.json"
    report_path.write_text(json.dumps(_full_report([], head, tree=tree)), encoding="utf-8")
    verdicts_path.write_text(json.dumps(_verdicts([])), encoding="utf-8")
    artifact_path = tmp_path / "ci-result.json"
    artifact_path.write_text(
        json.dumps(
            {
                "status": "passed",
                "tool": "pytest",
                "exit_code": 0,
                "git_head": head,
                "source_tree_sha256": tree,
            }
        ),
        encoding="utf-8",
    )
    # The tree changes after the audit and the artifact were produced.
    (pkg / "a.py").write_text("x = 2\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable, "-m", "run_verify",
            "--root", str(tmp_path),
            "--report", str(report_path),
            "--verdicts", str(verdicts_path),
            "--test-result", str(artifact_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "re-run the audit" in result.stderr + result.stdout


def test_verify_incompatible_previous_rejects(tmp_path) -> None:
    """A pre-patch report with a different schema, package, scanner set,
    audit-config hash, or scanner-bundle hash makes the new-risk check
    meaningless: the gate rejects rather than trusting a garbage
    baseline."""
    import subprocess
    import sys

    head = "f" * 40
    report_path = tmp_path / "report.json"
    previous_path = tmp_path / "previous.json"
    verdicts_path = tmp_path / "verdicts.json"
    report_path.write_text(json.dumps(_full_report([], head)), encoding="utf-8")
    verdicts_path.write_text(json.dumps(_verdicts([])), encoding="utf-8")
    previous = _full_report([], head)
    previous["scanners"]["deadcode"] = {"candidates": []}
    previous_path.write_text(json.dumps(previous), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable, "-m", "run_verify",
            "--report", str(report_path),
            "--verdicts", str(verdicts_path),
            "--previous", str(previous_path),
            "--no-tests",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "not comparable" in result.stderr + result.stdout


def test_verify_config_or_bundle_hash_mismatch_rejects(tmp_path) -> None:
    """A baseline whose audit-config or scanner-bundle fingerprint differs
    from the post-fix report's is not comparable: candidate deltas could
    come from a config or tool change instead of a code edit."""
    import subprocess
    import sys

    head = "h" * 40
    report_path = tmp_path / "report.json"
    previous_path = tmp_path / "previous.json"
    verdicts_path = tmp_path / "verdicts.json"
    report_path.write_text(json.dumps(_full_report([], head)), encoding="utf-8")
    verdicts_path.write_text(json.dumps(_verdicts([])), encoding="utf-8")
    for key, value in (
        ("audit_config_hash", "9" * 64),
        ("scanner_bundle_hash", "8" * 64),
    ):
        previous = _full_report([], head)
        previous["provenance"][key] = value
        previous_path.write_text(json.dumps(previous), encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable, "-m", "run_verify",
                "--report", str(report_path),
                "--verdicts", str(verdicts_path),
                "--previous", str(previous_path),
                "--no-tests",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1, key
        assert "not comparable" in result.stderr + result.stdout, key


def test_verify_invalid_verdict_artifact_rejects_gate(tmp_path) -> None:
    """A protocol-invalid verdict file in the input directory rejects the
    gate fail-closed — it is never silently dropped from the input."""
    import subprocess
    import sys

    head = "f" * 40
    report_path = tmp_path / "report.json"
    previous_path = tmp_path / "previous.json"
    verdicts_dir = tmp_path / "verdicts"
    verdicts_dir.mkdir()
    report_path.write_text(json.dumps(_full_report([], head)), encoding="utf-8")
    previous_path.write_text(json.dumps(_full_report([], head)), encoding="utf-8")
    (verdicts_dir / "broken.json").write_text(
        json.dumps(
            {"schema_version": 1, "verdict": {"disposition": "true_finding"}}
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable, "-m", "run_verify",
            "--report", str(report_path),
            "--verdicts", str(verdicts_dir),
            "--previous", str(previous_path),
            "--no-tests",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "invalid verdict artifact" in result.stderr + result.stdout


def test_verify_no_tests_is_external_unverified(tmp_path) -> None:
    """--no-tests declares external delegation: accepted but never
    fully_verified."""
    import subprocess
    import sys

    report_path = tmp_path / "report.json"
    verdicts_path = tmp_path / "verdicts.json"
    report_path.write_text(json.dumps(_cluster_report([])), encoding="utf-8")
    verdicts_path.write_text(json.dumps(_verdicts([])), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable, "-m", "run_verify",
            "--report", str(report_path),
            "--verdicts", str(verdicts_path),
            "--no-tests",
            "--json", str(tmp_path / "verify.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "test_gate=external_unverified" in result.stderr + result.stdout
    assert "fully_verified=False" in result.stderr + result.stdout
    payload = json.loads((tmp_path / "verify.json").read_text(encoding="utf-8"))
    assert payload["stats"]["test_gate"] == "external_unverified"
    assert payload["stats"]["fully_verified"] is False


def test_verify_test_flags_are_mutually_exclusive(tmp_path) -> None:
    """Only one test-evidence flag may be given."""
    import subprocess
    import sys

    report_path = tmp_path / "report.json"
    verdicts_path = tmp_path / "verdicts.json"
    report_path.write_text(json.dumps(_cluster_report([])), encoding="utf-8")
    verdicts_path.write_text(json.dumps(_verdicts([])), encoding="utf-8")
    artifact_path = tmp_path / "ci-result.json"
    artifact_path.write_text('{"status": "passed"}', encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable, "-m", "run_verify",
            "--report", str(report_path),
            "--verdicts", str(verdicts_path),
            "--test-result", str(artifact_path),
            "--no-tests",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "mutually exclusive" in result.stderr + result.stdout


def test_audit_inputs_hash_tracks_document_channel(tmp_path) -> None:
    """A change to a scanned document-channel file (docs/*.md) changes the
    audit-input fingerprint even though the Python tree is untouched; with
    the channel off, the file is not an input at all."""
    from _scanner_common import audit_inputs_sha256

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    notes = tmp_path / "docs" / "notes.md"
    notes.write_text("first\n", encoding="utf-8")
    settings = dict(doc_dirs=["docs"], doc_exclude=[], tex_dir="docs", tex_exclude=[])
    on = audit_inputs_sha256(tmp_path, "pkg", document_channel=True, profile="code", **settings)
    off = audit_inputs_sha256(tmp_path, "pkg", document_channel=False, profile="code", **settings)
    assert on != off  # docs are an input only when the channel is on
    notes.write_text("second\n", encoding="utf-8")
    assert audit_inputs_sha256(tmp_path, "pkg", document_channel=True, profile="code", **settings) != on
    assert audit_inputs_sha256(tmp_path, "pkg", document_channel=False, profile="code", **settings) == off


def test_python_fingerprints_exclude_generated_and_audit_state(tmp_path) -> None:
    """Files no scanner consumes must not stale source or input evidence."""
    from _scanner_common import audit_inputs_sha256, source_tree_sha256

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("value = 1\n", encoding="utf-8")
    tree = source_tree_sha256(tmp_path, "pkg")
    inputs = audit_inputs_sha256(
        tmp_path, "pkg", document_channel=False, profile="code"
    )

    for directory in ("reports", ".venv", "build", "__pycache__"):
        target = pkg / directory
        target.mkdir()
        (target / "generated.py").write_text("private = True\n", encoding="utf-8")

    assert source_tree_sha256(tmp_path, "pkg") == tree
    assert (
        audit_inputs_sha256(
            tmp_path, "pkg", document_channel=False, profile="code"
        )
        == inputs
    )


def test_python_fingerprints_match_configured_subdirs(tmp_path) -> None:
    """A narrowed scanner scope and its provenance hash use the same files."""
    from _scanner_common import audit_inputs_sha256, source_tree_sha256

    pkg = tmp_path / "pkg"
    (pkg / "keep").mkdir(parents=True)
    (pkg / "other").mkdir()
    kept = pkg / "keep" / "a.py"
    outside = pkg / "other" / "b.py"
    kept.write_text("value = 1\n", encoding="utf-8")
    outside.write_text("value = 1\n", encoding="utf-8")
    subdirs = ["keep"]
    tree = source_tree_sha256(tmp_path, "pkg", subdirs=subdirs)
    inputs = audit_inputs_sha256(
        tmp_path,
        "pkg",
        document_channel=False,
        profile="code",
        subdirs=subdirs,
    )

    outside.write_text("value = 2\n", encoding="utf-8")
    assert source_tree_sha256(tmp_path, "pkg", subdirs=subdirs) == tree
    assert (
        audit_inputs_sha256(
            tmp_path,
            "pkg",
            document_channel=False,
            profile="code",
            subdirs=subdirs,
        )
        == inputs
    )
    kept.write_text("value = 2\n", encoding="utf-8")
    assert source_tree_sha256(tmp_path, "pkg", subdirs=subdirs) != tree


def test_all_py_keeps_package_boundary(tmp_path) -> None:
    """--all-py expands subdirs but never escapes the selected package."""
    from _scanner_common import source_tree_sha256

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("value = 1\n", encoding="utf-8")
    tree = source_tree_sha256(tmp_path, "pkg", all_py=True)
    (tmp_path / "outside.py").write_text("private = True\n", encoding="utf-8")
    assert source_tree_sha256(tmp_path, "pkg", all_py=True) == tree


def test_audit_inputs_hash_tracks_tex_in_research_profile(tmp_path) -> None:
    """TeX files under tex_dir enter the fingerprint only in the research
    profile (the style scanner's profile)."""
    from _scanner_common import audit_inputs_sha256

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    tex = tmp_path / "docs" / "paper.tex"
    tex.write_text("\\section{One}\n", encoding="utf-8")
    settings = dict(doc_dirs=[], doc_exclude=[], tex_dir="docs", tex_exclude=[])
    research = audit_inputs_sha256(tmp_path, "pkg", profile="research", **settings)
    code = audit_inputs_sha256(tmp_path, "pkg", profile="code", **settings)
    assert research != code
    tex.write_text("\\section{Two}\n", encoding="utf-8")
    assert audit_inputs_sha256(tmp_path, "pkg", profile="research", **settings) != research
    assert audit_inputs_sha256(tmp_path, "pkg", profile="code", **settings) == code


def test_verify_live_inputs_mismatch_rejects(tmp_path) -> None:
    """The gate also fingerprints the full audit-input manifest (document
    channel, TeX) and rejects when a scanned input changed after the audit,
    even though the Python tree is byte-identical."""
    import subprocess
    import sys

    from _scanner_common import audit_inputs_sha256, source_tree_sha256

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    notes = tmp_path / "docs" / "notes.md"
    notes.write_text("first\n", encoding="utf-8")
    tree = source_tree_sha256(tmp_path, "pkg")
    inputs = audit_inputs_sha256(
        tmp_path,
        "pkg",
        document_channel=True,
        profile="code",
        doc_dirs=["docs"],
        doc_exclude=[],
        tex_dir="docs",
        tex_exclude=[],
    )

    head = "k" * 40
    report_path = tmp_path / "report.json"
    verdicts_path = tmp_path / "verdicts.json"
    report = _full_report([], head, tree=tree)
    report["configuration"]["document_channel"] = True
    report["provenance"]["audit_inputs_sha256"] = inputs
    report["provenance"]["audit_inputs"] = {
        "doc_dirs": ["docs"],
        "doc_exclude": [],
        "tex_dir": "docs",
        "tex_exclude": [],
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    verdicts_path.write_text(json.dumps(_verdicts([])), encoding="utf-8")

    # A scanned input changes after the audit; the Python tree does not.
    notes.write_text("second\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable, "-m", "run_verify",
            "--root", str(tmp_path),
            "--report", str(report_path),
            "--verdicts", str(verdicts_path),
            "--no-tests",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "audit inputs" in result.stderr + result.stdout
    assert "re-run the audit" in result.stderr + result.stdout


def test_verify_post_test_rehash_rejects_modified_tree(tmp_path) -> None:
    """TOCTOU guard: a passing --test-command that rewrites source must not
    verify — the suite ran against mutated code, so the recorded tree hash
    no longer matches the live tree."""
    import subprocess
    import sys

    from _scanner_common import source_tree_sha256

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    source = pkg / "a.py"
    source.write_text("x = 1\n", encoding="utf-8")
    tree = source_tree_sha256(tmp_path, "pkg")

    head = "m" * 40
    report_path = tmp_path / "report.json"
    verdicts_path = tmp_path / "verdicts.json"
    report_path.write_text(
        json.dumps(_full_report([], head, tree=tree)), encoding="utf-8"
    )
    verdicts_path.write_text(json.dumps(_verdicts([])), encoding="utf-8")

    # Control: a test command that does not touch source passes.
    benign = subprocess.run(
        [
            sys.executable, "-m", "run_verify",
            "--root", str(tmp_path),
            "--report", str(report_path),
            "--verdicts", str(verdicts_path),
            "--test-command", f"{sys.executable} -c pass",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert benign.returncode == 0

    # A test command that rewrites a scanned .py file rejects the gate.
    mutator = tmp_path / "mutate.py"
    mutator.write_text(
        "from pathlib import Path\n"
        f"Path({str(source)!r}).write_text('y = 2\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable, "-m", "run_verify",
            "--root", str(tmp_path),
            "--report", str(report_path),
            "--verdicts", str(verdicts_path),
            "--test-command", f'{sys.executable} "{mutator}"',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "modified the audited source" in result.stderr + result.stdout


def test_verify_post_test_rehash_rejects_modified_inputs(tmp_path) -> None:
    """TOCTOU guard covers the full audit-input manifest: a passing
    --test-command that rewrites a scanned document-channel file (with the
    Python tree untouched) must not verify either."""
    import subprocess
    import sys

    from _scanner_common import audit_inputs_sha256, source_tree_sha256

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    notes = tmp_path / "docs" / "notes.md"
    notes.write_text("first\n", encoding="utf-8")
    tree = source_tree_sha256(tmp_path, "pkg")
    inputs = audit_inputs_sha256(
        tmp_path,
        "pkg",
        document_channel=True,
        profile="code",
        doc_dirs=["docs"],
        doc_exclude=[],
        tex_dir="docs",
        tex_exclude=[],
    )

    head = "n" * 40
    report_path = tmp_path / "report.json"
    verdicts_path = tmp_path / "verdicts.json"
    report = _full_report([], head, tree=tree)
    report["configuration"]["document_channel"] = True
    report["provenance"]["audit_inputs_sha256"] = inputs
    report["provenance"]["audit_inputs"] = {
        "doc_dirs": ["docs"],
        "doc_exclude": [],
        "tex_dir": "docs",
        "tex_exclude": [],
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    verdicts_path.write_text(json.dumps(_verdicts([])), encoding="utf-8")

    mutator = tmp_path / "mutate.py"
    mutator.write_text(
        "from pathlib import Path\n"
        f"Path({str(notes)!r}).write_text('second\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable, "-m", "run_verify",
            "--root", str(tmp_path),
            "--report", str(report_path),
            "--verdicts", str(verdicts_path),
            "--test-command", f'{sys.executable} "{mutator}"',
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "modified a scanned input" in result.stderr + result.stdout
    # The Python tree is byte-identical after the run: only the inputs gate
    # caught the rewrite.
    assert source_tree_sha256(tmp_path, "pkg") == tree


def test_verify_rejects_incomplete_analysis_by_default(tmp_path) -> None:
    import subprocess
    import sys

    report_path = tmp_path / "report.json"
    verdicts_path = tmp_path / "verdicts.json"
    report_path.write_text(
        json.dumps(
            {
                "package": "pkg",
                "analysis": {
                    "complete": False,
                    "parse_failures": {
                        "duplicates": [{"path": "x.py", "error": "SyntaxError"}]
                    },
                },
                "scanners": {},
                "provenance": {},
            }
        ),
        encoding="utf-8",
    )
    verdicts_path.write_text(json.dumps({"verdicts": []}), encoding="utf-8")

    rejected = subprocess.run(
        [sys.executable, "-m", "run_verify",
         "--report", str(report_path),
         "--verdicts", str(verdicts_path),
         "--no-tests"],
        capture_output=True, text=True, check=False,
    )
    assert rejected.returncode == 2
    assert "incomplete" in rejected.stderr + rejected.stdout

    allowed = subprocess.run(
        [sys.executable, "-m", "run_verify",
         "--report", str(report_path),
         "--verdicts", str(verdicts_path),
         "--no-tests", "--allow-incomplete-analysis"],
        capture_output=True, text=True, check=False,
    )
    assert allowed.returncode == 0
