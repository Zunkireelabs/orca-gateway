from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Gateway-wide config. Anything backend-specific (URLs, per-tenant routing
    keys) lives here as data, never as a literal in a backend's own module.
    """

    model_config = SettingsConfigDict(env_prefix="ORCA_", env_file=".env")

    # Tenant config lives in Postgres (schema orca_gw). Required at runtime.
    database_url: str = ""
    # A config edit takes effect within this many seconds unless invalidated explicitly.
    tenant_cache_ttl_s: float = 15.0

    # Voice channel adapter. All required at runtime; the route fails closed if unset.
    voice_shared_secret: str = ""
    voice_debounce_ms: int = 300
    # Max backend runs in flight ACROSS conversations. The stage backend has a 2-socket pool
    # that shares a connection ceiling with production, so stay at or below it.
    voice_max_concurrent_runs: int = 1
    # Bound on one backend call (including waiting for a slot). A hung backend must not
    # hold a call open forever.
    voice_run_timeout_s: float = 25.0

    # Numbers are spoken as words on voice (see channels/number_speech.py). A switch, not a
    # setting to tune: turn it off only to compare against the raw model text.
    voice_number_speech: bool = True

    # Commit the running image was built from (set by the Docker build).
    git_sha: str = "unknown"

    # Console (S6 PR 2). One shared secret, exchanged at /console/login for a signed session
    # cookie -- no user accounts. Required at runtime; the route fails closed if unset (same
    # pattern as voice_shared_secret). Also used to sign/verify the session cookie itself (HMAC),
    # so there is exactly one secret to rotate, not two.
    console_secret: str = ""
    console_session_ttl_s: int = 12 * 60 * 60
    # Panel 2 links out to the ElevenLabs conversation instead of storing audio (S6 brief §2).
    # {conversation_id} is substituted. Best-guess dashboard URL pattern -- correct via env if
    # ElevenLabs' actual path differs; the console never fails if it's wrong, it just links wrong.
    console_elevenlabs_conversation_url: str = (
        "https://elevenlabs.io/app/conversational-ai/history?conversation={conversation_id}"
    )

    # Metering (S5). A call is considered ended when no turn arrives for its conversation_id for
    # this long -- there is no telephony hangup signal to end it on (see 0002_calls.sql). Swept
    # periodically, not on the request path.
    metering_idle_timeout_s: float = 300.0
    metering_sweep_interval_s: float = 60.0
    # Transcript rows (orca_gw.turns) older than this are purged by the sweep; calls, costs and
    # labels are kept. Decided 2026-09-21; revisit when call-recording legality is answered.
    metering_turn_retention_days: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()
