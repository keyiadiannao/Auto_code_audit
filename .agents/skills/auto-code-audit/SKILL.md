---
name: auto-code-audit
description: >
  Deterministic cross-file audit and adjudication protocol for AI-maintained
  codebases. Runs the scanner CLI (dead code, duplicate implementations,
  contract drift, capability overlap, hardcoded values), turns candidates into
  label-free protocol case bundles, adjudicates each candidate against evidence
  and project lessons, writes protocol verdicts, and enforces the deterministic
  re-audit gate before any fix is accepted. Also answers pre-change reuse
  questions ("have we already implemented this capability?"). Use when auditing
  a codebase after AI-driven changes, checking for duplicate implementations or
  drift, deciding whether to reuse an existing implementation or extract a
  shared component, or verifying that a fix actually removed a finding.
  Trigger words: 审计, self-audit, 查重复, 公共组件, 复用检查, adjudication,
  reuse check.
---

# Auto Code Audit

## Ground Rule

Deterministic evidence is truth. The executing agent has judgement and
modification rights but **no final acceptance right**: a finding is fixed
only when the deterministic re-audit says the target is gone, no new
high-risk candidate appeared, and tests pass. Never mark a finding as
remediated on the strength of your own belief that it is fixed.

All scanners are pure Python stdlib. Run them with the project's Python
interpreter (use the interpreter the repository itself uses; never hardcode
a machine-specific path).

## The Two Flows

```
              Global Code Index
         capability / duplicate / contract
                       │
        ┌──────────────┴───────────────┐
   Pre-change Advisor             Post-change Audit
   (写之前)                        (写之后)
   decompose needed capabilities   scan for anomalies
   reuse or new implementation?    ownership / contract drift?
   extract shared component?       duplicate implementation?
                       │
                       ▼
              AI adjudication (protocol verdicts)
                       ▼
         deterministic re-audit gate (accept/reject)
```

Both flows share the same index. A "true finding" is never "looks
similar"; it requires same capability + same ownership / contract /
evolution pressure.

## Phase 0 — Pre-change Advisor (write before)

When the user plans new code, decompose the task into required
capabilities, then for each:

1. Run the audit: `python run_all.py --root . --package <pkg> --profile code --no-doc-channel --json report.json --markdown report.md` (add `--all-py` for whole-repo scope).
2. Query the index for existing implementations: duplicate clusters and
   capability overlaps in `report.json` (sections `duplicates/clusters`,
   `capabilities/overlap`), same-name contract candidates
   (`contracts/same_name_contracts`), and latent implementations
   (`regions/clusters`): a `helper_not_reused` cluster is a named helper the
   region already duplicates inline — prefer `reuse`; a `shared_capability`
   cluster is repeated capability across blocks — consider
   `extract_shared_component`.
3. Answer per capability as structured JSON:
   `{"capability": ..., "existing_candidates": [{"symbol": "path::qualname", "confidence": ..., "callers": ...}], "recommendation": "reuse" | "extract_shared_component" | "new_implementation"}`.
4. Never recommend "extract shared component" from structural similarity
   alone: require same capability and same ownership/evolution pressure
   (see LESSONS.md for project-specific ownership boundaries).

## Phase 1 — Deterministic Audit (write after)

Run the full scan as in Phase 0. The report is the evidence layer; do not
edit or filter it. Every candidate is a "worth semantic review" item, not
a bug assertion.

## Phase 2 — Adjudication (protocol verdicts)

1. Prepare cases: `python benchmarks/run_adjudication.py --prepare-cases --results-dir benchmarks/results --labels-dir benchmarks/labels` (when benchmarking) or build bundles from `report.json` via `run_all._candidate_signatures` for a live project. Prepared cases are **label-free**: never inject the known label or ground truth into a bundle.
2. Read each case: evidence JSON + code snippets. When unsure, read the
   actual file at the reported location and its callers.
3. Consult `LESSONS.md` and `ignore.json` (existing suppressions) before
   judging.
