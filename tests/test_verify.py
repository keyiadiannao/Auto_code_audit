"""Tests for the engine-owned acceptance gate (run_verify.py)."""
from __future__ import annotations

import json

from run_all import finding_evidence_hash
from run_verify import (
    _check_report,
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
    failures, _ = _check_report(report, _verdicts([]), previous, None)
    assert any("new high candidate" in failure for failure in failures)


def test_verify_scope_filters_new_candidates() -> None:
    previous = _cluster_report([])
    report = _cluster_report([_cluster("new1", "high")])
    failures, _ = _check_report(report, _verdicts([]), previous, "experiments/")
    assert failures == []


def test_verify_ignores_new_low_candidate() -> None:
    previous = _cluster_report([])
    report = _cluster_report([_cluster("new1", "low")])
    failures, _ = _check_report(report, _verdicts([]), previous, None)
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
    assert any("no test gate" in failure for failure in failures)

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
    assert _risk_level(report["scanners"]["hardcoded"]["hits"]["sha256"][0]) == "medium"
    failures, _ = _check_report(report, _verdicts([]), _cluster_report([]), None)
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
    assert _risk_level(detail) == "high"
    failures, _ = _check_report(report, _verdicts([]), _cluster_report([]), None)
    assert any("new high candidate" in failure for failure in failures)


def test_verify_loads_per_case_verdict_directory(tmp_path) -> None:
    """A directory of SKILL Phase 2 per-case verdict files feeds the gate."""
    verdicts_dir = tmp_path / "verdicts"
    verdicts_dir.mkdir()
    cluster = _cluster("abc")
    digest = "d" * 64
    (verdicts_dir / f"{digest}.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "case_hash": digest,
                "scanner": "duplicates",
                "target_id": "cluster/abc",
                "finding_evidence_hash": finding_evidence_hash(
                    "duplicates", "cluster/abc", cluster
                ),
                "adapter": "agent",
                "verdict": {
                    "disposition": "true_finding",
                    "recommended_action": "extract_shared_component",
                },
            }
        ),
        encoding="utf-8",
    )
    (verdicts_dir / "prebridge.json").write_text(
        json.dumps(
            {"schema_version": 1, "verdict": {"disposition": "true_finding"}}
        ),
        encoding="utf-8",
    )
    entries, warnings = _load_verdicts(verdicts_dir)
    assert len(entries) == 1
    assert entries[0]["scanner"] == "duplicates"
    assert entries[0]["target_id"] == "cluster/abc"
    assert entries[0]["case_hash"] == digest
    assert any("pre-bridge" in warning for warning in warnings)

    failures, stats = _check_report(
        _cluster_report([cluster]), {"verdicts": entries}, None, None, "skipped"
    )
    assert stats["code_action_verdicts"] == 1
    assert any("finding still present" in failure for failure in failures)


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
    assert "no test gate" in result.stderr + result.stdout
