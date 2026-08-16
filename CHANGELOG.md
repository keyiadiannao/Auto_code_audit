# Changelog

## 0.4.1

- Add the pre-write channel: `capability_retrieval --describe "<task>"`
  retrieves existing implementations from a natural-language task description
  (callable-name, docstring-lexical, and string-literal channels, IDF-weighted
  query coverage; deterministic, stdlib-only, zero LLM). This is the
  "check before you write" half of the reuse firewall — no new code needed.
- Support `--json -` to emit machine-readable results on stdout for MCP
  integration (previously file-only).

## 0.4.0

- Add the implementation-reuse firewall: a repository-wide, deterministic
  multi-channel retrieval (`capability_retrieval.py`) that surfaces the
  existing implementations a new symbol overlaps with (structural
  normalization, called-name overlap, string-literal overlap, one-hop call
  closure; stdlib-only, no LLM on the query path).
- Add the reimplementation benchmark (`benchmarks/reimplementation/`):
  hand-authored `(new, existing, verdict)` ground truth independent of scanner
  output, measuring Candidate Recall@K and MRR. Baseline Recall@10 0.000 →
  1.000.
- Add the reuse-check CLI: `python -m capability_retrieval --root --file`.
- Add `VISION.md` documenting the pivot and its design decisions.

## 0.3.1

- Make the acceptance gate fail-closed on incomplete analysis: `run_all`
  aggregates scanner parse failures into `report.analysis.complete`, and
  `run_verify` rejects a PASS when analysis is incomplete unless
  `--allow-incomplete-analysis` is passed.
- Write reports, verdicts, and suppression registries atomically (unique temp
  file + `os.replace`) so a crash cannot leave a torn evidence artifact.
- Validate `hardcoded.patterns` config entries (name/regex/severity/suggestion/
  exclude_paths) so a malformed pattern warns instead of raising inside the
  scanner.
- Fix `--scope` to path-prefix semantics (was substring, so `--scope lib`
  matched `liberal/...` and the README example `src/lib` matched nothing).
- Fix `scan_capabilities.tag_similarity` length gate (`sorted` without
  `key=len`), best-candidate tie-break, and `scan_contracts._return_contract`
  nested-scope traversal.
- Require a valid 64-char `finding_evidence_hash` in protocol verdicts; treat a
  corrupt `verdicts.json` as an error rather than "no verdicts".
- Add `scan_regions` to the CLI `--help` smoke gate; aggregate parse warnings
  across all scanners in the markdown report.

## 0.3.0

- Detect runtime-created modules and distinguish review-worthy dynamic loads
  from high-risk mutation of dynamically loaded module state.
- Include dynamic-runtime findings in the unified issue surface and Markdown
  contract report.
- Add exact start/end lines to duplicate-cluster JSON and Markdown members so
  findings can be handed directly to a repair agent.
- Reduce high-priority `helper_not_reused` noise by requiring a cross-file
  match, a configured shared-code owner, and a non-generic shared call anchor.
- Add `regions.shared_paths` configuration for repository-specific ownership
  layouts without embedding project knowledge in the scanner.

## 0.2.0

- Add fail-closed Git provenance: unavailable state is never reported clean.
- Add `--state-dir` and `--read-only` for immutable or third-party targets.
- Add conservative, label-independent issue fusion across corroborating
  duplicate and region clusters.
- Make code-focused scanning the default profile.
- Scan the selected package recursively by default and exclude common cache,
  environment, build, and audit-state directories.
- Bind provenance hashes to the same effective Python subdirectories used by
  scanners while keeping `--all-py` inside the selected package boundary.
- Replace bundled project suppressions and lessons with generic templates.
- Add agent metadata and a generic open-source skill protocol.
