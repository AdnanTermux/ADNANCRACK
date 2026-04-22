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

from fastapi import FastAPI, Query, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select, func

sys.path.insert(0, os.path.dirname(__file__))

from database import (
    AsyncSessionLocal, APIToken, Number, History,
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

DEV_HANDLE = "@NONEXPERTCODER"
BOT_CHANNEL = "https://t.me/crackotp"


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
<title>CRACK SMS Live Dashboard</title>
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
  .hero,.chart-grid{grid-template-columns:1fr}
  .stats-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
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
        <span class="brand-kicker">Live OTP Monitor</span>
        <span class="brand-name">CRACK SMS Dashboard</span>
      </div>
    </a>
    <div class="topbar-actions">
      <span class="chip live" id="livePill">Live feed online</span>
      <span class="chip" id="countPill">Waiting for data</span>
      <button class="action-btn alt" onclick="load()">Refresh</button>
      <button class="action-btn" onclick="location.href='/api/docs'">API Docs</button>
    </div>
  </header>

  <section class="hero">
    <div class="hero-copy">
      <span class="eyebrow">Operational View</span>
      <h1>Track OTP activity in real time with a cleaner control surface.</h1>
      <p>Browse incoming messages, monitor service trends, and export recent traffic without leaving the live dashboard.</p>
      <div class="hero-badges">
        <span class="chip live">Auto-refresh every 4s</span>
        <span class="chip" id="updatedPill">Awaiting first sync</span>
        <span class="chip warm" id="servicesPill">Services: —</span>
      </div>
    </div>
    <aside class="hero-panel">
      <div>
        <div class="panel-label">Operations Snapshot</div>
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
        <h2>Live OTP feed</h2>
        <p>Filter by service, search by number or code, and export recent records to CSV.</p>
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
      <div>CRACK SMS live dashboard for public OTP traffic visibility.</div>
      <div class="footer-links">
        <a href="/api/docs">API Docs</a>
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
let lineChart=null,donutChart=null;

window.onload=()=>{
  initCharts();
  load();
  loadStats();
  setInterval(load,4000);
  setInterval(loadStats,30000);
};

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
<title>CRACK SMS — API Documentation</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@600;800&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#050508;--s1:#0c0d14;--s2:#11121c;--bdr:#1e2035;
  --acc:#6c47ff;--acc2:#ff3cac;--grn:#00ff88;--yel:#ffd600;
  --txt:#e8eaf6;--sub:#7b7fa8;--dim:#454870;
  --mono:'JetBrains Mono',monospace;--head:'Syne',sans-serif;}
