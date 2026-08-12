#!/usr/bin/env python3
"""Build the gold ground-truth subset manifest from the label corpus.

The gold subset is the trust anchor for comparing an AI adjudicator against
the human labels: a small, stable, reproducible selection where every
verdict is uncontroversial.  Selection is deterministic — re-running this
script with the same label files reproduces the manifest byte for byte.

Composition (per the review requirement):

* every true finding (16 at the pinned commits) — the positives a candidate
  adjudicator must not miss;
* a stratified false-positive sample: per-project quota by largest
  remainder over FP counts, then per-scanner quota (minimum one) with
  stride sampling over target ids so scanner channels and reason variety
  stay represented;
* boundary cases are marked, not filtered: false positives whose reason
  mentions near-miss signals (token-level matches, boilerplate, dunders,
  overload stubs, coincidental similarity) are tagged ``boundary: true``
  so a candidate adjudicator can be scored on the hard cases separately.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

from benchmarks.run_benchmarks import DEFAULT_LABELS_DIR, load_labels

TARGET_SIZE = 100
BOUNDARY_WORDS = (
    "token-level",
    "boilerplate",
    "dunder",
    "overload",
    "coinciden",
    "near-",
    "almost",
    "unrelated",
)


def _stride_sample(items: list[dict[str, str]], quota: int) -> list[dict[str, str]]:
    """Deterministically spread a quota across a sorted list: take every
    k-th element so the sample covers the group's variety."""
    if len(items) <= quota:
        return list(items)
    step = len(items) / quota
    return [items[math.floor(i * step)] for i in range(quota)]


def _project_fp_quotas(fp_counts: dict[str, int], target: int) -> dict[str, int]:
    """Largest-remainder proportional allocation of ``target`` seats."""
    total = sum(fp_counts.values())
    quotas: dict[str, int] = {}
    remainders: list[tuple[float, str]] = []
    for project, count in fp_counts.items():
        exact = count / total * target
        quotas[project] = math.floor(exact)
        remainders.append((exact - math.floor(exact), project))
    remaining = target - sum(quotas.values())
    for _, project in sorted(remainders, reverse=True)[:remaining]:
        quotas[project] += 1
    return quotas


def _scanner_quotas(counts: dict[str, int], quota: int) -> dict[str, int]:
    """Per-scanner seats with a minimum of one per non-empty scanner.

    A scanner that emitted candidates must be represented in the sample even
    when its share of the project quota would floor to zero."""
    if not counts:
        return {}
    quotas: dict[str, int] = {scanner: 1 for scanner in counts}
    remaining = quota - len(counts)
    if remaining <= 0:
        return quotas
    total = sum(counts.values())
    floors: dict[str, int] = {
        scanner: math.floor(count / total * remaining)
        for scanner, count in counts.items()
    }
    used = sum(floors.values())
    for _, scanner in sorted(
        (
            (count / total * remaining - floors[scanner], scanner)
            for scanner, count in counts.items()
        ),
        reverse=True,
    )[: remaining - used]:
        floors[scanner] += 1
    for scanner in counts:
        quotas[scanner] += floors[scanner]
    return quotas


def build(labels_dir: Path) -> dict[str, Any]:
    by_project = {
        path.stem: load_labels(path) for path in sorted(labels_dir.glob("*.json"))
    }
    entries: list[dict[str, Any]] = []

    for project, payload in sorted(by_project.items()):
        labels = payload["labels"]
        for entry in sorted(
            (e for e in labels if e["label"] == "true_finding"),
            key=lambda e: (e["scanner"], e["target_id"]),
        ):
            entries.append(
                {
                    "project": project,
                    "scanner": entry["scanner"],
                    "target_id": entry["target_id"],
                    "gold_label": "true_finding",
                    "issue_id": entry.get("issue_id"),
                    "cohort": "true_finding",
                    "boundary": False,
                    "reason": entry["reason"],
                }
            )

    fp_counts = {
        project: sum(e["label"] == "false_positive" for e in payload["labels"])
        for project, payload in by_project.items()
    }
    fp_target = TARGET_SIZE - len(entries)
    for project, quota in _project_fp_quotas(fp_counts, fp_target).items():
        labels = by_project[project]["labels"]
        fps = sorted(
            (e for e in labels if e["label"] == "false_positive"),
            key=lambda e: (e["scanner"], e["target_id"]),
        )
        by_scanner: dict[str, list[dict[str, str]]] = {}
        for entry in fps:
            by_scanner.setdefault(entry["scanner"], []).append(entry)
        scanner_quotas = _scanner_quotas(
            {s: len(group) for s, group in by_scanner.items()}, quota
        )
        for scanner in sorted(by_scanner):
            for entry in _stride_sample(by_scanner[scanner], scanner_quotas[scanner]):
                entries.append(
                    {
                        "project": project,
                        "scanner": scanner,
                        "target_id": entry["target_id"],
                        "gold_label": "false_positive",
                        "issue_id": entry.get("issue_id"),
                        "cohort": "false_positive",
                        "boundary": any(
                            word in entry["reason"] for word in BOUNDARY_WORDS
                        ),
                        "reason": entry["reason"],
                    }
                )

    entries.sort(key=lambda e: (e["project"], e["scanner"], e["target_id"]))
    true = sum(1 for e in entries if e["gold_label"] == "true_finding")
    boundary = sum(1 for e in entries if e["boundary"])
    return {
        "schema_version": 1,
        "purpose": (
            "curated subset of the adjudicated corpus used as the trust "
            "anchor for AI adjudicator comparisons; every entry reproduces "
            "a human label"
        ),
        "selection": {
            "all_true_findings": True,
            "false_positive_sampling": (
                "per-project largest-remainder quotas, per-scanner stride "
                "sampling, deterministic"
            ),
            "boundary_tagging": "near-miss reason keywords",
        },
        "counts": {
            "entries": len(entries),
            "true_findings": true,
            "false_positives": len(entries) - true,
            "boundary": boundary,
        },
        "entries": entries,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--labels-dir",
        default=DEFAULT_LABELS_DIR,
        type=Path,
        help="directory of per-project label files",
    )
    ap.add_argument(
        "--out",
        default=Path(__file__).resolve().parent / "gold_manifest.json",
        type=Path,
        help="output manifest path",
    )
    args = ap.parse_args(argv)

    manifest = build(args.labels_dir)
    args.out.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    counts = manifest["counts"]
    print(
        f"gold entries={counts['entries']} true={counts['true_findings']} "
        f"fp={counts['false_positives']} boundary={counts['boundary']}"
    )
    by_project: dict[str, int] = {}
    for entry in manifest["entries"]:
        by_project[entry["project"]] = by_project.get(entry["project"], 0) + 1
    print("per project:", dict(sorted(by_project.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
