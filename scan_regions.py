#!/usr/bin/env python3
"""Find repeated extractable code regions (latent capabilities).

Functions are the named, packaged capabilities of a codebase.  Large bodies
also contain *unnamed* capabilities: blocks of statements that recur with
similar inputs, outputs, API usage, and control flow, even though no symbol
names them.  This scanner segments every function body into natural
statement blocks (loop / if / try / with bodies and top-level statement
runs), filters them for extractability, and clusters structurally similar
regions across the package.

Three extraction channels feed different clusterings:

- ``shared`` (5-40 statements, method calls present): region-to-region
  clustering of the original "latent capability" signal.
- ``helper`` (5-40 statements, method-call free): maximal runs of pure
  validation/guard statements are matched against named functions to find
  inline copies of an existing helper (``kind=helper_not_reused``).
- ``short_risky`` (1-4 statements, semantic-risk density): blocks whose
  raw AST shows asymmetric indexing, contract keyword args, or repeated
  non-trivial constants are clustered by their subscript-pattern signature.

It deliberately does not decide whether two regions express the same
capability; adjudication does.  The scanner only claims: "these regions look
like they could share one implementation."
"""
from __future__ import annotations

import argparse
import ast
import copy
import datetime as dt
import difflib
import hashlib
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import _audit_config
from _scanner_common import (
    EXCLUDE_PARTS,
    PY_SUBDIRS,
    NormalizeAST as _Normalize,
    iter_functions as _iter_functions,
    load_ignore,
    short_hash as _short_hash,
    write_json as _write_json,
)

MIN_STATEMENTS = 5
MAX_STATEMENTS = 40
MAX_REGION_LINES = 80
MAX_FREE_VARS = 8
MAX_MUTATIONS = 4
MIN_EXTRACTABILITY = 0.5
COMMON_NAMES = {"main", "parse_args", "require", "close", "count"}
TOKEN_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*|==|!=|<=|>=|:=|->|\*\*|//|<<|>>|[^\s]"
)
# Keyword arguments that bind a call to a concrete dtype/device/strict
# contract; their presence makes a short block semantically dense.
_CONTRACT_KWARGS = {"dtype", "device", "strict", "weights_only"}

_CONTROL_TYPES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith)
_WINDOW_BREAKERS = (ast.Return, ast.Raise, ast.Break, ast.Continue)


@dataclass(frozen=True)
class RegionRecord:
    path: str
    parent: str
    start_line: int
    end_line: int
    nstatements: int
    nlines: int
    tokens: tuple[str, ...]
    calls: tuple[str, ...]
    control_shape: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    extractability: float
    risk: float
    risk_signals: tuple[str, ...]
    channel: tuple[str, ...]
    parent_tokens: tuple[str, ...]
    short_signature: tuple | None

    @property
    def key(self) -> str:
        return f"{self.path}:{self.parent}:{self.start_line}-{self.end_line}"


@dataclass(frozen=True)
class FunctionRecord:
    path: str
    qualname: str
    name: str
    start_line: int
    nstatements: int
    tokens: tuple[str, ...]


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _control_shape(block: list[ast.stmt]) -> str:
    counts: dict[str, int] = {"If": 0, "For": 0, "While": 0, "Try": 0, "With": 0}
    for node in ast.walk(ast.Module(body=block, type_ignores=[])):
        for kind in counts:
            if node.__class__.__name__.startswith(kind):
                counts[kind] += 1
    return "".join(f"{kind}{counts[kind]}" for kind in counts)


def _region_metrics(
    block: list[ast.stmt], fn_locals: set[str]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], int, bool, bool]:
    """Return (inputs, outputs, calls, break_continue, has_yield, has_await).

    ``outputs`` are names first *read* and later *written* inside the region
    (mutation of pre-existing state); names initialized inside the region
    (including loop targets) are not outputs.
    """
    defined: set[str] = set()
    mutated: list[str] = []
    loaded: set[str] = set()
    first_occ: dict[str, str] = {}
    calls: list[str] = []
    escape = 0
    has_yield = False
    has_await = False

    def walk(node: ast.AST) -> None:
        nonlocal escape, has_yield, has_await
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Store):
                if first_occ.get(node.id) == "load":
                    mutated.append(node.id)
                defined.add(node.id)
                first_occ.setdefault(node.id, "store")
            elif isinstance(node.ctx, ast.Load):
                first_occ.setdefault(node.id, "load")
                loaded.add(node.id)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                calls.append(func.attr)
            elif isinstance(func, ast.Name):
                calls.append(func.id)
        elif isinstance(node, (ast.Break, ast.Continue)):
            escape += 1
        elif isinstance(node, (ast.Yield, ast.YieldFrom)):
            has_yield = True
        elif isinstance(node, ast.Await):
            has_await = True
        for child in ast.iter_child_nodes(node):
            walk(child)

    for stmt in block:
        walk(stmt)
    inputs = sorted(loaded & fn_locals - defined)
    outputs = list(dict.fromkeys(mutated))
    return tuple(inputs), tuple(outputs), tuple(calls), escape, has_yield, has_await


