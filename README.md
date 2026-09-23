# orca-gateway

> **Nothing product-specific goes in this repo.** No clinics, no bookings, no services, slots,
> patients or ClinicMD. The gateway moves audio and turns; it must never learn what a booking is.
> The words clinic / dental / appointment / patient / booking must not appear in `src/` except in
> test fixtures and tenant-config *values*. Grep before every PR.

The channel and control plane for Orca: the agent seam, channel adapters (voice first), tenant
config, metering, and a console UI. It does **not** hold agents *yet* — the clinic agent stays in
Zunkiree and is reached over HTTP through the seam, and the brain (agent definitions, run loop,
evals) moves into this repo's middle box at Q4 convergence, seams unchanged. See `CLAUDE.md`.

## Status

See `docs/DEPLOY.md`.

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

Tenants are **data**, not configuration: rows in Postgres (schema `orca_gw`), so two tenants differ
by rows and never by code. See `docs/TENANT-CONFIG.md`. A voice request names its tenant with the
`X-Orca-Tenant` header; there is no default tenant.

## Console

`/console/*`: four server-rendered panels (Fleet, Calls, Cost, Config) over the data above.
Server-rendered FastAPI + Jinja2, no JS framework, no build step. Auth is one shared secret
(`ORCA_CONSOLE_SECRET`) exchanged at `/console/login` for a signed, `HttpOnly`, `Secure`,
`SameSite=Strict` session cookie -- no user accounts. PII-bearing (call transcripts), so it is
never part of the public surface pinned above by omission: it is its own gated router, checked
from outside on every deploy (`.github/workflows/deploy.yml`'s `verify` job). Every write (kill
switch, enable/disable, config edit, call/turn label) leaves one `orca_gw.config_audit` row. See
`docs/orca-platform/platform/S6-OPERATING-PANELS-BRIEF.md` in the brain folder for the full spec.

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
