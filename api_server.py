# api_server.py — CRACK SMS API Server v4.0
# Developer: @NONEXPERTCODER
"""
FastAPI server:
  • /                    — Animated public live OTP dashboard
  • /api/public/otps     — Public JSON (no token, all OTPs)
  • /api/public/stats    — Public stats (no token)
  • /api/sms             — Auth + panel-filtered OTPs (token required)
  • /api/stats           — Auth stats (token required)
  • /api/tokens          — List tokens (admin auth)
  • /api/tokens/create   — Create token (admin auth)
  • /api/tokens/{id}     — Delete token (admin auth)
  • /health              — Health check (public)
  • /api/docs            — API documentation page
"""

import json
import os
import secrets
import string
import sys
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Query, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, func

sys.path.insert(0, os.path.dirname(__file__))

from database import (
    AsyncSessionLocal, APIToken, Number, History, MIRROR_STATUS,
    get_api_token, update_api_token_last_used,
    create_api_token, get_all_api_tokens, delete_api_token,
)

try:
    from logging_system import bootstrap, get_logger, audit_api
    bootstrap()
    logger = get_logger("api_server")
except ImportError:
    import logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("api_server")
    async def audit_api(**kwargs): pass

app = FastAPI(title="CRACK SMS API", version="4.0.0", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1200)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    started = datetime.now()
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["X-Process-Time-Ms"] = str(int((datetime.now() - started).total_seconds() * 1000))
    if request.url.path.startswith("/api/public/otps"):
        response.headers["Cache-Control"] = "public, max-age=4, stale-while-revalidate=20"
    elif request.url.path.startswith("/api/public/stats"):
        response.headers["Cache-Control"] = "public, max-age=10, stale-while-revalidate=30"
    elif request.url.path.startswith("/api/public/site-settings"):
        response.headers["Cache-Control"] = "public, max-age=20, stale-while-revalidate=60"
    elif request.url.path.startswith("/api/public/get-number"):
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/api/public/request-status"):
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/api/public/bot-request"):
        response.headers["Cache-Control"] = "no-store"
    if request.url.path in ("/", "/api/docs", "/create-my-bot", "/track-request"):
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self';"
        )
    return response

DEV_HANDLE = "@NONEXPERTCODER"
BOT_CHANNEL = "https://t.me/crackotp"
PUBLIC_ASSIGN_USER_ID = 900000001
PUBLIC_ASSIGN_TTL_SECONDS = 3600
CONFIG_FILE = "config.json"
WEBSITE_REQUESTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "website_bot_requests")
DEFAULT_WEBSITE_ANNOUNCEMENT = (
    "Owner approvals are handled manually. Submit accurate bot details, then contact the owner "
    "with your request ID for a faster review."
)
DEFAULT_WEBSITE_STATUS_NOTE = (
    "Website requests are mirrored into the admin bot so approvals and follow-ups stay in one queue."
)
DEFAULT_WEBSITE_CONTACT_WHATSAPP = "+923000767749"
DEFAULT_WEBSITE_CONTACT_TELEGRAM = "@NONEXPERTCODER"
PUBLIC_FORM_LIMIT_WINDOW = 600
PUBLIC_FORM_LIMIT_COUNT = 8
_PUBLIC_FORM_RATE: dict[str, list[datetime]] = {}


# ══════════════════════════════════════════════════════════════════════════════
#  AUTH HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def validate_token(token: str) -> Optional[APIToken]:
    if not token:
        return None
    t = await get_api_token(token)
    if not t or t.status != "ACTIVE":
        return None
    await update_api_token_last_used(token)
    return t


def _gen_token(length: int = 40) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


