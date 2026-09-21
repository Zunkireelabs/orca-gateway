"""Numbers are spoken as words on voice: a channel concern, applied to the FINAL answer text in
the voice adapter, in code (never a prompt directive: the platform's own "write numbers as words"
prompt is ignored by design, and prompt rules are the kind that fail).

Product-blind: it knows numbers, times, dates and digit strings, never what they refer to. The
language comes from the sentence itself: a sentence containing any Devanagari character is
Nepali, otherwise English. Both ASCII and Devanagari digits are read.

Nepali sentences:  plain numbers -> Nepali words (lakh / crore grouping, from an EXPLICIT 1-99
table, never composed from tens and units), amounts after a rupee marker -> words + रुपैयाँ (a
trailing .0/.00 dropped), clock times -> period + hour + साढे/सवा/पौने, dates -> English month in
Devanagari + day, phone-like digit strings -> digit by digit.
English sentences: only phone-like strings (digit by digit) and a trailing .0/.00 on a currency
amount are touched; everything else is left to the TTS.

The guard that matters: a conversion must never change a value, and anything ambiguous passes
through UNCHANGED (logged at DEBUG as a pattern class only, never text). A wrong conversion is
worse than none. The output contains no digits, so running it twice changes nothing.

REVIEW NEEDED (native ear): the 1-99 table's spellings and the judgment calls flagged below
(डेढ/अढाई for 1:30/2:30, the period boundaries, the year kept on ISO dates). The property test
proves the arithmetic round-trips, not that a spelling is idiomatic.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("orca_gateway.channels.number_speech")

_TO_ASCII = str.maketrans("०१२३४५६७८९", "0123456789")
_D = "[0-9०-९]"  # an ASCII or Devanagari digit
_DEVANAGARI = re.compile("[ऀ-ॿ]")

NE_DIGIT = ["शून्य", "एक", "दुई", "तीन", "चार", "पाँच", "छ", "सात", "आठ", "नौ"]
EN_DIGIT = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]

# Complete and explicit: Nepali 1-99 is irregular, so NOTHING here is composed.
NE_1_99 = [
    "",  # 0 is spoken शून्य and never appears inside a larger number
    "एक", "दुई", "तीन", "चार", "पाँच", "छ", "सात", "आठ", "नौ", "दस",
    "एघार", "बाह्र", "तेह्र", "चौध", "पन्ध्र", "सोह्र", "सत्र", "अठार", "उन्नाइस", "बीस",
    "एक्काइस", "बाइस", "तेइस", "चौबीस", "पच्चीस", "छब्बीस", "सत्ताइस", "अठ्ठाइस", "उनन्तीस", "तीस",
    "एकतीस", "बत्तीस", "तेत्तीस", "चौँतीस", "पैँतीस", "छत्तीस", "सैँतीस", "अठतीस", "उनन्चालीस", "चालीस",
    "एकचालीस", "बयालीस", "त्रिचालीस", "चवालीस", "पैंतालीस", "छयालीस", "सतचालीस", "अठचालीस",
    "उनन्पचास", "पचास",
    "एकाउन्न", "बाउन्न", "त्रिपन्न", "चौवन्न", "पचपन्न", "छपन्न", "सन्ताउन्न", "अन्ठाउन्न",
    "उनन्साठी", "साठी",
    "एकसट्ठी", "बासट्ठी", "त्रिसट्ठी", "चौसट्ठी", "पैँसट्ठी", "छयसट्ठी", "सतसट्ठी", "अठसट्ठी",
    "उनन्सत्तरी", "सत्तरी",
    "एकहत्तर", "बहत्तर", "त्रिहत्तर", "चौहत्तर", "पचहत्तर", "छयहत्तर", "सतहत्तर", "अठहत्तर",
    "उनासी", "असी",
    "एकासी", "बयासी", "त्रियासी", "चौरासी", "पचासी", "छयासी", "सतासी", "अठासी", "उनान्नब्बे", "नब्बे",
    "एकानब्बे", "बयानब्बे", "त्रियानब्बे", "चौरानब्बे", "पन्चानब्बे", "छयानब्बे", "सन्तानब्बे",
    "अन्ठानब्बे", "उनान्सय",
]
assert len(NE_1_99) == 100 and len(set(NE_1_99[1:])) == 99

_MONTHS = [
    ("January", "Jan", "जनवरी"), ("February", "Feb", "फेब्रुअरी"), ("March", "Mar", "मार्च"),
    ("April", "Apr", "अप्रिल"), ("May", "May", "मे"), ("June", "Jun", "जुन"),
    ("July", "Jul", "जुलाई"), ("August", "Aug", "अगस्ट"), ("September", "Sept", "सेप्टेम्बर"),
    ("October", "Oct", "अक्टोबर"), ("November", "Nov", "नोभेम्बर"), ("December", "Dec", "डिसेम्बर"),
]
_MONTH_NE = {i + 1: m[2] for i, m in enumerate(_MONTHS)}
_MONTH_NUM = {}
for _i, (_full, _abbr, _) in enumerate(_MONTHS):
    _MONTH_NUM[_full.lower()] = _i + 1
    _MONTH_NUM[_full[:3].lower()] = _i + 1
_MONTH_NUM["sept"] = 9
_MONTH_RE = "|".join(
    sorted({m[0] for m in _MONTHS} | {m[0][:3] for m in _MONTHS} | {"Sept"}, key=len, reverse=True)
)
_DAYS_IN_MONTH = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

_MAX_NUMBER = 999_999_999  # 99 crore 99 lakh 99 thousand 999; beyond it passes through


def number_words(n: int) -> str:
    """Nepali words for 0 <= n <= 999,999,999, lakh/crore grouping."""
    if n == 0:
        return NE_DIGIT[0]
    parts: list[str] = []
    for size, name in ((10_000_000, "करोड"), (100_000, "लाख"), (1_000, "हजार")):
        chunk, n = divmod(n, size)
        if chunk:
            parts += [NE_1_99[chunk], name]
    hundreds, rest = divmod(n, 100)
    if hundreds:
        parts += [NE_1_99[hundreds], "सय"]
    if rest:
        parts.append(NE_1_99[rest])
    return " ".join(parts)


def _period(hour: int) -> str:
    # Judgment call (review by ear): बिहान 04-11, दिउँसो 12-16, बेलुका 17-19, राति 20-03.
    if 4 <= hour < 12:
        return "बिहान"
    if 12 <= hour < 17:
        return "दिउँसो"
    if 17 <= hour < 20:
        return "बेलुका"
    return "राति"


def time_words(hour: int, minute: int) -> str:
    period, h12 = _period(hour), hour % 12 or 12
    if minute == 0:
        return f"{period} {NE_1_99[h12]} बजे"
    if minute == 30:
        # डेढ / अढाई are the idiomatic 1:30 / 2:30 (a judgment call: the brief only shows 4:30)
        body = {1: "डेढ", 2: "अढाई"}.get(h12, f"साढे {NE_1_99[h12]}")
        return f"{period} {body} बजे"
    if minute == 15:
        return f"{period} सवा {NE_1_99[h12]} बजे"
    if minute == 45:
        return f"{period} पौने {NE_1_99[h12 % 12 + 1]} बजे"
    return f"{period} {NE_1_99[h12]} बजेर {NE_1_99[minute]} मिनेट"


def _digits_to_int(s: str) -> int | None:
    """An integer from digits with optional grouping commas (Indian or Western), else None."""
    s = s.translate(_TO_ASCII)
    if "," in s:
        if not (re.fullmatch(r"\d{1,3}(,\d{3})+", s) or re.fullmatch(r"\d{1,2}(,\d{2})*,\d{3}", s)):
            return None
        s = s.replace(",", "")
    if not s.isdigit() or (len(s) > 1 and s[0] == "0"):
        return None
    n = int(s)
    return n if n <= _MAX_NUMBER else None


def _phone_words(text: str, nepali: bool) -> str | None:
    """Digit by digit, grouped as written. None when it does not look like a phone number."""
    plus = text.startswith("+")
    groups = re.split(r"[-\s]+", text.lstrip("+"))
    total = sum(len(g) for g in groups)
    if total < 7 or any(not g for g in groups):
        return None
    if len(groups) > 1 and not plus:
        # separated groups: every group >= 2 digits and one >= 3 ('10-20-30-40' is a list, not
        # a phone number)
        if any(len(g) < 2 for g in groups) or all(len(g) < 3 for g in groups):
            return None
    words = NE_DIGIT if nepali else EN_DIGIT
    spoken = ", ".join(" ".join(words[int(c)] for c in g.translate(_TO_ASCII)) for g in groups)
    return (("प्लस " if nepali else "plus ") if plus else "") + spoken


_CUR = r"(?<![A-Za-zऀ-ॿ])(?:रु|Rs|NPR|USD|\$)\.?\s*"
_TOKEN = re.compile(
    rf"""
      (?P<cur>{_CUR}(?P<amt>{_D}+(?:,{_D}+)*(?:\.{_D}+)?))
    | (?P<iso>(?<!{_D})(?P<iy>{_D}{{4}})-(?P<im>{_D}{{2}})-(?P<id>{_D}{{2}})(?!{_D}))
    | (?P<dmon>(?<!{_D})(?P<dd>{_D}{{1,2}})(?:st|nd|rd|th)?\s+(?P<dm>{_MONTH_RE})\b
        (?:\s+(?P<dy>{_D}{{4}})(?!{_D}))?)
    | (?P<mond>\b(?P<mm>{_MONTH_RE})\s+(?P<md>{_D}{{1,2}})(?:st|nd|rd|th)?(?!{_D})
        (?:,?\s+(?P<my>{_D}{{4}})(?!{_D}))?)
    | (?P<time>(?<![\d०-९:])(?P<th>{_D}{{1,2}}):(?P<tm>{_D}{{2}})
        (?:\s?(?P<ap>[AaPp])\.?[Mm]\.?)?(?![\d०-९:]))
    | (?P<phone>(?<![\d०-९])\+?(?:{_D}{{7,}}|{_D}+(?:[- ]{_D}+)+)(?!{_D}))
    | (?P<num>{_D}+(?:,{_D}+)*(?:\.{_D}+)?)
    """,
    re.VERBOSE,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[।?!\n])|(?<=\.)(?=\s+[A-Z\u0904-\u0939])")
_OK_BEFORE = set(" \t\r\n([{\"'“‘")
_OK_AFTER = set(" \t\r\n)]}\"'”’,;:!?।")


@dataclass
class SpeechResult:
    text: str
    conversions: dict[str, int] = field(default_factory=dict)
    passthrough: dict[str, int] = field(default_factory=dict)

    @property
    def converted(self) -> int:
        return sum(self.conversions.values())


def _standalone(s: str, m: re.Match) -> bool:
    """A bare number must stand alone: '-5', '10-20', '50%', '3D', 'A4', '1/2' pass through."""
    before = s[m.start() - 1] if m.start() > 0 else " "
    after = s[m.end()] if m.end() < len(s) else " "
    if before not in _OK_BEFORE:
        return False
    if after == ".":
        return m.end() + 1 >= len(s) or s[m.end() + 1].isspace()
    return after in _OK_AFTER


def _currency(m: re.Match, nepali: bool) -> str | None:
    amt = m.group("amt")
    head, _, decimals = amt.partition(".")
    if decimals and decimals.translate(_TO_ASCII).strip("0") != "":
        return None  # a real fractional amount: ambiguous, leave it
    if decimals and len(decimals) > 2:
        return None
    marker = m.group("cur")[: len(m.group("cur")) - len(amt)]
    if not nepali:
        return marker + head if decimals else None  # English: only clean a trailing .0/.00
    if not marker.strip().rstrip(".").startswith(("रु", "Rs", "NPR")):
        return None  # only rupees are spoken; another currency passes through
    n = _digits_to_int(head)
    return None if n is None else f"{number_words(n)} रुपैयाँ"


def _date(day: str, month: int, year: str | None) -> str | None:
    d = int(day.translate(_TO_ASCII))
    if not 1 <= d <= _DAYS_IN_MONTH[month - 1]:
        return None
    out = f"{_MONTH_NE[month]} {NE_1_99[d]}"
    if year is not None:
        y = _digits_to_int(year)
        if y is None or not 1900 <= y <= 2199:
            return None
        out += f", {number_words(y)}"  # kept: dropping the year would lose information
    return out


def _replace(s: str, m: re.Match, nepali: bool) -> tuple[str, str] | tuple[None, str]:
    """(replacement, class) or (None, class) when the match must pass through unchanged."""
    kind = "num"
    for name in ("cur", "iso", "dmon", "mond", "time", "phone", "num"):
        if m.group(name) is not None:
            kind = name
            break
    if kind == "cur":
        return _currency(m, nepali), "currency"
    if kind == "phone":
        raw = m.group("phone")
        ph = _phone_words(raw, nepali)
        if ph is not None:
            return ph, "phone"
        if nepali and "-" not in raw and not raw.startswith("+"):
            # space-separated numbers that are not a phone number ('३ १२'): each is a plain number
            groups = raw.split(" ")
            words = [
                number_words(n) if (n := _digits_to_int(g)) is not None else g for g in groups
            ]
            if words != groups:
                return " ".join(words), "number"
        return None, "phone"
    if not nepali:
        return None, kind  # English: only phone strings and currency .0 are touched
    if kind == "iso":
        mo = int(m.group("im").translate(_TO_ASCII))
        if not 1 <= mo <= 12:
            return None, "date"
        return _date(m.group("id"), mo, m.group("iy")), "date"
    if kind == "dmon":
        return _date(m.group("dd"), _MONTH_NUM[m.group("dm").lower()], m.group("dy")), "date"
    if kind == "mond":
        return _date(m.group("md"), _MONTH_NUM[m.group("mm").lower()], m.group("my")), "date"
    if kind == "time":
        h, mi = int(m.group("th").translate(_TO_ASCII)), int(m.group("tm").translate(_TO_ASCII))
        ap = m.group("ap")
        if ap is not None:
            if not 1 <= h <= 12:
                return None, "time"
            h = h % 12 + (12 if ap.lower() == "p" else 0)
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            return None, "time"
        return time_words(h, mi), "time"
    # kind == "num"
    if not _standalone(s, m):
        return None, "number"
    text = m.group("num")
    if "." in text:
        return None, "number"  # a decimal that is not a currency amount: ambiguous
    n = _digits_to_int(text)
    return (None if n is None else number_words(n)), "number"


def _convert_sentence(s: str, result: SpeechResult) -> str:
    nepali = _DEVANAGARI.search(s) is not None
    out: list[str] = []
    pos = 0
    for m in _TOKEN.finditer(s):
        replacement, kind = _replace(s, m, nepali)
        if replacement is None:
            if nepali or kind == "phone":
                result.passthrough[kind] = result.passthrough.get(kind, 0) + 1
                logger.debug("number speech: left a %s pattern unchanged", kind)
            continue
        out.append(s[pos : m.start()])
        out.append(replacement)
        pos = m.end()
        result.conversions[kind] = result.conversions.get(kind, 0) + 1
    out.append(s[pos:])
    return "".join(out)


def verbalize(text: str) -> SpeechResult:
    """Rewrites numbers in `text` as spoken words. Never raises: on any internal error the
    original text is returned untouched (a channel guard must not be able to break a turn)."""
    result = SpeechResult(text)
    try:
        parts = _SENTENCE_SPLIT.split(text)
        result.text = "".join(_convert_sentence(part, result) for part in parts)
    except Exception:  # pragma: no cover - defensive
        logger.exception("number speech failed; returning the text unchanged")
        return SpeechResult(text)
    return result
