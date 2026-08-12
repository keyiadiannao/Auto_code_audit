# Auto Code Audit

[![CI](https://github.com/keyiadiannao/Auto_code_audit/actions/workflows/ci.yml/badge.svg)](https://github.com/keyiadiannao/Auto_code_audit/actions/workflows/ci.yml)

Your AI assistant refactored a large Python codebase and the tests pass.
Then the subtle bugs start: a hash-locked runner silently broken by a
rename, an archive root hardcoded instead of honoring an env handoff, two
copies of the same function drifting apart. Static checkers won't find
these because nothing *looks* wrong — one implementation is just dead and
the other is subtly different.

Auto Code Audit is a three-layer static audit toolkit for exactly this
situation. It generates candidate lists for dead modules, duplicate
implementations, hard-coded behavior drift, contract violations, and AI
writing-style signals in TeX prose; forces semantic triage of every
candidate; then runs package and provenance gates. It caught the two bugs
above in its origin project — a 100%-test-passing codebase.

```text
$ python run_all.py --package src
DEADCODE_SCAN package=src scanned=8 USED=0 ENTRYPOINT=0 PACKAGE=2 DEAD=4 ...
SELF_AUDIT_RUN_ALL package=src dead=4 dup_clusters=1 cap_overlap=1 ...

## Duplicate-implementation candidates
### [high] `a52d3baa1512`: 2 members (edge similarity 0.909)
Reason: shared lib member and an active non-lib implementation.
- `experiments/e01.py:load_min_mask` (7 lines)
- `lib/runner.py:load_min_mask` (9 lines)

### Env-contract candidates
- env `E02_MODE` written at `experiments/e02.py:2` but never read in-package
```

Nothing is deleted automatically. Every candidate gets a verdict (`false
positive` writes a suppression entry; everything else requires a code
action), and the toolkit ships with an empty suppression registry: each
project builds its own `ignore.json` from its own semantic reviews.

## The three layers

Never promote a static hit directly into a deletion or refactor. Similarity
is only candidate generation; the unit of adjudication is the caller's
functional contract.

1. **Layer 1 — generate candidates** with deterministic scanners.
2. **Layer 2 — review every candidate** against its call sites and its role,
   writing a contract card per caller family.
3. **Layer 3 — verify accepted edits** with tests, package gates, and
   evidence checks.

## Requirements

- Python 3.10+ (stdlib only for the scanners)
- `pytest` for the test suite (optional)

## Installation (optional)

The toolkit runs straight from a checkout with stdlib only. `pip install -e .`
additionally installs two console commands wrapping the same entrypoints:

```text
auto-code-audit --root /work/foo --package src
auto-code-adjudicate --report /work/foo/reports/latest.json
```

## Quick start

```text
# Layer 1: generate candidates for the audited project
python run_all.py --root /work/foo --package src

# Layer 2: adjudicate candidates; state defaults follow the report's project
# root, not the toolkit checkout
python adjudicate.py --report /work/foo/reports/latest.json

# Layer 3: run the audited project's tests and provenance gates
python -m pytest /work/foo/tests -q
```

Runs all scanners against the package under the current directory (override
with `--root <repo>` / `--package <name>`). State paths default under the
repo root — not the toolkit directory — so every artifact of a run lands in
the project being audited:

```text
<root>/reports/latest.json
<root>/reports/latest.md
```

Useful options:

- `--no-doc-channel` — faster code-only dead-module pass
- `--profile code|research` — run code-focused scanners only, or include the
  research TeX style channel (default: `research`)
- `--all-py` — scan every Python file recursively; use this for flat or `src/`
  package layouts instead of the configured `lib/experiments` subdirectories
- `--public-api` — classify importable public-package modules with no internal
  references as `PUBLIC_API_CANDIDATE` instead of `DEAD`; review them manually
- `--duplicate-threshold` / `--duplicate-min-chars` — duplicate sensitivity
- `--ignore ignore.json` — approved suppression registry (Layer-2 output)
- `--cli-smoke` — run `--help` on every scanner entrypoint first; abort the
  audit with a non-zero exit if any regressed
- `--stale-check` — report `ignore.json` entries that no longer target live
  code (file, line, or symbol gone); read-only, never edits the registry
- run any scanner directly for a scoped investigation

`adjudicate.py` resumes from `reports/verdicts.json`. Use
`python adjudicate.py --report <report> --check` in CI to fail when candidates
remain undecided. False positives update
the audited project's `ignore.json` and `LESSONS.md`; other dispositions stay
in the verdict log because they require code changes or parity evidence.
Suppression entries carry a `date` stamp and an `owner` (from `--owner`,
defaulting to `git user.name`); re-suppressing an already-registered candidate
keeps the original record. Pass `--ignore`, `--lessons`, or `--verdicts`
explicitly when a project uses a non-default state layout.

Each non-deferred verdict records a stable `target_id` and an `evidence_hash`.
If the candidate evidence changes, `--check` requires a new review. False
positive verdicts also store their compiled suppression payload, so this is
reproducible even after the candidate disappears from a later report:

```text
python adjudicate.py --root /work/foo \
  --verdicts /work/foo/reports/verdicts.json \
  --export-ignore /work/foo/ignore.json
```

An optional `<root>/audit.config.json` tunes scanner thresholds and exclusions.
The config is checked for supported keys, types, ranges, and schema version;
invalid values produce one warning and use compiled-in defaults.

## Benchmark Corpus

The fixed-commit pilot corpus lives in `benchmarks/manifest.json`. It contains
small, popular Python projects with different package layouts. The harness
clones the declared commits, runs a read-only `code` profile, and writes one
JSON result plus logs per project:

```powershell
python benchmarks\run_benchmarks.py `
  --workspace C:\Temp\auto-code-audit-benchmarks `
  --output C:\Temp\auto-code-audit-benchmarks\results.json
```

Use `--project requests` to run one entry, `--refresh` to replace a checkout at
the wrong commit, and `--dry-run` to validate commands without network access.
The harness does not install or execute target-project code. For flat and
`src/` layouts, manifest entries use `--all-py`; ordinary audits retain the
configured subdirectory scope.

Consecutive runs against the same `--json` path diff against the previous
report: `latest.json` gains a `previous_run` block (per-scanner new/gone
signatures) and `latest.md` gains a "Changes since last run" section, so a
review session starts from what changed instead of re-reading the whole
worksheet.

## Scanners

| scanner | candidate signal | common false positive |
|---|---|---|
| `scan_deadcode.py` | no visible import or documentation reference | dynamic dispatch, manually invoked runner, provenance-only tool |
| `scan_duplicates.py` | structurally similar function component | symmetric experiment arms, intentionally separate intervention boundaries |
| `scan_forks.py` | cross-file callables sharing a large common skeleton with diverged bodies (>= 40 lines, >= 75% token similarity) | deliberate specialization forks with distinct contracts, same-file symmetric helpers |
| `scan_contracts.py` | modules used as libraries, forwarding wrappers, repeated contract-sensitive names, unreferenced top-level functions, env-handoff and load-strictness violations | a valuable adapter, dynamic entrypoint, or intentionally independent audit implementation |
| `scan_hardcoded.py` | syntax known to drift from shared behavior | a distinct hash contract or an intentional frozen-forward implementation |
| `scan_capabilities.py` | script-local reimplementations of library functions | thin role-specific wrappers with real contracts |
| `scan_style.py` | AI-typical writing signals in TeX prose (semicolon chains, template openers, em-dash rate, burstiness, excess vocabulary, bare `\pm`) | technical enumeration, section-map lists, statistics-context "robust/significant" |

Highlights worth knowing before you interpret a report:

- **`scan_deadcode.py`** marks `__main__`-guarded scripts as `ENTRYPOINT` and
  package initializers as `PACKAGE`, never `DEAD`. Its dependency graph covers
  three channels: static imports, bare imports under a `sys.path`-pinned
  subdirectory, and importlib file loads (including thin wrapper calls).
- **`scan_contracts.py`** runs four runtime-blind-spot channels that plain
  AST fingerprints cannot see: `env_written_not_read`,
  `generation_path_without_env`, `cli_without_bootstrap`, and
  `defensive_param_loosening`.
- **`scan_style.py`** strips TeX to prose span-preservingly, so reported line
  numbers match the source exactly; it scans `--tex-dir` (default `docs`)
  recursively, skipping archive/frozen/legacy directories.

## Semantic review (Layer 2)

Before assigning a verdict, write a contract card for each caller or caller
family: scientific role, accepted inputs/contracts, outputs, randomness and
provenance behavior, the existing canonical implementation, the exact
semantic delta preventing direct reuse, and the parity or evidence gate
needed before a change.

Assign one disposition per candidate:

| disposition | action |
|---|---|
| necessary specialization | retain locally |
| valuable adapter | retain and name by its role |
| independent audit | retain separately and parity-test |
| compatibility debt | migrate active callers, then remove/deprecate |
| true duplicate | consolidate |
| false positive | suppress only after review |

Record the rationale in `LESSONS.md` **before** editing `ignore.json`.
`ignore.json` is an approved suppression registry — never let a scanner
mutate it. A clean static report cannot override a failed behavior or
provenance gate.

## Verification gates (Layer 3)

```text
python -m pytest tests -q          # this toolkit's fixtures
python -m pytest <package>/tests -q   # the target package
python <package>/verify/... --quick   # the package verifier, if one exists
git diff --check
```

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request across Python
3.10–3.13 on Ubuntu and Windows: the test suite, an entrypoint `--help`
smoke, an end-to-end dogfood self-audit (including the report-diff path), a
wheel build/import smoke, and a whitespace check.

## Project layout

```text
run_all.py              one-command orchestration + summary report + report diff
adjudicate.py           resumable Layer-2 semantic candidate review
scan_*.py               the seven deterministic scanners
scan_cli_smoke.py       entrypoint --help regression gate (run_all --cli-smoke)
pyproject.toml          packaging metadata; console scripts auto-code-audit/-adjudicate
benchmarks/             fixed-commit pilot corpus and read-only benchmark harness
SKILL.md                the full three-layer protocol
LESSONS.md              false-positive lesson archive (read before Layer 2)
ignore.json             approved suppression registry (ships empty)
tests/                  fixture tests for every scanner
```

## License

MIT — see [LICENSE](LICENSE).
