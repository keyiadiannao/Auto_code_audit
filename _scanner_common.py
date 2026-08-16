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
import difflib
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Iterator, cast

# ---------------------------------------------------------------------------
# Directory / exclusion constants
# ---------------------------------------------------------------------------

#: Scan the selected package recursively by default. Projects that need a
#: narrower ownership boundary can override ``subdirs`` in ``audit.config.json``.
PY_SUBDIRS = (".",)

#: Path components that cause a file to be skipped by every Python scanner.
#: These are generated, vendored, cache, environment, or audit-state trees;
#: treating them as maintained source creates noisy candidates and can copy
#: prior report fixtures into a new report.
EXCLUDE_PARTS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".nox",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "env",
    "frozen_source",
    "node_modules",
    "reports",
    "site-packages",
    "venv",
}


class UnionFind:
    """Deterministic disjoint-set structure shared by cluster scanners."""

    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, index: int) -> int:
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return index

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)


def sequence_similarity(
    left: tuple[str, ...], right: tuple[str, ...], threshold: float
) -> float:
    """Token-sequence similarity with sound length and quick-ratio gates."""
    shorter, longer = sorted((len(left), len(right)))
    if longer == 0 or shorter / longer < threshold:
        return 0.0
    matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
    if matcher.real_quick_ratio() < threshold:
        return 0.0
    if matcher.quick_ratio() < threshold:
        return 0.0
    return matcher.ratio()


# ---------------------------------------------------------------------------
# Frozen-JSON provenance locks  (hash-pinned dependency manifests)
# ---------------------------------------------------------------------------

#: Values that look like a sha256 digest: 64 lowercase hex chars.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: Keys that look like a repository-relative source path (e.g.
#: ``mechanism/_ring_utils.py``, ``lib/protocol.py``, ``configs/protocol_v1.json``).
#: Distinguishes dependency-path keys from provenance metadata keys such as
#: ``reference_checkpoint_sha256`` / ``split_hash`` / ``audit_id``, whose
#: values are also 64-hex digests but which must never be treated as locked
#: source files.
_PATH_KEY_RE = re.compile(r"^[A-Za-z0-9_./-]+\.[A-Za-z0-9]+$")

#: Path components of derived/run-output trees whose JSON manifests are
#: *snapshots*, not edit constraints.  A run-metadata JSON records the hashes
#: of every file present at run time; treating it as a lock would falsely pin
#: scripts that are safe to edit (e.g. probe runners the frozen summaries
#: never reference).  Only frozen-result and config manifests (``frozen_results/``,
#: ``configs/``) plus any other user-written manifest are lock sources.
_LOCK_EXCLUDE_PARTS = {"outputs", "reports", "logs", "runs", "cache"}


def _looks_like_source_path(key: str) -> bool:
    """True when *key* is a repo-relative path (``dir/file.ext``), not metadata."""
    if not _PATH_KEY_RE.match(key):
        return False
    if key.endswith("_sha256") or key.endswith("_hash"):
        return False
    return True


