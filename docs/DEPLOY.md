# Deploying orca-gateway

**Stage URL:** `https://orca-gw-stage.zunkireelabs.com` (plain A record, Let's Encrypt via Traefik, HSTS on)
**Prod URL:** `https://orca-gw.zunkireelabs.com` -- see [Production](#production) below. Same VPS,
own container, own schema, own secrets; stage is untouched by anything in that section.

> ⚠️ **"Stage" is not "safe".** This gateway fronts Zunkiree *stage*, and the `dental-city` tenant's
> ClinicMD credentials are **production**. A conversational request through this URL can create a
> real appointment in a real clinic's live system. The deploy pipeline never sends one; neither
> should you outside a supervised test with a TEST identity.

## What is public, and why

Exactly two routes, enforced **in the Traefik router rule** (`docker-compose.yml`), not only in the app:

| Route | Why it is public |
|---|---|
| `POST /chat/completions` | The voice platform's custom-LLM client calls it. Guarded by a shared secret: `Authorization: Bearer <ORCA_VOICE_SHARED_SECRET>` (constant-time compare; the platform sends an Authorization header even with no key set, so only the *value* counts). Fails closed (503) if the secret is unset. |
| `GET /health` | Liveness + which commit is running: `{"status":"ok","sha":"<full commit sha>"}`. Unauthenticated, cheap, and **never touches a backend**: a probing health check would compete for the 1-run cap. |

Production Traefik is **v2.11**: a request that matches an allowed *path* but not its *method* gets an
empty-body **405**, not 404 (v3 says 404); `HEAD /health` is 405 too, since the router is `Method(GET)`.
Both mean "rejected at the proxy". Verify proxy behaviour against the production version, not a newer one.

Everything else (other methods, `/docs`, `/openapi.json`, unknown paths, wrong `Host`) gets Traefik's own
404 before reaching the container. The app also disables `/docs`, `/redoc` and `/openapi.json`.
The route allowlist is what keeps a future accidental route (like the unauthenticated `/v1/turn`, removed
in #4) from being reachable. **Any new route must be added to the router rule deliberately, in review.**

## How it deploys (stage)

Two independent paths in one workflow (`.github/workflows/deploy.yml`): a push to `main` deploys
**stage only**; a manual dispatch deploys **prod only**, never the reverse. See
[Production](#production) for the prod path.

`push to main` → `.github/workflows/deploy.yml`:

1. **checks**: ruff + pytest.
2. **release**: build once, push `ghcr.io/zunkireelabs/orca-gateway:<full-sha>` (and `:main`; never `:latest`), with `GIT_SHA` baked in.
3. **migrate**: applies `migrations/*.sql` to schema `orca_gw` (idempotent, checksummed) and bootstraps
   tenants from `config/bootstrap-tenants/*.json`, which only INSERTS slugs that are absent and so never
   overwrites an edit made afterwards. If this fails, deploy is skipped and the previous build keeps serving.
4. **deploy** (SSH): fresh HTTPS checkout of the repo into `/home/zunkireelabs/devprojects/orca-gateway-stage`
   with `git checkout -B main origin/main` **and `git reset --hard`** (the non-force checkout alone keeps
   uncommitted drift across deploys), render `.env` from Actions secrets, pull the image, recreate the
   container, then confirm the **running container** reports the deployed sha.
5. **verify** (retries ~5 min: the first deploy may still be obtaining the Let's Encrypt certificate; if it times out, read Traefik's ACME logs before suspecting the route allowlist) (from a GitHub runner, i.e. outside the VPS): `/health` reports the sha; the rest of the
   surface 404s; a wrong bearer gets 401; HSTS present. **No conversational request is ever sent.**

`.env` is regenerated every deploy. **Never hand-edit it on the VPS.** `.dockerignore` excludes `.env*`,
and the Dockerfile copies only named paths, so it can't end up in the image.

### Required repo secrets (stage) (set by a human, from 1Password: `gh secret set NAME`)

`VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `ORCA_VOICE_SHARED_SECRET`, `ORCA_DATABASE_URL`.
Verify by **name only**: `gh secret list`. Never print a value.
These are plain **repo-level** secrets (this org is on GitHub Free, so no deployment environments,
hence the `PROD_` prefix on prod's own secrets below rather than an environment-scoped secret of
the same name). Generate the voice secret as **hex** (`openssl rand -hex 32`): it is rendered into
`.env`, which docker compose also reads for interpolation, so a `$` in a secret would be mangled.

### Runtime config (stage) (rendered into `.env`)

| Var | Value | Note |
|---|---|---|
| `ORCA_DATABASE_URL` | secret | Postgres holding tenant config (schema `orca_gw`). Use the Supabase **session-mode pooler** URL with `sslmode=require` (GitHub runners are IPv4-only and the direct host is IPv6-only; transaction-mode pooling breaks the migration advisory lock). Percent-encode the password. |
| `ORCA_VOICE_MAX_CONCURRENT_RUNS` | `1` | **Do not raise.** Zunkiree stage has a 2-socket pool sharing a ceiling with PROD. Never load-test. |
| `ORCA_VOICE_RUN_TIMEOUT_S` | `25.0` | |

**Run exactly one uvicorn worker** (the Dockerfile and compose pin `--workers 1`). Turn de-duplication and
the global run cap live in process memory; a second worker would silently split them. A restart drops
in-flight turns (the platform retries a failed request at the same depth).

## Roll back (stage)

Preferred: **revert the commit on `main`**; CI redeploys the previous code with the normal checks.

Emergency (on the VPS, no editing of files): pin a previous image by sha.

```bash
cd /home/zunkireelabs/devprojects/orca-gateway-stage
IMAGE_TAG=<previous-full-sha> docker compose up -d --no-build --force-recreate orca-gateway-stage
curl -s https://orca-gw-stage.zunkireelabs.com/health   # confirm the sha
```

The next push to `main` deploys `main` again, so follow an emergency pin with a revert.

## Production

P2 brief (`~/Projects/sadin-stark-brain/docs/orca-platform/platform/P2-ORCA-PRODUCTION-BRIEF.md`)
§3 Part A, D2–D6. Prod is **a second, independent target of the same pipeline** — same repo, same
`docker-compose.yml`, same VPS — never a fork of the stage config. It is deployed **only** by a
manual `workflow_dispatch` with an explicit commit sha, **never** on push; during the Nov 1–10
freeze nobody dispatches it except to fix a break. Stage keeps deploying on every push exactly as
above, untouched by anything below.

### How it deploys

`gh workflow run deploy.yml -f sha=<full commit sha>` (the sha must already be on `main` — checked
in CI before anything else runs — and must already have an image at
`ghcr.io/zunkireelabs/orca-gateway:<sha>`, i.e. it already went through an ordinary push-to-main
deploy to stage). Unlike the stage path, **prod never builds**: `validate-image-prod` confirms
that image is pullable, then:

1. **migrate-prod**: applies `migrations/*.sql` to schema `orca_gw_prod` (D2 — same Supabase
   project as stage's `orca_gw`, never the same schema) and bootstraps tenants from
   `config/bootstrap-tenants-prod/*.json` (A3) — **never** `config/bootstrap-tenants/` (stage's).
2. **deploy-prod** (SSH): same shape as stage's deploy step, at its own checkout path
   (`/home/zunkireelabs/devprojects/orca-gateway-prod`), its own container (`orca-gateway-prod`),
   its own compose service, and a pre-flight check that the checkout has no uncommitted drift
   after `git reset --hard` before rendering `.env`.
3. **verify-prod**: the same outside checks as stage (`scripts/verify-deploy.sh`, shared between
   both), against `https://orca-gw.zunkireelabs.com` (D3).

### Required repo secrets (prod)

`PROD_VOICE_SHARED_SECRET`, `PROD_ORCA_DATABASE_URL`, `PROD_CONSOLE_SECRET` — **fresh, never
copied from stage's** (D6; also closes a hygiene item: the stage console secret has appeared in
two transcripts). `VPS_HOST`/`VPS_USER`/`VPS_SSH_KEY` are shared with stage (same VPS). **Sadin
sets these**; the Window verifies by **name only** (`gh secret list`), never a value.

### Runtime config (prod) (rendered into `.env`)

| Var | Value | Note |
|---|---|---|
| `ORCA_DATABASE_URL` | `PROD_ORCA_DATABASE_URL` secret | Same Supabase project as stage, session-mode pooler URL, `sslmode=require`. Percent-encode the password. |
| `ORCA_DB_SCHEMA` | `orca_gw_prod` | D2. Stage and prod never share a schema — separate tenant config, kill switches, call records. |
| `ORCA_VOICE_MAX_CONCURRENT_RUNS` | `5` | A2. Proven by the concurrency proof (P2 brief §5) before the pilot, against prod's own backend (the clinic lane, Part B), never against stage's shared pool. |
| `ORCA_VOICE_RUN_TIMEOUT_S` | `25.0` | Same as stage. |

**Run exactly one uvicorn worker**, same as stage — this is what keeps the run cap and turn
de-duplication correct in process memory; raising `ORCA_VOICE_MAX_CONCURRENT_RUNS` never means
raising the worker count.

Prod's tenant config points `dental-city` at **the clinic lane** (Part B: a dedicated,
single-worker Zunkiree container, never Zunkiree stage and never Zunkiree prod's own API
container) — see `config/bootstrap-tenants-prod/README.md` for the current, provisional URL and
what confirms it. The voice channel bootstraps with `kill_switch: true`; flip it from the console
only once the clinic lane and the prod secrets are verified.

### Roll back (prod)

Preferred, and the one to rehearse before the pilot (exit test §1.5): **re-point the ElevenLabs
pilot agent's Custom LLM URL back to stage**, place a call, confirm it answers, then point it
forward again. This is the rollback that matters — it doesn't touch this deploy pipeline at all.

Pipeline-level: re-dispatch with the previous good sha —
`gh workflow run deploy.yml -f sha=<previous-full-sha>`. **Only shas from this PR onward can be
dispatched** — `deploy.yml`'s `checks` job rejects any `sha` input that isn't exactly 40 lowercase
hex characters before any step uses it, so a short, uppercase, or otherwise malformed sha (from a
pre-PR habit or a copy/paste slip) fails fast instead of silently resolving to the wrong ref.
Emergency (on the VPS): same pin pattern as stage, against the prod checkout and container:

```bash
cd /home/zunkireelabs/devprojects/orca-gateway-prod
IMAGE_TAG=<previous-full-sha> docker compose up -d --no-build --force-recreate orca-gateway-prod
curl -s https://orca-gw.zunkireelabs.com/health   # confirm the sha
```

A deploy of the clinic lane or the prod gateway drops any in-flight calls (in-memory state; P2
brief §6) — acceptable under the dispatch-only rule and the Nov 1–10 freeze, not after.

### Checks (prod)

```bash
curl -s https://orca-gw.zunkireelabs.com/health                # 200 + sha
curl -s -o /dev/null -w '%{http_code}\n' https://orca-gw.zunkireelabs.com/docs   # 404
```

## Re-point the voice platform's dashboard (stage)

Agent → LLM → **Custom LLM**:
- **Server URL:** `https://orca-gw-stage.zunkireelabs.com` (bare; the form appends `/chat/completions`), format **Chat Completions**.
- **API Key:** the value of `ORCA_VOICE_SHARED_SECRET`, so it can't leak through a stray copy of the URL.
- Keep **Backup LLM = Disabled** (Default would let a vendor model with no clinic tools answer a caller), **Speculative turn OFF**, **Turn V3 on**.
- Click **Test Connection** (costs no call minutes). Use it, not a live call, to verify.

For the prod pilot agent, the same steps against `https://orca-gw.zunkireelabs.com` and
`PROD_VOICE_SHARED_SECRET` — done once, by Sadin, when exit test §2 is ready, not before.

## Checks (stage)

```bash
curl -s https://orca-gw-stage.zunkireelabs.com/health                # 200 + sha
curl -s -o /dev/null -w '%{http_code}\n' https://orca-gw-stage.zunkireelabs.com/docs   # 404
```

## Tenants and the dashboard header

Which tenant a call is for comes from the **`X-Orca-Tenant`** request header, set per agent in the voice
platform's **Request headers** (e.g. `X-Orca-Tenant: dental-city`). A missing or malformed header is a 400;
an unknown, inactive, disabled or killed tenant is a uniform 403. **There is no default tenant.**
Add the header in the dashboard *before* merging the release that requires it (old code ignores it).
