---
name: self-audit
description: >
  Audit a large Python project after AI-assisted refactors or cleanup. Generate
  candidate lists for dead modules, duplicate implementations, hard-coded
  behavior drift, and AI writing-style signals in TeX prose; perform semantic
  triage; then run package and provenance gates. Use for requests such as
  self-audit, dead-code review, duplicate-code
  review, refactor verification, or a pre-commit engineering hygiene pass.
---

# Self-Audit

Use three layers. Never promote a static hit directly into a deletion or
refactor. Similarity is only candidate generation; the unit of adjudication is
the caller's functional contract.

1. Generate candidates with deterministic scanners.
2. Review every candidate against its call sites and scientific role.
3. Verify accepted edits with tests, submission gates, and evidence checks.

Read `LESSONS.md` before Layer 2. It records project-specific false-positive
patterns. Treat `ignore.json` as an approved suppression registry, not a list
that scanners update automatically.

An optional `<root>/audit.config.json` is validated before scanner execution.
Missing configuration is normal; invalid JSON or semantic values emit one
warning per path and fall back to compiled-in defaults.

## Run Layer 1

From the repository root (or with `--root <repo>`):

```text
<python> run_all.py --package src
```

The default outputs are:

```text
reports/latest.json
reports/latest.md
```

Useful options:

- Add `--no-doc-channel` for a faster code-only dead-module pass.
- Use `--profile code` for software-only audits or `--profile research` (the
  default) to include TeX writing-style signals.
- Adjust duplicate sensitivity with `--duplicate-threshold` and
  `--duplicate-min-chars`.
- Run any scanner directly for a scoped investigation.

Small fixtures finish in seconds. A repository-wide pass can take longer,
especially with document references enabled. The report records elapsed time,
Git HEAD, dirty package files, configuration, and scanner hashes.

## Interpret the scanners

| scanner | candidate signal | common false positive |
|---|---|---|
| `scan_deadcode.py` | no visible import or documentation reference | dynamic dispatch, manually invoked scientific runner, provenance-only tool |
| `scan_duplicates.py` | structurally similar function component | symmetric experiment arms, intentionally separate intervention boundaries |
| `scan_forks.py` | cross-file callables sharing a large common skeleton with diverged bodies (>= 40 lines, >= 75% token similarity) | deliberate specialization forks with distinct contracts (extra parameter, group-specific output keys), same-file symmetric helpers |
| `scan_contracts.py` | experiment modules used as libraries, forwarding wrappers, repeated contract-sensitive names, and unreferenced top-level functions | a valuable adapter, dynamic entrypoint, or intentionally independent audit implementation |
| `scan_hardcoded.py` | syntax known to drift from shared behavior | a distinct hash contract or an intentional frozen-forward implementation |
| `scan_style.py` | AI-typical writing signals in TeX prose (semicolon chains, template openers, em-dash rate, burstiness, excess vocabulary, bare `\pm`) | technical enumeration (numbers or math in most subclauses), section-map lists, statistics-context "robust/significant", the paper's own `{\pm}` convention |

`scan_deadcode.py` marks a script with a `__main__` guard as `ENTRYPOINT` and
package initializers as `PACKAGE`, not `DEAD`. UTF-8 and UTF-8-with-BOM source
are accepted; any remaining syntax failure becomes an explicit `PARSE-ERROR`.
Its dependency graph covers three channels: static imports, bare imports
under a `sys.path`-pinned package subdirectory, and importlib file loads
(`spec_from_file_location` / `SourceFileLoader` / `import_module` /
`__import__`, including thin `_load(name, path)` wrappers resolved from their
call-site literals). Dynamic edges are reported per
`(source, mechanism, target, lineno)` and merged into the `USED` verdicts, so
a module loaded only through those channels is never marked dead.
`scan_duplicates.py` compares blocked candidate pairs and emits a
stable cluster ID. `scan_forks.py` indexes every package callable (lib and
scripts alike) with its docstring tag and reports cross-file pairs sharing
>= 75% of their normalized body above a size floor (default 40 lines); each
pair carries signature-shape match, longest common run, divergence regions,
and the identifiers unique to each side. `scan_hardcoded.py` uses
pattern-specific exclusions so it can audit `lib/` without reporting
canonical implementations as their own duplicates. `scan_style.py` strips
TeX to prose span-preservingly (comments, environments, headings, math, and
citation commands become same-span whitespace, so reported line numbers
match the source exactly); it scans `--tex-dir` (default `docs`) recursively
for `*.tex` files, skipping archive/frozen/legacy directories, or the explicit
`--tex-files` list; its `em_dash_rate`/`burstiness` hits are file-scoped and
carry no `line` field.

## Perform Layer 2

After Layer 1, run the resumable adjudicator against the report written for
the audited project:

```text
<python> adjudicate.py --report <root>/reports/latest.json
```

CI can enforce a completed Layer-2 session without opening an interactive
prompt:

```text
<python> adjudicate.py --report <root>/reports/latest.json --check
```

When the report contains state metadata, the adjudicator resolves the project
`ignore.json`, `LESSONS.md`, and `reports/verdicts.json` from that ownership
record. Explicit `--root`, `--ignore`, `--lessons`, and `--verdicts` paths take
precedence. This prevents a toolkit checkout from receiving state belonging to
the project under audit.

