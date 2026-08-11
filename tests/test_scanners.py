from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR))

import adjudicate
import run_all
import scan_capabilities
import scan_cli_smoke
import scan_contracts
import scan_deadcode
import scan_duplicates
import scan_forks
import scan_hardcoded
import scan_style


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def mini_repo(tmp_path: Path) -> Path:
    package = tmp_path / "pkg"
    _write(package / "lib" / "__init__.py", "")
    _write(package / "experiments" / "__init__.py", "")
    return tmp_path


def test_duplicate_scan_is_stable_and_finds_shared_member(
    mini_repo: Path, tmp_path: Path
) -> None:
    body = """
def {name}(values, scale=1):
    total = 0
    count = len(values)
    for value in values:
        total += value * scale
    if total > 10:
        return total / count
    return total
"""
    _write(mini_repo / "pkg" / "lib" / "shared.py", body.format(name="shared"))
    _write(mini_repo / "pkg" / "experiments" / "a.py", body.format(name="alpha"))
    _write(mini_repo / "pkg" / "experiments" / "b.py", body.format(name="beta"))

    output = tmp_path / "duplicates.json"
    rc = scan_duplicates.main(
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
    )
    assert rc == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    cluster = next(item for item in payload["clusters"] if item["size"] == 3)
    assert len(cluster["id"]) == 12
    assert cluster["priority"] == "high"
    assert cluster["lib_shared"] == [
        {"path": "lib/shared.py", "name": "shared", "qualname": "shared"}
    ]
    assert [item["path"] for item in cluster["members"]] == sorted(
        item["path"] for item in cluster["members"]
    )


def test_short_shared_duplicate_stays_low_priority(
    mini_repo: Path, tmp_path: Path
) -> None:
    body = """
def {name}(values):
    total = 0
    for value in values:
        total += value
    if total > 10:
        return total
    return 0
"""
    _write(mini_repo / "pkg" / "lib" / "shared.py", body.format(name="shared"))
    _write(mini_repo / "pkg" / "experiments" / "a.py", body.format(name="alpha"))

    output = tmp_path / "duplicates-short.json"
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
    cluster = next(item for item in payload["clusters"] if item["size"] == 2)
    assert cluster["max_lines"] == 7
    assert cluster["priority"] == "low"


def test_duplicate_min_chars_is_honored(mini_repo: Path, tmp_path: Path) -> None:
    _write(mini_repo / "pkg" / "experiments" / "short.py", "def tiny(x):\n    return x\n")
    output = tmp_path / "duplicates.json"
    assert (
        scan_duplicates.main(
            [
                "--root",
                str(mini_repo),
                "--package",
                "pkg",
                "--min-chars",
                "10000",
                "--json",
                str(output),
            ]
        )
        == 0
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["functions_scanned"] == 0


def test_scanners_accept_utf8_bom_entrypoint(mini_repo: Path, tmp_path: Path) -> None:
    path = mini_repo / "pkg" / "experiments" / "bom_cli.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "def main():\n    return 0\n\nif __name__ == '__main__':\n    raise SystemExit(main())\n",
        encoding="utf-8-sig",
    )
    dead_output = tmp_path / "deadcode.json"
    assert scan_deadcode.main(
        [
            "--root",
            str(mini_repo),
            "--package",
            "pkg",
            "--no-doc-channel",
            "--json",
            str(dead_output),
        ]
    ) == 0
    payload = json.loads(dead_output.read_text(encoding="utf-8"))
    row = next(item for item in payload["modules"] if item["path"].endswith("bom_cli.py"))
    assert row["status"] == "ENTRYPOINT"
    assert payload["parse_failures"] == {}

    duplicate_output = tmp_path / "duplicates.json"
    assert scan_duplicates.main(
        [
            "--root",
            str(mini_repo),
            "--package",
            "pkg",
            "--min-chars",
            "0",
            "--json",
            str(duplicate_output),
        ]
    ) == 0
    duplicate_payload = json.loads(duplicate_output.read_text(encoding="utf-8"))
    assert duplicate_payload["parse_failures"] == []


