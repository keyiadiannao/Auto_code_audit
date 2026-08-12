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
DEFAULT_WORKSPACE = Path(tempfile.gettempdir()) / "auto-code-audit-benchmarks"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "results" / "latest.json"


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


def _metrics(report: dict[str, Any]) -> dict[str, Any]:
    scanners = report.get("scanners", {})
    contracts = scanners.get("contracts", {})
    return {
        "dead": len(scanners.get("deadcode", {}).get("candidates", [])),
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
        "elapsed_seconds": report.get("elapsed_seconds"),
    }


def _run_project(
    project: dict[str, Any],
    repo: Path,
    output_dir: Path,
    *,
    timeout: int,
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
    result["report"] = str(report_path)
    return result


def run_benchmarks(
    manifest: dict[str, Any],
    *,
    workspace: Path,
    output: Path,
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
            results.append({"id": project["id"], "status": "planned", "command": command})
            continue
        repo = _checkout(project, workspace, refresh=refresh)
        package_root = repo / project["package"]
        if not package_root.is_dir():
            raise FileNotFoundError(f"benchmark package does not exist: {package_root}")
        result = _run_project(project, repo, output.parent, timeout=timeout)
        result["commit"] = _git(repo, "rev-parse", "HEAD")
        result["python_files"] = len(list(package_root.rglob("*.py")))
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
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
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
            selected=set(args.project) or None,
            refresh=args.refresh,
            timeout=args.timeout,
            dry_run=args.dry_run,
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: benchmark failed: {exc}", file=sys.stderr)
        return 2
    for result in payload["results"]:
        print(f"BENCHMARK {result['id']} {result['status']}")
    return 0 if all(result["status"] in {"pass", "planned"} for result in payload["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
