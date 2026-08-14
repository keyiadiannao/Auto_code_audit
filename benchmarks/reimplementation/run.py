"""Measure Candidate Recall@K and MRR of the capability retrieval.

This is the go/no-go instrument for the "implementation-reuse firewall" pivot:
for each authored case it asks one question — *does the correct existing
implementation surface in the retrieval's top-K for the new symbol?*

It measures two retrievers side by side:

  - ``scan_capabilities`` — the current name-first + docstring-fallback channel
    (measured via its internal ``_match_candidates``, which returns the full
    candidate list rather than the CLI's single best candidate);
  - ``capability_retrieval`` — the PR-2 multi-channel structural+lexical layer.

Run from the repository root:

    python benchmarks/reimplementation/run.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import capability_retrieval  # noqa: E402
import scan_capabilities  # noqa: E402

HERE = Path(__file__).resolve().parent
PKG = HERE / "fixture" / "pkg"
LIB = PKG / "lib"
EXP = PKG / "experiments"
CASES = HERE / "cases.json"

#: Verdicts for which the correct existing symbol MUST be surfaced.
_SURFACE_VERDICTS = {"REUSE_EXISTING", "EXTEND_EXISTING"}


def _recall_and_mrr(rank_of: dict[str, int | None], cases: list[dict]) -> dict[str, float]:
    surface = [c for c in cases if c["verdict"] in _SURFACE_VERDICTS]

    def recall(k: int) -> float:
        hits = sum(
            1
            for c in surface
            if rank_of.get(c["id"]) is not None and rank_of[c["id"]] <= k
        )
        return hits / len(surface)

    mrr = (
        sum(
            1 / rank_of[c["id"]]
            for c in surface
            if rank_of.get(c["id"]) is not None
        )
        / len(surface)
    )
    return {
        "recall@1": recall(1),
        "recall@5": recall(5),
        "recall@10": recall(10),
        "mrr": mrr,
        "must_surface": len(surface),
    }


def _run_baseline(cases: list[dict], doc_threshold: float) -> dict[str, int | None]:
    index, by_name = _cap_index()
    new_caps = _cap_new()
    rank_of: dict[str, int | None] = {}
    for case in cases:
        local = new_caps.get(case["new"])
        rank_of[case["id"]] = None
        if local is None:
            continue
        candidates, _ = scan_capabilities._match_candidates(
            local, index, by_name, doc_threshold
        )
        ranked = sorted(candidates, key=lambda p: (-p[0], p[1].key))
        for i, (_, item) in enumerate(ranked, start=1):
            if item.key == case["existing"]:
                rank_of[case["id"]] = i
                break
    return rank_of


def _run_retrieval(cases: list[dict]) -> dict[str, int | None]:
    index = capability_retrieval.build_index(LIB, rel_root=PKG)
    new_syms = {s.key: s for s in capability_retrieval.build_index(EXP, rel_root=PKG)}
    rank_of: dict[str, int | None] = {}
    for case in cases:
        query = new_syms.get(case["new"])
        rank_of[case["id"]] = None
        if query is None:
            continue
        for i, (_, sym) in enumerate(capability_retrieval.retrieve(query, index, k=10), start=1):
            if sym.key == case["existing"]:
                rank_of[case["id"]] = i
                break
    return rank_of


def _cap_index() -> tuple[list, dict[str, list]]:
    index = []
    for path in sorted(LIB.glob("*.py")):
        index.extend(scan_capabilities.extract_capabilities(path, rel_root=PKG))
    by_name: dict[str, list] = {}
    for item in index:
        by_name.setdefault(item.name, []).append(item)
    return index, by_name


def _cap_new() -> dict[str, "scan_capabilities.Capability"]:
    caps = {}
    for path in sorted(EXP.rglob("*.py")):
        for cap in scan_capabilities.extract_capabilities(path, rel_root=PKG):
            caps[cap.key] = cap
    return caps


def main(argv: list[str] | None = None) -> int:
    manifest = json.loads(CASES.read_text(encoding="utf-8"))
    doc_threshold = float(manifest.get("doc_threshold", 0.55))
    cases = manifest["cases"]

    baseline = _run_baseline(cases, doc_threshold)
    retrieval = _run_retrieval(cases)

    base_metrics = _recall_and_mrr(baseline, cases)
    ret_metrics = _recall_and_mrr(retrieval, cases)

    print("Reimplementation benchmark — recall of the correct existing symbol")
    print(f"{'case':<26} {'verdict':<16} {'baseline':<9} {'retrieval':<9}")
    for case in cases:
        b = baseline.get(case["id"])
        r = retrieval.get(case["id"])
        print(
            f"{case['id']:<26} {case['verdict']:<16} "
            f"{str(b):<9} {str(r):<9}"
        )
    print()
    print("metric            baseline   retrieval")
    for metric in ("recall@1", "recall@5", "recall@10", "mrr"):
        print(
            f"{metric:<16}  {base_metrics[metric]:<9.3f}  {ret_metrics[metric]:<9.3f}"
        )
    print()
    print(f"must-surface cases: {ret_metrics['must_surface']} "
          f"(REUSE_EXISTING + EXTEND_EXISTING)")
    keep = sum(1 for c in cases if c["verdict"] == "KEEP_SEPARATE")
    print(f"KEEP_SEPARATE cases: {keep} (surfacing them is also correct — the "
          f"reviewer must see the candidate to reject it; action accuracy is a "
          f"future metric)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
