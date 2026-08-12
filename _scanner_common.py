"""Shared utilities for all scanners.

This module centralizes the JSON I/O, ignore-file loading, directory constants,
and AST helper functions that were previously duplicated across 6+ scanner
modules or imported cross-scanner (creating hidden coupling).

Extracting these here is the prerequisite for the future ``src/auto_code_audit/``
package migration: once scanners no longer import from each other, they can be
moved into a ``scanners/`` subpackage without import-path surgery.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator, cast

# ---------------------------------------------------------------------------
# Directory / exclusion constants
# ---------------------------------------------------------------------------

#: Package subdirectories scanned by default (used by duplicates, forks,
#: capabilities, contracts).  ``tests`` is included so scanners that want
#: to skip tests can do so explicitly.
PY_SUBDIRS = (
    "lib",
    "experiments",
    "mechanism",
    "audit",
    "verify",
    "figures",
    "tests",
)

#: Path components that cause a file to be skipped by every scanner.
EXCLUDE_PARTS = {"frozen_source", "__pycache__"}


# ---------------------------------------------------------------------------
# JSON I / O  (was duplicated 7 times)
# ---------------------------------------------------------------------------

def write_json(path: Path | None, payload: dict) -> None:
    """Write *payload* to *path* as pretty-printed JSON (no trailing newline).

    ``path`` may be ``None`` (no-op), which lets callers pass ``--json``
    without a value.
    """
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Ignore-file loading  (was duplicated 4 times + 1 cross-scanner import)
# ---------------------------------------------------------------------------

def load_ignore(path: Path | None) -> dict:
    """Load an ``ignore.json`` file, returning ``{}`` when missing or unreadable."""
    if path and path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


# ---------------------------------------------------------------------------
# AST helpers  (were imported cross-scanner: scan_forks -> scan_duplicates,
#               scan_forks -> scan_capabilities, scan_forks -> scan_deadcode)
# ---------------------------------------------------------------------------

class NormalizeAST(ast.NodeTransformer):
    """Normalize local names and literals while retaining called APIs.

    Used by ``scan_duplicates`` and ``scan_forks`` to compare function bodies
    structurally: all ``Name`` nodes become ``_name``, all ``arg`` nodes
    become ``_arg``, and all constants are replaced with type-placeholder
    values (``"_str"``, ``0``, ``b"_bytes"``, etc.).
    """

    def visit_Name(self, node: ast.Name) -> ast.Name:
        return ast.copy_location(ast.Name(id="_name", ctx=node.ctx), node)

    def visit_arg(self, node: ast.arg) -> ast.arg:
        node.arg = "_arg"
        self.generic_visit(node)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.Constant:
        value = node.value
        if value is None or isinstance(value, bool):
            replacement: object = value
        elif isinstance(value, str):
            replacement = "_str"
        elif isinstance(value, bytes):
            replacement = b"_bytes"
        elif isinstance(value, (int, float, complex)):
            replacement = 0
        else:
            replacement = None
        return ast.copy_location(
            ast.Constant(value=cast(Any, replacement)), node
        )

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.Constant:
        return ast.copy_location(ast.Constant(value="_fstr"), node)


def without_docstring(node: ast.FunctionDef | ast.AsyncFunctionDef):
    """Return a deep-copied function node with its docstring stripped.

    The node's ``name`` is reset to ``"_function"`` so it does not influence
    structural comparison.  Used by ``scan_duplicates`` and ``scan_forks``.
    """
    cloned = copy.deepcopy(node)
    cloned.name = "_function"
    if (
        cloned.body
        and isinstance(cloned.body[0], ast.Expr)
        and isinstance(cloned.body[0].value, ast.Constant)
        and isinstance(cloned.body[0].value.value, str)
    ):
        cloned.body = cloned.body[1:]
    return cloned


def signature_shape(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Parameter kinds and default presence only, e.g. ``(p, p, p=)``.

    Names are dropped so two callables that differ only in parameter naming
    still compare as the same shape.  Used by ``scan_capabilities`` and
    ``scan_forks``.
    """
    args = node.args
    n_pos = len(args.posonlyargs) + len(args.args)
    n_defaults = len(args.defaults)
    bits = ["p"] * (n_pos - n_defaults) + ["p="] * n_defaults
    if args.vararg:
        bits.append("*p")
    for index, arg in enumerate(args.kwonlyargs):
        default = args.kw_defaults[index] if index < len(args.kw_defaults) else None
        bits.append("k=" if default is not None else "k")
    if args.kwarg:
        bits.append("**p")
    return f"({', '.join(bits)})"


