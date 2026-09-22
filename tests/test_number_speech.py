"""Number verbalization for voice. The property that matters is that a conversion never changes a
value: every generated amount and time is verbalized, parsed back FROM THE WORDS by an independent
parser, and must equal the original. The parser lives here, not in src/, so it cannot share a bug
with the code it checks."""

import random

import pytest

from orca_gateway.channels import number_speech as ns
from orca_gateway.channels.number_speech import verbalize

DEV = str.maketrans("0123456789", "०१२३४५६७८९")
INV = {w: i for i, w in enumerate(ns.NE_1_99) if i}  # word -> 1..99
INV["शून्य"] = 0
UNITS = {"सय": 100, "हजार": 1_000, "लाख": 100_000, "करोड": 10_000_000}


def say(text: str) -> str:
    return verbalize(text).text


def parse_number_words(words: str) -> int:
    """Independent inverse of number_words: '<1-99> <unit> ... <1-99>'."""
    toks, total, i = words.split(), 0, 0
    while i < len(toks):
        v = INV[toks[i]]
        if i + 1 < len(toks) and toks[i + 1] in UNITS:
            total += v * UNITS[toks[i + 1]]
            i += 2
        else:
            total += v
            i += 1
    return total


def indian(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    groups = []
    while head:
        groups.insert(0, head[-2:])
        head = head[:-2]
    return ",".join(groups + [tail])


def western(n: int) -> str:
    return f"{n:,}"


# ---- the brief's table (in a Nepali sentence), ASCII and Devanagari digits ----------------------

NE_ROWS = [
    ("मूल्य १,००,००० रुपैयाँ छ", "मूल्य एक लाख रुपैयाँ छ"),
    ("मूल्य 1,00,000 छ", "मूल्य एक लाख छ"),
    ("मूल्य 100000 छ", "मूल्य एक लाख छ"),
    ("शुल्क २०,००० हो", "शुल्क बीस हजार हो"),
    ("शुल्क 20,000 हो", "शुल्क बीस हजार हो"),
    ("रु 2000.0 पर्छ", "दुई हजार रुपैयाँ पर्छ"),
    ("रु 2000.00 पर्छ", "दुई हजार रुपैयाँ पर्छ"),
    ("रु. २००० पर्छ", "दुई हजार रुपैयाँ पर्छ"),
    ("Rs 2,000 पर्छ", "दुई हजार रुपैयाँ पर्छ"),
    ("रु २,००० पर्छ", "दुई हजार रुपैयाँ पर्छ"),
    ("मूल्य 1,25,50,000 छ", "मूल्य एक करोड पच्चीस लाख पचास हजार छ"),
    ("मूल्य १,२५,५०,००० छ", "मूल्य एक करोड पच्चीस लाख पचास हजार छ"),
    ("समय 16:30 हो", "समय दिउँसो साढे चार बजे हो"),
    ("समय १६:३० हो", "समय दिउँसो साढे चार बजे हो"),
    ("समय 4:30 PM हो", "समय दिउँसो साढे चार बजे हो"),
    ("समय 10:00 हो", "समय बिहान दस बजे हो"),
    ("समय 10:00 AM हो", "समय बिहान दस बजे हो"),
    ("मिति 22 September हो", "मिति सेप्टेम्बर बाइस हो"),
    ("मिति २२ September हो", "मिति सेप्टेम्बर बाइस हो"),
    ("फोन 980-1222339 हो", "फोन नौ आठ शून्य, एक दुई दुई दुई तीन तीन नौ हो"),
    ("फोन ९८४१५४०४३४ हो", "फोन नौ आठ चार एक पाँच चार शून्य चार तीन चार हो"),
    ("फोन 9841540434 हो", "फोन नौ आठ चार एक पाँच चार शून्य चार तीन चार हो"),
    ("3 जना छन्", "तीन जना छन्"),
    ("३ जना छन्", "तीन जना छन्"),
    ("0 वटा छ", "शून्य वटा छ"),
]


@pytest.mark.parametrize("given,expected", NE_ROWS)
def test_nepali_rows(given, expected):
    assert say(given) == expected


def verbalize_in(text: str, year: int | None) -> str:
    return verbalize(text, current_year=year).text


def test_a_date_in_the_current_year_is_read_without_the_year():
    assert verbalize_in("मिति 2026-09-22 हो", 2026) == "मिति सेप्टेम्बर बाइस हो"
    assert verbalize_in("मिति २०२६-०९-०५ हो", 2026) == "मिति सेप्टेम्बर पाँच हो"
    assert verbalize_in("मिति 22 September 2026 हो", 2026) == "मिति सेप्टेम्बर बाइस हो"
    assert verbalize_in("मिति September 22, 2026 हो", 2026) == "मिति सेप्टेम्बर बाइस हो"
    assert verbalize_in("मिति 22 September हो", 2026) == "मिति सेप्टेम्बर बाइस हो"  # none given


def test_a_date_in_any_other_year_keeps_its_year():
    assert verbalize_in("मिति 2027-01-05 हो", 2026) == "मिति जनवरी पाँच, दुई हजार सत्ताइस हो"
    assert verbalize_in("मिति 2025-12-31 हो", 2026) == "मिति डिसेम्बर एकतीस, दुई हजार पच्चीस हो"
    assert verbalize_in("मिति 22 September 2027 हो", 2026) == (
        "मिति सेप्टेम्बर बाइस, दुई हजार सत्ताइस हो"
    )


def test_when_the_current_year_is_unknown_every_year_is_kept():
    assert say("मिति 2026-09-22 हो") == "मिति सेप्टेम्बर बाइस, दुई हजार छब्बीस हो"


def test_year_dropping_never_touches_anything_but_a_dates_year():
    # a bare '2026' is a number, not a date: it is still spoken in full
    assert verbalize_in("साल 2026 हो", 2026) == "साल दुई हजार छब्बीस हो"


def test_quarter_and_half_hour_forms():
    assert say("समय 4:15 PM हो") == "समय दिउँसो सवा चार बजे हो"
    assert say("समय 4:45 PM हो") == "समय दिउँसो पौने पाँच बजे हो"
    assert say("समय 1:30 PM हो") == "समय दिउँसो डेढ बजे हो"
    assert say("समय 2:30 PM हो") == "समय दिउँसो अढाई बजे हो"
    assert say("समय 4:20 PM हो") == "समय दिउँसो चार बजेर बीस मिनेट हो"
    assert say("समय 12:45 हो") == "समय दिउँसो पौने एक बजे हो"


# ---- English sentences: only phone strings and a trailing .0 on a currency amount ----------


@pytest.mark.parametrize(
    "given,expected",
    [
        ("Call 980-1222339 now", "Call nine eight zero, one two two two three three nine now"),
        ("Call 9841540434.", "Call nine eight four one five four zero four three four."),
        ("The fee is Rs 2000.0 today", "The fee is Rs 2000 today"),
        ("The fee is Rs. 2,000.00 today", "The fee is Rs. 2,000 today"),
        ("That costs $20.00", "That costs $20"),
        # left to the TTS, by the brief:
        ("We open at 10:00 and it costs Rs 2,000", "We open at 10:00 and it costs Rs 2,000"),
        ("We have 3 dentists on 22 September", "We have 3 dentists on 22 September"),
        ("Rs 2000.50 is the price", "Rs 2000.50 is the price"),
    ],
)
def test_english_rows(given, expected):
    assert say(given) == expected


def test_language_is_decided_per_sentence():
    assert say("We open at 10:00. समय 10:00 हो।") == "We open at 10:00. समय बिहान दस बजे हो।"
    assert say("The fee is Rs 2000.0. मूल्य 3 हजार छ। We open at 10:00.") == (
        "The fee is Rs 2000. मूल्य तीन हजार छ। We open at 10:00."
    )


# ---- ambiguous input passes through UNCHANGED -------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "बाँकी 10-20 हो",  # a range
        "५०% छुट छ",  # a percentage
        "मूल्य 2000.5 हो",  # a real decimal
        "तापक्रम -5 डिग्री",  # negative
        "कोठा A4 मा",  # letters glued on
        "1/2 भाग",  # a fraction
        "समय 25:99 हो",  # not a time
        "समय 13:30 PM हो",  # 13 with PM
        "मिति 2026-13-45 हो",  # not a date
        "मिति 31 February हो",  # not a date
        "अंक 007 हो",  # leading zeros
        "कुल 1,2,3 हो",  # not a valid grouping
        "यो 1,00,00,00,000 हो",  # beyond the supported range (over 99 crore)
        "रु 2000.5 पर्छ",  # a fractional amount
        "फोन 12-34-56-78 हो",  # a list of pairs, not a phone number
        "USD 20 हो",  # another currency in a Nepali sentence
    ],
)
def test_ambiguous_passes_through_unchanged(text):
    assert say(text) == text


