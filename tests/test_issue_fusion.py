from __future__ import annotations

from issue_fusion import cluster_issue_bundles, issue_summary


def _cluster(identifier: str, *symbols: tuple[str, str]) -> dict:
    return {
        "id": identifier,
        "members": [
            {"path": path, "qualname": qualname} for path, qualname in symbols
        ],
    }


def test_exact_cross_scanner_symbol_sets_form_one_issue() -> None:
    scanners = {
        "duplicates": {
            "clusters": [_cluster("dup", ("pkg/a.py", "parse"), ("pkg/b.py", "parse"))]
        },
        "regions": {
            "clusters": [_cluster("reg", ("pkg/a.py", "parse"), ("pkg/b.py", "parse"))]
        },
    }
    bundles = cluster_issue_bundles(scanners)
    assert len(bundles) == 1
    assert bundles[0]["channels"] == ["duplicates", "regions"]
    assert bundles[0]["signals"] == 2
    assert bundles[0]["candidates"] == [
        {"scanner": "duplicates", "target_id": "cluster/dup"},
        {"scanner": "regions", "target_id": "region/reg"},
    ]
    summary = issue_summary(bundles)
    assert summary["candidate_signals"] == 2
    assert summary["issue_count"] == 1
    assert summary["corroborated_issues"] == 1
    assert summary["compression_ratio"] == 2.0


def test_overlap_without_exact_equality_stays_separate() -> None:
    scanners = {
        "duplicates": {
            "clusters": [_cluster("dup", ("pkg/a.py", "parse"), ("pkg/b.py", "parse"))]
        },
        "regions": {
            "clusters": [_cluster("reg", ("pkg/a.py", "parse"), ("pkg/c.py", "parse"))]
        },
    }
    bundles = cluster_issue_bundles(scanners)
    assert len(bundles) == 2
    assert all(bundle["signals"] == 1 for bundle in bundles)


def test_keep_filter_controls_review_issue_scope() -> None:
    scanners = {
        "duplicates": {
            "clusters": [
                {**_cluster("keep", ("pkg/a.py", "parse"), ("pkg/b.py", "parse")), "max_sim": 1.0},
                {**_cluster("hide", ("pkg/c.py", "load"), ("pkg/d.py", "load")), "max_sim": 0.8},
            ]
        },
        "regions": {"clusters": []},
    }
    bundles = cluster_issue_bundles(
        scanners, keep=lambda scanner, detail: detail.get("max_sim", 0) >= 0.98
    )
    assert [bundle["issue_id"] for bundle in bundles] == [
        "duplicates/cluster/keep"
    ]


def test_dynamic_runtime_contract_is_a_unified_issue() -> None:
    scanners = {
        "duplicates": {"clusters": []},
        "regions": {"clusters": []},
        "contracts": {
            "dynamic_module_runtime_coupling": [
                {
                    "path": "scripts/runner.py",
                    "priority": "high",
                    "kind": "dynamic_module_state_mutation",
                }
            ]
        },
    }
    assert cluster_issue_bundles(scanners) == [
        {
            "issue_id": "contracts/dynamic_runtime/scripts/runner.py",
            "candidates": [
                {
                    "scanner": "contracts",
                    "target_id": "dynamic_runtime/scripts/runner.py",
                }
            ],
            "channels": ["contracts"],
            "signals": 1,
            "member_symbols": ["scripts/runner.py"],
        }
    ]
