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
        total    = await s.scalar(select(func.count(History.id))) or 0
        today    = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        today_ct = await s.scalar(
            select(func.count(History.id)).where(History.created_at >= today)
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
        since  = datetime.now() - timedelta(hours=24)
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
        "by_service":      services,
        "hourly_last_24h": hourly,
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
<title>TempNum — Live OTP Feed | tempnum.net</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#050508;
  --s1:#0c0d14;--s2:#11121c;--s3:#181928;
  --bdr:#1e2035;--bdr2:#262840;
  --acc:#6c47ff;--acc2:#ff3cac;--acc3:#00e5ff;
  --grn:#00ff88;--yel:#ffd600;--red:#ff4444;
  --txt:#e8eaf6;--sub:#7b7fa8;--dim:#454870;
  --font:'Syne',sans-serif;--mono:'JetBrains Mono',monospace;
  --r:14px;--r2:8px;
  --glow-acc:0 0 30px rgba(108,71,255,.35);
  --glow-grn:0 0 20px rgba(0,255,136,.3);
  --glow-pink:0 0 30px rgba(255,60,172,.3);
}
html{scroll-behavior:smooth}

/* ── SCROLLBAR ── */
::-webkit-scrollbar{width:5px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--acc);border-radius:4px}

/* ── BASE ── */
body{
  font-family:var(--font);background:var(--bg);color:var(--txt);
  min-height:100vh;overflow-x:hidden;position:relative;
}

/* ── PARTICLE CANVAS ── */
#particles{position:fixed;inset:0;z-index:0;pointer-events:none;opacity:.45}

/* ── GRID OVERLAY ── */
body::before{
  content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background-image:
    linear-gradient(rgba(108,71,255,.04) 1px,transparent 1px),
    linear-gradient(90deg,rgba(108,71,255,.04) 1px,transparent 1px);
  background-size:60px 60px;
}

/* ── GRADIENT ORBS ── */
.orb{position:fixed;border-radius:50%;filter:blur(100px);pointer-events:none;z-index:0;animation:orb-move 20s ease-in-out infinite}
.orb1{width:600px;height:600px;top:-200px;left:-200px;background:radial-gradient(circle,rgba(108,71,255,.12),transparent 70%);animation-delay:0s}
.orb2{width:500px;height:500px;bottom:-150px;right:-100px;background:radial-gradient(circle,rgba(255,60,172,.1),transparent 70%);animation-delay:-7s}
.orb3{width:400px;height:400px;top:50%;left:50%;transform:translate(-50%,-50%);background:radial-gradient(circle,rgba(0,229,255,.06),transparent 70%);animation-delay:-14s}
@keyframes orb-move{0%,100%{transform:translate(0,0) scale(1)} 33%{transform:translate(40px,-60px) scale(1.1)} 66%{transform:translate(-30px,40px) scale(.95)}}

