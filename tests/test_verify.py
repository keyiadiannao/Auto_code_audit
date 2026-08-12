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
    assert _risk_level("deadcode", detail) == "high"
    failures, _ = _check_report(report, _verdicts([]), _cluster_report([]), None)
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
    failures, _ = _check_report(report, verdicts, _cluster_report([]), None)
    assert any("new high candidate" in failure for failure in failures)
    # And the finding hash canonicalizes the channel-injected detail.
    assert finding_evidence_hash(scanner, target_id, detail)


def test_verify_loads_per_case_verdict_directory(tmp_path) -> None:
    """A directory of SKILL Phase 2 per-case verdict files feeds the gate.

    The shared ``load_protocol_verdict`` pipeline validates every file: the
    triple binding (filename == evidence_hash == case_hash), the bridge
    fields, and the full protocol validator.  A file that fails any step is
    skipped with a warning — never half-parsed into the gate.
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
    entries, warnings = _load_verdicts(verdicts_dir)
    assert len(entries) == 1
    assert entries[0]["scanner"] == "duplicates"
    assert entries[0]["target_id"] == "cluster/abc"
    assert entries[0]["case_hash"] == digest
    assert any("pre-bridge" in warning for warning in warnings)
    assert any("must set recommended_action" in warning for warning in warnings)

    failures, stats = _check_report(
        _cluster_report([cluster]), {"verdicts": entries}, None, None, "skipped"
    )
    assert stats["code_action_verdicts"] == 1
    assert any("finding still present" in failure for failure in failures)


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
    entries, warnings = _load_verdicts(verdicts_dir)
    assert entries == []
    assert any("case_hash" in warning for warning in warnings)


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


def test_verify_external_artifact_passed_is_fully_verified(tmp_path) -> None:
    """A machine-readable external test artifact with status 'passed' is
    machine-checked evidence: the gate can be fully verified."""
    import subprocess
    import sys

    report_path = tmp_path / "report.json"
    verdicts_path = tmp_path / "verdicts.json"
    artifact_path = tmp_path / "ci-result.json"
    report_path.write_text(json.dumps(_cluster_report([])), encoding="utf-8")
    verdicts_path.write_text(json.dumps(_verdicts([])), encoding="utf-8")
    artifact_path.write_text(
        json.dumps(
            {
                "status": "passed",
                "tool": "pytest",
                "summary": "42 passed",
                "exit_code": 0,
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
    assert "test_gate=external_passed" in result.stderr + result.stdout
    assert "fully_verified=True" in result.stderr + result.stdout
    payload = json.loads((tmp_path / "verify.json").read_text(encoding="utf-8"))
    assert payload["stats"]["test_gate"] == "external_passed"
    assert payload["stats"]["fully_verified"] is True


def test_verify_external_artifact_failure_rejects(tmp_path) -> None:
    """An artifact reporting failure rejects the gate."""
    import subprocess
    import sys

    report_path = tmp_path / "report.json"
    verdicts_path = tmp_path / "verdicts.json"
    artifact_path = tmp_path / "ci-result.json"
    report_path.write_text(json.dumps(_cluster_report([])), encoding="utf-8")
    verdicts_path.write_text(json.dumps(_verdicts([])), encoding="utf-8")
    artifact_path.write_text(
        json.dumps({"status": "failed", "exit_code": 3}), encoding="utf-8"
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
    """An unreadable or malformed artifact cannot self-approve the gate."""
    import subprocess
    import sys

    report_path = tmp_path / "report.json"
    verdicts_path = tmp_path / "verdicts.json"
    report_path.write_text(json.dumps(_cluster_report([])), encoding="utf-8")
    verdicts_path.write_text(json.dumps(_verdicts([])), encoding="utf-8")
    for name, content in (
        ("garbage.json", "not json at all"),
        ("bad-status.json", '{"status": "skipped"}'),
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
