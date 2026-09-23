# orca-gateway

**Read first:** `~/Projects/sadin-stark-brain/docs/orca-platform/ORCA-PLATFORM-VISION.md`.
§8 is decided (don't re-litigate), §10 is open, §7.1 is the vision scorecard.

## What this repo is

Orca, the agent platform. The `-gateway` name is historical. It holds the inbound seam, channel
adapters, tenant config, metering and the console, and **at Q4 convergence it receives the brain**
(agent definitions, run loop, evals) into its middle box, with the seams unchanged.

## The four parts the work is measured against

- One brain / many channels (channels are thin adapters).
- One brain / many products (tools reached over MCP with a scoped key, never an in-process import).
- An agent is an employee (identity, permissions, audit, budget, kill switch).
- Customers build their own (deliberately not yet).

## Standing rules (already in the README)

- Nothing product-specific in `src/` — grep before every PR.
- Never merge your own PR.
- Never print secrets.
- `temp_ss/` and `.secrets-local/` are gitignored.
