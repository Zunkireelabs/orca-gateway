"""Two deliberately different tenants, as DATA. Test fixtures may name products; src/ may not."""

from __future__ import annotations

from datetime import date

from orca_gateway.tenants import ChannelConfig, TenantConfig


def voice(**over) -> ChannelConfig:
    base = dict(
        channel="voice",
        languages=["ne", "en"],
        default_language="ne",
        voice_id="voice-ne-1",
        spoken_brand_name="डेन्टल सिटी",
    )
    base.update(over)
    return ChannelConfig(**base)


def chat(**over) -> ChannelConfig:
    base = dict(
        channel="chat",
        languages=["ne", "en"],
        default_language="ne",
        spoken_brand_name="डेन्टल सिटी",
        allowed_origins=["https://widget.example.com"],
    )
    base.update(over)
    return ChannelConfig(**base)


def dental_city() -> TenantConfig:
    return TenantConfig(
        slug="dental-city",
        display_name="The Dental City",  # a label; NOT what is said aloud
        backend_config={"site_id": "dental-city", "base_url": "https://staging-api.example.com"},
        channels={"voice": voice(agent_id="front-desk", out_of_hours_behaviour="handoff_anyway")},
    )


def quiet_spa() -> TenantConfig:
    """Differs from dental_city in language order, voice, brand, backend, hours policy."""
    return TenantConfig(
        slug="quiet-spa",
        display_name="Quiet Spa Pvt Ltd",
        timezone="Asia/Kathmandu",
        backend_config={"site_id": "quiet-spa-site", "base_url": "https://other-brain.example.com"},
        channels={
            "voice": voice(
                agent_id="concierge",
                languages=["en", "ne"],
                default_language="en",
                voice_id="voice-en-9",
                spoken_brand_name="Quiet Spa",
                out_of_hours_behaviour="say_closed",
                out_of_hours_message="Thank you for calling {brand}. We are closed today.",
                closed_weekdays=[5],  # Saturday
                closed_dates=[date(2026, 10, 21)],
            )
        },
    )


class InMemoryRepo:
    def __init__(self, *tenants: TenantConfig) -> None:
        self.rows = {t.slug: t for t in tenants}
        self.loads = 0
        self.fail = False

    async def load(self, slug: str) -> TenantConfig | None:
        self.loads += 1
        if self.fail:
            raise ConnectionError("db down")
        row = self.rows.get(slug)
        return None if row is None else row.model_copy(deep=True)  # like a real DB: fresh copy
