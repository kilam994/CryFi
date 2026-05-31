"""Wordlist generator endpoints — one comprehensive generator.

A request carries a list of *strategies* (phone / charset / combinator / social);
``/generate`` runs them all and merges the output into a single deduplicated
``.txt`` in the wordlists directory. ``/estimate`` predicts the combined size
without generating, so the UI can warn before a huge run. A hard line cap guards
disk/memory.
"""
from __future__ import annotations

import itertools

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import config
from ..core import wordgen, wordgen_jobs
from ..core.security import clean_filename, safe_path_in

router = APIRouter(prefix="/api/wordgen", tags=["wordgen"])

# "words" is the unified word generator; combinator/social kept for compatibility.
_MODES = {"charset", "phone", "words", "combinator", "social"}
_NO_DEDUPE = {"charset", "phone"}  # already unique by construction

_MAX_WORDS = 500
_MAX_WORD_LEN = 128
_MAX_LEN_BOUND = 32


class StrategySpec(BaseModel):
    mode: str
    # charset
    presets: list[str] = []
    custom: str = ""
    min_len: int = 1
    max_len: int = 4
    # phone
    mask: str = ""
    wildcard: str = "X"
    strip_nondigits: bool = False
    # combinator + social
    words: list[str] = []
    # combinator
    separators: list[str] = Field(default_factory=lambda: [""])
    min_parts: int = 1
    max_parts: int = 2
    # social
    numbers: list[str] = []
    years: list[str] = []
    suffixes: list[str] | None = None
    use_cases: bool = True
    use_leet: bool = False
    append_numbers: bool = True
    combine_pairs: bool = True
    reverse: bool = False


class WordgenRequest(BaseModel):
    filename: str | None = None
    strategies: list[StrategySpec] = []
    # Output options for large/streamed generation.
    target_lines: int | None = None      # max lines to produce (None => default)
    lines_per_file: int = 0              # split size; 0 => single file
    dedupe: bool = True                  # in-memory dedupe (auto-off for charset/phone)


def _validate(s: StrategySpec) -> None:
    if s.mode not in _MODES:
        raise HTTPException(400, f"Unknown mode: {s.mode!r}")
    if s.mode == "charset":
        if not (1 <= s.min_len <= s.max_len <= _MAX_LEN_BOUND):
            raise HTTPException(400, f"Charset: lengths must satisfy 1 ≤ min ≤ max ≤ {_MAX_LEN_BOUND}")
        if not wordgen.build_charset(s.presets, s.custom):
            raise HTTPException(400, "Charset: pick a preset or add custom characters")
    elif s.mode == "phone":
        wc = s.wildcard or "X"
        if len(wc) != 1:
            raise HTTPException(400, "Phone: wildcard must be a single character")
        if wc.upper() not in s.mask.upper():
            raise HTTPException(400, f"Phone: mask has no '{wc}' wildcard positions")
        if s.mask.upper().count(wc.upper()) > 12:
            raise HTTPException(400, "Phone: too many wildcards (max 12 → 10¹² lines)")
    else:  # words | combinator | social
        words = [w for w in s.words if w and w.strip()]
        if not words:
            raise HTTPException(400, "Target words: provide at least one base word")
        if len(words) > _MAX_WORDS:
            raise HTTPException(400, f"Target words: too many words (max {_MAX_WORDS})")
        if any(len(w) > _MAX_WORD_LEN for w in words):
            raise HTTPException(400, f"Target words: each word must be ≤ {_MAX_WORD_LEN} chars")
        if s.mode in {"combinator", "words"} and not (1 <= s.min_parts <= s.max_parts <= 6):
            raise HTTPException(400, "Combine words: parts must satisfy 1 ≤ min ≤ max ≤ 6")


def _estimate_one(s: StrategySpec) -> int | None:
    """Predicted line count for a strategy, or None when it can't be predicted."""
    if s.mode == "charset":
        cs = wordgen.build_charset(s.presets, s.custom)
        return wordgen.charset_count(len(cs), s.min_len, s.max_len)
    if s.mode == "phone":
        return wordgen.phone_count(s.mask, s.wildcard)
    if s.mode == "combinator":
        words = wordgen._dedupe_keep(w.strip() for w in s.words)
        return wordgen.combinator_count(len(words), len(s.separators or [""]), s.min_parts, s.max_parts)
    return None  # words/social: dedupe + toggles make exact counts impractical


