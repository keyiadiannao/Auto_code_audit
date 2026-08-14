## What changed

<!-- One or two sentences: what and why. -->

## Evidence

- [ ] Tests pass: `python -m pytest tests -q`
- [ ] Type check passes: `python -m mypy <changed files>` (CI runs the full set)
- [ ] Scanner changes: added/updated a fixture in `tests/test_scanners.py`, and
      `python benchmarks/run_mutation.py` still reports recall 1.000
- [ ] Acceptance-gate changes: added/updated a test in `tests/test_verify.py`
- [ ] Skill protocol changes: `python tools/check_skill_sync.py` passes
- [ ] Docs updated (`README.md` / `README.zh-CN.md` / `BENCHMARKS.md`) if behavior changed

## Notes for the reviewer

<!-- Anything that is not obvious from the diff, e.g. a deliberate
     non-atomic write you left for a follow-up. -->
