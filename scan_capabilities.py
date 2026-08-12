#!/usr/bin/env python3
"""Build a lib capability index and flag overlap with local definitions.

This scanner implements the "capability registry" idea: every reusable lib
function/class gets a one-line tag (its docstring first line), and any
specialized script that defines its own function is checked against the
index.  The scanner generates review candidates only -- deciding whether a
local definition is (a) a thin wrapper that should import the lib version,
(b) a genuine contract variant, or (c) group-specific logic with no lib
counterpart stays a human judgment.

Two workflows:

* Full overlap report (default): scan every local definition against the
  lib index and list candidates.  Each record carries a ``signature_match``
  flag -- the fastest triage key.  Same name + same signature shape is
  usually a true duplicate; same name + different signature shape is usually
  a contract variant or a name collision.  Registry health (how many lib
  capabilities have no docstring tag and are therefore invisible to doc
  matching) is reported so docstring gaps stay visible.

* Single-file checklist (--file): after writing a long specialized script,
  run ``scan_capabilities.py --file experiments/eXX.py`` to see, for every
  definition in that file, which lib capabilities already exist under the
  same name or with a similar docstring tag.  Definitions with no lib
  counterpart are listed as such -- that is the group-specific logic case
  of the user's build-and-check workflow.

The lib capability index (--index) is machine-generated from docstrings and
must not be hand-edited: stale tags are worse than no tags.
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import difflib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import _audit_config
from _scanner_common import (
    EXCLUDE_PARTS,
    PY_SUBDIRS,
    signature_shape as _signature_shape,
    write_json as _write_json,
)
BOILERPLATE = {
    "main",
    "parse_args",
    "require",
    "close",
    "count",
    "load_ignore",
    "_write_json",
    "checkpoint_step",
    "stratum_name",
}


@dataclass(frozen=True)
class Capability:
    """One callable with its docstring first line acting as its tag."""

    path: str
    name: str
    qualname: str
    lineno: int
    signature: str
    sig_shape: str  # parameter kinds + default presence, no names
    tag: str  # docstring first line, normalized for matching

    @property
    def key(self) -> str:
        return f"{self.path}:{self.qualname}"


def _module_first_line(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        tree = ast.parse(text)
        return ast.get_docstring(tree) or ""
    except (OSError, UnicodeError, SyntaxError):
        return ""


def _doc_first_line(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    doc = ast.get_docstring(node)
    if not doc:
        return ""
    return doc.strip().splitlines()[0].strip()


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    return f"({ast.unparse(node.args)})"


def extract_capabilities(path: Path) -> list[Capability]:
    """Top-level functions and class methods, each with its docstring tag."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    tree = ast.parse(text)
    rel = path.as_posix()
    out: list[Capability] = []

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(
                Capability(
                    path=rel,
                    name=node.name,
                    qualname=node.name,
                    lineno=node.lineno,
                    signature=_signature(node),
                    sig_shape=_signature_shape(node),
                    tag=_doc_first_line(node),
                )
            )
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(
                        Capability(
                            path=rel,
                            name=child.name,
                            qualname=f"{node.name}.{child.name}",
                            lineno=child.lineno,
                            signature=_signature(child),
                            sig_shape=_signature_shape(child),
                            tag=_doc_first_line(child),
                        )
                    )
    return out


