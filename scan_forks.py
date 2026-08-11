#!/usr/bin/env python3
"""Find script-to-script fork pairs: cross-file callables that share a large
common skeleton but have diverged bodies.

The existing scanners leave a middle ground uncovered:

* ``scan_duplicates`` clusters near-identical implementations (token
  similarity ~1.000); partial-divergence forks fall below its threshold.
* ``scan_capabilities`` matches local definitions against the lib capability
  index only; it is blind to one experiment script forking another.
* ``scan_contracts`` checks a fixed blacklist of contract-sensitive names.

This scanner indexes **every callable in the package** -- lib, experiments,
mechanism, audit, verify, figures -- with its docstring tag (first line), so
the label registry is not limited to the lib layer the project designed
itself.  It then compares every pair of callables in different files that
pass a size floor (default 40 lines) and reports those whose normalized
bodies share >= 75% of their tokens.  The result is a verdict item per pair:
deliberate fork to keep, parameterizable merge candidate, or true duplicate.

Each pair carries divergence hints -- signature-shape match, longest common
token run, the regions that diverged, and the identifiers unique to each
side -- so the reviewer can see what the fork changed without opening both
files.  The scanner never decides; it generates review candidates.

Same-file structural symmetry (e.g. symmetric experiment arms in one probe)
is intentionally out of scope: scan_duplicates + manual verdicts cover it.

For the "I just built a long script, what already exists in the package?"
workflow, run ``scan_forks.py --file experiments/eXX.py`` to list every
>= min-lines callable in that file and its cross-file relatives.
"""
from __future__ import annotations

import argparse
import ast
import copy
import datetime as dt
import difflib
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from scan_capabilities import _signature_shape
from scan_duplicates import _Normalize, load_ignore, _without_docstring

PY_SUBDIRS = (
    "lib",
    "experiments",
    "mechanism",
    "audit",
    "verify",
    "figures",
)
EXCLUDE_PARTS = {"frozen_source", "__pycache__"}
COMMON_NAMES = {"main", "parse_args", "require", "close", "count"}

_TOKEN_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*|==|!=|<=|>=|:=|->|\*\*|//|<<|>>|[^\s]"
)
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class CallableRecord:
    path: str
    name: str
    qualname: str
    lineno: int
    nlines: int
    tag: str  # docstring first line, empty when untagged
    sig_shape: str
    normalized: str
    tokens: tuple[str, ...]
    arg_count: int
    node_count: int
    top_signature: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.path}:{self.qualname}"


