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
import json
from pathlib import Path

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
        return self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> ast.Constant:
        value = node.value
        if value is None or isinstance(value, bool):
            replacement = value
        elif isinstance(value, str):
            replacement = "_str"
        elif isinstance(value, bytes):
            replacement = b"_bytes"
        elif isinstance(value, (int, float, complex)):
            replacement = 0
        else:
            replacement = None
        return ast.copy_location(ast.Constant(value=replacement), node)

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
