"""Shared per-case verdict file I/O and full protocol validation.

The SKILL Phase 2 protocol verdict files are written by the benchmark runner
and read back by its scoring loop; the engine-owned verify gate consumes the
same files as its Layer-3 acceptance input.  One module owns the file schema
and the complete validation pipeline so both consumers share one validator:
a verdict that ``validate_verdict`` (benchmarks/adjudication_protocol.py)
rejects can never reach the acceptance gate.

``load_protocol_verdict`` runs the full pipeline:

    JSON parse -> schema version check -> filename == evidence_hash ==
    case_hash binding -> bridge-field check (scanner/target_id) ->
    validate_verdict (including cross-field rules) -> normalized entry

``write_verdict_file`` / ``read_verdict_file`` are re-exported by
``benchmarks/run_adjudication.py`` for compatibility with the scoring loop.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from benchmarks.adjudication_protocol import validate_verdict

VERDICT_FILE_SCHEMA = 1


def write_verdict_file(
    verdicts_dir: Path,
    digest: str,
    verdict: dict[str, Any],
    case: dict[str, Any] | None = None,
) -> Path:
    """Write a per-case protocol verdict file.

    ``digest`` is the case hash (also the filename).  When ``case`` is given,
    the file additionally carries the bridge fields the engine-owned verify
    gate needs to match the verdict against report candidates: ``scanner``,
    ``target_id``, ``finding_evidence_hash`` (stale-evidence binding), and
    ``case_hash``.  These fields are additive, so pre-bridge files remain
    valid for the scoring loop.
    """
    payload: dict[str, Any] = {
        "schema_version": VERDICT_FILE_SCHEMA,
        "evidence_hash": digest,
        "adapter": "agent",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "verdict": verdict,
    }
    if case is not None:
        payload.update(
            {
                "case_hash": digest,
                "scanner": case["scanner"],
                "target_id": case["target_id"],
                "finding_evidence_hash": case["finding_evidence_hash"],
            }
        )
    path = verdicts_dir / f"{digest}.json"
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def read_verdict_file(path: Path, expected_evidence_hash: str) -> dict[str, Any] | None:
    """Read and validate a verdict file bound to ``expected_evidence_hash``.

    Enforces the triple binding: filename stem == payload evidence_hash == the
    case's current evidence hash.  Any mismatch raises ValueError (the scoring
    loop records it as an invalid verdict), so a verdict can never be reused
    against different evidence.  A schema-version mismatch returns None (the
    scoring loop treats it as "not a verdict file").  Returns the
    protocol-validated verdict dict.
    """
    if path.stem != expected_evidence_hash:
        raise ValueError(
            f"verdict filename {path.name!r} does not match expected evidence "
            f"hash {expected_evidence_hash!r}"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != VERDICT_FILE_SCHEMA:
        return None
    if payload.get("evidence_hash") != expected_evidence_hash:
        raise ValueError(
            f"verdict payload evidence_hash {payload.get('evidence_hash')!r} "
            f"does not match expected {expected_evidence_hash!r}"
        )
    return validate_verdict(payload.get("verdict", {}))


def load_protocol_verdict(path: Path) -> dict[str, Any]:
    """Load and fully validate a per-case verdict file for the verify gate.

    Runs the complete protocol pipeline — JSON parse, schema-version check,
    the filename -> evidence_hash -> case_hash binding, the bridge-field
    check, and ``validate_verdict`` with its cross-field rules — and raises
    ValueError with a clear message on any violation.  A non-compliant
    verdict therefore can never reach the acceptance gate.

    Returns a normalized entry shaped like the aggregated
    ``verdicts.json`` entries the gate already consumes:

    ``{scanner, target_id, finding_evidence_hash, disposition,
    recommended_action, case_hash}``.
    """
    digest = path.stem
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("verdict file must contain a JSON object")
    if payload.get("schema_version") != VERDICT_FILE_SCHEMA:
        raise ValueError(
            f"unsupported schema_version {payload.get('schema_version')!r} "
            f"(expected {VERDICT_FILE_SCHEMA})"
        )
    if not (
        isinstance(payload.get("scanner"), str)
        and isinstance(payload.get("target_id"), str)
    ):
        raise ValueError(
            "pre-bridge verdict file without scanner/target_id, skipped"
        )
    evidence_hash = payload.get("evidence_hash")
    if not isinstance(evidence_hash, str) or evidence_hash != digest:
        raise ValueError(
            f"payload evidence_hash {evidence_hash!r} does not match filename "
            f"stem {digest!r}"
        )
    case_hash = payload.get("case_hash")
    if case_hash is not None and case_hash != digest:
        raise ValueError(
            f"payload case_hash {case_hash!r} does not match filename stem "
            f"{digest!r}"
        )
    verdict_payload = payload.get("verdict")
    if not isinstance(verdict_payload, dict):
        raise ValueError("verdict file missing the 'verdict' object")
    validated = validate_verdict(verdict_payload)
    return {
        "scanner": payload["scanner"],
        "target_id": payload["target_id"],
        "finding_evidence_hash": payload.get("finding_evidence_hash"),
        "disposition": validated["disposition"],
        "recommended_action": validated["recommended_action"],
        "case_hash": case_hash or digest,
    }
