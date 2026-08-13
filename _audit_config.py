#!/usr/bin/env python3
"""Project-level tuning for the self-audit scanners (read-only).

A project may drop an ``audit.config.json`` at its repo root to tune scanner
behavior without editing code constants.  Scanners never write this file.
Absent or invalid file => behavior is identical to the compiled-in module
defaults.

Precedence: explicit CLI flag > config value > module default.

Supported schema (all keys optional):

{
  "schema_version": 1,
  "subdirs": ["src", "tests"],
  "deadcode": {"exclude": [...], "doc_dirs": ["{pkg}", "docs", ".github"]},
  "duplicates": {"threshold": 0.82, "min_chars": 120, "skip_names": [...]},
  "forks": {"threshold": 0.75, "min_lines": 40, "small_floor": 8,
            "small_threshold": 0.9, "include_tests": false},
  "regions": {"threshold": 0.82, "helper_reuse_threshold": 0.6,
              "twin_threshold": 0.85, "shared_paths": ["lib", "src"]},
  "contracts": {"contract_sensitive_names": [...],
                "source_locked_active_paths": {"path": "why locked"}},
  "capabilities": {"doc_threshold": 0.55, "top": 40},
  "hardcoded": {"patterns": [{"name": ..., "regex": ..., "suggestion": ...,
                              "severity": ..., "exclude_paths": [...]}],
                "exclude_parts": [...]},
  "style": {"tex_dir": "docs", "exclude_parts": [...], "excess_vocab": [...],
            "template_openers": [...], "threshold_emdash": 0.5,
            "threshold_burstiness": 0.4}
}
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

CONFIG_FILENAME = "audit.config.json"
CONFIG_SCHEMA_VERSION = 1

_warned_invalid: set[Path] = set()


def _warn_once(path: Path, message: str) -> None:
    if path in _warned_invalid:
        return
    _warned_invalid.add(path)
    print(f"warning: {message}", file=sys.stderr)


def _is_number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _validate_config(data: dict) -> list[str]:
    """Return semantic config issues without changing the user payload."""
    issues: list[str] = []
    top_keys = {
        "schema_version",
        "subdirs",
        "deadcode",
        "duplicates",
        "forks",
        "contracts",
        "regions",
        "capabilities",
        "hardcoded",
        "style",
    }
    for key in sorted(set(data) - top_keys):
        issues.append(f"unknown key {key!r}")

    version = data.get("schema_version", CONFIG_SCHEMA_VERSION)
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != CONFIG_SCHEMA_VERSION
    ):
        issues.append(
            f"schema_version must be {CONFIG_SCHEMA_VERSION}, got {version!r}"
        )

    def section(name: str, allowed: set[str]) -> dict:
        value = data.get(name, {})
        if not isinstance(value, dict):
            issues.append(f"{name} must be an object")
            return {}
        for key in sorted(set(value) - allowed):
            issues.append(f"unknown key {name}.{key}")
        return value

    def string_list(value, path: str) -> None:
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            issues.append(f"{path} must be a list of strings")

    def bounded_number(value, path: str, lower: float, upper: float) -> None:
        if not _is_number(value) or not lower <= float(value) <= upper:
            issues.append(
                f"{path} must be a number in [{lower}, {upper}], got {value!r}"
            )

    def nonnegative_int(value, path: str, *, allow_zero: bool = True) -> None:
        valid = isinstance(value, int) and not isinstance(value, bool)
        if valid and not allow_zero:
            valid = value >= 1
        elif valid:
            valid = value >= 0
        if not valid:
            minimum = 0 if allow_zero else 1
            issues.append(f"{path} must be an integer >= {minimum}, got {value!r}")

    if "subdirs" in data:
        string_list(data["subdirs"], "subdirs")

    deadcode = section("deadcode", {"exclude", "doc_dirs"})
    for key in ("exclude", "doc_dirs"):
        if key in deadcode:
            string_list(deadcode[key], f"deadcode.{key}")

    duplicates = section("duplicates", {"threshold", "min_chars", "skip_names"})
    if "threshold" in duplicates:
        bounded_number(duplicates["threshold"], "duplicates.threshold", 0.0, 1.0)
    if "min_chars" in duplicates:
        nonnegative_int(duplicates["min_chars"], "duplicates.min_chars")
    if "skip_names" in duplicates:
        string_list(duplicates["skip_names"], "duplicates.skip_names")

    forks = section(
        "forks", {"threshold", "min_lines", "small_floor", "small_threshold", "include_tests"}
    )
    if "threshold" in forks:
        bounded_number(forks["threshold"], "forks.threshold", 0.0, 1.0)
    if "small_threshold" in forks:
        bounded_number(forks["small_threshold"], "forks.small_threshold", 0.0, 1.0)
    for key in ("min_lines", "small_floor"):
        if key in forks:
            nonnegative_int(forks[key], f"forks.{key}", allow_zero=(key == "small_floor"))
    if "include_tests" in forks and not isinstance(forks["include_tests"], bool):
        issues.append("forks.include_tests must be boolean")

    contracts = section("contracts", {"contract_sensitive_names", "source_locked_active_paths"})
    if "contract_sensitive_names" in contracts:
        string_list(contracts["contract_sensitive_names"], "contracts.contract_sensitive_names")
    if "source_locked_active_paths" in contracts:
        value = contracts["source_locked_active_paths"]
        if not isinstance(value, dict) or not all(
            isinstance(key, str) and isinstance(reason, str)
            for key, reason in value.items()
        ):
            issues.append("contracts.source_locked_active_paths must map strings to strings")

    regions = section(
        "regions", {"threshold", "helper_reuse_threshold", "twin_threshold", "shared_paths"}
    )
    if "threshold" in regions:
        bounded_number(regions["threshold"], "regions.threshold", 0.0, 1.0)
    if "helper_reuse_threshold" in regions:
        bounded_number(
            regions["helper_reuse_threshold"],
            "regions.helper_reuse_threshold",
            0.0,
            1.0,
        )
    if "twin_threshold" in regions:
        bounded_number(regions["twin_threshold"], "regions.twin_threshold", 0.0, 1.0)
    if "shared_paths" in regions:
        string_list(regions["shared_paths"], "regions.shared_paths")

    capabilities = section("capabilities", {"doc_threshold", "top"})
    if "doc_threshold" in capabilities:
        bounded_number(capabilities["doc_threshold"], "capabilities.doc_threshold", 0.0, 1.0)
    if "top" in capabilities:
        nonnegative_int(capabilities["top"], "capabilities.top")

    hardcoded = section("hardcoded", {"patterns", "exclude_parts"})
    if "patterns" in hardcoded:
        if not isinstance(hardcoded["patterns"], list) or not all(
            isinstance(item, dict) for item in hardcoded["patterns"]
        ):
            issues.append("hardcoded.patterns must be a list of objects")
    if "exclude_parts" in hardcoded:
        string_list(hardcoded["exclude_parts"], "hardcoded.exclude_parts")

    style = section(
        "style",
        {
            "tex_dir",
            "exclude_parts",
            "excess_vocab",
            "template_openers",
            "threshold_emdash",
            "threshold_burstiness",
        },
    )
    if "tex_dir" in style and not isinstance(style["tex_dir"], str):
        issues.append("style.tex_dir must be a string")
    for key in ("exclude_parts", "excess_vocab", "template_openers"):
        if key in style:
            string_list(style[key], f"style.{key}")
    for key in ("threshold_emdash", "threshold_burstiness"):
        if key in style:
            bounded_number(style[key], f"style.{key}", 0.0, 1.0)
    return issues


def load_config(root: Path) -> dict:
    """Return project tuning for ``root``; {} when absent or broken.

    A missing file is normal (the config is optional).  A file that exists
    but cannot be parsed warns once on stderr per path, then falls back to
    module defaults — so a typo does not silently change scanner behavior.
    """
    path = root / CONFIG_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        _warn_once(
            path,
            f"{path} is unreadable or invalid JSON "
            f"({type(exc).__name__}); using module defaults",
        )
        return {}
    if not isinstance(data, dict):
        _warn_once(path, f"{path} is not a JSON object; using module defaults")
        return {}
    issues = _validate_config(data)
    if issues:
        _warn_once(
            path,
            f"{path} has invalid configuration ({'; '.join(issues[:4])}); "
            "using module defaults",
        )
        return {}
    return data


def pick(explicit, config_section, key, default):
    """First non-None among: explicit CLI value, config value, module default."""
    if explicit is not None:
        return explicit
    if isinstance(config_section, dict):
        value = config_section.get(key)
        if value is not None:
            return value
    return default


def as_string_list(value, default):
    """Coerce a config value to a list[str] or fall back to ``default``."""
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    return default


def as_string_set(value, default):
    """Coerce a config value to a set[str] or fall back to ``default``."""
    result = as_string_list(value, None)
    return set(result) if result is not None else default