def _extractability(
    inputs: int, outputs: int, escape: int, n_try: int, has_yield: bool, has_await: bool
) -> float:
    score = (
        1.0
        - 0.08 * inputs
        - 0.1 * outputs
        - 0.06 * escape
        - 0.04 * n_try
        - (0.4 if has_yield or has_await else 0.0)
    )
    return round(min(1.0, max(0.0, score)), 2)


def _blocks_of(body: list[ast.stmt]) -> Iterator[list[ast.stmt]]:
    """Yield natural statement blocks: control-body suites and their nested suites."""
    for stmt in body:
        if isinstance(stmt, _CONTROL_TYPES):
            yield stmt.body
            yield from _blocks_of(stmt.body)
            orelse = getattr(stmt, "orelse", None) or []
            yield orelse
            yield from _blocks_of(orelse)
            if isinstance(stmt, ast.Try):
                for handler in stmt.handlers:
                    yield handler.body
                    yield from _blocks_of(handler.body)
                yield stmt.finalbody
                yield from _blocks_of(stmt.finalbody)


def _top_windows(body: list[ast.stmt]) -> Iterator[list[ast.stmt]]:
    """Yield runs of consecutive simple statements between control/exit nodes."""
    window: list[ast.stmt] = []
    for stmt in body:
        if isinstance(stmt, _CONTROL_TYPES + _WINDOW_BREAKERS):
            if window:
                yield window
                window = []
        else:
            window.append(stmt)
    if window:
        yield window


def _segments_of(body: list[ast.stmt]) -> Iterator[list[ast.stmt]]:
    """Split a statement list into logical paragraphs at blank lines.

    Authors separate capabilities with blank lines; ``stmt.lineno`` gaps
    recover that paragraph structure from the AST.
    """
    run: list[ast.stmt] = []
    prev_end: int | None = None
    for stmt in body:
        gap = prev_end is not None and stmt.lineno - prev_end > 1
        if gap and run:
            yield run
            run = []
        run.append(stmt)
        prev_end = stmt.end_lineno or stmt.lineno
    if run:
        yield run


def _api_free_runs(body: list[ast.stmt]) -> Iterator[list[ast.stmt]]:
    """Yield maximal runs of statements that contain no method call.

    Hand-written validation guards cluster into runs of attribute tests and
    bare calls (``isinstance``, ``ValueError``); a method call
    (``tensor.reshape(...)``) ends the run.  These runs feed the
    ``helper`` channel: an api-free run is what an inline copy of a named
    validation helper looks like.
    """
    run: list[ast.stmt] = []
    for stmt in body:
        if _stmt_has_api_call(stmt):
            if run:
                yield run
                run = []
        else:
            run.append(stmt)
    if run:
        yield run


def _risky_statements(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield statements (any nesting depth) whose raw AST carries semantic
    risk — e.g. a nested ``torch.stack`` of S00/S01/S10/S11 lookups inside a
    loop body, which no suite/window segmentation isolates.  The statement
    itself becomes a 1-stmt span on the short_risky channel.
    """
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if _semantic_risk([stmt])[0] > 0:
            yield stmt
        for child in ast.iter_child_nodes(stmt):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(child, ast.stmt):
                yield from _risky_statements([child])


def _stmt_has_api_call(stmt: ast.stmt) -> bool:
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        for node in ast.walk(stmt)
    )


def _strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    """Return ``body`` without a leading string-expr docstring."""
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _normalize_block(block: list[ast.stmt]) -> str:
    module = ast.Module(body=copy.deepcopy(block), type_ignores=[])
    normalized_node = _Normalize().visit(module)
    ast.fix_missing_locations(normalized_node)
    return " ".join(ast.unparse(normalized_node).split())


def _function_locals(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names assigned anywhere in the function body (excluding nested scopes)."""
    assigned: set[str] = set()
    args = node.args

    def walk(inner: ast.AST) -> None:
        if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if inner is not node:
                return
        if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Store):
            assigned.add(inner.id)
        for child in ast.iter_child_nodes(inner):
            walk(child)

    walk(node)
    params = {
        arg.arg
        for group in (
            args.posonlyargs,
            args.args,
            args.kwonlyargs,
        )
        for arg in group
    }
    if args.vararg:
        params.add(args.vararg.arg)
    if args.kwarg:
        params.add(args.kwarg.arg)
    return assigned | params


