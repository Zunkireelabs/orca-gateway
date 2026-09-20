# orca-gateway

> **Nothing product-specific goes in this repo.** No clinics, no bookings, no services, slots,
> patients or ClinicMD. The gateway moves audio and turns; it must never learn what a booking is.
> The words clinic / dental / appointment / patient / booking must not appear in `src/` except in
> test fixtures and tenant-config *values*. Grep before every PR.

The channel and control plane for Orca: the agent seam, channel adapters (voice first), tenant
config, metering, and a console UI. It does **not** hold agents — the clinic agent stays in
Zunkiree and is reached over HTTP through the seam.

## Status

S3b — deployed to stage at `https://orca-gw-stage.zunkireelabs.com` (see `docs/DEPLOY.md`).
Public surface: `POST /chat/completions` and `GET /health` only.

## The seam

```
session(agent_id, channel, identity, tenant, turn) -> token stream + tool events + usage
```

Defined in `src/orca_gateway/seam.py` as the `AgentBackend` protocol. The only
implementation today is `ZunkireeAgentBackend` (`src/orca_gateway/backends/zunkiree.py`),
which calls Zunkiree's `POST /api/v1/query/stream`. That adapter is the **only** place
a backend-specific tenant key or payload shape is allowed to exist — everything above it
speaks `tenant` / `agent` / `channel` / `identity` / `turn`.

Channel adapters call the seam **in-process** (`deps.get_backend().session(...)`); there is
deliberately no HTTP entry point to the seam. A public route that reaches a tenant's backend
must carry its own auth, as `POST /chat/completions` does (`ORCA_VOICE_SHARED_SECRET`).

Configure the tenant → backend-tenant-key mapping via `ORCA_ZUNKIREE_TENANT_KEYS` (see
`.env.example`). Never commit a real mapping.

## Run

```bash
uv sync
uv run uvicorn orca_gateway.main:app --reload
curl localhost:8000/health
```

## Test / lint

```bash
uv run pytest
uv run ruff check .
```
