#!/usr/bin/env bash
# Verify a deployed gateway from OUTSIDE the VPS: /health reports the right sha, the route
# allowlist is enforced at the proxy, a wrong bearer is rejected before any backend call, HSTS is
# present, and /console/* is gated. NEVER sends a conversational request (see deploy.yml's header
# comment) -- shared, byte-identical, between stage and prod (.github/workflows/deploy.yml calls
# this once per environment, with that environment's domain and sha).
#
# Usage: verify-deploy.sh <domain> <sha>
set -u

DOMAIN="${1:?usage: verify-deploy.sh <domain> <sha>}"
SHA="${2:?usage: verify-deploy.sh <domain> <sha>}"
URL="https://${DOMAIN}"

# First deploy: Traefik may still be obtaining the certificate (ACME HTTP-01).
ok=0
for i in $(seq 1 30); do
  BODY=$(curl -fsS -m 10 "$URL/health" 2>/dev/null || true)
  case "$BODY" in *"\"sha\":\"$SHA\""*) ok=1; break ;; esac
  echo "attempt $i: /health not serving $SHA yet"; sleep 10
done
[ "$ok" = 1 ] || { echo "::error::/health never reported $SHA"; exit 1; }
echo "OK  /health serves $SHA"

fail=0
check() { # name expected actual
  if [ "$2" = "$3" ]; then echo "OK  $1 -> $3"; else echo "::error::$1 expected $2 got $3"; fail=1; fi
}
# "Rejected at the proxy" is the property that matters, not which code a given Traefik version
# emits. Production runs Traefik v2.11, which answers 405 (empty body) when a router matches the
# PATH but not the METHOD, where v3 answers 404. Do NOT "fix" these back to 404-only: the 405 is
# the method filter working.
rejected() { # name actual
  case "$2" in 404|405) echo "OK  $1 -> $2 (rejected at proxy)" ;;
    *) echo "::error::$1 expected 404 or 405 got $2"; fail=1 ;; esac
}
code() { curl -s -o /dev/null -m 10 -w '%{http_code}' "$@"; }

# No router matches these at all, so every Traefik version answers 404.
check "GET /docs"            404 "$(code "$URL/docs")"
check "GET /openapi.json"    404 "$(code "$URL/openapi.json")"
check "POST /v1/turn"        404 "$(code -X POST "$URL/v1/turn" -d '{}')"
check "GET /"                404 "$(code "$URL/")"
# Path matches an allowed route, method does not: 404 (v3) or 405 (v2.11).
rejected "GET /chat/completions"    "$(code "$URL/chat/completions")"
rejected "PUT /chat/completions"    "$(code -X PUT "$URL/chat/completions" -d '{}')"
rejected "DELETE /chat/completions" "$(code -X DELETE "$URL/chat/completions")"
rejected "POST /health"             "$(code -X POST "$URL/health" -d '{}')"
# P3 brief A2: POST/OPTIONS /v1/widget/stream is the widget's public chat route -- every other
# method must be rejected at the proxy, same as the checks above.
rejected "GET /v1/widget/stream"    "$(code "$URL/v1/widget/stream")"
rejected "PUT /v1/widget/stream"    "$(code -X PUT "$URL/v1/widget/stream" -d '{}')"
# A disallowed Origin is rejected by the APP (no tenant's allowed_origins list has this origin --
# it's not a real site), never reaching a backend: 403, with no CORS header on the response (a
# real listed origin would get one; asserting its absence here is the actual safety property).
disallowed_origin_headers=$(curl -s -D- -o /dev/null -m 10 -X POST "$URL/v1/widget/stream" \
  -H 'origin: https://not-a-real-tenant-origin.example.com' \
  -H 'content-type: application/json' \
  -d '{"site_id":"verify-deploy-probe","question":"x","session_id":"verify-deploy-probe"}')
disallowed_origin_code=$(printf '%s' "$disallowed_origin_headers" | head -1 | tr -dc '0-9')
check "POST /v1/widget/stream disallowed origin" 403 "$disallowed_origin_code"
if printf '%s' "$disallowed_origin_headers" | grep -qi '^access-control-allow-origin:'; then
  echo "::error::disallowed Origin got a CORS header on /v1/widget/stream"; fail=1
else
  echo "OK  disallowed Origin -> no access-control-allow-origin header"
fi
# HEAD does not match Method(`GET`) either, so it is rejected too. Deliberate evidence that the
# method filter works (and the reason HSTS below must be read from a GET).
rejected "HEAD /health"             "$(code -I "$URL/health")"
# A wrong bearer is rejected BEFORE any backend call, so this cannot reach the clinic system.
check "POST /chat/completions wrong bearer" 401 "$(code -X POST "$URL/chat/completions" \
  -H 'authorization: Bearer definitely-wrong' -H 'content-type: application/json' \
  -H 'traceparent: 00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01' \
  -d '{"stream":true,"messages":[{"role":"user","content":"x"}]}')"
# Read HSTS from a real GET. `curl -I` sends HEAD, which does not match the router, so its
# middleware never runs and the header is absent: a false negative.
if curl -s -D- -o /dev/null -m 10 "$URL/health" | grep -qi '^strict-transport-security:'; then
  echo "OK  HSTS present (GET /health)"
else
  echo "::error::HSTS header missing on GET /health"; fail=1
fi

# S6 brief §4.1: /console/* is 401 or a redirect WITHOUT a session cookie, never 200. No cookie is
# ever sent here -- this is exactly the anonymous-browser case.
console_gated() { # name path
  got="$(code "$URL$2")"
  case "$got" in 401|301|302|303|307|308) echo "OK  $1 -> $got (gated)" ;;
    *) echo "::error::$1 expected 401 or a redirect, got $got"; fail=1 ;; esac
}
console_gated "GET /console/fleet"           "/console/fleet"
console_gated "GET /console/calls"           "/console/calls"
console_gated "GET /console/cost"            "/console/cost"
console_gated "GET /console/config"          "/console/config"
console_gated "GET /console/"                "/console/"
# The login page itself must still render (200) with no cookie -- it's how you get one.
check "GET /console/login" 200 "$(code "$URL/console/login")"
exit $fail
