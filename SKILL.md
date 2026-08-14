---
name: auto-code-audit
description: >
  Deterministic audit and adjudication workflow for AI-maintained Python
  repositories. Use after AI-assisted changes, before adding a capability,
  when checking dead code, duplicate implementations, contract drift, shared
  component opportunities, hardcoded behavior, or when verifying that a fix
  removed a finding without introducing new risk. Trigger on code audit,
  self-audit, reuse check, duplicate review, adjudication, 审计, 查重复,
  公共组件, or 复用检查.
---

# Auto Code Audit

## Ground rule

Treat deterministic scanner output as evidence, not as a defect verdict.
Accept a remediation only after tests and a deterministic re-audit pass. The
executing agent may investigate and edit code, but it may not self-approve a
fix from intuition alone.

Use the target project's Python interpreter. The scanners require only the
Python standard library.

## Choose the flow

- Before writing code, search for an existing capability and decide whether
  to reuse, extract a shared component, or add a new implementation.
- After changing code, scan for dead code, duplication, ownership drift,
  contract drift, and hardcoded behavior; adjudicate; then verify fixes.

A reuse opportunity requires the same capability, compatible contracts, and
the same ownership or evolution pressure. Similarity alone is insufficient.

## Generate evidence

Run a code-focused audit:

```text
python run_all.py --root <repo> --package <package> \
  --profile code --no-doc-channel --json <state>/latest.json \
  --markdown <state>/latest.md
```

Add `--all-py` for a flat or whole-repository Python scope. Use
`--profile research` only when the optional document/style channel is wanted.

For a third-party or immutable target, keep every writable artifact outside
it:

```text
python run_all.py --root <repo> --package <package> --profile code \
  --state-dir <external-state> --read-only
```

`--read-only` must reject state paths inside `--root`. Do not copy private
source, paths, or project-specific findings into this skill or its repository.
Keep them in external audit state.

Read report layers in order:

1. `issues`: exact member-set bundles combining corroborating scanners.
2. high-value near-exact duplicates, function twins, and dynamic-module state
   mutation.
3. medium-value shared-capability regions.
4. exhaustive low-value candidates only when required.

Every candidate still requires semantic review.

## Advise before implementation

Decompose the change into capabilities. Inspect duplicate clusters,
capability overlaps, same-name contracts, region clusters, and callers. Return:

```json
{"capability":"...","existing_candidates":[{"symbol":"path::qualname","confidence":0.0,"callers":[]}],"recommendation":"reuse|extract_shared_component|new_implementation"}
```

For a cross-name reuse check (different names/docstrings, same
responsibility), run the reuse retrieval before recommending reuse:

```text
python -m capability_retrieval --root <repo> --base HEAD --json <state>/reuse.json
```

It surfaces existing implementations a new/changed callable overlaps with,
ranked by structural/call/string evidence — read the top candidates, then
decide `reuse` / `extract_shared_component` / `new_implementation` against
real code, never against the score alone.

Recommend extraction only when callers can share one explicit contract and
ownership boundary.

## Adjudicate after scanning

Inspect each issue's evidence, source, callers, tests, configuration, and the
project-local `LESSONS.md` or `ignore.json`. Project knowledge belongs to the
target's audit state or a user-supplied profile, never to the generic skill.

Write one label-free protocol verdict per case. Keep context-binding
`case_hash` separate from stale-evidence `finding_evidence_hash`:

```json
{"schema_version":1,"case_hash":"...","evidence_hash":"...","scanner":"...","target_id":"...","finding_evidence_hash":"...","adapter":"agent","verdict":{"disposition":"true_finding|false_positive","confidence":0.0,"reason":"...","reason_codes":["OTHER"],"recommended_action":"none|delete_dead_code|extract_shared_component|reuse_existing|fix_contract_drift|replace_with_library|externalize_config|investigate","reuse_target":null,"required_verification":["unit_tests","re_audit"]}}
```

Use concrete evidence in `reason`. Do not suppress a cluster because one
member is intentional. Compare contracts before merging serialization,
persistence, platform, generated, frozen, compatibility, or public-API code.

Never let a scanner mutate `ignore.json` or `LESSONS.md`. Add a suppression
only after a reviewed false-positive verdict, with a reason and owner.

## Verify a remediation

After an accepted code action:

1. Run the target's unit and integration tests.
2. Re-run the same audit scope and configuration.
3. Run the engine-owned gate:

```text
python run_verify.py --root <repo> --report <post.json> \
  --verdicts <verdicts.json-or-dir> --previous <pre.json> \
  --scope <changed-path> --test-command "pytest -q"
```

Bind the live tree to `source_tree_sha256`, full inputs to
`audit_inputs_sha256`, semantic configuration to `audit_config_hash`, and the
engine to `scanner_bundle_hash`. Report unavailable Git provenance as unknown,
never clean. `--no-tests` may declare external delegation but cannot fully
verify. Mark remediation complete only when the gate prints
`fully_verified=True`.

## Safety rules

- Do not delete or refactor from a candidate alone.
- Do not inject labels or known truth into adjudication cases.
- Do not add candidates to a benchmark corpus without human verification.
- Re-read changed evidence instead of trusting agent summaries.
- Separate the generic engine, optional rules, and project-local knowledge.
- Treat JSON evidence and protocol verdicts as authoritative; prose is only
  navigation.
