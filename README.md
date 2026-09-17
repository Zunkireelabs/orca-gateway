# orca-gateway

> **Nothing product-specific goes in this repo.** No clinics, no bookings, no services, slots,
> patients or ClinicMD. The gateway moves audio and turns; it must never learn what a booking is.
> The words clinic / dental / appointment / patient / booking must not appear in `src/` except in
> test fixtures and tenant-config *values*. Grep before every PR.

The channel and control plane for Orca: the agent seam, channel adapters (voice first), tenant
config, metering, and a console UI. It does **not** hold agents — the clinic agent stays in
Zunkiree and is reached over HTTP through the seam.

## Status

S2 — the seam. Local-first: no deploy, no VPS, no domain yet.

## The seam

```
session(agent_id, channel, identity, tenant, turn) -> token stream + tool events + usage
```

Defined in `src/orca_gateway/seam.py` as the `AgentBackend` protocol. The only
implementation today is `ZunkireeAgentBackend` (`src/orca_gateway/backends/zunkiree.py`),
which calls Zunkiree's `POST /api/v1/query/stream`. That adapter is the **only** place
a backend-specific tenant key or payload shape is allowed to exist — everything above it
speaks `tenant` / `agent` / `channel` / `identity` / `turn`.

`POST /v1/turn` is the channel-agnostic entry point a future voice/chat adapter calls; it
runs one turn through the seam and streams the events back as SSE.

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