4. Write the verdict to `adjudication/verdicts/<case_hash>.json`. The file
   carries the two hashes separately — `case_hash` (context binding: the
   canonical case digest, identical to the filename) and
   `finding_evidence_hash` (stale-evidence binding: the raw
   `{scanner, target_id, detail}` digest) — plus the bridge fields that let
   the engine-owned verify gate match the verdict to report candidates:
   ```json
   {"schema_version": 1, "case_hash": "<from case>", "evidence_hash": "<from case>", "scanner": "...", "target_id": "...", "finding_evidence_hash": "...", "adapter": "agent", "verdict": {"disposition": "true_finding|false_positive", "confidence": 0.0, "reason": "...", "reason_codes": ["..."], "recommended_action": "...", "reuse_target": null, "required_verification": ["unit_tests", "re_audit"]}}
   ```
5. A verdict that changes evidence (e.g. proposed refactor) does not
   retroactively change the bundle: the hash binds verdict to evidence.

### Verdict protocol (must validate)

| Field | Allowed values |
|---|---|
| `disposition` | `true_finding`, `false_positive` |
| `confidence` | number 0..1 |
| `reason` | non-empty string |
| `reason_codes` | `INTENTIONAL_DUPLICATION`, `PUBLIC_API_SURFACE`, `PLATFORM_ADAPTATION`, `BOILERPLATE`, `OVERRIDE_FAMILY`, `CONTRACT_DRIFT`, `ORPHANED_CODE`, `DUPLICATED_OWNERSHIP`, `UNEXTRACTED_SHARED_CAPABILITY`, `UNNECESSARY_REIMPLEMENTATION`, `HARDCODED_CONSTANT`, `ENV_MISUSE`, `UNSAFE_REFACTOR`, `OTHER` |
| `recommended_action` | `none`, `delete_dead_code`, `extract_shared_component`, `reuse_existing`, `fix_contract_drift`, `replace_with_library`, `externalize_config`, `investigate` |
| `reuse_target` | string or null |
| `required_verification` | subset of `unit_tests`, `integration_tests`, `type_check`, `lint`, `re_audit` |

`false_positive` dispositions MUST carry a reason and SHOULD carry a
`reason_code`; this becomes a suppression entry (or LESSONS entry for
recurring patterns).

## Phase 3 — Acceptance Gate (never self-approved)

After the user (or you) applies a patch for an accepted true finding:

1. Tests: run the project's test suite; all must pass. The gate makes the
   test evidence machine-checkable: pass `--test-command <cmd>` to execute
   the suite inside the gate (non-zero exit rejects), or `--no-tests` to
   declare that tests are verified outside it. A code-action verdict without
   either flag is rejected — the gate never self-approves.
2. Re-audit: re-run the full scan on the same scope.
3. Run the engine-owned gate (replaces the manual checklist). `--verdicts`
   accepts either the aggregated `verdicts.json` or the per-case verdict
   directory `adjudication/verdicts/` from Phase 2:

   ```text
   python run_verify.py --report <post-fix report.json> \
     --verdicts <verdicts.json | verdicts dir> --previous <pre-fix report.json> \
     --scope <path substring the patch touched> --test-command "pytest -q"
   ```

   It fails when a code-action verdict's `target_id` still appears in the
   new report, when a still-present finding's `finding_evidence_hash`
   recomputes unchanged, when the patch scope gained a high/medium candidate
   that was absent before the patch, or when the test gate rejects. It
   speaks both the protocol vocabulary (`true_finding` +
   `recommended_action`) and the legacy `adjudicate.py` dispositions. Exit
   code 0 = acceptance.
4. Only then mark the finding remediated and, if the outcome is a lasting
   project convention, append one line to `LESSONS.md`.

## Rules

- Do not delete or auto-edit code from a candidate alone; adjudication
  always precedes modification.
- Do not merge scanner candidates into the ground-truth corpus without a
  human review marker (`human_verified: true`, reviewer, version).
- On disagreement between your judgement and an existing suppression,
  prefer the deterministic evidence and flag the disagreement instead of
  silently overriding.
- Audit across packages/files: pass `--all-py` unless the user scoped the
  package explicitly; drift findings are usually cross-file.
- Output is evidence + verdict JSON; prose summaries are navigation only.