def test_text_without_numbers_is_returned_exactly():
    for text in [
        "",
        "नमस्ते, तपाईंलाई कसरी मद्दत गर्न सक्छु?",
        "Hello there.\nHow can I help?",
        "  spaced  ",
    ]:
        assert say(text) == text


def test_running_it_twice_changes_nothing():
    for text, _ in NE_ROWS:
        once = say(text)
        assert say(once) == once


def test_the_output_of_a_converted_row_contains_no_digits():
    for text, _ in NE_ROWS:
        assert not any(c.isdigit() for c in say(text))


def test_results_carry_counts_by_class_and_never_text():
    r = verbalize("मूल्य 1,00,000 छ, समय 16:30 हो, फोन 980-1222339 हो।")
    assert r.conversions == {"number": 1, "time": 1, "phone": 1} and r.converted == 3
    r = verbalize("बाँकी 10-20 हो")
    assert r.converted == 0 and r.passthrough == {"phone": 1}


def test_a_phone_number_keeps_every_digit_in_order():
    for raw in ["9841540434", "980-1222339", "01-4444444", "+977-9841540434", "9801222339"]:
        spoken = say(f"फोन {raw} हो")
        toks = [t for t in spoken.replace(",", " ").split() if t not in ("फोन", "हो", "प्लस")]
        digits = "".join(str(ns.NE_DIGIT.index(t)) for t in toks)
        assert digits == "".join(c for c in raw if c.isdigit())


