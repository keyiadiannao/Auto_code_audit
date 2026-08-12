#!/usr/bin/env python3
"""Find Python modules with no visible import, entry point, or documentation use.

The dependency graph includes three channels: static imports, dynamic
sys.path-pinned bare imports, and importlib file loads
(spec_from_file_location / SourceFileLoader / import_module / __import__).
Dynamic edges are recorded per module so reference counts do not silently
under-count dependencies (a bare ``import runner`` after a ``sys.path`` insert
and ``spec_from_file_location("runner", path)`` are both visible to the graph).

The result is a review queue, not permission to delete files. Static analysis
cannot fully resolve subprocess entry points or scientific provenance
references.
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import _audit_config
from _scanner_common import (
    PY_SUBDIRS,
    load_ignore as _load_ignore,
    matches_module as _matches_module,
    module_name as _module_name,
    write_json as _write_json,
)

DEFAULT_DOC_DIRS = [
    "{pkg}",
    "docs",
    ".github",
]
DOC_EXTS = {
    ".md",
    ".txt",
    ".tex",
    ".bat",
    ".sh",
    ".toml",
    ".yml",
    ".yaml",
    ".ps1",
    ".cfg",
    ".ini",
}
DEFAULT_EXCLUDE = {
    "frozen_source",
    "frozen_results",
    "outputs",
    "artifacts",
    "reports",
    "__pycache__",
}


def _collect(root: Path, exts: set[str], exclude: set[str]) -> list[Path]:
    if not root.is_dir():
        return []
    result = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in exts:
            continue
        if any(part in exclude for part in path.relative_to(root).parts):
            continue
        result.append(path)
    return result


def _string_constants(node) -> list[str]:
    """Collect str-literal constants anywhere in an AST subtree."""
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]


def _call_argument(node, position: int, name: str):
    """Read a call argument by position or keyword, or None."""
    if node.args and len(node.args) > position:
        return node.args[position]
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _is_spec_file_load(node) -> bool:
    """True for spec_from_file_location / SourceFileLoader call nodes."""
    if not isinstance(node, ast.Call):
        return False
    attr = node.func.attr if isinstance(node.func, ast.Attribute) else None
    return attr in {"spec_from_file_location", "SourceFileLoader"}


def _dynamic_import_hints(tree) -> list[tuple[str, str, int]]:
    """Extract (mechanism, hint, lineno) from importlib / __import__ calls.

    Mechanisms:
    - "importlib_file": spec_from_file_location / SourceFileLoader, hint is a
      file path (or module nickname when the path is not a string literal).
    - "importlib_module": importlib.import_module, hint is a module name.
    - "dunder_import": __import__, hint is a module name.

    Direct call sites are resolved first. When a spec call's arguments are
    bare parameters of an enclosing function (a thin ``_load(name, path)``
    wrapper), hints are recovered from that function's call sites.
    """
    hints: list[tuple[str, str, int]] = []
    for node in ast.walk(tree):
        if not _is_spec_file_load(node):
            continue
        name_arg = _call_argument(node, 0, "fullname") or _call_argument(
            node, 0, "name"
        )
        path_arg = _call_argument(node, 1, "location") or _call_argument(
            node, 1, "path"
        )
        path_consts = _string_constants(path_arg) if path_arg is not None else []
        name_consts = _string_constants(name_arg) if name_arg is not None else []
        if path_consts:
            hints.append(("importlib_file", path_consts[0], node.lineno))
        elif name_consts:
            hints.append(("importlib_file", name_consts[0], node.lineno))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        attr = node.func.attr if isinstance(node.func, ast.Attribute) else None
        if attr == "import_module" or (
            isinstance(node.func, ast.Name) and node.func.id == "__import__"
        ):
            name_arg = _call_argument(node, 0, "name")
            name_consts = _string_constants(name_arg) if name_arg is not None else []
            if name_consts:
                mechanism = (
                    "importlib_module" if attr == "import_module" else "dunder_import"
                )
                hints.append((mechanism, name_consts[0], node.lineno))

    # Wrapper pattern: spec call inside a function whose name/path arguments
    # are the function's own parameters, with literal call sites in the file.
    for function in (
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    ):
        params = {arg.arg for arg in function.args.args}
        param_slots: list[tuple[int, str]] = []
        for node in ast.walk(function):
            if not _is_spec_file_load(node):
                continue
            name_arg = _call_argument(node, 0, "fullname") or _call_argument(
                node, 0, "name"
            )
            path_arg = _call_argument(node, 1, "location") or _call_argument(
                node, 1, "path"
            )
            if isinstance(name_arg, ast.Name) and name_arg.id in params:
                param_slots.append((0, name_arg.id))
            if isinstance(path_arg, ast.Name) and path_arg.id in params:
                param_slots.append((1, path_arg.id))
        if not param_slots:
            continue
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == function.name
            ):
                continue
            # One hint per call site, preferring the path slot: the path and
            # the nickname may both resolve to the same target, and the path
            # is the more precise hint.
            for position, _param in sorted(param_slots, reverse=True):
                argument = _call_argument(node, position, None)
                consts = _string_constants(argument) if argument is not None else []
                if consts:
                    hints.append(("importlib_file", consts[0], node.lineno))
                    break
    return hints


def _syspath_subdirs(tree, package_root: Path) -> set[str]:
    """Return package subdirectory names a file pins onto sys.path.

    Collects string constants inside ``sys.path.insert/append`` arguments and
    intersects them with the package's immediate subdirectory names. Empty
    result means the mutation is uninformative (e.g. repo root only); callers
    may then fall back to whole-package basename matching.
    """
    subdir_names = {path.name for path in package_root.iterdir() if path.is_dir()}
    subdir_names.add(package_root.name)
    matched: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"insert", "append"}
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "path"
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "sys"
        ):
            continue
        for constant in _string_constants(node):
            for subdir in subdir_names:
                if subdir in constant:
                    matched.add(subdir)
    return matched


def _analyze_source(
    path: Path, package_root: Path
) -> tuple[set[str], bool, str | None, list[tuple[str, str, int]], set[str]]:
    """Parse one source file into its dependency signals.

    Returns (imports, has_entrypoint, failure, dynamic_hints, syspath_subdirs).
    """
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return set(), False, f"{exc.msg} at line {exc.lineno}", [], set()

    imports: set[str] = set()
    rel_parts = list(path.relative_to(package_root).with_suffix("").parts)
    package_parts = rel_parts if rel_parts[-1:] == ["__init__"] else rel_parts[:-1]
    if package_parts[-1:] == ["__init__"]:
        package_parts.pop()

    has_entrypoint = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = max(0, len(package_parts) - (node.level - 1))
                prefix = package_parts[:keep]
            else:
                prefix = []
            module_parts = node.module.split(".") if node.module else []
            base = ".".join(prefix + module_parts)
            if base:
                imports.add(base)
            for alias in node.names:
                if alias.name != "*":
                    imports.add(".".join(part for part in (base, alias.name) if part))
        elif isinstance(node, ast.If):
            test = node.test
            if (
                isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"
                and any(
                    isinstance(item, ast.Constant) and item.value == "__main__"
                    for item in test.comparators
                )
            ):
                has_entrypoint = True
    return imports, has_entrypoint, None, _dynamic_import_hints(tree), _syspath_subdirs(
        tree, package_root
    )


def _resolve_dynamic_target(
    hint: str, source_path: Path, package_root: Path, modules: dict[Path, str]
) -> str | None:
    """Map an importlib hint to a package module name, or None.

    Path hints (ending in .py) resolve against the source directory and the
    package root. Module-name hints match an exact package module first, then
    a module whose basename equals the hint or starts with ``hint_`` (the
    ``spec_from_file_location("runner", ...)`` nickname pattern).
    """
    if hint.endswith(".py"):
        for candidate in (source_path.parent / hint, package_root / hint, Path(hint)):
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved in modules:
                return modules[resolved]
        return None
    if hint in modules.values() or any(
        hint.startswith(module + ".") for module in modules.values()
    ):
        return hint
    name = hint.rsplit(".", 1)[-1]
    for target_module in modules.values():
        base = target_module.rsplit(".", 1)[-1]
        if base == name or base.startswith(name + "_"):
            return target_module
    return None


def _references(
    needles: list[str], haystacks: dict[Path, str], repo: Path
) -> list[str]:
    regexes = [re.compile(rf"(?<![A-Za-z0-9_]){re.escape(x)}(?![A-Za-z0-9_])") for x in needles if x]
    hits = []
    for path, text in haystacks.items():
        if any(regex.search(text) for regex in regexes):
            try:
                hits.append(path.relative_to(repo).as_posix())
            except ValueError:
                hits.append(path.as_posix())
    return sorted(set(hits))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repo root (default: script's repo)",
    )
    ap.add_argument("--package", default="src")
    ap.add_argument("--subdirs", nargs="*", default=None,
                    help="package subdirs to scan (default: all subdirs in config "
                         "or PY_SUBDIRS)")
    ap.add_argument("--doc-dirs", nargs="*", default=None)
    ap.add_argument("--exclude", nargs="*", default=None)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--ignore", type=Path, default=None)
    ap.add_argument("--no-doc-channel", action="store_true")
    args = ap.parse_args(argv)

    repo = args.root.resolve()
    pkg = (repo / args.package).resolve()
    if not pkg.is_dir() or pkg != repo / args.package:
        print(f"error: package dir not found or escapes root: {pkg}", file=sys.stderr)
        return 2

    cfg = _audit_config.load_config(repo)
    dead_cfg = cfg.get("deadcode", {})
    subdirs = _audit_config.pick(args.subdirs, cfg, "subdirs", list(PY_SUBDIRS))
    exclude = set(
        _audit_config.pick(args.exclude, dead_cfg, "exclude", sorted(DEFAULT_EXCLUDE))
    )
    doc_dir_templates = _audit_config.as_string_list(
        dead_cfg.get("doc_dirs"), DEFAULT_DOC_DIRS
    )
    extra_doc_dirs = args.doc_dirs or []
    py_files = []
    for sub in subdirs:
        subdir = pkg / sub
        if not subdir.is_dir():
            continue
        for path in subdir.rglob("*.py"):
            if any(part in exclude for part in path.relative_to(pkg).parts):
                continue
            py_files.append(path)
    py_files.sort()

    imports_by_file: dict[Path, set[str]] = {}
    entrypoints: dict[Path, bool] = {}
    dynamic_hints_by_file: dict[Path, list[tuple[str, str, int]]] = {}
    syspath_subdirs_by_file: dict[Path, set[str]] = {}
    parse_failures: dict[str, str] = {}
    py_text: dict[Path, str] = {}
    for path in py_files:
        py_text[path] = path.read_text(encoding="utf-8-sig", errors="replace")
        imports, entrypoint, failure, dynamic_hints, syspath_subdirs = _analyze_source(
            path, pkg
        )
        imports_by_file[path] = imports
        entrypoints[path] = entrypoint
        dynamic_hints_by_file[path] = dynamic_hints
        syspath_subdirs_by_file[path] = syspath_subdirs
        if failure:
            parse_failures[path.relative_to(pkg).as_posix()] = failure

    import_refs: dict[str, list[str]] = defaultdict(list)
    dynamic_edges: dict[str, list[dict]] = defaultdict(list)
    modules = {path: _module_name(path, pkg) for path in py_files}
    for target_path, target_module in modules.items():
        if not target_module:
            continue
        for source_path, imported_names in imports_by_file.items():
            if source_path == target_path:
                continue
            if any(_matches_module(name, target_module) for name in imported_names):
                import_refs[target_module].append(source_path.relative_to(pkg).as_posix())

    # Dynamic channel 1: bare imports under a sys.path-pinned subdirectory.
    for source_path, imported_names in imports_by_file.items():
        pinned = syspath_subdirs_by_file[source_path]
        if not pinned:
            continue
        for name in sorted(imported_names):
            if "." in name or any(
                _matches_module(name, target_module) for target_module in modules.values()
            ):
                continue
            for target_path, target_module in modules.items():
                if target_path == source_path or not target_module:
                    continue
                base = target_module.rsplit(".", 1)[-1]
                if base != name:
                    continue
                first_segment = target_module.split(".", 1)[0]
                if pinned and first_segment not in pinned:
                    continue
                rel = source_path.relative_to(pkg).as_posix()
                dynamic_edges[target_module].append(
                    {
                        "path": rel,
                        "mechanism": "syspath_bare_import",
                        "lineno": None,
                    }
                )
                import_refs[target_module].append(rel)
                break

    # Dynamic channel 2: importlib file loads and module imports.
    for source_path, hints in dynamic_hints_by_file.items():
        rel = source_path.relative_to(pkg).as_posix()
        for mechanism, hint, lineno in hints:
            target_module = _resolve_dynamic_target(hint, source_path, pkg, modules)
            if target_module is None or target_module == _module_name(
                source_path, pkg
            ):
                continue
            dynamic_edges[target_module].append(
                {"path": rel, "mechanism": mechanism, "lineno": lineno}
            )
            import_refs[target_module].append(rel)

    docs: set[Path] = set()
    if not args.no_doc_channel:
        doc_dirs = []
        for template in doc_dir_templates:
            candidate = (repo / template.format(pkg=args.package)).resolve()
            if candidate.is_dir():
                doc_dirs.append(candidate)
        for extra in extra_doc_dirs:
            candidate = (repo / extra).resolve()
            if candidate.is_dir():
                doc_dirs.append(candidate)
        for directory in doc_dirs:
            docs.update(_collect(directory, DOC_EXTS, exclude))
        for path in repo.iterdir():
            if path.is_file() and path.suffix.lower() in DOC_EXTS:
                docs.add(path)
    doc_text = {
        path: path.read_text(encoding="utf-8", errors="replace") for path in sorted(docs)
    }

    ignore = _load_ignore(args.ignore)
    ignore_entries = {
        entry["path"]: entry.get("reason", "")
        for entry in ignore.get("deadcode", [])
        if entry.get("path")
    }

    report = []
    skipped = []
    for path in py_files:
        rel = path.relative_to(pkg).as_posix()
        if rel in ignore_entries:
            skipped.append({"path": rel, "reason": ignore_entries[rel]})
            continue
        module = modules[path]
        direct_refs = sorted(set(import_refs.get(module, [])))
        dyn_edges = sorted(
            dynamic_edges.get(module, []),
            key=lambda edge: (edge["path"], edge["mechanism"], edge["lineno"] or 0),
        )
        dynamic_refs = []
        if not direct_refs and module:
            needles = [module, rel, Path(rel).name]
            others = {item: text for item, text in py_text.items() if item != path}
            dynamic_refs = _references(needles, others, pkg)
        doc_refs = (
            _references([rel, Path(rel).name, path.stem, module], doc_text, repo)
            if doc_text
            else []
        )
        is_test = "tests" in path.relative_to(pkg).parts
        is_package_init = path.name == "__init__.py"
        if rel in parse_failures:
            status = "PARSE-ERROR"
        elif is_test:
            status = "TEST"
        elif is_package_init:
            status = "PACKAGE"
        elif direct_refs or dyn_edges or dynamic_refs:
            status = "USED"
        elif entrypoints[path]:
            status = "ENTRYPOINT"
        elif doc_refs:
            status = "DOC-ONLY"
        else:
            status = "DEAD"
        report.append(
            {
                "path": rel,
                "module": module,
                "status": status,
                "py_refs": direct_refs[:12],
                "dynamic_edges": dyn_edges,
                "dynamic_refs": dynamic_refs[:12],
                "doc_refs": doc_refs[:12],
                "has_main_guard": entrypoints[path],
            }
        )

    totals = dict(Counter(item["status"] for item in report))
    all_dynamic_edges = sorted(
        (
            {"target": target, **edge}
            for target, edges in dynamic_edges.items()
            for edge in edges
        ),
        key=lambda edge: (edge["target"], edge["path"], edge["mechanism"]),
    )
    payload = {
        "scanner": "deadcode",
        "schema_version": 1,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "package": args.package,
        "document_channel": not args.no_doc_channel,
        "documents_scanned": len(doc_text),
        "totals": totals,
        "candidates": [
            item
            for item in report
            if item["status"] in {"DEAD", "DOC-ONLY", "PARSE-ERROR"}
        ],
        "entrypoints": [item for item in report if item["status"] == "ENTRYPOINT"],
        "parse_failures": parse_failures,
        "skipped": skipped,
        "modules": report,
        "dynamic_import_edges": all_dynamic_edges,
    }
    _write_json(args.json, payload)

    print(
        f"DEADCODE_SCAN package={args.package} scanned={len(report)} "
        f"USED={totals.get('USED', 0)} ENTRYPOINT={totals.get('ENTRYPOINT', 0)} "
        f"PACKAGE={totals.get('PACKAGE', 0)} "
        f"PARSE_ERROR={totals.get('PARSE-ERROR', 0)} "
        f"DOC_ONLY={totals.get('DOC-ONLY', 0)} DEAD={totals.get('DEAD', 0)} "
        f"TEST={totals.get('TEST', 0)} DYNAMIC_EDGES={len(all_dynamic_edges)} "
        f"skipped={len(skipped)}"
    )
    for edge in all_dynamic_edges:
        lineno = edge.get("lineno") or "-"
        print(f"  [dynamic] {edge['path']} {edge['mechanism']} -> {edge['target']} (L{lineno})")
    for item in payload["candidates"]:
        suffix = ""
        if item["doc_refs"]:
            suffix = " doc: " + ", ".join(item["doc_refs"][:4])
        print(f"  [{item['status']}] {item['path']}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
