#!/usr/bin/env python3
"""Evidence-fusion experiment: does multi-scanner corroboration raise
issue-level precision?

An issue is a distinct defect.  The label registry groups candidates into
issues via ``issue_id`` (every true finding carries one; every other
candidate is its own issue).  An issue is *corroborated* when it carries
2+ candidate signals — ideally from 2+ scanners (e.g. a ``duplicates``
cluster confirmed by a ``regions`` twin for the same function pair).

The observed gap — single-signal issue precision of a few percent versus
corroborated-issue precision at 100% in the current corpus — is the
quantified case for fusing candidates into IssueBundles before
adjudication: judging the bundle once gives the adjudicator strictly more
evidence than judging its candidates independently.

Construction caveat (printed with every run): ``issue_id`` was assigned to
true findings during adjudication, so corroborated issues in the label set
are enriched by construction.  This pipeline (a) measures the gap and
(b) emits the per-issue evidence table a future LLM adjudicator will
consume; the gap itself is only validated by adjudicating unlabelled
bundles.

Fusion is conservative by design: no heuristic merging of candidates into
issues.  Bundles come only from explicit ``issue_id`` groups; everything
else stays a single-signal issue.

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
from run_all import _candidate_signatures


def _issue_bundles(report: dict, labels: dict) -> list[dict[str, Any]]:
    """Group matched labels into issues; a label without an issue_id is its
    own issue.  Issues with no candidate reproduced in this run are dropped
    (stale labels are not evidence)."""
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
    stats = {
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
    ap.add_argument("--json", type=Path, default=None, help="write issue table here")
    args = ap.parse_args(argv)

    bundles: list[dict[str, Any]] = []
    for path in sorted(args.results_dir.glob("*.json")):
        if path.name in {"latest.json", "mutation_check.json"}:
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        labels_path = args.labels_dir / f"{path.stem}.json"
        if not labels_path.is_file():
            continue
        labels = load_labels(labels_path)
        project_bundles = _issue_bundles(report, labels)
        for bundle in project_bundles:
            bundle["project"] = path.stem
        bundles.extend(project_bundles)

    stats = _summarize(bundles)
    print("ISSUE BUNDLES", json.dumps(stats))
    by_channel = Counter(len(bundle["channels"]) for bundle in bundles)
    print("channels-per-issue:", dict(sorted(by_channel.items())))
    true_bundles = [bundle for bundle in bundles if bundle["true"]]
    corroborated_true = [bundle for bundle in true_bundles if bundle["signals"] >= 2]
    print(
        f"true issues corroborated: {len(corroborated_true)}/{len(true_bundles)} "
        f"(signals across scanners: "
        f"{sorted(set(len(b['channels']) for b in corroborated_true))})"
    )
    for bundle in sorted(true_bundles, key=lambda b: (-b["signals"], b["issue_id"])):
        print(
            f"  [{bundle['signals']} sig / {len(bundle['channels'])} ch] "
            f"{bundle['project']} {bundle['issue_id']}: "
            f"{', '.join(m['scanner'] + '/' + m['target_id'] for m in bundle['candidates'])}"
        )
    print(
        "caveat: issue_ids were assigned to true findings during adjudication, "
        "so corroborated issues are enriched by construction; the gap is "
        "validated only by adjudicating unlabelled bundles"
    )

    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {"stats": stats, "issues": bundles}, indent=2, ensure_ascii=False
            ),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
