# Self-Audit False-Positive Lessons (LESSONS)

> Every false positive (or semi-false positive) is a correction to the scanner
> methodology. Read this file before Layer 2 so the same class of case is not
> adjudicated twice; new false-positive adjudications must be appended here.
> Machine suppression lives in `ignore.json`; this file records the semantic
> lessons behind each scanner channel. Use the two together.

## 1. Dead code: static reference scanning cannot distinguish "not wired" from "truly dead"

- **Case**: a diagnostic script was flagged DEAD even though it backs a live
  evidence claim, because the code referencing it lives in a verifier layer and
  reaches it through string/configuration concatenation that the static graph
  cannot see.
- **Lesson**: DEAD/DOC-ONLY is a candidate label, not a verdict. **Semantically
  review any module before deletion**: consult the package manifest, the
  verifier / orchestrator audit links, and the paper/report pointers. Modules
  that are deliberately unwired (historical diagnostics, reserve verifiers)
  look identical in the static graph to modules that should be wired but
  missed.
- **Implementation**: the "intentional design" and "real problem" verdicts both
  require the reviewer to cite the evidence location (manifest line / verifier
  chain / report anchor). Only a DEAD verdict that cannot be pinned to any
  evidence chain is truly dead.

## 2. Snapshot trap: scanning while a concurrent agent is editing disk captures a transient state

- **Case**: a duplicate scan run while a parallel agent was mid-migration
  reported known duplicate bodies as missing; the "scanner bug" was actually a
  half-migrated working tree.
- **Lesson**: **run `git status` before scanning**. If the target package has
  uncommitted changes, the report must say "disk had N uncommitted files at
  scan time (half-migration snapshot)", and duplicate findings must be
  recomputed against `git diff`. Do not scan the same package concurrently
  with another writer.
- **Implementation**: the run report header records the uncommitted-file count;
  a nonzero count adds a snapshot warning banner.

## 3. Hardcoded drift: a default configuration can mask an invariant difference

- **Case**: an inline replication of a shared function was bit-identical under
  the default configuration (parity self-test passed) but drifted measurably
  under a different configuration of the same order parameter.
- **Lesson**: **default-config parity does not prove the two implementations
  are semantically equal**. Once a shared implementation owns an order
  parameter (readout order, norm order, projection basis), any inline copy is
  a potential drift point. A scanner can only find "inline code matching a
  known pattern"; it cannot judge whether that inline is equivalent at a given
  call site — that requires Layer 2 to inspect each call site's configuration.
- **Implementation**: hardcoded suggestions always read "use the shared
  implementation"; the reviewer decides per hit whether the call site is
  equivalent under its configuration (keep or unify) or drifting (must unify).

## 4. AI self-reports must be re-vetted against disk, not trusted at face value

- **Case**: two agent reports disagreed with disk — one cited stale line
  numbers, one missed a page regression. Even when counts matched, semantic
  conclusions needed re-verification.
