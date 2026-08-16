"""Frozen-JSON provenance-lock awareness in duplicate/fork scanners."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR))

import scan_duplicates
import scan_forks
from _scanner_common import discover_locked_files


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def mini_repo(tmp_path: Path) -> Path:
    package = tmp_path / "pkg"
    _write(package / "lib" / "__init__.py", "")
    _write(package / "experiments" / "__init__.py", "")
    return tmp_path


def _locked_manifest(root: Path, mapping: dict[str, str], name: str = "frozen.json") -> Path:
    """Write a frozen provenance manifest mapping source paths to digests."""
    path = root / "configs" / name
    _write(
        path,
        json.dumps(
            {
                "audit_id": "test-compat-v1",
                "current_dependency_files": mapping,
                "current_dependency_fingerprint_sha256": "a" * 64,
                "reference_checkpoint_sha256": "b" * 64,
            },
            indent=2,
        ),
    )
    return path


DUPLICATE_BODY = """
def {name}(values, scale=1):
    total = 0
    count = len(values)
    for value in values:
        total += value * scale
    if total > 10:
        return total / count
    return total
"""


def test_discover_locked_files_parses_manifest_and_drops_missing(tmp_path: Path) -> None:
    _write(tmp_path / "lib" / "protocol.py", "def a():\n    pass\n")
    _write(
        tmp_path / "configs" / "locked.json",
        json.dumps(
            {
                "files_sha256": {
                    "lib/protocol.py": "9" * 64,
                    "mechanism/_ring_utils.py": "8" * 64,
                }
            }
        ),
    )
    # A metadata value that also looks like a digest must not become a lock.
    _write(
        tmp_path / "configs" / "meta.json",
        json.dumps({"split_hash": "c" * 64, "reference_checkpoint_sha256": "d" * 64}),
    )
    locked = discover_locked_files(tmp_path)
    # mechanism/_ring_utils.py does not exist on disk -> dropped
    assert locked == {"lib/protocol.py": ["configs/locked.json"]}


def test_discover_locked_files_ignores_excluded_trees(tmp_path: Path) -> None:
    _write(tmp_path / "lib" / "model.py", "def a():\n    pass\n")
    _write(
        tmp_path / "reports" / "frozen.json",
        json.dumps({"files_sha256": {"lib/model.py": "e" * 64}}),
    )
    assert discover_locked_files(tmp_path) == {}


def test_discover_locked_files_ignores_run_output_snapshots(tmp_path: Path) -> None:
    """Run-metadata under outputs/ is a snapshot, not an edit constraint.

    A frozen probe summary may lock ``mechanism/_ring_utils.py`` while the
    probe runner itself only appears in run metadata; the runner must not be
    flagged locked (it is safe to edit without regenerating frozen results).
    """
    _write(tmp_path / "mechanism" / "_ring_utils.py", "def f():\n    pass\n")
    _write(tmp_path / "mechanism" / "probe_x.py", "def g():\n    pass\n")
    _write(
        tmp_path / "frozen_results" / "r1_summary.json",
        json.dumps({"analysis_provenance": {"files_sha256": {
            "mechanism/_ring_utils.py": "a" * 64,
        }}}),
    )
    _write(
        tmp_path / "outputs" / "runs" / "run_metadata.json",
        json.dumps({"files_sha256": {
            "mechanism/_ring_utils.py": "a" * 64,
            "mechanism/probe_x.py": "c" * 64,
        }}),
    )
    locked = discover_locked_files(tmp_path)
    # probe_x.py is pinned only by the run snapshot -> not locked;
    # _ring_utils.py is pinned by the frozen summary -> locked.
    assert "mechanism/_ring_utils.py" in locked
    assert "mechanism/probe_x.py" not in locked


def test_duplicate_cluster_marks_locked_member(
    mini_repo: Path, tmp_path: Path
) -> None:
    _write(mini_repo / "pkg" / "lib" / "shared.py", DUPLICATE_BODY.format(name="shared"))
    _write(mini_repo / "pkg" / "experiments" / "a.py", DUPLICATE_BODY.format(name="alpha"))
    _write(mini_repo / "pkg" / "experiments" / "b.py", DUPLICATE_BODY.format(name="beta"))
    # Lock the shared implementation: pkg-relative member path is lib/shared.py,
    # so the manifest key must match the scanner's package-relative path.
    _locked_manifest(mini_repo, {"pkg/lib/shared.py": "1" * 64})

    output = tmp_path / "duplicates-locked.json"
    assert scan_duplicates.main(
        [
            "--root",
            str(mini_repo),
            "--package",
            "pkg",
            "--threshold",
            "0.95",
            "--min-chars",
            "0",
            "--json",
            str(output),
        ]
    ) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["locked_files"]["count"] == 1
    assert payload["locked_files"]["sources"] == ["configs/frozen.json"]
    cluster = next(item for item in payload["clusters"] if item["size"] == 3)
    shared_member = next(m for m in cluster["members"] if m["path"] == "lib/shared.py")
    assert shared_member["locked"] is True
    assert shared_member["locked_by"] == ["configs/frozen.json"]
    assert cluster["locked_members"] == ["lib/shared.py:shared"]
    unshared = [m for m in cluster["members"] if not m.get("locked")]
    assert len(unshared) == 2
    assert all("locked" not in m for m in unshared)


def test_duplicate_manifest_without_lock_has_empty_summary(
    mini_repo: Path, tmp_path: Path
) -> None:
    _write(mini_repo / "pkg" / "experiments" / "a.py", DUPLICATE_BODY.format(name="alpha"))
    _write(mini_repo / "pkg" / "experiments" / "b.py", DUPLICATE_BODY.format(name="beta"))
    output = tmp_path / "duplicates-no-lock.json"
    assert scan_duplicates.main(
        [
            "--root",
            str(mini_repo),
            "--package",
            "pkg",
            "--threshold",
            "0.95",
            "--min-chars",
            "0",
            "--json",
            str(output),
        ]
    ) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["locked_files"] == {"count": 0, "sources": []}
    assert all(not c.get("locked_members") for c in payload["clusters"])


FORK_LEFT = """
def run_probe(model, loader, device, steps):
    model.eval()
    total = 0
    correct = 0
    for x, y in loader:
        x = x.to(device)
        y = y.to(device)
        out = model(x)
        pred = out.argmax(dim=1)
        total += y.size(0)
        correct += (pred == y).sum().item()
    acc = correct / max(total, 1)
    return {"acc": acc, "steps": steps}
