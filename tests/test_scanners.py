from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR))

import _audit_config
import adjudicate
import report_formatter
import run_all
import scan_capabilities
import scan_cli_smoke
import scan_contracts
import scan_deadcode
import scan_duplicates
import scan_forks
import scan_hardcoded
import scan_regions
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
        "_audit_config.py",
        "_scanner_common.py",
        "report_formatter.py",
        "run_all.py",
        "scan_capabilities.py",
        "scan_contracts.py",
        "scan_deadcode.py",
        "scan_duplicates.py",
        "scan_forks.py",
        "scan_hardcoded.py",
        "scan_regions.py",
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


def test_cli_smoke_version_passes() -> None:
    """Modules with --version must accept it and exit 0."""
    failures = scan_cli_smoke.version_smoke()
    assert failures == []


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
    date, owner = "2026-08-12", "reviewer-a"
    stamped = {"date": date, "owner": owner}
    cases: list[tuple[str, dict[str, str | int], str]] = [
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
            "contracts", {**detail, "_channel": channel}, "note", date=date, owner=owner
        )
        assert entries == [
            (f"contracts/{channel}", {"key": key, "reason": "note", **stamped})
        ], channel

    assert adjudicate._ignore_entries(
        "deadcode", {"path": "x.py"}, "n", date=date, owner=owner
    ) == [("deadcode", {"path": "x.py", "reason": "n", **stamped})]
    assert adjudicate._ignore_entries(
        "duplicates", {"id": "abc123def456"}, "n", date=date, owner=owner
    ) == [("duplicates", {"id": "abc123def456", "reason": "n", **stamped})]
    assert adjudicate._ignore_entries(
        "regions", {"id": "abc123def456"}, "n", date=date, owner=owner
    ) == [("regions", {"id": "abc123def456", "reason": "n", **stamped})]
    # Distinct regions clusters must not collide under suppression identity.
    first = {"id": "r1"}
    second = {"id": "r2"}
    assert adjudicate._suppression_identity(
        "regions", first
    ) != adjudicate._suppression_identity("regions", second)
    assert adjudicate._ignore_entries(
        "forks", {"key": "a.py:f::b.py:g"}, "n", date=date, owner=owner
    ) == [("forks", {"key": "a.py:f::b.py:g", "reason": "n", **stamped})]
    assert adjudicate._ignore_entries(
        "capabilities",
        {"local": {"path": "x.py", "qualname": "f"}, "lib": {"path": "y.py", "qualname": "g"}},
        "n",
        date=date,
        owner=owner,
    ) == [("capabilities", {"key": "x.py:f", "reason": "n", **stamped})]
    assert adjudicate._ignore_entries(
        "hardcoded", {"path": "x.py", "_pattern": "hash"}, "n", date=date, owner=owner
    ) == [("hardcoded", {"path": "x.py", "pattern": "hash", "reason": "n", **stamped})]
    assert adjudicate._ignore_entries(
        "style", {"path": "x.py", "_metric": "em_dash"}, "n", date=date, owner=owner
    ) == [("style", {"path": "x.py", "pattern": "em_dash", "reason": "n", **stamped})]

    # Default date is today; owner is omitted when unknown.
    (section, entry), = adjudicate._ignore_entries(
        "deadcode", {"path": "x.py"}, "n", owner=None
    )
    assert section == "deadcode"
    assert entry["path"] == "x.py"
    assert entry["date"] == dt.date.today().isoformat()
    assert "owner" not in entry


def test_adjudicate_merge_ignore_dedupes_and_preserves() -> None:
    """_merge_ignore adds new entries only and keeps unrelated keys.

    The dedupe ignores the date/owner stamps: re-suppressing the same
    candidate keeps the first suppression record with its original date.
    """
    registry: dict[str, Any] = {
        "schema_version": 1,
        "_notes": "keep me",
        "contracts": {"cli_without_bootstrap": [{"key": "a", "reason": "old"}]},
    }
    entries = [
        ("deadcode", {"path": "p.py", "reason": "r", "date": "2026-08-01", "owner": "a"}),
        # Same candidate re-suppressed later: stamp differs, identity does not.
        ("deadcode", {"path": "p.py", "reason": "r", "date": "2026-08-12", "owner": "b"}),
        ("contracts/cli_without_bootstrap", {"key": "b", "reason": "new", "date": "2026-08-12"}),
        # Already present (stamp-less); a stamped copy must not duplicate it.
        ("contracts/cli_without_bootstrap", {"key": "a", "reason": "old", "date": "2026-08-12", "owner": "c"}),
    ]
    added = adjudicate._merge_ignore(registry, entries)
    assert added == 2
    assert registry["schema_version"] == 1
    assert registry["_notes"] == "keep me"
    # First suppression (date and owner) is preserved.
    assert registry["deadcode"] == [{"path": "p.py", "reason": "r", "date": "2026-08-01", "owner": "a"}]
    assert registry["contracts"]["cli_without_bootstrap"] == [
        {"key": "a", "reason": "old"},
        {"key": "b", "reason": "new", "date": "2026-08-12"},
    ]


