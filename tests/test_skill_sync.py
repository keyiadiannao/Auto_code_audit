"""Contract-drift guard: the platform skill mirror must carry every
binding token the root skill does (tools/check_skill_sync.py)."""
from __future__ import annotations

from tools.check_skill_sync import (
    ADAPTER_SKILL,
    CONTRACT_TOKENS,
    ROOT_SKILL,
    missing_tokens,
)


def test_both_skill_copies_carry_all_contract_tokens() -> None:
    assert missing_tokens(ROOT_SKILL) == []
    assert missing_tokens(ADAPTER_SKILL) == []


def test_checker_detects_missing_token(tmp_path) -> None:
    copy = tmp_path / "SKILL.md"
    copy.write_text("fully_verified=True\n", encoding="utf-8")
    missing = missing_tokens(copy)
    assert "--root" in missing
    assert "source_tree_sha256" in missing
    assert "fully_verified=True" not in missing


def test_checker_reports_unreadable_copy(tmp_path) -> None:
    missing = missing_tokens(tmp_path / "does-not-exist.md")
    assert any("unreadable" in token for token in missing)