/* ── TOPBAR ── */
.topbar{
  position:sticky;top:0;z-index:200;
  display:flex;align-items:center;justify-content:space-between;
  padding:0 32px;height:64px;
  background:rgba(5,5,8,.8);backdrop-filter:blur(24px) saturate(180%);
  border-bottom:1px solid var(--bdr);
}
.logo{display:flex;align-items:center;gap:14px;text-decoration:none}
.logo-mark{
  width:40px;height:40px;border-radius:10px;
  background:linear-gradient(135deg,var(--acc),var(--acc2));
  display:grid;place-items:center;font-size:18px;
  box-shadow:var(--glow-acc);
  animation:logo-pulse 3s ease-in-out infinite;
}
@keyframes logo-pulse{0%,100%{box-shadow:var(--glow-acc)} 50%{box-shadow:0 0 50px rgba(108,71,255,.6)}}
.logo-text{font-size:.95rem;font-weight:800;letter-spacing:.5px;
  background:linear-gradient(90deg,#c4b5fd,#f472b6,#67e8f9);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
  background-size:200%;animation:grad-shift 4s linear infinite;
}
@keyframes grad-shift{0%{background-position:0%} 100%{background-position:200%}}
.topbar-right{display:flex;align-items:center;gap:12px}
.pill{
  display:flex;align-items:center;gap:7px;
  padding:6px 14px;border-radius:20px;font-size:.72rem;font-weight:700;
  border:1px solid;font-family:var(--mono);letter-spacing:.3px;
}
.pill-live{color:var(--grn);border-color:rgba(0,255,136,.25);background:rgba(0,255,136,.06)}
.pill-live::before{
  content:'';width:8px;height:8px;border-radius:50%;background:var(--grn);
  animation:live-blink 1.5s ease-in-out infinite;box-shadow:var(--glow-grn);
}
@keyframes live-blink{0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.4;transform:scale(1.4)}}
.pill-count{color:var(--sub);border-color:var(--bdr);background:rgba(255,255,255,.02)}
.btn-docs{
  padding:8px 16px;border-radius:8px;font-size:.76rem;font-weight:700;
  background:linear-gradient(135deg,var(--acc),var(--acc2));
  color:#fff;border:none;cursor:pointer;
  box-shadow:0 4px 16px rgba(108,71,255,.35);
  transition:all .25s;letter-spacing:.3px;
}
.btn-docs:hover{transform:translateY(-2px);box-shadow:0 8px 28px rgba(108,71,255,.5)}

/* ── MAIN WRAP ── */
.wrap{position:relative;z-index:1;max-width:1600px;margin:0 auto;padding:32px 24px 60px}

/* ── SECTION HEADER ── */
.sh{display:flex;align-items:center;gap:10px;margin-bottom:16px}
.sh-line{flex:1;height:1px;background:linear-gradient(90deg,var(--acc),transparent)}
.sh-text{font-size:.68rem;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--dim)}

/* ── STATS GRID ── */
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin-bottom:36px}
.stat{
  background:var(--s2);border:1px solid var(--bdr);border-radius:var(--r);
  padding:22px 24px;position:relative;overflow:hidden;cursor:default;
  transition:all .3s;
}
.stat::before{
  content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:var(--line,linear-gradient(90deg,var(--acc),var(--acc2)));
}
.stat::after{
  content:'';position:absolute;inset:0;opacity:0;transition:.3s;
  background:radial-gradient(circle at 50% 0,rgba(108,71,255,.08),transparent 70%);
}
.stat:hover{transform:translateY(-5px);border-color:var(--acc);box-shadow:var(--glow-acc)}
.stat:hover::after{opacity:1}
.stat-icon{font-size:1.6rem;opacity:.12;position:absolute;top:14px;right:16px}
.stat-val{
  font-size:2.5rem;font-weight:800;font-family:var(--mono);line-height:1;margin-bottom:8px;
  background:linear-gradient(135deg,var(--txt),var(--sub));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
}
.stat-label{font-size:.68rem;text-transform:uppercase;letter-spacing:1px;color:var(--dim);font-weight:700}
.stat-delta{font-size:.72rem;color:var(--grn);margin-top:6px;font-family:var(--mono)}
.stat.g::before{background:linear-gradient(90deg,var(--grn),transparent)}
.stat.y::before{background:linear-gradient(90deg,var(--yel),transparent)}
.stat.c::before{background:linear-gradient(90deg,var(--acc3),transparent)}

/* ── CHART ROW ── */
.chart-row{display:grid;grid-template-columns:1.8fr 1fr;gap:16px;margin-bottom:36px}
@media(max-width:1100px){.chart-row{grid-template-columns:1fr}}
.chart-card{
  background:var(--s2);border:1px solid var(--bdr);border-radius:var(--r);
  padding:24px;position:relative;overflow:hidden;
}
.chart-card::before{
  content:'';position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(135deg,rgba(108,71,255,.04),transparent 60%);
}
.chart-title{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:var(--dim);margin-bottom:20px}
.chart-card canvas{max-height:230px}