def module_name(path: Path, package_root: Path) -> str:
    """Dotted module name for *path* relative to *package_root*.

    ``__init__.py`` is stripped: ``lib/foo/__init__.py`` -> ``lib.foo``.
    Used by ``scan_deadcode`` and ``scan_forks``.
    """
    parts = list(path.relative_to(package_root).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def matches_module(imported: str, target: str) -> bool:
    """True when *imported* is *target* or a submodule of it.

    ``"lib.foo"`` matches ``"lib"``; ``"libfoo"`` does not.
    Used by ``scan_deadcode`` and ``scan_forks``.
    """
    return imported == target or imported.startswith(target + ".")


def extract_imports(path: Path, package_root: Path) -> set[str]:
    """Return the set of module names imported by *path*.

    Handles both absolute (``import foo``) and relative (``from . import bar``)
    imports, resolving the latter against the file's position in *package_root*.
    Used by ``scan_forks`` to determine whether fork sides already import
    each other (live coupling vs inert copy).
    """
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()

    imports: set[str] = set()
    rel_parts = list(path.relative_to(package_root).with_suffix("").parts)
    package_parts = rel_parts if rel_parts[-1:] == ["__init__"] else rel_parts[:-1]
    if package_parts[-1:] == ["__init__"]:
        package_parts.pop()

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
    return imports


# ---------------------------------------------------------------------------
# Function walker  (was duplicated verbatim in scan_duplicates and scan_forks)
# ---------------------------------------------------------------------------

def iter_functions(
    node: ast.AST,
    parents: tuple[str, ...] = (),
) -> Iterator[tuple[ast.FunctionDef | ast.AsyncFunctionDef, tuple[str, ...]]]:
    """Yield ``(function_node, parent_qualname_parts)`` for every nested def.

    Walks the AST depth-first, descending into classes and nested functions.
    *parents* accumulates the enclosing class/function names so the caller can
    build a qualified name like ``ClassName.method_name.inner``.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield child, parents
            yield from iter_functions(child, parents + (child.name,))
        elif isinstance(child, ast.ClassDef):
            yield from iter_functions(child, parents + (child.name,))
        else:
            yield from iter_functions(child, parents)


# ---------------------------------------------------------------------------
# Short identifier hash  (was duplicated in scan_hardcoded, scan_style,
#                         scan_duplicates with identical [:12] truncation)
# ---------------------------------------------------------------------------

def collect_py_files(
    package_root: Path,
    subdirs: list[str] | None = None,
    exclude_parts: set[str] | None = None,
) -> list[Path]:
    """Collect ``.py`` files under *package_root*, optionally filtered by subdirs.

    When *subdirs* is ``None``, empty, or contains ``"."``, scans all ``.py``
    files recursively under *package_root* (the ``--all-py`` mode).  Otherwise
    only scans the named subdirectories.

    *exclude_parts* defaults to :data:`EXCLUDE_PARTS`; any path component in
    the set causes the file to be skipped.
    """
    if exclude_parts is None:
        exclude_parts = EXCLUDE_PARTS
    files: list[Path] = []
    if not subdirs or "." in subdirs:
        for path in package_root.rglob("*.py"):
            if any(part in exclude_parts for part in path.relative_to(package_root).parts):
                continue
            files.append(path)
    else:
        for sub in subdirs:
            subdir = package_root / sub
            if not subdir.is_dir():
                continue
            for path in subdir.rglob("*.py"):
                if any(part in exclude_parts for part in path.relative_to(package_root).parts):
                    continue
                files.append(path)
    return sorted(files)


def short_hash(*parts: str) -> str:
    """Return a 12-char hex digest from the concatenation of *parts*.

    Used for stable candidate/cluster identifiers that only need to avoid
    collisions within a single scan report, not cryptographic strength.
    """
    raw = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def source_tree_sha256(root: Path, package: str | None, all_py: bool = False) -> str:
    """Canonical content fingerprint of the audited source tree.

    Walks the scanned scope — ``package``'s directory, or the whole ``root``
    in ``--all-py`` mode — and hashes every ``*.py`` file as
    ``relpath(scope-relative, posix) + NUL + raw bytes`` in sorted order.  Any
    byte change, rename, or add/remove of a scanned file invalidates the hash
    while path separators stay deterministic across platforms.

    This is the binding run_verify uses to prove that the tree the tests ran
    against is the tree the report was scanned at: ``git_head`` pins the
    commit, but a dirty working tree can change between scan and verify while
    the HEAD stays the same — the content hash is what actually pins the
    audited code state.  A scope that does not exist hashes to the empty
    digest.
    """
    digest = hashlib.sha256()
    scope = root if all_py else (root / package if package else root)
    if scope.is_dir():
        for path in sorted(scope.rglob("*.py")):
            rel = path.relative_to(scope).as_posix()
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            try:
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError:
                continue
    return digest.hexdigest()


def audit_inputs_sha256(
    root: Path,
    package: str | None,
    all_py: bool = False,
    document_channel: bool = True,
    profile: str = "research",
    doc_dirs: list[str] | None = None,
    doc_exclude: list[str] | None = None,
    tex_dir: str | None = None,
    tex_exclude: list[str] | None = None,
) -> str:
    """Content fingerprint of every file the active scanners consume.

    ``source_tree_sha256`` covers only the ``*.py`` scope.  The dead-code
    document channel (``{pkg}``/``docs``/``.github`` templates and root-level
    markdown/TOML/... files) and the TeX style scanner (``*.tex`` under
    ``tex_dir`` in the research profile) consume files outside that scope;
    a change to one of those can stale a report while the Python tree hash
    stays identical.  This fingerprint covers the full audit-input manifest:
    the Python scope first, then document-channel files, then TeX files,
    each domain tagged and root-relative so the digest is reproducible from
    any checkout of the same tree.

    The effective settings (``doc_dirs`` templates, ``tex_dir``, exclusion
    sets) must be passed in explicitly — run_verify cannot re-read
    ``audit.config.json`` — so run_all records them in the report's
    ``provenance.audit_inputs`` and both sides call this with identical
    arguments.
    """
    # Lazy imports: scanners import this module at module level, so pulling
    # their constants here would create an import cycle.
    from scan_deadcode import DOC_EXTS, DEFAULT_DOC_DIRS, DEFAULT_EXCLUDE
    from scan_style import DEFAULT_EXCLUDE_PARTS, DEFAULT_TEX_DIR

    doc_dirs = doc_dirs if doc_dirs is not None else DEFAULT_DOC_DIRS
    doc_exclude = doc_exclude if doc_exclude is not None else sorted(DEFAULT_EXCLUDE)
    tex_dir = tex_dir if tex_dir is not None else DEFAULT_TEX_DIR
    tex_exclude = tex_exclude if tex_exclude is not None else sorted(DEFAULT_EXCLUDE_PARTS)

    def _append_files(digest: Any, files: list[Path], base: Path) -> None:
        for path in sorted(files):
            try:
                rel = path.relative_to(base).as_posix()
            except ValueError:
                rel = path.as_posix()
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            try:
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError:
                continue

    digest = hashlib.sha256()
    digest.update(b"python-scope\n")
    scope = root if all_py else (root / package if package else root)
    if scope.is_dir():
        _append_files(digest, list(scope.rglob("*.py")), scope)
    if document_channel:
        digest.update(b"documents\n")
        exclude = set(doc_exclude)
        docs: list[Path] = []
        for template in doc_dirs:
            candidate = (root / template.format(pkg=package or "")).resolve()
            if candidate.is_dir():
                for path in candidate.rglob("*"):
                    if not path.is_file() or path.suffix.lower() not in DOC_EXTS:
                        continue
                    if any(
                        part in exclude for part in path.relative_to(candidate).parts
                    ):
                        continue
                    docs.append(path)
        docs.extend(
            path
            for path in root.iterdir()
            if path.is_file() and path.suffix.lower() in DOC_EXTS
        )
        _append_files(digest, docs, root)
    if profile == "research":
        digest.update(b"tex\n")
        tex_root = (root / tex_dir).resolve()
        tex_files: list[Path] = []
        if tex_root.is_dir():
            exclude = set(tex_exclude)
            tex_files = [
                path
                for path in tex_root.rglob("*.tex")
                if path.is_file() and not any(part in exclude for part in path.parts)
            ]
        _append_files(digest, tex_files, root)
    return digest.hexdigest()
