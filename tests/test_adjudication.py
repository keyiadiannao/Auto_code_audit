from __future__ import annotations

import json

import pytest

from benchmarks.adjudication_cases import build_case
from benchmarks.adjudication_protocol import (
    CASE_SCHEMA_VERSION,
    RECOMMENDED_ACTIONS,
    REASON_CODES,
    VERIFICATION_GATES,
    validate_case,
    validate_verdict,
)
from benchmarks.run_adjudication import (
    _parse_verdict,
    confusion_metrics,
    load_cases_from_file,
    load_truth_file,
    prepare_cases,
    read_verdict_file,
    restrict_to_corpus,
    user_prompt,
    write_verdict_file,
)


def _valid_verdict() -> dict:
    return {
        "disposition": "true_finding",
        "confidence": 0.9,
        "reason": "identical 11-line env parsing in three shells",
        "reason_codes": ["DUPLICATED_OWNERSHIP"],
        "recommended_action": "extract_shared_component",
        "reuse_target": "utils.py::parse_env_args",
        "required_verification": ["unit_tests", "re_audit"],
    }


def test_validate_verdict_accepts_full_protocol() -> None:
    verdict = validate_verdict(_valid_verdict())
    assert verdict["disposition"] == "true_finding"
    assert verdict["confidence"] == 0.9
    assert verdict["reason_codes"] == ["DUPLICATED_OWNERSHIP"]
    assert verdict["required_verification"] == ["re_audit", "unit_tests"]


def test_validate_verdict_rejects_bad_fields() -> None:
    with pytest.raises(ValueError, match="missing required"):
        validate_verdict({"disposition": "true_finding"})
    bad = _valid_verdict()
    bad["disposition"] = "maybe"
    with pytest.raises(ValueError, match="disposition"):
        validate_verdict(bad)
    bad = _valid_verdict()
    bad["confidence"] = 1.5
    with pytest.raises(ValueError, match="confidence"):
        validate_verdict(bad)
    bad = _valid_verdict()
    bad["reason"] = ""
    with pytest.raises(ValueError, match="reason"):
        validate_verdict(bad)
    bad = _valid_verdict()
    bad["reason_codes"] = ["NOT_A_CODE"]
    with pytest.raises(ValueError, match="reason_code"):
        validate_verdict(bad)
    bad = _valid_verdict()
    bad["recommended_action"] = "delete_everything"
    with pytest.raises(ValueError, match="recommended_action"):
        validate_verdict(bad)
    bad = _valid_verdict()
    bad["required_verification"] = ["fuzz_forever"]
    with pytest.raises(ValueError, match="verification"):
        validate_verdict(bad)


def test_protocol_enums_are_nonempty_and_stable() -> None:
    assert REASON_CODES >= {"CONTRACT_DRIFT", "ORPHANED_CODE", "DUPLICATED_OWNERSHIP"}
    assert RECOMMENDED_ACTIONS >= {"none", "extract_shared_component", "reuse_existing"}
    assert VERIFICATION_GATES >= {"unit_tests", "re_audit"}
    assert "investigate" in RECOMMENDED_ACTIONS


def test_validate_case_checks_schema_version_and_scanner() -> None:
    bundle = {
        "case_schema_version": CASE_SCHEMA_VERSION,
        "project_id": "p",
        "commit": "0" * 40,
        "scanner": "duplicates",
        "target_id": "cluster/abc",
        "display": "x",
        "evidence": {},
        "evidence_hash": "h",
    }
    assert validate_case(bundle)["scanner"] == "duplicates"
    with pytest.raises(ValueError, match="missing required"):
        validate_case({key: value for key, value in bundle.items() if key != "evidence"})
    bad = dict(bundle)
    bad["case_schema_version"] = 99
    with pytest.raises(ValueError, match="case_schema_version"):
        validate_case(bad)
    bad = dict(bundle)
    bad["scanner"] = "nonsense"
    with pytest.raises(ValueError, match="scanner"):
        validate_case(bad)