/* ── CONTROLS ── */
.controls{display:flex;gap:10px;margin-bottom:20px;flex-wrap:wrap;align-items:center}
.search-wrap{flex:1;min-width:200px;position:relative}
.search-wrap input{
  width:100%;background:var(--s2);border:1px solid var(--bdr);border-radius:10px;
  color:var(--txt);font-family:var(--mono);font-size:.82rem;
  padding:10px 14px 10px 38px;outline:none;transition:.25s;
}
.search-wrap input:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(108,71,255,.12)}
.search-wrap input::placeholder{color:var(--dim)}
.search-icon{position:absolute;left:13px;top:50%;transform:translateY(-50%);color:var(--dim);font-size:.9rem}
select.ctrl-sel{
  background:var(--s2);border:1px solid var(--bdr);border-radius:10px;
  color:var(--txt);font-family:var(--mono);font-size:.82rem;
  padding:10px 14px;outline:none;cursor:pointer;min-width:150px;transition:.25s;
}
select.ctrl-sel:focus{border-color:var(--acc)}
.ctrl-btn{
  background:var(--s2);color:var(--txt);border:1px solid var(--bdr);
  border-radius:10px;padding:10px 18px;font-size:.82rem;font-weight:700;
  cursor:pointer;transition:.25s;font-family:var(--font);
}
.ctrl-btn:hover{border-color:var(--acc);color:var(--acc);box-shadow:0 4px 14px rgba(108,71,255,.2)}
.ctrl-btn.accent{
  background:linear-gradient(135deg,var(--acc),var(--acc2));
  color:#fff;border-color:transparent;
  box-shadow:0 4px 14px rgba(108,71,255,.35);
}
.ctrl-btn.accent:hover{box-shadow:0 8px 24px rgba(108,71,255,.55);transform:translateY(-1px)}

/* ── OTP GRID ── */
.otp-grid{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(310px,1fr));
  gap:14px;margin-bottom:32px;
}

/* ── OTP CARD ── */
.otp-card{
  background:var(--s2);border:1px solid var(--bdr);border-radius:var(--r);
  padding:18px 20px;position:relative;overflow:hidden;
  transition:all .3s;
  animation:card-in .4s cubic-bezier(.16,1,.3,1) both;
}
@keyframes card-in{from{opacity:0;transform:translateY(16px) scale(.97)} to{opacity:1;transform:none}}
.otp-card:hover{border-color:var(--acc2);transform:translateY(-5px);box-shadow:0 16px 48px rgba(255,60,172,.15)}
.otp-card::before{
  content:'';position:absolute;left:0;top:0;bottom:0;width:3px;border-radius:0 2px 2px 0;
  background:linear-gradient(180deg,var(--acc),var(--acc2));
}
.otp-card::after{
  content:'';position:absolute;top:0;right:0;width:80px;height:80px;
  background:radial-gradient(circle,rgba(108,71,255,.06),transparent 70%);
  pointer-events:none;
}
/* NEW badge */
.new-badge{
  position:absolute;top:12px;right:12px;
  font-size:.62rem;font-weight:800;text-transform:uppercase;letter-spacing:.8px;
  padding:3px 9px;border-radius:20px;
  background:rgba(0,255,136,.1);color:var(--grn);border:1px solid rgba(0,255,136,.25);
  animation:badge-fade 5s forwards;
}
@keyframes badge-fade{0%,70%{opacity:1} 100%{opacity:0}}

/* Card header */
.card-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:14px;gap:8px}
.svc-tag{
  font-size:.64rem;font-weight:800;letter-spacing:.8px;text-transform:uppercase;
  padding:4px 10px;border-radius:6px;
  background:rgba(108,71,255,.1);color:var(--acc);border:1px solid rgba(108,71,255,.18);
}
.card-ts{font-size:.68rem;font-family:var(--mono);color:var(--dim)}
.card-num{font-family:var(--mono);font-size:.94rem;font-weight:700;margin-bottom:6px}
.card-country{font-size:.72rem;color:var(--dim);margin-bottom:14px}