def test_deadcode_distinguishes_imports_entrypoints_and_orphans(
    mini_repo: Path, tmp_path: Path
) -> None:
    package = mini_repo / "pkg"
    _write(package / "lib" / "used.py", "VALUE = 1\n")
    _write(package / "experiments" / "consumer.py", "from lib import used\nprint(used.VALUE)\n")
    _write(
        package / "experiments" / "cli.py",
        "def main():\n    return 0\n\nif __name__ == '__main__':\n    raise SystemExit(main())\n",
    )
    _write(package / "experiments" / "orphan.py", "VALUE = 2\n")

    output = tmp_path / "deadcode.json"
    assert (
        scan_deadcode.main(
            [
                "--root",
                str(mini_repo),
                "--package",
                "pkg",
                "--no-doc-channel",
                "--json",
                str(output),
            ]
        )
        == 0
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    statuses = {item["path"]: item["status"] for item in payload["modules"]}
    assert statuses["lib/__init__.py"] == "PACKAGE"
    assert statuses["lib/used.py"] == "USED"
    assert statuses["experiments/cli.py"] == "ENTRYPOINT"
    assert statuses["experiments/orphan.py"] == "DEAD"
    used = next(item for item in payload["modules"] if item["path"] == "lib/used.py")
    assert used["py_refs"] == ["experiments/consumer.py"]


def test_deadcode_sees_syspath_bare_import(
    mini_repo: Path, tmp_path: Path
) -> None:
    package = mini_repo / "pkg"
    _write(package / "experiments" / "dep.py", "VALUE = 7\n")
    _write(
        package / "experiments" / "user.py",
        "import sys\n"
        "from pathlib import Path\n"
        "REPO = Path(__file__).resolve().parents[2]\n"
        "sys.path.insert(0, str(REPO / 'pkg' / 'experiments'))\n"
        "import dep\n"
        "print(dep.VALUE)\n",
    )
    output = tmp_path / "deadcode.json"
    assert (
        scan_deadcode.main(
            [
                "--root",
                str(mini_repo),
                "--package",
                "pkg",
                "--no-doc-channel",
                "--json",
                str(output),
            ]
        )
        == 0
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    statuses = {item["path"]: item["status"] for item in payload["modules"]}
    assert statuses["experiments/dep.py"] == "USED"
    dep = next(
        item for item in payload["modules"] if item["path"] == "experiments/dep.py"
    )
    assert any(
        edge["mechanism"] == "syspath_bare_import" for edge in dep["dynamic_edges"]
    )


def test_deadcode_sees_importlib_file_load(mini_repo: Path, tmp_path: Path) -> None:
    package = mini_repo / "pkg"
    _write(package / "experiments" / "dep.py", "VALUE = 7\n")
    _write(
        package / "experiments" / "user.py",
        "import importlib.util\n"
        "from pathlib import Path\n"
        "HERE = Path(__file__).resolve().parent\n"
        "spec = importlib.util.spec_from_file_location('dep', HERE / 'dep.py')\n"
        "dep = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(dep)\n",
    )
    output = tmp_path / "deadcode.json"
    assert (
        scan_deadcode.main(
            [
                "--root",
                str(mini_repo),
                "--package",
                "pkg",
                "--no-doc-channel",
                "--json",
                str(output),
            ]
        )
        == 0
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    statuses = {item["path"]: item["status"] for item in payload["modules"]}
    assert statuses["experiments/dep.py"] == "USED"
    dep = next(
        item for item in payload["modules"] if item["path"] == "experiments/dep.py"
    )
    assert dep["dynamic_edges"][0]["mechanism"] == "importlib_file"
    assert dep["dynamic_edges"][0]["lineno"] == 4


def test_deadcode_resolves_exact_module_name_hint(
    mini_repo: Path, tmp_path: Path
) -> None:
    package = mini_repo / "pkg"
    _write(package / "experiments" / "dep.py", "VALUE = 7\n")
    _write(
        package / "experiments" / "user.py",
        "import importlib\n"
        "dep = importlib.import_module('pkg.experiments.dep')\n"
        "print(dep.VALUE)\n",
    )
    output = tmp_path / "deadcode.json"
    assert (
        scan_deadcode.main(
            [
                "--root",
                str(mini_repo),
                "--package",
                "pkg",
                "--no-doc-channel",
                "--json",
                str(output),
            ]
        )
        == 0
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    statuses = {item["path"]: item["status"] for item in payload["modules"]}
    assert statuses["experiments/dep.py"] == "USED"
    dep = next(
        item for item in payload["modules"] if item["path"] == "experiments/dep.py"
    )
    assert dep["dynamic_edges"][0]["mechanism"] == "importlib_module"


def test_hardcoded_scan_uses_ignore_pair_suppression(
    mini_repo: Path, tmp_path: Path
) -> None:
    package = mini_repo / "pkg"
    _write(
        package / "lib" / "hash_utils.py",
        "import hashlib\n\ndef sha(data):\n    return hashlib.sha256(data).hexdigest()\n",
    )
    _write(
        package / "experiments" / "bad.py",
        "import hashlib\n\ndef sha(data):\n    return hashlib.sha256(data).hexdigest()\n",
    )
    _write(
        package / "mechanism" / "readout.py",
        "def read(model, states):\n    return model.norm_f(states.mean(dim=1))\n",
    )
    ignore = tmp_path / "ignore.json"
    ignore.write_text(
        json.dumps(
            {
                "hardcoded": [
                    {
                        "path": "lib/hash_utils.py",
                        "pattern": "manual_sha256",
                        "reason": "canonical implementation owns the pattern",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    output = tmp_path / "hardcoded.json"
    assert (
        scan_hardcoded.main(
            [
                "--root",
                str(mini_repo),
                "--package",
                "pkg",
                "--ignore",
                str(ignore),
                "--json",
                str(output),
            ]
        )
        == 0
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    hash_hits = payload["hits"]["manual_sha256"]
    assert [item["path"] for item in hash_hits] == ["experiments/bad.py"]
    readout_hits = payload["hits"]["readout_pool_then_ln_inline"]
    assert [item["path"] for item in readout_hits] == ["mechanism/readout.py"]
    assert [item["path"] for item in payload["ignored"]] == ["lib/hash_utils.py"]


def test_contract_scan_exposes_boundaries_wrappers_and_name_collisions(
    mini_repo: Path, tmp_path: Path
) -> None:
    package = mini_repo / "pkg"
    _write(
        package / "experiments" / "runner.py",
        "def evaluate_provider(model, data):\n    return {'accuracy': 1.0}\n",
    )
    _write(
        package / "lib" / "shared.py",
        "from pkg.experiments import runner\n\n"
        "def evaluate_provider(model, data, batch_size=32):\n"
        "    return 1.0\n\n"
        "def legacy(*args, **kwargs):\n"
        "    return evaluate_provider(*args, **kwargs)\n",
    )
    output = tmp_path / "contracts.json"
    assert scan_contracts.main(
        [
            "--root",
            str(mini_repo),
            "--package",
            "pkg",
            "--json",
            str(output),
        ]
    ) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["counts"] == {
        "experiment_as_library": 1,
        "experiment_path_hacks": 0,
        "forwarding_wrappers": 1,
        "same_name_contracts": 1,
        "unreferenced_top_level_functions": 1,
        "cli_without_bootstrap": 0,
        "defensive_param_loosening": 0,
        "env_written_not_read": 0,
        "generation_path_without_env": 0,
    }
    assert payload["experiment_as_library"][0]["path"] == "lib/shared.py"
    assert payload["forwarding_wrappers"][0]["target"] == "evaluate_provider"
    collision = payload["same_name_contracts"][0]
    assert collision["name"] == "evaluate_provider"
    assert {row["path"] for row in collision["definitions"]} == {
        "experiments/runner.py",
        "lib/shared.py",
    }
    unused = {
        (row["path"], row["name"])
        for row in payload["unreferenced_top_level_functions"]
    }
    assert unused == {("lib/shared.py", "legacy")}


def test_contract_scan_flags_cli_scripts_without_syspath_bootstrap(
    mini_repo: Path, tmp_path: Path
) -> None:
    package = mini_repo / "pkg"
    _write(package / "lib" / "protocol.py", "def load():\n    return 1\n")
    _write(
        package / "experiments" / "good.py",
        "import sys\n"
        "from pathlib import Path\n"
        "REPO = Path(__file__).resolve().parents[2]\n"
        "sys.path.insert(0, str(REPO))\n"
        "from pkg.lib.protocol import load\n"
        "def main():\n    return load()\n",
    )
    _write(
        package / "experiments" / "bad.py",
        "from pkg.lib.protocol import load\n"
        "def main():\n    return load()\n",
    )
    _write(
        package / "audit" / "also_bad.py",
        "import pkg.lib.protocol\n"
        "def main():\n    return 0\n",
    )
    # lib/ and tests/ are never executed directly: bootstrap not required.
    _write(
        package / "lib" / "internal.py",
        "from pkg.lib.protocol import load\n"
        "def helper():\n    return load()\n",
    )
    _write(package / "tests" / "test_x.py", "from pkg.lib.protocol import load\n")

    output = tmp_path / "contracts-bootstrap.json"
    assert scan_contracts.main(
        [
            "--root",
            str(mini_repo),
            "--package",
            "pkg",
            "--json",
            str(output),
        ]
    ) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert {item["path"] for item in payload["cli_without_bootstrap"]} == {
        "experiments/bad.py",
        "audit/also_bad.py",
    }
    assert payload["counts"]["cli_without_bootstrap"] == 2
    assert payload["schema_version"] == 4


def test_contract_scan_flags_defensive_param_loosening(
    mini_repo: Path, tmp_path: Path
) -> None:
    package = mini_repo / "pkg"
    _write(
        package / "experiments" / "loose.py",
        "import torch\n"
        "def load(model, state):\n"
        "    model.load_state_dict(state, strict=False)\n"
        "    return torch.load('x.pt', weights_only=False)\n",
    )
    _write(
        package / "experiments" / "tight.py",
        "def load(model, state):\n"
        "    model.load_state_dict(state, strict=True)\n"
        "    return torch.load('x.pt', weights_only=True)\n",
    )
    output = tmp_path / "contracts-defensive.json"
    assert scan_contracts.main(
        [
            "--root",
            str(mini_repo),
            "--package",
            "pkg",
            "--json",
            str(output),
        ]
    ) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert len(payload["defensive_param_loosening"]) == 2
    kinds = {(item["path"], item["kind"]) for item in payload["defensive_param_loosening"]}
    assert kinds == {
        ("experiments/loose.py", "strict_false"),
        ("experiments/loose.py", "weights_only_false"),
    }


def test_contract_scan_flags_env_contract_candidates(
    mini_repo: Path, tmp_path: Path
) -> None:
    package = mini_repo / "pkg"
    _write(
        package / "experiments" / "writer.py",
        "import os\n"
        "os.environ['PKG_MODE_UNREAD'] = 'gen'\n",
    )
    _write(
        package / "lib" / "reader.py",
        "import os\n"
        "def root():\n"
        "    return os.environ.get('PKG_MODE')\n",
    )
    _write(
        package / "experiments" / "hardcoded.py",
        "def gen_path():\n"
        "    return 'artifacts/generation_a/outputs'\n",
    )
    _write(
        package / "experiments" / "envgated.py",
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
        "REPO = Path(__file__).resolve().parents[2]\n"
        "sys.path.insert(0, str(REPO))\n"
        "def gen_path():\n"
        "    return os.environ.get('PKG_ROOT', 'artifacts/generation_a')\n",
    )
    output = tmp_path / "contracts-env.json"
    assert scan_contracts.main(
        [
            "--root",
            str(mini_repo),
            "--package",
            "pkg",
            "--json",
            str(output),
        ]
    ) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert [item["var"] for item in payload["env_written_not_read"]] == [
        "PKG_MODE_UNREAD"
    ]
    # generation-pinned path with no env read fires; env-gated fallback does not.
    assert [item["path"] for item in payload["generation_path_without_env"]] == [
        "experiments/hardcoded.py"
    ]


def test_contract_scan_ignore_registry_suppresses_channels(
    mini_repo: Path, tmp_path: Path
) -> None:
    package = mini_repo / "pkg"
    _write(
        package / "experiments" / "writer.py",
        "import os\n"
        "os.environ['PKG_MODE_UNREAD'] = 'gen'\n",
    )
    _write(
        package / "experiments" / "hardcoded.py",
        "def gen_path():\n"
        "    return 'artifacts/generation_a/outputs'\n",
    )
    _write(
        package / "experiments" / "cli_main.py",
        "from pkg.lib import root\n"
        "def main():\n"
        "    return root()\n",
    )
    _write(
        package / "experiments" / "loose.py",
        "def load(p):\n"
        "    return torch.load(p, weights_only=False)\n",
    )
    ignore = tmp_path / "ignore.json"
    ignore.write_text(
        json.dumps(
            {
                "contracts": {
                    "env_written_not_read": [
                        {"key": "experiments/writer.py:PKG_MODE_UNREAD"}
                    ],
                    "generation_path_without_env": [
                        {"key": "experiments/hardcoded.py"}
                    ],
                    "cli_without_bootstrap": [
                        {"key": "experiments/cli_main.py"}
                    ],
                    "defensive_param_loosening": [
                        {"key": "experiments/loose.py:2"}
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "contracts-ignore.json"
    assert scan_contracts.main(
        [
            "--root",
            str(mini_repo),
            "--package",
            "pkg",
            "--ignore",
            str(ignore),
            "--json",
            str(output),
        ]
    ) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["env_written_not_read"] == []
    assert payload["generation_path_without_env"] == []
    assert payload["cli_without_bootstrap"] == []
    assert payload["defensive_param_loosening"] == []
    assert payload["ignored_counts"] == {
        "cli_without_bootstrap": 1,
        "defensive_param_loosening": 1,
        "env_written_not_read": 1,
        "generation_path_without_env": 1,
        "experiment_as_library": 0,
        "forwarding_wrappers": 0,
        "same_name_contracts": 0,
        "unreferenced_top_level_functions": 0,
    }


def test_contract_scan_ignores_docstring_generation_mentions(
    mini_repo: Path, tmp_path: Path
) -> None:
    package = mini_repo / "pkg"
    _write(
        package / "experiments" / "docs_only.py",
        '"""Checkpoint root: artifacts/generation_a/outputs."""\n'
        "def gen_path():\n"
        "    return 'artifacts/generation_a/outputs'\n",
    )
    _write(
        package / "experiments" / "prose_only.py",
        '"""Generation-A analysis script; no pinned paths inside."""\n'
        "def nothing():\n"
        "    return 1\n",
    )
    output = tmp_path / "contracts-docstring.json"
    assert scan_contracts.main(
        [
            "--root",
            str(mini_repo),
            "--package",
            "pkg",
            "--json",
            str(output),
        ]
    ) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    # Real path constants still fire; docstring-only prose does not.
    assert [item["path"] for item in payload["generation_path_without_env"]] == [
        "experiments/docs_only.py"
    ]


def test_fork_scan_small_function_channel(mini_repo: Path, tmp_path: Path) -> None:
    small = (
        "def {name}(rows, fn):\n"
        "    vals = []\n"
        "    for r in rows:\n"
        "        try:\n"
        "            v = fn(r)\n"
        "            if v is not None and v == v:\n"
        "                vals.append(float(v))\n"
        "        except (KeyError, TypeError, ValueError):\n"
        "            pass\n"
        "    if not vals:\n"
        "        return {{'mean': float('nan'), 'n': 0}}\n"
        "    return {{'mean': float(np.mean(vals)), 'n': len(vals)}}\n"
    )
    _write(
        mini_repo / "pkg" / "experiments" / "a.py",
        small.format(name="_agg"),
    )
    _write(
        mini_repo / "pkg" / "experiments" / "b.py",
        small.format(name="_agg"),
    )
    _write(
        mini_repo / "pkg" / "experiments" / "c.py",
        small.format(name="different"),
    )
    _write(
        mini_repo / "pkg" / "experiments" / "big.py",
        _fork_body("analyze_big", ""),
    )
    output = tmp_path / "forks-small.json"
    assert scan_forks.main(
        [
            "--root",
            str(mini_repo),
            "--package",
            "pkg",
            "--json",
            str(output),
        ]
    ) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    # Three cross-file pairs among the identical 17-line helpers; the 40-line
    # main channel sees none of them and the big function stays out of small.
    assert payload["small_channel"]["pairs"] == 3
    assert all(item["channel"] == "small" for item in payload["small_function_pairs"])
    assert all(
        item["left"]["nlines"] < 40 for item in payload["small_function_pairs"]
    )
    assert all(
        item["right"]["nlines"] < 40 for item in payload["small_function_pairs"]
    )
    assert all(
        item["left"]["path"] != "experiments/big.py"
        and item["right"]["path"] != "experiments/big.py"
        for item in payload["small_function_pairs"]
    )
    assert payload["fork_pairs"] == 0


def test_run_all_writes_provenance_and_cleans_temporary_files(
    mini_repo: Path, tmp_path: Path
) -> None:
    package = mini_repo / "pkg"
    _write(package / "lib" / "helper.py", "def helper(x):\n    return x + 1\n")
    _write(
        package / "experiments" / "cli.py",
        "from lib.helper import helper\n\ndef main():\n    return helper(1)\n\nif __name__ == '__main__':\n    raise SystemExit(main())\n",
    )
    ignore = tmp_path / "ignore.json"
    ignore.write_text('{"deadcode": [], "duplicates": [], "hardcoded": []}', encoding="utf-8")
    output_json = tmp_path / "reports" / "audit.json"
    output_md = tmp_path / "reports" / "audit.md"

    assert (
        run_all.main(
            [
                "--root",
                str(mini_repo),
                "--package",
                "pkg",
                "--json",
                str(output_json),
                "--markdown",
                str(output_md),
                "--ignore",
                str(ignore),
                "--no-doc-channel",
            ]
        )
        == 0
    )
    payload = json.loads(output_json.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 5
    assert set(payload["provenance"]["scanner_sha256"]) == {
        "run_all.py",
        "scan_capabilities.py",
        "scan_contracts.py",
        "scan_deadcode.py",
        "scan_duplicates.py",
        "scan_forks.py",
        "scan_hardcoded.py",
        "scan_style.py",
    }
    assert "candidate list" in output_md.read_text(encoding="utf-8")
    assert not list(output_json.parent.glob("self-audit-*"))


def test_run_all_reports_changes_since_last_run(
    mini_repo: Path, tmp_path: Path
) -> None:
    report = tmp_path / "audit.json"
    markdown = tmp_path / "audit.md"
    argv = [
        "--root",
        str(mini_repo),
        "--package",
        "pkg",
        "--json",
        str(report),
        "--markdown",
        str(markdown),
        "--no-doc-channel",
    ]
    _write(mini_repo / "pkg" / "lib" / "unused.py", "def dead_a():\n    return 1\n")

    assert run_all.main(argv) == 0
    first = json.loads(report.read_text(encoding="utf-8"))
    assert first["previous_run"] is None  # first run has nothing to diff against

    # Replace the candidate: drop unused.py, add unused2.py.
    (mini_repo / "pkg" / "lib" / "unused.py").unlink()
    _write(mini_repo / "pkg" / "lib" / "unused2.py", "def dead_b():\n    return 1\n")
    assert run_all.main(argv) == 0
    second = json.loads(report.read_text(encoding="utf-8"))
    previous_run = second["previous_run"]
    assert previous_run["comparable"] is True
    deadcode_new = {
        item["signature"] for item in previous_run["per_scanner"]["deadcode"]["new"]
    }
    assert any("unused2" in sig for sig in deadcode_new)
    deadcode_gone = {
        item["signature"] for item in previous_run["per_scanner"]["deadcode"]["gone"]
    }
    assert any("unused.py" in sig for sig in deadcode_gone)

    md_text = markdown.read_text(encoding="utf-8")
    assert "## Changes since last run" in md_text
    assert "unused2" in md_text
    assert "| dead code |" in md_text


def test_run_all_previous_schema_mismatch_is_not_comparable(
    mini_repo: Path, tmp_path: Path
) -> None:
    report = tmp_path / "audit.json"
    markdown = tmp_path / "audit.md"
    _write(mini_repo / "pkg" / "lib" / "unused.py", "def dead_a():\n    return 1\n")
    argv = [
        "--root",
        str(mini_repo),
        "--package",
        "pkg",
        "--json",
        str(report),
        "--markdown",
        str(markdown),
        "--no-doc-channel",
    ]
    assert run_all.main(argv) == 0

    # Tamper with the previous report's schema so the next run must refuse.
    summary = json.loads(report.read_text(encoding="utf-8"))
    summary["schema_version"] = summary["schema_version"] - 1
    report.write_text(json.dumps(summary), encoding="utf-8")

    assert run_all.main(argv) == 0
    second = json.loads(report.read_text(encoding="utf-8"))
    previous_run = second["previous_run"]
    assert previous_run["comparable"] is False
    assert "schema_version" in previous_run["reason"]
    md_text = markdown.read_text(encoding="utf-8")
    assert "was not comparable" in md_text


def _run_style_scan(mini_repo: Path, tmp_path: Path, name: str):
    output = tmp_path / f"style-{name}.json"
    rc = scan_style.main(
        [
            "--root",
            str(mini_repo),
            "--package",
            "pkg",
            "--tex-dir",
            "docs/tex",
            "--json",
            str(output),
        ]
    )
    assert rc == 0
    return json.loads(output.read_text(encoding="utf-8"))


def test_capability_scan_marks_signature_match(
    mini_repo: Path, tmp_path: Path
) -> None:
    _write(
        mini_repo / "pkg" / "lib" / "shared.py",
        "def evaluate_provider(model, a, b, y, provider, batch_size):\n"
        "    return 1.0\n\n"
        "def resolve_seeds(tier):\n"
        "    return [1]\n",
    )
    _write(
        mini_repo / "pkg" / "experiments" / "runner.py",
        "def evaluate_provider(model, a, b, y, provider, batch_size):\n"
        "    return {'accuracy': 1.0}\n\n"
        "def resolve_seeds(group, arm, max_seeds):\n"
        "    return [0]\n",
    )
    output = tmp_path / "capabilities.json"
    assert scan_capabilities.main(
        [
            "--root",
            str(mini_repo),
            "--package",
            "pkg",
            "--json",
            str(output),
        ]
    ) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    overlap = {item["local"]["qualname"]: item for item in payload["overlap"]}
    assert set(overlap) == {"evaluate_provider", "resolve_seeds"}
    # Same name + same signature shape: strong duplicate signal.
    assert overlap["evaluate_provider"]["signature_match"] is True
    # Same name + different signature shape: contract variant / collision.
    assert overlap["resolve_seeds"]["signature_match"] is False
    # Empty docstrings are surfaced as registry-health gaps.
    assert len(payload["untagged_lib_capabilities"]) == 2


def test_capability_file_check_lists_lib_counterparts(
    mini_repo: Path, tmp_path: Path
) -> None:
    _write(
        mini_repo / "pkg" / "lib" / "shared.py",
        "def load_model(path):\n"
        "    return None\n",
    )
    _write(
        mini_repo / "pkg" / "experiments" / "probe.py",
        "def load_model(path, strict=False):\n"
        "    return None\n\n"
        "def only_here(x):\n"
        "    return x\n",
    )
    output = tmp_path / "file-check.json"
    assert scan_capabilities.main(
        [
            "--root",
            str(mini_repo),
            "--package",
            "pkg",
            "--file",
            "experiments/probe.py",
            "--json",
            str(output),
        ]
    ) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    by_name = {item["local"]["qualname"]: item for item in payload["definitions"]}
    assert by_name["load_model"]["mode"] == "name"
    assert by_name["load_model"]["lib_candidates"][0]["signature_match"] is False
    assert by_name["only_here"]["mode"] == "none"
    assert by_name["only_here"]["lib_candidates"] == []


def test_style_scan_flags_ai_signals_with_exact_line_numbers(
    mini_repo: Path, tmp_path: Path
) -> None:
    tex = (
        "% preamble comment\n"
        "\\documentclass{article}\n"
        "\\usepackage{amsmath}\n"
        "\\begin{document}\n"
        "The baseline is accurate.\n"
        "\n"
        "The signal is not just strong; it is robust; it is decisive.\n"
        "\n"
        "In summary, the mechanism localizes cleanly.\n"
        "\n"
        "\\end{document}\n"
    )
    _write(
        mini_repo / "docs" / "tex" / "paper_main.tex", tex
    )

    payload = _run_style_scan(mini_repo, tmp_path, "signals")
    chains = payload["hits"]["semicolon_chains"]
    assert len(chains) == 1
    assert chains[0]["line"] == 7  # source line of the semicolon sentence
    assert chains[0]["subclause_count"] == 3
    assert chains[0]["triad_like"] is True
    assert chains[0]["technical"] is False
    assert chains[0]["severity"] == "high"

    openers = payload["hits"]["template_openers"]
    assert len(openers) == 1
    assert openers[0]["line"] == 9  # source line of "In summary, ..."

    stats = payload["prose_stats"]["per_file"][
        "docs/tex/paper_main.tex"
    ]
    assert stats["words"] == 22
    assert stats["sentences"] == 3


def test_style_scan_technical_semicolon_chain_is_exempt_from_triad_verdict(
    mini_repo: Path, tmp_path: Path
) -> None:
    tex = (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "Across seeds: seed 0 reaches 0.95; seed 1 reaches 0.93; "
        "seed 2 reaches 0.91.\n"
        "\\end{document}\n"
    )
    _write(
        mini_repo / "docs" / "tex" / "paper_main.tex", tex
    )

    payload = _run_style_scan(mini_repo, tmp_path, "technical")
    chains = payload["hits"]["semicolon_chains"]
    assert len(chains) == 1
    assert chains[0]["technical"] is True
    assert chains[0]["severity"] == "medium"  # not the high triad verdict


def _fork_body(name: str, extra: str) -> str:
    return f"""def {name}(checkpoint, seed, val_samples):
    model = load_model(checkpoint)
    a, b, y = make_data(0.47)
    if val_samples > 0:
        a, b, y = a[:val_samples], b[:val_samples], y[:val_samples]
    components = build_components(model)
    entropy_scales = find_scales(a, b, components)
    providers = build_providers(components, entropy_scales)
    interventions = {{}}
    for head in range(4):
        interventions[head] = evaluate(model, a, b, y, providers[head])
        {extra}
    result = {{"checkpoint": str(checkpoint), "seed": seed,
              "interventions": interventions, "n_validation": len(a)}}
    del model
    return result
"""


def test_fork_scan_finds_diverged_cross_file_pairs(
    mini_repo: Path, tmp_path: Path
) -> None:
    _write(
        mini_repo / "pkg" / "experiments" / "a.py",
        _fork_body("analyze_a", "scale = entropy_scales[head]\n"),
    )
    _write(
        mini_repo / "pkg" / "experiments" / "b.py",
        _fork_body(
            "analyze_b",
            "scale = entropy_scales[head].mean().item()\n"
            "        scale = scale * 0.5\n"
            "        if scale > 0:\n"
            "            scale = scale.clamp(max=1.0)\n",
        ),
    )
    output = tmp_path / "forks.json"
    assert scan_forks.main(
        [
            "--root",
            str(mini_repo),
            "--package",
            "pkg",
            "--min-lines",
            "12",
            "--json",
            str(output),
        ]
    ) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["fork_pairs"] == 1
    pair = payload["pairs"][0]
    assert pair["kind"] == "fork"  # partial divergence, not near-identical
    assert 0.75 <= pair["similarity"] < 0.95
    assert pair["left"]["path"] == "experiments/a.py"
    assert pair["right"]["path"] == "experiments/b.py"
    assert pair["signature_match"] is True
    assert pair["divergence"]
    assert "item" in pair["right_only_identifiers"]
    assert pair["left"]["nlines"] < pair["right"]["nlines"]


def test_fork_scan_respects_min_lines_floor(
    mini_repo: Path, tmp_path: Path
) -> None:
    _write(
        mini_repo / "pkg" / "experiments" / "a.py",
        _fork_body("analyze_a", "scale = entropy_scales[head]\n"),
    )
    _write(
        mini_repo / "pkg" / "experiments" / "b.py",
        _fork_body(
            "analyze_b",
            "scale = entropy_scales[head].mean().item()\n"
            "        scale = scale * 0.5\n",
        ),
    )
    output = tmp_path / "forks-floor.json"
    assert scan_forks.main(
        [
            "--root",
            str(mini_repo),
            "--package",
            "pkg",
            "--min-lines",
            "100",
            "--json",
            str(output),
        ]
    ) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["functions_indexed"] == 2
    assert payload["functions_over_min_lines"] == 0
    assert payload["fork_pairs"] == 0
    assert payload["pairs"] == []


def test_cli_smoke_all_entrypoints_pass_help() -> None:
    """Every scanner entrypoint must accept --help and exit 0."""
    failures = scan_cli_smoke.smoke()
    assert failures == []


def test_cli_smoke_catches_failing_entrypoint(tmp_path: Path) -> None:
    """A module whose main() exits non-zero on --help must be reported."""
    broken = tmp_path / "broken_scan.py"
    broken.write_text(
        "import sys\n"
        "def main(argv=None):\n"
        "    return 2\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(tmp_path))
    try:
        failures = scan_cli_smoke.smoke(("broken_scan",))
        assert failures == [("broken_scan", 2)]
    finally:
        sys.path.remove(str(tmp_path))


def test_contract_scan_ignores_remaining_four_channels(
    mini_repo: Path, tmp_path: Path
) -> None:
    """Suppression keys for experiment/forwarding/same_name/unreferenced."""
    package = mini_repo / "pkg"
    _write(
        package / "lib" / "math.py",
        "def compute(x):\n"
        "    return x * 2\n",
    )
    _write(
        package / "experiments" / "worker.py",
        "def run_task(items):\n"
        "    return items\n"
        "\n"
        "def pass_through(x):\n"
        "    return helper(x)\n",
    )
    _write(
        package / "experiments" / "a.py",
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        "\n"
        "from pkg.experiments import worker\n"
        "\n"
        "def compute(x):\n"
        "    return x + 1\n",
    )
    ignore = tmp_path / "ignore.json"
    ignore.write_text(
        json.dumps(
            {
                "contracts": {
                    "experiment_as_library": [{"key": "experiments/a.py"}],
                    "forwarding_wrappers": [
                        {"key": "experiments/worker.py:4:pass_through"}
                    ],
                    "same_name_contracts": [
                        {"key": "lib/math.py:1:compute"}
                    ],
                    "unreferenced_top_level_functions": [
                        {"key": "experiments/worker.py:1"},
                        {"key": "experiments/worker.py:4"},
                        {"key": "experiments/a.py:6"},
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "contracts-ignore4.json"
    assert scan_contracts.main(
        [
            "--root",
            str(mini_repo),
            "--package",
            "pkg",
            "--ignore",
            str(ignore),
            "--json",
            str(output),
        ]
    ) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["experiment_as_library"] == []
    assert payload["forwarding_wrappers"] == []
    # Suppressing one definition dissolves the two-path same-name group.
    assert payload["same_name_contracts"] == []
    # Only the lib-side compute definition survives suppression.
    assert [item["path"] for item in payload["unreferenced_top_level_functions"]] == [
        "lib/math.py"
    ]
    assert payload["ignored_counts"] == {
        "cli_without_bootstrap": 0,
        "defensive_param_loosening": 0,
        "env_written_not_read": 0,
        "generation_path_without_env": 0,
        "experiment_as_library": 1,
        "forwarding_wrappers": 1,
        "same_name_contracts": 1,
        "unreferenced_top_level_functions": 3,
    }


def test_adjudicate_flatten_injects_details() -> None:
    """_flatten expands report payloads into (scanner, signature, detail)."""
    report = {
        "scanners": {
            "deadcode": {
                "candidates": [
                    {"path": "a.py", "status": "DEAD", "py_refs": [], "doc_refs": []}
                ]
            },
            "hardcoded": {
                "hits": {
                    "archive": [
                        {"path": "b.py", "line": 7, "code": "c", "suggestion": "s"}
                    ]
                }
            },
        }
    }
    items = adjudicate._flatten(report)
    assert len(items) == 2
    dead = next(item for item in items if item["scanner"] == "deadcode")
    assert dead["signature"] == "DEAD/a.py"
    assert dead["detail"] == {
        "path": "a.py",
        "status": "DEAD",
        "py_refs": [],
        "doc_refs": [],
    }
    hard = next(item for item in items if item["scanner"] == "hardcoded")
    assert hard["signature"] == "hard/archive/b.py:7"
    assert hard["detail"]["_pattern"] == "archive"
    # The injected marker lives on the copy, not the payload record.
    assert "hits" not in hard["detail"]


def test_adjudicate_ignore_entries_key_formats() -> None:
    """Suppression entries must match the keys each scanner reads."""
    cases = [
        ("experiment_as_library", {"path": "e/a.py"}, "e/a.py"),
        ("forwarding_wrappers", {"path": "e/a.py", "line": 2, "name": "fwd"}, "e/a.py:2:fwd"),
        ("same_name_contracts", {"path": "e/a.py", "line": 3, "_name": "compute"}, "e/a.py:3:compute"),
        ("unreferenced_top_level_functions", {"path": "e/a.py", "line": 4}, "e/a.py:4"),
        ("cli_without_bootstrap", {"path": "e/b.py"}, "e/b.py"),
        ("defensive_param_loosening", {"path": "e/b.py", "line": 2}, "e/b.py:2"),
        ("env_written_not_read", {"path": "e/b.py", "var": "VAR"}, "e/b.py:VAR"),
        ("generation_path_without_env", {"path": "e/b.py"}, "e/b.py"),
    ]
    for channel, detail, key in cases:
        entries = adjudicate._ignore_entries(
            "contracts", {**detail, "_channel": channel}, "note"
        )
        assert entries == [(f"contracts/{channel}", {"key": key, "reason": "note"})], channel

    assert adjudicate._ignore_entries(
        "deadcode", {"path": "x.py"}, "n"
    ) == [("deadcode", {"path": "x.py", "reason": "n"})]
    assert adjudicate._ignore_entries(
        "duplicates", {"id": "abc123def456"}, "n"
    ) == [("duplicates", {"id": "abc123def456", "reason": "n"})]
    assert adjudicate._ignore_entries(
        "forks", {"key": "a.py:f::b.py:g"}, "n"
    ) == [("forks", {"key": "a.py:f::b.py:g", "reason": "n"})]
    assert adjudicate._ignore_entries(
        "capabilities",
        {"local": {"path": "x.py", "qualname": "f"}, "lib": {"path": "y.py", "qualname": "g"}},
        "n",
    ) == [("capabilities", {"key": "x.py:f", "reason": "n"})]
    assert adjudicate._ignore_entries(
        "hardcoded", {"path": "x.py", "_pattern": "hash"}, "n"
    ) == [("hardcoded", {"path": "x.py", "pattern": "hash", "reason": "n"})]
    assert adjudicate._ignore_entries(
        "style", {"path": "x.py", "_metric": "em_dash"}, "n"
    ) == [("style", {"path": "x.py", "pattern": "em_dash", "reason": "n"})]


def test_adjudicate_merge_ignore_dedupes_and_preserves() -> None:
    """_merge_ignore adds new entries only and keeps unrelated keys."""
    registry = {
        "schema_version": 1,
        "_notes": "keep me",
        "contracts": {"cli_without_bootstrap": [{"key": "a", "reason": "old"}]},
    }
    entries = [
        ("deadcode", {"path": "p.py", "reason": "r"}),
        ("deadcode", {"path": "p.py", "reason": "r"}),  # duplicate of the above
        ("contracts/cli_without_bootstrap", {"key": "b", "reason": "new"}),
        ("contracts/cli_without_bootstrap", {"key": "a", "reason": "old"}),  # already there
    ]
    added = adjudicate._merge_ignore(registry, entries)
    assert added == 2
    assert registry["schema_version"] == 1
    assert registry["_notes"] == "keep me"
    assert registry["deadcode"] == [{"path": "p.py", "reason": "r"}]
    assert registry["contracts"]["cli_without_bootstrap"] == [
        {"key": "a", "reason": "old"},
        {"key": "b", "reason": "new"},
    ]


def test_adjudicate_append_lesson_numbers_blocks(tmp_path: Path) -> None:
    """Lesson blocks continue numbering and follow the Case/Lesson format."""
    lessons = tmp_path / "LESSONS.md"
    lessons.write_text("## 7. dead code: suppressed something\n\n- **Case**: old\n", encoding="utf-8")
    adjudicate._append_lesson(lessons, "deadcode", "`x.py` (DEAD)", "unused by design")
    text = lessons.read_text(encoding="utf-8")
    assert "## 8. dead code: suppressed `x.py` (DEAD)" in text
    assert "- **Case**: `x.py` (DEAD)" in text
    assert "- **Lesson**: unused by design" in text
    assert "- **Implementation**: suppressed in ignore.json after semantic review." in text


def test_adjudicate_false_positive_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An fp decision extends ignore.json, appends LESSONS.md, persists the verdict."""
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "scanner": "self-audit-run-all",
                "schema_version": 5,
                "scanners": {
                    "deadcode": {
                        "candidates": [
                            {
                                "path": "pkg/lib/unused.py",
                                "status": "DEAD",
                                "py_refs": [],
                                "doc_refs": [],
                            }
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    ignore = tmp_path / "ignore.json"
    ignore.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    lessons = tmp_path / "LESSONS.md"
    verdicts = tmp_path / "verdicts.json"

    answers = iter(["fp", "docstring-only helpers reached via introspection"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert adjudicate.main(
        [
            "--report", str(report),
            "--ignore", str(ignore),
            "--lessons", str(lessons),
            "--verdicts", str(verdicts),
        ]
    ) == 0

    registry = json.loads(ignore.read_text(encoding="utf-8"))
    assert registry["deadcode"] == [
        {
            "path": "pkg/lib/unused.py",
            "reason": "docstring-only helpers reached via introspection",
        }
    ]
    assert registry["schema_version"] == 1
    lesson_text = lessons.read_text(encoding="utf-8")
    assert "## 1. dead code: suppressed `pkg/lib/unused.py` (DEAD)" in lesson_text
    assert "- **Lesson**: docstring-only helpers reached via introspection" in lesson_text
    verdict_log = json.loads(verdicts.read_text(encoding="utf-8"))
    assert len(verdict_log["verdicts"]) == 1
    assert verdict_log["verdicts"][0]["disposition"] == "false positive"
    assert verdict_log["verdicts"][0]["suppressed"] is True

    # A second run resumes from the verdict log: no pending candidates.
    monkeypatch.setattr("builtins.input", lambda _prompt: (_ for _ in ()).throw(AssertionError("no input expected")))
    assert adjudicate.main(
        [
            "--report", str(report),
            "--ignore", str(ignore),
            "--lessons", str(lessons),
            "--verdicts", str(verdicts),
        ]
    ) == 0


def test_adjudicate_quit_early_writes_nothing_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "scanner": "self-audit-run-all",
                "schema_version": 5,
                "scanners": {
                    "style": {
                        "hits": {
                            "em_dash": [
                                {"path": "docs/main.tex", "line": 12, "text": "x"}
                            ]
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    ignore = tmp_path / "ignore.json"
    lessons = tmp_path / "LESSONS.md"
    verdicts = tmp_path / "verdicts.json"
    monkeypatch.setattr("builtins.input", lambda _prompt: "q")
    assert adjudicate.main(
        [
            "--report", str(report),
            "--ignore", str(ignore),
            "--lessons", str(lessons),
            "--verdicts", str(verdicts),
        ]
    ) == 0
    # Quitting early suppresses nothing and writes no session state.
    assert not verdicts.exists()
    assert not ignore.exists()
    assert not lessons.exists()
