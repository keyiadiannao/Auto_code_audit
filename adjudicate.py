#!/usr/bin/env python3
"""Guided Layer-2 adjudication of a self-audit report.

Walks every candidate from a ``run_all`` report, one at a time, and records a
disposition: ``false positive`` writes a suppression entry to ``ignore.json``
plus a numbered lesson block to ``LESSONS.md``; the other dispositions are
recorded in the verdicts JSON only (they require code action, not
suppression). Decisions persist after each candidate, so quitting early loses
nothing and resuming skips already-adjudicated candidates.

Exit code 0: all candidates were adjudicated or the session was quit early.
Exit code 1: ``--check`` found pending candidates (not yet adjudicated).
Exit code 2: the report file is missing or malformed, or stdin was closed
             during interactive adjudication.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
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


# Identity fields per suppression section.  Only these fields locate the
# target in the codebase; reason/date/owner are review annotations, not
# identity.  contracts/<channel> entries always use ``key``.
_IDENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "deadcode": ("path",),
    "duplicates": ("id",),
    "forks": ("key",),
    "capabilities": ("key",),
    "hardcoded": ("path", "pattern"),
    "style": ("path", "pattern"),
}


def _suppression_identity(section: str, entry: dict) -> tuple:
    """Return a hashable identity for a suppression entry.

    Two entries collide when their identity tuples match, regardless of
    ``reason``/``date``/``owner`` annotations.  This means re-suppressing
    the same candidate with a different reason keeps the first record.
    """
    if section.startswith("contracts/"):
        fields = ("key",)
    else:
        fields = _IDENTITY_FIELDS.get(section, ())
    return tuple((f, entry.get(f)) for f in fields)


def _suppression_meta(date: str | None, owner: str | None) -> dict:
    """Stamp a suppression entry with review provenance (date always present)."""
    meta = {"date": date or dt.date.today().isoformat()}
    if owner:
        meta["owner"] = owner
    return meta


def _ignore_entries(
    scanner: str, detail: dict, note: str, date: str | None = None, owner: str | None = None
) -> list[tuple[str, dict]]:
    """Build (registry section, entry) pairs for a false-positive suppression.

    Section names match the keys the scanners read from ``ignore.json``
    (``contracts/<channel>`` nests under the ``contracts`` object).  Every
    entry carries a ``date`` stamp and, when an owner is known, an ``owner``.
    """
    def stamped(entry: dict) -> dict:
        return {**entry, **_suppression_meta(date, owner)}

    if scanner == "deadcode":
        return [("deadcode", stamped({"path": detail["path"], "reason": note}))]
    if scanner == "duplicates":
        return [("duplicates", stamped({"id": detail["id"], "reason": note}))]
    if scanner == "forks":
        return [("forks", stamped({"key": detail["key"], "reason": note}))]
    if scanner == "capabilities":
        key = f"{detail['local']['path']}:{detail['local']['qualname']}"
        return [("capabilities", stamped({"key": key, "reason": note}))]
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
        return [(f"contracts/{channel}", stamped({"key": key, "reason": note}))]
    if scanner == "hardcoded":
        return [
            ("hardcoded", stamped({"path": detail["path"], "pattern": detail["_pattern"], "reason": note}))
        ]
    if scanner == "style":
        return [
            ("style", stamped({"path": detail["path"], "pattern": detail["_metric"], "reason": note}))
        ]
    return []


def _merge_ignore(registry: dict, entries: list[tuple[str, dict]]) -> int:
    """Merge entries into the registry; return the number of new entries.

    Two entries collide when their suppression identity matches — that is,
    when the fields that locate the target in the codebase are identical.
    ``reason``/``date``/``owner`` are annotations and do not participate in
    identity, so re-suppressing an already-suppressed candidate with a
    different reason keeps the first suppression record (with its original
    review date).
    """
    added = 0
    for section, entry in entries:
        if section.startswith("contracts/"):
            channel = section.split("/", 1)[1]
            channel_list = registry.setdefault("contracts", {}).setdefault(channel, [])
        else:
            channel_list = registry.setdefault(section, [])
        identity = _suppression_identity(section, entry)
        if any(
            _suppression_identity(section, existing) == identity
            for existing in channel_list
        ):
            continue
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
    lessons.parent.mkdir(parents=True, exist_ok=True)
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


def _upsert_verdict(verdicts: dict, record: dict) -> None:
    """Replace any existing verdict for the same candidate, else append.

    A ``skip`` is a session-level deferral, not a permanent decision: the
    candidate reappears on resume.  When the user later gives it a real
    verdict, the old skip record is replaced rather than left alongside.
    """
    key = (record["scanner"], record["signature"])
    records = verdicts.setdefault("verdicts", [])
    for i, existing in enumerate(records):
        if (existing.get("scanner"), existing.get("signature")) == key:
            records[i] = record
            return
    records.append(record)


def _path_from_state(value: str | None, root: Path, fallback: Path) -> Path:
    if not value:
        return fallback.resolve()
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve()


def _resolve_path(
    explicit: Path | None,
    state_value: str | None,
    root: Path,
    fallback: Path,
) -> Path:
    if explicit is not None:
        return explicit.resolve()
    return _path_from_state(state_value, root, fallback)


def _git_owner(root: Path) -> str | None:
    """Read the repository user name for suppression ownership, when available."""
    try:
        proc = subprocess.run(
            ["git", "config", "user.name"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    name = proc.stdout.strip()
    return name or None


def _project_root(
    report_path: Path, report: dict, explicit_root: Path | None
) -> Path:
    if explicit_root is not None:
        return explicit_root.resolve()
    state = report.get("state")
    if isinstance(state, dict) and state.get("project_root"):
        return Path(state["project_root"]).resolve()
    if report_path.parent.name == "reports":
        return report_path.parent.parent.resolve()
    return report_path.parent.resolve()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--root",
        type=Path,
        default=None,
        help="audited project root (default: report state or current directory)",
    )
    ap.add_argument(
        "--report",
        type=Path,
        default=None,
        help="run_all report JSON to adjudicate",
    )
    ap.add_argument(
        "--ignore",
        type=Path,
        default=None,
        help="suppression registry (default: report project state)",
    )
    ap.add_argument(
        "--lessons",
        type=Path,
        default=None,
        help="lesson archive (default: <project-root>/LESSONS.md)",
    )
    ap.add_argument(
        "--verdicts",
        type=Path,
        default=None,
        help="decision log (default: report directory/verdicts.json)",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="check for pending candidates without prompting or writing files",
    )
    ap.add_argument(
        "--owner",
        default=None,
        help="suppression owner stamped into ignore.json (default: git user.name)",
    )
    args = ap.parse_args(argv)

    root = args.root.resolve() if args.root is not None else Path.cwd().resolve()
    report_path = (
        args.report.resolve()
        if args.report is not None
        else root / "reports" / "latest.json"
    )
    if not report_path.is_file():
        print(f"error: report not found: {report_path}", file=sys.stderr)
        return 2
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read report: {exc}", file=sys.stderr)
        return 2

    root = _project_root(report_path, report, args.root)
    owner = args.owner or _git_owner(root)
    state = report.get("state") if isinstance(report.get("state"), dict) else {}
    args.report = report_path
    args.ignore = _resolve_path(
        args.ignore,
        state.get("ignore_file"),
        root,
        root / "ignore.json",
    )
    args.lessons = _resolve_path(
        args.lessons,
        state.get("lessons_file"),
        root,
        root / "LESSONS.md",
    )
    args.verdicts = _resolve_path(
        args.verdicts,
        state.get("verdicts_file"),
        root,
        report_path.parent / "verdicts.json",
    )

    candidates = _flatten(report)
    if not candidates:
        print("ADJUDICATE ok candidates=0")
        return 0

    if args.ignore.is_file():
        try:
            registry = json.loads(args.ignore.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: cannot read suppression registry: {exc}", file=sys.stderr)
            return 2
        if not isinstance(registry, dict):
            print("error: suppression registry must be a JSON object", file=sys.stderr)
            return 2
    else:
        registry = {}

    # Stamp schema version so future migrations can detect the format.
    if "schema_version" not in registry:
        registry["schema_version"] = 1
    verdicts = _load_verdicts(args.verdicts) or {
        "scanner": "self-audit-adjudicate",
        "report": str(args.report.resolve()),
        "adjudicated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "verdicts": [],
    }
    # ``skip`` is a session-level deferral, not a final decision: skipped
    # candidates reappear on resume and still count as pending for --check.
    decided = {
        (item["scanner"], item["signature"])
        for item in verdicts.get("verdicts", [])
        if item.get("disposition") != "skip"
    }

    pending = [
        item for item in candidates
        if (item["scanner"], item["signature"]) not in decided
    ]
    if args.check:
        if pending:
            print(f"ADJUDICATE CHECK_FAIL pending={len(pending)}", file=sys.stderr)
            return 1
        print(f"ADJUDICATE CHECK_PASS candidates={len(candidates)}")
        return 0

    for index, item in enumerate(pending, start=1):
        print("=" * 72)
        print(
            f"[{index}/{len(pending)}] {item['scanner']} — {item['display']}"
        )
        print(f"  {_render_detail(item['scanner'], item['detail'])}")
        while True:
            try:
                answer = input(f"verdict [{PROMPT_CHOICES}]: ").strip().lower()
            except EOFError:
                print(
                    "\nerror: stdin closed; use --check for non-interactive mode",
                    file=sys.stderr,
                )
                return 2
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
                _upsert_verdict(verdicts, record)
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
                        try:
                            note = input("reason (appends to LESSONS.md; required): ").strip()
                        except EOFError:
                            print(
                                "\nerror: stdin closed; use --check for non-interactive mode",
                                file=sys.stderr,
                            )
                            return 2
                        if note:
                            break
                    entries = _ignore_entries(item["scanner"], item["detail"], note, owner=owner)
                    added = _merge_ignore(registry, entries)
                    args.ignore.parent.mkdir(parents=True, exist_ok=True)
                    args.ignore.write_text(
                        json.dumps(registry, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    if added:
                        _append_lesson(args.lessons, item["scanner"], item["display"], note)
                    record["note"] = note
                    record["suppressed"] = True
                else:
                    try:
                        optional = input(f"optional note ({guidance}): ").strip()
                    except EOFError:
                        optional = ""
                    if optional:
                        record["note"] = optional
                _upsert_verdict(verdicts, record)
                _save_verdicts(args.verdicts, verdicts)
                break
            print(f"  invalid choice; use one of {PROMPT_CHOICES}", file=sys.stderr)

    total = len(verdicts.get("verdicts", []))
    fp = sum(1 for v in verdicts["verdicts"] if v["disposition"] == "false positive")
    print(f"ADJUDICATE ok total={total} false_positives={fp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
