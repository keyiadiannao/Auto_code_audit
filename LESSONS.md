# Maintainer Lessons

This file records scanner lessons that generalize across projects. Put target-
specific decisions, paths, symbols, and suppressions in that target's external
audit state or profile, not in this repository.

## 1. Candidates are not verdicts

Static reachability and similarity cannot establish intent. Inspect callers,
entry points, configuration, ownership, and tests before deleting or merging
code. A scanner must never edit code or suppression state automatically.

## 2. Unknown provenance is not a clean tree

If Git status or the commit cannot be read, record provenance as unavailable
and keep `dirty_count` unknown. Never turn a failed command into an empty clean
status. Content hashes can still bind the scanned tree, but external test
artifacts cannot claim full Git provenance.

## 3. Keep third-party audits read-only

Reports, verdicts, suppressions, and lessons are writable state. For an
immutable target, route all of them to an external state directory and reject
paths that resolve inside the target root.

## 4. Same name or syntax does not imply the same contract

Before consolidation, compare accepted inputs, outputs, errors, serialization,
side effects, configuration, compatibility, public API, and lifecycle. Hashing
and persistence helpers are especially sensitive to byte-level contracts.

## 5. Source identity can be a contract

Generated, vendored, frozen, compatibility, and independently audited code may
intentionally duplicate active code. Preserve such boundaries when their
identity is verified elsewhere; do not use a hygiene refactor to rewrite the
expected evidence.

## 6. Fuse signals before asking for review

When duplicate and region scanners report the exact same non-trivial symbol
set, present one issue with multiple evidence channels. Conservative fusion
reduces review work without claiming that corroboration proves a defect.

## 7. Parse failures make an audit incomplete

Accept UTF-8 with or without a BOM. Surface every remaining syntax or decoding
failure explicitly; never reinterpret an unreadable file as containing no
functions, imports, or entry points.

## 8. Re-audit is the acceptance authority

After a fix, require the finding to disappear, reject new high-risk candidates
in scope, bind the report to the live source and audit inputs, and require test
evidence. Agent confidence alone cannot mark a remediation complete.

## 5. External probes validate on unfamiliar code

Pinning benchmarks to known corpora can hide overfitting. Run the tool once
against an unrelated, mid-size pure-Python project and read the output without
prior expectations. Probe: arrow (arrow-py/arrow, pinned HEAD, read-only code
profile). Result: 1.08s scan, zero noise in the dead-code / hardcoded / style /
env-contract categories, and credible duplicate-implementation findings ¡ª
9 `_format_timeframe` locale methods at 0.84-1.00 edge similarity, 4
`describe` twins, `api.get` vs `ArrowFactory.get` near-duplicates, and
internal L14/L15/L16 repetition inside `DateTimeParser.parse`. All findings
map to real, independently recognizable code smells in that project. A
pre-write reuse firewall that surfaces these before the 10th locale method is
written is the value proposition this probe confirms.
