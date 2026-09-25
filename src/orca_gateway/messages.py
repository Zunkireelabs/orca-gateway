"""Built-in caller-facing messages for the guards a channel can speak (P4 brief A1). Generic by
construction: nothing here knows what a tenant sells. `{brand}` is the channel's spoken brand name.
A tenant overrides the wording per channel (`kill_switch_message`, `error_fallback_message`); the
built-in follows the channel's default language and falls back to English for any other."""

from __future__ import annotations

from typing import Literal

from orca_gateway.tenants import ChannelConfig

Kind = Literal["kill_switch", "error_fallback"]

_DEFAULTS: dict[Kind, dict[str, str]] = {
    "kill_switch": {
        "en": "{brand} can't take calls right now. Please try again later.",
        "ne": "{brand} ले अहिले कल लिन सक्दैन। कृपया पछि फेरि प्रयास गर्नुहोस्।",
    },
    "error_fallback": {
        "en": "Sorry, I'm having trouble right now. Could you say that again?",
        "ne": "माफ गर्नुहोस्, मलाई अहिले समस्या भइरहेको छ। कृपया फेरि भन्नुहोस्।",
    },
}


def spoken_message(kind: Kind, ch: ChannelConfig) -> str:
    override = ch.kill_switch_message if kind == "kill_switch" else ch.error_fallback_message
    lang = ch.default_language.split("-")[0].lower()
    text = override or _DEFAULTS[kind].get(lang) or _DEFAULTS[kind]["en"]
    return text.replace("{brand}", ch.spoken_brand_name)
