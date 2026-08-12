#!/usr/bin/env python3
"""Run fixed-commit, read-only audits over the benchmark manifest."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


TOOL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = Path(__file__).resolve().parent / "manifest.json"
DEFAULT_LABELS_DIR = Path(__file__).resolve().parent / "labels"
DEFAULT_WORKSPACE = Path(tempfile.gettempdir()) / "auto-code-audit-benchmarks"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "results" / "latest.json"

LABEL_VALUES = {"true_finding", "false_positive"}


def load_labels(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError(f"label file schema_version must be 1: {path}")
    project_id = payload.get("project_id")
    commit = payload.get("commit")
    labels = payload.get("labels")
    if not isinstance(project_id, str) or not project_id:
        raise ValueError(f"label file project_id is required: {path}")
    if not isinstance(commit, str) or len(commit) != 40 or any(
        char not in "0123456789abcdef" for char in commit.lower()
    ):
        raise ValueError(f"label file commit must be a 40-char SHA: {path}")
    if not isinstance(labels, list) or not labels:
        raise ValueError(f"label file labels must be a non-empty list: {path}")
    seen: set[tuple[str, str]] = set()
    for entry in labels:
        if entry.get("label") not in LABEL_VALUES:
            raise ValueError(
                f"label must be one of {sorted(LABEL_VALUES)}: {path}"
            )
        scanner = entry.get("scanner")
        target_id = entry.get("target_id")
        if not isinstance(scanner, str) or not isinstance(target_id, str):
            raise ValueError(f"label needs scanner and target_id: {path}")
        if not entry.get("reason"):
            raise ValueError(f"label needs a reason: {path}")
        if (scanner, target_id) in seen:
            raise ValueError(f"duplicated label {scanner}/{target_id}: {path}")
        seen.add((scanner, target_id))
    return payload


def _load_labels_for(labels_dir: Path, project_id: str) -> dict[str, Any] | None:
    path = labels_dir / f"{project_id}.json"
    if not path.is_file():
        return None
    return load_labels(path)


def _run(command: list[str], *, cwd: Path | None = None, timeout: int = 120) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{detail}"
        )
    return result.stdout.strip()


def _git(repo: Path, *args: str, timeout: int = 120) -> str:
    return _run(["git", *args], cwd=repo, timeout=timeout)


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("benchmark manifest schema_version must be 1")
    projects = payload.get("projects")
    if not isinstance(projects, list) or not projects:
        raise ValueError("benchmark manifest projects must be a non-empty list")

    seen: set[str] = set()
    for project in projects:
        if not isinstance(project, dict):
            raise ValueError("each benchmark project must be an object")
        required = {"id", "url", "commit", "package", "license"}
        missing = required - set(project)
        if missing:
            raise ValueError(f"benchmark project missing keys: {sorted(missing)}")
        project_id = project["id"]
        if not isinstance(project_id, str) or not project_id or project_id in seen:
            raise ValueError(f"benchmark project id is missing or duplicated: {project_id!r}")
        seen.add(project_id)
        if not isinstance(project["url"], str) or not project["url"].startswith("https://"):
            raise ValueError(f"benchmark {project_id} url must be HTTPS")
        commit = project["commit"]
        if not isinstance(commit, str) or len(commit) != 40 or any(
            char not in "0123456789abcdef" for char in commit.lower()
        ):
            raise ValueError(f"benchmark {project_id} commit must be a 40-char SHA")
        package = Path(project["package"])
        if package.is_absolute() or ".." in package.parts:
            raise ValueError(f"benchmark {project_id} package escapes repository")
        if not isinstance(project["license"], str) or not project["license"]:
            raise ValueError(f"benchmark {project_id} license is required")
        if not isinstance(project.get("all_py", True), bool):
            raise ValueError(f"benchmark {project_id} all_py must be boolean")
    return payload


def _checkout(project: dict[str, Any], workspace: Path, *, refresh: bool) -> Path:
    destination = workspace / project["id"]
    if destination.exists():
        if _git(destination, "rev-parse", "HEAD") == project["commit"]:
            return destination
        if not refresh:
            raise RuntimeError(
                f"benchmark checkout exists at a different commit: {destination}; "
                "use --refresh or choose another workspace"
            )
        shutil.rmtree(destination)

    destination.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "git",
            "clone",
            "--no-checkout",
            "--depth",
            "1",
            "--no-tags",
            project["url"],
            str(destination),
        ],
        timeout=900,
    )
    _git(destination, "fetch", "--depth", "1", "origin", project["commit"], timeout=900)
    _git(destination, "checkout", "--detach", project["commit"])
    return destination


def _license_path(repo: Path, project: dict[str, Any]) -> str | None:
    candidates = [
        repo / name
        for name in ("LICENSE", "LICENSE.txt", "LICENSE.md", "COPYING")
    ]
    for path in candidates:
        if path.is_file():
            return path.relative_to(repo).as_posix()
    return None


def _label_stats(
    report: dict[str, Any], labels: dict[str, Any]
) -> dict[str, Any]:
    """Compute labelled-candidate precision and coverage for one project.

    Precision is measured only over candidates that carry a ground-truth
    label; ``coverage`` reports how many of the emitted candidates remain
    unlabelled.  Labels whose target_id matches no current candidate are
    reported as ``unmatched_labels`` (stale or out-of-scope labels).
    """
    from run_all import _candidate_signatures

    candidates = _candidate_signatures(report.get("scanners", {}))
    labels_by_scanner: dict[str, dict[str, str]] = {}
    for entry in labels["labels"]:
        labels_by_scanner.setdefault(entry["scanner"], {})[
            entry["target_id"]
        ] = entry["label"]

    per_scanner: dict[str, Any] = {}
    total_candidates = 0
    total_labelled = 0
    total_true = 0
    total_false = 0
    for scanner, items in candidates.items():
        mapping = labels_by_scanner.get(scanner, {})
        true = false = 0
        for signature, _, _ in items:
            if signature in mapping:
                if mapping[signature] == "true_finding":
                    true += 1
                else:
                    false += 1
        total_candidates += len(items)
        total_labelled += true + false
        total_true += true
        total_false += false
        per_scanner[scanner] = {
            "candidates": len(items),
            "labelled": true + false,
            "true_findings": true,
            "false_positives": false,
            "precision": round(true / (true + false), 3) if true + false else None,
        }

    unmatched: list[str] = []
    for scanner, mapping in labels_by_scanner.items():
        known = {signature for signature, _, _ in candidates.get(scanner, [])}
        for target_id in mapping:
            if target_id not in known:
                unmatched.append(f"{scanner}/{target_id}")

    return {
        "coverage": {
            "candidates": total_candidates,
            "labelled": total_labelled,
            "unlabelled": total_candidates - total_labelled,
        },
        "per_scanner": per_scanner,
        "aggregate": {
            "candidates": total_candidates,
            "labelled": total_labelled,
            "true_findings": total_true,
            "false_positives": total_false,
            "precision": round(total_true / total_labelled, 3)
            if total_labelled
            else None,
        },
        "unmatched_labels": sorted(unmatched),
    }


def _python_lines(package_root: Path) -> int:
    total = 0
    for path in package_root.rglob("*.py"):
        try:
            total += sum(1 for _ in path.open("r", encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return total


def _aggregate_totals(results: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, Any] = {
        "labelled_projects": 0,
        "candidates": 0,
        "labelled": 0,
        "true_findings": 0,
        "false_positives": 0,
        "python_lines": 0,
        "elapsed_seconds": 0.0,
    }
    for result in results:
        stats = result.get("label_stats")
        if stats:
            totals["labelled_projects"] += 1
            coverage = stats["coverage"]
            aggregate = stats["aggregate"]
            totals["candidates"] += coverage["candidates"]
            totals["labelled"] += coverage["labelled"]
            totals["true_findings"] += aggregate["true_findings"]
            totals["false_positives"] += aggregate["false_positives"]
        totals["python_lines"] += result.get("python_lines", 0)
        totals["elapsed_seconds"] += result.get("elapsed_seconds", 0)
    labelled = totals["labelled"]
    totals["precision"] = round(totals["true_findings"] / labelled, 3) if labelled else None
    true_findings = totals["true_findings"]
    totals["review_burden"] = round(totals["candidates"] / true_findings, 1) if true_findings else None
    kloc = max(0.001, totals["python_lines"] / 1000.0)
    totals["candidates_per_kloc"] = round(totals["candidates"] / kloc, 2)
    totals["runtime_per_kloc"] = round(totals["elapsed_seconds"] / kloc, 3)
    return totals


def _metrics(report: dict[str, Any]) -> dict[str, Any]:
    scanners = report.get("scanners", {})
    contracts = scanners.get("contracts", {})
    deadcode = scanners.get("deadcode", {})
    totals = deadcode.get("totals", {})
    region_clusters = scanners.get("regions", {}).get("clusters", [])
    return {
        "dead": totals.get("DEAD", 0),
        "public_api_candidates": totals.get("PUBLIC_API_CANDIDATE", 0),
        "duplicate_clusters": len(scanners.get("duplicates", {}).get("clusters", [])),
        "fork_pairs": len(scanners.get("forks", {}).get("pairs", [])),
        "small_fork_pairs": len(
            scanners.get("forks", {}).get("small_function_pairs", [])
        ),
        "contract_candidates": sum(contracts.get("counts", {}).values()),
        "capability_overlap": len(scanners.get("capabilities", {}).get("overlap", [])),
        "hardcoded_hits": sum(
            len(items) for items in scanners.get("hardcoded", {}).get("hits", {}).values()
        ),
        "region_clusters": len(region_clusters),
        "region_helper_not_reused": sum(
            1 for cluster in region_clusters if cluster.get("kind") == "helper_not_reused"
        ),
        "region_shared_capability": sum(
            1
            for cluster in region_clusters
            if cluster.get("kind") == "shared_capability"
        ),
        "region_short_block": sum(
            1 for cluster in region_clusters if cluster.get("short_block_cluster")
        ),
        "elapsed_seconds": report.get("elapsed_seconds"),
    }


def _run_project(
    project: dict[str, Any],
    repo: Path,
    output_dir: Path,
    *,
    timeout: int,
    labels: dict[str, Any] | None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"{project['id']}.json"
    markdown_path = output_dir / f"{project['id']}.md"
    log_path = output_dir / f"{project['id']}.log"
    command = [
        sys.executable,
        str(TOOL_ROOT / "run_all.py"),
        "--root",
        str(repo),
        "--package",
        project["package"],
        "--profile",
        "code",
        "--no-doc-channel",
        "--json",
        str(report_path),
        "--markdown",
        str(markdown_path),
    ]
    if project.get("all_py", True):
        command.append("--all-py")
    if project.get("public_api", False):
        command.append("--public-api")
    started = time.perf_counter()
    try:
        with log_path.open("w", encoding="utf-8", newline="\n") as handle:
            completed = subprocess.run(
                command,
                cwd=TOOL_ROOT,
                stdout=handle,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
    except subprocess.TimeoutExpired:
        return {
            "id": project["id"],
            "status": "timeout",
            "command": command,
            "log": str(log_path),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }

    result: dict[str, Any] = {
        "id": project["id"],
        "status": "pass" if completed.returncode == 0 else "fail",
        "command": command,
        "log": str(log_path),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    if completed.returncode != 0:
        result["returncode"] = completed.returncode
        return result
    report = json.loads(report_path.read_text(encoding="utf-8"))
    result["metrics"] = _metrics(report)
    result["label_stats"] = (
        _label_stats(report, labels) if labels is not None else None
    )
    result["report"] = str(report_path)
    return result


def run_benchmarks(
    manifest: dict[str, Any],
    *,
    workspace: Path,
    output: Path,
    labels_dir: Path = DEFAULT_LABELS_DIR,
    selected: set[str] | None = None,
    refresh: bool = False,
    timeout: int = 900,
    dry_run: bool = False,
) -> dict[str, Any]:
    projects = [
        project for project in manifest["projects"]
        if selected is None or project["id"] in selected
    ]
    if selected and len(projects) != len(selected):
        known = {project["id"] for project in manifest["projects"]}
        raise ValueError(f"unknown benchmark project(s): {sorted(selected - known)}")

    results = []
    for project in projects:
        if dry_run:
            command = [
                sys.executable,
                str(TOOL_ROOT / "run_all.py"),
                "--root",
                str(workspace / project["id"]),
                "--package",
                project["package"],
                "--profile",
                "code",
                "--no-doc-channel",
            ]
            if project.get("all_py", True):
                command.append("--all-py")
            if project.get("public_api", False):
                command.append("--public-api")
            results.append({"id": project["id"], "status": "planned", "command": command})
            continue
        repo = _checkout(project, workspace, refresh=refresh)
        package_root = repo / project["package"]
        if not package_root.is_dir():
            raise FileNotFoundError(f"benchmark package does not exist: {package_root}")
        labels = _load_labels_for(labels_dir, project["id"])
        result = _run_project(
            project, repo, output.parent, timeout=timeout, labels=labels
        )
        result["commit"] = _git(repo, "rev-parse", "HEAD")
        result["python_files"] = len(list(package_root.rglob("*.py")))
        result["python_lines"] = _python_lines(package_root)
        result["license"] = project["license"]
        result["license_file"] = _license_path(repo, project)
        results.append(result)

    payload = {
        "schema_version": 1,
        "tool": "auto-code-audit",
        "profile": "code",
        "manifest": str(DEFAULT_MANIFEST),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results": results,
    }
    if not dry_run:
        payload["totals"] = _aggregate_totals(results)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--labels-dir", type=Path, default=DEFAULT_LABELS_DIR)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--project", action="append", default=[])
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest.resolve())
        payload = run_benchmarks(
            manifest,
            workspace=args.workspace.resolve(),
            output=args.output.resolve(),
            labels_dir=args.labels_dir.resolve(),
            selected=set(args.project) or None,
            refresh=args.refresh,
            timeout=args.timeout,
            dry_run=args.dry_run,
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: benchmark failed: {exc}", file=sys.stderr)
        return 2
    for result in payload["results"]:
        line = f"BENCHMARK {result['id']} {result['status']}"
        stats = result.get("label_stats")
        if stats:
            aggregate = stats["aggregate"]
            coverage = stats["coverage"]
            precision = (
                f"{aggregate['precision']:.3f}" if aggregate["precision"] is not None else "-"
            )
            line += (
                f" precision={precision} "
                f"labelled={coverage['labelled']}/{coverage['candidates']}"
            )
        print(line)
    totals = payload.get("totals")
    if totals:
        precision = totals["precision"]
        review_burden = totals["review_burden"]
        print(
            "TOTALS "
            f"precision={precision if precision is not None else '-'} "
            f"labelled={totals['labelled']}/{totals['candidates']} "
            f"review_burden={review_burden if review_burden is not None else '-'} "
            f"candidates_per_kloc={totals['candidates_per_kloc']} "
            f"runtime_per_kloc={totals['runtime_per_kloc']}s "
            f"labelled_projects={totals['labelled_projects']}"
        )
    return 0 if all(result["status"] in {"pass", "planned"} for result in payload["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
