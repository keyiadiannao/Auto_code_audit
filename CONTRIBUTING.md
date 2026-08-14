# Contributing

Thanks for considering a contribution. This project is a deterministic audit
tool, so its own changes follow the same discipline it enforces: **evidence
first, never auto-delete, verify before accept.**

## Ground rules

- A static hit is never a bug. Never change or delete code from a scanner
  candidate alone; adjudication always precedes modification.
- Do not merge scanner candidates into the ground-truth corpus without a human
  review marker (`human_verified: true`, reviewer, version) — see
  `benchmarks/labels/*.json`.
- Keep the root `SKILL.md` and the `.agents/skills/auto-code-audit/SKILL.md`
  adapter in sync; CI runs `python tools/check_skill_sync.py` to enforce the
  binding contract tokens.

## Local verification

```bash
# test suite (stdlib scanners + pytest for fixtures)
python -m pytest tests -q

# the same type-check CI runs (blocking)
python -m mypy run_all.py adjudicate.py run_verify.py _audit_config.py \
  _scanner_common.py _verdict_files.py report_formatter.py \
  scan_regions.py scan_capabilities.py scan_contracts.py scan_deadcode.py \
  scan_duplicates.py scan_forks.py scan_hardcoded.py scan_style.py scan_cli_smoke.py

# skill adapter drift check
python tools/check_skill_sync.py

# scanner CLI smoke (run --help on every entrypoint)
python scan_cli_smoke.py
```

Python 3.10+ only; the scanners use the standard library.

## What a change should include

- **Scanner change**: add or update a fixture in `tests/test_scanners.py` that
  pins the new behavior, and re-run the mutation corpus
  (`python benchmarks/run_mutation.py`) — recall must stay 1.000.
- **Acceptance-gate change** (`run_verify.py`): add a test in
  `tests/test_verify.py` for the new pass/fail path. A PASS must stay
  fail-closed: never widen what the gate accepts without a test.
- **Report/label change**: keep `BENCHMARKS.md` numbers consistent with the
  committed labels; do not hand-edit `benchmarks/gold_manifest.json` — regenerate
  it with `benchmarks/gold_sample.py`.

## Style

- Match the surrounding code; keep functions small and evidence-oriented.
- Commit messages should say *what* and *why*, not just *what*.

## Questions

Open an issue. If you found a security problem, see `SECURITY.md` first.
