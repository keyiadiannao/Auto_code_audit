#!/usr/bin/env python3
"""Run every code scanner over the synthetic mutation corpus and report recall.

The corpus under ``mutation/project`` injects one known defect per channel
(dead module, duplicated function, env write without read, ``strict=False``,
generation path without env variable, capability overlap, manual SHA-256).
``expected.json`` lists the exact stable ``(scanner, target_id)`` signature
each mutant must produce. Recall is the intersection of expected targets with
the detected target set, so a scanner that misses an injected defect cannot
pass by finding an unrelated candidate with the same count.

Covered channels: deadcode, duplicates, forks, contracts (all sub-channels),
capabilities, hardcoded, regions (shared / helper_not_reused / short_risky).
The exit code is nonzero when any expected target is missing.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

TOOL_ROOT = Path(__file__).resolve().parents[1]
CORPUS = Path(__file__).resolve().parent / "mutation" / "project"
EXPECTED_PATH = Path(__file__).resolve().parent / "mutation" / "expected.json"

CONTRACT_CHANNELS = (
    "experiment_as_library",
    "experiment_path_hacks",
    "forwarding_wrappers",
    "same_name_contracts",
    "unreferenced_top_level_functions",
    "cli_without_bootstrap",
    "defensive_param_loosening",
    "env_written_not_read",
    "generation_path_without_env",
)


def _detected_targets(report: dict[str, Any]) -> set[tuple[str, str]]:
    """Flatten the report to the same (scanner, target_id) keys labels use."""
    from run_all import _candidate_signatures

    signatures = _candidate_signatures(report.get("scanners", {}))
    return {
        (scanner, signature)
        for scanner, items in signatures.items()
        for signature, _, _ in items
    }


def _channel_counts(report: dict[str, Any]) -> dict[str, int]:
    """Secondary per-channel candidate counts (informational only)."""
    scanners = report["scanners"]
    dead = scanners["deadcode"]
    counts: dict[str, int] = {
        "deadcode.DEAD": sum(
            1 for item in dead.get("candidates", []) if item["status"] == "DEAD"
        ),
        "duplicates.clusters": len(scanners.get("duplicates", {}).get("clusters", [])),
        "forks.pairs": len(scanners.get("forks", {}).get("pairs", [])),
        "forks.small_function_pairs": len(
            scanners.get("forks", {}).get("small_function_pairs", [])
        ),
        "capabilities.overlap": len(
            scanners.get("capabilities", {}).get("overlap", [])
        ),
        "hardcoded.hits": sum(
            len(items)
            for items in scanners.get("hardcoded", {}).get("hits", {}).values()
        ),
    }
    for channel in CONTRACT_CHANNELS:
        counts[f"contracts.{channel}"] = len(
            scanners.get("contracts", {}).get(channel, [])
        )
    return counts


def run_corpus() -> tuple[dict[str, Any], dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="mutation-") as temporary:
        tempdir = Path(temporary)
        command = [
            sys.executable,
            str(TOOL_ROOT / "run_all.py"),
            "--root",
            str(CORPUS),
            "--package",
            "pkg",
            "--profile",
            "code",
            "--no-doc-channel",
            "--json",
            str(tempdir / "report.json"),
            "--markdown",
            str(tempdir / "report.md"),
        ]
        completed = subprocess.run(
            command,
            cwd=TOOL_ROOT,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(
                f"mutation run_all failed ({completed.returncode}): {detail}"
            )
        report = json.loads((tempdir / "report.json").read_text(encoding="utf-8"))
    expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    return report, expected


def _recall(
    detected: set[tuple[str, str]], expected: dict[str, Any]
) -> dict[str, Any]:
    findings = expected["expected_findings"]
    matched: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for entry in findings:
        key = (entry["scanner"], entry["target_id"])
        record = {
            "scanner": entry["scanner"],
            "target_id": entry["target_id"],
            "defect": entry.get("defect", ""),
        }
        if key in detected:
            matched.append(record)
        else:
            missing.append(record)
    total = len(findings)
    return {
        "total": total,
        "matched": len(matched),
        "missing": missing,
        "recall": round(len(matched) / total, 3) if total else 1.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=None, help="write recall JSON")
    args = parser.parse_args(argv)
    try:
        report, expected = run_corpus()
    except (OSError, RuntimeError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"error: mutation corpus failed: {exc}", file=sys.stderr)
        return 2

    detected = _detected_targets(report)
    recall = _recall(detected, expected)
    counts = _channel_counts(report)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "recall": recall,
                    "channel_counts": counts,
                    "detected_targets": sorted(detected),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    for entry in recall["missing"]:
        print(
            f"MUTATION MISS {entry['scanner']} {entry['target_id']} "
            f"({entry['defect']})"
        )
    for scanner, count in sorted(counts.items()):
        print(f"MUTATION {scanner} count={count}")
    print(
        f"MUTATION TOTAL targets={recall['total']} matched={recall['matched']} "
        f"recall={recall['recall']:.3f}"
    )
    return 0 if not recall["missing"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
