#!/usr/bin/env python3
"""Guided Layer-2 adjudication of a self-audit report.

Walks every candidate from a ``run_all`` report, one at a time, and records a
disposition: ``false positive`` writes a suppression entry to ``ignore.json``
plus a numbered lesson block to ``LESSONS.md``; the other dispositions are
recorded in the verdicts JSON only (they require code action, not
suppression). Decisions persist after each candidate, so quitting early loses
nothing and resuming skips already-adjudicated candidates.

Exit code 0: all candidates were adjudicated or the session was quit early.
Exit code 2: the report file is missing or malformed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_all

SKILL_DIR = Path(__file__).resolve().parent

# code -> (disposition, guidance line)
DISPOSITIONS = {
    "fp": ("false positive", "suppress in ignore.json + record LESSONS.md"),
    "td": ("true duplicate", "consolidate; fix code, no suppression"),
    "cd": ("compatibility debt", "migrate active callers, then remove"),
    "ia": ("independent audit", "retain separately and parity-test"),
    "va": ("valuable adapter", "retain and name by its role"),
    "ns": ("necessary specialization", "retain locally"),
}
PROMPT_CHOICES = "fp/td/cd/ia/va/ns/s/q"


def _flatten(report: dict) -> list[dict]:
    """Collect every candidate as {scanner, signature, display, detail}."""
    items = []
    for scanner, triples in run_all._candidate_signatures(
        report.get("scanners", {})
    ).items():
        for signature, display, detail in triples:
            items.append(
                {
                    "scanner": scanner,
                    "signature": signature,
                    "display": display,
                    "detail": detail,
                }
            )
    return items


def _render_detail(scanner: str, detail: dict) -> str:
    if scanner == "deadcode":
        return (
            f"status={detail.get('status')} py_refs={detail.get('py_refs')} "
            f"doc_refs={detail.get('doc_refs')}"
        )
    if scanner == "duplicates":
        return (
            f"priority={detail.get('priority')} reason={detail.get('priority_reason')}"
        )
    if scanner == "forks":
        left, right = detail["left"], detail["right"]
        return (
            f"sim={detail.get('similarity')} kind={detail.get('kind')} "
            f"sig={detail.get('signature_match')} "
            f"left={left['path']}:{left['qualname']} "
            f"right={right['path']}:{right['qualname']}"
        )
    if scanner == "contracts":
        return json.dumps(detail, ensure_ascii=False)[:400]
    if scanner == "capabilities":
        local, lib = detail["local"], detail["lib"]
        return (
            f"match={detail.get('match')} sig={detail.get('signature_match')} "
            f"local={local['path']}:{local['qualname']} "
            f"lib={lib['path']}:{lib['qualname']}"
        )
    if scanner == "hardcoded":
        return (
            f"pattern={detail.get('_pattern')} code={detail.get('code')} "
            f"hint={detail.get('suggestion')}"
        )
    if scanner == "style":
        return f"metric={detail.get('_metric')} text={detail.get('text')}"
    return ""


def _ignore_entries(scanner: str, detail: dict, note: str) -> list[tuple[str, dict]]:
    """Build (registry section, entry) pairs for a false-positive suppression.

    Section names match the keys the scanners read from ``ignore.json``
    (``contracts/<channel>`` nests under the ``contracts`` object).
    """
    if scanner == "deadcode":
        return [("deadcode", {"path": detail["path"], "reason": note})]
    if scanner == "duplicates":
        return [("duplicates", {"id": detail["id"], "reason": note})]
    if scanner == "forks":
        return [("forks", {"key": detail["key"], "reason": note})]
    if scanner == "capabilities":
        key = f"{detail['local']['path']}:{detail['local']['qualname']}"
        return [("capabilities", {"key": key, "reason": note})]
    if scanner == "contracts":
        channel = detail["_channel"]
        if channel == "forwarding_wrappers":
            key = f"{detail['path']}:{detail['line']}:{detail['name']}"
        elif channel == "same_name_contracts":
            key = f"{detail['path']}:{detail['line']}:{detail['_name']}"
        elif channel == "unreferenced_top_level_functions":
            key = f"{detail['path']}:{detail['line']}"
        elif channel == "defensive_param_loosening":
            key = f"{detail['path']}:{detail['line']}"
        elif channel == "env_written_not_read":
            key = f"{detail['path']}:{detail['var']}"
        else:  # experiment_as_library / cli_without_bootstrap / generation_path_without_env
            key = detail["path"]
        return [(f"contracts/{channel}", {"key": key, "reason": note})]
    if scanner == "hardcoded":
        return [
            ("hardcoded", {"path": detail["path"], "pattern": detail["_pattern"], "reason": note})
        ]
    if scanner == "style":
        return [
            ("style", {"path": detail["path"], "pattern": detail["_metric"], "reason": note})
        ]
    return []


def _merge_ignore(registry: dict, entries: list[tuple[str, dict]]) -> int:
    """Merge entries into the registry; return the number of new entries."""
    added = 0
    for section, entry in entries:
        if section.startswith("contracts/"):
            channel = section.split("/", 1)[1]
            channel_list = registry.setdefault("contracts", {}).setdefault(channel, [])
        else:
            channel_list = registry.setdefault(section, [])
        if entry not in channel_list:
            channel_list.append(entry)
            added += 1
    return added


def _append_lesson(lessons: Path, scanner: str, display: str, note: str) -> None:
    """Append a numbered lesson block; create the file when absent."""
    text = lessons.read_text(encoding="utf-8") if lessons.is_file() else ""
    numbers = [int(line[3:].split(".", 1)[0]) for line in text.splitlines()
               if line.startswith("## ")
               and line[3:].split(".", 1)[0].strip().isdigit()]
    number = (max(numbers) + 1) if numbers else 1
    section = (
        f"\n## {number}. {run_all._SCANNER_NAMES.get(scanner, scanner)}: "
        f"suppressed {display}\n"
        f"\n- **Case**: {display}\n"
        f"- **Lesson**: {note}\n"
        f"- **Implementation**: suppressed in ignore.json after semantic review.\n"
    )
    with lessons.open("a", encoding="utf-8") as handle:
        handle.write(section)


def _load_verdicts(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_verdicts(path: Path, verdicts: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(verdicts, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--report",
        type=Path,
        default=SKILL_DIR / "reports" / "latest.json",
        help="run_all report JSON to adjudicate",
    )
    ap.add_argument(
        "--ignore",
        type=Path,
        default=SKILL_DIR / "ignore.json",
        help="suppression registry to extend on false positives",
    )
    ap.add_argument(
        "--lessons",
        type=Path,
        default=SKILL_DIR / "LESSONS.md",
        help="lesson archive to append false-positive rationales to",
    )
    ap.add_argument(
        "--verdicts",
        type=Path,
        default=SKILL_DIR / "reports" / "verdicts.json",
        help="decision log; existing decisions are resumed from",
    )
    args = ap.parse_args(argv)

    if not args.report.is_file():
        print(f"error: report not found: {args.report}", file=sys.stderr)
        return 2
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read report: {exc}", file=sys.stderr)
        return 2

    candidates = _flatten(report)
    if not candidates:
        print("ADJUDICATE ok candidates=0")
        return 0

    registry = (
        json.loads(args.ignore.read_text(encoding="utf-8"))
        if args.ignore.is_file()
        else {}
    )
    verdicts = _load_verdicts(args.verdicts) or {
        "scanner": "self-audit-adjudicate",
        "report": str(args.report.resolve()),
        "adjudicated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "verdicts": [],
    }
    decided = {
        (item["scanner"], item["signature"]) for item in verdicts.get("verdicts", [])
    }

    pending = [
        item for item in candidates
        if (item["scanner"], item["signature"]) not in decided
    ]
    for index, item in enumerate(pending, start=1):
        print("=" * 72)
        print(
            f"[{index}/{len(pending)}] {item['scanner']} — {item['display']}"
        )
        print(f"  {_render_detail(item['scanner'], item['detail'])}")
        while True:
            answer = input(f"verdict [{PROMPT_CHOICES}]: ").strip().lower()
            if answer == "q":
                print(
                    f"ADJUDICATE quit at {index}/{len(pending)}; "
                    f"decisions so far are in {args.verdicts}"
                )
                return 0
            if answer == "s":
                record = {
                    "scanner": item["scanner"],
                    "signature": item["signature"],
                    "display": item["display"],
                    "disposition": "skip",
                }
                verdicts["verdicts"].append(record)
                _save_verdicts(args.verdicts, verdicts)
                break
            if answer in DISPOSITIONS:
                disposition, guidance = DISPOSITIONS[answer]
                record = {
                    "scanner": item["scanner"],
                    "signature": item["signature"],
                    "display": item["display"],
                    "disposition": disposition,
                }
                if disposition == "false positive":
                    while True:
                        note = input("reason (appends to LESSONS.md; required): ").strip()
                        if note:
                            break
                    entries = _ignore_entries(item["scanner"], item["detail"], note)
                    added = _merge_ignore(registry, entries)
                    args.ignore.write_text(
                        json.dumps(registry, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    if added:
                        _append_lesson(args.lessons, item["scanner"], item["display"], note)
                    record["note"] = note
                    record["suppressed"] = True
                else:
                    optional = input(f"optional note ({guidance}): ").strip()
                    if optional:
                        record["note"] = optional
                verdicts["verdicts"].append(record)
                _save_verdicts(args.verdicts, verdicts)
                break
            print(f"  invalid choice; use one of {PROMPT_CHOICES}", file=sys.stderr)

    total = len(verdicts.get("verdicts", []))
    fp = sum(1 for v in verdicts["verdicts"] if v["disposition"] == "false positive")
    print(f"ADJUDICATE ok total={total} false_positives={fp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
