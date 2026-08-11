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
import scan_capabilities
import scan_cli_smoke
import scan_deadcode
import scan_contracts
import scan_duplicates
import scan_forks
import scan_hardcoded
import scan_style

SKILL_DIR = Path(__file__).resolve().parent

SCHEMA_VERSION = 5

_SCANNER_NAMES = {
    "deadcode": "dead code",
    "duplicates": "duplicate clusters",
    "forks": "script-to-script forks",
    "contracts": "contract-boundary candidates",
    "capabilities": "capability overlap",
    "hardcoded": "hard-coded patterns",
    "style": "writing-style candidates",
}

PROFILE_SCANNERS = {
    "code": (
        "deadcode",
        "duplicates",
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


def _git_provenance(repo: Path, package: str) -> dict:
    def run(*parts: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *parts],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    head = run("rev-parse", "HEAD")
    status = run("status", "--porcelain", "--", package) or ""
    dirty = [line[3:].strip() for line in status.splitlines() if line.strip()]
    return {"head": head, "dirty_files": dirty, "dirty_count": len(dirty)}


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
    previous: dict | None, payloads: dict, package: str, profile: str
) -> dict | None:
    """Compare current payloads against a previous report; None when absent.

    Returns a ``previous_run`` block: comparable plus per-scanner new/gone
    candidate lists (each item is {"signature", "display"}).
    """
    if previous is None:
        return None
    reason = None
    if previous.get("schema_version") != SCHEMA_VERSION:
        reason = (
            f"schema_version {previous.get('schema_version')} != "
            f"{SCHEMA_VERSION}"
        )
    elif previous.get("package") != package:
        reason = f"package {previous.get('package')!r} != {package!r}"
    elif (previous.get("configuration") or {}).get("profile", "research") != profile:
        reason = "profile changed; candidate reports are not comparable"
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


def _changes_markdown(block: dict | None) -> list[str]:
    """Render the ``previous_run`` block as a markdown delta section."""
    if block is None:
        return []
    lines = ["", "## Changes since last run", ""]
    if not block.get("comparable"):
        lines.extend(
            [
                f"The previous report at this path was not comparable: "
                f"{block.get('reason', 'unknown')}.",
                "Verify the report path or schema before treating this run as a delta.",
                "",
            ]
        )
        return lines
    lines.append(
        "Compared against the previous report (generated "
        f"{block.get('generated_at') or 'unknown time'}, "
        f"Git HEAD `{block.get('head') or 'unavailable'}`)."
    )
    lines.append("")
    lines.append("| scanner | previous | current | new | gone |")
    lines.append("|---|---:|---:|---:|---:|")
    for scanner in _SCANNER_NAMES:
        stats = block["per_scanner"][scanner]
        lines.append(
            f"| {_SCANNER_NAMES[scanner]} | {stats['previous']} | "
            f"{stats['current']} | {len(stats['new'])} | {len(stats['gone'])} |"
        )
    lines.append("")
    for scanner in _SCANNER_NAMES:
        stats = block["per_scanner"][scanner]
        for label, items in (("New", stats["new"]), ("Gone", stats["gone"])):
            if not items:
                continue
            lines.extend([f"### {label} {_SCANNER_NAMES[scanner]} ({len(items)})", ""])
            for item in items[:15]:
                lines.append(f"- {item['display']}")
            if len(items) > 15:
                lines.append(f"- ... and {len(items) - 15} more")
            lines.append("")
    return lines


def _markdown(payloads: dict[str, dict], summary: dict) -> str:
    dead = payloads["deadcode"]
    duplicates = payloads["duplicates"]
    forks = payloads["forks"]
    contracts = payloads["contracts"]
    capabilities = payloads["capabilities"]
    hardcoded = payloads["hardcoded"]
    style = payloads["style"]
    provenance = summary["provenance"]

    lines = [
        f"# Self-Audit review worksheet: `{summary['package']}`",
        "",
        f"Generated: {summary['generated_at']}",
        f"Git HEAD: `{provenance['git']['head'] or 'unavailable'}`",
        "",
    ]
    if provenance["git"]["dirty_count"]:
        lines.extend(
            [
                f"> Snapshot warning: {provenance['git']['dirty_count']} scanned-package files were dirty.",
                "> Re-check each candidate against the current diff before editing.",
                "",
            ]
        )
    parse_errors = dead.get("parse_failures", {})
    duplicate_parse_errors = duplicates.get("parse_failures", [])
    if parse_errors or duplicate_parse_errors:
        lines.extend(
            [
                "> Parse warning: one or more Python files could not be analyzed.",
                "> Treat this as an audit failure until the encoding or syntax error is resolved.",
                "",
            ]
        )
    lines.extend(_changes_markdown(summary.get("previous_run")))
    lines.extend(
        [
            "This is a candidate list, not a deletion or refactor decision.",
            "Assign each item one verdict: `real issue`, `intentional design`, or `false positive`.",
            "Update `ignore.json` only after recording semantic evidence in `LESSONS.md`.",
            "",
            "## Scanner summary",
            "",
            "| scanner | candidates | seconds |",
            "|---|---:|---:|",
            f"| dead code | {len(dead.get('candidates', []))} | {dead.get('elapsed_seconds', 0):.3f} |",
            f"| duplicate clusters | {len(duplicates.get('clusters', []))} "
            f"(H/M/L: {duplicates.get('priority_counts', {}).get('high', 0)}/"
            f"{duplicates.get('priority_counts', {}).get('medium', 0)}/"
            f"{duplicates.get('priority_counts', {}).get('low', 0)}) | "
            f"{duplicates.get('elapsed_seconds', 0):.3f} |",
            f"| script-to-script forks | {len(forks.get('pairs', []))} "
            f"(+{len(forks.get('small_function_pairs', []))} small) | "
            f"{forks.get('elapsed_seconds', 0):.3f} |",
            f"| contract-boundary candidates | "
            f"{sum(contracts.get('counts', {}).values())} | "
            f"{contracts.get('elapsed_seconds', 0):.3f} |",
            f"| capability overlap | {len(capabilities.get('overlap', []))} "
            f"(sig-match {sum(1 for item in capabilities.get('overlap', []) if item.get('signature_match'))}) | "
            f"{capabilities.get('elapsed_seconds', 0):.3f} |",
            f"| hard-coded patterns | {sum(len(v) for v in hardcoded.get('hits', {}).values())} | {hardcoded.get('elapsed_seconds', 0):.3f} |",
            f"| writing-style candidates | {sum(len(v) for v in style.get('hits', {}).values())} | {style.get('elapsed_seconds', 0):.3f} |",
            "",
            "## Dead-code candidates",
            "",
        ]
    )
    if dead.get("candidates"):
        lines.extend(
            [
                "| status | path | Python references | document references | verdict |",
                "|---|---|---|---|---|",
            ]
        )
        for item in dead["candidates"]:
            py_refs = ", ".join(item.get("py_refs", []) + item.get("dynamic_refs", [])) or "-"
            doc_refs = ", ".join(item.get("doc_refs", [])) or "-"
            lines.append(
                f"| {item['status']} | `{item['path']}` | {py_refs} | {doc_refs} | |"
            )
    else:
        lines.append("No candidates.")

    lines.extend(["", "## Duplicate-implementation candidates", ""])
    if duplicates.get("clusters"):
        for cluster in duplicates["clusters"]:
            shared = ""
            if cluster.get("lib_shared"):
                shared = "; shared-lib member: " + ", ".join(
                    f"`{item['path']}:{item.get('qualname', item['name'])}`"
                    for item in cluster["lib_shared"]
                )
            lines.append(
                f"### [{cluster['priority']}] `{cluster['id']}`: {cluster['size']} members "
                f"(edge similarity {cluster['min_edge_sim']:.3f}-{cluster['max_sim']:.3f}){shared}"
            )
            lines.append("")
            lines.append(f"Reason: {cluster['priority_reason']}.")
            lines.append("")
            for member in cluster["members"]:
                lines.append(
                    f"- `{member['path']}:{member.get('qualname', member['name'])}` "
                    f"({member['nlines']} lines)"
                )
            lines.extend(["- Verdict:", ""])
    else:
        lines.extend(["No candidates.", ""])

    lines.extend(["", "## Script-to-script fork candidates", ""])
    if forks.get("pairs"):
        lines.extend(
            [
                "Cross-file callables sharing a large common skeleton with diverged ",
                "bodies. Each pair needs a verdict: deliberate fork, parameterizable ",
                "merge candidate, or true duplicate.",
                "",
                "| sim | kind | left | right | sig | imports | verdict |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for item in forks["pairs"]:
            left, right = item["left"], item["right"]
            sig = "yes" if item["signature_match"] else "no"
            imp = (
                "left->right"
                if item.get("a_imports_b")
                else ("right->left" if item.get("b_imports_a") else "-")
            )
            lines.append(
                f"| {item['similarity']:.3f} | {item['kind']} | "
                f"`{left['path']}:{left['qualname']}` "
                f"(L{left['lineno']}, {left['nlines']} lines) | "
                f"`{right['path']}:{right['qualname']}` "
                f"(L{right['lineno']}, {right['nlines']} lines) | {sig} | {imp} | |"
            )
    else:
        lines.extend(["No fork pairs.", ""])

    lines.extend(["", "## Small-function fork candidates", ""])
    small_pairs = forks.get("small_function_pairs", [])
    if small_pairs:
        lines.extend(
            [
                "Sub-`--min-lines` callables sharing near-identical bodies (at or "
                "above the small-channel threshold). Small helpers duplicate easily "
                "and evade the main size floor; each pair still needs a verdict.",
                "",
                "| sim | kind | left | right | sig | imports | verdict |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for item in small_pairs:
            left, right = item["left"], item["right"]
            sig = "yes" if item["signature_match"] else "no"
            imp = (
                "left->right"
                if item.get("a_imports_b")
                else ("right->left" if item.get("b_imports_a") else "-")
            )
            lines.append(
                f"| {item['similarity']:.3f} | {item['kind']} | "
                f"`{left['path']}:{left['qualname']}` "
                f"(L{left['lineno']}, {left['nlines']} lines) | "
                f"`{right['path']}:{right['qualname']}` "
                f"(L{right['lineno']}, {right['nlines']} lines) | {sig} | {imp} | |"
            )
    else:
        lines.extend(["No small-function pairs.", ""])

    lines.extend(["", "## Function-contract candidates", ""])
    lines.extend(
        [
            "These hits require a caller-by-caller contract review. A thin wrapper may be a ",
            "valuable adapter, compatibility debt, or an independent audit boundary; code ",
            "shape alone cannot decide which.",
            "",
            "### Experiment modules used as libraries",
            "",
        ]
    )
    if contracts.get("experiment_as_library"):
        for item in contracts["experiment_as_library"]:
            names = ", ".join(item["names"])
            lines.append(
                f"- `{item['path']}:{item['line']}` imports `{item['module']}` "
                f"({names}); importer layer: `{item['importer_layer']}`"
            )
    else:
        lines.append("No candidates.")

    lines.extend(["", "### Forwarding wrappers", ""])
    if contracts.get("forwarding_wrappers"):
        for item in contracts["forwarding_wrappers"]:
            lines.append(
                f"- `{item['path']}:{item['line']}` `{item['name']}{item['signature']}` "
                f"delegates to `{item['target']}`; returns "
                f"`{', '.join(item['return_contract'])}`"
            )
    else:
        lines.append("No candidates.")

    lines.extend(["", "### Repeated contract-sensitive names", ""])
    if contracts.get("same_name_contracts"):
        for group in contracts["same_name_contracts"]:
            lines.append(f"#### `{group['name']}`")
            lines.append("")
            for item in group["definitions"]:
                lines.append(
                    f"- `{item['path']}:{item['line']}` `{item['signature']}` -> "
                    f"`{', '.join(item['return_contract'])}`"
                )
            lines.append("")
    else:
        lines.append("No candidates.")

    lines.extend(["", "### Unreferenced top-level functions", ""])
    lines.append(
        "This is a coarse symbol-use screen. Dynamic dispatch and external entrypoints "
        "remain manual-review false positives."
    )
    lines.append("")
    if contracts.get("unreferenced_top_level_functions"):
        for item in contracts["unreferenced_top_level_functions"]:
            lock = (
                f"; source lock: {item['source_lock']}"
                if item.get("source_lock")
                else ""
            )
            lines.append(
                f"- `{item['path']}:{item['line']}` "
                f"`{item['name']}{item['signature']}`{lock}"
            )
    else:
        lines.append("No candidates.")

    lines.extend(["", "### CLI entry scripts without sys.path bootstrap", ""])
    if contracts.get("cli_without_bootstrap"):
        lines.extend(
            [
                "These entry scripts import package modules but never add the repo "
                "root to sys.path, so they only run when the cwd already contains "
                "the repo root or when launched via `python -m`. Verify the launch "
                "method against how the submission actually runs them.",
                "",
            ]
        )
        for item in contracts["cli_without_bootstrap"]:
            lines.append(
                f"- `{item['path']}:{item['line']}` imports `{item['module']}` "
                "without a bootstrap"
            )
    else:
        lines.append("No candidates.")

    lines.extend(["", "### Defensive-parameter loosening", ""])
    if contracts.get("defensive_param_loosening"):
        lines.extend(
            [
                "`strict=False` / `weights_only=False` weaken a load-time safety "
                "contract. Each hit needs a verdict: deliberate partial load, or "
                "accidental degradation.",
                "",
            ]
        )
        for item in contracts["defensive_param_loosening"]:
            lines.append(f"- `{item['path']}:{item['line']}` `{item['code']}`")
    else:
        lines.append("No candidates.")

    lines.extend(["", "### Env-contract candidates", ""])
    env_hits = contracts.get("env_written_not_read", []) + contracts.get(
        "generation_path_without_env", []
    )
    if env_hits:
        for item in contracts.get("env_written_not_read", []):
            lines.append(
                f"- env `{item['var']}` written at `{item['path']}:{item['line']}` "
                "but never read in-package"
            )
        for item in contracts.get("generation_path_without_env", []):
            first = item["constants"][0]
            lines.append(
                f"- `{item['path']}` embeds a generation-pinned path "
                f"({len(item['constants'])} const(s), first L{first['line']}) "
                "with no env-var read in the file"
            )
    else:
        lines.append("No candidates.")

    lines.extend(
        [
            "### Required contract card",
            "",
            "For every accepted consolidation or retention, record:",
            "",
            "- scientific role and callers",
            "- accepted inputs, shapes, indexing, dtype, and device ownership",
            "- outputs and required intermediate tensors",
            "- randomness and checkpoint/provenance ownership",
            "- existing canonical implementation and any semantic delta",
            "- disposition: necessary specialization, valuable adapter, independent audit, "
            "compatibility debt, or true duplicate",
            "- parity/evidence gate that makes the decision safe.",
            "",
        ]
    )

    lines.extend(["", "## Capability-overlap candidates", ""])
    cap_overlap = capabilities.get("overlap", [])
    if cap_overlap:
        untagged = capabilities.get("untagged_lib_capabilities", [])
        if untagged:
            lines.append(
                f"> Registry health: {len(untagged)} lib capabilities have no "
                "docstring tag and are only recalled by exact name."
            )
            lines.append("")
        lines.extend(
            [
                "Same name or docstring tag as a lib capability. `sig-match` = "
                "identical parameter shape (likely a true duplicate); `no` = "
                "contract variant or name collision.",
                "",
                "| match | sig-match | local | lib (refs) | verdict |",
                "|---|---|---|---|---|",
            ]
        )
        for item in cap_overlap:
            local = item["local"]
            lib = item["lib"]
            sig = "yes" if item.get("signature_match") else "no"
            lines.append(
                f"| {item['match']} | {sig} | "
                f"`{local['path']}:{local['qualname']}` (L{local['lineno']}) | "
                f"`{lib['path']}:{lib['qualname']}` (L{lib['lineno']}, refs={lib['occurrences']}) | |"
            )
    else:
        lines.extend(["No candidates.", ""])

    lines.extend(["## Hard-coded-pattern candidates", ""])
    if hardcoded.get("hits"):
        for pattern, items in hardcoded["hits"].items():
            lines.append(f"### `{pattern}` ({len(items)})")
            lines.append("")
            for item in items:
                lines.append(
                    f"- `{item['id']}` `{item['path']}:{item['line']}`: `{item['code']}`"
                )
                lines.append(f"  Review prompt: {item['suggestion']}")
                lines.append("  Verdict:")
            lines.append("")
    else:
        lines.extend(["No candidates.", ""])

    lines.extend(["## Writing-style candidates", ""])
    if style.get("hits"):
        for metric, items in style["hits"].items():
            lines.append(f"### `{metric}` ({len(items)})")
            lines.append("")
            for item in items:
                line = item.get("line", "-")  # rate metrics are file-scoped
                lines.append(
                    f"- `{item['path']}:{line}`: {item['text']}"
                )
                lines.append(f"  Review prompt: {item['suggestion']}")
                lines.append("  Verdict:")
            lines.append("")
    else:
        lines.extend(["No candidates.", ""])

    lines.extend(
        [
            "## Semantic review log",
            "",
            "| scanner | candidate ID/path | verdict | evidence | follow-up |",
            "|---|---|---|---|---|",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
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
        default="research",
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
    args = ap.parse_args(argv)

    if args.cli_smoke:
        smoke_failures = scan_cli_smoke.smoke(("run_all",))
        if smoke_failures:
            for name, rc in smoke_failures:
                print(f"error: CLI smoke failed {name} --help exited {rc}", file=sys.stderr)
            return 2

    args.root = args.root.resolve()
    if args.json is None:
        args.json = args.root / "reports" / "latest.json"
    if args.markdown is None:
        args.markdown = args.root / "reports" / "latest.md"
    if args.ignore is None:
        args.ignore = args.root / "ignore.json"

    cfg = _audit_config.load_config(args.root)
    dup_cfg = cfg.get("duplicates", {})
    duplicate_threshold = _audit_config.pick(
        args.duplicate_threshold, dup_cfg, "threshold", 0.82
    )
    duplicate_min_chars = _audit_config.pick(
        args.duplicate_min_chars, dup_cfg, "min_chars", 120
    )
    config_path = args.root / _audit_config.CONFIG_FILENAME
    args.json = args.json.resolve()
    args.markdown = args.markdown.resolve()
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
                "contracts": (scan_contracts, []),
                "forks": (scan_forks, []),
                "capabilities": (scan_capabilities, []),
                "hardcoded": (scan_hardcoded, []),
                "style": (scan_style, []),
            }
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
        Path(scan_capabilities.__file__),
        Path(scan_deadcode.__file__),
        Path(scan_contracts.__file__),
        Path(scan_duplicates.__file__),
        Path(scan_forks.__file__),
        Path(scan_hardcoded.__file__),
        Path(scan_style.__file__),
        Path(__file__),
    ]
    summary = {
        "scanner": "self-audit-run-all",
        "schema_version": SCHEMA_VERSION,
        "package": args.package,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "configuration": {
            "document_channel": not args.no_doc_channel,
            "profile": args.profile,
            "duplicate_threshold": duplicate_threshold,
            "duplicate_min_chars": duplicate_min_chars,
            "ignore_file": str(args.ignore.resolve()) if args.ignore else None,
            "config_file": str(config_path) if config_path.is_file() else None,
            "cli_smoke": bool(args.cli_smoke),
        },
        "state": {
            "project_root": str(args.root),
            "state_dir": str(args.json.parent),
            "report_json": str(args.json),
            "report_markdown": str(args.markdown),
            "ignore_file": str(args.ignore),
            "lessons_file": str(args.root / "LESSONS.md"),
            "verdicts_file": str(args.json.parent / "verdicts.json"),
        },
        "provenance": {
            "git": _git_provenance(args.root, args.package),
            "scanner_sha256": {
                path.name: _sha256(path) for path in scanner_files
            },
        },
        "previous_run": _diff_previous(
            previous, payloads, args.package, args.profile
        ),
        "scanners": payloads,
    }
    args.json.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    args.markdown.write_text(_markdown(payloads, summary), encoding="utf-8")

    dead_count = sum(
        item["status"] == "DEAD" for item in payloads["deadcode"]["candidates"]
    )
    duplicate_count = len(payloads["duplicates"]["clusters"])
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
        f"dup_clusters={duplicate_count} cap_overlap={capability_count} "
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