/* OTP display */
.otp-box{
  background:var(--s1);border:1px solid var(--bdr2);border-radius:var(--r2);
  padding:12px 14px;display:flex;justify-content:space-between;align-items:center;
  margin-bottom:10px;transition:border-color .25s;
}
.otp-box:hover{border-color:var(--acc)}
.otp-val{
  font-family:var(--mono);font-size:1.65rem;font-weight:700;letter-spacing:4px;
  background:linear-gradient(135deg,var(--grn),#67e8f9);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
}
.copy-btn{
  background:rgba(0,255,136,.08);color:var(--grn);
  border:1px solid rgba(0,255,136,.2);border-radius:7px;
  padding:6px 12px;font-size:.69rem;font-weight:800;font-family:var(--mono);
  cursor:pointer;transition:.2s;letter-spacing:.5px;
}
.copy-btn:hover{background:rgba(0,255,136,.18);transform:scale(1.05)}
.copy-btn.ok{background:rgba(0,255,136,.25);color:#fff}

/* Message preview */
.card-sender{font-size:.7rem;font-weight:700;color:var(--acc);margin-bottom:4px}
.card-msg{
  font-size:.72rem;color:var(--sub);line-height:1.5;
  border-top:1px solid var(--bdr);padding-top:10px;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
}

/* ── EMPTY STATE ── */
.empty{
  grid-column:1/-1;text-align:center;padding:80px 20px;
}
.empty-icon{font-size:3.5rem;margin-bottom:16px;opacity:.2;display:block}
.empty-text{font-size:1rem;color:var(--sub);margin-bottom:8px}
.empty-hint{font-size:.82rem;color:var(--dim)}

/* ── PAGINATION ── */
.pagination{
  display:flex;justify-content:center;align-items:center;gap:8px;
  padding-top:24px;border-top:1px solid var(--bdr);margin-top:8px;
}
.pag-btn{
  background:var(--s2);color:var(--txt);border:1px solid var(--bdr);
  width:36px;height:36px;border-radius:8px;cursor:pointer;font-size:.85rem;font-weight:700;
  transition:.2s;display:grid;place-items:center;
}
.pag-btn:hover{border-color:var(--acc);color:var(--acc)}
.pag-btn.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.pag-btn:disabled{opacity:.3;pointer-events:none}
.pag-info{font-size:.75rem;color:var(--dim);font-family:var(--mono);padding:0 12px}

/* ── TOAST ── */
.toast{
  position:fixed;bottom:28px;right:28px;z-index:999;
  background:linear-gradient(135deg,var(--grn),#00c9ff);
  color:#000;font-weight:800;padding:12px 22px;border-radius:12px;
  font-size:.82rem;font-family:var(--mono);
  opacity:0;transform:translateY(24px);transition:.3s;pointer-events:none;
  box-shadow:0 8px 28px rgba(0,255,136,.4);letter-spacing:.3px;
}
.toast.show{opacity:1;transform:translateY(0)}

/* ── FOOTER ── */
footer{
  position:relative;z-index:1;text-align:center;
  padding:32px 24px;color:var(--dim);font-size:.76rem;
  border-top:1px solid var(--bdr);
}
footer a{color:var(--acc);text-decoration:none;transition:.2s}
footer a:hover{color:var(--acc2)}
.dev-badge{
  display:inline-flex;align-items:center;gap:6px;
  background:var(--s2);border:1px solid var(--bdr);border-radius:8px;
  padding:6px 14px;margin-top:10px;font-family:var(--mono);font-size:.75rem;
}
.dev-badge span{color:var(--acc2);font-weight:700}

/* ── RESPONSIVE ── */
@media(max-width:768px){
  .topbar{padding:0 16px}
  .wrap{padding:20px 12px 48px}
  .chart-row{grid-template-columns:1fr}
  .stats-grid{grid-template-columns:repeat(2,1fr)}
  .otp-grid{grid-template-columns:1fr}
  .controls{flex-direction:column}
  .search-wrap,.ctrl-sel{width:100%}
}

/* ── SCAN LINE ANIMATION ── */
@keyframes scan{0%{transform:translateY(-100%)} 100%{transform:translateY(100vh)}}
.scan-line{
  position:fixed;left:0;right:0;height:2px;z-index:0;pointer-events:none;
  background:linear-gradient(90deg,transparent,rgba(108,71,255,.3),transparent);
  animation:scan 8s linear infinite;
}

/* Number counter animation */
@keyframes count-up{from{opacity:0;transform:translateY(8px)} to{opacity:1;transform:none}}
.stat-val{animation:count-up .6s ease both}
</style>
</head>
<body>

<!-- Background effects -->
<canvas id="particles"></canvas>
<div class="orb orb1"></div>
<div class="orb orb2"></div>
<div class="orb orb3"></div>
<div class="scan-line"></div>

<!-- TOPBAR -->
<header class="topbar">
  <a class="logo" href="/">
    <div class="logo-mark">🔐</div>
    <span class="logo-text">CRACK SMS</span>
  </a>
  <div class="topbar-right">
    <div class="pill pill-live" id="livePill">LIVE FEED</div>
    <div class="pill pill-count" id="countPill">— OTPs</div>
    <button class="btn-docs" onclick="location.href='/api/docs'">⚡ API Docs</button>
  </div>
</header>

<div class="wrap">

  <!-- STATS -->
  <div class="sh"><div class="sh-line"></div><span class="sh-text">📊 Real-Time Overview</span><div class="sh-line"></div></div>
  <div class="stats-grid">
    <div class="stat">
      <div class="stat-icon">🔑</div>
      <div class="stat-val" id="sTotal">—</div>
      <div class="stat-label">Total OTPs</div>
      <div class="stat-delta" id="sDelta">Loading…</div>
    </div>
    <div class="stat g">
      <div class="stat-icon">📅</div>
      <div class="stat-val" id="sToday">—</div>
      <div class="stat-label">OTPs Today</div>
    </div>
    <div class="stat y">
      <div class="stat-icon">🏆</div>
      <div class="stat-val" id="sTop">—</div>
      <div class="stat-label">Top Service</div>
    </div>
    <div class="stat c">
      <div class="stat-icon">⚡</div>
      <div class="stat-val" id="sHour">—</div>
      <div class="stat-label">Last Hour</div>
    </div>
  </div>

  <!-- CHARTS -->
  <div class="sh"><div class="sh-line"></div><span class="sh-text">📈 Analytics</span><div class="sh-line"></div></div>
  <div class="chart-row">
    <div class="chart-card">
      <div class="chart-title">⏱ OTP Volume — Last 24 Hours</div>
      <canvas id="lineChart"></canvas>
    </div>
    <div class="chart-card">
      <div class="chart-title">🎯 Services Breakdown</div>
      <canvas id="donutChart"></canvas>
    </div>
  </div>

  <!-- CONTROLS -->
  <div class="sh"><div class="sh-line"></div><span class="sh-text">📋 Live Feed</span><div class="sh-line"></div></div>
  <div class="controls">
    <div class="search-wrap">
      <span class="search-icon">🔍</span>
      <input id="q" placeholder="Search number, OTP, service, country…" oninput="render()">
    </div>
    <select id="sf" class="ctrl-sel" onchange="render()">
      <option value="">All Services</option>
    </select>
    <button class="ctrl-btn" onclick="load()">🔄 Refresh</button>
    <button class="ctrl-btn accent" onclick="exportCSV()">📥 Export CSV</button>
  </div>

  <!-- OTP GRID -->
  <div class="otp-grid" id="grid">
    <div class="empty">
      <span class="empty-icon">⏳</span>
      <div class="empty-text">Connecting to live feed…</div>
    </div>
  </div>

  <!-- PAGINATION -->
  <div id="pagBox" style="display:none">
    <div class="pagination">
      <button class="pag-btn" id="prevBtn" onclick="prevPage()">←</button>
      <span class="pag-info" id="pagInfo">1 / 1</span>
      <button class="pag-btn" id="nextBtn" onclick="nextPage()">→</button>
    </div>
  </div>

</div><!-- /wrap -->

<footer>
  <div>
    CRACK SMS &copy; 2024 &nbsp;·&nbsp; Dev: <a href="https://t.me/NONEXPERTCODER" target="_blank">@NONEXPERTCODER</a>–2026 &nbsp;|&nbsp;
    <a href="/api/docs">⚡ API Docs</a> &nbsp;|&nbsp;
    <a href="/health">💚 Health</a> &nbsp;|&nbsp;
    <a href="https://t.me/crackotp" target="_blank">📢 Channel</a>
  </div>
  <div class="dev-badge">
    ⚡ Developed by <a href="https://t.me/NONEXPERTCODER" target="_blank"><span>@NONEXPERTCODER</span></a>
  </div>
</footer>

<div class="toast" id="toast"></div>

<script>
/* ── PARTICLE ENGINE ── */
(function(){
  const c=document.getElementById('particles');
  const ctx=c.getContext('2d');
  let W,H,pts=[];
  function resize(){W=c.width=innerWidth;H=c.height=innerHeight;init()}
  function init(){
    pts=[];
    const n=Math.min(80,Math.floor(W*H/18000));
    for(let i=0;i<n;i++) pts.push({
      x:Math.random()*W,y:Math.random()*H,
      vx:(Math.random()-.5)*.4,vy:(Math.random()-.5)*.4,
      r:Math.random()*1.8+.4,
      c:`hsla(${[260,310,190][Math.floor(Math.random()*3)]},80%,70%,`
    });
  }
  function draw(){
    ctx.clearRect(0,0,W,H);
    pts.forEach(p=>{
      p.x+=p.vx;p.y+=p.vy;
      if(p.x<0||p.x>W)p.vx*=-1;
      if(p.y<0||p.y>H)p.vy*=-1;
      ctx.beginPath();ctx.arc(p.x,p.y,p.r,0,Math.PI*2);
      ctx.fillStyle=p.c+'.7)';ctx.fill();
    });
    /* connections */
    for(let i=0;i<pts.length;i++){
      for(let j=i+1;j<pts.length;j++){
        const dx=pts[i].x-pts[j].x,dy=pts[i].y-pts[j].y;
        const d=Math.sqrt(dx*dx+dy*dy);
        if(d<120){
          ctx.beginPath();ctx.moveTo(pts[i].x,pts[i].y);ctx.lineTo(pts[j].x,pts[j].y);
          ctx.strokeStyle=`rgba(108,71,255,${(1-d/120)*.15})`;
          ctx.lineWidth=.6;ctx.stroke();
        }
      }
    }
    requestAnimationFrame(draw);
  }
  window.addEventListener('resize',resize);resize();draw();
})();

/* ── APP STATE ── */
let all=[],seen=new Set(),page=1,PER=12;
let lineChart=null,donutChart=null;

/* ── INIT ── */
window.onload=()=>{
  initCharts();
  load();
  loadStats();
  setInterval(load,4000);
  setInterval(loadStats,30000);
};

/* ── DATA FETCH ── */
async function load(){
  try{
    const r=await fetch('/api/public/otps?limit=1000');
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d=await r.json();
    if(d.status==='success'){
      const data=d.data||[];
      const isNew=o=>!seen.has(o.number+o.received_at);
      const newKeys=new Set(data.filter(isNew).map(o=>o.number+o.received_at));
      data.forEach(o=>seen.add(o.number+o.received_at));
      all=data;
      const cp=document.getElementById('countPill');
      if(cp) cp.textContent=all.length+' OTPs';
      buildFilter();
      if(newKeys.size>0) page=1;
      render(newKeys);
    } else {
      showErr('⚠️ API Error: '+(d.message||'unknown'),'Check api_server.py logs.');
    }
  }catch(e){
    console.error('load error:',e);
    showErr('🔌 Cannot reach API','Make sure api_server.py is running. '+e.message);
  }
}
function showErr(title,hint){
  const g=document.getElementById('grid');
  if(g) g.innerHTML='<div class="empty"><span class="empty-icon"></span>'
    +'<div class="empty-text">'+title+'</div>'
    +'<div class="empty-hint">'+hint+'</div></div>';
  const pb=document.getElementById('pagBox'); if(pb) pb.style.display='none';
}

async function loadStats(){
  try{
    const r=await fetch('/api/public/stats');
    const d=await r.json();
    if(d.status==='success'){
      animNum('sTotal',d.total_otps);
      animNum('sToday',d.otps_today);
      const top=Object.entries(d.by_service||{}).sort((a,b)=>b[1]-a[1])[0];
      document.getElementById('sTop').textContent=top?top[0].slice(0,8):'—';
      const now=new Date(),bk=`${now.getFullYear()}-${p2(now.getMonth()+1)}-${p2(now.getDate())} ${p2(now.getHours())}:00`;
      animNum('sHour',d.hourly_last_24h?.[bk]||0);
      document.getElementById('sDelta').textContent=`↑ ${d.otps_today} today`;
      updateCharts(d);
    }
  }catch(e){console.error(e)}
}

function animNum(id,target){
  const el=document.getElementById(id);
  const start=parseInt(el.textContent)||0;
  if(start===target)return;
  const dur=600,step=16;
  let cur=start,inc=(target-start)/(dur/step);
  const t=setInterval(()=>{
    cur+=inc;
    if((inc>0&&cur>=target)||(inc<0&&cur<=target)){cur=target;clearInterval(t)}
    el.textContent=Math.round(cur).toLocaleString();
  },step);
}

/* ── RENDER ── */
function render(newKeys=new Set()){
  const q=document.getElementById('q').value.toLowerCase();
  const sf=document.getElementById('sf').value;
  let filtered=all.filter(o=>{
    if(sf&&o.service!==sf)return false;
    if(q&&!`${o.number}${o.otp}${o.service}${o.country}`.toLowerCase().includes(q))return false;
    return true;
  });
  const g=document.getElementById('grid');
  if(!filtered.length){
    g.innerHTML=`<div class="empty">
      <span class="empty-icon">📭</span>
      <div class="empty-text">No OTPs match your filter</div>
      <div class="empty-hint">Try clearing the search or changing the service</div>
    </div>`;
    document.getElementById('pagBox').style.display='none';
    return;
  }
  const totalPages=Math.ceil(filtered.length/PER);
  if(page>totalPages)page=totalPages;
  const slice=filtered.slice((page-1)*PER,page*PER);
  g.innerHTML=slice.map((o,i)=>{
    const isN=newKeys.has(o.number+o.received_at);
    const fmt=s=>{if(!s||s==='—')return s;if(s.length===6)return s.slice(0,3)+'-'+s.slice(3);return s};
    return`<div class="otp-card" style="animation-delay:${i*.04}s">
      ${isN?'<span class="new-badge">✦ NEW</span>':''}
      <div class="card-top">
        <span class="svc-tag">${e(o.service)}</span>
        <span class="card-ts">${e(o.received_at.slice(11))}</span>
      </div>
      <div class="card-num">📱 ${e(o.number)}</div>
      <div class="card-country">📍 ${e(o.country)}</div>
      <div class="otp-box">
        <span class="otp-val">${fmt(e(o.otp))}</span>
        <button class="copy-btn" onclick="cpOtp('${e(o.otp)}',this)">📋 COPY</button>
      </div>
      <div class="card-sender">📤 ${e(o.service)}</div>
      <div class="card-msg">${e(o.message)}</div>
    </div>`;
  }).join('');
  /* pagination */
  if(totalPages>1){
    document.getElementById('pagBox').style.display='block';
    document.getElementById('pagInfo').textContent=`${page} / ${totalPages}`;
    document.getElementById('prevBtn').disabled=page===1;
    document.getElementById('nextBtn').disabled=page===totalPages;
  }else{
    document.getElementById('pagBox').style.display='none';
  }
}
function prevPage(){if(page>1){page--;render()}}
function nextPage(){page++;render()}

/* ── FILTER ── */
function buildFilter(){
  const sel=document.getElementById('sf');const cur=sel.value;
  const svcs=[...new Set(all.map(o=>o.service))].sort();
  sel.innerHTML='<option value="">All Services</option>'+svcs.map(s=>`<option${s===cur?' selected':''}>${e(s)}</option>`).join('');
}

/* ── CHARTS ── */
function initCharts(){
  const fg='#7b7fa8',grid='rgba(30,32,53,.8)';
  Chart.defaults.color=fg;Chart.defaults.font.family="'JetBrains Mono'";
  lineChart=new Chart(document.getElementById('lineChart'),{
    type:'line',
    data:{labels:[],datasets:[{
      label:'OTPs',data:[],
      borderColor:'#6c47ff',
      backgroundColor:'rgba(108,71,255,.12)',
      fill:true,tension:.45,pointRadius:5,pointHoverRadius:9,
      pointBackgroundColor:'#6c47ff',borderWidth:2.5,
    }]},
    options:{responsive:true,maintainAspectRatio:true,
      plugins:{legend:{display:false}},
      scales:{
        x:{grid:{color:grid},ticks:{maxTicksLimit:8,color:fg}},
        y:{grid:{color:grid},beginAtZero:true,ticks:{precision:0,color:fg}},
      },animation:{duration:400}},
  });
  donutChart=new Chart(document.getElementById('donutChart'),{
    type:'doughnut',
    data:{labels:[],datasets:[{data:[],
      backgroundColor:['#6c47ff','#ff3cac','#00e5ff','#ffd600','#00ff88','#ff4444','#a855f7','#22d3ee'],
      borderWidth:3,borderColor:'#11121c',
    }]},
    options:{responsive:true,maintainAspectRatio:true,
      plugins:{legend:{position:'bottom',labels:{boxWidth:12,padding:14,color:fg,font:{size:11}}}},
      cutout:'68%',animation:{duration:400}},
  });
}
function updateCharts(d){
  if(!lineChart||!donutChart)return;
  const h=d.hourly_last_24h||{},keys=Object.keys(h).sort().slice(-24);
  lineChart.data.labels=keys.map(k=>k.slice(11,16));
  lineChart.data.datasets[0].data=keys.map(k=>h[k]);
  lineChart.update('none');
  const sv=d.by_service||{},top=Object.entries(sv).sort((a,b)=>b[1]-a[1]).slice(0,7);
  donutChart.data.labels=top.map(x=>x[0]);
  donutChart.data.datasets[0].data=top.map(x=>x[1]);
  donutChart.update('none');
}

/* ── COPY ── */
function cpOtp(otp,btn){
  if(!otp||otp==='—')return;
  navigator.clipboard.writeText(otp.replace('-','')).then(()=>{
    btn.textContent='✓ Copied!';btn.classList.add('ok');
    toast('OTP copied!');
    setTimeout(()=>{btn.textContent='📋 COPY';btn.classList.remove('ok')},2000);
  });
}

/* ── EXPORT ── */
function exportCSV(){
  if(!all.length){toast('No OTPs to export');return}
  const rows=[['Service','Number','Country','OTP','Message','Received At'],
    ...all.slice(0,500).map(o=>[o.service,o.number,o.country,o.otp,o.message,o.received_at])];
  const csv=rows.map(r=>r.map(v=>`"${String(v).replace(/"/g,'""')}"`).join(',')).join('\n');
  const a=Object.assign(document.createElement('a'),{
    href:URL.createObjectURL(new Blob([csv],{type:'text/csv'})),
    download:`crack-sms-otps-${new Date().toISOString().slice(0,10)}.csv`,
  });
  a.click();toast('Exported '+all.length+' OTPs');
}

/* ── TOAST ── */
function toast(m){
  const t=document.getElementById('toast');
  t.textContent=m;t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),2800);
}

/* ── UTILS ── */
function e(s){const d=document.createElement('div');d.textContent=String(s||'');return d.innerHTML}
function p2(n){return String(n).padStart(2,'0')}
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
