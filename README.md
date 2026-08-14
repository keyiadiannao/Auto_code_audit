[English](README.md) | [简体中文](README.zh-CN.md)

# Auto Code Audit

[![CI](https://github.com/keyiadiannao/Auto_code_audit/actions/workflows/ci.yml/badge.svg)](https://github.com/keyiadiannao/Auto_code_audit/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org)

> **Direction (2026):** pivoting from a general static auditor into an
> **implementation-reuse firewall** for AI coding — surface the existing code a
> new helper / service / manager overlaps with, *before* it is written. See
> [VISION.md](VISION.md); the audit scanners below remain as evidence providers.

Your AI assistant refactored a large Python codebase and the tests pass. Then
the subtle bugs start: a hash-locked runner silently broken by a rename, an
archive root hardcoded instead of honoring an env handoff, two copies of the
same function drifting apart. Static checkers won't find these because nothing
*looks* wrong — one implementation is just dead and the other is subtly
different.

Auto Code Audit is a three-layer toolkit for exactly this situation. It
**generates** candidate lists for dead modules, duplicate implementations,
hard-coded drift, contract violations, and AI writing-style signals in TeX
prose; **forces semantic review** of every candidate; then **verifies** accepted
edits with tests, package gates, and evidence checks. It caught the bugs above
in its origin project — a 100%-test-passing codebase.

```text
$ python run_all.py --package src
DEADCODE_SCAN package=src scanned=8 USED=0 ENTRYPOINT=0 PACKAGE=2 DEAD=4 ...

## Duplicate-implementation candidates
### [high] `a52d3baa1512`: 2 members (edge similarity 0.909)
- `experiments/e01.py:load_min_mask` (7 lines)
- `lib/runner.py:load_min_mask` (9 lines)

### Env-contract candidates
- env `E02_MODE` written at `experiments/e02.py:2` but never read in-package
```

Nothing is deleted automatically. Every candidate gets a verdict (`false
positive` writes a suppression entry; everything else requires a code action),
and the toolkit ships with an empty suppression registry: each project builds
its own `ignore.json` from its own semantic reviews.

## Contents

- [The three layers](#the-three-layers)
- [Quick start](#quick-start)
- [Key options](#key-options)
- [Scanners](#scanners)
- [Semantic review (Layer 2)](#semantic-review-layer-2)
- [Verification gates (Layer 3)](#verification-gates-layer-3)
- [Benchmark results](#benchmark-results)
- [Continuous integration](#continuous-integration)
- [Project layout](#project-layout)
- [Honest limitations](#honest-limitations)
- [License](#license)

## The three layers

Never promote a static hit directly into a deletion. Similarity is only
*candidate generation*; the unit of adjudication is the caller's functional
contract.

1. **Layer 1 — generate candidates** with deterministic scanners.
2. **Layer 2 — review every candidate** against its call sites and its role,
   writing a contract card per caller family.
3. **Layer 3 — verify accepted edits** with tests, package gates, and evidence
   checks.

## Quick start

The toolkit runs straight from a checkout with the standard library only
(Python 3.10+). `pip install -e .` additionally installs three console
commands: `auto-code-audit`, `auto-code-adjudicate`, `auto-code-verify`.

```text
# Layer 1: generate candidates for the audited project
python run_all.py --root /work/foo --package src

# Layer 2: adjudicate candidates (resumes from reports/verdicts.json)
python adjudicate.py --report /work/foo/reports/latest.json

# Layer 3: verify a fix (tests already ran, so point the gate at the result
# artifact; or use --test-command / --no-tests)
python -m pytest /work/foo/tests -q
python run_verify.py --report /work/foo/reports/latest.json \
  --verdicts /work/foo/reports/verdicts.json --previous /work/foo/reports/pre.json \
  --scope lib --test-result /work/foo/reports/ci-result.json
```

Runs all scanners against the package under the current directory (override
with `--root <repo>` / `--package <name>`). All workflow state — reports,
`ignore.json`, `LESSONS.md`, verdicts — defaults under the **audited project's**
root, never the toolkit checkout:

```text
<root>/reports/latest.json
<root>/reports/latest.md
```

To audit a third-party or immutable tree, route state outside it and pass
`--read-only` (it rejects the run if any writable state would land under
`--root`):

```text
python run_all.py --root /work/vendor --package src --profile code \
  --state-dir /work/audit-state/vendor --read-only
```

### Install as an agent skill

The repository doubles as an agent skill: install or clone it keeping `SKILL.md`
beside the scanner scripts, then invoke `$auto-code-audit` for a reuse check,
post-change audit, adjudication, or remediation verification. Copying only
`SKILL.md` is insufficient — the workflow calls the deterministic CLI bundled
here. The skill is the agent-facing protocol; the CLI is the evidence engine.
Project-specific rules belong in the audited project's `audit.config.json`, not
in a fork of the generic skill.

## Key options

| option | effect |
|---|---|
| `--profile code\|research` | code scanners only, or include the optional research TeX-style channel (default `code`) |
| `--no-doc-channel` | faster code-only dead-module pass |
| `--state-dir <path>` | set report / ignore / lessons / verdict defaults |
| `--read-only` | require external state; forbid writable state under the audited root |
| `--all-py` | scan every Python file recursively, overriding `subdirs` config |
| `--public-api` | classify unreferenced public-package modules as `PUBLIC_API_CANDIDATE` instead of `DEAD` |
| `--duplicate-threshold` / `--duplicate-min-chars` | duplicate sensitivity |
| `--ignore ignore.json` | approved suppression registry (Layer-2 output) |
| `--cli-smoke` | run `--help` on every scanner first; abort non-zero if any regressed |
| `--stale-check` | report `ignore.json` entries whose target no longer exists (read-only) |
| `--exhaustive` | render the full worksheet, including the low-value cohort |

`adjudicate.py --check` fails CI while candidates remain undecided. False
positives update the project's `ignore.json` (with `date` and `owner`) and
`LESSONS.md`; every other disposition stays in the verdict log because it
requires a code change or parity evidence. Each non-deferred verdict records a
stable `target_id` and a `finding_evidence_hash` (the digest of
`{scanner, target_id, detail}`), so a changed candidate forces re-review. An
optional `<root>/audit.config.json` tunes thresholds and exclusions:

```json
{"schema_version": 1, "regions": {"shared_paths": ["lib", "src/core"]}}
```

## Scanners

| scanner | candidate signal | common false positive |
|---|---|---|
| `scan_deadcode.py` | no visible import or documentation reference | dynamic dispatch, manually invoked runner, provenance-only tool |
| `scan_duplicates.py` | structurally similar function component | symmetric experiment arms, intentionally separate intervention boundaries |
| `scan_forks.py` | cross-file callables sharing a large common skeleton with diverged bodies (>= 40 lines, >= 75% token similarity) | deliberate specialization forks with distinct contracts |
| `scan_contracts.py` | modules used as libraries, dynamic module loading/state mutation, forwarding wrappers, unreferenced top-level functions, env-handoff and load-strictness violations | a valuable adapter, plugin loader with explicit lifecycle, intentional independent audit implementation |
| `scan_regions.py` | repeated capability blocks: inline copies of a named helper, shared-capability blocks, short high-density blocks, near-identical whole functions carrying API calls (`twin_match`) | parallel branches with genuinely distinct contracts, generic validation boilerplate |
| `scan_hardcoded.py` | syntax known to drift from shared behavior | a distinct hash contract or intentional frozen-forward implementation |
| `scan_capabilities.py` | script-local reimplementations of library functions | thin role-specific wrappers with real contracts |
| `scan_style.py` | AI-typical writing signals in TeX prose (semicolon chains, template openers, em-dash rate, burstiness, bare `\pm`) | technical enumeration, statistics-context wording |

Worth knowing before you read a report:

- `scan_deadcode.py` marks `__main__`-guarded scripts `ENTRYPOINT` and package
  initializers `PACKAGE`, never `DEAD`. Its dependency graph covers static
  imports, `sys.path`-pinned subdirectory imports, and importlib file loads.
- `scan_contracts.py` also detects runtime-created module bindings, plus four
  runtime-blind-spot channels invisible to AST fingerprints:
  `env_written_not_read`, `generation_path_without_env`, `cli_without_bootstrap`,
  `defensive_param_loosening`.
- `scan_regions.py` emits `helper_not_reused` (inline copy of an existing named
  function), `shared_capability`, `short_block_cluster`, and `twin_match`
  (near-identical whole functions with API calls) clusters.
- `scan_style.py` strips TeX to prose span-preservingly so reported line numbers
  match source; it scans `--tex-dir` (default `docs`), skipping archived trees.

## Semantic review (Layer 2)

Before assigning a verdict, write a contract card for each caller family:
functional role and ownership, inputs/outputs, errors, side effects,
configuration and persistence behavior, the existing canonical implementation,
and the parity gate needed before a change. Then assign one disposition:

| disposition | action |
|---|---|
| necessary specialization | retain locally |
| valuable adapter | retain and name by its role |
| independent audit | retain separately and parity-test |
| compatibility debt | migrate active callers, then remove/deprecate |
| true duplicate | consolidate |
| false positive | suppress only after review |

Record the rationale in `LESSONS.md` **before** editing `ignore.json`. A clean
static report cannot override a failed behavior or provenance gate.

## Verification gates (Layer 3)

```text
python -m pytest tests -q              # this toolkit's fixtures
python -m pytest <package>/tests -q    # the target package
python run_verify.py --report <new> --verdicts <verdicts.json> --previous <old>
```

`run_verify.py` re-audits after a fix and rejects when: a code-action verdict's
`target_id` still appears in the new report, a still-present finding's evidence
hash is unchanged, or the patch scope gained a high/medium candidate that was
absent before. New-candidate severity is unified across scanner schemas in one
function (`run_all.finding_severity`), so a patch that strands a module, adds a
defensive-param loosening, or writes an env var without a read is rejected —
not just a duplicate or region hit.

Test evidence is machine-checkable three ways (`--test-command`, `--test-result`,
`--no-tests` are mutually exclusive):

- `--test-command "<cmd>"` — runs the target's tests inside the gate.
- `--test-result <file>` — consumes a machine-readable artifact with provenance
  (`status`, consistent `exit_code`, and a `git_head` equal to the report's
  commit); a hand-written `{"status":"passed"}` is not full evidence.
- `--no-tests` — declares verification delegated outside the gate; accepted but
  reports `fully_verified: false`.

A code-action verdict with no test evidence is rejected — the gate never
self-approves. `fully_verified` is true only when the gate passed, the test
evidence was machine-checked, **and** a comparable pre-patch report (`--previous`)
was given; an incompatible baseline rejects rather than trusting a garbage one.

> **Trust-model note.** `fully_verified` means the supplied report, verdicts,
> and test evidence satisfy this deterministic gate — it is *not* a
> cryptographic or independently-reproduced attestation that the target
> repository was completely rescanned and tested. The gate validates that the
> artifacts are internally consistent and bound to the audited source tree; it
> does not re-run the scanners itself, and it trusts the operator-supplied
> `--scope` and `--test-command`. For an unattended merge gate, run a fresh
> scan as part of the same pipeline and authenticate test evidence at your CI
> layer.

## Benchmark results

The pilot corpus is six small, popular Python projects — click, httpx, pytest,
requests, starlette, werkzeug — pinned to fixed commits. Every candidate the
toolkit emits there is adjudicated by hand, and those labels are committed under
`benchmarks/labels/` as ground truth. The harness clones the pinned commits,
runs a read-only `code` profile, and scores a fresh run against the labels.

| metric | value |
|---|---|
| adjudicated candidates | 594 (618 labels; 24 stale after scanner changes) |
| confirmed defects (true findings) | 16 |
| distinct issues they collapse to | 10 |
| precision | 0.027 |
| review burden | ~37 candidates per confirmed defect |
| mutation-corpus recall | 1.000 (25/25 injected targets) |

The confirmed defects concentrate in two channels — `duplicates` (10) and
`regions` (6); the other scanners found none in this corpus. That low precision
is deliberate: the toolkit over-signals so a real defect is never silently
dropped, and the expected-value cohort below compresses the review cost.

| cohort | candidates | true findings | precision |
|---|---:|---:|---:|
| high (near-exact duplicates, region twins) | 69 | 12 | 0.17 |
| medium (shared-capability regions) | 21 | 2 | 0.10 |
| low (everything else) | 504 | 2 | 0.004 |

The markdown worksheet hides the low cohort by default, so a review starts from
the ~90 high/medium candidates carrying 14 of the 16 confirmed findings; run
with `--exhaustive` to restore the full surface.

The mutation corpus (`benchmarks/mutation/`) injects one known defect per
channel and checks recall by exact `(scanner, target_id)` matching — a hit on
the wrong target is a miss. 25 injected, 25 matched.

Methodology, metric definitions, evidence fusion, and the per-batch
adjudication history are documented in [BENCHMARKS.md](BENCHMARKS.md).

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request across Python
3.10–3.13 on Ubuntu and Windows: the test suite, a blocking mypy pass, an
entrypoint `--help` smoke, an end-to-end dogfood self-audit (including the
report-diff path), a wheel build/import smoke, and a whitespace check.

## Project layout

```text
run_all.py              one-command orchestration + summary report + report diff
adjudicate.py           resumable Layer-2 semantic candidate review
run_verify.py           engine-owned deterministic acceptance gate (post-fix)
scan_*.py               the deterministic scanners (deadcode, duplicates,
                        regions, forks, contracts, capabilities, hardcoded, style)
scan_cli_smoke.py       entrypoint --help regression gate
pyproject.toml          packaging metadata; console scripts
benchmarks/             fixed-commit pilot corpus, labels, and read-only harness
SKILL.md                the full three-layer agent protocol
LESSONS.md              false-positive lesson archive (read before Layer 2)
ignore.json             approved suppression registry (ships empty)
tests/                  fixture tests for every scanner
```

## Honest limitations

Auto Code Audit is a candidate generator, not a verdict engine. Its own design
demands the same honesty it applies to your codebase:

- **Most candidates are false positives, by design.** On the pinned public
  corpus, only 16 of 594 adjudicated candidates resolve to a confirmed defect
  (~2.7%) — a review burden of ~37 candidates per confirmed finding. It
  deliberately over-signals so nothing is silently missed; the cost is that
  every candidate still needs a human (or LLM) semantic review.
- **Layer 2 is where the real work happens.** A static hit is never proof of a
  bug — the tool forces you to write a contract card and adjudicate. Skip
  Layer 2 and the tool only produces noise.
- **It sees only what is statically visible.** Dynamic dispatch, runtime
  configuration, and behavior that emerges only at execution time are blind
  spots; the contracts scanner has channels for some of these, but they remain
  review candidates, not verdicts.
- **It never decides for you.** Nothing is deleted automatically; every
  code-changing disposition is a human decision the tool records and later
  verifies — not one it makes.
- **Benchmark numbers are corpus-bound.** The precision/recall figures come
  from six small pinned public projects plus a synthetic mutation fixture;
  they describe those corpora, not your codebase.
- **This tool is itself AI-maintained.** It is a dogfooding project: its own
  CI runs the scanners against itself. Treat its claims with the same
  skepticism it applies to yours.

## License

MIT — see [LICENSE](LICENSE).