def test_build_case_is_deterministic_and_label_free(tmp_path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "a.py").write_text(
        "def foo(x):\n    return x + 1\n\ndef bar(y):\n    return y - 1\n",
        encoding="utf-8",
    )
    detail = {"path": "a.py", "name": "foo", "line": 1, "signature": "(x)"}
    first = build_case("p", "0" * 40, "contracts", "t", "d", detail, package)
    second = build_case("p", "0" * 40, "contracts", "t", "d", detail, package)
    assert first["evidence_hash"] == second["evidence_hash"]
    assert "label" not in first
    assert "ground_truth" not in first
    assert first["snippets"][0]["code"].startswith("L1: def foo(x):")
    altered = build_case("p", "0" * 40, "contracts", "t", "d", {"path": "a.py", "line": 4}, package)
    assert altered["evidence_hash"] != first["evidence_hash"]


def test_parse_verdict_handles_fences_and_garbage() -> None:
    verdict = _parse_verdict('```json\n{"disposition": "false_positive", '
                             '"confidence": 0.2, "reason": "public API"}\n```')
    assert verdict is not None
    assert verdict["disposition"] == "false_positive"
    assert _parse_verdict("no json here") is None
    assert _parse_verdict('{"disposition": "true_finding"}') is None
    assert _parse_verdict('{"disposition": "maybe", "confidence": 1, "reason": "x"}') is None


def test_user_prompt_contains_evidence_but_no_label(tmp_path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "a.py").write_text("def foo(x):\n    return x\n", encoding="utf-8")
    bundle = build_case(
        "p", "0" * 40, "duplicates", "cluster/1", "d",
        {"members": [{"path": "a.py", "qualname": "foo"}]}, package,
    )
    prompt = user_prompt(bundle)
    assert "cluster/1" in prompt
    assert "def foo(x):" in prompt
    assert "ground_truth" not in prompt
    assert '"label"' not in prompt


def test_confusion_metrics_numbers() -> None:
    scored = [
        {"ground_truth": "true_finding", "predicted": "true_finding"},
        {"ground_truth": "true_finding", "predicted": "false_positive"},
        {"ground_truth": "false_positive", "predicted": "true_finding"},
        {"ground_truth": "false_positive", "predicted": "false_positive"},
        {"ground_truth": "false_positive", "predicted": "false_positive"},
    ]
    metrics = confusion_metrics(scored)
    assert metrics["tp"] == 1
    assert metrics["fp"] == 1
    assert metrics["fn"] == 1
    assert metrics["tn"] == 2
    assert metrics["adjudication_precision"] == 0.5
    assert metrics["adjudication_recall"] == 0.5
    assert metrics["fp_rejection_rate"] == pytest.approx(0.667, abs=0.001)
    assert metrics["fn_rate"] == 0.5


def test_verdict_file_roundtrip_validates(tmp_path) -> None:
    verdict = _valid_verdict()
    path = write_verdict_file(tmp_path, "abc123", verdict)
    loaded = read_verdict_file(path, "abc123")
    assert loaded is not None
    assert loaded["disposition"] == "true_finding"
    malformed = tmp_path / "malformed.json"
    malformed.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "evidence_hash": "malformed",
                "verdict": {"disposition": "true_finding"},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing required"):
        read_verdict_file(malformed, "malformed")
    wrong_schema = tmp_path / "wrongschema.json"
    wrong_schema.write_text(
        json.dumps(
            {
                "schema_version": 99,
                "evidence_hash": "wrongschema",
                "verdict": _valid_verdict(),
            }
        ),
        encoding="utf-8",
    )
    assert read_verdict_file(wrong_schema, "wrongschema") is None


def test_verdict_hash_binding_rejects_filename_mismatch(tmp_path) -> None:
    path = write_verdict_file(tmp_path, "aaa", _valid_verdict())
    with pytest.raises(ValueError, match="filename"):
        read_verdict_file(path, "bbb")


