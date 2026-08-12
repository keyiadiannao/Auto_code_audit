from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.run_benchmarks import (
    _aggregate_totals,
    _label_stats,
    _metrics,
    _python_lines,
    load_labels,
    load_manifest,
    run_benchmarks,
)
from benchmarks.run_mutation import _recall, run_corpus


def test_benchmark_manifest_is_fixed_and_valid() -> None:
    manifest = load_manifest(Path("benchmarks/manifest.json"))
    projects = manifest["projects"]
    assert [project["id"] for project in projects] == [
        "requests",
        "click",
        "httpx",
        "pytest",
        "werkzeug",
        "starlette",
    ]
    assert all(len(project["commit"]) == 40 for project in projects)
    assert all(project["all_py"] for project in projects)


def test_benchmark_manifest_rejects_duplicate_ids(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "projects": [
                    {
                        "id": "same",
                        "url": "https://example.com/a.git",
                        "commit": "0" * 40,
                        "package": "src/pkg",
                        "license": "MIT",
                    },
                    {
                        "id": "same",
                        "url": "https://example.com/b.git",
                        "commit": "1" * 40,
                        "package": "src/pkg",
                        "license": "MIT",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicated"):
        load_manifest(path)


def _toy_report() -> dict:
    return {
        "scanners": {
            "deadcode": {
                "candidates": [
                    {"path": "lib/a.py", "status": "DEAD"},
                    {"path": "lib/b.py", "status": "DEAD"},
                    {"path": "lib/c.py", "status": "PUBLIC_API_CANDIDATE"},
                ]
            },
            "duplicates": {"clusters": [{"id": "abc123", "priority": "high"}]},
            "forks": {"pairs": [], "small_function_pairs": []},
            "contracts": {
                "experiment_as_library": [],
                "forwarding_wrappers": [],
                "unreferenced_top_level_functions": [],
                "cli_without_bootstrap": [],
                "defensive_param_loosening": [],
                "env_written_not_read": [],
                "generation_path_without_env": [],
                "same_name_contracts": [],
            },
            "capabilities": {"overlap": []},
            "hardcoded": {"hits": {}},
            "style": {"hits": {}},
        }
    }


def _toy_labels() -> dict:
    return {
        "schema_version": 1,
        "project_id": "toy",
        "commit": "0" * 40,
        "labels": [
            {
                "scanner": "deadcode",
                "target_id": "DEAD/lib/a.py",
                "label": "true_finding",
                "reason": "module is never imported",
            },
            {
                "scanner": "deadcode",
                "target_id": "DEAD/lib/b.py",
                "label": "false_positive",
                "reason": "imported through entry point",
            },
            {
                "scanner": "deadcode",
                "target_id": "DEAD/lib/zzz.py",
                "label": "true_finding",
                "reason": "stale label",
            },
            {
                "scanner": "duplicates",
                "target_id": "cluster/abc123",
                "label": "true_finding",
                "reason": "identical implementation",
            },
        ],
    }


def test_label_stats_compute_precision_and_coverage() -> None:
    stats = _label_stats(_toy_report(), _toy_labels())
    assert stats["coverage"] == {"candidates": 4, "labelled": 3, "unlabelled": 1}
    assert stats["aggregate"] == {
        "candidates": 4,
        "labelled": 3,
        "true_findings": 2,
        "false_positives": 1,
        "precision": 0.667,
    }
    assert stats["unmatched_labels"] == ["deadcode/DEAD/lib/zzz.py"]
    assert stats["per_scanner"]["duplicates"]["precision"] == 1.0
    assert stats["per_scanner"]["forks"]["labelled"] == 0


def test_label_stats_without_labels_leaves_all_unlabelled() -> None:
    labels = {
        "schema_version": 1,
        "project_id": "toy",
        "commit": "0" * 40,
        "labels": [],
    }
    stats = _label_stats(_toy_report(), labels)
    assert stats["coverage"]["labelled"] == 0
    assert stats["aggregate"]["precision"] is None


def test_load_labels_validates_entries(tmp_path: Path) -> None:
    path = tmp_path / "labels.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": "toy",
                "commit": "0" * 40,
                "labels": [
                    {
                        "scanner": "deadcode",
                        "target_id": "DEAD/a.py",
                        "label": "maybe",
                        "reason": "bad value",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="label must be one of"):
        load_labels(path)


def test_load_labels_rejects_duplicated_targets(tmp_path: Path) -> None:
    path = tmp_path / "labels.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": "toy",
                "commit": "0" * 40,
                "labels": [
                    {
                        "scanner": "deadcode",
                        "target_id": "DEAD/a.py",
                        "label": "true_finding",
                        "reason": "first",
                    },
                    {
                        "scanner": "deadcode",
                        "target_id": "DEAD/a.py",
                        "label": "false_positive",
                        "reason": "second",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicated label"):
        load_labels(path)


def test_python_lines_counts_scan_directory(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    (package / "lib").mkdir(parents=True)
    (package / "lib" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (package / "lib" / "b.py").write_text("y = 2\nz = 3\n", encoding="utf-8")
    assert _python_lines(package) == 3


def test_aggregate_totals_combine_labelled_results() -> None:
    totals = _aggregate_totals(
        [
            {
                "elapsed_seconds": 2.0,
                "python_lines": 2000,
                "label_stats": {
                    "coverage": {"candidates": 10, "labelled": 5, "unlabelled": 5},
                    "aggregate": {
                        "candidates": 10,
                        "labelled": 5,
                        "true_findings": 4,
                        "false_positives": 1,
                        "precision": 0.8,
                    },
                },
            },
            {"elapsed_seconds": 1.0, "python_lines": 3000, "label_stats": None},
        ]
    )
    assert totals["labelled_projects"] == 1
    assert totals["candidates"] == 10
    assert totals["true_findings"] == 4
    assert totals["precision"] == 0.8
    assert totals["review_burden"] == 2.5
    assert totals["candidates_per_kloc"] == 2.0
    assert totals["runtime_per_kloc"] == 0.6


def test_mutation_recall_reports_partial_detection() -> None:
    recall = _recall({"deadcode": {"DEAD": 2}}, {"deadcode": {"DEAD": 5}})
    assert recall["deadcode.DEAD"] == {
        "expected": 5,
        "detected": 2,
        "recall": 0.4,
    }


def test_mutation_corpus_reaches_full_recall() -> None:
    detected, expected = run_corpus()
    leaves = _recall(detected, expected)
    assert all(entry["recall"] == 1.0 for entry in leaves.values())


def test_label_files_load_and_match_manifest_projects() -> None:
    manifest = load_manifest(Path("benchmarks/manifest.json"))
    labels_dir = Path("benchmarks/labels")
    by_id = {project["id"]: project for project in manifest["projects"]}
    assert {path.stem for path in labels_dir.glob("*.json")} == set(by_id)
    for project_id, project in by_id.items():
        payload = load_labels(labels_dir / f"{project_id}.json")
        assert payload["project_id"] == project_id
        assert payload["commit"] == project["commit"]


def test_benchmark_metrics_summarize_scanner_payloads() -> None:
    report = {
        "elapsed_seconds": 1.25,
        "scanners": {
            "deadcode": {
                "candidates": [{"path": "dead.py"}],
                "totals": {"DEAD": 1, "PUBLIC_API_CANDIDATE": 2},
            },
            "duplicates": {"clusters": [{"id": "a"}]},
            "forks": {
                "pairs": [{"left": {}, "right": {}}],
                "small_function_pairs": [{"left": {}, "right": {}}],
            },
            "contracts": {"counts": {"x": 2, "y": 1}},
            "capabilities": {"overlap": [{"local": {}}]},
            "hardcoded": {"hits": {"one": [{"path": "x.py"}]}},
        },
    }
    assert _metrics(report) == {
        "dead": 1,
        "public_api_candidates": 2,
        "duplicate_clusters": 1,
        "fork_pairs": 1,
        "small_fork_pairs": 1,
        "contract_candidates": 3,
        "capability_overlap": 1,
        "hardcoded_hits": 1,
        "elapsed_seconds": 1.25,
    }


def test_benchmark_dry_run_is_network_free(tmp_path: Path) -> None:
    manifest = load_manifest(Path("benchmarks/manifest.json"))
    payload = run_benchmarks(
        manifest,
        workspace=tmp_path / "workspace",
        output=tmp_path / "results.json",
        selected={"requests"},
        dry_run=True,
    )
    assert payload["results"][0]["id"] == "requests"
    assert payload["results"][0]["status"] == "planned"
    assert "--all-py" in payload["results"][0]["command"]
    assert not (tmp_path / "results.json").exists()
