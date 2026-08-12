"""Markdown report formatter for the self-audit worksheet.

Extracted from ``run_all.py`` so that the ~380-line rendering logic has a
single responsibility and can be tested independently.  The public entry
point is :func:`markdown`, which receives the raw scanner payloads plus a
summary dict and returns the complete worksheet as a string.
"""
from __future__ import annotations

from collections.abc import Callable

SCANNER_NAMES: dict[str, str] = {
    "deadcode": "dead code",
    "duplicates": "duplicate clusters",
    "regions": "repeated code regions",
    "forks": "script-to-script forks",
    "contracts": "contract-boundary candidates",
    "capabilities": "capability overlap",
    "hardcoded": "hard-coded patterns",
    "style": "writing-style candidates",
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
    for scanner in SCANNER_NAMES:
        stats = block["per_scanner"].get(
            scanner, {"previous": 0, "current": 0, "new": [], "gone": []}
        )
        lines.append(
            f"| {SCANNER_NAMES[scanner]} | {stats['previous']} | "
            f"{stats['current']} | {len(stats['new'])} | {len(stats['gone'])} |"
        )
    lines.append("")
    for scanner in SCANNER_NAMES:
        stats = block["per_scanner"].get(
            scanner, {"previous": 0, "current": 0, "new": [], "gone": []}
        )
        for label, items in (("New", stats["new"]), ("Gone", stats["gone"])):
            if not items:
                continue
            lines.extend([f"### {label} {SCANNER_NAMES[scanner]} ({len(items)})", ""])
            for item in items[:15]:
                lines.append(f"- {item['display']}")
            if len(items) > 15:
                lines.append(f"- ... and {len(items) - 15} more")
            lines.append("")
    return lines


def _hidden_note(hidden: int, label: str) -> str:
    return (
        f"_{hidden} {label} in the low-value cohort are hidden by default; "
        "run with `--exhaustive` to include them._"
    )


def markdown(
    payloads: dict[str, dict],
    summary: dict,
    *,
    keep: Callable[[str, dict], bool] | None = None,
) -> str:
    """Render the full markdown review worksheet from scanner payloads.

    ``keep`` optionally rejects candidates by ``(scanner, detail)``: rejected
    items are the low-value cohort (no confirmed findings at the pinned
    benchmark commits), hidden by default and reachable by running the audit
    with ``--exhaustive``.
    """
    dead = payloads["deadcode"]
    duplicates = payloads["duplicates"]
    forks = payloads["forks"]
    contracts = payloads["contracts"]
    capabilities = payloads["capabilities"]
    hardcoded = payloads["hardcoded"]
    style = payloads["style"]
    regions = payloads.get("regions", {})
    provenance = summary["provenance"]

    def partition(scanner: str, items: list[dict]) -> tuple[list[dict], int]:
        if keep is None:
            return list(items), 0
        kept = [item for item in items if keep(scanner, item)]
        return kept, len(items) - len(kept)

    dead_candidates, dead_hidden = partition("deadcode", dead.get("candidates", []))
    dup_clusters, dup_hidden = partition("duplicates", duplicates.get("clusters", []))
    regions_clusters, regions_hidden = partition(
        "regions", regions.get("clusters", [])
    )
    fork_pairs, fork_hidden = partition("forks", forks.get("pairs", []))
    small_pairs, small_hidden = partition(
        "forks", forks.get("small_function_pairs", [])
    )
    cap_overlap, cap_hidden = partition(
        "capabilities", capabilities.get("overlap", [])
    )
    contract_parts = {
        key: partition("contracts", contracts.get(key, []))
        for key in (
            "experiment_as_library",
            "forwarding_wrappers",
            "same_name_contracts",
            "unreferenced_top_level_functions",
            "cli_without_bootstrap",
            "defensive_param_loosening",
            "env_written_not_read",
            "generation_path_without_env",
        )
    }
    contracts_hidden = sum(hidden for _, hidden in contract_parts.values())
    hardcoded_hits: dict[str, list[dict]] = {}
    hardcoded_hidden = 0
    for pattern, items in hardcoded.get("hits", {}).items():
        kept, hidden = partition("hardcoded", items)
        hardcoded_hidden += hidden
        if kept:
            hardcoded_hits[pattern] = kept
    style_hits: dict[str, list[dict]] = {}
    style_hidden = 0
    for metric, items in style.get("hits", {}).items():
        kept, hidden = partition("style", items)
        style_hidden += hidden
        if kept:
            style_hits[metric] = kept
    hidden_total = (
        dead_hidden
        + dup_hidden
        + regions_hidden
        + fork_hidden
        + small_hidden
        + cap_hidden
        + contracts_hidden
        + hardcoded_hidden
        + style_hidden
    )

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
            f"| repeated code regions | {len(regions.get('clusters', []))} "
            f"(H/M/L: {regions.get('priority_counts', {}).get('high', 0)}/"
            f"{regions.get('priority_counts', {}).get('medium', 0)}/"
            f"{regions.get('priority_counts', {}).get('low', 0)}) | "
            f"{regions.get('elapsed_seconds', 0):.3f} |",
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
        ]
    )
    if hidden_total:
        lines.extend(
            [
                f"> {hidden_total} candidates are in the low-value cohort "
                "(no confirmed findings at the pinned benchmark commits) and "
                "are hidden by default; run with `--exhaustive` to include them.",
                "",
            ]
        )
    lines.extend(["## Dead-code candidates", ""])
    if dead_candidates:
        lines.extend(
            [
                "| status | path | Python references | document references | verdict |",
                "|---|---|---|---|---|",
            ]
        )
        for item in dead_candidates:
            py_refs = ", ".join(item.get("py_refs", []) + item.get("dynamic_refs", [])) or "-"
            doc_refs = ", ".join(item.get("doc_refs", [])) or "-"
            lines.append(
                f"| {item['status']} | `{item['path']}` | {py_refs} | {doc_refs} | |"
            )
    elif dead_hidden:
        lines.append(_hidden_note(dead_hidden, "candidates"))
    else:
        lines.append("No candidates.")

    lines.extend(["", "## Duplicate-implementation candidates", ""])
    if dup_clusters:
        for cluster in dup_clusters:
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
        if dup_hidden:
            lines.append(_hidden_note(dup_hidden, "clusters"))
    elif dup_hidden:
        lines.extend([_hidden_note(dup_hidden, "clusters"), ""])
    else:
        lines.extend(["No candidates.", ""])

    lines.extend(["", "## Repeated code regions", ""])
    if regions_clusters:
        lines.extend(
            [
                "Statement blocks inside functions that recur with similar inputs, "
                "outputs, and API usage but have no named symbol: latent capabilities "
                "that may deserve one shared implementation.",
                "",
            ]
        )
        for cluster in regions_clusters:
            lines.append(f"### [{cluster['priority']}] `{cluster['id']}`")
            lines.append("")
            lines.append(f"Reason: {cluster['priority_reason']}.")
            lines.append("")
            if cluster.get("kind") == "helper_not_reused":
                canonical = cluster.get("canonical") or {}
                canonical_s = (
                    f"`{canonical['path']}:{canonical['qualname']}` "
                    f"(L{canonical['lineno']})"
                    if canonical
                    else f"`{cluster.get('canonical_symbol')}`"
                )
                lines.append(
                    f"Canonical helper: {canonical_s}; max coverage "
                    f"{cluster['max_coverage']:.3f}; {cluster['size']} inline copies."
                )
                lines.append("")
                for member in cluster["members"]:
                    referenced = (
                        " (referenced in parent)"
                        if member.get("canonical_referenced_in_parent")
                        else ""
                    )
                    lines.append(
                        f"- `{member['path']}:{member['qualname']}:"
                        f"{member['start_line']}-{member['end_line']}` "
                        f"({member['nstatements']} stmts, ext={member['extractability']}, "
                        f"coverage={member['coverage']:.3f}){referenced}"
                    )
            elif cluster.get("twin_match"):
                lines.append(
                    f"{cluster['size']} functions across {cluster['file_count']} files "
                    f"(edge similarity {cluster['min_edge_sim']:.3f}-{cluster['max_sim']:.3f}); "
                    "near-identical API-ful bodies"
                )
                lines.append("")
                for member in cluster["members"]:
                    lines.append(
                        f"- `{member['path']}:{member['qualname']}:"
                        f"{member['start_line']}` "
                        f"({member['nstatements']} stmts, coverage={member['coverage']:.3f})"
                    )
            else:
                hints = ", ".join(
                    f"`{hint}`" for hint in cluster.get("capability_hints", [])
                )
                short_tag = " [short-block cluster]" if cluster.get("short_block_cluster") else ""
                lines.append(
                    f"{cluster['size']} regions "
                    f"(edge similarity {cluster['min_edge_sim']:.3f}-{cluster['max_sim']:.3f})"
                    + (f"; hints: {hints}" if hints else "")
                    + short_tag
                )
                lines.append("")
                for member in cluster["members"]:
                    lines.append(
                        f"- `{member['path']}:{member['qualname']}:"
                        f"{member['start_line']}-{member['end_line']}` "
                        f"({member['nstatements']} stmts, ext={member['extractability']})"
                    )
            lines.extend(["- Verdict:", ""])
        if regions_hidden:
            lines.append(_hidden_note(regions_hidden, "clusters"))
    elif regions_hidden:
        lines.extend([_hidden_note(regions_hidden, "clusters"), ""])
    else:
        lines.extend(["No region clusters.", ""])

    lines.extend(["", "## Script-to-script fork candidates", ""])
    if fork_pairs:
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
        for item in fork_pairs:
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
    elif fork_hidden:
        lines.extend([_hidden_note(fork_hidden, "pairs"), ""])
    else:
        lines.extend(["No fork pairs.", ""])

    lines.extend(["", "## Small-function fork candidates", ""])
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
    elif small_hidden:
        lines.extend([_hidden_note(small_hidden, "pairs"), ""])
    else:
        lines.extend(["No small-function pairs.", ""])

    lines.extend(["", "## Function-contract candidates", ""])
    lines.extend(
        [
            "These hits require a caller-by-caller contract review. A thin wrapper may be a ",
            "valuable adapter, compatibility debt, or an independent audit boundary; code ",
            "shape alone cannot decide which.",
            "",
        ]
    )
    if any(kept for kept, _ in contract_parts.values()):
        lines.extend(["### Experiment modules used as libraries", ""])
        kept, hidden = contract_parts["experiment_as_library"]
        if kept:
            for item in kept:
                names = ", ".join(item["names"])
                lines.append(
                    f"- `{item['path']}:{item['line']}` imports `{item['module']}` "
                    f"({names}); importer layer: `{item['importer_layer']}`"
                )
        elif hidden:
            lines.append(_hidden_note(hidden, "candidates"))

        lines.extend(["", "### Forwarding wrappers", ""])
        kept, hidden = contract_parts["forwarding_wrappers"]
        if kept:
            for item in kept:
                lines.append(
                    f"- `{item['path']}:{item['line']}` `{item['name']}{item['signature']}` "
                    f"delegates to `{item['target']}`; returns "
                    f"`{', '.join(item['return_contract'])}`"
                )
        elif hidden:
            lines.append(_hidden_note(hidden, "candidates"))

        lines.extend(["", "### Repeated contract-sensitive names", ""])
        kept, hidden = contract_parts["same_name_contracts"]
        if kept:
            for group in kept:
                lines.append(f"#### `{group['name']}`")
                lines.append("")
                for item in group["definitions"]:
                    lines.append(
                        f"- `{item['path']}:{item['line']}` `{item['signature']}` -> "
                        f"`{', '.join(item['return_contract'])}`"
                    )
                lines.append("")
        elif hidden:
            lines.append(_hidden_note(hidden, "candidates"))

        lines.extend(["", "### Unreferenced top-level functions", ""])
        lines.append(
            "This is a coarse symbol-use screen. Dynamic dispatch and external entrypoints "
            "remain manual-review false positives."
        )
        lines.append("")
        kept, hidden = contract_parts["unreferenced_top_level_functions"]
        if kept:
            for item in kept:
                lock = (
                    f"; source lock: {item['source_lock']}"
                    if item.get("source_lock")
                    else ""
                )
                lines.append(
                    f"- `{item['path']}:{item['line']}` "
                    f"`{item['name']}{item['signature']}`{lock}"
                )
        elif hidden:
            lines.append(_hidden_note(hidden, "candidates"))

        lines.extend(["", "### CLI entry scripts without sys.path bootstrap", ""])
        kept, hidden = contract_parts["cli_without_bootstrap"]
        if kept:
            lines.extend(
                [
                    "These entry scripts import package modules but never add the repo "
                    "root to sys.path, so they only run when the cwd already contains "
                    "the repo root or when launched via `python -m`. Verify the launch "
                    "method against how the submission actually runs them.",
                    "",
                ]
            )
            for item in kept:
                lines.append(
                    f"- `{item['path']}:{item['line']}` imports `{item['module']}` "
                    "without a bootstrap"
                )
        elif hidden:
            lines.append(_hidden_note(hidden, "candidates"))

        lines.extend(["", "### Defensive-parameter loosening", ""])
        kept, hidden = contract_parts["defensive_param_loosening"]
        if kept:
            lines.extend(
                [
                    "`strict=False` / `weights_only=False` weaken a load-time safety "
                    "contract. Each hit needs a verdict: deliberate partial load, or "
                    "accidental degradation.",
                    "",
                ]
            )
            for item in kept:
                lines.append(f"- `{item['path']}:{item['line']}` `{item['code']}`")
        elif hidden:
            lines.append(_hidden_note(hidden, "candidates"))

        lines.extend(["", "### Env-contract candidates", ""])
        env_written, env_hidden = contract_parts["env_written_not_read"]
        gen_path, gen_hidden = contract_parts["generation_path_without_env"]
        env_hidden_total = env_hidden + gen_hidden
        if env_written or gen_path:
            for item in env_written:
                lines.append(
                    f"- env `{item['var']}` written at `{item['path']}:{item['line']}` "
                    "but never read in-package"
                )
            for item in gen_path:
                first = item["constants"][0]
                lines.append(
                    f"- `{item['path']}` embeds a generation-pinned path "
                    f"({len(item['constants'])} const(s), first L{first['line']}) "
                    "with no env-var read in the file"
                )
        elif env_hidden_total:
            lines.append(_hidden_note(env_hidden_total, "candidates"))
    elif contracts_hidden:
        lines.extend([_hidden_note(contracts_hidden, "candidates"), ""])
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
    elif cap_hidden:
        lines.extend([_hidden_note(cap_hidden, "candidates"), ""])
    else:
        lines.extend(["No candidates.", ""])

    lines.extend(["## Hard-coded-pattern candidates", ""])
    if hardcoded_hits:
        for pattern, items in hardcoded_hits.items():
            lines.append(f"### `{pattern}` ({len(items)})")
            lines.append("")
            for item in items:
                lines.append(
                    f"- `{item['id']}` `{item['path']}:{item['line']}`: `{item['code']}`"
                )
                lines.append(f"  Review prompt: {item['suggestion']}")
                lines.append("  Verdict:")
            lines.append("")
    elif hardcoded_hidden:
        lines.extend([_hidden_note(hardcoded_hidden, "candidates"), ""])
    else:
        lines.extend(["No candidates.", ""])

    lines.extend(["## Writing-style candidates", ""])
    if style_hits:
        for metric, items in style_hits.items():
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
    elif style_hidden:
        lines.extend([_hidden_note(style_hidden, "candidates"), ""])
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
