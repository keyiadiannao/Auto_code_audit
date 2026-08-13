# Changelog

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