def test_build_case_carries_both_hash_concepts(tmp_path) -> None:
    """The bundle distinguishes the case hash (context binding) from the
    finding hash (stale-evidence binding)."""
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "a.py").write_text(
        "def foo(x):\n    return x + 1\n", encoding="utf-8"
    )
    detail = {"path": "a.py", "name": "foo", "line": 1, "signature": "(x)"}
    bundle = build_case("p", "0" * 40, "contracts", "t", "d", detail, package)
    from run_all import finding_evidence_hash

    assert bundle["evidence_hash"] != bundle["finding_evidence_hash"]
    assert bundle["finding_evidence_hash"] == finding_evidence_hash(
        "contracts", "t", detail
    )
    # Snippet drift changes the case hash but not the finding hash.
    package.joinpath("a.py").write_text(
        "def foo(x):\n    return x + 1\n\n# filler\n", encoding="utf-8"
    )
    drifted = build_case("p", "0" * 40, "contracts", "t", "d", detail, package)
    assert drifted["evidence_hash"] != bundle["evidence_hash"]
    assert drifted["finding_evidence_hash"] == bundle["finding_evidence_hash"]


def test_write_verdict_file_bridge_fields(tmp_path) -> None:
    """Per-case verdict files carry the scanner/target_id/finding hash the
    engine-owned verify gate needs to match report candidates."""
    case = {
        "scanner": "regions",
        "target_id": "region/abc",
        "finding_evidence_hash": "f" * 64,
    }
    path = write_verdict_file(tmp_path, "abc123", _valid_verdict(), case=case)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["scanner"] == "regions"
    assert payload["target_id"] == "region/abc"
    assert payload["finding_evidence_hash"] == "f" * 64
    assert payload["case_hash"] == "abc123"
    # The scoring loop still binds on the case digest.
    assert payload["evidence_hash"] == "abc123"
    loaded = read_verdict_file(path, "abc123")
    assert loaded is not None


def test_verdict_hash_binding_rejects_payload_mismatch(tmp_path) -> None:
    path = write_verdict_file(tmp_path, "aaa", _valid_verdict())
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "evidence_hash": "ccc",
                "verdict": _valid_verdict(),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="payload evidence_hash"):
        read_verdict_file(path, "aaa")


def test_verdict_cross_field_validation() -> None:
    no_reaadit = _valid_verdict()
    no_reaadit["required_verification"] = ["unit_tests"]
    with pytest.raises(ValueError, match="re_audit"):
        validate_verdict(no_reaadit)
    no_action = _valid_verdict()
    no_action["recommended_action"] = "none"
    with pytest.raises(ValueError, match="recommended_action"):
        validate_verdict(no_action)
    fp_with_action = {
        "disposition": "false_positive",
        "confidence": 0.8,
        "reason": "public API surface",
        "recommended_action": "extract_shared_component",
    }
    with pytest.raises(ValueError, match="false_positive"):
        validate_verdict(fp_with_action)
    ok_fp = {
        "disposition": "false_positive",
        "confidence": 0.8,
        "reason": "public API surface",
        "recommended_action": "none",
    }
    assert validate_verdict(ok_fp)["recommended_action"] == "none"
    reuse = _valid_verdict()
    reuse["recommended_action"] = "reuse_existing"
    reuse["reuse_target"] = None
    with pytest.raises(ValueError, match="reuse_target"):
        validate_verdict(reuse)


def test_build_case_slices_region_snippets(tmp_path) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "m.py").write_text(
        "def run():\n    x = lib.load()\n    y = x + 1\n    z = lib.save(y)\n    return z\n",
        encoding="utf-8",
    )
    detail = {
        "members": [
            {
                "path": "m.py",
                "qualname": "run",
                "region_id": "m.py:run:2-4",
                "start_line": 2,
                "end_line": 4,
            }
        ]
    }
    bundle = build_case("p", "0" * 40, "regions", "region/abc", "d", detail, package)
    assert bundle["snippets"][0]["code"].startswith("L2:     x = lib.load()")
    assert bundle["evidence_hash"]


