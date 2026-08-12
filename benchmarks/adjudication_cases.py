#!/usr/bin/env python3
"""Shared adjudication-case construction: evidence trimming, code snippet
extraction, and deterministic evidence hashing.

Used by both the benchmark runner (``run_adjudication.py``) and the corpus
builder (``build_corpus.py``) so that evidence hashes agree exactly: a
corpus entry is bound to the precise evidence it was reviewed against, and
any change in evidence (or pinned commit, or candidate record) invalidates
the hash.
"""
from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

SNIPPET_MAX_LINES = 30
SNIPPET_MAX_FILES = 4
EVIDENCE_TRIM_KEYS = {"divergence"}


def _trim_evidence(detail: dict[str, Any]) -> dict[str, Any]:
    trimmed: dict[str, Any] = {}
    for key, value in detail.items():
        if key in EVIDENCE_TRIM_KEYS:
            continue
        if isinstance(value, list) and len(value) > 8:
            value = value[:8]
        trimmed[key] = value
    return trimmed


def _snippet_locations(detail: dict[str, Any]) -> list[tuple[str, str | None, int | None]]:
    """Resolve (relpath, qualname, fallback_line) for code extraction.

    Handles every scanner's candidate record shape: duplicate cluster
    members, fork left/right, contract channels, dead modules, hardcoded
    hits, and capability pairs.
    """
    locations: list[tuple[str, str | None, int | None]] = []
    if "members" in detail:
        for member in detail["members"]:
            locations.append((member["path"], member.get("qualname"), None))
    elif "left" in detail and "right" in detail:
        for side in ("left", "right"):
            locations.append(
                (detail[side]["path"], detail[side].get("qualname"), None)
            )
    elif "local" in detail:
        locations.append(
            (detail["local"].get("path", ""), detail["local"].get("qualname"), None)
        )
    else:
        path = detail.get("path")
        if isinstance(path, str):
            name = detail.get("name") or detail.get("module")
            locations.append((path, name, detail.get("line") or detail.get("lineno")))
    return [loc for loc in locations if loc[0]]


def _locate_qualname(node: ast.AST, qualname: str) -> tuple[int, int] | None:
    """Find (lineno, end_lineno) of a dotted qualname in an AST."""
    parts = qualname.split(".")

    def walk(root: ast.AST, depth: int) -> tuple[int, int] | None:
        if depth >= len(parts):
            return None
        for child in ast.iter_child_nodes(root):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name == parts[depth]:
                    if depth == len(parts) - 1:
                        return (child.lineno, child.end_lineno or child.lineno)
                    found = walk(child, depth + 1)
                    if found:
                        return found
            elif isinstance(child, ast.ClassDef) and child.name == parts[depth]:
                found = walk(child, depth + 1)
                if found:
                    return found
        return None

    return walk(node, 0)


def _extract_snippets(
    package_root: Path,
    detail: dict[str, Any],
) -> list[dict[str, Any]]:
    snippets: list[dict[str, Any]] = []
    for relpath, qualname, fallback_line in _snippet_locations(detail)[:SNIPPET_MAX_FILES]:
        source = package_root / relpath
        if not source.is_file():
            snippets.append({"path": relpath, "error": "file not found"})
            continue
        try:
            lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            snippets.append({"path": relpath, "error": str(exc)})
            continue
        start: int | None = None
        if qualname:
            try:
                module = ast.parse("\n".join(lines))
                start_end = _locate_qualname(module, qualname)
                if start_end:
                    start = start_end[0]
            except SyntaxError:
                start = None
        if start is None:
            start = fallback_line
        if start is None:
            snippets.append(
                {"path": relpath, "qualname": qualname, "error": "location not found"}
            )
            continue
        window = lines[start - 1 : start - 1 + SNIPPET_MAX_LINES]
        snippets.append(
            {
                "path": relpath,
                "qualname": qualname,
                "start_line": start,
                "code": "".join(f"L{start + i}: {text}\n" for i, text in enumerate(window)),
            }
        )
    return snippets


def build_case(
    project_id: str,
    commit: str,
    scanner: str,
    target_id: str,
    display: str,
    detail: dict[str, Any],
    package_root: Path,
) -> dict[str, Any]:
    """Build a protocol-shaped case bundle with its deterministic digest."""
    from benchmarks.adjudication_protocol import CASE_SCHEMA_VERSION, canonical_case

    bundle: dict[str, Any] = {
        "case_schema_version": CASE_SCHEMA_VERSION,
        "project_id": project_id,
        "commit": commit,
        "scanner": scanner,
        "target_id": target_id,
        "display": display,
        "evidence": _trim_evidence(detail),
        "snippets": _extract_snippets(package_root, detail),
    }
    digest = hashlib.sha256(
        json.dumps(canonical_case(bundle), sort_keys=True, ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()
    bundle["evidence_hash"] = digest
    return bundle