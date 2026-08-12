#!/usr/bin/env python3
"""Adjudication protocol: the stable contract between the benchmark, any
model adapter, and the human-review loop.

The benchmark tests this protocol, not a particular prompt or vendor.  A
model becomes an adapter: it must consume ``ADJUDICATION_CASE``-shaped
input and produce ``VERDICT``-shaped output; everything else (prompt
wording, temperature, provider) is interchangeable and must not change the
metric definitions.

Verdict fields:

- ``disposition``            true_finding | false_positive  (required)
- ``confidence``             0.0 .. 1.0                     (required)
- ``reason``                 short justification            (required)
- ``reason_codes``           controlled enum; the machine-readable
                             half of the reason
- ``recommended_action``     controlled enum; what the author should do
- ``reuse_target``           suggested existing symbol, else null
- ``required_verification``  gates that must pass before acceptance

Ground truth entries (corpus) add review metadata: ``human_verified``,
``reviewers``, ``ground_truth_version``.  Unverified entries are not used
for scoring unless explicitly included.
"""
from __future__ import annotations

from typing import Any

PROTOCOL_VERSION = 1
CASE_SCHEMA_VERSION = 1

DISPOSITIONS: set[str] = {"true_finding", "false_positive"}

REASON_CODES: set[str] = {
    "INTENTIONAL_DUPLICATION",
    "PUBLIC_API_SURFACE",
    "PLATFORM_ADAPTATION",
    "BOILERPLATE",
    "OVERRIDE_FAMILY",
    "CONTRACT_DRIFT",
    "ORPHANED_CODE",
    "DUPLICATED_OWNERSHIP",
    "UNEXTRACTED_SHARED_CAPABILITY",
    "UNNECESSARY_REIMPLEMENTATION",
    "HARDCODED_CONSTANT",
    "ENV_MISUSE",
    "UNSAFE_REFACTOR",
    "OTHER",
}

RECOMMENDED_ACTIONS: set[str] = {
    "none",
    "delete_dead_code",
    "extract_shared_component",
    "reuse_existing",
    "fix_contract_drift",
    "replace_with_library",
    "externalize_config",
    "investigate",
}

VERIFICATION_GATES: set[str] = {
    "unit_tests",
    "integration_tests",
    "type_check",
    "lint",
    "re_audit",
}

VERDICT_KEYS = (
    "disposition",
    "confidence",
    "reason",
    "reason_codes",
    "recommended_action",
    "reuse_target",
    "required_verification",
)

REQUIRED_VERDICT_KEYS = ("disposition", "confidence", "reason")

SCANNER_RUBRIC: dict[str, str] = {
    "duplicates": (
        "Duplicate implementations: duplication is INTENTIONAL when members are "
        "platform variants, public API mirrors, overload families, or tiny "
        "boilerplate. It is a TRUE FINDING when near-identical logic drifts "
        "apart, could safely share one implementation, or silently re-implements "
        "a canonical helper."
    ),
    "regions": (
        "Repeated code regions: statement blocks inside functions that recur with "
        "similar inputs, outputs, and API usage but have no named symbol. TRUE "
        "FINDING (UNEXTRACTED_SHARED_CAPABILITY) when the regions express the "
        "same capability under the same ownership and no canonical shared "
        "implementation exists, so a shared helper should be extracted. FALSE "
        "POSITIVE when the regions are generic loop/validation skeletons, "
        "boilerplate, already delegate to a shared core, or belong to separate "
        "ownerships that should not be merged."
    ),
    "forks": (
        "Forked functions: same-name or structurally identical functions that "
        "have drifted. TRUE FINDING when the drift looks like an unintended "
        "silent behavior change; FALSE POSITIVE when the divergence is "
        "deliberate (different accepted parameter shapes, separate ownership)."
    ),
    "contracts": (
        "Contract candidates: forwarding wrappers and unreferenced functions. "
        "A wrapper that merely delegates to a canonical implementation is a "
        "FALSE POSITIVE. TRUE FINDING when the wrapper's contract drifts from "
        "the target, when the code is genuinely orphaned, or when an env "
        "variable is written but never consumed."
    ),
    "deadcode": (
        "Dead code: TRUE FINDING when a module/function is unreferenced and "
        "not part of the public API or dynamic (duck-typed) usage; FALSE "
        "POSITIVE for public API surface, entry points, or runtime-registered "
        "callbacks (e.g. decorators, plugin hooks, argparse commands)."
    ),
    "capabilities": (
        "Capability overlap: local code re-implementing a standard/third-party "
        "capability. FALSE POSITIVE when vendoring is deliberate or the local "
        "signature differs; TRUE FINDING when an unnecessary reimplementation "
        "should be replaced by the canonical library call."
    ),
    "hardcoded": (
        "Hardcoded values: a path, URL, hash, or constant that should honor an "
        "environment variable or configuration is a TRUE FINDING; test "
        "fixtures and stable protocol constants are FALSE POSITIVES."
    ),
    "style": (
        "Style signals: judge whether the flagged prose/code style issue "
        "warrants an edit."
    ),
}

