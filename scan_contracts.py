#!/usr/bin/env python3
"""Find API-boundary and function-contract review candidates.

This scanner deliberately does not decide whether a wrapper or cross-module
dependency is wrong.  It exposes cases that structural duplicate detection
cannot adjudicate: experiment modules used as libraries, one-line delegation
wrappers, and repeated public names whose input/output contracts may differ.

Additional runtime-contract channels (LESSONS §14; these fired in production
on a hardcoded checkpoint-root bug and a ``strict=False`` load bug):

* ``cli_without_bootstrap`` -- entry scripts that import package modules but
  never add the repo root to sys.path.  Such a script only runs when the cwd
  already contains the repo root or when launched via ``python -m``; the
  reviewer must verify the launch method against how the project actually
  runs it.
* ``defensive_param_loosening`` -- ``load_state_dict(strict=False)`` and
  ``torch.load(weights_only=False)`` weaken load-time safety contracts.  Each
  hit needs a verdict: deliberate partial load or accidental degradation.
* ``env_written_not_read`` -- env vars written inside the package but never
  read in-package; the writer's contract has no in-package consumer.
* ``generation_path_without_env`` -- files embedding generation-pinned path
  strings (e.g. ``generation_a`` / ``generation_b``) with no env read
  anywhere in the file.  The failure signature: a hardcoded archive root
  instead of honoring the environment-variable handoff used by the
  orchestrator.
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

from scan_duplicates import load_ignore

import _audit_config


PY_SUBDIRS = (
    "lib",
    "experiments",
    "mechanism",
    "audit",
    "verify",
    "figures",
    "tests",
)
CLI_ENTRY_LAYERS = ("experiments", "audit", "verify")
EXCLUDE_PARTS = {"frozen_source", "__pycache__"}
COMMON_NAMES = {"main", "parse_args", "require", "close", "count"}
# Default sample of contract-sensitive names; tune per project. The channel
# flags repeated top-level functions with these names as drift candidates.
CONTRACT_SENSITIVE_NAMES = {
    "build_score_components",
    "evaluate_provider",
    "intervention_providers",
    "load_model",
    "matrix_provider",
    "parity_check",
}
# Project-specific: map package-relative paths to the reason their source
# identity is locked (recorded source hashes, frozen fingerprints, ...).
SOURCE_LOCKED_ACTIVE_PATHS: dict[str, str] = {}


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _without_docstring(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    return f"({ast.unparse(node.args)})"


def _env_name(arg: ast.AST | None) -> str | None:
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    return None


def _return_contract(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    kinds = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Return):
            continue
        value = child.value
        if value is None:
            kinds.add("None")
        elif isinstance(value, ast.Call):
            kinds.add(f"call:{_dotted(value.func) or type(value.func).__name__}")
        elif isinstance(value, ast.Tuple):
            kinds.add(f"tuple[{len(value.elts)}]")
        elif isinstance(value, ast.Dict):
            kinds.add("dict")
        elif isinstance(value, ast.List):
            kinds.add("list")
        elif isinstance(value, ast.Constant):
            kinds.add(type(value.value).__name__)
        else:
            kinds.add(type(value).__name__)
    return sorted(kinds) or ["implicit-None"]


def _iter_python(pkg: Path, subdirs: list[str]):
    for subdir_name in subdirs:
        subdir = pkg / subdir_name
        if not subdir.is_dir():
            continue
        for path in sorted(subdir.rglob("*.py")):
            rel = path.relative_to(pkg)
            if any(part in EXCLUDE_PARTS for part in rel.parts):
                continue
            yield path, rel.as_posix()


def _write_json(path: Path | None, payload: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root",
    )
    parser.add_argument("--package", default="src")
    parser.add_argument("--subdirs", nargs="*", default=None)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--ignore", type=Path, default=None)
    args = parser.parse_args(argv)

    repo = args.root.resolve()
    pkg = (repo / args.package).resolve()
    if not pkg.is_dir() or pkg != repo / args.package:
        print(f"error: package dir not found or escapes root: {pkg}", file=sys.stderr)
        return 2

    cfg = _audit_config.load_config(repo)
    contracts_cfg = cfg.get("contracts", {})
    subdirs = _audit_config.pick(args.subdirs, cfg, "subdirs", list(PY_SUBDIRS))
    sensitive_names = set(
        _audit_config.as_string_list(
            contracts_cfg.get("contract_sensitive_names"),
            sorted(CONTRACT_SENSITIVE_NAMES),
        )
    )
    source_locked = SOURCE_LOCKED_ACTIVE_PATHS
    config_source_locked = contracts_cfg.get("source_locked_active_paths")
    if isinstance(config_source_locked, dict) and all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in config_source_locked.items()
    ):
        source_locked = config_source_locked

    experiment_prefix = f"{args.package}.experiments"
    pkg_prefix = args.package + "."
    experiment_stems = {
        path.stem for path in (pkg / "experiments").glob("*.py")
    }
    experiment_imports = []
    experiment_path_hacks = []
    forwarding_wrappers = []
    functions_by_name: dict[str, list[dict]] = defaultdict(list)
    symbol_references: dict[str, int] = defaultdict(int)
    parse_failures = []
    cli_without_bootstrap = []
    defensive_param_loosening = []
    env_written = []
    env_read = []
    generation_consts: dict[str, list[dict]] = defaultdict(list)

    for path, rel in _iter_python(pkg, list(subdirs)):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, UnicodeError, SyntaxError) as exc:
            parse_failures.append({"path": rel, "error": str(exc)})
            continue

        layer = rel.split("/", 1)[0]
        file_has_bootstrap = False
        file_has_pkg_import = False
        first_pkg_import: dict | None = None
        file_env_read = False

        docstring_const_ids = set()
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_const_ids.add(id(body[0].value))

        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                symbol_references[node.id] += 1
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                symbol_references[node.attr] += 1
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if layer in CLI_ENTRY_LAYERS and (
                    module.startswith(pkg_prefix) or module.startswith("lib.")
                ):
                    file_has_pkg_import = True
                    first_pkg_import = first_pkg_import or {
                        "module": module,
                        "line": node.lineno,
                    }
                if module == experiment_prefix or module.startswith(experiment_prefix + "."):
                    experiment_imports.append(
                        {
                            "path": rel,
                            "line": node.lineno,
                            "module": module,
                            "names": [alias.name for alias in node.names],
                            "importer_layer": rel.split("/", 1)[0],
                            "import_kind": "package_import",
                        }
                    )
                elif module.split(".", 1)[0] in experiment_stems:
                    experiment_imports.append(
                        {
                            "path": rel,
                            "line": node.lineno,
                            "module": module,
                            "names": [alias.name for alias in node.names],
                            "importer_layer": rel.split("/", 1)[0],
                            "import_kind": "bare_experiment_import",
                        }
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if layer in CLI_ENTRY_LAYERS and (
                        alias.name.startswith(pkg_prefix) or alias.name.startswith("lib.")
                    ):
                        file_has_pkg_import = True
                        first_pkg_import = first_pkg_import or {
                            "module": alias.name,
                            "line": node.lineno,
                        }
                    if alias.name == experiment_prefix or alias.name.startswith(
                        experiment_prefix + "."
                    ):
                        experiment_imports.append(
                            {
                                "path": rel,
                                "line": node.lineno,
                                "module": alias.name,
                                "names": [alias.asname or alias.name],
                                "importer_layer": rel.split("/", 1)[0],
                                "import_kind": "package_import",
                            }
                        )
                    elif alias.name.split(".", 1)[0] in experiment_stems:
                        experiment_imports.append(
                            {
                                "path": rel,
                                "line": node.lineno,
                                "module": alias.name,
                                "names": [alias.asname or alias.name],
                                "importer_layer": rel.split("/", 1)[0],
                                "import_kind": "bare_experiment_import",
                            }
                        )
            elif isinstance(node, ast.Call):
                target = _dotted(node.func) or ""
                if target.endswith("spec_from_file_location"):
                    experiment_imports.append(
                        {
                            "path": rel,
                            "line": node.lineno,
                            "module": "<dynamic spec_from_file_location>",
                            "names": [ast.unparse(arg) for arg in node.args],
                            "importer_layer": rel.split("/", 1)[0],
                            "import_kind": "dynamic_experiment_load",
                        }
                    )
                elif target.endswith("import_module") and node.args:
                    first = node.args[0]
                    if (
                        isinstance(first, ast.Constant)
                        and isinstance(first.value, str)
                        and (
                            first.value.startswith(experiment_prefix + ".")
                            or first.value.split(".", 1)[0] in experiment_stems
                        )
                    ):
                        experiment_imports.append(
                            {
                                "path": rel,
                                "line": node.lineno,
                                "module": first.value,
                                "names": [],
                                "importer_layer": rel.split("/", 1)[0],
                                "import_kind": "dynamic_import_module",
                            }
                        )
                elif target in {"sys.path.insert", "sys.path.append"}:
                    if layer in CLI_ENTRY_LAYERS:
                        file_has_bootstrap = True
                    expression = " ".join(ast.unparse(arg) for arg in node.args)
                    if "experiment" in expression.lower():
                        experiment_path_hacks.append(
                            {
                                "path": rel,
                                "line": node.lineno,
                                "expression": expression,
                                "importer_layer": rel.split("/", 1)[0],
                            }
                        )
                elif target in {"os.environ.setdefault", "os.putenv"} and node.args:
                    name = _env_name(node.args[0])
                    if name is not None:
                        env_written.append(
                            {
                                "path": rel,
                                "line": node.lineno,
                                "var": name,
                                "kind": target,
                            }
                        )
                elif target in {"os.environ.get", "os.getenv"} and node.args:
                    name = _env_name(node.args[0])
                    if name is not None:
                        env_read.append(
                            {
                                "path": rel,
                                "line": node.lineno,
                                "var": name,
                                "kind": target,
                                "default": (
                                    ast.unparse(node.args[1]).strip()
                                    if len(node.args) > 1
                                    else None
                                ),
                            }
                        )
                        file_env_read = True
                elif target.endswith("load_state_dict"):
                    for keyword in node.keywords:
                        if (
                            keyword.arg == "strict"
                            and isinstance(keyword.value, ast.Constant)
                            and keyword.value.value is False
                        ):
                            defensive_param_loosening.append(
                                {
                                    "path": rel,
                                    "line": node.lineno,
                                    "kind": "strict_false",
                                    "api": "load_state_dict",
                                    "code": " ".join(
                                        ast.unparse(node).split()
                                    )[:120],
                                }
                            )
                elif target.endswith("torch.load"):
                    for keyword in node.keywords:
                        if (
                            keyword.arg == "weights_only"
                            and isinstance(keyword.value, ast.Constant)
                            and keyword.value.value is False
                        ):
                            defensive_param_loosening.append(
                                {
                                    "path": rel,
                                    "line": node.lineno,
                                    "kind": "weights_only_false",
                                    "api": "torch.load",
                                    "code": " ".join(
                                        ast.unparse(node).split()
                                    )[:120],
                                }
                            )
            elif isinstance(node, ast.Subscript) and _dotted(node.value) == "os.environ":
                name = _env_name(node.slice)
                if name is not None:
                    if isinstance(node.ctx, ast.Store):
                        env_written.append(
                            {
                                "path": rel,
                                "line": node.lineno,
                                "var": name,
                                "kind": "environ_subscript",
                            }
                        )
                    else:
                        env_read.append(
                            {
                                "path": rel,
                                "line": node.lineno,
                                "var": name,
                                "kind": "environ_subscript",
                                "default": None,
                            }
                        )
                        file_env_read = True
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstring_const_ids
                and (
                    "generation_a" in node.value or "generation_b" in node.value
                )
            ):
                generation_consts[rel].append(
                    {"line": node.lineno, "snippet": node.value[:80]}
                )

        if (
            layer in CLI_ENTRY_LAYERS
            and file_has_pkg_import
            and not file_has_bootstrap
        ):
            cli_without_bootstrap.append(
                {
                    "path": rel,
                    "line": (first_pkg_import or {}).get("line", 1),
                    "module": (first_pkg_import or {}).get("module", "package import"),
                    "layer": layer,
                }
            )

        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            record = {
                "path": rel,
                "line": node.lineno,
                "name": node.name,
                "signature": _signature(node),
                "return_contract": _return_contract(node),
                "layer": rel.split("/", 1)[0],
                "source_lock": source_locked.get(rel),
            }
            functions_by_name[node.name].append(record)
            body = _without_docstring(node)
            if len(body) == 1 and isinstance(body[0], ast.Return):
                call = body[0].value
                if isinstance(call, ast.Call):
                    forwarding_wrappers.append(
                        {
                            **record,
                            "target": _dotted(call.func) or ast.unparse(call.func),
                            "passes_varargs": any(
                                isinstance(arg, ast.Starred) for arg in call.args
                            ),
                            "passes_kwargs": any(
                                keyword.arg is None for keyword in call.keywords
                            ),
                        }
                    )

    read_vars = {item["var"] for item in env_read}
    seen_written: set[str] = set()
    env_written_not_read = []
    for item in sorted(env_written, key=lambda i: (i["var"], i["path"], i["line"])):
        if item["var"] in read_vars or item["var"] in seen_written:
            continue
        seen_written.add(item["var"])
        env_written_not_read.append(item)

    env_read_files = {item["path"] for item in env_read}
    generation_path_without_env = [
        {
            "path": rel,
            "constants": sorted(items, key=lambda i: i["line"]),
        }
        for rel, items in sorted(generation_consts.items())
        if rel not in env_read_files
    ]

    ignore = load_ignore(args.ignore)
    contracts_ignore = ignore.get("contracts", {}) or {}
    cli_ignored = {
        entry.get("key", "")
        for entry in contracts_ignore.get("cli_without_bootstrap", [])
        if entry.get("key")
    }
    defensive_ignored = {
        entry.get("key", "")
        for entry in contracts_ignore.get("defensive_param_loosening", [])
        if entry.get("key")
    }
    env_written_ignored = {
        entry.get("key", "")
        for entry in contracts_ignore.get("env_written_not_read", [])
        if entry.get("key")
    }
    generation_ignored = {
        entry.get("key", "")
        for entry in contracts_ignore.get("generation_path_without_env", [])
        if entry.get("key")
    }
    experiment_import_ignored = {
        entry.get("key", "")
        for entry in contracts_ignore.get("experiment_as_library", [])
        if entry.get("key")
    }
    forwarding_ignored = {
        entry.get("key", "")
        for entry in contracts_ignore.get("forwarding_wrappers", [])
        if entry.get("key")
    }
    same_name_ignored = {
        entry.get("key", "")
        for entry in contracts_ignore.get("same_name_contracts", [])
        if entry.get("key")
    }
    unreferenced_ignored = {
        entry.get("key", "")
        for entry in contracts_ignore.get("unreferenced_top_level_functions", [])
        if entry.get("key")
    }

    same_name_contracts = []
    same_name_removed = 0
    for name, records in sorted(functions_by_name.items()):
        paths = {record["path"] for record in records}
        if len(paths) < 2 or name in COMMON_NAMES:
            continue
        if name not in sensitive_names and not any(
            record["layer"] == "lib" for record in records
        ):
            continue
        definitions = [
            record
            for record in records
            if f"{record['path']}:{record['line']}:{name}" not in same_name_ignored
        ]
        same_name_removed += len(records) - len(definitions)
        if len({record["path"] for record in definitions}) < 2:
            continue
        same_name_contracts.append({"name": name, "definitions": definitions})

    unreferenced_functions = []
    unreferenced_removed = 0
    for name, records in sorted(functions_by_name.items()):
        if name.startswith("__") or name.startswith("test_"):
            continue
        references = symbol_references.get(name, 0)
        if references:
            continue
        for record in records:
            if record["layer"] == "tests":
                continue
            if f"{record['path']}:{record['line']}" in unreferenced_ignored:
                unreferenced_removed += 1
                continue
            unreferenced_functions.append(
                {**record, "coarse_symbol_references": references}
            )

    ignored_counts = {
        "cli_without_bootstrap": sum(
            item["path"] in cli_ignored for item in cli_without_bootstrap
        ),
        "defensive_param_loosening": sum(
            f"{item['path']}:{item['line']}" in defensive_ignored
            for item in defensive_param_loosening
        ),
        "env_written_not_read": sum(
            f"{item['path']}:{item['var']}" in env_written_ignored
            for item in env_written_not_read
        ),
        "generation_path_without_env": sum(
            item["path"] in generation_ignored for item in generation_path_without_env
        ),
        "experiment_as_library": sum(
            item["path"] in experiment_import_ignored for item in experiment_imports
        ),
        "forwarding_wrappers": sum(
            f"{item['path']}:{item['line']}:{item['name']}" in forwarding_ignored
            for item in forwarding_wrappers
        ),
        "same_name_contracts": same_name_removed,
        "unreferenced_top_level_functions": unreferenced_removed,
    }
    cli_without_bootstrap = [
        item for item in cli_without_bootstrap if item["path"] not in cli_ignored
    ]
    defensive_param_loosening = [
        item
        for item in defensive_param_loosening
        if f"{item['path']}:{item['line']}" not in defensive_ignored
    ]
    env_written_not_read = [
        item
        for item in env_written_not_read
        if f"{item['path']}:{item['var']}" not in env_written_ignored
    ]
    generation_path_without_env = [
        item
        for item in generation_path_without_env
        if item["path"] not in generation_ignored
    ]
    experiment_imports = [
        item for item in experiment_imports if item["path"] not in experiment_import_ignored
    ]
    forwarding_wrappers = [
        item
        for item in forwarding_wrappers
        if f"{item['path']}:{item['line']}:{item['name']}" not in forwarding_ignored
    ]

    experiment_imports.sort(key=lambda item: (item["path"], item["line"], item["module"]))
    forwarding_wrappers.sort(key=lambda item: (item["path"], item["line"], item["name"]))
    unreferenced_functions.sort(
        key=lambda item: (item["path"], item["line"], item["name"])
    )
    cli_without_bootstrap.sort(key=lambda item: (item["path"], item["line"]))
    defensive_param_loosening.sort(key=lambda item: (item["path"], item["line"]))
    payload = {
        "scanner": "function-contract-candidates",
        "schema_version": 4,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "package": args.package,
        "experiment_as_library": experiment_imports,
        "experiment_path_hacks": experiment_path_hacks,
        "forwarding_wrappers": forwarding_wrappers,
        "same_name_contracts": same_name_contracts,
        "unreferenced_top_level_functions": unreferenced_functions,
        "cli_without_bootstrap": cli_without_bootstrap,
        "defensive_param_loosening": defensive_param_loosening,
        "env_written_not_read": env_written_not_read,
        "generation_path_without_env": generation_path_without_env,
        "parse_failures": parse_failures,
        "counts": {
            "experiment_as_library": len(experiment_imports),
            "experiment_path_hacks": len(experiment_path_hacks),
            "forwarding_wrappers": len(forwarding_wrappers),
            "same_name_contracts": len(same_name_contracts),
            "unreferenced_top_level_functions": len(unreferenced_functions),
            "cli_without_bootstrap": len(cli_without_bootstrap),
            "defensive_param_loosening": len(defensive_param_loosening),
            "env_written_not_read": len(env_written_not_read),
            "generation_path_without_env": len(generation_path_without_env),
        },
        "ignored_counts": ignored_counts,
        "guardrail": (
            "Candidates require manual contract review; no finding alone implies "
            "deletion, consolidation, or retention."
        ),
    }
    _write_json(args.json, payload)
    print(json.dumps(payload["counts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