def test_adjudicate_merge_ignore_dedupes_across_different_reasons() -> None:
    """Re-suppressing the same candidate with a different reason keeps the first.

    ``reason`` is a review annotation, not identity: the dedupe must match on
    the fields that locate the target (path, id, key, path+pattern) alone.
    """
    registry: dict = {}
    entries = [
        # deadcode identity = path
        ("deadcode", {"path": "foo.py", "reason": "dynamic dispatch", "date": "2026-08-01"}),
        ("deadcode", {"path": "foo.py", "reason": "loaded dynamically", "date": "2026-08-12"}),
        # hardcoded identity = path + pattern
        ("hardcoded", {"path": "a.py", "pattern": "sha", "reason": "canonical", "date": "2026-08-01"}),
        ("hardcoded", {"path": "a.py", "pattern": "sha", "reason": "intentional", "date": "2026-08-12"}),
        # contracts identity = key (within channel)
        ("contracts/cli_without_bootstrap", {"key": "b.py", "reason": "v1", "date": "2026-08-01"}),
        ("contracts/cli_without_bootstrap", {"key": "b.py", "reason": "v2", "date": "2026-08-12"}),
        # different target — must be added
        ("deadcode", {"path": "bar.py", "reason": "gone", "date": "2026-08-12"}),
    ]
    added = adjudicate._merge_ignore(registry, entries)
    assert added == 4
    # First record (with original reason and date) is preserved.
    assert registry["deadcode"] == [
        {"path": "foo.py", "reason": "dynamic dispatch", "date": "2026-08-01"},
        {"path": "bar.py", "reason": "gone", "date": "2026-08-12"},
    ]
    assert registry["hardcoded"] == [
        {"path": "a.py", "pattern": "sha", "reason": "canonical", "date": "2026-08-01"},
    ]
    assert registry["contracts"]["cli_without_bootstrap"] == [
        {"key": "b.py", "reason": "v1", "date": "2026-08-01"},
    ]