def _semantic_risk(block: list[ast.stmt]) -> tuple[float, tuple[str, ...]]:
    """Semantic-density risk signals on the RAW (unnormalized) AST.

    ``NormalizeAST`` would erase the signals (names become ``_name``,
    constants become placeholders); the raw tree keeps the asymmetric
    subscript patterns and contract keywords that make a short block
    semantically dense.

    - ``asymmetric_indexing`` (+0.45): a name repeats inside one subscript
      tuple (``S00[a, a]``), or one subscripted object is addressed with
      two distinct tuple patterns (``t[:, :, 0, 1]`` vs ``t[:, :, 1, 0]``).
      The subscripted value may itself be a subscript (``m["S00"][aa, aa]``).
    - ``contract_kwargs`` (+0.35): a call binds dtype/device/strict/
      weights_only.
    - ``repeated_constants`` (+0.2): a non-trivial numeric literal appears
      twice or more (``1e-12`` twice in an entropy formula).
    """
    risk = 0.0
    signals: list[str] = []
    tuple_slices: dict[str, set[tuple]] = defaultdict(set)
    diagonal = 0
    constants: Counter = Counter()
    for node in ast.walk(ast.Module(body=block, type_ignores=[])):
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Tuple):
            parts = tuple(
                elt.id
                if isinstance(elt, ast.Name)
                else (elt.value if isinstance(elt, ast.Constant) else None)
                for elt in node.slice.elts
            )
            if len(parts) >= 2:
                name_ids = [part for part in parts if isinstance(part, str)]
                if isinstance(node.value, ast.Name):
                    tuple_slices[node.value.id].add(parts)
                if len(name_ids) >= 2 and len(set(name_ids)) < len(name_ids):
                    diagonal += 1
        elif isinstance(node, ast.Call):
            if any(kw.arg in _CONTRACT_KWARGS for kw in node.keywords):
                risk += 0.35
                signals.append("contract_kwargs")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool)
        ):
            if node.value not in (0, 1):
                constants[node.value] += 1
    if diagonal >= 1 or any(len(slices) >= 2 for slices in tuple_slices.values()):
        risk += 0.45
        signals.append("asymmetric_indexing")
    if any(count >= 2 for count in constants.values()):
        risk += 0.2
        signals.append("repeated_constants")
    return round(risk, 2), tuple(sorted(set(signals)))


def _short_signature(block: list[ast.stmt]) -> tuple | None:
    """Subscript-pattern signature for short risky blocks.

    Each element is ``(value_key, slice_pattern)``: ``value_key`` is the
    literal when the subscripted value is itself a subscript with a constant
    key (``tables["S00"][a, a]`` -> ``"S00"``), else None; ``slice_pattern``
    collapses a tuple slice to ``"diag"`` (a name repeats within the slice),
    ``"off"`` (distinct names), or the literal constant pattern itself.
    Names never appear, so two copies using different variable names share a
    bucket, while different table keys (S00 vs P00) do not.  The signature
    is deliberately subscript-centric: wrapper-call variance (``out.append(
    torch.stack(...))`` vs ``scores = torch.stack(...)``) would split
    identical lookups if the full call multiset were part of the key.

    Returns None when the block carries fewer than two subscript patterns —
    a single lookup or a contract-kwargs-only block has nothing to signal
    replication and must not enter the signature cluster.
    """
    subscripts: Counter[object] = Counter()
    for node in ast.walk(ast.Module(body=block, type_ignores=[])):
        if not (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Tuple)):
            continue
        parts = tuple(
            elt.id
            if isinstance(elt, ast.Name)
            else (elt.value if isinstance(elt, ast.Constant) else None)
            for elt in node.slice.elts
        )
        if len(parts) < 2:
            continue
        name_ids = [part for part in parts if isinstance(part, str)]
        if len(name_ids) >= 2 and len(set(name_ids)) < len(name_ids):
            slice_pattern: object = "diag"
        elif len(name_ids) >= 2:
            slice_pattern = "off"
        else:
            slice_pattern = parts
        value = node.value
        value_key = (
            value.slice.value
            if isinstance(value, ast.Subscript)
            and isinstance(value.slice, ast.Constant)
            else None
        )
        subscripts[(value_key, slice_pattern)] += 1
    if sum(subscripts.values()) < 2:
        return None
    return tuple(
        sorted(subscripts.items(), key=lambda item: (str(item[0]), item[1]))
    )


