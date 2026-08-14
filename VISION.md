# Vision: an implementation-reuse firewall for AI coding

## One sentence

Auto Code Audit is becoming a **reuse firewall**: before (or right after) an AI
agent adds a new helper / manager / service / adapter, it surfaces the existing
implementations the new code overlaps with, so the agent must justify "this is
new" against evidence instead of blindly re-implementing.

The question it answers is not "is this code dead or duplicated?" but:

> **Does this new implementation deserve to exist as a new thing in this
> repository?**

## Why this pivot

The original static-audit framing (dead code, duplicates, hardcoded drift) sits
in a crowded, low-differentiation category. The problem that actually hurts in
AI-assisted development is different and earlier: the agent *never checks what
the repository already has*, so it keeps inventing parallel helpers, managers,
services, and adapters. Catching that at write-time is higher value than any
post-hoc linter.

## The core discipline (unchanged from the audit tool)

- **Search by responsibility, not syntax.** Real reuse is cross-name,
  cross-docstring, cross-file.
- **The index only recalls; adjudication re-reads real code.** Never let a
  summary make the decision.
- **Never auto-delete, never auto-merge.** The firewall *surfaces candidates*;
  a human (or, later, an LLM) decides `REUSE_EXISTING` / `EXTEND_EXISTING` /
  `KEEP_SEPARATE`.
- **Do not reward reuse for its own sake.** Two similar implementations with
  different ownership or domain are correctly separate.

## Where we are

| step | status |
|---|---|
| PR 1 — reimplementation benchmark (ground truth independent of scanners) | ✅ done |
| PR 2 — deterministic multi-channel retrieval (structural + call + string + lexical + one-hop closure) | ✅ done — Recall@10 0.000 → **1.000** on 12 cases |
| PR 3 — data model: scanner finding → `(new_symbol, existing_symbol)` reuse decision | ⏳ next |
| semantic recall (offline LLM summary + embedding) | 🔜 later, only if proven worth it |

## Design decisions (and why)

1. **No full code graph / AST-graph platform.** The graph's cheap projections
   — normalized body, called-name overlap, string-literal overlap — already
   reach Recall@10 = 1.0. The one thing a full graph would add, transitive
   caller/callee, is covered by a *one-hop closure* over the new-symbol set
   (~30 lines). The graph's remaining value does not justify its cost yet.
2. **No embedding first.** Deterministic stdlib signals carry 12/12 recall. The
   one case they cannot rank first (`check_palindrome` vs `is_palindrome` —
   same I/O, different algorithm) is exactly where semantic recall is needed,
   but that is a *ranking* refinement on top of a solved recall problem, not a
   missing foundation.
3. **Recall-first, precision-later.** Aggressive normalization over-surfaces on
   purpose: the `KEEP_SEPARATE` cases rank 1 because the reviewer must *see*
   the candidate to reject it. False-consolidation rate is a separate metric
   that needs the adjudication layer.
4. **Lightweight at runtime.** The query path stays stdlib-only, zero LLM. If
   semantic recall is added later it must be: offline LLM summary per symbol,
   runtime embedding similarity only — LLM never on the query path.

## The honest boundary

Recall@10 is 1.0, but Recall@1 is 0.75. Three cases rank below the top slot:
control-flow rewrite, repository bypass, and (rank 10) a different-algorithm
palindrome. The last is the deterministic ceiling: same I/O contract, genuinely
different algorithm, no shared calls/literals/structure. It needs semantics —
and, more honestly, it is also the kind of "duplication" a human would hesitate
to call reuse.

## What "landing" means

The reuse check is callable today:

```bash
python -m capability_retrieval --root <existing-codebase> --file <new-file>
```

Next landing steps, in cost order:

1. **Pre-write stub mode** — the agent writes a stub (name + docstring), the
   CLI retrieves before implementation.
2. **Wire into the agent skill** — a reuse check precedes implementation in the
   agent workflow.
3. **(Optional) MCP tool** — agent-native invocation, only after the shell path
   proves out.

Each is cheap; do them only as the workflow proves out.