def test_validate_case_accepts_regions_scanner() -> None:
    bundle = {
        "case_schema_version": CASE_SCHEMA_VERSION,
        "project_id": "p",
        "commit": "0" * 40,
        "scanner": "regions",
        "target_id": "region/abc",
        "display": "x",
        "evidence": {"members": [{"path": "m.py", "region_id": "r"}]},
        "evidence_hash": "h",
    }
    assert validate_case(bundle)["scanner"] == "regions"


def test_validate_verdict_accepts_unextracted_capability_code() -> None:
    verdict = {
        "disposition": "true_finding",
        "confidence": 0.9,
        "reason": "checkpoint normalization repeated in three functions",
        "reason_codes": ["UNEXTRACTED_SHARED_CAPABILITY"],
        "recommended_action": "extract_shared_component",
        "reuse_target": "checkpoints.py::load_checkpoint",
        "required_verification": ["re_audit"],
    }
    assert validate_verdict(verdict)["reason_codes"] == ["UNEXTRACTED_SHARED_CAPABILITY"]


def test_load_cases_and_truth_files(tmp_path) -> None:
    bundle = {
        "evidence_hash": "abc",
        "scanner": "contracts",
        "target_id": "t",
        "display": "d",
        "evidence": {"path": "a.py"},
        "snippets": [],
    }
    cases_file = tmp_path / "cases.jsonl"
    prepare_cases([bundle], cases_file)
    assert load_cases_from_file(cases_file)[0]["evidence_hash"] == "abc"
    truth_file = tmp_path / "truth.json"
    truth_file.write_text(json.dumps({"abc": "true_finding", "zzz": "maybe"}))
    with pytest.raises(ValueError, match="unknown labels"):
        load_truth_file(truth_file)
    truth_file.write_text(json.dumps({"abc": "true_finding"}))
    assert load_truth_file(truth_file) == {"abc": "true_finding"}


def test_restrict_to_corpus_gates_on_verified_and_hash(tmp_path) -> None:
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    cases = [
        {
            "project_id": "p",
            "scanner": "duplicates",
            "target_id": "cluster/1",
            "evidence_hash": "h1",
        },
        {
            "project_id": "p",
            "scanner": "duplicates",
            "target_id": "cluster/2",
            "evidence_hash": "h2",
        },
    ]
    (corpus_dir / "p.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "scanner": "duplicates",
                        "target_id": "cluster/1",
                        "label": "true_finding",
                        "evidence_hash": "h1",
                        "human_verified": True,
                    },
                    {
                        "scanner": "duplicates",
                        "target_id": "cluster/2",
                        "label": "false_positive",
                        "evidence_hash": "STALE",
                        "human_verified": True,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    truth = {"h1": "true_finding", "h2": "false_positive"}
    kept, truth_out, warnings = restrict_to_corpus(cases, truth, corpus_dir, verified_only=True)
    assert [case["evidence_hash"] for case in kept] == ["h1"]
    assert truth_out["h1"] == "true_finding"
    assert any("mismatch" in warning for warning in warnings)
    kept_all, _, _ = restrict_to_corpus(cases, truth, corpus_dir, verified_only=False)
    assert len(kept_all) == 1


def test_prepare_cases_writes_jsonl(tmp_path) -> None:
    cases_file = tmp_path / "cases.jsonl"
    bundle = {
        "evidence_hash": "abc",
        "scanner": "contracts",
        "target_id": "t",
        "display": "d",
        "evidence": {"path": "a.py"},
        "snippets": [],
    }
    prepare_cases([bundle], cases_file)
    lines = cases_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["evidence_hash"] == "abc"
    assert "prompt" in payload
