"""Measure Candidate Recall@K and MRR of the capability retrieval.

This is the go/no-go instrument for the "implementation-reuse firewall" pivot:
for each authored case it asks one question — *does the correct existing
implementation surface in the retrieval's top-K for the new symbol?*

It measures the CURRENT retrieval (``scan_capabilities``), whose candidate
generation is name-first and docstring-fallback.  It deliberately calls the
internal ``_match_candidates`` (which returns the full candidate list) rather
than the CLI (which collapses to a single best candidate), because the pivot's
first question is recall — whether the right symbol is surfaced *at all*.

Run from the repository root:

    python benchmarks/reimplementation/run.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import scan_capabilities  # noqa: E402

HERE = Path(__file__).resolve().parent
PKG = HERE / "fixture" / "pkg"
LIB = PKG / "lib"
EXP = PKG / "experiments"
CASES = HERE / "cases.json"

#: Verdicts for which the correct existing symbol MUST be surfaced.
_SURFACE_VERDICTS = {"REUSE_EXISTING", "EXTEND_EXISTING"}


def _build_index() -> tuple[list, dict[str, list]]:
    index = []
    for path in sorted(LIB.glob("*.py")):
        index.extend(scan_capabilities.extract_capabilities(path, rel_root=PKG))
    by_name: dict[str, list] = {}
    for item in index:
        by_name.setdefault(item.name, []).append(item)
    return index, by_name


def _extract_new() -> dict[str, "scan_capabilities.Capability"]:
    caps = {}
    for path in sorted(EXP.rglob("*.py")):
        for cap in scan_capabilities.extract_capabilities(path, rel_root=PKG):
            caps[cap.key] = cap
    return caps


def main(argv: list[str] | None = None) -> int:
    manifest = json.loads(CASES.read_text(encoding="utf-8"))
    doc_threshold = float(manifest.get("doc_threshold", 0.55))
    cases = manifest["cases"]

    index, by_name = _build_index()
    new_caps = _extract_new()

    rank_of: dict[str, int | None] = {}
    rows: list[tuple[str, str, str, int | None, int]] = []
    for case in cases:
        local = new_caps.get(case["new"])
        if local is None:
            rows.append((case["id"], "MISSING_NEW", "-", None, 0))
            continue
        candidates, channel = scan_capabilities._match_candidates(
            local, index, by_name, doc_threshold
        )
        ranked = sorted(candidates, key=lambda p: (-p[0], p[1].key))
        rank = None
        for i, (_, item) in enumerate(ranked, start=1):
            if item.key == case["existing"]:
                rank = i
                break
        rank_of[case["id"]] = rank
        rows.append((case["id"], case["verdict"], channel, rank, len(ranked)))

    surface = [c for c in cases if c["verdict"] in _SURFACE_VERDICTS]

    def recall(k: int) -> float:
        hits = sum(
            1
            for c in surface
            if rank_of.get(c["id"]) is not None and rank_of[c["id"]] <= k
        )
        return hits / len(surface)

    mrr = sum(
        1 / rank_of[c["id"]]
        for c in surface
        if rank_of.get(c["id"]) is not None
    ) / len(surface)

    print("Reimplementation benchmark — current scan_capabilities retrieval")
    print(f"{'case':<26} {'verdict':<16} {'channel':<8} {'rank':<6} {'cands'}")
    for cid, verdict, channel, rank, n in rows:
        print(f"{cid:<26} {verdict:<16} {channel:<8} {str(rank):<6} {n}")
    print()
    print(f"must-surface cases        : {len(surface)}")
    print(f"Candidate Recall@1        : {recall(1):.3f}")
    print(f"Candidate Recall@5        : {recall(5):.3f}")
    print(f"Candidate Recall@10       : {recall(10):.3f}")
    print(f"MRR                       : {mrr:.3f}")
    print(f"KEEP_SEPARATE cases       : {sum(1 for c in cases if c['verdict'] == 'KEEP_SEPARATE')} (excluded from recall; measured by future action-accuracy)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
