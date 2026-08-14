"""Deterministic multi-channel retrieval for implementation-reuse detection.

This is the PR-2 retrieval layer: given a NEW capability, surface the most
relevant EXISTING implementations in the repository, ranked.  It is stdlib-only;
the LLM is reserved for adjudication over the top candidates, never for recall.

Channels (all deterministic):
  - structural — normalized-body similarity (identifier-rename and
    control-flow-rewrite proof via ``NormalizeAST`` + ``SequenceMatcher``);
  - lexical   — docstring-first-line tag similarity (the old
    ``scan_capabilities`` fallback, kept as one channel rather than a gate).

The structural channel is the workhorse: two functions that mint a token and
persist it with a TTL are near-identical after name/constant normalization,
regardless of what they are called.
"""
from __future__ import annotations

import ast
import builtins
import copy
import difflib
import math
import re
from dataclasses import dataclass
from pathlib import Path

from _scanner_common import NormalizeAST, collect_py_files, signature_shape


class _ReuseNormalize(NormalizeAST):
    """Aggressively normalize for reuse detection, not structural clones.

    ``NormalizeAST`` renames variables but keeps method/attribute names, so
    ``store.mint(x)`` and ``token_store.issue(x)`` still differ.  For reuse the
    *vocabulary* is exactly what changes, so attribute, keyword, and function
    names are normalized too: ``def _function(_arg): _name._attr(_arg, _kw=0)``.
    """

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        node.name = "_function"
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef:
        node.name = "_function"
        self.generic_visit(node)
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.Attribute:
        self.generic_visit(node)
        node.attr = "_attr"
        return node

    def visit_keyword(self, node: ast.keyword) -> ast.keyword:
        self.generic_visit(node)
        if node.arg is not None:
            node.arg = "_kw"
        return node


@dataclass
class Symbol:
    path: str
    name: str
    qualname: str
    lineno: int
    signature: str
    sig_shape: str
    tag: str
    norm_body: tuple[str, ...]
    call_names: frozenset[str]
    returns_value: bool
    string_literals: frozenset[str]

    @property
    def key(self) -> str:
        return f"{self.path}:{self.qualname}"


