"""Built-in caller-facing messages for the guards a channel can speak (P4 brief A1, extended by the
P4 gateway-polish brief's A3+A4). Generic by construction: nothing here knows what a tenant sells.
`{brand}` is the channel's spoken brand name. A tenant overrides the wording per channel
(`kill_switch_message`, `error_fallback_message`, `phone_guard_message`, `silence_nudge_message`);
an override is one tenant-authored string, so it is used verbatim regardless of caller language.
Absent an override, the built-in follows an explicit `lang` when the caller passes one (A3), else
the channel's default language, and falls back to English for any language without a translation."""

from __future__ import annotations

from typing import Literal

from orca_gateway.channels.number_speech import _DEVANAGARI
from orca_gateway.tenants import ChannelConfig

Kind = Literal["kill_switch", "error_fallback", "phone_guard", "silence_nudge"]

_DEFAULTS: dict[Kind, dict[str, str]] = {
    "kill_switch": {
        "en": "{brand} can't take calls right now. Please try again later.",
        "ne": "{brand} ले अहिले कल लिन सक्दैन। कृपया पछि फेरि प्रयास गर्नुहोस्।",
    },
    "error_fallback": {
        "en": "Sorry, I'm having trouble right now. Could you say that again?",
        "ne": "माफ गर्नुहोस्, मलाई अहिले समस्या भइरहेको छ। कृपया फेरि भन्नुहोस्।",
    },
    # Spliced INTO a sentence in place of a number, so it starts lowercase and has no full stop.
    "phone_guard": {
        "en": "please check the {brand} website for our contact number",
        "ne": "कृपया सम्पर्क नम्बरको लागि {brand} को वेबसाइट हेर्नुहोस्",
    },
    # REVIEW NEEDED (native ear), same discipline as number_speech's own note.
    "silence_nudge": {
        "en": "Sorry, I didn't catch that. Could you say that again?",
        "ne": "माफ गर्नुहोस्, मैले बुझिनँ। कृपया फेरि भन्नुहोस्।",
    },
}


def caller_language(text: str, ch: ChannelConfig) -> str:
    """The language to SPEAK a built-in message in for this caller turn. Devanagari -> 'ne';
    otherwise the Latin-script language 'en' IF the channel serves it, else the tenant default.
    Product-blind, script-only -- the same doctrine `number_speech` already uses (module docstring
    lines 5-7): a sentence containing any Devanagari character is Nepali, otherwise English.

    Known limitation (stated, not fixed): romanized Nepali (Nepali written in Latin letters) is
    still Latin script, so it resolves to 'en' when the channel serves English. Acceptable for a
    built-in guard message -- English is a safe lingua franca and still beats today's
    Nepali-spliced-into-an-English-sentence. Same Devanagari-only limit number_speech lives with;
    do not add a romanized-Nepali detector here."""
    lang = "ne" if _DEVANAGARI.search(text or "") else "en"
    served = {tag.split("-")[0].lower() for tag in ch.languages}
    return lang if lang in served else ch.default_language.split("-")[0].lower()


def spoken_message(kind: Kind, ch: ChannelConfig, lang: str | None = None) -> str:
    override = {
        "kill_switch": ch.kill_switch_message,
        "error_fallback": ch.error_fallback_message,
        "phone_guard": ch.phone_guard_message,
        "silence_nudge": ch.silence_nudge_message,
    }[kind]
    pick = lang or ch.default_language.split("-")[0].lower()
    text = override or _DEFAULTS[kind].get(pick) or _DEFAULTS[kind]["en"]
    return text.replace("{brand}", ch.spoken_brand_name)