def test_adjudicate_skip_is_not_permanent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``skip`` defers within the session; the candidate reappears on resume."""
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
                                "path": "pkg/dead.py",
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
    lessons = tmp_path / "LESSONS.md"
    verdicts = tmp_path / "verdicts.json"

    # Session 1: skip the only candidate.
    monkeypatch.setattr("builtins.input", lambda _prompt: "s")
    assert adjudicate.main(
        [
            "--report", str(report),
            "--ignore", str(ignore),
            "--lessons", str(lessons),
            "--verdicts", str(verdicts),
        ]
    ) == 0
    log = json.loads(verdicts.read_text(encoding="utf-8"))
    assert log["verdicts"][0]["disposition"] == "skip"

    # --check must report the skipped candidate as still pending.
    assert adjudicate.main(
        ["--report", str(report), "--verdicts", str(verdicts), "--check"]
    ) == 1

    # Session 2: the candidate reappears; give it a real verdict.
    answers = iter(["fp", "intentional generated entrypoint"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert adjudicate.main(
        [
            "--report", str(report),
            "--ignore", str(ignore),
            "--lessons", str(lessons),
            "--verdicts", str(verdicts),
        ]
    ) == 0
    # The skip record is replaced, not duplicated.
    log = json.loads(verdicts.read_text(encoding="utf-8"))
    assert len(log["verdicts"]) == 1
    assert log["verdicts"][0]["disposition"] == "false positive"

    # Now --check passes: the candidate has a real verdict.
    assert adjudicate.main(
        ["--report", str(report), "--verdicts", str(verdicts), "--check"]
    ) == 0


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
            "--owner", "reviewer-a",
        ]
    ) == 0

    registry = json.loads(ignore.read_text(encoding="utf-8"))
    assert registry["deadcode"] == [
        {
            "path": "pkg/lib/unused.py",
            "reason": "docstring-only helpers reached via introspection",
            "date": dt.date.today().isoformat(),
            "owner": "reviewer-a",
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
    assert verdict_log["verdicts"][0]["target_id"] == "DEAD/pkg/lib/unused.py"
    assert len(verdict_log["verdicts"][0]["finding_evidence_hash"]) == 64
    assert verdict_log["verdicts"][0]["suppression"][0]["section"] == "deadcode"

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


def test_run_all_default_state_paths_follow_root(
    mini_repo: Path, tmp_path: Path
) -> None:
    """State defaults derive from --root, not from the toolkit directory."""
    _write(mini_repo / "pkg" / "lib" / "helper.py", "def helper(x):\n    return x\n")
    assert (
        run_all.main(
            [
                "--root",
                str(mini_repo),
                "--package",
                "pkg",
                "--no-doc-channel",
            ]
        )
        == 0
    )
    assert (mini_repo / "reports" / "latest.json").is_file()
    assert (mini_repo / "reports" / "latest.md").is_file()


def test_adjudicate_default_state_paths_follow_report_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "audited-project"
    report_path = project / "reports" / "latest.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        json.dumps(
            {
                "state": {"project_root": str(project)},
                "scanners": {
                    "deadcode": {
                        "candidates": [
                            {
                                "path": "pkg/dead.py",
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
    answers = iter(["fp", "intentional generated entrypoint"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert adjudicate.main(["--report", str(report_path)]) == 0
    assert (project / "ignore.json").is_file()
    assert (project / "LESSONS.md").is_file()
    assert (project / "reports" / "verdicts.json").is_file()


def test_adjudicate_check_reports_pending_without_writing(
    tmp_path: Path,
) -> None:
    project = tmp_path / "audited-project"
    report_path = project / "reports" / "latest.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        json.dumps(
            {
                "state": {"project_root": str(project)},
                "scanners": {
                    "deadcode": {
                        "candidates": [
                            {
                                "path": "pkg/dead.py",
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
    assert adjudicate.main(["--report", str(report_path), "--check"]) == 1
    assert not (project / "ignore.json").exists()
    assert not (project / "reports" / "verdicts.json").exists()

    finding = adjudicate._flatten(json.loads(report_path.read_text(encoding="utf-8")))[0]
    (project / "reports" / "verdicts.json").write_text(
        json.dumps(
            {
                "verdicts": [
                    {
                        "scanner": "deadcode",
                        "signature": "DEAD/pkg/dead.py",
                        "target_id": finding["target_id"],
                        # legacy key on purpose: resume must accept verdicts
                        # written before the finding_evidence_hash rename
                        "evidence_hash": finding["finding_evidence_hash"],
                        "disposition": "false positive",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert adjudicate.main(["--report", str(report_path), "--check"]) == 0


def test_adjudicate_check_rejects_stale_evidence(
    tmp_path: Path,
) -> None:
    project = tmp_path / "audited-project"
    report_path = project / "reports" / "latest.json"
    report_path.parent.mkdir(parents=True)
    report = {
        "scanners": {
            "deadcode": {
                "candidates": [
                    {
                        "path": "pkg/dead.py",
                        "status": "DEAD",
                        "py_refs": [],
                        "doc_refs": [],
                    }
                ]
            }
        }
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    finding = adjudicate._flatten(report)[0]
    verdicts = project / "reports" / "verdicts.json"
    verdicts.write_text(
        json.dumps(
            {
                "verdicts": [
                    {
                        "scanner": "deadcode",
                        "signature": finding["signature"],
                        "target_id": finding["target_id"],
                        # legacy key: stale-evidence detection must fall back
                        # to evidence_hash for pre-rename verdict files
                        "evidence_hash": finding["finding_evidence_hash"],
                        "disposition": "false positive",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assert adjudicate.main(["--report", str(report_path), "--check"]) == 0

    report["scanners"]["deadcode"]["candidates"][0]["status"] = "USED"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    assert adjudicate.main(["--report", str(report_path), "--check"]) == 1


def test_run_all_report_records_state_paths(
    mini_repo: Path,
) -> None:
    assert (
        run_all.main(
            [
                "--root",
                str(mini_repo),
                "--package",
                "pkg",
                "--no-doc-channel",
            ]
        )
        == 0
    )
    report = json.loads(
        (mini_repo / "reports" / "latest.json").read_text(encoding="utf-8")
    )
    assert report["state"]["project_root"] == str(mini_repo.resolve())
    assert report["state"]["state_dir"] == str((mini_repo / "reports").resolve())
    assert report["state"]["ignore_file"] == str((mini_repo / "ignore.json").resolve())


def test_run_all_code_profile_disables_research_style_channel(
    mini_repo: Path,
) -> None:
    assert (
        run_all.main(
            [
                "--root",
                str(mini_repo),
                "--package",
                "pkg",
                "--profile",
                "code",
                "--no-doc-channel",
            ]
        )
        == 0
    )
    report = json.loads(
        (mini_repo / "reports" / "latest.json").read_text(encoding="utf-8")
    )
    assert report["configuration"]["profile"] == "code"
    assert report["scanners"]["style"]["disabled"] is True


def test_run_all_all_py_scans_flat_package_layout(
    mini_repo: Path,
) -> None:
    _write(mini_repo / "pkg" / "root_module.py", "def root_only():\n    return 1\n")
    assert (
        run_all.main(
            [
                "--root",
                str(mini_repo),
                "--package",
                "pkg",
                "--all-py",
                "--profile",
                "code",
                "--no-doc-channel",
            ]
        )
        == 0
    )
    report = json.loads(
        (mini_repo / "reports" / "latest.json").read_text(encoding="utf-8")
    )
    modules = report["scanners"]["deadcode"]["modules"]
    assert any(item["path"] == "root_module.py" for item in modules)


def test_deadcode_public_api_modules_are_review_candidates(
    mini_repo: Path, tmp_path: Path
) -> None:
    _write(mini_repo / "pkg" / "__init__.py", "")
    _write(mini_repo / "pkg" / "public_module.py", "def api():\n    return 1\n")
    output = tmp_path / "deadcode.json"
    assert (
        scan_deadcode.main(
            [
                "--root",
                str(mini_repo),
                "--package",
                "pkg",
                "--subdirs",
                ".",
                "--public-api",
                "--no-doc-channel",
                "--json",
                str(output),
            ]
        )
        == 0
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    row = next(item for item in payload["modules"] if item["path"] == "public_module.py")
    assert row["status"] == "PUBLIC_API_CANDIDATE"
    assert payload["public_api_candidates"] == [row]


def test_stale_ignore_entries_flags_gone_targets(
    mini_repo: Path, tmp_path: Path
) -> None:
    _write(mini_repo / "pkg" / "a.py", "MY_FLAG = 1\ndef compute():\n    pass\n")
    registry = {
        "deadcode": [
            {"path": "pkg/a.py", "reason": "ok"},
            {"path": "pkg/gone.py", "reason": "deleted"},
        ],
        "contracts": {
            "forwarding_wrappers": [
                {"key": "pkg/a.py:2:compute", "reason": "ok"},
                {"key": "pkg/a.py:2:missing", "reason": "renamed"},
                {"key": "pkg/gone.py:1:compute", "reason": "gone file"},
            ],
            "env_written_not_read": [
                {"key": "pkg/a.py:OTHER_VAR", "reason": "never read"},
            ],
            "unreferenced_top_level_functions": [
                {"key": "pkg/a.py:9", "reason": "line past EOF"},
            ],
        },
        "capabilities": [
            {"key": "pkg/a.py:compute", "reason": "ok"},
            {"key": "pkg/a.py:nope", "reason": "renamed"},
        ],
        "forks": [
            {"key": "pkg/a.py:compute::pkg/gone.py:other", "reason": "pair"},
        ],
        "hardcoded": [
            {"path": "pkg/a.py", "pattern": "x", "reason": "ok"},
            {"path": "pkg/x.py", "pattern": "x", "reason": "gone"},
        ],
    }
    stale = run_all._stale_ignore_entries(registry, mini_repo)
    flagged = {(section, key) for section, key, _ in stale}
    assert flagged == {
        ("deadcode", "pkg/gone.py"),
        ("contracts/forwarding_wrappers", "pkg/a.py:2:missing"),
        ("contracts/forwarding_wrappers", "pkg/gone.py:1:compute"),
        ("contracts/env_written_not_read", "pkg/a.py:OTHER_VAR"),
        ("contracts/unreferenced_top_level_functions", "pkg/a.py:9"),
        ("capabilities", "pkg/a.py:nope"),
        ("forks", "pkg/a.py:compute::pkg/gone.py:other"),
        ("hardcoded", "pkg/x.py"),
    }
    # Live targets are not flagged.
    assert all(section != "deadcode" for section, key, _ in stale if key == "pkg/a.py")
    assert not any(
        section == "capabilities" and key == "pkg/a.py:compute"
        for section, key, _ in stale
    )


def test_run_all_stale_check_end_to_end(
    mini_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    _write(mini_repo / "pkg" / "lib" / "helper.py", "def helper(x):\n    return x\n")
    ignore = tmp_path / "ignore.json"
    ignore.write_text(
        json.dumps({"deadcode": [{"path": "pkg/gone.py", "reason": "deleted"}]}),
        encoding="utf-8",
    )
    assert (
        run_all.main(
            [
                "--root",
                str(mini_repo),
                "--package",
                "pkg",
                "--ignore",
                str(ignore),
                "--stale-check",
                "--no-doc-channel",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "STALE_CHECK" in out
    assert "file missing: pkg/gone.py" in out


def test_adjudicate_export_ignore_rebuilds_registry(
    tmp_path: Path,
) -> None:
    """--export-ignore rebuilds ignore.json from verdicts, keeping only FP dispositions."""
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
                    },
                    "duplicates": {
                        "clusters": [
                            {
                                "id": "abcd1234abcd",
                                "size": 2,
                                "priority": "high",
                                "priority_reason": "lib_shared",
                                "lib_shared": [],
                                "members": [
                                    {"path": "lib/a.py", "name": "a", "qualname": "a"},
                                    {"path": "experiments/b.py", "name": "b", "qualname": "b"},
                                ],
                            }
                        ]
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    verdicts = tmp_path / "verdicts.json"
    verdicts.write_text(
        json.dumps(
            {
                "verdicts": [
                    {
                        "scanner": "deadcode",
                        "signature": "DEAD/pkg/lib/unused.py",
                        "disposition": "false positive",
                        "note": "reached via introspection",
                    },
                    {
                        "scanner": "duplicates",
                        "signature": "cluster/abcd1234abcd",
                        "disposition": "true duplicate",
                        "note": "consolidate later",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    export_path = tmp_path / "exported_ignore.json"

    rc = adjudicate.main(
        [
            "--report", str(report),
            "--verdicts", str(verdicts),
            "--export-ignore", str(export_path),
            "--owner", "ci-bot",
        ]
    )
    assert rc == 0
    registry = json.loads(export_path.read_text(encoding="utf-8"))
    assert registry["schema_version"] == 1
    # Only the false-positive verdict generates a suppression entry.
    assert len(registry["deadcode"]) == 1
    assert registry["deadcode"][0]["path"] == "pkg/lib/unused.py"
    assert registry["deadcode"][0]["reason"] == "reached via introspection"
    assert registry["deadcode"][0]["owner"] == "ci-bot"
    assert "date" in registry["deadcode"][0]
    # The true-duplicate verdict must NOT appear as a suppression.
    assert "duplicates" not in registry


def test_adjudicate_export_ignore_is_idempotent(
    tmp_path: Path,
) -> None:
    """Running --export-ignore twice does not duplicate entries."""
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
                                "path": "pkg/lib/gone.py",
                                "status": "DEAD",
                                "py_refs": [],
                                "doc_refs": [],
                            }
                        ]
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    verdicts = tmp_path / "verdicts.json"
    verdicts.write_text(
        json.dumps(
            {
                "verdicts": [
                    {
                        "scanner": "deadcode",
                        "signature": "DEAD/pkg/lib/gone.py",
                        "disposition": "false positive",
                        "note": "generated code",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    export_path = tmp_path / "exported_ignore.json"

    for _ in range(2):
        assert (
            adjudicate.main(
                [
                    "--report", str(report),
                    "--verdicts", str(verdicts),
                    "--export-ignore", str(export_path),
                ]
            )
            == 0
        )
    registry = json.loads(export_path.read_text(encoding="utf-8"))
    assert len(registry["deadcode"]) == 1


def test_export_ignore_rebuilds_from_self_contained_verdicts(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    verdicts = project / "reports" / "verdicts.json"
    verdicts.parent.mkdir(parents=True)
    verdicts.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "verdicts": [
                    {
                        "scanner": "deadcode",
                        "signature": "DEAD/pkg/gone.py",
                        "target_id": "DEAD/pkg/gone.py",
                        "evidence_hash": "evidence-v1",
                        "disposition": "false positive",
                        "suppression": [
                            {
                                "section": "deadcode",
                                "entry": {
                                    "path": "pkg/gone.py",
                                    "reason": "generated artifact",
                                    "date": "2026-08-12",
                                    "owner": "reviewer-a",
                                },
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    export_path = project / "ignore.json"

    assert adjudicate.main(
        [
            "--root",
            str(project),
            "--verdicts",
            str(verdicts),
            "--export-ignore",
            str(export_path),
        ]
    ) == 0
    registry = json.loads(export_path.read_text(encoding="utf-8"))
    assert registry["deadcode"] == [
        {
            "path": "pkg/gone.py",
            "reason": "generated artifact",
            "date": "2026-08-12",
            "owner": "reviewer-a",
        }
    ]


def test_audit_config_warns_once_on_invalid_json(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    import _audit_config

    broken = tmp_path / "broken"
    _write(broken / _audit_config.CONFIG_FILENAME, "{not json")
    assert _audit_config.load_config(broken) == {}
    assert _audit_config.load_config(broken) == {}
    warnings = capsys.readouterr().err
    assert warnings.count("warning:") == 1

    not_object = tmp_path / "list"
    _write(not_object / _audit_config.CONFIG_FILENAME, "[1, 2]")
    assert _audit_config.load_config(not_object) == {}
    assert "not a JSON object" in capsys.readouterr().err

    missing = tmp_path / "missing"
    assert _audit_config.load_config(missing) == {}
    assert capsys.readouterr().err == ""


def test_audit_config_warns_and_falls_back_on_semantic_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    broken = tmp_path / "semantic-broken"
    _write(
        broken / _audit_config.CONFIG_FILENAME,
        json.dumps(
            {
                "duplicates": {"threshold": "high", "min_chars": -1},
                "unknown_section": {},
            }
        ),
    )
    assert _audit_config.load_config(broken) == {}
    assert _audit_config.load_config(broken) == {}
    warning = capsys.readouterr().err
    assert warning.count("warning:") == 1
    assert "duplicates.threshold" in warning
    assert "unknown key 'unknown_section'" in warning


def test_audit_config_keeps_valid_typed_values(tmp_path: Path) -> None:
    valid = tmp_path / "valid"
    _write(
        valid / _audit_config.CONFIG_FILENAME,
        json.dumps(
            {
                "schema_version": 1,
                "duplicates": {"threshold": 0.91, "min_chars": 80},
                "forks": {"include_tests": True},
            }
        ),
    )
    assert _audit_config.load_config(valid)["duplicates"] == {
        "threshold": 0.91,
        "min_chars": 80,
    }


CHECKPOINT_REGION = """    state = torch.load(config.path, map_location=device)
    meta = state.get("meta", {})
    cleaned = {}
    for key, value in state.items():
        if key.startswith("module."):
            key = key[7:]
        cleaned[key] = value
    model.load_state_dict(cleaned, strict=True)