# ---- the property test: a conversion never changes a value ---------------------------------------

BOUNDARIES = [
    0, 1, 9, 10, 11, 19, 20, 21, 29, 30, 39, 40, 49, 50, 59, 60, 69, 70, 79, 80, 89, 90, 99,
    100, 101, 999, 1_000, 1_001, 9_999, 10_000, 99_999, 100_000, 100_001, 999_999, 1_000_000,
    9_999_999, 10_000_000, 10_000_001, 99_999_999, 100_000_000, 999_999_999,
]


def amounts():
    rng = random.Random(20260921)
    out = set(BOUNDARIES) | set(range(0, 2_001))
    for digits in range(1, 10):
        for _ in range(400):
            out.add(rng.randrange(10 ** (digits - 1) if digits > 1 else 0, 10**digits))
    return sorted(out)


def test_every_1_to_99_is_a_distinct_explicit_word_never_composed():
    words = ns.NE_1_99[1:]
    assert len(words) == 99 and len(set(words)) == 99
    for n in range(1, 100):
        assert parse_number_words(ns.number_words(n)) == n and ns.number_words(n) == ns.NE_1_99[n]


def test_number_words_round_trip_for_every_boundary_and_a_large_sample():
    for n in amounts():
        assert parse_number_words(ns.number_words(n)) == n, n


def test_grouped_and_devanagari_forms_round_trip():
    for n in amounts():
        forms = [indian(n), western(n)] + ([str(n)] if n < 1_000_000 else [])
        for form in forms:
            for f in (form, form.translate(DEV)):
                out = say(f"मूल्य {f} छ")
                assert out.startswith("मूल्य ") and out.endswith(" छ"), (f, out)
                assert parse_number_words(out[len("मूल्य ") : -len(" छ")]) == n, (f, out)


def test_amounts_after_a_rupee_marker_round_trip_including_large_unseparated_ones():
    # A bare 7+ digit run is a phone number by design; behind a rupee marker it is an amount.
    for n in amounts():
        for f in (str(n), indian(n), str(n).translate(DEV)):
            for marker in ("रु ", "रु. ", "Rs "):
                out = say(f"{marker}{f}.0 पर्छ")
                assert out.endswith(" रुपैयाँ पर्छ"), (marker, f, out)
                assert parse_number_words(out[: -len(" रुपैयाँ पर्छ")]) == n, (marker, f, out)


