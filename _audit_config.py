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
  "subdirs": ["lib", "experiments", "mechanism", "audit", "verify", "figures", "tests"],
  "deadcode": {"exclude": [...], "doc_dirs": ["{pkg}", "docs", ".github"]},
  "duplicates": {"threshold": 0.82, "min_chars": 120, "skip_names": [...]},
  "forks": {"threshold": 0.75, "min_lines": 40, "small_floor": 8,
            "small_threshold": 0.9, "include_tests": false},
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
import sys
from pathlib import Path

CONFIG_FILENAME = "audit.config.json"

_warned_invalid: set[Path] = set()


def _warn_once(path: Path, message: str) -> None:
    if path in _warned_invalid:
        return
    _warned_invalid.add(path)
    print(f"warning: {message}", file=sys.stderr)


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
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return set(value)
    return default