# ══════════════════════════════════════════════════════════════════════════════
#  SHARED DATA FETCHERS
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_otps(limit: int = 200,
                      allowed_panels: list = None,
                      date_str: str = None) -> list:
    """
    Return OTP records from History table.
    - allowed_panels=None  → return ALL records (public endpoint)
    - allowed_panels=[...]  → filter by panel/category substring match
    """
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(History).order_by(History.created_at.desc()).limit(limit * 4)
        )).scalars().all()

    out = []
    for r in rows:
        # Date filter
        if date_str:
            try:
                if r.created_at.date() != datetime.strptime(date_str, "%Y-%m-%d").date():
                    continue
            except ValueError:
                pass

        # Panel filter (only for authenticated endpoints)
        if allowed_panels:
            cat = r.category or ""
            if not any(str(p).lower() in cat.lower() for p in allowed_panels):
                continue

        cat = r.category or ""
        if " - " in cat:
            country_part, service = (part.strip() for part in cat.split(" - ", 1))
            words = country_part.split()
            if words and any(ord(ch) > 127 for ch in words[0]):
                country = " ".join(words[1:]) or country_part
            elif len(words) > 1 and len(words[0]) in (2, 3) and words[0].isupper():
                country = " ".join(words[1:]) or country_part
            else:
                country = country_part
        else:
            country = "Unknown"
            service = cat or "Unknown"

        phone = f"+{r.phone_number}" if r.phone_number else "—"

        otp_val  = r.otp or ""
        full_sms = (f"Your {service} verification code is: {otp_val}"
                    if otp_val else f"SMS from {service} ({country})")
        out.append({
            "number":      phone,
            "service":     service,
            "country":     country,
            "otp":         r.otp or "—",
            "message":     full_sms,
            "received_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        })
        if len(out) >= limit:
            break
    return out


async def _fetch_stats() -> dict:
    async with AsyncSessionLocal() as s:
        now      = datetime.now()
        total    = await s.scalar(select(func.count(History.id))) or 0
        today    = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_ct = await s.scalar(
            select(func.count(History.id)).where(History.created_at >= today)
        ) or 0
        last_hour_since = now - timedelta(hours=1)
        last_hour_ct = await s.scalar(
            select(func.count(History.id)).where(History.created_at >= last_hour_since)
        ) or 0
        rows = (await s.execute(
            select(History.category, func.count(History.id))
            .group_by(History.category)
            .order_by(func.count(History.id).desc())
            .limit(15)
        )).all()
        services: dict = {}
        for cat, cnt in rows:
            svc = cat.split(" - ", 1)[1].strip() if cat and " - " in cat else (cat or "Unknown")
            services[svc] = services.get(svc, 0) + cnt
        since  = now - timedelta(hours=24)
        h_rows = (await s.execute(
            select(History.created_at).where(History.created_at >= since)
        )).scalars().all()
        hourly: dict = {}
        for ts in h_rows:
            b = ts.strftime("%Y-%m-%d %H:00")
            hourly[b] = hourly.get(b, 0) + 1
    return {
        "total_otps":      total,
        "otps_today":      today_ct,
        "last_hour_otps":  last_hour_ct,
        "by_service":      services,
        "hourly_last_24h": hourly,
        "generated_at":    now.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _split_category_parts(category: str) -> tuple[str, str, str]:
    cat = (category or "").strip()
    if not cat:
        return "", "Unknown", "Unknown"
    if " - " in cat:
        country_part, service = (part.strip() for part in cat.split(" - ", 1))
    else:
        country_part, service = cat, "Unknown"

    words = country_part.split()
    flag = ""
    if words and any(ord(ch) > 127 for ch in words[0]):
        flag = words[0]
        country = " ".join(words[1:]) or country_part
    elif len(words) > 1 and len(words[0]) in (2, 3) and words[0].isupper():
        country = " ".join(words[1:]) or country_part
    else:
        country = country_part
    return flag, country or "Unknown", service or "Unknown"


def _normalize_phone(value: Optional[str]) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _read_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        return {}
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("config read failed: %s", e)
        return {}


def _website_settings() -> dict:
    cfg = _read_config()
    return {
        "announcement": cfg.get("WEBSITE_ANNOUNCEMENT", DEFAULT_WEBSITE_ANNOUNCEMENT),
        "status_note": cfg.get("WEBSITE_STATUS_NOTE", DEFAULT_WEBSITE_STATUS_NOTE),
        "whatsapp": cfg.get("WEBSITE_CONTACT_WHATSAPP", DEFAULT_WEBSITE_CONTACT_WHATSAPP),
        "telegram": cfg.get("WEBSITE_CONTACT_TELEGRAM", DEFAULT_WEBSITE_CONTACT_TELEGRAM),
    }


def _ensure_website_requests_dir() -> None:
    os.makedirs(WEBSITE_REQUESTS_DIR, exist_ok=True)


def _website_request_path(req_id: str) -> str:
    safe_req_id = "".join(ch for ch in str(req_id or "") if ch.isalnum() or ch in {"-", "_"})
    return os.path.join(WEBSITE_REQUESTS_DIR, f"{safe_req_id}.json")


def _load_website_requests() -> list[dict]:
    if not os.path.isdir(WEBSITE_REQUESTS_DIR):
        return []
    rows: list[dict] = []
    for name in sorted(os.listdir(WEBSITE_REQUESTS_DIR)):
        if not name.lower().endswith(".json"):
            continue
        path = os.path.join(WEBSITE_REQUESTS_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                row = json.load(f)
            if isinstance(row, dict):
                rows.append(row)
        except Exception as e:
            logger.warning("website request read failed for %s: %s", name, e)
    rows.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    return rows


def _save_website_request(req: dict) -> None:
    req_id = str(req.get("req_id", "")).strip()
    if not req_id:
        raise ValueError("website request missing req_id")
    _ensure_website_requests_dir()
    path = _website_request_path(req_id)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(req, f, indent=2, ensure_ascii=True)
    os.replace(tmp_path, path)


def _get_website_request(req_id: str) -> Optional[dict]:
    req_id = "".join(ch for ch in str(req_id or "").strip() if ch.isalnum() or ch in {"-", "_"})
    if not req_id:
        return None
    path = _website_request_path(req_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            row = json.load(f)
        return row if isinstance(row, dict) else None
    except Exception as e:
        logger.warning("website request read failed for %s: %s", req_id, e)
        return None


def _generate_website_request_id() -> str:
    existing = {str(row.get("req_id", "")).strip() for row in _load_website_requests()}
    while True:
        req_id = f"REQ{secrets.randbelow(900000) + 100000}"
        if req_id not in existing:
            return req_id


def _clean_text(value: Optional[str], limit: int = 120) -> str:
    text = " ".join(str(value or "").strip().split())
    return text[:limit]


def _masked_token_preview(token: str) -> str:
    token = str(token or "").strip()
    if not token:
        return "—"
    if len(token) <= 10:
        return token
    return f"{token[:8]}...{token[-4:]}"


def _build_owner_message(req: dict) -> str:
    if not req.get("has_panel", True):
        return (
            f"Create My Bot request {req['req_id']}\n"
            f"Mode: No panel / forward from main panels\n"
            f"Group ID: {req['group_id']}\n"
            f"Contact method: {req['contact_method']}\n"
            f"Contact target: {req.get('contact_target', 'unknown')}"
        )
    return (
        f"Create My Bot request {req['req_id']}\n"
        f"Mode: Own panel / separate bot\n"
        f"Name: {req['user_name']}\n"
        f"Bot: {req['bot_name']} (@{req['bot_username']})\n"
        f"Admin ID: {req['admin_id']}\n"
        f"Channel: {req['channel']}\n"
        f"OTP Group: {req['otp_group']}\n"
        f"Number Bot: {req['number_bot']}\n"
        f"Support: {req['support']}\n"
        f"Group ID: {req['group_id']}\n"
        f"Token: {_masked_token_preview(req['token'])}\n"
        f"Contact method: {req['contact_method']}"
    )


def _request_mode_label(req: dict) -> str:
    return "Own panel / separate bot" if req.get("has_panel", True) else "Forward from main panels"


def _request_status_note(req: dict) -> str:
    status = str(req.get("status", "pending")).strip().lower() or "pending"
    if status == "approved":
        return "Approved by the admin bot."
    if status == "rejected":
        return "Reviewed and declined by the admin bot."
    if req.get("admin_notified_count", 0):
        return "Queued and mirrored into the Telegram admin bot."
    return "Queued on the website and waiting for the Telegram admin bot sync."


def _public_request_payload(req: dict) -> dict:
    req_id = str(req.get("req_id", "")).strip()
    bot_username = str(req.get("bot_username", "")).strip().lstrip("@")
    return {
        "request_id": req_id,
        "status": str(req.get("status", "pending")).strip().lower() or "pending",
        "status_note": _request_status_note(req),
        "mode": _request_mode_label(req),
        "has_panel": bool(req.get("has_panel", True)),
        "user_name": _clean_text(req.get("user_name"), 60) or "Unknown requester",
        "bot_name": _clean_text(req.get("bot_name"), 60) or "Forward From Main",
        "bot_username": f"@{bot_username}" if bot_username else "—",
        "group_id": str(req.get("group_id", "—")),
        "contact_method": _clean_text(req.get("contact_method"), 20).lower() or "pending",
        "contact_target": _clean_text(req.get("contact_target"), 80) or "—",
        "created_at": str(req.get("created_at", "—")),
        "bot_queue_loaded": bool(req.get("bot_queue_loaded_at") or req.get("bot_queue_loaded")),
        "bot_queue_loaded_at": str(req.get("bot_queue_loaded_at", "")) or None,
        "reviewed_at": str(req.get("reviewed_at", "")) or None,
        "reviewed_by": req.get("reviewed_by"),
        "admin_notified": bool(req.get("admin_notified_at") or int(req.get("admin_notified_count", 0) or 0) > 0),
        "admin_notified_at": str(req.get("admin_notified_at", "")) or None,
        "admin_notified_count": int(req.get("admin_notified_count", 0) or 0),
        "deployed_bot_id": str(req.get("deployed_bot_id", "")) or None,
        "support": _clean_text(req.get("support"), 60) or "—",
    }


def _build_contact_url(method: str, target: str, message: str) -> str:
    encoded = quote(message)
    if method == "whatsapp":
        digits = "".join(ch for ch in str(target) if ch.isdigit())
        return f"https://wa.me/{digits}?text={encoded}"
    handle = str(target).lstrip("@")
    return f"https://t.me/{handle}?text={encoded}"


def _enforce_public_form_rate_limit(ip_key: str) -> None:
    now = datetime.now()
    hits = [ts for ts in _PUBLIC_FORM_RATE.get(ip_key, []) if (now - ts).total_seconds() < PUBLIC_FORM_LIMIT_WINDOW]
    if len(hits) >= PUBLIC_FORM_LIMIT_COUNT:
        raise HTTPException(status_code=429, detail="Too many create-bot requests from this address. Please wait a few minutes.")
    hits.append(now)
    _PUBLIC_FORM_RATE[ip_key] = hits


def _build_history_message(service: str, country: str, otp: str) -> str:
    otp = (otp or "").strip()
    if otp:
        return f"Your {service} verification code is: {otp}"
    return f"SMS received on {service} ({country})"


async def _fetch_available_category_counts() -> list:
    async with AsyncSessionLocal() as s:
        return (await s.execute(
            select(Number.category, func.count(Number.id))
            .where(Number.status == "AVAILABLE")
            .group_by(Number.category)
            .order_by(func.count(Number.id).desc(), Number.category.asc())
        )).all()


def _build_country_catalog(rows: list) -> list:
    countries: dict[str, dict] = {}
    for category, count in rows:
        flag, country, service = _split_category_parts(category)
        if country not in countries:
            countries[country] = {
                "country": country,
                "flag": flag,
                "numbers": 0,
                "services": set(),
            }
        countries[country]["numbers"] += int(count or 0)
        if service and service != "Unknown":
            countries[country]["services"].add(service)
    out = []
    for item in countries.values():
        out.append({
            "country": item["country"],
            "flag": item["flag"],
            "numbers": item["numbers"],
            "services": len(item["services"]),
        })
    return sorted(out, key=lambda item: (-item["numbers"], item["country"]))


def _build_service_catalog(rows: list, country_name: str) -> list:
    wanted = (country_name or "").strip().lower()
    services: dict[str, dict] = {}
    for category, count in rows:
        _, country, service = _split_category_parts(category)
        if country.strip().lower() != wanted:
            continue
        if service not in services:
            services[service] = {"service": service, "numbers": 0}
        services[service]["numbers"] += int(count or 0)
    return sorted(services.values(), key=lambda item: (-item["numbers"], item["service"]))


def _match_categories(rows: list, country_name: str, service_name: str) -> list:
    wanted_country = (country_name or "").strip().lower()
    wanted_service = (service_name or "").strip().lower()
    matches = []
    for category, _ in rows:
        _, country, service = _split_category_parts(category)
        if country.strip().lower() == wanted_country and service.strip().lower() == wanted_service:
            matches.append(category)
    return matches


async def _fetch_number_history(phone_number: str, service: str, country: str, limit: int = 20) -> list:
    normalized = _normalize_phone(phone_number)
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(History)
            .where(History.phone_number == normalized)
            .order_by(History.created_at.desc())
            .limit(limit)
        )).scalars().all()
    return [{
        "otp": row.otp or "—",
        "message": _build_history_message(service, country, row.otp or ""),
        "received_at": row.created_at.strftime("%Y-%m-%d %H:%M:%S"),
    } for row in rows]


class PublicAssignRequest(BaseModel):
    service: str
    country: str
    previous_number: Optional[str] = None


class PublicBotRequestPayload(BaseModel):
    has_panel: bool = True
    user_name: str = ""
    bot_name: str = ""
    bot_username: str = ""
    token: str = ""
    admin_id: str = ""
    channel: str = ""
    otp_group: str = ""
    number_bot: str = ""
    support: str = ""
    group_id: str
    contact_method: str


async def _number_family(session, phone_number: str) -> list[Number]:
    return (await session.execute(
        select(Number).where(Number.phone_number == phone_number)
    )).scalars().all()


async def _set_public_family_available(session, phone_number: str) -> None:
    for row in await _number_family(session, phone_number):
        row.status = "AVAILABLE"
        row.assigned_to = None
        row.assigned_at = None
        row.retention_until = None


async def _assign_public_family(session, picked: Number) -> datetime:
    assigned_at = datetime.now()
    for row in await _number_family(session, picked.phone_number):
        row.status = "ASSIGNED" if row.id == picked.id else MIRROR_STATUS
        row.assigned_to = PUBLIC_ASSIGN_USER_ID
        row.assigned_at = assigned_at
        row.retention_until = assigned_at + timedelta(seconds=PUBLIC_ASSIGN_TTL_SECONDS)
    return assigned_at


async def _release_expired_public_assignments() -> None:
    cutoff = datetime.now() - timedelta(seconds=PUBLIC_ASSIGN_TTL_SECONDS)
    async with AsyncSessionLocal() as s:
        stale = (await s.execute(
            select(Number).where(
                Number.assigned_to == PUBLIC_ASSIGN_USER_ID,
                Number.status == "ASSIGNED",
            )
        )).scalars().all()
        changed = False
        seen = set()
        for row in stale:
            if row.phone_number in seen:
                continue
            seen.add(row.phone_number)
            if row.assigned_at and row.assigned_at >= cutoff:
                continue
            await _set_public_family_available(s, row.phone_number)
            changed = True
        if changed:
            await s.commit()


# ══════════════════════════════════════════════════════════════════════════════
#  PUBLIC ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/public/otps")
async def public_otps(limit: int = Query(200, ge=1, le=1000)):
    """Return ALL recent OTPs — no token required."""
    try:
        data = await _fetch_otps(limit)
        if not data:
            logger.warning(f"public_otps: No records found in History table (limit={limit})")
        return {"status": "success", "total_records": len(data), "data": data}
    except Exception as e:
        logger.exception("public_otps: %s", e)
        return {"status": "error", "message": str(e), "data": []}


@app.get("/api/debug/db-check")
async def debug_db_check():
    """Debug endpoint to check if database has History records."""
    try:
        async with AsyncSessionLocal() as s:
            total = await s.scalar(select(func.count(History.id))) or 0
            if total > 0:
                sample = (await s.execute(
                    select(History).order_by(History.created_at.desc()).limit(5)
                )).scalars().all()
                return {
                    "status": "success",
                    "total_records": total,
                    "sample": [
                        {"id": r.id, "phone": r.phone_number, "otp": r.otp, "category": r.category, "created_at": r.created_at.isoformat()}
                        for r in sample
                    ]
                }
            else:
                return {"status": "success", "total_records": 0, "sample": []}
    except Exception as e:
        logger.exception("debug_db_check: %s", e)
        return {"status": "error", "message": str(e)}


@app.get("/api/public/stats")
async def public_stats_ep():
    try:
        return {"status": "success", **(await _fetch_stats())}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/public/site-settings")
async def public_site_settings():
    settings = _website_settings()
    pending = [row for row in _load_website_requests() if row.get("status", "pending") == "pending"]
    return {
        "status": "success",
        "announcement": settings["announcement"],
        "status_note": settings["status_note"],
        "contact_whatsapp": settings["whatsapp"],
        "contact_telegram": settings["telegram"],
        "pending_requests": len(pending),
        "channel": BOT_CHANNEL,
        "developer": DEV_HANDLE,
    }


@app.get("/api/public/request-status")
async def public_request_status(request_id: str = Query(..., min_length=4)):
    req = _get_website_request(request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request ID not found.")
    return {"status": "success", "data": _public_request_payload(req)}


@app.post("/api/public/bot-request")
async def public_bot_request(payload: PublicBotRequestPayload, request: Request):
    try:
        client_ip = (request.client.host if request.client else "unknown").strip() or "unknown"
        _enforce_public_form_rate_limit(client_ip)

        settings = _website_settings()
        has_panel = bool(payload.has_panel)
        user_name = _clean_text(payload.user_name, 60)
        bot_name = _clean_text(payload.bot_name, 60)
        bot_username = _clean_text(payload.bot_username, 40).lstrip("@")
        token = str(payload.token or "").strip()
        admin_id_raw = _clean_text(payload.admin_id, 20)
        channel = _clean_text(payload.channel, 120)
        otp_group = _clean_text(payload.otp_group, 120)
        number_bot = _clean_text(payload.number_bot, 120)
        support = _clean_text(payload.support, 60) or "@ownersigma"
        group_id_raw = _clean_text(payload.group_id, 24)
        contact_method = _clean_text(payload.contact_method, 20).lower()

        if not group_id_raw.lstrip("-").isdigit():
            raise HTTPException(status_code=400, detail="Group ID must be numeric, like -1001234567890.")
        if contact_method not in {"whatsapp", "telegram"}:
            raise HTTPException(status_code=400, detail="Choose WhatsApp or Telegram as the contact method.")

        if has_panel:
            if not user_name or len(user_name) < 2:
                raise HTTPException(status_code=400, detail="Please enter your full name.")
            if not bot_name or len(bot_name) < 3:
                raise HTTPException(status_code=400, detail="Please enter the bot name.")
            if not bot_username or len(bot_username) < 4:
                raise HTTPException(status_code=400, detail="Please enter a valid bot username.")
            if ":" not in token or len(token) < 20:
                raise HTTPException(status_code=400, detail="Please provide a valid Telegram bot token from BotFather.")
            if not admin_id_raw.isdigit():
                raise HTTPException(status_code=400, detail="Admin ID must be a numeric Telegram user ID.")
            admin_id = int(admin_id_raw)
        else:
            user_name = user_name or "Website Forward Request"
            bot_name = "Forward From Main"
            bot_username = "forward_from_main"
            token = ""
            admin_id = int(admin_id_raw) if admin_id_raw.isdigit() else 0
            channel = ""
            otp_group = ""
            number_bot = ""
            support = support or settings["telegram"]

        contact_target = settings["whatsapp"] if contact_method == "whatsapp" else settings["telegram"]
        req_id = _generate_website_request_id()
        req = {
            "req_id": req_id,
            "source": "website",
            "status": "pending",
            "has_panel": has_panel,
            "uid": admin_id,
            "user_name": user_name,
            "username": f"website:{admin_id or req_id}",
            "bot_name": bot_name,
            "bot_username": bot_username,
            "token": token,
            "admin_id": admin_id,
            "channel": None if channel.lower() == "none" else channel,
            "otp_group": otp_group,
            "number_bot": number_bot,
            "support": support,
            "group_id": group_id_raw,
            "contact_method": contact_method,
            "contact_target": contact_target,
            "created_at": datetime.now().isoformat(),
            "bot_queue_loaded": False,
            "bot_queue_loaded_at": None,
            "admin_notified_at": None,
            "admin_notified_count": 0,
        }
        owner_message = _build_owner_message(req)
        req["owner_message"] = owner_message
        _save_website_request(req)

        return {
            "status": "success",
            "request_id": req_id,
            "has_panel": has_panel,
            "contact_method": contact_method,
            "contact_target": contact_target,
            "contact_url": _build_contact_url(contact_method, contact_target, owner_message),
            "prefilled_message": owner_message,
            "approval_note": settings["status_note"],
            "queued_for_admin_bot": True,
            "track_url": f"/track-request?request_id={req_id}",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("public_bot_request: %s", e)
        raise HTTPException(status_code=500, detail="Unable to submit the create-bot request right now.")


@app.get("/api/public/get-number/countries")
async def public_get_number_countries():
    try:
        rows = await _fetch_available_category_counts()
        data = _build_country_catalog(rows)
        return {"status": "success", "total_countries": len(data), "data": data}
    except Exception as e:
        logger.exception("public_get_number_countries: %s", e)
        return {"status": "error", "message": str(e), "data": []}


@app.get("/api/public/get-number/services")
async def public_get_number_services(country: str = Query(..., min_length=1)):
    try:
        rows = await _fetch_available_category_counts()
        data = _build_service_catalog(rows, country)
        return {
            "status": "success",
            "country": country,
            "total_services": len(data),
            "data": data,
        }
    except Exception as e:
        logger.exception("public_get_number_services: %s", e)
        return {"status": "error", "message": str(e), "data": []}


@app.get("/api/public/services")
async def public_services():
    try:
        rows = await _fetch_available_category_counts()
        totals: dict[str, int] = {}
        for category, count in rows:
            _, _, service = _split_category_parts(category)
            totals[service] = totals.get(service, 0) + int(count or 0)
        data = [
            {"service": service, "numbers": total}
            for service, total in sorted(totals.items(), key=lambda item: (-item[1], item[0]))
        ]
        return {"status": "success", "total_services": len(data), "data": data}
    except Exception as e:
        logger.exception("public_services: %s", e)
        return {"status": "error", "message": str(e), "data": []}


@app.post("/api/public/assign")
async def public_assign(payload: PublicAssignRequest):
    try:
        await _release_expired_public_assignments()
        rows = await _fetch_available_category_counts()
        categories = _match_categories(rows, payload.country, payload.service)
        if not categories:
            return {
                "status": "error",
                "message": "No live route found for the selected country and service.",
            }

        previous_number = _normalize_phone(payload.previous_number)
        now = datetime.now()
        async with AsyncSessionLocal() as s:
            if previous_number:
                previous = await s.scalar(
                    select(Number).where(
                        Number.phone_number == previous_number,
                        Number.assigned_to == PUBLIC_ASSIGN_USER_ID,
                    ).order_by(Number.status.desc(), Number.id.asc()).limit(1)
                )
                if previous:
                    await _set_public_family_available(s, previous.phone_number)

            query = select(Number).where(
                Number.status == "AVAILABLE",
                Number.category.in_(categories),
            )
            if previous_number:
                query = query.where(Number.phone_number != previous_number)
            query = query.order_by(func.random()).limit(1)
            picked = await s.scalar(query)

            if not picked and previous_number:
                picked = await s.scalar(
                    select(Number)
                    .where(Number.status == "AVAILABLE", Number.category.in_(categories))
                    .order_by(func.random())
                    .limit(1)
                )

            if not picked:
                return {
                    "status": "error",
                    "message": "No numbers are available right now for this route.",
                }

            now = await _assign_public_family(s, picked)
            await s.commit()

        flag, clean_country, clean_service = _split_category_parts(picked.category)
        history = await _fetch_number_history(picked.phone_number, clean_service, clean_country)
        return {
            "status": "success",
            "number": f"+{picked.phone_number}",
            "service": clean_service,
            "country": clean_country,
            "flag": flag,
            "expires_in": PUBLIC_ASSIGN_TTL_SECONDS,
            "history": history,
            "history_count": len(history),
            "assigned_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as e:
        logger.exception("public_assign: %s", e)
        return {"status": "error", "message": str(e)}


@app.get("/api/public/get-number")
async def public_get_number(
    country: str = Query(..., min_length=1),
    service: str = Query(..., min_length=1),
    exclude: Optional[str] = Query(None),
):
    try:
        rows = await _fetch_available_category_counts()
        categories = _match_categories(rows, country, service)
        if not categories:
            return {
                "status": "error",
                "message": "No live route found for the selected country and service.",
                "data": None,
            }

        normalized_exclude = _normalize_phone(exclude)
        async with AsyncSessionLocal() as s:
            query = select(Number).where(
                Number.status == "AVAILABLE",
                Number.category.in_(categories),
            )
            if normalized_exclude:
                query = query.where(Number.phone_number != normalized_exclude)
            query = query.order_by(func.random()).limit(1)
            picked = await s.scalar(query)

            if not picked and normalized_exclude:
                picked = await s.scalar(
                    select(Number)
                    .where(Number.status == "AVAILABLE", Number.category.in_(categories))
                    .order_by(func.random())
                    .limit(1)
                )

        if not picked:
            return {
                "status": "error",
                "message": "No live number is available right now for this route.",
                "data": None,
            }

        flag, clean_country, clean_service = _split_category_parts(picked.category)
        history = await _fetch_number_history(picked.phone_number, clean_service, clean_country)
        route_total = sum(int(count or 0) for cat, count in rows if cat in categories)
        return {
            "status": "success",
            "data": {
                "number": f"+{picked.phone_number}",
                "service": clean_service,
                "country": clean_country,
                "flag": flag,
                "available_numbers": route_total,
                "history_count": len(history),
                "history": history,
                "refreshed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        }
    except Exception as e:
        logger.exception("public_get_number: %s", e)
        return {"status": "error", "message": str(e), "data": None}


@app.get("/api/public/get-number/history")
async def public_get_number_history(number: str = Query(..., min_length=5)):
    try:
        normalized = _normalize_phone(number)
        if not normalized:
            raise HTTPException(400, "A valid phone number is required.")

        async with AsyncSessionLocal() as s:
            current_number = await s.scalar(
                select(Number)
                .where(Number.phone_number == normalized)
                .order_by(
                    (Number.assigned_to == PUBLIC_ASSIGN_USER_ID).desc(),
                    (Number.status == "ASSIGNED").desc(),
                    (Number.status == MIRROR_STATUS).desc(),
                    Number.id.asc(),
                )
                .limit(1)
            )
            latest_history = await s.scalar(
                select(History)
                .where(History.phone_number == normalized)
                .order_by(History.created_at.desc())
                .limit(1)
            )

        category = ""
        if current_number:
            category = current_number.category or ""
        elif latest_history:
            category = latest_history.category or ""
        else:
            raise HTTPException(404, "Number not found.")

        flag, clean_country, clean_service = _split_category_parts(category)
        history = await _fetch_number_history(normalized, clean_service, clean_country)
        return {
            "status": "success",
            "data": {
                "number": f"+{normalized}",
                "service": clean_service,
                "country": clean_country,
                "flag": flag,
                "history_count": len(history),
                "history": history,
                "refreshed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("public_get_number_history: %s", e)
        return {"status": "error", "message": str(e), "data": None}


# ══════════════════════════════════════════════════════════════════════════════
#  AUTHENTICATED ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/sms")
async def get_otps(
    request: Request,
    token: str = Query(...),
    date: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
):
    """Return OTPs filtered to the panels linked to this API token."""
    api_token = await validate_token(token)
    if not api_token:
        raise HTTPException(401, "Not authorized")
    try:
        allowed: list = json.loads(api_token.panels_data or "[]")
        # If no panels are configured on the token, return ALL OTPs
        otps = await _fetch_otps(limit, allowed_panels=allowed or None, date_str=date)
        ip = request.client.host if request.client else None
        await audit_api(
            token_name=api_token.name,
            endpoint="/api/sms",
            records_returned=len(otps),
            ip=ip,
        )
        return {
            "status":        "success",
            "token_name":    api_token.name,
            "api_dev":       api_token.api_dev or "Anonymous",
            "total_records": len(otps),
            "data":          otps,
        }
    except Exception as e:
        logger.exception("get_otps: %s", e)
        return {"status": "error", "message": str(e), "data": []}


@app.get("/api/stats")
async def get_stats_ep(token: str = Query(...)):
    t = await validate_token(token)
    if not t:
        raise HTTPException(401, "Not authorized")
    try:
        return {"status": "success", **(await _fetch_stats())}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
#  TOKEN MANAGEMENT ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/tokens")
async def list_tokens(token: str = Query(...)):
    api_token = await validate_token(token)
    if not api_token:
        raise HTTPException(401, "Not authorized")
    try:
        tokens = await get_all_api_tokens()
        return {
            "status": "success",
            "total":  len(tokens),
            "tokens": [
                {
                    "id":         t.id,
                    "name":       t.name,
                    "token":      f"{t.token[:10]}...{t.token[-4:]}",
                    "api_dev":    t.api_dev or "",
                    "created_at": t.created_at.isoformat(),
                    "last_used":  t.last_used.isoformat() if t.last_used else None,
                    "status":     t.status,
                }
                for t in tokens
            ],
        }
    except Exception as e:
        logger.exception("list_tokens: %s", e)
        return {"status": "error", "message": str(e)}


@app.post("/api/tokens/create")
async def create_token_ep(
    token: str = Query(...),
    name: str = Query(...),
    developer: str = Query(""),
    panels: str = Query("[]"),
):
    api_token = await validate_token(token)
    if not api_token:
        raise HTTPException(401, "Not authorized")
    try:
        new_tok = _gen_token(40)
        panels_list = json.loads(panels)
        await create_api_token(new_tok, name, 1, developer or None, json.dumps(panels_list))
        return {"status": "success", "token": new_tok, "name": name}
    except Exception as e:
        logger.exception("create_token: %s", e)
        return {"status": "error", "message": str(e)}


@app.delete("/api/tokens/{token_id}")
async def delete_token_ep(token_id: str, token: str = Query(...)):
    api_token = await validate_token(token)
    if not api_token:
        raise HTTPException(401, "Not authorized")
    try:
        ok = await delete_api_token(token_id)
        return {"status": "success" if ok else "error",
                "message": "Deleted" if ok else "Not found"}
    except Exception as e:
        logger.exception("delete_token: %s", e)
        return {"status": "error", "message": str(e)}


@app.post("/api/tokens/{token_id}/block")
async def block_token_ep(token_id: str, token: str = Query(...)):
    api_token = await validate_token(token)
    if not api_token:
        raise HTTPException(401, "Not authorized")
    try:
        from database import update_api_token_status
        ok = await update_api_token_status(token_id, "BLOCKED")
        return {"status": "success" if ok else "error"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/health")
async def health():
    return {
        "status":    "healthy",
        "service":   "CRACK SMS API v4",
        "developer": DEV_HANDLE,
        "timestamp": datetime.now().isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  ULTRA-MODERN ANIMATED DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

_DASHBOARD = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Virtual Number Web Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Space+Grotesk:wght@400;500;700&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#06111f;
  --panel:#0b1728;
  --panel-2:#10233a;
  --panel-3:#0d1d31;
  --line:#18314f;
  --line-strong:#28496a;
  --text:#ecf7ff;
  --muted:#8ba6c3;
  --soft:#5d7694;
  --accent:#59d4c5;
  --accent-2:#ff9166;
  --accent-3:#ffe08a;
  --ok:#4ade80;
  --warn:#ffd166;
  --shadow:0 20px 60px rgba(0,0,0,.35);
  --glass:linear-gradient(180deg,rgba(255,255,255,.06),rgba(255,255,255,.02));
  --font:'Space Grotesk',sans-serif;
  --mono:'IBM Plex Mono',monospace;
  --radius:22px;
  --radius-sm:14px;
}
html{scroll-behavior:smooth}
body{
  font-family:var(--font);
  background:
    radial-gradient(circle at top left,rgba(89,212,197,.16),transparent 28%),
    radial-gradient(circle at 85% 12%,rgba(255,145,102,.14),transparent 24%),
    radial-gradient(circle at 50% 100%,rgba(255,224,138,.08),transparent 30%),
    linear-gradient(180deg,#06111f 0%,#071423 52%,#081628 100%);
  color:var(--text);
  min-height:100vh;
  overflow-x:hidden;
}
body::before,
body::after{
  content:'';
  position:fixed;
  inset:0;
  pointer-events:none;
  z-index:0;
}
body::before{
  background-image:
    linear-gradient(rgba(255,255,255,.02) 1px,transparent 1px),
    linear-gradient(90deg,rgba(255,255,255,.02) 1px,transparent 1px);
  background-size:48px 48px;
  mask-image:linear-gradient(180deg,rgba(255,255,255,.55),transparent 90%);
}
body::after{
  background:
    radial-gradient(circle at 20% 20%,rgba(89,212,197,.16),transparent 0 26%),
    radial-gradient(circle at 80% 18%,rgba(255,145,102,.16),transparent 0 20%);
  filter:blur(80px);
  opacity:.65;
}
a{color:inherit}
button,input,select{font:inherit}

.shell{
  position:relative;
  z-index:1;
  max-width:1480px;
  margin:0 auto;
  padding:24px 20px 56px;
}
.topbar{
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:18px;
  position:sticky;
  top:16px;
  z-index:5;
  padding:18px 20px;
  border:1px solid rgba(255,255,255,.08);
  background:rgba(7,18,31,.78);
  backdrop-filter:blur(18px);
  border-radius:24px;
  box-shadow:var(--shadow);
}
.brand{
  display:flex;
  align-items:center;
  gap:14px;
  text-decoration:none;
}
.brand-mark{
  width:52px;
  height:52px;
  display:grid;
  place-items:center;
  border-radius:18px;
  background:
    linear-gradient(145deg,rgba(89,212,197,.24),rgba(255,145,102,.2)),
    rgba(255,255,255,.04);
  border:1px solid rgba(255,255,255,.1);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 12px 30px rgba(0,0,0,.22);
  font-size:1.35rem;
}
.brand-text{
  display:flex;
  flex-direction:column;
  gap:4px;
}
.brand-kicker{
  color:var(--muted);
  font-size:.72rem;
  letter-spacing:.2em;
  text-transform:uppercase;
}
.brand-name{
  font-size:1.15rem;
  font-weight:700;
  letter-spacing:.02em;
}
.topbar-actions{
  display:flex;
  align-items:center;
  gap:10px;
  flex-wrap:wrap;
  justify-content:flex-end;
}
.chip{
  display:inline-flex;
  align-items:center;
  gap:8px;
  padding:10px 14px;
  border-radius:999px;
  border:1px solid rgba(255,255,255,.08);
  background:rgba(255,255,255,.03);
  color:var(--muted);
  font-size:.78rem;
  font-family:var(--mono);
}
.chip.live{
  color:#dffef9;
  background:rgba(89,212,197,.14);
  border-color:rgba(89,212,197,.32);
}
.chip.live::before{
  content:'';
  width:9px;
  height:9px;
  border-radius:50%;
  background:var(--ok);
  box-shadow:0 0 0 0 rgba(74,222,128,.75);
  animation:pulse 1.7s infinite;
}
.chip.warm{
  color:#fff0cb;
  background:rgba(255,209,102,.12);
  border-color:rgba(255,209,102,.2);
}
.action-btn{
  padding:11px 16px;
  border:none;
  border-radius:14px;
  cursor:pointer;
  color:#07111f;
  font-weight:700;
  background:linear-gradient(135deg,var(--accent),#7de7db);
  box-shadow:0 14px 30px rgba(89,212,197,.24);
  transition:transform .2s ease, box-shadow .2s ease;
}
.action-btn.alt{
  color:var(--text);
  background:linear-gradient(135deg,rgba(255,145,102,.2),rgba(255,224,138,.22));
  border:1px solid rgba(255,255,255,.08);
  box-shadow:none;
}
.action-btn:hover{transform:translateY(-1px);box-shadow:0 16px 32px rgba(89,212,197,.3)}
.action-btn.alt:hover{box-shadow:0 12px 26px rgba(255,145,102,.15)}

.hero{
  display:grid;
  grid-template-columns:1.3fr .9fr;
  gap:18px;
  margin-top:20px;
}
.hero-copy,
.hero-panel,
.stats-card,
.chart-card,
.feed-card{
  position:relative;
  overflow:hidden;
  border-radius:var(--radius);
  border:1px solid rgba(255,255,255,.08);
  background:var(--glass),rgba(11,23,40,.88);
  backdrop-filter:blur(18px);
  box-shadow:var(--shadow);
}
.hero-copy{
  padding:32px;
}
.hero-copy::before,
.hero-panel::before,
.stats-card::before,
.chart-card::before,
.feed-card::before{
  content:'';
  position:absolute;
  inset:auto -10% 100% auto;
  width:220px;
  height:220px;
  border-radius:50%;
  background:radial-gradient(circle,rgba(89,212,197,.22),transparent 68%);
  pointer-events:none;
}
.hero-panel::before,.chart-card::before{background:radial-gradient(circle,rgba(255,145,102,.2),transparent 68%)}
.workflow-band{
  display:grid;
  grid-template-columns:repeat(3,minmax(0,1fr));
  gap:18px;
  margin-top:18px;
}
.workflow-card{
  position:relative;
  overflow:hidden;
  padding:24px;
  border-radius:var(--radius);
  border:1px solid rgba(255,255,255,.08);
  background:var(--glass),rgba(11,23,40,.88);
  box-shadow:var(--shadow);
}
.workflow-card::before{
  content:'';
  position:absolute;
  inset:-30% auto auto 58%;
  width:180px;
  height:180px;
  border-radius:50%;
  background:radial-gradient(circle,rgba(89,212,197,.18),transparent 68%);
  filter:blur(12px);
}
.workflow-card:nth-child(2)::before{background:radial-gradient(circle,rgba(255,145,102,.18),transparent 68%)}
.workflow-card:nth-child(3)::before{background:radial-gradient(circle,rgba(255,224,138,.16),transparent 68%)}
.workflow-kicker{
  position:relative;
  z-index:1;
  display:inline-flex;
  align-items:center;
  gap:8px;
  padding:7px 11px;
  border-radius:999px;
  border:1px solid rgba(255,255,255,.08);
  background:rgba(255,255,255,.04);
  color:var(--muted);
  font-size:.74rem;
  letter-spacing:.18em;
  text-transform:uppercase;
}
.workflow-card h3{
  position:relative;
  z-index:1;
  margin-top:16px;
  font-size:1.18rem;
  letter-spacing:-.02em;
}
.workflow-card p{
  position:relative;
  z-index:1;
  margin-top:10px;
  color:var(--muted);
  line-height:1.75;
  min-height:72px;
}
.workflow-meta{
  position:relative;
  z-index:1;
  display:flex;
  gap:10px;
  flex-wrap:wrap;
  margin-top:16px;
}
.workflow-pill{
  display:inline-flex;
  align-items:center;
  gap:8px;
  padding:9px 12px;
  border-radius:999px;
  border:1px solid rgba(255,255,255,.08);
  background:rgba(255,255,255,.04);
  color:var(--text);
  font-size:.82rem;
  font-family:var(--mono);
}
.workflow-actions{
  position:relative;
  z-index:1;
  display:flex;
  gap:10px;
  flex-wrap:wrap;
  margin-top:18px;
}
.eyebrow{
  display:inline-flex;
  align-items:center;
  gap:8px;
  padding:8px 12px;
  margin-bottom:18px;
  border-radius:999px;
  background:rgba(255,255,255,.04);
  border:1px solid rgba(255,255,255,.08);
  color:var(--muted);
  font-size:.76rem;
  letter-spacing:.14em;
  text-transform:uppercase;
}
.eyebrow::before{
  content:'';
  width:8px;
  height:8px;
  border-radius:50%;
  background:var(--accent);
}
.hero h1{
  max-width:12ch;
  font-size:clamp(2.2rem,4vw,4.5rem);
  line-height:.96;
  letter-spacing:-.04em;
}
.hero p{
  margin-top:16px;
  max-width:58ch;
  color:var(--muted);
  font-size:1rem;
  line-height:1.7;
}
.hero-badges{
  display:flex;
  flex-wrap:wrap;
  gap:12px;
  margin-top:24px;
}
.hero-panel{
  padding:28px;
  display:grid;
  align-content:space-between;
  gap:18px;
}
.panel-label{
  color:var(--soft);
  font-size:.76rem;
  text-transform:uppercase;
  letter-spacing:.18em;
}
.panel-value{
  display:flex;
  justify-content:space-between;
  gap:14px;
  align-items:flex-end;
  padding:16px 0;
  border-bottom:1px solid rgba(255,255,255,.08);
}
.panel-value:last-child{border-bottom:none;padding-bottom:0}
.panel-value span{
  color:var(--muted);
  font-size:.88rem;
}
.panel-value strong{
  font-size:1.02rem;
  font-family:var(--mono);
  font-weight:600;
  color:var(--text);
  text-align:right;
}

.stats-grid{
  display:grid;
  grid-template-columns:repeat(4,minmax(0,1fr));
  gap:16px;
  margin-top:18px;
}
.stats-card{
  padding:24px;
}
.stats-card .top{
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:12px;
}
.stats-card .icon{
  width:42px;
  height:42px;
  display:grid;
  place-items:center;
  border-radius:14px;
  background:rgba(255,255,255,.05);
  color:var(--muted);
  font-size:1rem;
}
.stats-card .label{
  color:var(--muted);
  font-size:.74rem;
  text-transform:uppercase;
  letter-spacing:.14em;
}
.stats-card .value{
  margin-top:18px;
  font-size:clamp(1.9rem,3vw,2.6rem);
  line-height:1;
  letter-spacing:-.05em;
  font-weight:700;
}
.stats-card .meta{
  margin-top:12px;
  color:var(--soft);
  font-size:.85rem;
}
.stats-card.accent .value{color:var(--accent)}
.stats-card.orange .value{color:var(--accent-2)}
.stats-card.gold .value{color:var(--accent-3)}

.section-head{
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:12px;
  margin:34px 0 16px;
}
.section-title{
  font-size:.84rem;
  text-transform:uppercase;
  letter-spacing:.2em;
  color:var(--soft);
}
.section-note{
  color:var(--muted);
  font-size:.86rem;
}
.chart-grid{
  display:grid;
  grid-template-columns:1.5fr .95fr;
  gap:16px;
}
.chart-card{
  padding:24px 24px 18px;
}
.chart-kicker{
  color:var(--soft);
  font-size:.74rem;
  text-transform:uppercase;
  letter-spacing:.16em;
}
.chart-card h3{
  margin-top:10px;
  font-size:1.16rem;
  letter-spacing:-.03em;
}
.chart-card p{
  margin-top:8px;
  color:var(--muted);
  font-size:.9rem;
  line-height:1.6;
}
.chart-card canvas{
  margin-top:18px;
  max-height:300px;
}

.feed-card{
  margin-top:18px;
  padding:24px;
}
.route-card{
  margin-top:18px;
}
.route-grid{
  display:grid;
  grid-template-columns:360px minmax(0,1fr);
  gap:18px;
  align-items:start;
}
.route-panel,
.inbox-card{
  border-radius:20px;
  border:1px solid rgba(255,255,255,.08);
  background:linear-gradient(180deg,rgba(255,255,255,.04),rgba(255,255,255,.025));
  padding:20px;
}
.route-panel h3,
.inbox-card h3{
  margin-top:10px;
  font-size:1.18rem;
  letter-spacing:-.03em;
}
.route-panel p{
  margin-top:8px;
  color:var(--muted);
  font-size:.92rem;
  line-height:1.65;
}
.route-label{
  display:block;
  margin:16px 0 8px;
  color:var(--soft);
  font-size:.76rem;
  text-transform:uppercase;
  letter-spacing:.14em;
}
.route-panel .ctrl-sel{
  width:100%;
}
.route-actions{
  display:flex;
  flex-wrap:wrap;
  gap:10px;
  margin-top:16px;
}
.route-actions .ctrl-btn{
  flex:1 1 150px;
}
.inbox-number{
  margin-top:14px;
  font-family:var(--mono);
  font-size:clamp(1.5rem,3vw,2.2rem);
  line-height:1.05;
  letter-spacing:-.04em;
}
.inbox-sub{
  margin-top:8px;
  color:var(--muted);
  font-size:.92rem;
}
.history-list{
  display:grid;
  gap:12px;
  margin-top:18px;
}
.history-item{
  padding:16px;
  border-radius:16px;
  border:1px solid rgba(255,255,255,.07);
  background:rgba(4,12,23,.42);
}
.history-head{
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:12px;
}
.history-otp{
  font-family:var(--mono);
  font-size:1.1rem;
  color:var(--accent-3);
  letter-spacing:.12em;
}
.history-actions{
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:12px;
  margin-top:12px;
}
.route-empty{
  margin-top:18px;
}
.feed-header{
  display:flex;
  align-items:flex-end;
  justify-content:space-between;
  gap:16px;
  margin-bottom:18px;
}
.feed-header h2{
  font-size:1.35rem;
  letter-spacing:-.03em;
}
.feed-header p{
  color:var(--muted);
  font-size:.92rem;
  margin-top:6px;
}
.controls{
  display:grid;
  grid-template-columns:minmax(0,1.4fr) repeat(3,auto);
  gap:12px;
  margin-bottom:22px;
}
.search-wrap{
  position:relative;
}
.search-wrap input,
.ctrl-sel{
  width:100%;
  min-height:52px;
  padding:0 16px 0 48px;
  border-radius:16px;
  border:1px solid rgba(255,255,255,.08);
  outline:none;
  background:rgba(255,255,255,.04);
  color:var(--text);
}
.ctrl-sel{
  min-width:180px;
  padding:0 42px 0 16px;
}
.search-wrap input:focus,
.ctrl-sel:focus{
  border-color:rgba(89,212,197,.4);
  box-shadow:0 0 0 4px rgba(89,212,197,.12);
}
.search-icon{
  position:absolute;
  inset:0 auto 0 18px;
  display:grid;
  place-items:center;
  color:var(--soft);
}
.ctrl-btn{
  min-height:52px;
  padding:0 18px;
  border:none;
  border-radius:16px;
  cursor:pointer;
  color:var(--text);
  font-weight:700;
  background:rgba(255,255,255,.05);
  border:1px solid rgba(255,255,255,.08);
  transition:transform .18s ease, border-color .18s ease, background .18s ease;
}
.ctrl-btn:hover{
  transform:translateY(-1px);
  border-color:rgba(89,212,197,.3);
}
.ctrl-btn.accent{
  color:#07111f;
  background:linear-gradient(135deg,var(--accent),#9af3e9);
  box-shadow:0 14px 28px rgba(89,212,197,.22);
}

.feed-meta{
  display:flex;
  justify-content:space-between;
  align-items:center;
  gap:12px;
  margin-bottom:16px;
  color:var(--muted);
  font-size:.86rem;
}
.otp-grid{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(290px,1fr));
  gap:14px;
}
.otp-card{
  position:relative;
  padding:18px;
  border-radius:18px;
  border:1px solid rgba(255,255,255,.08);
  background:linear-gradient(180deg,rgba(255,255,255,.04),rgba(255,255,255,.025));
  overflow:hidden;
  transition:transform .18s ease,border-color .18s ease, box-shadow .18s ease;
  animation:card-in .35s cubic-bezier(.2,.8,.2,1) both;
}
.otp-card:hover{
  transform:translateY(-3px);
  border-color:rgba(89,212,197,.24);
  box-shadow:0 16px 36px rgba(0,0,0,.24);
}
.otp-card::after{
  content:'';
  position:absolute;
  inset:auto auto -20px -10px;
  width:120px;
  height:120px;
  border-radius:50%;
  background:radial-gradient(circle,rgba(89,212,197,.18),transparent 70%);
  pointer-events:none;
}
.new-badge{
  position:absolute;
  top:14px;
  right:14px;
  padding:5px 10px;
  border-radius:999px;
  background:rgba(74,222,128,.12);
  border:1px solid rgba(74,222,128,.22);
  color:#d8ffea;
  font-size:.68rem;
  font-family:var(--mono);
}
.card-top{
  display:flex;
  align-items:flex-start;
  justify-content:space-between;
  gap:10px;
}
.svc-tag{
  display:inline-flex;
  align-items:center;
  gap:8px;
  padding:8px 10px;
  border-radius:12px;
  background:rgba(89,212,197,.12);
  color:#dffef9;
  font-size:.72rem;
  font-weight:700;
  text-transform:uppercase;
  letter-spacing:.08em;
}
.card-ts{
  color:var(--soft);
  font-size:.78rem;
  font-family:var(--mono);
  padding-top:8px;
}
.meta-row{
  display:flex;
  flex-wrap:wrap;
  gap:10px;
  margin-top:14px;
}
.meta-pill{
  display:inline-flex;
  align-items:center;
  gap:8px;
  padding:9px 12px;
  border-radius:12px;
  background:rgba(255,255,255,.04);
  border:1px solid rgba(255,255,255,.06);
  color:var(--muted);
  font-size:.8rem;
  font-family:var(--mono);
}
.otp-box{
  margin-top:16px;
  padding:15px 16px;
  border-radius:16px;
  background:rgba(4,12,23,.55);
  border:1px solid rgba(255,255,255,.08);
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:12px;
}
.otp-val{
  font-family:var(--mono);
  font-size:1.55rem;
  font-weight:600;
  letter-spacing:.16em;
  color:var(--accent-3);
}
.copy-btn{
  border:none;
  cursor:pointer;
  border-radius:12px;
  padding:11px 14px;
  color:#06111f;
  font-weight:700;
  background:linear-gradient(135deg,var(--accent-2),#ffb088);
  box-shadow:0 10px 22px rgba(255,145,102,.22);
}
.copy-btn.ok{background:linear-gradient(135deg,var(--accent),#a9fff3)}
.card-caption{
  margin-top:14px;
  color:var(--soft);
  font-size:.74rem;
  text-transform:uppercase;
  letter-spacing:.16em;
}
.card-msg{
  margin-top:8px;
  color:var(--muted);
  line-height:1.65;
  font-size:.92rem;
  display:-webkit-box;
  -webkit-box-orient:vertical;
  -webkit-line-clamp:3;
  overflow:hidden;
}
.empty{
  grid-column:1/-1;
  padding:72px 20px;
  text-align:center;
  border-radius:18px;
  border:1px dashed rgba(255,255,255,.1);
  background:rgba(255,255,255,.025);
}
.empty-icon{
  display:block;
  font-size:2.3rem;
  margin-bottom:14px;
}
.empty-text{
  font-size:1rem;
  font-weight:700;
}
.empty-hint{
  margin-top:8px;
  color:var(--muted);
  font-size:.9rem;
}
.pagination{
  display:flex;
  align-items:center;
  justify-content:center;
  gap:12px;
  margin-top:22px;
}
.pag-btn{
  width:44px;
  height:44px;
  border:none;
  border-radius:14px;
  cursor:pointer;
  color:var(--text);
  background:rgba(255,255,255,.05);
  border:1px solid rgba(255,255,255,.08);
}
.pag-btn:disabled{opacity:.35;cursor:not-allowed}
.pag-info{
  color:var(--muted);
  font-family:var(--mono);
}
footer{
  margin-top:34px;
  padding:22px 4px 0;
  color:var(--muted);
  font-size:.88rem;
}
.footer-row{
  display:flex;
  justify-content:space-between;
  gap:14px;
  flex-wrap:wrap;
  align-items:center;
}
.footer-links{
  display:flex;
  flex-wrap:wrap;
  gap:14px;
}
.footer-links a{
  text-decoration:none;
  color:var(--text);
}
.toast{
  position:fixed;
  right:22px;
  bottom:22px;
  z-index:10;
  padding:12px 16px;
  border-radius:14px;
  color:#06111f;
  font-weight:700;
  background:linear-gradient(135deg,var(--accent),#9af3e9);
  box-shadow:0 16px 36px rgba(89,212,197,.24);
  opacity:0;
  transform:translateY(14px);
  transition:opacity .22s ease, transform .22s ease;
  pointer-events:none;
}
.toast.show{
  opacity:1;
  transform:translateY(0);
}
@keyframes card-in{
  from{opacity:0;transform:translateY(10px)}
  to{opacity:1;transform:translateY(0)}
}
@keyframes pulse{
  0%{box-shadow:0 0 0 0 rgba(74,222,128,.7)}
  70%{box-shadow:0 0 0 12px rgba(74,222,128,0)}
  100%{box-shadow:0 0 0 0 rgba(74,222,128,0)}
}
@media(max-width:1120px){
  .hero,.chart-grid,.workflow-band{grid-template-columns:1fr}
  .stats-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .route-grid{grid-template-columns:1fr}
}
@media(max-width:860px){
  .controls{grid-template-columns:1fr}
  .ctrl-sel,.ctrl-btn{width:100%}
}
@media(max-width:640px){
  .shell{padding:16px 14px 42px}
  .topbar,.hero-copy,.hero-panel,.stats-card,.chart-card,.feed-card{border-radius:20px}
  .topbar{padding:16px}
  .brand-name{font-size:1rem}
  .hero-copy,.hero-panel,.stats-card,.chart-card,.feed-card{padding:20px}
  .stats-grid{grid-template-columns:1fr}
  .hero h1{max-width:none}
  .otp-grid{grid-template-columns:1fr}
  .footer-row{flex-direction:column;align-items:flex-start}
}
</style>
</head>
<body>
<main class="shell">
  <header class="topbar">
    <a class="brand" href="/">
      <div class="brand-mark">◈</div>
      <div class="brand-text">
        <span class="brand-kicker">Virtual Number Web</span>
        <span class="brand-name">Premium Live Dashboard</span>
      </div>
    </a>
    <div class="topbar-actions">
      <span class="chip live" id="livePill">Live feed online</span>
      <span class="chip" id="countPill">Waiting for data</span>
      <span class="chip warm" id="ownerPill">Owner: @NONEXPERTCODER</span>
      <button class="action-btn alt" onclick="load()">Refresh</button>
      <button class="action-btn alt" onclick="location.href='/track-request'">Track Request</button>
      <button class="action-btn" onclick="location.href='/create-my-bot'">Create My Bot</button>
      <button class="action-btn" onclick="location.href='/api/docs'">API Docs</button>
    </div>
  </header>

  <section class="feed-card" id="siteAnnouncementCard" style="display:none;margin-top:18px">
    <div class="feed-header">
      <div>
        <h2 id="siteAnnouncementTitle">Website announcement</h2>
        <p id="siteAnnouncementText">Loading website settings…</p>
      </div>
      <div class="topbar-actions">
        <button class="action-btn" onclick="location.href='/create-my-bot'">Start Create My Bot</button>
      </div>
    </div>
  </section>

  <section class="hero">
    <div class="hero-copy">
      <span class="eyebrow">Premium Route Monitor</span>
      <h1>Browse virtual number traffic in real time with a cleaner temp-number style control surface.</h1>
      <p>Watch live inbox activity, compare country and service demand, and export recent OTP traffic without leaving the dashboard.</p>
      <div class="hero-badges">
        <span class="chip live">Auto-refresh every 4s</span>
        <span class="chip" id="updatedPill">Awaiting first sync</span>
        <span class="chip warm" id="servicesPill">Services: —</span>
      </div>
    </div>
    <aside class="hero-panel">
      <div>
        <div class="panel-label">Route Snapshot</div>
      </div>
      <div class="panel-value">
        <span>Top service</span>
        <strong id="heroTop">—</strong>
      </div>
      <div class="panel-value">
        <span>Last refresh</span>
        <strong id="lastSync">—</strong>
      </div>
      <div class="panel-value">
        <span>Last hour volume</span>
        <strong id="heroHour">—</strong>
      </div>
    </aside>
  </section>

  <section class="workflow-band">
    <article class="workflow-card">
      <span class="workflow-kicker">Launch</span>
      <h3>Create a bot request with a guided approval flow</h3>
      <p>Open the full website form, choose whether you already have a panel, and send a clean approval packet to the Telegram admin bot.</p>
      <div class="workflow-meta">
        <span class="workflow-pill">Website queue</span>
        <span class="workflow-pill">Owner handoff</span>
      </div>
      <div class="workflow-actions">
        <button class="action-btn" onclick="location.href='/create-my-bot'">Create My Bot</button>
      </div>
    </article>
    <article class="workflow-card">
      <span class="workflow-kicker">Track</span>
      <h3>Check if the admin bot already received your request</h3>
      <p>Use the request ID to see when the bot queue loaded it, whether admins were notified, and whether a final review decision is ready.</p>
      <div class="workflow-meta">
        <span class="workflow-pill">Bot intake</span>
        <span class="workflow-pill">Review status</span>
      </div>
      <div class="workflow-actions">
        <button class="action-btn alt" onclick="location.href='/track-request'">Track Request</button>
      </div>
    </article>
    <article class="workflow-card">
      <span class="workflow-kicker">Integrate</span>
      <h3>Use the public API routes for feed, stats, and get-number</h3>
      <p>Browse live OTP traffic, embed the virtual number experience in your site, and keep the token-protected routes on your own backend.</p>
      <div class="workflow-meta">
        <span class="workflow-pill">Public JSON</span>
        <span class="workflow-pill">Docs ready</span>
      </div>
      <div class="workflow-actions">
        <button class="action-btn alt" onclick="location.href='/api/docs'">Open Docs</button>
      </div>
    </article>
  </section>

  <section class="feed-card route-card">
    <div class="feed-header">
      <div>
        <h2>Get number</h2>
        <p>Pick any live country from the same route pool as the bot, choose a service, then open one random public number with its OTP history below.</p>
      </div>
    </div>
    <div class="route-grid">
      <div class="route-panel">
        <div class="chart-kicker">Temp-number picker</div>
        <h3>Country first, service second</h3>
        <p>This website view assigns one live number for the selected route, then keeps its inbox visible below with refresh and change-number actions.</p>
        <label class="route-label" for="countrySel">Country</label>
        <select id="countrySel" class="ctrl-sel" onchange="onCountryChange()">
          <option value="">Loading countries…</option>
        </select>
        <label class="route-label" for="serviceSel">Service</label>
        <select id="serviceSel" class="ctrl-sel" onchange="onServiceChange()">
          <option value="">Choose a country first</option>
        </select>
        <div class="route-actions">
          <button class="ctrl-btn accent" onclick="openRoute()">Get Number</button>
          <button class="ctrl-btn" id="changeRouteBtn" onclick="changeRouteNumber()" disabled>Change Number</button>
          <button class="ctrl-btn" id="refreshRouteBtn" onclick="refreshRouteHistory()" disabled>Refresh Inbox</button>
        </div>
        <div class="feed-meta" style="margin-top:16px;margin-bottom:0">
          <span id="routeMeta">Checking live route inventory…</span>
          <span id="routeUpdated">Updated: —</span>
        </div>
      </div>
      <div class="inbox-card" id="routeInbox">
        <div class="empty route-empty">
          <span class="empty-icon">◌</span>
          <div class="empty-text">Open a public live number</div>
          <div class="empty-hint">Select a country and service, then use Get Number to load the live inbox and previous OTPs for that number.</div>
        </div>
      </div>
    </div>
  </section>

  <section class="stats-grid">
    <article class="stats-card accent">
      <div class="top">
        <div class="label">Total OTPs</div>
        <div class="icon">◎</div>
      </div>
      <div class="value" id="sTotal">—</div>
      <div class="meta" id="sDelta">Loading overall volume…</div>
    </article>
    <article class="stats-card">
      <div class="top">
        <div class="label">Today</div>
        <div class="icon">◔</div>
      </div>
      <div class="value" id="sToday">—</div>
      <div class="meta">Messages received since midnight</div>
    </article>
    <article class="stats-card gold">
      <div class="top">
        <div class="label">Top Service</div>
        <div class="icon">✦</div>
      </div>
      <div class="value" id="sTop">—</div>
      <div class="meta">Highest volume category right now</div>
    </article>
    <article class="stats-card orange">
      <div class="top">
        <div class="label">Last Hour</div>
        <div class="icon">◷</div>
      </div>
      <div class="value" id="sHour">—</div>
      <div class="meta">Calculated on the server clock</div>
    </article>
  </section>

  <div class="section-head">
    <div class="section-title">Analytics</div>
    <div class="section-note">24-hour OTP flow and current service mix</div>
  </div>
  <section class="chart-grid">
    <article class="chart-card">
      <div class="chart-kicker">Volume</div>
      <h3>OTP traffic over the last 24 hours</h3>
      <p>Watch spikes as they happen and compare hourly movement across the most recent day of activity.</p>
      <canvas id="lineChart"></canvas>
    </article>
    <article class="chart-card">
      <div class="chart-kicker">Breakdown</div>
      <h3>Service distribution</h3>
      <p>See which services currently dominate the feed across the latest OTP traffic captured by the API.</p>
      <canvas id="donutChart"></canvas>
    </article>
  </section>

  <section class="feed-card">
    <div class="feed-header">
      <div>
        <h2>Live virtual inbox feed</h2>
        <p>Filter by service, search by number or code, and export recent records from your public virtual number wall.</p>
      </div>
    </div>

    <div class="controls">
      <div class="search-wrap">
        <span class="search-icon">⌕</span>
        <input id="q" placeholder="Search number, OTP, service, or country" oninput="render()">
      </div>
      <select id="sf" class="ctrl-sel" onchange="render()">
        <option value="">All services</option>
      </select>
      <button class="ctrl-btn" onclick="load()">Refresh feed</button>
      <button class="ctrl-btn accent" onclick="exportCSV()">Export CSV</button>
    </div>

    <div class="feed-meta">
      <span id="feedSummary">Preparing live feed…</span>
      <span id="feedTimestamp">Updated: —</span>
    </div>

    <div class="otp-grid" id="grid">
      <div class="empty">
        <span class="empty-icon">◌</span>
        <div class="empty-text">Connecting to the live feed</div>
        <div class="empty-hint">Recent OTP records will appear here once the API responds.</div>
      </div>
    </div>

    <div class="pagination" id="pagBox" style="display:none">
      <button class="pag-btn" id="prevBtn" onclick="prevPage()">←</button>
      <span class="pag-info" id="pagInfo">1 / 1</span>
      <button class="pag-btn" id="nextBtn" onclick="nextPage()">→</button>
    </div>
  </section>

  <footer>
    <div class="footer-row">
      <div>Virtual Number Web dashboard for public OTP traffic visibility.</div>
      <div class="footer-links">
        <a href="/api/docs">API Docs</a>
        <a href="/track-request">Track Request</a>
        <a href="/create-my-bot">Create My Bot</a>
        <a href="/health">Health</a>
        <a href="https://t.me/crackotp" target="_blank">Telegram Channel</a>
        <a href="https://t.me/NONEXPERTCODER" target="_blank">@NONEXPERTCODER</a>
      </div>
    </div>
  </footer>
</main>

<div class="toast" id="toast"></div>

<script>
let all=[],seen=new Set(),page=1,PER=12;
let routeState={countries:[],services:[],current:null};
let lineChart=null,donutChart=null;
let siteSettings=null;

window.onload=()=>{
  initCharts();
  loadSiteSettings();
  load();
  loadStats();
  loadCountries();
  setInterval(load,4000);
  setInterval(loadStats,30000);
  setInterval(()=>refreshRouteHistory(true),5000);
};

async function loadSiteSettings(){
  try{
    const r=await fetch('/api/public/site-settings');
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d=await r.json();
    if(d.status!=='success') throw new Error(d.message||'Unable to load website settings');
    siteSettings=d;
    const ownerText=d.contact_telegram || d.contact_whatsapp || '—';
    document.getElementById('ownerPill').textContent=`Owner: ${ownerText}`;
    const announcement=(d.announcement||'').trim();
    const card=document.getElementById('siteAnnouncementCard');
    if(announcement){
      card.style.display='block';
      document.getElementById('siteAnnouncementTitle').textContent='Website announcement';
      document.getElementById('siteAnnouncementText').textContent=announcement;
    }else{
      card.style.display='none';
    }
  }catch(e){
    console.error('loadSiteSettings error:',e);
  }
}

async function loadCountries(){
  try{
    const r=await fetch('/api/public/get-number/countries');
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d=await r.json();
    if(d.status!=='success') throw new Error(d.message||'Unable to load countries');
    routeState.countries=d.data||[];
    const sel=document.getElementById('countrySel');
    if(!routeState.countries.length){
      sel.innerHTML='<option value="">No live countries</option>';
      document.getElementById('serviceSel').innerHTML='<option value="">No services</option>';
      document.getElementById('routeMeta').textContent='No live countries available right now';
      renderRoutePlaceholder('No live public routes', 'Upload available numbers in the bot first, then refresh this page.');
      return;
    }
    sel.innerHTML=routeState.countries.map((item,idx)=>`<option value="${e(item.country)}"${idx===0?' selected':''}>${e(item.country)} (${item.numbers})</option>`).join('');
    document.getElementById('routeMeta').textContent=`${routeState.countries.length} live countries ready`;
    await loadServices(sel.value);
  }catch(e){
    console.error('loadCountries error:',e);
    document.getElementById('routeMeta').textContent='Country list unavailable';
    renderRoutePlaceholder('Unable to load live routes', e.message);
  }
}

async function loadServices(country){
  const serviceSel=document.getElementById('serviceSel');
  if(!country){
    serviceSel.innerHTML='<option value="">Choose a country first</option>';
    routeState.services=[];
    return;
  }
  try{
    const url=`/api/public/get-number/services?country=${encodeURIComponent(country)}`;
    const r=await fetch(url);
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d=await r.json();
    if(d.status!=='success') throw new Error(d.message||'Unable to load services');
    routeState.services=d.data||[];
    if(!routeState.services.length){
      serviceSel.innerHTML='<option value="">No live services</option>';
      document.getElementById('routeMeta').textContent=`No live services for ${country}`;
      renderRoutePlaceholder('No services in this country', 'Pick another country or upload route stock for this one.');
      updateRouteButtons(false);
      return;
    }
    serviceSel.innerHTML=routeState.services.map((item,idx)=>`<option value="${e(item.service)}"${idx===0?' selected':''}>${e(item.service)} (${item.numbers})</option>`).join('');
    document.getElementById('routeMeta').textContent=`${routeState.services.length} services live in ${country}`;
  }catch(e){
    console.error('loadServices error:',e);
    serviceSel.innerHTML='<option value="">Unable to load services</option>';
    document.getElementById('routeMeta').textContent='Service list unavailable';
    renderRoutePlaceholder('Unable to load services', e.message);
  }
}

async function onCountryChange(){
  routeState.current=null;
  updateRouteButtons(false);
  renderRoutePlaceholder('Pick a service next', 'Choose a service for the selected country, then open a public number.');
  await loadServices(document.getElementById('countrySel').value);
}

function onServiceChange(){
  routeState.current=null;
  updateRouteButtons(false);
  renderRoutePlaceholder('Ready to open a live inbox', 'Use Get Number to load one random public number for this route.');
}

async function openRoute(exclude=''){
  const country=document.getElementById('countrySel').value;
  const service=document.getElementById('serviceSel').value;
  if(!country||!service){
    toast('Choose a country and service first');
    return;
  }
  try{
    const r=await fetch('/api/public/assign',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        country,
        service,
        previous_number: exclude || routeState.current?.number || null
      })
    });
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d=await r.json();
    if(d.status!=='success' || !d.number) throw new Error(d.message||'No live number available');
    routeState.current={
      number:d.number,
      service:d.service,
      country:d.country,
      flag:d.flag,
      history:d.history||[],
      history_count:d.history_count||0,
      refreshed_at:d.assigned_at,
      expires_in:d.expires_in
    };
    renderRouteInbox(routeState.current);
    toast('Live number loaded');
  }catch(e){
    console.error('openRoute error:',e);
    renderRoutePlaceholder('No live number available', e.message);
    updateRouteButtons(false);
    toast('Route is currently empty');
  }
}

function changeRouteNumber(){
  if(routeState.current?.number){
    openRoute(routeState.current.number);
    return;
  }
  openRoute();
}

async function refreshRouteHistory(silent=false){
  if(!routeState.current?.number) return;
  try{
    const r=await fetch(`/api/public/get-number/history?number=${encodeURIComponent(routeState.current.number)}`);
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d=await r.json();
    if(d.status!=='success' || !d.data) throw new Error(d.message||'Unable to refresh inbox');
    routeState.current={...routeState.current,...d.data};
    renderRouteInbox(routeState.current);
    if(!silent) toast('Inbox refreshed');
  }catch(e){
    console.error('refreshRouteHistory error:',e);
    if(!silent) toast('Unable to refresh inbox');
  }
}

function updateRouteButtons(enabled){
  document.getElementById('changeRouteBtn').disabled=!enabled;
  document.getElementById('refreshRouteBtn').disabled=!enabled;
}

function renderRoutePlaceholder(title,hint){
  document.getElementById('routeInbox').innerHTML=`<div class="empty route-empty">
    <span class="empty-icon">◌</span>
    <div class="empty-text">${e(title)}</div>
    <div class="empty-hint">${e(hint)}</div>
  </div>`;
}

function renderRouteInbox(data){
  const history=(data.history||[]);
  document.getElementById('routeUpdated').textContent=`Updated: ${fmtClock(new Date())}`;
  document.getElementById('routeMeta').textContent=`Assigned public number in ${data.country} • ${data.service}`;
  document.getElementById('routeInbox').innerHTML=`<div class="chart-kicker">Live public inbox</div>
    <h3>${e(data.service)} • ${e(data.country)}</h3>
    <div class="inbox-number">${e(data.number)}</div>
    <div class="inbox-sub">Random route assigned from the public web pool. Use refresh to load newly received OTPs on this number.</div>
    <div class="meta-row">
      <span class="meta-pill">Expires ${e(String(data.expires_in || 3600))}s</span>
      <span class="meta-pill">History ${e(String(history.length))}</span>
      <span class="meta-pill">Updated ${e((data.refreshed_at||'').slice(11) || '—')}</span>
    </div>
    ${history.length ? `<div class="history-list">${history.map(row=>`<div class="history-item">
      <div class="history-head">
        <span class="svc-tag">${e(data.service)}</span>
        <span class="card-ts">${e((row.received_at||'').slice(11) || '—')}</span>
      </div>
      <div class="card-msg">${e(row.message)}</div>
      <div class="history-actions">
        <span class="history-otp">${fmtOtp(row.otp)}</span>
        <button class="copy-btn" onclick="cpOtp('${js(row.otp)}',this)">Copy OTP</button>
      </div>
    </div>`).join('')}</div>` : `<div class="empty route-empty">
      <span class="empty-icon">◌</span>
      <div class="empty-text">No OTP history yet</div>
      <div class="empty-hint">This number is live, but no OTP has been recorded for it yet.</div>
    </div>`}`;
  updateRouteButtons(true);
}

async function load(){
  try{
    const r=await fetch('/api/public/otps?limit=1000');
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d=await r.json();
    if(d.status!=='success') throw new Error(d.message||'Unknown API response');

    const data=d.data||[];
    const newKeys=new Set(data.filter(o=>!seen.has(o.number+o.received_at)).map(o=>o.number+o.received_at));
    data.forEach(o=>seen.add(o.number+o.received_at));
    all=data;
    if(newKeys.size>0) page=1;

    document.getElementById('countPill').textContent=`${all.length} OTPs loaded`;
    document.getElementById('updatedPill').textContent=`Feed sync ${fmtClock(new Date())}`;
    document.getElementById('feedTimestamp').textContent=`Updated: ${fmtClock(new Date())}`;
    buildFilter();
    render(newKeys);
  }catch(e){
    console.error('load error:',e);
    showErr('Unable to reach the OTP feed','Make sure api_server.py is running and returning /api/public/otps. '+e.message);
  }
}

async function loadStats(){
  try{
    const r=await fetch('/api/public/stats');
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d=await r.json();
    if(d.status!=='success') throw new Error(d.message||'Unknown stats response');

    const top=Object.entries(d.by_service||{}).sort((a,b)=>b[1]-a[1])[0];
    const topLabel=top?clip(top[0],14):'—';
    const serviceCount=Object.keys(d.by_service||{}).length;

    animNum('sTotal',d.total_otps||0);
    animNum('sToday',d.otps_today||0);
    animNum('sHour',d.last_hour_otps||0);
    document.getElementById('sTop').textContent=topLabel;
    document.getElementById('heroTop').textContent=top?top[0]:'—';
    document.getElementById('heroHour').textContent=(d.last_hour_otps||0).toLocaleString();
    document.getElementById('lastSync').textContent=d.generated_at||fmtDateTime(new Date());
    document.getElementById('servicesPill').textContent=`Services: ${serviceCount}`;
    document.getElementById('sDelta').textContent=`${(d.otps_today||0).toLocaleString()} received today`;

    updateCharts(d);
  }catch(e){
    console.error('stats error:',e);
  }
}

function render(newKeys=new Set()){
  const q=(document.getElementById('q').value||'').toLowerCase();
  const sf=document.getElementById('sf').value;
  const filtered=all.filter(o=>{
    if(sf&&o.service!==sf)return false;
    return !q || `${o.number}${o.otp}${o.service}${o.country}${o.message}`.toLowerCase().includes(q);
  });

  document.getElementById('feedSummary').textContent=`Showing ${filtered.length.toLocaleString()} of ${all.length.toLocaleString()} recent OTPs`;

  const grid=document.getElementById('grid');
  if(!filtered.length){
    grid.innerHTML=`<div class="empty">
      <span class="empty-icon">◌</span>
      <div class="empty-text">No OTPs match your filter</div>
      <div class="empty-hint">Try clearing the search or switching the selected service.</div>
    </div>`;
    document.getElementById('pagBox').style.display='none';
    return;
  }

  const totalPages=Math.ceil(filtered.length/PER);
  if(page>totalPages) page=totalPages;
  const slice=filtered.slice((page-1)*PER,page*PER);

  grid.innerHTML=slice.map((o,i)=>{
    const key=o.number+o.received_at;
    const fresh=newKeys.has(key);
    return `<article class="otp-card" style="animation-delay:${i*.03}s">
      ${fresh?'<span class="new-badge">New</span>':''}
      <div class="card-top">
        <span class="svc-tag">${e(o.service)}</span>
        <span class="card-ts">${e((o.received_at||'').slice(11) || '—')}</span>
      </div>
      <div class="meta-row">
        <span class="meta-pill"># ${e(o.number)}</span>
        <span class="meta-pill">${e(o.country)}</span>
      </div>
      <div class="otp-box">
        <span class="otp-val">${fmtOtp(o.otp)}</span>
        <button class="copy-btn" onclick="cpOtp('${js(o.otp)}',this)">Copy</button>
      </div>
      <div class="card-caption">Message preview</div>
      <div class="card-msg">${e(o.message)}</div>
    </article>`;
  }).join('');

  if(totalPages>1){
    document.getElementById('pagBox').style.display='flex';
    document.getElementById('pagInfo').textContent=`${page} / ${totalPages}`;
    document.getElementById('prevBtn').disabled=page===1;
    document.getElementById('nextBtn').disabled=page===totalPages;
  }else{
    document.getElementById('pagBox').style.display='none';
  }
}

function buildFilter(){
  const sel=document.getElementById('sf');
  const cur=sel.value;
  const svcs=[...new Set(all.map(o=>o.service).filter(Boolean))].sort((a,b)=>a.localeCompare(b));
  sel.innerHTML='<option value="">All services</option>'+svcs.map(s=>`<option value="${e(s)}"${s===cur?' selected':''}>${e(s)}</option>`).join('');
}

function initCharts(){
  Chart.defaults.color='#8ba6c3';
  Chart.defaults.font.family="'IBM Plex Mono'";
  lineChart=new Chart(document.getElementById('lineChart'),{
    type:'line',
    data:{labels:[],datasets:[{
      label:'OTPs',
      data:[],
      borderColor:'#59d4c5',
      backgroundColor:'rgba(89,212,197,.14)',
      fill:true,
      tension:.38,
      pointRadius:3,
      pointHoverRadius:6,
      pointBackgroundColor:'#59d4c5',
      borderWidth:2.2
    }]},
    options:{
      maintainAspectRatio:true,
      plugins:{legend:{display:false}},
      scales:{
        x:{grid:{color:'rgba(255,255,255,.05)'},ticks:{maxTicksLimit:8}},
        y:{grid:{color:'rgba(255,255,255,.05)'},beginAtZero:true,ticks:{precision:0}}
      }
    }
  });

  donutChart=new Chart(document.getElementById('donutChart'),{
    type:'doughnut',
    data:{labels:[],datasets:[{
      data:[],
      backgroundColor:['#59d4c5','#ff9166','#ffe08a','#8ad4ff','#7ae7b3','#f6a5d8','#b6c3ff']
    }]},
    options:{
      maintainAspectRatio:true,
      cutout:'68%',
      plugins:{
        legend:{
          position:'bottom',
          labels:{boxWidth:12,padding:16,color:'#8ba6c3'}
        }
      }
    }
  });
}

function updateCharts(d){
  if(!lineChart||!donutChart) return;

  const hourly=d.hourly_last_24h||{};
  const hourKeys=Object.keys(hourly).sort().slice(-24);
  lineChart.data.labels=hourKeys.map(k=>k.slice(11,16));
  lineChart.data.datasets[0].data=hourKeys.map(k=>hourly[k]);
  lineChart.update('none');

  const top=Object.entries(d.by_service||{}).sort((a,b)=>b[1]-a[1]).slice(0,7);
  donutChart.data.labels=top.map(x=>clip(x[0],16));
  donutChart.data.datasets[0].data=top.map(x=>x[1]);
  donutChart.update('none');
}

function cpOtp(otp,btn){
  if(!otp||otp==='—'){
    toast('No OTP available to copy');
    return;
  }
  if(!navigator.clipboard){
    toast('Clipboard not supported in this browser');
    return;
  }
  navigator.clipboard.writeText(String(otp).replace(/-/g,'')).then(()=>{
    const prev=btn.textContent;
    btn.textContent='Copied';
    btn.classList.add('ok');
    toast('OTP copied');
    setTimeout(()=>{
      btn.textContent=prev;
      btn.classList.remove('ok');
    },1800);
  }).catch(()=>toast('Copy failed'));
}

function exportCSV(){
  if(!all.length){
    toast('No OTPs to export');
    return;
  }
  const rows=[['Service','Number','Country','OTP','Message','Received At'],
    ...all.slice(0,500).map(o=>[o.service,o.number,o.country,o.otp,o.message,o.received_at])];
  const csv=rows.map(r=>r.map(v=>`"${String(v??'').replace(/"/g,'""')}"`).join(',')).join('\n');
  const a=Object.assign(document.createElement('a'),{
    href:URL.createObjectURL(new Blob([csv],{type:'text/csv'})),
    download:`crack-sms-otps-${new Date().toISOString().slice(0,10)}.csv`
  });
  a.click();
  toast(`Exported ${Math.min(all.length,500)} OTPs`);
}

function prevPage(){if(page>1){page--;render()}}
function nextPage(){page++;render()}

function showErr(title,hint){
  document.getElementById('grid').innerHTML=`<div class="empty">
    <span class="empty-icon">◌</span>
    <div class="empty-text">${e(title)}</div>
    <div class="empty-hint">${e(hint)}</div>
  </div>`;
  document.getElementById('pagBox').style.display='none';
  document.getElementById('feedSummary').textContent='Feed unavailable';
}

function animNum(id,target){
  const el=document.getElementById(id);
  if(!el) return;
  const current=Number(String(el.textContent).replace(/,/g,''))||0;
  if(current===target){
    el.textContent=Number(target).toLocaleString();
    return;
  }
  const duration=420,steps=24,inc=(target-current)/steps;
  let i=0,val=current;
  const timer=setInterval(()=>{
    i++;
    val=(i>=steps)?target:val+inc;
    el.textContent=Math.round(val).toLocaleString();
    if(i>=steps) clearInterval(timer);
  },duration/steps);
}

function toast(message){
  const t=document.getElementById('toast');
  t.textContent=message;
  t.classList.add('show');
  clearTimeout(window.__toastTimer);
  window.__toastTimer=setTimeout(()=>t.classList.remove('show'),2400);
}

function fmtOtp(v){
  const s=String(v||'—');
  if(s==='—') return s;
  return s.length===6 ? `${s.slice(0,3)}-${s.slice(3)}` : s;
}
function fmtClock(d){
  return new Intl.DateTimeFormat(undefined,{hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(d);
}
function fmtDateTime(v){
  const d=new Date(String(v).replace(' ','T'));
  return isNaN(d.getTime()) ? String(v||'—') : new Intl.DateTimeFormat(undefined,{
    year:'numeric',month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'
  }).format(d);
}
function clip(s,n){
  s=String(s||'');
  return s.length>n ? s.slice(0,n-1)+'…' : s;
}
function e(s){
  const d=document.createElement('div');
  d.textContent=String(s??'');
  return d.innerHTML;
}
function js(s){
  return String(s??'').replace(/\\/g,'\\\\').replace(/'/g,"\\'");
}
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
#  API DOCS PAGE
# ══════════════════════════════════════════════════════════════════════════════

_DOCS = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Virtual Number Web API Docs</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Space+Grotesk:wght@400;500;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#07111f;
  --panel:#0d1a2d;
  --panel-2:#12243d;
  --line:#1e3655;
  --text:#edf7ff;
  --muted:#8ca9c7;
  --soft:#5f7b99;
  --accent:#5dd8ca;
  --accent-2:#ff956c;
  --accent-3:#ffe08a;
  --ok:#61e4a2;
  --warn:#ffd166;
  --mono:'IBM Plex Mono',monospace;
  --font:'Space Grotesk',sans-serif;
  --shadow:0 22px 60px rgba(0,0,0,.34);
}
body{
  font-family:var(--font);
  background:
    radial-gradient(circle at 14% 16%,rgba(93,216,202,.14),transparent 24%),
    radial-gradient(circle at 84% 10%,rgba(255,149,108,.14),transparent 20%),
    linear-gradient(180deg,#07111f 0%,#081526 100%);
  color:var(--text);
  min-height:100vh;
  line-height:1.7;
}
body::before{
  content:'';
  position:fixed;
  inset:0;
  pointer-events:none;
  background-image:
    linear-gradient(rgba(255,255,255,.022) 1px,transparent 1px),
    linear-gradient(90deg,rgba(255,255,255,.022) 1px,transparent 1px);
  background-size:46px 46px;
  mask-image:linear-gradient(180deg,rgba(255,255,255,.7),transparent 92%);
}
.wrap{
  position:relative;
  z-index:1;
  max-width:1180px;
  margin:0 auto;
  padding:28px 20px 56px;
}
.hero,
.card,
.step,
.endpoint,
.example,
.note{
  border:1px solid rgba(255,255,255,.08);
  background:linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,.025)),rgba(13,26,45,.88);
  border-radius:24px;
  box-shadow:var(--shadow);
}
.hero{
  display:grid;
  grid-template-columns:1.35fr .95fr;
  gap:18px;
  padding:28px;
}
.hero-copy h1{
  font-size:clamp(2.2rem,4vw,4rem);
  line-height:.95;
  letter-spacing:-.05em;
  max-width:10ch;
}
.hero-copy p{
  margin-top:16px;
  max-width:60ch;
  color:var(--muted);
  font-size:1rem;
}
.kicker{
  display:inline-flex;
  align-items:center;
  gap:8px;
  padding:8px 12px;
  border-radius:999px;
  background:rgba(255,255,255,.04);
  border:1px solid rgba(255,255,255,.08);
  color:var(--muted);
  font-size:.76rem;
  text-transform:uppercase;
  letter-spacing:.18em;
  margin-bottom:16px;
}
.kicker::before{
  content:'';
  width:8px;height:8px;border-radius:50%;
  background:var(--accent);
}
.hero-links{
  display:flex;
  flex-wrap:wrap;
  gap:10px;
  margin-top:22px;
}
.pill{
  display:inline-flex;
  align-items:center;
  gap:8px;
  padding:10px 14px;
  border-radius:999px;
  background:rgba(255,255,255,.04);
  border:1px solid rgba(255,255,255,.08);
  color:var(--muted);
  font-size:.82rem;
  text-decoration:none;
}
.pill strong{color:var(--text)}
.hero-side{
  display:grid;
  gap:14px;
}
.mini{
  padding:18px;
  border-radius:18px;
  background:rgba(255,255,255,.035);
  border:1px solid rgba(255,255,255,.06);
}
.mini .label{
  color:var(--soft);
  font-size:.72rem;
  text-transform:uppercase;
  letter-spacing:.18em;
}
.mini strong{
  display:block;
  margin-top:8px;
  font-size:1rem;
}
.grid{
  display:grid;
  gap:16px;
}
.grid.three{
  grid-template-columns:repeat(3,minmax(0,1fr));
  margin-top:22px;
}
.grid.two{
  grid-template-columns:repeat(2,minmax(0,1fr));
}
.step{
  padding:20px;
}
.step-num{
  font-family:var(--mono);
  color:var(--accent);
  font-size:.8rem;
  letter-spacing:.16em;
  text-transform:uppercase;
}
.step h3{
  margin-top:10px;
  font-size:1.08rem;
}
.step p{
  margin-top:8px;
  color:var(--muted);
  font-size:.92rem;
}
.sec-title{
  display:flex;
  align-items:center;
  gap:12px;
  margin:34px 0 16px;
  color:var(--soft);
  font-size:.8rem;
  font-weight:700;
  text-transform:uppercase;
  letter-spacing:.2em;
}
.sec-title::after{
  content:'';
  flex:1;
  height:1px;
  background:linear-gradient(90deg,rgba(255,255,255,.12),transparent);
}
.card{
  padding:22px;
}
.card p{
  color:var(--muted);
  font-size:.94rem;
}
.endpoint{
  padding:22px;
  position:relative;
  overflow:hidden;
}
.endpoint::before{
  content:'';
  position:absolute;
  inset:0 auto auto 0;
  width:100%;
  height:3px;
  background:linear-gradient(90deg,var(--line-color,#5dd8ca),transparent);
}
.endpoint-top{
  display:flex;
  align-items:center;
  gap:10px;
  flex-wrap:wrap;
  margin-bottom:10px;
}
.badge{
  display:inline-flex;
  align-items:center;
  gap:6px;
  padding:5px 10px;
  border-radius:999px;
  font-size:.72rem;
  font-weight:700;
  text-transform:uppercase;
  letter-spacing:.12em;
}
.get{background:rgba(97,228,162,.14);color:#dfffee;border:1px solid rgba(97,228,162,.22)}
.post{background:rgba(93,216,202,.12);color:#dffef9;border:1px solid rgba(93,216,202,.22)}
.del{background:rgba(255,149,108,.13);color:#ffe6dd;border:1px solid rgba(255,149,108,.24)}
.pub{background:rgba(138,212,255,.12);color:#dff5ff;border:1px solid rgba(138,212,255,.2)}
.auth{background:rgba(255,209,102,.12);color:#fff0c5;border:1px solid rgba(255,209,102,.22)}
.endpoint h3{
  font-size:1.12rem;
}
.meta{
  margin:12px 0 14px;
  color:var(--muted);
  font-size:.9rem;
}
.list{
  display:grid;
  gap:8px;
  margin:14px 0;
}
.list div{
  color:var(--muted);
  font-size:.88rem;
}
.list strong{color:var(--text)}
pre{
  margin-top:14px;
  padding:16px 18px;
  border-radius:18px;
  background:#091321;
  border:1px solid rgba(255,255,255,.07);
  color:#b8fff3;
  font-size:.78rem;
  line-height:1.65;
  white-space:pre-wrap;
  word-break:break-word;
  overflow:auto;
  font-family:var(--mono);
}
code{
  font-family:var(--mono);
  color:#dffef9;
}
.example{
  padding:22px;
}
.example h3{
  font-size:1.08rem;
  margin-bottom:8px;
}
.example p{
  color:var(--muted);
  font-size:.9rem;
}
.note{
  margin-top:22px;
  padding:22px;
}
.note h3{
  font-size:1rem;
  margin-bottom:10px;
}
.note ul{
  margin-left:18px;
  color:var(--muted);
}
.note li+li{margin-top:8px}
a{color:inherit}
.footer{
  margin-top:26px;
  color:var(--muted);
  font-size:.9rem;
}
.footer a{
  color:var(--text);
  text-decoration:none;
}
@media(max-width:980px){
  .hero,.grid.three,.grid.two{grid-template-columns:1fr}
}
@media(max-width:640px){
  .wrap{padding:18px 14px 42px}
  .hero,.card,.step,.endpoint,.example,.note{border-radius:20px}
  .hero{padding:20px}
}
</style>
</head>
<body>
<div class="wrap">
  <section class="hero">
    <div class="hero-copy">
      <span class="kicker">Virtual Number Web API</span>
      <h1>Integrate the virtual number feed cleanly and safely.</h1>
      <p>Use the public endpoints for open dashboards, temp-number style landing pages, live widgets, and a public get-number experience. Keep token-protected endpoints on your server for production integrations. The examples below match the current live payloads, including <code>last_hour_otps</code>, <code>generated_at</code>, and the website-side get-number routes.</p>
      <div class="hero-links">
        <a class="pill" href="/"><strong>Open</strong> Live Dashboard</a>
        <a class="pill" href="/create-my-bot"><strong>Open</strong> Create My Bot</a>
        <a class="pill" href="/track-request"><strong>Open</strong> Track Request</a>
        <a class="pill" href="/health"><strong>Check</strong> Health</a>
        <span class="pill"><strong>Base URL</strong> https://tempnum.net</span>
      </div>
    </div>
    <div class="hero-side">
      <div class="mini">
        <div class="label">Best for browser use</div>
        <strong>/api/public/otps, /api/public/stats, /api/public/site-settings, and /api/public/get-number/*</strong>
      </div>
      <div class="mini">
        <div class="label">Best for private integrations</div>
        <strong>/api/sms and /api/stats with your token kept server-side</strong>
      </div>
      <div class="mini">
        <div class="label">Payload highlights</div>
        <strong>service, country, otp, message, received_at, last_hour_otps, generated_at, history</strong>
      </div>
    </div>
  </section>

  <div class="grid three">
    <article class="step">
      <div class="step-num">Step 01</div>
      <h3>Pick the right endpoint</h3>
      <p>Public endpoints are ideal for widgets and landing pages. Authenticated endpoints are better when you need panel-filtered data or private dashboards.</p>
    </article>
    <article class="step">
      <div class="step-num">Step 02</div>
      <h3>Normalize upstream data</h3>
      <p>Map each record to the fields your app uses most often: <code>number</code>, <code>service</code>, <code>country</code>, <code>otp</code>, and <code>received_at</code>.</p>
    </article>
    <article class="step">
      <div class="step-num">Step 03</div>
      <h3>Keep secrets on your server</h3>
      <p>Never expose private API tokens in frontend JavaScript. Use your own backend route as a small proxy if you need authenticated access.</p>
    </article>
  </div>

  <div class="sec-title">Public Endpoints</div>

  <article class="endpoint" style="--line-color:#61e4a2">
    <div class="endpoint-top">
      <span class="badge get">GET</span>
      <span class="badge pub">Public</span>
      <h3>/api/public/otps</h3>
    </div>
    <p class="meta">Returns recent OTP records with no token required.</p>
    <div class="list">
      <div><strong>Query params:</strong> <code>limit</code> from 1 to 1000, default recommended 50 to 200 for UI use.</div>
      <div><strong>Primary fields:</strong> <code>number</code>, <code>service</code>, <code>country</code>, <code>otp</code>, <code>message</code>, <code>received_at</code>.</div>
      <div><strong>Use case:</strong> public dashboards, demo widgets, ticker feeds, and analytics previews.</div>
    </div>
<pre>GET /api/public/otps?limit=3

{
  "status": "success",
  "total_records": 3,
  "data": [
    {
      "number": "+923001234567",
      "service": "WhatsApp",
      "country": "Pakistan",
      "otp": "847293",
      "message": "Your WhatsApp verification code is: 847293",
      "received_at": "2026-04-23 18:35:22"
    }
  ]
}</pre>
  </article>

  <article class="endpoint" style="--line-color:#8ad4ff">
    <div class="endpoint-top">
      <span class="badge get">GET</span>
      <span class="badge pub">Public</span>
      <h3>/api/public/stats</h3>
    </div>
    <p class="meta">Returns overall traffic statistics generated on the server clock.</p>
    <div class="list">
      <div><strong>Key fields:</strong> <code>total_otps</code>, <code>otps_today</code>, <code>last_hour_otps</code>, <code>by_service</code>, <code>hourly_last_24h</code>, <code>generated_at</code>.</div>
      <div><strong>Use case:</strong> metric cards, service breakdown charts, and operational summary widgets.</div>
    </div>
<pre>GET /api/public/stats

{
  "status": "success",
  "total_otps": 1420,
  "otps_today": 84,
  "last_hour_otps": 11,
  "generated_at": "2026-04-23 18:42:10",
  "by_service": {
    "WhatsApp": 44,
    "Telegram": 21,
    "Instagram": 8
  },
  "hourly_last_24h": {
    "2026-04-23 17:00": 9,
    "2026-04-23 18:00": 11
  }
}</pre>
  </article>

  <article class="endpoint" style="--line-color:#5dd8ca">
    <div class="endpoint-top">
      <span class="badge get">GET</span>
      <span class="badge pub">Public</span>
      <h3>/api/public/site-settings</h3>
    </div>
    <p class="meta">Returns the website announcement, owner contact targets, and the approval note used by the Create My Bot flow.</p>
    <div class="list">
      <div><strong>Primary fields:</strong> <code>announcement</code>, <code>status_note</code>, <code>contact_whatsapp</code>, <code>contact_telegram</code>.</div>
      <div><strong>Use case:</strong> populate website banners, contact cards, and Create My Bot owner handoff screens.</div>
    </div>
<pre>GET /api/public/site-settings

{
  "status": "success",
  "announcement": "Owner approvals are handled manually...",
  "status_note": "Website requests are mirrored into the admin bot queue.",
  "contact_whatsapp": "+923000767749",
  "contact_telegram": "@NONEXPERTCODER",
  "pending_requests": 3
}</pre>
  </article>

  <article class="endpoint" style="--line-color:#5dd8ca">
    <div class="endpoint-top">
      <span class="badge get">GET</span>
      <span class="badge pub">Public</span>
      <h3>/api/public/get-number/countries</h3>
    </div>
    <p class="meta">Lists the countries that currently have live public numbers available on the website flow.</p>
    <div class="list">
      <div><strong>Use case:</strong> country dropdowns, route catalogs, and temp-number homepages.</div>
      <div><strong>Fields:</strong> <code>country</code>, <code>flag</code>, <code>numbers</code>, <code>services</code>.</div>
    </div>
<pre>GET /api/public/get-number/countries

{
  "status": "success",
  "total_countries": 2,
  "data": [
    {
      "country": "Pakistan",
      "flag": "🇵🇰",
      "numbers": 42,
      "services": 6
    }
  ]
}</pre>
  </article>

  <article class="endpoint" style="--line-color:#ff956c">
    <div class="endpoint-top">
      <span class="badge get">GET</span>
      <span class="badge pub">Public</span>
      <h3>/api/public/get-number/services</h3>
    </div>
    <p class="meta">Returns the currently live services for one selected country.</p>
    <div class="list">
      <div><strong>Required param:</strong> <code>country</code></div>
      <div><strong>Fields:</strong> <code>service</code>, <code>numbers</code></div>
    </div>
<pre>GET /api/public/get-number/services?country=Pakistan

{
  "status": "success",
  "country": "Pakistan",
  "total_services": 3,
  "data": [
    {"service": "WhatsApp", "numbers": 20},
    {"service": "Telegram", "numbers": 12}
  ]
}</pre>
  </article>

  <article class="endpoint" style="--line-color:#ffe08a">
    <div class="endpoint-top">
      <span class="badge post">POST</span>
      <span class="badge pub">Public</span>
      <h3>/api/public/assign</h3>
    </div>
    <p class="meta">Assigns one random live number for the selected country and service, then returns the number with its current inbox history.</p>
    <div class="list">
      <div><strong>Request body:</strong> <code>{"service":"WhatsApp","country":"Pakistan"}</code></div>
      <div><strong>Optional field:</strong> <code>previous_number</code> when the client wants to change the current number cleanly.</div>
    </div>
<pre>POST /api/public/assign
Content-Type: application/json

{
  "service": "WhatsApp",
  "country": "Pakistan"
}

{
  "status": "success",
  "number": "+923001234567",
  "service": "WhatsApp",
  "country": "Pakistan",
  "expires_in": 3600,
  "history_count": 2
}</pre>
  </article>

  <article class="endpoint" style="--line-color:#ff956c">
    <div class="endpoint-top">
      <span class="badge post">POST</span>
      <span class="badge pub">Public</span>
      <h3>/api/public/bot-request</h3>
    </div>
    <p class="meta">Queues a Create My Bot request, generates the request ID, and returns the owner contact handoff payload for WhatsApp or Telegram.</p>
    <div class="list">
      <div><strong>Required fields:</strong> <code>user_name</code>, <code>bot_name</code>, <code>bot_username</code>, <code>token</code>, <code>admin_id</code>, <code>group_id</code>, <code>contact_method</code>.</div>
      <div><strong>Response highlights:</strong> <code>request_id</code>, <code>contact_target</code>, <code>contact_url</code>, <code>prefilled_message</code>.</div>
    </div>
<pre>POST /api/public/bot-request
Content-Type: application/json

{
  "user_name": "Muhammad Adnan",
  "bot_name": "Crack OTP Pro",
  "bot_username": "CrackOtpProBot",
  "token": "1234567890:AAEXAMPLE_TOKEN",
  "admin_id": "123456789",
  "channel": "https://t.me/crackotp",
  "otp_group": "https://t.me/crackotpgroup",
  "number_bot": "https://t.me/CrackSMSReBot",
  "support": "@ownersigma",
  "group_id": "-1001234567890",
  "contact_method": "whatsapp"
}

{
  "status": "success",
  "request_id": "REQ483921",
  "contact_method": "whatsapp",
  "contact_target": "+923000767749",
  "contact_url": "https://wa.me/923000767749?text=...",
  "queued_for_admin_bot": true,
  "track_url": "/track-request?request_id=REQ483921"
}</pre>
  </article>

  <article class="endpoint" style="--line-color:#8ad4ff">
    <div class="endpoint-top">
      <span class="badge get">GET</span>
      <span class="badge pub">Public</span>
      <h3>/api/public/request-status</h3>
    </div>
    <p class="meta">Tracks a website create-bot request by request ID and shows whether it has been mirrored into the Telegram admin bot yet.</p>
    <div class="list">
      <div><strong>Required param:</strong> <code>request_id</code></div>
      <div><strong>Useful fields:</strong> <code>status</code>, <code>admin_notified</code>, <code>admin_notified_count</code>, <code>reviewed_at</code>, <code>deployed_bot_id</code>.</div>
    </div>
<pre>GET /api/public/request-status?request_id=REQ483921

{
  "status": "success",
  "data": {
    "request_id": "REQ483921",
    "status": "pending",
    "status_note": "Queued and mirrored into the Telegram admin bot.",
    "bot_queue_loaded": true,
    "admin_notified": true,
    "admin_notified_count": 5
  }
}</pre>
  </article>

  <article class="endpoint" style="--line-color:#ffe08a">
    <div class="endpoint-top">
      <span class="badge get">GET</span>
      <span class="badge pub">Public</span>
      <h3>/api/public/get-number</h3>
    </div>
    <p class="meta">Picks one random live public number for the selected country and service, then returns its OTP history for the website inbox.</p>
    <div class="list">
      <div><strong>Required params:</strong> <code>country</code>, <code>service</code></div>
      <div><strong>Optional param:</strong> <code>exclude</code> to avoid returning the currently open number when the user taps Change Number.</div>
    </div>
<pre>GET /api/public/get-number?country=Pakistan&amp;service=WhatsApp

{
  "status": "success",
  "data": {
    "number": "+923001234567",
    "service": "WhatsApp",
    "country": "Pakistan",
    "flag": "🇵🇰",
    "available_numbers": 20,
    "history_count": 2,
    "history": [
      {
        "otp": "847293",
        "message": "Your WhatsApp verification code is: 847293",
        "received_at": "2026-04-23 18:35:22"
      }
    ],
    "refreshed_at": "2026-04-23 18:42:10"
  }
}</pre>
  </article>

  <article class="endpoint" style="--line-color:#8ad4ff">
    <div class="endpoint-top">
      <span class="badge get">GET</span>
      <span class="badge pub">Public</span>
      <h3>/api/public/get-number/history</h3>
    </div>
    <p class="meta">Refreshes the OTP inbox for the currently open public number without choosing a new one.</p>
<pre>GET /api/public/get-number/history?number=+923001234567</pre>
  </article>

  <div class="sec-title">Authenticated Endpoints</div>

  <article class="endpoint" style="--line-color:#ffd166">
    <div class="endpoint-top">
      <span class="badge get">GET</span>
      <span class="badge auth">Auth</span>
      <h3>/api/sms</h3>
    </div>
    <p class="meta">Returns OTPs filtered by the panels linked to your token. If the token has no panel restrictions, it returns all available OTPs.</p>
    <div class="list">
      <div><strong>Required param:</strong> <code>token</code></div>
      <div><strong>Optional params:</strong> <code>limit</code> and <code>date=YYYY-MM-DD</code></div>
      <div><strong>Best practice:</strong> call this only from your backend or secure worker process.</div>
    </div>
<pre>GET /api/sms?token=YOUR_TOKEN&amp;limit=100&amp;date=2026-04-23

{
  "status": "success",
  "token_name": "My Website",
  "api_dev": "MyCompany",
  "total_records": 18,
  "data": [
    {
      "number": "+923001234567",
      "service": "Telegram",
      "country": "Pakistan",
      "otp": "554812",
      "message": "Your Telegram verification code is: 554812",
      "received_at": "2026-04-23 18:10:05"
    }
  ]
}</pre>
  </article>

  <article class="endpoint" style="--line-color:#ffe08a">
    <div class="endpoint-top">
      <span class="badge get">GET</span>
      <span class="badge auth">Auth</span>
      <h3>/api/stats</h3>
    </div>
    <p class="meta">Same stats payload as the public stats endpoint, but useful when you want authenticated request logging or consistent private access patterns.</p>
<pre>GET /api/stats?token=YOUR_TOKEN</pre>
  </article>

  <div class="sec-title">Token Management</div>

  <div class="grid two">
    <article class="endpoint" style="--line-color:#5dd8ca">
      <div class="endpoint-top">
        <span class="badge get">GET</span>
        <span class="badge auth">Auth</span>
        <h3>/api/tokens</h3>
      </div>
      <p class="meta">List all API tokens available to the authenticated admin context.</p>
<pre>GET /api/tokens?token=ADMIN_TOKEN</pre>
    </article>

    <article class="endpoint" style="--line-color:#5dd8ca">
      <div class="endpoint-top">
        <span class="badge post">POST</span>
        <span class="badge auth">Auth</span>
        <h3>/api/tokens/create</h3>
      </div>
      <p class="meta">Create a new token. Use the <code>panels</code> query parameter with a JSON array if you want to scope access.</p>
<pre>POST /api/tokens/create?token=ADMIN_TOKEN&amp;name=MyApp&amp;developer=Backend&amp;panels=["Panel A","Panel B"]

{
  "status": "success",
  "token": "abc123...",
  "name": "MyApp"
}</pre>
    </article>
  </div>

  <article class="endpoint" style="--line-color:#ff956c">
    <div class="endpoint-top">
      <span class="badge del">DELETE</span>
      <span class="badge auth">Auth</span>
      <h3>/api/tokens/{token_id}</h3>
    </div>
    <p class="meta">Delete a token by its token string value.</p>
<pre>DELETE /api/tokens/TOKEN_VALUE?token=ADMIN_TOKEN</pre>
  </article>

  <div class="sec-title">System</div>

  <article class="endpoint" style="--line-color:#61e4a2">
    <div class="endpoint-top">
      <span class="badge get">GET</span>
      <span class="badge pub">Public</span>
      <h3>/health</h3>
    </div>
    <p class="meta">Simple health probe for uptime checks and deployment monitors.</p>
<pre>GET /health

{
  "status": "healthy",
  "service": "CRACK SMS API v4",
  "developer": "@NONEXPERTCODER",
  "timestamp": "2026-04-23T18:42:10.112345"
}</pre>
  </article>

  <div class="sec-title">Integration Examples</div>

  <div class="grid two">
    <article class="example">
      <h3>Public get-number widget</h3>
      <p>Best when you want a temp-number style website flow that loads countries, services, one live public number, and then refreshes that number's OTP history.</p>
<pre>async function openPublicInbox(country, service, currentNumber) {
  const pickRes = await fetch('https://tempnum.net/api/public/assign', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      country,
      service,
      previous_number: currentNumber || null
    })
  });
  const pick = await pickRes.json();
  if (pick.status !== 'success') throw new Error(pick.message || 'Route unavailable');

  renderNumberCard(pick.number, pick.country, pick.service);
  renderInboxHistory(pick.history || []);

  setInterval(async () => {
    const refreshRes = await fetch(
      `https://tempnum.net/api/public/get-number/history?number=${encodeURIComponent(pick.number)}`
    );
    const refresh = await refreshRes.json();
    if (refresh.status === 'success') renderInboxHistory(refresh.data.history);
  }, 5000);
}</pre>
    </article>

    <article class="example">
      <h3>Frontend widget using public endpoints</h3>
      <p>Good when you want a public page, embedded dashboard, or live OTP ticker with no secret token in the browser.</p>
<pre>async function loadWidget() {
  const [feedRes, statsRes] = await Promise.all([
    fetch('https://tempnum.net/api/public/otps?limit=20'),
    fetch('https://tempnum.net/api/public/stats')
  ]);

  const feed = await feedRes.json();
  const stats = await statsRes.json();

  if (feed.status !== 'success') throw new Error(feed.message || 'Feed failed');
  if (stats.status !== 'success') throw new Error(stats.message || 'Stats failed');

  document.querySelector('#otp-count').textContent = stats.total_otps;
  document.querySelector('#last-hour').textContent = stats.last_hour_otps;

  const list = feed.data.map(row => ({
    number: row.number,
    service: row.service,
    country: row.country,
    otp: row.otp,
    receivedAt: row.received_at
  }));

  renderCards(list);
}

loadWidget();
setInterval(loadWidget, 5000);</pre>
    </article>

    <article class="example">
      <h3>Node.js backend proxy with a private token</h3>
      <p>Recommended for production apps. Your frontend calls your backend, and your backend talks to CRACK SMS securely.</p>
<pre>import express from 'express';

const app = express();
const TOKEN = process.env.CRACK_SMS_TOKEN;

app.get('/internal/otps', async (req, res) => {
  const url = new URL('https://tempnum.net/api/sms');
  url.searchParams.set('token', TOKEN);
  url.searchParams.set('limit', req.query.limit || '100');
  if (req.query.date) url.searchParams.set('date', req.query.date);

  const upstream = await fetch(url);
  const json = await upstream.json();

  if (!upstream.ok || json.status !== 'success') {
    return res.status(502).json({ error: 'Upstream API failed', details: json });
  }

  const cleaned = json.data.map(row => ({
    number: row.number,
    service: row.service,
    country: row.country,
    otp: row.otp,
    receivedAt: row.received_at
  }));

  res.json({ count: cleaned.length, data: cleaned });
});</pre>
    </article>

    <article class="example">
      <h3>Python polling worker</h3>
      <p>Useful for cron jobs, analytics workers, or internal dashboards that snapshot OTP traffic every few minutes.</p>
<pre>import requests

TOKEN = 'YOUR_TOKEN'
BASE = 'https://tempnum.net/api/sms'

resp = requests.get(BASE, params={
    'token': TOKEN,
    'limit': 100,
    'date': '2026-04-23'
}, timeout=20)
resp.raise_for_status()
payload = resp.json()

if payload['status'] != 'success':
    raise RuntimeError(payload)

for row in payload['data']:
    print(
        row['received_at'],
        row['service'],
        row['country'],
        row['number'],
        row['otp']
    )</pre>
    </article>

    <article class="example">
      <h3>cURL smoke test</h3>
      <p>Handy for quick server-side checks, CI health probes, or troubleshooting from a terminal.</p>
<pre>curl "https://tempnum.net/api/public/stats"

curl "https://tempnum.net/api/public/otps?limit=5"

curl "https://tempnum.net/api/sms?token=YOUR_TOKEN&limit=20&date=2026-04-23"</pre>
    </article>
  </div>

  <div class="sec-title">Recommended Integration Pattern</div>

  <section class="note">
    <h3>What usually works best</h3>
    <ul>
      <li>Use <code>/api/public/otps</code> and <code>/api/public/stats</code> directly in the browser when the data is meant to be public.</li>
      <li>Use <code>/api/public/get-number/countries</code>, <code>/services</code>, <code>/get-number</code>, and <code>/history</code> for temp-number style website flows that should not consume Telegram bot assignments.</li>
      <li>Use <code>/api/public/bot-request</code> together with <code>/api/public/request-status</code> when you want a website form plus a user-facing approval tracker.</li>
      <li>Use <code>/api/sms</code> and <code>/api/stats</code> only from your backend if you are using a private token.</li>
      <li>Cache responses for a few seconds if you poll aggressively. A 4 to 10 second refresh interval is usually enough for dashboards.</li>
      <li>Rely on <code>last_hour_otps</code> from the stats endpoint instead of calculating “last hour” in the browser, because the API already computes it on the server clock.</li>
      <li>Store only the fields you need downstream. Most integrations only need <code>service</code>, <code>country</code>, <code>otp</code>, <code>number</code>, and <code>received_at</code>.</li>
    </ul>
  </section>

  <div class="footer">
    Built for fast integration. Developer: <a href="https://t.me/NONEXPERTCODER">@NONEXPERTCODER</a>
  </div>
</div>
</body>
</html>"""


_CREATE_MY_BOT_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Create My Bot</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Space+Grotesk:wght@400;500;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#07111f;
  --panel:#0d1a2d;
  --panel-2:#12243d;
  --line:#1e3655;
  --text:#edf7ff;
  --muted:#8ca9c7;
  --soft:#5f7b99;
  --accent:#5dd8ca;
  --accent-2:#ff956c;
  --accent-3:#ffe08a;
  --ok:#61e4a2;
  --warn:#ffd166;
  --mono:'IBM Plex Mono',monospace;
  --font:'Space Grotesk',sans-serif;
  --shadow:0 26px 70px rgba(0,0,0,.34);
}
body{
  font-family:var(--font);
  color:var(--text);
  min-height:100vh;
  background:
    radial-gradient(circle at 14% 16%,rgba(93,216,202,.14),transparent 24%),
    radial-gradient(circle at 84% 10%,rgba(255,149,108,.14),transparent 20%),
    linear-gradient(180deg,#07111f 0%,#081526 100%);
}
body::before{
  content:'';
  position:fixed;
  inset:0;
  pointer-events:none;
  background-image:
    linear-gradient(rgba(255,255,255,.022) 1px,transparent 1px),
    linear-gradient(90deg,rgba(255,255,255,.022) 1px,transparent 1px);
  background-size:46px 46px;
  mask-image:linear-gradient(180deg,rgba(255,255,255,.7),transparent 92%);
}
.wrap{max-width:1240px;margin:0 auto;padding:26px 18px 52px;position:relative;z-index:1}
.topbar,.hero-card,.card,.contact-card,.result-card{
  border:1px solid rgba(255,255,255,.08);
  background:linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,.025)),rgba(13,26,45,.88);
  border-radius:24px;
  box-shadow:var(--shadow);
}
.topbar{
  display:flex;align-items:center;justify-content:space-between;gap:14px;
  padding:18px 20px;
}
.brand{display:flex;align-items:center;gap:14px;text-decoration:none;color:inherit}
.brand-mark{
  width:44px;height:44px;border-radius:14px;display:grid;place-items:center;
  background:linear-gradient(135deg,var(--accent),rgba(93,216,202,.2));
  color:#07111f;font-weight:800;
}
.brand-copy{display:grid;gap:3px}
.brand-kicker{font-size:.76rem;letter-spacing:.18em;text-transform:uppercase;color:var(--muted)}
.brand-name{font-size:1.05rem;font-weight:700}
.nav-actions{display:flex;gap:10px;flex-wrap:wrap}
.btn{
  border:none;border-radius:14px;padding:11px 16px;cursor:pointer;font-weight:700;
  transition:transform .2s ease, box-shadow .2s ease;text-decoration:none;
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
}
.btn.primary{color:#07111f;background:linear-gradient(135deg,var(--accent),#7de7db);box-shadow:0 14px 30px rgba(89,212,197,.22)}
.btn.alt{color:var(--text);background:linear-gradient(135deg,rgba(255,145,102,.2),rgba(255,224,138,.22));border:1px solid rgba(255,255,255,.08)}
.btn.ghost{color:var(--text);background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08)}
.btn:hover{transform:translateY(-1px)}
.hero{
  margin-top:18px;
  display:grid;
  grid-template-columns:1.16fr .84fr;
  gap:18px;
}
.hero-card{padding:26px}
.eyebrow{
  display:inline-flex;align-items:center;gap:8px;padding:8px 12px;border-radius:999px;
  border:1px solid rgba(255,255,255,.08);background:rgba(255,255,255,.04);
  color:var(--muted);font-size:.76rem;letter-spacing:.18em;text-transform:uppercase;
}
.eyebrow::before{content:'';width:8px;height:8px;border-radius:50%;background:var(--accent)}
.hero h1{
  margin-top:16px;font-size:clamp(2.2rem,4vw,4rem);line-height:.95;letter-spacing:-.05em;max-width:11ch;
}
.hero p{margin-top:14px;color:var(--muted);max-width:58ch;font-size:1rem;line-height:1.7}
.stats{display:grid;gap:14px}
.mini{
  padding:18px;border-radius:18px;background:rgba(255,255,255,.035);border:1px solid rgba(255,255,255,.06)
}
.mini .label{font-size:.72rem;color:var(--soft);text-transform:uppercase;letter-spacing:.16em}
.mini strong{display:block;margin-top:8px;font-size:1rem}
.grid{
  margin-top:18px;
  display:grid;
  grid-template-columns:1.12fr .88fr;
  gap:18px;
}
.card{padding:24px}
.section-title{font-size:1.28rem}
.section-copy{margin-top:8px;color:var(--muted);line-height:1.7}
.form-grid{
  margin-top:18px;
  display:grid;
  grid-template-columns:repeat(2,minmax(0,1fr));
  gap:14px;
}
.field{display:grid;gap:8px}
.field.full{grid-column:1/-1}
label{font-size:.88rem;color:var(--muted);font-weight:600}
.mode-switch{
  margin-top:18px;
  display:grid;
  grid-template-columns:repeat(2,minmax(0,1fr));
  gap:12px;
}
.mode-card{
  border-radius:18px;
  border:1px solid rgba(255,255,255,.08);
  background:rgba(255,255,255,.03);
  padding:16px;
  display:grid;
  gap:8px;
  cursor:pointer;
  transition:border-color .2s ease, transform .2s ease, background .2s ease;
}
.mode-card strong{font-size:1rem}
.mode-card span{font-size:.86rem;color:var(--soft);line-height:1.6}
.mode-card.active{
  border-color:rgba(93,216,202,.36);
  background:rgba(93,216,202,.08);
  transform:translateY(-1px);
}
.field.hidden{display:none}
input,textarea{
  width:100%;border-radius:16px;border:1px solid rgba(255,255,255,.08);
  background:#091321;color:var(--text);padding:14px 15px;font-size:.95rem;font-family:var(--font);
  outline:none;transition:border-color .2s ease, box-shadow .2s ease;
}
textarea{min-height:110px;resize:vertical}
input:focus,textarea:focus{border-color:rgba(93,216,202,.55);box-shadow:0 0 0 4px rgba(93,216,202,.1)}
.hint{font-size:.82rem;color:var(--soft);line-height:1.6}
.panel{display:none}
.panel.active{display:block}
.steps{margin-top:18px;display:grid;gap:10px}
.step-row{
  display:flex;align-items:center;gap:12px;padding:12px 14px;border-radius:16px;
  background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06)
}
.dot{
  width:28px;height:28px;border-radius:999px;display:grid;place-items:center;
  font-size:.76rem;font-family:var(--mono);background:rgba(255,255,255,.05);color:var(--soft)
}
.step-row.active{border-color:rgba(93,216,202,.28);background:rgba(93,216,202,.08)}
.step-row.active .dot{background:rgba(93,216,202,.16);color:var(--accent)}
.step-row.done .dot{background:rgba(97,228,162,.18);color:var(--ok)}
.step-row p{color:var(--muted);font-size:.92rem}
.contact-grid{margin-top:18px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.contact-card{padding:20px;display:grid;gap:12px}
.contact-kicker{font-size:.74rem;letter-spacing:.18em;text-transform:uppercase;color:var(--soft)}
.contact-card h3{font-size:1.08rem}
.contact-card p{color:var(--muted);line-height:1.7;font-size:.92rem}
.contact-value{
  padding:12px 14px;border-radius:14px;background:#091321;border:1px solid rgba(255,255,255,.07);
  font-family:var(--mono);font-size:.9rem
}
.contact-card.whatsapp{border-color:rgba(97,228,162,.16)}
.contact-card.telegram{border-color:rgba(138,212,255,.16)}
.result-card{padding:24px}
.result-grid{display:grid;gap:14px;margin-top:18px}
.result-meta{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
.meta-box{
  padding:16px;border-radius:16px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06)
}
.meta-box .label{font-size:.76rem;text-transform:uppercase;letter-spacing:.16em;color:var(--soft)}
.meta-box strong{display:block;margin-top:8px;font-size:1rem}
.status-strip{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.status-box{
  padding:16px;border-radius:16px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06)
}
.status-pill{
  display:inline-flex;align-items:center;gap:8px;padding:8px 12px;border-radius:999px;
  background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08);
  font-size:.82rem;font-weight:700;text-transform:uppercase;letter-spacing:.14em;
}
.status-pill.pending{color:#ffe08a;border-color:rgba(255,224,138,.26)}
.status-pill.approved{color:#9bf0b9;border-color:rgba(97,228,162,.28)}
.status-pill.rejected{color:#ffb4a1;border-color:rgba(255,149,108,.26)}
.status-copy{margin-top:10px;color:var(--muted);line-height:1.7;font-size:.92rem}
.message-box{
  width:100%;min-height:170px;border-radius:18px;padding:16px;border:1px solid rgba(255,255,255,.07);
  background:#091321;color:#dffef9;font-family:var(--mono);font-size:.86rem;line-height:1.6
}
.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
.notice{
  margin-top:14px;padding:14px 16px;border-radius:16px;background:rgba(255,209,102,.08);
  border:1px solid rgba(255,209,102,.18);color:#ffe7a2;font-size:.9rem;line-height:1.7
}
.success-stage{
  position:relative;
  min-height:220px;
  margin-bottom:18px;
  border-radius:22px;
  overflow:hidden;
  border:1px solid rgba(255,255,255,.08);
  background:
    radial-gradient(circle at 50% 28%,rgba(93,216,202,.16),transparent 30%),
    radial-gradient(circle at 30% 78%,rgba(255,149,108,.12),transparent 28%),
    linear-gradient(180deg,rgba(255,255,255,.04),rgba(255,255,255,.02)),
    #091321;
}
.success-stage::before{
  content:'';
  position:absolute;
  inset:-40% 18%;
  background:linear-gradient(120deg,transparent,rgba(255,255,255,.18),transparent);
  transform:translateX(-140%) rotate(12deg);
  opacity:0;
}
.success-stage.play::before{
  animation:successSweep 1.5s ease .22s forwards;
}
.success-core{
  position:absolute;
  inset:0;
  display:grid;
  place-items:center;
}
.success-ring,
.success-ring::before,
.success-ring::after{
  position:absolute;
  border-radius:999px;
  border:1px solid rgba(93,216,202,.26);
}
.success-ring{
  width:124px;
  height:124px;
  box-shadow:0 0 0 1px rgba(93,216,202,.08),0 0 30px rgba(93,216,202,.16);
}
.success-ring::before{
  content:'';
  inset:-18px;
  border-color:rgba(138,212,255,.18);
}
.success-ring::after{
  content:'';
  inset:-38px;
  border-color:rgba(255,149,108,.16);
}
.success-stage.play .success-ring{
  animation:successPulse 1.55s ease-out forwards;
}
.success-orb{
  position:relative;
  width:96px;
  height:96px;
  border-radius:999px;
  display:grid;
  place-items:center;
  background:linear-gradient(145deg,var(--accent),#9af1e9);
  box-shadow:0 20px 45px rgba(93,216,202,.24);
  color:#07111f;
  font-size:2rem;
  font-weight:800;
  transform:scale(.72);
  opacity:0;
}
.success-stage.play .success-orb{
  animation:successPop .72s cubic-bezier(.2,.85,.2,1.2) .18s forwards;
}
.success-copy{
  position:absolute;
  left:0; right:0; bottom:24px;
  text-align:center;
  padding:0 18px;
}
.success-copy strong{
  display:block;
  font-size:1.08rem;
  letter-spacing:.02em;
}
.success-copy span{
  display:block;
  margin-top:6px;
  color:var(--muted);
  font-size:.92rem;
}
.success-stage.play .success-copy{
  animation:successFade .8s ease .34s both;
}
.confetti{
  position:absolute;
  width:10px;
  height:18px;
  border-radius:999px;
  opacity:0;
  transform:translate3d(0,0,0) rotate(0deg);
}
.success-stage.play .confetti{animation:confettiFall 1.35s ease-out forwards}
.confetti.c1{left:18%;top:22%;background:#5dd8ca;animation-delay:.1s}
.confetti.c2{left:28%;top:14%;background:#ff956c;animation-delay:.18s}
.confetti.c3{left:40%;top:10%;background:#ffe08a;animation-delay:.05s}
.confetti.c4{left:60%;top:9%;background:#8ad4ff;animation-delay:.14s}
.confetti.c5{left:72%;top:16%;background:#61e4a2;animation-delay:.22s}
.confetti.c6{left:82%;top:24%;background:#ff956c;animation-delay:.12s}
.success-lines{
  position:absolute;
  inset:auto 24px 22px 24px;
  display:grid;
  gap:8px;
}
.success-line{
  height:10px;
  border-radius:999px;
  background:linear-gradient(90deg,rgba(93,216,202,.24),rgba(255,255,255,.06));
  transform:scaleX(.2);
  transform-origin:left center;
  opacity:0;
}
.success-stage.play .success-line:nth-child(1){animation:lineGrow .8s ease .28s forwards}
.success-stage.play .success-line:nth-child(2){animation:lineGrow .8s ease .38s forwards}
.success-stage.play .success-line:nth-child(3){animation:lineGrow .8s ease .48s forwards}
@keyframes successPop{
  0%{transform:scale(.72);opacity:0}
  62%{transform:scale(1.08);opacity:1}
  100%{transform:scale(1);opacity:1}
}
@keyframes successPulse{
  0%{transform:scale(.7);opacity:0}
  40%{opacity:1}
  100%{transform:scale(1.12);opacity:1}
}
@keyframes successFade{
  from{opacity:0;transform:translateY(12px)}
  to{opacity:1;transform:translateY(0)}
}
@keyframes successSweep{
  0%{transform:translateX(-140%) rotate(12deg);opacity:0}
  35%{opacity:1}
  100%{transform:translateX(140%) rotate(12deg);opacity:0}
}
@keyframes confettiFall{
  0%{opacity:0;transform:translateY(-12px) rotate(0deg)}
  18%{opacity:1}
  100%{opacity:0;transform:translateY(160px) rotate(210deg)}
}
@keyframes lineGrow{
  0%{opacity:0;transform:scaleX(.2)}
  100%{opacity:1;transform:scaleX(1)}
}
.toast{
  position:fixed;right:18px;bottom:18px;padding:12px 16px;border-radius:14px;
  background:#06111d;border:1px solid rgba(255,255,255,.08);color:var(--text);
  transform:translateY(18px);opacity:0;pointer-events:none;transition:.25s ease
}
.toast.show{transform:translateY(0);opacity:1}
@media (max-width:980px){
  .hero,.grid,.contact-grid,.result-meta,.status-strip,.form-grid{grid-template-columns:1fr}
}
@media (max-width:640px){
  .wrap{padding:18px 14px 40px}
  .topbar,.hero-card,.card,.contact-card,.result-card{border-radius:20px}
  .topbar{padding:16px}
  .hero-card,.card,.result-card{padding:20px}
}
</style>
</head>
<body>
<div class="wrap">
  <header class="topbar">
    <a class="brand" href="/">
      <div class="brand-mark">◆</div>
      <div class="brand-copy">
        <span class="brand-kicker">Virtual Number Web</span>
        <span class="brand-name">Create My Bot</span>
      </div>
    </a>
    <div class="nav-actions">
      <a class="btn ghost" href="/">Back to Dashboard</a>
      <a class="btn ghost" href="/track-request">Track Request</a>
      <a class="btn alt" href="/api/docs">API Docs</a>
    </div>
  </header>

  <section class="hero">
    <article class="hero-card">
      <span class="eyebrow">Owner review workflow</span>
      <h1>Submit your bot setup with a cleaner approval handoff.</h1>
      <p>Fill in the same delivery details used by the Telegram bot flow, let the website verify your request, then contact the owner with a prefilled message containing your request ID and setup summary.</p>
      <div class="notice" id="announcementBox">Loading website announcement…</div>
    </article>
    <aside class="hero-card stats">
      <div class="mini">
        <div class="label">Owner WhatsApp</div>
        <strong id="waTarget">+923000767749</strong>
      </div>
      <div class="mini">
        <div class="label">Owner Telegram</div>
        <strong id="tgTarget">@NONEXPERTCODER</strong>
      </div>
      <div class="mini">
        <div class="label">Approval route</div>
        <strong>Every website request is mirrored into the Telegram admin bot queue.</strong>
      </div>
    </aside>
  </section>

  <section class="grid">
    <article class="card">
      <div class="panel active" id="formPanel">
        <h2 class="section-title">Step 1: Enter your bot details</h2>
        <p class="section-copy" id="formIntroCopy">Choose whether you already have your own panel. If you do, the website collects the full bot details. If you do not, only the target group chat ID is needed for a forward-from-main setup.</p>
        <form id="createBotForm" onsubmit="startVerification(event)">
          <div class="mode-switch">
            <button class="mode-card active" type="button" id="modeHavePanel" onclick="setPanelMode(true)">
              <strong>I have a panel</strong>
              <span>Submit the full bot configuration for separate bot approval.</span>
            </button>
            <button class="mode-card" type="button" id="modeNoPanel" onclick="setPanelMode(false)">
              <strong>I do not have a panel</strong>
              <span>Send only the OTP group chat ID and request forwarding from the main panels.</span>
            </button>
          </div>
          <div class="form-grid">
            <div class="field panel-only">
              <label for="user_name">Your name</label>
              <input id="user_name" maxlength="60" placeholder="Muhammad Adnan">
            </div>
            <div class="field panel-only">
              <label for="admin_id">Telegram admin ID</label>
              <input id="admin_id" maxlength="20" placeholder="123456789">
            </div>
            <div class="field panel-only">
              <label for="bot_name">Bot name</label>
              <input id="bot_name" maxlength="60" placeholder="Crack OTP Pro">
            </div>
            <div class="field panel-only">
              <label for="bot_username">Bot username</label>
              <input id="bot_username" maxlength="40" placeholder="@MyOtpBot">
            </div>
            <div class="field full panel-only">
              <label for="token">BotFather token</label>
              <input id="token" maxlength="120" placeholder="1234567890:AAEXAMPLE_TOKEN_FROM_BOTFATHER">
              <div class="hint">A real BotFather token is required for approval. The website stores only the pending request until an admin reviews it.</div>
            </div>
            <div class="field panel-only">
              <label for="channel">Channel link</label>
              <input id="channel" maxlength="120" placeholder="https://t.me/yourchannel or none">
            </div>
            <div class="field panel-only">
              <label for="otp_group">OTP group link</label>
              <input id="otp_group" maxlength="120" placeholder="https://t.me/yourotpgroup">
            </div>
            <div class="field panel-only">
              <label for="number_bot">Number bot link</label>
              <input id="number_bot" maxlength="120" placeholder="https://t.me/YourNumberBot">
            </div>
            <div class="field panel-only">
              <label for="support">Support username</label>
              <input id="support" maxlength="60" placeholder="@ownersigma">
            </div>
            <div class="field full">
              <label for="group_id" id="groupIdLabel">OTP group chat ID</label>
              <input id="group_id" maxlength="24" placeholder="-1001234567890">
              <div class="hint" id="groupIdHint">This should be the numeric Telegram chat ID where OTP messages will be delivered.</div>
            </div>
          </div>
          <div class="actions">
            <button class="btn primary" type="submit">Verify My Details</button>
            <a class="btn ghost" href="/">Return to dashboard</a>
          </div>
        </form>
      </div>

      <div class="panel" id="verifyPanel">
        <h2 class="section-title">Step 2: Verifying your details</h2>
        <p class="section-copy">The website is preparing your approval packet before the owner contact step opens.</p>
        <div class="steps" id="verifySteps">
          <div class="step-row" data-step="1"><div class="dot">1</div><p>Checking bot identity and required fields</p></div>
          <div class="step-row" data-step="2"><div class="dot">2</div><p>Preparing admin review summary for the Telegram bot</p></div>
          <div class="step-row" data-step="3"><div class="dot">3</div><p>Generating your request ID and approval handoff</p></div>
        </div>
      </div>
    </article>

    <aside class="card">
      <div class="panel active" id="contactInfoPanel">
        <h2 class="section-title">What happens next?</h2>
        <p class="section-copy" id="statusNoteBox">Loading approval note…</p>
        <div class="notice">After the verification step finishes, choose how you want to contact the owner. The website will create your request ID, queue the request for the Telegram admin bot, and prepare a message you can send immediately.</div>
      </div>

      <div class="panel" id="contactPanel">
        <h2 class="section-title">Step 3: Choose the owner contact method</h2>
        <p class="section-copy">Pick the channel you want to use. Your request will be queued for the admin bot and a prefilled message will be prepared for you.</p>
        <div class="contact-grid">
          <article class="contact-card whatsapp">
            <div class="contact-kicker">WhatsApp</div>
            <h3>Faster owner handoff</h3>
            <p>Use WhatsApp if you want the owner contact card to open with your request summary already filled in.</p>
            <div class="contact-value" id="waContactCard">+923000767749</div>
            <button class="btn primary" type="button" onclick="submitBotRequest('whatsapp')">Use WhatsApp</button>
          </article>
          <article class="contact-card telegram">
            <div class="contact-kicker">Telegram</div>
            <h3>Direct Telegram approval chat</h3>
            <p>Use Telegram if you want to continue inside Telegram after the request is queued for the admin bot.</p>
            <div class="contact-value" id="tgContactCard">@NONEXPERTCODER</div>
            <button class="btn alt" type="button" onclick="submitBotRequest('telegram')">Use Telegram</button>
          </article>
        </div>
      </div>
    </aside>
  </section>

  <section class="result-card panel" id="resultPanel">
    <h2 class="section-title">Request submitted for owner approval</h2>
    <p class="section-copy">Your create-bot request is now queued for the Telegram admin bot and ready to be forwarded to the owner with one click.</p>
    <div class="result-grid">
      <div class="success-stage" id="successStage">
        <div class="confetti c1"></div>
        <div class="confetti c2"></div>
        <div class="confetti c3"></div>
        <div class="confetti c4"></div>
        <div class="confetti c5"></div>
        <div class="confetti c6"></div>
        <div class="success-core">
          <div class="success-ring"></div>
          <div class="success-orb">✓</div>
        </div>
        <div class="success-copy">
          <strong>Request queued successfully</strong>
          <span>Your approval packet and owner handoff are ready.</span>
        </div>
        <div class="success-lines">
          <div class="success-line"></div>
          <div class="success-line"></div>
          <div class="success-line"></div>
        </div>
      </div>
      <div class="result-meta">
        <div class="meta-box">
          <div class="label">Request ID</div>
          <strong id="resultRequestId">—</strong>
        </div>
        <div class="meta-box">
          <div class="label">Contact target</div>
          <strong id="resultContact">—</strong>
        </div>
        <div class="meta-box">
          <div class="label">Current Status</div>
          <strong id="resultStatusText">Pending</strong>
        </div>
      </div>
      <div class="status-strip">
        <div class="status-box">
          <div class="label">Bot Queue Intake</div>
          <div class="status-pill pending" id="resultSyncPill">Waiting</div>
          <div class="status-copy" id="resultSyncCopy">The website queue is waiting for the Telegram admin bot to load this request.</div>
        </div>
        <div class="status-box">
          <div class="label">Request Review</div>
          <div class="status-pill pending" id="resultReviewPill">Pending</div>
          <div class="status-copy" id="resultReviewCopy">No admin review has been recorded yet.</div>
        </div>
      </div>
      <div class="meta-box">
        <div class="label">Prefilled owner message</div>
        <textarea id="prefilledMessage" class="message-box" readonly></textarea>
      </div>
      <div class="notice" id="resultNote">Contact the owner with the request ID below. Admins will also see the same request in the Telegram bot.</div>
      <div class="actions">
        <a class="btn primary" id="openContactBtn" href="#" target="_blank" rel="noreferrer">Open Contact</a>
        <button class="btn alt" type="button" onclick="copyPrefilledMessage()">Copy Message</button>
        <a class="btn alt" id="trackRequestBtn" href="/track-request">Track Request</a>
        <a class="btn ghost" href="/">Back to Dashboard</a>
      </div>
    </div>
  </section>
</div>

<div class="toast" id="toast"></div>

<script>
let siteSettings=null;
let draftPayload=null;
let hasPanel=true;
let requestTrackTimer=null;

window.onload=()=>{
  loadSiteSettings();
  setPanelMode(true);
};

async function loadSiteSettings(){
  try{
    const r=await fetch('/api/public/site-settings');
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d=await r.json();
    if(d.status!=='success') throw new Error(d.message||'Unable to load site settings');
    siteSettings=d;
    document.getElementById('announcementBox').textContent=d.announcement || 'Owner approvals are reviewed manually.';
    document.getElementById('statusNoteBox').textContent=d.status_note || 'Choose a contact method after verification.';
    document.getElementById('waTarget').textContent=d.contact_whatsapp || '+923000767749';
    document.getElementById('tgTarget').textContent=d.contact_telegram || '@NONEXPERTCODER';
    document.getElementById('waContactCard').textContent=d.contact_whatsapp || '+923000767749';
    document.getElementById('tgContactCard').textContent=d.contact_telegram || '@NONEXPERTCODER';
  }catch(e){
    console.error('loadSiteSettings error:',e);
    toast('Unable to load owner contact settings');
  }
}

function payloadFromForm(){
  return {
    has_panel: hasPanel,
    user_name: val('user_name'),
    admin_id: val('admin_id'),
    bot_name: val('bot_name'),
    bot_username: val('bot_username'),
    token: val('token'),
    channel: val('channel'),
    otp_group: val('otp_group'),
    number_bot: val('number_bot'),
    support: val('support'),
    group_id: val('group_id')
  };
}

function validatePayload(p){
  if(!/^-?\d{5,}$/.test((p.group_id||'').trim())) return 'Group ID must be numeric';
  if(!p.has_panel) return '';
  if((p.user_name||'').trim().length < 2) return 'Enter your name first';
  if(!/^\d{5,}$/.test((p.admin_id||'').trim())) return 'Admin ID must be numeric';
  if((p.bot_name||'').trim().length < 3) return 'Bot name is too short';
  if((p.bot_username||'').replace('@','').trim().length < 4) return 'Bot username is too short';
  if(!(p.token||'').includes(':')) return 'A valid BotFather token is required';
  return '';
}

async function startVerification(ev){
  ev.preventDefault();
  const payload=payloadFromForm();
  const err=validatePayload(payload);
  if(err){
    toast(err);
    return;
  }
  draftPayload=payload;
  configureVerifySteps(payload.has_panel, false);
  showPanel('verifyPanel');
  showPanel('contactInfoPanel');
  const rows=[...document.querySelectorAll('#verifySteps .step-row')];
  rows.forEach(row=>row.classList.remove('active','done'));
  for(let i=0;i<rows.length;i++){
    rows[i].classList.add('active');
    await wait(850);
    rows[i].classList.remove('active');
    rows[i].classList.add('done');
  }
  showPanel('contactPanel');
  toast('Verification complete. Choose a contact method.');
}

async function submitBotRequest(method){
  if(!draftPayload){
    toast('Fill the form first');
    return;
  }
  showPanel('verifyPanel');
  configureVerifySteps(draftPayload.has_panel, true);
  const steps=[...document.querySelectorAll('#verifySteps .step-row')];
  steps.forEach(row=>row.classList.remove('done','active'));
  for(let i=0;i<steps.length;i++){
    steps[i].classList.add('active');
    await wait(550);
    steps[i].classList.remove('active');
    steps[i].classList.add('done');
  }

  try{
    const r=await fetch('/api/public/bot-request',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({...draftPayload, contact_method: method})
    });
    const d=await r.json();
    if(!r.ok || d.status!=='success') throw new Error(d.message||'Unable to submit request');
    renderResult(d);
    toast('Request queued successfully');
  }catch(e){
    console.error('submitBotRequest error:',e);
    toast(e.message || 'Unable to submit request');
    showPanel('contactPanel');
  }
}

function renderResult(data){
  if(requestTrackTimer){
    clearInterval(requestTrackTimer);
    requestTrackTimer=null;
  }
  document.getElementById('resultRequestId').textContent=data.request_id || '—';
  document.getElementById('resultContact').textContent=data.contact_target || '—';
  document.getElementById('prefilledMessage').value=data.prefilled_message || '';
  document.getElementById('openContactBtn').href=data.contact_url || '#';
  document.getElementById('openContactBtn').textContent=data.contact_method === 'whatsapp' ? 'Open WhatsApp' : 'Open Telegram';
  document.getElementById('trackRequestBtn').href=data.track_url || `/track-request?request_id=${encodeURIComponent(data.request_id || '')}`;
  document.getElementById('resultStatusText').textContent='Pending';
  const modeNote = data.has_panel === false
    ? 'Admins will review it as a forward-from-main request inside the Telegram bot.'
    : 'Admins can review the same request inside the Telegram bot.';
  document.getElementById('resultNote').textContent=(data.approval_note || 'Contact the owner and mention your request ID.') + ' ' + modeNote;
  document.getElementById('resultPanel').classList.add('active');
  const stage=document.getElementById('successStage');
  stage.classList.remove('play');
  void stage.offsetWidth;
  stage.classList.add('play');
  document.getElementById('resultPanel').scrollIntoView({behavior:'smooth',block:'start'});
  refreshRequestStatus(data.request_id, true);
  requestTrackTimer=setInterval(()=>refreshRequestStatus(data.request_id, false), 6000);
}

function copyPrefilledMessage(){
  const value=document.getElementById('prefilledMessage').value;
  navigator.clipboard.writeText(value).then(()=>toast('Message copied'));
}

async function refreshRequestStatus(requestId, loud){
  if(!requestId) return;
  try{
    const r=await fetch(`/api/public/request-status?request_id=${encodeURIComponent(requestId)}`);
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d=await r.json();
    if(d.status!=='success' || !d.data) throw new Error(d.message||'Unable to load request status');
    applyRequestStatus(d.data);
  }catch(e){
    console.error('refreshRequestStatus error:',e);
    if(loud) toast('Request submitted. Live tracking will appear once status is available.');
  }
}

function applyRequestStatus(data){
  const requestStatus=(data.status || 'pending').toLowerCase();
  document.getElementById('resultStatusText').textContent=requestStatus.charAt(0).toUpperCase() + requestStatus.slice(1);
  const syncReady=Boolean(data.bot_queue_loaded);
  const syncPill=document.getElementById('resultSyncPill');
  syncPill.className=`status-pill ${syncReady ? 'approved' : 'pending'}`;
  syncPill.textContent=syncReady ? 'Loaded by bot' : 'Waiting';
  document.getElementById('resultSyncCopy').textContent=syncReady
    ? (data.bot_queue_loaded_at ? `The Telegram admin bot loaded this request at ${data.bot_queue_loaded_at}.` : 'The Telegram admin bot has loaded this request.')
    : 'The website queue is still waiting for the Telegram admin bot sync job to load this request.';

  const reviewPill=document.getElementById('resultReviewPill');
  reviewPill.className=`status-pill ${requestStatus}`;
  reviewPill.textContent=requestStatus.charAt(0).toUpperCase() + requestStatus.slice(1);
  let reviewCopy=data.status_note || 'No admin review has been recorded yet.';
  if(data.admin_notified && requestStatus === 'pending'){
    reviewCopy = `${data.admin_notified_count || 0} admin notification(s) sent${data.admin_notified_at ? ' at ' + data.admin_notified_at : ''}. ` + reviewCopy;
  }
  if(data.reviewed_at){
    reviewCopy += ` Reviewed at ${data.reviewed_at}.`;
  }
  if(data.deployed_bot_id){
    reviewCopy += ` Deployment ID: ${data.deployed_bot_id}.`;
  }
  document.getElementById('resultReviewCopy').textContent=reviewCopy;
  if(requestStatus === 'approved' || requestStatus === 'rejected'){
    if(requestTrackTimer){
      clearInterval(requestTrackTimer);
      requestTrackTimer=null;
    }
  }
}

function setPanelMode(value){
  hasPanel=!!value;
  document.getElementById('modeHavePanel').classList.toggle('active', hasPanel);
  document.getElementById('modeNoPanel').classList.toggle('active', !hasPanel);
  document.querySelectorAll('.panel-only').forEach(el=>{
    el.classList.toggle('hidden', !hasPanel);
  });
  document.getElementById('formIntroCopy').textContent = hasPanel
    ? 'Use the full bot details flow when you already have your own panel and want a separate bot approved.'
    : 'No panel is needed here. Enter only the target group chat ID and request forwarding from the main panels.';
  document.getElementById('groupIdLabel').textContent = hasPanel ? 'OTP group chat ID' : 'Forward target group chat ID';
  document.getElementById('groupIdHint').textContent = hasPanel
    ? 'This should be the numeric Telegram chat ID where OTP messages will be delivered.'
    : 'Enter the numeric Telegram group chat ID that should receive OTPs from the main panels.';
}

function configureVerifySteps(panelMode, submitting){
  const lines = panelMode
    ? [
        submitting ? 'Submitting your full bot request to the website queue' : 'Checking bot identity and required fields',
        submitting ? 'Routing the separate-bot request toward the Telegram admin bot' : 'Preparing admin review summary for the Telegram bot',
        submitting ? 'Preparing your owner contact message' : 'Generating your request ID and approval handoff',
      ]
    : [
        submitting ? 'Submitting your forward-from-main request to the website queue' : 'Checking the target group chat ID format',
        submitting ? 'Routing the forwarding request toward the Telegram admin bot' : 'Preparing admin review summary for OTP forwarding',
        submitting ? 'Preparing your owner contact message' : 'Generating your request ID and approval handoff',
      ];
  document.querySelectorAll('#verifySteps .step-row').forEach((row, idx)=>{
    row.querySelector('p').textContent = lines[idx] || row.querySelector('p').textContent;
  });
}

function showPanel(id){
  const leftPanels=['formPanel','verifyPanel'];
  const rightPanels=['contactInfoPanel','contactPanel'];
  if(leftPanels.includes(id)){
    leftPanels.forEach(name=>{
      document.getElementById(name).classList.toggle('active', name===id);
    });
  }
  if(rightPanels.includes(id)){
    rightPanels.forEach(name=>{
      document.getElementById(name).classList.toggle('active', name===id);
    });
  }
}

function val(id){ return (document.getElementById(id)?.value || '').trim(); }
function wait(ms){ return new Promise(resolve=>setTimeout(resolve,ms)); }

let toastTimer=null;
function toast(msg){
  const el=document.getElementById('toast');
  el.textContent=msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer=setTimeout(()=>el.classList.remove('show'),2200);
}
</script>
</body>
</html>"""


_TRACK_REQUEST_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Track Request</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Space+Grotesk:wght@400;500;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#07111f;--panel:#0d1a2d;--line:#1e3655;--text:#edf7ff;--muted:#8ca9c7;--soft:#5f7b99;--accent:#5dd8ca;--accent-2:#ff956c;--ok:#61e4a2;--warn:#ffe08a;--mono:'IBM Plex Mono',monospace;--font:'Space Grotesk',sans-serif;--shadow:0 22px 60px rgba(0,0,0,.34)}
body{font-family:var(--font);color:var(--text);min-height:100vh;background:radial-gradient(circle at 14% 16%,rgba(93,216,202,.14),transparent 24%),radial-gradient(circle at 84% 10%,rgba(255,149,108,.14),transparent 20%),linear-gradient(180deg,#07111f 0%,#081526 100%)}
.wrap{max-width:1100px;margin:0 auto;padding:26px 18px 52px}
.topbar,.hero,.card,.timeline-item,.empty{border:1px solid rgba(255,255,255,.08);background:linear-gradient(180deg,rgba(255,255,255,.05),rgba(255,255,255,.025)),rgba(13,26,45,.88);border-radius:24px;box-shadow:var(--shadow)}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:18px 20px}
.brand{display:flex;align-items:center;gap:14px;text-decoration:none;color:inherit}
.brand-mark{width:44px;height:44px;border-radius:14px;display:grid;place-items:center;background:linear-gradient(135deg,var(--accent),rgba(93,216,202,.2));color:#07111f;font-weight:800}
.brand-copy{display:grid;gap:3px}
.brand-kicker{font-size:.76rem;letter-spacing:.18em;text-transform:uppercase;color:var(--muted)}
.brand-name{font-size:1.05rem;font-weight:700}
.nav-actions{display:flex;gap:10px;flex-wrap:wrap}
.btn{border:none;border-radius:14px;padding:11px 16px;cursor:pointer;font-weight:700;display:inline-flex;align-items:center;justify-content:center;gap:8px;text-decoration:none;transition:transform .2s ease}
.btn:hover{transform:translateY(-1px)}
.btn.primary{color:#07111f;background:linear-gradient(135deg,var(--accent),#7de7db)}
.btn.alt{color:var(--text);background:linear-gradient(135deg,rgba(255,145,102,.2),rgba(255,224,138,.22));border:1px solid rgba(255,255,255,.08)}
.btn.ghost{color:var(--text);background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08)}
.hero{margin-top:18px;padding:26px;display:grid;gap:14px}
.eyebrow{display:inline-flex;align-items:center;gap:8px;padding:8px 12px;border-radius:999px;border:1px solid rgba(255,255,255,.08);background:rgba(255,255,255,.04);color:var(--muted);font-size:.76rem;letter-spacing:.18em;text-transform:uppercase}
.eyebrow::before{content:'';width:8px;height:8px;border-radius:50%;background:var(--accent)}
.hero h1{font-size:clamp(2.1rem,4vw,3.6rem);line-height:.95;letter-spacing:-.05em;max-width:12ch}
.hero p{color:var(--muted);max-width:62ch;line-height:1.7}
.search-card{margin-top:18px;padding:24px}
.search-row{display:grid;grid-template-columns:1fr auto;gap:12px;margin-top:18px}
input{width:100%;border-radius:16px;border:1px solid rgba(255,255,255,.08);background:#091321;color:var(--text);padding:14px 15px;font-size:1rem;font-family:var(--mono);outline:none}
input:focus{border-color:rgba(93,216,202,.55);box-shadow:0 0 0 4px rgba(93,216,202,.1)}
.hint{margin-top:10px;color:var(--soft);font-size:.88rem;line-height:1.7}
.status-card{margin-top:18px;padding:24px;display:none}
.status-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-top:18px}
.meta,.timeline-item,.empty{padding:18px}
.meta{border-radius:18px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06)}
.label{font-size:.72rem;color:var(--soft);text-transform:uppercase;letter-spacing:.16em}
.value{display:block;margin-top:8px;font-size:1rem;word-break:break-word}
.pill{display:inline-flex;align-items:center;gap:8px;padding:8px 12px;border-radius:999px;border:1px solid rgba(255,255,255,.08);background:rgba(255,255,255,.04);font-size:.82rem;font-weight:700;letter-spacing:.14em;text-transform:uppercase}
.pill.pending{color:var(--warn);border-color:rgba(255,224,138,.26)}
.pill.approved{color:#9bf0b9;border-color:rgba(97,228,162,.28)}
.pill.rejected{color:#ffb4a1;border-color:rgba(255,149,108,.26)}
.summary{margin-top:16px;color:var(--muted);line-height:1.8}
.timeline{margin-top:18px;display:grid;gap:12px}
.timeline-item{display:grid;gap:8px}
.timeline-item strong{font-size:1rem}
.timeline-item span{color:var(--muted);line-height:1.7}
.empty{margin-top:18px;text-align:center;color:var(--muted);display:grid;gap:8px}
.toast{position:fixed;right:18px;bottom:18px;padding:12px 16px;border-radius:14px;background:#06111d;border:1px solid rgba(255,255,255,.08);color:var(--text);transform:translateY(18px);opacity:0;pointer-events:none;transition:.25s ease}
.toast.show{transform:translateY(0);opacity:1}
@media (max-width:900px){.search-row,.status-grid{grid-template-columns:1fr}}
@media (max-width:640px){.wrap{padding:18px 14px 40px}.topbar,.hero,.card,.timeline-item,.empty{border-radius:20px}}
</style>
</head>
<body>
<div class="wrap">
  <header class="topbar">
    <a class="brand" href="/">
      <div class="brand-mark">◆</div>
      <div class="brand-copy">
        <span class="brand-kicker">Virtual Number Web</span>
        <span class="brand-name">Track Request</span>
      </div>
    </a>
    <div class="nav-actions">
      <a class="btn ghost" href="/">Dashboard</a>
      <a class="btn ghost" href="/create-my-bot">Create My Bot</a>
      <a class="btn alt" href="/api/docs">API Docs</a>
    </div>
  </header>
  <section class="hero">
    <span class="eyebrow">Approval tracker</span>
    <h1>Check whether your request reached the Telegram admin bot.</h1>
    <p>Enter the request ID you received after submission. The tracker shows whether the website has mirrored it into the bot, whether it has been reviewed, and whether a deployment ID has been attached yet.</p>
  </section>
  <section class="search-card card">
    <h2>Request lookup</h2>
    <p class="hint">Use the ID in the format <code>REQ123456</code>. This page never returns raw bot tokens.</p>
    <div class="search-row">
      <input id="requestIdInput" placeholder="REQ123456" maxlength="20">
      <button class="btn primary" onclick="lookupRequest()">Track Request</button>
    </div>
  </section>
  <section class="status-card card" id="statusCard">
    <div class="pill pending" id="statusPill">Pending</div>
    <div class="summary" id="statusSummary">Waiting for request details.</div>
    <div class="status-grid">
      <div class="meta"><div class="label">Request ID</div><strong class="value" id="metaRequestId">—</strong></div>
      <div class="meta"><div class="label">Mode</div><strong class="value" id="metaMode">—</strong></div>
      <div class="meta"><div class="label">Contact</div><strong class="value" id="metaContact">—</strong></div>
      <div class="meta"><div class="label">Requester</div><strong class="value" id="metaUser">—</strong></div>
      <div class="meta"><div class="label">Group ID</div><strong class="value" id="metaGroup">—</strong></div>
      <div class="meta"><div class="label">Bot / Route</div><strong class="value" id="metaBot">—</strong></div>
    </div>
    <div class="timeline" id="timeline"></div>
  </section>
  <div class="empty" id="emptyState">
    <strong>No request loaded yet</strong>
    <span>Enter your request ID above to view the latest website and admin-bot status.</span>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
let pollTimer=null;
window.onload=()=>{
  const params=new URLSearchParams(location.search);
  const reqId=(params.get('request_id') || '').trim();
  if(reqId){
    document.getElementById('requestIdInput').value=reqId;
    lookupRequest(true);
  }
};
async function lookupRequest(silent=false){
  const reqId=(document.getElementById('requestIdInput').value || '').trim().toUpperCase();
  if(!reqId){ toast('Enter your request ID first'); return; }
  try{
    const r=await fetch(`/api/public/request-status?request_id=${encodeURIComponent(reqId)}`);
    const d=await r.json();
    if(!r.ok || d.status!=='success' || !d.data) throw new Error(d.message || 'Request not found');
    renderRequest(d.data);
    const next=new URL(location.href);
    next.searchParams.set('request_id', reqId);
    history.replaceState(null,'',next);
    if(!silent) toast('Request status updated');
    if(pollTimer) clearInterval(pollTimer);
    if(!['approved','rejected'].includes((d.data.status || '').toLowerCase())){
      pollTimer=setInterval(()=>lookupRequest(true),6000);
    }
  }catch(e){
    console.error('lookupRequest error:',e);
    if(pollTimer){ clearInterval(pollTimer); pollTimer=null; }
    toast(e.message || 'Unable to load request');
  }
}
function renderRequest(data){
  const status=(data.status || 'pending').toLowerCase();
  const pill=document.getElementById('statusPill');
  pill.className=`pill ${status}`;
  pill.textContent=status.charAt(0).toUpperCase() + status.slice(1);
  document.getElementById('statusSummary').textContent=data.status_note || 'Status updated.';
  document.getElementById('metaRequestId').textContent=data.request_id || '—';
  document.getElementById('metaMode').textContent=data.mode || '—';
  document.getElementById('metaContact').textContent=`${titleCase(data.contact_method || 'pending')} • ${data.contact_target || '—'}`;
  document.getElementById('metaUser').textContent=data.user_name || '—';
  document.getElementById('metaGroup').textContent=data.group_id || '—';
  document.getElementById('metaBot').textContent=data.has_panel === false ? 'Forward From Main Panels' : `${data.bot_name || '—'} ${data.bot_username && data.bot_username !== '—' ? '(' + data.bot_username + ')' : ''}`.trim();
  document.getElementById('statusCard').style.display='block';
  document.getElementById('emptyState').style.display='none';
  const items=[
    {title:'Website queue accepted', body:`Created at ${data.created_at || '—'}. The request is now stored on the website queue.`},
    {title:data.bot_queue_loaded ? 'Telegram admin bot loaded request' : 'Waiting for Telegram admin bot intake', body:data.bot_queue_loaded ? `The bot queue loaded this request${data.bot_queue_loaded_at ? ' at ' + data.bot_queue_loaded_at : ''}.` : 'The sync job has not loaded this request into the Telegram admin bot queue yet.'},
    {title:data.admin_notified ? 'Admins notified' : 'Waiting for admin notifications', body:data.admin_notified ? `${data.admin_notified_count || 0} admin notification(s) have been sent${data.admin_notified_at ? ' at ' + data.admin_notified_at : ''}.` : 'No Telegram admin notification has been recorded yet.'},
    {title:status === 'approved' ? 'Request approved' : status === 'rejected' ? 'Request rejected' : 'Pending admin review', body:status === 'approved' ? `Reviewed at ${data.reviewed_at || '—'}${data.deployed_bot_id ? '. Deployment ID: ' + data.deployed_bot_id : '.'}` : status === 'rejected' ? `Reviewed at ${data.reviewed_at || '—'}.` : 'No final admin review has been recorded yet.'}
  ];
  document.getElementById('timeline').innerHTML=items.map(item=>`<article class="timeline-item"><strong>${e(item.title)}</strong><span>${e(item.body)}</span></article>`).join('');
}
function titleCase(v){ return String(v || '').replace(/(^|\\s)\\S/g, ch => ch.toUpperCase()); }
function e(v){ return String(v ?? '').replace(/[&<>\"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[m])); }
let toastTimer=null;
function toast(msg){
  const el=document.getElementById('toast');
  el.textContent=msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer=setTimeout(()=>el.classList.remove('show'),2200);
}
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def home():
    return _DASHBOARD


@app.get("/create-my-bot", response_class=HTMLResponse)
async def create_my_bot_page():
    return _CREATE_MY_BOT_PAGE


@app.get("/track-request", response_class=HTMLResponse)
async def track_request_page():
    return _TRACK_REQUEST_PAGE


@app.get("/api/docs", response_class=HTMLResponse)
async def api_docs():
    return _DOCS


@app.exception_handler(HTTPException)
async def http_ex(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"status": "error", "message": exc.detail},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
