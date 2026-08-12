#!/usr/bin/env python3
"""Contract-drift check between the root skill and platform skill copies.

The audit protocol lives once (``SKILL.md`` at the repo root) and is
mirrored into platform adapter copies (``.agents/skills/auto-code-audit/
SKILL.md``).  A mirror can drift silently — the acceptance-gate wording
lags the engine.  CI runs this checker so the binding contract tokens —
the fingerprints, flags, and hash names the acceptance gate depends on —
must appear in every copy.  A missing token fails the check; the files are
otherwise free to diverge in wording.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT_SKILL = Path(__file__).resolve().parents[1] / "SKILL.md"
ADAPTER_SKILL = (
    Path(__file__).resolve().parents[1]
    / ".agents"
    / "skills"
    / "auto-code-audit"
    / "SKILL.md"
)

#: Tokens the acceptance protocol depends on; every platform copy must
#: mention each of them.
CONTRACT_TOKENS = (
    "fully_verified=True",
    "--root",
    "source_tree_sha256",
    "audit_inputs_sha256",
    "audit_config_hash",
    "scanner_bundle_hash",
    "case_hash",
    "finding_evidence_hash",
)


def missing_tokens(path: Path) -> list[str]:
    """Return the contract tokens absent from ``path``'s text."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"(unreadable: {exc})"]
    return [token for token in CONTRACT_TOKENS if token not in text]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root_path = Path(argv[0]) if argv else ROOT_SKILL
    adapter_path = Path(argv[1]) if len(argv) > 1 else ADAPTER_SKILL
    problems: list[str] = []
    for label, path in (("root", root_path), ("adapter", adapter_path)):
        for token in missing_tokens(path):
            problems.append(f"{label} skill ({path}) is missing {token!r}")
    if problems:
        print("skill drift detected:", file=sys.stderr)
        for message in problems:
            print(f"  - {message}", file=sys.stderr)
        return 1
    print(
        f"skill sync ok: all {len(CONTRACT_TOKENS)} contract tokens present "
        f"in {root_path} and {adapter_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
