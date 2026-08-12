from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.evidence_fusion import _issue_bundles, _summarize, main as fusion_main
from benchmarks.run_benchmarks import (
    _aggregate_totals,
    _label_stats,
    _metrics,
    _python_lines,
    load_labels,
    load_manifest,
    run_benchmarks,
)
from benchmarks.run_mutation import _detected_targets, _recall, run_corpus


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
                "issue_id": "unused-module",
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
                "issue_id": "unused-module",
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
        "unique_issues": 1,
    }
    assert stats["unmatched_labels"] == ["deadcode/DEAD/lib/zzz.py"]
    assert stats["per_scanner"]["duplicates"]["precision"] == 1.0
    assert stats["per_scanner"]["forks"]["labelled"] == 0
    # cross-scanner issue grouping: the matched duplicates and deadcode true
    # findings share one issue_id, so two candidates collapse into one issue;
    # the stale true label is its own issue but is not reproduced.
    assert stats["issues"] == {
        "unused-module": {
            "true_candidates": [
                "deadcode/DEAD/lib/a.py",
                "duplicates/cluster/abc123",
            ],
            "matched": [
                "deadcode/DEAD/lib/a.py",
                "duplicates/cluster/abc123",
            ],
        },
        "deadcode/DEAD/lib/zzz.py": {
            "true_candidates": ["deadcode/DEAD/lib/zzz.py"],
        },
    }


def _fusion_report() -> dict:
    """Minimal payloads; ``_candidate_signatures`` reads duplicates
    ``clusters[].id`` and regions ``clusters[].id`` only."""
    return {
        "scanners": {
            "duplicates": {
                "clusters": [
                    {"id": "abc123", "priority": "high"},
                    {"id": "def456", "priority": "low"},
                ]
            },
            "regions": {
                "clusters": [
                    {"id": "abc123", "priority": "high", "kind": "shared_capability"},
                ]
            },
        }
    }


def _fusion_labels() -> dict:
    return {
        "schema_version": 1,
        "project_id": "toy",
        "commit": "0" * 40,
        "labels": [
            {
                "scanner": "duplicates",
                "target_id": "cluster/abc123",
                "label": "true_finding",
                "issue_id": "twin-pair",
                "reason": "identical implementation",
            },
            {
                "scanner": "regions",
                "target_id": "region/abc123",
                "label": "true_finding",
                "issue_id": "twin-pair",
                "reason": "region twin of the duplicate pair",
            },
            {
                "scanner": "duplicates",
                "target_id": "cluster/def456",
                "label": "false_positive",
                "reason": "coincidental structural similarity",
            },
            {
                "scanner": "regions",
                "target_id": "region/nothere",
                "label": "true_finding",
                "issue_id": "stale-issue",
                "reason": "stale label, candidate not reproduced",
            },
        ],
    }


def test_evidence_fusion_bundles_shared_issue_signals() -> None:
    bundles = _issue_bundles(_fusion_report(), _fusion_labels())
    by_id = {bundle["issue_id"]: bundle for bundle in bundles}
    twin = by_id["twin-pair"]
    assert twin["signals"] == 2
    assert twin["channels"] == ["duplicates", "regions"]
    assert twin["true"] is True
    single = by_id["duplicates/cluster/def456"]
    assert single["signals"] == 1
    assert single["channels"] == ["duplicates"]
    assert single["true"] is False
    # A stale label whose candidate is not reproduced contributes no issue.
    assert "stale-issue" not in by_id


def test_evidence_fusion_summarize_precision_gap() -> None:
    bundles = [
        {"issue_id": "a", "signals": 1, "channels": ["duplicates"], "true": True},
        {"issue_id": "b", "signals": 1, "channels": ["duplicates"], "true": False},
        {"issue_id": "c", "signals": 2, "channels": ["duplicates", "regions"], "true": True},
    ]
    stats = _summarize(bundles)
    assert stats["issues"] == 3
    assert stats["single_signal_precision"] == 0.5
    assert stats["corroborated_precision"] == 1.0
    assert stats["issues_per_finding"] == 1.5