body{background:var(--bg);color:var(--txt);font-family:var(--mono);
  min-height:100vh;line-height:1.7;}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:0;
  background:
    radial-gradient(circle at 15% 30%,rgba(108,71,255,.07),transparent 50%),
    radial-gradient(circle at 85% 70%,rgba(255,60,172,.06),transparent 50%);
}
.wrap{max-width:900px;margin:0 auto;padding:48px 24px;position:relative;z-index:1}
h1{font-family:var(--head);font-size:2.2rem;font-weight:800;margin-bottom:6px;
  background:linear-gradient(90deg,#c4b5fd,#f472b6);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;}
.tagline{color:var(--sub);font-size:.82rem;margin-bottom:40px}
.tagline a{color:var(--acc);text-decoration:none}
.tagline a:hover{color:var(--acc2)}

/* endpoint card */
.ep{background:var(--s2);border:1px solid var(--bdr);border-radius:14px;
  padding:24px;margin:16px 0;position:relative;overflow:hidden;
  transition:border-color .25s;}
.ep::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:var(--line,linear-gradient(90deg,var(--acc),var(--acc2)));opacity:.7;}
.ep:hover{border-color:var(--acc)}
.ep-top{display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap}
.badge{display:inline-block;padding:3px 10px;border-radius:5px;
  font-size:.65rem;font-weight:700;letter-spacing:.8px;text-transform:uppercase;}
.get{background:rgba(0,255,136,.1);color:var(--grn);border:1px solid rgba(0,255,136,.2)}
.post{background:rgba(108,71,255,.1);color:#a78bfa;border:1px solid rgba(108,71,255,.2)}
.del{background:rgba(255,68,68,.1);color:#f87171;border:1px solid rgba(255,68,68,.2)}
.pub{background:rgba(0,229,255,.08);color:#67e8f9;border:1px solid rgba(0,229,255,.18)}
.auth{background:rgba(255,214,0,.08);color:var(--yel);border:1px solid rgba(255,214,0,.2)}
.ep h3{font-family:var(--head);font-size:1rem;font-weight:700;color:var(--txt)}
.ep p{font-size:.8rem;color:var(--sub);margin-bottom:12px}
pre{background:var(--s1);border:1px solid var(--bdr);border-radius:8px;
  padding:14px 16px;font-size:.75rem;overflow-x:auto;color:#a78bfa;
  line-height:1.6;white-space:pre-wrap;word-break:break-all;}
.ep.g::before{--line:linear-gradient(90deg,var(--grn),transparent)}
.ep.y::before{--line:linear-gradient(90deg,var(--yel),transparent)}
.ep.r::before{--line:linear-gradient(90deg,#f87171,transparent)}
.ep.c::before{--line:linear-gradient(90deg,#67e8f9,transparent)}

.sec-title{font-family:var(--head);font-size:.72rem;font-weight:800;
  letter-spacing:1.5px;text-transform:uppercase;color:var(--dim);
  margin:36px 0 12px;display:flex;align-items:center;gap:10px;}
.sec-title::after{content:'';flex:1;height:1px;background:linear-gradient(90deg,var(--bdr),transparent)}

.dev-note{
  margin-top:40px;text-align:center;padding:20px;
  background:var(--s2);border:1px solid var(--bdr);border-radius:14px;
}
.dev-note .lbl{font-size:.7rem;color:var(--dim);margin-bottom:6px;letter-spacing:1px;text-transform:uppercase}
.dev-note a{color:var(--acc2);text-decoration:none;font-weight:700;font-size:.95rem}
.dev-note a:hover{color:var(--acc)}
</style>
</head>
<body>
<div class="wrap">
<h1>CRACK SMS API</h1>
<div class="tagline">
  Base URL: <code style="color:#a78bfa">https://tempnum.net</code>
  &nbsp;·&nbsp; <a href="/">← Live Dashboard</a>
  &nbsp;·&nbsp; Developer: <a href="https://t.me/NONEXPERTCODER">@NONEXPERTCODER</a>
</div>

<div class="sec-title">Public Endpoints — No Token Required</div>

<div class="ep g">
  <div class="ep-top">
    <span class="badge get">GET</span>
    <span class="badge pub">PUBLIC</span>
    <h3>/api/public/otps</h3>
  </div>
  <p>Fetch ALL received OTPs from all panels. No authentication needed.</p>
  <pre>GET /api/public/otps?limit=200

Response:
{
  "status": "success",
  "total_records": 42,
  "data": [
    {
      "number":      "+923001234567",
      "service":     "WhatsApp",
      "country":     "Pakistan",
      "otp":         "847293",
      "message":     "Your WhatsApp verification code is: 847293",
      "received_at": "2025-04-20 14:35:22"
    }
  ]
}</pre>
</div>

<div class="ep c">
  <div class="ep-top">
    <span class="badge get">GET</span>
    <span class="badge pub">PUBLIC</span>
    <h3>/api/public/stats</h3>
  </div>
  <p>Overall statistics — total OTPs, today count, top services, hourly breakdown.</p>
  <pre>GET /api/public/stats</pre>
</div>

<div class="sec-title">Authenticated Endpoints — Token Required</div>

<div class="ep">
  <div class="ep-top">
    <span class="badge get">GET</span>
    <span class="badge auth">AUTH</span>
    <h3>/api/sms</h3>
  </div>
  <p>OTPs filtered to the panels configured on your API token. If no panels are selected on the token, returns ALL OTPs.</p>
  <pre>GET /api/sms?token=YOUR_TOKEN&amp;limit=200&amp;date=2025-04-20

Response:
{
  "status":        "success",
  "token_name":    "My Website",
  "api_dev":       "MyCompany",
  "total_records": 18,
  "data": [ ... ]
}</pre>
</div>

<div class="ep y">
  <div class="ep-top">
    <span class="badge get">GET</span>
    <span class="badge auth">AUTH</span>
    <h3>/api/stats</h3>
  </div>
  <p>Authenticated statistics (same payload as public stats, access-logged).</p>
  <pre>GET /api/stats?token=YOUR_TOKEN</pre>
</div>

<div class="sec-title">Token Management</div>

<div class="ep">
  <div class="ep-top">
    <span class="badge get">GET</span>
    <span class="badge auth">AUTH</span>
    <h3>/api/tokens</h3>
  </div>
  <p>List all API tokens (admin use).</p>
  <pre>GET /api/tokens?token=ADMIN_TOKEN</pre>
</div>

<div class="ep">
  <div class="ep-top">
    <span class="badge post">POST</span>
    <span class="badge auth">AUTH</span>
    <h3>/api/tokens/create</h3>
  </div>
  <p>Create a new API token with optional panel filter.</p>
  <pre>POST /api/tokens/create?token=ADMIN_TOKEN&amp;name=MyApp&amp;developer=Dev&amp;panels=[]

Response:
{ "status": "success", "token": "abc123...", "name": "MyApp" }</pre>
</div>

<div class="ep r">
  <div class="ep-top">
    <span class="badge del">DELETE</span>
    <span class="badge auth">AUTH</span>
    <h3>/api/tokens/{token_id}</h3>
  </div>
  <p>Delete an API token by its string value.</p>
  <pre>DELETE /api/tokens/TOKEN_VALUE?token=ADMIN_TOKEN</pre>
</div>

<div class="sec-title">System</div>

<div class="ep g">
  <div class="ep-top">
    <span class="badge get">GET</span>
    <span class="badge pub">PUBLIC</span>
    <h3>/health</h3>
  </div>
  <p>Health check — returns service status and timestamp.</p>
  <pre>GET /health
→ { "status": "healthy", "service": "CRACK SMS API v4", "developer": "@NONEXPERTCODER" }</pre>
</div>

<!-- Code Examples -->
<div class="sec-title">Integration Examples</div>

<div class="ep">
  <div class="ep-top"><h3>JavaScript / Node.js</h3></div>
<pre>// Fetch all latest OTPs
const res  = await fetch('https://tempnum.net/api/sms?token=TOKEN&amp;limit=50');
const data = await res.json();
data.data.forEach(otp => console.log(otp.number, otp.otp));</pre>
</div>

<div class="ep">
  <div class="ep-top"><h3>Python</h3></div>
<pre>import requests
r = requests.get('https://tempnum.net/api/sms',
                  params={'token': 'TOKEN', 'limit': 50})
for otp in r.json()['data']:
    print(otp['number'], otp['otp'])</pre>
</div>

<div class="ep">
  <div class="ep-top"><h3>PHP</h3></div>
<pre>$data = json_decode(file_get_contents(
    'https://tempnum.net/api/sms?token=TOKEN&limit=50'
), true);
foreach ($data['data'] as $otp) echo $otp['number'].' '.$otp['otp']."\\n";</pre>
</div>

<div class="dev-note">
  <div class="lbl">⚡ Developed by</div>
  <a href="https://t.me/NONEXPERTCODER">@NONEXPERTCODER</a>
</div>

</div><!-- /wrap -->
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def home():
    return _DASHBOARD


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