def extract_regions(path: Path) -> tuple[list[RegionRecord], list[FunctionRecord]]:
    """Return (records, indexable functions) for one module.

    Every block from every segmentation is tried on the short_risky channel
    (contained blocks included); only the largest non-contained spans feed
    the shared/helper channels, preserving the historical region extraction.
    """
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    tree = ast.parse(text)
    records: list[RegionRecord] = []
    function_candidates: list[FunctionRecord] = []
    body_spans: dict[str, tuple[int, int]] = {}
    for node, parents in _iter_functions(tree):
        parent = ".".join(parents + (node.name,))
        body = _strip_docstring(node.body)
        if not body:
            continue
        fn_locals = _function_locals(node)
        body_spans[parent] = _span_key(body)
        parent_tokens = tuple(
            TOKEN_RE.findall(ast.unparse(ast.Module(body=body, type_ignores=[])))
        )
        spans: dict[tuple[int, int], list[ast.stmt]] = {}
        for block in _blocks_of(node.body):
            if block:
                spans.setdefault(_span_key(block), block)
        for window in _top_windows(node.body):
            if window:
                spans.setdefault(_span_key(window), window)
        for segment in _segments_of(node.body):
            if segment:
                spans.setdefault(_span_key(segment), segment)
        for run in _api_free_runs(body):
            if run:
                spans.setdefault(_span_key(run), run)
        for stmt in _risky_statements(node.body):
            spans.setdefault(_span_key([stmt]), [stmt])
        kept_blocks = _drop_contained_spans(spans)
        for block in kept_blocks:
            record = _region_record(path, parent, fn_locals, block, parent_tokens, None)
            if record is not None:
                records.append(record)
        kept_spans = {_span_key(block) for block in kept_blocks}
        for span, block in spans.items():
            if span in kept_spans:
                continue
            record = _region_record(
                path, parent, fn_locals, block, parent_tokens, {"short_risky", "helper"}
            )
            if record is not None:
                records.append(record)
        nstmts = len(body)
        if 3 <= nstmts <= 60 and node.name not in COMMON_NAMES:
            tokens = tuple(TOKEN_RE.findall(_normalize_block(body)))
            if len(tokens) >= 25:
                function_candidates.append(
                    FunctionRecord(
                        path="",
                        qualname=parent,
                        name=node.name,
                        start_line=node.lineno,
                        nstatements=nstmts,
                        tokens=tokens,
                    )
                )
    covered = {
        record.parent
        for record in records
        if body_spans.get(record.parent) == (record.start_line, record.end_line)
    }
    functions = [fn for fn in function_candidates if fn.qualname not in covered]
    return records, functions


def _drop_contained_spans(spans: dict[tuple[int, int], list[ast.stmt]]) -> list[list[ast.stmt]]:
    """Keep only the largest span when several overlap inside one function.

    Different segmentation sources (suites, windows, blank-line paragraphs)
    produce nested views of the same capability; the smaller ones are noise.
    """
    ordered = sorted(spans.items(), key=lambda item: (item[0][0], -item[0][1]))
    kept: list[list[ast.stmt]] = []
    for (start, end), block in ordered:
        if any(
            start >= other[0].lineno
            and end <= (getattr(other[-1], "end_lineno", other[-1].lineno) or other[-1].lineno)
            for other in kept
        ):
            continue
        kept.append(block)
    return kept


def _span_key(block: list[ast.stmt]) -> tuple[int, int]:
    start = block[0].lineno
    end = getattr(block[-1], "end_lineno", block[-1].lineno) or block[-1].lineno
    return start, end