"""

FORK_RIGHT = """
def run_probe2(model, loader, device, steps, tag="a"):
    model.eval()
    total = 0
    correct = 0
    for x, y in loader:
        x = x.to(device)
        y = y.to(device)
        out = model(x)
        pred = out.argmax(dim=1)
        total += y.size(0)
        correct += (pred == y).sum().item()
    acc = correct / max(total, 1)
    return {"acc": acc, "steps": steps, "tag": tag}
"""


def test_fork_pair_marks_locked_side(mini_repo: Path, tmp_path: Path) -> None:
    _write(mini_repo / "pkg" / "probe_a.py", FORK_LEFT)
    _write(mini_repo / "pkg" / "probe_b.py", FORK_RIGHT)
    _locked_manifest(mini_repo, {"pkg/probe_a.py": "2" * 64})

    output = tmp_path / "forks-locked.json"
    assert scan_forks.main(
        [
            "--root",
            str(mini_repo),
            "--package",
            "pkg",
            "--min-lines",
            "8",
            "--small-floor",
            "5",
            "--small-threshold",
            "0.95",
            "--threshold",
            "0.8",
            "--json",
            str(output),
        ]
    ) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["locked_files"]["count"] == 1
    all_pairs = payload["pairs"] + payload["small_function_pairs"]
    assert all_pairs, "expected at least one fork pair"
    pair = next(item for item in all_pairs if item["kind"] == "fork")
    left_locked = [p for p in all_pairs if p["left"].get("locked")]
    assert left_locked, "expected the locked probe_a side to be annotated"
    assert left_locked[0]["left"]["locked_by"] == ["configs/frozen.json"]
    right_locked = [p for p in all_pairs if p["right"].get("locked")]
    # probe_b is not locked, so no pair should have both sides locked
    assert not any(p["left"].get("locked") and p["right"].get("locked") for p in all_pairs)
    assert right_locked == []
