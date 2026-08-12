#!/usr/bin/env python3
"""Run every scanner over the synthetic mutation corpus and report recall.

The corpus under ``mutation/project`` injects one known issue per channel
(dead module, duplicated function, env write without read, ``strict=False``,
generation path without env variable, capability overlap).  ``expected.json``
lists the exact detection counts; this runner compares the scanners' output
against them and exits nonzero when any channel misses expected detections.
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


def _detected(report: dict[str, Any]) -> dict[str, Any]:
    scanners = report["scanners"]
    dead = scanners["deadcode"]
    return {
        "deadcode": {
            "DEAD": sum(
                1 for item in dead.get("candidates", []) if item["status"] == "DEAD"
            )
        },
        "duplicates": {
            "clusters": len(scanners.get("duplicates", {}).get("clusters", []))
        },
        "forks": {
            "small_function_pairs": len(
                scanners.get("forks", {}).get("small_function_pairs", [])
            )
        },
        "contracts": {
            channel: len(scanners.get("contracts", {}).get(channel, []))
            for channel in CONTRACT_CHANNELS
        },
        "capabilities": {
            "overlap": len(scanners.get("capabilities", {}).get("overlap", []))
        },
    }


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
    detected = _detected(report)
    expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))["expected"]
    return detected, expected


def _leaf_paths(value: dict[str, Any], prefix: str = "") -> list[tuple[str, int]]:
    paths: list[tuple[str, int]] = []
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict):
            paths.extend(_leaf_paths(item, path))
        else:
            paths.append((path, int(item)))
    return paths


def _recall(detected: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    leaves: dict[str, Any] = {}
    detected_leaves = dict(_leaf_paths(detected))
    expected_leaves = dict(_leaf_paths(expected))
    for path, wanted in sorted(expected_leaves.items()):
        found = detected_leaves.get(path, 0)
        leaves[path] = {
            "expected": wanted,
            "detected": found,
            "recall": round(min(1.0, found / wanted), 3) if wanted else 1.0,
        }
    return leaves


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=None, help="write recall JSON")
    args = parser.parse_args(argv)
    try:
        detected, expected = run_corpus()
    except (OSError, RuntimeError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        print(f"error: mutation corpus failed: {exc}", file=sys.stderr)
        return 2
    leaves = _recall(detected, expected)
    total_expected = sum(entry["expected"] for entry in leaves.values())
    total_detected = sum(entry["detected"] for entry in leaves.values())
    total_recall = round(min(1.0, total_detected / total_expected), 3) if total_expected else 1.0
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "detected": detected,
                    "recall": leaves,
                    "total_recall": total_recall,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    ok = True
    for path, entry in leaves.items():
        print(
            f"MUTATION {path} {entry['detected']}/{entry['expected']} "
            f"recall={entry['recall']:.3f}"
        )
        if entry["detected"] < entry["expected"]:
            ok = False
    print(
        f"MUTATION TOTAL {total_detected}/{total_expected} recall={total_recall:.3f}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