SYSTEM_PROMPT = (
    "You are an adjudicator for a deterministic static-audit toolkit used on "
    "large Python codebases maintained by AI assistants. For every candidate "
    "you receive evidence collected by a deterministic scanner plus code "
    "snippets.\n"
    "Decide:\n"
    '- "true_finding": the code should be changed (fix contract drift, delete '
    "orphaned code, extract a shared component, replace a hardcoded value, "
    "deduplicate drifted logic).\n"
    '- "false_positive": the evidence is explainable as intentional design '
    "(public API, deliberate duplication, platform adaptation, small "
    "boilerplate) and no code action is warranted.\n"
    "Base your decision ONLY on the provided evidence and snippets. Reply "
    'with exactly one JSON object and nothing else:\n'
    '{"disposition": "true_finding" or "false_positive", "confidence": 0.0-1.0, '
    '"reason": "<one short paragraph>"}'
)


def validate_verdict(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a verdict dict against the protocol; raise on violation."""
    missing = [key for key in REQUIRED_VERDICT_KEYS if key not in payload]
    if missing:
        raise ValueError(f"verdict missing required keys: {missing}")
    disposition = payload["disposition"]
    if disposition not in DISPOSITIONS:
        raise ValueError(f"verdict disposition must be one of {sorted(DISPOSITIONS)}")
    confidence = payload["confidence"]
    if not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
        raise ValueError(f"verdict confidence must be a number in [0, 1]: {confidence!r}")
    reason = payload["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("verdict reason must be a non-empty string")
    for code in payload.get("reason_codes", []):
        if code not in REASON_CODES:
            raise ValueError(f"unknown reason_code: {code!r}")
    action = payload.get("recommended_action")
    if action is not None and action not in RECOMMENDED_ACTIONS:
        raise ValueError(f"unknown recommended_action: {action!r}")
    reuse_target = payload.get("reuse_target")
    if reuse_target is not None and not isinstance(reuse_target, str):
        raise ValueError("reuse_target must be a string or null")
    for gate in payload.get("required_verification", []):
        if gate not in VERIFICATION_GATES:
            raise ValueError(f"unknown verification gate: {gate!r}")
    _validate_verdict_cross_fields(
        disposition,
        action,
        reuse_target,
        payload.get("required_verification", []),
    )
    return {
        "disposition": disposition,
        "confidence": float(confidence),
        "reason": reason,
        "reason_codes": sorted(payload.get("reason_codes", [])),
        "recommended_action": action,
        "reuse_target": reuse_target,
        "required_verification": sorted(payload.get("required_verification", [])),
    }


def _validate_verdict_cross_fields(
    disposition: str,
    action: str | None,
    reuse_target: str | None,
    required_verification: list[str],
) -> None:
    """Reject verdicts whose fields contradict each other semantically.

    - ``true_finding`` requires a concrete action and the deterministic
      ``re_audit`` gate (the executing agent can judge and modify, but only
      the deterministic re-audit accepts).
    - ``false_positive`` requires ``recommended_action == "none"``.
    - ``recommended_action == "reuse_existing"`` requires a ``reuse_target``.
    """
    if disposition == "true_finding":
        if action is None or action == "none":
            raise ValueError(
                "true_finding verdicts require a recommended_action other "
                "than 'none'"
            )
        if "re_audit" not in required_verification:
            raise ValueError(
                "true_finding verdicts require the 're_audit' verification gate"
            )
    elif action is not None and action != "none":
        raise ValueError(
            f"false_positive verdicts must set recommended_action 'none', "
            f"got {action!r}"
        )
    if action == "reuse_existing" and not reuse_target:
        raise ValueError(
            "recommended_action 'reuse_existing' requires a reuse_target"
        )


def validate_case(bundle: dict[str, Any]) -> dict[str, Any]:
    """Validate an adjudication case bundle; raise on violation."""
    required = (
        "case_schema_version",
        "project_id",
        "commit",
        "scanner",
        "target_id",
        "display",
        "evidence",
        "evidence_hash",
    )
    missing = [key for key in required if key not in bundle]
    if missing:
        raise ValueError(f"case missing required keys: {missing}")
    if bundle["case_schema_version"] != CASE_SCHEMA_VERSION:
        raise ValueError(
            f"case_schema_version must be {CASE_SCHEMA_VERSION}: "
            f"{bundle['case_schema_version']!r}"
        )
    if bundle["scanner"] not in {
        "deadcode",
        "duplicates",
        "regions",
        "forks",
        "contracts",
        "capabilities",
        "hardcoded",
        "style",
    }:
        raise ValueError(f"unknown scanner: {bundle['scanner']!r}")
    return bundle


def canonical_case(bundle: dict[str, Any]) -> dict[str, Any]:
    """Return the hashable rendering of a case: everything a verdict's
    validity depends on, minus the digest itself."""
    return {
        "case_schema_version": bundle["case_schema_version"],
        "project_id": bundle["project_id"],
        "commit": bundle["commit"],
        "scanner": bundle["scanner"],
        "target_id": bundle["target_id"],
        "display": bundle["display"],
        "evidence": bundle["evidence"],
        "snippets": bundle.get("snippets", []),
    }