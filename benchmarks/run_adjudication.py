#!/usr/bin/env python3
"""Score adjudications against human-reviewed ground truth.

Skill-first design: the product is a skill for CLI AI platforms, so the
default adapter is ``file`` -- the executing agent (the platform's own,
user-configured model) reads prepared case bundles and writes protocol
verdicts as JSON files.  The benchmark runner only collects, validates,
and scores.  ``--adapter http`` remains available purely as a headless
benchmark harness (OpenAI-compatible endpoint; see AUDIT_LLM_* env vars).

Workflow:

1. ``--prepare-cases`` writes ``cases.jsonl`` (one protocol case bundle per
   line) for the agent/skill to adjudicate.
2. The agent writes one verdict file per case into the verdicts directory:
   ``<evidence_hash>.json`` containing ``{"schema_version": 1,
   "evidence_hash": ..., "adapter": "agent", "verdict": {...}}``.
3. The runner scores every verdict file against corpus ground truth
   (``--corpus-dir``) or raw labels, reports confusion metrics and the
   Layer-1/Layer-2 funnel, and fails when verdicts are missing or violate
   the protocol.

Verdicts are bound to evidence by hash: a changed candidate record, code
snippet, or pinned commit changes the hash and silently invalidates the
old verdict file.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

TOOL_ROOT = Path(__file__).resolve().parents[1]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent / "results"
DEFAULT_LABELS_DIR = Path(__file__).resolve().parent / "labels"
DEFAULT_CORPUS_DIR = Path(__file__).resolve().parent / "adjudication" / "corpus"
DEFAULT_VERDICTS_DIR = Path(__file__).resolve().parent / "adjudication" / "verdicts"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "adjudication" / "latest.json"
DEFAULT_CASES_FILE = Path(__file__).resolve().parent / "adjudication" / "cases.jsonl"

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_API_KEY = "ollama"

# ---------------------------------------------------------------------------
# Case preparation (protocol-shaped, label-free bundles)
# ---------------------------------------------------------------------------


def build_cases(
    results_dir: Path,
    labels_dir: Path,
    package_roots: dict[str, Path],
    *,
    projects: set[str] | None = None,
    scanners: set[str] | None = None,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str], list[str]]:
    """Build protocol case bundles for labelled candidates.

    Returns ``(cases, truth_by_hash, warnings)``.  ``truth_by_hash`` maps
    evidence hash -> label and is used for scoring only; the case bundles
    never carry the label.
    """
    from run_all import _candidate_signatures
    from benchmarks.adjudication_protocol import validate_case
    from benchmarks.adjudication_cases import build_case

    cases: list[dict[str, Any]] = []
    truth_by_hash: dict[str, str] = {}
    warnings: list[str] = []
    for report_path in sorted(results_dir.glob("*.json")):
        if report_path.name == "latest.json":
            continue
        project_id = report_path.stem
        if projects and project_id not in projects:
            continue
        report = json.loads(report_path.read_text(encoding="utf-8"))
        labels = _load_labels_for(labels_dir, project_id)
        if labels is None:
            warnings.append(f"{project_id}: no label file, skipped")
            continue
        commit = report.get("commit") or labels.get("commit", "")
        labels_commit = labels.get("commit", "")
        if (
            labels_commit
            and report.get("commit")
            and labels_commit != report.get("commit")
        ):
            warnings.append(
                f"{project_id}: label commit {labels_commit!r} != report "
                f"commit {report.get('commit')!r}, skipped"
            )
            continue
        if not commit:
            warnings.append(f"{project_id}: no pinned commit for evidence, skipped")
            continue
        root = package_roots.get(project_id)
        if root is None:
            warnings.append(f"{project_id}: no checkout at pinned commit, skipped")
            continue
        truth = {
            (entry["scanner"], entry["target_id"]): entry["label"]
            for entry in labels.get("labels", [])
        }
        signatures = _candidate_signatures(report.get("scanners", {}))
        for scanner, items in signatures.items():
            if scanners and scanner not in scanners:
                continue
            for signature, display, detail in items:
                if (scanner, signature) not in truth:
                    continue
                bundle = build_case(
                    project_id, commit, scanner, signature, display, detail, root
                )
                validate_case(bundle)
                truth_by_hash[bundle["evidence_hash"]] = truth[(scanner, signature)]
                cases.append(bundle)
    cases.sort(key=lambda case: (case["project_id"], case["scanner"], case["target_id"]))
    if limit is not None:
        cases = cases[:limit]
    return cases, truth_by_hash, warnings


def restrict_to_corpus(
    cases: list[dict[str, Any]],
    truth_by_hash: dict[str, str],
    corpus_dir: Path,
    *,
    verified_only: bool,
) -> tuple[list[dict[str, Any]], dict[str, str], list[str]]:
    """Keep only corpus entries; corpus entries are the ground truth.

    ``verified_only`` gates on the human-review marker and is applied at
    scoring time; case preparation always covers the full corpus scope.
    """
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    for corpus_path in sorted(corpus_dir.glob("*.json")):
        payload = json.loads(corpus_path.read_text(encoding="utf-8"))
        for entry in payload.get("entries", []):
            entries[(entry["scanner"], entry["target_id"])] = entry
    kept: list[dict[str, Any]] = []
    truth: dict[str, str] = {}
    warnings: list[str] = []
    for case in cases:
        entry = entries.get((case["scanner"], case["target_id"]))
        if entry is None:
            continue
        if verified_only and not entry.get("human_verified"):
            continue
        digest = case["evidence_hash"]
        if entry.get("evidence_hash") and entry["evidence_hash"] != digest:
            warnings.append(
                f"{case['project_id']}/{case['scanner']}/{case['target_id']}: "
                "corpus evidence_hash mismatch, skipped (stale truth)"
            )
            continue
        kept.append(case)
        truth[digest] = entry["label"]
    return kept, truth, warnings


# ---------------------------------------------------------------------------
# Verdict file I/O (protocol-shaped, one validator)
# ---------------------------------------------------------------------------
# The file schema and the full validation pipeline live in _verdict_files.py,
# shared with the engine-owned verify gate (run_verify.py).  This module
# re-exports them so the scoring loop and the gate consume the same code.
from _verdict_files import (
    VERDICT_FILE_SCHEMA,
    load_protocol_verdict,
    read_verdict_file,
    write_verdict_file,
)


def user_prompt(bundle: dict[str, Any]) -> str:
    from benchmarks.adjudication_protocol import SCANNER_RUBRIC

    rubric = SCANNER_RUBRIC.get(bundle["scanner"], "")
    lines = [
        "Adjudicate the following candidate. Scanner-specific guidance:",
        rubric or "Use general judgement.",
        "",
        "Candidate evidence (JSON):",
        json.dumps(
            {
                "scanner": bundle["scanner"],
                "target_id": bundle["target_id"],
                "display": bundle["display"],
                "evidence": bundle["evidence"],
            },
            ensure_ascii=False,
            indent=1,
        ),
    ]
    if bundle["snippets"]:
        lines.append("Code snippets (path, start line, source):")
        for snippet in bundle["snippets"]:
            lines.append(
                f"--- {snippet.get('path')} (L{snippet.get('start_line', '?')}) ---"
            )
            lines.append(snippet.get("code", snippet.get("error", "")))
    lines.append("Return only the JSON verdict object.")
    return "\n".join(lines)


def prepare_cases(
    cases: list[dict[str, Any]],
    cases_file: Path,
) -> None:
    cases_file.parent.mkdir(parents=True, exist_ok=True)
    with cases_file.open("w", encoding="utf-8", newline="\n") as handle:
        for bundle in cases:
            handle.write(
                json.dumps(
                    {
                        "evidence_hash": bundle["evidence_hash"],
                        "bundle": bundle,
                        "prompt": user_prompt(bundle),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


# ---------------------------------------------------------------------------
# Optional headless HTTP adapter (benchmark harness only)
# ---------------------------------------------------------------------------


def _parse_verdict(content: str) -> dict[str, Any] | None:
    from benchmarks.adjudication_protocol import validate_verdict

    text = content.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    try:
        return validate_verdict(payload)
    except ValueError:
        return None


def call_model(
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    timeout: int = 180,
) -> dict[str, Any] | None:
    """Call an OpenAI-compatible endpoint; returns a protocol verdict or None."""
    from benchmarks.adjudication_protocol import SYSTEM_PROMPT

    url = base_url.rstrip("/") + "/chat/completions"
    body: dict[str, Any] = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }
    attempts = [dict(body, response_format={"type": "json_object"}), body]
    for index, payload in enumerate(attempts):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"]
            return _parse_verdict(content)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            status = getattr(exc, "code", None)
            if status == 400 and index == 0:
                continue
            return None
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            return None
    return None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


def confusion_metrics(
    scored: list[dict[str, Any]],
) -> dict[str, Any]:
    true = [entry for entry in scored if entry["ground_truth"] == "true_finding"]
    false = [entry for entry in scored if entry["ground_truth"] == "false_positive"]
    tp = sum(1 for entry in true if entry["predicted"] == "true_finding")
    fn = len(true) - tp
    fp = sum(1 for entry in false if entry["predicted"] == "true_finding")
    tn = len(false) - fp

    return {
        "ground_true": len(true),
        "ground_false": len(false),
        "predicted_true": tp + fp,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "adjudication_precision": _ratio(tp, tp + fp),
        "adjudication_recall": _ratio(tp, tp + fn),
        "fp_rejection_rate": _ratio(tn, tn + fp),
        "fn_rate": _ratio(fn, tp + fn),
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _default_workspace() -> Path:
    env = os.environ.get("AUDIT_BENCH_WORKSPACE")
    if env:
        return Path(env)
    from benchmarks.run_benchmarks import DEFAULT_WORKSPACE

    default = Path(DEFAULT_WORKSPACE)
    fallback = Path(os.environ.get("TEMP", ".")) / "opencode" / "bench_ws"
    return default if default.is_dir() else fallback


def _package_roots(workspace: Path, projects: list[dict[str, Any]]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for project in projects:
        package = workspace / project["id"] / project["package"]
        if package.is_dir():
            roots[project["id"]] = package
    return roots


def _load_labels_for(labels_dir: Path, project_id: str) -> dict[str, Any] | None:
    from benchmarks.run_benchmarks import _load_labels_for as load

    return load(labels_dir, project_id)


def load_cases_from_file(cases_file: Path) -> list[dict[str, Any]]:
    """Load prepared protocol bundles (JSONL) for scoring."""
    return [
        json.loads(line)["bundle"]
        for line in cases_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_truth_file(path: Path) -> dict[str, str]:
    """Load a ``{evidence_hash: label}`` ground-truth map from JSON."""
    from benchmarks.adjudication_protocol import DISPOSITIONS

    payload = json.loads(path.read_text(encoding="utf-8"))
    labels = payload if isinstance(payload, dict) else payload.get("labels", {})
    if not isinstance(labels, dict):
        raise ValueError("--truth-file must be a JSON object {evidence_hash: label}")
    unknown = set(labels.values()) - DISPOSITIONS
    if unknown:
        raise ValueError(f"--truth-file contains unknown labels: {sorted(unknown)}")
    return {str(digest): label for digest, label in labels.items() if digest}


def run_adjudication(
    *,
    results_dir: Path,
    labels_dir: Path,
    workspace: Path,
    verdicts_dir: Path,
    output: Path,
    cases_file: Path,
    corpus_dir: Path,
    projects: set[str] | None = None,
    scanners: set[str] | None = None,
    limit: int | None = None,
    include_unverified: bool = False,
    adapter: str = "file",
    model: str = "",
    base_url: str = DEFAULT_BASE_URL,
    api_key: str = DEFAULT_API_KEY,
    prepare_only: bool = False,
    from_cases_file: Path | None = None,
    truth_file: Path | None = None,
) -> dict[str, Any]:
    from benchmarks.run_benchmarks import load_manifest

    if from_cases_file is not None:
        cases = load_cases_from_file(from_cases_file)
        truth_by_hash: dict[str, str] = {}
        warnings: list[str] = []
        if not cases:
            raise ValueError(f"no case bundles found in {from_cases_file}")
    else:
        manifest = load_manifest(Path(__file__).resolve().parent / "manifest.json")
        roots = _package_roots(workspace, manifest["projects"])
        cases, truth_by_hash, warnings = build_cases(
            results_dir,
            labels_dir,
            roots,
            projects=projects,
            scanners=scanners,
            limit=limit,
        )
    if corpus_dir.is_dir() and any(corpus_dir.glob("*.json")):
        cases, truth_by_hash, corpus_warnings = restrict_to_corpus(
            cases,
            truth_by_hash,
            corpus_dir,
            verified_only=not include_unverified and not prepare_only,
        )
        warnings.extend(corpus_warnings)
    elif include_unverified:
        warnings.append("--include-unverified given but no corpus found")
    if truth_file is not None:
        override = load_truth_file(truth_file)
        unknown = sorted(set(override) - {case["evidence_hash"] for case in cases})
        if unknown:
            warnings.append(
                f"--truth-file covers hashes outside the case set: {unknown}"
            )
        truth_by_hash.update(override)
    if prepare_only:
        prepare_cases(cases, cases_file)
        return {
            "schema_version": 1,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mode": "prepare",
            "cases": len(cases),
            "cases_file": str(cases_file),
            "warnings": warnings,
        }

    verdicts_dir.mkdir(parents=True, exist_ok=True)
    if adapter == "http":
        if not model:
            raise ValueError("--model is required for the http adapter")
        for case in cases:
            path = verdicts_dir / f"{case['evidence_hash']}.json"
            if path.is_file():
                continue
            verdict = call_model(
                base_url=base_url, api_key=api_key, model=model, prompt=user_prompt(case)
            )
            if verdict is not None:
                write_verdict_file(
                    verdicts_dir, case["evidence_hash"], verdict, case=case
                )

    predictions: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    uncovered: list[dict[str, Any]] = []
    for case in cases:
        digest = case["evidence_hash"]
        ground = truth_by_hash.get(digest)
        if ground is None:
            uncovered.append(
                {
                    "project_id": case["project_id"],
                    "scanner": case["scanner"],
                    "target_id": case["target_id"],
                    "evidence_hash": digest,
                }
            )
            continue
        path = verdicts_dir / f"{digest}.json"
        if not path.is_file():
            pending.append(
                {
                    "project_id": case["project_id"],
                    "scanner": case["scanner"],
                    "target_id": case["target_id"],
                    "evidence_hash": digest,
                }
            )
            continue
        try:
            verdict = read_verdict_file(path, digest)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            invalid.append(
                {
                    "project_id": case["project_id"],
                    "scanner": case["scanner"],
                    "target_id": case["target_id"],
                    "evidence_hash": digest,
                    "error": str(exc),
                }
            )
            continue
        if verdict is None:
            invalid.append(
                {
                    "project_id": case["project_id"],
                    "scanner": case["scanner"],
                    "target_id": case["target_id"],
                    "evidence_hash": digest,
                    "error": "verdict violates protocol",
                }
            )
            continue
        predictions.append(
            {
                "project_id": case["project_id"],
                "scanner": case["scanner"],
                "target_id": case["target_id"],
                "evidence_hash": digest,
                "ground_truth": ground,
                "predicted": verdict["disposition"],
                "confidence": verdict["confidence"],
                "reason": verdict["reason"],
                "reason_codes": verdict["reason_codes"],
                "recommended_action": verdict["recommended_action"],
                "reuse_target": verdict["reuse_target"],
                "required_verification": verdict["required_verification"],
            }
        )

    per_project_metrics: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in predictions:
        grouped.setdefault(entry["project_id"], []).append(entry)
    for project_id in sorted(grouped):
        metric = confusion_metrics(grouped[project_id])
        metric["project_id"] = project_id
        metric["cases"] = len(grouped[project_id])
        per_project_metrics.append(metric)
    totals = confusion_metrics(predictions)
    totals["cases"] = len(cases)
    totals["pending"] = len(pending)
    totals["invalid"] = len(invalid)
    totals["uncovered"] = len(uncovered)
    totals["protocol_compliance_rate"] = _ratio(len(predictions), len(cases))
    high_conf = [entry for entry in predictions if entry["confidence"] >= 0.9]
    high_conf_errors = [
        entry
        for entry in high_conf
        if entry["predicted"] != entry["ground_truth"]
    ]
    totals["high_confidence_errors"] = len(high_conf_errors)
    totals["high_confidence_cases"] = len(high_conf)
    totals["high_confidence_error_rate"] = _ratio(
        len(high_conf_errors), len(high_conf)
    )
    totals["funnel"] = {
        "labelled": len(cases),
        "ground_true": totals["ground_true"],
        "ai_true": totals["predicted_true"],
    }

    payload = {
        "schema_version": 1,
        "tool": "auto-code-audit",
        "adapter": adapter,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "warnings": warnings,
        "pending": pending,
        "invalid": invalid,
        "uncovered": uncovered,
        "per_project": per_project_metrics,
        "totals": totals,
        "predictions": predictions,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--labels-dir", type=Path, default=DEFAULT_LABELS_DIR)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="checkouts dir (default: benchmark workspace, else TEMP/opencode/bench_ws)",
    )
    parser.add_argument("--verdicts-dir", type=Path, default=DEFAULT_VERDICTS_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cases-file", type=Path, default=DEFAULT_CASES_FILE)
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=DEFAULT_CORPUS_DIR,
        help="reviewed ground-truth corpus; default mode when present",
    )
    parser.add_argument("--include-unverified", action="store_true")
    parser.add_argument("--project", action="append", default=[])
    parser.add_argument("--scanner", action="append", default=[])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--adapter",
        choices=("file", "http"),
        default="file",
        help="file: agent-written verdicts (default); http: harness-only model calls",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("AUDIT_LLM_MODEL", ""),
        help="model name for the http adapter (env AUDIT_LLM_MODEL)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AUDIT_LLM_BASE_URL", DEFAULT_BASE_URL),
        help="OpenAI-compatible base URL (env AUDIT_LLM_BASE_URL)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("AUDIT_LLM_API_KEY", DEFAULT_API_KEY),
        help="API key (env AUDIT_LLM_API_KEY)",
    )
    parser.add_argument(
        "--prepare-cases",
        action="store_true",
        help="write prepared case bundles for the agent, then exit",
    )
    parser.add_argument(
        "--from-cases",
        type=Path,
        default=None,
        help="score already-prepared case bundles (JSONL) instead of building "
        "them from results/labels",
    )
    parser.add_argument(
        "--truth-file",
        type=Path,
        default=None,
        help="JSON object mapping evidence_hash -> label; overrides/augments "
        "corpus truth (used with --from-cases)",
    )
    args = parser.parse_args(argv)
    if args.truth_file is not None and args.from_cases is None:
        parser.error("--truth-file requires --from-cases")
    try:
        payload = run_adjudication(
            results_dir=args.results_dir.resolve(),
            labels_dir=args.labels_dir.resolve(),
            workspace=(args.workspace or _default_workspace()).resolve(),
            verdicts_dir=args.verdicts_dir.resolve(),
            output=args.output.resolve(),
            cases_file=args.cases_file.resolve(),
            corpus_dir=args.corpus_dir.resolve(),
            projects=set(args.project) or None,
            scanners=set(args.scanner) or None,
            limit=args.limit,
            include_unverified=args.include_unverified,
            adapter=args.adapter,
            model=args.model,
            base_url=args.base_url,
            api_key=args.api_key,
            prepare_only=args.prepare_cases,
            from_cases_file=args.from_cases,
            truth_file=args.truth_file,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: adjudication benchmark failed: {exc}", file=sys.stderr)
        return 2

    if payload.get("mode") == "prepare":
        print(
            f"ADJUDICATION PREPARED cases={payload['cases']} "
            f"file={payload['cases_file']}"
        )
        return 0

    for metric in payload["per_project"]:
        precision = metric["adjudication_precision"]
        recall = metric["adjudication_recall"]
        print(
            f"ADJUDICATION {metric['project_id']} "
            f"precision={precision if precision is not None else '-'} "
            f"recall={recall if recall is not None else '-'} "
            f"fp_rejection={metric['fp_rejection_rate'] if metric['fp_rejection_rate'] is not None else '-'} "
            f"fn_rate={metric['fn_rate'] if metric['fn_rate'] is not None else '-'} "
            f"cases={metric['cases']}"
        )
    totals = payload["totals"]
    precision = totals["adjudication_precision"]
    recall = totals["adjudication_recall"]
    funnel = totals["funnel"]
    print(
        "TOTALS "
        f"precision={precision if precision is not None else '-'} "
        f"recall={recall if recall is not None else '-'} "
        f"fp_rejection={totals['fp_rejection_rate'] if totals['fp_rejection_rate'] is not None else '-'} "
        f"fn_rate={totals['fn_rate'] if totals['fn_rate'] is not None else '-'} "
        f"cases={totals['cases']} pending={totals['pending']} invalid={totals['invalid']} "
        f"uncovered={totals['uncovered']} "
        f"compliance={totals['protocol_compliance_rate'] if totals['protocol_compliance_rate'] is not None else '-'} "
        f"high_conf_errors={totals['high_confidence_errors']}/{totals['high_confidence_cases']} "
        f"funnel_labelled={funnel['labelled']} ground_true={funnel['ground_true']} "
        f"ai_true={funnel['ai_true']}"
    )
    return 0 if not totals["pending"] and not totals["invalid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