def _hour_lookup():
    table = {}
    for h in range(24):
        key = (ns._period(h), h % 12 or 12)
        assert key not in table, "two hours would sound identical"
        table[key] = h
    return table


def parse_time_words(words: str) -> tuple[int, int]:
    lookup = _hour_lookup()
    t = words.split()
    period = t[0]
    if t[1] == "साढे":
        return lookup[(period, INV[t[2]])], 30
    if t[1] in ("डेढ", "अढाई"):
        return lookup[(period, 1 if t[1] == "डेढ" else 2)], 30
    if t[1] == "सवा":
        return lookup[(period, INV[t[2]])], 15
    if t[1] == "पौने":
        spoken = INV[t[2]]
        return lookup[(period, 12 if spoken == 1 else spoken - 1)], 45
    hour = lookup[(period, INV[t[1]])]
    if t[2] == "बजे":
        return hour, 0
    assert t[2] == "बजेर" and t[4] == "मिनेट"
    return hour, INV[t[3]]


def test_every_minute_of_the_day_round_trips():
    for h in range(24):
        for m in range(60):
            out = say(f"समय {h}:{m:02d} हो")
            assert out.startswith("समय ") and out.endswith(" बजे हो") or out.endswith(" मिनेट हो")
            words = out[len("समय ") : -len(" हो")]
            assert parse_time_words(words) == (h, m), (h, m, out)


def test_twelve_hour_clock_times_round_trip_to_the_same_minute():
    for h12 in range(1, 13):
        for ap, add in (("AM", 0), ("PM", 12)):
            hour = h12 % 12 + add
            out = say(f"समय {h12}:00 {ap} हो")
            assert parse_time_words(out[len("समय ") : -len(" हो")]) == (hour, 0)


def test_a_long_unseparated_digit_run_is_a_phone_like_string_and_keeps_every_digit():
    spoken = say("यो 10000000000 हो")
    toks = spoken.split()[1:-1]
    assert toks == ["एक"] + ["शून्य"] * 10  # 11 digits, every one kept in order


# ---- a trailing rupee word marks an amount ----------------------------------------------------

SUFFIXES = ["रुपैयाँ", "रुपैया", "rupees"]


def test_a_trailing_rupee_word_makes_a_long_digit_run_an_amount_not_a_phone_number():
    assert say("मूल्य 1500000 रुपैयाँ छ") == "मूल्य पन्ध्र लाख रुपैयाँ छ"
    assert say("मूल्य १५००००० रुपैया छ") == "मूल्य पन्ध्र लाख रुपैया छ"
    assert say("मूल्य 1500000 rupees छ") == "मूल्य पन्ध्र लाख rupees छ"
    assert say("मूल्य १,००,००० रुपैयाँ छ") == "मूल्य एक लाख रुपैयाँ छ"
    assert say("मूल्य 2000.0 रुपैयाँ छ") == "मूल्य दुई हजार रुपैयाँ छ"
    # without the rupee word, the same digits are still a phone-like string
    assert say("मूल्य 1500000 छ") == "मूल्य एक पाँच शून्य शून्य शून्य शून्य शून्य छ"


def test_amounts_with_a_trailing_rupee_word_round_trip():
    for n in amounts():
        for f in (str(n), indian(n), str(n).translate(DEV)):
            for suf in SUFFIXES:
                out = say(f"मूल्य {f} {suf} छ")
                assert out.startswith("मूल्य ") and out.endswith(f" {suf} छ"), (f, suf, out)
                words = out[len("मूल्य ") : -len(f" {suf} छ")]
                assert parse_number_words(words) == n, (f, suf, out)


def test_a_trailing_rupee_word_after_an_ambiguous_number_passes_through():
    for text in ["मूल्य 2000.5 रुपैयाँ छ", "मूल्य -5 रुपैयाँ छ", "मूल्य 1,2,3 रुपैयाँ छ"]:
        assert say(text) == text


def test_english_with_a_trailing_rupee_word_only_cleans_a_trailing_point_zero():
    assert say("It costs 2000.0 rupees") == "It costs 2000 rupees"
    assert say("It costs 1500000 rupees") == "It costs 1500000 rupees"  # not read as a phone number