"""


def _checkpoint_fixture(mini_repo: Path) -> None:
    _write(
        mini_repo / "pkg" / "experiments" / "train.py",
        "def train_model(config, data, model, logdir):\n"
        "    device = resolve_device(config.device)\n"
        "\n"
        + CHECKPOINT_REGION
        + "\n"
        "    for epoch in range(config.epochs):\n"
        "        metrics = run_epoch(model, data, device)\n"
        "        log_metrics(logdir, epoch, metrics)\n"
        "    return model\n",
    )
    _write(
        mini_repo / "pkg" / "experiments" / "evaluate.py",
        "def evaluate_model(config, model, data, outdir):\n"
        "    device = resolve_device(config.device)\n"
        "\n"
        + CHECKPOINT_REGION
        + "\n"
        "    results = run_batch(model, data, device)\n"
        "    dump_results(outdir, results, meta)\n"
        "    return results\n",
    )


def test_region_scan_finds_latent_capability(
    mini_repo: Path, tmp_path: Path
) -> None:
    _checkpoint_fixture(mini_repo)
    output = tmp_path / "regions.json"
    rc = scan_regions.main(
        [
            "--root", str(mini_repo),
            "--package", "pkg",
            "--threshold", "0.9",
            "--json", str(output),
        ]
    )
    assert rc == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    cluster = next(item for item in payload["clusters"] if item["size"] == 2)
    assert cluster["file_count"] == 2
    assert cluster["min_edge_sim"] == 1.0
    assert "load_state_dict" in cluster["capability_hints"]
    members = {m["path"] + ":" + m["qualname"] + ":" + str(m["start_line"]) + "-" + str(m["end_line"])
               for m in cluster["members"]}
    assert "experiments/train.py:train_model:4-11" in members
    assert "experiments/evaluate.py:evaluate_model:4-11" in members
    member = cluster["members"][0]
    assert member["inputs"] == ["config", "device", "model"]
    assert member["outputs"] == []
    assert member["extractability"] >= 0.5
    assert member["control_shape"] == "If1For1While0Try0With0"


def _markdown_payloads(clusters: list[dict]) -> dict:
    """Minimal payloads dict for report_formatter.markdown() with regions."""
    return {
        "deadcode": {},
        "duplicates": {},
        "forks": {},
        "contracts": {},
        "capabilities": {},
        "hardcoded": {},
        "style": {},
        "regions": {"clusters": clusters},
    }


def test_report_formatter_renders_region_cluster_kinds() -> None:
    """Both region cluster kinds render without KeyError or field bleed."""
    shared = {
        "id": "shared1",
        "kind": "shared_capability",
        "priority": "high",
        "priority_reason": "latent capability repeated across files",
        "size": 2,
        "max_lines": 8,
        "file_count": 2,
        "max_sim": 1.0,
        "min_edge_sim": 0.9,
        "semantic_risk": 0.2,
        "risk_signals": [],
        "capability_hints": ["load_state_dict"],
        "canonical_symbol": None,
        "members": [
            {
                "path": "experiments/train.py",
                "qualname": "train_model",
                "region_id": "r1",
                "start_line": 4,
                "end_line": 11,
                "nstatements": 6,
                "nlines": 8,
                "inputs": ["config", "device", "model"],
                "outputs": [],
                "calls": ["load_state_dict"],
                "effects": {"mutates": []},
                "control_shape": "If1For1",
                "extractability": 0.8,
            }
        ],
    }
    helper = {
        "id": "helper1",
        "kind": "helper_not_reused",
        "priority": "high",
        "priority_reason": "inline copy covers 98% of canonical helper check",
        "size": 1,
        "max_lines": 7,
        "max_coverage": 0.978,
        "canonical_symbol": "lib/helper.py:check",
        "canonical": {"path": "lib/helper.py", "qualname": "check", "lineno": 3},
        "semantic_risk": 0.0,
        "risk_signals": [],
        "members": [
            {
                "path": "experiments/a.py",
                "qualname": "run",
                "region_id": "r9",
                "start_line": 14,
                "end_line": 20,
                "nstatements": 5,
                "nlines": 7,
                "inputs": ["tensor"],
                "outputs": [],
                "calls": [],
                "effects": {"mutates": []},
                "control_shape": "If2",
                "extractability": 1.0,
                "coverage": 0.978,
                "canonical_referenced_in_parent": True,
            }
        ],
    }
    summary = {
        "package": "pkg",
        "generated_at": "2026-08-12T00:00:00",
        "provenance": {"git": {"head": "abc1234", "dirty_count": 0}},
    }
    text = report_formatter.markdown(_markdown_payloads([shared, helper]), summary)
    assert "### [high] `shared1`" in text
    assert "edge similarity 0.900-1.000" in text
    assert "`load_state_dict`" in text
    assert "### [high] `helper1`" in text
    assert "Canonical helper: `lib/helper.py:check` (L3)" in text
    assert "coverage=0.978" in text
    assert "(referenced in parent)" in text


def test_region_scan_ignores_generic_loops_without_api_calls(
    mini_repo: Path, tmp_path: Path
) -> None:
    body = """def calc(values, cap):
    total = 0
    count = 0
    seen = frozenset(values)
    for value in values:
        if value < 0:
            total -= value
            count += 1
        elif value == 0:
            count += 1
        else:
            total += value
            count += 1
    return min(total, cap), count
