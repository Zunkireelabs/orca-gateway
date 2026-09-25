"""S6 PR 2: the operating console. Four server-rendered panels (FastAPI + Jinja2, no JS
framework, no build step) over data S4/S5/S6-PR1 already write. Product-blind (§8.10): this
module shows tool names, ended_reason strings and config *values* verbatim and interprets none of
them -- it has no idea what any of it means downstream.

Everything here requires a session (see console_auth.py); `/console/*` is 401 or a redirect
without a valid cookie, never 200 -- the external verify checks this from outside on every deploy.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from orca_gateway import deps
from orca_gateway.config import get_settings
from orca_gateway.console_auth import (
    COOKIE_NAME,
    ConsoleAuthError,
    check_login,
    new_session_cookie_value,
    read_session,
    require_csrf,
    require_session,
)
from orca_gateway.reporting import Reporting
from orca_gateway.tenants import ChannelConfig

router = APIRouter(prefix="/console")
log = logging.getLogger("orca_gateway.console")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_WEEKDAYS = list(range(7))


def _reporting() -> Reporting:
    settings = get_settings()
    if not settings.database_url:
        raise HTTPException(503, "console database not configured")
    return Reporting(settings.database_url, schema=settings.db_schema)


def _cookie_kwargs() -> dict:
    settings = get_settings()
    return dict(
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=settings.console_session_ttl_s,
    )


# ---- auth -------------------------------------------------------------------------------------


@router.get("/login")
def login_form(request: Request):
    try:
        require_session(request)
        return RedirectResponse("/console/fleet", status_code=303)
    except HTTPException:
        pass
    return templates.TemplateResponse(request, "login.html", {"session": None, "error": None})


@router.post("/login")
def login_submit(request: Request, secret: str = Form(...)):
    if not check_login(secret):
        log.warning("console login failed")
        return templates.TemplateResponse(
            request,
            "login.html",
            {"session": None, "error": "Wrong secret."},
            status_code=401,
        )
    cookie_value, _csrf = new_session_cookie_value()
    resp = RedirectResponse("/console/fleet", status_code=303)
    resp.set_cookie(COOKIE_NAME, cookie_value, **_cookie_kwargs())
    log.info("console login ok")
    return resp


@router.post("/logout")
def logout(request: Request, csrf_token: str = Form(...)):
    try:
        session_csrf = read_session(request)
        require_csrf(request, csrf_token, session_csrf)
    except ConsoleAuthError:
        pass  # already logged out / expired: clearing the cookie is still fine
    resp = RedirectResponse("/console/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ---- panel 1: fleet -----------------------------------------------------------------------------


@router.get("/", include_in_schema=False)
def console_root(session: str = Depends(require_session)):
    return RedirectResponse("/console/fleet", status_code=303)


@router.get("/fleet")
async def fleet(request: Request, session: str = Depends(require_session)):
    rows = await _reporting().fleet_rows()
    return templates.TemplateResponse(
        request,
        "fleet.html",
        {
            "session": session,
            "active": "fleet",
            "rows": rows,
            "any_alert": any(r["alert"] for r in rows),
        },
    )


@router.post("/fleet/toggle")
async def fleet_toggle(
    request: Request,
    csrf_token: str = Form(...),
    tenant_slug: str = Form(...),
    channel: str = Form(...),
    field: str = Form(...),
    on: str = Form(...),
    session: str = Depends(require_session),
):
    require_csrf(request, csrf_token, session)
    if field not in ("kill_switch", "is_enabled"):
        raise HTTPException(400, "unknown field")
    repo = deps.get_tenant_repo()
    try:
        await repo.set_channel_field(tenant_slug, channel, on == "true", field=field)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    deps.get_tenant_store().invalidate(tenant_slug)  # effective now, not after the 15s TTL
    return RedirectResponse("/console/fleet", status_code=303)


# ---- panel 2: calls -----------------------------------------------------------------------------


@router.get("/calls")
async def calls(
    request: Request,
    tenant_slug: str = "",
    date: str = "",
    session: str = Depends(require_session),
):
    reporting = _reporting()
    rows = await reporting.list_calls(tenant_slug=tenant_slug or None, on_date=date or None)
    tenants = await _tenant_slugs()
    return templates.TemplateResponse(
        request,
        "calls.html",
        {
            "session": session,
            "active": "calls",
            "rows": rows,
            "tenants": tenants,
            "tenant_slug": tenant_slug,
            "on_date": date,
        },
    )


async def _tenant_slugs() -> list[str]:
    rows = await deps.get_tenant_repo().list_tenants()
    return [r["slug"] for r in rows]


@router.get("/calls/{call_id}")
async def call_detail(request: Request, call_id: str, session: str = Depends(require_session)):
    call = await _reporting().call_detail(call_id)
    if call is None:
        raise HTTPException(404, "no such call")
    return templates.TemplateResponse(
        request,
        "call_detail.html",
        {
            "session": session,
            "active": "calls",
            "call": call,
            "eleven_url": get_settings().console_elevenlabs_conversation_url.format(
                conversation_id=call["conversation_id"]
            ),
            "cutoff": Reporting.METERING_FIX_CUTOFF.isoformat(),
            "success": request.query_params.get("labeled") and "Label saved.",
        },
    )


@router.post("/calls/{call_id}/label")
async def label_call(
    request: Request,
    call_id: str,
    csrf_token: str = Form(...),
    depth: str = Form(""),
    verdict: str = Form(...),
    note: str = Form(""),
    session: str = Depends(require_session),
):
    require_csrf(request, csrf_token, session)
    if verdict not in ("good", "bad", "unsure"):
        raise HTTPException(400, "bad verdict")
    depth_val = int(depth) if depth.strip() != "" else None
    try:
        await deps.get_calls_repo().add_label(
            call_id=call_id, depth=depth_val, verdict=verdict, note=note or None
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from None
    return RedirectResponse(f"/console/calls/{call_id}?labeled=1", status_code=303)


# ---- panel 3: cost ------------------------------------------------------------------------------


@router.get("/cost")
async def cost(request: Request, session: str = Depends(require_session)):
    rows = await _reporting().cost_by_tenant_month()
    return templates.TemplateResponse(
        request,
        "cost.html",
        {
            "session": session,
            "active": "cost",
            "rows": rows,
            "cutoff": Reporting.METERING_FIX_CUTOFF.isoformat(),
        },
    )


@router.get("/cost/export.csv")
async def cost_export(
    request: Request, tenant_slug: str = "", session: str = Depends(require_session)
):
    rows = await _reporting().cost_csv_rows(tenant_slug or None)
    buf = io.StringIO()
    fields = [
        "tenant_slug",
        "conversation_id",
        "channel",
        "started_at",
        "ended_at",
        "ended_reason",
        "turn_count",
        "llm_model",
        "llm_prompt_tokens",
        "llm_completion_tokens",
        "llm_cost_usd",
        "abandoned_run_count",
        "overstated",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k) for k in fields})
    buf.seek(0)
    filename = f"orca-cost-{tenant_slug or 'all'}-{datetime.now(UTC):%Y-%m}.csv"
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


# ---- panel 4: config ----------------------------------------------------------------------------


def _handoff_hours_text(ch) -> dict[int, str]:
    hours = ch.handoff_hours or {}
    out = {}
    for wd in _WEEKDAYS:
        windows = hours.get(str(wd), [])
        out[wd] = ",".join(f"{s}-{e}" for s, e in windows)
    return out


def _parse_handoff_hours(form: dict) -> dict | None:
    result: dict[str, list[list[str]]] = {}
    for wd in _WEEKDAYS:
        raw = (form.get(f"handoff_hours_{wd}") or "").strip()
        if not raw:
            continue
        windows = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" not in part:
                raise ValueError(f"bad window {part!r} on weekday {wd} (want HH:MM-HH:MM)")
            start, end = part.split("-", 1)
            windows.append([start.strip(), end.strip()])
        if windows:
            result[str(wd)] = windows
    return result or None


@router.get("/config")
async def config_index(request: Request, session: str = Depends(require_session)):
    tenants = await deps.get_tenant_repo().list_tenants()
    tenant_channels = [
        {"tenant_slug": t["slug"], "display_name": t["display_name"], "channel": ch["channel"]}
        for t in tenants
        for ch in t["channels"]
    ]
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "session": session,
            "active": "config",
            "tenant_channels": tenant_channels,
            "tenant_slug": None,
            "concurrency_summary": _concurrency_summary(tenants),
        },
    )


# P2 brief A5 follow-up: per-environment ceilings, by channel. voice and chat share the ONE
# ORCA_VOICE_MAX_CONCURRENT_RUNS ceiling (P3 brief A1/D2 -- the env var's name is historical, not
# voice-only); a channel with its own, separate ceiling is added here, never guessed.
def _environment_ceiling(channel: str) -> int | None:
    ceiling = get_settings().voice_max_concurrent_runs
    return {"voice": ceiling, "chat": ceiling}.get(channel)


def _concurrency_summary(tenants: list[dict]) -> list[dict]:
    """Per channel, across every tenant: the environment ceiling, the sum of every tenant's OWN
    configured cap, and whether any tenant has no cap of its own. Two independent warning
    conditions, surfaced separately since they are different risks: the sum can be over the
    ceiling even with every tenant capped (they would still starve each other in the worst case),
    and a single uncapped tenant can alone exhaust the ceiling regardless of the sum."""
    caps_by_channel: dict[str, list[int | None]] = {}
    for t in tenants:
        for ch in t["channels"]:
            caps_by_channel.setdefault(ch["channel"], []).append(ch.get("max_concurrent_runs"))

    summary = []
    for channel in sorted(caps_by_channel):
        caps = caps_by_channel[channel]
        ceiling = _environment_ceiling(channel)
        configured = [c for c in caps if c is not None]
        uncapped_tenants = len(caps) - len(configured)
        total = sum(configured)
        summary.append(
            {
                "channel": channel,
                "ceiling": ceiling,
                "total": total,
                "uncapped_tenants": uncapped_tenants,
                "over_ceiling": ceiling is not None and total > ceiling,
            }
        )
    return summary


@router.get("/config/{tenant_slug}/{channel}")
async def config_edit(
    request: Request, tenant_slug: str, channel: str, session: str = Depends(require_session)
):
    cfg = await deps.get_tenant_repo().load(tenant_slug)
    if cfg is None or channel not in cfg.channels:
        raise HTTPException(404, "no such tenant/channel")
    ch = cfg.channels[channel]
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "session": session,
            "active": "config",
            "tenant_slug": tenant_slug,
            "display_name": cfg.display_name,
            "channel": channel,
            "timezone": cfg.timezone,
            "ch": ch,
            "handoff_hours_text": _handoff_hours_text(ch),
            "cache_ttl_s": get_settings().tenant_cache_ttl_s,
            "error": None,
            "success": None,
        },
    )


@router.post("/config/{tenant_slug}/{channel}")
async def config_save(
    request: Request,
    tenant_slug: str,
    channel: str,
    session: str = Depends(require_session),
):
    form = await request.form()
    require_csrf(request, form.get("csrf_token", ""), session)

    repo = deps.get_tenant_repo()
    cfg = await repo.load(tenant_slug)
    if cfg is None or channel not in cfg.channels:
        raise HTTPException(404, "no such tenant/channel")
    ch = cfg.channels[channel]

    def _int(name: str) -> int | None:
        v = (form.get(name) or "").strip()
        return int(v) if v else None

    def _float(name: str) -> float | None:
        v = (form.get(name) or "").strip()
        return float(v) if v else None

    error = None
    try:
        timezone = (form.get("timezone") or cfg.timezone).strip()
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone {timezone!r}") from exc

        updates = dict(
            is_enabled="is_enabled" in form,
            kill_switch="kill_switch" in form,
            agent_id=(form.get("agent_id") or ch.agent_id).strip(),
            elevenlabs_agent_id=(form.get("elevenlabs_agent_id") or "").strip() or None,
            languages=[
                lang.strip() for lang in (form.get("languages") or "").split(",") if lang.strip()
            ],
            default_language=(form.get("default_language") or "").strip(),
            voice_id=(form.get("voice_id") or "").strip() or None,
            spoken_brand_name=(form.get("spoken_brand_name") or "").strip(),
            handoff_target=(form.get("handoff_target") or "").strip() or None,
            handoff_hours=_parse_handoff_hours(form),
            out_of_hours_behaviour=form.get("out_of_hours_behaviour") or ch.out_of_hours_behaviour,
            out_of_hours_message=(form.get("out_of_hours_message") or "").strip() or None,
            escalation_policy=form.get("escalation_policy") or ch.escalation_policy,
            escalation_instruction=(form.get("escalation_instruction") or "").strip() or None,
            closed_dates=[
                d.strip() for d in (form.get("closed_dates") or "").split(",") if d.strip()
            ],
            closed_weekdays=[int(d) for d in form.getlist("closed_weekdays")],
            max_session_seconds=_int("max_session_seconds"),
            daily_spend_cap=_float("daily_spend_cap"),
            per_caller_rate_limit=_int("per_caller_rate_limit"),
            max_concurrent_runs=_int("max_concurrent_runs"),
            allowed_origins=[
                o.strip() for o in (form.get("allowed_origins") or "").split(",") if o.strip()
            ],
            spoken_kill_switch="spoken_kill_switch" in form,
            kill_switch_message=(form.get("kill_switch_message") or "").strip() or None,
            spoken_error_fallback="spoken_error_fallback" in form,
            error_fallback_message=(form.get("error_fallback_message") or "").strip() or None,
        )
        # Validate the WHOLE form (both writes) before touching the database: update_channel_config
        # validates again internally, but that is after update_tenant_timezone would already have
        # committed -- checking here first is what keeps a bad channel field from leaving a saved
        # timezone change behind as a partial write.
        ChannelConfig(**{**ch.model_dump(mode="json"), **updates})
        await repo.update_tenant_timezone(tenant_slug, timezone)
        before, after = await repo.update_channel_config(tenant_slug, channel, updates)
    except (ValidationError, ValueError) as exc:
        error = str(exc)
    else:
        deps.get_tenant_store().invalidate(tenant_slug)  # effective now, not after the TTL

    cfg = await repo.load(tenant_slug)
    ch = cfg.channels[channel]
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "session": session,
            "active": "config",
            "tenant_slug": tenant_slug,
            "display_name": cfg.display_name,
            "channel": channel,
            "timezone": cfg.timezone,
            "ch": ch,
            "handoff_hours_text": _handoff_hours_text(ch),
            "cache_ttl_s": get_settings().tenant_cache_ttl_s,
            "error": error,
            "success": None if error else "Saved.",
        },
        status_code=400 if error else 200,
    )