def extract_callables(path: Path) -> list[CallableRecord]:
    """Top-level, class-method, and nested callables with docstring tags."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    tree = ast.parse(text)

    def iter_functions(node: ast.AST, parents: tuple[str, ...] = ()):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield child, parents
                yield from iter_functions(child, parents + (child.name,))
            elif isinstance(child, ast.ClassDef):
                yield from iter_functions(child, parents + (child.name,))
            else:
                yield from iter_functions(child, parents)

    records: list[CallableRecord] = []
    for node, parents in iter_functions(tree):
        cloned = _without_docstring(node)
        normalized_node = _Normalize().visit(cloned)
        ast.fix_missing_locations(normalized_node)
        normalized = " ".join(ast.unparse(normalized_node).split())
        tokens = tuple(_TOKEN_RE.findall(normalized))
        end_lineno = getattr(node, "end_lineno", node.lineno)
        doc = ast.get_docstring(node)
        tag = doc.strip().splitlines()[0].strip() if doc else ""
        records.append(
            CallableRecord(
                path="",
                name=node.name,
                qualname=".".join(parents + (node.name,)),
                lineno=node.lineno,
                nlines=end_lineno - node.lineno + 1,
                tag=tag,
                sig_shape=_signature_shape(node),
                normalized=normalized,
                tokens=tokens,
                arg_count=len(node.args.posonlyargs)
                + len(node.args.args)
                + len(node.args.kwonlyargs),
                node_count=sum(1 for _ in ast.walk(cloned)),
                top_signature=tuple(type(x).__name__ for x in cloned.body[:3]),
            )
        )
    return records


def _candidate_pairs_small(
    records: list[CallableRecord], small_floor: int, small_ceil: int
) -> set[tuple[int, int]]:
    """Cross-file pairs for sub-``min_lines`` callables (LESSONS §14, blind
    spot C: the 40-line floor let byte-identical small helpers like the four
    ``_agg`` aggregators slip past the main channel).

    Size bands use ``nlines // 5 +- 2`` so small forks that grew or shrank by a
    couple of lines still land together; the name bucket catches same-name
    pairs whose sizes drifted apart.  Small bodies match on very short token
    sequences, so ``main`` applies a stricter similarity threshold on top.
    """
    blocks: dict[tuple, list[int]] = defaultdict(list)
    for i, record in enumerate(records):
        if not (small_floor <= record.nlines < small_ceil):
            continue
        band = record.nlines // 5
        for bucket in range(band - 2, band + 3):
            blocks[("small_size", bucket)].append(i)
        if record.name not in COMMON_NAMES:
            blocks[("small_name", record.name)].append(i)

    pairs: set[tuple[int, int]] = set()
    for members in blocks.values():
        unique = sorted(set(members))
        for pos, left in enumerate(unique):
            for right in unique[pos + 1 :]:
                if records[left].path != records[right].path:
                    pairs.add((left, right))
    return pairs


def _candidate_pairs(records: list[CallableRecord], min_lines: int) -> set[tuple[int, int]]:
    """Cross-file pairs sharing a size band or an exact name.

    Size bands use ``nlines // 10 +- 2`` so partially diverged forks (a fork
    usually grows or shrinks its body) still land in the same band.  The name
    bucket catches same-name pairs whose sizes drifted beyond the band.
    """
    blocks: dict[tuple, list[int]] = defaultdict(list)
    for i, record in enumerate(records):
        if record.nlines < min_lines:
            continue
        band = record.nlines // 10
        for bucket in range(band - 2, band + 3):
            blocks[("size", record.top_signature, bucket)].append(i)
        if record.name not in COMMON_NAMES:
            blocks[("name", record.name)].append(i)

    pairs: set[tuple[int, int]] = set()
    for members in blocks.values():
        unique = sorted(set(members))
        for pos, left in enumerate(unique):
            for right in unique[pos + 1 :]:
                if records[left].path != records[right].path:
                    pairs.add((left, right))
    return pairs


def _analyze_pair(
    left: CallableRecord, right: CallableRecord, threshold: float
) -> dict | None:
    """Similarity + divergence hints, or None below threshold."""
    shorter, longer = sorted((len(left.tokens), len(right.tokens)))
    if longer == 0 or shorter / longer < threshold:
        return None
    matcher = difflib.SequenceMatcher(None, left.tokens, right.tokens, autojunk=False)
    if matcher.real_quick_ratio() < threshold:
        return None
    if matcher.quick_ratio() < threshold:
        return None
    ratio = matcher.ratio()
    if ratio < threshold:
        return None
    equal_blocks = [
        b1 - b0
        for tag, _a0, _a1, b0, b1 in matcher.get_opcodes()
        if tag == "equal"
    ]
    divergences: list[dict] = []
    for tag, a0, a1, b0, b1 in matcher.get_opcodes():
        if tag != "equal":
            divergences.append({"left": [a0, a1], "right": [b0, b1]})
        if len(divergences) >= 3:
            break
    return {
        "similarity": ratio,
        "longest_match_tokens": max(equal_blocks, default=0),
        "divergence": divergences,
    }


def _unique_identifiers(record: CallableRecord) -> list[str]:
    """Surviving identifiers (attribute names, keyword args, call targets)
    not present in the other side -- the API surface the fork changed."""
    ids = {
        match.group()
        for match in _IDENT_RE.finditer(record.normalized)
        if match.group() not in ("_name", "_arg")
    }
    return sorted(ids)


def _record_dict(record: CallableRecord) -> dict:
    return {
        "path": record.path,
        "qualname": record.qualname,
        "lineno": record.lineno,
        "nlines": record.nlines,
        "tag": record.tag,
        "sig_shape": record.sig_shape,
    }


def _write_json(path: Path | None, payload: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repo root (default: script's repo)",
    )
    ap.add_argument("--package", default="src")
    ap.add_argument(
        "--subdirs",
        nargs="*",
        default=list(PY_SUBDIRS),
        help="package subdirs to index (default: all except tests)",
    )
    ap.add_argument("--include-tests", action="store_true")
    ap.add_argument("--threshold", type=float, default=0.75)
    ap.add_argument("--min-lines", type=int, default=40)
    ap.add_argument(
        "--small-floor",
        type=int,
        default=8,
        help="small-function channel lower bound on callable lines "
        "(blind spot C: sub-min-lines helpers with near-identical bodies)",
    )
    ap.add_argument(
        "--small-threshold",
        type=float,
        default=0.9,
        help="similarity threshold for the small-function channel (stricter "
        "than the main channel: short token sequences match easily)",
    )
    ap.add_argument(
        "--file",
        default=None,
        help="package-relative file whose callables to check against the "
             "whole-package index (e.g. src/experiments/run_experiment.py)",
    )
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--ignore", type=Path, default=None)
    ap.add_argument("--top", type=int, default=40, help="max fork pairs to print")
    args = ap.parse_args(argv)

    if not 0.0 <= args.threshold <= 1.0:
        print("error: --threshold must be in [0, 1]", file=sys.stderr)
        return 2
    if args.min_lines < 1:
        print("error: --min-lines must be positive", file=sys.stderr)
        return 2
    if not (1 <= args.small_floor < args.min_lines):
        print(
            "error: --small-floor must satisfy 1 <= small-floor < min-lines",
            file=sys.stderr,
        )
        return 2
    if not 0.0 <= args.small_threshold <= 1.0:
        print("error: --small-threshold must be in [0, 1]", file=sys.stderr)
        return 2

    repo = args.root.resolve()
    pkg = (repo / args.package).resolve()
    if not pkg.is_dir() or pkg != repo / args.package:
        print(f"error: package dir not found or escapes root: {pkg}", file=sys.stderr)
        return 2

    rel = None
    if args.file is not None:
        rel = Path(args.file).as_posix()
        path = (pkg / rel).resolve()
        if not path.is_file() or pkg not in path.parents:
            print(
                f"error: --file must be an existing file inside the package: {rel}",
                file=sys.stderr,
            )
            return 2

    subdirs = list(args.subdirs)
    if args.include_tests and "tests" not in subdirs:
        subdirs.append("tests")

    records: list[CallableRecord] = []
    parse_failures: list[dict] = []
    for sub in subdirs:
        subdir = pkg / sub
        if not subdir.is_dir():
            continue
        for path in sorted(subdir.rglob("*.py")):
            rel_parts = path.relative_to(pkg).parts
            if any(part in EXCLUDE_PARTS for part in rel_parts):
                continue
            try:
                extracted = extract_callables(path)
            except (OSError, UnicodeError, SyntaxError) as exc:
                parse_failures.append(
                    {"path": path.relative_to(pkg).as_posix(), "error": str(exc)}
                )
                continue
            rel_path = path.relative_to(pkg).as_posix()
            records.extend(
                CallableRecord(
                    path=rel_path,
                    name=item.name,
                    qualname=item.qualname,
                    lineno=item.lineno,
                    nlines=item.nlines,
                    tag=item.tag,
                    sig_shape=item.sig_shape,
                    normalized=item.normalized,
                    tokens=item.tokens,
                    arg_count=item.arg_count,
                    node_count=item.node_count,
                    top_signature=item.top_signature,
                )
                for item in extracted
            )

    candidate_pairs = _candidate_pairs(records, args.min_lines)

    ignore = load_ignore(args.ignore)
    ignored_keys = {
        entry.get("key", "")
        for entry in ignore.get("forks", [])
        if entry.get("key")
    }

    pairs: list[dict] = []
    ignored_pairs: list[dict] = []
    for left, right in sorted(candidate_pairs):
        a, b = records[left], records[right]
        analysis = _analyze_pair(a, b, args.threshold)
        if analysis is None:
            continue
        left_ids = _unique_identifiers(a)
        right_ids = _unique_identifiers(b)
        pair_key = "::".join(sorted((a.key, b.key)))
        left_only = sorted(set(left_ids) - set(right_ids))[:12]
        right_only = sorted(set(right_ids) - set(left_ids))[:12]
        if pair_key in ignored_keys:
            ignored_pairs.append({"key": pair_key, "similarity": analysis["similarity"]})
            continue
        record = {
            "key": pair_key,
            "similarity": round(analysis["similarity"], 4),
            "kind": "near_duplicate"
            if analysis["similarity"] >= 0.95
            else "fork",
            "name_match": a.name == b.name,
            "signature_match": a.sig_shape == b.sig_shape,
            "longest_match_tokens": analysis["longest_match_tokens"],
            "divergence": analysis["divergence"],
            "left_only_identifiers": left_only,
            "right_only_identifiers": right_only,
            "left": _record_dict(a),
            "right": _record_dict(b),
        }
        if rel is not None and a.path != rel and b.path != rel:
            continue
        pairs.append(record)
    pairs.sort(key=lambda item: (-item["similarity"], item["key"]))

    small_candidates = _candidate_pairs_small(records, args.small_floor, args.min_lines)
    small_pairs: list[dict] = []
    small_ignored: list[dict] = []
    for left, right in sorted(small_candidates):
        a, b = records[left], records[right]
        analysis = _analyze_pair(a, b, args.small_threshold)
        if analysis is None:
            continue
        pair_key = "::".join(sorted((a.key, b.key)))
        if pair_key in ignored_keys:
            small_ignored.append(
                {"key": pair_key, "similarity": analysis["similarity"]}
            )
            continue
        left_ids = _unique_identifiers(a)
        right_ids = _unique_identifiers(b)
        record = {
            "key": pair_key,
            "similarity": round(analysis["similarity"], 4),
            "kind": "near_duplicate"
            if analysis["similarity"] >= 0.95
            else "fork",
            "channel": "small",
            "name_match": a.name == b.name,
            "signature_match": a.sig_shape == b.sig_shape,
            "longest_match_tokens": analysis["longest_match_tokens"],
            "divergence": analysis["divergence"],
            "left_only_identifiers": sorted(set(left_ids) - set(right_ids))[:12],
            "right_only_identifiers": sorted(set(right_ids) - set(left_ids))[:12],
            "left": _record_dict(a),
            "right": _record_dict(b),
        }
        if rel is not None and a.path != rel and b.path != rel:
            continue
        small_pairs.append(record)
    small_pairs.sort(key=lambda item: (-item["similarity"], item["key"]))

    tagged = sum(1 for record in records if record.tag)
    kind_counts = {"fork": 0, "near_duplicate": 0}
    for item in pairs:
        kind_counts[item["kind"]] += 1
    small_kind_counts = {"fork": 0, "near_duplicate": 0}
    for item in small_pairs:
        small_kind_counts[item["kind"]] += 1

    payload = {
        "scanner": "forks",
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "package": args.package,
        "threshold": args.threshold,
        "min_lines": args.min_lines,
        "file": rel,
        "functions_indexed": len(records),
        "functions_over_min_lines": sum(
            1 for record in records if record.nlines >= args.min_lines
        ),
        "tagged": tagged,
        "untagged": len(records) - tagged,
        "candidate_pairs": len(candidate_pairs),
        "fork_pairs": len(pairs),
        "kind_counts": kind_counts,
        "parse_failures": parse_failures,
        "ignored_pairs": ignored_pairs,
        "pairs": pairs,
        "small_channel": {
            "floor": args.small_floor,
            "threshold": args.small_threshold,
            "candidate_pairs": len(small_candidates),
            "pairs": len(small_pairs),
        },
        "small_kind_counts": small_kind_counts,
        "small_function_pairs": small_pairs,
        "small_ignored_pairs": small_ignored,
    }
    _write_json(args.json, payload)

    label = rel or "all"
    print(
        f"FORK_SCAN package={args.package} scope={label} "
        f"indexed={len(records)} over_min_lines={payload['functions_over_min_lines']} "
        f"tagged={tagged} untagged={len(records) - tagged} "
        f"candidate_pairs={len(candidate_pairs)} forks={len(pairs)} "
        f"({kind_counts['fork']} fork, {kind_counts['near_duplicate']} near_dup) "
        f"small={len(small_pairs)} "
        f"({small_kind_counts['fork']} fork, {small_kind_counts['near_duplicate']} near_dup)"
    )
    for item in pairs[: args.top]:
        a, b = item["left"], item["right"]
        sig = "sig=yes" if item["signature_match"] else "sig=no"
        print(
            f"=== [{item['similarity']:.3f} {item['kind']}] "
            f"{a['path']}:{a['qualname']} ({a['nlines']} lines, L{a['lineno']}) <-> "
            f"{b['path']}:{b['qualname']} ({b['nlines']} lines, L{b['lineno']}) {sig}"
        )
        hints = []
        if item["left_only_identifiers"]:
            hints.append(f"left-only: {', '.join(item['left_only_identifiers'])}")
        if item["right_only_identifiers"]:
            hints.append(f"right-only: {', '.join(item['right_only_identifiers'])}")
        if hints:
            print(f"    {'; '.join(hints)}")
    for item in small_pairs[: args.top]:
        a, b = item["left"], item["right"]
        sig = "sig=yes" if item["signature_match"] else "sig=no"
        print(
            f"=== [SMALL {item['similarity']:.3f} {item['kind']}] "
            f"{a['path']}:{a['qualname']} ({a['nlines']} lines, L{a['lineno']}) <-> "
            f"{b['path']}:{b['qualname']} ({b['nlines']} lines, L{b['lineno']}) {sig}"
        )
    if not pairs and not small_pairs:
        print("No fork pairs.")
    if payload["untagged"]:
        print(
            f"NOTE: {payload['untagged']} callables have no docstring tag "
            f"(invisible to the label registry) -- see report 'untagged'"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
