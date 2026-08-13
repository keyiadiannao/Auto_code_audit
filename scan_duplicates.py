#!/usr/bin/env python3
"""Find structurally similar function implementations.

This scanner generates review candidates. It deliberately does not decide
whether two implementations have the same scientific semantics.
"""
from __future__ import annotations

import argparse
import ast
import copy
import datetime as dt
import hashlib
import json
import re
import sys
from collections import defaultdict
from typing import Any
from dataclasses import dataclass
from pathlib import Path

import _audit_config
from _scanner_common import (
    EXCLUDE_PARTS,
    PY_SUBDIRS,
    NormalizeAST as _Normalize,
    UnionFind as _UnionFind,
    iter_functions as _iter_functions,
    load_ignore,
    sequence_similarity as _similarity,
    short_hash as _short_hash,
    without_docstring as _without_docstring,
    write_json as _write_json,
)

COMMON_NAMES = {"require", "parse_args", "main", "close", "count"}


@dataclass(frozen=True)
class FunctionRecord:
    path: str
    name: str
    qualname: str
    is_top_level: bool
    nlines: int
    normalized: str
    tokens: tuple[str, ...]
    arg_count: int
    node_count: int
    top_signature: tuple[str, ...]
    definition_ordinal: int = 0

    @property
    def key(self) -> str:
        """Stable identity.  ``path:qualname`` is the symbol id; conditional
        or version-gated branches can define the same symbol twice, so later
        definitions disambiguate with a ``#N`` definition ordinal.  The
        first definition keeps the bare symbol so existing cluster ids and
        labels stay stable."""
        if self.definition_ordinal:
            return f"{self.path}:{self.qualname}#{self.definition_ordinal}"
        return f"{self.path}:{self.qualname}"


def _is_signature_stub(node: ast.AST) -> bool:
    """True for signature-only declarations whose body is exactly ``...``.

    ``@typing.overload`` stubs (and bare ``...`` placeholders) declare a
    signature but no implementation.  Keeping them in the index creates
    multiple records with the same ``path:qualname`` key, so distinct
    clusters can hash to the same ``_cluster_id``.
    """
    body = getattr(node, "body", None)
    return bool(
        body
        and len(body) == 1
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and body[0].value.value is Ellipsis
    )


def extract_functions(path: Path, min_chars: int) -> list[FunctionRecord]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    tree = ast.parse(text)

    records: list[FunctionRecord] = []
    ordinals: dict[str, int] = {}
    for node, parents in _iter_functions(tree):
        cloned = _without_docstring(node)
        if _is_signature_stub(cloned):
            continue
        qualname = ".".join(parents + (node.name,))
        ordinal = ordinals.get(qualname, 0)
        ordinals[qualname] = ordinal + 1
        normalized_node = _Normalize().visit(cloned)
        ast.fix_missing_locations(normalized_node)
        normalized = " ".join(ast.unparse(normalized_node).split())
        if len(normalized) < min_chars:
            continue
        tokens = tuple(
            re.findall(
                r"[A-Za-z_][A-Za-z0-9_]*|==|!=|<=|>=|:=|->|\*\*|//|<<|>>|[^\s]",
                normalized,
            )
        )
        end_lineno = getattr(node, "end_lineno", node.lineno)
        records.append(
            FunctionRecord(
                path="",
                name=node.name,
                qualname=qualname,
                is_top_level=not parents,
                nlines=end_lineno - node.lineno + 1,
                normalized=normalized,
                tokens=tokens,
                arg_count=len(node.args.posonlyargs)
                + len(node.args.args)
                + len(node.args.kwonlyargs),
                node_count=sum(1 for _ in ast.walk(cloned)),
                top_signature=tuple(type(x).__name__ for x in cloned.body[:3]),
                definition_ordinal=ordinal,
            )
        )
    return records


def _candidate_pairs(records: list[FunctionRecord]) -> set[tuple[int, int]]:
    """Block comparisons by shape and name instead of comparing every pair."""
    blocks: dict[tuple, list[int]] = defaultdict(list)
    for i, record in enumerate(records):
        node_bucket = record.node_count // 12
        for bucket in (node_bucket - 1, node_bucket, node_bucket + 1):
            blocks[
                ("shape", record.arg_count, record.top_signature, bucket)
            ].append(i)
        if record.name not in COMMON_NAMES:
            blocks[("name", record.name)].append(i)

    pairs: set[tuple[int, int]] = set()
    for members in blocks.values():
        unique = sorted(set(members))
        for pos, left in enumerate(unique):
            for right in unique[pos + 1 :]:
                pairs.add((left, right))
    return pairs


