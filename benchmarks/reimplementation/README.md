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

## Baseline vs multi-channel retrieval

12 must-surface cases: the 7 rename family plus 5 harder ones (control-flow
rewrite, different algorithm, inlined helper, service→manager reinvention,
repository bypass).

| metric | `scan_capabilities` (name+docstring) | `capability_retrieval` (structural+call+string+lexical+closure) |
|---|---:|---:|
| Candidate Recall@1 | 0.083 | **0.833** |
| Candidate Recall@5 | 0.083 | **0.917** |
| Candidate Recall@10 | 0.083 | **0.917** |
| MRR | 0.083 | **0.875** |

The old channel catches only a same-name case; the new layer surfaces 11 of 12
cases. Which channel wins is visible per case:

- **structural** handles the rename family and the composed/decomposed email
  pair (names normalized away);
- **call overlap** catches the inlined helper (`clean_title` vs `slugify`: both
  call `strip`/`lower`/`replace`);
- **string overlap** catches the repository bypass (`load_active_user` vs
  `find_active`: the identical SQL literal);
- **one-hop closure** catches the composite (`save_avatar_variant` calls its
  own duplicate `upload_avatar`, so it transitively surfaces
  `StorageService.upload`).

The one miss is the deterministic ceiling: `check_palindrome` (two pointers) vs
`is_palindrome` (slice-reverse) — same I/O contract, genuinely different
algorithm, no shared calls/literals/structure. It needs semantic/embedding
recall. (The structural channel compares normalized *token sequences*, not raw
strings, so the retrieval stays fast on real repositories.)
