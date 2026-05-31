"""Wordlist generation engine.

Combines four generation strategies into one .txt producer that writes straight
into the wordlists directory, so generated lists are immediately usable by the
cracking step:

  * charset    — brute-force every combination of a character set (crunch-style)
  * phone      — phone-number masks with X wildcards (pnwgen-style, improved)
  * combinator — join/permute base words with separators (dictionary-generator,
                 J4NN0 wordlist-generator word mode)
  * social     — social-engineering candidates from personal info (SocialPassGen)

Every generator is a lazy iterator; ``write_wordlist`` streams it to disk with
an optional dedupe pass and a hard line cap, so a mis-sized request can never
exhaust the disk or memory.
"""
from __future__ import annotations

import itertools
import math
from pathlib import Path
from typing import Iterable, Iterator

# --- shared data ----------------------------------------------------------

CHARSETS = {
    "lower": "abcdefghijklmnopqrstuvwxyz",
    "upper": "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "digits": "0123456789",
    "special": "!@#$%^&*()-_=+.",
    "space": " ",
}

# Conservative leet substitutions (lowercase keys; applied case-insensitively).
LEET_MAP = {"a": "@", "e": "3", "i": "1", "o": "0", "s": "$",
            "t": "7", "l": "1", "g": "9", "b": "8"}

# Common trailing tokens people append to passwords.
COMMON_SUFFIXES = ["", "1", "12", "123", "1234", "12345", "123456",
                   "!", "@", "#", "1!", "123!", "00", "01", "007",
                   "69", "420", "111", "777"]


def _dedupe_keep(seq: Iterable[str]) -> list[str]:
    """Order-preserving dedupe, dropping empties."""
    return list(dict.fromkeys(x for x in seq if x is not None and x != ""))


# --- charset (brute force) ------------------------------------------------

def build_charset(presets: Iterable[str], custom: str = "") -> str:
    chars: list[str] = []
    for p in presets or []:
        chars.extend(CHARSETS.get(p, ""))
    chars.extend(custom or "")
    return "".join(dict.fromkeys(chars))  # dedupe, keep order


def charset_count(charset_len: int, min_len: int, max_len: int) -> int:
    if charset_len <= 0 or min_len < 1 or max_len < min_len:
        return 0
    return sum(charset_len ** L for L in range(min_len, max_len + 1))


def charset_iter(charset: str, min_len: int, max_len: int) -> Iterator[str]:
    for L in range(min_len, max_len + 1):
        for combo in itertools.product(charset, repeat=L):
            yield "".join(combo)


# --- phone numbers (pnwgen, improved) -------------------------------------

def phone_count(mask: str, wildcard: str = "X") -> int:
    return 10 ** mask.upper().count(wildcard.upper())


def phone_iter(mask: str, wildcard: str = "X", strip_nondigits: bool = False) -> Iterator[str]:
    """Expand every X (wildcard) in ``mask`` over digits 0-9.

    Literal characters (country code, separators) are preserved unless
    ``strip_nondigits`` is set, in which case only digits remain in the output.
    """
    wc = (wildcard or "X").upper()
    chars = list(mask)
    positions = [i for i, c in enumerate(chars) if c.upper() == wc]
    n = len(positions)
    for combo in itertools.product("0123456789", repeat=n):
        for pos, d in zip(positions, combo):
            chars[pos] = d
        out = "".join(chars)
        if strip_nondigits:
            out = "".join(ch for ch in out if ch.isdigit())
        yield out


# --- combinator (word permutations + separators) --------------------------

def combinator_count(n_words: int, n_sep: int, min_parts: int, max_parts: int) -> int:
    total = 0
    for r in range(max(min_parts, 1), max_parts + 1):
        if r > n_words:
            continue
        perms = math.perm(n_words, r)
        total += perms * (n_sep if r > 1 else 1)
    return total


def combinator_iter(words: list[str], separators: list[str],
                    min_parts: int, max_parts: int) -> Iterator[str]:
    words = _dedupe_keep(w.strip() for w in words)
    seps = separators or [""]
    for r in range(max(min_parts, 1), max_parts + 1):
        if r > len(words):
            break
        for combo in itertools.permutations(words, r):
            if r == 1:
                yield combo[0]
            else:
                for sep in seps:
                    yield sep.join(combo)


# --- social engineering ---------------------------------------------------

