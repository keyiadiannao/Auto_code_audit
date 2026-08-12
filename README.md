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
auto-code-verify --report /work/foo/reports/latest.json --verdicts /work/foo/reports/verdicts.json
```

## Quick start

```text
# Layer 1: generate candidates for the audited project
python run_all.py --root /work/foo --package src

# Layer 2: adjudicate candidates; state defaults follow the report's project
# root, not the toolkit checkout
python adjudicate.py --report /work/foo/reports/latest.json

# Layer 3: run the audited project's tests, provenance gates, and the
# engine-owned acceptance gate after a fix. Tests ran above, so point the
# gate at the machine-readable result artifact (--test-result), or declare
# external delegation with --no-tests; --test-command would run the suite
# inside the gate instead
python -m pytest /work/foo/tests -q
python run_verify.py --report /work/foo/reports/latest.json \
  --verdicts /work/foo/reports/verdicts.json --previous /work/foo/reports/pre.json \
  --scope src/lib --test-result /work/foo/reports/ci-result.json
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

Each non-deferred verdict records a stable `target_id` and a
`finding_evidence_hash` (the digest of `{scanner, target_id, detail}` — the
stale-evidence binding; the protocol layer's separate *case* hash, which
binds commit and snippets, is only used for adjudication bundles, never for
stale detection). If the candidate evidence changes, `--check` requires a
new review. False positive verdicts also store their compiled suppression
payload, so this is reproducible even after the candidate disappears from a
later report:

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

### Labelled evaluation

`benchmarks/labels/<project>.json` holds ground-truth adjudications keyed by
the same stable `(scanner, target_id)` signatures the report diff uses. Each
entry is `true_finding` or `false_positive` with a reason. The harness matches
labels against the fresh run and reports per-scanner and aggregate stats per
project, plus corpus totals:

- `precision` — confirmed findings / labelled candidates (denominator is the
  labelled subset only; unlabelled candidates are counted in `coverage` but do
  not affect precision)
- `coverage` — labelled vs. total candidates per project, so unlabelled
  channels stay visible instead of being silently dropped
- `review_burden` — candidates per confirmed finding (corpus total)
- `unique_issues` — distinct defects behind the true findings: labels may
  carry an `issue_id`, so several candidates reporting one defect (a region
  twin corroborating a `duplicates` finding for the same function pair)
  count once; an issue counts only when a run reproduced it
- `issues` — per-issue evidence: every true label of the issue plus which of
  them the run matched
- `candidates_per_kloc` and `runtime_per_kloc` — cost of a full pass against
  package size (lines of scanned `.py` code)

The label set covers the channels most reliably adjudicated on public
packages: dead-`public`-`API` candidates, forwarding wrappers, unreferenced
public functions, fork pairs that are intentional convenience wrappers,
non-security digest hashes, every `duplicates` cluster emitted at the pinned
commits, and the first region batch (all 21 shared/short clusters plus the 50
highest-coverage `helper_not_reused` clusters and all 11 `twin_match`
clusters). 423 of 605 candidates carry
labels (the 182 unlabelled remainder are lower-coverage `regions` helper
clusters).
Sixteen clusters are `true_finding` — twelve
from `duplicates`: near-verbatim copies inside the pytest thread/unraisable
plugin pair, werkzeug's Request/Response deprecation wrappers
(`content_md5`, `pragma`), `is_json`, and the `mimetype_params` copied from
`Response` into `EnvironBuilder`, plus click's twin reader/writer helpers and
shell-completion env parsing and starlette's `Route`/`WebSocketRoute`
`url_path_for`; and four from `regions` — the two halves of the same pytest
plugin pair (the `collect_thread_exception`/`collect_unraisable` hooks and the
`pytest_configure` wiring each plugin copies from its twin) reported twice,
once by the shared-capability channel and once by the `twin_match` function
channel, plus werkzeug's `mimetype_params` and `content_md5` copies that the
twin channel surfaces as API-ful builder twins. The
remaining clusters are false positives (intentional API-layer wrappers,
sync/async dual-interface mirrors, boilerplate dunder families, parallel
parsers with distinct grammars, and token-coincidence matches where the
region never actually inline-copies the canonical named helper).
Corpus totals at the pinned commits: precision 0.038 (16/423), review burden
37.8 candidates per confirmed finding, 6.34 candidates per KLOC, runtime
~0.3s per KLOC. The 16 labelled true findings collapse to 10 unique issues:
each corroborating twin label shares the issue id of the `duplicates`
finding it confirms (pytest's two plugin-pair defects, werkzeug's
`mimetype_params` and `content_md5` copies), so the region twins add channel
evidence without inflating the defect count. A label file whose `target_id`
matches no candidate in the pinned commit is reported as `unmatched_labels`
(stale scope), so label drift is visible rather than silent. Eleven entries
are currently unmatched — seven `false_positive`
constructor/`__init__`-boilerplate or ASGI-idiom labels created before the
`__init__`-canonical helper filter landed, plus four stale `regions` twin
labels whose issues are still counted through their matched `duplicates`
siblings, so no unique issue was lost.