"""
    _write(mini_repo / "pkg" / "experiments" / "a.py", body.replace("def calc", "def calc_a"))
    _write(mini_repo / "pkg" / "experiments" / "b.py", body.replace("def calc", "def calc_b"))
    output = tmp_path / "regions.json"
    scan_regions.main(
        ["--root", str(mini_repo), "--package", "pkg", "--json", str(output)]
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["clusters"] == []


def test_region_scan_filters_highly_coupled_regions(
    mini_repo: Path, tmp_path: Path
) -> None:
    params = ", ".join(chr(ord("a") + i) for i in range(10))
    _write(
        mini_repo / "pkg" / "experiments" / "couple.py",
        f"def fat(fn, {params}):\n"
        "    total = lib.one(a, b)\n"
        "    total = lib.two(total, c, d)\n"
        "    total = lib.three(total, e, f)\n"
        "    total = lib.four(total, g, h)\n"
        "    total = lib.five(total, i, j)\n"
        "    return lib.finish(fn, total)\n",
    )
    output = tmp_path / "regions.json"
    scan_regions.main(
        ["--root", str(mini_repo), "--package", "pkg", "--json", str(output)]
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["regions_scanned"] == 0


INLINE_VALIDATION = """    device, dtype = _model_device_dtype(model)
    if tensor.device != device:
        raise ValueError("device mismatch")
    if tensor.dtype != dtype:
        raise ValueError("dtype mismatch")
