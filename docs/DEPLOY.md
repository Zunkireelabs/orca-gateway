# Deploying orca-gateway (stage)

**URL:** `https://orca-gw-stage.zunkireelabs.com` (plain A record, Let's Encrypt via Traefik, HSTS on)

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

## How it deploys

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

### Required repo secrets (set by a human, from 1Password: `gh secret set NAME`)

`VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY`, `ORCA_VOICE_SHARED_SECRET`, `ORCA_DATABASE_URL`.
Verify by **name only**: `gh secret list`. Never print a value.
These are plain **repo-level** secrets (this org is on GitHub Free, so no deployment environments).
Generate the voice secret as **hex** (`openssl rand -hex 32`): it is rendered into `.env`, which
docker compose also reads for interpolation, so a `$` in a secret would be mangled.

### Runtime config (rendered into `.env`)

| Var | Value | Note |
|---|---|---|
| `ORCA_DATABASE_URL` | secret | Postgres holding tenant config (schema `orca_gw`). Use the Supabase **session-mode pooler** URL with `sslmode=require` (GitHub runners are IPv4-only and the direct host is IPv6-only; transaction-mode pooling breaks the migration advisory lock). Percent-encode the password. |
| `ORCA_VOICE_MAX_CONCURRENT_RUNS` | `1` | **Do not raise.** Zunkiree stage has a 2-socket pool sharing a ceiling with PROD. Never load-test. |
| `ORCA_VOICE_RUN_TIMEOUT_S` | `25.0` | |

**Run exactly one uvicorn worker** (the Dockerfile and compose pin `--workers 1`). Turn de-duplication and
the global run cap live in process memory; a second worker would silently split them. A restart drops
in-flight turns (the platform retries a failed request at the same depth).

## Roll back

Preferred: **revert the commit on `main`**; CI redeploys the previous code with the normal checks.

Emergency (on the VPS, no editing of files): pin a previous image by sha.

```bash
cd /home/zunkireelabs/devprojects/orca-gateway-stage
IMAGE_TAG=<previous-full-sha> docker compose up -d --no-build --force-recreate orca-gateway-stage
curl -s https://orca-gw-stage.zunkireelabs.com/health   # confirm the sha
```

The next push to `main` deploys `main` again, so follow an emergency pin with a revert.

## Re-point the voice platform's dashboard

Agent → LLM → **Custom LLM**:
- **Server URL:** `https://orca-gw-stage.zunkireelabs.com` (bare; the form appends `/chat/completions`), format **Chat Completions**.
- **API Key:** the value of `ORCA_VOICE_SHARED_SECRET`, so it can't leak through a stray copy of the URL.
- Keep **Backup LLM = Disabled** (Default would let a vendor model with no clinic tools answer a caller), **Speculative turn OFF**, **Turn V3 on**.
- Click **Test Connection** (costs no call minutes). Use it, not a live call, to verify.

## Checks

```bash
curl -s https://orca-gw-stage.zunkireelabs.com/health                # 200 + sha
curl -s -o /dev/null -w '%{http_code}\n' https://orca-gw-stage.zunkireelabs.com/docs   # 404
```

## Tenants and the dashboard header

Which tenant a call is for comes from the **`X-Orca-Tenant`** request header, set per agent in the voice
platform's **Request headers** (e.g. `X-Orca-Tenant: dental-city`). A missing or malformed header is a 400;
an unknown, inactive, disabled or killed tenant is a uniform 403. **There is no default tenant.**
Add the header in the dashboard *before* merging the release that requires it (old code ignores it).
