#!/usr/bin/env python3
"""Run the self-audit candidate scanners and produce a review worksheet."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _audit_config
import _scanner_common
import issue_fusion
import report_formatter
import scan_capabilities
import scan_cli_smoke
import scan_deadcode
import scan_contracts
import scan_duplicates
import scan_forks
import scan_hardcoded
import scan_regions
import scan_style

SKILL_DIR = Path(__file__).resolve().parent

__version__ = "0.2.0"

SCHEMA_VERSION = 7

#: Configuration keys that change scanner semantics; the audit-config
#: fingerprint is projected onto these (absolute-path and non-semantic keys
#: like ``ignore_file``/``config_file`` locations and ``cli_smoke`` are
#: excluded so reports stay comparable across machines).
_CONFIG_SEMANTIC_KEYS = (
    "document_channel",
    "profile",
    "public_api",
    "duplicate_threshold",
    "duplicate_min_chars",
    "all_py",
    "subdirs",
)


def audit_config_hash(configuration: dict) -> str:
    """Content fingerprint of the semantic audit configuration.

    ``_diff_previous`` requires before/after reports to share it: a threshold
    change (e.g. ``duplicate_threshold`` 0.85 -> 0.95) or a scope change
    (``all_py``) can make candidates vanish without any code edit, so the
    new-risk check is only meaningful when the config is byte-identical.  The
    ignore rules file content is folded in when one was in effect, so ignore
    edits invalidate comparability too.
    """
    semantic = {key: configuration.get(key) for key in _CONFIG_SEMANTIC_KEYS}
    for key in ("ignore_file", "config_file"):
        path = configuration.get(key)
        if isinstance(path, str) and path:
            try:
                semantic[f"{key}_content"] = _sha256(Path(path))
            except OSError:
                pass
    payload = json.dumps(
        semantic, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def scanner_bundle_hash(sha_map: dict, version: str) -> str:
    """Fingerprint of the scanner implementation bundle.

    ``provenance.scanner_sha256`` already pins every scanner module
    individually; this folds the map plus the tool version into one digest so
    ``_diff_previous`` can reject before/after comparisons across tool
    upgrades with a single check.
    """
    parts = [f"{name}:{sha_map[name]}" for name in sorted(sha_map)]
    payload = ("\n".join(parts) + "\n" + version).encode("ascii")
    return hashlib.sha256(payload).hexdigest()

#: Default scanner profile.  ``research`` runs all scanners including TeX
#: writing-style analysis; ``code`` excludes it.  When changing this default,
#: the fallback in ``_diff_previous`` uses this constant automatically.
DEFAULT_PROFILE = "code"

_SCANNER_NAMES = report_formatter.SCANNER_NAMES

PROFILE_SCANNERS = {
    "code": (
        "deadcode",
        "duplicates",
        "regions",
        "forks",
        "contracts",
        "capabilities",
        "hardcoded",
    ),
    "research": tuple(_SCANNER_NAMES),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def finding_evidence_hash(scanner: str, target_id: str, detail: dict) -> str:
    """Canonical finding-evidence hash shared by adjudicate.py and run_verify.py.

    Binds a verdict to the exact candidate evidence it was reviewed against:
    the payload is ``{scanner, target_id, detail}`` over the raw report
    record, so any change in the scanner output invalidates the hash.  This
    is the *finding* hash (stale-evidence detection).  It is distinct from
    the protocol *case* hash in ``benchmarks/adjudication_cases.py``, which
    additionally binds commit, display, and code snippets.
    """
    payload = json.dumps(
        {"scanner": scanner, "target_id": target_id, "detail": detail},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


#: Contracts channels whose candidates are real drift risks: a new one after a
#: patch must reach the Layer-3 new-risk gate.
CONTRACTS_RISK: dict[str, str] = {
    "defensive_param_loosening": "high",
    "env_written_not_read": "high",
    "generation_path_without_env": "medium",
}


def value_cohort(scanner: str, detail: dict) -> str:
    """Label-independent expected-value cohort for review prioritisation.

    Derived from the pinned-commit corpus (16 true findings over 594
    adjudicated candidates): the signal that separates true findings is
    near-exact duplication (``duplicates`` ``max_sim >= 0.98`` — the same-file
    reader/writer copies, the pytest plugin-pair hooks) and region twins
    (``regions`` ``twin_match``); plain shared-capability regions are medium;
    every other channel — contracts, forks, deadcode, capabilities,
    hardcoded, style, low-similarity duplicates, and helper regions — has
    zero confirmed findings at the pinned commits and is low-value.  The
    benchmark reports per-cohort precision so the table stays honest.
    """
    if scanner == "duplicates":
        return "high" if detail.get("max_sim", 0) >= 0.98 else "low"
    if scanner == "regions":
        if detail.get("twin_match"):
            return "high"
        return "medium" if detail.get("kind") == "shared_capability" else "low"
    return "low"


def finding_severity(scanner: str, detail: dict) -> str | None:
    """Unified risk severity for the Layer-3 new-risk gate.

    One fallback chain across scanner detail schemas, so the gate and any
    other consumer agree on what a new high/medium candidate is:

    1. ``priority`` (duplicates, regions);
    2. ``severity`` (hardcoded);
    3. a new ``DEAD`` deadcode module's ``status`` (a patch that silently
       strands a module);
    4. the contracts ``_channel`` (``defensive_param_loosening`` /
       ``env_written_not_read`` high, ``generation_path_without_env``
       medium) — ``_candidate_signatures`` injects ``_channel`` on detail
       copies.

    Returns None for candidates that are not gate-worthy risks.
    """
    for key in ("priority", "severity"):
        value = detail.get(key)
        if isinstance(value, str) and value.lower() in ("high", "medium"):
            return value.lower()
    if detail.get("status") == "DEAD":
        return "high"
    if scanner == "contracts":
        channel = detail.get("_channel")
        if isinstance(channel, str):
            return CONTRACTS_RISK.get(channel)
    return None


def _git_provenance(repo: Path, package: str) -> dict:
    """Return Git provenance without treating command failure as a clean tree."""

    def run(*parts: str) -> tuple[str | None, str | None]:
        try:
            result = subprocess.run(
                ["git", *parts],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"{type(exc).__name__}: {exc}"
        if result.returncode != 0:
            message = result.stderr.strip() or f"git exited {result.returncode}"
            return None, message
        return result.stdout.strip(), None

    head, head_error = run("rev-parse", "HEAD")
    status, status_error = run("status", "--porcelain", "--", package)
    errors = [error for error in (head_error, status_error) if error]
    if status is None:
        dirty: list[str] = []
        dirty_count: int | None = None
    else:
        dirty = [line[3:].strip() for line in status.splitlines() if line.strip()]
        dirty_count = len(dirty)
    return {
        "status": "unavailable" if errors else "ok",
        "head": head,
        "dirty_files": dirty,
        "dirty_count": dirty_count,
        "errors": errors,
    }


def _inside(path: Path, root: Path) -> bool:
    """Whether resolved *path* is *root* or one of its descendants."""
    return path == root or root in path.parents


def _run_scanner(
    module,
    args: argparse.Namespace,
    output: Path,
    extra_argv: list[str],
) -> dict:
    argv = [
        "--root",
        str(args.root.resolve()),
        "--package",
        args.package,
        "--json",
        str(output),
    ]
    if args.ignore and args.ignore.is_file():
        argv += ["--ignore", str(args.ignore.resolve())]
    argv += extra_argv
    started = time.perf_counter()
    rc = module.main(argv)
    elapsed = time.perf_counter() - started
    if rc != 0:
        raise RuntimeError(f"{module.__name__} exited with rc={rc}")
    payload = json.loads(output.read_text(encoding="utf-8"))
    payload["elapsed_seconds"] = round(elapsed, 3)
    return payload


def _candidate_signatures(
    payloads: dict,
) -> dict[str, list[tuple[str, str, dict]]]:
    """Map scanner payloads to per-scanner [(signature, display, detail)].

    Signatures are stable identifiers so consecutive runs can be diffed;
    displays are the human-readable one-liners used in the markdown worksheet;
    details are the original candidate records (with ``_channel`` /
    ``_pattern`` / ``_metric`` injected on copies) used by the adjudication
    tool to rebuild suppression entries.
    """
    sigs: dict[str, list[tuple[str, str, dict]]] = {}

    dead = payloads.get("deadcode", {})
    sigs["deadcode"] = [
        (
            f"DEAD/{item['path']}",
            f"`{item['path']}` ({item.get('status', '?')})",
            item,
        )
        for item in dead.get("candidates", [])
    ]

    duplicates = payloads.get("duplicates", {})
    sigs["duplicates"] = [
        (
            f"cluster/{item['id']}",
            f"`{item['id']}` [{item.get('priority', '?')}]",
            item,
        )
        for item in duplicates.get("clusters", [])
    ]

    regions = payloads.get("regions", {})
    sigs["regions"] = [
        (
            f"region/{item['id']}",
            f"`{item['id']}` [{item.get('priority', '?')}] "
            f"{item.get('kind', 'shared_capability')}"
            + (
                f" canonical={item.get('canonical_symbol', '?')}"
                if item.get("canonical_symbol")
                else ""
            )
            + (
                f" ({len(item.get('members', []))} functions, "
                if item.get("twin_match")
                else f" ({len(item.get('members', []))} regions, "
            )
            + f"hints: {', '.join(item.get('capability_hints', [])[:3]) or '-'})",
            item,
        )
        for item in regions.get("clusters", [])
    ]

    def fork_sigs(pairs: list[dict]) -> list[tuple[str, str, dict]]:
        out = []
        for item in pairs:
            left, right = item["left"], item["right"]
            a = f"{left['path']}:{left['qualname']}"
            b = f"{right['path']}:{right['qualname']}"
            out.append(
                (
                    "fork/" + "|".join(sorted((a, b))),
                    f"`{a}` <-> `{b}` ({item.get('kind', '?')})",
                    item,
                )
            )
        return out

    forks = payloads.get("forks", {})
    sigs["forks"] = fork_sigs(forks.get("pairs", [])) + fork_sigs(
        forks.get("small_function_pairs", [])
    )

    contracts = payloads.get("contracts", {})
    con: list[tuple[str, str, dict]] = []

    def contract_sigs(
        channel: str,
        items: list[dict],
        sig: Callable[[dict], str],
        display: Callable[[dict], str],
    ) -> None:
        for item in items:
            detail = dict(item)
            detail["_channel"] = channel
            con.append((sig(item), display(item), detail))

    contract_sigs(
        "experiment_as_library",
        contracts.get("experiment_as_library", []),
        lambda i: f"experiment_as_library/{i['path']}:{i['line']}:{i['module']}",
        lambda i: f"`{i['path']}:{i['line']}` imports `{i['module']}`",
    )
    contract_sigs(
        "forwarding_wrappers",
        contracts.get("forwarding_wrappers", []),
        lambda i: f"forwarding/{i['path']}:{i['line']}:{i['name']}",
        lambda i: f"`{i['path']}:{i['line']}` `{i['name']}{i['signature']}`",
    )
    contract_sigs(
        "unreferenced_top_level_functions",
        contracts.get("unreferenced_top_level_functions", []),
        lambda i: f"unreferenced/{i['path']}:{i['line']}:{i['name']}",
        lambda i: f"`{i['path']}:{i['line']}` `{i['name']}{i['signature']}`",
    )
    contract_sigs(
        "cli_without_bootstrap",
        contracts.get("cli_without_bootstrap", []),
        lambda i: f"cli_noboot/{i['path']}:{i['line']}:{i['module']}",
        lambda i: f"`{i['path']}:{i['line']}` imports `{i['module']}`",
    )
    contract_sigs(
        "defensive_param_loosening",
        contracts.get("defensive_param_loosening", []),
        lambda i: f"defensive/{i['path']}:{i['line']}",
        lambda i: f"`{i['path']}:{i['line']}` `{i['code']}`",
    )
    contract_sigs(
        "env_written_not_read",
        contracts.get("env_written_not_read", []),
        lambda i: f"env/{i['var']}:{i['path']}:{i['line']}",
        lambda i: f"env `{i['var']}` at `{i['path']}:{i['line']}`",
    )
    contract_sigs(
        "generation_path_without_env",
        contracts.get("generation_path_without_env", []),
        lambda i: f"genpath/{i['path']}",
        lambda i: f"`{i['path']}` ({len(i['constants'])} const(s))",
    )
    for group in contracts.get("same_name_contracts", []):
        for item in group["definitions"]:
            detail = dict(item)
            detail["_channel"] = "same_name_contracts"
            detail["_name"] = group["name"]
            con.append(
                (
                    f"same_name/{item['path']}:{item['line']}:{group['name']}",
                    f"`{item['path']}:{item['line']}` "
                    f"`{group['name']}{item['signature']}`",
                    detail,
                )
            )
    sigs["contracts"] = con

    capabilities = payloads.get("capabilities", {})
    sigs["capabilities"] = [
        (
            f"cap/{item['local']['path']}:{item['local']['qualname']}"
            f"|{item['lib']['path']}:{item['lib']['qualname']}",
            f"`{item['local']['path']}:{item['local']['qualname']}` <-> "
            f"`{item['lib']['path']}:{item['lib']['qualname']}` "
            f"({item.get('match', '?')})",
            item,
        )
        for item in capabilities.get("overlap", [])
    ]

    hardcoded = payloads.get("hardcoded", {})
    sigs["hardcoded"] = [
        (
            f"hard/{pattern}/{item['path']}:{item['line']}",
            f"`{item['path']}:{item['line']}` [{pattern}]",
            {**item, "_pattern": pattern},
        )
        for pattern, items in hardcoded.get("hits", {}).items()
        for item in items
    ]

    style = payloads.get("style", {})
    sigs["style"] = [
        (
            f"style/{metric}/{item['path']}:{item.get('line', '-')}",
            f"`{item['path']}:{item.get('line', '-')}` [{metric}]",
            {**item, "_metric": metric},
        )
        for metric, items in style.get("hits", {}).items()
        for item in items
    ]
    return sigs


def _stale_ignore_entries(
    registry: dict, root: Path
) -> list[tuple[str, str, str]]:
    """Best-effort check that suppression entries still target live code.

    Returns (section, key, reason) for entries whose referenced file is gone,
    whose referenced line is gone, or whose expected symbol no longer appears
    where the entry points.  Entry shapes without a path (duplicate cluster
    ids) are skipped as unverifiable.  Relative paths resolve against ``root``.
    """
    stale: list[tuple[str, str, str]] = []

    def key_of(entry: dict) -> str:
        return str(entry.get("key") or entry.get("path") or entry.get("id") or "")

    def check(section: str, entry: dict, path: str | None, line: int | None,
              symbol: str | None) -> None:
        if path is None:
            return
        target = root / path
        if not target.is_file():
            stale.append((section, key_of(entry), f"file missing: {path}"))
            return
        if line is not None:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            if line > len(lines):
                stale.append((section, key_of(entry), f"line {line} gone"))
            elif symbol is not None and symbol not in lines[line - 1]:
                stale.append(
                    (section, key_of(entry),
                     f"line {line} no longer defines `{symbol}`")
                )
        elif symbol is not None:
            text = target.read_text(encoding="utf-8", errors="replace")
            if symbol not in text:
                stale.append((section, key_of(entry), f"symbol `{symbol}` gone"))

    def from_key(key: str) -> tuple[str | None, int | None, str | None]:
        parts = key.split(":")
        path = parts[0]
        if len(parts) >= 2 and parts[1].isdigit():
            line = int(parts[1])
            symbol = parts[2] if len(parts) >= 3 else None
        elif len(parts) >= 2:
            line = None
            symbol = parts[1]  # env var or capability qualname
        else:
            line = None
            symbol = None
        return path, line, symbol

    for entry in registry.get("deadcode", []):
        check("deadcode", entry, entry.get("path"), None, None)
    # duplicates: cluster ids are not path-addressable, skipped
    for entry in registry.get("forks", []):
        key = entry.get("key", "")
        for half in key.split("::"):
            if half:
                path, _, _ = from_key(half)
                check("forks", entry, path, None, None)
    for channel, entries in registry.get("contracts", {}).items():
        for entry in entries:
            path, line, symbol = from_key(entry.get("key", ""))
            check(f"contracts/{channel}", entry, path, line, symbol)
    for entry in registry.get("capabilities", []):
        path, _, symbol = from_key(entry.get("key", ""))
        check("capabilities", entry, path, None, symbol)
    for entry in registry.get("hardcoded", []):
        check("hardcoded", entry, entry.get("path"), None, None)
    for entry in registry.get("style", []):
        check("style", entry, entry.get("path"), None, None)
    return stale


def _diff_previous(
    previous: dict | None,
    payloads: dict,
    package: str,
    profile: str,
    config_hash: str | None = None,
    bundle_hash: str | None = None,
) -> dict | None:
    """Compare current payloads against a previous report; None when absent.

    Returns a ``previous_run`` block: comparable plus per-scanner new/gone
    candidate lists (each item is {"signature", "display"}).

    ``config_hash``/``bundle_hash`` are the current run's audit-config and
    scanner-bundle fingerprints (``audit_config_hash``/``scanner_bundle_hash``
    from this module); when the previous report carries different ones the
    reports are not comparable — candidate deltas could come from a config or
    tool change instead of a code edit.
    """
    if previous is None:
        return None
    _prev_provenance = previous.get("provenance") or {}
    reason = None
    if previous.get("schema_version") != SCHEMA_VERSION:
        reason = (
            f"schema_version {previous.get('schema_version')} != "
            f"{SCHEMA_VERSION}"
        )
    elif previous.get("package") != package:
        reason = f"package {previous.get('package')!r} != {package!r}"
    elif (previous.get("configuration") or {}).get("profile", DEFAULT_PROFILE) != profile:
        reason = "profile changed; candidate reports are not comparable"
    elif set(previous.get("scanners", {})) != set(payloads):
        reason = "scanner set changed; candidate reports are not comparable"
    elif config_hash is not None and _prev_provenance.get("audit_config_hash") != config_hash:
        reason = "audit config changed; candidate reports are not comparable"
    elif bundle_hash is not None and _prev_provenance.get("scanner_bundle_hash") != bundle_hash:
        reason = "scanner implementation changed; candidate reports are not comparable"
    if reason is not None:
        return {
            "comparable": False,
            "reason": reason,
            "generated_at": previous.get("generated_at"),
        }

    prev_sigs = _candidate_signatures(previous.get("scanners", {}))
    cur_sigs = _candidate_signatures(payloads)
    per_scanner: dict[str, dict] = {}
    for scanner in cur_sigs:
        prev_pairs = {
            (sig, display) for sig, display, _ in prev_sigs.get(scanner, [])
        }
        cur_pairs = {
            (sig, display) for sig, display, _ in cur_sigs[scanner]
        }
        per_scanner[scanner] = {
            "previous": len(prev_pairs),
            "current": len(cur_pairs),
            "new": [
                {"signature": sig, "display": display}
                for sig, display in sorted(cur_pairs - prev_pairs)
            ],
            "gone": [
                {"signature": sig, "display": display}
                for sig, display in sorted(prev_pairs - cur_pairs)
            ],
        }
    provenance = previous.get("provenance") or {}
    return {
        "comparable": True,
        "generated_at": previous.get("generated_at"),
        "head": (provenance.get("git") or {}).get("head"),
        "per_scanner": per_scanner,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )
    ap.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repo root (default: current directory)",
    )
    ap.add_argument("--package", default="src")
    ap.add_argument(
        "--profile",
        choices=sorted(PROFILE_SCANNERS),
        default=DEFAULT_PROFILE,
        help="scanner profile: code excludes TeX style signals; research runs all scanners",
    )
    ap.add_argument(
        "--json", type=Path, default=None,
        help="report JSON path (default: <root>/reports/latest.json)",
    )
    ap.add_argument(
        "--markdown", type=Path, default=None,
        help="report markdown path (default: <root>/reports/latest.md)",
    )
    ap.add_argument(
        "--ignore", type=Path, default=None,
        help="suppression registry (default: <root>/ignore.json)",
    )
    ap.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="state directory for report, ignore, lessons, and verdict defaults",
    )
    ap.add_argument(
        "--read-only",
        action="store_true",
        help="require --state-dir outside --root and keep every writable audit "
             "state path outside the audited tree",
    )
    ap.add_argument(
        "--all-py",
        action="store_true",
        help="scan all .py files under --package recursively, overriding "
             "configured subdirs",
    )
    ap.add_argument(
        "--public-api",
        action="store_true",
        help="classify importable public-package modules as review candidates, not dead",
    )
    ap.add_argument("--no-doc-channel", action="store_true")
    ap.add_argument("--duplicate-threshold", type=float, default=None)
    ap.add_argument("--duplicate-min-chars", type=int, default=None)
    ap.add_argument(
        "--cli-smoke",
        action="store_true",
        help="run --help smoke on every scanner entrypoint first; abort "
             "with rc=2 when any fails",
    )
    ap.add_argument(
        "--stale-check",
        action="store_true",
        help="report ignore.json entries that no longer target live code "
             "(file, line, or symbol gone); does not modify the registry",
    )
    ap.add_argument(
        "--exhaustive",
        action="store_true",
        help="include the low-value cohort in the review worksheet (hidden "
             "by default: contracts, forks, deadcode, low-similarity "
             "duplicates, and helper regions have no confirmed findings at "
             "the pinned commits)",
    )
    args = ap.parse_args(argv)

    if args.cli_smoke:
        smoke_failures = scan_cli_smoke.smoke(("run_all",))
        smoke_failures += scan_cli_smoke.version_smoke()
        if smoke_failures:
            for name, rc in smoke_failures:
                print(f"error: CLI smoke failed {name} exited {rc}", file=sys.stderr)
            return 2

    args.root = args.root.resolve()
    if args.read_only and args.state_dir is None:
        print("error: --read-only requires --state-dir", file=sys.stderr)
        return 2
    state_dir = args.state_dir.resolve() if args.state_dir is not None else None
    default_report_dir = state_dir or (args.root / "reports")
    if args.json is None:
        args.json = default_report_dir / "latest.json"
    if args.markdown is None:
        args.markdown = default_report_dir / "latest.md"
    if args.ignore is None:
        args.ignore = (
            state_dir / "ignore.json" if state_dir else args.root / "ignore.json"
        )

    args.json = args.json.resolve()
    args.markdown = args.markdown.resolve()
    args.ignore = args.ignore.resolve()
    effective_state_dir = state_dir or args.json.parent
    lessons_path = (
        effective_state_dir / "LESSONS.md"
        if state_dir
        else args.root / "LESSONS.md"
    )
    verdicts_path = effective_state_dir / "verdicts.json"
    if args.read_only:
        writable_paths = {
            "state directory": effective_state_dir,
            "JSON report": args.json,
            "Markdown report": args.markdown,
            "suppression registry": args.ignore,
            "lessons": lessons_path,
            "verdicts": verdicts_path,
        }
        violations = [
            f"{label}={path}"
            for label, path in writable_paths.items()
            if _inside(path, args.root)
        ]
        if violations:
            print(
                "error: --read-only state paths must be outside --root: "
                + "; ".join(violations),
                file=sys.stderr,
            )
            return 2

    cfg = _audit_config.load_config(args.root)
    configured_subdirs = _audit_config.as_string_list(
        cfg.get("subdirs"), list(_scanner_common.PY_SUBDIRS)
    )
    effective_subdirs = ["."] if args.all_py else configured_subdirs
    dup_cfg = cfg.get("duplicates", {})
    duplicate_threshold = _audit_config.pick(
        args.duplicate_threshold, dup_cfg, "threshold", 0.82
    )
    duplicate_min_chars = _audit_config.pick(
        args.duplicate_min_chars, dup_cfg, "min_chars", 120
    )
    config_path = args.root / _audit_config.CONFIG_FILENAME
    effective_state_dir.mkdir(parents=True, exist_ok=True)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)

    previous = None
    if args.json.is_file():
        try:
            previous = json.loads(args.json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None

    started = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(
            prefix="self-audit-", dir=str(args.json.parent)
        ) as temporary:
            tempdir = Path(temporary)
            scanner_specs = {
                "deadcode": (
                    scan_deadcode,
                    ["--no-doc-channel"] if args.no_doc_channel else [],
                ),
                "duplicates": (
                    scan_duplicates,
                    [
                        "--threshold",
                        str(duplicate_threshold),
                        "--min-chars",
                        str(duplicate_min_chars),
                    ],
                ),
                "regions": (scan_regions, []),
                "contracts": (scan_contracts, []),
                "forks": (scan_forks, []),
                "capabilities": (scan_capabilities, []),
                "hardcoded": (scan_hardcoded, []),
                "style": (scan_style, []),
            }
            if args.all_py:
                for name, (module, extra) in scanner_specs.items():
                    if name != "style":  # style is TeX-based, not Python
                        scanner_specs[name] = (module, extra + ["--subdirs", "."])
            if args.public_api:
                module, extra = scanner_specs["deadcode"]
                scanner_specs["deadcode"] = (module, extra + ["--public-api"])
            selected = set(PROFILE_SCANNERS[args.profile])
            payloads = {}
            for name in _SCANNER_NAMES:
                if name in selected:
                    module, extra = scanner_specs[name]
                    payloads[name] = _run_scanner(
                        module, args, tempdir / f"{name}.json", extra
                    )
                else:
                    payloads[name] = {"scanner": name, "disabled": True}
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"error: self-audit failed: {exc}", file=sys.stderr)
        return 2

    scanner_files = [
        Path(_audit_config.__file__),
        Path(_scanner_common.__file__),
        Path(issue_fusion.__file__),
        Path(report_formatter.__file__),
        Path(scan_capabilities.__file__),
        Path(scan_deadcode.__file__),
        Path(scan_contracts.__file__),
        Path(scan_duplicates.__file__),
        Path(scan_forks.__file__),
        Path(scan_hardcoded.__file__),
        Path(scan_regions.__file__),
        Path(scan_style.__file__),
        Path(__file__),
    ]
    configuration = {
        "document_channel": not args.no_doc_channel,
        "profile": args.profile,
        "public_api": bool(args.public_api),
        "duplicate_threshold": duplicate_threshold,
        "duplicate_min_chars": duplicate_min_chars,
        "all_py": bool(args.all_py),
        "subdirs": effective_subdirs,
        "ignore_file": str(args.ignore.resolve()) if args.ignore else None,
        "config_file": str(config_path) if config_path.is_file() else None,
        "cli_smoke": bool(args.cli_smoke),
        "exhaustive": bool(args.exhaustive),
        "read_only": bool(args.read_only),
        "state_dir": str(effective_state_dir),
    }
    config_hash = audit_config_hash(configuration)
    bundle_sha_map = {path.name: _sha256(path) for path in scanner_files}
    bundle_hash = scanner_bundle_hash(bundle_sha_map, __version__)
    source_tree_hash = _scanner_common.source_tree_sha256(
        args.root, args.package, bool(args.all_py), effective_subdirs
    )
    # Effective audit-input settings: the scanners read these from
    # audit.config.json, which the report stores only as a path — so the
    # effective values must ride along in provenance for run_verify to
    # reproduce the fingerprint.
    dead_cfg = cfg.get("deadcode", {})
    doc_dirs = _audit_config.as_string_list(
        dead_cfg.get("doc_dirs"), scan_deadcode.DEFAULT_DOC_DIRS
    )
    doc_exclude = _audit_config.as_string_list(
        dead_cfg.get("exclude"), sorted(scan_deadcode.DEFAULT_EXCLUDE)
    )
    style_cfg = cfg.get("style", {})
    tex_dir_name = _audit_config.pick(
        None, style_cfg, "tex_dir", scan_style.DEFAULT_TEX_DIR
    )
    tex_exclude = _audit_config.as_string_list(
        style_cfg.get("exclude_parts"), sorted(scan_style.DEFAULT_EXCLUDE_PARTS)
    )
    audit_inputs_hash = _scanner_common.audit_inputs_sha256(
        args.root,
        args.package,
        bool(args.all_py),
        bool(configuration["document_channel"]),
        args.profile,
        doc_dirs,
        doc_exclude,
        tex_dir_name,
        tex_exclude,
        effective_subdirs,
    )
    keep = (
        None
        if args.exhaustive
        else lambda scanner, detail: value_cohort(scanner, detail) != "low"
    )
    issue_bundles = issue_fusion.cluster_issue_bundles(payloads, keep=keep)
    issues = issue_fusion.issue_summary(issue_bundles)
    issues["scope"] = "all" if args.exhaustive else "review_cohort"
    summary = {
        "scanner": "self-audit-run-all",
        "schema_version": SCHEMA_VERSION,
        "package": args.package,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "configuration": configuration,
        "state": {
            "project_root": str(args.root),
            "state_dir": str(effective_state_dir),
            "read_only": bool(args.read_only),
            "report_json": str(args.json),
            "report_markdown": str(args.markdown),
            "ignore_file": str(args.ignore),
            "lessons_file": str(lessons_path),
            "verdicts_file": str(verdicts_path),
        },
        "provenance": {
            "git": _git_provenance(args.root, args.package),
            "source_tree_sha256": source_tree_hash,
            "audit_inputs_sha256": audit_inputs_hash,
            "audit_inputs": {
                "doc_dirs": doc_dirs,
                "doc_exclude": doc_exclude,
                "tex_dir": tex_dir_name,
                "tex_exclude": tex_exclude,
                "subdirs": effective_subdirs,
            },
            "audit_config_hash": config_hash,
            "scanner_bundle_hash": bundle_hash,
            "scanner_sha256": bundle_sha_map,
        },
        "previous_run": _diff_previous(
            previous,
            payloads,
            args.package,
            args.profile,
            config_hash,
            bundle_hash,
        ),
        "issues": issues,
        "scanners": payloads,
    }
    args.json.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    args.markdown.write_text(
        report_formatter.markdown(payloads, summary, keep=keep), encoding="utf-8"
    )

    dead_count = sum(
        item["status"] == "DEAD" for item in payloads["deadcode"]["candidates"]
    )
    public_api_count = len(
        payloads["deadcode"].get("public_api_candidates", [])
    )
    duplicate_count = len(payloads["duplicates"]["clusters"])
    region_count = len(payloads.get("regions", {}).get("clusters", []))
    capability_count = len(payloads["capabilities"].get("overlap", []))
    fork_count = len(payloads["forks"].get("pairs", []))
    hardcoded_count = sum(
        len(items) for items in payloads["hardcoded"]["hits"].values()
    )
    style_count = sum(
        len(items) for items in payloads["style"].get("hits", {}).values()
    )
    print(
        f"SELF_AUDIT_RUN_ALL package={args.package} dead={dead_count} "
        f"public_api={public_api_count} "
        f"dup_clusters={duplicate_count} region_clusters={region_count} "
        f"cap_overlap={capability_count} "
        f"forks={fork_count} "
        f"hardcoded={hardcoded_count} "
        f"style={style_count} seconds={summary['elapsed_seconds']:.3f}"
    )
    print(f"  json: {args.json}")
    print(f"  markdown: {args.markdown}")
    if args.stale_check:
        registry: dict = {}
        if args.ignore.is_file():
            try:
                registry = json.loads(args.ignore.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                registry = {}
        stale = _stale_ignore_entries(registry, args.root)
        if stale:
            print(f"STALE_CHECK ignore={args.ignore} stale={len(stale)}")
            for section, key, reason in stale:
                print(f"  [{section}] `{key}`: {reason}")
        else:
            print(f"STALE_CHECK ignore={args.ignore} stale=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