def _collect_lock_paths(
    node: Any,
    root: Path,
    out: dict[str, set[str]],
) -> None:
    """Recursively collect ``path -> sha256`` mappings from a frozen manifest.

    *out* maps a repository-relative source path to the set of JSON files
    (repo-relative) that hash-lock it.  The scan is deliberately tolerant:
    any dict whose key looks like a source path and whose value is a 64-hex
    digest is treated as a lock entry, so a new project that names its
    manifest field differently still gets provenance-aware candidates without
    a code change.  Dicts whose values are plain strings (e.g. a metadata
    blob) do not recurse; lists recurse element-wise.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if (
                isinstance(key, str)
                and _looks_like_source_path(key)
                and isinstance(value, str)
                and _SHA256_RE.match(value)
            ):
                out.setdefault(key, set()).add(root)
            elif isinstance(value, (dict, list)):
                _collect_lock_paths(value, root, out)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, (dict, list)):
                _collect_lock_paths(item, root, out)


def discover_locked_files(root: Path) -> dict[str, list[str]]:
    """Return ``{repo-relative source path: [locking JSON files]}``.

    Scans every ``*.json`` under *root* (honouring :data:`EXCLUDE_PARTS` plus
    :data:`_LOCK_EXCLUDE_PARTS` — derived run-output trees are snapshots, not
    edit constraints) for hash-locked provenance manifests and collects the
    union of source paths pinned by any of them.  A file listed here must not
    be edited without regenerating the frozen results that reference it —
    duplicate / fork candidates touching a locked file need a provenance
    check before consolidation.  Files not present in the repo are dropped.
    Returns an empty dict when no locked manifests exist (the common case for
    projects without frozen-results provenance).
    """
    locked: dict[str, set[str]] = {}
    for path in sorted(root.rglob("*.json")):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if any(part in EXCLUDE_PARTS for part in rel.parts):
            continue
        if any(part in _LOCK_EXCLUDE_PARTS for part in rel.parts):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(data, (dict, list)):
            continue
        _collect_lock_paths(data, rel.as_posix(), locked)
    return {
        path: sorted(sources)
        for path, sources in sorted(locked.items())
        if (root / path).is_file()
    }


def locked_for_package(
    locked: dict[str, list[str]], package: str
) -> dict[str, list[str]]:
    """Rebase repo-relative lock keys to package-relative scanner paths.

    Scanner member paths are relative to ``--package`` (e.g. ``lib/shared.py``
    when scanning ``pkg``), while frozen manifests pin repo-relative paths
    (``pkg/lib/shared.py``).  When the package is a repo subdirectory, strip
    its prefix so a member lookup matches; a lock key outside the package
    (e.g. ``configs/frozen.json`` itself) is dropped since no scanned member
    can reference it as a package path.  A bare ``.`` package (repo-root
    scope) maps keys unchanged.
    """
    prefix = f"{package}/"
    rebased: dict[str, set[str]] = {}
    for path, sources in locked.items():
        if package in ("", "."):
            key = path
        elif path.startswith(prefix):
            key = path[len(prefix):]
        else:
            continue
        rebased.setdefault(key, set()).update(sources)
    return {key: sorted(sources) for key, sources in sorted(rebased.items())}


# ---------------------------------------------------------------------------
# JSON I / O  (was duplicated 7 times)
# ---------------------------------------------------------------------------

def atomic_write_text(path: Path, text: str) -> None:
    """Atomically replace *path* with *text* (unique temp file + os.replace).

    Evidence artifacts — reports, verdicts, suppression registries — must never
    be left half-written: a crash mid-write would otherwise leave a torn file
    that the next run silently reads as absent or corrupt.  The temp file lives
    in the same directory so ``os.replace`` is an atomic rename, not a copy.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def write_json(path: Path | None, payload: dict) -> None:
    """Write *payload* to *path* as pretty-printed JSON (no trailing newline).

    ``path`` may be ``None`` (no-op), which lets callers pass ``--json``
    without a value.  Atomic: a crash mid-write cannot corrupt the file.
    """
    if path is None:
        return
    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False))


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


def _python_scope_subdirs(
    all_py: bool, subdirs: list[str] | tuple[str, ...] | None
) -> list[str]:
    """Return the effective Python scan scope recorded by the orchestrator."""
    if all_py:
        return ["."]
    return list(PY_SUBDIRS if subdirs is None else subdirs)


def source_tree_sha256(
    root: Path,
    package: str | None,
    all_py: bool = False,
    subdirs: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Canonical content fingerprint of the audited source tree.

    Walks the effective Python scope under ``package`` and hashes every
    maintained ``*.py`` file as
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
    scope = root / package if package else root
    if scope.is_dir():
        for path in collect_py_files(
            scope, _python_scope_subdirs(all_py, subdirs)
        ):
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
    subdirs: list[str] | tuple[str, ...] | None = None,
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
    scope = root / package if package else root
    if scope.is_dir():
        _append_files(
            digest,
            collect_py_files(scope, _python_scope_subdirs(all_py, subdirs)),
            scope,
        )
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
