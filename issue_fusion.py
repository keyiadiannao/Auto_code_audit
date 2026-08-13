"""Deterministically fuse corroborating scanner clusters into review issues.

Fusion is deliberately conservative: duplicate and region clusters merge only
when they describe the exact same non-trivial set of ``path:qualname`` symbols.
The function is label-independent and safe to run before adjudication.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any

from _scanner_common import short_hash


def _cluster_symbols(scanner: str, cluster: dict) -> tuple[frozenset[str], str]:
    """Return a cluster's member symbols and stable scanner target ID."""
    symbols = frozenset(
        f"{member['path']}:{member['qualname']}"
        for member in cluster.get("members", [])
        if member.get("path") and member.get("qualname")
    )
    prefix = "cluster" if scanner == "duplicates" else "region"
    return symbols, f"{prefix}/{cluster['id']}"


def cluster_issue_bundles(
    scanners: dict[str, dict],
    *,
    keep: Callable[[str, dict], bool] | None = None,
) -> list[dict[str, Any]]:
    """Build label-free issue bundles from clusters and atomic contract risks.

    ``keep`` applies the same review-cohort filter used by the Markdown report.
    Every retained cluster appears exactly once: corroborating cross-channel
    clusters share one issue, while unmatched clusters remain single-signal
    issues.
    """
    clusters: list[tuple[str, str, frozenset[str]]] = []
    for scanner, key in (("duplicates", "clusters"), ("regions", "clusters")):
        for cluster in scanners.get(scanner, {}).get(key, []):
            if keep is not None and not keep(scanner, cluster):
                continue
            symbols, target_id = _cluster_symbols(scanner, cluster)
            clusters.append((scanner, target_id, symbols))

    # File-level runtime-coupling findings are already one coherent issue.
    # Include them in the unified issue count even though they cannot fuse
    # with function-member clusters.
    for detail in scanners.get("contracts", {}).get(
        "dynamic_module_runtime_coupling", []
    ):
        if keep is not None and not keep("contracts", detail):
            continue
        path = detail.get("path")
        if path:
            clusters.append(
                (
                    "contracts",
                    f"dynamic_runtime/{path}",
                    frozenset({path}),
                )
            )

    bundles: list[dict[str, Any]] = []
    merged: set[tuple[str, str]] = set()
    by_symbols: dict[frozenset[str], list[tuple[str, str]]] = defaultdict(list)
    for scanner, target_id, symbols in clusters:
        if len(symbols) >= 2:
            by_symbols[symbols].append((scanner, target_id))

    for symbols, candidates in by_symbols.items():
        channels = {scanner for scanner, _ in candidates}
        if len(channels) < 2:
            continue
        members = [
            {"scanner": scanner, "target_id": target_id}
            for scanner, target_id in sorted(candidates)
        ]
        bundles.append(
            {
                "issue_id": "proposed/" + short_hash(*sorted(symbols)),
                "candidates": members,
                "channels": sorted(channels),
                "signals": len(members),
                "member_symbols": sorted(symbols),
            }
        )
        merged.update(candidates)

    for scanner, target_id, symbols in clusters:
        if (scanner, target_id) in merged:
            continue
        bundles.append(
            {
                "issue_id": f"{scanner}/{target_id}",
                "candidates": [{"scanner": scanner, "target_id": target_id}],
                "channels": [scanner],
                "signals": 1,
                "member_symbols": sorted(symbols),
            }
        )
    return sorted(bundles, key=lambda bundle: bundle["issue_id"])


def issue_summary(bundles: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the compact report-level summary for issue bundles."""
    signals = sum(int(bundle["signals"]) for bundle in bundles)
    corroborated = sum(1 for bundle in bundles if len(bundle["channels"]) >= 2)
    return {
        "schema_version": 2,
        "method": "exact-member-set-plus-atomic-v2",
        "candidate_signals": signals,
        "issue_count": len(bundles),
        "corroborated_issues": corroborated,
        "compression_ratio": round(signals / len(bundles), 3) if bundles else None,
        "bundles": bundles,
    }