Verdicts carry a target identity and an evidence fingerprint. A changed
candidate is stale even when its path/symbol identity is unchanged. False
positive verdicts should carry the suppression payload so `--export-ignore`
can rebuild a registry without depending on a mutable latest report.
Verdict files bind two hashes: `case_hash` (the context digest, identical
to the filename) and `finding_evidence_hash` (the raw
`{scanner, target_id, detail}` digest that makes a changed candidate stale).

Before assigning a verdict, write a contract card for each caller or caller
family. It must state:

- scientific role and evidence artifact,
- accepted inputs, tensor shapes, indexing convention, dtype, and device owner,
- outputs and any required intermediate tensors,
- randomness, checkpoint loading, and provenance behavior,
- the existing canonical implementation, if any,
- the exact semantic delta that prevents direct reuse,
- the parity or evidence gate needed before a change.

Do not accept "historical compatibility" or "fewer changed lines" by itself as
a reason to retain an active wrapper. Frozen source may preserve a historical
API; active callers should migrate unless the wrapper owns a real interface,
validation, policy, lifecycle, or evidence-boundary responsibility.

Assign one disposition to every duplicate or contract candidate:

| disposition | action | required evidence |
|---|---|---|
| necessary specialization | retain locally | concrete mathematical, intervention, output, or provenance contract not covered by the shared API |
| valuable adapter | retain and name by its role | interface conversion, validation, policy, or lifecycle behavior beyond forwarding |
| independent audit | retain separately and parity-test | independence is itself part of the audit design |
| compatibility debt | migrate active callers, then remove/deprecate | same contract; wrapper exists only to avoid updating old callers |
| true duplicate | consolidate | same contract and no independence/provenance reason |
| false positive | suppress only after review | why the scanner cannot distinguish this case |

Thinness is not the decision rule. A two-line adapter can be valuable; a
hundred-line copy can be compatibility debt. Review what the caller needs and
what the callee guarantees.

An active source hash is not a permanent design exemption. Record whether the
lock protects historical evidence, a training dependency, or an independent
audit. If the implementation is otherwise redundant, preserve its exact bytes
under frozen provenance and migrate the active API at the next evidence rebuild
rather than letting the lock become indefinite compatibility debt.

Before deleting or consolidating a file in the audited package, inspect:

- the package manifest (claim → runner → artifact map, if one exists)
- the package verifier / orchestrator entry points
- the relevant experiment record and paper/report pointer
- frozen result/source hashes when the file participates in locked evidence

Do not merge hashing helpers merely because both call SHA-256. File hashes,
canonical JSON hashes, NumPy-array hashes, and protocol fingerprints can have
different serialization contracts.

Do not suppress an entire duplicate cluster because one member is intentional.
Prefer stable candidate or cluster IDs. Add a legacy path suppression only
when every member covered by it has the same reviewed rationale.

Record a false-positive rationale in `LESSONS.md` before editing
`ignore.json`. Never let a scanner mutate either file.

## Run Layer 3

After edits:

1. Run this skill's fixture tests:

   ```text
   <python> -m pytest tests -q
   ```

2. Run the target package tests and record the observed count rather than
   copying a historical count:

   ```text
   <python> -m pytest <package>/tests -q
   ```

3. Run the package verifier when the changed files participate in locked
   evidence:

   ```text
   <python> <package>/verify/verify_submission.py --quick
   ```

   (Replace `verify_submission.py` with whatever verifier the package owns, or
   skip this step when none exists.)

4. Recompute any affected frozen-result summary independently. Run parity
   tests for shared forward/readout changes and finish with `git diff --check`.

5. Run the provenance gate against the post-fix report when the audit has
   verdicts, a pre-patch report, and machine-readable test evidence:

   ```text
   <python> run_verify.py --root <root> \
     --report <root>/reports/latest.json \
     --verdicts <root>/reports/verdicts.json \
     --previous <pre-patch-report>.json \
     --test-result <ci-result>.json
   ```

   `--root` must point at the tree the audit ran against (the same value
   `run_all.py --root` received, or the current directory when the audit
   ran from the project root): the gate fingerprints the live source tree
   and the full audit-input manifest (`source_tree_sha256` and
   `audit_inputs_sha256`, which also covers the document channel and TeX)
   and requires them to equal the report's recorded values.

   The gate is machine-checked, not advisory. Remediation is complete only
   when it prints `fully_verified=True`: the recorded test run passed and ran
   against the exact audited source tree (`source_tree_sha256` matches the
   live tree), the test artifact is bound to the report's git head, and a
   comparable pre-patch report (same `audit_config_hash`, `scanner_bundle_hash`,
   profile, and package) proves the patch introduced no new candidate. A
   `VERIFY FAIL` means the patch or its evidence is not done — return to
   Layer 2.

If a gate fails, return to semantic review. A clean static report cannot
override a failed behavior or provenance gate, and a passing test suite
cannot override a provenance mismatch: verification without a comparable
pre-patch report or a bound test artifact never reaches `fully_verified`.

## Scope

Use this skill for engineering hygiene and refactor safety. Use the project
paper-audit workflow for scientific claim validity, paper/reproduction
agreement, figure quality, or evidence-tier promotion.