- **Lesson**: every agent report (including this skill's own) goes through
  "report → disk → rerun" verification. In particular, existence assertions
  (a shared implementation exists / a function was deleted) must be confirmed
  by grep or file read, not by another report.
- **Implementation**: in Layer 2, every "real problem" verdict carries
  `path:line` evidence; every "delete/unify" verdict re-reads the target file
  before acting.

## 5. A matching total does not mean item-by-item agreement

- **Case**: an agent reported "22 inline copies", the number was right, but the
  composition was 14 in one module and 8 elsewhere — two completely different
  fix paths.
- **Lesson**: when re-verifying a count, always demand the categorized detail,
  never just the total. Every scanner emits item-level hits (path:line), and
  summaries must not collapse them to totals.

## 6. CLI entry points are not dead code

- **Case**: experiment runners started directly by a shell or a scheduler are
  never imported by another Python module; a pure import graph bulk-flags them
  DEAD.
- **Lesson**: files with `if __name__ == "__main__"` are marked `ENTRYPOINT`
  first. That does not prove they belong to the evidence chain, but it defers
  "still needed?" to manifest and provenance review instead of the dead-code
  heuristic.

## 7. Same algorithm name does not mean the same implementation contract

- **Case**: two modules both call SHA-256, but one holds file/plain-JSON
  helpers while the other owns the canonical serialization of a fingerprint,
  Path and NumPy handling, and atomic writes.
- **Lesson**: duplicate scanning only finds surface-similar implementations.
  Before merging, compare input domains, byte serialization, error behavior,
  and persistence semantics. DRY by itself is not a reason to change a
  provenance contract.

## 8. UTF-8 BOM breaks string-level AST parsing

- **Case**: source files with UTF-8 BOM execute fine but raise U+FEFF when
  read as `utf-8` strings and passed to `ast.parse`; the first scanner version
  mislabeled CLI scripts and silently missed whole files.
- **Lesson**: read Python sources as `utf-8-sig`; a parse failure must become an
  explicit `PARSE-ERROR`, never degrade to "no functions / no entrypoint". A
  report containing any parse failure is not a complete static audit.

## 9. Behavior equivalence cannot cross a source-compatibility lock

- **Case**: a hand-written hash implementation could be behavior-equivalently
  delegated to a shared helper and package tests passed — but a frozen
  evidence dependency hashes that exact source file, so the verifier correctly
  rejected the refactor.
- **Lesson**: for evidence-locked source files, source identity is itself the
  contract. Do not make hygiene refactors pass by updating expected hashes
  unless the scientific evidence is formally rebuilt; restore the source file
  and record the intentional duplicate with an exact `(path, pattern)`
  suppression.

## 10. Similar preregistration loaders can carry different lock semantics

- **Case**: a family of preregistration loaders all share the
  "read expected hash → verify file → read JSON → check status" shape, but
  differ in concrete schema, amendment, status fields, and failure conditions.
- **Lesson**: similar control flow is not a reason to extract a semantics-free
  generic loader. Merge only when the shared API preserves each experiment's
  lock conditions as explicit parameters or callbacks; otherwise local
  duplication is easier to audit.

## 11. TeX prose stripping: position, coordinate systems, and leading whitespace are three separate pitfalls

- **Case** (three real bugs found while integrating the TeX scanner):
  1. **Isolated `$` swallowing prose**: a title with nested braces containing
     a `\texorpdfstring{$...$}{...}` made the brace-based title regex stop
     early, blanking half the title and leaving an orphan `$`; the parity of
     `$` pairs then shifted and a paragraph of prose was consumed as math.
     Fix: brace-balanced title stripping plus a residual-`$` safety net.
  2. **Dual coordinate systems (marked vs prose)**: with `mark_math=True`,
     every math span appends a ` [MATH] ` tag (+8 chars); sentence-level
     metrics use the tagged text while regex metrics use the tag-collapsed
     text. Positions from the tagged text must not be counted into line
     numbers against the collapsed text. Fix: `_line_no(text, base, pos)`
     must receive the same text space as `pos`.
  3. **Leading newline loss**: a trailing `.strip()` on the stripped body
     removed leading whitespace, systematically shifting line numbers low.
     Fix: return `(text, lead_nl, tail_nl)` and add `lead_nl` to the base.
- **New finding (test-caught)**: after slicing a document body, a trailing
  `\end{document}` without its `\begin` escapes the environment regex and one
  stray "document" word pollutes word counts. Fix: explicitly replace
  `\end{document}` with a space after slicing.
- **Design behavior (not a bug)**: enumerated lists such as abstract
  contribution lists are excluded wholesale from prose statistics — list
  content is not narrative prose.
- **Implementation**: line-number mapping is
  `text.count("\n", 0, pos) + base_newlines + 1`; before reporting any line
  number, validate the whole chain against one known anchor.
- **File-level metrics**: `em_dash_rate`/`burstiness` are file-level aggregates;
  hits carry no `line` field, and the report renderer must use
  `item.get("line", "-")`.

## 12. Cross-group symmetric arms: explicit import of the parent is strong evidence that "similarity = protocol"

- **Case** (first fork-scan round): every stable pair adjudicated as a
  deliberate fork, zero true duplicates. The decisive evidence: the shorter
  file already explicitly reuses the parent — one loads the parent with
  importlib and calls all of its functions (only the data-panel loading and a
  constant patch differ); others `import parent` and reuse its validation; a
  profile family all `import parent` plus the shared profile library. The
  remaining similarity is the protocol skeleton itself. Exception: a pair
  that does not import each other can be two independent mathematical audit
  arms of the same question, each with its own manifest record and output
  keys.
- **Lesson**: **check the import graph first when adjudicating fork pairs**.
  If one side already imports/importlibs the parent, skeleton similarity
  means symmetric arm — ask only whether the parent has more shareable
  layers (optional improvement, not required). Only when two siblings never
  reference each other must you compare mathematical objects and output
  contracts function by function.
- **Implementation**: deliberate fork pairs are recorded with sorted
  `path:qualname` keys in `ignore.json` under `forks`; pairs in an active
  refactor zone are re-verified after the refactor lands before entering the
  registry.

## 13. The dependency graph is three-channel: static imports are only one

- **Case**: the dead-code scanner only recognized regular package imports and
  was blind to `sys.path.insert(...) + import <basename>` and to
  `spec_from_file_location("nickname", path)` file loads. Dependency counts
  before the fix were semantically wrong, not merely imprecise — they cannot
  serve as a "package is clean" metric.
- **Lesson**: three legal dependency channels exist inside a package —
  ① static `ast.Import`/`ImportFrom`; ② bare imports after a `sys.path`-pinned
  subdirectory (module name is the basename, so package-qualified matching
  misses it; use basename fallback scoped to the pinned subdirectory to avoid
  false positives); ③ importlib file loads
  (`spec_from_file_location`/`SourceFileLoader`/`import_module`/`__import__`),
  where the spec `name` is only a nickname and the path is the identity, and
  thin wrappers (`_load(name, path)`) hide the argument at the spec call site
  so the literal must be resolved from the wrapper's call sites. USED/DEAD
  verdicts must not be issued until all channels are merged; each report
  should emit per-edge detail (source/mechanism/target/lineno), not only
  counts.
- **Implementation**: the dead-code scanner merges all three channels and
  reports each dynamic edge; a complementary contract channel flags `sys.path`
  modification itself. When the path slot and the nickname slot resolve to the
  same target, report a single edge (path slot preferred).

## 14. Four audit blind spots and their contract channels

- **Case**: an external audit reported high-risk findings that static scans
  had all missed. The root cause was not scanner precision but blind spots —
  four problem classes falling outside AST fingerprints. Analyzed class by
  class, 3.75/4 can be automated; the remaining 0.25 (exit-code semantics)
  stays manual.
- **Blind spot A (runtime behavior)**: static scans never execute code, so
  two cross-process contracts are invisible — ① the **env handoff contract**
  (an orchestrator sets environment variables for every script, but one
  script hardcodes an archive root without reading the env, misplacing its
  output generation under orchestration); ② **exit-code semantics** (a
  script missing its checkpoint still exits 0, so a verification chain may
  treat "did not run" as "ran"). Automation: the `generation_path_without_env`
  channel (AST-detects generation-root constants without an env read) — its
  first run caught a real bug: a hash-locked runner failed at import because
  a hash-helper rename removed the old name, and the verify chain stayed
  green because its snapshot gate hashes files but does not verify
  runnability. The fix was a backward-compatible alias that keeps the locked
  runner's bytes unchanged. **The exit-code half cannot live in AST; it stays
  manual plus this record.**
- **Blind spot B (defensive strength)**: `strict=False` / `weights_only=False`
  can be correct use or a performance shortcut; a static fingerprint cannot
  tell. The `defensive_param_loosening` channel (torch.load/strict parameter
  loosening) first-round found every hit deliberate: wrapped checkpoints
  containing non-tensor metadata entries require `weights_only=False`.
- **Blind spot C (thresholds)**: a fork scan's line floor missed small
  functions. A `small` channel (function level) returned pairs all
  adjudicated deliberate: compose families (each arm carries its own
  intervention bundle from its own parameter source), prereg loader families
  (per-file schema/hash contracts), byte-identical thin aggregation helpers
  (waiting on a consolidation refactor), and single-point helper idioms.
  After registration, the small channel reports zero residue.
- **Blind spot D (inverse detection)**: old scans reported "has sys.path
  hack" but not "should have a bootstrap and does not". The
  `cli_without_bootstrap` channel (CLI scripts importing package modules
  without a bootstrap) first-round found one = a hash-locked runner that
  cannot gain a bootstrap line without invalidating its locked hash; its
  standard entry path is the orchestrator's package context.
- **Implementation**: the four channels all live in scan_contracts
  (schema v4), wired into run_all's summary, with new `contracts` sections in
  `ignore.json` and matching fixture tests (ignore suppression across all four
  channels, docstring mention not triggering a generation constant,
  schema-v4 shape). A docstring filter removes noise from documentation-only
  matches. **Boundary**: compose/agg-family adjudications in an active
  refactor zone must be re-verified after the refactor lands before they are
  treated as final.

## 15. Region clusters: helper inlining is a deliberate fork family, not a defect

- **Case**: the region scanner's first real-project run (reproduce_submit)
  reported 43 high clusters — 132 helper_not_reused + 64 shared_capability
  total, 43 flagged high. Adjudication found **zero computation-level bugs**
  and zero clusters needing code changes.
- **What the clusters actually are**:
  - `y_and_y_swap` (mechanism/_ring_utils.py) inlined at 30 sites with
    cov=1.00 — a trivial tuple-swap idiom; inlining is the standalone-script
    design, not drift.
  - `_validate_model_tensor` (n=15), `_make_xor_data_v1` (n=12),
    `score_provider_from_gauge` (n=5), `_print_table` (n=7),
    `_resolve_ckpt_root` (n=4) — short validation/construction/printing
    blocks repeated across frozen experiment scripts. Refactoring them would
    invalidate frozen evidence fingerprints; risk exceeds benefit.
  - CLI boilerplate clusters (ArgumentParser idiom), experiment twin
    families (e51/e52/e53, e70/e72/e73), audit-script families, test
    mirrors — all deliberate per-script self-containment, consistent with
    the 87 fork pairs already adjudicated in section 14's small channel.
- **The one heuristic false positive**: `3469d0221c30` fired the
  `asymmetric_indexing` signal on `scores_from_prepared_tables` vs
  `_forward_intervened` (intervention_forward.py / qk_blockmask.py). Manual
  index-by-index review proved both blocks equivalent: score tables are
  structurally cross-indexed (S_ij = query index x key index), so "a in dim 0,
  b in dim 1" is a legal invariant, not an asymmetry bug.
- **Resolution**: 43 entries registered in `ignore.json` under `regions`
  (id + reason + date + owner). Rerun: 196 clusters → 153, ignored=43,
  high=0. Future drift is still caught by the previous-run delta report: a
  changed inline copy lowers its coverage and the cluster reappears in the
  delta even though the stale ID is suppressed.
