# Changelog

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
