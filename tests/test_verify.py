"""Tests for the engine-owned acceptance gate (run_verify.py)."""
from __future__ import annotations

import json

from run_verify import _check_report, _evidence_hash


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


def test_verify_rejects_finding_still_present() -> None:
    cluster = _cluster("abc")
    report = _cluster_report([cluster])
    verdicts = _verdicts(
        [
            {
                "scanner": "duplicates",
                "target_id": "cluster/abc",
                "evidence_hash": _evidence_hash("duplicates", "cluster/abc", cluster),
                "disposition": "true duplicate",
            }
        ]
    )
    failures, stats = _check_report(report, verdicts, None, None)
    assert stats["code_action_verdicts"] == 1
    assert stats["still_present"] == 1
    assert any("finding still present" in failure for failure in failures)


def test_verify_passes_when_finding_removed() -> None:
    verdicts = _verdicts(
        [
            {
                "scanner": "duplicates",
                "target_id": "cluster/abc",
                "evidence_hash": "x",
                "disposition": "true duplicate",
            }
        ]
    )
    failures, stats = _check_report(_cluster_report([]), verdicts, None, None)
    assert failures == []
    assert stats["still_present"] == 0


def test_verify_ignores_false_positive_verdicts() -> None:
    report = _cluster_report([_cluster("abc")])
    verdicts = _verdicts(
        [
            {
                "scanner": "duplicates",
                "target_id": "cluster/abc",
                "evidence_hash": "x",
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
    old_hash = _evidence_hash("duplicates", "cluster/abc", cluster)
    verdicts = _verdicts(
        [
            {
                "scanner": "duplicates",
                "target_id": "cluster/abc",
                "evidence_hash": old_hash,
                "disposition": "true duplicate",
            }
        ]
    )
    failures, _ = _check_report(report, verdicts, None, None)
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
    first = _evidence_hash("duplicates", "cluster/abc", {"id": "abc", "size": 1})
    second = _evidence_hash("duplicates", "cluster/abc", {"id": "abc", "size": 2})
    assert first != second
    assert first == _evidence_hash("duplicates", "cluster/abc", {"id": "abc", "size": 1})


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
            _verdicts(
                [
                    {
                        "scanner": "duplicates",
                        "target_id": "cluster/abc",
                        "evidence_hash": _evidence_hash(
                            "duplicates", "cluster/abc", cluster
                        ),
                        "disposition": "true duplicate",
                    }
                ]
            )
        ),
        encoding="utf-8",
    )
    failed = subprocess.run(
        [sys.executable, "-m", "run_verify", "--report", str(report_path), "--verdicts", str(verdicts_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode == 1
    assert "VERIFY REJECT" in failed.stderr + failed.stdout

    report_path.write_text(json.dumps(_cluster_report([])), encoding="utf-8")
    passed = subprocess.run(
        [sys.executable, "-m", "run_verify", "--report", str(report_path), "--verdicts", str(verdicts_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert passed.returncode == 0
    assert "VERIFY PASS" in passed.stderr + passed.stdout
