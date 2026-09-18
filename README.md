# orca-gateway

> **Nothing product-specific goes in this repo.** No clinics, no bookings, no services, slots,
> patients or ClinicMD. The gateway moves audio and turns; it must never learn what a booking is.
> The words clinic / dental / appointment / patient / booking must not appear in `src/` except in
> test fixtures and tenant-config *values*. Grep before every PR.

The channel and control plane for Orca: the agent seam, channel adapters (voice first), tenant
config, metering, and a console UI. It does **not** hold agents — the clinic agent stays in
Zunkiree and is reached over HTTP through the seam.

## Status

S1 — skeleton. Local-first: no deploy, no VPS, no domain yet.

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