def _region_record(
    path: Path,
    parent: str,
    fn_locals: set[str],
    block: list[ast.stmt],
    parent_tokens: tuple[str, ...],
    allowed_channels: set[str] | None,
) -> RegionRecord | None:
    """Route one block to the shared / helper / short_risky channel."""
    nstatements = len(block)
    if not 1 <= nstatements <= MAX_STATEMENTS:
        return None
    start_line = block[0].lineno
    end_line = getattr(block[-1], "end_lineno", block[-1].lineno) or block[-1].lineno
    if end_line - start_line + 1 > MAX_REGION_LINES:
        return None
    inputs, outputs, calls, escape, has_yield, has_await = _region_metrics(
        block, fn_locals
    )
    has_api_call = any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        for node in ast.walk(ast.Module(body=block, type_ignores=[]))
    )
    risk, risk_signals = _semantic_risk(block)
    channels: list[str] = []
    if MIN_STATEMENTS <= nstatements <= MAX_STATEMENTS:
        channels.append("shared" if has_api_call else "helper")
    elif risk > 0:
        channels.append("short_risky")
    if not channels:
        return None
    if allowed_channels is not None and not any(
        channel in allowed_channels for channel in channels
    ):
        return None
    is_short = "short_risky" in channels
    if has_yield or has_await:
        return None
    if not is_short and not (
        0 <= len(inputs) <= MAX_FREE_VARS and len(outputs) <= MAX_MUTATIONS
    ):
        return None
    n_try = sum(1 for _ in ast.walk(ast.Module(body=block, type_ignores=[])) if isinstance(_, ast.Try))
    score = _extractability(
        len(inputs), len(outputs), escape, n_try, has_yield, has_await
    )
    if not is_short and score < MIN_EXTRACTABILITY:
        return None
    normalized = _normalize_block(block)
    tokens = tuple(TOKEN_RE.findall(normalized))
    min_tokens = 30 if channels == ["shared"] else 25 if channels == ["helper"] else 18
    if len(tokens) < min_tokens:
        return None
    short_signature: tuple | None = None
    if is_short:
        short_signature = _short_signature(block)
        if short_signature is None:
            return None
    return RegionRecord(
        path="",
        parent=parent,
        start_line=start_line,
        end_line=end_line,
        nstatements=nstatements,
        nlines=end_line - start_line + 1,
        tokens=tokens,
        calls=tuple(sorted(set(calls))),
        control_shape=_control_shape(block),
        inputs=inputs,
        outputs=tuple(outputs),
        extractability=score,
        risk=risk,
        risk_signals=risk_signals,
        channel=tuple(channels),
        parent_tokens=parent_tokens,
        short_signature=short_signature,
    )


def _similarity(
    left: tuple[str, ...], right: tuple[str, ...], threshold: float
) -> float:
    shorter, longer = sorted((len(left), len(right)))
    if longer == 0 or shorter / longer < threshold:
        return 0.0
    matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
    if matcher.real_quick_ratio() < threshold:
        return 0.0
    if matcher.quick_ratio() < threshold:
        return 0.0
    return matcher.ratio()


def _coverage(left: tuple[str, ...], right: tuple[str, ...], divisor: int) -> float:
    """Matched-block token sum of ``left`` vs ``right`` over ``divisor``.

    The quick_ratio gates only reject hopeless pairs; with ``divisor`` at
    least as large as the shorter side, quick_ratio < 0.5 implies
    coverage < 0.5, so gates at 0.5 are sound for thresholds >= 0.5.
    """
    if not left or not right or divisor <= 0:
        return 0.0
    matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
    if matcher.real_quick_ratio() < 0.5:
        return 0.0
    if matcher.quick_ratio() < 0.5:
        return 0.0
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / divisor


