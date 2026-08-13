#!/usr/bin/env python3
"""Evidence-fusion benchmark: does multi-scanner corroboration raise
issue-level precision?

Two views are computed from the same per-project reports:

* **Proposed bundles (predictive, label-independent).**  Deterministic
  fusion runs *before* any ground truth is consulted: a ``duplicates``
  cluster and a ``regions`` cluster are merged into one proposed issue
  only when their member-symbol sets are *identical*
  (``{a.py:Foo, b.py:Bar}`` == ``{a.py:Foo, b.py:Bar}`` — Jaccard 1.0).
  Labels are joined afterwards, only to score the bundles.  This answers
  the product question directly: a fresh candidate corroborated by two
  scanners is a proposed bundle *before* anyone adjudicates it.

* **Retrospective oracle (reference).**  The previous grouping, which
  merges candidates by the ``issue_id`` assigned to true findings during
  adjudication.  Its corroborated precision is enriched by construction —
  it measures "how many confirmed issues carry multi-scanner evidence",
  not whether corroboration predicts truth.

Four scores are reported for the proposed view:

* ``bundle_precision`` — of the corroborated bundles (2+ channels), how
  many contain at least one ground-truth true finding;
* ``issue_recall`` — of the ground-truth issues with a reproduced
  candidate, how many were *correctly formed*: every reproduced candidate
  of the issue lands in exactly one proposed bundle with no other issue;
* ``fusion_purity`` — share of proposed bundles whose candidates come
  from at most one ground-truth issue (a bundle mixing two issues is a
  mis-merge);
* ``compression_ratio`` — candidates per proposed bundle, i.e. how many
  AI adjudication units the raw candidates collapse to.

Exit code 0 unless the inputs cannot be loaded.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from benchmarks.run_benchmarks import load_labels
from issue_fusion import cluster_issue_bundles
from run_all import _candidate_signatures

# Retrospective view ------------------------------------------------------------------


def _issue_bundles(report: dict, labels: dict) -> list[dict[str, Any]]:
    """Group matched labels into issues; a label without an issue_id is its
    own issue.  Issues with no candidate reproduced in this run are dropped
    (stale labels are not evidence).  This is the *retrospective* grouping:
    issue_id was assigned to true findings during adjudication, so bundles
    built this way are oracle-enriched by construction."""
    candidates = _candidate_signatures(report.get("scanners", {}))
    matched: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_target: dict[tuple[str, str], str] = {}
    for entry in labels["labels"]:
        issue = entry.get("issue_id") or f"{entry['scanner']}/{entry['target_id']}"
        by_target[(entry["scanner"], entry["target_id"])] = issue
    for scanner, items in candidates.items():
        for signature, _, _ in items:
            key = (scanner, signature)
            if key in by_target:
                matched[by_target[key]].append(
                    {"scanner": scanner, "target_id": signature}
                )
    bundles = []
    for issue, members in sorted(matched.items()):
        labels_by_target = {
            (entry["scanner"], entry["target_id"]): entry for entry in labels["labels"]
        }
        true = any(
            labels_by_target.get((m["scanner"], m["target_id"]), {}).get("label")
            == "true_finding"
            for m in members
        )
        bundles.append(
            {
                "issue_id": issue,
                "candidates": members,
                "channels": sorted({m["scanner"] for m in members}),
                "true": true,
                "signals": len(members),
            }
        )
    return bundles


def _summarize(bundles: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(bundles)
    true = sum(1 for bundle in bundles if bundle["true"])
    single = [bundle for bundle in bundles if bundle["signals"] == 1]
    corroborated = [bundle for bundle in bundles if bundle["signals"] >= 2]
    stats: dict[str, Any] = {
        "issues": total,
        "true_issues": true,
        "single_signal": len(single),
        "single_signal_true": sum(1 for bundle in single if bundle["true"]),
        "corroborated": len(corroborated),
        "corroborated_true": sum(1 for bundle in corroborated if bundle["true"]),
        "issues_per_finding": round(total / true, 2) if true else None,
    }
    stats["single_signal_precision"] = (
        round(stats["single_signal_true"] / len(single), 3) if single else None
    )
    stats["corroborated_precision"] = (
        round(stats["corroborated_true"] / len(corroborated), 3)
        if corroborated
        else None
    )
    return stats


# Proposed (predictive) view ----------------------------------------------------------

def _proposed_bundles(report: dict) -> list[dict[str, Any]]:
    """Label-independent deterministic fusion over the full candidate set.

    Two clusters from *different* channels are merged into one proposed issue
    only when their member-symbol sets are identical (conservative: exact
    sets, two or more members).  Every remaining candidate — single-channel
    clusters and all non-cluster candidates (deadcode, contracts, forks,
    capabilities, hardcoded, style) — is its own single bundle, so the
    proposed view covers every candidate the scanners emitted.  No label
    information is consulted anywhere in this function.
    """
    scanners = report.get("scanners", {})
    bundles = cluster_issue_bundles(scanners)
    for bundle in bundles:
        bundle.pop("member_symbols", None)
    # Every non-cluster candidate is its own single bundle.
    for scanner, items in _candidate_signatures(scanners).items():
        if scanner in {"duplicates", "regions"}:
            continue
        for signature, _, _ in items:
            bundles.append(
                {
                    "issue_id": f"{scanner}/{signature}",
                    "candidates": [{"scanner": scanner, "target_id": signature}],
                    "channels": [scanner],
                    "signals": 1,
                }
            )
    return sorted(bundles, key=lambda b: b["issue_id"])


def _score_bundles(bundles: list[dict[str, Any]], labels: dict) -> list[dict[str, Any]]:
    """Join ground truth onto proposed bundles: label each bundle with the
    ground-truth issues among its true candidates and whether any is true.

    False-positive candidates define no issue, so a bundle that fuses one
    true issue together with false candidates is impure only if it mixes two
    *true* issues; the false contamination is a precision matter, not a
    merge-quality one."""
    truth = {
        (entry["scanner"], entry["target_id"]): (
            entry["label"],
            entry.get("issue_id") or f"{entry['scanner']}/{entry['target_id']}",
        )
        for entry in labels["labels"]
    }
    for bundle in bundles:
        issues: set[str] = set()
        true = False
        for member in bundle["candidates"]:
            entry = truth.get((member["scanner"], member["target_id"]))
            if entry is None:
                continue
            if entry[0] == "true_finding":
                true = True
                issues.add(entry[1])
        bundle["ground_truth_issues"] = sorted(issues)
        bundle["true"] = true
    return bundles


def _proposed_stats(
    bundles: list[dict[str, Any]], labels_by_project: dict[str, dict]
) -> dict[str, Any]:
    """Corpus-wide scores for the proposed view."""
    corroborated = [b for b in bundles if len(b["channels"]) >= 2]
    single = [b for b in bundles if len(b["channels"]) == 1]
    stats: dict[str, Any] = {
        "bundles": len(bundles),
        "corroborated": len(corroborated),
        "corroborated_true": sum(1 for b in corroborated if b["true"]),
        "single": len(single),
        "single_true": sum(1 for b in single if b["true"]),
    }
    stats["corroborated_precision"] = (
        round(stats["corroborated_true"] / len(corroborated), 3)
        if corroborated
        else None
    )
    stats["single_precision"] = (
        round(stats["single_true"] / len(single), 3) if single else None
    )
    # issue recall: ground-truth issues with a reproduced candidate, formed
    # when all their reproduced candidates sit in one bundle with no other
    # issue.
    gt_issues: defaultdict[str, list[str]] = defaultdict(list)
    for project, labels in labels_by_project.items():
        for entry in labels["labels"]:
            if entry["label"] != "true_finding":
                continue
            issue = entry.get("issue_id") or (
                f"{entry['scanner']}/{entry['target_id']}"
            )
            gt_issues[issue].append(f"{entry['scanner']}/{entry['target_id']}")
    reproduced = {
        f"{b['candidates'][i]['scanner']}/{b['candidates'][i]['target_id']}": b
        for b in bundles
        for i in range(b["signals"])
    }
    formed = 0
    total_gt = 0
    for issue, candidates in gt_issues.items():
        present = [c for c in candidates if c in reproduced]
        if not present:
            continue
        total_gt += 1
        owning = {reproduced[c]["issue_id"] for c in present}
        if len(owning) != 1:
            continue
        if len(reproduced[present[0]]["ground_truth_issues"]) == 1:
            formed += 1
    stats["issue_recall"] = round(formed / total_gt, 3) if total_gt else None
    stats["ground_truth_issues"] = total_gt
    stats["issues_formed"] = formed
    # purity: share of bundles touching at most one ground-truth issue.
    pure = sum(1 for b in bundles if len(b["ground_truth_issues"]) <= 1)
    stats["fusion_purity"] = round(pure / len(bundles), 3) if bundles else None
    candidates = sum(b["signals"] for b in bundles)
    stats["compression_ratio"] = round(candidates / len(bundles), 2) if bundles else None
    stats["candidates"] = candidates
    return stats


# CLI ---------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--results-dir",
        default=Path("benchmarks") / "results",
        type=Path,
        help="directory of per-project benchmark reports",
    )
    ap.add_argument(
        "--labels-dir",
        default=Path("benchmarks") / "labels",
        type=Path,
        help="directory of per-project label files",
    )
    ap.add_argument("--json", type=Path, default=None, help="write bundle table here")
    args = ap.parse_args(argv)

    proposed: list[dict[str, Any]] = []
    retrospective: list[dict[str, Any]] = []
    labels_by_project: dict[str, dict] = {}
    for path in sorted(args.results_dir.glob("*.json")):
        if path.name in {"latest.json", "mutation_check.json"}:
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        labels_path = args.labels_dir / f"{path.stem}.json"
        if not labels_path.is_file():
            continue
        labels = load_labels(labels_path)
        labels_by_project[path.stem] = labels
        project_proposed = _score_bundles(
            _proposed_bundles(report), labels
        )
        for bundle in project_proposed:
            bundle["project"] = path.stem
        proposed.extend(project_proposed)
        for bundle in _issue_bundles(report, labels):
            bundle["project"] = path.stem
            retrospective.append(bundle)

    proposed_stats = _proposed_stats(proposed, labels_by_project)
    retro_stats = _summarize(retrospective)
    print("PROPOSED BUNDLES", json.dumps(proposed_stats))
    by_channel = Counter(len(bundle["channels"]) for bundle in proposed)
    print("channels-per-proposed-bundle:", dict(sorted(by_channel.items())))
    for bundle in sorted(
        (b for b in proposed if len(b["channels"]) >= 2),
        key=lambda b: b["issue_id"],
    ):
        print(
            f"  [{'/'.join(bundle['channels'])}] {bundle['project']} "
            f"{bundle['issue_id']}: true={bundle['true']} "
            f"issues={bundle['ground_truth_issues']}"
        )
    print("RETROSPECTIVE (oracle grouping)", json.dumps(retro_stats))
    print(
        "note: proposed bundles are label-independent; retrospective bundles "
        "merge by adjudication-time issue_id and are oracle-enriched by "
        "construction"
    )

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "stats": {
                        "proposed": proposed_stats,
                        "retrospective": retro_stats,
                    },
                    "bundles": proposed,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
