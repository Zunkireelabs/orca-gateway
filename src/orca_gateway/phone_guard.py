"""The phone-number guard (P4 brief A3): a code-side check that any phone-shaped span in an answer
is one the tenant configured, and a REPLACEMENT (never a pass-through) for any that is not. The
prompt-side instruction is not enough: a model asked never to invent a number still does.

Channel- and product-blind, like number_speech: it knows digit strings, never what they refer to.
It runs on the answer text BEFORE numbers are turned into words (a word-form number can no longer
be compared), on voice and chat alike.

What counts as phone-shaped: 7 or more digits (ASCII or Devanagari), optionally led by '+' and
joined by single spaces or hyphens, with '(...)' allowed around a group. What does NOT: an ISO-style
date (2026-10-20, 20-10-2026), an amount marked by a currency prefix (रु, Rs, NPR, USD, $) or a
rupee word after it, and any run broken by a comma or a decimal point. Short codes (under 7 digits,
e.g. an emergency line) are out of scope by design.

An empty allowlist with the guard on replaces every number (fail closed). Matching ignores
formatting and leading zeros, and forgives an explicit country code ('+' plus 1-3 digits) on one
side only.

Never logs a digit: callers get counts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TO_ASCII = str.maketrans("०१२३४५६७८९", "0123456789")
_D = "[0-9०-९]"
_GROUP = rf"(?:\({_D}+\)|{_D}+)"
_SPAN = re.compile(rf"(?<![0-9०-९\w.,+])\+?{_GROUP}(?:[ \-]{_GROUP})*(?![0-9०-९])")
_ISO_DATE = re.compile(
    rf"^(?:{_D}{{4}}-{_D}{{1,2}}-{_D}{{1,2}}|{_D}{{1,2}}-{_D}{{1,2}}-{_D}{{4}})$"
)
_CURRENCY_BEFORE = re.compile(r"(?:रु|Rs|NPR|USD|\$)\.?\s*$")
_CURRENCY_AFTER = re.compile(r"^\s*(?:रुपैयाँ|रुपैया|rupees?)(?![A-Za-zऀ-ॿ])", re.I)
MIN_DIGITS = 7


def digits_of(text: str) -> str:
    return "".join(c for c in text.translate(_TO_ASCII) if c.isdigit())


def _significant(text: str) -> str:
    return digits_of(text).lstrip("0")


def same_number(a: str, b: str) -> bool:
    """The same line, whatever the formatting. An explicit '+country code' (1-3 digits) on the
    longer side is forgiven; anything else must match exactly (leading zeros aside)."""
    da, db = _significant(a), _significant(b)
    if not da or not db:
        return False
    if da == db:
        return True
    (long_s, long_d), (_, short_d) = sorted(((a, da), (b, db)), key=lambda p: -len(p[1]))
    return (
        long_s.strip().startswith("+")
        and long_d.endswith(short_d)
        and 1 <= len(long_d) - len(short_d) <= 3
    )


@dataclass
class GuardResult:
    text: str
    replaced: int = 0


def guard_phone_numbers(text: str, allowed: list[str], replacement: str) -> GuardResult:
    """`text` with every phone-shaped span not in `allowed` replaced by `replacement`."""
    out: list[str] = []
    pos = 0
    replaced = 0
    for m in _SPAN.finditer(text):
        span = m.group(0)
        if len(digits_of(span)) < MIN_DIGITS or _ISO_DATE.match(span):
            continue
        if _CURRENCY_BEFORE.search(text[: m.start()]) or _CURRENCY_AFTER.match(text[m.end() :]):
            continue
        if any(same_number(span, a) for a in allowed):
            continue
        out.append(text[pos : m.start()])
        out.append(replacement)
        pos = m.end()
        replaced += 1
    if not replaced:
        return GuardResult(text)
    out.append(text[pos:])
    return GuardResult("".join(out), replaced)
