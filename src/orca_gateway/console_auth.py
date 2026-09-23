"""Console auth (S6 brief §1): one shared secret (`ORCA_CONSOLE_SECRET`), no user accounts.

Login exchanges the secret for a signed, `HttpOnly`, `Secure`, `SameSite=Strict` session cookie
with an expiry. The cookie payload is itself the CSRF token (a session fixation is a non-issue
here: there is no per-user identity to fix, and the cookie is HttpOnly + signed, so a page cannot
read or forge it) -- every write form carries it as a hidden field, and the POST handler compares
the two with a constant-time check.

`/console/*` must be 401 or a redirect without a valid cookie, never 200 (S6 brief §4.1, checked
by the external verify). This module is the one place that decides "authenticated," so every
console route depends on it.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from orca_gateway.config import get_settings

COOKIE_NAME = "orca_console_session"
_SALT = "orca-console-session-v1"


class ConsoleAuthError(Exception):
    """No valid session. Routes turn this into a 401 (writes/exports) or a redirect (pages)."""


def _serializer() -> URLSafeTimedSerializer:
    secret = get_settings().console_secret
    if not secret:
        raise ConsoleAuthError("console not configured")
    return URLSafeTimedSerializer(secret, salt=_SALT)


def check_login(supplied_secret: str) -> bool:
    """Constant-time compare against ORCA_CONSOLE_SECRET. Fails closed if unset."""
    secret = get_settings().console_secret
    if not secret:
        return False
    return hmac.compare_digest(supplied_secret, secret)


def new_session_cookie_value() -> tuple[str, str]:
    """Returns (cookie_value, csrf_token). The csrf token IS the session payload -- there is
    nothing else to hold a session, and it not being guessable is what makes the cookie
    unforgeable without the signing key."""
    csrf = secrets.token_urlsafe(32)
    return _serializer().dumps({"csrf": csrf}), csrf


def read_session(request: Request) -> str:
    """Returns the session's csrf token, or raises ConsoleAuthError. Never logged."""
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        raise ConsoleAuthError("no session cookie")
    try:
        data = _serializer().loads(raw, max_age=get_settings().console_session_ttl_s)
    except (BadSignature, SignatureExpired) as exc:
        raise ConsoleAuthError("invalid or expired session") from exc
    csrf = data.get("csrf")
    if not isinstance(csrf, str) or not csrf:
        raise ConsoleAuthError("malformed session")
    return csrf


def require_session(request: Request) -> str:
    """FastAPI dependency for read-only pages: 302 to the login page (not raw 401) so a browser
    tab just bounces there. Still satisfies "401 or redirect, never 200"."""
    try:
        return read_session(request)
    except ConsoleAuthError:
        raise HTTPException(
            status_code=303, headers={"Location": "/console/login"}, detail="not authenticated"
        ) from None


def require_csrf(request: Request, csrf_token: str, session_csrf: str) -> None:
    """Every write compares the form's hidden field against the session's own csrf value."""
    if not hmac.compare_digest(csrf_token or "", session_csrf or ""):
        raise HTTPException(403, "bad or missing csrf token")
