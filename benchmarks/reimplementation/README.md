# Reimplementation benchmark

The instrument for the "implementation-reuse firewall" pivot. It asks a single
question per authored case:

> **Given a NEW symbol, does the retrieval surface the EXISTING canonical
> implementation it overlaps with, in the top-K?**

It is deliberately **not** a scanner-regression test. The existing mutation
corpus proves each scanner "still fires on its injected target"; this benchmark
proves (or disproves) that the system can find *semantic reinvention* — a new
implementation whose name, docstring, and control flow all differ from the
existing one but whose responsibility, dependencies, and side effects are the
same.

## Why this exists

`scan_capabilities.py` currently indexes only `lib/*.py` and matches
name-first, then docstring similarity. So:

```text
rotate_credentials()   vs   SessionService.refresh()
```

— different name, different docstring, same responsibility — is invisible to it.
This benchmark makes that gap a number instead of an opinion.

## Ground-truth independence

Each case in `cases.json` is authored by hand: a `(new, existing, verdict)`
triple plus a `trick` and `rationale`. The ground truth is **not** produced by
running the scanner and labelling its output — a semantic duplicate the scanner
never surfaces still exercises the metric. The runner locates the new symbol in
`fixture/pkg/experiments/`, the canonical symbols in `fixture/pkg/lib/`, and
asks only whether the correct existing symbol appears in the ranked candidate
list.

## Metrics

Over the "must-surface" cases (`REUSE_EXISTING` / `EXTEND_EXISTING`):

- **Candidate Recall@K** — fraction where the correct existing symbol ranks
  ≤ K. This is the headline: if the right symbol never enters the candidate
  set, no downstream reviewer can save it.
- **MRR** — mean reciprocal rank over the same cases.

`KEEP_SEPARATE` cases are excluded from recall: they measure *action accuracy*
(whether the system wrongly recommends reuse), which needs the AI adjudication
layer and is a future metric. They are kept in the manifest so the benchmark
does not silently optimize toward "merge everything similar".

## Case tricks (the hard cases)

The corpus is built to defeat structural scanners:

| trick | example |
|---|---|
| renamed + different vocabulary | `rotate_credentials` ≈ `refresh_session` |
| key construction rewritten | `persist_user_session` ≈ `SessionRepository.save` |
| method vs function | `upload_avatar` ≈ `StorageService.upload` |
| pipeline verbs renamed | `normalize_image` ≈ `ImageProcessor.prepare` |
| partial overlap (EXTEND) | `save_avatar_variant` adds validation on top of upload |
| same shape, different domain (KEEP) | `encrypt_report` vs `encrypt_audit_log` |
| same shape, different protocol (KEEP) | `serialize_invoice` vs `serialize_purchase_order` |
| deliberate duplicate across trust boundary (KEEP) | `sanitize_user_input` vs `sanitize_input` |

## Running

```bash
python benchmarks/reimplementation/run.py
```

## Baseline vs structural retrieval

| metric | `scan_capabilities` (name+docstring) | `capability_retrieval` (structural+lexical) |
|---|---:|---:|
| Candidate Recall@1 | 0.000 | **0.857** |
| Candidate Recall@5 | 0.000 | **0.857** |
| Candidate Recall@10 | 0.000 | **1.000** |
| MRR | 0.000 | **0.873** |

The old channel returns the `none` channel for every must-surface case (zero
candidates): `rotate_credentials` cannot find `refresh_session` because the
names and docstrings differ. The new layer normalizes the function body —
variable, method, keyword, and function names plus constants — so
`store.mint(id); store.write(id, secret, expiry=3600)` and
`token_store.issue(id); token_store.put(id, token, ttl=3600)` collapse to the
same normalized pipeline and rank first.

Two honest caveats:

1. **Small corpus.** Ten hand-written cases is a direction, not a result. The
   number will move as harder cases (control-flow rewrites, inlined helpers,
   partial overlaps) are added.
2. **Recall-first by design.** Aggressive normalization over-surfaces; the
   `KEEP_SEPARATE` cases rank 1 *on purpose* (the reviewer must see the
   candidate to reject it). Precision / false-consolidation rate is a separate
   metric that needs the adjudication layer and is not yet measured.