def _make_iter(s: StrategySpec):
    if s.mode == "charset":
        return wordgen.charset_iter(wordgen.build_charset(s.presets, s.custom), s.min_len, s.max_len)
    if s.mode == "phone":
        return wordgen.phone_iter(s.mask, s.wildcard, s.strip_nondigits)
    if s.mode == "combinator":
        return wordgen.combinator_iter(s.words, s.separators, s.min_parts, s.max_parts)
    if s.mode == "words":
        return wordgen.words_iter(
            words=s.words, numbers=s.numbers, years=s.years, suffixes=s.suffixes,
            use_cases=s.use_cases, use_leet=s.use_leet, reverse=s.reverse,
            append_numbers=s.append_numbers, combine_min=s.min_parts, combine_max=s.max_parts,
            separators=s.separators,
        )
    return wordgen.social_iter(
        words=s.words, numbers=s.numbers, years=s.years, suffixes=s.suffixes,
        use_cases=s.use_cases, use_leet=s.use_leet, append_numbers=s.append_numbers,
        combine_pairs=s.combine_pairs, reverse=s.reverse,
    )


def _resolve_filename(raw: str | None):
    name = (raw or "wordlist").strip()
    if not name.lower().endswith(".txt"):
        name += ".txt"
    name = clean_filename(name)
    return name, safe_path_in(config.WORDLISTS_DIR, name)


def _check_request(body: WordgenRequest) -> None:
    if not body.strategies:
        raise HTTPException(400, "Enable at least one generation strategy")
    for s in body.strategies:
        _validate(s)


@router.post("/estimate")
async def estimate(body: WordgenRequest) -> dict:
    _check_request(body)
    counts = [_estimate_one(s) for s in body.strategies]
    known = [c for c in counts if c is not None]
    total = sum(known)
    has_unknown = any(c is None for c in counts)
    cap = config.WORDGEN_MAX_TARGET
    return {
        "count": total,
        "has_unknown": has_unknown,           # True when a word/social strategy is included
        "exact": not has_unknown,
        "cap": cap,
        "will_cap": total > cap,
        "bytes_estimate": total * 9 if total else 0,
        "strategies": len(body.strategies),
    }


@router.post("/preview")
def preview(body: WordgenRequest) -> dict:
    """Return the first ~30 deduped lines without saving — a quick sanity check."""
    _check_request(body)
    chained = itertools.chain.from_iterable(_make_iter(s) for s in body.strategies)
    seen: set[str] = set()
    sample: list[str] = []
    for w in chained:
        if not w or w in seen:
            continue
        seen.add(w)
        sample.append(w)
        if len(sample) >= 30:
            break
    return {"sample": sample}


@router.post("/generate")
def generate(body: WordgenRequest) -> dict:
    """Kick off a background generation job; returns a job_id to poll.

    Streams to one or more split files with constant memory, so this scales far
    beyond RAM (limited by disk, not the old in-memory cap).
    """
    _check_request(body)
    name, _ = _resolve_filename(body.filename)

    target = body.target_lines or config.WORDGEN_DEFAULT_TARGET
    target = max(1, min(int(target), config.WORDGEN_MAX_TARGET))
    per = max(0, int(body.lines_per_file or 0))

    # Auto-disable dedupe for a single already-unique source (charset/phone),
    # otherwise honour the user's choice.
    single_unique = len(body.strategies) == 1 and body.strategies[0].mode in _NO_DEDUPE
    dedupe = bool(body.dedupe) and not single_unique

    # Effective total for the progress bar: when every strategy has a known,
    # finite count (charset/phone), the job can't exceed that — so a 1B "max"
    # on a 100M phone mask should fill toward 100M, not 1B.
    counts = [_estimate_one(s) for s in body.strategies]
    has_unknown = any(c is None for c in counts)
    known_total = sum(c for c in counts if c is not None)
    progress_target = target if (has_unknown or known_total == 0) else min(target, known_total)

    # Snapshot the strategies so the worker thread builds fresh iterators.
    strategies = list(body.strategies)

    def build():
        return itertools.chain.from_iterable(_make_iter(s) for s in strategies)

    job = wordgen_jobs.manager.start(
        base_name=name, build=build, target_lines=target,
        lines_per_file=per, dedupe=dedupe,
    )
    return {"job_id": job.id, "status": "running", "target": progress_target,
            "dedupe": dedupe, "split": per}


@router.get("/jobs/{job_id}")
def gen_status(job_id: str) -> dict:
    job = wordgen_jobs.manager.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown generation job")
    return wordgen_jobs.manager.status(job)


@router.post("/jobs/{job_id}/stop")
def gen_stop(job_id: str) -> dict:
    return {"ok": wordgen_jobs.manager.stop(job_id)}