The regions scanner adds ~1.9 candidates per KLOC on the six pinned repos
(184 clusters; helper-not-reused matches dominate at 152, plain
shared-capability 12, function-twin 11, short-block 9); once regions entered
the profile the review burden rose from 42.1, and the helper-FP filters below
brought it back to 49.2. The twin channel's 11 clusters were then adjudicated
in the second batch: 4 `true_finding` (pytest's `pytest_configure` and collect
hook pairs, werkzeug's `mimetype_params` and `content_md5`) and 7
`false_positive` in deliberate-parallel families (public `@fixture` API
surface, text/bytes mirror hierarchies, the header-property idiom, parallel
deprecation shims, and type-narrowed dunder overrides). All 4 true findings
corroborate pairs the `duplicates` channel already flags — the twin channel
confirms them as function-level API-ful builders rather than span-level
coincidences, with twin precision 4/11 = 0.36. That adjudication dropped the
review burden to 37.8 and raised corpus precision to 0.038, and argues the
remaining twin FPs need semantic filters (public API / parallel-family
suppression), not a different clustering linkage: every FP pair is
deliberately parallel, so complete-link or medoid clustering would still join
them.
First-batch adjudication of that cohort found 2
`true_finding` clusters out of 71 labelled: the pytest thread/unraisable
plugin pair duplicates both its collect hook and its configure wiring (the
same copy-paste the `duplicates` channel flags). The remaining 69 labelled
clusters are false positives in four families: constructor/`__init__`
attribute-assignment boilerplate (`check_ispytest`-style), deliberate
parallel families (xunit setup fixtures, sync/async mirrors, ASGI connection
classes, capture `snap` variants), token-coincidence matches where the
canonical is a nested span or an unrelated member, and `short_risky`
self-pairs where the same function is matched against itself. The labelled
`regions` precision at the pinned commits is 0.038 (2/52): all 50 labelled
`helper_not_reused` clusters are false positives, so the helper channel now
suppresses the two dominant FP families before matching — `__init__`
canonicals (constructor attribute-assignment boilerplate; 14 of the 50) and
regions whose parent already references the canonical by name (an inline
block that is not an orphaned copy; 3 more). That cut labelled helper
clusters from 50 to 31 (19 dropped, every one a false positive) and helper
clusters corpus-wide from 178 to 149 with zero loss of confirmed findings
(both regions `true_finding` clusters are `shared_capability`, untouched;
the mutation-corpus helper recall still passes). The remaining helper FP
families — deliberate parallel families and token-coincidence matches — need
semantic signals beyond token overlap; the 31 labelled survivors stay open
for a second adjudication batch alongside the 116 remaining unlabelled
helper clusters.

### Mutation corpus (recall)

`benchmarks/mutation/project/` is a synthetic fixture that injects one known
issue per channel: a dead module, a duplicated function, an env write without
a read, `strict=False` loosening, a `generation_a` hardcoded path, and a
capability overlap. `run_mutation.py` scans the fixture and compares the
detected `(scanner, target_id)` set — the same stable signatures the label
set uses — against `benchmarks/mutation/expected.json`:

```powershell
python benchmarks\run_mutation.py
```

Recall is exact target matching, not count matching: a channel that fires on
the wrong target (or reports the right count for unrelated reasons) is a
miss. The v2 corpus covers all seven code scanners and now also injects a
`hardcoded` mutant (a hand-written SHA-256 hexdigest) and region mutants
covering all four channels (a checkpoint block in the `shared` channel,
inline device/dtype validation re-implementing a lib helper in
`helper_not_reused` — both a plain inline copy and a partial-reuse drift
case where the parent also calls the helper, asymmetric
`S00`/`S01`/`S10`/`S11` table lookups in `short_risky`, and a gauge-style
provider builder duplicated between lib and experiments in the
function-twin channel). Current corpus: 25 injected targets, 25 matched,
recall 1.000; the runner exits nonzero when
any channel misses expected detections, so the regression gate covers
scanner recall as well as precision on the corpus.

## Scanners

| scanner | candidate signal | common false positive |
|---|---|---|
| `scan_deadcode.py` | no visible import or documentation reference | dynamic dispatch, manually invoked runner, provenance-only tool |
| `scan_duplicates.py` | structurally similar function component | symmetric experiment arms, intentionally separate intervention boundaries |
| `scan_forks.py` | cross-file callables sharing a large common skeleton with diverged bodies (>= 40 lines, >= 75% token similarity) | deliberate specialization forks with distinct contracts, same-file symmetric helpers |
| `scan_contracts.py` | modules used as libraries, forwarding wrappers, repeated contract-sensitive names, unreferenced top-level functions, env-handoff and load-strictness violations | a valuable adapter, dynamic entrypoint, or intentionally independent audit implementation |
| `scan_regions.py` | repeated capability blocks: inline copies of an existing named helper (`helper_not_reused`), shared-capability blocks across files, short high-semantic-density blocks (asymmetric indexing, contract kwargs), and near-identical whole functions carrying API calls (`twin_match`) | parallel branches with genuinely distinct contracts, single-occurrence boilerplate |
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
- **`scan_regions.py`** extracts every capability block in the package and
  emits four cluster kinds: `helper_not_reused` (an inline copy of an
  existing named function — carries the `canonical_symbol` and per-member
  coverage), `shared_capability` (repeated blocks across files or functions),
  short high-semantic-density blocks (`short_block_cluster: true`, e.g.
  asymmetric `S00[a, a]`-style table lookups too short for region matching),
  and function twins (`twin_match: true`): near-identical whole functions
  whose bodies carry attribute API calls — invisible to the other channels,
  because the `helper` channel only accepts API-free blocks and fully
  covered bodies are excluded from the canonical function index.
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
python run_verify.py --report <new> --verdicts <verdicts.json> --previous <old>
git diff --check
```

`run_verify.py` is the engine-owned Layer-3 acceptance gate (SKILL.md Phase 3).
It re-audits after a fix and fails when a code-action verdict's `target_id`
still appears in the new report, when a still-present finding's
`finding_evidence_hash` recomputes unchanged, or when the patch scope gained
a high/medium candidate that was absent before the patch. It speaks the
skill-first protocol vocabulary (`disposition: true_finding` +
`recommended_action`, e.g. `extract_shared_component`) as well as the legacy
`adjudicate.py` dispositions (`true duplicate` / `compatibility debt`), so
`--verdicts` accepts either the aggregated `verdicts.json` or the per-case
protocol verdict directory from SKILL Phase 2.

New-candidate severity is unified across scanner schemas in one function
(`run_all.finding_severity`): `priority` (duplicates, regions) →
`severity` (hardcoded) → a new `DEAD` module's `status` → the contracts
`_channel` (`defensive_param_loosening` and `env_written_not_read` high,
`generation_path_without_env` medium). A patch that silently strands a
module — or that adds a defensive-param loosening or an env write without a
read — is rejected, not just a duplicate or region hit.

The gate also makes test evidence machine-checkable, three ways
(`--test-command`, `--test-result`, and `--no-tests` are mutually
exclusive):

- `--test-command "<shell command>"` — runs the target project's tests
  inside the gate; a non-zero exit rejects.
- `--test-result <file>` — consumes a machine-readable external test
  artifact with strong provenance so a hand-written `{"status":"passed"}`
  cannot be full verification evidence: required fields are `status`
  (`"passed"`/`"failed"`), `exit_code` (consistent with `status`),
  `git_head` (40-char hex, must equal the report's `provenance.git.head` —
  test evidence must come from the same commit the report was scanned at),
  and a runner identity via `tool` or `command`. Machine-checked like an
  internal run.
- `--no-tests` — declares that behavioral verification is delegated outside
  the gate (`test_gate: "external_unverified"`); accepted, but the result
  JSON then reports `fully_verified: false`.

A code-action verdict with no test evidence at all is rejected — the gate
never self-approves. The result JSON (`--json`) reports `passed`, the
`test_gate` value, and `fully_verified`, which is true only when the gate
passed, the test evidence was machine-checked, **and** a comparable
pre-patch report was given (`--previous`): full verification means the gate
can prove the patch introduced no new high/medium candidate, which is
impossible without a baseline. An incompatible `--previous` (schema /
package / profile / scanner-set mismatch) rejects rather than trusting a
garbage baseline. Verdict artifacts the Layer 2 validator would reject also
reject the gate fail-closed — an invalid verdict file is never silently
dropped from the gate's input. Exit code 0 means acceptance; `--scope`
limits the new-candidate check to the path the patch touched.

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request across Python
3.10–3.13 on Ubuntu and Windows: the test suite, an entrypoint `--help`
smoke, an end-to-end dogfood self-audit (including the report-diff path), a
wheel build/import smoke, and a whitespace check.

## Project layout

```text
run_all.py              one-command orchestration + summary report + report diff
adjudicate.py           resumable Layer-2 semantic candidate review
run_verify.py           engine-owned deterministic acceptance gate (post-fix)
scan_*.py               the deterministic scanners (deadcode, duplicates,
                        regions, forks, contracts, capabilities, hardcoded,
                        style)
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