def _doc_first_line(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    doc = ast.get_docstring(node)
    return doc.strip().splitlines()[0].strip() if doc else ""


_BUILTIN_NAMES = frozenset(dir(builtins))


def _called_names(node: ast.AST) -> frozenset[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                # builtins (len, sum, range, ...) are language primitives,
                # not reuse evidence; method names are kept.
                if func.id not in _BUILTIN_NAMES:
                    names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return frozenset(names)


def _strip_docstring(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Deep-copy *node* with its docstring removed (it has its own channel)."""
    node = copy.deepcopy(node)
    if (
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    ):
        node.body = node.body[1:]
    return node


def _string_literals(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> frozenset[str]:
    node = _strip_docstring(node)
    return frozenset(
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    )


def _returns_value(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Return) and child.value is not None
        for child in ast.walk(node)
    )


def _normalized_body(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ...]:
    node = _strip_docstring(node)
    norm = _ReuseNormalize()
    normalized = norm.visit(node)
    # Token-level, not string-level: comparing token tuples is 5-10x shorter
    # than the unparse string and lets the quick-ratio gate actually filter.
    # Punctuation is kept as its own tokens so ``a.b(c)`` and ``a[b]c`` and
    # ``a + b`` stay distinguishable.
    return tuple(re.findall(r"[A-Za-z_]\w*|\d+|[^\sA-Za-z_0-9]", ast.unparse(normalized)))


def _extract_source(source: str, rel: str) -> list[Symbol]:
    """Extract callables from *source* text, tagged with repo-relative *rel*."""
    source = source.lstrip("\ufeff")  # tolerate a UTF-8 BOM (git show / Windows files)
    try:
        tree = ast.parse(source)
    except (UnicodeError, SyntaxError):
        return []
    out: list[Symbol] = []

    def add(node: ast.FunctionDef | ast.AsyncFunctionDef, qualname: str) -> None:
        out.append(
            Symbol(
                path=rel,
                name=node.name,
                qualname=qualname,
                lineno=node.lineno,
                signature=f"({ast.unparse(node.args)})",
                sig_shape=signature_shape(node),
                tag=_doc_first_line(node),
                norm_body=_normalized_body(node),
                call_names=_called_names(node),
                returns_value=_returns_value(node),
                string_literals=_string_literals(node),
            )
        )

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node, node.name)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(child, f"{node.name}.{child.name}")
    return out


def _extract(path: Path, rel_root: Path) -> list[Symbol]:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except (OSError, UnicodeError):
        return []
    rel = path.relative_to(rel_root).as_posix()
    return _extract_source(text, rel)


def _base_index(root: Path, base: str) -> list[Symbol]:
    """Index the ``.py`` files at git ref *base* — the pre-change codebase.

    Reading from the base tree (not the live working tree) is what makes the
    diff-mode comparison honest: a helper the patch removed, or a canonical it
    modified, must still be a candidate.
    """
    import subprocess

    def _git(args: list[str]) -> str:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git failed: {proc.stderr.strip()}")
        return proc.stdout

    index: list[Symbol] = []
    for rel in _git(["ls-tree", "-r", "--name-only", base]).splitlines():
        rel = rel.strip()
        if not rel.endswith(".py"):
            continue
        index.extend(_extract_source(_git(["show", f"{base}:{rel}"]), rel))
    return index


def build_index(root: Path, rel_root: Path | None = None) -> list[Symbol]:
    """Repository-wide symbol index (recursive, ``EXCLUDE_PARTS``-aware).

    ``rel_root`` defaults to ``root``; pass the package root when ``root`` is a
    subdirectory (e.g. ``lib/``) so emitted paths stay package-relative.
    """
    rel_root = rel_root or root
    index: list[Symbol] = []
    for path in collect_py_files(root):
        index.extend(_extract(path, rel_root))
    return index


def body_similarity(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    if not left or not right:
        return 0.0
    shorter, longer = sorted((len(left), len(right)))
    if longer == 0 or shorter / longer < 0.35:
        return 0.0
    matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
    # cheap upper-bound gates before the O(n^2) full ratio
    if matcher.real_quick_ratio() < 0.35:
        return 0.0
    if matcher.quick_ratio() < 0.35:
        return 0.0
    return matcher.ratio()


def _tag_similarity(left: str, right: str) -> float:
    def tokens(text: str) -> tuple[str, ...]:
        normalized = re.sub(r"[_\-.,;:()\[\]{}/]", " ", text.lower())
        return tuple(re.sub(r"\s+", " ", normalized).strip().split())

    a, b = tokens(left), tokens(right)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _idf_map(values_per_symbol: list[frozenset[str]]) -> dict[str, float]:
    """Inverse document frequency of each token across the index.

    A token shared by many symbols (``get``, ``utf-8``) is weak reuse evidence;
    a token shared by few (a full SQL query) is strong.  ``+1`` smoothing keeps
    a token present in every symbol at idf 1.
    """
    n = len(values_per_symbol)
    df: dict[str, int] = {}
    for values in values_per_symbol:
        for value in values:
            df[value] = df.get(value, 0) + 1
    return {value: math.log((n + 1) / (count + 1)) + 1 for value, count in df.items()}


def _weighted_jaccard(
    query: frozenset[str], sym: frozenset[str], idf: dict[str, float]
) -> float:
    shared = query & sym
    if not shared:
        return 0.0
    union = query | sym
    shared_w = sum(idf.get(t, 1.0) for t in shared)
    union_w = sum(idf.get(t, 1.0) for t in union)
    return shared_w / union_w if union_w else 0.0


def _score_components(
    query: Symbol, sym: Symbol, call_idf: dict[str, float], str_idf: dict[str, float]
) -> dict[str, float]:
    return {
        "structural": body_similarity(query.norm_body, sym.norm_body),
        "call": _weighted_jaccard(query.call_names, sym.call_names, call_idf),
        "string": _weighted_jaccard(query.string_literals, sym.string_literals, str_idf),
        "lexical": _tag_similarity(query.tag, sym.tag),
    }


def retrieve_detailed(
    query: Symbol, index: list[Symbol], k: int = 10
) -> list[dict]:
    """Rank the index against *query*, returning score + per-channel evidence.

    Each entry is ``{"score", "symbol", "channels"}`` where ``channels`` keeps
    the individual component scores so the caller can show *why* a candidate
    ranked where it did.
    """
    call_idf = _idf_map([s.call_names for s in index])
    str_idf = _idf_map([s.string_literals for s in index])

    scored: list[dict] = []
    for sym in index:
        if sym.key == query.key:
            continue
        components = _score_components(query, sym, call_idf, str_idf)
        score = max(
            components["structural"],
            components["call"],
            components["string"],
            components["lexical"] * 0.8,
        )
        if score > 0.0:
            scored.append(
                {
                    "score": score,
                    "symbol": sym,
                    "channels": {c: round(v, 4) for c, v in components.items()},
                }
            )
    scored.sort(key=lambda d: (-d["score"], d["symbol"].key))
    return scored[:k]


def retrieve(
    query: Symbol, index: list[Symbol], k: int = 10
) -> list[tuple[float, Symbol]]:
    """Rank the index against *query*; see ``retrieve_detailed`` for the
    per-channel breakdown."""
    return [(d["score"], d["symbol"]) for d in retrieve_detailed(query, index, k)]


def retrieve_with_closure_detailed(
    queries: dict[str, Symbol],
    index: list[Symbol],
    k: int = 10,
) -> dict[str, list[dict]]:
    """Retrieve for each new symbol, then propagate one hop along intra-set calls.

    A composite function that calls another NEW function inherits the latter's
    overlaps: ``save_avatar_variant`` calls ``upload_avatar``, and
    ``upload_avatar`` duplicates ``StorageService.upload``, so the composite
    transitively duplicates it too.  This is the deterministic stand-in for a
    transitive call graph — it needs only the new-symbol set, not a full graph.
    """
    by_name: dict[str, list[Symbol]] = {}
    for sym in queries.values():
        by_name.setdefault(sym.name, []).append(sym)

    results: dict[str, list[dict]] = {}
    for query in queries.values():
        merged: dict[str, dict] = {}

        def _absorb(cands: list[dict]) -> None:
            for d in cands:
                key = d["symbol"].key
                if key not in merged or d["score"] > merged[key]["score"]:
                    merged[key] = d

        _absorb(retrieve_detailed(query, index, k))
        for called_name in query.call_names:
            for callee in by_name.get(called_name, ()):
                if callee.key == query.key:
                    continue
                _absorb(retrieve_detailed(callee, index, k))
        ranked = sorted(
            merged.values(), key=lambda d: (-d["score"], d["symbol"].key)
        )
        results[query.key] = ranked[:k]
    return results


def retrieve_with_closure(
    queries: dict[str, Symbol],
    index: list[Symbol],
    k: int = 10,
) -> dict[str, list[tuple[float, Symbol]]]:
    """``retrieve_with_closure_detailed`` reduced to ``(score, symbol)`` pairs."""
    return {
        key: [(d["score"], d["symbol"]) for d in cands]
        for key, cands in retrieve_with_closure_detailed(queries, index, k).items()
    }


def _changed_py_files(root: Path, base: str) -> list[str]:
    """Return the ``.py`` paths (repo-relative) changed since ``base``.

    Union of tracked modifications (``git diff <base>``) and untracked files
    (``git ls-files --others``) — the latter is the common case: the agent just
    wrote a new file and hasn't committed it.
    """
    import subprocess

    def _run(cmd: list[str]) -> str:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git failed: {proc.stderr.strip()}")
        return proc.stdout

    tracked = _run(["git", "-C", str(root), "diff", "--name-only", base])
    untracked = _run(
        ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard"]
    )
    files = {
        line.strip()
        for line in tracked.splitlines() + untracked.splitlines()
        if line.strip().endswith(".py")
    }
    return sorted(files)


def main(argv: list[str] | None = None) -> int:
    """CLI: surface existing implementations that new/changed code overlaps.

    Two modes:
      - ``--file <path>`` — check one new/changed file;
      - ``--base <ref>``   — ``git diff`` the working tree against ``<ref>`` and
        check every changed ``.py`` file against the pre-change index.

    In both cases the index is the existing codebase (the working tree minus
    the changed files), and the report lists, per new callable, the top
    existing implementations it overlaps with plus the channels that fired.
    """
    import argparse
    import sys

    ap = argparse.ArgumentParser(
        description="Reuse-check: find existing implementations new/changed code overlaps with."
    )
    ap.add_argument(
        "--root", type=Path, required=True,
        help="repository root to index (the existing codebase)",
    )
    ap.add_argument(
        "--file", type=Path, default=None,
        help="new/changed Python file to check",
    )
    ap.add_argument(
        "--base", default=None,
        help="git ref; diff the working tree against it and check the changed .py files",
    )
    ap.add_argument(
        "--symbol", default=None,
        help="optional path:qualname to restrict the query to one callable",
    )
    ap.add_argument("--k", type=int, default=10, help="top-K candidates per symbol")
    ap.add_argument(
        "--min-score", type=float, default=0.3,
        help="only print candidates above this score",
    )
    ap.add_argument(
        "--json", type=Path, default=None,
        help="write machine-readable results as JSON",
    )
    args = ap.parse_args(argv)

    if (args.file is None) == (args.base is None):
        print("error: provide exactly one of --file or --base", file=sys.stderr)
        return 2

    root = args.root.resolve()

    if args.file is not None:
        file_path = args.file.resolve()
        changed = [
            file_path.relative_to(root).as_posix()
            if file_path.is_relative_to(root)
            else file_path.as_posix()
        ]
        index = [s for s in build_index(root) if s.path not in set(changed)]
    else:
        changed = _changed_py_files(root, args.base)
        index = _base_index(root, args.base)

    query_list: list[Symbol] = []
    for rel in changed:
        path = root / rel
        if path.is_file():
            query_list.extend(_extract(path, root))
    if args.symbol:
        query_list = [q for q in query_list if q.key == args.symbol]

    if not query_list:
        print(
            f"no callables found in {len(changed)} changed .py file(s)",
            file=sys.stderr,
        )
        return 1

    results = retrieve_with_closure_detailed(
        {q.key: q for q in query_list}, index, args.k
    )
    for query in query_list:
        ranked = [d for d in results[query.key] if d["score"] >= args.min_score]
        print(f"\n## {query.key}")
        if not ranked:
            print("  (no existing implementations above threshold)")
            continue
        for i, d in enumerate(ranked, start=1):
            top = max(d["channels"], key=d["channels"].get)
            print(
                f"  {i}. {d['symbol'].key:<44} score={d['score']:.3f}  "
                f"[{top}={d['channels'][top]:.3f}]"
            )

    if args.json is not None:
        import json as _json

        from _scanner_common import atomic_write_text

        payload = {
            "schema_version": 1,
            "root": str(root),
            "base": args.base,
            "changed_files": changed,
            "results": [
                {
                    "new_symbol": q.key,
                    "candidates": [
                        {
                            "existing_symbol": d["symbol"].key,
                            "score": round(d["score"], 4),
                            "channels": d["channels"],
                        }
                        for d in results[q.key]
                        if d["score"] >= args.min_score
                    ],
                }
                for q in query_list
            ],
        }
        atomic_write_text(
            args.json, _json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        )
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main())
