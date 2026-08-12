#!/usr/bin/env python3
"""Scan paper TeX prose for AI-typical writing signals.

All text-level metrics are computed on LaTeX-stripped prose (comments, math
environments, tables, and citation commands removed). Em-dash and sentence
statistics measured on raw source are dominated by math notation and produce
false positives; the stripping below is the verified baseline (2026-08-09).

Hits are review candidates, not verdicts. Technical enumerations (section maps,
data tables, parameter lists) legitimately use semicolon chains; each chain is
tagged with a ``technical`` hint when a majority of subclauses carry numbers.

Metrics:
  semicolon_chains  sentences with >= 2 semicolons; ``triad_like`` marks
                    >= 3 subclauses of similar length (parallel construction,
                    the rhetorical signature, as opposed to technical lists)
  em_dash_rate      em-dashes per 100 words (>= threshold reported)
  negative_anaphora "not just X---Y" / "not only X but Y" constructions
  burstiness        sentence-length sd/mean (AI-generated text is uniform,
                    below threshold reported)
  excess_vocab      AI-favored vocabulary, with statistical/technical-context
                    exemption (significant + stat token, robust to/against)
  template_openers  sentences opened by formulaic connectors
  note_that         sentences opened by "Note that"
  pm_format         TeX-layer check: bare \\pm without braces (the paper
                    convention is $x{\\pm}y$; \\!\\pm\\! table forms are exempt)
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

import _audit_config
from _scanner_common import load_ignore, short_hash as _short_hash, write_json as _write_json

EXCESS_VOCAB = [
    "additionally",
    "moreover",
    "furthermore",
    "delve",
    "leverage",
    "utilize",
    "pivotal",
    "crucial",
    "notably",
    "significant",
    "robust",
]
STAT_TOKENS = re.compile(
    r"\b(p[- ]?value|p{=}?|CI|confidence|std|sd|mean|accuracy|error|"
    r"variance|statistic|t[- ]?stat|F[- ]?stat|n{=}|dof|p{<}|p{>})\b",
    re.IGNORECASE,
)
TEMPLATE_OPENERS = [
    "However",
    "Moreover",
    "Furthermore",
    "Additionally",
    "In summary",
    "Notably",
    "Specifically",
    "Importantly",
    "Overall",
    "In conclusion",
]

ENV_RE = re.compile(
    r"\\begin\{(\*?[a-zA-Z]+\*?)\}"
    r"(?:\[[^\]]*\])?.*?\\end\{\1\}",
    re.S,
)
REF_COMMAND_RE = re.compile(
    r"\\(?:cite[tp]?|ref|autoref|label|url|texttt|codepath|mask|mainref)"
    r"(?:\[[^\]]*\])?\{[^}]*\}"
)
# Section/caption braces are matched with a brace counter, not [^}]*:
# a heading such as \section{...\texorpdfstring{$d^{R}=...$}{...}} contains
# closing braces inside math, and a naive [^}]* stops at the first one,
# leaving an orphan $ that corrupts all later $...$ pairing.
_HEADING_PREFIX_RE = re.compile(
    r"\\(?:sub)*section\*?\{|\\paragraph\*?\{"
    r"|\\caption(?:\[[^\]]*\])?\{"
)


def _balanced_end(text: str, brace_pos: int) -> int:
    """Index just after the '}' matching '{' at brace_pos (backslashes skip)."""
    depth = 0
    i = brace_pos
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _blank_headings(src: str) -> str:
    """Blank headings/captions with span-preserving whitespace."""
    out: list[str] = []
    pos = 0
    for pm in _HEADING_PREFIX_RE.finditer(src):
        end = _balanced_end(src, pm.end() - 1)
        out.append(src[pos:pm.start()])
        out.append(re.sub(r"[^\n]", " ", src[pm.start():end]))
        pos = end
    out.append(src[pos:])
    return "".join(out)
TEXT_KEEP_COMMAND_RE = re.compile(r"\\(?:emph|textbf|textit)\{([^}]*)\}")
MATH_INLINE_RE = re.compile(r"\\\(.*?\\\)", re.S)
MATH_DISPLAY_RE = re.compile(r"\\\[.*?\\\]", re.S)
DOLLAR_MATH_RE = re.compile(r"(?<!\\)\$\$.*?(?<!\\)\$\$|(?<!\\)\$[^$]*\$", re.S)
COMMENT_RE = re.compile(r"(?<!\\)%.*")
COMMAND_NAME_RE = re.compile(r"\\[a-zA-Z@]+\*?")
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(])")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*[A-Za-z]|[A-Za-z]")
EM_DASH_RE = re.compile(r"---|—")
NEG_ANAPHORA_RE = re.compile(
    r"\bnot just\b[^.!?]{0,140}?EMDASH|\bnot only\b[^.!?]{0,140}?\bbut\b",
    re.I,
)
NOTE_THAT_RE = re.compile(r"^Note that\b", re.I)
BARE_PM_RE = re.compile(r"(?<!\\!)\\pm(?![{!}])")

DEFAULT_TEX_DIR = "docs"
DEFAULT_TEX_FILES = None  # None = scan all *.tex under --tex-dir recursively
DEFAULT_EXCLUDE_PARTS = {"frozen", "archive", "legacy", "__pycache__"}

DEFAULT_EMDASH_THRESHOLD = 0.5   # per 100 words
DEFAULT_BURSTINESS_THRESHOLD = 0.4


def strip_tex(src: str, mark_math: bool = False) -> tuple[str, int, int]:
    """Strip LaTeX down to readable prose.

    Replacement is span-preserving: every stripped construct (comments,
    headings/captions, environments, math, citation commands) becomes
    whitespace of the same span, so line numbers in the returned text match
    the original source exactly (verified baseline 2026-08-09). With
    mark_math=True each math span additionally leaves a " [MATH] " tag for
    technical-classification use; strip the tag before any word-counting.

    Returns (stripped_text, leading_newlines_removed, trailing_newlines_removed).
    The final ``.strip()`` removes the body's leading whitespace (typically
    the newline right after ``\\begin{document}``); positions in the returned
    text are therefore shifted back by ``leading_newlines_removed`` relative
    to the body slice, and line-number mapping must account for it.
    """
    def _blank(m: re.Match) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))

    def _math(m: re.Match) -> str:
        if not mark_math:
            return re.sub(r"[^\n]", " ", m.group(0))
        return re.sub(r"[^\n]", " ", m.group(0)) + " [MATH] "

    match = re.search(r"\\begin\{document\}", src)
    if match:
        src = src[match.end():]
    # \end{document} never pairs after the preamble cut (ENV_RE needs a
    # \begin{...} match); blank it explicitly so the closing tag cannot leak
    # a stray "document" word into prose stats.
    src = re.sub(r"\\end\{document\}", " ", src)
    src = COMMENT_RE.sub(_blank, src)
    src = ENV_RE.sub(_blank, src)
    src = REF_COMMAND_RE.sub(_blank, src)
    src = _blank_headings(src)
    src = TEXT_KEEP_COMMAND_RE.sub(r"\1", src)
    src = MATH_INLINE_RE.sub(_math, src)
    src = MATH_DISPLAY_RE.sub(_math, src)
    src = DOLLAR_MATH_RE.sub(_math, src)
    # Safety net: any dollar left unpaired by DOLLAR_MATH_RE is a stray
    # (orphan from a half-stripped construct); blank it alone rather than
    # letting it corrupt prose. (Paper corpus contains no escaped \$.)
    src = re.sub(r"(?<!\\)\$", " ", src)
    src = COMMAND_NAME_RE.sub(" ", src)
    src = re.sub(r"[\-]{2,}", lambda m: " " if len(m.group(0)) == 2 else " EMDASH ", src)
    src = re.sub(r"[{}~^&_=#]|\\[.,;:!?]", " ", src)
    src = re.sub(r"[ \t]+", " ", src)
    stripped = src.strip()
    lead_ws = len(src) - len(src.lstrip())
    tail_ws = len(src) - len(src.rstrip())
    lead_nl = src.count("\n", 0, lead_ws)
    tail_nl = src.count("\n", max(0, len(src) - tail_ws), len(src))
    return stripped, lead_nl, tail_nl


def _iter_sentences(prose: str) -> list[tuple[str, int]]:
    """Yield (sentence_text, start_pos) pairs preserving prose positions."""
    out: list[tuple[str, int]] = []
    start = 0
    for m in SENTENCE_RE.finditer(prose):
        sent = prose[start:m.start()].strip()
        if sent:
            out.append((sent, start))
        start = m.end()
    tail = prose[start:].strip()
    if tail:
        out.append((tail, start))
    return out


def _line_no(text: str, base_newlines: int, pos: int) -> int:
    """Map a position in ``text`` back to a 1-based source line number.

    ``base_newlines`` is the number of newlines in the raw source up to the
    end of ``\\begin{document}``. Positions must come from the same string
    passed here: sentence positions live in *marked* space (math spans carry
    " [MATH] " tags, which lengthen the text), regex positions live in
    *prose* space (tags collapsed). The newline layout is identical in both,
    so the count is correct either way; mixing spaces would clamp positions
    near the document end (prose is shorter) to the last line.
    """
    return text.count("\n", 0, pos) + base_newlines + 1


def _words(prose: str) -> list[str]:
    return WORD_RE.findall(prose)


def _mean_std(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    if len(values) == 1 or mean == 0:
        return mean, 0.0
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return mean, variance ** 0.5


def _candidate_id(metric: str, path: str, text: str) -> str:
    return _short_hash(metric, path, text)


def analyze_prose(
    path: Path,
    rel: str,
    src: str,
    excess_vocab: list[str] = EXCESS_VOCAB,
    template_openers: list[str] = TEMPLATE_OPENERS,
    threshold_emdash: float = DEFAULT_EMDASH_THRESHOLD,
    threshold_burstiness: float = DEFAULT_BURSTINESS_THRESHOLD,
) -> dict:
    """Run all prose-level metrics on one TeX file."""
    template_openers_re = re.compile(
        r"^(?:" + r"|".join(re.escape(word) for word in template_openers) + r")\b[,\s]"
    )
    preamble_match = re.search(r"\\begin\{document\}", src)
    # Newlines in the raw source up to the body start, plus any newlines the
    # final .strip() removed from the front of the stripped text (positions
    # in marked/prose live past that stripped prefix).
    base_newlines = (src.count("\n", 0, preamble_match.end()) if preamble_match else 0)
    marked, lead_nl, _tail_nl = strip_tex(src, mark_math=True)
    base_newlines += lead_nl
    prose = marked.replace(" [MATH] ", " ")
    words = _words(prose)
    word_count = len(words)
    sentences = _iter_sentences(marked)

    lengths = [_words(sent.replace(" [MATH] ", " ")).__len__() for sent, _ in sentences]
    mean_len, sd_len = _mean_std([float(v) for v in lengths])
    burstiness = sd_len / mean_len if mean_len else 0.0
    em_dashes = len(EM_DASH_RE.findall(prose))
    em_dash_per_100w = em_dashes / word_count * 100 if word_count else 0.0

    hits: dict[str, list[dict]] = {metric: [] for metric in (
        "semicolon_chains", "negative_anaphora", "template_openers",
        "note_that", "excess_vocab", "em_dash_rate", "burstiness",
    )}

    # --- semicolon chains ---------------------------------------------------
    for sent, pos in sentences:
        subclauses = [part.strip() for part in sent.split(";") if part.strip()]
        if len(subclauses) < 3:
            continue
        sub_lengths = [
            _words(part.replace(" [MATH] ", " ")).__len__() for part in subclauses
        ]
        _, sub_sd = _mean_std([float(v) for v in sub_lengths])
        sub_mean = sum(sub_lengths) / len(sub_lengths)
        similar = sub_mean > 0 and sub_sd / sub_mean < 0.45
        technical = (
            sum(1 for part in subclauses if re.search(r"\d|\[MATH\]", part))
            / len(subclauses) >= 0.5
        )
        hits["semicolon_chains"].append({
            "id": _candidate_id("semicolon_chains", rel, sent),
            "path": rel,
            "line": _line_no(marked, base_newlines, pos),
            "text": sent[:220],
            "subclause_count": len(subclauses),
            "triad_like": similar,
            "technical": technical,
            "severity": "high" if similar and not technical else "medium",
            "suggestion": (
                "Parallel semicolon chain (rhetorical AI signature). Split into "
                "separate sentences unless this is a technical enumeration."
            ),
        })

    # --- negative anaphora ---------------------------------------------------
    for match in NEG_ANAPHORA_RE.finditer(prose):
        window = prose[max(0, match.start() - 60):match.end() + 40]
        hits["negative_anaphora"].append({
            "id": _candidate_id("negative_anaphora", rel, window),
            "path": rel,
            "line": _line_no(prose, base_newlines, match.start()),
            "text": window[:220],
            "severity": "medium",
            "suggestion": (
                "'not just X---Y' contrast is an AI-typical rhetorical device; "
                "consider direct statement."
            ),
        })

    # --- template openers -----------------------------------------------------
    for sent, pos in sentences:
        if template_openers_re.match(sent):
            hits["template_openers"].append({
                "id": _candidate_id("template_openers", rel, sent),
                "path": rel,
                "line": _line_no(marked, base_newlines, pos),
                "text": sent[:160],
                "severity": "medium",
                "suggestion": "Formulaic sentence opener; consider removing or varying.",
            })

    # --- note that -------------------------------------------------------------
    for sent, pos in sentences:
        if NOTE_THAT_RE.match(sent):
            hits["note_that"].append({
                "id": _candidate_id("note_that", rel, sent),
                "path": rel,
                "line": _line_no(marked, base_newlines, pos),
                "text": sent[:160],
                "severity": "low",
                "suggestion": "'Note that' opener; direct statement is usually stronger.",
            })

    # --- excess vocabulary ------------------------------------------------------
    for token in excess_vocab:
        pattern = re.compile(rf"\b{re.escape(token)}\b", re.I)
        for match in pattern.finditer(prose):
            window = prose[max(0, match.start() - 40):match.end() + 60]
            technical = False
            if token in ("significant", "robust"):
                tail = prose[match.end():match.end() + 80]
                if STAT_TOKENS.search(tail) or (token == "robust" and re.search(
                    r"\b(?:to|against|w[.]?r[.]?t[.]?)\b", tail
                )):
                    technical = True
            if technical:
                continue
            hits["excess_vocab"].append({
                "id": _candidate_id("excess_vocab", rel, window),
                "path": rel,
                "line": _line_no(prose, base_newlines, match.start()),
                "text": window[:220],
                "word": token,
                "severity": "medium",
                "suggestion": (
                    f"'{token}' is over-represented in AI-generated text; "
                    "check whether a more specific word fits."
                ),
            })

    # --- aggregate rate metrics ---------------------------------------------------
    if em_dash_per_100w >= threshold_emdash:
        hits["em_dash_rate"].append({
            "id": _candidate_id("em_dash_rate", rel, prose[:80]),
            "path": rel,
            "text": f"{em_dashes} em-dashes / {word_count} words "
                    f"({em_dash_per_100w:.2f} per 100w)",
            "severity": "medium",
            "suggestion": (
                "Em-dash is a strong AI-writing marker (ChatGPT unofficial "
                "signature); consider hyphen or restructure."
            ),
        })
    if burstiness < threshold_burstiness:
        hits["burstiness"].append({
            "id": _candidate_id("burstiness", rel, prose[:80]),
            "path": rel,
            "text": f"sentence-length sd/mean {burstiness:.2f} over "
                    f"{len(sentences)} sentences (AI-typical uniformity)",
            "severity": "medium",
            "suggestion": "Uniform sentence length; vary short and long sentences.",
        })

    stats = {
        "words": word_count,
        "sentences": len(sentences),
        "mean_sentence_words": round(mean_len, 2),
        "sd_sentence_words": round(sd_len, 2),
        "burstiness": round(burstiness, 3),
        "em_dash": em_dashes,
        "em_dash_per_100w": round(em_dash_per_100w, 3),
    }
    return {"stats": stats, "hits": hits, "prose_preview": prose[:200],
            "_sentence_lengths": lengths}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repo root (default: script's repo)",
    )
    ap.add_argument("--package", default="src",
                    help="accepted for run_all compatibility; TeX scanning "
                         "is root-scoped, not package-scoped")
    ap.add_argument(
        "--tex-dir",
        default=None,
        help="TeX directory relative to root (default: %(default)s)",
    )
    ap.add_argument(
        "--tex-files", nargs="*", default=DEFAULT_TEX_FILES,
        help="TeX files inside --tex-dir; default: all *.tex recursively "
             "excluding DEFAULT_EXCLUDE_PARTS",
    )
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--ignore", type=Path, default=None)
    ap.add_argument("--threshold-emdash", type=float, default=None)
    ap.add_argument(
        "--threshold-burstiness", type=float, default=None
    )
    args = ap.parse_args(argv)

    repo = args.root.resolve()
    cfg = _audit_config.load_config(repo)
    style_cfg = cfg.get("style", {})
    tex_dir_name = _audit_config.pick(args.tex_dir, style_cfg, "tex_dir", DEFAULT_TEX_DIR)
    threshold_emdash = _audit_config.pick(
        args.threshold_emdash, style_cfg, "threshold_emdash", DEFAULT_EMDASH_THRESHOLD
    )
    threshold_burstiness = _audit_config.pick(
        args.threshold_burstiness,
        style_cfg,
        "threshold_burstiness",
        DEFAULT_BURSTINESS_THRESHOLD,
    )
    excess_vocab = _audit_config.as_string_list(
        style_cfg.get("excess_vocab"), EXCESS_VOCAB
    )
    template_openers = _audit_config.as_string_list(
        style_cfg.get("template_openers"), TEMPLATE_OPENERS
    )
    exclude_parts = set(
        _audit_config.as_string_list(
            style_cfg.get("exclude_parts"), sorted(DEFAULT_EXCLUDE_PARTS)
        )
    )
    tex_dir = (repo / tex_dir_name).resolve()
    try:
        tex_dir.relative_to(repo)
    except ValueError:
        print(f"error: --tex-dir escapes root: {tex_dir}", file=sys.stderr)
        return 2

    ignore = load_ignore(args.ignore)
    ignored_entries = ignore.get("style", [])
    ignored_ids = {entry.get("id", "") for entry in ignored_entries}
    ignored_pairs = {
        (entry.get("path", ""), entry.get("pattern", ""))
        for entry in ignored_entries
    }

    files: list[Path] = []
    if args.tex_files:
        for name in args.tex_files:
            path = tex_dir / name
            if path.is_file():
                files.append(path)
    else:
        for path in tex_dir.rglob("*.tex"):
            if path.is_file() and not any(part in exclude_parts for part in path.parts):
                files.append(path)

    per_file: dict[str, dict] = {}
    hits: dict[str, list[dict]] = {
        metric: [] for metric in (
            "semicolon_chains", "negative_anaphora", "template_openers",
            "note_that", "excess_vocab", "em_dash_rate", "burstiness",
            "pm_format",
        )
    }
    ignored_hits: list[dict] = []

    all_lengths: list[float] = []

    for path in sorted(files):
        rel = path.relative_to(repo).as_posix()
        src = path.read_text(encoding="utf-8", errors="replace")
        result = analyze_prose(
            path,
            rel,
            src,
            excess_vocab=excess_vocab,
            template_openers=template_openers,
            threshold_emdash=threshold_emdash,
            threshold_burstiness=threshold_burstiness,
        )
        per_file[rel] = result["stats"]
        all_lengths.extend(float(n) for n in result.get("_sentence_lengths", []))
        for metric, items in result["hits"].items():
            for item in items:
                if (item["id"] in ignored_ids
                        or (rel, metric) in ignored_pairs):
                    item["metric"] = metric
                    ignored_hits.append(item)
                else:
                    hits[metric].append(item)

        # TeX-layer: bare \pm (paper convention $x{\pm}y$; \!\pm\! exempt)
        for match in BARE_PM_RE.finditer(src):
            line = src.count("\n", 0, match.start()) + 1
            snippet = src[max(0, match.start() - 20):match.end() + 12]
            hit = {
                "id": _candidate_id("pm_format", rel, snippet),
                "path": rel,
                "line": line,
                "text": f"bare \\pm: ...{snippet}...",
                "severity": "low",
                "suggestion": "Paper convention is {\\pm}; \\!\\pm\\! is the "
                             "only exempt spacing-compressed table form.",
            }
            if hit["id"] in ignored_ids or (rel, "pm_format") in ignored_pairs:
                hit["metric"] = "pm_format"
                ignored_hits.append(hit)
            else:
                hits["pm_format"].append(hit)

    aggregated = {
        "words": sum(stats["words"] for stats in per_file.values()),
        "sentences": sum(stats["sentences"] for stats in per_file.values()),
        "em_dash": sum(stats["em_dash"] for stats in per_file.values()),
        "em_dash_per_100w": 0.0,
        "burstiness_overall": 0.0,
    }
    total_words = aggregated["words"]
    if total_words:
        aggregated["em_dash_per_100w"] = round(
            aggregated["em_dash"] / total_words * 100, 3
        )
    if len(all_lengths) > 1:
        mean, sd = _mean_std(all_lengths)
        aggregated["burstiness_overall"] = round(sd / mean, 3) if mean else 0.0

    payload = {
        "scanner": "style",
        "schema_version": 1,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "package": args.package,
        "tex_dir": tex_dir_name,
        "files_scanned": [path.relative_to(repo).as_posix() for path in files],
        "prose_stats": {"per_file": per_file, "aggregate": aggregated},
        "hits": {name: values for name, values in hits.items() if values},
        "ignored": ignored_hits,
    }
    _write_json(args.json, payload)

    hit_count = sum(len(values) for values in hits.values())
    print(f"STYLE_SCAN tex_dir={tex_dir_name} files={len(files)} "
          f"hits={hit_count} ignored={len(ignored_hits)}")
    for metric, values in hits.items():
        if values:
            print(f"  [{values[0]['severity']}] {metric}: {len(values)} hits")
            for hit in values[:12]:
                line = hit.get("line", "-")  # rate metrics are file-scoped
                print(f"      {hit['path']}:{line} {hit['text'][:100]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
