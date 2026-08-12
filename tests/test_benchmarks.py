from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.run_benchmarks import (
    _metrics,
    load_manifest,
    run_benchmarks,
)


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


def test_benchmark_metrics_summarize_scanner_payloads() -> None:
    report = {
        "elapsed_seconds": 1.25,
        "scanners": {
            "deadcode": {"candidates": [{"path": "dead.py"}]},
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
