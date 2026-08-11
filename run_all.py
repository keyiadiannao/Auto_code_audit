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

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scan_capabilities
import scan_deadcode
import scan_contracts
import scan_duplicates
import scan_forks
import scan_hardcoded
import scan_style

SKILL_DIR = Path(__file__).resolve().parent


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
                "| sim | kind | left | right | sig | verdict |",
                "|---|---|---|---|---|---|",
            ]
        )
        for item in forks["pairs"]:
            left, right = item["left"], item["right"]
            sig = "yes" if item["signature_match"] else "no"
            lines.append(
                f"| {item['similarity']:.3f} | {item['kind']} | "
                f"`{left['path']}:{left['qualname']}` "
                f"(L{left['lineno']}, {left['nlines']} lines) | "
                f"`{right['path']}:{right['qualname']}` "
                f"(L{right['lineno']}, {right['nlines']} lines) | {sig} | |"
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
                "| sim | kind | left | right | sig | verdict |",
                "|---|---|---|---|---|---|",
            ]
        )
        for item in small_pairs:
            left, right = item["left"], item["right"]
            sig = "yes" if item["signature_match"] else "no"
            lines.append(
                f"| {item['similarity']:.3f} | {item['kind']} | "
                f"`{left['path']}:{left['qualname']}` "
                f"(L{left['lineno']}, {left['nlines']} lines) | "
                f"`{right['path']}:{right['qualname']}` "
                f"(L{right['lineno']}, {right['nlines']} lines) | {sig} | |"
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
        help="repo root (default: script's repo)",
    )
    ap.add_argument("--package", default="src")
    ap.add_argument(
        "--json", type=Path, default=SKILL_DIR / "reports" / "latest.json"
    )
    ap.add_argument(
        "--markdown", type=Path, default=SKILL_DIR / "reports" / "latest.md"
    )
    ap.add_argument("--ignore", type=Path, default=SKILL_DIR / "ignore.json")
    ap.add_argument("--no-doc-channel", action="store_true")
    ap.add_argument("--duplicate-threshold", type=float, default=0.82)
    ap.add_argument("--duplicate-min-chars", type=int, default=120)
    args = ap.parse_args(argv)

    args.root = args.root.resolve()
    args.json = args.json.resolve()
    args.markdown = args.markdown.resolve()
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(
            prefix="self-audit-", dir=str(args.json.parent)
        ) as temporary:
            tempdir = Path(temporary)
            payloads = {
                "deadcode": _run_scanner(
                    scan_deadcode,
                    args,
                    tempdir / "deadcode.json",
                    ["--no-doc-channel"] if args.no_doc_channel else [],
                ),
                "duplicates": _run_scanner(
                    scan_duplicates,
                    args,
                    tempdir / "duplicates.json",
                    [
                        "--threshold",
                        str(args.duplicate_threshold),
                        "--min-chars",
                        str(args.duplicate_min_chars),
                    ],
                ),
                "contracts": _run_scanner(
                    scan_contracts, args, tempdir / "contracts.json", []
                ),
                "forks": _run_scanner(
                    scan_forks, args, tempdir / "forks.json", []
                ),
                "capabilities": _run_scanner(
                    scan_capabilities, args, tempdir / "capabilities.json", []
                ),
                "hardcoded": _run_scanner(
                    scan_hardcoded, args, tempdir / "hardcoded.json", []
                ),
                "style": _run_scanner(
                    scan_style,
                    args,
                    tempdir / "style.json",
                    ["--tex-dir", scan_style.DEFAULT_TEX_DIR],
                ),
            }
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
        "schema_version": 4,
        "package": args.package,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "configuration": {
            "document_channel": not args.no_doc_channel,
            "duplicate_threshold": args.duplicate_threshold,
            "duplicate_min_chars": args.duplicate_min_chars,
            "ignore_file": str(args.ignore.resolve()) if args.ignore else None,
        },
        "provenance": {
            "git": _git_provenance(args.root, args.package),
            "scanner_sha256": {
                path.name: _sha256(path) for path in scanner_files
            },
        },
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
    style_count = sum(len(items) for items in payloads["style"]["hits"].values())
    print(
        f"SELF_AUDIT_RUN_ALL package={args.package} dead={dead_count} "
        f"dup_clusters={duplicate_count} cap_overlap={capability_count} "
        f"forks={fork_count} "
        f"hardcoded={hardcoded_count} "
        f"style={style_count} seconds={summary['elapsed_seconds']:.3f}"
    )
    print(f"  json: {args.json}")
    print(f"  markdown: {args.markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