def _length_comparable(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return abs(len(left) - len(right)) <= max(len(left), len(right)) // 2


def _candidate_pairs(records: list[RegionRecord], indices: list[int]) -> set[tuple[int, int]]:
    """Block comparisons by control shape and statement count; require a
    shared API call set before similarity is computed."""
    blocks: dict[tuple, list[int]] = defaultdict(list)
    for i in indices:
        record = records[i]
        bucket = record.nstatements // 8
        for offset in (bucket - 1, bucket, bucket + 1):
            blocks[(record.control_shape, offset)].append(i)
    pairs: set[tuple[int, int]] = set()
    for members in blocks.values():
        unique = sorted(set(members))
        for pos, left in enumerate(unique):
            for right in unique[pos + 1 :]:
                left_calls = set(records[left].calls)
                right_calls = set(records[right].calls)
                overlap = left_calls & right_calls
                union = left_calls | right_calls
                if overlap and len(overlap) / len(union) >= 0.5:
                    pairs.add((left, right))
    return pairs


def _cluster_id(members: list[RegionRecord]) -> str:
    return _short_hash(*sorted(member.key for member in members))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=Path.cwd())
    ap.add_argument("--package", default="src")
    ap.add_argument("--subdirs", nargs="*", default=None)
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--helper-reuse-threshold", type=float, default=None)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--ignore", type=Path, default=None)
    args = ap.parse_args(argv)

    repo = args.root.resolve()
    pkg = (repo / args.package).resolve()
    if not pkg.is_dir() or pkg != repo / args.package:
        print(f"error: package dir not found or escapes root: {pkg}", file=sys.stderr)
        return 2

    cfg = _audit_config.load_config(repo)
    region_cfg = cfg.get("regions", {})
    subdirs = _audit_config.pick(args.subdirs, cfg, "subdirs", list(PY_SUBDIRS))
    threshold = _audit_config.pick(args.threshold, region_cfg, "threshold", 0.82)
    if not 0.0 <= threshold <= 1.0:
        print("error: --threshold must be in [0, 1]", file=sys.stderr)
        return 2
    helper_reuse_threshold = _audit_config.pick(
        args.helper_reuse_threshold, region_cfg, "helper_reuse_threshold", 0.6
    )
    if not 0.0 <= helper_reuse_threshold <= 1.0:
        print("error: --helper-reuse-threshold must be in [0, 1]", file=sys.stderr)
        return 2

    records: list[RegionRecord] = []
    functions: list[FunctionRecord] = []
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
                extracted, indexed = extract_regions(path)
            except (OSError, UnicodeError, SyntaxError) as exc:
                parse_failures.append(
                    {"path": path.relative_to(pkg).as_posix(), "error": str(exc)}
                )
                continue
            rel = path.relative_to(pkg).as_posix()
            records.extend(
                RegionRecord(
                    path=rel,
                    parent=item.parent,
                    start_line=item.start_line,
                    end_line=item.end_line,
                    nstatements=item.nstatements,
                    nlines=item.nlines,
                    tokens=item.tokens,
                    calls=item.calls,
                    control_shape=item.control_shape,
                    inputs=item.inputs,
                    outputs=item.outputs,
                    extractability=item.extractability,
                    risk=item.risk,
                    risk_signals=item.risk_signals,
                    channel=item.channel,
                    parent_tokens=item.parent_tokens,
                    short_signature=item.short_signature,
                )
                for item in extracted
            )
            functions.extend(
                FunctionRecord(
                    path=rel,
                    qualname=item.qualname,
                    name=item.name,
                    start_line=item.start_line,
                    nstatements=item.nstatements,
                    tokens=item.tokens,
                )
                for item in indexed
            )

    union_find = _UnionFind(len(records))
    edge_similarity: dict[tuple[int, int], float] = {}

    shared_indices = [i for i, record in enumerate(records) if "shared" in record.channel]

    exact: dict[str, list[int]] = defaultdict(list)
    for i in shared_indices:
        digest = hashlib.sha256(" ".join(records[i].tokens).encode("utf-8")).hexdigest()
        exact[digest].append(i)
    for exact_members in exact.values():
        for index in exact_members[1:]:
            union_find.union(exact_members[0], index)
            edge_similarity[(exact_members[0], index)] = 1.0

    candidate_pairs = _candidate_pairs(records, shared_indices)
    for left, right in sorted(candidate_pairs):
        similarity = _similarity(records[left].tokens, records[right].tokens, threshold)
        if similarity >= threshold:
            union_find.union(left, right)
            edge_similarity[(left, right)] = similarity

    # short_risky signature clustering: same subscript-pattern bucket, then
    # matched-block coverage over the shorter block.
    short_buckets: dict[tuple, list[int]] = defaultdict(list)
    for i, record in enumerate(records):
        if "short_risky" in record.channel and record.short_signature is not None:
            short_buckets[record.short_signature].append(i)
    for members in short_buckets.values():
        unique = sorted(set(members))
        for pos, left in enumerate(unique):
            for right in unique[pos + 1 :]:
                if not _length_comparable(records[left].tokens, records[right].tokens):
                    continue
                divisor = min(len(records[left].tokens), len(records[right].tokens))
                coverage = _coverage(records[left].tokens, records[right].tokens, divisor)
                if coverage >= 0.5:
                    union_find.union(left, right)
                    edge_similarity[(left, right)] = coverage

    components: dict[int, list[int]] = defaultdict(list)
    for i in range(len(records)):
        components[union_find.find(i)].append(i)

    ignore = load_ignore(args.ignore)
    ignored_ids = {
        entry.get("id", "") for entry in ignore.get("regions", []) if entry.get("id")
    }
    ignored: list[dict[str, Any]] = []

    # helper_not_reused: helper-channel regions matched against named functions.
    helper_reports: list[dict[str, Any]] = []
    helper_matches: list[tuple[int, int, float, bool]] = []
    helper_indices = [i for i, record in enumerate(records) if "helper" in record.channel]
    for qi in helper_indices:
        region = records[qi]
        for fi, fn in enumerate(functions):
            if fn.path == region.path and (
                fn.qualname == region.parent
                or region.parent.startswith(fn.qualname + ".")
            ):
                continue
            # One-sided prefilter: an inline copy is usually *longer* than
            # the canonical helper it duplicates (extra local stmts), so a
            # symmetric length bucket would miss the match.  Coverage is
            # matched/len(fn.tokens), so a region shorter than
            # threshold*fn cannot reach the threshold; the reverse is fine.
            if len(region.tokens) < helper_reuse_threshold * len(fn.tokens):
                continue
            coverage = _coverage(region.tokens, fn.tokens, len(fn.tokens))
            if coverage >= helper_reuse_threshold:
                helper_matches.append((qi, fi, coverage, fn.name in region.parent_tokens))
    by_function: dict[int, list[tuple[int, float, bool]]] = defaultdict(list)
    for qi, fi, coverage, referenced in helper_matches:
        by_function[fi].append((qi, coverage, referenced))
    for fi, fn_matches in by_function.items():
        fn = functions[fi]
        members: list[dict[str, Any]] = []
        files = {fn.path}
        for qi, coverage, referenced in sorted(fn_matches):
            region = records[qi]
            members.append(
                {
                    "path": region.path,
                    "qualname": region.parent,
                    "region_id": region.key,
                    "start_line": region.start_line,
                    "end_line": region.end_line,
                    "nstatements": region.nstatements,
                    "nlines": region.nlines,
                    "inputs": list(region.inputs),
                    "outputs": list(region.outputs),
                    "calls": list(region.calls),
                    "effects": {"mutates": list(region.outputs)},
                    "control_shape": region.control_shape,
                    "extractability": region.extractability,
                    "coverage": round(coverage, 4),
                    "canonical_referenced_in_parent": referenced,
                }
            )
            files.add(region.path)
        best = max(coverage for _, coverage, _ in fn_matches)
        cross_file = len(files) >= 2
        if best >= 0.85 and (fn.path.startswith("lib/") or cross_file):
            priority = "high"
            priority_reason = (
                f"inline copy covers {best:.0%} of canonical helper {fn.qualname}"
            )
        elif best >= 0.7:
            priority = "medium"
            priority_reason = (
                f"inline copy partially re-implements canonical helper {fn.qualname}"
            )
        else:
            priority = "low"
            priority_reason = f"inline copy resembles canonical helper {fn.qualname}"
        cluster_id = _short_hash(
            fn.path, fn.qualname, *sorted(member["region_id"] for member in members)
        )
        if cluster_id in ignored_ids:
            ignored.append(
                {
                    "id": cluster_id,
                    "members": sorted(member["region_id"] for member in members),
                }
            )
            continue
        helper_reports.append(
            {
                "id": cluster_id,
                "kind": "helper_not_reused",
                "priority": priority,
                "priority_reason": priority_reason,
                "size": len(members),
                "max_lines": max(member["nlines"] for member in members),
                "max_coverage": round(best, 4),
                "canonical_symbol": f"{fn.path}:{fn.qualname}",
                "canonical": {
                    "path": fn.path,
                    "qualname": fn.qualname,
                    "lineno": fn.start_line,
                },
                "semantic_risk": round(
                    max(records[qi].risk for qi, _, _ in fn_matches), 2
                ),
                "risk_signals": sorted(
                    {
                        signal
                        for qi, _, _ in fn_matches
                        for signal in records[qi].risk_signals
                    }
                ),
                "members": members,
            }
        )

    # Region-to-region clusters (shared / short_risky channels).
    reports: list[dict[str, Any]] = []
    for indices in components.values():
        if len(indices) < 2:
            continue
        members = [records[i] for i in indices]
        cluster_id = _cluster_id(members)
        if cluster_id in ignored_ids:
            ignored.append(
                {
                    "id": cluster_id,
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
        max_lines = max(member.nlines for member in members)
        file_count = len({member.path for member in members})
        cross_file = file_count >= 2
        max_risk = max(member.risk for member in members)
        risk_signals = sorted(
            {signal for member in members for signal in member.risk_signals}
        )
        short_block_cluster = all(
            "short_risky" in member.channel for member in members
        )
        call_counter: Counter[str] = Counter()
        for member in members:
            call_counter.update(member.calls)
        capability_hints = [
            name for name, _ in call_counter.most_common(8) if call_counter[name] >= 2
        ]
        if short_block_cluster:
            if cross_file or len(members) >= 3:
                priority = "high"
                priority_reason = (
                    "semantic-risk-dense short blocks repeated across call sites"
                )
            else:
                priority = "medium"
                priority_reason = (
                    "semantic-risk-dense short block similarity (verify ownership)"
                )
        elif cross_file and max_lines >= 15 and max(component_edges, default=1.0) >= 0.9:
            priority = "high"
            priority_reason = "latent capability repeated across files with no canonical symbol"
        elif cross_file:
            priority = "medium"
            priority_reason = "cross-file region similarity requiring semantic review"
        else:
            priority = "low"
            priority_reason = "same-file region similarity (verify ownership)"
        if not short_block_cluster and max_risk >= 0.7 and priority != "high":
            priority = "high"
            priority_reason = "semantic-risk-dense region similarity requiring review"
        report: dict[str, Any] = {
            "id": cluster_id,
            "kind": "shared_capability",
            "priority": priority,
            "priority_reason": priority_reason,
            "size": len(members),
            "max_lines": max_lines,
            "file_count": file_count,
            "max_sim": round(max(component_edges, default=1.0), 4),
            "min_edge_sim": round(min(component_edges, default=1.0), 4),
            "semantic_risk": round(max_risk, 2),
            "risk_signals": risk_signals,
            "capability_hints": capability_hints,
            "canonical_symbol": None,
            "members": [
                {
                    "path": member.path,
                    "qualname": member.parent,
                    "region_id": member.key,
                    "start_line": member.start_line,
                    "end_line": member.end_line,
                    "nstatements": member.nstatements,
                    "nlines": member.nlines,
                    "inputs": list(member.inputs),
                    "outputs": list(member.outputs),
                    "calls": list(member.calls),
                    "effects": {"mutates": list(member.outputs)},
                    "control_shape": member.control_shape,
                    "extractability": member.extractability,
                }
                for member in sorted(members, key=lambda item: item.key)
            ],
        }
        if short_block_cluster:
            report["short_block_cluster"] = True
        reports.append(report)
    reports.extend(helper_reports)

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
        "scanner": "regions",
        "schema_version": 2,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "package": args.package,
        "threshold": threshold,
        "helper_reuse_threshold": helper_reuse_threshold,
        "regions_scanned": len(records),
        "functions_indexed": len(functions),
        "short_risky_blocks": sum(
            1 for record in records if "short_risky" in record.channel
        ),
        "candidate_pairs": len(candidate_pairs),
        "clusters_reported": len(reports),
        "helper_reports": len(helper_reports),
        "priority_counts": {
            level: sum(item["priority"] == level for item in reports)
            for level in ("high", "medium", "low")
        },
        "parse_failures": parse_failures,
        "ignored": ignored,
        "clusters": reports,
    }
    _write_json(args.json, payload)

    print(
        f"REGION_SCAN package={args.package} regions={len(records)} "
        f"functions={len(functions)} candidate_pairs={len(candidate_pairs)} "
        f"clusters={len(reports)} helpers={len(helper_reports)} "
        f"ignored={len(ignored)}"
    )
    for report in reports[:40]:
        if report["kind"] == "helper_not_reused":
            print(
                f"=== [{report['priority']}] {report['id']} helper_not_reused "
                f"canonical={report['canonical_symbol']} "
                f"coverage={report['max_coverage']:.3f}"
            )
            for member in report["members"]:
                print(
                    f"    {member['path']}:{member['qualname']}:"
                    f"{member['start_line']}-{member['end_line']} "
                    f"(L{member['nlines']}, cov={member['coverage']:.2f}, "
                    f"referenced={member['canonical_referenced_in_parent']})"
                )
            continue
        hints = ", ".join(report["capability_hints"]) or "-"
        short_tag = " [SHORT]" if report.get("short_block_cluster") else ""
        print(
            f"=== [{report['priority']}] {report['id']} {report['size']} regions, "
            f"edge_sim={report['min_edge_sim']:.3f}-{report['max_sim']:.3f} "
            f"hints={hints}{short_tag}"
        )
        for member in report["members"]:
            print(
                f"    {member['path']}:{member['qualname']}:"
                f"{member['start_line']}-{member['end_line']} "
                f"(L{member['nlines']}, {member['nstatements']} stmts, "
                f"ext={member['extractability']})"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