def _cluster_id(members: list[FunctionRecord]) -> str:
    return _short_hash(*sorted(member.key for member in members))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repo root (default: script's repo)",
    )
    ap.add_argument("--package", default="src")
    ap.add_argument("--subdirs", nargs="*", default=None)
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--min-chars", type=int, default=None)
    ap.add_argument("--skip-names", nargs="*", default=None)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--ignore", type=Path, default=None)
    args = ap.parse_args(argv)

    repo = args.root.resolve()
    pkg = (repo / args.package).resolve()
    if not pkg.is_dir() or pkg != repo / args.package:
        print(f"error: package dir not found or escapes root: {pkg}", file=sys.stderr)
        return 2

    cfg = _audit_config.load_config(repo)
    dup_cfg = cfg.get("duplicates", {})
    subdirs = _audit_config.pick(args.subdirs, cfg, "subdirs", list(PY_SUBDIRS))
    threshold = _audit_config.pick(args.threshold, dup_cfg, "threshold", 0.82)
    min_chars = _audit_config.pick(args.min_chars, dup_cfg, "min_chars", 120)
    skip_names = _audit_config.pick(
        args.skip_names, dup_cfg, "skip_names", sorted(COMMON_NAMES)
    )

    if not 0.0 <= threshold <= 1.0:
        print("error: --threshold must be in [0, 1]", file=sys.stderr)
        return 2
    if min_chars < 0:
        print("error: --min-chars must be non-negative", file=sys.stderr)
        return 2

    records: list[FunctionRecord] = []
    parse_failures: list[dict[str, str]] = []
    for sub in subdirs:
        subdir = pkg / sub
        if not subdir.is_dir():
            continue
        for path in sorted(subdir.rglob("*.py")):
            rel_parts = path.relative_to(pkg).parts
            if any(part in EXCLUDE_PARTS for part in rel_parts):
                continue
            try:
                extracted = extract_functions(path, min_chars)
            except (OSError, UnicodeError, SyntaxError) as exc:
                parse_failures.append(
                    {
                        "path": path.relative_to(pkg).as_posix(),
                        "error": str(exc),
                    }
                )
                continue
            rel = path.relative_to(pkg).as_posix()
            records.extend(
                FunctionRecord(
                    path=rel,
                    name=item.name,
                    qualname=item.qualname,
                    is_top_level=item.is_top_level,
                    nlines=item.nlines,
                    normalized=item.normalized,
                    tokens=item.tokens,
                    arg_count=item.arg_count,
                    node_count=item.node_count,
                    top_signature=item.top_signature,
                    definition_ordinal=item.definition_ordinal,
                )
                for item in extracted
            )

    union_find = _UnionFind(len(records))
    edge_similarity: dict[tuple[int, int], float] = {}

    exact: dict[str, list[int]] = defaultdict(list)
    for i, record in enumerate(records):
        digest = hashlib.sha256(record.normalized.encode("utf-8")).hexdigest()
        exact[digest].append(i)
    for exact_members in exact.values():
        for index in exact_members[1:]:
            union_find.union(exact_members[0], index)
            edge_similarity[(exact_members[0], index)] = 1.0

    candidate_pairs = _candidate_pairs(records)
    for left, right in sorted(candidate_pairs):
        if records[left].normalized == records[right].normalized:
            continue
        similarity = _similarity(
            records[left].tokens, records[right].tokens, threshold
        )
        if similarity >= threshold:
            union_find.union(left, right)
            edge_similarity[(left, right)] = similarity

    components: dict[int, list[int]] = defaultdict(list)
    for i in range(len(records)):
        components[union_find.find(i)].append(i)

    ignore = load_ignore(args.ignore)
    ignored_ids = {
        entry.get("id", "") for entry in ignore.get("duplicates", []) if entry.get("id")
    }
    legacy_paths = [
        entry.get("path", "")
        for entry in ignore.get("duplicates", [])
        if entry.get("path")
    ]

    reports: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    boilerplate_skipped = 0
    for indices in components.values():
        if len(indices) < 2:
            continue
        members = [records[i] for i in indices]
        if all(member.name in skip_names for member in members):
            boilerplate_skipped += 1
            continue
        cluster_id = _cluster_id(members)
        legacy_match = next(
            (
                token
                for token in legacy_paths
                if token and all(token in member.path for member in members)
            ),
            None,
        )
        if cluster_id in ignored_ids or legacy_match:
            ignored.append(
                {
                    "id": cluster_id,
                    "legacy_path": legacy_match,
                    "members": sorted(member.key for member in members),
                }
            )
            continue

        index_set = set(indices)
        component_edges = [
            score
            for (left, right), score in edge_similarity.items()
            if left in index_set and right in index_set
        ]
        lib_members = [
            member
            for member in members
            if member.path.startswith("lib/") and member.is_top_level
        ]
        non_lib_members = [
            member for member in members if not member.path.startswith("lib/")
        ]
        non_test_members = [
            member for member in non_lib_members if not member.path.startswith("tests/")
        ]
        max_lines = max(member.nlines for member in members)
        file_count = len({member.path for member in members})
        cross_file = file_count >= 2
        has_shared_candidate = bool(lib_members and non_test_members)
        if has_shared_candidate and max_lines >= 8:
            priority = "high"
            priority_reason = "shared lib member and an active non-lib implementation"
        elif cross_file and max_lines >= 20 and max(component_edges, default=1.0) >= 0.95:
            priority = "high"
            priority_reason = "large near-exact implementation repeated across files"
        elif cross_file and len(members) >= 5 and max_lines >= 10:
            priority = "high"
            priority_reason = "repeated implementation family with broad package spread"
        elif cross_file and max_lines >= 8:
            priority = "medium"
            priority_reason = "cross-file structural similarity requiring semantic review"
        else:
            priority = "low"
            priority_reason = "short, same-file, or test-adjacent structural similarity"
        reports.append(
            {
                "id": cluster_id,
                "priority": priority,
                "priority_reason": priority_reason,
                "size": len(members),
                "max_lines": max_lines,
                "file_count": file_count,
                "max_sim": round(max(component_edges, default=1.0), 4),
                "min_edge_sim": round(min(component_edges, default=1.0), 4),
                "names": sorted({member.name for member in members}),
                "members": [
                    {
                        "path": member.path,
                        "name": member.name,
                        "qualname": member.qualname,
                        "is_top_level": member.is_top_level,
                        "nlines": member.nlines,
                        **(
                            {"definition_ordinal": member.definition_ordinal}
                            if member.definition_ordinal
                            else {}
                        ),
                    }
                    for member in sorted(members, key=lambda item: item.key)
                ],
                "lib_shared": [
                    {
                        "path": member.path,
                        "name": member.name,
                        "qualname": member.qualname,
                    }
                    for member in sorted(lib_members, key=lambda item: item.key)
                ]
                if lib_members and non_lib_members
                else [],
            }
        )
    priority_order = {"high": 0, "medium": 1, "low": 2}
    reports.sort(
        key=lambda item: (
            priority_order[item["priority"]],
            -item["size"],
            -item["max_lines"],
            item["id"],
        )
    )

    payload = {
        "scanner": "duplicates",
        "schema_version": 1,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "package": args.package,
        "threshold": threshold,
        "min_chars": min_chars,
        "functions_scanned": len(records),
        "candidate_pairs": len(candidate_pairs),
        "clusters_reported": len(reports),
        "priority_counts": {
            level: sum(item["priority"] == level for item in reports)
            for level in ("high", "medium", "low")
        },
        "boilerplate_skipped": boilerplate_skipped,
        "parse_failures": parse_failures,
        "ignored": ignored,
        "clusters": reports,
    }
    _write_json(args.json, payload)

    print(
        f"DUP_SCAN package={args.package} functions={len(records)} "
        f"candidate_pairs={len(candidate_pairs)} clusters={len(reports)} "
        f"ignored={len(ignored)}"
    )
    for report in reports[:40]:
        tag = ""
        if report["lib_shared"]:
            tag = " [lib-shared: " + ", ".join(
                f"{item['name']}@{item['path']}" for item in report["lib_shared"]
            ) + "]"
        print(
            f"=== [{report['priority']}] {report['id']} {report['size']} members, "
            f"edge_sim={report['min_edge_sim']:.3f}-{report['max_sim']:.3f}{tag}"
        )
        for member in report["members"]:
            print(
                f"    {member['path']}:{member['qualname']} "
                f"({member['nlines']} lines)"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