def _normalize(text: str) -> str:
    text = re.sub(r"[_\-.,;:()\[\]{}/]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _tag_tokens(text: str) -> tuple[str, ...]:
    return tuple(_normalize(text).split())


def tag_similarity(left: str, right: str) -> float:
    a, b = _tag_tokens(left), _tag_tokens(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    shorter, longer = sorted((a, b))
    if len(shorter) / len(longer) < 0.4:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _usage_counts(
    root: Path, package: str, names: list[str], subdirs: list[str]
) -> dict[str, int]:
    """Rough text-occurrence count of each name across package .py files."""
    counts: dict[str, int] = {name: 0 for name in names}
    for sub in subdirs:
        subdir = root / package / sub
        if not subdir.is_dir():
            continue
        for path in subdir.rglob("*.py"):
            if any(part in EXCLUDE_PARTS for part in path.relative_to(root / package).parts):
                continue
            try:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            for name in names:
                counts[name] += len(re.findall(rf"\b{re.escape(name)}\b", text))
    return counts


def _local_dict(local: Capability) -> dict:
    return {
        "path": local.path,
        "qualname": local.qualname,
        "lineno": local.lineno,
        "signature": local.signature,
        "doc": local.tag,
    }


def _lib_dict(item: Capability, usage: dict[str, int]) -> dict:
    return {
        "path": item.path,
        "qualname": item.qualname,
        "lineno": item.lineno,
        "signature": item.signature,
        "occurrences": usage[item.name],
        "doc": item.tag,
    }


def _match_candidates(
    local: Capability,
    index: list[Capability],
    by_name: dict[str, list[Capability]],
    doc_threshold: float,
) -> tuple[list[tuple[float, Capability]], str]:
    """Return ``(candidates, channel)`` with channel in name/doc/none."""
    candidates = [(1.0, item) for item in by_name.get(local.name, [])]
    if candidates:
        return candidates, "name"
    if local.tag:
        doc = [
            (sim, item)
            for item in index
            if (sim := tag_similarity(local.tag, item.tag)) >= doc_threshold
        ]
        if doc:
            return doc, "doc"
    return [], "none"


def _run_file_check(
    args: argparse.Namespace,
    repo: Path,
    pkg: Path,
    index: list[Capability],
    usage: dict[str, int],
    by_name: dict[str, list[Capability]],
    doc_threshold: float,
) -> int:
    """Single-file checklist: every def in one script vs the lib index."""
    rel = Path(args.file).as_posix()
    path = (pkg / rel).resolve()
    if not path.is_file() or pkg not in path.parents:
        print(f"error: --file must be an existing file inside the package: {rel}", file=sys.stderr)
        return 2
    if rel.startswith("tests/"):
        print(f"error: --file is for specialized scripts, not tests: {rel}", file=sys.stderr)
        return 2
    try:
        extracted = extract_capabilities(path)
    except (OSError, UnicodeError, SyntaxError) as exc:
        print(f"error: cannot parse {rel}: {exc}", file=sys.stderr)
        return 2

    records: list[dict] = []
    for local in extracted:
        if local.name in BOILERPLATE:
            continue
        candidates, channel = _match_candidates(local, index, by_name, doc_threshold)
        records.append(
            {
                "local": _local_dict(local),
                "mode": channel,
                "lib_candidates": [
                    {
                        **dict(_lib_dict(item, usage)),
                        "signature_match": local.sig_shape == item.sig_shape,
                        "similarity": round(sim, 3),
                    }
                    for sim, item in candidates
                ],
            }
        )

    def _sort_key(record: dict) -> tuple:
        lib = record["lib_candidates"]
        return (
            -int(bool(lib)),
            -int(lib[0]["signature_match"]) if lib else 0,
            record["local"]["qualname"],
        )

    records.sort(key=_sort_key)

    payload = {
        "scanner": "capabilities-file-check",
        "schema_version": 1,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "package": args.package,
        "file": rel,
        "definitions": records,
    }
    _write_json(args.json, payload)

    with_lib = sum(1 for r in records if r["lib_candidates"])
    print(
        f"FILE_CHECK {rel} defs={len(records)} with_lib_candidates={with_lib} "
        f"no_counterpart={len(records) - with_lib}"
    )
    for record in records:
        local = record["local"]
        if record["lib_candidates"]:
            print(f"=== {local['qualname']}{local['signature']} (L{local['lineno']})")
            for candidate in record["lib_candidates"]:
                marker = "SIG-MATCH" if candidate["signature_match"] else (
                    f"doc sim={candidate['similarity']:.2f}"
                    if record["mode"] == "doc"
                    else "diff-sig"
                )
                print(
                    f"    -> lib {candidate['path']}:{candidate['qualname']} "
                    f"(L{candidate['lineno']}, refs={candidate['occurrences']}) [{marker}]"
                )
        else:
            print(f"=== {local['qualname']}{local['signature']} (L{local['lineno']}) -- no lib counterpart")
    return 0


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
        "--file",
        default=None,
        help="check one package-relative file (e.g. src/experiments/run_experiment.py) "
             "against the lib index and exit",
    )
    ap.add_argument("--index", type=Path, default=None, help="write capability index JSON")
    ap.add_argument("--json", type=Path, default=None, help="write overlap report JSON")
    ap.add_argument("--doc-threshold", type=float, default=None)
    ap.add_argument("--top", type=int, default=None, help="max overlap candidates to print")
    ap.add_argument(
        "--ignore",
        type=Path,
        default=None,
        help="ignore.json whose 'capabilities' list holds 'path:qualname' keys to drop",
    )
    args = ap.parse_args(argv)

    repo = args.root.resolve()
    pkg = (repo / args.package).resolve()
    if not pkg.is_dir() or pkg != repo / args.package:
        print(f"error: package dir not found or escapes root: {pkg}", file=sys.stderr)
        return 2

    cfg = _audit_config.load_config(repo)
    cap_cfg = cfg.get("capabilities", {})
    doc_threshold = _audit_config.pick(args.doc_threshold, cap_cfg, "doc_threshold", 0.55)
    top = _audit_config.pick(args.top, cap_cfg, "top", 40)
    subdirs = _audit_config.as_string_list(cfg.get("subdirs"), list(PY_SUBDIRS))

    lib_dir = pkg / "lib"
    index: list[Capability] = []
    parse_failures: list[dict] = []
    if lib_dir.is_dir():
        for path in sorted(lib_dir.glob("*.py")):
            try:
                index.extend(extract_capabilities(path))
            except (OSError, UnicodeError, SyntaxError) as exc:
                parse_failures.append(
                    {"path": path.relative_to(pkg).as_posix(), "error": str(exc)}
                )

    # Reference count of each lib capability across the package.
    lib_names = sorted({item.name for item in index})
    usage = _usage_counts(repo, args.package, lib_names, subdirs)
    index_records = [
        {
            **dict(_lib_dict(item, usage)),
            "sig_shape": item.sig_shape,
        }
        for item in sorted(index, key=lambda item: item.key)
    ]

    by_name: dict[str, list[Capability]] = {}
    for item in index:
        by_name.setdefault(item.name, []).append(item)

    if args.file is not None:
        return _run_file_check(args, repo, pkg, index, usage, by_name, doc_threshold)

    local_defs: list[Capability] = []
    for sub in subdirs:
        if sub == "lib":
            continue
        subdir = pkg / sub
        if not subdir.is_dir():
            continue
        for path in sorted(subdir.rglob("*.py")):
            rel_parts = path.relative_to(pkg).parts
            if any(part in EXCLUDE_PARTS for part in rel_parts):
                continue
            try:
                extracted = extract_capabilities(path)
            except (OSError, UnicodeError, SyntaxError) as exc:
                parse_failures.append(
                    {"path": path.relative_to(pkg).as_posix(), "error": str(exc)}
                )
                continue
            rel = path.relative_to(pkg).as_posix()
            local_defs.extend(
                Capability(
                    path=rel,
                    name=item.name,
                    qualname=item.qualname,
                    lineno=item.lineno,
                    signature=item.signature,
                    sig_shape=item.sig_shape,
                    tag=item.tag,
                )
                for item in extracted
            )

    overlap: list[dict] = []
    match_modes = {"name": 0, "doc": 0}
    for local in local_defs:
        if local.name in BOILERPLATE:
            continue
        if local.path.startswith("tests/"):
            continue
        candidates, channel = _match_candidates(
            local, index, by_name, doc_threshold
        )
        if channel == "none":
            continue
        match_modes[channel] += 1
        best_sim, best = max(candidates, key=lambda pair: pair[0])
        overlap.append(
            {
                "local": _local_dict(local),
                "lib": _lib_dict(best, usage),
                "match": channel,
                "signature_match": local.sig_shape == best.sig_shape,
                "similarity": round(best_sim, 3),
                "verdict": "",  # human: real issue / contract variant / false positive
            }
        )
    overlap.sort(
        key=lambda item: (
            -int(item["signature_match"]),
            -item["similarity"],
            item["local"]["path"],
        )
    )

    ignored: set[str] = set()
    if args.ignore is not None and args.ignore.is_file():
        try:
            ignore_data = json.loads(args.ignore.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            ignore_data = {}
        for entry in ignore_data.get("capabilities", []) or []:
            if isinstance(entry, str):
                ignored.add(entry)
            elif isinstance(entry, dict) and entry.get("key"):
                ignored.add(entry["key"])
    overlap = [
        item
        for item in overlap
        if f"{item['local']['path']}:{item['local']['qualname']}" not in ignored
    ]

    untagged = [f"{item.path}:{item.qualname}" for item in index if not item.tag]
    payload = {
        "scanner": "capabilities",
        "schema_version": 1,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "package": args.package,
        "lib_capabilities": len(index),
        "local_definitions_scanned": len(local_defs),
        "overlap_candidates": len(overlap),
        "match_modes": match_modes,
        "untagged_lib_capabilities": untagged,
        "parse_failures": parse_failures,
        "index": index_records,
        "overlap": overlap,
    }
    _write_json(args.index, {"generated_at": payload["generated_at"], "index": index_records})
    _write_json(args.json, payload)

    sig_matches = sum(1 for item in overlap if item["signature_match"])
    print(
        f"CAP_SCAN package={args.package} lib_caps={len(index)} "
        f"local_defs={len(local_defs)} overlap={len(overlap)} "
        f"(name={match_modes['name']} doc={match_modes['doc']}) "
        f"sig_match={sig_matches} untagged={len(untagged)}"
    )
    for item in overlap[:top]:
        local = item["local"]
        lib = item["lib"]
        marker = " SIG-MATCH" if item["signature_match"] else ""
        print(
            f"=== [{item['match']} sim={item['similarity']:.2f}{marker}] "
            f"{local['path']}:{local['qualname']} (L{local['lineno']})"
        )
        print(f"    -> lib {lib['path']}:{lib['qualname']} (L{lib['lineno']}, "
              f"refs={lib['occurrences']})")
        if local["doc"] or lib["doc"]:
            print(f"    local doc: {local['doc']}")
            print(f"    lib   doc: {lib['doc']}")
    if not overlap:
        print("No overlap candidates.")
    if untagged:
        print(f"NOTE: {len(untagged)} lib capabilities have no docstring tag "
              f"(invisible to doc matching) -- see report untagged_lib_capabilities")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