def test_evidence_fusion_cli_writes_issue_table(tmp_path: Path) -> None:
    results = tmp_path / "results"
    labels = tmp_path / "labels"
    results.mkdir()
    labels.mkdir()
    (results / "toy.json").write_text(
        json.dumps(_fusion_report()), encoding="utf-8"
    )
    (labels / "toy.json").write_text(
        json.dumps(_fusion_labels()), encoding="utf-8"
    )
    out = tmp_path / "issues.json"
    rc = fusion_main(
        [
            "--results-dir",
            str(results),
            "--labels-dir",
            str(labels),
            "--json",
            str(out),
        ]
    )
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["stats"]["issues"] == 2
    assert payload["stats"]["single_signal_precision"] == 0.0
    assert payload["stats"]["corroborated_precision"] == 1.0
    assert len(payload["issues"]) == 2


def test_label_stats_counts_matched_issues_only(tmp_path: Path) -> None:
    """An issue whose labels are all stale (no current candidate) must not
    inflate unique_issues: it was adjudicated once but is not reproduced by
    this run."""
    labels = _toy_labels()
    labels["labels"].append(
        {
            "scanner": "deadcode",
            "target_id": "DEAD/lib/zzz.py",
            "label": "true_finding",
            "issue_id": "unused-module",
            "reason": "stale twin of the matched finding",
        }
    )
    stats = _label_stats(_toy_report(), labels)
    assert stats["aggregate"]["unique_issues"] == 1
    assert stats["issues"]["unused-module"]["true_candidates"] == [
        "deadcode/DEAD/lib/a.py",
        "duplicates/cluster/abc123",
        "deadcode/DEAD/lib/zzz.py",
    ]
    assert stats["issues"]["unused-module"]["matched"] == [
        "deadcode/DEAD/lib/a.py",
        "duplicates/cluster/abc123",
    ]


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


def test_load_labels_rejects_bad_issue_id(tmp_path: Path) -> None:
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
                        "issue_id": "",
                        "reason": "empty issue id",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="issue_id must be a non-empty string"):
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
                        "unique_issues": 3,
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
    assert totals["unique_issues"] == 3
    assert totals["unique_issue_ratio"] == 0.75
    assert totals["evidence_per_issue"] == 1.33
    assert totals["candidates_per_kloc"] == 2.0
    assert totals["runtime_per_kloc"] == 0.6


def test_mutation_recall_reports_missing_targets() -> None:
    detected = {("deadcode", "DEAD/lib/dead.py"), ("duplicates", "cluster/abc")}
    expected = {
        "expected_findings": [
            {"scanner": "deadcode", "target_id": "DEAD/lib/dead.py", "defect": "a"},
            {"scanner": "deadcode", "target_id": "DEAD/lib/gone.py", "defect": "b"},
            {"scanner": "duplicates", "target_id": "cluster/abc", "defect": "c"},
        ]
    }
    recall = _recall(detected, expected)
    assert recall["total"] == 3
    assert recall["matched"] == 2
    assert recall["recall"] == 0.667
    assert [entry["target_id"] for entry in recall["missing"]] == [
        "DEAD/lib/gone.py"
    ]


def test_mutation_corpus_reaches_full_target_recall() -> None:
    report, expected = run_corpus()
    recall = _recall(_detected_targets(report), expected)
    assert recall["matched"] == recall["total"]
    assert not recall["missing"]
    assert recall["recall"] == 1.0


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
            "regions": {
                "clusters": [
                    {"kind": "helper_not_reused", "id": "h1"},
                    {"kind": "shared_capability", "id": "s1"},
                    {"kind": "shared_capability", "id": "s2", "short_block_cluster": True},
                ]
            },
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
        "region_clusters": 3,
        "region_helper_not_reused": 1,
        "region_shared_capability": 2,
        "region_short_block": 1,
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
