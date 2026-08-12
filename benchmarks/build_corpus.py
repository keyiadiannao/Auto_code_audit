#!/usr/bin/env python3
"""Seed and refresh the human-reviewed adjudication corpus.

The 355 existing benchmark labels were produced by AI sessions and cannot
be used as ground truth without review.  This tool selects a small,
balanced, deterministic working set -- every known ``true_finding`` plus an
evenly spaced sample of ``false_positive`` covers per scanner -- and writes
it to ``benchmarks/adjudication/corpus/<project>.json``.

Corpus entries carry review metadata: ``human_verified`` (default False),
``reviewers``, ``ground_truth_version`` and a ``note``.  Re-running the
tool only adds/removes unverified selections; review fields on existing
entries are preserved.  Entries whose ``evidence_hash`` no longer matches
the current evidence are dropped and reported (stale truth).

The runner refuses unverified entries unless ``--include-unverified`` is
given, so the metric only ever reflects reviewed ground truth.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

DEFAULT_LABELS_DIR = Path(__file__).resolve().parent / "labels"
DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / "results"
DEFAULT_CORPUS_DIR = Path(__file__).resolve().parent / "adjudication" / "corpus"
DEFAULT_TARGET_TOTAL = 80

REVIEW_KEYS = ("human_verified", "reviewers", "ground_truth_version", "note")


def _load_report(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_labels_for(labels_dir: Path, project_id: str) -> dict[str, Any] | None:
    from benchmarks.run_benchmarks import _load_labels_for as load

    return load(labels_dir, project_id)


def _existing_entries(corpus_path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    if not corpus_path.is_file():
        return {}
    payload = json.loads(corpus_path.read_text(encoding="utf-8"))
    return {
        (entry.get("scanner", ""), entry.get("target_id", "")): entry
        for entry in payload.get("entries", [])
        if entry.get("scanner") and entry.get("target_id")
    }


def _even_sample(items: list[str], cap: int) -> list[str]:
    """Deterministic evenly spaced sample of sorted target ids."""
    if len(items) <= cap:
        return items
    step = len(items) / cap
    return [items[int(round(index * step))] for index in range(cap)]


def build_corpus(
    *,
    labels_dir: Path,
    results_dir: Path,
    workspace: Path,
    corpus_dir: Path,
    target_total: int = DEFAULT_TARGET_TOTAL,
    projects: set[str] | None = None,
    scanners: set[str] | None = None,
) -> dict[str, Any]:
    """Select corpus entries per project and merge with reviewed state."""
    from run_all import _candidate_signatures
    from benchmarks.adjudication_cases import build_case
    from benchmarks.run_benchmarks import load_manifest

    manifest = load_manifest(Path(__file__).resolve().parent / "manifest.json")
    reports: list[tuple[str, Path]] = []
    for report_path in sorted(results_dir.glob("*.json")):
        if report_path.name == "latest.json":
            continue
        project_id = report_path.stem
        if projects and project_id not in projects:
            continue
        reports.append((project_id, report_path))

    total_positives = 0
    stats_by_project: dict[str, dict[str, Any]] = {}
    for project_id, report_file in reports:
        labels = _load_labels_for(labels_dir, project_id)
        if labels is None:
            continue
        report = _load_report(report_file)
        package_root = workspace / project_id / _package_rel(
            manifest, project_id
        )
        if not package_root.is_dir():
            continue
        by_scanner: dict[str, dict[str, Any]] = {}
        signatures = _candidate_signatures(report.get("scanners", {}))
        for scanner, items in signatures.items():
            if scanners and scanner not in scanners:
                continue
            grouped: dict[str, Any] = {"pos": [], "neg": []}
            for signature, display, detail in items:
                label = _label_for(labels, scanner, signature)
                if label == "true_finding":
                    grouped["pos"].append(signature)
                elif label == "false_positive":
                    grouped["neg"].append(signature)
            if grouped["pos"] or grouped["neg"]:
                by_scanner[scanner] = grouped
        stats_by_project[project_id] = {
            "pos_candidates": sum(len(g["pos"]) for g in by_scanner.values()),
            "neg_candidates": sum(len(g["neg"]) for g in by_scanner.values()),
            "scanners": sorted(by_scanner),
            "by_scanner": by_scanner,
        }
        total_positives += stats_by_project[project_id]["pos_candidates"]

    budget_left = max(0, target_total - total_positives)
    total_negatives = sum(
        stats["neg_candidates"] for stats in stats_by_project.values()
    )
    summary: dict[str, Any] = {
        "schema_version": 1,
        "target_total": target_total,
        "positives": total_positives,
        "negative_budget": budget_left,
        "projects": [],
    }
    corpus_dir.mkdir(parents=True, exist_ok=True)
    for project_id, report_file in reports:
        stats = stats_by_project.get(project_id)
        if not stats:
            continue
        labels = _load_labels_for(labels_dir, project_id)
        assert labels is not None
        report = _load_report(report_file)
        package_root = workspace / project_id / _package_rel(
            manifest, project_id
        )
        assert package_root.is_dir()
        commit = report.get("commit") or labels.get("commit", "")
        neg_budget = (
            round(budget_left * stats["neg_candidates"] / total_negatives)
            if total_negatives
            else 0
        )
        corpus_path = corpus_dir / f"{project_id}.json"
        existing = _existing_entries(corpus_path)
        entries: list[dict[str, Any]] = []
        kept_reviewed = 0
        dropped_stale: list[str] = []
        seen: set[tuple[str, str]] = set()
        signatures = _candidate_signatures(report.get("scanners", {}))
        for scanner, grouped in stats["by_scanner"].items():
            for signature in sorted(grouped["pos"]) + _even_sample(
                sorted(grouped["neg"]),
                max(1, round(neg_budget * len(grouped["neg"]) / max(1, stats["neg_candidates"]))),
            ):
                key = (scanner, signature)
                seen.add(key)
                items = signatures.get(scanner, [])
                detail = next(
                    (item[2] for item in items if item[0] == signature), {}
                )
                bundle = build_case(
                    project_id,
                    commit,
                    scanner,
                    signature,
                    _display_for(items, signature),
                    detail,
                    package_root,
                )
                digest = bundle["evidence_hash"]
                old = existing.get(key) or {}
                if old.get("evidence_hash") and old["evidence_hash"] != digest:
                    dropped_stale.append(f"{project_id}/{scanner}/{signature}")
                    old = {}
                entry = {
                    "scanner": scanner,
                    "target_id": signature,
                    "label": _label_for(labels, scanner, signature),
                    "evidence_hash": digest,
                    "human_verified": bool(old.get("human_verified", False)),
                    "reviewers": list(old.get("reviewers", [])),
                    "ground_truth_version": old.get("ground_truth_version", 1),
                    "note": old.get("note", ""),
                }
                if entry["human_verified"]:
                    kept_reviewed += 1
                entries.append(entry)
        for key, old in existing.items():
            if key not in seen and old.get("human_verified"):
                entries.append(
                    {
                        "scanner": key[0],
                        "target_id": key[1],
                        "label": old["label"],
                        "evidence_hash": old["evidence_hash"],
                        "human_verified": True,
                        "reviewers": list(old.get("reviewers", [])),
                        "ground_truth_version": old.get("ground_truth_version", 1),
                        "note": old.get("note", ""),
                    }
                )
        entries.sort(key=lambda entry: (entry["scanner"], entry["target_id"]))
        payload = {
            "schema_version": 1,
            "project_id": project_id,
            "commit": commit,
            "entries": entries,
        }
        corpus_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        summary["projects"].append(
            {
                "project_id": project_id,
                "entries": len(entries),
                "verified": sum(1 for entry in entries if entry["human_verified"]),
                "dropped_stale": dropped_stale,
            }
        )
    return summary


def _package_rel(manifest: dict[str, Any], project_id: str) -> str:
    for project in manifest["projects"]:
        if project["id"] == project_id:
            return str(project["package"])
    raise KeyError(project_id)


def _label_for(labels: dict[str, Any], scanner: str, target_id: str) -> str:
    for entry in labels.get("labels", []):
        if entry["scanner"] == scanner and entry["target_id"] == target_id:
            return entry["label"]
    return ""


def _display_for(items: list[tuple[str, str, dict]], target_id: str) -> str:
    for signature, display, _ in items:
        if signature == target_id:
            return display
    return target_id


def _default_workspace() -> Path:
    env = os.environ.get("AUDIT_BENCH_WORKSPACE")
    if env:
        return Path(env)
    from benchmarks.run_benchmarks import DEFAULT_WORKSPACE

    default = Path(DEFAULT_WORKSPACE)
    fallback = Path(os.environ.get("TEMP", ".")) / "opencode" / "bench_ws"
    return default if default.is_dir() else fallback


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels-dir", type=Path, default=DEFAULT_LABELS_DIR)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=DEFAULT_CORPUS_DIR,
        help="corpus output directory",
    )
    parser.add_argument("--target-total", type=int, default=DEFAULT_TARGET_TOTAL)
    parser.add_argument("--project", action="append", default=[])
    parser.add_argument("--scanner", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        summary = build_corpus(
            labels_dir=args.labels_dir.resolve(),
            results_dir=args.results_dir.resolve(),
            workspace=(args.workspace or _default_workspace()).resolve(),
            corpus_dir=args.corpus_dir.resolve(),
            target_total=args.target_total,
            projects=set(args.project) or None,
            scanners=set(args.scanner) or None,
        )
    except (OSError, ValueError, json.JSONDecodeError, KeyError) as exc:
        print(f"error: corpus build failed: {exc}", file=sys.stderr)
        return 2
    for project in summary["projects"]:
        print(
            f"CORPUS {project['project_id']} entries={project['entries']} "
            f"verified={project['verified']} "
            f"stale={len(project['dropped_stale'])}"
        )
        for target in project["dropped_stale"]:
            print(f"CORPUS STALE {target}")
    print(
        f"CORPUS TOTALS positives={summary['positives']} "
        f"negative_budget={summary['negative_budget']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())