def _case_variants(w: str) -> set[str]:
    return {w, w.lower(), w.upper(), w.capitalize()}


def _leet(w: str) -> str:
    return "".join(LEET_MAP.get(c.lower(), c) for c in w)


def social_iter(*, words: list[str], numbers: list[str], years: list[str],
                suffixes: list[str] | None = None, use_cases: bool = True,
                use_leet: bool = False, append_numbers: bool = True,
                combine_pairs: bool = True, reverse: bool = False) -> Iterator[str]:
    """Generate likely passwords from personal-info tokens.

    Pipeline: base words -> case/leet/reverse variants -> optionally append a
    number/year/suffix pool -> optionally concatenate token pairs (also with the
    number pool). Dedupe is left to the writer so this stays a lazy stream.
    """
    base_words = _dedupe_keep(w.strip() for w in words)
    pool = _dedupe_keep([
        *(str(n).strip() for n in numbers),
        *(str(y).strip() for y in years),
        *(suffixes if suffixes is not None else COMMON_SUFFIXES),
    ])

    bases: list[str] = []
    seen: set[str] = set()
    for w in base_words:
        variants = {w}
        if use_cases:
            variants |= _case_variants(w)
        if use_leet:
            variants |= {_leet(v) for v in list(variants)}
        if reverse:
            variants |= {v[::-1] for v in list(variants)}
        for v in variants:
            if v and v not in seen:
                seen.add(v)
                bases.append(v)

    for b in bases:
        yield b
        if append_numbers:
            for n in pool:
                if n:
                    yield b + n

    if combine_pairs:
        for a, b in itertools.permutations(bases, 2):
            yield a + b
            if append_numbers:
                for n in pool:
                    if n:
                        yield a + b + n


# --- unified word generator (combinator + social in one) ------------------

def words_iter(*, words: list[str], numbers: list[str] | None = None,
               years: list[str] | None = None, suffixes: list[str] | None = None,
               use_cases: bool = True, use_leet: bool = False, reverse: bool = False,
               append_numbers: bool = True, combine_min: int = 1, combine_max: int = 2,
               separators: list[str] | None = None) -> Iterator[str]:
    """One word generator covering both join-permutations and social mutations.

    For each combination of combine_min..combine_max base words (joined by the
    given separators), emit case/leet/reverse variants, optionally each followed
    by a number/year/suffix from the pool. Fully lazy — the writer dedupes.
    """
    tokens = _dedupe_keep(w.strip() for w in words)
    seps = separators if separators else [""]
    pool = _dedupe_keep([
        *(str(n).strip() for n in (numbers or [])),
        *(str(y).strip() for y in (years or [])),
        *(suffixes if suffixes is not None else COMMON_SUFFIXES),
    ])

    def combos() -> Iterator[str]:
        lo = max(int(combine_min), 1)
        hi = max(int(combine_max), lo)
        for r in range(lo, hi + 1):
            if r == 1:
                yield from tokens
            elif r <= len(tokens):
                for perm in itertools.permutations(tokens, r):
                    for sep in seps:
                        yield sep.join(perm)

    for base in combos():
        variants = [base]
        if use_cases:
            variants = list(dict.fromkeys(variants + list(_case_variants(base))))
        if use_leet:
            variants = list(dict.fromkeys(variants + [_leet(v) for v in variants]))
        if reverse:
            variants = list(dict.fromkeys(variants + [v[::-1] for v in variants]))
        for v in variants:
            yield v
            if append_numbers:
                for n in pool:
                    if n:
                        yield v + n


# --- writer ---------------------------------------------------------------

def write_wordlist(path: Path, it: Iterator[str], *, dedupe: bool, max_lines: int) -> dict:
    """Stream ``it`` to ``path``, one entry per line.

    Returns ``{"lines", "bytes", "capped"}``. ``capped`` is True if generation
    stopped at ``max_lines`` (so the UI can warn — never a silent truncation).
    """
    count = 0
    capped = False
    seen: set[str] | None = set() if dedupe else None
    with path.open("w", encoding="utf-8", errors="ignore", newline="\n") as f:
        for w in it:
            if not w:
                continue
            if seen is not None:
                if w in seen:
                    continue
                seen.add(w)
            f.write(w)
            f.write("\n")
            count += 1
            if count >= max_lines:
                capped = True
                break
    return {"lines": count, "bytes": path.stat().st_size, "capped": capped}