"""


def test_region_scan_finds_helper_not_reused(
    mini_repo: Path, tmp_path: Path
) -> None:
    _write(
        mini_repo / "pkg" / "lib" / "helper.py",
        "def check(model, tensor):\n" + INLINE_VALIDATION,
    )
    _write(
        mini_repo / "pkg" / "experiments" / "a.py",
        "def run(model, tensor, scale):\n"
        + INLINE_VALIDATION
        + "    if scale <= 0:\n"
        "        raise ValueError(\"scale must be positive\")\n"
        "    if tuple(tensor.shape) != (2, 2):\n"
        "        raise ValueError(\"unexpected shape\")\n"
        "    return tensor.mul(scale)\n",
    )
    output = tmp_path / "regions.json"
    rc = scan_regions.main(
        ["--root", str(mini_repo), "--package", "pkg", "--json", str(output)]
    )
    assert rc == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    reports = [
        item for item in payload["clusters"] if item["kind"] == "helper_not_reused"
    ]
    assert reports, "expected a helper_not_reused report"
    cluster = reports[0]
    assert cluster["canonical_symbol"] == "lib/helper.py:check"
    assert cluster["priority"] == "high"
    assert cluster["max_coverage"] == 1.0
    member = cluster["members"][0]
    assert member["path"] == "experiments/a.py"
    assert member["qualname"] == "run"
    assert member["canonical_referenced_in_parent"] is False


def test_region_scan_short_risky_lookup_cluster(
    mini_repo: Path, tmp_path: Path
) -> None:
    lookup = (
        "def scores_from_prepared_tables(a, b, tables):\n"
        "    per_head = []\n"
        "    for key in tables:\n"
        "        per_head.append(torch.stack([\n"
        '            tables["S00"][a, a],\n'
        '            tables["S01"][a, b],\n'
        '            tables["S10"][b, a],\n'
        '            tables["S11"][b, b],\n'
        "        ]))\n"
        "    return torch.stack(per_head)\n"
        "\n"
        "def mean_entropy_for_head(a, b, tables):\n"
        "    scores = torch.stack([\n"
        '        tables["S00"][a, a],\n'
        '        tables["S01"][a, b],\n'
        '        tables["S10"][b, a],\n'
        '        tables["S11"][b, b],\n'
        "    ])\n"
        "    return scores\n"
    )
    _write(mini_repo / "pkg" / "experiments" / "lookup.py", lookup)
    output = tmp_path / "regions.json"
    rc = scan_regions.main(
        ["--root", str(mini_repo), "--package", "pkg", "--json", str(output)]
    )
    assert rc == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    short = [
        item for item in payload["clusters"] if item.get("short_block_cluster")
    ]
    assert short, "expected a short_risky signature cluster"
    cluster = short[0]
    assert "asymmetric_indexing" in cluster["risk_signals"]
    assert cluster["semantic_risk"] >= 0.45
    assert cluster["size"] >= 2
    assert {m["path"] for m in cluster["members"]} == {"experiments/lookup.py"}


def test_region_scan_helper_skips_constructor_boilerplate(
    mini_repo: Path, tmp_path: Path
) -> None:
    """__init__ canonicals are never helper matches: constructor
    attribute-assignment bodies are generic (14/50 labelled FPs)."""
    body = "    self.model = model\n    self.tensor = tensor\n    self.device = model.device\n"
    _write(mini_repo / "pkg" / "lib" / "helper.py", "class Box:\n" + body)
    _write(
        mini_repo / "pkg" / "experiments" / "a.py",
        "class Envelope:\n" + body,
    )
    output = tmp_path / "regions.json"
    rc = scan_regions.main(
        ["--root", str(mini_repo), "--package", "pkg", "--json", str(output)]
    )
    assert rc == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    helper = [
        item for item in payload["clusters"] if item["kind"] == "helper_not_reused"
    ]
    assert helper == [], "constructor boilerplate must not match a helper"


def test_region_scan_helper_skips_parent_referencing_canonical(
    mini_repo: Path, tmp_path: Path
) -> None:
    """A region whose parent already references the canonical by name is not
    an orphaned copy (3/50 labelled FPs)."""
    _write(
        mini_repo / "pkg" / "lib" / "helper.py",
        "def check(model, tensor):\n" + INLINE_VALIDATION,
    )
    _write(
        mini_repo / "pkg" / "experiments" / "a.py",
        "def run(model, tensor, scale):\n"
        "    check(model, tensor)\n"
        + INLINE_VALIDATION
        + "    return tensor.mul(scale)\n",
    )
    output = tmp_path / "regions.json"
    rc = scan_regions.main(
        ["--root", str(mini_repo), "--package", "pkg", "--json", str(output)]
    )
    assert rc == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    helper = [
        item for item in payload["clusters"] if item["kind"] == "helper_not_reused"
    ]
    assert helper == [], "a caller that references the canonical is not a match"


def test_region_external_effects_parameter_mutation() -> None:
    """A method call on a function input (``model.load_state_dict``) is an
    external effect: it mutates state the region does not own."""
    import ast as ast_module

    tree = ast_module.parse(
        "def run(model, tensor, scale):\n"
        "    model.load_state_dict(tensor)\n"
        "    device = model.device\n"
        "    if tensor.device != device:\n"
        '        raise ValueError("mismatch")\n'
        "    return tensor.mul(scale)\n"
    )
    fn = tree.body[0]
    fn_locals = {
        arg.arg
        for arg in list(fn.args.posonlyargs) + list(fn.args.args) + list(fn.args.kwonlyargs)
    }
    effects = scan_regions._external_effects(fn.body, fn_locals)
    assert "method_on_input:load_state_dict" in effects
    assert "method_on_input:mul" in effects
    assert "ValueError" in effects

    # A receiver created inside the region is region-local state, not an
    # input mutation.
    tree = ast_module.parse(
        "def build(items):\n"
        "    cache = Cache()\n"
        "    cache.update(items)\n"
        "    return cache\n"
    )
    fn = tree.body[0]
    fn_locals = {arg.arg for arg in fn.args.args}
    effects = scan_regions._external_effects(fn.body, fn_locals)
    assert not any(effect.startswith("method_on_input") for effect in effects)
