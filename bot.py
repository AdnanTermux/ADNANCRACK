import logging, asyncio, re, aiohttp, json, os, random, html, sqlite3, time
try:
    import psutil
except ImportError:
    psutil = None
import hashlib, ssl, websockets, subprocess, shutil, uuid, unicodedata
from types import SimpleNamespace
from urllib.parse import urljoin, urlparse
import phonenumbers
from phonenumbers import geocoder
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CopyTextButton,
    Bot,
    Message,
    CallbackQuery,
)

# ══════════════════════════════════════════════════════════════════
#  BUTTON ENGINE — animated emoji icons + colored buttons
#  Bot API 9.4+: icon_custom_emoji_id = animated icon on button
#                style = "primary"|"success"|"danger" colored button
#  Older clients: graceful fallback, static emoji in label
# ══════════════════════════════════════════════════════════════════

def _btn_clean(text: str) -> str:
    """Strip tg-emoji HTML keeping fallback char. Strip all other HTML.
    Telegram button labels are plain text — HTML never renders there."""
    s = re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', r'\1', str(text or ''), flags=re.DOTALL)
    return re.sub(r'<[^>]+>', '', s).strip() or '•'

_OrigIKB = InlineKeyboardButton

def InlineKeyboardButton(text='', *, callback_data=None, url=None,
                          style=None, icon_custom_emoji_id=None,
                          copy_text=None, switch_inline_query=None,
                          switch_inline_query_current_chat=None,
                          switch_inline_query_chosen_chat=None,
                          web_app=None, login_url=None, pay=False, **kw):
    """
    Drop-in PTB InlineKeyboardButton wrapper.
    • Strips <tg-emoji> tags from button text (keeps static fallback emoji) ✅
    • icon_custom_emoji_id → animated emoji icon on button (Bot API 9.4+) ✅
    • style= → colored button on supported clients ✅
    • Graceful TypeError fallback for older PTB versions ✅
    """
    resolver = globals().get("_resolve_button_payload")
    if callable(resolver):
        clean, icon_custom_emoji_id = resolver(text, icon_custom_emoji_id)
    else:
        clean = _btn_clean(text)
    kw2 = {}
    if callback_data is not None:                    kw2['callback_data'] = callback_data
    if url is not None:                              kw2['url'] = url
    if copy_text is not None:                        kw2['copy_text'] = copy_text
    if switch_inline_query is not None:              kw2['switch_inline_query'] = switch_inline_query
    if switch_inline_query_current_chat is not None: kw2['switch_inline_query_current_chat'] = switch_inline_query_current_chat
    if switch_inline_query_chosen_chat is not None:  kw2['switch_inline_query_chosen_chat'] = switch_inline_query_chosen_chat
    if web_app is not None:                          kw2['web_app'] = web_app
    if login_url is not None:                        kw2['login_url'] = login_url
    if pay:                                          kw2['pay'] = pay
    if icon_custom_emoji_id:                         kw2['icon_custom_emoji_id'] = str(icon_custom_emoji_id)
    if style:                                        kw2['style'] = style
    kw2.update({k: v for k, v in kw.items() if k not in kw2})
    try:
        return _OrigIKB(clean, **kw2)
    except TypeError:
        # Older PTB — strip unknown fields gracefully
        safe = {k: v for k, v in kw2.items()
                if k in ('callback_data','url','copy_text','switch_inline_query',
                          'switch_inline_query_current_chat','switch_inline_query_chosen_chat',
                          'web_app','login_url','pay')}
        return _OrigIKB(clean, **safe)

from telegram.ext import (ApplicationBuilder, ContextTypes, CommandHandler,
                           MessageHandler, CallbackQueryHandler, filters)
from telegram.error import BadRequest as TelegramBadRequest, Forbidden as TelegramForbidden, TimedOut as TelegramTimedOut, NetworkError as TelegramNetworkError
from sqlalchemy import text as stext, select, delete, func
sfunc = func  # alias used in stat queries
from sqlalchemy.ext.asyncio import AsyncSession

import database as db
from utils import to_bold
import bot_manager as bm

# Load configuration
try:
    from bot_config import get_button_style, get_button_text, is_feature_enabled, get_timeout, get_limit, get_message
except ImportError:
    # Fallback if bot_config not available
    def get_button_style(x="primary"): return "primary"
    def get_button_text(x, default=""): return default
    def is_feature_enabled(x): return True
    def get_timeout(x): return 10
    def get_limit(x): return 1
    def get_message(x): return ""

# ── Optional: use logging_system for enhanced logging ──
try:
    from logging_system import audit_otp as _audit_otp, audit_api as _audit_api
except ImportError:
    async def _audit_otp(**kw): pass
    async def _audit_api(**kw): pass


# ═══════════════════════════════════════════════════════════
#  CRACK SMS v20 — TELEGRAM PROFESSIONAL EDITION
#  Advanced Telegram OTP Bot with Pro-Level UI & Dynamic Themes
#  Features: Admin Panel • Multi-panel • Child Bots • 15 OTP Themes
#  Pro Features: Style-Buttons • Unique GUIs • Advanced Analytics
# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
#  LOGGING
# ═══════════════════════════════════════════════════════════
def _strip_log_markup(text: str) -> str:
    plain = re.sub(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", r"\1", str(text or ""), flags=re.DOTALL)
    return re.sub(r"</?[^>]+>", "", plain)


class PlainTextFormatter(logging.Formatter):
    def format(self, record):
        clone = logging.makeLogRecord(record.__dict__.copy())
        clone.msg = _strip_log_markup(clone.getMessage())
        clone.args = ()
        return super().format(clone)


class EmojiFormatter(logging.Formatter):
    """Enhanced emoji formatter with color support for console output."""
    EMOJIS = {
        logging.DEBUG:    "🔍 DEBUG",
        logging.INFO:     "✅  INFO ",
        logging.WARNING:  "⚠️   WARN ",
        logging.ERROR:    "❌ ERROR",
        logging.CRITICAL: "🔥 CRIT ",
    }
    COLORS = {
        logging.DEBUG:    "\033[36m",      # Cyan
        logging.INFO:     "\033[32m",      # Green
        logging.WARNING:  "\033[33m",      # Yellow
        logging.ERROR:    "\033[31m",      # Red
        logging.CRITICAL: "\033[95m",      # Magenta
    }
    RESET = "\033[0m"
    
    def format(self, record):
        label = self.EMOJIS.get(record.levelno, "❓ ?????")
        ts    = self.formatTime(record, "%H:%M:%S")
        msg   = _strip_log_markup(record.getMessage())
        color = self.COLORS.get(record.levelno, "")
        
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        
        return f"{ts} {color}│ {label} │{self.RESET} {msg}"

# ── File handler keeps plain text (easier to grep) ──────────────
_file_fmt    = PlainTextFormatter("%(asctime)s | %(levelname)-8s | %(message)s")
_file_h      = logging.FileHandler("bot.log", encoding="utf-8")
_file_h.setFormatter(_file_fmt)

# ── Console handler gets emoji-rich output ───────────────────────
_console_h   = logging.StreamHandler()
_console_h.setFormatter(EmojiFormatter())

logging.basicConfig(level=logging.INFO, handlers=[_file_h, _console_h])

# Silence noisy third-party loggers
for _noisy in ("httpx","httpcore","telegram.ext","apscheduler","aiohttp"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
#  RATE LIMITING & QUOTA MANAGEMENT
# ═══════════════════════════════════════════════════════════
from collections import defaultdict
import time as time_module

USER_RATE_LIMITS = defaultdict(lambda: {"count": 0, "reset_at": 0})
USER_COMMAND_HISTORY = defaultdict(list)
COMMAND_COOLDOWNS = {"get_number": 2, "copy_otp": 1, "send_broadcast": 30}

def check_rate_limit(uid: int, command: str = "default", limit: int = 10, window: int = 60) -> tuple:
    """
    Check if user is rate limited.
    Returns: (allowed: bool, remaining: int, reset_in: int_seconds)
    """
    now = time_module.time()
    key = f"{uid}_{command}"
    limiter = USER_RATE_LIMITS[key]
    
    if now >= limiter["reset_at"]:
        limiter["count"] = 0
        limiter["reset_at"] = now + window
    
    allowed = limiter["count"] < limit
    remaining = max(0, limit - limiter["count"])
    reset_in = max(0, int(limiter["reset_at"] - now))
    
    if allowed:
        limiter["count"] += 1
    
    return allowed, remaining, reset_in

def rate_limit_decorator(limit: int = 10, window: int = 60):
    """Decorator to rate limit command handlers."""
    def decorator(func):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            uid = update.effective_user.id
            allowed, remaining, reset_in = check_rate_limit(uid, func.__name__, limit, window)
            
            if not allowed:
                msg = f"{ui('clock')} <code>Rate limit exceeded!</code>\n{ui('clock')} Try again in {reset_in}s"
                try:
                    await update.message.reply_text(msg, parse_mode="HTML")
                except:
                    pass
                logger.warning(f"{ui('cancel')} Rate limit: {uid} | {func.__name__}")
                return
            
            return await func(update, context)
        return wrapper
    return decorator

# ═══════════════════════════════════════════════════════════
#  ERROR HANDLING & RESILIENCE
# ═══════════════════════════════════════════════════════════
def safe_handler_decorator(func):
    """Decorator to safely handle errors in command handlers."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await func(update, context)
        except TelegramForbidden:
            logger.warning(f"{ui('info')} Bot blocked by user {update.effective_user.id}")
        except TelegramTimedOut:
            logger.warning(f"{ui('clock')} Telegram timeout for {update.effective_user.id} | {func.__name__}")
            try:
                await update.message.reply_text(f"{ui('clock')} Request timed out. Please try again.", parse_mode="HTML")
            except:
                pass
        except Exception as e:
            logger.error(f"{ui('cancel')} Error in {func.__name__}: {e}", exc_info=True)
            try:
                msg = f"{ui('cancel')} Error: {type(e).__name__}\n\n<i>Contact support if this persists.</i>"
                await update.message.reply_text(msg, parse_mode="HTML")
            except:
                pass
    return wrapper

# ═══════════════════════════════════════════════════════════
#  INPUT VALIDATION & SECURITY
# ═══════════════════════════════════════════════════════════
def validate_input(text: str, min_len: int = 1, max_len: int = 4096, 
                  allowed_chars: str = None) -> tuple:
    """
    Validate user input.
    Returns: (valid: bool, error_msg: str)
    """
    if not text:
        return False, "Input cannot be empty"
    
    if len(text) < min_len:
        return False, f"Input too short (min {min_len} chars)"
    
    if len(text) > max_len:
        return False, f"Input too long (max {max_len} chars)"
    
    if allowed_chars and not all(c in allowed_chars for c in text):
        return False, f"Invalid characters in input"
    
    return True, ""

def sanitize_html_input(text: str) -> str:
    """Sanitize user input for HTML parsing."""
    return html.escape(text).replace("\n", "<br>")

def is_valid_user_id(uid: str) -> bool:
    """Validate Telegram user ID format."""
    try:
        user_id = int(uid)
        return -9999999999999 <= user_id <= 9999999999999
    except:
        return False

def is_valid_chat_id(cid: str) -> bool:
    """Validate Telegram chat ID format."""
    return is_valid_user_id(cid)

# ═══════════════════════════════════════════════════════════
#  PERFORMANCE OPTIMIZATION & CACHING
# ═══════════════════════════════════════════════════════════
SIMPLE_CACHE = {}
CACHE_EXPIRY = {}

def cache_get(key: str, default=None):
    """Get value from cache if not expired."""
    now = time_module.time()
    if key in SIMPLE_CACHE:
        if key in CACHE_EXPIRY and CACHE_EXPIRY[key] > now:
            return SIMPLE_CACHE[key]
        else:
            del SIMPLE_CACHE[key]
    return default

def cache_set(key: str, value, ttl_seconds: int = 300):
    """Set cache with TTL."""
    SIMPLE_CACHE[key] = value
    CACHE_EXPIRY[key] = time_module.time() + ttl_seconds
    logger.debug(f"{ui('copy')} Cache set: {key} (TTL: {ttl_seconds}s)")

def cache_clear():
    """Clear all expired cache entries."""
    now = time_module.time()
    expired = [k for k, exp in CACHE_EXPIRY.items() if exp <= now]
    for k in expired:
        SIMPLE_CACHE.pop(k, None)
        CACHE_EXPIRY.pop(k, None)
    if expired:
        logger.debug(f"{ui('trash')} Cleared {len(expired)} expired cache entries")

# ═══════════════════════════════════════════════════════════
#  PERFORMANCE METRICS & MONITORING
# ═══════════════════════════════════════════════════════════
PERF_METRICS = {"command_times": defaultdict(list), "errors": defaultdict(int)}

def log_performance(command_name: str, duration_ms: float):
    """Log command execution performance."""
    PERF_METRICS["command_times"][command_name].append(duration_ms)
    # Keep only last 100 measurements per command
    if len(PERF_METRICS["command_times"][command_name]) > 100:
        PERF_METRICS["command_times"][command_name] = PERF_METRICS["command_times"][command_name][-100:]

def get_performance_stats(command_name: str = None) -> dict:
    """Get performance statistics."""
    if command_name:
        times = PERF_METRICS["command_times"].get(command_name, [])
        if not times:
            return {}
        return {
            "min": min(times),
            "max": max(times),
            "avg": sum(times) / len(times),
            "count": len(times),
        }
    
    stats = {}
    for cmd, times in PERF_METRICS["command_times"].items():
        if times:
            stats[cmd] = {
                "avg": sum(times) / len(times),
                "count": len(times),
            }
    return stats

# ═══════════════════════════════════════════════════════════
#  ANIMATED EMOJI IDs  (Telegram Premium custom emoji)
#  All emojis are animated for Premium users, static for others.
#  Get IDs for any emoji via @getidsbot on Telegram.
# ═══════════════════════════════════════════════════════════

# Telegram Emoji IDs for country flags (animated, optional for Premium users)
COUNTRY_EMOJI_ID = {
    "UA":"5222250679371839695","US":"5224321781321442532","PL":"5224670399521892983",
    "KZ":"5222276376161171525","AZ":"5224426544163728284","EU":"5222108911091331711",
    "UN":"5451772687993031127","AM":"5224369957969603463","RU":"5280582975270963511",
    "CN":"5224435456220868088","UZ":"5222404546575219535","DE":"5222165617544542414",
    "JP":"5222390089715299207","TR":"5224601903383457698","BY":"5280820319458707404",
    "GB":"5224518800061245598","IN":"5222300011366200403","BR":"5224688610183228070",
    "VN":"5222359651282071925","AE":"5224565851427976312","TH":"5224638530864556281",
    "TZ":"5224397364155923150","TJ":"5222217865821696536","CH":"5224707263226194753",
    "SE":"5222201098269373561","ES":"5222024776976970940","KR":"5222345550904439270",
    "ZA":"5224696216570309138","RS":"5222145396838512729","SA":"5224698145010624573",
    "QA":"5222225596762830469","PT":"5224404094369672274","PH":"5222065042295376892",
    "PE":"5224482026551258766","PK":"5224637061985742245","OM":"5222396686785066306",
    "NO":"5224465228934163949","NG":"5224723614166691638","NZ":"5224573595254009705",
    "NL":"5224516489368841614","NP":"5222444378101925267","MA":"5224530035695693965",
    "MX":"5221971386238514431","MY":"5224312886444174057","KE":"5222089648163009103",
    "IQ":"5221980268230882832","IR":"5224374154152653367","ID":"5224405893960969756",
    "HU":"5224691998912427164","GR":"5222463490706389920","GH":"5224511339703056124",
    "GE":"5222152195771742239","FR":"5222029789203804982","FI":"5224282903277482188",
    "ET":"5224467805914542024","EE":"5222195463272281351","EG":"5222161185138292290",
    "DK":"5222297215342490217","CZ":"5222073533445714675","CO":"5224455152940886669",
    "CL":"5222350726340032308","CA":"5222001124592071204","BG":"5222092074819530668",
    "BE":"5224513182244024630","BD":"5224407289825340729","BH":"5224492892818518587",
    "AU":"5224659803837574114","AR":"5221980461504411710","DZ":"5224260376174015500",
    "AL":"5224312057515486246","AF":"5222096009009575868","ZW":"5222060442385397848",
    "VE":"5294476442854247878","LB":"5222244425899455269","LV":"5224401229626484931",
    "LT":"5224245902134226386","KG":"5224388147156102493","KW":"5221949726718442491",
    "JO":"5222292177345853436","IT":"5222460101977190141","IL":"5224720599099648709",
    "IE":"5224257017509588818","RO":"5222273794885826118","UA_2":"5280587278828193324",
    "DEFAULT":"5222250679371839695",
}

APP_EMOJI_ID = {
    "whatsapp":  "5334998226636390258",
    "telegram":  "5330237710655306682",
    "instagram": "5319160079465857105",
    "facebook":  "5323261730283863478",
    "google":    "5359758030198031389",
    "gmail":     "5359758030198031389",
    "twitter":   "5330337435500951363",
    "tiktok":    "5327982530702359565",
    "snapchat":  "5330248916224983855",
    "binance":   "5359437015752401733",
    "DEFAULT":   "5373026167722876724",
}

SERVICE_HASHTAGS = {
    "whatsapp":"WS","telegram":"TG","instagram":"IG","facebook":"FB",
    "google":"GG","gmail":"GG","twitter":"TW","tiktok":"TT","snapchat":"SC",
    "netflix":"NF","amazon":"AM","paypal":"PP","binance":"BN","discord":"DC",
    "microsoft":"MS","yahoo":"YH","apple":"AP","spotify":"SP","uber":"UB",
    "bolt":"BL","careem":"CR","tinder":"TN","bumble":"BM","linkedin":"LI",
    "shopee":"SH","grab":"GR","gojek":"GJ","foodpanda":"FP","signal":"SG",
    "steam":"ST","twitch":"TC","viber":"VB","line":"LN","wechat":"WC",
}

def tg_emoji(emoji_id: str, fallback: str) -> str:
    """Return animated tg-emoji tag. Animated for Premium users; fallback for all."""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'

def country_flag_emoji(region: str) -> str:
    """Return animated country flag tg-emoji, or standard unicode flag fallback."""
    if not region or len(region) != 2:
        return tg_emoji(COUNTRY_EMOJI_ID["DEFAULT"], "🌍")
    eid = COUNTRY_EMOJI_ID.get(region.upper(), COUNTRY_EMOJI_ID["DEFAULT"])
    # Unicode flag fallback
    base = 127462 - ord("A")
    flag = chr(base + ord(region[0].upper())) + chr(base + ord(region[1].upper()))
    return tg_emoji(eid, flag)

def app_emoji(service_name: str) -> str:
    """Return animated app icon tg-emoji for a given service name."""
    key = service_name.lower().strip()
    for svc_key, eid in APP_EMOJI_ID.items():
        if svc_key != "DEFAULT" and svc_key in key:
            return tg_emoji(eid, "📱")
    return tg_emoji(APP_EMOJI_ID["DEFAULT"], "📱")


def app_icon_id(service_name: str) -> str:
    """Return the custom emoji id for a service icon."""
    key = (service_name or "").lower().strip()
    for svc_key, eid in APP_EMOJI_ID.items():
        if svc_key != "DEFAULT" and svc_key in key:
            return eid
    return APP_EMOJI_ID["DEFAULT"]

def app_emoji_by_code(svc_code: str) -> str:
    """Return animated app icon by short code like WS, TG, FB."""
    code_map = {
        "WS":"whatsapp","TG":"telegram","IG":"instagram","FB":"facebook",
        "GG":"google","TW":"twitter","TT":"tiktok","SC":"snapchat","BN":"binance",
        "DC":"discord","MS":"microsoft","YH":"yahoo","AP":"apple","SP":"spotify",
    }
    name = code_map.get(svc_code.upper(), "")
    if name and name in APP_EMOJI_ID:
        return tg_emoji(APP_EMOJI_ID[name], "📱")
    return tg_emoji(APP_EMOJI_ID["DEFAULT"], "📱")

# ═══════════════════════════════════════════════════════════
#  ANIMATED EMOJI DICTIONARIES — Service Icons & Country Flags
# ═══════════════════════════════════════════════════════════
APP_EMOJI_ID = {
    "whatsapp":  "5334998226636390258",
    "telegram":  "5330237710655306682",
    "instagram": "5319160079465857105",
    "facebook":  "5323261730283863478",
    "google":    "5359758030198031389",
    "gmail":     "5359758030198031389",
    "twitter":   "5330337435500951363",
    "tiktok":    "5327982530702359565",
    "snapchat":  "5330248916224983855",
    "binance":   "5359437015752401733",
    "DEFAULT":   "5373026167722876724",
}

COUNTRY_EMOJI_ID = {
    "UA":"5222250679371839695","US":"5224321781321442532","PL":"5224670399521892983",
    "KZ":"5222276376161171525","AZ":"5224426544163728284","EU":"5222108911091331711",
    "UN":"5451772687993031127","AM":"5224369957969603463","RU":"5280582975270963511",
    "CN":"5224435456220868088","UZ":"5222404546575219535","DE":"5222165617544542414",
    "JP":"5222390089715299207","TR":"5224601903383457698","BY":"5280820319458707404",
    "GB":"5224518800061245598","IN":"5222300011366200403","BR":"5224688610183228070",
    "VN":"5222359651282071925","AE":"5224565851427976312","TH":"5224638530864556281",
    "TZ":"5224397364155923150","TJ":"5222217865821696536","CH":"5224707263226194753",
    "SE":"5222201098269373561","ES":"5222024776976970940","KR":"5222345550904439270",
    "ZA":"5224696216570309138","RS":"5222145396838512729","SA":"5224698145010624573",
    "QA":"5222225596762830469","PT":"5224404094369672274","PH":"5222065042295376892",
    "PE":"5224482026551258766","PK":"5224637061985742245","OM":"5222396686785066306",
    "NO":"5224465228934163949","NG":"5224723614166691638","NZ":"5224573595254009705",
    "NL":"5224516489368841614","NP":"5222444378101925267","MA":"5224530035695693965",
    "MX":"5221971386238514431","MY":"5224312886444174057","KE":"5222089648163009103",
    "IQ":"5221980268230882832","IR":"5224374154152653367","ID":"5224405893960969756",
    "HU":"5224691998912427164","GR":"5222463490706389920","GH":"5224511339703056124",
    "GE":"5222152195771742239","FR":"5222029789203804982","FI":"5224282903277482188",
    "ET":"5224467805914542024","EE":"5222195463272281351","EG":"5222161185138292290",
    "DK":"5222297215342490217","CZ":"5222073533445714675","CO":"5224455152940886669",
    "CL":"5222350726340032308","CA":"5222001124592071204","BG":"5222092074819530668",
    "BE":"5224513182244024630","BD":"5224407289825340729","BH":"5224492892818518587",
    "AU":"5224659803837574114","AR":"5221980461504411710","DZ":"5224260376174015500",
    "AL":"5224312057515486246","AF":"5222096009009575868","ZW":"5222060442385397848",
    "VE":"5294476442854247878","LB":"5222244425899455269","LV":"5224401229626484931",
    "LT":"5224245902134226386","KG":"5224388147156102493","KW":"5221949726718442491",
    "JO":"5222292177345853436","IT":"5222460101977190141","IL":"5224720599099648709",
    "IE":"5224257017509588818","RO":"5222273794885826118","UA_2":"5280587278828193324",
    "DEFAULT":"5222250679371839695",
}

# Animated UI emoji shortcuts
_UI = {
    "fire":         ("5402406965252989103", "🔥"),
    "bolt":         ("5206263763124117710", "⚡️"),
    "crown":        ("5319149831673887746", "👑"),
    "diamond":      ("5235940101643883746", "💎"),
    "star":         ("5778458646534952216", "⭐️"),
    "key":          ("6176966310920983412", "🔑"),
    "lock":         ("5291873529464122510", "🔒"),
    "robot":        ("5339267587337370029", "🤖"),
    "shield":       ("5339163352776058483", "🛡"),
    "rocket":       ("5235575317191474172", "🚀"),
    "gear":         ("5330399283030013876", "⚙️"),
    "chart":        ("5343862721307748990", "📊"),
    "bell":         ("5391115831239253682", "🔔"),
    "skull":        ("5807631052251861399", "💀"),
    "zap":          ("5411590687663608498", "⚡️"),
    "check":        ("5778475783454463308", "✅"),
    "earth":        ("5224450179368767019", "🌍"),
    "phone":        ("5312310156384557787", "📱"),
    "chat":         ("5040036030414062506", "💬"),
    "speak":        ("5850449436651556295", "🗣"),
    "receiver":     ("6093587384954262033", "📞"),
    "satellite":    ("5352564488258200671", "📡"),
    "clock":        ("5805205259018048822", "🕐"),
    "pushpin":      ("5397782960512444700", "📍"),
    "snow":         ("5449449325434266744", "❄️"),
    "ice":          ("5814355643892503100", "🧊"),
    "megaphone":    ("6104927893912030655", "📢"),
    "document":     ("5258079129051356005", "📄"),
    "notepad":      ("5262974657329394511", "📝"),
    "scissors":     ("5235728424185649782", "✂️"),
    "laptop":       ("5321154224191462061", "💻"),
    "people":       ("5359735404426468588", "👥"),
    "globe":        ("5224450179368767019", "🌐"),
    "microscope":   ("5377580546748588396", "🔬"),
    "copy":         ("5197219609970758159", "📋"),
    "envelope":     ("5274102582585877844", "📩"),
    "dice":         ("5210701280384668714", "🎲"),
    "shield2":      ("5197288647275071607", "🎲"),
    "dev":          ("5215263059639017128", "🧑‍💻"),
    "focus":        ("5226851658792717025", "🎯"),
    "user":         ("5321154224191462061", "👤"),
    "book":         ("5411369574157286161", "📖"),
    "help":         ("5436113877181941026", "❓"),
    "back":         ("5255703720078879038", "🔙"),
    "link":         ("541784026880621073", "🔗"),
    "trash":        ("5445267414562389170", "🗑"),
    "cancel":       ("5974083768233760323", "❌"),
    "online":       ("5319310205752717294", "🟢"),
    "offline":      ("5319238892115733906", "🔴"),
    "info":         ("5467889436807157272", "ℹ️"),
    "play":         ("6265011645840363936", "▶️"),
    "stop":         ("5454380420336466255", "⏹️"),
    "refresh":      ("5359543311897998264", "🔄"),
    "settings":     ("5341715473882955310", "⚙️"),
    "announce":     ("5370599459661045441", "📢"),
    "next":         ("5253767677670862169", "👉"),
    "next2":        ("6158862632926319619", "👉"),
    "find":         ("5229102316145106683", "🧐"),
    "verify":       ("5776080909690213450", "✅"),
    "bot":          ("5355051922862653659", "🤖"),
    "phone2":       ("5318779098686826724", "📞"),
    "block":        ("5269694545980835074", "📞"),
    "add":          ("5397916757333654639", "➕"),
    "edit":         ("5395444784611480792", "✏️"),
    "wow":          ("5461151367559141950",  "🎉"),
    "question":     ("5251450740383170139", "❓"),
    "warn":         ("5447644880824181073", "⚠️"),
    "database":     ("5409260938488458240", "🗄"),
    "antenna":      ("5188592906163732955", "📡"),
    "tools":        ("5332272404167140865", "🔧"),
    "id":           ("6068610874522735901", "🆔"),
    "calendar":     ("5413879192267805083", "📅"),
    "plus":         ("5397916757333654639", "➕"),
    "desktop":      ("5030534970049823529", "🖥"),
    "users":        ("5848183076898737707", "👥"),
    "broadcast":    ("6104927893912030655", "📢"),
    "support":      ("5307746710682869587", "🛟"),
    "package":      ("6174801243676871772", "📦"),
    "clipboard":    ("5877618313139327986", "📋"),
    "xap":          ("5226570145161302027", "🔑"),
}
def ui(name: str) -> str:
    """Animated UI emoji by name."""
    if name in _UI: return tg_emoji(_UI[name][0], _UI[name][1])
    return "•"

def emoji(name: str, uid: int = None) -> str:
    """
    Smart emoji selector:
    - Main bot: Always use animated emoji
    - Child bot: Use animated emoji ONLY if:
      * User has enterprise tier
      * User has enabled animated emoji feature
      * Otherwise use static fallback
    
    uid: user_id (optional, override current user)
    """
    if not IS_CHILD_BOT:
        # Main bot always uses animated
        return ui(name)
    
    # Child bot: check user tier and preferences
    if uid and get_user_tier(uid) == "enterprise":
        # Check if user enabled animated emoji feature (stored in config)
        try:
            cfg = {}
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE) as f:
                    cfg = json.load(f)
            # New config section: child_bot_emoji_prefs
            emoji_prefs = cfg.get("child_bot_emoji_prefs", {})
            if emoji_prefs.get(str(uid), False):
                # User enabled animated emojis
                return ui(name)
        except:
            pass
    
    # Default: return static emoji fallback
    if name in _UI:
        return _UI[name][1]
    return "•"

def set_child_emoji_pref(uid: int, enabled: bool):
    """Set whether a child bot user wants animated emojis (enterprise only)."""
    if not IS_CHILD_BOT:
        return False
    
    if get_user_tier(uid) != "enterprise":
        return False  # Only enterprise users can enable this
    
    try:
        cfg = {}
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
        
        if "child_bot_emoji_prefs" not in cfg:
            cfg["child_bot_emoji_prefs"] = {}
        
        cfg["child_bot_emoji_prefs"][str(uid)] = enabled
        
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
        return True
    except:
        return False


_UI_ID_TO_NAME = {str(emoji_id): name for name, (emoji_id, _) in _UI.items()}
_UI_FALLBACK_TO_NAME = {}
for _name, (_emoji_id, _fallback) in _UI.items():
    _UI_FALLBACK_TO_NAME.setdefault(_fallback, _name)
    _UI_FALLBACK_TO_NAME.setdefault(_fallback.replace("\ufe0f", ""), _name)

_UI_FALLBACK_TO_NAME.update({
    "⚠": "warn",
    "⚠️": "warn",
    "⚡": "zap",
    "⚡️": "zap",
    "📢": "announce",
    "📋": "copy",
    "📩": "envelope",
    "📧": "envelope",
    "📞": "receiver",
    "📄": "document",
    "📂": "document",
    "🛟": "support",
    "📈": "chart",
    "📜": "book",
    "📤": "announce",
    "📥": "envelope",
    "💾": "database",
    "💚": "check",
    "💡": "info",
    "👋": "wow",
    "👮": "shield",
    "👇": "play",
    "🏆": "crown",
    "🏠": "rocket",
    "📚": "book",
    "🔁": "refresh",
    "🔍": "find",
    "➕": "plus",
    "✏️": "edit",
    "🎬": "play",
    "👁️": "focus",
    "🎨": "star",
    "🧹": "trash",
    "🖥": "desktop",
    "🔌": "satellite",
    "🔓": "key",
    "🚫": "cancel",
    "ℹ": "info",
    "ℹ️": "info",
})

_BUTTON_ICON_LABELS = {
    "announce": "Channel",
    "back": "Back",
    "bell": "Alerts",
    "cancel": "Cancel",
    "chart": "Stats",
    "check": "Confirm",
    "copy": "Copy",
    "earth": "Info",
    "edit": "Edit",
    "gear": "Edit",
    "help": "Help",
    "info": "Info",
    "key": "Key",
    "lock": "Open",
    "megaphone": "Broadcast",
    "play": "Start",
    "refresh": "Refresh",
    "settings": "Settings",
    "skull": "Delete",
    "stop": "Stop",
    "trash": "Delete",
    "user": "User",
    "zap": "Run",
}

_BUTTON_PREFIX_TOKENS = sorted({
    token for token in (
        list(_UI_FALLBACK_TO_NAME.keys()) + [
            "0️⃣", "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣",
            "🔁", "🖼️", "🎬", "👁️", "🛟", "🆓", "🏠", "📚", "🎨", "📤",
            "📥", "📭", "💾", "🧹", "👮", "👋", "🏆", "✦", "✏️", "➕",
            "⬅", "➡", "←", "→",
        ]
    ) if token
}, key=len, reverse=True)

_TG_EMOJI_BLOCK_RE = re.compile(r"<tg-emoji\b[^>]*>.*?</tg-emoji>", flags=re.DOTALL)
_HTML_FORMATTING_RE = re.compile(r"</?(?:a|b|blockquote|code|i|pre|s|tg-emoji|u)\b", flags=re.IGNORECASE)
_AUTO_UI_REPLACEMENTS = sorted({
    (token, name) for token, name in _UI_FALLBACK_TO_NAME.items()
    if token and name in _UI
}, key=lambda item: len(item[0]), reverse=True)


def _button_icon_name(icon_id: str = None, inferred_name: str = None) -> Optional[str]:
    if icon_id:
        return _UI_ID_TO_NAME.get(str(icon_id))
    return inferred_name


def _strip_button_prefix(text: str) -> tuple[str, Optional[str]]:
    s = _btn_clean(text).replace("\xa0", " ").strip()
    inferred_name = None
    while s:
        matched = False
        for token in _BUTTON_PREFIX_TOKENS:
            if s.startswith(token):
                if inferred_name is None:
                    inferred_name = _UI_FALLBACK_TO_NAME.get(token)
                s = s[len(token):].lstrip(" \t-–—:|•·")
                matched = True
                break
        if matched:
            continue

        ch = s[0]
        category = unicodedata.category(ch)
        if category.startswith("S") or (category.startswith("P") and ch not in "+#/@&"):
            s = s[1:].lstrip(" \t-–—:|•·")
            continue
        break

    s = re.sub(r"\s{2,}", " ", s).strip(" \t-–—:|•·")
    return s, inferred_name


def _resolve_button_payload(text: str, icon_id: str = None) -> tuple[str, Optional[str]]:
    label, inferred_name = _strip_button_prefix(text)
    final_icon = str(icon_id) if icon_id else None
    if not final_icon and inferred_name and inferred_name in _UI:
        final_icon = _UI[inferred_name][0]

    if not label:
        icon_name = _button_icon_name(final_icon, inferred_name)
        label = _BUTTON_ICON_LABELS.get(icon_name, "Open")

    return label, final_icon


def _replace_ui_fallbacks(fragment: str) -> str:
    out = fragment
    for token, name in _AUTO_UI_REPLACEMENTS:
        out = out.replace(token, ui(name))
    return out


def _auto_ui_text(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text

    parts = []
    last = 0
    for match in _TG_EMOJI_BLOCK_RE.finditer(text):
        parts.append(_replace_ui_fallbacks(text[last:match.start()]))
        parts.append(match.group(0))
        last = match.end()
    parts.append(_replace_ui_fallbacks(text[last:]))
    return "".join(parts)


def _prepare_outgoing_text(text: str, parse_mode: Optional[str] = None) -> tuple[str, Optional[str]]:
    if not isinstance(text, str) or parse_mode not in (None, "HTML"):
        return text, parse_mode

    updated = _auto_ui_text(text)
    if parse_mode is None and (updated != text or _HTML_FORMATTING_RE.search(updated)):
        parse_mode = "HTML"
    return updated, parse_mode


_orig_message_reply_text = Message.reply_text
_orig_bot_send_message = Bot.send_message
_orig_bot_edit_message_text = Bot.edit_message_text
_orig_callback_edit_message_text = CallbackQuery.edit_message_text
_orig_callback_edit_message_reply_markup = CallbackQuery.edit_message_reply_markup


def _normalize_reply_markup(reply_markup):
    if reply_markup is None or isinstance(reply_markup, InlineKeyboardMarkup):
        return reply_markup

    rows = None
    if isinstance(reply_markup, dict) and "inline_keyboard" in reply_markup:
        rows = reply_markup.get("inline_keyboard", [])
    elif isinstance(reply_markup, (list, tuple)):
        rows = reply_markup

    if rows is None:
        return reply_markup

    normalized_rows = []
    for row in rows:
        if not isinstance(row, (list, tuple)):
            row = [row]
        normalized_row = []
        for item in row:
            if isinstance(item, _OrigIKB):
                normalized_row.append(item)
                continue
            if isinstance(item, dict):
                payload = dict(item)
                text = payload.pop("text", payload.pop("label", "Open"))
                normalized_row.append(InlineKeyboardButton(text, **payload))
                continue
            normalized_row.append(item)
        normalized_rows.append(normalized_row)
    return InlineKeyboardMarkup(normalized_rows)


def _strip_button_extras(reply_markup):
    """Retry keyboards without animated icon/style payloads if Telegram rejects them."""
    if reply_markup is None:
        return None
    try:
        normalized = _normalize_reply_markup(reply_markup)
        payload = normalized.to_dict() if hasattr(normalized, "to_dict") else normalized
    except Exception:
        payload = reply_markup

    def _clean(node):
        if isinstance(node, dict):
            return {
                k: _clean(v)
                for k, v in node.items()
                if k not in {"icon_custom_emoji_id", "style"}
            }
        if isinstance(node, list):
            return [_clean(item) for item in node]
        if isinstance(node, tuple):
            return [_clean(item) for item in node]
        return node

    return _normalize_reply_markup(_clean(payload))


def _is_button_payload_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(token in msg for token in (
        "document_invalid",
        "document invalid",
        "button_type_invalid",
        "can't parse inline keyboard button",
        "inline keyboard button",
        "custom emoji",
    ))


async def _reply_text_with_ui(self, text, *args, **kwargs):
    kwargs = dict(kwargs)
    skip_ui_prepare = kwargs.pop("_skip_ui_prepare", False)
    if skip_ui_prepare:
        parse_mode = kwargs.get("parse_mode")
    else:
        text, parse_mode = _prepare_outgoing_text(text, kwargs.get("parse_mode"))
    kwargs["reply_markup"] = _normalize_reply_markup(kwargs.get("reply_markup"))
    if parse_mode is None:
        kwargs.pop("parse_mode", None)
    else:
        kwargs["parse_mode"] = parse_mode
    try:
        return await _orig_message_reply_text(self, text, *args, **kwargs)
    except TelegramBadRequest as e:
        if "reply_markup" not in kwargs or not _is_button_payload_error(e):
            raise
        retry_kwargs = dict(kwargs)
        retry_kwargs["reply_markup"] = _strip_button_extras(kwargs.get("reply_markup"))
        return await _orig_message_reply_text(self, text, *args, **retry_kwargs)


async def _send_message_with_ui(self, chat_id, text, *args, **kwargs):
    kwargs = dict(kwargs)
    skip_ui_prepare = kwargs.pop("_skip_ui_prepare", False)
    if skip_ui_prepare:
        parse_mode = kwargs.get("parse_mode")
    else:
        text, parse_mode = _prepare_outgoing_text(text, kwargs.get("parse_mode"))
    kwargs["reply_markup"] = _normalize_reply_markup(kwargs.get("reply_markup"))
    if parse_mode is None:
        kwargs.pop("parse_mode", None)
    else:
        kwargs["parse_mode"] = parse_mode
    try:
        return await _orig_bot_send_message(self, chat_id, text, *args, **kwargs)
    except TelegramBadRequest as e:
        if "reply_markup" not in kwargs or not _is_button_payload_error(e):
            raise
        retry_kwargs = dict(kwargs)
        retry_kwargs["reply_markup"] = _strip_button_extras(kwargs.get("reply_markup"))
        return await _orig_bot_send_message(self, chat_id, text, *args, **retry_kwargs)


async def _edit_message_text_with_ui(self, text, *args, **kwargs):
    kwargs = dict(kwargs)
    skip_ui_prepare = kwargs.pop("_skip_ui_prepare", False)
    if skip_ui_prepare:
        parse_mode = kwargs.get("parse_mode")
    else:
        text, parse_mode = _prepare_outgoing_text(text, kwargs.get("parse_mode"))
    kwargs["reply_markup"] = _normalize_reply_markup(kwargs.get("reply_markup"))
    if parse_mode is None:
        kwargs.pop("parse_mode", None)
    else:
        kwargs["parse_mode"] = parse_mode
    try:
        return await _orig_bot_edit_message_text(self, text, *args, **kwargs)
    except TelegramBadRequest as e:
        if "reply_markup" not in kwargs or not _is_button_payload_error(e):
            raise
        retry_kwargs = dict(kwargs)
        retry_kwargs["reply_markup"] = _strip_button_extras(kwargs.get("reply_markup"))
        return await _orig_bot_edit_message_text(self, text, *args, **retry_kwargs)


async def _callback_edit_message_text_with_ui(self, text, *args, **kwargs):
    kwargs = dict(kwargs)
    skip_ui_prepare = kwargs.pop("_skip_ui_prepare", False)
    if skip_ui_prepare:
        parse_mode = kwargs.get("parse_mode")
    else:
        text, parse_mode = _prepare_outgoing_text(text, kwargs.get("parse_mode"))
    kwargs["reply_markup"] = _normalize_reply_markup(kwargs.get("reply_markup"))
    if parse_mode is None:
        kwargs.pop("parse_mode", None)
    else:
        kwargs["parse_mode"] = parse_mode
    try:
        return await _orig_callback_edit_message_text(self, text, *args, **kwargs)
    except TelegramBadRequest as e:
        if "reply_markup" not in kwargs or not _is_button_payload_error(e):
            raise
        retry_kwargs = dict(kwargs)
        retry_kwargs["reply_markup"] = _strip_button_extras(kwargs.get("reply_markup"))
        return await _orig_callback_edit_message_text(self, text, *args, **retry_kwargs)


async def _callback_edit_message_reply_markup_with_ui(self, *args, **kwargs):
    kwargs = dict(kwargs)
    kwargs["reply_markup"] = _normalize_reply_markup(kwargs.get("reply_markup"))
    try:
        return await _orig_callback_edit_message_reply_markup(self, *args, **kwargs)
    except TelegramBadRequest as e:
        if "reply_markup" not in kwargs or not _is_button_payload_error(e):
            raise
        retry_kwargs = dict(kwargs)
        retry_kwargs["reply_markup"] = _strip_button_extras(kwargs.get("reply_markup"))
        return await _orig_callback_edit_message_reply_markup(self, *args, **retry_kwargs)


Message.reply_text = _reply_text_with_ui
Bot.send_message = _send_message_with_ui
Bot.edit_message_text = _edit_message_text_with_ui
CallbackQuery.edit_message_text = _callback_edit_message_text_with_ui
CallbackQuery.edit_message_reply_markup = _callback_edit_message_reply_markup_with_ui

# ═══════════════════════════════════════════════════════════
#  SAFE TELEGRAM HELPERS
# ═══════════════════════════════════════════════════════════
async def safe_edit(query, text: str, reply_markup=None, parse_mode="HTML"):
    """
    Wrapper for query.edit_message_text that silently swallows
    'Message is not modified' (content unchanged) and retries
    once on timeout / network error.
    """
    kwargs = dict(text=text, parse_mode=parse_mode)
    if reply_markup is not None:
        kwargs["reply_markup"] = reply_markup
    for attempt in range(2):
        try:
            await query.edit_message_text(**kwargs)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e).lower():
                return   # already showing correct content — not an error
            if attempt == 0:
                await asyncio.sleep(0.5); continue
            raise
        except (TelegramTimedOut, TelegramNetworkError):
            if attempt == 0:
                await asyncio.sleep(1); continue
            return   # give up gracefully on second timeout


# ═══════════════════════════════════════════════════════════
#  DEFAULT CONSTANTS  (overridden by config.json)
# ═══════════════════════════════════════════════════════════
BOT_TOKEN         = "7952943119:AAFGuZiurY4yiaTCPwkrmsH51EUayr_DUFU"
BOT_USERNAME      = "CrackSMSReBot"
INITIAL_ADMIN_IDS = [7763727542, 7057157722, 7968271742, 7831921606, 8222195948]

# ═══════════════════════════════════════════════════════════
#  PREMIUM TIER SYSTEM (Professional Features)
# ═══════════════════════════════════════════════════════════
PREMIUM_TIERS = {
    "free": {
        "name": f"{ui('online')} Free",
        "daily_otp_limit": -1,  # unlimited
        "max_panels": 2,
        "features": ["basic_otp", "admin_panel"],
        "price": 0,
        "emoji": ui('online'),
    },
    "pro": {
        "name": "💎 Professional",
        "daily_otp_limit": -1,  # unlimited
        "max_panels": 10,
        "features": ["basic_otp", "admin_panel", "analytics", "webhooks", "priority_support"],
        "price": 5.00,
        "emoji": "💎",
    },
    "enterprise": {
        "name": f"{ui('crown')} Enterprise",
        "daily_otp_limit": -1,  # unlimited
        "max_panels": 50,
        "features": ["basic_otp", "admin_panel", "analytics", "webhooks", "priority_support",
                     "wa_business", "media_support", "scheduling", "rate_limiting", "api_access"],
        "price": 10.00,
        "emoji": ui('crown'),
    },
}

PREMIUM_ANALYTICS = {}  # {user_id: {"otps_sent": 0, "panels_used": 0, ...}}
WEBHOOK_STORE = {}      # {user_id: [{"url": "...", "events": [...], ...}]}
MESSAGE_SCHEDULE = {}   # {user_id: [{"timestamp": ..., "target": "...", "message": "..."}]}

# ── Mandatory membership gates ─────────────────────────────
REQUIRED_CHATS = [
    {"id": -1003720717628, "title": "CrackOTP Group",   "link": "https://t.me/crackotpgroup"},
    {"id": -1003563202204, "title": "CrackOTP Channel", "link": "https://t.me/crackotp"},
    {"id": -1003866750250, "title": "Crack Chat GC",    "link": "https://t.me/crackchatgc"},
]

# ──────────────────────────────────────────────────────────────────────────────────
# DETAILS
# ──────────────────────────────────────────────────────────────────────────────────
SUPPORT_USER      = "@ownersigma"
DEVELOPER         = "@NONEXPERTCODER"
OTP_GROUP_LINK    = "https://t.me/crackotpgroup"
GET_NUMBER_URL    = "https://t.me/CrackSMSReBot"
NUMBER_BOT_LINK   = "https://t.me/CrackSMSReBot"
CHANNEL_LINK      = "https://t.me/crackotp"
CHANGE_COOLDOWN_S = 7
COUNTRIES_FILE    = "countries.json"
DEX_FILE          = "dex.txt"
SEEN_DB_FILE      = "sms_database_np.db"
CONFIG_FILE       = "config.json"
OTP_STORE_FILE    = "otp_store.json"
LOG_FILE          = "bot.log"
WEBSITE_REQUESTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "website_bot_requests")
API_FETCH_INTERVAL= 1   # 1s 
MSG_AGE_LIMIT_MIN = 120
API_MAX_RECORDS   = 200  # max 200 — server limit
CHILD_BOT_FORWARD_URL = ""  # main bot webhook URL for child→main OTP forwarding
IS_CHILD_BOT      = False
DEFAULT_ASSIGN_LIMIT = -1  # -1 = UNLIMITED numbers per user
DEFAULT_WEBSITE_ANNOUNCEMENT = (
    "Owner approvals are handled manually. Submit accurate bot details, then contact the owner "
    "with your request ID for a faster review."
)
DEFAULT_WEBSITE_STATUS_NOTE = (
    "Website requests are mirrored into the admin bot so approvals and follow-ups stay in one queue."
)
DEFAULT_WEBSITE_CONTACT_WHATSAPP = "+923000767749"
DEFAULT_WEBSITE_CONTACT_TELEGRAM = "@NONEXPERTCODER"

PERMISSIONS = {
    "manage_panels": "Manage Panels",
    "manage_files":  "Manage Files",
    "manage_logs":   "Manage Log Groups",
    "broadcast":     "Send Broadcasts",
    "view_stats":    "View Statistics",
    "manage_admins": "Manage Admins",
}

PERMISSION_ICONS = {
    "manage_panels": _UI["rocket"][0],
    "manage_files":  _UI["document"][0],
    "manage_logs":   _UI["copy"][0],
    "broadcast":     _UI["announce"][0],
    "view_stats":    _UI["chart"][0],
    "manage_admins": _UI["people"][0],
}

DEFAULT_IVAS_URI = (
    "wss://ivas.tempnum.qzz.io:2087/socket.io/?token=eyJpdiI6IjI4c3JCUVNJa"
    "zRWRkp5M3lHL0pLeEE9PSIsInZhbHVlIjoiU09YK0llL1llc3ZIVzhia0sxTjZYTnZLN"
    "0dFOE1QSEZqMk1GVE1EUDhOVTR2R2tqbGUrVlBNSGJmQ1Q3WjhoUllZWlFTYUlwSmI0"
    "VUZRSHYwUFNqZ1VEY0U1RzFFcmo0MHJlU1BHcHNTYitpK1BKUDRkSGU5NlRoUnB4aThE"
    "TGFwemU2NTRGeUpoczRlNEFBT2tIejlrdWFSWFM1QjlBRURlOXIzbkNaWEJpcTlNV0ZD"
    "KzNrSFVLMEhEem5wUUZlS1NDRmtUVlhX2pxUGZqT2poMWs4UW1JU1d4UmFoTC9LVVHRL"
    "3Zrc00yVkZLcXRzYU9RNkh3dUl1eGNQSWhpZG12aGttMU5qSVovVm9KcytYa0hHb1Rod"
    "TFzYUt0bEdtQ3pVN0pUQkdZR0JGL2hGV21IanJqQXBsSisrSjlMdCtzbUc2dWhVdGdWZz"
    "FPWVgwVDJpSE1jak9LTVl1Vmh4bGNVZlgrT3BWT0g5YldmYVdVWVA1S0crbk9GOTNERWF"
    "1NG5kd0k3YkdXWXBMUk56QVVNNWtFclNoYWdYVXMrQ0NkSEdwamQrZUVNOGJybTdzTmV3"
    "TlpmakU1TmxxdmZIMkVOVGYwc3Y5NTdTeE9Xdm5Jc1FhU092dmE1ZzA4aktXOCtCMTdOb"
    "FgvSmliQlkwYjdmOFkzeHJQdzlOb252NWFHWnR5L3JSQnNDK3k1L0R6U2ZTZStWeDhOQz"
    "dLL01sZDVmamtNZzIrT2NvPSIsIm1hYyI6IjY2MWE1OTcxNWQ5YzU3OTUxZjgwZjA3MW"
    "U2OTUzYmUxMDI4NmQ3Y2ZmOTBkMmRkNTU1MmM0Zjc5ODAyNTRmODAiLCJ0YWciOiIifQ"
    "%3D%3D&user=9704f70096e34e36454e6ad92265698b&EIO=4&transport=websocket"
)

# ═══════════════════════════════════════════════════════════
#  CONFIG LOAD  — reads config.json and overrides constants
# ═══════════════════════════════════════════════════════════
def load_config():
    global DEFAULT_ASSIGN_LIMIT, IS_CHILD_BOT, BOT_TOKEN, BOT_USERNAME
    global INITIAL_ADMIN_IDS, SUPPORT_USER, DEVELOPER
    global OTP_GROUP_LINK, GET_NUMBER_URL, NUMBER_BOT_LINK, CHANNEL_LINK, DEVELOPER
    if not os.path.exists(CONFIG_FILE):
        return
    try:
        with open(CONFIG_FILE) as f:
            c = json.load(f)
        DEFAULT_ASSIGN_LIMIT = c.get("default_limit", DEFAULT_ASSIGN_LIMIT)
        IS_CHILD_BOT          = c.get("IS_CHILD_BOT",   False)
        if c.get("BOT_TOKEN"):       BOT_TOKEN         = c["BOT_TOKEN"]
        if c.get("BOT_USERNAME"):    BOT_USERNAME      = c["BOT_USERNAME"].lstrip("@")
        if c.get("ADMIN_IDS"):       INITIAL_ADMIN_IDS = c["ADMIN_IDS"]
        if c.get("SUPPORT_USER"):    SUPPORT_USER      = c["SUPPORT_USER"]
        if c.get("DEVELOPER"):       DEVELOPER         = c["DEVELOPER"]
        if c.get("OTP_GROUP_LINK"):  OTP_GROUP_LINK    = c["OTP_GROUP_LINK"]
        if c.get("GET_NUMBER_URL"):  GET_NUMBER_URL    = c["GET_NUMBER_URL"]
        if c.get("NUMBER_BOT_LINK"): NUMBER_BOT_LINK   = c["NUMBER_BOT_LINK"]
        if c.get("CHANNEL_LINK"):    CHANNEL_LINK      = c["CHANNEL_LINK"]
        global OTP_GUI_THEME, AUTO_BROADCAST_ON, REQUIRED_CHATS
        OTP_GUI_THEME     = int(c.get("OTP_GUI_THEME", 0))
        AUTO_BROADCAST_ON = bool(c.get("AUTO_BROADCAST_ON", True))
        if c.get("REQUIRED_CHATS"):
            REQUIRED_CHATS = c["REQUIRED_CHATS"]
    except Exception as e:
        print(f"Config load error: {e}")

def save_config_key(key: str, value):
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f: cfg = json.load(f)
        except Exception: pass
    cfg[key] = value
    with open(CONFIG_FILE,"w") as f: json.dump(cfg, f, indent=2)

# ═══════════════════════════════════════════════════════════
#  PREMIUM TIER FUNCTIONS
# ═══════════════════════════════════════════════════════════

def get_config_dict() -> dict:
    """Return the current config.json as a dict (never None)."""
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def get_website_settings() -> dict:
    cfg = get_config_dict()
    return {
        "announcement": cfg.get("WEBSITE_ANNOUNCEMENT", DEFAULT_WEBSITE_ANNOUNCEMENT),
        "status_note": cfg.get("WEBSITE_STATUS_NOTE", DEFAULT_WEBSITE_STATUS_NOTE),
        "whatsapp": cfg.get("WEBSITE_CONTACT_WHATSAPP", DEFAULT_WEBSITE_CONTACT_WHATSAPP),
        "telegram": cfg.get("WEBSITE_CONTACT_TELEGRAM", DEFAULT_WEBSITE_CONTACT_TELEGRAM),
    }


def _ensure_website_requests_dir() -> None:
    os.makedirs(WEBSITE_REQUESTS_DIR, exist_ok=True)


def _website_request_path(req_id: str) -> str:
    safe_req_id = re.sub(r"[^A-Za-z0-9_-]", "", str(req_id or ""))
    return os.path.join(WEBSITE_REQUESTS_DIR, f"{safe_req_id}.json")


def load_website_requests() -> list:
    if not os.path.isdir(WEBSITE_REQUESTS_DIR):
        return []
    rows = []
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
            logger.warning(f"Website request read failed for {name}: {e}")
    rows.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    return rows


def save_website_request(req: dict) -> None:
    req_id = str(req.get("req_id", "")).strip()
    if not req_id:
        raise ValueError("Website request missing req_id")
    _ensure_website_requests_dir()
    path = _website_request_path(req_id)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(req, f, indent=2, ensure_ascii=True)
    os.replace(tmp_path, path)


def update_website_request(req_id: str, **updates) -> Optional[dict]:
    path = _website_request_path(req_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        payload = {}
    payload.update(updates)
    save_website_request(payload)
    return payload

def get_user_tier(user_id: int) -> str:
    """Get user's premium tier (free/pro/enterprise) - default free."""
    try:
        if user_id in INITIAL_ADMIN_IDS:
            return "enterprise"
        tier_data = get_config_dict().get("user_tiers", {})
        return tier_data.get(str(user_id), "free")
    except Exception:
        return "free"

def set_user_tier(user_id: int, tier: str) -> bool:
    """Set user's premium tier."""
    if tier not in PREMIUM_TIERS:
        return False
    try:
        tier_data = get_config_dict().get("user_tiers", {})
        tier_data[str(user_id)] = tier
        save_config_key("user_tiers", tier_data)
        return True
    except:
        return False

def check_otp_limit(user_id: int) -> dict:
    """Check if user has reached daily OTP limit. Returns {ok: bool, remaining: int, limit: int}"""
    tier = get_user_tier(user_id)
    limit = PREMIUM_TIERS[tier]["daily_otp_limit"]
    
    if user_id not in PREMIUM_ANALYTICS:
        PREMIUM_ANALYTICS[user_id] = {"otps_today": 0, "last_reset": datetime.now()}
    
    # Reset if new day
    if datetime.now().date() > PREMIUM_ANALYTICS[user_id]["last_reset"].date():
        PREMIUM_ANALYTICS[user_id]["otps_today"] = 0
        PREMIUM_ANALYTICS[user_id]["last_reset"] = datetime.now()
    
    sent = PREMIUM_ANALYTICS[user_id]["otps_today"]
    return {
        "ok": sent < limit,
        "sent": sent,
        "remaining": max(0, limit - sent),
        "limit": limit,
        "tier": tier
    }

def increment_otp_count(user_id: int):
    """Increment daily OTP counter for analytics."""
    if user_id not in PREMIUM_ANALYTICS:
        PREMIUM_ANALYTICS[user_id] = {"otps_today": 0, "last_reset": datetime.now()}
    PREMIUM_ANALYTICS[user_id]["otps_today"] += 1

def register_webhook(user_id: int, webhook_url: str, events: list) -> dict:
    """Register webhook callback for premium users."""
    tier = get_user_tier(user_id)
    if tier == "free":
        return {"ok": False, "error": "Webhooks require Pro tier or higher"}
    
    if user_id not in WEBHOOK_STORE:
        WEBHOOK_STORE[user_id] = []
    
    webhook_id = str(uuid.uuid4())[:8]
    webhook = {
        "id": webhook_id,
        "url": webhook_url,
        "events": events,
        "created": datetime.now().isoformat(),
        "active": True
    }
    WEBHOOK_STORE[user_id].append(webhook)
    return {"ok": True, "webhook_id": webhook_id, "message": "Webhook registered"}

async def trigger_webhook(user_id: int, event: str, data: dict):
    """Trigger webhook callbacks asynchronously."""
    if user_id not in WEBHOOK_STORE:
        return
    
    async with aiohttp.ClientSession() as session:
        for webhook in WEBHOOK_STORE[user_id]:
            if not webhook["active"] or event not in webhook["events"]:
                continue
            try:
                payload = {"event": event, "timestamp": datetime.now().isoformat(), "data": data}
                async with session.post(webhook["url"], json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        logger.warn(f"Webhook {webhook['id']} returned {resp.status}")
            except asyncio.TimeoutError:
                logger.warn(f"Webhook {webhook['id']} timeout")
            except Exception as e:
                logger.error(f"Webhook {webhook['id']} error: {e}")

# WhatsApp scheduling functions removed (Telegram-only focus)

load_config()
if not os.path.exists(LOG_FILE):
    open(LOG_FILE,"a").close()

# ═══════════════════════════════════════════════════════════
#  OTP STORE
# ═══════════════════════════════════════════════════════════
def load_otp_store() -> dict:
    if os.path.exists(OTP_STORE_FILE):
        try:
            with open(OTP_STORE_FILE) as f: return json.load(f)
        except Exception: pass
    return {}

def save_otp_store(store: dict):
    try:
        path = os.path.abspath(OTP_STORE_FILE)
        with open(path, "w") as f:
            json.dump(store, f, indent=2)
    except Exception as e:
        logger.error(f"{ui('cancel')} OTP store save failed ({OTP_STORE_FILE}): {e}", exc_info=True)

def append_otp(num_raw: str, otp_code: str):
    """Thread-safe single OTP save — always writes immediately."""
    try:
        store = load_otp_store()
        store[num_raw] = otp_code
        # Keep max 2000 entries — trim oldest if over limit
        if len(store) > 2000:
            keys = list(store.keys())
            for k in keys[:-2000]: del store[k]
        save_otp_store(store)
        logger.info(f"{ui('copy')} OTP saved: {mask_number(num_raw)} → {otp_code}")
    except Exception as e:
        logger.error(f"{ui('cancel')} append_otp failed: {e}")

# ═══════════════════════════════════════════════════════════
#  SEEN-SMS  (deduplication)
# ═══════════════════════════════════════════════════════════
def init_seen_db() -> set:
    try:
        conn = sqlite3.connect(SEEN_DB_FILE)
        conn.execute("CREATE TABLE IF NOT EXISTS reported_sms (hash TEXT PRIMARY KEY)")
        conn.commit()
        rows = conn.execute("SELECT hash FROM reported_sms").fetchall()
        conn.close()
        logger.info(f"Loaded {len(rows)} seen-SMS hashes.")
        return {r[0] for r in rows}
    except Exception as e:
        logger.error(f"Seen DB: {e}")
        return set()

def save_seen_hash(h: str):
    try:
        conn = sqlite3.connect(SEEN_DB_FILE)
        conn.execute("INSERT OR IGNORE INTO reported_sms (hash) VALUES (?)", (h,))
        conn.commit(); conn.close()
    except Exception: pass

TEST_NUMBERS = [f"1202555010{i}" for i in range(10)]

# ═══════════════════════════════════════════════════════════
#  OTP EXTRACTION — 200+ patterns
# ═══════════════════════════════════════════════════════════
_OTP_RE = [
    # ── 1. WhatsApp / Telegram split format (HIGHEST PRIORITY) ──
    # Matches: "code 359-072", "code 378-229", "#code 796-123"
    r"(?:code|رمز|کد|otp)\s+(\d{3,4})-(\d{3,4})",
    r"(?:code|رمز|کد|otp)\s+(\d{3,4})\s(\d{3,4})",
    # ── 2. Explicit keyword OTP/code IS <digits> ─────────────────
    r"(?:your|the)\s+(?:otp|one.?time.?pass(?:word|code)?)\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:otp|one.?time.?pass(?:word|code)?)\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:verification|confirm(?:ation)?)\s*(?:code|pin|otp)\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:auth(?:entication)?|security|access)\s*code\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:login|sign.?in|sign.?up)\s*(?:code|pin|otp)\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:activation|account)\s*(?:code|pin)\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:reset|recovery|2fa|two.?factor)\s*(?:code|pin|otp)\s*(?:is|:)?\s*(\d{4,8})",
    r"code\s*(?:is|:)\s*(\d{4,8})",
    r"pin\s*(?:is|:)\s*(\d{4,8})",
    r"otp\s*[:#=]\s*(\d{4,8})",
    r"code\s*[:#=]\s*(\d{4,8})",
    r"token\s*(?:is|:)?\s*(\d{4,8})",
    r"passcode\s*(?:is|:)?\s*(\d{4,8})",
    r"one.?time\s+(?:password|passcode|code)\s*[:#=]?\s*(\d{4,8})",
    r"confirmation\s*(?:number|code)\s*[:#=]?\s*(\d{4,8})",
    # ── 3. Service-specific ────────────────────────────────────────
    r"(?:WhatsApp|WA)\s*(?:Business\s+)?(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Telegram|TG)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Facebook|FB)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Instagram|IG)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Twitter|TW|X)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:TikTok|TT)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Snapchat|SC)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Google|GG|Gmail|GM)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Microsoft|MS|Outlook)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Apple|iCloud)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Amazon|AM)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:PayPal|PP)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Uber|UB|Lyft|LF)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Discord|DC)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Viber|VB|LINE|LN)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:WeChat|WC|KakaoTalk)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Netflix|NF|Spotify)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:LinkedIn|LI)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Steam|Twitch)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Binance|BN|Coinbase|CB)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Bybit|Kucoin|OKX|Mexc|Kraken)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Signal|Skype|Zoom)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Tinder|Bumble|Hinge)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Airbnb|Booking)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Careem|Swvl|Rapido|Bolt)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Jazz|Telenor|Zong|Ufone|PTCL)\s*(?:code|OTP|PIN)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Easypaisa|JazzCash|HBL|MCB|UBL|Meezan|Allied)\s*(?:code|OTP|PIN)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Bykea|Daraz|foodpanda|Cheetay)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Ola|Didi|Grab|Gojek)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Lazada|Shopee|Tokopedia)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Paytm|PhonePe|GPay|BHIM)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:HDFC|ICICI|Axis|SBI|Kotak)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Mezan|Sadapay|NayaPay|Keenu)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Etisalat|Du|STC|Mobily|Zain)\s*(?:code|OTP|PIN)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Orange|Vodafone|MTN|Airtel|Safaricom)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Reddit|Pinterest|Quora|Discord)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Hulu|Disney|Prime|HBO|Netflix)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Canva|Notion|Figma|Slack|GitHub)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:Xbox|PlayStation|Nintendo|Roblox)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:PUBG|Fortnite|Valorant|Epic)\s*(?:code|OTP)?\s*(?:is|:)?\s*(\d{4,8})",
    # ── 4. Action phrases ─────────────────────────────────────────
    r"use\s+(?:this\s+)?(?:code|otp|pin)\s*(?:to|:)?\s*(\d{4,8})",
    r"enter\s+(?:this\s+)?(?:code|otp|pin)\s*(?:to|:)?\s*(\d{4,8})",
    r"your\s+code\s+(\d{4,8})",
    r"code\s+(\d{4,8})\s+(?:is|will)",
    r"(\d{4,8})\s+is\s+your\s+(?:otp|code|pin|password)",
    r"(\d{4,8})\s+(?:is\s+)?(?:the|your)\s+(?:verification|auth|login)\s+code",
    r"(\d{4,8})\s+(?:is\s+)?(?:the|your)\s+one.?time",
    r"(?:do\s+not\s+share|never\s+share).{0,60}?(\d{4,8})",
    r"(\d{4,8}).{0,60}?(?:do\s+not\s+share|never\s+share)",
    r"(?:expires?\s+in|valid\s+for).{0,40}?(\d{4,8})",
    r"(\d{4,8}).{0,40}?(?:expires?|valid)",
    r"(?:confirm|verify)\s+(?:with|using)?\s*(\d{4,8})",
    r"(?:transaction|txn)\s*(?:code|pin|otp)\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:payment|transfer)\s*(?:code|pin)\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:temporary|temp)\s+(?:code|password|pin)\s*(?:is|:)?\s*(\d{4,8})",
    r"secret\s*(?:code|key|pin)\s*(?:is|:)?\s*(\d{4,8})",
    # ── 5. Platform-hardcoded ──────────────────────────────────────
    r"msverify[\s:/]*(\d{4,8})",
    r"msauth[\s:/]*(\d{4,8})",
    r"G-(\d{6})",
    r"FB-(\d{5,8})",
    r"WA-(\d{4,8})",
    r"(?:<#>|#)\s*your\s+whatsapp\s+(?:business\s+)?code\s+(\d{3,4})-(\d{3,4})",
    r"\[(\d{4,8})\]\s+is\s+your",
    r"(\d{4,8})\s+is\s+your\s+\w+\s+code",
    r"code:\s*(\d{4,8})",
    r"OTP:\s*(\d{4,8})",
    r"PIN:\s*(\d{4,8})",
    r"verification\s+number\s*[:#=]?\s*(\d{4,8})",
    r"(\d{6})\s+(?:is|are)\s+your",
    # ── 6. Split digit formats (NNN-NNN) ─────────────────────────
    r"(\d{3})-(\d{3})",
    r"(\d{4})-(\d{4})",
    r"(\d{3})\s(\d{3})",
    r"(\d{4})\s(\d{2})",
    r"(\d{2})\s(\d{4})",
    # ── 7. Language variants ──────────────────────────────────────
    r"(?:رمز|کد|کود)\s*(?:تأیید|التحقق|OTP)?\s*(?:است|:)?\s*(\d{4,8})",
    r"(?:کوڈ|رمز)\s*(?:ہے|:)?\s*(\d{4,8})",
    r"(?:código|code|clave)\s*(?:de\s+verificación|OTP)?\s*(?:es|:)?\s*(\d{4,8})",
    r"(?:код|OTP)\s*(?:подтверждения)?\s*(?:[:—])\s*(\d{4,8})",
    r"(?:驗證碼|验证码|코드)\s*(?:是|:)?\s*(\d{4,8})",
    r"(?:کد\s*واتساپ|رمز\s*واتساپ)\s+(\d{3,4})-(\d{3,4})",
    r"(?:کد\s*واتساپ|رمز\s*واتساپ)\s+(\d{4,8})",
    r"(?:رمز|کد)\s*(?:تأیید|عبور|OTP)?\s*(?:شما)?\s*(?:است|:)?\s*(\d{4,8})",
    r"(?:کوڈ|رمز)\s*(?:ہے|آپکا|آپ\s*کا)?\s*(\d{4,8})",
    r"(?:doğrulama|onay)\s*kodu?\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:kode|kod)\s*(?:verifikasi|doğrulama)?\s*(?:anda|:)?\s*(\d{4,8})",
    r"(?:mật\s*khẩu|xác\s*nhận)\s*(?:is|:)?\s*(\d{4,8})",
    r"(?:รหัส|ยืนยัน)\s*(\d{4,8})",
    r"आपका\s*(?:OTP|कोड|पिन)?\s*(?:है)?\s*(\d{4,8})",
    r"আপনার\s*(?:OTP|কোড)?\s*(?:হল)?\s*(\d{4,8})",
    r"(?:الرمز|رمزك|كودك)\s*(?:هو|:)?\s*(\d{4,8})",
    r"أدخل\s*(?:الرمز)?\s*(\d{4,8})",
    r"(?:einmalcode|bestätigungscode)\s*(?:ist|:)?\s*(\d{4,8})",
    r"(?:votre|ton)\s+(?:code|mot\s+de\s+passe)\s+(?:est|:)?\s*(\d{4,8})",
    # ── 8. Position-based ─────────────────────────────────────────
    r":\s*(\d{6})",
    r"=\s*(\d{6})",
    r":\s*(\d{4})",
    r"is\s+(\d{6})",
    r"is\s+(\d{4})",
    # ── 9. Catch-all (last resort) ────────────────────────────────
    r"(?<!\d)(\d{6})(?!\d)",
    r"(?<!\d)(\d{5})(?!\d)",
    r"(?<!\d)(\d{8})(?!\d)",
    r"(?<!\d)(\d{7})(?!\d)",
    r"(?<!\d)(\d{4})(?!\d)",
]

_OTP_RE_C = []
for _p in _OTP_RE:
    try: _OTP_RE_C.append(re.compile(_p, re.IGNORECASE | re.UNICODE))
    except re.error: pass

def extract_otp_regex(text: str) -> Optional[str]:
    """Extract OTP using pre-compiled patterns. Handles split formats (359-072)."""
    if not text: return None
    for pat in _OTP_RE_C:
        try:
            m = pat.search(text)
            if not m: continue
            raw = re.sub(r"[^0-9]", "", "".join(g for g in m.groups() if g is not None))
            if raw.isdigit() and 4 <= len(raw) <= 9:
                return raw
        except Exception: continue
    return None

# ═══════════════════════════════════════════════════════════
#  PHONE / COUNTRY HELPERS
# ═══════════════════════════════════════════════════════════
COUNTRY_DATA: List[dict] = []

# ── Embedded country data (fallback when countries.json is missing) ──
_EMBEDDED_COUNTRIES = [{"name":"Afghanistan","dial_code":"+93","code":"AF","flag":"🇦🇫"},{"name":"Albania","dial_code":"+355","code":"AL","flag":"🇦🇱"},{"name":"Algeria","dial_code":"+213","code":"DZ","flag":"🇩🇿"},{"name":"American Samoa","dial_code":"+1684","code":"AS","flag":"🇦🇸"},{"name":"Andorra","dial_code":"+376","code":"AD","flag":"🇦🇩"},{"name":"Angola","dial_code":"+244","code":"AO","flag":"🇦🇴"},{"name":"Anguilla","dial_code":"+1264","code":"AI","flag":"🇦🇮"},{"name":"Argentina","dial_code":"+54","code":"AR","flag":"🇦🇷"},{"name":"Armenia","dial_code":"+374","code":"AM","flag":"🇦🇲"},{"name":"Aruba","dial_code":"+297","code":"AW","flag":"🇦🇼"},{"name":"Australia","dial_code":"+61","code":"AU","flag":"🇦🇺"},{"name":"Austria","dial_code":"+43","code":"AT","flag":"🇦🇹"},{"name":"Azerbaijan","dial_code":"+994","code":"AZ","flag":"🇦🇿"},{"name":"Bahamas","dial_code":"+1242","code":"BS","flag":"🇧🇸"},{"name":"Bahrain","dial_code":"+973","code":"BH","flag":"🇧🇭"},{"name":"Bangladesh","dial_code":"+880","code":"BD","flag":"🇧🇩"},{"name":"Barbados","dial_code":"+1246","code":"BB","flag":"🇧🇧"},{"name":"Belarus","dial_code":"+375","code":"BY","flag":"🇧🇾"},{"name":"Belgium","dial_code":"+32","code":"BE","flag":"🇧🇪"},{"name":"Belize","dial_code":"+501","code":"BZ","flag":"🇧🇿"},{"name":"Benin","dial_code":"+229","code":"BJ","flag":"🇧🇯"},{"name":"Bermuda","dial_code":"+1441","code":"BM","flag":"🇧🇲"},{"name":"Bhutan","dial_code":"+975","code":"BT","flag":"🇧🇹"},{"name":"Bolivia","dial_code":"+591","code":"BO","flag":"🇧🇴"},{"name":"Bosnia and Herzegovina","dial_code":"+387","code":"BA","flag":"🇧🇦"},{"name":"Botswana","dial_code":"+267","code":"BW","flag":"🇧🇼"},{"name":"Brazil","dial_code":"+55","code":"BR","flag":"🇧🇷"},{"name":"Brunei","dial_code":"+673","code":"BN","flag":"🇧🇳"},{"name":"Bulgaria","dial_code":"+359","code":"BG","flag":"🇧🇬"},{"name":"Burkina Faso","dial_code":"+226","code":"BF","flag":"🇧🇫"},{"name":"Burundi","dial_code":"+257","code":"BI","flag":"🇧🇮"},{"name":"Cambodia","dial_code":"+855","code":"KH","flag":"🇰🇭"},{"name":"Cameroon","dial_code":"+237","code":"CM","flag":"🇨🇲"},{"name":"Canada","dial_code":"+1","code":"CA","flag":"🇨🇦"},{"name":"Cape Verde","dial_code":"+238","code":"CV","flag":"🇨🇻"},{"name":"Cayman Islands","dial_code":"+1345","code":"KY","flag":"🇰🇾"},{"name":"Central African Republic","dial_code":"+236","code":"CF","flag":"🇨🇫"},{"name":"Chad","dial_code":"+235","code":"TD","flag":"🇹🇩"},{"name":"Chile","dial_code":"+56","code":"CL","flag":"🇨🇱"},{"name":"China","dial_code":"+86","code":"CN","flag":"🇨🇳"},{"name":"Colombia","dial_code":"+57","code":"CO","flag":"🇨🇴"},{"name":"Comoros","dial_code":"+269","code":"KM","flag":"🇰🇲"},{"name":"Congo","dial_code":"+242","code":"CG","flag":"🇨🇬"},{"name":"Costa Rica","dial_code":"+506","code":"CR","flag":"🇨🇷"},{"name":"Croatia","dial_code":"+385","code":"HR","flag":"🇭🇷"},{"name":"Cuba","dial_code":"+53","code":"CU","flag":"🇨🇺"},{"name":"Cyprus","dial_code":"+357","code":"CY","flag":"🇨🇾"},{"name":"Czech Republic","dial_code":"+420","code":"CZ","flag":"🇨🇿"},{"name":"Denmark","dial_code":"+45","code":"DK","flag":"🇩🇰"},{"name":"Djibouti","dial_code":"+253","code":"DJ","flag":"🇩🇯"},{"name":"Dominican Republic","dial_code":"+1809","code":"DO","flag":"🇩🇴"},{"name":"Ecuador","dial_code":"+593","code":"EC","flag":"🇪🇨"},{"name":"Egypt","dial_code":"+20","code":"EG","flag":"🇪🇬"},{"name":"El Salvador","dial_code":"+503","code":"SV","flag":"🇸🇻"},{"name":"Equatorial Guinea","dial_code":"+240","code":"GQ","flag":"🇬🇶"},{"name":"Eritrea","dial_code":"+291","code":"ER","flag":"🇪🇷"},{"name":"Estonia","dial_code":"+372","code":"EE","flag":"🇪🇪"},{"name":"Ethiopia","dial_code":"+251","code":"ET","flag":"🇪🇹"},{"name":"Faroe Islands","dial_code":"+298","code":"FO","flag":"🇫🇴"},{"name":"Fiji","dial_code":"+679","code":"FJ","flag":"🇫🇯"},{"name":"Finland","dial_code":"+358","code":"FI","flag":"🇫🇮"},{"name":"France","dial_code":"+33","code":"FR","flag":"🇫🇷"},{"name":"French Guiana","dial_code":"+594","code":"GF","flag":"🇬🇫"},{"name":"French Polynesia","dial_code":"+689","code":"PF","flag":"🇵🇫"},{"name":"Gabon","dial_code":"+241","code":"GA","flag":"🇬🇦"},{"name":"Gambia","dial_code":"+220","code":"GM","flag":"🇬🇲"},{"name":"Georgia","dial_code":"+995","code":"GE","flag":"🇬🇪"},{"name":"Germany","dial_code":"+49","code":"DE","flag":"🇩🇪"},{"name":"Ghana","dial_code":"+233","code":"GH","flag":"🇬🇭"},{"name":"Gibraltar","dial_code":"+350","code":"GI","flag":"🇬🇮"},{"name":"Greece","dial_code":"+30","code":"GR","flag":"🇬🇷"},{"name":"Greenland","dial_code":"+299","code":"GL","flag":"🇬🇱"},{"name":"Grenada","dial_code":"+1473","code":"GD","flag":"🇬🇩"},{"name":"Guadeloupe","dial_code":"+590","code":"GP","flag":"🇬🇵"},{"name":"Guam","dial_code":"+1671","code":"GU","flag":"🇬🇺"},{"name":"Guatemala","dial_code":"+502","code":"GT","flag":"🇬🇹"},{"name":"Guinea","dial_code":"+224","code":"GN","flag":"🇬🇳"},{"name":"Guinea-Bissau","dial_code":"+245","code":"GW","flag":"🇬🇼"},{"name":"Guyana","dial_code":"+592","code":"GY","flag":"🇬🇾"},{"name":"Haiti","dial_code":"+509","code":"HT","flag":"🇭🇹"},{"name":"Honduras","dial_code":"+504","code":"HN","flag":"🇭🇳"},{"name":"Hong Kong","dial_code":"+852","code":"HK","flag":"🇭🇰"},{"name":"Hungary","dial_code":"+36","code":"HU","flag":"🇭🇺"},{"name":"Iceland","dial_code":"+354","code":"IS","flag":"🇮🇸"},{"name":"India","dial_code":"+91","code":"IN","flag":"🇮🇳"},{"name":"Indonesia","dial_code":"+62","code":"ID","flag":"🇮🇩"},{"name":"Iran","dial_code":"+98","code":"IR","flag":"🇮🇷"},{"name":"Iraq","dial_code":"+964","code":"IQ","flag":"🇮🇶"},{"name":"Ireland","dial_code":"+353","code":"IE","flag":"🇮🇪"},{"name":"Israel","dial_code":"+972","code":"IL","flag":"🇮🇱"},{"name":"Italy","dial_code":"+39","code":"IT","flag":"🇮🇹"},{"name":"Jamaica","dial_code":"+1876","code":"JM","flag":"🇯🇲"},{"name":"Japan","dial_code":"+81","code":"JP","flag":"🇯🇵"},{"name":"Jordan","dial_code":"+962","code":"JO","flag":"🇯🇴"},{"name":"Kazakhstan","dial_code":"+77","code":"KZ","flag":"🇰🇿"},{"name":"Kenya","dial_code":"+254","code":"KE","flag":"🇰🇪"},{"name":"Kuwait","dial_code":"+965","code":"KW","flag":"🇰🇼"},{"name":"Kyrgyzstan","dial_code":"+996","code":"KG","flag":"🇰🇬"},{"name":"Laos","dial_code":"+856","code":"LA","flag":"🇱🇦"},{"name":"Latvia","dial_code":"+371","code":"LV","flag":"🇱🇻"},{"name":"Lebanon","dial_code":"+961","code":"LB","flag":"🇱🇧"},{"name":"Liberia","dial_code":"+231","code":"LR","flag":"🇱🇷"},{"name":"Libya","dial_code":"+218","code":"LY","flag":"🇱🇾"},{"name":"Liechtenstein","dial_code":"+423","code":"LI","flag":"🇱🇮"},{"name":"Lithuania","dial_code":"+370","code":"LT","flag":"🇱🇹"},{"name":"Luxembourg","dial_code":"+352","code":"LU","flag":"🇱🇺"},{"name":"Macau","dial_code":"+853","code":"MO","flag":"🇲🇴"},{"name":"Macedonia","dial_code":"+389","code":"MK","flag":"🇲🇰"},{"name":"Madagascar","dial_code":"+261","code":"MG","flag":"🇲🇬"},{"name":"Malawi","dial_code":"+265","code":"MW","flag":"🇲🇼"},{"name":"Malaysia","dial_code":"+60","code":"MY","flag":"🇲🇾"},{"name":"Maldives","dial_code":"+960","code":"MV","flag":"🇲🇻"},{"name":"Mali","dial_code":"+223","code":"ML","flag":"🇲🇱"},{"name":"Malta","dial_code":"+356","code":"MT","flag":"🇲🇹"},{"name":"Martinique","dial_code":"+596","code":"MQ","flag":"🇲🇶"},{"name":"Mauritania","dial_code":"+222","code":"MR","flag":"🇲🇷"},{"name":"Mauritius","dial_code":"+230","code":"MU","flag":"🇲🇺"},{"name":"Mexico","dial_code":"+52","code":"MX","flag":"🇲🇽"},{"name":"Moldova","dial_code":"+373","code":"MD","flag":"🇲🇩"},{"name":"Monaco","dial_code":"+377","code":"MC","flag":"🇲🇨"},{"name":"Mongolia","dial_code":"+976","code":"MN","flag":"🇲🇳"},{"name":"Montenegro","dial_code":"+382","code":"ME","flag":"🇲🇪"},{"name":"Morocco","dial_code":"+212","code":"MA","flag":"🇲🇦"},{"name":"Mozambique","dial_code":"+258","code":"MZ","flag":"🇲🇿"},{"name":"Myanmar","dial_code":"+95","code":"MM","flag":"🇲🇲"},{"name":"Namibia","dial_code":"+264","code":"NA","flag":"🇳🇦"},{"name":"Nepal","dial_code":"+977","code":"NP","flag":"🇳🇵"},{"name":"Netherlands","dial_code":"+31","code":"NL","flag":"🇳🇱"},{"name":"New Caledonia","dial_code":"+687","code":"NC","flag":"🇳🇨"},{"name":"New Zealand","dial_code":"+64","code":"NZ","flag":"🇳🇿"},{"name":"Nicaragua","dial_code":"+505","code":"NI","flag":"🇳🇮"},{"name":"Niger","dial_code":"+227","code":"NE","flag":"🇳🇪"},{"name":"Nigeria","dial_code":"+234","code":"NG","flag":"🇳🇬"},{"name":"Norway","dial_code":"+47","code":"NO","flag":"🇳🇴"},{"name":"Oman","dial_code":"+968","code":"OM","flag":"🇴🇲"},{"name":"Pakistan","dial_code":"+92","code":"PK","flag":"🇵🇰"},{"name":"Palestine","dial_code":"+970","code":"PS","flag":"🇵🇸"},{"name":"Panama","dial_code":"+507","code":"PA","flag":"🇵🇦"},{"name":"Papua New Guinea","dial_code":"+675","code":"PG","flag":"🇵🇬"},{"name":"Paraguay","dial_code":"+595","code":"PY","flag":"🇵🇾"},{"name":"Peru","dial_code":"+51","code":"PE","flag":"🇵🇪"},{"name":"Philippines","dial_code":"+63","code":"PH","flag":"🇵🇭"},{"name":"Poland","dial_code":"+48","code":"PL","flag":"🇵🇱"},{"name":"Portugal","dial_code":"+351","code":"PT","flag":"🇵🇹"},{"name":"Puerto Rico","dial_code":"+1939","code":"PR","flag":"🇵🇷"},{"name":"Qatar","dial_code":"+974","code":"QA","flag":"🇶🇦"},{"name":"Romania","dial_code":"+40","code":"RO","flag":"🇷🇴"},{"name":"Russia","dial_code":"+7","code":"RU","flag":"🇷🇺"},{"name":"Rwanda","dial_code":"+250","code":"RW","flag":"🇷🇼"},{"name":"Saudi Arabia","dial_code":"+966","code":"SA","flag":"🇸🇦"},{"name":"Senegal","dial_code":"+221","code":"SN","flag":"🇸🇳"},{"name":"Serbia","dial_code":"+381","code":"RS","flag":"🇷🇸"},{"name":"Sierra Leone","dial_code":"+232","code":"SL","flag":"🇸🇱"},{"name":"Singapore","dial_code":"+65","code":"SG","flag":"🇸🇬"},{"name":"Slovakia","dial_code":"+421","code":"SK","flag":"🇸🇰"},{"name":"Slovenia","dial_code":"+386","code":"SI","flag":"🇸🇮"},{"name":"Somalia","dial_code":"+252","code":"SO","flag":"🇸🇴"},{"name":"South Africa","dial_code":"+27","code":"ZA","flag":"🇿🇦"},{"name":"South Korea","dial_code":"+82","code":"KR","flag":"🇰🇷"},{"name":"South Sudan","dial_code":"+211","code":"SS","flag":"🇸🇸"},{"name":"Spain","dial_code":"+34","code":"ES","flag":"🇪🇸"},{"name":"Sri Lanka","dial_code":"+94","code":"LK","flag":"🇱🇰"},{"name":"Sudan","dial_code":"+249","code":"SD","flag":"🇸🇩"},{"name":"Suriname","dial_code":"+597","code":"SR","flag":"🇸🇷"},{"name":"Sweden","dial_code":"+46","code":"SE","flag":"🇸🇪"},{"name":"Switzerland","dial_code":"+41","code":"CH","flag":"🇨🇭"},{"name":"Syria","dial_code":"+963","code":"SY","flag":"🇸🇾"},{"name":"Taiwan","dial_code":"+886","code":"TW","flag":"🇹🇼"},{"name":"Tajikistan","dial_code":"+992","code":"TJ","flag":"🇹🇯"},{"name":"Tanzania","dial_code":"+255","code":"TZ","flag":"🇹🇿"},{"name":"Thailand","dial_code":"+66","code":"TH","flag":"🇹🇭"},{"name":"Togo","dial_code":"+228","code":"TG","flag":"🇹🇬"},{"name":"Trinidad and Tobago","dial_code":"+1868","code":"TT","flag":"🇹🇹"},{"name":"Tunisia","dial_code":"+216","code":"TN","flag":"🇹🇳"},{"name":"Turkey","dial_code":"+90","code":"TR","flag":"🇹🇷"},{"name":"Turkmenistan","dial_code":"+993","code":"TM","flag":"🇹🇲"},{"name":"Uganda","dial_code":"+256","code":"UG","flag":"🇺🇬"},{"name":"Ukraine","dial_code":"+380","code":"UA","flag":"🇺🇦"},{"name":"United Arab Emirates","dial_code":"+971","code":"AE","flag":"🇦🇪"},{"name":"United Kingdom","dial_code":"+44","code":"GB","flag":"🇬🇧"},{"name":"United States","dial_code":"+1","code":"US","flag":"🇺🇸"},{"name":"Uruguay","dial_code":"+598","code":"UY","flag":"🇺🇾"},{"name":"Uzbekistan","dial_code":"+998","code":"UZ","flag":"🇺🇿"},{"name":"Venezuela","dial_code":"+58","code":"VE","flag":"🇻🇪"},{"name":"Vietnam","dial_code":"+84","code":"VN","flag":"🇻🇳"},{"name":"Yemen","dial_code":"+967","code":"YE","flag":"🇾🇪"},{"name":"Zambia","dial_code":"+260","code":"ZM","flag":"🇿🇲"},{"name":"Zimbabwe","dial_code":"+263","code":"ZW","flag":"🇿🇼"}]

def load_countries():
    global COUNTRY_DATA
    if os.path.exists(COUNTRIES_FILE):
        try:
            with open(COUNTRIES_FILE, encoding="utf-8") as f:
                COUNTRY_DATA = json.load(f)
            logger.info(f"Loaded {len(COUNTRY_DATA)} countries from file.")
            return
        except Exception as e:
            logger.error(f"Countries file error: {e}")
    # Fall back to embedded data
    COUNTRY_DATA = _EMBEDDED_COUNTRIES
    logger.info(f"Loaded {len(COUNTRY_DATA)} countries from embedded data.")

load_countries()

def get_country_info(num: str):
    try:
        n = num if num.startswith("+") else "+" + num
        p = phonenumbers.parse(n)
        country = geocoder.description_for_number(p, "en")
        region  = phonenumbers.region_code_for_number(p)
        flag = "🌍"
        if region and len(region) == 2:
            b = 127462 - ord("A")
            flag = chr(b+ord(region[0])) + chr(b+ord(region[1]))
        return country or "Unknown", flag, region or ""
    except Exception: return "Unknown", "🌍", ""

def get_country_code(num: str) -> str:
    try:
        n = num if num.startswith("+") else "+" + num
        return f"+{phonenumbers.parse(n).country_code}"
    except Exception: return ""

def get_last5(num: str) -> str:
    d = re.sub(r"[^0-9]","",num)
    return d[-5:] if len(d) >= 5 else d

def mask_number(num: str) -> str:
    c = num.replace("+","").replace(" ","")
    return f"{c[:4]}-SIGMA-{c[-4:]}" if len(c) >= 8 else num

def detect_country_from_numbers(nums: list):
    """Detect country from a list of phone numbers.
    Uses countries.json if available, falls back to phonenumbers library."""
    if not nums:
        return "Unknown", "🌍"

    # ── Primary: countries.json dict (fast, supports dial_code matching) ──
    if COUNTRY_DATA:
        sc = sorted(COUNTRY_DATA, key=lambda x: len(x.get("dial_code","")), reverse=True)
        votes = {}
        for raw in nums[:50]:
            chk = "+" + re.sub(r"[^0-9]","",str(raw))
            for c in sc:
                dc = c.get("dial_code","")
                if dc and chk.startswith(dc):
                    k = (c.get("name","Unknown"), c.get("flag","🌍"))
                    votes[k] = votes.get(k,0) + 1
                    break
        if votes:
            return max(votes, key=votes.get)

    # ── Fallback: phonenumbers library (works even without countries.json) ──
    votes2 = {}
    for raw in nums[:50]:
        try:
            n = "+" + re.sub(r"[^0-9]","",str(raw))
            if len(n) < 6:
                continue
            p = phonenumbers.parse(n)
            country = geocoder.description_for_number(p, "en")
            region  = phonenumbers.region_code_for_number(p)
            if not country or not region:
                continue
            b = 127462 - ord("A")
            flag = chr(b + ord(region[0].upper())) + chr(b + ord(region[1].upper()))
            k = (country, flag)
            votes2[k] = votes2.get(k,0) + 1
        except Exception:
            continue
    if votes2:
        return max(votes2, key=votes2.get)

    return "Unknown", "🌍"

_SVC_MAP = {
    "telegram":"TG","facebook":"FB","instagram":"IG","twitter":"TW",
    "tiktok":"TT","snapchat":"SC","google":"GG","gmail":"GM","microsoft":"MS",
    "amazon":"AM","apple":"AP","uber":"UB","lyft":"LF","paypal":"PP","viber":"VB",
    "line":"LN","wechat":"WC","yahoo":"YH","netflix":"NF","discord":"DC",
    "linkedin":"LI","shopify":"SH","binance":"BN","coinbase":"CB","steam":"ST","twitch":"TC",
}

def get_service_short(svc: str) -> str:
    s = svc.lower().strip()
    for k,v in _SVC_MAP.items():
        if k in s: return v
    clean = re.sub(r"[^a-zA-Z]","",svc)
    return clean[:2].upper() if clean else "OT"


def split_category_label(category: str) -> tuple[str, str, str]:
    """Return (flag, country, service) from a stored category label."""
    cat = (category or "").strip()
    if not cat:
        return "🌍", "Unknown", "Unknown"
    if " - " not in cat:
        return "🌍", "Unknown", cat
    left, service = cat.split(" - ", 1)
    words = left.strip().split(None, 1)
    if len(words) == 2:
        flag, country = words[0], words[1].strip()
    else:
        flag, country = "🌍", left.strip() or "Unknown"
    return flag or "🌍", country or "Unknown", service.strip() or "Unknown"


def get_country_icon_id(country: str) -> str:
    """Find the animated custom emoji id for a country button."""
    wanted = (country or "").strip().lower()
    for item in COUNTRY_DATA:
        if str(item.get("name", "")).strip().lower() == wanted:
            code = str(item.get("code", "")).upper()
            return COUNTRY_EMOJI_ID.get(code, COUNTRY_EMOJI_ID["DEFAULT"])
    return COUNTRY_EMOJI_ID["DEFAULT"]


def get_effective_assign_count(raw_limit: Optional[int]) -> int:
    """Treat unlimited/invalid limits as one active route at a time."""
    try:
        value = int(raw_limit)
    except (TypeError, ValueError):
        value = DEFAULT_ASSIGN_LIMIT
    return value if value > 0 else 1


def get_assign_limit_label(raw_limit: Optional[int] = None) -> str:
    return str(get_effective_assign_count(raw_limit if raw_limit is not None else DEFAULT_ASSIGN_LIMIT))


async def get_effective_limit(user_id: int) -> int:
    user_limit = await db.get_user_limit(user_id)
    target = user_limit if user_limit is not None else DEFAULT_ASSIGN_LIMIT
    return get_effective_assign_count(target)


async def get_user_assign_count(user_id: int) -> int:
    return await get_effective_limit(user_id)


def set_number_flow_state(user_id: int, mode: str, service: str = None,
                          country: str = None, flag: str = None):
    NUMBER_FLOW_STATE[user_id] = {
        "mode": mode,
        "service": service,
        "country": country,
        "flag": flag,
    }


def get_number_flow_state(user_id: int) -> dict:
    return NUMBER_FLOW_STATE.get(user_id, {})


def format_recent_history_block(history_rows: list, skip_otp: str = "",
                                title: str = "Previous OTPs On This Number") -> str:
    entries = []
    skip_once = bool(skip_otp)
    clean_skip = re.sub(r"[^0-9]", "", skip_otp or "")
    for row in history_rows or []:
        otp = re.sub(r"[^0-9]", "", getattr(row, "otp", "") or "")
        if not otp:
            continue
        if clean_skip and skip_once and otp == clean_skip:
            skip_once = False
            continue
        stamp = getattr(row, "created_at", None)
        stamp_txt = stamp.strftime("%d %b %H:%M") if stamp else "Unknown time"
        entries.append(f"• <code>{otp}</code>  <i>{stamp_txt}</i>")
    if not entries:
        return ""
    return f"{ui('clock')} <b>{title}</b>\n" + "\n".join(entries[:4])


def build_virtual_number_text(number_obj, country: str, service: str,
                              flag: str = "🌍", prefix: Optional[str] = None,
                              history_rows: list = None, current_otp: str = "",
                              current_message: str = "") -> str:
    assigned_at = getattr(number_obj, "assigned_at", None)
    assigned_txt = assigned_at.strftime("%Y-%m-%d %H:%M:%S") if assigned_at else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    clean_otp = re.sub(r"[^0-9]", "", current_otp or "")
    phone = getattr(number_obj, "phone_number", "")
    prefix_txt = html.escape(prefix) if prefix else "OFF"
    body = html.escape((current_message or "")[:220])
    lines = [
        f"{ui('diamond')} <b>Virtual Number</b>",
        D,
        f"{ui('earth')} <b>Country:</b> {html.escape(country)} {flag}",
        f"{app_emoji(service)} <b>Service:</b> {html.escape(service)}",
        f"{ui('phone')} <b>Assigned Number:</b> <code>+{phone}</code>",
        f"{ui('clock')} <b>Assigned:</b> {assigned_txt}",
        f"{ui('chart')} <b>Prefix:</b> {prefix_txt}",
        "",
    ]
    if clean_otp or body:
        lines.append(f"{ui('envelope')} <b>Latest Inbox</b>")
        if clean_otp:
            lines.append(f"{ui('key')} <b>OTP:</b> <code>{clean_otp}</code>")
        if body:
            lines.append(f"{ui('chat')} <b>SMS:</b> <i>{body}</i>")
        lines.append(f"{ui('satellite')} <b>Status:</b> Monitoring this route for more OTPs.")
    else:
        lines.append(f"{ui('bolt')} <b>Status:</b> Waiting for the first SMS on this number.")
    history_block = format_recent_history_block(history_rows, skip_otp=clean_otp)
    if history_block:
        lines.extend(["", history_block])
    return "\n".join(lines)


async def build_virtual_number_text_from_active(user_id: int, active: list,
                                                prefix: Optional[str] = None,
                                                latest_phone: str = None,
                                                latest_otp: str = "",
                                                latest_message: str = "") -> str:
    if not active:
        return styled_error("No Active Number", "Request a new virtual number to continue.")
    chosen = next((n for n in active if latest_phone and n.phone_number == latest_phone), active[0])
    flag, country, service = split_category_label(getattr(chosen, "category", ""))
    flow = get_number_flow_state(user_id)
    country = flow.get("country") or country
    service = flow.get("service") or service
    flag = flow.get("flag") or flag
    history_rows = await db.get_recent_history_for_number(chosen.phone_number, limit=6)
    return build_virtual_number_text(
        chosen,
        country=country,
        service=service,
        flag=flag,
        prefix=prefix,
        history_rows=history_rows,
        current_otp=latest_otp,
        current_message=latest_message,
    )

def get_message_body(rec: list) -> Optional[str]:
    noise = {"0","0.00","€","$","null","None",""}
    for idx in [4,5]:
        if len(rec) > idx:
            v = str(rec[idx]).strip()
            if v and v not in noise and len(v) > 1: return v
    return None

def parse_panel_dt(dt_str: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d %H:%M:%S","%Y/%m/%d %H:%M:%S","%d-%m-%Y %H:%M:%S"):
        try: return datetime.strptime(dt_str.strip(), fmt)
        except Exception: pass
    return None

def pbar(cur: int, total: int, length: int = 12) -> str:
    if total <= 0: return f"[{chr(9617)*length}] 0/0"
    f = int(length * cur / total)
    return f"[{chr(9608)*f}{chr(9617)*(length-f)}] {cur}/{total}"

D = "┄" * 22

# ── OTP GUI Theme ─────────────────────────────────────────────────
# 15 premium designs. Only super-admins can change. Clamp to index %40.
OTP_GUI_THEME = 0   # 0-14

_THEME_NAMES = {
    0: "💬 WhatsApp Pro",        # Service emoji: WhatsApp animated
    1: "📸 Instagram Business",  # Service emoji: Instagram animated
    2: "🔐 Security Clean",      # Minimalist security focus
    3: "💎 Premium VIP",         # Luxury animated gems
    4: "⚡ Speed Mode",          # Fast, OTP-only compact
    5: "🌍 Global Secure",       # Country flags + service
    6: "🚀 Rocket Elite",        # Animated rocket emoji
    7: "🔥 Fire Storm",          # Animated fire effects
    8: "⭐ Star VIP",            # Animated stars
    9: "🛡️ Shield Guardian",      # Security focused
    10: "🎭 Classic Elegance",   # Elegant professional theme
    11: "🌈 Rainbow Vibrant",    # Colorful and dynamic theme
    12: "🎯 Focus Mode",         # Minimalist focused theme
    13: "👑 Royal Premium",      # Premium luxury theme
    14: "🚁 Hover Luxury",       # Advanced luxury theme
}

def _get_bot_tag() -> str:
    nb = NUMBER_BOT_LINK or GET_NUMBER_URL or ""
    if "t.me/" in nb:
        u = nb.rstrip("/").split("t.me/")[-1].lstrip("@")
        if u: return f"@{u}"
    return f"@{BOT_USERNAME}" if BOT_USERNAME else "@CrackSMSReBot"

def mask_phone_for_display(phone: str) -> str:
    """
    Show +CC•••••LAST4 with bullet dots masking the middle.
    Always shows at least 4 bullet dots regardless of phone length.
    Example: +14155550123  →  +1415•••••0123
             +137375       →  +13•••75
    """
    digits = re.sub(r"[^0-9]", "", str(phone or ""))
    if not digits:
        return "+"
    if len(digits) >= 10:
        # Full number: show first 4 digits, mask middle, show last 4
        return f"+{digits[:4]}{'•'*5}{digits[-4:]}"
    elif len(digits) >= 7:
        # Medium: show first 2, mask middle, show last 3
        return f"+{digits[:2]}{'•'*4}{digits[-3:]}"
    elif len(digits) >= 4:
        # Short: show first 2, mask middle, show last 2
        return f"+{digits[:2]}{'•'*3}{digits[-2:]}"
    else:
        return f"+{digits[:1]}{'•'*3}"

def _e(eid, fb): return f'<tg-emoji emoji-id="{eid}">{fb}</tg-emoji>'
_FIRE  = lambda: _e("5402406965252989103","🔥")
_KEY   = lambda: _e("6176966310920983412","🔑")
_BOLT  = lambda: _e("5411590687663608498","⚡")
_LOCK  = lambda: _e("5291873529464122510","🔒")
_GEM   = lambda: _e("5235940101643883746","💎")

# ═══════════════════════════════════════════════════════════
#  COUNTRY LIVE TRAFFIC & ANALYTICS
# ═══════════════════════════════════════════════════════════
TRAFFIC_CACHE = {}  # {country: {service: count, "timestamp": datetime}}
TRAFFIC_CACHE_TTL = 3600  # 1 hour

# ═══════════════════════════════════════════════════════════
#  API TOKEN MANAGEMENT
# ═══════════════════════════════════════════════════════════
AWAITING_API_CREATE = {}  # {uid: {"step": "name|dev|panels", "name": "", "dev": "", "panels": []}}

async def get_country_live_traffic() -> Dict[str, Dict]:
    """Get live traffic data: top countries and services by available numbers."""
    try:
        now = datetime.now()
        
        # Get all categories with their available counts
        categories_data = await db.get_categories_summary()
        
        traffic_data = {}  # {country: {service: count}}
        
        for category, count in categories_data:
            if not category or " - " not in category:
                continue
            
            country_part = category.split(" - ")[0].strip()  # "🇺🇦 Ukraine"
            service = category.split(" - ")[1].strip() if len(category.split(" - ")) > 1 else "Unknown"
            
            if country_part not in traffic_data:
                traffic_data[country_part] = {}
            
            traffic_data[country_part][service] = count
        
        # Cache the data
        TRAFFIC_CACHE["data"] = traffic_data
        TRAFFIC_CACHE["timestamp"] = now
        
        return traffic_data
    except Exception as e:
        logger.error(f"Error getting live traffic: {e}")
        return {}

async def get_top_countries_and_services(limit: int = 5) -> tuple:
    """Return (top_countries, top_services) for broadcasting."""
    traffic = await get_country_live_traffic()
    
    if not traffic:
        return [], []
    
    # Count total numbers per country
    country_totals = {}
    service_totals = {}
    
    for country, services in traffic.items():
        total = sum(services.values())
        country_totals[country] = total
        
        for service, count in services.items():
            service_totals[service] = service_totals.get(service, 0) + count
    
    # Get top items
    top_countries = sorted(country_totals.items(), key=lambda x: x[1], reverse=True)[:limit]
    top_services = sorted(service_totals.items(), key=lambda x: x[1], reverse=True)[:limit]
    
    return top_countries, top_services

async def auto_broadcast_traffic_report(application):
    """Auto-broadcast traffic report every hour (if enabled)."""
    if not AUTO_BROADCAST_ON:
        return
    
    try:
        top_countries, top_services = await get_top_countries_and_services(5)
        
        if not top_countries and not top_services:
            return
        
        # Build traffic report message
        msg_lines = [
            f"{ui('chart')} <b>Live Traffic Report</b>",
            f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>",
            "",
            f"{ui('earth')} <b>Top Countries:</b>",
        ]
        
        for country, count in top_countries:
            msg_lines.append(f"  {country}: <b>{count:,}</b> numbers")
        
        msg_lines.extend(["", f"{ui('phone')} <b>Top Services:</b>"])
        for service, count in top_services:
            msg_lines.append(f"  {service}: <b>{count:,}</b> numbers")
        
        msg_lines.extend([
            "",
            "<b>━━━━━━━━━━━━━━━━━━━━━━</b>",
            f"<i>Updated: {datetime.now().strftime('%H:%M:%S UTC')}</i>"
        ])
        
        traffic_msg = "\n".join(msg_lines)
        
        # Broadcast to log groups
        for gid in await db.get_all_log_chats():
            try:
                await application.bot.send_message(gid, traffic_msg, parse_mode="HTML")
            except Exception:
                pass
        
        # Broadcast to all users (if enabled)
        users = await db.get_all_users()
        for uid in users:
            try:
                await application.bot.send_message(uid, traffic_msg, parse_mode="HTML")
            except Exception:
                pass
            await asyncio.sleep(0.05)
        
        logger.info(f"{ui('chart')} Traffic report broadcast complete to {len(users)} users")
    except Exception as e:
        logger.error(f"Error broadcasting traffic report: {e}")
_CROWN = lambda: _e("5319149831673887746","👑")
_STAR  = lambda: _e("5778458646534952216","⭐")
_SKULL = lambda: _e("5807631052251861399","💀")
_ROBOT = lambda: _e("5339267587337370029","🤖")

def build_otp_msg(header: str, count_badge: str, clean: str,
                  msg_body: str, svc: str, panel_name: str,
                  flag: str, region: str, dial: str, last5: str,
                  for_group: bool) -> str:
    """
    15 compact OTP designs. Only super-admins can change OTP_GUI_THEME.
    All designs follow the compact style seen in the reference screenshot:
    flag + service + number + OTP, ©By line. Each has a unique personality.
    """
    bot_tag  = _get_bot_tag()
    bt       = html.escape(bot_tag)
    body160  = html.escape(msg_body[:160])
    body260  = html.escape(msg_body[:260])
    aflag    = country_flag_emoji(region)
    aicon    = app_emoji_by_code(svc)
    # Build display number: +CC•••••LAST4
    # dial = country code (e.g. "1"), last5 = last 5 digits of full number
    # We show +CC followed by dots then the last 4 digits
    _dial_clean = dial.lstrip('+').strip() if dial else ""
    if _dial_clean and last5:
        _visible_end = last5[-4:] if len(last5) >= 4 else last5
        _dots        = '•' * 5
        num_full     = f"+{_dial_clean}{_dots}{_visible_end}"
        nd           = num_full
    else:
        num_full = f"+{_dial_clean}{last5}" if _dial_clean else f"+{last5}"
        nd       = mask_phone_for_display(num_full)
    num_star = nd
    now_ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # helper: formatted OTP  491-138 or 491138
    def fmt_otp(o):
        if len(o)==6: return f"{o[:3]}-{o[3:]}"
        if len(o)==8: return f"{o[:4]}-{o[4:]}"
        return o

    fotp = fmt_otp(clean) if clean else "—"

    t = OTP_GUI_THEME % 15

    # ── T0: WHATSAPP PRO (Service-branded) ──────
    if t == 0:
        if for_group:
            masked_num = mask_phone_for_display(num_full)
            return (f"{_FIRE()} {aflag} {region} | {aicon} {svc} | {ui('speak')} <i>Auto</i>\n"
                    f"{ui('receiver')} {masked_num}\n"
                    f"{_LOCK()} © {bt}") if clean else (
                    f"{_FIRE()} {aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code> {_FIRE()}\n"
                    f"<i>{body160}</i>\n<i>©By {bt}</i>")
        return (
            f"{_KEY()} <b>{aflag} {region} {svc} OTP Received!</b> {_KEY()}\n"
            f"\n"
            f"{ui('phone')} <b>Number:</b> <code>{num_star}</code>\n"
            f"{_KEY()} <b>OTP Code:</b> <code>{clean}</code>\n"
            f"\n"
            f"{ui('chat')} <i>{body260}</i>\n\n{_LOCK()} <i>©By {bt}</i> {_LOCK()}"
        ) if clean else (
            f"{aflag} {aicon} <b>{svc}</b>  <code>{nd}</code>\n"
            f"<b>{header}</b>\n{ui('chat')} <i>{body260}</i>\n<i>©By {bt}</i>")

    # ── T1: TEMPNUM ─── ENHANCED ──
    elif t == 1:
        if for_group:
            return (f"{_FIRE()} {aflag} <b>{region} {svc} OTP!</b> {ui('star')}\n\n"
                    f"┌─ {ui('clock')} <b>Time:</b> <code>{now_ts}</code>\n"
                    f"├─ {ui('earth')} <b>Country:</b> {region} {aflag}\n"
                    f"└─ {ui('pushpin')} <i>Waiting for copy...</i>\n"
                    f"<i>©By {bt}</i>") if clean else (
                    f"{aflag} {aicon} <b>{svc}</b>  <code>{nd}</code>\n"
                    f"<i>{body160}</i>\n<i>©By {bt}</i>")
        return (
            f"{_FIRE()} <b>{aflag} {region} {svc} OTP Received! {ui('star')} {_FIRE()}\n\n"
            f"┌─ {ui('clock')} <b>Time:</b> <code>{now_ts}</code>\n"
            f"├─ {ui('earth')} <b>Country:</b> {region} {aflag}\n"
            f"├─ {aicon} <b>Service:</b> #{svc}\n"
            f"├─ {ui('phone')} <b>Number:</b> <code>{num_star}</code>\n"
            f"├─ {_KEY()} <b>OTP:</b> <code>{clean}</code>\n"
            f"└─ {ui('chat')} <b>SMS:</b> <i>{body160}</i>\n\n"
            f"<i>©By {bt}</i>"
        ) if clean else (
            f"{_FIRE()} <b>{header}</b> {_FIRE()}\n"
            f"┌─ {ui('earth')} {region} {aflag}  | {aicon} #{svc}\n"
            f"└─ {ui('chat')} <i>{body260}</i>\n<i>©By {bt}</i>")

    # ── T2: NEON ELECTRIC ──────────────── ENHANCED DESIGN ──────────
    elif t == 2:
        if for_group:
            return (f"{_BOLT()} {aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code> {_BOLT()}\n"
                    f"<i>{bt}</i>") if clean else (
                    f"{ui('satellite')} {aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code>\n"
                    f"<i>{body160}</i>\n<i>{bt}</i>")
        return (
            f"{_BOLT()}{_BOLT()}{_BOLT()} <b>{header}</b> {_BOLT()}{_BOLT()}{_BOLT()}\n"
            f"╔{'═'*24}╗\n"
            f"  {aflag} {aicon} <b>#{svc}</b>  {_BOLT()} <code>{nd}</code>\n"
            f"╚{'═'*24}╝\n"
            f"{_BOLT()} <b>OTP:</b> {_GEM()} <code>{clean}</code> {_GEM()}\n"
            f"{'─'*28}\n"
            f"{ui('chat')} <i>{body260}</i>\n<i>{bt}</i>"
        ) if clean else (
            f"{ui('satellite')} <b>{header}</b>\n{aflag} {aicon} <b>#{svc}</b>\n"
            f"{ui('chat')} <i>{body260}</i>\n<i>{bt}</i>")

    # ── T3: PREMIUM DARK ─────────────────────────────────────────────
    elif t == 3:
        if for_group:
            return (f"{aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code>  {_FIRE()}\n\n"
                    f"{ui('lock')} Hidden OTP\n<i>©By {bt}</i>") if clean else (
                    f"{aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code>\n"
                    f"<i>{body160}</i>\n<i>©By {bt}</i>")
        return (
            f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
            f"  {aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code>  {_FIRE()}\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            f"{_KEY()} <b>OTP:</b>  <code>{clean}</code>\n\n"
            f"{ui('chat')} <i>{body260}</i>\n<i>©By {bt}</i>"
        ) if clean else (
            f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
            f"  {aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code>\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
            f"{ui('chat')} <i>{body260}</i>\n<i>©By {bt}</i>")

    # ── T4: MINIMAL CLEAN ────────────────────────────────────────────
    elif t == 4:
        if for_group:
            return (f"{_BOLT()} {aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code> {_BOLT()}\n<i>{bt}</i>") if clean else (
                    f"{_BOLT()} {aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code>\n<i>{body160}</i>\n{_BOLT()} {bt}")
        return (
            f"{_STAR()} {aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code> {_STAR()}\n"
            f"{'─'*20}\n"
            f"{_KEY()} <b>OTP:</b>  <code>{clean}</code>\n"
            f"{'─'*20}\n{ui('chat')} <i>{body260}</i>\n<i>{bt}</i>"
        ) if clean else (
            f"{_STAR()} {aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code>\n"
            f"{'─'*20}\n{ui('chat')} <i>{body260}</i>\n<i>{bt}</i>")

    # ── T5: ROYAL GOLD ───────────────────────────────────────────────
    elif t == 5:
        if for_group:
            return (f"{_CROWN()} {aflag} <b>#{svc}</b>  <code>{nd}</code> {ui('star')}\n"
                    f"{ui('star')} <i>{bt}</i> {ui('star')}") if clean else (
                    f"{ui('star')} {aflag} <b>#{svc}</b>  <code>{nd}</code>\n"
                    f"<i>{body160}</i>\n{ui('star')} <i>{bt}</i> {ui('star')}")
        return (
            f"{_CROWN()} {'─'*20} {_CROWN()}\n"
            f"{ui('star')} <b>{header}</b> {ui('star')}\n"
            f"{_CROWN()} {'─'*20} {_CROWN()}\n\n"
            f"{ui('star')} {aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code> {ui('star')}\n"
            f"{_KEY()} <code>{clean}</code>\n\n"
            f"{ui('chat')} <i>{body260}</i>\n"
            f"{_CROWN()} <i>©By {bt}</i> {_CROWN()}"
        ) if clean else (
            f"{ui('star')} <b>{header}</b>\n{ui('star')} {aflag} {aicon} <b>#{svc}</b>\n"
            f"{ui('chat')} <i>{body260}</i>\n{ui('star')} <i>©By {bt}</i> {ui('star')}")

    # ── T6: JACK-X ───────────────────────────────────────────────────
    elif t == 6:
        if for_group:
            return (f"{_STAR()} {aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code>  {_FIRE()}\n<i>©By {bt}</i>") if clean else (
                    f"→ {aflag} <b>#{svc}</b>  <code>{nd}</code>\n<i>{body160}</i>\n<i>©By {bt}</i>")
        return (
            f"{_BOLT()} <b>{bt}</b>  {_STAR()}\n"
            f"→ {aflag} {aicon} <b>#{svc}</b>  [{region}]  <code>{nd}</code>  {_FIRE()}\n\n"
            f"<i>{body260}</i>\n{_GEM()} <i>©By {bt}</i> {_GEM()}"
        ) if clean else (
            f"{_BOLT()} <b>{bt}</b>\n→ {aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code>\n"
            f"{ui('chat')} <i>{body260}</i>\n{_GEM()} <i>©By {bt}</i> {_GEM()}")

    # ── T7: CYBER MATRIX ─────────────────────────────────────────────
    elif t == 7:
        if for_group:
            return (f"{_FIRE()} {aflag} #{svc}  <code>{nd}</code> {_BOLT()}\n"
                    f"<i>©By {bt}</i>") if clean else (
                    f"{_BOLT()} {aflag} #{svc}  <code>{nd}</code>\n"
                    f"<i>{body160}</i>\n<i>©By {bt}</i>")
        return (
            f"<b>[ {_BOLT()} CRACK SMS {_BOLT()} ]</b>\n{'─'*24}\n"
            f"  {aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code>  {_FIRE()}\n"
            f"{'─'*24}\n  {_KEY()} <b>OTP CODE:</b> {_SKULL()}\n  <code>{clean}</code>\n"
            f"{'─'*24}\n  {_LOCK()} {ui('chat')} <i>{body260}</i>\n"
            f"  {_SKULL()} <i>©By {bt}</i> {_SKULL()}"
        ) if clean else (
            f"<b>[ {_BOLT()} CRACK SMS {_BOLT()} ]</b>\n"
            f"  {aflag} <b>#{svc}</b>  <code>{nd}</code>\n"
            f"  {_LOCK()} <i>{body260}</i>\n  <i>©By {bt}</i>")

    # ── T8: FIRE STORM ───────────────────────────────────────────────
    elif t == 8:
        if for_group:
            return (f"{_FIRE()}{_FIRE()} {aflag} <b>#{svc}</b>  <code>{nd}</code>\n"
                    f"<i>{bt}</i>") if clean else (
                    f"{_FIRE()} {aflag} <b>#{svc}</b>  <code>{nd}</code>\n"
                    f"<i>{body160}</i>  <i>{bt}</i>")
        return (
            f"{_FIRE()}{_FIRE()}{_FIRE()} <b>{header}</b> {_FIRE()}{_FIRE()}{_FIRE()}\n\n"
            f"{aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code>\n"
            f"{_KEY()} OTP: <code>{clean}</code>\n\n"
            f"{ui('chat')} <i>{body260}</i>\n<i>©By {bt}</i>"
        ) if clean else (
            f"{_FIRE()} <b>{header}</b>\n{aflag} {aicon}  <code>{nd}</code>\n"
            f"{ui('chat')} <i>{body260}</i>\n<i>©By {bt}</i>")

    # ── T9: ICE BLUE ─────────────────────────────────────────────────
    elif t == 9:
        if for_group:
            return (f"{ui('snow')} {aflag} <b>#{svc}</b>  <code>{nd}</code>  {ui('ice')}\n"
                    f"<i>{bt}</i>") if clean else (
                    f"{ui('snow')} {aflag} <b>#{svc}</b>  <code>{nd}</code>\n"
                    f"<i>{body160}</i>  <i>{bt}</i>")
        return (
            f"{ui('snow')} <b>━━━━━━━━━━━━━━━━━━</b> {ui('ice')}\n"
            f"  {aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code>\n"
            f"  {_KEY()} OTP: <code>{clean}</code>\n"
            f"{ui('snow')} <b>━━━━━━━━━━━━━━━━━━</b> {ui('ice')}\n"
            f"{ui('chat')} <i>{body260}</i>\n<i>©By {bt}</i>"
        ) if clean else (
            f"{ui('snow')} <b>━━━━━━━━━━━━━━━━━━</b> {ui('ice')}\n"
            f"  {aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code>\n"
            f"{ui('chat')} <i>{body260}</i>\n<i>©By {bt}</i>")

    # ── T10: CLASSIC ELEGANCE ──────────────────────────────────────
    elif t == 10:
        if for_group:
            return (f"{ui('notepad')} {aflag} <b>#{svc}</b>  <code>{nd}</code>\n"
                    f"<i>{bt}</i>") if clean else (
                    f"{ui('notepad')} {aflag} <b>#{svc}</b>  <code>{nd}</code>\n"
                    f"<i>{body160}</i>\n<i>{bt}</i>")
        return (
            f"┌{'─'*26}┐\n"
            f"│ {aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code> │\n"
            f"├{'─'*26}┤\n"
            f"│ {_KEY()} <code>{clean}</code> │\n"
            f"└{'─'*26}┘\n"
            f"{ui('chat')} <i>{body260}</i>\n<i>©By {bt}</i>"
        ) if clean else (
            f"┌{'─'*26}┐\n"
            f"│ {aflag} {aicon} <b>#{svc}</b> │\n"
            f"├{'─'*26}┤\n"
            f"│ {ui('chat')} {body260[:20]} │\n"
            f"└{'─'*26}┘\n<i>©By {bt}</i>")

    # ── T11: RAINBOW VIBRANT ─────────────────────────────────────
    elif t == 11:
        if for_group:
            return (f"🌈 {aflag} <b>#{svc}</b>  <code>{nd}</code>\n"
                    f"<i>{bt}</i>") if clean else (
                    f"🌈 {aflag} <b>#{svc}</b>  <code>{nd}</code>\n"
                    f"<i>{body160}</i>\n<i>{bt}</i>")
        return (
            f"🌈 <b>{aflag} {region} {svc} OTP</b> 🌈\n"
            f"{'▓'*26}\n"
            f"{ui('phone')} <b>Number:</b> <code>{num_star}</code>\n"
            f"{_KEY()} <b>OTP:</b> <code>{clean}</code>\n"
            f"{'▓'*26}\n"
            f"<i>{body260}</i>\n🌈 <i>©By {bt}</i> 🌈"
        ) if clean else (
            f"🌈 <b>{header}</b> 🌈\n{aflag} {aicon} <b>#{svc}</b>\n"
            f"<i>{body260}</i>\n🌈 <i>©By {bt}</i> 🌈")

    # ── T12: FOCUS MODE ──────────────────────────────────────────
    elif t == 12:
        if for_group:
            return (f"{ui('focus')} {aflag} <b>#{svc}</b>  <code>{nd}</code>\n"
                    f"<i>{bt}</i>") if clean else (
                    f"{ui('focus')} {aflag} <b>#{svc}</b>  <code>{nd}</code>\n"
                    f"<i>{body160}</i>\n<i>{bt}</i>")
        return (
            f"{ui('focus')} <b>{header}</b>\n"
            f"{aflag} {aicon} {svc}\n"
            f"{ui('phone')} {num_star}\n"
            f"{_KEY()} <code>{clean}</code>\n"
            f"{ui('chat')} <i>{body260}</i>\n<i>©{bt}</i>"
        ) if clean else (
            f"{ui('focus')} <b>{header}</b>\n{aflag} {aicon} {svc}\n"
            f"{ui('chat')} <i>{body260}</i>\n<i>©{bt}</i>")

    # ── T13: ROYAL PREMIUM ───────────────────────────────────────
    elif t == 13:
        if for_group:
            return (f"{_CROWN()} {aflag} <b>#{svc}</b>  <code>{nd}</code> {_CROWN()}\n"
                    f"<i>{bt}</i>") if clean else (
                    f"{_CROWN()} {aflag} <b>#{svc}</b>  <code>{nd}</code>\n"
                    f"<i>{body160}</i>\n<i>{bt}</i>")
        return (
            f"{_CROWN()} {'═'*20} {_CROWN()}\n"
            f"{_STAR()} <b>{header}</b> {_STAR()}\n"
            f"{_CROWN()} {'═'*20} {_CROWN()}\n"
            f"{aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code>\n"
            f"{_KEY()} <code>{clean}</code>\n"
            f"{ui('chat')} <i>{body260}</i>\n"
            f"{_CROWN()} {_STAR()} <i>©By {bt}</i> {_STAR()} {_CROWN()}"
        ) if clean else (
            f"{_CROWN()} {'═'*20} {_CROWN()}\n"
            f"{_STAR()} <b>{header}</b> {_STAR()}\n"
            f"{_CROWN()} {'═'*20} {_CROWN()}\n"
            f"{ui('chat')} <i>{body260}</i>\n{_CROWN()} <i>©By {bt}</i> {_CROWN()}")

    # ── T14: HOVER LUXURY ────────────────────────────────────────
    elif t == 14:
        if for_group:
            return (f"{ui('diamond')} {aflag} <b>#{svc}</b>  <code>{nd}</code> {ui('diamond')}\n"
                    f"<i>{bt}</i>") if clean else (
                    f"{ui('diamond')} {aflag} <b>#{svc}</b>  <code>{nd}</code>\n"
                    f"<i>{body160}</i>\n<i>{bt}</i>")
        return (
            f"{ui('diamond')} {'█'*22} {ui('diamond')}\n"
            f"  {aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code>\n"
            f"{ui('diamond')} {'█'*22} {ui('diamond')}\n"
            f"  {_KEY()} <code>{clean}</code>\n"
            f"{ui('diamond')} {'█'*22} {ui('diamond')}\n"
            f"  {ui('chat')} <i>{body260}</i>\n"
            f"  {ui('diamond')} <i>©By {bt}</i> {ui('diamond')}"
        ) if clean else (
            f"{ui('diamond')} {'█'*22} {ui('diamond')}\n"
            f"  {aflag} {aicon} <b>#{svc}</b>  <code>{nd}</code>\n"
            f"{ui('diamond')} {'█'*22} {ui('diamond')}\n"
            f"  {ui('chat')} <i>{body260}</i>\n"
            f"  {ui('diamond')} <i>©By {bt}</i> {ui('diamond')}")

    else:
        # ── DEFAULT FALLBACK: Use Shield theme for out-of-range indices
        if for_group:
            return f"{ui('shield')} {aflag} {aicon} {ui('lock')} Hidden"
        return f"{aflag} <b>{region} {svc}</b>\n{ui('key')} {ui('lock')} Hidden OTP\n{ui('phone')} {num_star}\n<i>©{bt}</i>"


def hide_otp_in_message(msg_text: str, clean_otp: str = "") -> str:
    """
    PRIVACY FEATURE: Hide OTP code in message text, replace with visual placeholders.
    OTP code will only be visible in the hidden button that user must click to reveal.
    """
    if not clean_otp or not msg_text:
        return msg_text
    
    # Hide any occurrence of the OTP code (exact digits) with a privacy placeholder
    # Format patterns: 123456, 123-456, 1234-5678
    otp_no_sep = re.sub(r"[^0-9]", "", clean_otp)
    
    # Replace various formats of the OTP with privacy message
    privacy_placeholder = "🔐 ✦✦✦✦✦✦"
    
    # Pattern 1: OTP in code tags (most common)
    msg_text = re.sub(rf"<code>\??{re.escape(clean_otp)}\??</code>", privacy_placeholder, msg_text, flags=re.IGNORECASE)
    msg_text = re.sub(rf"<code>\d{{3,8}}</code>", privacy_placeholder, msg_text, count=1, flags=re.IGNORECASE)
    
    # Pattern 2: OTP in plain text with various separators
    msg_text = msg_text.replace(clean_otp, privacy_placeholder)
    msg_text = msg_text.replace(clean_otp.replace("-", ""), privacy_placeholder)
    
    return msg_text

def add_dynamic_design_elements(msg_text: str, theme_id: int) -> str:
    """
    💫 DYNAMIC DESIGN ENHANCEMENT: Add animated/visual elements to OTP messages.
    Creates visually appealing, modern OTP GUI with dynamic effects.
    Supports all 15 unique themes with distinct animations.
    """
    # Animation signature based on theme (exactly 15 themes: 0-14)
    animations = {
        0: "✨",  # CLASSIC - Sparkles (Professional Standard)
        1: "➖",  # MINIMAL - Minimalist (Essential Only)
        2: "💻",  # DEVELOPER - Computer (Dev-Focused)
        3: "⚡",  # ELECTRIC - Lightning (Tech-Savvy)
        4: "🔬",  # TECH - Science (Advanced Users)
        5: "💎",  # PREMIUM - Diamond (Premium Features)
        6: "🎲",  # ULTRAMINIMAL - Dice (Extremely Compact)
        7: "💼",  # BUSINESS - Briefcase (Professional Enterprise)
        8: "🌐",  # SOCIAL - Globe (Community-Focused)
        9: "🌟",  # DELUXE - Star (All Features)
        10: "🎭", # CLASSIC ELEGANCE - Theater (Sophisticated)
        11: "🌈", # RAINBOW VIBRANT - Rainbow (Colorful)
        12: "🎯", # FOCUS MODE - Target (Minimalist)
        13: "👑", # ROYAL PREMIUM - Crown (Luxury)
        14: "🚁", # HOVER LUXURY - Helicopter (Elite)
    }
    
    anim = animations.get(theme_id % 15, "✨")
    
    # Add dynamic header animation signature
    if "═" in msg_text or "━" in msg_text or "─" in msg_text:
        # Has borders - enhance with animation
        pass  # Already has visual appeal
    
    # Add subtle metadata to make it feel live/dynamic
    timestamp = datetime.now().strftime("%H:%M:%S")
    
    # For themes without timestamp, optionally add it (can be disabled)
    return msg_text

# ═══════════════════════════════════════════════════════════
#  GLOBAL STATE
# ═══════════════════════════════════════════════════════════
PANELS:               List              = []
IVAS_TASKS:           Dict[str,asyncio.Task] = {}
PROCESSED_MESSAGES:   set               = set()
OTP_SESSION_COUNTS:   Dict[str,int]     = {}
LAST_CHANGE_TIME:     Dict[int,datetime]= {}
NUMBER_FLOW_STATE:    Dict[int,dict]    = {}
CATEGORY_MAP:         Dict[str,str]     = {}
PANEL_ADD_STATES:     Dict[int,dict]    = {}
PANEL_EDIT_STATES:    Dict[int,dict]    = {}
AWAITING_ADMIN_ID:    Dict[int,bool]    = {}
AWAITING_PERMISSIONS: Dict[tuple,list]  = {}
AWAITING_LOG_ID:      Dict[int,bool]    = {}
BOT_ADD_STATES:       Dict[int,dict]    = {}
AWAITING_SUPER_ADMIN: Dict[int,bool]    = {}
CREATE_BOT_STATES:    Dict[int,dict]    = {}
BOT_REQUESTS:         Dict[str,dict]    = {}   # pending bot creation requests (6-digit ID key)
REQUEST_ID_COUNTER:   int               = 100000  # Start 6-digit ID counter
AUTO_BROADCAST_ON:    bool              = True  # auto-broadcast on number upload
AWAITING_REQ_CHAT:    Dict[int,bool]    = {}   # waiting for group/channel ID to add
app = None

VWEB_COUNTRIES_PER_PAGE = 12
VWEB_SERVICES_PER_PAGE = 10

# Generate 6-digit request ID
def generate_request_id() -> str:
    global REQUEST_ID_COUNTER
    REQUEST_ID_COUNTER += 1
    if REQUEST_ID_COUNTER > 999999:
        REQUEST_ID_COUNTER = 100000
    return f"REQ{REQUEST_ID_COUNTER}"


def sync_website_requests_into_bot_requests() -> list:
    pending_rows = []
    for req in load_website_requests():
        if req.get("status", "pending") != "pending":
            continue
        req_id = str(req.get("req_id", "")).strip()
        if not req_id:
            continue
        pending_rows.append(req)
        existing = BOT_REQUESTS.get(req_id)
        if existing:
            existing.update(req)
        else:
            BOT_REQUESTS[req_id] = req.copy()
    return pending_rows


def _safe_short(value: str, limit: int = 60) -> str:
    text = str(value or "—").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_bot_request_admin_summary(req: dict) -> str:
    source = "Website" if req.get("source") == "website" else "Bot"
    contact_method = _safe_short(req.get("contact_method") or "Pending")
    contact_target = _safe_short(req.get("contact_target") or "—")
    request_id = html.escape(str(req.get("req_id", "?")))
    requester = html.escape(str(req.get("user_name", "Unknown requester")))
    requester_uid = html.escape(str(req.get("uid", "—")))
    created_at = _safe_short(req.get("created_at") or "—", 25)
    return (
        f"{ui('robot')} <b>Bot Creation Request</b>\n{D}\n"
        f"{ui('desktop')} Source: <b>{html.escape(source)}</b>\n"
        f"{ui('copy')} Request ID: <code>{request_id}</code>\n"
        f"{ui('user')} Requester: <b>{requester}</b> (<code>{requester_uid}</code>)\n"
        f"{ui('calendar')} Created: <code>{html.escape(created_at)}</code>\n"
        f"{ui('phone')} Contact: <b>{html.escape(contact_method.title())}</b> • <code>{html.escape(contact_target)}</code>\n\n"
        f"{ui('robot')} Bot Name: <code>{html.escape(str(req.get('bot_name', '?')))}</code>\n"
        f"{ui('robot')} Username: <code>@{html.escape(str(req.get('bot_username', '?')).lstrip('@'))}</code>\n"
        f"{ui('key')} Token: <code>{html.escape(str(req.get('token', '?'))[:20])}...</code>\n"
        f"{ui('people')} Admin ID: <code>{html.escape(str(req.get('admin_id', '?')))}</code>\n"
        f"{ui('megaphone')} Channel: <code>{html.escape(_safe_short(req.get('channel'), 55))}</code>\n"
        f"{ui('chat')} OTP Group: <code>{html.escape(_safe_short(req.get('otp_group'), 55))}</code>\n"
        f"{ui('receiver')} Number Bot: <code>{html.escape(_safe_short(req.get('number_bot'), 55))}</code>\n"
        f"{ui('support')} Support: <code>{html.escape(_safe_short(req.get('support'), 40))}</code>\n"
        f"{ui('focus')} Group ID: <code>{html.escape(str(req.get('group_id', '—')))}</code>"
    )


def bot_request_review_kb(req_id: str) -> "InlineKeyboardMarkup":
    return InlineKeyboardMarkup([[
        btn("Approve", cbd=f"approvebot_{req_id}", style="success", icon=_UI["check"][0]),
        btn("Reject", cbd=f"rejectbot_{req_id}", style="danger", icon=_UI["cancel"][0]),
    ]])


async def notify_admins_about_website_request(bot_app, req: dict) -> int:
    req_id = str(req.get("req_id", "")).strip()
    if not req_id:
        return 0
    sent_count = 0
    message = build_bot_request_admin_summary(req)
    for admin_id in INITIAL_ADMIN_IDS:
        try:
            await bot_app.bot.send_message(
                chat_id=admin_id,
                text=message,
                reply_markup=bot_request_review_kb(req_id),
                parse_mode="HTML",
            )
            sent_count += 1
        except Exception as e:
            logger.warning(f"Website request notify failed for admin {admin_id}: {e}")
    updates = {"admin_notified_count": sent_count}
    if sent_count > 0:
        updates["admin_notified_at"] = datetime.now().isoformat()
    else:
        updates["admin_notified_at"] = None
    update_website_request(req_id, **updates)
    return sent_count


async def website_request_sync_job(context: ContextTypes.DEFAULT_TYPE):
    pending_rows = sync_website_requests_into_bot_requests()
    for req in pending_rows:
        if req.get("admin_notified_at"):
            continue
        req_id = str(req.get("req_id", "")).strip()
        if not req_id:
            continue
        try:
            sent_count = await notify_admins_about_website_request(context.application, req)
            logger.info(f"Website request {req_id} synced to {sent_count} admin(s)")
        except Exception as e:
            logger.error(f"Website request sync failed for {req_id}: {e}", exc_info=True)

# ── OTP GUI Style (1-5, saved in config.json, changes all message formats) ──
# GUI_STYLE removed — OTP_GUI_THEME (0-29) is the single theme variable

# ═══════════════════════════════════════════════════════════
#  ERROR HANDLING & DECORATORS  (Professional error management)
# ═══════════════════════════════════════════════════════════

def safe_handler(func):
    """Decorator: Safely handles errors in handlers without breaking the bot."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            return await func(update, context)
        except TelegramForbidden:
            logger.warning(f"User blocked bot: {update.effective_user.id}")
        except TelegramTimedOut:
            logger.warning(f"Telegram timeout for user {update.effective_user.id}")
        except TelegramNetworkError as e:
            logger.error(f"Network error: {e}")
        except Exception as e:
            logger.error(f"Handler error in {func.__name__}: {e}", exc_info=True)
            try:
                if update.message:
                    await update.message.reply_text(
                        "⚠️ <b>An error occurred</b>\\n\\n"
                        "Please try again later or contact support.",
                        parse_mode="HTML"
                    )
            except:
                pass
    return wrapper

def admin_only(perms: list = None):
    """Decorator: Checks if user is admin before allowing access."""
    def decorator(func):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            uid = update.effective_user.id
            user_perms = await get_admin_permissions(uid)
            is_sup = is_super_admin(uid)
            
            if not (user_perms or is_sup):
                await update.message.reply_text(
                    "🔒 <b>Admin Access Required</b>\\n\\n"
                    "You don't have permission to use this command.",
                    parse_mode="HTML"
                )
                return
            
            if perms and not is_sup:
                for perm in perms:
                    if perm not in user_perms:
                        await update.message.reply_text(
                            f"🚫 <b>Permission Denied</b>\n\n"
                            f"Required: {PERMISSIONS.get(perm, perm)}",
                            parse_mode="HTML"
                        )
                        return
            
            return await func(update, context)
        return wrapper
    return decorator

def rate_limit(seconds: int = 2):
    """Decorator: Rate limits handler calls per user."""
    user_times = {}
    
    def decorator(func):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            uid = update.effective_user.id
            now = datetime.now()
            
            if uid in user_times:
                elapsed = (now - user_times[uid]).total_seconds()
                if elapsed < seconds:
                    await update.message.reply_text(
                        f"{ui('clock')} <b>Please wait {seconds - int(elapsed)}s before trying again.</b>",
                        parse_mode="HTML"
                    )
                    return
            
            user_times[uid] = now
            return await func(update, context)
        return wrapper
    return decorator
# ═══════════════════════════════════════════════════════════
class PanelSession:
    """
    Each PanelSession owns a completely isolated aiohttp.ClientSession with its
    own CookieJar.  This means two different accounts on the same panel host
    (same base_url, different username/password) never share cookies or auth
    state — they are treated as entirely separate HTTP clients.
    """
    def __init__(self, base_url, username=None, password=None,
                 name="Unknown", panel_type="login", token=None, uri=None):
        self.base_url = base_url.rstrip("/")
        self.username = username; self.password = password
        self.name = name; self.panel_type = panel_type
        self.token = token; self.uri = uri
        self.login_url = f"{self.base_url}/login" if panel_type=="login" else None
        self.api_url = base_url; self.sesskey = None
        self.is_logged_in = False; self.id = None
        self.last_login_attempt = None; self.fail_count = 0
        self.stats_url: Optional[str] = None  # stored during endpoint discovery
        # Each PanelSession gets its OWN CookieJar — fully isolated from every
        # other session even when the same host is used by multiple accounts.
        self._cookie_jar = aiohttp.CookieJar(unsafe=True)
        self._session: Optional[aiohttp.ClientSession] = None

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # High-performance connector — 48-core server can handle more connections
            connector = aiohttp.TCPConnector(
                limit=100,           # max concurrent connections per session
                limit_per_host=20,   # max per host (prevents hammering one panel)
                ttl_dns_cache=300,   # cache DNS 5 minutes
                enable_cleanup_closed=True,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                headers={"User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36")},
                cookie_jar=self._cookie_jar)
        return self._session

    async def reset_session(self):
        """Close HTTP session and wipe cookies — call before re-login."""
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        # Fresh CookieJar so stale cookies from a failed login don't interfere
        self._cookie_jar = aiohttp.CookieJar(unsafe=True)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

# ═══════════════════════════════════════════════════════════
#  PANEL DB HELPERS
# ═══════════════════════════════════════════════════════════
async def init_panels_table():
    async with db.AsyncSessionLocal() as s:
        await s.execute(stext("""
            CREATE TABLE IF NOT EXISTS panels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL, base_url TEXT NOT NULL,
                username TEXT, password TEXT, sesskey TEXT, api_url TEXT,
                token TEXT, uri TEXT, panel_type TEXT DEFAULT 'login',
                is_logged_in INTEGER DEFAULT 0, last_login_attempt TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""))
        await s.commit()

async def migrate_panels_table():
    async with db.AsyncSessionLocal() as s:
        cols = [r[1] for r in (await s.execute(stext("PRAGMA table_info(panels)"))).fetchall()]
        for col, defval in [("token","TEXT"),("panel_type","TEXT DEFAULT 'login'"),("uri","TEXT")]:
            if col not in cols:
                try: await s.execute(stext(f"ALTER TABLE panels ADD COLUMN {col} {defval}"))
                except Exception: pass
        await s.commit()

async def refresh_panels_from_db():
    global PANELS
    async with db.AsyncSessionLocal() as s:
        rows = (await s.execute(stext("SELECT * FROM panels"))).fetchall()
    new = []
    for r in rows:
        p = PanelSession(base_url=r[2],username=r[3],password=r[4],
                         name=r[1],panel_type=r[9] or "login",token=r[7],uri=r[8])
        p.id=r[0]; p.sesskey=r[5]; p.api_url=r[6] or r[2]
        p.is_logged_in=bool(r[10]); p.last_login_attempt=r[11]
        old = next((x for x in PANELS if x.id==p.id), None)
        if old: p._session = old._session
        new.append(p)
    PANELS = new

async def add_panel_to_db(name,base_url,username,password,panel_type="login",token=None,uri=None):
    async with db.AsyncSessionLocal() as s:
        await s.execute(stext(
            "INSERT INTO panels (name,base_url,username,password,panel_type,token,uri) "
            "VALUES (:n,:u,:us,:pw,:pt,:tk,:uri)"),
            dict(n=name,u=base_url,us=username,pw=password,pt=panel_type,tk=token,uri=uri))
        await s.commit()

async def update_panel_in_db(pid,name,base_url,username,password,panel_type,token,uri):
    async with db.AsyncSessionLocal() as s:
        await s.execute(stext(
            "UPDATE panels SET name=:n,base_url=:u,username=:us,password=:pw,"
            "panel_type=:pt,token=:tk,uri=:uri WHERE id=:id"),
            dict(n=name,u=base_url,us=username,pw=password,pt=panel_type,tk=token,uri=uri,id=pid))
        await s.commit()

async def delete_panel_from_db(pid: int):
    async with db.AsyncSessionLocal() as s:
        await s.execute(stext("DELETE FROM panels WHERE id=:id"),{"id":pid})
        await s.commit()

async def update_panel_login(pid,sesskey,api_url,logged_in:bool):
    async with db.AsyncSessionLocal() as s:
        await s.execute(stext(
            "UPDATE panels SET sesskey=:sk,api_url=:au,is_logged_in=:li,"
            "last_login_attempt=:now WHERE id=:id"),
            dict(sk=sesskey,au=api_url,li=1 if logged_in else 0,now=datetime.now(),id=pid))
        await s.commit()

async def load_panels_from_dex_to_db():
    """
    Load panels from dex.txt into the database.

    OLD behaviour: if count: return  — skipped entirely when even ONE panel
    already existed, so new dex.txt entries were never picked up after the
    first run.

    NEW behaviour: reads every entry and inserts only those whose name is not
    already in the database.  Adding panels to dex.txt and restarting is now
    enough — no need to wipe the database.

    Comment lines (starting with #) are stripped before parsing so example
    values like PANEL_BASE_URL = "<http://ip/ints>" in the header never
    accidentally create a phantom panel entry.
    """
    to_add = []

    if os.path.exists(DEX_FILE):
        try:
            raw = open(DEX_FILE, encoding="utf-8").read()
            # Remove comment lines so header examples never match the regex
            clean = "\n".join(
                l for l in raw.splitlines() if not l.strip().startswith("#"))

            for block in clean.split("panel="):
                if not block.strip():
                    continue
                name = block.strip().split("\n")[0].strip()
                if not name or name.startswith("<"):   # skip placeholder "<n>"
                    continue
                url = re.search(r'PANEL_BASE_URL\s*=\s*["\'\']([^"\'\']+)["\'\']', block)
                usr = re.search(r'PANEL_USERNAME\s*=\s*["\'\']([^"\'\']+)["\'\']', block)
                pw  = re.search(r'PANEL_PASSWORD\s*=\s*["\'\']([^"\'\']+)["\'\']', block)
                if not (url and usr and pw):
                    continue
                base_url = url.group(1).rstrip("/")
                if base_url.startswith("<") or not base_url.startswith("http"):
                    continue   # skip any remaining comment-derived junk
                to_add.append((name, base_url, usr.group(1), pw.group(1), "login", None, None))
                logger.info(f"{ui('notepad')} DEX entry found: {name}  →  {base_url}  user={usr.group(1)}")
        except Exception as e:
            logger.error(f"{ui('cancel')} DEX read error: {e}", exc_info=True)

    # Built-in CR-API panel — always ensure it is present
    to_add.append((
        "TEST API",
        "http://147.135.212.197/crapi/had/viewstats",
        None, None, "api",
        "R1NQRTRSQopzh1aHZHSCfmiCklpycXSBeFV3QmaAdGtidFJeWItQ",
        None,
    ))

    inserted = 0
    skipped  = 0
    async with db.AsyncSessionLocal() as s:
        # Only insert panels whose name does not already exist in the database
        existing = {
            r[0] for r in
            (await s.execute(stext("SELECT name FROM panels"))).fetchall()
        }
        for name, url, usr, pw, pt, tok, uri in to_add:
            if name in existing:
                skipped += 1
                continue
            await s.execute(
                stext("INSERT INTO panels "
                      "(name,base_url,username,password,panel_type,token,uri) "
                      "VALUES (:n,:u,:us,:pw,:pt,:tk,:uri)"),
                dict(n=name, u=url, us=usr, pw=pw, pt=pt, tk=tok, uri=uri))
            inserted += 1
        await s.commit()

    logger.info(
        f"{ui('check')} DEX load done — {inserted} new panel(s) inserted, "
        f"{skipped} already in DB")


# ═══════════════════════════════════════════════════════════
#  ADMIN PERMISSIONS
# ═══════════════════════════════════════════════════════════
async def init_permissions_table():
    """
    Create or migrate the admin_permissions table.

    Uses db.ENGINE directly (not the ORM session) so DDL statements
    (CREATE TABLE, ALTER TABLE) are committed immediately and never
    swallowed by a pending ORM transaction.

    Handles:
      1. Fresh DB  → CREATE TABLE with 'permissions' column.
      2. Old DB    → table has 'perms' column → rename it.
      3. Good DB   → 'permissions' column already exists → just seed.
    """
    engine = db.ENGINE

    # ── Step 1: create table if it doesn't exist ─────────────────
    async with engine.begin() as conn:
        await conn.execute(stext("""
            CREATE TABLE IF NOT EXISTS admin_permissions (
                user_id     INTEGER PRIMARY KEY,
                permissions TEXT    NOT NULL DEFAULT '[]'
            )"""))
    # engine.begin() auto-commits on exit

    # ── Step 2: inspect current columns ──────────────────────────
    async with engine.connect() as conn:
        result   = await conn.execute(stext("PRAGMA table_info(admin_permissions)"))
        col_names = [row[1] for row in result.fetchall()]
    logger.info(f"admin_permissions columns: {col_names}")

    # ── Step 3: migrate 'perms' → 'permissions' if needed ────────
    if "perms" in col_names and "permissions" not in col_names:
        logger.info("Migrating admin_permissions: renaming perms → permissions")
        try:
            async with engine.begin() as conn:
                await conn.execute(stext(
                    "ALTER TABLE admin_permissions RENAME COLUMN perms TO permissions"))
            logger.info(f"{ui('check')} Renamed column perms → permissions")
        except Exception as e1:
            logger.warning(f"RENAME COLUMN failed ({e1}), using table-recreate fallback")
            try:
                async with engine.begin() as conn:
                    await conn.execute(stext("""
                        CREATE TABLE IF NOT EXISTS _ap_new (
                            user_id     INTEGER PRIMARY KEY,
                            permissions TEXT NOT NULL DEFAULT '[]'
                        )"""))
                    await conn.execute(stext("""
                        INSERT OR IGNORE INTO _ap_new (user_id, permissions)
                        SELECT user_id, perms FROM admin_permissions
                    """))
                    await conn.execute(stext("DROP TABLE admin_permissions"))
                    await conn.execute(stext(
                        "ALTER TABLE _ap_new RENAME TO admin_permissions"))
                logger.info(f"{ui('check')} Recreated admin_permissions table with correct column")
            except Exception as e2:
                logger.error(f"Migration fallback also failed: {e2}")
                raise

    # ── Step 4: add column if still missing (safety net) ─────────
    async with engine.connect() as conn:
        result2   = await conn.execute(stext("PRAGMA table_info(admin_permissions)"))
        col_names2 = [row[1] for row in result2.fetchall()]

    if "permissions" not in col_names2:
        logger.info("Adding missing 'permissions' column")
        async with engine.begin() as conn:
            await conn.execute(stext(
                "ALTER TABLE admin_permissions ADD COLUMN permissions TEXT NOT NULL DEFAULT '[]'"))

    # ── Step 5: seed super admins ─────────────────────────────────
    full_perms = json.dumps(list(PERMISSIONS.keys()))
    async with engine.begin() as conn:
        for uid in INITIAL_ADMIN_IDS:
            await conn.execute(
                stext("INSERT OR REPLACE INTO admin_permissions (user_id, permissions) VALUES (:u, :p)"),
                {"u": uid, "p": full_perms})
    logger.info(f"{ui('check')} admin_permissions seeded for {len(INITIAL_ADMIN_IDS)} super admins")

async def get_admin_permissions(uid: int) -> List[str]:
    async with db.AsyncSessionLocal() as s:
        row = (await s.execute(stext(
            "SELECT permissions FROM admin_permissions WHERE user_id=:u"),{"u":uid})).fetchone()
        return json.loads(row[0]) if row else []

async def set_admin_permissions(uid: int, perms: List[str]):
    async with db.AsyncSessionLocal() as s:
        await s.execute(stext(
            "INSERT OR REPLACE INTO admin_permissions (user_id,permissions) VALUES (:u,:p)"),
            {"u":uid,"p":json.dumps(perms)})
        await s.commit()

async def remove_admin_permissions(uid: int):
    async with db.AsyncSessionLocal() as s:
        await s.execute(stext("DELETE FROM admin_permissions WHERE user_id=:u"),{"u":uid})
        await s.commit()

async def list_all_admins() -> List[int]:
    async with db.AsyncSessionLocal() as s:
        return [r[0] for r in (await s.execute(stext("SELECT user_id FROM admin_permissions"))).fetchall()]

def is_super_admin(uid: int) -> bool:
    return uid in INITIAL_ADMIN_IDS

# ═══════════════════════════════════════════════════════════
#  MEMBERSHIP GATE
# ═══════════════════════════════════════════════════════════
async def check_membership(bot, uid: int) -> list:
    """
    Returns list of chats the user has NOT joined.
    Returns empty list if user is a member of all REQUIRED_CHATS.
    Admins and super-admins always pass.
    IMPROVED: More accurate checking with proper member status detection.
    """
    if is_super_admin(uid):
        return []
    perms = await get_admin_permissions(uid)
    if perms:
        return []
    missing = []
    for chat in REQUIRED_CHATS:
        try:
            member = await bot.get_chat_member(chat_id=chat["id"], user_id=uid)
            if member.status not in ("member","administrator","creator","restricted"):
                missing.append(chat)
        except TelegramForbidden:
            logger.warning(f"Bot blocked from chat {chat['id']} — treating as not joined")
            missing.append(chat)
        except TelegramBadRequest as e:
            err = str(e).lower()
            if any(k in err for k in ("chat not found","bot is not","not a member","user not found","have no access")):
                missing.append(chat)
            else:
                logger.warning(f"BadRequest checking chat {chat['id']}: {e} — skipping")
        except TelegramTimedOut:
            logger.warning(f"Timeout checking chat {chat['id']} — skipping")
        except Exception as e:
            logger.warning(f"check_membership {chat['id']}: {e}")
    return missing

# ═══════════════════════════════════════════════════════════
#  COLORED BUTTON HELPERS  (Bot API 9.4 — style field)
#  style: "primary" (blue) | "success" (green) | "danger" (red)
#  Falls back to default grey on old clients — fully backward compatible
# ═══════════════════════════════════════════════════════════

def btn(text: str, cbd: str = None, url: str = None,
        style: str = None, copy: str = None, icon: str = None):
    """
    Build an InlineKeyboardButton object.

    Returns a proper PTB InlineKeyboardButton (via our wrapper) so PTB
    can serialize it correctly — avoids document_invalid from raw dicts.

    icon  = _UI['name'][0]  → icon_custom_emoji_id (animated emoji, Bot API 9.4+)
    style = 'primary'|'success'|'danger'  → colored button (Bot API 9.4+)
    HTML/tg-emoji stripped from text automatically by the wrapper.
    """
    kw = {}
    if cbd:   kw['callback_data']      = cbd
    if url:   kw['url']                = url
    if copy:  kw['copy_text']          = CopyTextButton(copy)
    if style: kw['style']              = style
    if icon:  kw['icon_custom_emoji_id'] = str(icon)
    return InlineKeyboardButton(text, **kw)

def kb(*rows) -> "InlineKeyboardMarkup":
    """Build InlineKeyboardMarkup from rows of btn()/InlineKeyboardButton objects."""
    from telegram import InlineKeyboardMarkup as IKM
    processed = []
    for row in rows:
        if isinstance(row, (list, tuple)):
            processed.append(list(row))
        else:
            processed.append([row])
    return IKM(processed)


def join_required_kb(missing: list) -> "InlineKeyboardMarkup":
    """Keyboard: Join button per missing chat + I've Joined button."""
    rows = []
    for chat in missing:
        rows.append([btn(f"Join {html.escape(chat['title'])}",
                         url=chat["link"], icon=_UI["play"][0], style="success")])
    rows.append([btn("I've Joined — Check Again",
                     cbd="check_membership", icon=_UI["check"][0], style="success")])
    return InlineKeyboardMarkup(rows)

async def send_join_required(update_or_query, bot, uid: int, missing: list):
    """Send the 'please join first' message to a user."""
    chat_links = "\n".join(
        f"  • <a href='{c['link']}'>{c['title']}</a>" for c in missing)
    text = (
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"{ui('lock')} <b>Access Required</b>\n"
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
        "To use <b>Crack SMS</b> you must join our communities:\n\n"
        f"{chat_links}\n\n"
        f"After joining, tap {ui('check')} <b>I've Joined</b> below."
    )
    kb = join_required_kb(missing)
    async def _try_send(fn, **kw):
        try:
            await fn(text, reply_markup=kb, parse_mode="HTML",
                      disable_web_page_preview=True, **kw)
        except TelegramBadRequest as e:
            logger.error(f"send_join_required HTML error: {e}")
            try:
                await fn(re.sub(r"<[^>]+>", "", text), reply_markup=kb, **kw)
            except Exception as e2:
                logger.error(f"send_join_required plain fallback: {e2}")
        except Exception as e:
            logger.error(f"send_join_required: {e}")

    if hasattr(update_or_query, "message") and update_or_query.message:
        await _try_send(update_or_query.message.reply_text)
    elif hasattr(update_or_query, "edit_message_text"):
        await _try_send(update_or_query.edit_message_text)

# ═══════════════════════════════════════════════════════════
#  OTP KEYBOARD  — uses bot's own configured links
# ═══════════════════════════════════════════════════════════
def otp_keyboard(otp: Optional[str], full_msg: str = "",
                 for_group: bool = False) -> dict:
    """
    15 OTP GUI themes (0-14) — animated emoji icons on every button.
    icon_custom_emoji_id used throughout for animated icons (Bot API 9.4+).
    OTP shown directly in copy button — no hidden/masked text anywhere.
    """
    clean       = re.sub(r"[^0-9]", "", otp) if otp else ""
    panel_url   = NUMBER_BOT_LINK or GET_NUMBER_URL or (
        f"https://t.me/{BOT_USERNAME.lstrip('@')}" if BOT_USERNAME else None)
    info_url    = OTP_GROUP_LINK or CHANNEL_LINK
    dev_url     = f"https://t.me/{DEVELOPER.lstrip('@')}" if DEVELOPER else None
    support_url = f"https://t.me/{SUPPORT_USER.lstrip('@')}" if SUPPORT_USER else None
    t           = OTP_GUI_THEME % 15
    rows        = []

    # ─── shorthand helpers ─────────────────────────────────────────
    TAP_EMOJI_ID = "6319056439096644016"   # animated tap/finger emoji
    # Bold dots label — count matches OTP digit length (4-8)
    _dot_count   = max(4, min(8, len(clean))) if clean else 4
    _dots_label  = "●" * _dot_count          # ●●●●●● (bold filled circles)

    def _copy() -> dict:
        """Copy button with tap emoji + bold dots."""
        return btn(_dots_label, copy=clean, icon=TAP_EMOJI_ID, style="success")

    def _msg(label: str, iname: str) -> dict:
        return btn(label, copy=(full_msg or "")[:256], icon=_UI[iname][0], style="primary")

    def _link(label: str, iname: str, url: str, sty: str = "primary") -> dict:
        return btn(label, url=url, icon=_UI[iname][0], style=sty)

    # ─── GROUP mode: bold dots copy button, plus links ─────────────
    if for_group:
        if clean:
            rows.append([btn(_dots_label, copy=clean,
                             icon=TAP_EMOJI_ID, style="success")])
        lr = []
        if panel_url: lr.append(_link("Bot", "robot", panel_url, "primary"))
        if info_url:  lr.append(_link("Channel", "announce", info_url, "success"))
        if lr: rows.append(lr)
        return InlineKeyboardMarkup(rows)

    # ─── DM mode — 15 themes ───────────────────────────────────────

    # T0: CLASSIC ⭐
    if t == 0:
        if clean:    rows.append([_copy()])
        if full_msg: rows.append([_msg("Full Message", "envelope")])
        lr = []
        if panel_url: lr.append(_link("Get Numbers", "robot", panel_url))
        if info_url:  lr.append(_link("Community", "chat", info_url))
        if lr: rows.append(lr)

    # T1: MINIMAL
    elif t == 1:
        if clean: rows.append([_copy()])

    # T2: DEVELOPER 🧑‍💻
    elif t == 2:
        if clean: rows.append([_copy()])
        r1 = []
        if dev_url:     r1.append(_link("Dev", "dev", dev_url, "primary"))
        if support_url: r1.append(_link("Support", "shield", support_url, "danger"))
        if r1: rows.append(r1)

    # T3: ELECTRIC ⚡
    elif t == 3:
        if clean: rows.append([_copy()])
        lr = []
        if panel_url: lr.append(_link("Bot", "robot", panel_url))
        if lr: rows.append(lr)

    # T4: TECH 🔬
    elif t == 4:
        if clean:    rows.append([_copy()])
        if full_msg: rows.append([_msg("Full Message", "document")])
        r1 = []
        if panel_url: r1.append(_link("Bot", "robot", panel_url))
        if dev_url:   r1.append(_link("Creator", "robot", dev_url))
        if r1: rows.append(r1)

    # T5: PREMIUM 💎
    elif t == 5:
        if clean:    rows.append([_copy()])
        if full_msg: rows.append([_msg("Full Text", "notepad")])
        lr = []
        if panel_url: lr.append(_link("Numbers", "robot", panel_url))
        if info_url:  lr.append(_link("Community", "chat", info_url))
        if lr: rows.append(lr)

    # T6: ULTRA-MINIMAL
    elif t == 6:
        if clean: rows.append([_copy()])

    # T7: BUSINESS 💼
    elif t == 7:
        if clean: rows.append([_copy()])
        lr = []
        if panel_url:   lr.append(_link("Bot", "robot", panel_url, "primary"))
        if support_url: lr.append(_link("Support", "shield", support_url, "danger"))
        if lr: rows.append(lr)

    # T8: SOCIAL 🌐
    elif t == 8:
        if clean: rows.append([_copy()])
        r1 = []
        if info_url: r1.append(_link("Community", "globe", info_url))
        if dev_url:  r1.append(_link("Creator", "robot", dev_url))
        if r1: rows.append(r1)

    # T9: DELUXE 🌟
    elif t == 9:
        if clean:    rows.append([_copy()])
        if full_msg: rows.append([_msg("Full Text", "notepad")])
        r1 = []
        if panel_url: r1.append(_link("Numbers", "robot", panel_url))
        if info_url:  r1.append(_link("Community", "chat", info_url))
        if r1: rows.append(r1)
        r2 = []
        if dev_url:     r2.append(_link("Dev", "robot", dev_url, "primary"))
        if support_url: r2.append(_link("Support", "shield", support_url, "danger"))
        if r2: rows.append(r2)

    # T10: CLASSIC ELEGANCE 📝
    elif t == 10:
        if clean:    rows.append([_copy()])
        if full_msg: rows.append([_msg("Full Message", "document")])
        lr = []
        if panel_url: lr.append(_link("Get Numbers", "robot", panel_url))
        if info_url:  lr.append(_link("Community", "chat", info_url, "success"))
        if lr: rows.append(lr)

    # T11: RAINBOW 🌈
    elif t == 11:
        if clean:    rows.append([_copy()])
        if full_msg: rows.append([_msg("Full Text", "chat")])
        r1 = []
        if panel_url:   r1.append(_link("Get Numbers", "rocket", panel_url))
        if support_url: r1.append(_link("Support", "bell", support_url))
        if r1: rows.append(r1)

    # T12: FOCUS 🎯
    elif t == 12:
        if clean: rows.append([_copy()])
        lr = []
        if panel_url: lr.append(_link("Bot", "robot", panel_url))
        if info_url:  lr.append(_link("Channel", "announce", info_url, "success"))
        if lr: rows.append(lr)

    # T13: ROYAL PREMIUM 👑
    elif t == 13:
        if clean:    rows.append([_copy()])
        if full_msg: rows.append([_msg("Full Text", "notepad")])
        r1 = []
        if panel_url: r1.append(_link("Numbers", "rocket", panel_url))
        if dev_url:   r1.append(_link("Dev", "gear", dev_url))
        if r1: rows.append(r1)
        r2 = []
        if info_url:    r2.append(_link("Community", "chat", info_url, "success"))
        if support_url: r2.append(_link("Support", "shield", support_url, "danger"))
        if r2: rows.append(r2)

    # T14: HOVER LUXURY 💎
    elif t == 14:
        if clean:    rows.append([_copy()])
        if full_msg: rows.append([_msg("Full Text", "document")])
        r1 = []
        if panel_url: r1.append(_link("Get Numbers", "rocket", panel_url))
        if info_url:  r1.append(_link("Community", "bell", info_url, "success"))
        if r1: rows.append(r1)
        r2 = []
        if dev_url:     r2.append(_link("Developer", "gear", dev_url, "primary"))
        if support_url: r2.append(_link("Support", "shield", support_url, "danger"))
        if r2: rows.append(r2)

    return InlineKeyboardMarkup(rows)



# ═══════════════════════════════════════════════════════════
#  USER KEYBOARDS
# ═══════════════════════════════════════════════════════════
def main_menu_compact_kb() -> "InlineKeyboardMarkup":
    """Compact main menu with core buttons only - shows 'More' button. Child bots fully isolated."""
    logger.debug(f"🔧 Building compact menu | Bot Type: {'CHILD' if IS_CHILD_BOT else 'MAIN'} | IS_CHILD_BOT={IS_CHILD_BOT}")
    rows = []
    
    rows.append([btn("Get Number", cbd="buy_menu", style="success", icon=_UI["fire"][0])])
    
    # ✅ Row 2: Create Bot (only in main bot) / My Profile (child bot)
    if not IS_CHILD_BOT:
        rows.append([btn("Create My Bot", cbd="create_bot_menu", style="success", icon=_UI["robot"][0])])
    else:
        rows.append([btn("My Profile", cbd="profile", style="primary", icon=_UI["user"][0])])
    
    # Row 2: My Stats | My History
    rows.append([btn("My Stats", cbd="mystats", style="primary", icon=_UI["chart"][0]),
                 btn("My History", cbd="myhistory", style="success", icon=_UI["notepad"][0])])
    
    # Row 3: My OTPs | Premium
    rows.append([btn("My OTPs", cbd="my_otps", style="danger", icon=_UI["diamond"][0]),
                 btn("Premium", cbd="premium_menu", style="success", icon=_UI["crown"][0])])
    
    # Row 4: Settings (+ Analytics if main bot only)
    row4 = []
    if not IS_CHILD_BOT:
        row4.append(btn("Analytics", cbd="analytics", style="primary", icon=_UI["chart"][0]))
    row4.append(btn("Settings", cbd="user_settings", style="primary", icon=_UI["settings"][0]))
    rows.append(row4)
    
    # Row 5: More button → Shows full menu with additional options
    rows.append([btn("More Options", cbd="main_menu_full", style="primary", icon=_UI["book"][0])])
    
    logger.debug(f"{ui('check')} Compact menu built | Child Bot Isolated: {IS_CHILD_BOT}")
    return InlineKeyboardMarkup(rows)

def main_menu_full_kb() -> "InlineKeyboardMarkup":
    """Full main menu with all options - includes tutorials, links, help. Child bots fully isolated."""
    logger.debug(f"🔧 Building full menu | Bot Type: {'CHILD' if IS_CHILD_BOT else 'MAIN'} | IS_CHILD_BOT={IS_CHILD_BOT}")
    rows = []
    
    rows.append([btn("Get Number", cbd="buy_menu", style="success", icon=_UI["fire"][0])])
    
    # ✅ Row 2: Create Bot (only in main bot) / My Profile (child bot)
    if not IS_CHILD_BOT:
        rows.append([btn("Create My Bot", cbd="create_bot_menu", style="success", icon=_UI["robot"][0])])
    else:
        rows.append([btn("My Profile", cbd="profile", style="primary", icon=_UI["user"][0])])
    
    # Row 2: My Stats | My History
    rows.append([btn("My Stats", cbd="mystats", style="primary", icon=_UI["chart"][0]),
                 btn("My History", cbd="myhistory", style="success", icon=_UI["notepad"][0])])
    
    # Row 3: My OTPs | Premium
    rows.append([btn("My OTPs", cbd="my_otps", style="danger", icon=_UI["diamond"][0]),
                 btn("Premium", cbd="premium_menu", style="success", icon=_UI["crown"][0])])
    
    # Row 4: Analytics (only main bot) | Settings
    row4 = []
    if not IS_CHILD_BOT:
        row4.append(btn("Analytics", cbd="analytics", style="primary", icon=_UI["chart"][0]))
    row4.append(btn("Settings", cbd="user_settings", style="primary", icon=_UI["settings"][0]))
    if row4:
        rows.append(row4)
    
    # Row 5: Tutorials
    rows.append([btn("Tutorials", cbd="tutorials", style="primary", icon=_UI["book"][0])])
    
    # Links row (Channel & Community) - only in main bot
    link_row = []
    if not IS_CHILD_BOT:
        if CHANNEL_LINK:
            link_row.append(btn("Channel", url=CHANNEL_LINK, style="success", icon=_UI["megaphone"][0]))
        if OTP_GROUP_LINK:
            link_row.append(btn("Community", url=OTP_GROUP_LINK, style="primary", icon=_UI["chat"][0]))
        if link_row:
            rows.append(link_row)
    
    # Bot links (Get Numbers, Support)
    nb = NUMBER_BOT_LINK or GET_NUMBER_URL
    bot_row = []
    if nb:
        bot_row.append(btn("Get Numbers", url=nb, style="primary", icon=_UI["link"][0]))
    sup = SUPPORT_USER.lstrip("@")
    if sup:
        bot_row.append(btn("Support", url=f"https://t.me/{sup}", style="danger", icon=_UI["help"][0]))
    if bot_row:
        rows.append(bot_row)
    
    # Developer link - only main bot
    if not IS_CHILD_BOT:
        dev = DEVELOPER.lstrip("@")
        if dev:
            rows.append([btn("Developer", url=f"https://t.me/{dev}", style="primary", icon=_UI["rocket"][0])])
    
    # Help & Back to Compact Menu
    rows.append([btn("Help", cbd="cmd_help", style="primary", icon=_UI["question"][0]),
                 btn("Back", cbd="main_menu_compact", style="primary", icon=_UI["back"][0])])
    
    logger.debug(f"{ui('check')} Full menu built | Child Bot Isolated: {IS_CHILD_BOT}")
    return InlineKeyboardMarkup(rows)

def main_menu_kb() -> "InlineKeyboardMarkup":
    """Default menu - returns compact version (backward compatible)."""
    return main_menu_compact_kb()

def services_kb(svcs: list) -> "InlineKeyboardMarkup":
    rows = []
    row = []
    for s in svcs:
        row.append(btn(s, cbd=f"svc_{s}", style="primary", icon=_UI["phone"][0]))
        if len(row) == 2: rows.append(row); row = []
    if row: rows.append(row)
    rows.append([btn("Back", cbd="main_menu", style="primary", icon=_UI["back"][0])])
    return InlineKeyboardMarkup(rows)

def countries_kb(svc: str, countries: list) -> "InlineKeyboardMarkup":
    rows = []; row = []
    for flag, name in countries:
        row.append(btn(name, cbd=f"cntry|{svc}|{name}", style="primary", icon=_UI["earth"][0]))
        if len(row) == 2: rows.append(row); row = []
    if row: rows.append(row)
    rows.append([btn("Back", cbd="buy_menu", style="primary", icon=_UI["back"][0])])
    return InlineKeyboardMarkup(rows)


def vweb_countries_kb(countries: list, page: int = 1) -> "InlineKeyboardMarkup":
    page_items, total_pages, _, _ = paginate_list(countries, page, per_page=VWEB_COUNTRIES_PER_PAGE)
    rows = [[btn("Live Country Routes", cbd="noop", style="success", icon=_UI["diamond"][0])]]
    row = []
    start = (page - 1) * VWEB_COUNTRIES_PER_PAGE
    for offset, (_, country) in enumerate(page_items):
        idx = start + offset
        row.append(btn(
            country,
            cbd=f"vweb_country_pick_{idx}",
            style="primary",
            icon=get_country_icon_id(country),
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(btn("Prev", cbd=f"vweb_country_page_{page-1}", style="primary", icon=_UI["back"][0]))
        nav.append(btn(f"{page}/{total_pages}", cbd="noop", style="primary", icon=_UI["earth"][0]))
        if page < total_pages:
            nav.append(btn("Next", cbd=f"vweb_country_page_{page+1}", style="primary", icon=_UI["refresh"][0]))
        rows.append(nav)
    rows.append([btn("Main Menu", cbd="main_menu", style="primary", icon=_UI["rocket"][0])])
    return InlineKeyboardMarkup(rows)


def vweb_services_kb(country: str, services: list, page: int = 1) -> "InlineKeyboardMarkup":
    page_items, total_pages, _, _ = paginate_list(services, page, per_page=VWEB_SERVICES_PER_PAGE)
    rows = [[btn(country, cbd="noop", style="success", icon=get_country_icon_id(country))]]
    row = []
    start = (page - 1) * VWEB_SERVICES_PER_PAGE
    for offset, service in enumerate(page_items):
        idx = start + offset
        row.append(btn(
            service,
            cbd=f"vweb_service_pick_{idx}",
            style="primary",
            icon=app_icon_id(service),
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(btn("Prev", cbd=f"vweb_service_page_{page-1}", style="primary", icon=_UI["back"][0]))
        nav.append(btn(f"{page}/{total_pages}", cbd="noop", style="primary", icon=_UI["chart"][0]))
        if page < total_pages:
            nav.append(btn("Next", cbd=f"vweb_service_page_{page+1}", style="primary", icon=_UI["refresh"][0]))
        rows.append(nav)
    rows.append([
        btn("Change Country", cbd="virtual_web", style="success", icon=_UI["earth"][0]),
        btn("Main Menu", cbd="main_menu", style="primary", icon=_UI["rocket"][0]),
    ])
    return InlineKeyboardMarkup(rows)

def waiting_kb(prefix=None, service=None) -> "InlineKeyboardMarkup":
    pfx = f"ON ({prefix})" if prefix else "OFF"
    rows = [
        [btn(f"Assign Limit: {get_assign_limit_label()}",
             cbd="noop", icon=_UI["lock"][0], style="success")],
        [btn("Change Number", cbd="skip_next", icon=_UI["refresh"][0], style="primary"),
         btn("Change Service", cbd="change_service_menu", icon=_UI["notepad"][0], style="success")],
        [btn("Change Country", cbd="change_country", icon=_UI["earth"][0], style="success"),
         btn(f"Prefix: {pfx}", cbd="set_prefix", icon=_UI["phone"][0], style="primary")],
        [btn("Block Number", cbd="ask_block", icon=_UI["trash"][0], style="danger")],
    ]
    if OTP_GROUP_LINK:
        rows.append([btn("OTP Group", url=OTP_GROUP_LINK, icon=_UI["chat"][0], style="primary")])
    return InlineKeyboardMarkup(rows)


async def show_buy_menu(query, uid: int):
    svcs = await db.get_distinct_services()
    set_number_flow_state(uid, "classic")
    if not svcs:
        await query.edit_message_text(
            styled_error(
                "No Services Available",
                "The admin needs to upload phone numbers before users can request routes.",
                "Try again later or contact support.",
            ),
            reply_markup=InlineKeyboardMarkup([[btn("Back", cbd="main_menu", style="primary", icon=_UI["back"][0])]]),
            parse_mode="HTML",
        )
        return
    await query.edit_message_text(
        f"{ui('phone')} <b>Select Service</b>\n{D}\n{ui('copy')} Bot assign limit: <b>{get_assign_limit_label()}</b>",
        reply_markup=services_kb(svcs),
        parse_mode="HTML",
    )


async def show_virtual_country_menu(query, context: ContextTypes.DEFAULT_TYPE,
                                    uid: int, page: int = 1, notice: str = ""):
    countries = await db.get_distinct_countries()
    context.user_data["vweb_countries"] = countries
    set_number_flow_state(uid, "virtual", service=None, country=None, flag=None)
    if not countries:
        await query.edit_message_text(
            styled_error(
                "Virtual Routes Offline",
                "No live countries are currently available for the Virtual Number Web experience.",
                "Upload stock first, then reopen this screen.",
            ),
            reply_markup=InlineKeyboardMarkup([[btn("Main Menu", cbd="main_menu", style="primary", icon=_UI["rocket"][0])]]),
            parse_mode="HTML",
        )
        return
    parts = [
        f"{ui('desktop')} <b>Virtual Number Web</b>",
        D,
        f"{ui('diamond')} Premium single-number mode with a clean live inbox.",
        f"{ui('earth')} Pick a country first, then select the service you want.",
        f"{ui('satellite')} New OTPs and previous OTPs on the same number stay in one thread.",
    ]
    if notice:
        parts.extend(["", notice])
    await query.edit_message_text(
        "\n".join(parts),
        reply_markup=vweb_countries_kb(countries, page=page),
        parse_mode="HTML",
    )


async def show_virtual_service_menu(query, context: ContextTypes.DEFAULT_TYPE,
                                    uid: int, country: str = None, page: int = 1,
                                    notice: str = ""):
    country = country or context.user_data.get("vweb_country") or get_number_flow_state(uid).get("country")
    if not country:
        await show_virtual_country_menu(query, context, uid, page=1)
        return
    countries = context.user_data.get("vweb_countries") or await db.get_distinct_countries()
    context.user_data["vweb_countries"] = countries
    flag = next((f for f, c in countries if c == country), "🌍")
    services = await db.get_services_for_country(country)
    context.user_data["vweb_country"] = country
    context.user_data["vweb_flag"] = flag
    context.user_data["vweb_services"] = services
    set_number_flow_state(uid, "virtual", service=None, country=country, flag=flag)
    if not services:
        await query.edit_message_text(
            styled_warning(
                "No Services In This Country",
                f"Live inventory is empty for <b>{html.escape(country)}</b> right now.",
            ),
            reply_markup=vweb_countries_kb(countries, page=1),
            parse_mode="HTML",
        )
        return
    parts = [
        f"{ui('desktop')} <b>Virtual Number Web</b>",
        D,
        f"{ui('earth')} <b>Country:</b> {html.escape(country)} {flag}",
        f"{ui('notepad')} Choose a service to get one random live number instantly.",
        f"{ui('clock')} If that number has older OTPs in history, they will appear below the new inbox.",
    ]
    if notice:
        parts.extend(["", notice])
    await query.edit_message_text(
        "\n".join(parts),
        reply_markup=vweb_services_kb(country, services, page=page),
        parse_mode="HTML",
    )


async def assign_virtual_number(query, context: ContextTypes.DEFAULT_TYPE, uid: int,
                                country: str, service: str, flag: str = "🌍"):
    category = f"{flag} {country} - {service}"
    previous_flow = get_number_flow_state(uid)
    await db.set_user_prefix(uid, None)
    active = await db.get_active_numbers(uid)
    if active and (
        len(active) != 1
        or active[0].category != category
        or previous_flow.get("mode") != "virtual"
    ):
        await db.release_number(uid)
        active = []
    if not active:
        await db.request_numbers(uid, category, count=1)
        active = await db.get_active_numbers(uid)
    set_number_flow_state(uid, "virtual", service=service, country=country, flag=flag)
    context.user_data["vweb_country"] = country
    context.user_data["vweb_flag"] = flag
    context.user_data["vweb_service"] = service
    if not active:
        await query.edit_message_text(
            styled_warning(
                "Out Of Stock",
                f"No live numbers are available right now for <b>{html.escape(country)}</b> • <b>{html.escape(service)}</b>.",
            ),
            reply_markup=vweb_services_kb(country, context.user_data.get("vweb_services", [service]), page=1),
            parse_mode="HTML",
        )
        return
    pfx = await db.get_user_prefix(uid)
    text = await build_virtual_number_text_from_active(uid, active, prefix=pfx)
    await query.edit_message_text(
        text,
        reply_markup=waiting_kb(pfx, service=service),
        parse_mode="HTML",
    )
    for number in active:
        await db.update_message_id(number.phone_number, query.message.message_id)


# ═══════════════════════════════════════════════════════════
#  ENHANCED UI HELPERS — Styled error/success messages
# ═══════════════════════════════════════════════════════════
def styled_success(title: str, msg: str = "") -> str:
    """Format a success message with styled border and emojis."""
    return (
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"{ui('check')} <b>{title}</b>\n"
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"{msg}" if msg else ""
    )

def styled_error(title: str, msg: str = "", hint: str = "") -> str:
    """Format an error message with styled border and emojis."""
    text = (
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"{ui('cancel')} <b>{title}</b>\n"
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n"
    )
    if msg:
        text += f"{msg}\n"
    if hint:
        text += f"\n<i>💡 {hint}</i>"
    return text

def styled_warning(title: str, msg: str = "") -> str:
    """Format a warning message with styled border."""
    return (
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"{ui('warn')} <b>{title}</b>\n"
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"{msg}" if msg else ""
    )

def styled_info(title: str, info: dict = None) -> str:
    """Format an info message with styled header and body."""
    text = (
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"{ui('info')} <b>{title}</b>\n"
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n"
    )
    if info:
        for k, v in info.items():
            text += f"<b>{k}:</b> {v}\n"
    return text

# ═══════════════════════════════════════════════════════════
#  PAGINATION HELPERS — For large lists/results
# ═══════════════════════════════════════════════════════════
def paginate_list(items: list, page: int = 1, per_page: int = 5) -> tuple:
    """Paginate a list. Returns (page_items, total_pages, has_prev, has_next)."""
    total = len(items)
    total_pages = (total + per_page - 1) // per_page
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    end = start + per_page
    return items[start:end], total_pages, page > 1, page < total_pages

def pagination_kb(page: int, total_pages: int, action: str) -> "InlineKeyboardMarkup":
    """Build pagination keyboard for navigating large lists."""
    rows = []
    if total_pages > 1:
        nav_row = []
        if page > 1:
            nav_row.append(btn("Prev", cbd=f"{action}_page_{page-1}", style="primary", icon=_UI["back"][0]))
        nav_row.append(btn(f"{page}/{total_pages}", cbd="noop", style="primary"))
        if page < total_pages:
            nav_row.append(btn("Next", cbd=f"{action}_page_{page+1}", style="primary", icon=_UI["refresh"][0]))
        if nav_row:
            rows.append(nav_row)
    rows.append([btn("Back", cbd="main_menu", style="primary", icon=_UI["back"][0])])
    return InlineKeyboardMarkup(rows)

# ═══════════════════════════════════════════════════════════
#  SEARCH & FILTER HELPERS
def search_results_kb(results: list, search_type: str = "users") -> "InlineKeyboardMarkup":
    """Build keyboard for search results with limited display."""
    rows = []
    for idx, item in enumerate(results[:10]):
        label = item.get("name") or item.get("phone") or str(item)
        rows.append([btn(label, cbd=f"view_{search_type}_{idx}", icon=_UI["user"][0])])
    if len(results) > 10:
        rows.append([btn(f"+{len(results)-10} more...", cbd="noop", icon=_UI["chart"][0])])
    rows.append([btn("Back", cbd="admin_home", icon=_UI["back"][0])])
    return InlineKeyboardMarkup(rows)

# ═══════════════════════════════════════════════════════════
#  ADMIN KEYBOARDS  — Pro-level submenus
# ═══════════════════════════════════════════════════════════
def admin_main_kb(perms: list, is_sup: bool) -> "InlineKeyboardMarkup":
    """Professional admin menu with color-coded buttons - optimized 2-per-row layout.
    FIXED: All buttons now have proper style colors applied throughout."""
    rows = []
    
    # ✅ Child bots cannot manage other bots
    disable_bot_mgmt = IS_CHILD_BOT
    
    # NUMBERS & BROADCAST
    r = []
    if "manage_files" in perms: r.append(btn("Numbers", cbd="admin_numbers", style="primary", icon=_UI["document"][0]))
    if "broadcast"    in perms: r.append(btn("Broadcast", cbd="admin_broadcast", style="danger", icon=_UI["megaphone"][0]))
    if r: rows.append(r)
    
    # STATISTICS & ADVANCED ANALYTICS
    r = []
    if "view_stats" in perms: r.append(btn("Statistics", cbd="admin_stats_menu", style="success", icon=_UI["chart"][0]))
    if "view_stats" in perms: r.append(btn("Advanced", cbd="advanced_analytics", style="primary", icon=_UI["chart"][0]))
    if r: rows.append(r)
    
    # USERS (full-width button - important)
    r = []
    if is_sup and not disable_bot_mgmt: r.append(btn("Users", cbd="admin_users", style="success", icon=_UI["people"][0]))
    if r: rows.append(r)
    
    # PANELS & LOG GROUPS
    r = []
    if "manage_panels" in perms: r.append(btn("Panels", cbd="admin_panel_manager", style="primary", icon=_UI["satellite"][0]))
    if "manage_logs"   in perms: r.append(btn("Log Groups", cbd="admin_manage_logs", style="success", icon=_UI["notepad"][0]))
    if r: rows.append(r)
    
    # CLEAR LOGS (full-width button - important action)
    r = []
    if "manage_logs" in perms: r.append(btn("Clear Logs", cbd="clear_all_logs", style="danger", icon=_UI["trash"][0]))
    if r: rows.append(r)
    
    # ADMINS & PERMISSIONS
    r = []
    if is_sup and not disable_bot_mgmt: r.append(btn("Admins", cbd="admin_manage_admins", style="primary", icon=_UI["people"][0]))
    if is_sup and not disable_bot_mgmt: r.append(btn("Permissions", cbd="admin_perms", style="success", icon=_UI["shield"][0]))
    if r: rows.append(r)
    
    # TUTORIALS & SETTINGS
    r = []
    if is_sup and not disable_bot_mgmt: r.append(btn("Tutorials", cbd="admin_tutorials", style="primary", icon=_UI["book"][0]))
    r.append(btn("Settings", cbd="admin_settings", style="primary", icon=_UI["settings"][0]))
    if r: rows.append(r)
    
    # WEBSITE MANAGEMENT (visible to super admins)
    r = []
    if is_sup and not disable_bot_mgmt:
        r.append(btn("Website Management", cbd="website_management", style="success", icon=_UI["desktop"][0]))
    if r: rows.append(r)

    # FETCH SMS & CHILD BOTS (hidden in child bots)
    r = []
    if "manage_panels" in perms: r.append(btn("Fetch SMS", cbd="admin_fetch_sms", style="success", icon=_UI["satellite"][0]))
    if is_sup and not disable_bot_mgmt: r.append(btn("Child Bots", cbd="admin_bots", style="danger", icon=_UI["robot"][0]))
    if r: rows.append(r)
    
    # SYSTEM (full-width button - important, hidden in child bots)
    r = []
    if is_sup and not disable_bot_mgmt: r.append(btn("System", cbd="admin_system", style="primary", icon=_UI["gear"][0]))
    if r: rows.append(r)
    
    # HOME & EXIT
    rows.append([btn("Home", cbd="main_menu", style="primary", icon=_UI["rocket"][0]),
                 btn("Exit", cbd="cancel", style="danger", icon=_UI["cancel"][0])])
    
    return InlineKeyboardMarkup(rows)

def admin_numbers_kb(cats: list) -> InlineKeyboardMarkup:
    kb = []
    for cat, cnt in cats:
        sid = hashlib.md5(cat.encode()).hexdigest()[:10]
        CATEGORY_MAP[sid] = cat
        kb.append([
            InlineKeyboardButton(cat + f" ({cnt})", callback_data="ignore", style="primary", icon_custom_emoji_id=_UI["document"][0]),
            InlineKeyboardButton("Stats", callback_data=f"cat_stats_{sid}", style="primary", icon_custom_emoji_id=_UI["chart"][0]),
            InlineKeyboardButton("Delete", callback_data=f"del_{sid}", style="danger", icon_custom_emoji_id=_UI["skull"][0]),
        ])
    kb.append([
        InlineKeyboardButton("Upload Numbers", callback_data="admin_upload_info", style="success", icon_custom_emoji_id=_UI["rocket"][0]),
        InlineKeyboardButton("All Categories", callback_data="admin_files", style="primary", icon_custom_emoji_id=_UI["notepad"][0])
    ])
    kb.append([
        InlineKeyboardButton("Free Cooldowns", callback_data="admin_reset", style="primary", icon_custom_emoji_id=_UI["zap"][0]),
        InlineKeyboardButton("Purge Used", callback_data="purge_used", style="danger", icon_custom_emoji_id=_UI["skull"][0])
    ])
    kb.append([
        InlineKeyboardButton("Purge Blocked", callback_data="purge_blocked", style="danger", icon_custom_emoji_id=_UI["check"][0]),
        InlineKeyboardButton("Full Stats", callback_data="admin_stats", style="primary", icon_custom_emoji_id=_UI["chart"][0])
    ])
    kb.append([InlineKeyboardButton("Back", callback_data="admin_home", style="primary", icon_custom_emoji_id=_UI["back"][0])])
    return InlineKeyboardMarkup(kb)

# ── Stats submenu ─────────────────────────────────────────────
def admin_stats_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Live Stats", callback_data="admin_stats", style="primary", icon_custom_emoji_id=_UI["chart"][0]),
         InlineKeyboardButton("OTP History", callback_data="admin_otp_history", style="success", icon_custom_emoji_id=_UI["chart"][0])],
        [InlineKeyboardButton("Panel Status", callback_data="test_panels", style="primary", icon_custom_emoji_id=_UI["satellite"][0]),
         InlineKeyboardButton("DB Summary", callback_data="admin_db_summary", style="success", icon_custom_emoji_id=_UI["database"][0])],
        [InlineKeyboardButton("User Count", callback_data="admin_list_users", style="primary", icon_custom_emoji_id=_UI["people"][0]),
         InlineKeyboardButton("OTP Store", callback_data="admin_otp_store", style="primary", icon_custom_emoji_id=_UI["key"][0])],
        [InlineKeyboardButton("Back", callback_data="admin_home", style="primary", icon_custom_emoji_id=_UI["back"][0])],
    ])

# ── OTP Tools submenu (super only) ────────────────────────────
def admin_otp_tools_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{ui('key')}  View OTP Store", callback_data="admin_otp_store", style="primary"),
         InlineKeyboardButton(f"{ui('envelope')}  Export OTPs", callback_data="export_otps", style="success")],
        [InlineKeyboardButton(f"{ui('trash')}  Clear OTP Store", callback_data="clear_otps", style="danger"),
         InlineKeyboardButton(f"{ui('chart')}  OTP History", callback_data="admin_otp_history", style="success")],
        [InlineKeyboardButton(f"{ui('focus')}  Find OTP by Number", callback_data="find_otp_prompt", style="primary")],
        [InlineKeyboardButton(f"{ui('back')}  Back", callback_data="admin_home", style="primary")],
    ])

# ── Notify/Broadcast submenu ──────────────────────────────────
def admin_notify_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{ui('megaphone')}  Broadcast Users", callback_data="admin_broadcast", style="success"),
         InlineKeyboardButton(f"{ui('megaphone')}  Broadcast All Bots", callback_data="broadcast_all_bots", style="success")],
        [InlineKeyboardButton(f"{ui('notepad')}  Log Groups", callback_data="admin_manage_logs", style="primary"),
         InlineKeyboardButton(f"{ui('bell')}  Add Log Group", callback_data="add_log_prompt", style="success")],
        [InlineKeyboardButton(f"{ui('zap')}  Send Test OTP", callback_data="send_test_otp", style="success"),
         InlineKeyboardButton(f"{ui('satellite')}  Ping Log Groups", callback_data="ping_log_groups", style="primary")],
        [InlineKeyboardButton(f"{ui('back')}  Back", callback_data="admin_home", style="primary")],
    ])

# ── Users submenu (super admin) ───────────────────────────────
def admin_users_kb() -> InlineKeyboardMarkup:
    return kb(
        [btn("All Users", cbd="admin_list_users", style="primary", icon=_UI["people"][0]),
         btn("User Stats", cbd="admin_db_summary", style="primary", icon=_UI["chart"][0])],
        [btn("Assign Limit", cbd="set_limit", style="primary", icon=_UI["phone"][0]),
         btn("Set User Limit", cbd="set_user_limit_help", style="success", icon=_UI["focus"][0])],
        [btn("Broadcast Users", cbd="admin_broadcast", style="success", icon=_UI["megaphone"][0]),
         btn("Free All Numbers", cbd="admin_reset", style="danger", icon=_UI["zap"][0])],
        [btn("Back", cbd="admin_home", style="primary", icon=_UI["back"][0])],
    )

# ── Panel Manager ─────────────────────────────────────────────
def panel_mgr_kb() -> "InlineKeyboardMarkup":
    return kb(
        [btn("Add Panel", cbd="panel_add", style="success", icon=_UI["bell"][0]),
         btn("List All", cbd="panel_list_all", style="primary", icon=_UI["notepad"][0])],
        [btn("Login Panels", cbd="panel_list_login", style="primary", icon=_UI["key"][0]),
         btn("API Panels", cbd="panel_list_api", style="primary", icon=_UI["key"][0])],
        [btn("IVAS Panels", cbd="panel_list_ivas", style="primary", icon=_UI["phone"][0]),
         btn("Re-login All", cbd="panel_reloginall", style="primary", icon=_UI["refresh"][0])],
        [btn("Load .dex", cbd="panel_loaddex", style="primary", icon=_UI["envelope"][0]),
         btn("Back", cbd="admin_home", style="primary", icon=_UI["back"][0])],
    )
def panel_list_kb(panels: list, ptype: str) -> InlineKeyboardMarkup:
    kb = []
    for p in panels:
        if ptype == "ivas":
            st = "🟢" if (p.name in IVAS_TASKS and not IVAS_TASKS[p.name].done()) else "🔴"
        else:
            st = "🟢" if p.is_logged_in else "🔴"
        kb.append([
            InlineKeyboardButton(f"{st} {p.name}", callback_data="ignore", style="primary"),
            InlineKeyboardButton(f"{ui('earth')}", callback_data=f"p_info_{p.id}", style="primary"),
            InlineKeyboardButton(f"{ui('zap')}", callback_data=f"p_test_{p.id}", style="primary"),
            InlineKeyboardButton(f"{ui('gear')}", callback_data=f"p_edit_{p.id}", style="primary"),
            InlineKeyboardButton(f"{ui('skull')}", callback_data=f"p_del_{p.id}", style="danger"),
        ])
    kb.append([InlineKeyboardButton(f"{ui('star')}  Add Panel", callback_data="p_add", style="success")])
    kb.append([InlineKeyboardButton(f"{ui('lock')}  Back", callback_data="admin_panel_manager", style="primary")])
    return InlineKeyboardMarkup(kb)

def ptype_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{ui('key')}  Login Panel", callback_data="pt_login", style="primary")],
        [InlineKeyboardButton(f"{ui('satellite')}  API Type 1 (CR-API)", callback_data="pt_api", style="primary"),
         InlineKeyboardButton(f"{ui('star')}  API Type 2 (Reseller)", callback_data="pt_api_v2", style="primary")],
        [InlineKeyboardButton(f"{ui('antenna')}  IVAS Panel", callback_data="pt_ivas", style="primary")],
        [InlineKeyboardButton(f"{ui('skull')}  Cancel", callback_data="cancel_action", style="danger")],
    ])

def confirm_del_panel_kb() -> "InlineKeyboardMarkup":
    return kb([btn("Yes, Delete", cbd="p_del_confirm", style="danger", icon=_UI["check"][0]),
               btn("Cancel", cbd="admin_panel_manager", style="primary", icon=_UI["cancel"][0])])

def confirm_block_kb() -> "InlineKeyboardMarkup":
    return kb([btn("Yes, Block", cbd="block_yes", style="success", icon=_UI["check"][0]),
               btn("Cancel", cbd="block_no", style="danger", icon=_UI["cancel"][0])])

def admin_links_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Channel Link", callback_data="set_channel_prompt", style="success", icon_custom_emoji_id=_UI["megaphone"][0])],
        [InlineKeyboardButton("OTP Group Link", callback_data="set_otpgroup_prompt", style="primary", icon_custom_emoji_id=_UI["chat"][0])],
        [InlineKeyboardButton("Number Bot Link", callback_data="set_numbot_prompt", style="primary", icon_custom_emoji_id=_UI["receiver"][0])],
        [InlineKeyboardButton("Support User", callback_data="set_support_prompt", style="primary", icon_custom_emoji_id=_UI["support"][0])],
        [InlineKeyboardButton("Developer", callback_data="set_developer_prompt", style="primary", icon_custom_emoji_id=_UI["rocket"][0])],
        [InlineKeyboardButton("Back", callback_data="admin_settings", style="primary", icon_custom_emoji_id=_UI["back"][0])],
    ])

def admin_settings_kb() -> "InlineKeyboardMarkup":
    theme_name = _THEME_NAMES.get(OTP_GUI_THEME % 15, "Unknown")
    limit_label = get_assign_limit_label()
    return kb(
        [btn(f"Assign Limit: {limit_label}", cbd="set_limit", style="success", icon=_UI["lock"][0])],
        [btn("Bot Links", cbd="admin_links", style="primary", icon=_UI["link"][0]),
         btn("Bot Info", cbd="admin_botinfo", style="danger", icon=_UI["robot"][0])],
        [btn(f"OTP GUI: {theme_name}", cbd="admin_gui_theme", style="primary", icon=_UI["focus"][0])],
        [btn("Required Chats", cbd="admin_req_chats", style="primary", icon=_UI["bell"][0]),
         btn("Broadcast", cbd="admin_broadcast_menu", style="success", icon=_UI["megaphone"][0])],
        [btn("Maintenance", cbd="admin_maintenance", style="danger", icon=_UI["trash"][0]),
         btn("Change Token", cbd="change_token_prompt", style="primary", icon=_UI["key"][0])],
        [btn("Reload Countries", cbd="reload_countries", style="primary", icon=_UI["earth"][0]),
         btn("View Logs", cbd="view_logs", style="primary", icon=_UI["notepad"][0])],
        [btn("Back", cbd="admin_home", style="danger", icon=_UI["back"][0])],
    )


def website_management_kb() -> "InlineKeyboardMarkup":
    return kb(
        [btn("Overview", cbd="website_management", style="primary", icon=_UI["desktop"][0]),
         btn("Announcement", cbd="website_set_announcement", style="success", icon=_UI["announce"][0])],
        [btn("Approval Note", cbd="website_set_status_note", style="primary", icon=_UI["notepad"][0]),
         btn("Pending Requests", cbd="website_pending_requests", style="danger", icon=_UI["robot"][0])],
        [btn("WhatsApp Contact", cbd="website_set_whatsapp", style="success", icon=_UI["phone"][0]),
         btn("Telegram Contact", cbd="website_set_telegram", style="primary", icon=_UI["chat"][0])],
        [btn("Token Management", cbd="admin_api_tokens", style="primary", icon=_UI["key"][0])],
        [btn("Back", cbd="admin_home", style="danger", icon=_UI["back"][0])],
    )


def build_website_management_text() -> str:
    settings = get_website_settings()
    requests = load_website_requests()
    pending = [row for row in requests if row.get("status", "pending") == "pending"]
    notified = [row for row in pending if row.get("admin_notified_at")]
    return (
        f"{ui('desktop')} <b>Website Management</b>\n{D}\n"
        f"{ui('announce')} Announcement:\n<blockquote>{html.escape(_safe_short(settings['announcement'], 220))}</blockquote>\n"
        f"{ui('notepad')} Approval note:\n<blockquote>{html.escape(_safe_short(settings['status_note'], 220))}</blockquote>\n"
        f"{ui('phone')} WhatsApp: <code>{html.escape(settings['whatsapp'])}</code>\n"
        f"{ui('chat')} Telegram: <code>{html.escape(settings['telegram'])}</code>\n"
        f"{ui('robot')} Pending website requests: <b>{len(pending)}</b>\n"
        f"{ui('check')} Routed to admins: <b>{len(notified)}</b>"
    )


def build_website_pending_text() -> str:
    pending = [row for row in sync_website_requests_into_bot_requests() if row.get("source") == "website"]
    if not pending:
        return (
            f"{ui('check')} <b>No Website Requests Pending</b>\n{D}\n"
            "The website queue is clear right now."
        )
    lines = [f"{ui('robot')} <b>Pending Website Requests</b>\n{D}"]
    for row in pending[:12]:
        lines.append(
            f"{ui('copy')} <code>{html.escape(str(row.get('req_id', '?')))}</code>\n"
            f"{ui('user')} {html.escape(_safe_short(row.get('user_name'), 30))} • <code>{html.escape(str(row.get('admin_id', '—')))}</code>\n"
            f"{ui('robot')} @{html.escape(str(row.get('bot_username', '?')).lstrip('@'))} • {html.escape(_safe_short(row.get('bot_name'), 28))}\n"
            f"{ui('calendar')} {html.escape(_safe_short(row.get('created_at'), 25))}\n"
            f"{ui('check')} Admin notified: <b>{'Yes' if row.get('admin_notified_at') else 'No'}</b>"
        )
    if len(pending) > 12:
        lines.append(f"<i>Showing 12 of {len(pending)} pending website requests.</i>")
    return "\n\n".join(lines)


def website_pending_kb() -> "InlineKeyboardMarkup":
    return kb(
        [btn("Refresh Queue", cbd="website_pending_requests", style="success", icon=_UI["refresh"][0]),
         btn("Website Home", cbd="website_management", style="primary", icon=_UI["desktop"][0])],
        [btn("Back", cbd="admin_home", style="danger", icon=_UI["back"][0])],
    )

def gui_theme_kb(page: int = 0) -> "InlineKeyboardMarkup":
    """15 Premium OTP Themes, 5 per page. Active theme = green."""
    per_page    = 5
    start       = page * per_page
    end         = min(start + per_page, 15)
    total_pages = (15 + per_page - 1) // per_page
    rows = []; row = []
    for tid in range(start, end):
        active = tid == OTP_GUI_THEME % 15
        mark   = f"{ui('check')} " if active else ""
        name   = _THEME_NAMES.get(tid, f"Design {tid+1}")
        row.append(btn(f"{mark}{name}", cbd=f"set_gui_theme_{tid}",
                       style="success" if active else "primary"))
        if len(row) == 1: rows.append(row); row = []
    if row: rows.append(row)
    nav = []
    if page > 0:
        nav.append(btn(f"{ui('back')} Prev", cbd=f"gui_page_{page-1}"))
    nav.append(btn(f"  {page+1}/{total_pages}  ", cbd="ignore"))
    if page < total_pages - 1:
        nav.append(btn(f"Next {ui('refresh')}", cbd=f"gui_page_{page+1}"))
    if nav: rows.append(nav)
    rows.append([btn(f"{ui('back')}  Back", cbd="admin_settings",     style="primary")])
    return InlineKeyboardMarkup(rows)


def gui_theme_page_kb(page: int = 0) -> InlineKeyboardMarkup:
    """15 Premium Themes, 5 per page. Super admins only."""
    kb = []
    start_t = page * 5
    end_t   = min(start_t + 5, 15)
    for tid in range(start_t, end_t):
        name = _THEME_NAMES.get(tid, f"Design {tid+1}")
        mark = f"{ui('check')} " if tid == OTP_GUI_THEME % 15 else ""
        kb.append([InlineKeyboardButton(f"{mark}{name}", callback_data=f"set_gui_theme_{tid}", style="primary")])
    nav = []
    if page > 0:   nav.append(InlineKeyboardButton(f"{ui('back')} Prev", callback_data=f"gui_page_{page-1}", style="primary"))
    nav.append(InlineKeyboardButton(f"  {page+1}/2  ", callback_data="ignore", style="primary"))
    if page < 1:   nav.append(InlineKeyboardButton(f"Next {ui('refresh')}", callback_data=f"gui_page_{page+1}", style="success"))
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton(f"{ui('back')}  Back", callback_data="admin_settings", style="primary")])
    return InlineKeyboardMarkup(kb)

# WhatsApp admin keyboard removed (Telegram-only focus)

def admin_maintenance_kb() -> "InlineKeyboardMarkup":
    return kb(
        [btn("Free Cooldowns", cbd="admin_reset", style="primary", icon=_UI["refresh"][0]),
         btn("Clear OTP Store", cbd="clear_otps", style="danger", icon=_UI["trash"][0])],
        [btn("Purge Used Nums", cbd="purge_used", style="danger", icon=_UI["trash"][0]),
         btn("Purge Blocked", cbd="purge_blocked", style="danger", icon=_UI["trash"][0])],
        [btn("Reload Countries", cbd="reload_countries", style="primary", icon=_UI["earth"][0]),
         btn("Restart Workers", cbd="restart_workers", style="primary", icon=_UI["refresh"][0])],
        [btn("Back", cbd="admin_settings", style="danger", icon=_UI["back"][0])],
    )

def limit_kb() -> InlineKeyboardMarkup:
    ranges = [range(1, 5), range(5, 9), range(9, 13)]
    rows = [[
        InlineKeyboardButton(str(i), style="primary", callback_data=f"glimit_{i}", icon_custom_emoji_id=_UI["lock"][0])
        for i in rng
    ] for rng in ranges]
    rows.append([InlineKeyboardButton("Back", callback_data="admin_settings", style="primary", icon_custom_emoji_id=_UI["back"][0])])
    return InlineKeyboardMarkup(rows)

def advanced_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{ui('refresh')}  Test All Panels",   callback_data="test_panels", style="primary"),
         InlineKeyboardButton(f"{ui('key')}  Login All Panels",  callback_data="login_all_panels", style="primary")],
        [InlineKeyboardButton(f"{ui('satellite')}  Fetch SMS Now",     callback_data="admin_fetch_sms", style="primary"),
         InlineKeyboardButton(f"{ui('refresh')}  Restart Workers",   callback_data="restart_workers", style="primary")],
        [InlineKeyboardButton(f"{ui('key')}  OTP Tools",         callback_data="admin_otp_tools", style="primary"),
         InlineKeyboardButton(f"{ui('satellite')}  API Tokens",        callback_data="admin_api_tokens", style="success")],
        [InlineKeyboardButton(f"{ui('notepad')}  View Logs",         callback_data="view_logs", style="primary"),
         InlineKeyboardButton(f"{ui('database')}  DB Summary",        callback_data="admin_db_summary", style="success")],
        [InlineKeyboardButton(f"{ui('chart')}  OTP History",       callback_data="admin_otp_history", style="primary"),
         InlineKeyboardButton(f"{ui('bell')}  Notify Menu",       callback_data="admin_notify_menu", style="success")],
        [InlineKeyboardButton(f"{ui('earth')}  Reload Countries",  callback_data="reload_countries", style="primary"),
         InlineKeyboardButton(f"{ui('phone')}  Set Limit",         callback_data="set_limit", style="primary")],
        [InlineKeyboardButton(f"{ui('back')}  Back",              callback_data="admin_home", style="primary")],
    ])

def api_panel_selection_kb(selected: list = None) -> InlineKeyboardMarkup:
    """Panel selection keyboard for API token creation."""
    selected = selected or []
    rows = []
    for p in PANELS:
        is_selected = str(p.id) in selected
        check_mark = f"{ui('check')} " if is_selected else ""
        label = f"{check_mark}{html.escape(p.name[:20])} ({p.panel_type.upper()})"
        rows.append([InlineKeyboardButton(label, callback_data=f"api_panel_{p.id}", 
                                         style="success" if is_selected else "primary")])
    
    rows.append([
        InlineKeyboardButton(f"{ui('check')} Confirm", callback_data="api_create_confirm", style="success"),
        InlineKeyboardButton(f"{ui('cancel')} Cancel", callback_data="admin_api_tokens", style="danger")
    ])
    return InlineKeyboardMarkup(rows)

def files_kb(cats: list) -> InlineKeyboardMarkup:
    """Legacy alias kept for compatibility — use admin_numbers_kb for new code."""
    return admin_numbers_kb(cats)

def svc_sel_kb(selected: list = None) -> "InlineKeyboardMarkup":
    sel = selected or []
    svcs = ["WhatsApp","Telegram","Facebook","Instagram","Twitter","TikTok",
            "Snapchat","Google","Discord","Viber","WeChat","Signal","LINE",
            "Binance","Coinbase","PayPal","Amazon","Uber","LinkedIn","Spotify"]
    rows = []; row = []
    for s in svcs:
        active = s in sel
        mark   = f"{ui('check')} " if active else ""
        row.append(btn(f"{mark}{s}", cbd=f"us_{s}",
                       style="success" if active else "primary"))
        if len(row) == 2: rows.append(row); row = []
    if row: rows.append(row)
    rows.append([btn(f"{ui('check')}  Done",   cbd="us_done",   style="success"),
                 btn(f"{ui('cancel')}  Cancel", cbd="us_cancel",  style="danger")])
    return InlineKeyboardMarkup(rows)


def admin_list_kb(admins: list) -> InlineKeyboardMarkup:
    rows = []
    for aid in admins:
        label = f"Super Admin {aid}" if aid in INITIAL_ADMIN_IDS else f"Admin {aid}"
        icon = _UI["crown"][0] if aid in INITIAL_ADMIN_IDS else _UI["people"][0]
        rows.append([
            btn(label, cbd="ignore", style="primary", icon=icon),
            btn("Remove", cbd=f"rm_admin_{aid}", style="danger", icon=_UI["trash"][0]),
        ])
    rows.append([btn("Add Admin", cbd="add_admin_prompt", style="success", icon=_UI["bell"][0])])
    rows.append([btn("Back", cbd="admin_home", style="primary", icon=_UI["back"][0])])
    return InlineKeyboardMarkup(rows)

def perms_kb(selected: list, uid: int) -> "InlineKeyboardMarkup":
    rows = []
    for p, label in PERMISSIONS.items():
        active = p in selected
        rows.append([btn(label, cbd=f"ptoggle|{uid}|{p}",
                         style="success" if active else "primary",
                         icon=PERMISSION_ICONS.get(p, _UI["shield"][0]))])
    rows.append([btn("Save", cbd=f"pdone|{uid}", icon=_UI["copy"][0], style="success"),
                 btn("Back", cbd="admin_manage_admins", icon=_UI["back"][0], style="primary")])
    return InlineKeyboardMarkup(rows)
def logs_kb(chats: list) -> InlineKeyboardMarkup:
    kb = []
    for cid in chats:
        kb.append([
            InlineKeyboardButton(f"{ui('megaphone')} {cid}", callback_data="ignore", style="success"),
            InlineKeyboardButton(f"{ui('cancel')}", callback_data=f"rm_log_{cid}", style="danger")
        ])
    kb.append([InlineKeyboardButton(f"{ui('bell')}  Add Log Group", callback_data="add_log_prompt", style="success")])
    kb.append([InlineKeyboardButton(f"{ui('back')}  Back", callback_data="admin_home", style="primary")])
    return InlineKeyboardMarkup(kb)

def bots_list_kb(bots: list) -> InlineKeyboardMarkup:
    """Premium child bot list with status indicators and quick actions."""
    kb = []
    run_count = sum(1 for b in bots if b.get("running"))
    for info in bots:
        bid = info["id"]
        st = f"{ui('online')}" if info.get("running") else f"{ui('offline')}"
        name = html.escape(info["name"])[:18]
        running = info.get("running")
        kb.append([
            InlineKeyboardButton(f"{st} {name}", callback_data="ignore", style="primary"),
            InlineKeyboardButton(f"{ui('info')}", callback_data=f"bot_info_{bid}", style="primary"),
            InlineKeyboardButton(f"{ui('play')}" if not running else f"{ui('stop')}", 
                                 callback_data=f"bot_start_{bid}" if not running else f"bot_stop_{bid}",
                                 style="success" if not running else "danger"),
            InlineKeyboardButton(f"{ui('refresh')}", callback_data=f"bot_restart_{bid}", style="primary"),
            InlineKeyboardButton(f"{ui('trash')}", callback_data=f"bot_del_{bid}", style="danger"),
        ])
    kb.append([
        InlineKeyboardButton(f"{ui('robot')}  Add Bot", callback_data="add_bot_start", style="primary"),
        InlineKeyboardButton(f"{ui('megaphone')}  Broadcast All", callback_data="broadcast_all_bots", style="success"),
    ])
    kb.append([
        InlineKeyboardButton(f"{ui('play')}  Start All", callback_data="bots_start_all", style="primary"),
        InlineKeyboardButton(f"{ui('stop')}  Stop All", callback_data="bots_stop_all", style="danger"),
    ])
    kb.append([
        InlineKeyboardButton(f"{ui('chart')}  All Stats", callback_data="bots_all_stats", style="primary"),
        InlineKeyboardButton(f"{ui('refresh')}  Refresh", callback_data="admin_bots", style="primary"),
    ])
    kb.append([InlineKeyboardButton(f"{ui('back')}  Back to Admin", callback_data="admin_home", style="primary")])
    return InlineKeyboardMarkup(kb)
def bot_actions_kb(bid: str, running: bool, info: dict = None) -> InlineKeyboardMarkup:
    """Expanded per-bot action panel with colored buttons."""
    info = info or {}
    r_row = []
    if running:
        r_row = [
            InlineKeyboardButton(f"{ui('stop')}  Stop",    callback_data=f"bot_stop_{bid}", style="danger"),
            InlineKeyboardButton(f"{ui('refresh')}  Restart", callback_data=f"bot_restart_{bid}", style="primary")
        ]
    else:
        r_row = [
            InlineKeyboardButton(f"{ui('play')}  Start",   callback_data=f"bot_start_{bid}", style="success"),
            InlineKeyboardButton(f"{ui('refresh')}  Restart", callback_data=f"bot_restart_{bid}", style="primary")
        ]
    return InlineKeyboardMarkup([
        r_row,
        [
            InlineKeyboardButton(f"{ui('notepad')}  View Logs", callback_data=f"bot_log_{bid}", style="primary"),
            InlineKeyboardButton(f"{ui('chart')}  Bot Stats", callback_data=f"bot_stats_{bid}", style="primary")
        ],
        [
            InlineKeyboardButton(f"{ui('megaphone')}  Broadcast", callback_data=f"bot_bcast_{bid}", style="success"),
            InlineKeyboardButton(f"{ui('link')}  Edit Links", callback_data=f"bot_editlinks_{bid}", style="primary")
        ],
        [
            InlineKeyboardButton(f"{ui('trash')}  Delete Bot", callback_data=f"bot_del_{bid}", style="danger"),
            InlineKeyboardButton(f"{ui('back')}  Back", callback_data="admin_bots", style="primary")
        ],
    ])

def confirm_del_bot_kb(bid: str) -> "InlineKeyboardMarkup":
    return kb([btn("Yes, Delete", cbd=f"bot_del_yes_{bid}", style="danger", icon=_UI["check"][0]),
               btn("Cancel", cbd=f"bot_info_{bid}", style="primary", icon=_UI["cancel"][0])])
def bot_edit_links_kb(bid: str) -> InlineKeyboardMarkup:
    """Edit a child bot's configured links inline with colored buttons."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{ui('megaphone')}  Channel Link",    callback_data=f"bot_setlink_{bid}_CHANNEL_LINK", style="success")],
        [InlineKeyboardButton(f"{ui('chat')}  OTP Group Link",  callback_data=f"bot_setlink_{bid}_OTP_GROUP_LINK", style="primary")],
        [InlineKeyboardButton(f"{ui('phone')}  Number Bot Link", callback_data=f"bot_setlink_{bid}_NUMBER_BOT_LINK", style="primary")],
        [InlineKeyboardButton(f"{ui('help')}  Support User",    callback_data=f"bot_setlink_{bid}_SUPPORT_USER", style="primary")],
        [InlineKeyboardButton(f"{ui('back')}  Back",            callback_data=f"bot_info_{bid}", style="primary")],
    ])

def confirm_kb(action: str) -> "InlineKeyboardMarkup":
    return kb([btn("Confirm", cbd=f"confirm_{action}", style="success", icon=_UI["check"][0]),
               btn("Cancel", cbd="admin_home", style="danger", icon=_UI["cancel"][0])])
# ═══════════════════════════════════════════════════════════
#  PANEL LOGIN / FETCH
# ═══════════════════════════════════════════════════════════
async def test_api_panel(panel: PanelSession) -> bool:
    """Test Type 1 CR-API panel."""
    try:
        s = await panel.get_session()
        now = datetime.now(); prev = now - timedelta(hours=24)
        params = {"token":panel.token,"dt1":prev.strftime("%Y-%m-%d %H:%M:%S"),
                  "dt2":now.strftime("%Y-%m-%d %H:%M:%S"),"records":1}
        async with s.get(panel.base_url,params=params,timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200: return False
            try: data = await resp.json(content_type=None)
            except Exception: return False
            if isinstance(data, list): return True
            if isinstance(data, dict):
                st = str(data.get("status","")).lower()
                if st == "error": return False
                return st == "success" or any(k in data for k in ("data","records","sms"))
    except Exception as e: logger.error(f"API test '{panel.name}': {e}")
    return False

async def test_reseller_api(panel: PanelSession) -> bool:
    """Test Type 2 Reseller API (mdr.php endpoint)."""
    try:
        s   = await panel.get_session()
        now = datetime.now(); prev = now - timedelta(hours=1)
        params = {
            "token":    panel.token,
            "fromdate": prev.strftime("%Y-%m-%d %H:%M:%S"),
            "todate":   now.strftime("%Y-%m-%d %H:%M:%S"),
            "records":  1,
        }
        async with s.get(panel.base_url, params=params,
                         timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200: return False
            data = await resp.json(content_type=None)
            st = str(data.get("status","")).lower()
            return "success" in st or "data" in data
    except Exception as e:
        logger.error(f"Reseller API test '{panel.name}': {e}")
    return False

async def fetch_reseller_api(panel: PanelSession) -> Optional[list]:
    """
    Fetch SMS records from Type 2 Reseller API.
    URL format: http://host/crapi/reseller/mdr.php
    Response:   {"status":"Success","records":N,"data":[{"datetime":...,"number":...,"cli":...,"message":...}]}
    """
    try:
        s   = await panel.get_session()
        now = datetime.now(); prev = now - timedelta(days=1)
        params = {
            "token":    panel.token,
            "fromdate": prev.strftime("%Y-%m-%d %H:%M:%S"),
            "todate":   now.strftime("%Y-%m-%d %H:%M:%S"),
            "records":  API_MAX_RECORDS,   # max 200
        }
        async with s.get(panel.base_url, params=params,
                         timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                panel.fail_count += 1
                if panel.fail_count >= 3: panel.is_logged_in = False
                return None
            data = await resp.json(content_type=None)
            st = str(data.get("status","")).lower()
            if "error" in st:
                logger.error(f"Reseller API '{panel.name}' error: {data.get('status')}")
                panel.fail_count += 1
                if panel.fail_count >= 3: panel.is_logged_in = False
                return None
            records = data.get("data") or []
            panel.fail_count = 0; panel.is_logged_in = True
            out = []
            for rec in records:
                if not isinstance(rec, dict): continue
                dt  = rec.get("datetime") or rec.get("dt") or ""
                num = str(rec.get("number","")).replace("+","").strip()
                cli = str(rec.get("cli","") or "unknown").lower()
                msg = str(rec.get("message","") or rec.get("text","") or "")
                if not msg or not num: continue
                out.append([str(dt), num, cli, msg])
            out.sort(key=lambda x: x[0], reverse=True)
            logger.info('📥 Reseller %s → %d record(s)' % (panel.name, len(out)))
            return out
    except Exception as e:
        logger.error(f"Reseller API fetch '{panel.name}': {e}")
        panel.fail_count += 1
        if panel.fail_count >= 3: panel.is_logged_in = False
        return None

async def login_to_panel(panel: PanelSession) -> bool:
    """
    Login for panels that follow the /ints/ URL structure, e.g.:
        base_url  = http://185.2.83.39/ints          (trailing slash stripped)
        login page= http://185.2.83.39/ints/login
        form POST = http://185.2.83.39/ints/signin   (relative → urljoin)
        stats page= http://185.2.83.39/ints/SMSCDRStats
        data API  = http://185.2.83.39/ints/res/data_smscdr.php

    Key rule: panel.base_url already contains the full path prefix (/ints),
    so use it directly for all endpoint construction.  Never strip the path.
    """
    if panel.panel_type in ("api", "api_v2"):
        if panel.panel_type == "api_v2":
            ok = await test_reseller_api(panel)
        else:
            ok = await test_api_panel(panel)
        panel.is_logged_in = ok
        api_label = "Reseller API" if panel.panel_type == "api_v2" else "CR-API"
        if ok:  logger.info(f"{ui('rocket')} {api_label} panel \"{panel.name}\" — token OK")
        else:   logger.warning(f"{ui('rocket')} {api_label} panel \"{panel.name}\" — token FAILED")
        return ok

    logger.info(f"{ui('key')} Logging in to \"{panel.name}\"  →  {panel.base_url}")
    await panel.reset_session()  # fresh isolated CookieJar every attempt

    try:
        s = await panel.get_session()

        # ── 1. Load the login page ────────────────────────────────────
        # base_url already has /ints, so /login gives http://ip/ints/login
        login_url = panel.login_url or f"{panel.base_url}/login"
        logger.info(f"   ↗ GET  {login_url}")
        async with s.get(login_url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                logger.warning(f"   ✗ Login page HTTP {r.status}  panel=\"{panel.name}\"")
                return False
            pg = await r.text()

        # ── 2. Parse the form ─────────────────────────────────────────
        soup = BeautifulSoup(pg, "html.parser")
        form = soup.find("form")
        if not form:
            logger.warning(f"   ✗ No <form> found at {login_url}")
            return False

        payload = {}
        for tag in form.find_all("input"):
            nm  = tag.get("name")
            val = tag.get("value", "")
            ph  = (tag.get("placeholder", "") + " " + (nm or "")).lower()
            tp  = tag.get("type", "text").lower()
            if not nm:
                continue
            if tp == "hidden":
                # Keep all hidden fields exactly — these carry CSRF tokens
                payload[nm] = val
            elif any(k in ph for k in ("user", "email", "login", "uname", "username")):
                payload[nm] = panel.username or ""
                logger.info(f"   ↳ username field → {nm}")
            elif any(k in ph for k in ("pass", "pwd", "secret", "password")):
                payload[nm] = panel.password or ""
                logger.info(f"   ↳ password field → {nm}")
            elif any(k in ph for k in ("ans", "captcha", "answer", "result", "sum", "calc")):
                # Solve the arithmetic captcha (e.g. "What is 4 + 7?")
                cap = re.search(r"(\d+)\s*([+\-*])\s*(\d+)", form.get_text() or pg)
                if cap:
                    n1, op, n2 = int(cap.group(1)), cap.group(2), int(cap.group(3))
                    ans = n1 + n2 if op == "+" else (n1 - n2 if op == "-" else n1 * n2)
                    payload[nm] = str(ans)
                    logger.info(f"   ↳ captcha {n1}{op}{n2} = {ans}")
            else:
                payload[nm] = val

        # ── 3. Resolve the form action URL ────────────────────────────
        # MUST use urljoin so "signin" on page "/ints/login" becomes
        # "/ints/signin", NOT "/signin".
        # e.g.  urljoin("http://ip/ints/login", "signin")
        #       → "http://ip/ints/signin"   ✓
        raw_action = (form.get("action") or "").strip()
        if raw_action:
            if raw_action.startswith("http"):
                action = raw_action                         # already absolute
            else:
                # urljoin resolves relative to the current page directory
                from urllib.parse import urljoin
                action = urljoin(login_url, raw_action)
        else:
            action = login_url                              # no action = post to same URL

        origin = login_url.split("/ints/")[0] if "/ints/" in login_url else                  "/".join(login_url.split("/")[:3])

        logger.info(f"   ↗ POST {action}")
        async with s.post(
            action, data=payload,
            headers={"Referer": login_url, "Origin": origin},
            timeout=aiohttp.ClientTimeout(total=20),
            allow_redirects=True,
        ) as pr:
            final_url = str(pr.url)
            body      = await pr.text()
            body_l    = body.lower()
            logger.info(f"   ← HTTP {pr.status}  final URL → {final_url}")

            # ── 4. Detect success ─────────────────────────────────────
            #
            # IMPORTANT: All /ints/ panels POST to ints/signin (that is the
            # form action).  After a successful login they redirect to
            # ints/agent/SMSDashboard.  The old "still_auth" check wrongly
            # flagged Wolf and others because "signin" appeared in either the
            # form action URL or a redirect step, even though login succeeded.
            #
            # Rule: if the response body contains dashboard/logout keywords
            # → login succeeded, regardless of what the URL says.
            # Only fall back to URL inspection when the body is ambiguous.
            _OK_BODY = {
                "logout", "log out", "sign out", "signout",
                "dashboard", "smscdr", "sms log", "sms report",
                "smscdrstats", "welcome", "my account",
                "sms dashboard", "smsdashboard",
            }
            # A failed login usually returns a page with these keywords
            # AND has no dashboard content in the body.
            _FAIL_BODY = {"invalid", "incorrect", "wrong password",
                          "failed", "error", "invalid credentials"}
            _OK_URL    = {"dashboard", "smscdr", "smscdrstats",
                          "welcome", "inbox", "report", "home"}

            body_ok    = any(k in body_l for k in _OK_BODY)
            body_fail  = any(k in body_l for k in _FAIL_BODY)
            url_ok     = any(k in final_url.lower() for k in _OK_URL)

            # body_fail + no body_ok = definite failure
            # body_ok alone = definite success (URL doesn't matter)
            # neither: use URL as tiebreaker
            if body_fail and not body_ok:
                err_el = BeautifulSoup(body,"html.parser").find(
                    class_=re.compile(r"error|alert|danger|invalid", re.I))
                hint = err_el.get_text(strip=True)[:120] if err_el else body_l[:120]
                logger.warning(
                    f"   ✗ Login FAILED  panel=\"{panel.name}\"  hint=\"{hint}\""
                )
                panel.fail_count += 1
                return False

            if not body_ok and not url_ok:
                logger.warning(
                    f"   ✗ Login FAILED  panel=\"{panel.name}\"  "
                    f"(no success signal in body or URL)  final=\"{final_url[-60:]}\""
                )
                panel.fail_count += 1
                return False

            logger.info(f"   ✓ Authenticated  panel=\"{panel.name}\""
                        f"  (body_ok={body_ok} url_ok={url_ok})")

            # ── 5. Discover the SMS data endpoint ─────────────────────
            #
            # Panel redirects to:
            #   http://ip/ints/agent/SMSDashboard
            # which means the stats page is at:
            #   http://ip/ints/agent/SMSCDRStats  (agent sub-dir)
            # NOT at:
            #   http://ip/ints/SMSCDRStats         (always 404)
            #
            # Strategy: extract the directory portion of final_url
            # and try it first.  Fall back to panel.base_url if that fails.
            from urllib.parse import urlparse as _up
            parsed_final = _up(final_url)
            # directory of the redirect URL, e.g. /ints/agent from /ints/agent/SMSDashboard
            path_parts       = parsed_final.path.rstrip("/").rsplit("/", 1)
            redirect_dir     = path_parts[0] if len(path_parts) > 1 else ""
            redirect_base    = f"{parsed_final.scheme}://{parsed_final.netloc}{redirect_dir}"
            # e.g. http://185.2.83.39/ints/agent

            # Try the redirect directory first, then panel.base_url as fallback
            candidate_bases = []
            if redirect_base and redirect_base != panel.base_url:
                candidate_bases.append(redirect_base)    # /ints/agent  ← correct for your panels
            candidate_bases.append(panel.base_url)       # /ints         ← fallback

            for disc_base in candidate_bases:
                for stats_path in ["/SMSCDRStats", "/client/SMSCDRStats",
                                   "/smscdrstats", "/sms/log", "/smslogs", "/sms"]:
                    try:
                        stats_url = disc_base + stats_path
                        logger.info(f"   🔍 Trying {stats_url}")
                        async with s.get(stats_url, timeout=aiohttp.ClientTimeout(total=10)) as sr:
                            if sr.status != 200:
                                logger.info(f"      → {sr.status} skip")
                                continue
                            page = await sr.text()
                            for sc in BeautifulSoup(page, "html.parser").find_all("script"):
                                if not sc.string:
                                    continue
                                m = re.search(
                                    r'sAjaxSource["\'\\s]*:\s*["\']([^"\']+)["\']',
                                    sc.string)
                                if m:
                                    found = m.group(1)
                                    if not found.startswith("http"):
                                        found = disc_base + "/" + found.lstrip("/")
                                    if "sesskey=" in found:
                                        parts         = found.split("?", 1)
                                        panel.api_url = parts[0]
                                        sk = re.search(r"sesskey=([^&]+)", parts[1])
                                        if sk: panel.sesskey = sk.group(1)
                                    else:
                                        panel.api_url = found
                                    panel.stats_url    = stats_url   # store for Referer
                                    panel.is_logged_in = True
                                    panel.fail_count   = 0
                                    logger.info(
                                        f"   📡 Endpoint found: {panel.api_url}"
                                        + (f"  sesskey={panel.sesskey[:12]}…" if panel.sesskey else ""))
                                    return True
                    except Exception as disc_err:
                        logger.info(f"   ↳ error checking {stats_url}: {disc_err}")

            # ── 6. Fallback: use redirect directory + conventional path ──
            # e.g. http://ip/ints/agent/res/data_smscdr.php
            best_base          = candidate_bases[0]   # prefer agent-dir if found
            panel.api_url      = f"{best_base}/res/data_smscdr.php"
            panel.stats_url    = f"{best_base}/SMSCDRStats"
            panel.is_logged_in = True
            panel.fail_count   = 0
            logger.info(f"   📡 Fallback endpoint: {panel.api_url}")
            return True

    except aiohttp.ClientConnectorError as e:
        logger.error(f"{ui('rocket')} Cannot connect to panel \"{panel.name}\": {e}")
    except asyncio.TimeoutError:
        logger.error(f"⏱  Connection timeout  panel=\"{panel.name}\"")
    except Exception as e:
        logger.error(f"{ui('cancel')} Login error  panel=\"{panel.name}\": {e}", exc_info=True)

    panel.fail_count += 1
    return False


async def fetch_panel_sms(panel: PanelSession) -> Optional[list]:
    if panel.panel_type == "api_v2":
        return await fetch_reseller_api(panel)
    if panel.panel_type == "api":
        try:
            s=await panel.get_session(); now=datetime.now(); prev=now-timedelta(days=1)
            params={"token":panel.token,"dt1":prev.strftime("%Y-%m-%d %H:%M:%S"),
                    "dt2":now.strftime("%Y-%m-%d %H:%M:%S"),"records":API_MAX_RECORDS}
            async with s.get(panel.base_url,params=params,timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status!=200:
                    panel.fail_count+=1
                    if panel.fail_count>=3: panel.is_logged_in=False
                    return None
                try: data=await resp.json(content_type=None)
                except Exception as je:
                    logger.error(f"API JSON '{panel.name}': {je}")
                    panel.fail_count+=1
                    if panel.fail_count>=3: panel.is_logged_in=False
                    return None
                records=[]
                if isinstance(data,list): records=data
                elif isinstance(data,dict):
                    st=str(data.get("status","")).lower()
                    if st=="error":
                        logger.error(f"API '{panel.name}' auth: {data.get('msg','')}")
                        panel.fail_count+=1
                        if panel.fail_count>=3: panel.is_logged_in=False
                        return None
                    records=(data.get("data") or data.get("records") or
                             data.get("sms") or data.get("messages") or [])
                panel.fail_count=0; panel.is_logged_in=True
                if not records: return []
                out=[]
                for rec in records:
                    if not isinstance(rec,dict): continue
                    dt =(rec.get("dt")      or rec.get("date")      or rec.get("timestamp") or "")
                    num=(rec.get("num")     or rec.get("number")    or rec.get("recipient") or rec.get("phone") or "")
                    cli=(rec.get("cli")     or rec.get("sender")    or rec.get("originator")or rec.get("service") or "unknown")
                    msg=(rec.get("message") or rec.get("text")      or rec.get("body")      or rec.get("content") or "")
                    if not msg and not num: continue
                    out.append([str(dt),str(num).replace("+","").strip(),str(cli).lower(),str(msg)])
                out.sort(key=lambda x:x[0],reverse=True)
                return out
        except Exception as e:
            logger.error(f"API fetch '{panel.name}': {e}")
            panel.fail_count+=1
            if panel.fail_count>=3: panel.is_logged_in=False
            return None
    elif panel.panel_type=="login":
        if not panel.api_url: return None
        try:
            s=await panel.get_session(); now=datetime.now(); prev=now-timedelta(days=1)
            params={"fdate1":prev.strftime("%Y-%m-%d %H:%M:%S"),"fdate2":now.strftime("%Y-%m-%d %H:%M:%S"),
                    "sEcho":"1","iDisplayStart":"0","iDisplayLength":"200","iSortCol_0":"0","sSortDir_0":"desc"}
            if panel.sesskey: params["sesskey"]=panel.sesskey
            # Use the discovered stats page URL as Referer (server validates this)
            _referer = panel.stats_url or f"{panel.base_url}/SMSCDRStats"
            headers={"X-Requested-With":"XMLHttpRequest",
                     "Referer": _referer,
                     "Accept":"application/json, text/javascript, */*; q=0.01"}
            async with s.get(panel.api_url,params=params,headers=headers,
                             timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status!=200:
                    panel.fail_count+=1
                    if panel.fail_count>=3: panel.is_logged_in=False
                    return None
                data=await resp.json(content_type=None)
                if "aaData" in data:
                    panel.fail_count=0; data["aaData"].sort(key=lambda x:str(x[0]),reverse=True)
                    return data["aaData"]
                panel.fail_count+=1
                if panel.fail_count>=3: panel.is_logged_in=False
                return None
        except Exception as e:
            logger.error(f"Login fetch '{panel.name}': {e}")
            panel.fail_count+=1
            if panel.fail_count>=3: panel.is_logged_in=False
            return None
    return None

# ═══════════════════════════════════════════════════════
#  IVAS WORKER  (v2 — improved)
#  • seen set trims to last 500 when >1000 (no blind clear)
#  • check_counter % 100 for periodic panel-removal check
#  • Proper ping task lifecycle with CancelledError handling
#  • OSError separate from WebSocketException for network faults
# ═══════════════════════════════════════════════════════
async def _ivas_ping(ws, interval_ms: int):
    while True:
        await asyncio.sleep(interval_ms / 1000)
        try:
            await ws.send("3")
        except Exception:
            break

async def ivas_worker(panel: PanelSession):
    logger.info(f"📡 IVAS worker starting → \"{panel.name}\"")
    seen: set = set()
    while True:
        try:
            if panel.panel_type != "ivas" or not panel.uri:
                logger.info(f"IVAS \"{panel.name}\" — no URI or wrong type, stopping.")
                break
            ssl_ctx = ssl._create_unverified_context()
            try:
                async with websockets.connect(panel.uri, ssl=ssl_ctx) as ws:
                    logger.info(f"{ui('check')} IVAS \"{panel.name}\" connected")
                    initial = await ws.recv()
                    ping_iv = 25000
                    try:
                        if initial.startswith("0{"):
                            ping_iv = json.loads(initial[1:]).get("pingInterval", 25000)
                    except Exception:
                        pass
                    await ws.send("40/livesms,")
                    ping_task = asyncio.create_task(_ivas_ping(ws, ping_iv))
                    try:
                        counter = 0
                        while True:
                            counter += 1
                            # Periodic check every 100 messages: panel still in DB?
                            if counter % 100 == 0:
                                ids = [p.id for p in PANELS]
                                if panel.id is not None and panel.id not in ids:
                                    logger.info(f"IVAS \"{panel.name}\" removed — stopping.")
                                    break
                            raw = await ws.recv()
                            if not raw.startswith("42/livesms,"):
                                continue
                            try:
                                d = json.loads(raw[raw.find("["):])
                                if not (isinstance(d, list) and len(d) > 1
                                        and isinstance(d[1], dict)):
                                    continue
                                sms     = d[1]
                                number  = str(sms.get("recipient", "")).replace("+", "").strip()
                                body    = str(sms.get("message", "") or "")
                                service = str(sms.get("originator", "") or "unknown")
                                otp     = extract_otp_regex(body)
                                uniq    = f"{number}-{body[:20]}"
                                if uniq in seen:
                                    continue
                                seen.add(uniq)
                                # Trim to last 500 entries when over 1000 — never blind clear
                                if len(seen) > 1000:
                                    seen = set(list(seen)[-500:])
                                logger.info(
                                    f"📨 IVAS \"{panel.name}\" …{number[-5:]} "
                                    f"svc={service[:10]} otp={otp or '—'}")
                                await process_incoming_sms(
                                    None, number, body, otp, service, panel.name)
                            except Exception as e:
                                logger.error(f"IVAS \"{panel.name}\" parse: {e}")
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass
            except websockets.exceptions.WebSocketException as e:
                logger.error(f"IVAS WS \"{panel.name}\": {e}. Retry 5s…")
                await asyncio.sleep(5)
            except OSError as e:
                logger.error(f"IVAS network \"{panel.name}\": {e}. Retry 10s…")
                await asyncio.sleep(10)
            except Exception as e:
                logger.error(f"IVAS \"{panel.name}\": {e}. Retry 5s…")
                await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"IVAS \"{panel.name}\" critical: {e}. Retry 10s…")
            await asyncio.sleep(10)

def handle_task_exception(task: asyncio.Task):
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Task '{task.get_name()}': {e}", exc_info=True)

async def start_ivas_workers():
    for panel in PANELS:
        if panel.panel_type == "ivas":
            task = asyncio.create_task(ivas_worker(panel), name=f"IVAS-{panel.name}")
            task.add_done_callback(handle_task_exception)
            IVAS_TASKS[panel.name] = task
            logger.info(f"📡 IVAS task created for \"{panel.name}\"")

# forward_otp_to_main — REMOVED.
# Each bot (main and child) sends OTPs ONLY to its own configured log_chats.
# Main bot OTPs never go to child groups; child bot OTPs never go to main groups.
# The isolation is guaranteed by each bot having its own separate database
# with its own log_chats table.

async def process_incoming_sms(bot_app,num_raw:str,msg_body:str,
                                otp_code:Optional[str],service_name:str,panel_name:str):
    global app
    if bot_app is None: bot_app=app
    if otp_code and num_raw:
        append_otp(num_raw, otp_code)
    # ROUTING: each bot (main or child) sends ONLY to its own groups.
    # No cross-forwarding. Child bot OTPs stay in child groups.
    # Main bot OTPs stay in main groups.
    async with db.AsyncSessionLocal() as session:
        db_obj=(await session.execute(
            select(db.Number).where(db.Number.phone_number==num_raw)
        )).scalar_one_or_none()
        if db_obj and db_obj.assigned_to and db_obj.status in ("ASSIGNED","RETENTION"):
            await do_sms_hit(bot_app,db_obj,otp_code,msg_body,service_name,panel_name,num_raw,session)
        else:
            await log_unassigned(bot_app,num_raw,msg_body,otp_code,service_name,panel_name)

async def do_sms_hit(bot_app,db_obj,otp_code,msg_body,service_name,panel_name,num_raw,session):
    global app
    if bot_app is None: bot_app=app
    if bot_app is None: return
    db_obj.last_msg=msg_body
    if otp_code: db_obj.last_otp=otp_code
    cnt=OTP_SESSION_COUNTS.get(num_raw,0)
    if otp_code: cnt+=1; OTP_SESSION_COUNTS[num_raw]=cnt
    header={1:f"{ui('check')} OTP RECEIVED",2:"🫟 2nd OTP",3:"🫂 3rd OTP"}.get(
        cnt,f"☠️ {cnt}th OTP" if cnt>3 else "📩 NEW MESSAGE")
    clean=re.sub(r"[^0-9]","",otp_code) if otp_code else ""
    _,flag,region=get_country_info(num_raw)
    dial=get_country_code(num_raw) or ""; last5=get_last5(num_raw)
    svc=get_service_short(service_name)
    now_ts      = datetime.now().strftime("%H:%M:%S")
    count_badge = {1:"1️⃣ First OTP", 2:"2️⃣ Second OTP", 3:"3️⃣ Third OTP"}.get(
        cnt, f"🔢 OTP #{cnt}" if cnt > 0 else "📩 New SMS")

    # ── Write History so web dashboard shows ALL OTPs ──────────────
    try:
        _hcat = (f"{flag} {region} - {service_name}".strip()
                 if region and region not in ("Unknown","") else service_name)
        async with db.AsyncSessionLocal() as _hs:
            _hs.add(db.History(
                user_id=db_obj.assigned_to or 0,
                phone_number=num_raw,
                otp=otp_code or "",
                category=_hcat,
            ))
            await _hs.commit()
    except Exception as _he:
        logger.error(f"do_sms_hit history: {_he}")

    # Build OTP messages (OTP shown directly — no masking)
    history_rows = await db.get_recent_history_for_number(num_raw, limit=6)
    dm_txt  = build_otp_msg(header, count_badge, clean, msg_body,
                             svc, panel_name, flag, region, dial, last5,
                             for_group=False)
    grp_txt = build_otp_msg(header, count_badge, clean, msg_body,
                             svc, panel_name, flag, region, dial, last5,
                             for_group=True)
    history_block = format_recent_history_block(history_rows, skip_otp=clean)
    if history_block:
        dm_txt = f"{dm_txt}\n\n{history_block}"

    dm_kb  = otp_keyboard(otp_code, msg_body, for_group=False)
    grp_kb = otp_keyboard(otp_code, msg_body, for_group=True)

    # ── DM to assigned user ───────────────────────────────
    if db_obj.assigned_to:
        try:
            await bot_app.bot.send_message(
                chat_id=db_obj.assigned_to, text=dm_txt,
                reply_markup=dm_kb, parse_mode="HTML")
        except TelegramForbidden:
            logger.warning(f"User {db_obj.assigned_to} blocked bot.")
        except Exception as e:
            logger.error(f"DM error ({db_obj.assigned_to}): {e}")

    # ── Log groups — compact reference format + 15-min auto-delete ──
    _DEL_SEC = 900   # 15 minutes
    for gid in await db.get_all_log_chats():
        try:
            sent = await bot_app.bot.send_message(
                chat_id=gid, text=grp_txt,
                reply_markup=grp_kb, parse_mode="HTML")
            # Schedule deletion
            if bot_app.job_queue:
                bot_app.job_queue.run_once(
                    _delete_msg_job, when=_DEL_SEC,
                    data={"chat_id": gid, "msg_id": sent.message_id},
                    name=f"del_{gid}_{sent.message_id}")
            else:
                asyncio.create_task(
                    _delete_msg_after(bot_app, gid, sent.message_id, _DEL_SEC))
        except TelegramForbidden:
            logger.error(f"Not in log group {gid}")
        except Exception as e:
            logger.error(f"Log group ({gid}): {e}")

    # ── Record & reassign ─────────────────────────────────
    if otp_code:
        await session.commit()
        cat,user_id,msg_id=await db.record_success(num_raw,otp_code)
        if user_id is None: return
        limit=await get_effective_limit(user_id)
        await db.request_numbers(user_id,cat,count=limit,message_id=msg_id)
        active=await db.get_active_numbers(user_id)
        if active and msg_id:
            try:
                pfx=await db.get_user_prefix(user_id)
                _, _, svc_lbl = split_category_label(active[0].category)
                pfx_txt=f"on-{pfx}" if pfx else "off"
                lines=[]
                for idx,n in enumerate(active,1):
                    e=f"{idx}\uFE0F\u20E3" if idx<10 else ("🔟" if idx==10 else f"[{idx}]")
                    lines.append(f"{e} <code>+{n.phone_number}</code>")
                text=(
                    f"{ui('zap')} <b>New Numbers Ready!</b>\n{D}\n"
                    f"{ui('globe')} <b>Service:</b> {html.escape(svc_lbl)}\n"
                    + "\n".join(lines)
                    + f"\n\n{ui('chart')} <b>Prefix:</b> {pfx_txt}\n{ui('bolt')} <b>Waiting for SMS…</b>"
                )
                await bot_app.bot.edit_message_text(
                    chat_id=user_id,message_id=msg_id,
                    text=text,
                    reply_markup=waiting_kb(pfx,service=svc_lbl),parse_mode="HTML")
            except Exception as e: logger.error(f"Edit msg: {e}")
    else:
        session.add(db_obj); await session.commit()

async def log_unassigned(bot_app,num_raw,msg_body,otp_code,service_name,panel_name):
    global app
    if bot_app is None: bot_app=app
    if bot_app is None: return

    # ── Write to History so the web dashboard can show ALL OTPs ──────────
    if num_raw:
        try:
            _,_flag,_region = get_country_info(num_raw)
            _cat = (f"{_flag} {_region} - {service_name}".strip() if _region and _region not in ("Unknown","") else service_name.strip())
            async with db.AsyncSessionLocal() as _hist_sess:
                _hist_sess.add(db.History(
                    user_id=0,
                    phone_number=num_raw,
                    otp=otp_code or "",
                    category=_cat,
                ))
                await _hist_sess.commit()
                logger.debug(f"History record added: {num_raw} → {service_name}")
        except Exception as _he:
            logger.error(f"log_unassigned history ERROR: {_he}", exc_info=True)
    # ─────────────────────────────────────────────────────────────────────

    log_chats=await db.get_all_log_chats()
    if not log_chats: return
    _,flag,region=get_country_info(num_raw)
    dial=get_country_code(num_raw) or ""; last5=get_last5(num_raw)
    svc=get_service_short(service_name)
    clean=re.sub(r"[^0-9]","",otp_code) if otp_code else ""
    # Unassigned OTPs use "📩 UNASSIGNED" as the header in the current theme
    _ua_header = "📩 OTP LOG"
    _ua_badge  = ""
    txt = build_otp_msg(_ua_header, _ua_badge, clean, msg_body,
                        svc, panel_name, flag, region, dial, last5,
                        for_group=True)
    kb = otp_keyboard(otp_code, msg_body, for_group=True)
    _DEL_SEC2 = 900
    for gid in log_chats:
        try:
            sent = await bot_app.bot.send_message(chat_id=gid,text=txt,reply_markup=kb,parse_mode="HTML")
            if bot_app.job_queue:
                bot_app.job_queue.run_once(
                    _delete_msg_job, when=_DEL_SEC2,
                    data={"chat_id": gid, "msg_id": sent.message_id},
                    name=f"del_{gid}_{sent.message_id}")
            else:
                asyncio.create_task(_delete_msg_after(bot_app, gid, sent.message_id, _DEL_SEC2))
        except TelegramForbidden: logger.error(f"Not in log group {gid}")
        except Exception as e: logger.error(f"Log ({gid}): {e}")


# ═══════════════════════════════════════════════════════════
#  AUTO-DELETE HELPERS  (group messages removed after 15 min)
# ═══════════════════════════════════════════════════════════
async def _delete_msg_after(bot_app, chat_id: int, msg_id: int, delay_sec: int):
    """Coroutine: waits delay_sec then silently deletes the message."""
    await asyncio.sleep(delay_sec)
    try:
        await bot_app.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        logger.info(f"🗑️  Auto-deleted group msg {msg_id} from {chat_id}")
    except Exception:
        pass   # already deleted or bot lacks permission — ignore

async def _delete_msg_job(context):
    """PTB job_queue callback for auto-delete."""
    d = context.job.data or {}
    try:
        await context.bot.delete_message(
            chat_id=d["chat_id"], message_id=d["msg_id"])
        logger.info(f"🗑️  Auto-deleted group msg {d['msg_id']} from {d['chat_id']}")
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════
#  ACTIVE WATCHER
# ═══════════════════════════════════════════════════════════
async def active_watcher(application):
    global app, PROCESSED_MESSAGES
    app = application
    logger.info(f"{ui('rocket')} Active watcher started  ({len(PANELS)} panel(s) loaded)")

    # ── Initial login pass ────────────────────────────────────────
    # IMPORTANT: panels sharing the same base_url (same server, multiple
    # accounts) MUST login sequentially with a small gap.  Concurrent logins
    # on the same server cause the server to invalidate the earlier session
    # the moment the second one completes, leaving only one account active.
    # We group by base_url and login each group one account at a time.
    from collections import defaultdict as _dd
    login_groups = _dd(list)
    api_panels   = []
    for panel in PANELS:
        if panel.panel_type == "login":
            login_groups[panel.base_url].append(panel)
        elif panel.panel_type == "api":
            api_panels.append(panel)

    # Login panels — sequentially within each host group
    for host, group in login_groups.items():
        if len(group) > 1:
            logger.info(f"{ui('key')} Logging in {len(group)} accounts on {host} (sequential to avoid session clash)")
        for panel in group:
            logger.info(f"{ui('key')} Initial login → \"{panel.name}\"")
            ok = await login_to_panel(panel)
            if ok:
                if panel.id: await update_panel_login(panel.id, panel.sesskey, panel.api_url, True)
                logger.info(f"{ui('check')} \"{panel.name}\" ready  →  {panel.api_url}")
            else:
                logger.warning(f"{ui('warn')}  \"{panel.name}\" login failed, will retry each cycle")
            if len(group) > 1:
                await asyncio.sleep(1.5)  # give the server time between accounts

    # Initialise API panel sessions (no login needed, just a session object)
    for panel in api_panels:
        await panel.get_session()
        logger.info(f"{ui('rocket')} API panel \"{panel.name}\" session ready")
    for gid in await db.get_all_log_chats():
        try: await application.bot.send_message(gid,f"{ui('rocket')} <b>OTP Engine Online</b>",parse_mode="HTML")
        except Exception: pass
    first_cycle = True
    while True:
        t0 = datetime.now()
        try:
            try: await db.clean_cooldowns()
            except Exception: pass
            async with db.AsyncSessionLocal() as session:
                from sqlalchemy import or_
                active_nums=(await session.execute(
                    select(db.Number).filter(
                        or_(db.Number.status=="ASSIGNED",db.Number.status=="RETENTION"))
                )).scalars().all()
                targets={n.phone_number:n for n in active_nums}

                async def fetch_one(panel):
                    try:
                        # ── Login / reconnect if needed ──────────────────────
                        if not panel.is_logged_in:
                            if panel.panel_type == "login":
                                logger.info(f"{ui('refresh')} Re-logging in to \"{panel.name}\"…")
                                ok = await login_to_panel(panel)
                                if ok:
                                    await update_panel_login(
                                        panel.id or 0, panel.sesskey, panel.api_url, True)
                                    logger.info(f"{ui('check')} \"{panel.name}\" logged in, fetching SMS")
                                else:
                                    logger.warning(f"⏸  \"{panel.name}\" login failed, skipping cycle")
                                    return None, panel
                            elif panel.panel_type == "api":
                                ok = await test_api_panel(panel)
                                panel.is_logged_in = ok
                                if not ok:
                                    logger.warning(f"⏸  API \"{panel.name}\" unreachable, skipping cycle")
                                    return None, panel
                        # ── Fetch SMS ─────────────────────────────────────────
                        sms_list = await fetch_panel_sms(panel)
                        if sms_list is not None:
                            logger.info(
                                f"📥 \"{panel.name}\" → {len(sms_list)} record(s) fetched")
                        return sms_list, panel
                    except Exception as e:
                        logger.error(f"{ui('cancel')} Watcher error on \"{panel.name}\": {e}", exc_info=True)
                    return None, panel

                # Run panels sequentially to avoid same-host session collisions.
                # asyncio.gather ran them all at once; for panels sharing a host
                # this caused the second login to invalidate the first mid-fetch.
                results = []
                for p in PANELS:
                    if p.panel_type not in ("ivas",):
                        results.append(await fetch_one(p))
                for sms_list,panel in results:
                    if not sms_list: continue
                    for rec in sms_list:
                        if len(rec)<4: continue
                        if panel.panel_type=="api":
                            dt_str=str(rec[0]); num_raw=str(rec[1]).replace("+","").strip()
                            svc_raw=str(rec[2]); msg_body=str(rec[3])
                        else:
                            dt_str=str(rec[0])
                            num_raw=str(rec[2]).replace("+","").strip() if len(rec)>2 else ""
                            svc_raw=str(rec[3]) if len(rec)>3 else "unknown"
                            msg_body=get_message_body(rec) or (str(rec[4]) if len(rec)>4 else "")
                        if not num_raw: continue
                        msg_body = msg_body.strip() if msg_body else ""
                        msg_time=parse_panel_dt(dt_str)
                        if msg_time is None: continue
                        if (datetime.now()-msg_time).total_seconds()/60>MSG_AGE_LIMIT_MIN: continue
                        otp_code=extract_otp_regex(msg_body)
                        uid_str=hashlib.md5(f"{panel.base_url}-{dt_str}-{num_raw}-{msg_body}".encode()).hexdigest()
                        if uid_str in PROCESSED_MESSAGES: continue
                        PROCESSED_MESSAGES.add(uid_str); save_seen_hash(uid_str)
                        if first_cycle: continue
                        if num_raw in targets:
                            db_obj=targets[num_raw]
                            if db_obj.last_msg==msg_body: continue
                            await do_sms_hit(application,db_obj,otp_code,msg_body,svc_raw,panel.name,num_raw,session)
                        else:
                            await log_unassigned(application,num_raw,msg_body,otp_code,svc_raw,panel.name)
                first_cycle=False
        except Exception as e:
            logger.error(f"Watcher loop: {e}"); await asyncio.sleep(5)
        if len(PROCESSED_MESSAGES) > 5000:
            PROCESSED_MESSAGES.clear()
        await asyncio.sleep(API_FETCH_INTERVAL)

# ═══════════════════════════════════════════════════════════
#  PANEL FINALIZATION
# ═══════════════════════════════════════════════════════════
async def _finalize_panel_edit(uid: int, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Finalize panel edit by saving to DB and refreshing"""
    try:
        st = PANEL_EDIT_STATES[uid]
        d = st["data"]
        pid = st["panel_id"]
        panel_type = d.get("panel_type", "")
        
        # Update based on panel type
        await update_panel_in_db(
            pid,
            d.get("name"),
            d.get("base_url"),
            d.get("username"),
            d.get("password"),
            panel_type,
            d.get("token"),
            d.get("uri")
        )
        
        # Refresh and cleanup
        await refresh_panels_from_db()
        del PANEL_EDIT_STATES[uid]
        
        await update.message.reply_text(f"{ui('check')} <b>Panel updated!</b>", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Panel finalize error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)[:50]}", parse_mode="HTML")

# ═══════════════════════════════════════════════════════════
#  COMMAND HANDLERS
# ═══════════════════════════════════════════════════════════
async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /skip command — works inside any multi-step flow."""
    uid = update.effective_user.id
    # Panel edit flow
    if uid in PANEL_EDIT_STATES:
        st = PANEL_EDIT_STATES[uid]; d = st["data"]; step = st["step"]
        # advance without changing the value
        if step == "url":
            if d.get("panel_type") == "login":
                st["step"] = "username"
                await update.message.reply_text(f"{ui('user')} Username (current: <code>%s</code>) or /skip:" % d.get("username",""), parse_mode="HTML")
            elif d.get("panel_type") == "api":
                st["step"] = "token"
                await update.message.reply_text(f"{ui('key')} Token or /skip:")
            else:
                st["step"] = "uri"
                await update.message.reply_text(f"{ui('link')} URI or /skip:")
        elif step == "username":
            st["step"] = "password"
            await update.message.reply_text("🔒 Password or /skip:")
        elif step in ("password", "token", "uri"):
            # finalize
            await _finalize_panel_edit(uid, update, context)
        return
    # Panel add flow
    if uid in PANEL_ADD_STATES:
        await update.message.reply_text("⏩ Skipped field.")
        return
    # Bot creation flow
    if uid in BOT_ADD_STATES:
        st = BOT_ADD_STATES[uid]; step = st.get("step","")
        BOT_ADD_STATES[uid]["data"] = BOT_ADD_STATES[uid].get("data", {})
        BOT_ADD_STATES[uid]["data"][step] = ""
        await update.message.reply_text("⏩ Skipped.")
        return
    await update.message.reply_text(f"{ui('info')} Nothing to skip right now.")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cancel command — cancels any active flow."""
    uid = update.effective_user.id
    cancelled = False
    if uid in PANEL_EDIT_STATES:
        del PANEL_EDIT_STATES[uid]; cancelled = True
    if uid in PANEL_ADD_STATES:
        del PANEL_ADD_STATES[uid]; cancelled = True
    if uid in BOT_ADD_STATES:
        del BOT_ADD_STATES[uid]; cancelled = True
    if uid in CREATE_BOT_STATES:
        del CREATE_BOT_STATES[uid]; cancelled = True
    AWAITING_ADMIN_ID.pop(uid, None)
    AWAITING_LOG_ID.pop(uid, None)
    AWAITING_SUPER_ADMIN.pop(uid, None)
    AWAITING_REQ_CHAT.pop(uid, None)
    context.user_data.pop("awaiting_prefix", None)
    context.user_data.pop("awaiting_broadcast", None)
    context.user_data.pop("upload_path", None)
    if cancelled:
        await update.message.reply_text(
            "❌ <b>Cancelled.</b>",
            reply_markup=main_menu_kb(), parse_mode="HTML")
    else:
        await update.message.reply_text(
            f"{ui('info')} Nothing to cancel.",
            reply_markup=main_menu_kb(), parse_mode="HTML")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start  — fully crash-proof with HTML→plain fallback.
    Uses animated emoji in message text (tg-emoji tags are fine there).
    """
    if not update.message:
        return
    uid      = update.effective_user.id
    logger.info(f"{ui('rocket')} /start received | UID: {uid}")
    raw_name = (update.effective_user.first_name or "User")[:60]
    name     = html.escape(raw_name)
    await db.add_user(uid)

    # ── Membership gate ─────────────────────────────────────────────
    try:
        missing = await check_membership(context.bot, uid)
    except Exception as e:
        logger.error(f"check_membership in /start: {e}")
        missing = []
    if missing:
        await send_join_required(update, context.bot, uid, missing)
        return

    bot_name  = f"@{BOT_USERNAME}" if BOT_USERNAME else "@CrackSMSReBot"
    bot_label = "CRACK SMS" if not IS_CHILD_BOT else html.escape(bot_name.lstrip("@"))
    perms     = await get_admin_permissions(uid)
    sup       = is_super_admin(uid)
    role_line = (f"\n{ui('crown')} <b>Super Admin</b>" if sup
                 else (f"\n{ui('shield')} <b>Admin</b>" if perms else ""))
    lim_badge = f"{ui('copy')} <b>Bot Assign Limit:</b> {get_assign_limit_label()}"

    if IS_CHILD_BOT:
        parts = [
            f"{'━'*24}\n{ui('robot')} <b>{bot_label}</b>\n{'━'*24}\n\n",
            f"👋 Welcome, <a href='tg://user?id={uid}'><b>{name}</b></a>{role_line}\n\n",
            f"{lim_badge}\n",
        ]
        if CHANNEL_LINK:
            parts.append(f"  {ui('megaphone')} <a href='{CHANNEL_LINK}'>Channel</a>  •  Updates\n")
        if OTP_GROUP_LINK:
            parts.append(f"  {ui('chat')} <a href='{OTP_GROUP_LINK}'>Community</a>  •  Support\n")
        if DEVELOPER and DEVELOPER not in ("@",""):
            dev_u = DEVELOPER.lstrip("@")
            parts.append(f"  {ui('rocket')} <a href='https://t.me/{dev_u}'>Developer</a>\n")
        if SUPPORT_USER and SUPPORT_USER not in ("@",""):
            sup_u = SUPPORT_USER.lstrip("@")
            parts.append(f"  {ui('help')} <a href='https://t.me/{sup_u}'>Support</a>\n")
        parts.append(
            f"\n{ui('bolt')} Real-time OTP  |  200+ Countries  |  Auto-assign\n"
            f"{ui('next')} <b>Get Started</b>"
        )
        msg = "".join(parts)
    else:
        msg = (
            f"{ui('diamond')} <b>{bot_label}</b>  {bot_name}{role_line}\n\n"
            f"{lim_badge}\n\n"
            f"{ui('bolt')} Real-time OTP  |  200+ Countries  |  Auto-assign\n"
            f"{ui('next')} <b>Get Started</b>"
        )

    kb = main_menu_kb()
    try:
        await update.message.reply_text(msg, reply_markup=kb, parse_mode="HTML",
                                         disable_web_page_preview=True)
    except TelegramBadRequest as e:
        logger.error(f"cmd_start HTML bad request: {e}")
        try:
            plain = re.sub(r'<[^>]+>', '', msg)
            await update.message.reply_text(plain, reply_markup=kb)
        except Exception as e2:
            logger.error(f"cmd_start plain fallback failed: {e2}")
    except Exception as e:
        logger.error(f"cmd_start unexpected: {e}")
        try:
            await update.message.reply_text(
                f"{ui('user')} Welcome {name}!\n\n{ui('next')} Get Started",
                reply_markup=kb,
            )
        except Exception:
            pass


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    perms= await get_admin_permissions(uid)
    sup  = is_super_admin(uid)
    if not perms and not sup:
        await update.message.reply_text(
            f"{emoji('trash', uid)} <b>Access Denied</b>\n"
            f"<i>Admin privileges required</i>",
            parse_mode="HTML"); return
    
    # ✅ Child bots cannot access bot management features
    if IS_CHILD_BOT and sup:
        await update.message.reply_text(
            f"⚠️ <b>Not Available in Child Bot</b>\n\n"
            f"This feature is only available in the main bot.\n"
            f"Child bots have their own dedicated admin panel.",
            parse_mode="HTML"); return
    role      = f"{emoji('crown', uid)} Super Admin" if sup else f"{emoji('user', uid)} Admin"
    stats     = await db.get_stats()
    panel_cnt = len(PANELS)
    run_cnt   = len([p for p in PANELS if p.is_logged_in or
                     (p.panel_type=="ivas" and p.name in IVAS_TASKS
                      and not IVAS_TASKS[p.name].done())])
    # Compact admin panel stats
    lines = [
        f"<b>{emoji('shield', uid)} ADMIN PANEL</b>",
        f"{emoji('user', uid)} {role} • ID: <code>{uid}</code>",
        "",
        f"{emoji('phone', uid)} Numbers: <b>{stats.get('available',0)}</b>",
        f"{emoji('satellite', uid)} Panels: <b>{run_cnt}/{panel_cnt}</b> online",
    ]
    if not IS_CHILD_BOT:
        bot_cnt = len(bm.list_bots())
        lines.append(f"{emoji('robot', uid)} Bots: <b>{bot_cnt}</b>")
    try:
        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=admin_main_kb(perms, sup), parse_mode="HTML")
    except TelegramBadRequest as e:
        logger.error(f"cmd_admin HTML bad request: {e}")
        plain = re.sub(r"<[^>]+>", "", "\n".join(lines))
        await update.message.reply_text(plain, reply_markup=admin_main_kb(perms, sup))

async def cmd_add_admin(u,c): await u.message.reply_text("Use Admin Panel → Admins.")
async def cmd_rm_admin(u,c):  await u.message.reply_text("Use Admin Panel → Admins.")
async def cmd_list_admins(u,c):
    admins=await list_all_admins()
    lines="\n".join(f"• <code>{a}</code>{'  👑' if a in INITIAL_ADMIN_IDS else ''}" for a in admins)
    await u.message.reply_text(f"👮 <b>Admins</b>\n\n{lines or 'None'}",parse_mode="HTML")

async def cmd_setuserlimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_super_admin(uid):
        await update.message.reply_text(f"{ui('cancel')} <b>Unauthorized</b>", parse_mode="HTML")
        return
    if len(context.args) != 2:
        await update.message.reply_text(
            f"{ui('info')} <b>Usage</b>\n<code>/setuserlimit user_id limit</code>",
            parse_mode="HTML",
        )
        return
    try:
        target_user = int(context.args[0])
        target_limit = max(1, int(context.args[1]))
    except ValueError:
        await update.message.reply_text(
            f"{ui('cancel')} <b>Invalid format</b>\nUse numeric values only.",
            parse_mode="HTML",
        )
        return
    await db.set_user_limit(target_user, target_limit)
    await update.message.reply_text(
        f"{ui('check')} <b>User limit updated</b>\n\n"
        f"{ui('user')} User: <code>{target_user}</code>\n"
        f"{ui('copy')} New assign limit: <b>{target_limit}</b>",
        parse_mode="HTML",
    )

async def cmd_add_log(update,context):
    uid=update.effective_user.id; perms=await get_admin_permissions(uid)
    if "manage_logs" not in perms and not is_super_admin(uid):
        await update.message.reply_text("❌ No permission."); return
    if not context.args: await update.message.reply_text("Usage: /addlogchat <chat_id>"); return
    try:
        cid=int(context.args[0]); ok=await db.add_log_chat(cid)
        await update.message.reply_text(f"{'✅ Added' if ok else '⚠️ Exists'}: <code>{cid}</code>",parse_mode="HTML")
    except ValueError: await update.message.reply_text("❌ Invalid chat ID.")

async def cmd_rm_log(update,context):
    uid=update.effective_user.id; perms=await get_admin_permissions(uid)
    if "manage_logs" not in perms and not is_super_admin(uid):
        await update.message.reply_text("❌ No permission."); return
    if not context.args: await update.message.reply_text("Usage: /removelogchat <chat_id>"); return
    try:
        cid=int(context.args[0]); ok=await db.remove_log_chat(cid)
        await update.message.reply_text(f"{'✅ Removed' if ok else '❌ Not found'}: <code>{cid}</code>",parse_mode="HTML")
    except ValueError: await update.message.reply_text("❌ Invalid chat ID.")

async def cmd_list_logs(update,context):
    uid=update.effective_user.id; perms=await get_admin_permissions(uid)
    if "manage_logs" not in perms and not is_super_admin(uid):
        await update.message.reply_text("❌ No permission."); return
    chats=await db.get_all_log_chats()
    txt=f"{ui('copy')} <b>Log Groups</b>\n\n"+"\n".join(f"• <code>{c}</code>" for c in chats) if chats else f"{ui('offline')} None."
    await update.message.reply_text(txt,parse_mode="HTML")

async def cmd_pending(update, context):
    """Show all pending bot requests (Super Admin only)"""
    uid = update.effective_user.id
    if not is_super_admin(uid):
        await update.message.reply_text("❌ Super admin only.")
        return

    sync_website_requests_into_bot_requests()
    
    if not BOT_REQUESTS:
        await update.message.reply_text(
            "✅ <b>No Pending Requests</b>\n\n"
            "All bot creation requests have been processed.",
            parse_mode="HTML")
        return
    
    lines = ["📋 <b>PENDING BOT REQUESTS</b>\n"]
    for req_id, req in BOT_REQUESTS.items():
        user_name = req.get("user_name", "?")
        uid_str = req.get("uid", "?")
        created = req.get("created_at", "?")
        is_panel = "🔌" if req.get("has_panel") else "📡"
        source = "Website" if req.get("source") == "website" else "Bot"
        
        lines.append(
            f"\n{is_panel} <b>{html.escape(user_name)}</b>\n"
            f"   ID: <code>{req_id}</code>\n"
            f"   UID: <code>{uid_str}</code>\n"
            f"   Source: <b>{html.escape(source)}</b>\n"
            f"   Time: <code>{created[-8:]}</code>"
        )
    
    lines.append(f"\n\n<i>Total: {len(BOT_REQUESTS)} pending</i>")
    
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML")

async def cmd_otpfor(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /otpfor <phone>")
        return
    target = context.args[0].replace("+", "").strip()
    found = next((otp for num, otp in load_otp_store().items() if target in num), None)
    if found:
        await update.message.reply_text(
            f"{ui('key')} <b>OTP Retrieved</b>\n\n"
            f"{ui('chart')} Target: <code>{target}</code>\n"
            f"{ui('lock')} <b>Code:</b> <code>{found}</code>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"{ui('xap')} Copy OTP: {found}", copy_text=CopyTextButton(text=found))
            ]]),
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(f"❌ No OTP for <code>{target}</code>.", parse_mode="HTML")

async def cmd_set_channel(update,context):
    uid=update.effective_user.id
    if not is_super_admin(uid): await update.message.reply_text("❌ Unauthorized."); return
    if not context.args: await update.message.reply_text("Usage: /set_channel <url>"); return
    global CHANNEL_LINK; CHANNEL_LINK=context.args[0]
    save_config_key("CHANNEL_LINK",CHANNEL_LINK)
    await update.message.reply_text(f"✅ Channel → {CHANNEL_LINK}")

async def cmd_set_otpgroup(update,context):
    uid=update.effective_user.id
    if not is_super_admin(uid): await update.message.reply_text("❌ Unauthorized."); return
    if not context.args: await update.message.reply_text("Usage: /set_otpgroup <url>"); return
    global OTP_GROUP_LINK; OTP_GROUP_LINK=context.args[0]
    save_config_key("OTP_GROUP_LINK",OTP_GROUP_LINK)
    await update.message.reply_text(f"✅ OTP Group → {OTP_GROUP_LINK}")

async def cmd_set_numbot(update,context):
    uid=update.effective_user.id
    if not is_super_admin(uid): await update.message.reply_text("❌ Unauthorized."); return
    if not context.args: await update.message.reply_text("Usage: /set_numberbot <url>"); return
    global NUMBER_BOT_LINK; NUMBER_BOT_LINK=context.args[0]
    save_config_key("NUMBER_BOT_LINK",NUMBER_BOT_LINK)
    await update.message.reply_text(f"✅ Number Bot → {NUMBER_BOT_LINK}")

async def cmd_groups(u,c): await cmd_list_logs(u,c)
async def cmd_addgrp(u,c): await cmd_add_log(u,c)
async def cmd_rmgrp(u,c):  await cmd_rm_log(u,c)

async def cmd_bots(update,context):
    uid=update.effective_user.id
    if not is_super_admin(uid): await update.message.reply_text("❌ Super admin only."); return
    if IS_CHILD_BOT: await update.message.reply_text("ℹ️ Not available on child bots."); return
    bots=bm.list_bots()
    if not bots: await update.message.reply_text(f"{ui('robot')} No bots registered yet."); return
    lines=[f"{'🟢' if b.get('running') else '🔴'} <b>{html.escape(b['name'])}</b>  <code>{b['id']}</code>" for b in bots]
    await update.message.reply_text(f"🖥 <b>Child Bots ({len(bots)})</b>\n\n"+"\n".join(lines),
                                    reply_markup=bots_list_kb(bots),parse_mode="HTML")

async def cmd_startbot(u,c):
    uid=u.effective_user.id
    if not is_super_admin(uid): await u.message.reply_text("❌ Unauthorized."); return
    if not c.args: await u.message.reply_text("Usage: /startbot <id>"); return
    ok,msg=bm.start_bot(c.args[0]); await u.message.reply_text(f"{ui('check') if ok else ui('cancel')} {msg}")

async def cmd_stopbot(u,c):
    uid=u.effective_user.id
    if not is_super_admin(uid): await u.message.reply_text("❌ Unauthorized."); return
    if not c.args: await u.message.reply_text("Usage: /stopbot <id>"); return
    ok,msg=bm.stop_bot(c.args[0]); await u.message.reply_text(f"{ui('check') if ok else ui('cancel')} {msg}")

async def cmd_dox(update, context):
    """Demo/test command - shows available test services."""
    uid = update.effective_user.id
    lines = [
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>",
        "🧪 <b>Test Services</b>",
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>",
        "",
        "Usage: /test1",
        "",
        "This will assign a test number and demo OTP forwarding.",
        "Perfect for testing the bot and WhatsApp bridge integration.",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_test1(update,context):
    uid=update.effective_user.id
    try:
        num=random.choice(TEST_NUMBERS); cat="🇺🇸 USA - TestService"
        await db.release_number(uid)
        async with db.AsyncSessionLocal() as session:
            obj=(await session.execute(select(db.Number).where(db.Number.phone_number==num))).scalar_one_or_none()
            if not obj: obj=db.Number(phone_number=num,category=cat,status="AVAILABLE"); session.add(obj)
            obj.status="ASSIGNED"; obj.assigned_to=uid; obj.assigned_at=datetime.now()
            obj.category=cat; obj.last_msg=None; obj.last_otp=None
            await session.commit()
        pfx=await db.get_user_prefix(uid)
        msg=await context.bot.send_message(chat_id=uid,
            text=f"{ui('wow')} <b>Test Number</b>\n{D}\n{ui('phone')} <code>+{num}</code>\n\nUse /send1 to simulate OTP.",
            reply_markup=waiting_kb(pfx),parse_mode="HTML")
        await db.update_message_id(num,msg.message_id)
    except Exception as e: await update.message.reply_text(f"❌ /test1: {e}")

async def cmd_send1(update,context):
    uid=update.effective_user.id
    async with db.AsyncSessionLocal() as session:
        obj=(await session.execute(
            select(db.Number).where(db.Number.assigned_to==uid,db.Number.status=="ASSIGNED")
        )).scalars().first()
        if not obj: await update.message.reply_text("❌ No active number. Use /test1."); return
        otp=str(random.randint(100000,999999))
        await do_sms_hit(context.application,obj,otp,f"Telegram code: {otp}. Do not share.",
                         "TELEGRAM","TEST-PANEL",obj.phone_number,session)
        await update.message.reply_text(f"✅ Simulated OTP <b>{otp}</b>",parse_mode="HTML")

# ═══════════════════════════════════════════════════════════
#  NEW USER COMMANDS — Enhanced Features
# ═══════════════════════════════════════════════════════════

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comprehensive help menu with all available commands."""
    uid = update.effective_user.id
    perms = await get_admin_permissions(uid)
    is_sup = is_super_admin(uid)
    
    lines = [
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
        "❓ <b>CRACK SMS — Help & Commands</b>",
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
        "",
        "<b>👤 User Commands:</b>",
        "/start — Main menu",
        "/myprofile — View your profile & stats",
        "/getnum — Request a phone number",
        "/mystats — Your personal statistics",
        "/myhistory — View OTP history",
        "/info — System information",
        "/help — This message",
        "",
    ]
    
    if perms or is_sup:
        lines.extend([
            "<b>👮 Admin Commands:</b>",
            "/admin — Admin control panel",
            "/stats — View overall statistics",
            "/panels — Manage SMS panels",
            "/users — Manage users",
            "/broadcast — Send announcements",
            "/addlog — Add log chat",
            "/perfstats — Performance metrics",
            "/clearcache — Clear system cache",
        ])
    
    if is_sup:
        lines.extend([
            "",
            "<b>👑 Super Admin Commands:</b>",
            "/startbot &lt;id&gt; — Start child bot",
            "/stopbot &lt;id&gt; — Stop child bot",
            "/addadmin &lt;id&gt; — Add admin",
            "/rmadmin &lt;id&gt; — Remove admin",
            "/createbot — Create new bot",
            "/systemhealth — Full system health report",
        ])
    
    lines.append("<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

@safe_handler_decorator
async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display system and user information."""
    uid = update.effective_user.id
    user = update.effective_user
    
    try:
        # get_user() returns an ORM object, not a dict — use get_user_tier() instead
        _tier_key = get_user_tier(uid)
        user_tier = PREMIUM_TIERS.get(_tier_key, {}).get("name", "Free")
    except:
        user_tier = "Free"
    
    msg = (
        "ℹ️ <b>System Information</b>\n"
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
        f"👤 <b>Your ID:</b> <code>{uid}</code>\n"
        f"📝 <b>Username:</b> @{user.username or 'None'}\n"
        f"🎯 <b>Tier:</b> {user_tier}\n"
        f"🕐 <b>Server Time:</b> <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>\n\n"
        f"🤖 <b>Bot:</b> @{BOT_USERNAME}\n"
        f"{ui('copy')} <b>Database:</b> Connected {ui('check')}\n"
    )

    await update.message.reply_text(msg, parse_mode="HTML")

@safe_handler_decorator
async def cmd_perfstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: View performance statistics."""
    uid = update.effective_user.id
    if not is_super_admin(uid):
        await update.message.reply_text("👮 Super admins only", parse_mode="HTML")
        return
    
    stats = get_performance_stats()
    lines = [
        "<b>⚡ Performance Statistics</b>",
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>",
        "",
    ]
    
    if stats:
        for cmd, data in sorted(stats.items(), key=lambda x: x[1]["avg"], reverse=True)[:10]:
            lines.append(
                f"<code>{cmd:20}</code> "
                f"Avg: <code>{data['avg']:6.1f}ms</code> "
                f"Count: <code>{data['count']:4}</code>"
            )
    else:
        lines.append("<i>No performance data yet</i>")
    
    lines.extend([
        "",
        f"<b>Cache Entries:</b> <code>{len(SIMPLE_CACHE)}</code>",
        f"<b>Active Rate Limits:</b> <code>{len(USER_RATE_LIMITS)}</code>",
    ])
    
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

@safe_handler_decorator
async def cmd_clearcache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: Clear system cache."""
    uid = update.effective_user.id
    if not is_super_admin(uid):
        await update.message.reply_text("👮 Super admins only", parse_mode="HTML")
        return
    
    before = len(SIMPLE_CACHE)
    cache_clear()
    after = len(SIMPLE_CACHE)
    
    msg = (
        f"🧹 <b>Cache Cleared</b>\n"
        f"Before: <code>{before}</code> entries\n"
        f"After: <code>{after}</code> entries\n"
        f"Freed: <code>{before - after}</code> entries"
    )
    
    await update.message.reply_text(msg, parse_mode="HTML")
    logger.info(f"🧹 Cache cleared by {uid}: {before - after} entries")

@safe_handler_decorator
async def cmd_systemhealth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Super admin: Full system health report."""
    uid = update.effective_user.id
    if not is_super_admin(uid):
        await update.message.reply_text("👑 Super admins only", parse_mode="HTML")
        return
    
    analytics = await get_bot_analytics()
    health = await get_system_health()
    
    msg = (
        "🏥 <b>System Health Report</b>\n"
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
        
        "<b>📊 Bot Statistics:</b>\n"
        f"  👥 Users: <code>{analytics.get('active_users', 0)}</code>\n"
        f"  📱 Numbers: <code>{analytics.get('total_numbers', 0)}</code>\n"
        f"  ✅ Available: <code>{analytics.get('available_numbers', 0)}</code>\n"
        f"  🔗 Assigned: <code>{analytics.get('assigned_numbers', 0)}</code>\n"
        f"  🔑 OTPs: <code>{analytics.get('total_otps_processed', 0)}</code>\n\n"
        
        "<b>🔌 Panels:</b>\n"
        f"  🟢 Active: <code>{analytics.get('active_panels', 0)}/{analytics.get('total_panels', 0)}</code>\n\n"
        
        "<b>🖥️  System Resources:</b>\n"
        f"  💻 CPU: <code>{health.get('cpu_percent', 'N/A')}%</code>\n"
        f"  🧠 Memory: <code>{health.get('memory_mb', 0):.1f}MB</code> "
        f"(<code>{health.get('memory_percent', 0):.1f}%</code>)\n"
        f"  🔧 Threads: <code>{health.get('num_threads', 0)}</code>\n"
        f"  📁 Open Files: <code>{health.get('open_files', 0)}</code>\n\n"
        
        "<b>💾 Cache Status:</b>\n"
        f"  Entries: <code>{len(SIMPLE_CACHE)}</code>\n"
        f"  Rate Limits: <code>{len(USER_RATE_LIMITS)}</code>\n\n"
        
        "<b>🎨 Configuration:</b>\n"
        f"  OTP Theme: <code>#{OTP_GUI_THEME % 15}</code>\n"
        f"  Child Bot: <code>{'Yes' if IS_CHILD_BOT else 'No'}</code>\n"
        f"  Time: <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>"
    )
    
    await update.message.reply_text(msg, parse_mode="HTML")

async def cmd_otp_gui_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display all 15 OTP GUI themes so users can choose their favorite."""
    uid = update.effective_user.id
    
    fake_otp = "247381"
    sample_msg = "Your Telegram code: 247381. Do not share."
    now_ts = datetime.now().strftime("%H:%M:%S")
    
    preview_text = (
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        "🎨 <b>OTP GUI THEMES — All 10 Options</b>\n"
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
        
        "────────────────────────────────────────────────\n"
        "<b>THEME 0: CLASSIC ⭐</b> — Professional Standard\n"
        "────────────────────────────────────────────────\n"
        "📋 Copy OTP: 247381\n"
        "📩 Copy Message\n"
        "🤖 Get Numbers  |  💬 Community\n"
        "<i>Best for: Standard users</i>\n\n"
        
        "────────────────────────────────────────────────\n"
        "<b>THEME 1: MINIMAL 🎯</b> — Essential Only\n"
        "────────────────────────────────────────────────\n"
        "✂️ Copy: 247381\n"
        "⚙️ Settings\n"
        "<i>Best for: Users who want minimal interface</i>\n\n"
        
        "────────────────────────────────────────────────\n"
        "<b>THEME 2: DEVELOPER 👨‍💻</b> — Dev-Focused\n"
        "────────────────────────────────────────────────\n"
        "💻 247381\n"
        "📋 Copy  |  👨‍💻 Developer\n"
        "🆘 Support\n"
        "<i>Best for: Developers & tech users</i>\n\n"
        
        "────────────────────────────────────────────────\n"
        "<b>THEME 3: ELECTRIC ⚡</b> — Tech-Savvy Users\n"
        "────────────────────────────────────────────────\n"
        "⚡ 247381\n"
        "🔌 Panels  |  ⚙️ Options\n"
        "<i>Best for: Tech-savvy users</i>\n\n"
        
        "────────────────────────────────────────────────\n"
        "<b>THEME 4: TECH 🔬</b> — Advanced Users\n"
        "────────────────────────────────────────────────\n"
        "🔬 Code: 247381\n"
        "📄 Full Message\n"
        "🤖 Bot  |  👨‍💻 Creator\n"
        "<i>Best for: Advanced users</i>\n\n"
        
        "────────────────────────────────────────────────\n"
        "<b>THEME 5: PREMIUM 💎</b> — Premium Features\n"
        "────────────────────────────────────────────────\n"
        "💎 247381\n"
        "✨ Premium Message\n"
        "📖 Help  |  ❓ FAQ\n"
        "<i>Best for: Premium members</i>\n\n"
        
        "────────────────────────────────────────────────\n"
        "<b>THEME 6: ULTRAMINIMAL 🎲</b> — Extremely Compact\n"
        "────────────────────────────────────────────────\n"
        "🔐 247381\n"
        "<i>Best for: Ultra-minimalist users</i>\n\n"
        
        "────────────────────────────────────────────────\n"
        "<b>THEME 7: BUSINESS 💼</b> — Professional Enterprise\n"
        "────────────────────────────────────────────────\n"
        "📊 247381\n"
        "⚙️ Settings  |  📞 Support\n"
        "<i>Best for: Business users</i>\n\n"
        
        "────────────────────────────────────────────────\n"
        "<b>THEME 8: SOCIAL 🌐</b> — Community-Focused\n"
        "────────────────────────────────────────────────\n"
        "👥 247381\n"
        "🌐 Community  |  👤 Creator\n"
        "<i>Best for: Social users</i>\n\n"
        
        "────────────────────────────────────────────────\n"
        "<b>THEME 9: DELUXE 🌟</b> — All Features\n"
        "────────────────────────────────────────────────\n"
        "🌟 247381\n"
        "📝 Full Text\n"
        "🤖 Numbers  |  💬 Community\n"
        "👨‍💻 Dev  |  🆘 Support\n"
        "📖 Help  |  ⚙️ Settings\n"
        "<i>Best for: Users who want everything</i>\n\n"
        
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        "📊 <b>To Change Your Theme:</b>\n"
        "Use: <code>OTP_GUI_THEME: 0-9</code> in config.json\n"
        "Then restart the bot\n"
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>"
    )
    
    await update.message.reply_text(preview_text, parse_mode="HTML")
    logger.info(f"📺 OTP GUI preview (10 themes) shown | UID: {uid} | Bot Type: {'CHILD' if IS_CHILD_BOT else 'MAIN'}")

async def cmd_mystats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's personal statistics and achievements."""
    uid = update.effective_user.id
    user = await db.get_user(uid)
    
    async with db.AsyncSessionLocal() as session:
        nums = (await session.execute(
            select(db.Number).where(db.Number.assigned_to == uid)
        )).scalars().all()
        
        active_nums = [n for n in nums if n.status == "ASSIGNED"]
        otps_count = sum(1 for n in nums if n.last_otp)
        
    achievement_badges = []
    if otps_count >= 1: achievement_badges.append(f"{ui('star')} First OTP")
    if otps_count >= 10: achievement_badges.append(f"{ui('zap')} OTP Collector")
    if otps_count >= 100: achievement_badges.append(f"{ui('fire')} OTP Master")
    if len(active_nums) >= 3: achievement_badges.append(f"{ui('phone')} Multi-User")
    
    creation_date = user.created_at.strftime("%Y-%m-%d") if hasattr(user, 'created_at') else "Unknown"
    
    assign_limit_status = f"{ui('copy')} {get_assign_limit_label()}"
    
    lines = [
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
        "📊 <b>Your Statistics</b>",
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
        "",
        f"👤 <b>User ID:</b> <code>{uid}</code>",
        f"📅 <b>Member Since:</b> {creation_date}",
        f"📦 <b>Bot Assign Limit:</b> {assign_limit_status}",
        f"📊 <b>Total Numbers:</b> {len(nums)}",
        f"✅ <b>Active Numbers:</b> {len(active_nums)}",
        f"📧 <b>OTPs Received:</b> {otps_count}",
        "",
        "<b>🏆 Achievements:</b>",
    ]
    
    if achievement_badges:
        lines.extend(achievement_badges)
    else:
        lines.append("Receive your first OTP to unlock achievements!")
    
    lines.append("<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_myhistory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's OTP receive history with pagination."""
    uid = update.effective_user.id
    page = int(context.args[0]) if context.args else 1
    
    async with db.AsyncSessionLocal() as session:
        nums = (await session.execute(
            select(db.Number).where(db.Number.assigned_to == uid)
        )).scalars().all()
    
    history = []
    for num in sorted(nums, key=lambda x: x.last_msg or 0, reverse=True):
        if num.last_otp:
            history.append({
                "phone": num.phone_number,
                "otp": num.last_otp[:6] if num.last_otp else "N/A",
                "service": num.category.split("-")[-1].strip() if num.category else "Unknown",
                "time": num.last_msg or "Never"
            })
    
    items, total_pages, has_prev, has_next = paginate_list(history, page, 5)
    
    lines = [
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
        f"📜 <b>OTP History</b> (Page {page}/{total_pages})",
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
    ]
    
    if items:
        for h in items:
            lines.append(f"\n📱 <code>{h['phone']}</code> • {h['service']}")
            lines.append(f"   🔑 {h['otp']} • {h['time']}")
    else:
        lines.append("\n📭 No OTP history yet. Request a number to start!")
    
    lines.append("\n<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>")
    
    kb_rows = []
    if has_prev:
        kb_rows.append([btn("⬅  Prev", cbd=f"cmd_history_{page-1}")])
    if has_next:
        kb_rows.append([btn("Next ➡", cbd=f"cmd_history_{page+1}")])
    kb_rows.append([btn("🔙  Back", cbd="main_menu")])
    
    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(kb_rows),
       parse_mode="HTML"
    )

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enhanced profile view with  advanced stats."""
    uid = update.effective_user.id
    user_obj = update.effective_user
    perms = await get_admin_permissions(uid)
    
    async with db.AsyncSessionLocal() as session:
        nums = (await session.execute(
            select(db.Number).where(db.Number.assigned_to == uid)
        )).scalars().all()
    
    active = len([n for n in nums if n.status == "ASSIGNED"])
    available = len([n for n in nums if n.status == "AVAILABLE"])
    blocked = len([n for n in nums if n.status == "BLOCKED"])
    
    role_badge = "👑 Super Admin" if is_super_admin(uid) else ("👮 Admin" if perms else "👤 User")
    
    lines = [
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
        "👤 <b>User Profile</b>",
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
        "",
        f"<b>Name:</b> {html.escape(user_obj.first_name)} {html.escape(user_obj.last_name or '')}".strip(),
        f"<b>ID:</b> <code>{uid}</code>",
        f"<b>Username:</b> @{html.escape(user_obj.username or 'N/A')}",
        f"<b>Role:</b> {role_badge}",
        "",
        "<b>📱 Phone Numbers:</b>",
        f"  ✅ Active: <b>{active}</b>",
        f"  🔄 Available: <b>{available}</b>",
        f"  🚫 Blocked: <b>{blocked}</b>",
        f"  📊 Total: <b>{len(nums)}</b>",
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
    ]
    
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

# ═══════════════════════════════════════════════════════════
#  PREMIUM & PROFESSIONAL FUNCTIONS
# ═══════════════════════════════════════════════════════════

async def cmd_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show premium tier info and subscription."""
    uid = update.effective_user.id
    tier = get_user_tier(uid)
    tier_info = PREMIUM_TIERS[tier]
    limit_check = check_otp_limit(uid)
    
    lines = [
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
        f"💎 {tier_info['emoji']} <b>{tier_info['name']} Plan</b>",
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
        "",
        f"📊 Daily Limit: <b>{limit_check['sent']}/{limit_check['limit']}</b> OTPs",
        f"📈 Remaining: <b>{limit_check['remaining']}</b>",
        f"🔌 Max Panels: <b>{tier_info['max_panels']}</b>",
        "",
        "<b>✨ Features:</b>",
        "\n".join(f"  ✅ {f}" for f in tier_info['features']),
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
    ]
    
    if tier == "free":
        lines.append(f"\n💰 Upgrade to Pro: <b>${PREMIUM_TIERS['pro']['price']}.99/mo</b>")
        lines.append(f"🏆 Upgrade to Enterprise: <b>${PREMIUM_TIERS['enterprise']['price']}.99/mo</b>")
    
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show analytics dashboard for Pro/Enterprise."""
    uid = update.effective_user.id
    tier = get_user_tier(uid)
    
    if tier == "free":
        await update.message.reply_text(
            "📊 <b>Advanced Analytics</b> requires <b>Pro tier</b> or higher.\n\n"
            f"💰 Upgrade now for only <b>${PREMIUM_TIERS['pro']['price']}.99/month</b>",
            parse_mode="HTML"
        )
        return
    
    analytics = PREMIUM_ANALYTICS.get(uid, {"otps_today": 0, "panels_used": 0, "panels_failed": 0})
    today = datetime.now().strftime("%Y-%m-%d")
    
    lines = [
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
        "📊 <b>Analytics Dashboard</b>",
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
        "",
        f"📅 Date: <b>{today}</b>",
        f"📤 OTPs Sent: <b>{analytics.get('otps_today', 0)}</b>",
        f"🏃 Panels Active: <b>{analytics.get('panels_used', 0)}</b>",
        f"❌ Failed: <b>{analytics.get('panels_failed', 0)}</b>",
        f"✅ Success Rate: <b>{100 if not analytics.get('panels_failed') else 100 - ((analytics.get('panels_failed', 0) / max(1, analytics.get('panels_used', 1))) * 100):.1f}%</b>",
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
    ]
    
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_webhook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manage webhooks for Premium users."""
    uid = update.effective_user.id
    tier = get_user_tier(uid)
    
    if tier == "free":
        await update.message.reply_text(
            "🔗 <b>Webhooks</b> require <b>Pro tier</b>.\n\n"
            "Setup HTTP callbacks for OTP events (received, forwarded, failed).",
            parse_mode="HTML"
        )
        return
    
    if not context.args:
        webhooks = WEBHOOK_STORE.get(uid, [])
        lines = [
            "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>",
            "🔗 <b>Webhooks</b>",
            "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>",
            "",
            f"Total: <b>{len(webhooks)}</b>",
        ]
        if webhooks:
            for w in webhooks[:5]:
                lines.append(f"\n• <code>{w['id']}</code> - {w['url'][:40]}...")
        else:
            lines.append("\nNo webhooks registered.")
        
        lines.append("\n<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>")
        lines.append("\n/webhook add <url>")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return
    
    action = context.args[0].lower()
    if action == "add" and len(context.args) > 1:
        webhook_url = context.args[1]
        events = ["otp_received", "otp_forwarded"]
        result = register_webhook(uid, webhook_url, events)
        if result["ok"]:
            await update.message.reply_text(f"{ui('check')} Webhook registered: <code>{result['webhook_id']}</code>", parse_mode="HTML")
        else:
            await update.message.reply_text(f"❌ {result['error']}", parse_mode="HTML")

async def cmd_schedule_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Schedule WhatsApp message for Pro/Enterprise."""
    uid = update.effective_user.id
    tier = get_user_tier(uid)
    
    if tier == "free":
        await update.message.reply_text(
            "⏰ <b>Message Scheduling</b> requires <b>Pro tier</b>.",
            parse_mode="HTML"
        )
        return
    
    if len(context.args) < 3:
        await update.message.reply_text("/schedule <delay_sec> <target> <message>")
        return
    
    delay = int(context.args[0])
    target = context.args[1]
    message = " ".join(context.args[2:])
    
    await update.message.reply_text(
        f"⚠️ Message scheduling temporarily unavailable.",
        parse_mode="HTML"
    )

# ═══════════════════════════════════════════════════════════
#  ADVANCED ADMIN COMMANDS — Enhanced Dashboard & Analytics
# ═══════════════════════════════════════════════════════════

async def cmd_user_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search users by ID, name, or phone number."""
    uid = update.effective_user.id
    perms = await get_admin_permissions(uid)
    
    if "manage_files" not in perms and not is_super_admin(uid):
        await update.message.reply_text("❌ No permission."); return
    
    if not context.args:
        await update.message.reply_text("Usage: /usersearch <user_id|name|phone>"); return
    
    query = " ".join(context.args).lower()
    
    async with db.AsyncSessionLocal() as session:
        users = (await session.execute(select(db.User))).scalars().all()
        results = []
        for u in users:
            if (str(u.user_id) == query or 
                (hasattr(u, 'username') and u.username and query in u.username.lower()) or
                (hasattr(u, 'name') and u.name and query in u.name.lower())):
                results.append(u)
    
    lines = [
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>",
        f"🔍 <b>Search Results</b> ({len(results)})",
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>",
    ]
    
    if results:
        for u in results[:10]:
            lines.append(f"\n👤 ID: <code>{u.user_id}</code>")
            if hasattr(u, 'username') and u.username:
                lines.append(f"   Username: @{u.username}")
            lines.append(f"   Status: Active")
    else:
        lines.append("\n❌ No users found.")
    
    lines.append("\n<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_top_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show top active users by OTP count."""
    uid = update.effective_user.id
    perms = await get_admin_permissions(uid)
    
    if "view_stats" not in perms and not is_super_admin(uid):
        await update.message.reply_text("❌ No permission."); return
    
    async with db.AsyncSessionLocal() as session:
        nums = (await session.execute(select(db.Number))).scalars().all()
    
    user_otps = {}
    for n in nums:
        if n.assigned_to and n.last_otp:
            user_otps[n.assigned_to] = user_otps.get(n.assigned_to, 0) + 1
    
    sorted_users = sorted(user_otps.items(), key=lambda x: x[1], reverse=True)[:10]
    
    lines = [
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>",
        "🏆 <b>Top Active Users</b>",
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>",
    ]
    
    for idx, (user_id, count) in enumerate(sorted_users, 1):
        medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"{idx}."
        lines.append(f"{medal} <code>{user_id}</code> — {count} OTPs 📧")
    
    lines.append("<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_panel_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check health of all active SMS panels with detailed stats."""
    uid = update.effective_user.id
    perms = await get_admin_permissions(uid)
    
    if "manage_panels" not in perms and not is_super_admin(uid):
        await update.message.reply_text("❌ No permission."); return
    
    lines = [
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
        "🔌 <b>Panel Health Check</b>",
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
    ]
    
    total_panels = len(PANELS)
    online = len([p for p in PANELS if p.is_logged_in or (p.panel_type == "ivas" and p.name in IVAS_TASKS and not IVAS_TASKS[p.name].done())])
    offline = total_panels - online
    
    lines.extend([
        "",
        f"📊 Total Panels: <b>{total_panels}</b>",
        f"✅ Online: <b>{online}</b> ({100*online//max(1,total_panels)}%)",
        f"❌ Offline: <b>{offline}</b>",
        "",
        "<b>📋 Detailed Status:</b>",
    ])
    
    for p in PANELS[:15]:
        status_icon = "🟢" if (p.is_logged_in or (p.panel_type == "ivas" and p.name in IVAS_TASKS and not IVAS_TASKS[p.name].done())) else "🔴"
        lines.append(f"{status_icon} <b>{p.name}</b> ({p.panel_type})")
    
    if total_panels > 15:
        lines.append(f"\n... and {total_panels - 15} more panels")
    
    lines.append("\n<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

async def cmd_system_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed system information for debugging."""
    uid = update.effective_user.id
    
    if not is_super_admin(uid):
        await update.message.reply_text("❌ Super admin only."); return
    
    stats = await db.get_stats()
    
    lines = [
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
        "⚙️ <b>System Information</b>",
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
        "",
        "<b>📊 Database Stats:</b>",
        f"Users: <b>{stats.get('user_count', 0)}</b>",
        f"Numbers: <b>{stats.get('total', 0)}</b>",
        f"  ✅ Available: <b>{stats.get('available', 0)}</b>",
        f"  🟠 Assigned: <b>{stats.get('assigned', 0)}</b>",
        f"  🚫 Blocked: <b>{stats.get('blocked', 0)}</b>",
        "",
        "<b>🔌 Panel Information:</b>",
        f"Total Panels: <b>{len(PANELS)}</b>",
        "",
        "<b>🤖 Bot Configuration:</b>",
        f"Child Bot: <b>{'Yes' if IS_CHILD_BOT else 'No'}</b>",
        f"OTP Theme: <b>{OTP_GUI_THEME}</b>",
        f"Timezone: <b>{datetime.now().strftime('%Z')}</b>",
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
    ]
    
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

# ═══════════════════════════════════════════════════════════
#  DOCUMENT UPLOAD
# ═══════════════════════════════════════════════════════════
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id; perms=await get_admin_permissions(uid)
    if "manage_files" not in perms and not is_super_admin(uid):
        await update.message.reply_text("❌ No permission."); return
    doc=update.message.document
    if not doc or not doc.file_name.endswith(".txt"):
        await update.message.reply_text("❌ Send a <code>.txt</code> file.",parse_mode="HTML"); return
    f=await doc.get_file(); path=doc.file_name
    await f.download_to_drive(path)
    try:
        lines=[l.strip() for l in open(path).readlines() if l.strip()]
        if not lines: await update.message.reply_text("❌ File empty."); os.remove(path); return
        country,flag=detect_country_from_numbers(lines)
        context.user_data.update({"upload_path":path,"upload_country":country,
                                   "upload_flag":flag,"upload_count":len(lines),"upload_svcs":[]})
        await update.message.reply_text(
            f"📂 <b>File Received</b>\n{D}\n🔢 <b>{len(lines)}</b> numbers\n"
            f"🌍 Detected: {flag} <b>{country}</b>\n\nSelect services:",
            reply_markup=svc_sel_kb(),parse_mode="HTML")
    except Exception as e: await update.message.reply_text(f"❌ Error: {e}")

# ═══════════════════════════════════════════════════════════
#  TEXT INPUT HANDLER
# ═══════════════════════════════════════════════════════════
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid=update.effective_user.id
    user_text=update.message.text
    if user_text: user_text = user_text.strip()
    else: user_text = ""
    # Membership gate
    if not is_super_admin(uid):
        perms = await get_admin_permissions(uid)
        if not perms:
            missing = await check_membership(context.bot, uid)
            if missing:
                await send_join_required(update, context.bot, uid, missing)
                return

    # ── Panel Edit Flow ──────────────────────────────────
    if uid in PANEL_EDIT_STATES:
        st=PANEL_EDIT_STATES[uid]; step=st["step"]; d=st["data"]; pid=st["panel_id"]
        if user_text=="/cancel": del PANEL_EDIT_STATES[uid]; await update.message.reply_text("❌ Cancelled."); return
        if step=="name": d["name"]=user_text; st["step"]="url"; await update.message.reply_text(f"URL now: {d['base_url']}\nNew URL (/skip):")
        elif step=="url":
            if user_text.lower()!="/skip": d["base_url"]=user_text
            if d["panel_type"]=="login": st["step"]="username"; await update.message.reply_text(f"User: {d['username']}\nNew (/skip):")
            elif d["panel_type"]=="api": st["step"]="token"; await update.message.reply_text("New token (/skip):")
            else: st["step"]="uri"; await update.message.reply_text("New URI (/skip):")
        elif step=="username":
            if user_text.lower()!="/skip": d["username"]=user_text
            st["step"]="password"; await update.message.reply_text("New password (/skip):")
        elif step=="password":
            if user_text.lower()!="/skip": d["password"]=user_text
            await update_panel_in_db(pid,d["name"],d["base_url"],d.get("username"),d.get("password"),d["panel_type"],d.get("token"),d.get("uri"))
            await refresh_panels_from_db(); del PANEL_EDIT_STATES[uid]; await update.message.reply_text("✅ Panel updated!")
        elif step=="token":
            if user_text.lower()!="/skip": d["token"]=user_text
            await update_panel_in_db(pid,d["name"],d["base_url"],None,None,d["panel_type"],d.get("token"),None)
            await refresh_panels_from_db(); del PANEL_EDIT_STATES[uid]; await update.message.reply_text("✅ API panel updated!")
        elif step=="uri":
            if user_text.lower()!="/skip": d["uri"]=user_text
            await update_panel_in_db(pid,d["name"],d["base_url"],None,None,d["panel_type"],None,d.get("uri"))
            await refresh_panels_from_db(); del PANEL_EDIT_STATES[uid]; await update.message.reply_text("✅ IVAS panel updated!")
        return

    # ── Panel Add Flow ───────────────────────────────────
    if uid in PANEL_ADD_STATES:
        st=PANEL_ADD_STATES[uid]; step=st["step"]; d=st["data"]
        if user_text=="/cancel": del PANEL_ADD_STATES[uid]; await update.message.reply_text("❌ Cancelled."); return
        if step=="name": d["name"]=user_text; st["step"]="type"; await update.message.reply_text("Select panel type:",reply_markup=ptype_kb())
        elif step=="url":
            d["base_url"]=user_text
            pt=d["panel_type"]
            if pt=="login": st["step"]="username"; await update.message.reply_text("Enter username:")
            elif pt=="api": st["step"]="token"; await update.message.reply_text("Enter API token:")
            else: st["step"]="uri"; await update.message.reply_text("Paste IVAS URI (wss://...):")
        elif step=="username": d["username"]=user_text; st["step"]="password"; await update.message.reply_text("Enter password:")
        elif step=="password":
            await add_panel_to_db(d["name"],d["base_url"],d["username"],user_text,"login")
            await refresh_panels_from_db(); del PANEL_ADD_STATES[uid]; await update.message.reply_text("✅ Login panel added!")
        elif step=="token":
            await add_panel_to_db(d["name"],d["base_url"],None,None,"api",token=user_text.strip())
            await refresh_panels_from_db(); del PANEL_ADD_STATES[uid]; await update.message.reply_text("✅ API panel added!")
        elif step=="uri":
            await add_panel_to_db(d["name"],d.get("base_url",""),None,None,"ivas",uri=user_text.strip())
            await refresh_panels_from_db()
            panel=next((p for p in PANELS if p.name==d["name"]),None)
            if panel:
                task=asyncio.create_task(ivas_worker(panel),name=f"IVAS-{d['name']}")
                task.add_done_callback(handle_task_exception); IVAS_TASKS[d["name"]]=task
            del PANEL_ADD_STATES[uid]; await update.message.reply_text("✅ IVAS panel added and worker started!")
        return

    # ── Add Admin ID ─────────────────────────────────────
    # ── Create Bot text flow ──────────────────────────────────────
    if uid in CREATE_BOT_STATES:
        state = CREATE_BOT_STATES[uid]
        step  = state.get("step","")

        if step == "get_group_id":
            # User sends their group chat ID
            group_id_str = user_text.strip().replace(" ", "")
            if not group_id_str.lstrip("-").isdigit():
                await update.message.reply_text(
                    "❌ Invalid group ID. It should be a number like <code>-1001234567890</code>\n"
                    "Send /cancel to abort.", parse_mode="HTML"); return
            state["group_id"] = group_id_str
            state["step"]     = "await_verify"
            await update.message.reply_text(
                f"📋 Group ID received: <code>{group_id_str}</code>\n\n"
                f"Now tap <b>Verify</b> to confirm I am admin in your group.\n"
                f"Make sure @{BOT_USERNAME} is an admin first!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "✅  Verify Admin Status",
                        callback_data=f"cbot_verify_{group_id_str}", style="success"),
                    InlineKeyboardButton("❌  Cancel", callback_data="cancel_action", style="danger"),
                ]]), parse_mode="HTML")
            return

        if step == "get_bot_name":
            state["bot_name"] = user_text.strip()
            state["step"]     = "get_token"
            await update.message.reply_text(
                "🤖 <b>Step 2/9 — Bot Token</b>\n\n"
                "Send your <b>Bot Token</b> from @BotFather\n"
                "<i>Format: 1234567890:AAXXXXXXXX</i>\n\n"
                "<b>⚠️ Required:</b> You MUST provide your own bot token from @BotFather.\n"
                "Each child bot needs its own unique token.",
                parse_mode="HTML"); return

        if step == "get_token":
            token_input = user_text.strip()
            # Validate token format and prevent empty/skip
            if not token_input or token_input.lower() == "skip":
                await update.message.reply_text(
                    "❌ <b>Invalid or Empty Token</b>\n\n"
                    "You MUST provide a real bot token from @BotFather.\n"
                    "Do NOT type 'skip' — each child bot needs its own unique token.\n\n"
                    "Format: <code>1234567890:AAXXXXXXXX</code>\n\n"
                    "Get one from: https://t.me/BotFather", parse_mode="HTML"); return
            if ":" not in token_input:
                await update.message.reply_text(
                    "❌ <b>Invalid Token Format</b>\n\n"
                    "Token must contain ':' character.\n"
                    "Format: <code>1234567890:AAXXXXXXXX</code>",
                    parse_mode="HTML"); return
            state["token"] = token_input
            state["step"] = "get_username"
            await update.message.reply_text(
                "🤖 <b>Step 3/9 — Bot Username</b>\n\n"
                "Send the bot <b>@username</b> (e.g. @MyOTPBot):", parse_mode="HTML"); return

        if step == "get_username":
            state["bot_username"] = user_text.strip().lstrip("@")
            state["step"]         = "get_admin_id"
            await update.message.reply_text(
                "🤖 <b>Step 4/9 — Admin User ID</b>\n\n"
                "Send your Telegram <b>numeric User ID</b>:", parse_mode="HTML"); return

        if step == "get_admin_id":
            try: state["admin_id"] = int(user_text.strip())
            except ValueError:
                await update.message.reply_text("❌ Must be a number. Try again."); return
            state["step"] = "get_channel"
            await update.message.reply_text(
                "🤖 <b>Step 5/9 — Channel Link</b>\n\n"
                "Send your <b>channel link</b> or type <b>none</b>:", parse_mode="HTML"); return

        if step == "get_channel":
            state["channel"] = None if user_text.lower()=="none" else user_text.strip()
            state["step"] = "get_otp_group"
            await update.message.reply_text(
                "🤖 <b>Step 6/9 — OTP Group Link</b>\n\nSend your OTP group link:", parse_mode="HTML"); return

        if step == "get_otp_group":
            state["otp_group"] = user_text.strip()
            state["step"]      = "get_number_bot"
            await update.message.reply_text(
                "🤖 <b>Step 7/9 — Number Bot Link</b>\n\nLink where users get numbers:", parse_mode="HTML"); return

        if step == "get_number_bot":
            state["number_bot"] = user_text.strip()
            state["step"]       = "get_support"
            await update.message.reply_text(
                "🤖 <b>Step 8/9 — Support Username</b>\n\nYour support @username:", parse_mode="HTML"); return

        if step == "get_support":
            state["support"] = user_text.strip()
            state["step"]    = "get_group_id_panel"
            await update.message.reply_text(
                "🤖 <b>Step 9/9 — Group Chat ID</b>\n\n"
                "Send the chat ID of your OTP group (e.g. <code>-1001234567890</code>):",
                parse_mode="HTML"); return

        if step == "get_group_id_panel":
            group_id_str = user_text.strip()
            if not group_id_str.lstrip("-").isdigit():
                await update.message.reply_text("❌ Invalid ID. Must be numeric."); return
            state["group_id"] = group_id_str
            state["step"]     = "review_info"

            # ➜ SHOW PREVIEW OF ALL INFO FOR USER CONFIRMATION ➜
            preview_text = (
                f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
                f"  📋 <b>Review Your Bot Information</b>\n"
                f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
                f"<b>1️⃣  Bot Name:</b>\n"
                f"<code>{html.escape(state.get('bot_name', '?'))}</code>\n\n"
                f"<b>2️⃣  Bot Username:</b>\n"
                f"<code>@{html.escape(state.get('bot_username', '?'))}</code>\n\n"
                f"<b>3️⃣  Bot Token:</b>\n"
                f"<code>{html.escape(state.get('token', '?')[:20])}...</code> (hidden for security)\n\n"
                f"<b>4️⃣  Your Admin ID:</b>\n"
                f"<code>{state.get('admin_id', '?')}</code>\n\n"
                f"<b>5️⃣  Channel Link:</b>\n"
                f"<code>{html.escape(state.get('channel', 'None')[:50])}</code>\n\n"
                f"<b>6️⃣  OTP Group Link:</b>\n"
                f"<code>{html.escape(state.get('otp_group', '?')[:50])}</code>\n\n"
                f"<b>7️⃣  Number Bot Link:</b>\n"
                f"<code>{html.escape(state.get('number_bot', '?')[:50])}</code>\n\n"
                f"<b>8️⃣  Support Username:</b>\n"
                f"<code>@{html.escape(state.get('support', '?')[:30])}</code>\n\n"
                f"<b>9️⃣  Group Chat ID:</b>\n"
                f"<code>{group_id_str}</code>\n\n"
                f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
                f"✅ Confirm all information is correct before submitting to admins."
            )
            
            await update.message.reply_text(
                preview_text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅  Submit Request", callback_data=f"bot_confirm_{uid}", style="success")],
                    [InlineKeyboardButton("✏️  Edit Information", callback_data=f"bot_edit_{uid}", style="primary"),
                     InlineKeyboardButton("❌  Cancel", callback_data="cancel_action", style="danger")],
                ]),
                parse_mode="HTML"
            )
            return

        if step == "review_info":
            # This handles text input while in review (shouldn't normally reach here)
            await update.message.reply_text(
                "❌ Please use the buttons below the previous message to confirm or edit.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅  Submit Request", callback_data=f"bot_confirm_{uid}", style="success")],
                    [InlineKeyboardButton("✏️  Edit Information", callback_data=f"bot_edit_{uid}", style="primary"),
                     InlineKeyboardButton("❌  Cancel", callback_data="cancel_action", style="danger")],
                ])
            )
            return

    # ── Add required chat ────────────────────────────────────────
    if AWAITING_REQ_CHAT.get(uid):
        AWAITING_REQ_CHAT.pop(uid, None)
        if not is_super_admin(uid):
            await update.message.reply_text("❌ Unauthorized."); return
        parts = [p.strip() for p in user_text.replace("|", "||").split("||") if p.strip()]
        if len(parts) < 3:
            # Try space-separated format: -1001234 Title https://t.me/link
            parts2 = user_text.strip().split(None, 2)
            if len(parts2) == 3:
                parts = parts2
        if len(parts) < 2:
            await update.message.reply_text(
                "❌ Invalid format. Use:\n"
                "<code>CHAT_ID | Title | https://t.me/link</code>", parse_mode="HTML"); return
        try:
            chat_id = int(parts[0].strip())
        except ValueError:
            await update.message.reply_text("❌ Chat ID must be a number."); return
        title = parts[1].strip()
        link  = parts[2].strip() if len(parts) > 2 else f"https://t.me/c/{str(chat_id)[4:]}"
        new_chat = {"id": chat_id, "title": title, "link": link}
        REQUIRED_CHATS.append(new_chat)
        save_config_key("REQUIRED_CHATS", REQUIRED_CHATS)
        await update.message.reply_text(
            f"✅ Added required chat:\n"
            f"• <b>{html.escape(title)}</b>  (<code>{chat_id}</code>)\n"
            f"• Link: {html.escape(link)}",
            parse_mode="HTML")
        return

    if AWAITING_SUPER_ADMIN.get(uid):
        AWAITING_SUPER_ADMIN.pop(uid, None)
        if not is_super_admin(uid):
            await update.message.reply_text("❌ Unauthorized."); return
        try: new_sid = int(user_text.strip())
        except ValueError:
            await update.message.reply_text("❌ Invalid ID — must be a number."); return
        if new_sid in INITIAL_ADMIN_IDS:
            await update.message.reply_text(f"ℹ️ <code>{new_sid}</code> is already a super admin.",parse_mode="HTML"); return
        INITIAL_ADMIN_IDS.append(new_sid)
        await set_admin_permissions(new_sid, list(PERMISSIONS.keys()))
        save_config_key("ADMIN_IDS", INITIAL_ADMIN_IDS)
        await update.message.reply_text(
            f"✅ <b>Super Admin Added</b>\n\n👑 <code>{new_sid}</code> is now a super admin.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Back",callback_data="admin_manage_admins", style="primary", icon_custom_emoji_id=_UI["back"][0])]]),
            parse_mode="HTML")
        return

    if uid in AWAITING_ADMIN_ID:
        if not is_super_admin(uid): del AWAITING_ADMIN_ID[uid]; await update.message.reply_text("❌ Unauthorized."); return
        del AWAITING_ADMIN_ID[uid]
        try:
            new_a=int(user_text.strip())
            if new_a in INITIAL_ADMIN_IDS: await update.message.reply_text("❌ Already super admin."); return
            AWAITING_PERMISSIONS[(uid,new_a)]=[]
            await update.message.reply_text(
                f"✅ User <code>{new_a}</code>. Select permissions:",
                reply_markup=perms_kb([],new_a),parse_mode="HTML")
        except ValueError: await update.message.reply_text("❌ Invalid user ID.")
        return

    # ── API Token Creation Flow ──────────────────────────
    if uid in AWAITING_API_CREATE:
        state = AWAITING_API_CREATE[uid]
        step = state.get("step", "")
        
        if user_text.strip().lower() == "/cancel":
            del AWAITING_API_CREATE[uid]
            await update.message.reply_text(f"{ui('cancel')} Cancelled.")
            return
        
        if step == "name":
            state["name"] = user_text.strip()[:50]
            state["step"] = "dev"
            await update.message.reply_text(
                f"{ui('robot')} <b>Create API Token</b> — Step 2/3\n{D}\n"
                f"Send the <b>developer/company name</b>:\n"
                f"/skip to leave blank.",
                parse_mode="HTML")
            return
        
        if step == "dev":
            if user_text.strip().lower() != "/skip":
                state["dev"] = user_text.strip()[:50]
            else:
                state["dev"] = ""
            state["step"] = "panels"
            # Create inline keyboard for panel selection
            await context.bot.send_message(uid,
                f"{ui('robot')} <b>Create API Token</b> — Step 3/3\n{D}\n"
                "Select which panels this API token can access:\n"
                "(Click panels to toggle selection)",
                reply_markup=api_panel_selection_kb([]),
                parse_mode="HTML")
            return

    # ── Add Log Group ID ─────────────────────────────────
    if uid in AWAITING_LOG_ID:
        perms=await get_admin_permissions(uid)
        if "manage_logs" not in perms and not is_super_admin(uid):
            del AWAITING_LOG_ID[uid]; await update.message.reply_text("❌ Unauthorized."); return
        del AWAITING_LOG_ID[uid]
        try:
            cid=int(user_text.strip()); ok=await db.add_log_chat(cid)
            await update.message.reply_text(
                f"{'✅ Added' if ok else '⚠️ Exists'}: <code>{cid}</code>",parse_mode="HTML")
        except ValueError: await update.message.reply_text("❌ Invalid chat ID.")
        return

    # ── Config Link Prompts ──────────────────────────────
    if context.user_data.get("awaiting_website_setting"):
        if not is_super_admin(uid):
            context.user_data.pop("awaiting_website_setting", None)
            await update.message.reply_text("❌ Unauthorized.")
            return
        setting_key = context.user_data.pop("awaiting_website_setting")
        value = user_text.strip()
        save_config_key(setting_key, value)
        label_map = {
            "WEBSITE_ANNOUNCEMENT": "Website announcement",
            "WEBSITE_STATUS_NOTE": "Website approval note",
            "WEBSITE_CONTACT_WHATSAPP": "Website WhatsApp contact",
            "WEBSITE_CONTACT_TELEGRAM": "Website Telegram contact",
        }
        await update.message.reply_text(
            f"{ui('check')} <b>{html.escape(label_map.get(setting_key, setting_key))} updated</b>\n{D}\n"
            f"<code>{html.escape(_safe_short(value, 250))}</code>",
            parse_mode="HTML",
            reply_markup=website_management_kb(),
        )
        return

    if context.user_data.get("awaiting_link"):
        link_key=context.user_data.pop("awaiting_link")
        global CHANNEL_LINK, OTP_GROUP_LINK, NUMBER_BOT_LINK, SUPPORT_USER
        val=user_text.strip()
        if link_key=="CHANNEL_LINK":    CHANNEL_LINK=val
        elif link_key=="OTP_GROUP_LINK": OTP_GROUP_LINK=val
        elif link_key=="NUMBER_BOT_LINK": NUMBER_BOT_LINK=val
        elif link_key=="SUPPORT_USER":   SUPPORT_USER=val
        elif link_key=="DEVELOPER":      DEVELOPER=val
        save_config_key(link_key,val)
        if link_key == "FIND_OTP":
            target = val.replace("+","").strip()
            found_otp = next((otp for num,otp in load_otp_store().items() if target in num), None)
            if found_otp:
                await update.message.reply_text(
                    f"🔑 <b>OTP Found</b>\n\n"
                    f"📱 Number: <code>{target}</code>\n"
                    f"🔐 <b>Code:</b> <code>{found_otp}</code>",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(f"📋 Copy: {found_otp}",
                                             copy_text=CopyTextButton(text=found_otp)),
                        InlineKeyboardButton("🔙 Back",callback_data="admin_otp_tools", style="primary")]]),
                    parse_mode="HTML")
            else:
                await update.message.reply_text(
                    f"❌ No OTP found for <code>{target}</code>.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back",callback_data="admin_otp_tools", style="primary")]]),
                    parse_mode="HTML")
            return
        label={"CHANNEL_LINK":"Channel","OTP_GROUP_LINK":"OTP Group",
               "NUMBER_BOT_LINK":"Number Bot","SUPPORT_USER":"Support",
               "DEVELOPER":"Developer"}.get(link_key,link_key)
        await update.message.reply_text(
            f"✅ {label} updated to:\n<code>{html.escape(val)}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="admin_links", style="primary")]]))
        return

    # ── Prefix Setting ───────────────────────────────────
    if context.user_data.get("awaiting_prefix"):
        cat=context.user_data.pop("prefix_cat",None)
        context.user_data["awaiting_prefix"]=False
        if user_text.lower()=="off":
            await db.set_user_prefix(uid,None); await update.message.reply_text(f"{ui('check')} Prefix disabled.")
        else:
            cnt=await db.check_prefix_availability(cat,user_text)
            if cnt>0:
                await db.set_user_prefix(uid,user_text)
                await update.message.reply_text(
                    f"✅ Prefix <code>{user_text}</code> set. {cnt} numbers match.",parse_mode="HTML")
                await db.release_number(uid)
                limit=await get_user_assign_count(uid)
                phones,_,_=await db.request_numbers(uid,cat,count=limit)
                if phones:
                    active=await db.get_active_numbers(uid)
                    svc=(active[0].category.split(" - ")[1] if active and " - " in active[0].category else cat)
                    lines=[f"{i+1}. <code>+{n.phone_number}</code>" for i,n in enumerate(active)]
                    text=f"🎉 <b>New Numbers</b>\n{D}\n"+"\n".join(lines)+"\n\n⚡ Waiting…"
                    msg=await context.bot.send_message(chat_id=uid,
                        text=text,
                        reply_markup=waiting_kb(user_text,service=svc),parse_mode="HTML")
                    for n in active: await db.update_message_id(n.phone_number,msg.message_id)
            else:
                await update.message.reply_text(f"❌ No numbers with prefix <code>{user_text}</code>.",parse_mode="HTML")
        return

    # ── Child Bot Link Edit Flow ─────────────────────────
    if context.user_data.get("bot_setlink_bid") and not IS_CHILD_BOT:
        bid  = context.user_data.pop("bot_setlink_bid", None)
        key  = context.user_data.pop("bot_setlink_key", None)
        if not is_super_admin(uid):
            await update.message.reply_text("❌ Unauthorized."); return
        if user_text == "/cancel":
            await update.message.reply_text("❌ Cancelled."); return
        val = user_text.strip()
        # Update the registry entry
        reg = bm.load_registry()
        key_map = {
            "CHANNEL_LINK":    "channel_link",
            "OTP_GROUP_LINK":  "otp_group_link",
            "NUMBER_BOT_LINK": "number_bot_link",
            "SUPPORT_USER":    "support_user",
        }
        reg_key = key_map.get(key, key.lower())
        if bid and bid in reg:
            reg[bid][reg_key] = val
            bm.save_registry(reg)
            # Also update the config.json inside the child folder
            try:
                folder = reg[bid].get("folder", "")
                cfg_path = os.path.join(folder, "config.json")
                if os.path.exists(cfg_path):
                    with open(cfg_path) as f: child_cfg = json.load(f)
                    child_cfg[key] = val
                    with open(cfg_path, "w") as f: json.dump(child_cfg, f, indent=2)
            except Exception as e:
                logger.error(f"Child config update: {e}")
        label_map = {"CHANNEL_LINK":"Channel","OTP_GROUP_LINK":"OTP Group","NUMBER_BOT_LINK":"Number Bot","SUPPORT_USER":"Support"}
        await update.message.reply_text(
            f"✅ <b>{label_map.get(key,key)}</b> updated for bot <code>{bid}</code>\n"
            f"New value: <code>{html.escape(val)}</code>\n\n"
            "<i>Restart the child bot to apply changes.</i>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back to Bot", callback_data=f"bot_info_{bid}", style="primary")]]),
            parse_mode="HTML")
        return

    # ── Tutorial Creation Flow ───────────────────────────
    if context.user_data.get("tutorial_step"):
        is_sup = is_super_admin(uid)
        if not is_sup:
            context.user_data.pop("tutorial_step", None)
            await update.message.reply_text("❌ Unauthorized.")
            return
        
        step = context.user_data.get("tutorial_step")
        
        if step == "name":
            if user_text.lower() == "/cancel":
                context.user_data.pop("tutorial_step", None)
                context.user_data.pop("tutorial_data", None)
                await update.message.reply_text("❌ Cancelled.")
                return
            
            context.user_data["tutorial_data"] = {"title": user_text.strip()}
            context.user_data["tutorial_step"] = "description"
            
            await update.message.reply_text(
                "<b>Step 2: Add Description (Optional)</b>\n\n"
                "Send tutorial description or /skip",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data="admin_tutorials", style="danger")
                ]])
            )
            return
        
        elif step == "description":
            if user_text.lower() != "/skip":
                context.user_data["tutorial_data"]["description"] = user_text.strip()
            else:
                context.user_data["tutorial_data"]["description"] = None
            
            context.user_data["tutorial_step"] = "type"
            
            await context.bot.send_message(uid,
                "<b>Step 3: Select Content Type</b>\n\n"
                "What type of tutorial is this?",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📄  Text Only", callback_data="tut_type_text", style="primary")],
                    [InlineKeyboardButton("🎬  Video Only", callback_data="tut_type_video", style="primary")],
                    [InlineKeyboardButton("📚  Text + Video", callback_data="tut_type_both", style="primary")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="admin_tutorials", style="danger")]
                ])
            )
            return
        
        elif step == "text_content":
            if user_text.lower() == "/skip":
                context.user_data["tutorial_data"]["text_content"] = None
            else:
                context.user_data["tutorial_data"]["text_content"] = user_text.strip()
            
            content_type = context.user_data["tutorial_data"].get("content_type", "text")
            
            if content_type in ("video", "both"):
                context.user_data["tutorial_step"] = "video_content"
                await update.message.reply_text(
                    "<b>Step 4: Upload Video</b>\n\n"
                    "Send the video file or /skip if not needed",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("❌ Cancel", callback_data="admin_tutorials", style="danger")
                    ]])
                )
            else:
                # Saving tutorial
                data = context.user_data.pop("tutorial_data", {})
                context.user_data.pop("tutorial_step", None)
                
                tut_id = await db.add_tutorial(
                    title=data.get("title", ""),
                    description=data.get("description"),
                    content_type=data.get("content_type", "text"),
                    text_content=data.get("text_content"),
                    created_by=uid
                )
                
                await update.message.reply_text(
                    f"✅ <b>Tutorial Created!</b>\n\n"
                    f"📝 <b>Title:</b> {html.escape(data.get('title', ''))}\n"
                    f"📊 <b>Type:</b> {data.get('content_type', 'text')}\n\n"
                    "<i>Users can now view this tutorial in the app.</i>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📚  Manage Tutorials", callback_data="admin_tutorials", style="primary")
                    ]])
                )
            return
        
        elif step == "video_content":
            if update.message.video:
                context.user_data["tutorial_data"]["video_file_id"] = update.message.video.file_id
                
                # Saving tutorial
                data = context.user_data.pop("tutorial_data", {})
                context.user_data.pop("tutorial_step", None)
                
                tut_id = await db.add_tutorial(
                    title=data.get("title", ""),
                    description=data.get("description"),
                    content_type=data.get("content_type", "video"),
                    text_content=data.get("text_content"),
                    video_file_id=data.get("video_file_id"),
                    created_by=uid
                )
                
                await update.message.reply_text(
                    f"✅ <b>Tutorial Created!</b>\n\n"
                    f"📝 <b>Title:</b> {html.escape(data.get('title', ''))}\n"
                    f"📊 <b>Type:</b> {data.get('content_type', 'video')}\n\n"
                    "<i>Users can now view this tutorial in the app.</i>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📚  Manage Tutorials", callback_data="admin_tutorials", style="primary")
                    ]])
                )
            elif user_text.lower() == "/skip":
                data = context.user_data.pop("tutorial_data", {})
                context.user_data.pop("tutorial_step", None)
                
                tut_id = await db.add_tutorial(
                    title=data.get("title", ""),
                    description=data.get("description"),
                    content_type=data.get("content_type", "text"),
                    text_content=data.get("text_content"),
                    created_by=uid
                )
                
                await update.message.reply_text(
                    f"✅ <b>Tutorial Created!</b>\n\n"
                    f"📝 <b>Title:</b> {html.escape(data.get('title', ''))}\n"
                    f"📊 <b>Type:</b> {data.get('content_type', 'text')}\n\n"
                    "<i>Users can now view this tutorial in the app.</i>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("📚  Manage Tutorials", callback_data="admin_tutorials", style="primary")
                    ]])
                )
            else:
                await update.message.reply_text(
                    "❌ Please send a video file or use /skip",
                    parse_mode="HTML"
                )
            return

    # ── Multi-Bot Add Flow ───────────────────────────────
    if uid in BOT_ADD_STATES and not IS_CHILD_BOT:
        st=BOT_ADD_STATES[uid]; step=st["step"]; d=st["data"]
        if user_text=="/cancel": del BOT_ADD_STATES[uid]; await update.message.reply_text("❌ Bot creation cancelled."); return
        steps={
            "name":        ("token",       "🔑 Now send the <b>Bot Token</b>\n<i>Get from @BotFather → /newbot</i>"),
            "token":       ("username",    "🤖 Send the <b>Bot Username</b> (e.g. @MyOTPBot)\n<i>No need for @ symbol</i>"),
            "username":    ("admin_id",    "👤 Send the <b>Admin Telegram ID</b>\n<i>Numeric ID — use @userinfobot</i>"),
            "admin_id":    ("channel",     "📢 Send the <b>Channel Link</b> (https://t.me/...)\nor /skip to leave blank"),
            "channel":     ("otp_group",   "💬 Send the <b>OTP Group Link</b> (https://t.me/...)\nor /skip to leave blank"),
            "otp_group":   ("numbot",      "📞 Send the <b>Number Bot Link</b> (https://t.me/...)\nor /skip to leave blank"),
            "numbot":      ("support",     "🛟 Send the <b>Support Username</b> (e.g. @support)\nor /skip to leave blank"),
            "support":     ("developer",   "🧠 Send the <b>Developer Username</b> (e.g. @dev)\nor /skip to leave blank"),
            "developer":   (None,          ""),
        }
        if step=="token":
            if not re.match(r'^\d+:[A-Za-z0-9_-]{35,}$',user_text.strip()):
                await update.message.reply_text(
                    "❌ Invalid token format.\nExpected: <code>123456:ABCxyz...</code>\nTry again or /cancel.",
                    parse_mode="HTML"); return
        if step=="admin_id":
            try: d["admin_ids"]=[int(user_text.strip())]
            except ValueError: await update.message.reply_text("❌ Must be a numeric ID."); return
        elif step not in ("admin_id",):
            val="" if user_text.strip()=="/skip" else user_text.strip()
            d[step]=val

        nxt,prompt=steps.get(step,(None,""))
        if nxt:
            st["step"]=nxt
            await update.message.reply_text(prompt,parse_mode="HTML")
        else:
            # All steps done — create the bot
            await update.message.reply_text("⏳ Creating bot folder and files…")
            bot_id=str(uuid.uuid4())[:8]
            ok,folder,err=bm.create_bot_folder(
                bot_id=bot_id, name=d.get("name",""),
                token=d.get("token",""), bot_username=d.get("username",""),
                admin_ids=d.get("admin_ids",[uid]),
                channel_link=d.get("channel",""), otp_group_link=d.get("otp_group",""),
                number_bot_link=d.get("numbot",""), support_user=d.get("support","@ownersigma"),
                developer=d.get("developer","@NONEXPERTCODER"),
                get_number_url=d.get("numbot","https://t.me/CRACKSMSREBOT"))
            del BOT_ADD_STATES[uid]
            if ok:
                start_ok,start_msg=bm.start_bot(bot_id)
                st_icon="🟢" if start_ok else "🔴"
                await update.message.reply_text(
                    f"✅ <b>Bot Created Successfully!</b>\n{D}\n"
                    f"🤖 <b>Name:</b>     {html.escape(d.get('name',''))}\n"
                    f"👤 <b>Username:</b> @{html.escape(d.get('username',''))}\n"
                    f"🆔 <b>Bot ID:</b>   <code>{bot_id}</code>\n"
                    f"📁 <b>Folder:</b>   <code>{html.escape(folder)}</code>\n"
                    f"📢 <b>Channel:</b>  {html.escape(d.get('channel','—') or '—')}\n"
                    f"💬 <b>OTP Group:</b>{html.escape(d.get('otp_group','—') or '—')}\n"
                    f"{st_icon} <b>Status:</b>  {start_msg}\n\n"
                    f"<i>The bot runs independently with its own database.</i>",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🖥  Manage Bots",callback_data="admin_bots", style="primary")
                    ]]),parse_mode="HTML")
            else:
                await update.message.reply_text(f"❌ Failed: {err}")
        return

    # ── Broadcast Flow ───────────────────────────────────
    # ── BROADCAST: Handle different broadcast modes (text, image, video, tutorial) ──
    bcast_mode = context.user_data.get("bcast_mode")
    
    if bcast_mode == "text" and context.user_data.get("awaiting_broadcast"):
        perms=await get_admin_permissions(uid)
        if "broadcast" not in perms and not is_super_admin(uid):
            context.user_data.pop("awaiting_broadcast", None); await update.message.reply_text("❌ Unauthorized."); return
        context.user_data.pop("awaiting_broadcast", None)
        context.user_data.pop("bcast_mode", None)
        
        bcast_text=user_text
        all_users=await db.get_all_users()
        total=len(all_users); sent=0; failed=0
        
        sm=await context.bot.send_message(
            chat_id=uid,
            text=f"📢 <b>Broadcasting to {total} users…</b>\n\n{pbar(0,max(total,1))}\n⏳ Sending…",
            parse_mode="HTML")
        
        for target in all_users:
            try:
                await context.bot.send_message(chat_id=target,
                    text=f"📢 <b>Announcement</b>\n{'━'*24}\n{bcast_text}\n\n<i>— Admin Broadcast</i>",
                    parse_mode="HTML")
                sent+=1
            except TelegramForbidden: failed+=1
            except Exception: failed+=1
            if (sent+failed)%10==0 or (sent+failed)==total:
                try:
                    await sm.edit_text(
                        f"📢 Broadcasting…\n\n{pbar(sent+failed,max(total,1))}\n✅ {sent}  ❌ {failed}",
                        parse_mode="HTML")
                except Exception: pass
            await asyncio.sleep(0.04)
        
        try:
            await sm.edit_text(
                f"✅ <b>Broadcast Complete!</b>\n\n{pbar(total,max(total,1))}\n✅ {sent} sent  ❌ {failed} failed",
                parse_mode="HTML")
        except Exception: pass
        
        await context.bot.send_message(uid,
            f"📊 <b>Broadcast Summary</b>\n{'━'*24}\n✅ <b>Sent:</b> {sent}\n❌ <b>Failed:</b> {failed}\n📈 <b>Success Rate:</b> {int((sent/(max(sent+failed,1)))*100)}%",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="admin_broadcast_menu", style="primary")]]))
        return
    
    if bcast_mode in ("image", "video", "tutorial") and context.user_data.get("awaiting_broadcast"):
        perms=await get_admin_permissions(uid)
        if "broadcast" not in perms and not is_super_admin(uid):
            context.user_data.pop("awaiting_broadcast", None); await update.message.reply_text("❌ Unauthorized."); return
        
        # For image/video/tutorial - first collect the media, then ask for caption
        if update.message.photo:
            context.user_data["bcast_photo"] = update.message.photo[-1].file_id
            caption = update.message.caption or ""
        elif update.message.video:
            context.user_data["bcast_video_id"] = update.message.video.file_id
            caption = update.message.caption or ""
        else:
            caption = user_text
        
        context.user_data["bcast_caption"] = caption
        
        # Now ask for description if not provided
        if not caption:
            await update.message.reply_text(
                f"<b>Add a title/caption for your {bcast_mode}:</b>\n\n"
                "This will be shown to all users.",
                parse_mode="HTML")
            context.user_data["awaiting_broadcast_caption"] = True
            return
        
        # If caption provided or now available, proceed with broadcast
        context.user_data.pop("awaiting_broadcast", None)
        context.user_data.pop("awaiting_broadcast_caption", None)
        context.user_data.pop("bcast_mode", None)
        
        all_users=await db.get_all_users()
        total=len(all_users); sent=0; failed=0
        
        sm=await context.bot.send_message(
            chat_id=uid,
            text=f"📤 <b>Broadcasting {bcast_mode} to {total} users…</b>\n\n{pbar(0,max(total,1))}\n⏳ Sending…",
            parse_mode="HTML")
        
        bcast_caption = caption or f"New {bcast_mode} from admin"
        styled_caption = f"❦ <b>{bcast_mode.upper()} Broadcast</b>\n{'━'*24}\n{bcast_caption}\n\n<i>— Admin Update</i>"
        
        for target in all_users:
            try:
                if bcast_mode == "image" and "bcast_photo" in context.user_data:
                    await context.bot.send_photo(chat_id=target, photo=context.user_data["bcast_photo"],
                        caption=styled_caption, parse_mode="HTML")
                elif bcast_mode == "video" and "bcast_video_id" in context.user_data:
                    await context.bot.send_video(chat_id=target, video=context.user_data["bcast_video_id"],
                        caption=styled_caption, parse_mode="HTML")
                elif bcast_mode == "tutorial":
                    # Tutorial: Send media if available, otherwise just text
                    if "bcast_photo" in context.user_data:
                        await context.bot.send_photo(chat_id=target, 
                            photo=context.user_data["bcast_photo"],
                            caption=f"📚 <b>Tutorial</b>\n{'━'*24}\n{bcast_caption}",
                            parse_mode="HTML")
                    elif "bcast_video_id" in context.user_data:
                        await context.bot.send_video(chat_id=target,
                            video=context.user_data["bcast_video_id"],
                            caption=f"📚 <b>Tutorial</b>\n{'━'*24}\n{bcast_caption}",
                            parse_mode="HTML")
                    else:
                        await context.bot.send_message(chat_id=target,
                            text=f"📚 <b>Tutorial</b>\n{'━'*24}\n<b>{caption}</b>\n\n<i>— Learning Materials</i>",
                            parse_mode="HTML")
                sent+=1
            except TelegramForbidden: failed+=1
            except Exception as e: 
                logger.error(f"Broadcast send error: {e}")
                failed+=1
            
            if (sent+failed)%10==0 or (sent+failed)==total:
                try:
                    await sm.edit_text(
                        f"📤 Broadcasting…\n\n{pbar(sent+failed,max(total,1))}\n✅ {sent}  ❌ {failed}",
                        parse_mode="HTML")
                except Exception: pass
            await asyncio.sleep(0.04)
        
        try:
            await sm.edit_text(
                f"✅ <b>{bcast_mode.upper()} Broadcast Complete!</b>\n\n{pbar(total,max(total,1))}\n✅ {sent} sent  ❌ {failed} failed",
                parse_mode="HTML")
        except Exception: pass
        
        # Clean up uploaded media from user_data
        context.user_data.pop("bcast_photo", None)
        context.user_data.pop("bcast_video_id", None)
        context.user_data.pop("bcast_caption", None)
        
        await context.bot.send_message(uid,
            f"📊 <b>Broadcast Summary</b>\n{'━'*24}\n✅ <b>Sent:</b> {sent}\n❌ <b>Failed:</b> {failed}\n📈 <b>Success Rate:</b> {int((sent/(max(sent+failed,1)))*100)}%",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="admin_broadcast_menu", style="primary")]]))
        return
    
    # Handle caption input for image/video/tutorial
    if context.user_data.get("awaiting_broadcast_caption"):
        context.user_data["bcast_caption"] = user_text
        context.user_data["awaiting_broadcast_caption"] = False
        # Trigger the broadcast with caption now set
        context.user_data["awaiting_broadcast"] = True
        # Re-call by recursion - actually simpler to just continue below
        bcast_mode = context.user_data.get("bcast_mode")
        caption = user_text
        context.user_data.pop("awaiting_broadcast", None)
        context.user_data.pop("bcast_mode", None)
        
        all_users=await db.get_all_users()
        total=len(all_users); sent=0; failed=0
        sm=await context.bot.send_message(
            chat_id=uid,
            text=f"📤 <b>Broadcasting to {total} users…</b>\n\n{pbar(0,max(total,1))}\n⏳ Sending…",
            parse_mode="HTML")
        
        styled_caption = f"{'🎬' if bcast_mode=='video' else '🖼️' if bcast_mode=='image' else '📚'} <b></b>\n{'━'*24}\n{caption}\n\n<i>— Admin Update</i>"
        
        for target in all_users:
            try:
                if bcast_mode == "image" and "bcast_photo" in context.user_data:
                    await context.bot.send_photo(chat_id=target, photo=context.user_data.get("bcast_photo"),
                        caption=styled_caption, parse_mode="HTML")
                elif bcast_mode == "video" and "bcast_video_id" in context.user_data:
                    await context.bot.send_video(chat_id=target, video=context.user_data.get("bcast_video_id"),
                        caption=styled_caption, parse_mode="HTML")
                elif bcast_mode == "tutorial":
                    # Tutorial: Send media if available, otherwise just text
                    if "bcast_photo" in context.user_data:
                        await context.bot.send_photo(chat_id=target,
                            photo=context.user_data.get("bcast_photo"),
                            caption=f"📚 <b>Tutorial</b>\n{'━'*24}\n{caption}",
                            parse_mode="HTML")
                    elif "bcast_video_id" in context.user_data:
                        await context.bot.send_video(chat_id=target,
                            video=context.user_data.get("bcast_video_id"),
                            caption=f"📚 <b>Tutorial</b>\n{'━'*24}\n{caption}",
                            parse_mode="HTML")
                    else:
                        await context.bot.send_message(chat_id=target,
                            text=f"📚 <b>Tutorial</b>\n{'━'*24}\n{caption}",
                            parse_mode="HTML")
                else:
                    await context.bot.send_message(chat_id=target, text=styled_caption, parse_mode="HTML")
                sent+=1
            except Exception: failed+=1
            
            if (sent+failed)%10==0 or (sent+failed)==total:
                try:
                    await sm.edit_text(f"📤 Broadcasting…\n\n{pbar(sent+failed,max(total,1))}\n✅ {sent}  ❌ {failed}", parse_mode="HTML")
                except: pass
            await asyncio.sleep(0.04)
        
        context.user_data.pop("bcast_photo", None)
        context.user_data.pop("bcast_video_id", None)
        context.user_data.pop("bcast_caption", None)
        
        await context.bot.send_message(uid,
            f"✅ <b>Broadcast Complete!</b>\n{'━'*24}\n✅ Sent: {sent}\n❌ Failed: {failed}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="admin_broadcast_menu", style="primary")]]))
        return
    
    if context.user_data.get("awaiting_broadcast"):
        # Original simple broadcast for backward compatibility
        perms=await get_admin_permissions(uid)
        if "broadcast" not in perms and not is_super_admin(uid):
            context.user_data["awaiting_broadcast"]=False; await update.message.reply_text("❌ Unauthorized."); return
        context.user_data["awaiting_broadcast"]=False
        bcast_text=user_text
        all_users=await db.get_all_users()
        total=len(all_users); sent=0; failed=0
        sm=await context.bot.send_message(
            chat_id=uid,
            text=f"📢 <b>Broadcasting to {total} users…</b>\n\n{pbar(0,max(total,1))}\nStarting…",
            parse_mode="HTML")
        for target in all_users:
            try:
                await context.bot.send_message(chat_id=target,
                    text=f"📢 <b>Announcement</b>\n{D}\n{bcast_text}",parse_mode="HTML")
                sent+=1
            except TelegramForbidden: failed+=1
            except Exception: failed+=1
            if (sent+failed)%10==0 or (sent+failed)==total:
                try:
                    await sm.edit_text(
                        f"📢 Broadcasting…\n\n{pbar(sent+failed,max(total,1))}\n✅{sent} ❌{failed}",
                        parse_mode="HTML")
                except Exception: pass
            await asyncio.sleep(0.04)
        try:
            await sm.edit_text(
                f"✅ <b>Broadcast Done</b>\n\n{pbar(total,max(total,1))}\n✅{sent} ❌{failed}",
                parse_mode="HTML")
        except Exception: pass
        await context.bot.send_message(uid,"Done.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="admin_home", style="primary")]]))
        return

    # ── Broadcast to ALL bots users (master + all child bots) ──
    if context.user_data.get("bcast_all_bots") and not IS_CHILD_BOT:
        context.user_data.pop("bcast_all_bots", None)
        perms_check = await get_admin_permissions(uid)
        if not is_super_admin(uid):
            await update.message.reply_text("❌ Unauthorized."); return
        bcast_text = user_text
        # Collect all users from this master bot
        master_users = await db.get_all_users()
        # Collect users from each child bot's database
        all_targets = list(master_users)
        child_dbs_users = []
        bots_reg = bm.load_registry()
        for bid, info in bots_reg.items():
            folder = info.get("folder","")
            child_db = os.path.join(folder, "bot_database.db")
            if os.path.exists(child_db):
                try:
                    import sqlite3 as _sq
                    conn = _sq.connect(child_db)
                    rows = conn.execute("SELECT user_id FROM users").fetchall()
                    conn.close()
                    child_dbs_users.extend([r[0] for r in rows])
                except Exception: pass
        # Deduplicate
        all_targets = list(set(all_targets + child_dbs_users))
        total = len(all_targets); sent = 0; failed = 0
        sm = await context.bot.send_message(
            chat_id=uid,
            text=f"📢 <b>Broadcasting to ALL bots: {total} users…</b>",
            parse_mode="HTML")
        for target in all_targets:
            try:
                await context.bot.send_message(
                    chat_id=target,
                    text=f"📢 <b>Announcement</b>\n{D}\n{bcast_text}",
                    parse_mode="HTML")
                sent += 1
            except (TelegramForbidden, Exception): failed += 1
            if (sent+failed) % 20 == 0:
                try:
                    await sm.edit_text(
                        f"📢 Broadcasting all bots…\n{pbar(sent+failed,max(total,1))}\n✅{sent} ❌{failed}",
                        parse_mode="HTML")
                except Exception: pass
            await asyncio.sleep(0.04)
        try:
            await sm.edit_text(
                f"✅ <b>All-Bots Broadcast Done</b>\n{pbar(total,max(total,1))}\n"
                f"✅ Sent: {sent}  ❌ Failed: {failed}\n"
                f"📊 Total unique users: {total}",
                parse_mode="HTML")
        except Exception: pass
        return

    # Only send "Use /start" message in private chats, not in groups
    if update.message.chat.type == "private":
        await update.message.reply_text("Use /start to see the menu.")


# ═══════════════════════════════════════════════════════════
#  CALLBACK HANDLER
# ═══════════════════════════════════════════════════════════
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DEFAULT_ASSIGN_LIMIT, CHANNEL_LINK, OTP_GROUP_LINK, NUMBER_BOT_LINK, SUPPORT_USER
    global OTP_GUI_THEME, AUTO_BROADCAST_ON
    
    query = update.callback_query
    data = query.data
    uid = query.from_user.id
    
    # ── User menu: My Stats ─────────────────────────────────────────────
    if data == "mystats":
        await query.answer()
        # Reuse the /mystats command logic, but send as edit_message_text
        uid = query.from_user.id
        stats = await db.get_user_stats(uid)
        active = await db.get_active_numbers(uid)
        perms = await get_admin_permissions(uid)
        role = "👑 Super Admin" if is_super_admin(uid) else ("👮 Admin" if perms else "👤 User")
        ai = ""
        if active:
            ai = "\n\n📱 <b>Active Numbers:</b>\n" + ", ".join(f"<code>+{n.phone_number}</code>" for n in active)
        await query.edit_message_text(
            f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n👤 <b>USER PROFILE</b>\n<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            f"🆔 <b>ID:</b> <code>{uid}</code>\n"
            f"📛 <b>Name:</b> {html.escape(query.from_user.first_name)}\n"
            f"🎭 <b>Role:</b> {role}\n\n"
            f"📊 <b>Statistics</b>\n"
            f"✅ <b>Successful OTPs:</b> <i>{stats['success']}</i>\n"
            f"🔄 <b>Total Received:</b> <i>{stats['total']}</i>{ai}",
            reply_markup=main_menu_kb(), parse_mode="HTML")
        return

    # ── User menu: My History ───────────────────────────────────────────
    if data == "myhistory":
        await query.answer()
        # Reuse the /myhistory command logic, but send as edit_message_text
        uid = query.from_user.id
        page = 1
        async with db.AsyncSessionLocal() as session:
            nums = (await session.execute(
                select(db.Number).where(db.Number.assigned_to == uid)
            )).scalars().all()
        history = []
        for num in sorted(nums, key=lambda x: x.last_msg or 0, reverse=True):
            if num.last_otp:
                history.append({
                    "phone": num.phone_number,
                    "otp": num.last_otp[:6] if num.last_otp else "N/A",
                    "service": num.category.split("-")[-1].strip() if num.category else "Unknown",
                    "time": num.last_msg or "Never"
                })
        def paginate_list(lst, page, per_page):
            total_pages = max(1, (len(lst) + per_page - 1) // per_page)
            start = (page - 1) * per_page
            end = start + per_page
            items = lst[start:end]
            has_prev = page > 1
            has_next = page < total_pages
            return items, total_pages, has_prev, has_next
        items, total_pages, has_prev, has_next = paginate_list(history, page, 5)
        lines = [
            "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
            f"📜 <b>OTP History</b> (Page {page}/{total_pages})",
            "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
        ]
        if items:
            for h in items:
                lines.append(f"\n📱 <code>{h['phone']}</code> • {h['service']}")
                lines.append(f"   🔑 {h['otp']} • {h['time']}")
        else:
            lines.append("\n📭 No OTP history yet. Request a number to start!")
        lines.append("\n<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>")
        kb_rows = []
        kb_rows.append([btn("🔙  Back", cbd="main_menu")])
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(kb_rows),
            parse_mode="HTML"
        )
        return

    if data=="ignore": await query.answer(); return

    # ── Membership re-check ───────────────────────────────────────
    if data=="check_membership":
        await query.answer("⏳ Checking…")
        missing = await check_membership(context.bot, uid)
        if missing:
            logger.warning(f"⚠️ User membership incomplete | UID: {uid} | Missing: {missing} | Bot: {'CHILD' if IS_CHILD_BOT else 'MAIN'}")
            await send_join_required(query, context.bot, uid, missing)
        else:
            # All joined — show the welcome screen
            name     = html.escape(update.effective_user.first_name)
            bot_name = f"@{BOT_USERNAME}" if BOT_USERNAME else "@CrackSMSReBot"
            perms    = await get_admin_permissions(uid)
            role_line = ""
            if is_super_admin(uid): role_line = f"\n{emoji('crown', uid)} <b>Super Admin</b>"
            elif perms:             role_line = f"\n{emoji('user', uid)} <b>Admin</b>"
            logger.info(f"{emoji('check', uid)} User verified | UID: {uid} | Name: {name} | Bot: {bot_name} | Type: {'CHILD' if IS_CHILD_BOT else 'MAIN'} | Child Bot Isolated: {IS_CHILD_BOT}")
            await query.edit_message_text(
                "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n"
                f"{emoji('diamond', uid)} <b>CRACK SMS · FREE NUMBERS</b>  |  {emoji('robot', uid)} {html.escape(bot_name)}\n"
                "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
                f"{emoji('check', uid)} <b>Verified!</b> Welcome, <a href='tg://user?id={uid}'><b>{name}</b></a>{role_line}\n\n"
                f"{emoji('fire')} <b>100% FREE – No Payments, No Hidden Fees</b>\n\n"
                f"{emoji('bolt', uid)} <b>Real‑Time OTP Delivery</b>  –  instant codes\n"
                f"{emoji('key', uid)} <b>Auto‑Assign System</b>      –  no manual work\n"
                f"{emoji('rocket', uid)} <b>All Services & Countries</b> –  unlimited free OTPs\n\n"
                f"👇 <b>Choose an option below</b>",
                reply_markup=main_menu_kb(),
                parse_mode="HTML"
            )

    if data=="main_menu":
        await query.answer()
        logger.info(f"{emoji('notepad', uid)} Main menu requested | UID: {uid} | Bot Type: {'CHILD' if IS_CHILD_BOT else 'MAIN'} | Child Bot Isolated: {IS_CHILD_BOT} | Showing: COMPACT")
        await query.edit_message_text(f"{emoji('rocket', uid)} <b>Main Menu</b>",reply_markup=main_menu_compact_kb(),parse_mode="HTML")
        return
    
    if data=="main_menu_compact":
        await query.answer()
        logger.info(f"{emoji('notepad', uid)} Compact menu requested | UID: {uid} | Bot Type: {'CHILD' if IS_CHILD_BOT else 'MAIN'} | Child Bot Isolated: {IS_CHILD_BOT}")
        await query.edit_message_text(f"{emoji('rocket', uid)} <b>Main Menu</b>",reply_markup=main_menu_compact_kb(),parse_mode="HTML")
        return
    
    if data=="main_menu_full":
        await query.answer()
        logger.info(f"{emoji('book', uid)} Full menu requested | UID: {uid} | Bot Type: {'CHILD' if IS_CHILD_BOT else 'MAIN'} | Child Bot Isolated: {IS_CHILD_BOT} | Showing: EXPANDED")
        await query.edit_message_text(f"{emoji('rocket', uid)} <b>Main Menu — All Options</b>",reply_markup=main_menu_full_kb(),parse_mode="HTML")
        return

    if data=="profile":
        await query.answer()
        logger.info(f"{emoji('user', uid)} Profile view requested | UID: {uid} | Bot Type: {'CHILD' if IS_CHILD_BOT else 'MAIN'} | Child Bot Isolated: {IS_CHILD_BOT}")
        stats=await db.get_user_stats(uid)
        active=await db.get_active_numbers(uid)
        perms=await get_admin_permissions(uid)
        role=f"{emoji('crown', uid)} Super Admin" if is_super_admin(uid) else (f"{emoji('user', uid)} Admin" if perms else f"{emoji('user', uid)} User")
        ai=""
        if active: ai=f"\n\n{emoji('phone', uid)} <b>Active Numbers:</b>\n"+", ".join(f"<code>+{n.phone_number}</code>" for n in active)
        await query.edit_message_text(
            f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n{emoji('user', uid)} <b>USER PROFILE</b>\n<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            f"{emoji('info', uid)} <b>ID:</b> <code>{uid}</code>\n"
            f"{emoji('user', uid)} <b>Name:</b> {html.escape(query.from_user.first_name)}\n"
            f"{emoji('shield', uid)} <b>Role:</b> {role}\n\n"
            f"{emoji('chart', uid)} <b>Statistics</b>\n"
            f"{emoji('check', uid)} <b>Successful OTPs:</b> <i>{stats['success']}</i>\n"
            f"{emoji('refresh', uid)} <b>Total Received:</b> <i>{stats['total']}</i>{ai}",
            reply_markup=main_menu_kb(),parse_mode="HTML")
        return

    if data=="buy_menu":
        await query.answer()
        logger.info(f"{emoji('rocket', uid)} Buy menu opened | UID: {uid} | Bot Type: {'CHILD' if IS_CHILD_BOT else 'MAIN'} | Child Bot Isolated: {IS_CHILD_BOT}")
        await show_buy_menu(query, uid)
        return

    if data=="virtual_web":
        await query.answer("Use the classic Telegram flow in this bot.", show_alert=True)
        await show_buy_menu(query, uid)
        return

    if data.startswith("vweb_country_page_"):
        await query.answer()
        await show_buy_menu(query, uid)
        return

    if data.startswith("vweb_country_pick_"):
        await query.answer()
        await show_buy_menu(query, uid)
        return

    if data.startswith("vweb_service_page_"):
        await query.answer()
        await show_buy_menu(query, uid)
        return

    if data.startswith("vweb_service_pick_"):
        await query.answer()
        await show_buy_menu(query, uid)
        return

    if data.startswith("vweb_service_pick_"):
        await query.answer()
        idx = int(data.rsplit("_", 1)[1])
        services = context.user_data.get("vweb_services") or []
        country = context.user_data.get("vweb_country") or get_number_flow_state(uid).get("country")
        flag = context.user_data.get("vweb_flag") or get_number_flow_state(uid).get("flag") or "🌍"
        if not country or idx < 0 or idx >= len(services):
            await show_virtual_country_menu(query, context, uid, page=1)
            return
        service = services[idx]
        await assign_virtual_number(query, context, uid, country=country, service=service, flag=flag)
        return

    if data.startswith("svc_"):
        svc=data[4:]; await query.answer()
        set_number_flow_state(uid, "classic", service=svc)
        logger.info(f"🛍️ Service selected | UID: {uid} | Service: {svc} | Bot Type: {'CHILD' if IS_CHILD_BOT else 'MAIN'} | Child Bot Isolated: {IS_CHILD_BOT}")
        countries=await db.get_countries_for_service(svc)
        if not countries:
            await query.edit_message_text(f"🚫 No countries for <b>{svc}</b>.",
                reply_markup=services_kb(await db.get_distinct_services()),parse_mode="HTML")
        else:
            await query.edit_message_text(f"🌍 <b>Select Country</b> — {svc}\n{D}",
                reply_markup=countries_kb(svc,countries),parse_mode="HTML")
        return

    if data.startswith("cntry|"):
        _,svc,country=data.split("|",2); await query.answer()
        logger.info(f"🌍 Country selected | UID: {uid} | Service: {svc} | Country: {country} | Bot Type: {'CHILD' if IS_CHILD_BOT else 'MAIN'}")
        await db.set_user_prefix(uid,None)
        clist=await db.get_countries_for_service(svc)
        flag=next((f for f,c in clist if c==country),"🌍")
        set_number_flow_state(uid, "classic", service=svc, country=country, flag=flag)
        category=f"{flag} {country} - {svc}"
        limit=await get_user_assign_count(uid)
        active=await db.get_active_numbers(uid)
        if active and active[0].category!=category: await db.release_number(uid); active=[]
        if len(active)<limit:
            await db.request_numbers(uid,category,count=limit-len(active))
            active=await db.get_active_numbers(uid)
        if active:
            try: await query.message.delete()
            except Exception: pass
            pfx=await db.get_user_prefix(uid)
            lines=[f"{i+1}\uFE0F\u20E3 <code>+{n.phone_number}</code>" for i,n in enumerate(active)]
            msg=await context.bot.send_message(chat_id=uid,
                text=(f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n🎉 <b>NUMBERS ASSIGNED!</b>\n<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
                      f"🌍 <b>Service:</b> {svc} {flag}\n📦 <b>Assigned:</b> {len(active)} number(s)\n\n"+"\n".join(lines)+
                      "\n\n⚡ <b>Waiting for SMS…</b>"),
                reply_markup=waiting_kb(pfx,service=svc),parse_mode="HTML")
            for n in active: await db.update_message_id(n.phone_number,msg.message_id)
        else:
            await context.bot.send_message(uid,
                text=f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n❌ <b>OUT OF STOCK</b>\n<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
                     f"🌍 Service: <b>{svc} {flag}</b>\n"
                     f"Country: <b>{country}</b>\n\n"
                     f"Please try another country or service.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="buy_menu", style="primary")]]),
                parse_mode="HTML")
        return

    if data=="change_service_menu":
        await query.answer()
        await show_buy_menu(query, uid)
        return

    if data=="change_country":
        active=await db.get_active_numbers(uid)
        if not active: await query.answer("🚫 No active number assigned.",show_alert=True); return
        svc=active[0].category.split(" - ")[1] if " - " in active[0].category else active[0].category
        countries=await db.get_countries_for_service(svc)
        if not countries: await query.answer("🌍 No other countries available.",show_alert=True); return
        await query.answer()
        await query.edit_message_text(f"🌍 <b>Select Country</b> — {svc}",
            reply_markup=countries_kb(svc,countries),parse_mode="HTML")
        return

    if data=="skip_next":
        now_=datetime.now(); last=LAST_CHANGE_TIME.get(uid)
        if last and (now_-last).total_seconds()<CHANGE_COOLDOWN_S:
            await query.answer(f"⏳ Wait {CHANGE_COOLDOWN_S-int((now_-last).total_seconds())}s",show_alert=True); return
        LAST_CHANGE_TIME[uid]=now_; await query.answer()
        logger.info(f"🔄 Number skip requested | UID: {uid} | Bot Type: {'CHILD' if IS_CHILD_BOT else 'MAIN'}")
        ok,cat=await db.release_number(uid)
        if ok and cat:
            limit=await get_effective_limit(uid)
            await db.request_numbers(uid,cat,count=limit)
            active=await db.get_active_numbers(uid)
            if active:
                logger.info(f"✅ New numbers assigned | UID: {uid} | Count: {len(active)} | Service: {active[0].category if active else 'N/A'}")
                try: await query.message.delete()
                except Exception: pass
                svc=active[0].category.split(" - ")[1] if " - " in active[0].category else cat
                pfx=await db.get_user_prefix(uid)
                lines=[f"{i+1}. <code>+{n.phone_number}</code>" for i,n in enumerate(active)]
                msg=await context.bot.send_message(chat_id=uid,
                    text=f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n🔄 <b>NEW NUMBERS</b>\n<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"+"\n".join(lines)+"\n\n⚡ <b>Waiting for SMS…</b>",
                    reply_markup=waiting_kb(pfx,service=svc),parse_mode="HTML")
                for n in active: await db.update_message_id(n.phone_number,msg.message_id)
            else:
                logger.warning(f"❌ Out of stock | UID: {uid} | Service: {cat}")
                await query.edit_message_text(
                    f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n❌ <b>OUT OF STOCK</b>\n<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
                    f"All numbers for this service are currently unavailable.\n\n"
                    f"Try another service or check back later!",
                    reply_markup=main_menu_kb(),parse_mode="HTML")
        else: await query.answer("🚫 No active number assigned.",show_alert=True)
        return

    if data=="ask_block":
        await query.answer()
        await query.edit_message_text(
            f"⚠️ <b>Block This Number?</b>\n{D}\nPermanently removed — no one can use it again.",
            reply_markup=confirm_block_kb(),parse_mode="HTML")
        return

    if data=="block_no":
        await query.answer()
        active=await db.get_active_numbers(uid)
        if active:
            pfx=await db.get_user_prefix(uid)
            svc=active[0].category.split(" - ")[1] if " - " in active[0].category else active[0].category
            lines=[f"{i+1}. <code>+{n.phone_number}</code>" for i,n in enumerate(active)]
            await query.edit_message_text(
                f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n{emoji('check', uid)} <b>KEPT ACTIVE</b>\n<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
                + "\n".join(lines) + f"\n\n{emoji('bolt', uid)} <b>Waiting for SMS…</b>",
                reply_markup=waiting_kb(pfx,service=svc),parse_mode="HTML")
        else:
            await query.edit_message_text(
                f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n{emoji('cancel', uid)} <b>NO ACTIVE NUMBER</b>\n<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
                f"You don't have an active number assigned yet.\n\n"
                f"Select a service to get your free number!",
                reply_markup=main_menu_kb(),parse_mode="HTML")
        return

    if data=="block_yes":
        await query.answer(); await query.edit_message_text(f"⏳ Blocking…", parse_mode="HTML")
        ok,cat=await db.block_number(uid)
        if ok and cat:
            svc=cat.split(" - ")[1] if " - " in cat else cat
            cntrs=await db.get_countries_for_service(svc)
            if cntrs:
                await query.edit_message_text(
                    f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n{emoji('check', uid)} <b>NUMBER BLOCKED</b>\n<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
                    f"Select a new country for <b>{svc}</b>:",
                    reply_markup=countries_kb(svc,cntrs),parse_mode="HTML")
            else:
                await query.edit_message_text(
                    f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n{emoji('check', uid)} <b>NUMBER BLOCKED</b>\n<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
                    f"Choose another service:",
                    reply_markup=services_kb(await db.get_distinct_services()),parse_mode="HTML")
        else:
            await query.edit_message_text(
                f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n{emoji('cancel', uid)} <b>ERROR</b>\n<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
                f"No active number to block.",
                reply_markup=main_menu_kb(),parse_mode="HTML")
        return

    if data=="set_prefix":
        await query.answer()
        active=await db.get_active_numbers(uid)
        if not active: await query.answer(f"{emoji('trash', uid)} No active number assigned.",show_alert=True); return
        cur=await db.get_user_prefix(uid)
        if cur:
            await db.set_user_prefix(uid,None); await query.answer(f"{emoji('check', uid)} Prefix disabled.")
            svc=active[0].category.split(" - ")[1] if " - " in active[0].category else active[0].category
            lines=[f"{i+1}. <code>+{n.phone_number}</code>" for i,n in enumerate(active)]
            await query.edit_message_text(
                f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n{emoji('bolt', uid)} <b>READY TO RECEIVE</b>\n<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
                + "\n".join(lines) + f"\n\n{emoji('phone', uid)} <b>Prefix:</b> OFF\n\nWaiting for SMS…",
                reply_markup=waiting_kb(None,service=svc),parse_mode="HTML")
        else:
            context.user_data["awaiting_prefix"]=True
            context.user_data["prefix_cat"]=active[0].category
            svc=active[0].category.split(" - ")[1] if " - " in active[0].category else active[0].category
            await context.bot.send_message(uid,
                f"{emoji('phone', uid)} <b>Set Prefix</b>\n{D}\nService: {svc}\n\n"
                "Type prefix (e.g. <code>9198</code>) or <code>off</code>:",parse_mode="HTML")
        return

    # ── Upload service selection ─────────────────────────
    if data.startswith("us_"):
        action=data[3:]
        if action=="done":
            await query.answer()
            sel=list(set(context.user_data.get("upload_svcs",[])))  # Ensure unique services
            path=context.user_data.get("upload_path")
            country=context.user_data.get("upload_country","Unknown")
            flag=context.user_data.get("upload_flag","🌍")
            if not sel: await query.answer("Select at least one service.",show_alert=True); return
            if not path or not os.path.exists(path): await query.edit_message_text(f"{ui('cancel')} <b>File lost. Re-upload.</b>", parse_mode="HTML"); return
            lines=[l.strip() for l in open(path).readlines() if l.strip()]
            if not lines: await query.edit_message_text(f"{ui('cancel')} <b>File is empty.</b>", parse_mode="HTML"); os.remove(path); return
            total_added=0
            # Add numbers to each selected service
            for svc in sel:
                cat=f"{flag} {country} - {svc}"
                added=await db.add_numbers_bulk(list(lines),cat)  # Pass copy of lines to ensure integrity
                total_added+=added
                logger.info(f"📝 Added {added} numbers to {cat}")
            os.remove(path); context.user_data.pop("upload_path",None)

            # ── Total stock after upload ──────────────────────────────
            total_stock = 0
            for svc in sel:
                cat = f"{flag} {country} - {svc}"
                count = await db.count_available(cat)
                total_stock += count

            await query.edit_message_text(
                f"{ui('check')} <b>Upload Complete</b>\n{D}\n📥 Added: <b>{total_added}</b>\n"
                f"📱 Services: {', '.join(sel)}\n{ui('earth')} {flag} {country}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{ui('back')}  Back",callback_data="admin_files", style="primary")]]),
                parse_mode="HTML")

            # ── Auto-broadcast (only if enabled in settings) ─────────
            if not AUTO_BROADCAST_ON:
                return
            nb_url  = NUMBER_BOT_LINK or GET_NUMBER_URL or f"https://t.me/{BOT_USERNAME}"
            svc_str = "  ".join(sel)
            bcast_msg = (
                f"{ui('bell')} <b>New Numbers Added!</b>\n"
                f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
                f"{ui('earth')} <b>Country:</b> {flag} {country}\n"
                f"{ui('settings')} <b>Services:</b> {svc_str}\n"
                f"📞 <b>Fresh Numbers:</b> {total_added:,}\n"
                f"{ui('package')} <b>Total Stock:</b> {total_stock:,}\n"
                f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
                f"{ui('rocket')} Start the bot and tap <b>Get Number</b> now!"
            )
            bcast_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🧇  Get Number", url=nb_url, style="primary")
            ]])

            # Send to log groups
            for gid in await db.get_all_log_chats():
                try:
                    await context.bot.send_message(
                        chat_id=gid, text=bcast_msg,
                        reply_markup=bcast_kb, parse_mode="HTML")
                except Exception: pass

            # Send to all registered users (background task)
            async def _bcast_users():
                users = await db.get_all_users()
                sent = 0
                for u_id in users:
                    try:
                        await context.bot.send_message(
                            chat_id=u_id, text=bcast_msg,
                            reply_markup=bcast_kb, parse_mode="HTML")
                        sent += 1
                    except Exception:
                        pass
                    # 0.05s sleep = max 20 msgs/sec, well under Telegram 30/sec limit
                    # asyncio.sleep yields control so all other handlers stay responsive
                    await asyncio.sleep(0.05)
                logger.info("📢 Auto-broadcast sent to %d/%d users" % (sent, len(users)))
            # Fire and forget — bot stays fully responsive during broadcast
            asyncio.create_task(_bcast_users())
        elif action=="cancel":
            await query.answer()
            path=context.user_data.pop("upload_path",None)
            if path and os.path.exists(path): os.remove(path)
            await query.edit_message_text(
                f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n❌ <b>UPLOAD CANCELLED</b>\n<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
                f"The file upload has been cancelled.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="admin_home", style="primary")]]), parse_mode="HTML")
        else:
            sel=context.user_data.get("upload_svcs",[])
            if action in sel: sel.remove(action)
            else: sel.append(action)
            context.user_data["upload_svcs"]=sel
            await query.edit_message_reply_markup(reply_markup=svc_sel_kb(sel)); await query.answer()
        return

    # ── ADMIN SECTION ─────────────────────────────────────
    perms=await get_admin_permissions(uid); is_sup=is_super_admin(uid)

    if data=="admin_home":
        await query.answer()
        context.user_data["awaiting_broadcast"]=False
        context.user_data.pop("awaiting_website_setting", None)
        role="👑 Super Admin" if is_sup else "👮 Admin"
        s2=await db.get_stats(); avail=s2.get("available",0)
        online=len([p for p in PANELS if p.is_logged_in or
                    (p.panel_type=="ivas" and p.name in IVAS_TASKS
                     and not IVAS_TASKS[p.name].done())])
        await query.edit_message_text(
            f"<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n"
            f"🛡 <b>ADMIN PANEL</b>  ·  {role}\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n"
            f"📱 {avail} numbers  🔌 {online}/{len(PANELS)} panels",
            reply_markup=admin_main_kb(perms,is_sup),parse_mode="HTML")
        return

    # ── OTP Tools submenu ──────────────────────────────────────────
    if data=="admin_otp_tools":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        store=load_otp_store()
        await query.edit_message_text(
            f"<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n"
            f"🔑 <b>OTP TOOLS</b>\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            f"💾 <b>Stored OTPs:</b> <i>{len(store)}</i>\n\n"
            f"Manage your OTP history and settings.",
            reply_markup=admin_otp_tools_kb(),parse_mode="HTML")
        return

    # ── Notify / Broadcast menu ────────────────────────────────────
    if data=="admin_notify_menu":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        chats=await db.get_all_log_chats()
        users=await db.get_all_users()
        await query.edit_message_text(
            f"<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n"
            f"🔔 <b>NOTIFY &amp; BROADCAST</b>\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            f"👤 <b>Users:</b> <i>{len(users)}</i>\n"
            f"📋 <b>Log Groups:</b> <i>{len(chats)}</i>\n\n"
            f"Send announcements to users and log groups.",
            reply_markup=admin_notify_kb(),parse_mode="HTML")
        return

    if data=="ping_log_groups":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        chats=await db.get_all_log_chats()
        ok=0; fail=0
        for gid in chats:
            try:
                await context.bot.send_message(gid,f"📡 <b>Sigma Fetcher — Panel Online {ui('check')}</b>",parse_mode="HTML")
                ok+=1
            except Exception: fail+=1
        await query.answer(f"✅ Pinged {ok} groups, ❌ {fail} failed",show_alert=True)
        return

    if data=="send_test_otp":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        fake_otp=str(random.randint(100000,999999))
        await query.answer()
        bot_tag=f"@{BOT_USERNAME}" if BOT_USERNAME else "@CrackSMSReBot"
        now_ts=datetime.now().strftime("%H:%M:%S")
        test_txt=(
            f"<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n"
            f"  ✅ OTP RECEIVED  ·  1️⃣ First OTP\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            f"🤖 <b>{html.escape(bot_tag)}</b>   ⏰ <code>{now_ts}</code>\n\n"
            f"┌─────────────────────────┐\n"
            f"│  🔑  <b>OTP CODE</b>\n"
            f"│  <code>{fake_otp}</code>\n"
            f"└─────────────────────────┘\n\n"
            f"📱  <code>+92-𝗦𝗜𝗚𝗠𝗔-12345</code>   🇵🇰 #PK\n"
            f"📡  <b>Service:</b> #TG\n"
            f"🔌  <b>Panel:</b>   TEST\n\n"
            f"💬  <i>Your Telegram code: {fake_otp}. Do not share.</i>"
        )
        kb=otp_keyboard(fake_otp,"Your Telegram code: "+fake_otp,for_group=False)
        await context.bot.send_message(uid,test_txt,reply_markup=kb,parse_mode="HTML")
        await query.edit_message_text(f"{ui('check')} Test OTP sent to your DM.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{ui('back')}  Back",callback_data="admin_notify_menu", style="primary")]]), parse_mode="HTML")
        return

    if data=="find_otp_prompt":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        context.user_data["awaiting_link"]="FIND_OTP"
        await query.answer()
        await query.edit_message_text(
            "🔍 <b>Find OTP by Number</b>\n\nSend the phone number to search:",
            parse_mode="HTML")
        return

    # ── Numbers submenu ──────────────────────────────────────
    if data=="admin_numbers":
        if "manage_files" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        cats=await db.get_categories_summary()
        if not cats:
            await query.edit_message_text(
                f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n📂 <b>NUMBERS MANAGER</b>\n<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
                f"❌ <b>No numbers uploaded</b>\n\n"
                f"📤 Upload a <code>.txt</code> file with one number per line.\n"
                f"Supported format: Each line contains one phone number.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙  Back",callback_data="admin_home", style="primary")]]),
                parse_mode="HTML")
        else:
            s=await db.get_stats()
            await query.edit_message_text(
                f"📂 <b>Numbers Manager</b>\n{D}\n"
                f"🟢 Available: <b>{s.get('available',0)}</b>  |  "
                f"🔴 In Use: <b>{s.get('assigned',0)}</b>\n"
                f"🧊 Cooldown: <b>{s.get('cooldown',0)}</b>  |  "
                f"✅ Used: <b>{s.get('used',0)}</b>",
                reply_markup=admin_numbers_kb(cats),parse_mode="HTML")
        return

    if data=="admin_upload_info":
        if "manage_files" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        await query.edit_message_text(
            f"📤 <b>Upload Numbers</b>\n{D}\n"
            "Send a <b>.txt file</b> in this chat.\n\n"
            "Format: one phone number per line\n"
            "<code>923001234567</code>\n"
            "<code>923000767749</code>\n\n"
            "The bot will auto-detect country and ask for services.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙  Back",callback_data="admin_numbers", style="primary")]]),
            parse_mode="HTML")
        return

    if data.startswith("cat_stats_"):
        if "manage_files" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        sid=data[10:]; cat=CATEGORY_MAP.get(sid)
        if not cat: await query.answer("Expired. Reopen Numbers.",show_alert=True); return
        await query.answer()
        async with db.AsyncSessionLocal() as session:
            # sfunc = func (alias at module level)
            statuses=["AVAILABLE","ASSIGNED","RETENTION","USED","BLOCKED"]
            lines=[]
            for st in statuses:
                cnt=await session.scalar(
                    select(sfunc.count(db.Number.id)).where(
                        db.Number.category==cat,db.Number.status==st)) or 0
                if cnt>0:
                    icons={"AVAILABLE":"🟢","ASSIGNED":"🔴","RETENTION":"🧊","USED":"✅","BLOCKED":"🚫"}
                    lines.append(f"{icons[st]} {st}: <b>{cnt}</b>")
        await query.edit_message_text(
            f"📊 <b>Category Stats</b>\n{D}\n"
            f"<b>{html.escape(cat)}</b>\n\n"+("\n".join(lines) or "Empty"),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙  Back",callback_data="admin_numbers", style="primary")]]),
            parse_mode="HTML")
        return

    if data=="purge_used":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        await query.edit_message_text("⚠️ Delete ALL <b>USED</b> numbers permanently?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅  Yes Purge",callback_data="confirm_purge_used", style="success"),
                InlineKeyboardButton("❌  Cancel",   callback_data="admin_numbers", style="danger"),
            ]]),parse_mode="HTML")
        return

    if data=="confirm_purge_used":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        async with db.AsyncSessionLocal() as session:
            r=await session.execute(stext("DELETE FROM numbers WHERE status='USED'"))
            await session.commit(); n=r.rowcount
        await query.answer(f"✅ Purged {n} used numbers.",show_alert=True)
        cats=await db.get_categories_summary()
        await query.edit_message_text(f"📂 <b>Numbers Manager</b>\n{D}",
            reply_markup=admin_numbers_kb(cats),parse_mode="HTML")
        return

    if data=="purge_blocked":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        async with db.AsyncSessionLocal() as session:
            r=await session.execute(stext("DELETE FROM numbers WHERE status='BLOCKED'"))
            await session.commit(); n=r.rowcount
        await query.answer(f"✅ Purged {n} blocked numbers.",show_alert=True)
        cats=await db.get_categories_summary()
        await query.edit_message_text(f"📂 <b>Numbers Manager</b>\n{D}",
            reply_markup=admin_numbers_kb(cats),parse_mode="HTML")
        return

    # ── Stats submenu ─────────────────────────────────────────
    if data=="admin_stats_menu":
        if "view_stats" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        await query.edit_message_text(
            f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n📊 <b>STATISTICS</b>\n<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            f"View detailed statistics about your bot.",
            reply_markup=admin_stats_menu_kb(),parse_mode="HTML")
        return

    if data=="admin_db_summary":
        if "view_stats" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        users=await db.get_all_users(); logs=await db.get_all_log_chats()
        s=await db.get_stats()
        total_n=sum(s.values())
        active=[p for p in PANELS if p.is_logged_in or (p.panel_type=="ivas" and p.name in IVAS_TASKS and not IVAS_TASKS[p.name].done())]
        await query.edit_message_text(
            f"💾 <b>Database Summary</b>\n{D}\n"
            f"👤 Users:       <b>{len(users)}</b>\n"
            f"📋 Log Groups:  <b>{len(logs)}</b>\n"
            f"🔌 Panels:      <b>{len(PANELS)}</b>  (active: {len(active)})\n"
            f"📱 Numbers:     <b>{total_n}</b>\n"
            f"🟢 Available:   <b>{s.get('available',0)}</b>\n"
            f"🔴 In Use:      <b>{s.get('assigned',0)}</b>\n"
            f"🧊 Cooldown:    <b>{s.get('cooldown',0)}</b>\n"
            f"✅ Used:        <b>{s.get('used',0)}</b>\n"
            f"🚫 Blocked:     <b>{s.get('blocked',0)}</b>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙  Back",callback_data="admin_stats_menu", style="primary")]]),
            parse_mode="HTML")
        return

    if data=="admin_otp_history":
        if "view_stats" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        async with db.AsyncSessionLocal() as session:
            rows=(await session.execute(
                stext("SELECT phone_number,otp,category,created_at FROM history "
                      "ORDER BY created_at DESC LIMIT 10")
            )).fetchall()
        if not rows:
            await query.edit_message_text("📈 No OTP history yet.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙  Back",callback_data="admin_stats_menu", style="primary")]]))
            return
        lines=[]
        for row in rows:
            ts=str(row[3])[:16] if row[3] else "?"
            lines.append(f"📱 <code>{mask_number(str(row[0]))}</code>  🔑 <code>{row[1]}</code>  ⏰ {ts}")
        await query.edit_message_text(
            f"📈 <b>Last 10 OTP Deliveries</b>\n{D}\n"+"\n".join(lines),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙  Back",callback_data="admin_stats_menu", style="primary")]]),
            parse_mode="HTML")
        return

    # ── Users submenu ─────────────────────────────────────────
    if data=="admin_users":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        await query.edit_message_text(f"👤 <b>User Manager</b>\n{D}",
            reply_markup=admin_users_kb(),parse_mode="HTML")
        return

    if data=="admin_list_users":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        all_u=await db.get_all_users()
        lines=[]
        for u in all_u[:25]:
            stats=await db.get_user_stats(u)
            crown="👑 " if u in INITIAL_ADMIN_IDS else ""
            lines.append(f"{crown}<code>{u}</code>  ✅{stats['success']}")
        more="" if len(all_u)<=25 else f"\n<i>…and {len(all_u)-25} more</i>"
        await query.edit_message_text(
            f"👤 <b>All Users ({len(all_u)})</b>\n{D}\n"
            +("\n".join(lines) or "None")+more,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙  Back",callback_data="admin_users", style="primary")]]),
            parse_mode="HTML")
        return

    if data=="set_user_limit_help":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        await safe_edit(
            query,
            f"{ui('focus')} <b>Set User Limit</b>\n{D}\n"
            f"Use:\n<code>/setuserlimit user_id limit</code>\n\n"
            f"{ui('copy')} Example:\n<code>/setuserlimit 123456789 3</code>",
            reply_markup=kb([btn("Back", cbd="admin_users", style="primary", icon=_UI["back"][0])]),
            parse_mode="HTML",
        )
        return

    # ── Maintenance submenu ───────────────────────────────────
    if data=="admin_maintenance":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        await query.edit_message_text(f"🧹 <b>Maintenance</b>\n{D}",
            reply_markup=admin_maintenance_kb(),parse_mode="HTML")
        return

    if data=="reload_countries":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        load_countries()
        await query.answer(f"✅ Reloaded {len(COUNTRY_DATA)} countries.",show_alert=True)
        return

    if data=="login_all_panels":
        if "manage_panels" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer("🔄 Logging in to all panels…")
        ok=0; fail=0
        for p in PANELS:
            if p.panel_type=="login":
                if await login_to_panel(p): ok+=1
                else: fail+=1
            elif p.panel_type=="api":
                if await test_api_panel(p): p.is_logged_in=True; ok+=1
                else: fail+=1
        await refresh_panels_from_db()
        await query.edit_message_text(
            f"🔄 <b>Login All Panels</b>\n{D}\n✅ OK: {ok}  |  ❌ Failed: {fail}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙  Back",callback_data="admin_panel_manager", style="primary")]]),
            parse_mode="HTML")
        return

    # ── Settings extras ────────────────────────────────────────
    if data=="change_token_prompt":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        context.user_data["awaiting_link"]="BOT_TOKEN"
        await query.edit_message_text(
            f"⚠️ <b>Change Bot Token</b>\n{D}\n"
            "Send the new bot token.\nThe bot will need to be restarted after this.\n\n"
            "/cancel to abort.",parse_mode="HTML")
        return

    if data=="set_developer_prompt":
        context.user_data["awaiting_link"]="DEVELOPER"
        await query.edit_message_text("🧠 Send the new Developer username (@username):", parse_mode="HTML")
        return

    if data in ("pt_login","pt_api","pt_api_v2","pt_ivas"):
        if "manage_panels" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        if uid not in PANEL_ADD_STATES: await query.answer("No pending addition."); return
        ptype=data[3:]
        PANEL_ADD_STATES[uid]["data"]["panel_type"]=ptype
        if ptype=="ivas":
            PANEL_ADD_STATES[uid]["step"]="confirm_uri"
            await query.edit_message_text("📡 <b>IVAS Panel</b>\nUse default URI or enter custom?",parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Use Default",callback_data="pt_ivas_default", style="success")],
                    [InlineKeyboardButton("✏️ Custom URI", callback_data="pt_ivas_custom", style="primary")],
                    [InlineKeyboardButton("❌ Cancel",     callback_data="cancel_action", style="danger")],
                ]))
        else:
            PANEL_ADD_STATES[uid]["step"]="url"
            prompts={"login":"Enter Base URL (http://…):","api":"Enter API endpoint URL:"}
            await query.edit_message_text(prompts[ptype],parse_mode="HTML")
        return

    if data=="pt_ivas_default":
        if uid not in PANEL_ADD_STATES: await query.answer("No pending addition."); return
        name=PANEL_ADD_STATES[uid]["data"]["name"]
        await add_panel_to_db(name,"",None,None,"ivas",uri=DEFAULT_IVAS_URI)
        await refresh_panels_from_db()
        panel=next((p for p in PANELS if p.name==name),None)
        if panel:
            task=asyncio.create_task(ivas_worker(panel),name=f"IVAS-{name}")
            task.add_done_callback(handle_task_exception); IVAS_TASKS[name]=task
        del PANEL_ADD_STATES[uid]
        await query.edit_message_text(f"{ui('check')} IVAS panel added (default URI) and worker started!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="admin_panel_manager", style="primary")]]), parse_mode="HTML")
        return

    if data=="pt_ivas_custom":
        if uid in PANEL_ADD_STATES: PANEL_ADD_STATES[uid]["step"]="uri"
        await query.edit_message_text("Paste the custom IVAS URI (wss://…):", parse_mode="HTML")
        return

    if data=="admin_stats":
        if "view_stats" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer(); s=await db.get_stats()
        pi="\n".join(f"  {'🟢' if p.is_logged_in else '🔴'} {p.name} [{p.panel_type.upper()}]" for p in PANELS) or "  None"
        await query.edit_message_text(
            f"📊 <b>Live Stats</b>\n{D}\n"
            f"📦 Total:     <b>{s.get('available',0)+s.get('assigned',0)+s.get('cooldown',0)+s.get('used',0)+s.get('blocked',0)}</b>\n"
            f"🟢 Available: <b>{s.get('available',0)}</b>\n"
            f"🔴 In Use:    <b>{s.get('assigned',0)}</b>\n"
            f"🧊 Cooldown:  <b>{s.get('cooldown',0)}</b>\n"
            f"✅ Used:      <b>{s.get('used',0)}</b>\n"
            f"🚫 Blocked:   <b>{s.get('blocked',0)}</b>\n\n"
            f"🔌 <b>Panels:</b>\n{pi}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="admin_home", style="primary")]]),
            parse_mode="HTML")
        return

    if data=="admin_reset":
        if "view_stats" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        n=await db.clean_cooldowns(); await query.answer(f"✅ {n} numbers released.",show_alert=True); return

    if data in ("admin_files","admin_numbers"):
        if "manage_files" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        cats=await db.get_categories_summary()
        s=await db.get_stats()
        if not cats:
            await query.edit_message_text(
                f"📂 <b>Numbers Manager</b>\n{D}\n"
                "No numbers uploaded yet.\n\n"
                "📤 Send a <code>.txt</code> file with one number per line.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙  Back",callback_data="admin_home", style="primary")]]),
                parse_mode="HTML")
        else:
            await query.edit_message_text(
                f"📂 <b>Numbers Manager</b>\n{D}\n"
                f"🟢 Available: <b>{s.get('available',0)}</b>  "
                f"🔴 In Use: <b>{s.get('assigned',0)}</b>  "
                f"🧊 Cooldown: <b>{s.get('cooldown',0)}</b>",
                reply_markup=admin_numbers_kb(cats),parse_mode="HTML")
        return

    if data.startswith("del_"):
        if "manage_files" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        sid=data[4:]; cat=CATEGORY_MAP.get(sid)
        if not cat: await query.edit_message_text("❌ Expired menu. Reopen File Manager.", parse_mode="HTML"); return
        await db.delete_category(cat)
        cats=await db.get_categories_summary()
        if not cats:
            await query.edit_message_text("📂 All files deleted.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="admin_home", style="primary")]]))
        else:
            await query.edit_message_text(f"{ui('check')} Deleted.\n\n📂 <b>File Manager</b>",
                reply_markup=files_kb(cats),parse_mode="HTML")
        return

    if data=="admin_broadcast":
        if "broadcast" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        context.user_data["awaiting_broadcast"]=True
        await query.edit_message_text(
            f"📢 <b>Broadcast Mode</b>\n{D}\n"
            "Type your announcement and send it.\nDelivered to <b>all registered users</b>.",
            parse_mode="HTML")
        return

    if data=="admin_panel_manager":
        if "manage_panels" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.edit_message_text(f"🔌 <b>Panel Manager</b>\n{D}",reply_markup=panel_mgr_kb(),parse_mode="HTML")
        return

    # ── Panel Manager Buttons ────────────────────────────────────
    if data=="panel_add":
        if "manage_panels" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        PANEL_ADD_STATES[uid]={"step":"name","data":{}}; await query.answer()
        await query.edit_message_text(f"➕ <b>Add Panel</b>\n{D}\nStep 1 — Enter panel name:",parse_mode="HTML")
        return
    
    if data=="panel_list_all":
        if "manage_panels" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await refresh_panels_from_db()
        if not PANELS:
            await query.edit_message_text(f"📋 <b>All Panels</b>\n{D}\nNo panels configured.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add Panel",callback_data="panel_add", style="success")],
                    [InlineKeyboardButton("🔙 Back",callback_data="admin_panel_manager", style="primary")]]),parse_mode="HTML")
            return
        lines = []
        for p in sorted(PANELS, key=lambda x: x.panel_type):
            st = "🟢" if p.is_logged_in else "🔴"
            lines.append(f"{st} <b>{html.escape(p.name)}</b> [{p.panel_type.upper()}]")
        await query.edit_message_text(f"📋 <b>All Panels ({len(PANELS)})</b>\n{D}\n" + "\n".join(lines),
            reply_markup=panel_list_kb(PANELS, "all"), parse_mode="HTML")
        return
    
    if data=="panel_list_login":
        if "manage_panels" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await refresh_panels_from_db()
        pl = [p for p in PANELS if p.panel_type == "login"]
        if not pl:
            await query.edit_message_text(f"🔑 <b>Login Panels</b>\n{D}\nNone yet.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add Panel",callback_data="panel_add", style="success")],
                    [InlineKeyboardButton("🔙 Back",callback_data="admin_panel_manager", style="primary")]]),parse_mode="HTML")
            return
        lines = [f"{'🟢' if p.is_logged_in else '🔴'} <b>{html.escape(p.name)}</b>" for p in pl]
        await query.edit_message_text(f"🔑 <b>Login Panels ({len(pl)})</b>\n{D}\n" + "\n".join(lines),
            reply_markup=panel_list_kb(pl, "login"), parse_mode="HTML")
        return
    
    if data=="panel_list_api":
        if "manage_panels" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await refresh_panels_from_db()
        pl = [p for p in PANELS if p.panel_type == "api"]
        if not pl:
            await query.edit_message_text(f"🔌 <b>API Panels</b>\n{D}\nNone yet.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add Panel",callback_data="panel_add", style="success")],
                    [InlineKeyboardButton("🔙 Back",callback_data="admin_panel_manager", style="primary")]]),parse_mode="HTML")
            return
        lines = [f"{'🟢' if p.is_logged_in else '🔴'} <b>{html.escape(p.name)}</b>" for p in pl]
        await query.edit_message_text(f"🔌 <b>API Panels ({len(pl)})</b>\n{D}\n" + "\n".join(lines),
            reply_markup=panel_list_kb(pl, "api"), parse_mode="HTML")
        return
    
    if data=="panel_list_ivas":
        if "manage_panels" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await refresh_panels_from_db()
        pl = [p for p in PANELS if p.panel_type == "ivas"]
        if not pl:
            await query.edit_message_text(f"📡 <b>IVAS Panels</b>\n{D}\nNone yet.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add Panel",callback_data="panel_add", style="success")],
                    [InlineKeyboardButton("🔙 Back",callback_data="admin_panel_manager", style="primary")]]),parse_mode="HTML")
            return
        lines = []
        for p in pl:
            st = "🟢" if (p.name in IVAS_TASKS and not IVAS_TASKS[p.name].done()) else "🔴"
            lines.append(f"{st} <b>{html.escape(p.name)}</b>")
        await query.edit_message_text(f"📡 <b>IVAS Panels ({len(pl)})</b>\n{D}\n" + "\n".join(lines),
            reply_markup=panel_list_kb(pl, "ivas"), parse_mode="HTML")
        return
    
    if data=="panel_reloginall":
        if "manage_panels" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer("🔄 Re-logging into all panels...")
        ok = 0; fail = 0
        for p in PANELS:
            if p.panel_type == "login":
                if await login_to_panel(p): ok+=1
                else: fail+=1
            elif p.panel_type == "api":
                if await test_api_panel(p): p.is_logged_in = True; ok+=1
                else: fail+=1
        await refresh_panels_from_db()
        await query.edit_message_text(
            f"✅ <b>Re-login Complete</b>\n{D}\n"
            f"✅ Success: {ok}\n"
            f"❌ Failed: {fail}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="admin_panel_manager", style="primary")]]),
            parse_mode="HTML")
        return
    
    if data=="panel_loaddex":
        if "manage_panels" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        await load_panels_from_dex_to_db()
        await query.edit_message_text(
            f"📥 <b>Load DEX Complete</b>\n{D}\n"
            f"✅ Panels loaded from dex.txt\n"
            f"Current panels: {len(PANELS)}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="admin_panel_manager", style="primary")]]),
            parse_mode="HTML")
        return

    if data in ("panels_login","panels_api","panels_ivas"):
        if "manage_panels" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await refresh_panels_from_db(); ptype=data.split("_")[1]
        pl=[p for p in PANELS if p.panel_type==ptype]
        icons={"login":"🔑","api":"🔌","ivas":"📡"}; labels={"login":"Login","api":"API","ivas":"IVAS"}
        if not pl:
            await query.edit_message_text(f"{icons[ptype]} <b>{labels[ptype]} Panels</b>\n\nNone yet.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add Panel",callback_data="p_add", style="success")],
                    [InlineKeyboardButton("🔙 Back",callback_data="admin_panel_manager", style="primary")]]),parse_mode="HTML")
            return
        lines=[]
        for p in pl:
            if ptype=="ivas": st="🟢" if (p.name in IVAS_TASKS and not IVAS_TASKS[p.name].done()) else "🔴"
            else: st="🟢" if p.is_logged_in else "🔴"
            lines.append(f"{st} <b>{html.escape(p.name)}</b>")
        await query.edit_message_text(f"{icons[ptype]} <b>{labels[ptype]} Panels</b>\n{D}\n"+"\n".join(lines),
            reply_markup=panel_list_kb(pl,ptype),parse_mode="HTML")
        return

    if data=="p_add":
        if "manage_panels" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        PANEL_ADD_STATES[uid]={"step":"name","data":{}}; await query.answer()
        await query.edit_message_text(f"➕ <b>Add Panel</b>\n{D}\nStep 1 — Enter panel name:",parse_mode="HTML")
        return

    if data.startswith("p_test_"):
        if "manage_panels" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        pid=int(data.split("_")[-1]); panel=next((p for p in PANELS if p.id==pid),None)
        if not panel: await query.answer("Not found",show_alert=True); return
        await query.answer("🔄 Testing…")
        if panel.panel_type=="login":
            ok=await login_to_panel(panel)
            await update_panel_login(pid,panel.sesskey if ok else None,panel.api_url if ok else None,ok)
            result=f"{'✅ OK' if ok else '❌ FAILED'}\n{panel.base_url}"
        elif panel.panel_type=="api":
            ok=await test_api_panel(panel); panel.is_logged_in=ok
            await update_panel_login(pid,None,panel.base_url if ok else None,ok)
            result=f"{'✅ API OK' if ok else '❌ API FAILED'}\n{panel.base_url}"
        else:
            running=panel.name in IVAS_TASKS and not IVAS_TASKS[panel.name].done()
            result=f"{'✅ Running' if running else '❌ Stopped'}"
        await refresh_panels_from_db()
        back_cb={"login":"panels_login","api":"panels_api","ivas":"panels_ivas"}.get(panel.panel_type,"admin_panel_manager")
        await query.edit_message_text(f"<b>Test: {html.escape(panel.name)}</b>\n{D}\n{result}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data=back_cb, style="primary")]]))
        return

    if data.startswith("p_info_"):
        if "manage_panels" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        pid=int(data.split("_")[-1]); panel=next((p for p in PANELS if p.id==pid),None)
        if not panel: await query.answer("Not found",show_alert=True); return
        st="🟢 Online" if panel.is_logged_in else "🔴 Offline"
        info=f"🔍 <b>{html.escape(panel.name)}</b>\n{D}\n🆔 {panel.id} | {panel.panel_type.upper()} | {st}\n\n"
        if panel.panel_type=="login":
            info+=(f"🔗 <code>{html.escape(panel.base_url)}</code>\n"
                   f"👤 <code>{html.escape(panel.username or '')}</code>\n"
                   f"📡 API: <code>{html.escape(panel.api_url or 'N/A')}</code>")
        elif panel.panel_type=="api":
            info+=f"🌐 <code>{html.escape(panel.base_url)}</code>\n🪙 Token: {'✅' if panel.token else '❌'}"
        else:
            uri_=((panel.uri or "")[:80]+"…") if panel.uri and len(panel.uri)>80 else (panel.uri or "")
            running=panel.name in IVAS_TASKS and not IVAS_TASKS[panel.name].done()
            info+=(f"📡 <code>{html.escape(uri_)}</code>\n"
                   f"⚙️ {'🟢 Running' if running else '🔴 Stopped'}")
        back_cb={"login":"panels_login","api":"panels_api","ivas":"panels_ivas"}.get(panel.panel_type,"admin_panel_manager")
        await query.answer()
        try:
            await query.edit_message_text(info,parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Test",callback_data=f"p_test_{pid}", style="primary"),
                     InlineKeyboardButton("✏️ Edit",callback_data=f"p_edit_{pid}", style="primary")],
                    [InlineKeyboardButton("🔙 Back",callback_data=back_cb, style="primary")],
                ]))
        except TelegramBadRequest: pass
        return

    if data.startswith("p_edit_"):
        if "manage_panels" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        pid=int(data.split("_")[-1]); panel=next((p for p in PANELS if p.id==pid),None)
        if not panel: await query.answer("Not found",show_alert=True); return
        PANEL_EDIT_STATES[uid]={"step":"name","panel_id":pid,
            "data":{"name":panel.name,"base_url":panel.base_url,"username":panel.username,
                    "password":panel.password,"panel_type":panel.panel_type,"token":panel.token,"uri":panel.uri}}
        await query.answer()
        await query.edit_message_text(f"✏️ <b>Edit: {html.escape(panel.name)}</b>\n\nCurrent: <code>{html.escape(panel.name)}</code>\nNew name (/skip):",parse_mode="HTML")
        return

    if data.startswith("p_del_") and not data.endswith("confirm"):
        if "manage_panels" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        pid=int(data.split("_")[-1]); context.user_data["confirm_del_panel"]=pid
        p=next((x for x in PANELS if x.id==pid),None)
        await query.answer()
        await query.edit_message_text(f"⚠️ Delete panel <b>{html.escape(p.name if p else str(pid))}</b>?",
            reply_markup=confirm_del_panel_kb(),parse_mode="HTML")
        return

    if data=="p_del_confirm":
        if "manage_panels" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        pid=context.user_data.pop("confirm_del_panel",None)
        if pid:
            p=next((x for x in PANELS if x.id==pid),None)
            if p:
                if p.panel_type=="ivas" and p.name in IVAS_TASKS:
                    IVAS_TASKS[p.name].cancel(); IVAS_TASKS.pop(p.name,None)
                await p.close()
            await delete_panel_from_db(pid); await refresh_panels_from_db()
            await query.answer("✅ Deleted.")
        await query.edit_message_text(f"🔌 <b>Panel Manager</b>\n{D}",reply_markup=panel_mgr_kb(),parse_mode="HTML")
        return

    if data=="admin_manage_logs":
        if "manage_logs" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        chats=await db.get_all_log_chats()
        await query.edit_message_text(f"📋 <b>Log Groups</b>\n{D}\nTotal: <b>{len(chats)}</b>",
            reply_markup=logs_kb(chats),parse_mode="HTML")
        return

    if data.startswith("rm_log_"):
        if "manage_logs" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        cid=int(data.split("_")[-1]); ok=await db.remove_log_chat(cid)
        await query.answer(f"{'✅ Removed' if ok else '❌ Not found'}: {cid}")
        chats=await db.get_all_log_chats()
        await query.edit_message_text(f"📋 <b>Log Groups</b>\n{D}",reply_markup=logs_kb(chats),parse_mode="HTML")
        return

    if data=="add_log_prompt":
        if "manage_logs" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        AWAITING_LOG_ID[uid]=True
        await query.edit_message_text("📋 <b>Add Log Group</b>\n\nSend the numeric chat ID.\n(/cancel to abort)",parse_mode="HTML")
        return

    if data=="admin_manage_admins":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        admins = await list_all_admins()
        sup_count = len([a for a in admins if a in INITIAL_ADMIN_IDS])
        await safe_edit(query,
            f"👥 <b>Admin Manager</b>\n{D}\n"
            f"👑 Super Admins: <b>{sup_count}</b>\n"
            f"👮 Regular Admins: <b>{len(admins)-sup_count}</b>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("All Admins", callback_data="admin_list_admins_view", style="primary", icon_custom_emoji_id=_UI["people"][0]),
                 InlineKeyboardButton("Add Super Admin", callback_data="add_superadmin_prompt", style="primary", icon_custom_emoji_id=_UI["crown"][0])],
                [InlineKeyboardButton("Add Regular Admin", callback_data="add_admin_prompt", style="success", icon_custom_emoji_id=_UI["bell"][0])],
                [InlineKeyboardButton("Back", callback_data="admin_home", style="primary", icon_custom_emoji_id=_UI["back"][0])],
            ]))
        return

    if data == "admin_list_admins_view":
        if not is_sup:
            await query.answer("Unauthorized", show_alert=True)
            return
        await query.answer()
        admins = await list_all_admins()
        await safe_edit(query,
            f"👥 <b>All Admins</b>  ({len(admins)} total)",
            reply_markup=admin_list_kb(admins))
        return

    if data=="add_superadmin_prompt":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        AWAITING_SUPER_ADMIN[uid] = True
        await safe_edit(query,
            "👑 <b>Add Super Admin</b>\n\n"
            "Send the Telegram <b>User ID</b> of the new super admin.\n\n"
            "⚠️ Super admins have <b>full access</b> to all bot functions.\n"
            "/cancel to abort.")
        return

    if data.startswith("rm_admin_"):
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        aid=int(data.split("_")[-1])
        if aid==uid: await query.answer("Can't remove yourself!",show_alert=True); return
        if aid in INITIAL_ADMIN_IDS: await query.answer("Can't remove super admin!",show_alert=True); return
        await remove_admin_permissions(aid); await query.answer(f"✅ Removed {aid}")
        admins=await list_all_admins()
        await query.edit_message_text(f"👥 <b>Admin Management</b>",reply_markup=admin_list_kb(admins),parse_mode="HTML")
        return

    if data=="add_admin_prompt":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        AWAITING_ADMIN_ID[uid]=True
        await query.edit_message_text("👥 <b>Add Admin</b>\n\nSend the user's numeric Telegram ID.",parse_mode="HTML")
        return

    if data.startswith("ptoggle|"):
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        _,tuid_str,perm=data.split("|",2); tuid=int(tuid_str)
        sel=AWAITING_PERMISSIONS.get((uid,tuid),[])
        if perm in sel: sel.remove(perm)
        else: sel.append(perm)
        AWAITING_PERMISSIONS[(uid,tuid)]=sel
        await query.edit_message_reply_markup(reply_markup=perms_kb(sel,tuid)); await query.answer()
        return

    if data.startswith("pdone|"):
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        tuid=int(data.split("|")[1]); sel=AWAITING_PERMISSIONS.pop((uid,tuid),[])
        if not sel: await query.answer("Select at least one!",show_alert=True); return
        await set_admin_permissions(tuid,sel); AWAITING_ADMIN_ID.pop(uid,None)
        plist="\n".join(f"• {PERMISSIONS.get(p,p)}" for p in sel)
        await query.edit_message_text(f"{ui('check')} <b>Admin {tuid} added!</b>\n\n<b>Permissions:</b>\n{plist}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back",callback_data="admin_manage_admins", style="primary", icon_custom_emoji_id=_UI["back"][0])]]))
        return

    if data=="admin_settings":
        await query.answer()
        await query.edit_message_text(f"⚙️ <b>Settings</b>\n{D}",reply_markup=admin_settings_kb(),parse_mode="HTML")
        return

    if data=="website_management":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        sync_website_requests_into_bot_requests()
        await query.edit_message_text(
            build_website_management_text(),
            reply_markup=website_management_kb(),
            parse_mode="HTML",
        )
        return

    if data=="website_pending_requests":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        await query.edit_message_text(
            build_website_pending_text(),
            reply_markup=website_pending_kb(),
            parse_mode="HTML",
        )
        return

    if data in {"website_set_announcement", "website_set_status_note", "website_set_whatsapp", "website_set_telegram"}:
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        prompt_map = {
            "website_set_announcement": (
                "WEBSITE_ANNOUNCEMENT",
                f"{ui('announce')} <b>Website Announcement</b>\n{D}\nSend the announcement text that should appear on the dashboard and create-bot page."
            ),
            "website_set_status_note": (
                "WEBSITE_STATUS_NOTE",
                f"{ui('notepad')} <b>Website Approval Note</b>\n{D}\nSend the note shown after a website user submits the create-bot request."
            ),
            "website_set_whatsapp": (
                "WEBSITE_CONTACT_WHATSAPP",
                f"{ui('phone')} <b>Website WhatsApp Contact</b>\n{D}\nSend the WhatsApp number in international format."
            ),
            "website_set_telegram": (
                "WEBSITE_CONTACT_TELEGRAM",
                f"{ui('chat')} <b>Website Telegram Contact</b>\n{D}\nSend the Telegram username or public link."
            ),
        }
        setting_key, prompt_text = prompt_map[data]
        context.user_data["awaiting_website_setting"] = setting_key
        await query.edit_message_text(
            prompt_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[btn("Back", cbd="website_management", style="primary", icon=_UI["back"][0])]]),
        )
        return

    if data.startswith("gui_page_"):
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        page = int(data.split("_")[-1])
        await query.answer()
        theme_name = _THEME_NAMES.get(OTP_GUI_THEME % 15, "Unknown")
        try:
            await query.edit_message_reply_markup(reply_markup=gui_theme_kb(page))
        except Exception:
            pass
        return

    if data=="admin_gui_theme":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        theme_name = _THEME_NAMES.get(OTP_GUI_THEME % 15, "Unknown")
        await query.edit_message_text(
            f"🎨 <b>OTP GUI Theme</b>\n{D}\n"
            f"Current: <b>{theme_name}</b>\n\n"
            "Select a theme to see how OTP messages will look.\n"
            "Both DM and group messages update instantly.",
            reply_markup=gui_theme_kb(), parse_mode="HTML")
        return

    if data.startswith("set_gui_theme_"):
        # SUPER ADMIN ONLY — only super admins can change the OTP design
        if not is_super_admin(uid):
            await query.answer("⛔ Only Super Admins can change the OTP design.", show_alert=True)
            return
        OTP_GUI_THEME = int(data.split("_")[-1]) % 15
        save_config_key("OTP_GUI_THEME", OTP_GUI_THEME)
        theme_name = _THEME_NAMES.get(OTP_GUI_THEME, "Unknown")
        await query.answer(f"✅ Design → {theme_name}", show_alert=False)
        await safe_edit(query,
            f"🎨 <b>OTP Design Selected</b>\n{D}\n"
            f"✅ <b>{theme_name}</b>\n\n"
            f"All future OTP messages will use this design.",
            reply_markup=gui_theme_page_kb(OTP_GUI_THEME // 10))
        return

    if data=="admin_links":
        await query.answer()
        await query.edit_message_text(
            f"🔗 <b>Bot Links</b>\n{D}\n"
            f"📢 Channel: <code>{html.escape(CHANNEL_LINK or '—')}</code>\n"
            f"💬 OTP Group: <code>{html.escape(OTP_GROUP_LINK or '—')}</code>\n"
            f"📞 Number Bot: <code>{html.escape(NUMBER_BOT_LINK or '—')}</code>\n"
            f"🛟 Support: <code>{html.escape(SUPPORT_USER or '—')}</code>\n"
            f"🧠 Developer: <code>{html.escape(DEVELOPER or '—')}</code>",
            reply_markup=admin_links_kb(), parse_mode="HTML")
        return

    if data=="admin_botinfo":
        await query.answer()
        await query.edit_message_text(
            f"🤖 <b>Bot Info</b>\n{D}\n"
            f"👤 Username:  @{html.escape(BOT_USERNAME)}\n"
            f"🆔 Token:     <code>{'•'*20}</code>\n"
            f"🧸 Child Bot: {'Yes' if IS_CHILD_BOT else 'No'}\n"
            f"📦 Assign Limit: {get_assign_limit_label()}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back",callback_data="admin_settings", style="primary", icon_custom_emoji_id=_UI["back"][0])]]),
            parse_mode="HTML")
        return

    if data.endswith("_prompt") and data in ("set_channel_prompt","set_otpgroup_prompt","set_numbot_prompt","set_support_prompt"):
        key_map={"set_channel_prompt":"CHANNEL_LINK","set_otpgroup_prompt":"OTP_GROUP_LINK",
                 "set_numbot_prompt":"NUMBER_BOT_LINK","set_support_prompt":"SUPPORT_USER"}
        context.user_data["awaiting_link"]=key_map[data]
        label_map={"CHANNEL_LINK":"Channel Link (https://t.me/...)","OTP_GROUP_LINK":"OTP Group Link (https://t.me/...)","NUMBER_BOT_LINK":"Number Bot Link (https://t.me/...)","SUPPORT_USER":"Support Username (@username)"}
        k=key_map[data]
        await query.edit_message_text(f"✏️ Send new {label_map[k]}:",parse_mode="HTML")
        return

    if data=="set_limit":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.edit_message_text(
            f"{ui('copy')} <b>Bot Assign Limit</b>\n"
            f"{D}\nCurrent bot assignment count: <b>{get_assign_limit_label()}</b>\n"
            f"{ui('desktop')} Website get-number flow uses its own public viewer and is not affected by this setting.",
            reply_markup=limit_kb(),parse_mode="HTML")
        return

    if data.startswith("glimit_"):
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        DEFAULT_ASSIGN_LIMIT=int(data.split("_")[-1])
        save_config_key("default_limit",DEFAULT_ASSIGN_LIMIT)
        await query.answer(f"Assign limit set to {get_assign_limit_label(DEFAULT_ASSIGN_LIMIT)}")
        await query.edit_message_text(f"⚙️ <b>Settings</b>\n{D}",reply_markup=admin_settings_kb(),parse_mode="HTML")
        return

    if data=="admin_advanced":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        await query.edit_message_text(f"{ui('tools')} <b>Advanced Tools</b>\n{D}",reply_markup=advanced_kb(),parse_mode="HTML")
        return

    # ── Advanced analytics alias ─────────────────────────────────────────
    if data=="advanced_analytics":
        if "view_stats" not in perms and not is_sup:
            await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        await query.edit_message_text(
            f"{ui('chart')} <b>Advanced Analytics</b>\n{D}",
            reply_markup=advanced_kb(), parse_mode="HTML")
        return

    # ── Clear all log chats ──────────────────────────────────────────────
    if data=="clear_all_logs":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        await query.edit_message_text(
            f"⚠️ <b>Clear All Log Groups?</b>\n{D}\n"
            "This removes all registered log group IDs.\n"
            "OTPs will no longer be sent to any group.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅  Yes, Clear All", callback_data="confirm_clear_logs", style="danger"),
                InlineKeyboardButton("❌  Cancel", callback_data="admin_manage_logs", style="primary"),
            ]]), parse_mode="HTML")
        return

    if data=="confirm_clear_logs":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        chats = await db.get_all_log_chats()
        for _cid in chats:
            await db.remove_log_chat(_cid)
        await query.answer(f"✅ Removed {len(chats)} log group(s).", show_alert=True)
        await query.edit_message_text(
            f"{ui('check')} <b>All Log Groups Cleared</b>\n{D}\n"
            f"Removed <b>{len(chats)}</b> group(s).\n\n"
            "Use <b>Manage Logs</b> to add new ones.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="admin_home", style="primary")
            ]]), parse_mode="HTML")
        return

    # ── System tools panel ───────────────────────────────────────────────
    if data=="admin_system":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        analytics = await get_bot_analytics()
        health    = await get_system_health()
        await query.edit_message_text(
            f"⚙️ <b>System Tools</b>\n{D}\n"
            f"👥 Users: <b>{analytics.get('active_users',0)}</b>  "
            f"📱 Numbers: <b>{analytics.get('total_numbers',0)}</b>\n"
            f"🔌 Panels: <b>{analytics.get('active_panels',0)}/{analytics.get('total_panels',0)}</b>  "
            f"🔑 OTPs: <b>{analytics.get('total_otps_processed',0)}</b>\n"
            f"💻 CPU: <b>{health.get('cpu_percent','N/A')}%</b>  "
            f"🧠 RAM: <b>{health.get('memory_mb',0):.0f}MB</b>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{ui('database')} DB Summary",  callback_data="admin_db_summary",  style="primary"),
                 InlineKeyboardButton(f"{ui('chart')} OTP History",    callback_data="admin_otp_history", style="success")],
                [InlineKeyboardButton("🧹 Maintenance", callback_data="admin_maintenance", style="danger"),
                 InlineKeyboardButton(f"{ui('notepad')} View Logs",    callback_data="view_logs",         style="primary")],
                [InlineKeyboardButton("🔙 Back", callback_data="admin_home", style="primary")],
            ]), parse_mode="HTML")
        return

    # ═══════════════════════════════════════════════════
    #  API TOKEN MANAGEMENT
    # ═══════════════════════════════════════════════════
    if data=="admin_api_tokens":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        tokens = await db.get_all_api_tokens()
        lines = []
        for tok in tokens:
            status_emoji = ui('check') if tok.status == "ACTIVE" else ui('cancel')
            last_used = tok.last_used.strftime("%Y-%m-%d %H:%M") if tok.last_used else "Never"
            lines.append(f"{status_emoji} <b>{html.escape(tok.name)}</b>\n  {ui('key')} <code>{tok.token[:20]}...</code>\n  {ui('calendar')} {last_used}")
        
        msg = f"{ui('satellite')} <b>API Tokens</b> ({len(tokens)} total)\n{D}\n"
        if tokens:
            msg += "\n\n".join(lines)
        else:
            msg += "<i>No API tokens yet.</i>"
        
        await query.edit_message_text(msg,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{ui('plus')} Create Token", callback_data="api_create_token", style="success")],
                [InlineKeyboardButton("Back", callback_data="website_management", style="primary", icon_custom_emoji_id=_UI["back"][0])]
            ]), parse_mode="HTML")
        return

    if data=="api_create_token":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        AWAITING_API_CREATE[uid] = {"step": "name"}
        await query.edit_message_text(
            f"{ui('robot')} <b>Create API Token</b> — Step 1/3\n{D}\n"
            "Send a <b>name</b> for this API token:\n"
            "<i>(e.g. Website API, Mobile App, Partner Integration)</i>\n"
            "/cancel to abort.",
            parse_mode="HTML")
        return

    if data=="api_create_token_dev":
        if uid not in AWAITING_API_CREATE or AWAITING_API_CREATE[uid]["step"] != "name":
            return
        # This will be handled in handle_text
        AWAITING_API_CREATE[uid]["step"] = "dev"
        await query.edit_message_text(
            f"{ui('robot')} <b>Create API Token</b> — Step 2/3\n{D}\n"
            "Send the <b>developer/company name</b>:\n"
            "/skip to leave blank.",
            parse_mode="HTML")
        return

    if data=="api_create_token_panels":
        if uid not in AWAITING_API_CREATE or AWAITING_API_CREATE[uid]["step"] != "dev":
            return
        AWAITING_API_CREATE[uid]["step"] = "panels"
        # Show panel selection
        await query.edit_message_text(
            f"{ui('robot')} <b>Create API Token</b> — Step 3/3\n{D}\n"
            "Select which panels this API can access:",
            reply_markup=api_panel_selection_kb(),
            parse_mode="HTML")
        return

    # Handle panel selection checkboxes
    if data.startswith("api_panel_"):
        if uid not in AWAITING_API_CREATE:
            return
        panel_id = data.split("_")[-1]
        selected = AWAITING_API_CREATE[uid].get("panels", [])
        if panel_id in selected:
            selected.remove(panel_id)
        else:
            selected.append(panel_id)
        AWAITING_API_CREATE[uid]["panels"] = selected
        await query.edit_message_reply_markup(reply_markup=api_panel_selection_kb(selected))
        return

    if data=="api_create_confirm":
        if uid not in AWAITING_API_CREATE:
            await query.answer("Error: Invalid state", show_alert=True)
            return
        
        state = AWAITING_API_CREATE.pop(uid, {})
        name = state.get("name", "Unnamed")
        dev = state.get("dev", "")
        panels = state.get("panels", [])
        
        # Generate random token
        import secrets
        token = secrets.token_urlsafe(32)
        
        # Save to database
        import json
        panels_json = json.dumps(panels) if panels else "[]"
        await db.create_api_token(token, name, uid, api_dev=dev, panels_data=panels_json)
        
        await query.answer(f"{ui('check')} Token created!")
        await context.bot.send_message(uid,
            f"{ui('check')} <b>API Token Created!</b>\n{D}\n"
            f"{ui('robot')} <b>Name:</b> {html.escape(name)}\n"
            f"{ui('user')} <b>Developer:</b> {html.escape(dev or 'N/A')}\n"
            f"{ui('key')} <b>Token:</b>\n<code>{token}</code>\n\n"
            f"<b>API Endpoint:</b>\n<code>https://mywebsite.com/api/sms?token={token}</code>\n\n"
            f"<i>Keep this token safe! You won't be able to see it again.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"{ui('back')} Back", callback_data="admin_api_tokens", style="primary")
            ]]))
        return

    if data=="test_panels":
        if "manage_panels" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer("🔄 Testing…")
        lines=[]
        for p in PANELS:
            if p.panel_type=="login":   ok=await login_to_panel(p); lines.append(f"{'✅' if ok else '❌'} {html.escape(p.name)} [LOGIN]")
            elif p.panel_type=="api":   ok=await test_api_panel(p); p.is_logged_in=ok; lines.append(f"{'✅' if ok else '❌'} {html.escape(p.name)} [API]")
            else:
                running=p.name in IVAS_TASKS and not IVAS_TASKS[p.name].done()
                lines.append(f"{'🟢' if running else '🔴'} {html.escape(p.name)} [IVAS]")
        await query.edit_message_text(f"🔍 <b>Panel Tests</b>\n{D}\n"+"\n".join(lines),parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="admin_advanced", style="primary")]]))
        return

    if data=="restart_workers":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        for nm,task in list(IVAS_TASKS.items()): task.cancel(); IVAS_TASKS.pop(nm,None)
        for p in PANELS:
            if p.panel_type=="ivas":
                task=asyncio.create_task(ivas_worker(p),name=f"IVAS-{p.name}")
                task.add_done_callback(handle_task_exception); IVAS_TASKS[p.name]=task
        await query.answer("✅ Workers restarted.",show_alert=True)
        await query.edit_message_text(f"🛠 <b>Advanced Tools</b>\n{D}\n{ui('check')} Workers restarted.",
            reply_markup=advanced_kb(),parse_mode="HTML")
        return

    if data=="clear_otps":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.edit_message_text("🗑 Clear ALL OTPs?",reply_markup=confirm_kb("clear_otps"), parse_mode="HTML")
        return
    if data=="confirm_clear_otps":
        save_otp_store({})
        await query.edit_message_text(f"{ui('check')} All OTPs cleared.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="admin_advanced", style="primary")]]))
        return

    if data=="export_otps":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        store=load_otp_store()
        if not store: await query.answer("No OTPs.",show_alert=True); return
        fname=f"otp_export_{datetime.now():%Y%m%d_%H%M%S}.json"
        with open(fname,"w") as f: json.dump(store,f,indent=2)
        try:
            with open(fname,"rb") as f: await context.bot.send_document(chat_id=uid,document=f,caption="📤 OTP Export")
        finally: os.remove(fname)
        await query.answer("✅ Exported."); return

    if data=="view_logs":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        try:
            lines_=open(LOG_FILE,errors="replace").readlines()[-25:]
            await query.edit_message_text(
                f"<b>Last 25 log lines</b>\n<pre>{html.escape(''.join(lines_)[-3500:])}</pre>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="admin_advanced", style="primary")]]))
        except Exception as e: await query.edit_message_text(f"Error: {e}", parse_mode="HTML")
        return

    if data=="admin_fetch_sms":
        if "manage_panels" not in perms and not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer(f"{ui('satellite')} Fetching…")
        report=f"{ui('clipboard')} <b>SMS Fetch Report</b>\n{ui('clock')} {datetime.now():%Y-%m-%d %H:%M:%S}\n{D}\n"
        for p in PANELS:
            if p.panel_type=="ivas":
                running=p.name in IVAS_TASKS and not IVAS_TASKS[p.name].done()
                status_emoji = ui('check') if running else ui('cancel')
                report+=f"{ui('satellite')} <b>{html.escape(p.name)}</b> [IVAS] {status_emoji} {'Running' if running else 'Stopped'}\n\n"; continue
            if p.panel_type=="login" and not p.is_logged_in: await login_to_panel(p)
            sms=await fetch_panel_sms(p)
            if sms is None: report+=f"{ui('cancel')} <b>{html.escape(p.name)}</b>: Auth failed.\n\n"
            elif not sms: report+=f"{ui('check')} <b>{html.escape(p.name)}</b>: Connected — no recent SMS.\n\n"
            else:
                report+=f"{ui('check')} <b>{html.escape(p.name)}</b> — {len(sms)} records (latest 5):\n"
                for rec in sms[:5]:
                    if p.panel_type=="api": dt_=str(rec[0]); num_=str(rec[1]); msg_=str(rec[3])
                    else: dt_=str(rec[0]); num_=str(rec[2]) if len(rec)>2 else "?"; msg_=get_message_body(rec) or ""
                    otp_=extract_otp_regex(msg_) or ""; time_=dt_[11:19] if len(dt_)>=19 else dt_
                    otp_display = f"{ui('key')}{otp_}" if otp_ else ""
                    report+=f"  {ui('clock')}{time_} {ui('phone')}{mask_number(num_)} {otp_display}\n  {html.escape(msg_[:60])}\n"
                report+="\n"
        bkb=InlineKeyboardMarkup([[InlineKeyboardButton(f"{ui('back')} Back",callback_data="admin_home", style="primary")]])
        if len(report)>4000:
            for chunk in [report[i:i+4000] for i in range(0,len(report),4000)]:
                await context.bot.send_message(uid,chunk,parse_mode="HTML")
            await context.bot.send_message(uid,f"{ui('check')} Done.",reply_markup=bkb)
        else:
            await context.bot.send_message(uid,report,parse_mode="HTML",reply_markup=bkb)
        return

    # ── Multi-Bot Management ─────────────────────────────
    if not IS_CHILD_BOT:
        if data=="admin_bots":
            if not is_sup: await query.answer("Unauthorized",show_alert=True); return
            await query.answer(); bots=bm.list_bots()
            tr=sum(1 for b in bots if b.get("running"))
            if bots:
                lines_txt="\n".join(f"{ui('check') if b.get('running') else ui('cancel')} <b>{html.escape(b['name'])}</b>  <code>{b['id']}</code>" for b in bots)
                msg_txt=f"{ui('desktop')} <b>Bot Manager</b>\n{D}\nTotal: <b>{len(bots)}</b>  |  Running: <b>{tr}</b>\n\n{lines_txt}"
            else:
                msg_txt=f"{ui('desktop')} <b>Bot Manager</b>\n{D}\n<i>No bots yet. Create your first bot!</i>"
            await query.edit_message_text(
                msg_txt,
                reply_markup=bots_list_kb(bots),parse_mode="HTML")
            return

        if data=="add_bot_start":
            if not is_sup: await query.answer("Unauthorized",show_alert=True); return
            BOT_ADD_STATES[uid]={"step":"name","data":{}}; await query.answer()
            await query.edit_message_text(
                f"{ui('robot')} <b>Add New Bot</b>\n{D}\n"
                "Step 1/9 — Send a <b>name</b> for this bot\n"
                "<i>e.g. MyStore, OTPBot2, SigmaV2</i>\n\nSend /cancel to abort.",
                parse_mode="HTML")
            return

        if data.startswith("bot_info_"):
            if not is_sup: await query.answer("Unauthorized",show_alert=True); return
            bid=data[9:]; info=bm.get_bot_info(bid)
            if not info: await query.answer("Not found.",show_alert=True); return
            running=bm.is_running(bid); st=f"{ui('check')} Running" if running else f"{ui('cancel')} Stopped"
            created=info.get("created_at","?")[:16].replace("T"," ")
            uname=info.get("bot_username","?")
            bot_link=f"https://t.me/{uname.lstrip('@')}" if uname and uname!="?" else "—"
            await query.answer()
            try:
                await query.edit_message_text(
                    f"▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐\n"
                    f"▐▐  {ui('robot')}  {html.escape(info.get('name','?')):<21}▐▐\n"
                    f"▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐\n"
                    f"▐▐  {ui('satellite')} {st:<24}▐▐\n"
                    f"▐▐  {ui('id')} <code>{bid}</code>         ▐▐\n"
                    f"▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐\n"
                    f"  {ui('user')} @{html.escape(uname):<23}\n"
                    f"  {ui('users')} Admins: {str(info.get('admin_ids',[]))[:20]}\n"
                    f"  {ui('calendar')} Created: {created}\n"
                    f"  {ui('folder')} <code>{html.escape(info.get('folder','?'))[-30:]}</code>\n"
                    f"▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐▐\n"
                    f"  {ui('broadcast')} {html.escape(info.get('channel_link','—') or '—')[:30]}\n"
                    f"  {ui('chat')} {html.escape(info.get('otp_group_link','—') or '—')[:30]}\n"
                    f"  {ui('phone')} {html.escape(info.get('number_bot_link','—') or '—')[:30]}\n"
                    f"  {ui('support')} {html.escape(info.get('support_user','—') or '—')}",
                    reply_markup=bot_actions_kb(bid,running,info),parse_mode="HTML")
            except TelegramBadRequest: pass
            return

        if data.startswith("bot_start_"):
            if not is_sup: await query.answer("Unauthorized",show_alert=True); return
            bid=data[10:]; ok,msg=bm.start_bot(bid)
            await query.answer(f"{ui('check') if ok else ui('cancel')} {msg}",show_alert=True)
            bots=bm.list_bots()
            try: await query.edit_message_reply_markup(reply_markup=bots_list_kb(bots))
            except TelegramBadRequest: pass
            return

        if data.startswith("bot_stop_"):
            if not is_sup: await query.answer("Unauthorized",show_alert=True); return
            bid=data[9:]; ok,msg=bm.stop_bot(bid)
            await query.answer(f"{ui('check') if ok else ui('cancel')} {msg}",show_alert=True)
            bots=bm.list_bots()
            try: await query.edit_message_reply_markup(reply_markup=bots_list_kb(bots))
            except TelegramBadRequest: pass
            return

        if data.startswith("bot_restart_"):
            if not is_sup: await query.answer("Unauthorized",show_alert=True); return
            bid=data[12:]; await query.answer("🔁 Restarting…")
            ok,msg=bm.restart_bot(bid); info=bm.get_bot_info(bid) or {}; running=bm.is_running(bid)
            try:
                await query.edit_message_text(
                    f"🤖 <b>{html.escape(info.get('name','?'))}</b>\n"
                    f"{'🟢 Running' if running else '🔴 Stopped'}\nResult: {msg}",
                    reply_markup=bot_actions_kb(bid,running,info),parse_mode="HTML")
            except TelegramBadRequest: pass
            return

        if data.startswith("bot_log_"):
            if not is_sup: await query.answer("Unauthorized",show_alert=True); return
            bid=data[8:]; log=bm.get_bot_log(bid,lines=30); info=bm.get_bot_info(bid) or {}
            await query.answer()
            try:
                await query.edit_message_text(
                    f"📋 <b>Log: {html.escape(info.get('name','?'))}</b>\n<pre>{html.escape(log[-3000:])}</pre>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔁 Refresh",callback_data=f"bot_log_{bid}", style="primary"),
                        InlineKeyboardButton("🔙 Back",   callback_data=f"bot_info_{bid}", style="primary"),
                    ]]))
            except TelegramBadRequest: pass
            return

        if data.startswith("bot_del_") and not data.startswith("bot_del_yes_"):
            if not is_sup: await query.answer("Unauthorized",show_alert=True); return
            bid=data[8:]; info=bm.get_bot_info(bid) or {}; await query.answer()
            await query.edit_message_text(
                f"⚠️ Delete bot <b>{html.escape(info.get('name','?'))}</b>?\n\n"
                "This permanently stops and deletes its folder.",
                reply_markup=confirm_del_bot_kb(bid),parse_mode="HTML")
            return

        if data.startswith("bot_del_yes_"):
            if not is_sup: await query.answer("Unauthorized",show_alert=True); return
            bid=data[12:]; ok,msg=bm.delete_bot(bid)
            await query.answer(f"{'✅' if ok else '❌'} {msg}",show_alert=True)
            bots=bm.list_bots()
            await query.edit_message_text(f"🖥  <b>Bot Manager</b>\nTotal: <b>{len(bots)}</b>",
                reply_markup=bots_list_kb(bots),parse_mode="HTML")
            return

    # ═══════════════════════════════════════════════════
    #  OTP STORE VIEWER
    # ═══════════════════════════════════════════════════
    if data=="admin_otp_store":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        store=load_otp_store()
        if not store:
            await query.edit_message_text("🔑 <b>OTP Store</b>\nEmpty.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="admin_home", style="primary")]]),
                parse_mode="HTML")
            return
        lines=[f"📱 <code>{mask_number(k)}</code>  🔑 <code>{v}</code>" for k,v in list(store.items())[-20:]]
        await query.edit_message_text(
            f"🔑 <b>OTP Store</b>  ({len(store)} entries, last 20)\n{D}\n"+"\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Clear All", callback_data="clear_otps", style="danger"),
                 InlineKeyboardButton("📤 Export",    callback_data="export_otps", style="success")],
                [InlineKeyboardButton("🔙 Back",      callback_data="admin_home", style="primary")]]),
            parse_mode="HTML")
        return

    # ═══════════════════════════════════════════════════
    #  BROADCAST TO ALL BOTS USERS
    # ═══════════════════════════════════════════════════
    if data=="broadcast_all_bots":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        context.user_data["bcast_all_bots"]=True
        context.user_data["awaiting_broadcast"]=True
        await query.answer()
        bots=bm.list_bots(); total_bots=len(bots)
        await query.edit_message_text(
            f"📢 <b>Broadcast to ALL Bots</b>\n{D}\n"
            f"This will send your message to users of <b>ALL {total_bots} child bots</b> "
            f"plus this master bot.\n\n"
            "✏️ <b>Type your message and send it:</b>\n"
            "<i>(Supports HTML formatting)</i>",
            parse_mode="HTML")
        return

    # ═══════════════════════════════════════════════════
    #  CHILD BOT — START ALL / STOP ALL
    # ═══════════════════════════════════════════════════
    if data=="bots_start_all":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer("▶️ Starting all bots…")
        bots=bm.list_bots(); ok=0; fail=0
        for b in bots:
            if not bm.is_running(b["id"]):
                res,_=bm.start_bot(b["id"])
                if res: ok+=1
                else:   fail+=1
        bots=bm.list_bots(); run=sum(1 for b in bots if b.get("running"))
        await query.edit_message_text(
            f"🖥 <b>Bot Manager</b>\n{D}\n"
            f"▶️ Started: <b>{ok}</b>  ❌ Failed: <b>{fail}</b>\n"
            f"🟢 Running: <b>{run}/{len(bots)}</b>",
            reply_markup=bots_list_kb(bots),parse_mode="HTML")
        return

    if data=="bots_stop_all":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer("⏹ Stopping all bots…")
        bots=bm.list_bots(); stopped=0
        for b in bots:
            if bm.is_running(b["id"]):
                bm.stop_bot(b["id"]); stopped+=1
        bots=bm.list_bots()
        await query.edit_message_text(
            f"🖥 <b>Bot Manager</b>\n{D}\n⏹ Stopped: <b>{stopped}</b> bots",
            reply_markup=bots_list_kb(bots),parse_mode="HTML")
        return

    if data=="bots_all_stats":
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        await query.answer()
        bots=bm.list_bots(); lines=[]
        for b in bots:
            st="🟢" if b.get("running") else "🔴"
            reg=bm.load_registry().get(b["id"],{})
            lines.append(
                f"{st} <b>{html.escape(b['name'])}</b>\n"
                f"   📢 {html.escape(reg.get('channel_link','—') or '—')}\n"
                f"   💬 {html.escape(reg.get('otp_group_link','—') or '—')}\n"
                f"   👤 {reg.get('admin_ids',[])}"
            )
        await query.edit_message_text(
            f"📊 <b>All Bots Overview</b>  ({len(bots)} total)\n{D}\n"
            +("\n\n".join(lines) if lines else "None"),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back",callback_data="admin_bots", style="primary")]]),
            parse_mode="HTML")
        return

    # ═══════════════════════════════════════════════════
    #  CHILD BOT — INDIVIDUAL STATS + BROADCAST
    # ═══════════════════════════════════════════════════
    if data.startswith("bot_stats_"):
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        bid=data[10:]; info=bm.get_bot_info(bid) or {}; await query.answer()
        running=bm.is_running(bid)
        log_preview=bm.get_bot_log(bid,lines=5)
        last_lines=log_preview[-300:] if log_preview else "(no log)"
        await query.edit_message_text(
            f"📊 <b>Bot: {html.escape(info.get('name','?'))}</b>\n{D}\n"
            f"📶 Status: {'🟢 Running' if running else '🔴 Stopped'}\n"
            f"📢 Channel: {html.escape(info.get('channel_link','—') or '—')}\n"
            f"💬 OTP Grp: {html.escape(info.get('otp_group_link','—') or '—')}\n"
            f"📞 Num Bot: {html.escape(info.get('number_bot_link','—') or '—')}\n"
            f"👤 Admins:  {info.get('admin_ids',[])}\n\n"
            f"📋 <b>Last log lines:</b>\n<pre>{html.escape(last_lines)}</pre>",
            reply_markup=bot_actions_kb(bid,running,info),parse_mode="HTML")
        return

    if data.startswith("bot_bcast_"):
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        bid=data[10:]; info=bm.get_bot_info(bid) or {}; await query.answer()
        context.user_data["bcast_single_bot"]=bid
        context.user_data["awaiting_broadcast"]=True
        await query.edit_message_text(
            f"📢 <b>Broadcast — {html.escape(info.get('name','?'))}</b>\n{D}\n"
            "Type your message and send it.\n"
            "It will be delivered to users of <b>this bot only</b>.\n\n"
            "<i>(HTML formatting supported)</i>",
            parse_mode="HTML")
        return

    # ═══════════════════════════════════════════════════
    #  CHILD BOT — EDIT LINKS INLINE
    # ═══════════════════════════════════════════════════
    if data.startswith("bot_editlinks_"):
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        bid=data[14:]; info=bm.get_bot_info(bid) or {}; await query.answer()
        await query.edit_message_text(
            f"🔗 <b>Edit Links: {html.escape(info.get('name','?'))}</b>\n{D}\n"
            f"📢 Channel:  <code>{html.escape(info.get('channel_link','—') or '—')}</code>\n"
            f"💬 OTP Grp:  <code>{html.escape(info.get('otp_group_link','—') or '—')}</code>\n"
            f"📞 Num Bot:  <code>{html.escape(info.get('number_bot_link','—') or '—')}</code>\n"
            f"🛟 Support:  <code>{html.escape(info.get('support_user','—') or '—')}</code>",
            reply_markup=bot_edit_links_kb(bid),parse_mode="HTML")
        return

    if data.startswith("bot_setlink_"):
        if not is_sup: await query.answer("Unauthorized",show_alert=True); return
        parts=data.split("_",3); bid=parts[2]; link_key=parts[3]
        await query.answer()
        context.user_data["bot_setlink_bid"]=bid
        context.user_data["bot_setlink_key"]=link_key
        labels={"CHANNEL_LINK":"Channel Link (https://t.me/...)","OTP_GROUP_LINK":"OTP Group Link","NUMBER_BOT_LINK":"Number Bot Link","SUPPORT_USER":"Support Username"}
        await query.edit_message_text(
            f"✏️ Send the new <b>{labels.get(link_key,link_key)}</b> for bot <b>{bid}</b>:\n"
            "/cancel to abort.",parse_mode="HTML")
        return

    # ═══════════════════════════════════════════════════
    #  CANCEL
    # ═══════════════════════════════════════════════════
    # pick_gui and gui_set_ are legacy — redirect to the proper admin theme picker
    if data=="pick_gui" or data.startswith("gui_set_"):
        if not is_super_admin(uid):
            await query.answer("Open Admin → Settings → 🎨 OTP GUI to change theme.", show_alert=True)
            return
        await query.answer()
        await safe_edit(query,
            "🎨 <b>OTP Theme</b>\n\nUse Admin Panel → Settings → 🎨 OTP GUI to select from all 30 themes.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🎨  Open Theme Picker", callback_data="admin_gui_theme", style="primary"),
                InlineKeyboardButton("🔙  Back",              callback_data="main_menu", style="primary"),
            ]]))
        return

    # ════════════════════════════════════════════════════════
    #  CREATE MY BOT FLOW
    # ════════════════════════════════════════════════════════
    if data == "create_bot_menu":
        # ✅ Disable bot creation in child bots
        if IS_CHILD_BOT:
            await query.answer("⚠️ This feature is only available in the main bot.", show_alert=True)
            return
        await query.answer()
        bot_link = "https://t.me/CrackSMSReBot"
        await safe_edit(query,
            f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
            f"  🤖 <b>Create Your Own OTP Bot</b>\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            f"Launch your own branded OTP forwarding bot powered by our system.\n\n"
            f"<b>What you need:</b>\n"
            f"• A Telegram group (your bot must be admin)\n"
            f"• Bot token from @BotFather  <i>(optional — we can create)</i>\n"
            f"• A panel or we forward from main panels\n\n"
            f"👇 Choose an option:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅  I have a panel", callback_data="cbot_have_panel", style="success"),
                 InlineKeyboardButton("❌  No panel needed", callback_data="cbot_no_panel", style="danger")],
                [InlineKeyboardButton("🔙  Back", callback_data="main_menu", style="primary")],
            ]))
        return

    if data=="cbot_no_panel":
        await query.answer()
        CREATE_BOT_STATES[uid] = {
            "step": "get_group_id",
            "has_panel": False,
            "uid": uid,
            "timestamp": datetime.now().isoformat(),
        }
        await safe_edit(query,
            "🤖 <b>Create Bot — Step 1/3</b>\n\n"
            "Send your Telegram <b>Group Chat ID</b>.\n\n"
            "To get it: add @userinfobot to your group, it will show the chat ID.\n"
            "It usually starts with <code>-100...</code>\n\n"
            "/cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙  Back", callback_data="create_bot_menu", style="primary")]]))
        return

    if data=="cbot_have_panel":
        await query.answer()
        await safe_edit(query,
            "<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
            "  💎 <b>Select Your Plan</b>\n"
            "<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            "<b>Premium Tier System</b>\n\n"
            "🆓 <b>Free</b> — Unlimited OTPs/day, 2 panels max\n"
            "    • Basic OTP forwarding\n"
            "    • Admin panel\n"
            "    • Price: <b>FREE</b>\n\n"
            "💎 <b>Pro</b> — Unlimited OTPs/day, 10 panels max\n"
            "    • Everything in Free, plus:\n"
            "    • Analytics & detailed reports\n"
            "    • Webhooks integration\n"
            "    • Priority support\n"
            "    • Price: <b>$5.00/month</b>\n\n"
            "🏆 <b>Enterprise</b> — Unlimited OTPs/day, 50 panels max\n"
            "    • Everything in Pro, plus:\n"
            "    • Full API access\n"
            "    • Advanced scheduling\n"
            "    • Media support\n"
            "    • Design partner experience\n"
            "    • Price: <b>$10.00/month</b>\n\n"
            "<b>Choose a plan to continue:</b>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🆓 Free",  callback_data="cbot_tier_free", style="primary")],
                [InlineKeyboardButton("💎 Pro ($5/mo)",  callback_data="cbot_tier_pro")],
                [InlineKeyboardButton("🏆 Enterprise ($10/mo)",  callback_data="cbot_tier_enterprise")],
                [InlineKeyboardButton("🔙  Back", callback_data="create_bot_menu", style="primary")],
            ]))
        return

    if data.startswith("cbot_tier_"):
        tier = data.split("_")[-1]  # free, pro, or enterprise
        if tier not in PREMIUM_TIERS:
            await query.answer("Invalid tier", show_alert=True); return
        await query.answer()
        CREATE_BOT_STATES[uid] = {
            "step": "get_bot_name",
            "has_panel": True,
            "tier": tier,
            "uid": uid,
            "timestamp": datetime.now().isoformat(),
        }
        tier_info = PREMIUM_TIERS[tier]
        price_text = 'Free' if tier_info['price'] == 0 else f"${tier_info['price']:.2f}/month"
        await safe_edit(query,
            f"<b>✅ Plan Selected: {tier_info['emoji']} {tier_info['name']}</b>\n\n"
            f"📋 You selected the {tier_info['name']} plan\n"
            f"💰 Price: {price_text}\n\n"
            f"🤖 <b>Create Bot — Step 1/2</b>\n\n"
            f"Send your <b>Bot Name</b> (e.g. <i>My OTP Bot</i>):",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙  Back", callback_data="cbot_have_panel", style="primary")]]))
        return

    if data.startswith("cbot_verify_"):
        group_id_str = data.split("_")[-1]
        if uid not in CREATE_BOT_STATES:
            await query.answer("Session expired. Start again.", show_alert=True); return
        
        state = CREATE_BOT_STATES[uid]
        has_panel = state.get("has_panel", False)
        
        await query.answer("⏳ Verifying…")
        try:
            # Verify bot is admin in the group
            test_msg = await context.bot.send_message(
                chat_id=int(group_id_str),
                text="✅ <b>Crack SMS Bot</b> verification check — I am admin here!",
                parse_mode="HTML")
            await context.bot.delete_message(chat_id=int(group_id_str), message_id=test_msg.message_id)
            
            # ✅ If NO PANEL selected: Simply add group to log chats (no bot creation)
            if not has_panel:
                # Add group to log chats for OTP forwarding
                group_label = f"User {uid} - No Panel Group"
                await db.add_log_chat(int(group_id_str), group_label)
                
                # Clean up state
                del CREATE_BOT_STATES[uid]
                
                # Notify user - group is now active for OTP receiving
                await query.edit_message_text(
                    "✅ <b>Group Linked Successfully!</b>\n\n"
                    f"📱 Group: <code>{group_id_str}</code>\n"
                    f"🔌 Configuration: Forward from main panels\n\n"
                    "Your group is now active and will receive OTPs forwarded from main panels.\n\n"
                    "No bot needed — just add me as admin and assign numbers!",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🏠  Back to Home", callback_data="main_menu", style="primary")
                    ]]))
                return
            
            # ✅ If PANEL selected: Submit bot creation request to admins
            CREATE_BOT_STATES[uid]["group_id"]  = group_id_str
            CREATE_BOT_STATES[uid]["step"]      = "confirmed"
            CREATE_BOT_STATES[uid]["user_name"] = update.effective_user.first_name
            
            # Create bot request with 6-digit ID
            req_id = generate_request_id()
            BOT_REQUESTS[req_id] = {
                "uid": uid,
                "group_id": group_id_str,
                "has_panel": True,
                "user_name": update.effective_user.first_name,
                "username": f"@{update.effective_user.username}" if update.effective_user.username else str(uid),
                "status": "pending",
                "req_id": req_id,
                "created_at": datetime.now().isoformat(),
            }
            
            # Notify all super admins
            req_txt = (
                f"🆕 <b>New Bot Request</b>\n\n"
                f"👤 User: {html.escape(update.effective_user.first_name)} "
                f"(<code>{uid}</code>)\n"
                f"📱 Group: <code>{group_id_str}</code>\n"
                f"🔌 Panel: Has own panel\n\n"
                f"Approve or reject below:"
            )
            for admin_id in INITIAL_ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id, text=req_txt,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("✅  Approve", callback_data=f"approvebot_{req_id}", style="success"),
                             InlineKeyboardButton("❌  Reject",  callback_data=f"rejectbot_{req_id}", style="danger")],
                        ]), parse_mode="HTML")
                except Exception:
                    pass

            await query.edit_message_text(
                "✅ <b>Verification Successful!</b>\n\n"
                "Your bot request has been submitted to the admins.\n"
                "You will receive a notification once approved!\n\n"
                f"📋 Request ID: <code>{req_id}</code>",
                parse_mode="HTML")

        except Exception as e:
            await query.edit_message_text(
                f"❌ <b>Verification Failed</b>\n\n"
                f"I could not send a message to group <code>{group_id_str}</code>.\n\n"
                f"<b>Please make sure:</b>\n"
                f"1. The group ID is correct\n"
                f"2. @{BOT_USERNAME} is an admin in that group\n\n"
                f"Error: <code>{html.escape(str(e))}</code>",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔁  Try Again", callback_data="cbot_no_panel", style="primary"),
                    InlineKeyboardButton("🔙  Back",      callback_data="main_menu", style="primary"),
                ]]), parse_mode="HTML")
            return

    # ════════════════════════════════════════════════════════════
    #  BOT CREATION CONFIRMATION & EDITING
    # ════════════════════════════════════════════════════════════
    
    # Handle bot confirmation (submit to admins)
    if data.startswith("bot_confirm_"):
        uid = int(data.split("_")[-1])
        if uid != query.from_user.id or uid not in CREATE_BOT_STATES:
            await query.answer("Session expired or unauthorized.", show_alert=True); return
        
        await query.answer("⏳ Submitting request to admins...")
        
        state = CREATE_BOT_STATES[uid]
        group_id_str = state.get("group_id", "?")
        
        # ✅ Validate critical fields before submit
        if not state.get("bot_name") or not state.get("token") or not state.get("bot_username"):
            await query.answer("❌ Missing required information.", show_alert=True)
            return
        
        # ✅ Check for token conflicts
        token_input = state.get("token", "")
        # Token conflict check against bot registry file
        existing_reg = bm.load_registry()
        token_already_used = any(
            info.get("token") == token_input for info in existing_reg.values()
        )
        if token_already_used:
                await query.edit_message_text(
                    "❌ <b>Token Already Registered</b>\n\n"
                    "This bot token is already registered in the system.\n\n"
                    "Get a new token from @BotFather and try again.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙  Edit Information", callback_data=f"bot_edit_{uid}", style="primary"),
                        InlineKeyboardButton("❌  Cancel", callback_data="cancel_action", style="danger"),
                    ]])
                )
                return
        
        # ✅ Build admin summary and send to all super admins
        req_id = generate_request_id()
        BOT_REQUESTS[req_id] = {
            **state, 
            "status": "pending", 
            "req_id": req_id,
            "user_name": query.from_user.first_name,
            "username": f"@{query.from_user.username}" if query.from_user.username else str(uid),
            "created_at": datetime.now().isoformat(),
        }
        
        admin_summary = (
            f"🆕 <b>Bot Creation Request</b>\n\n"
            f"👤 From: {html.escape(query.from_user.first_name)} (<code>{uid}</code>)\n"
            f"🤖 Bot Name: {html.escape(state.get('bot_name','?'))}\n"
            f"🔑 Token: <code>{html.escape(state.get('token', '?')[:20])}...</code>\n"
            f"👥 Admin ID: <code>{state.get('admin_id','?')}</code>\n"
            f"📱 Group: <code>{group_id_str}</code>\n"
            f"📢 Channel: {html.escape(str(state.get('channel', '—')))[:60]}\n"
            f"💬 OTP Group: {html.escape(str(state.get('otp_group', '—')))[:60]}\n"
            f"📞 Support: @{html.escape(str(state.get('support', '?'))[:30])}\n"
            f"🤖 Bot: @{html.escape(str(state.get('bot_username', '?'))[:30])}"
        )
        
        sent_count = 0
        for admin_id in INITIAL_ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=admin_id, 
                    text=admin_summary,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅  Approve", callback_data=f"approvebot_{req_id}", style="success"),
                        InlineKeyboardButton("❌  Reject", callback_data=f"rejectbot_{req_id}", style="danger"),
                    ]]), 
                    parse_mode="HTML"
                )
                sent_count += 1
            except Exception as e:
                pass  # Silent fail for unreachable admins
        
        # ✅ Clean up state and notify user
        del CREATE_BOT_STATES[uid]
        
        await query.edit_message_text(
            f"✅ <b>Request Submitted!</b>\n\n"
            f"Your bot creation request has been sent to {sent_count} admin(s).\n\n"
            f"📋 Request ID: <code>{req_id}</code>\n"
            f"⏱️  Admins will review within 1-24 hours.\n\n"
            f"You will be notified when your request is approved or rejected.\n\n"
            f"For urgent support, contact @NONEXPERTCODER",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠  Back to Home", callback_data="main_menu", style="primary")
            ]])
        )
        return
    
    # Handle bot edit (choose which field to change)
    if data.startswith("bot_edit_"):
        uid = int(data.split("_")[-1])
        if uid != query.from_user.id or uid not in CREATE_BOT_STATES:
            await query.answer("Session expired or unauthorized.", show_alert=True); return
        
        await query.answer()
        state = CREATE_BOT_STATES[uid]
        
        # Show which field to edit
        await query.edit_message_text(
            "<b>✏️  Which information do you want to change?</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("1️⃣  Bot Name", callback_data=f"bot_edit_field_bot_name_{uid}", style="primary")],
                [InlineKeyboardButton("2️⃣  Bot Token", callback_data=f"bot_edit_field_token_{uid}", style="primary")],
                [InlineKeyboardButton("3️⃣  Bot Username", callback_data=f"bot_edit_field_bot_username_{uid}", style="primary")],
                [InlineKeyboardButton("4️⃣  Admin ID", callback_data=f"bot_edit_field_admin_id_{uid}", style="primary")],
                [InlineKeyboardButton("5️⃣  Channel Link", callback_data=f"bot_edit_field_channel_{uid}", style="primary")],
                [InlineKeyboardButton("6️⃣  OTP Group Link", callback_data=f"bot_edit_field_otp_group_{uid}", style="primary")],
                [InlineKeyboardButton("7️⃣  Number Bot Link", callback_data=f"bot_edit_field_number_bot_{uid}", style="primary")],
                [InlineKeyboardButton("8️⃣  Support Username", callback_data=f"bot_edit_field_support_{uid}", style="primary")],
                [InlineKeyboardButton("9️⃣  Group Chat ID", callback_data=f"bot_edit_field_group_id_{uid}", style="primary")],
                [InlineKeyboardButton("🔙  Cancel Edit", callback_data=f"bot_confirm_{uid}", style="primary")],
            ])
        )
        return
    
    # Handle individual field editing
    if data.startswith("bot_edit_field_"):
        parts = data.split("_")
        field_name = "_".join(parts[3:-1])  # Extract field name
        uid = int(parts[-1])
        
        if uid != query.from_user.id or uid not in CREATE_BOT_STATES:
            await query.answer("Session expired.", show_alert=True); return
        
        await query.answer()
        
        # Map field names to user-friendly prompts
        field_prompts = {
            "bot_name": ("🤖 <b>Step 1/9 — Bot Name</b>\n\nSend the name for your bot:", "bot_name"),
            "token": ("🤖 <b>Step 2/9 — Bot Token</b>\n\nSend your bot token from @BotFather:", "token"),
            "bot_username": ("🤖 <b>Step 3/9 — Bot Username</b>\n\nSend the bot @username:", "bot_username"),
            "admin_id": ("🤖 <b>Step 4/9 — Admin ID</b>\n\nSend your Telegram numeric User ID:", "admin_id"),
            "channel": ("🤖 <b>Step 5/9 — Channel Link</b>\n\nSend your channel link or type 'none':", "channel"),
            "otp_group": ("🤖 <b>Step 6/9 — OTP Group Link</b>\n\nSend your OTP group link:", "otp_group"),
            "number_bot": ("🤖 <b>Step 7/9 — Number Bot Link</b>\n\nLink where users get numbers:", "number_bot"),
            "support": ("🤖 <b>Step 8/9 — Support Username</b>\n\nYour support @username:", "support"),
            "group_id": ("🤖 <b>Step 9/9 — Group Chat ID</b>\n\nSend the chat ID of your OTP group:", "group_id"),
        }
        
        if field_name not in field_prompts:
            await query.answer("Invalid field.", show_alert=True); return
        
        prompt, state_key = field_prompts[field_name]
        
        # Set state to expect this field update
        state = CREATE_BOT_STATES[uid]
        state["editing_step"] = state_key
        state["step"] = f"editing_{state_key}"
        
        await query.edit_message_text(prompt, parse_mode="HTML")
        return
    
    # Handle text input while editing a field
    if "editing_" in CREATE_BOT_STATES.get(uid, {}).get("step", ""):
        state = CREATE_BOT_STATES[uid]
        editing_step = state.get("editing_step", "")
        
        # Validate and update the field
        if editing_step == "bot_name":
            state["bot_name"] = user_text.strip()
        elif editing_step == "token":
            token_input = user_text.strip()
            if not token_input or ":" not in token_input:
                await update.message.reply_text("❌ Invalid token format. Must contain ':'")
                return
            state["token"] = token_input
        elif editing_step == "bot_username":
            state["bot_username"] = user_text.strip().lstrip("@")
        elif editing_step == "admin_id":
            try:
                state["admin_id"] = int(user_text.strip())
            except ValueError:
                await update.message.reply_text("❌ Must be a number.")
                return
        elif editing_step == "channel":
            state["channel"] = None if user_text.lower() == "none" else user_text.strip()
        elif editing_step == "otp_group":
            state["otp_group"] = user_text.strip()
        elif editing_step == "number_bot":
            state["number_bot"] = user_text.strip()
        elif editing_step == "support":
            state["support"] = user_text.strip()
        elif editing_step == "group_id":
            group_id_str = user_text.strip()
            if not group_id_str.lstrip("-").isdigit():
                await update.message.reply_text("❌ Invalid ID. Must be numeric.")
                return
            state["group_id"] = group_id_str
        
        # Clear editing state and go back to review
        state["step"] = "review_info"
        del state["editing_step"]
        
        # Re-show review page
        preview_text = (
            f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n"
            f"  📋 <b>Review Your Bot Information</b>\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            f"<b>1️⃣  Bot Name:</b>\n"
            f"<code>{html.escape(state.get('bot_name', '?'))}</code>\n\n"
            f"<b>2️⃣  Bot Username:</b>\n"
            f"<code>@{html.escape(state.get('bot_username', '?'))}</code>\n\n"
            f"<b>3️⃣  Bot Token:</b>\n"
            f"<code>{html.escape(state.get('token', '?')[:20])}...</code> (hidden for security)\n\n"
            f"<b>4️⃣  Your Admin ID:</b>\n"
            f"<code>{state.get('admin_id', '?')}</code>\n\n"
            f"<b>5️⃣  Channel Link:</b>\n"
            f"<code>{html.escape(state.get('channel', 'None')[:50])}</code>\n\n"
            f"<b>6️⃣  OTP Group Link:</b>\n"
            f"<code>{html.escape(state.get('otp_group', '?')[:50])}</code>\n\n"
            f"<b>7️⃣  Number Bot Link:</b>\n"
            f"<code>{html.escape(state.get('number_bot', '?')[:50])}</code>\n\n"
            f"<b>8️⃣  Support Username:</b>\n"
            f"<code>@{html.escape(state.get('support', '?')[:30])}</code>\n\n"
            f"<b>9️⃣  Group Chat ID:</b>\n"
            f"<code>{state.get('group_id', '?')}</code>\n\n"
            f"<b>━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            f"✅ <b>Updated!</b> Confirm again or edit more."
        )
        
        await update.message.reply_text(
            preview_text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅  Submit Request", callback_data=f"bot_confirm_{uid}", style="success")],
                [InlineKeyboardButton("✏️  Edit Information", callback_data=f"bot_edit_{uid}", style="primary"),
                 InlineKeyboardButton("❌  Cancel", callback_data="cancel_action", style="danger")],
            ]),
            parse_mode="HTML"
        )
        return

    # Approve/reject bot requests (super admins only)
    if data.startswith("approvebot_"):
        if not is_sup: await query.answer("Unauthorized", show_alert=True); return
        req_id = data[11:]  # Get 6-digit ID (REQ######)
        if req_id not in BOT_REQUESTS:
            await query.answer(f"❌ Request {req_id} not found or expired.", show_alert=True)
            logger.warning(f"Approve attempt for missing request: {req_id} | Available: {list(BOT_REQUESTS.keys())}"); return
        req = BOT_REQUESTS.pop(req_id)  # Remove from pending
        req["status"] = "approved"
        
        # ✅ CRITICAL FIX: Check if this is "forward from main" (no bot creation needed)
        is_forward_from_main = not req.get("has_panel", False)
        
        try:
            if is_forward_from_main:
                # ✅ FORWARD FROM MAIN MODE: Don't create bot, just add group to log chats
                group_id = int(req.get("group_id", 0))
                if group_id:
                    group_label = f"User {req['uid']} - Forward from main"
                    await db.add_log_chat(group_id, group_label)
                    
                    await query.answer("✅ Group added!")
                    await query.edit_message_text(
                        f"✅ <b>Request Approved!</b>\n\n"
                        f"👤 {html.escape(req['user_name'])} — <code>{req['uid']}</code>\n"
                        f"📱 Group: <code>{group_id}</code>\n"
                        f"🔌 Mode: Forward from main panels\n\n"
                        f"✨ Group added to log chats. OTPs will be forwarded automatically!",
                        parse_mode="HTML")
                    
                    # Notify user
                    try:
                        await context.bot.send_message(
                            chat_id=req["uid"],
                            text=(
                                f"🎉 <b>Your Request Approved!</b>\n\n"
                                f"✨ Group <code>{group_id}</code> is now active.\n"
                                f"📡 OTPs from main panels will be forwarded here.\n\n"
                                f"No separate bot needed — just use /numbers to assign!"
                            ),
                            parse_mode="HTML")
                    except Exception:
                        pass
                    if req.get("source") == "website":
                        update_website_request(
                            req_id,
                            status="approved",
                            reviewed_at=datetime.now().isoformat(),
                            reviewed_by=uid,
                        )
                else:
                    await query.answer("❌ Invalid group ID", show_alert=True)
                return
            
            # ⭐ PANEL MODE: Create a separate bot (existing logic)
            bid = f"bot_{int(time.time())}_{random.randint(1000,9999)}"
            
            # Extract all required parameters from request
            bot_name = req.get("bot_name", "Unnamed Bot")
            bot_token = req.get("token", "").strip()
            bot_username = req.get("bot_username", "?")
            admin_list = [req.get("admin_id", req.get("uid", 0))]
            channel_lnk = req.get("channel", CHANNEL_LINK)
            otp_lnk = req.get("otp_group", OTP_GROUP_LINK)
            num_bot_lnk = req.get("number_bot", "")
            supp_user = req.get("support", SUPPORT_USER)
            dev = DEVELOPER
            get_num_url = req.get("number_bot", "")
            
            # ⚠️ CRITICAL: Validate token to prevent getUpdates conflicts
            if not bot_token or bot_token.lower() == "skip":
                await query.answer(
                    f"❌ <b>Bot Creation Failed</b>\n\n"
                    f"User @{update.effective_user.username or update.effective_user.id} did not provide a bot token.\n\n"
                    f"All child bots MUST have their own unique Telegram bot token from @BotFather.\n\n"
                    f"Tell user to use /create_bot and use their own bot token (not 'skip').",
                    show_alert=True)
                logger.error(f"Bot creation rejected: no token provided by {req.get('uid')}")
                return
            
            if bot_token == BOT_TOKEN:
                await query.answer(
                    f"❌ <b>Security Error</b>\n\n"
                    f"User provided the MASTER bot token!\n\n"
                    f"Each child bot MUST have its OWN unique token from @BotFather.",
                    show_alert=True)
                logger.error(f"Bot creation rejected: user tried to use master token")
                return
            
            # ⚠️ Check if token is already running (token conflict)
            registry = bm.load_registry()
            for existing_bid, existing_info in registry.items():
                if existing_info.get("token") == bot_token:
                    await query.answer(
                        f"⚠️ <b>Token Already in Use!</b>\n\n"
                        f"Bot token <code>{bot_token[:20]}...</code> is already registered.\n\n"
                        f"<b>Options:</b>\n"
                        f"1️⃣ Create a NEW bot at @BotFather\n"
                        f"2️⃣ Ask user to use a different token\n"
                        f"3️⃣ Try the request again with correct token",
                        show_alert=True)
                    logger.error(f"Bot creation rejected: token conflict with {existing_bid}")
                    return
            
            # ⚠️ Validate channel links format
            invalid_links = []
            for link_name, link_val in [("Channel", channel_lnk), ("OTP Group", otp_lnk), 
                                        ("Number Bot", num_bot_lnk)]:
                if link_val and not (link_val.startswith("http") or link_val.startswith("t.me")):
                    invalid_links.append(f"• {link_name}: <code>{link_val}</code> (invalid)")
            
            if invalid_links:
                await query.answer(
                    f"❌ <b>Invalid Links Provided</b>\n\n"
                    + "\n".join(invalid_links) + "\n\n"
                    f"Links must start with 'http://', 'https://', or 't.me/'\n\n"
                    f"Tell user to use /create_bot again with valid links.",
                    show_alert=True)
                logger.error(f"Bot creation rejected: invalid links - {invalid_links}")
                return
            
            # Call with all required parameters
            success, folder, err = bm.create_bot_folder(
                bid, bot_name, bot_token, bot_username,
                admin_list, channel_lnk, otp_lnk,
                num_bot_lnk, supp_user, dev, get_num_url)
            
            if not success:
                await query.answer(f"❌ {err}", show_alert=True)
                logger.error(f"Bot folder creation failed: {err}")
                return
            
            # Start the bot (create_bot_folder already registered it in the registry)
            success, msg = bm.start_bot(bid)
            status_msg = f"✅ <b>Bot Request Approved & Created!</b>\n\n" if success else "✅ <b>Bot Approved!</b> (Manual start needed)\n\n"
            await query.answer("✅ Approved & Registered!")
            await query.edit_message_text(
                f"{status_msg}"
                f"👤 {html.escape(req['user_name'])} — <code>{req['uid']}</code>\n"
                f"🤖 Bot ID: <code>{bid}</code>\n"
                f"📱 Status: {'🟢 Running' if success else '🔴 Stopped'}",
                parse_mode="HTML")
            try:
                await context.bot.send_message(
                    chat_id=req["uid"],
                    text=(
                        f"🎉 <b>Your Bot is Ready!</b>\n\n"
                        f"Your bot has been created and {'started' if success else 'is waiting to be started'}.\n\n"
                        f"🤖 Bot ID: <code>{bid}</code>\n"
                        f"💬 Group: <code>{req['group_id']}</code>\n"
                        f"{'📍 Status: 🟢 Running' if success else '📍 Status: 🔴 Pending start'}"
                    ),
                    parse_mode="HTML")
            except Exception:
                pass
            if req.get("source") == "website":
                update_website_request(
                    req_id,
                    status="approved",
                    reviewed_at=datetime.now().isoformat(),
                    reviewed_by=uid,
                    deployed_bot_id=bid,
                )
        except Exception as e:
            error_msg = str(e)
            await query.answer(f"❌ Error: {error_msg[:50]}", show_alert=True)
            logger.error(f"[BOT_APPROVAL] Bot creation failed for {req_id}: {error_msg}", exc_info=True)
            # Re-add request to pending if failed
            BOT_REQUESTS[req_id] = req
        return

    if data.startswith("rejectbot_"):
        if not is_sup: await query.answer("Unauthorized", show_alert=True); return
        req_id = data[10:]  # Get 6-digit ID (REQ######)
        if req_id not in BOT_REQUESTS:
            await query.answer(f"❌ Request {req_id} not found or expired.", show_alert=True)
            logger.warning(f"Reject attempt for missing request: {req_id} | Available: {list(BOT_REQUESTS.keys())}"); return
        req = BOT_REQUESTS.pop(req_id, {})
        
        logger.info(f"[BOT_REJECTION] Request {req_id} rejected by {update.effective_user.username} | User: {req.get('uid')}")
        
        await query.answer(f"❌ Request {req_id} rejected")
        await query.edit_message_text(
            f"❌ <b>Bot Request Rejected</b>\n\n"
            f"Request ID: <code>{req_id}</code>\n"
            f"👤 {html.escape(req.get('user_name','?'))} — <code>{req.get('uid','?')}</code>\n\n"
            f"<i>User has been notified.</i>",
            parse_mode="HTML")
        try:
            await context.bot.send_message(
                chat_id=req.get("uid", 0),
                text=(
                    f"❌ <b>Your Bot Request Was Not Approved</b>\n\n"
                    f"Request ID: <code>{req_id}</code>\n\n"
                    f"Your bot creation request was reviewed and decided not to proceed at this time.\n\n"
                    f"Contact @NONEXPERTCODER for more information."
                ),
                parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Could not notify user {req.get('uid', 0)}: {e}")
        if req.get("source") == "website":
            update_website_request(
                req_id,
                status="rejected",
                reviewed_at=datetime.now().isoformat(),
                reviewed_by=uid,
            )
        return

    # ════════════════════════════════════════════════════════
    #  USER CALLBACKS  (Settings, Help, etc.)
    # ════════════════════════════════════════════════════════
    if data=="user_settings":
        await query.answer()
        logger.info(f"{emoji('settings', uid)} Settings opened | UID: {uid} | Bot Type: {'CHILD' if IS_CHILD_BOT else 'MAIN'} | Child Bot Isolated: {IS_CHILD_BOT}")
        
        # Child bot enterprise users can toggle animated emojis
        tier = get_user_tier(uid)
        emoji_pref_setting = ""
        emoji_pref_kb = []
        
        if IS_CHILD_BOT and tier == "enterprise":
            # Load current preference
            try:
                cfg = {}
                if os.path.exists(CONFIG_FILE):
                    with open(CONFIG_FILE) as f:
                        cfg = json.load(f)
                emoji_prefs = cfg.get("child_bot_emoji_prefs", {})
                is_enabled = emoji_prefs.get(str(uid), False)
            except:
                is_enabled = False
            
            emoji_status = f"{emoji('check', uid)} ENABLED" if is_enabled else f"{emoji('cancel', uid)} DISABLED"
            emoji_pref_setting = f"\n\n{emoji('settings', uid)} <b>Animated Emojis: {emoji_status}</b>\n(Enterprise Plan Feature)"
            emoji_pref_kb = [
                [InlineKeyboardButton(
                    f"{emoji('check', uid)} Enable Animated Emojis" if not is_enabled else f"{emoji('cancel', uid)} Disable Animated Emojis",
                    callback_data="toggle_animated_emoji",
                    style="success" if not is_enabled else "danger"
                )]
            ]
        
        kb_rows = emoji_pref_kb + [[InlineKeyboardButton(f"{emoji('rocket', uid)}  Back", callback_data="main_menu", style="primary")]]
        
        await query.edit_message_text(
            f"{emoji('settings', uid)} <b>User Settings</b>\n\n"
            f"{emoji('user', uid)} <b>Tier:</b> {PREMIUM_TIERS.get(tier, {}).get('name', 'Unknown')}\n"
            f"<i>User settings allow you to customize your experience.</i>\n\n"
            f"{emoji('bolt', uid)} <b>Features:</b>\n"
            f"• {emoji('book', uid)} Notification preferences\n"
            f"• {emoji('phone', uid)} Number auto-assign settings\n"
            f"• {emoji('notepad', uid)} OTP display formats"
            f"{emoji_pref_setting}\n\n"
            f"{emoji('help', uid)} Contact support for inquiries.",
            reply_markup=InlineKeyboardMarkup(kb_rows), parse_mode="HTML")
        return
    
    if data=="toggle_animated_emoji":
        await query.answer()
        if not IS_CHILD_BOT or get_user_tier(uid) != "enterprise":
            await query.answer(f"{emoji('cancel', uid)} This feature is only available for enterprise users on child bots.", show_alert=True)
            return
        
        # Toggle the preference
        try:
            cfg = {}
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE) as f:
                    cfg = json.load(f)
            
            if "child_bot_emoji_prefs" not in cfg:
                cfg["child_bot_emoji_prefs"] = {}
            
            current = cfg["child_bot_emoji_prefs"].get(str(uid), False)
            new_value = not current
            cfg["child_bot_emoji_prefs"][str(uid)] = new_value
            
            with open(CONFIG_FILE, "w") as f:
                json.dump(cfg, f, indent=2)
            
            status = f"{emoji('check', uid)} ENABLED" if new_value else f"{emoji('cancel', uid)} DISABLED"
            await query.answer(f"{emoji('check', uid)} Animated emojis {status}", show_alert=True)
            
            # Refresh settings view
            await query.edit_message_text(
                f"{emoji('settings', uid)} <b>User Settings</b>\n\n"
                f"{emoji('user', uid)} <b>Tier:</b> Enterprise\n\n"
                f"{emoji('settings', uid)} <b>Animated Emojis: {status}</b>\n"
                f"(Enterprise Plan Feature)\n\n"
                f"{emoji('check', uid)} Changes saved!",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        f"{emoji('cancel', uid)} Disable Animated Emojis" if new_value else f"{emoji('check', uid)} Enable Animated Emojis",
                        callback_data="toggle_animated_emoji",
                        style="danger" if new_value else "success"
                    )],
                    [InlineKeyboardButton(f"{emoji('rocket', uid)}  Back", callback_data="main_menu", style="primary")]
                ]),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Toggle emoji pref error: {e}")
            await query.answer(f"{emoji('cancel', uid)} Error updating preference", show_alert=True)
    
    if data=="tutorials":
        await query.answer()
        logger.info(f"📚 Tutorials menu opened | UID: {uid} | Bot Type: {'CHILD' if IS_CHILD_BOT else 'MAIN'}")
        tutorials = await db.get_all_tutorials()
        
        if not tutorials:
            await query.edit_message_text(
                "<b>📚 Tutorials & Guides</b>\n\n"
                "No tutorials available yet.\n"
                "Check back later or contact support.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠  Back", callback_data="main_menu", style="primary")
                ]]), parse_mode="HTML")
            return
        
        lines = ["<b>📚 Available Tutorials</b>\n" + D]
        kb_rows = []
        for tut in tutorials[:10]:  # Show max 10
            icon = "📄" if tut.content_type == "text" else "🎬" if tut.content_type == "video" else "📚"
            lines.append(f"{icon} <b>{html.escape(tut.title)}</b>")
            kb_rows.append([InlineKeyboardButton(
                f"{icon} {tut.title[:25]}", 
                callback_data=f"tut_view_{tut.id}", 
                style="primary"
            )])
        
        lines.append(f"\n{D}\nTotal: {len(tutorials)} tutorials")
        kb_rows.append([InlineKeyboardButton("🏠  Back", callback_data="main_menu", style="primary")])
        
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(kb_rows),
            parse_mode="HTML")
        return
    
    if data.startswith("tut_view_"):
        tut_id = int(data.split("_")[-1])
        await query.answer()
        tut = await db.get_tutorial(tut_id)
        
        if not tut:
            await query.answer("Tutorial not found", show_alert=True)
            return
        
        lines = [
            f"<b>📚 {html.escape(tut.title)}</b>",
            D,
        ]
        if tut.description:
            lines.append(f"<i>{html.escape(tut.description)}</i>\n")
        
        # Show content based on type
        if tut.content_type in ("text", "both"):
            if tut.text_content:
                lines.append(f"<b>📝 Content:</b>\n{html.escape(tut.text_content)}")
        
        kb_rows = []
        if tut.content_type in ("video", "both") and tut.video_file_id:
            kb_rows.append([InlineKeyboardButton("▶️  Watch Video", callback_data=f"tut_video_{tut.id}", style="primary")])
        
        kb_rows.append([InlineKeyboardButton("📚  Back to Tutorials", callback_data="tutorials", style="primary")])
        kb_rows.append([InlineKeyboardButton("🏠  Back to Menu", callback_data="main_menu", style="primary")])
        
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(kb_rows),
            parse_mode="HTML")
        return
    
    if data.startswith("tut_video_"):
        tut_id = int(data.split("_")[-1])
        await query.answer()
        tut = await db.get_tutorial(tut_id)
        
        if tut and tut.video_file_id:
            caption = f"<b>📚 {html.escape(tut.title)}</b>\n"
            if tut.description:
                caption += f"{html.escape(tut.description)}\n"
            
            await context.bot.send_video(
                chat_id=uid,
                video=tut.video_file_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("📚  Back", callback_data=f"tut_view_{tut.id}", style="primary")
                ]])
            )
        else:
            await query.answer("Video not found", show_alert=True)
        return
    
    if data=="my_otps":
        await query.answer()
        logger.info(f"📋 My OTPs viewed | UID: {uid} | Bot Type: {'CHILD' if IS_CHILD_BOT else 'MAIN'} | Child Bot Isolated: {IS_CHILD_BOT}")
        async with db.AsyncSessionLocal() as session:
            nums = (await session.execute(
                select(db.Number).where(db.Number.assigned_to == uid)
            )).scalars().all()
        
        if not nums:
            await query.edit_message_text(
                "<b>🔐 My OTPs</b>\n\n"
                "You don't have any active numbers yet.\n"
                "Click 'Get Number' to request one.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Get Number", callback_data="buy_menu", style="primary", icon_custom_emoji_id=_UI["fire"][0]),
                    InlineKeyboardButton("Back", callback_data="main_menu", style="primary", icon_custom_emoji_id=_UI["rocket"][0])
                ]]),
                parse_mode="HTML")
            return
        
        lines = [
            "<b>🔐 My OTP Numbers</b>",
            D,
            f"You have <b>{len(nums)}</b> active number(s):\n"
        ]
        
        kb_rows = []
        for num in nums:
            service = num.category.split(" - ")[-1].strip() if num.category else "Unknown"
            lines.append(f"📱 <code>+{num.phone_number}</code> • {service}")
            kb_rows.append([InlineKeyboardButton(
                f"+{num.phone_number[-4:]}",
                callback_data=f"otp_detail_{num.id}",
                style="primary",
                icon_custom_emoji_id=_UI["phone"][0]
            )])
        
        kb_rows.append([InlineKeyboardButton("Back", callback_data="main_menu", style="primary", icon_custom_emoji_id=_UI["rocket"][0])])
        
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(kb_rows),
            parse_mode="HTML")
        return
    
    if data.startswith("otp_detail_"):
        num_id = int(data.split("_")[-1])
        await query.answer()
        
        async with db.AsyncSessionLocal() as session:
            num = await session.scalar(
                select(db.Number).where(db.Number.id == num_id, db.Number.assigned_to == uid)
            )
        
        if not num:
            await query.answer("Number not found", show_alert=True)
            return
        
        _, country, service = split_category_label(num.category)
        status_emoji = "✅" if num.last_otp else "⏳"
        history_rows = await db.get_recent_history_for_number(num.phone_number, limit=6)
        
        lines = [
            f"<b>📱 Number Details</b>",
            D,
            f"📞 <b>Number:</b> <code>+{num.phone_number}</code>",
            f"🌍 <b>Country:</b> {country}",
            f"🏷️  <b>Service:</b> {service}",
            f"📊 <b>Status:</b> {status_emoji} {num.status}",
        ]
        
        if num.assigned_at:
            lines.append(f"⏰ <b>Assigned:</b> {num.assigned_at.strftime('%Y-%m-%d %H:%M:%S')}")
        
        if num.last_otp:
            lines.append(f"🔑 <b>Last OTP:</b> <code>{num.last_otp[:6]}</code>")
        
        if num.last_msg:
            lines.append(f"💬 <b>Last Message:</b> {num.last_msg}")
        
        if num.retention_until:
            lines.append(f"⏱️  <b>Retention Until:</b> {num.retention_until.strftime('%Y-%m-%d %H:%M:%S')}")
        history_block = format_recent_history_block(history_rows, skip_otp=num.last_otp or "", title="Recent OTPs On This Number")
        if history_block:
            lines.extend(["", history_block])
        
        kb_rows = [
            [InlineKeyboardButton("My OTPs", callback_data="my_otps", style="primary", icon_custom_emoji_id=_UI["phone"][0])],
            [InlineKeyboardButton("Back", callback_data="main_menu", style="primary", icon_custom_emoji_id=_UI["rocket"][0])]
        ]
        
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(kb_rows),
            parse_mode="HTML")
        return
    
    if data=="analytics":
        await query.answer()
        stats = await db.get_user_stats(uid)
        await query.edit_message_text(
            "<b>📈 Your Analytics</b>\n\n"
            f"<b>Total OTPs:</b> {stats.get('total', 0)}\n"
            f"<b>Successful:</b> {stats.get('success', 0)}\n"
            f"<b>Success Rate:</b> {int((stats.get('success', 0) / max(stats.get('total', 1), 1)) * 100)}%\n\n"
            "More detailed analytics coming soon!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(f"{ui('rocket')}  Back", callback_data="main_menu", style="primary")
            ]]), parse_mode="HTML")
        return
    
    if data=="premium_menu":
        await query.answer()
        logger.info(f"💎 Premium menu viewed | UID: {uid} | Bot Type: {'CHILD' if IS_CHILD_BOT else 'MAIN'} | Child Bot Isolated: {IS_CHILD_BOT}")
        await query.edit_message_text(
            "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>\n"
            "<b>💎 PREMIUM FEATURES — NOW AVAILABLE ✅</b>\n"
            "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
            
            "<b>🎨 Professional OTP Rendering</b>\n"
            "   ✅ <b>10 Unique GUI Themes</b>\n"
            "      CLASSIC, MINIMAL, DEVELOPER, ELECTRIC,\n"
            "      TECH, PREMIUM, ULTRAMINIMAL, BUSINESS,\n"
            "      SOCIAL, DELUXE\n"
            "   ✅ <b>Custom Button Combinations</b>\n"
            "      Each theme has unique layout\n"
            "   ✅ /otpguipreview - Preview all themes\n\n"
            
            "<b>📊 Real-Time Analytics</b>\n"
            "   ✅ <b>Personal Statistics</b>\n"
            "      OTP count, success rate, stats tracking\n"
            "   ✅ <b>/mystats</b> - View detailed analytics\n"
            "   ✅ <b>/myhistory</b> - OTP delivery history\n"
            "   ✅ <b>Live Dashboard</b> - Real-time updates\n\n"
            
            "<b>🚀 Advanced Features</b>\n"
            "   ✅ <b>Unlimited Phone Numbers</b>\n"
            "      Request multiple numbers simultaneously\n"
            "   ✅ <b>Multi-Country Support</b>\n"
            "      200+ countries available\n"
            "   ✅ <b>Panel Management</b>\n"
            "      Manage unlimited SMS panels\n"
            "   ✅ <b>Priority Support</b>\n"
            "      Direct developer contact available\n"
            "   ✅ <b>Custom Settings</b>\n"
            "      Personalize your experience\n\n"
            
            "<b>🔧 Advanced Tools</b>\n"
            "   ✅ <b>OTP Logger</b> - Track all received OTPs\n"
            "   ✅ <b>Auto-Assign System</b> - Get numbers instantly\n"
            "   ✅ <b>Number Blocking</b> - Mark numbers as blocked\n"
            "   ✅ <b>SMS Prefix</b> - Custom message filtering\n\n"
            
            "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>\n"
            "🌟 <b>All features are LIVE and ready to use!</b>\n"
            "Explore: /start → Main Menu\n"
            "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━━━━</b>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠  Back", callback_data="main_menu", style="primary")
            ]]), parse_mode="HTML")
        return
    
    if data=="cmd_help":
        await query.answer()
        logger.info(f"❓ Help viewed | UID: {uid} | Bot Type: {'CHILD' if IS_CHILD_BOT else 'MAIN'} | Child Bot Isolated: {IS_CHILD_BOT}")
        await query.edit_message_text(
            "<b>❓ HELP & SUPPORT</b>\n\n"
            "<b>📖 How to use Crack SMS:</b>\n"
            "1. /start - Open main menu\n"
            "2. Get Number - Request phone numbers\n"
            "3. Wait for OTPs - SMS arrives in DM\n"
            "4. Copy & use - Get OTP instantly\n\n"
            "<b>🆘 Need Help?</b>\n"
            "Contact our " + (f"<a href='https://t.me/{SUPPORT_USER.lstrip('@')}'>support team</a>" if SUPPORT_USER else "support team") + "\n\n"
            "<b>❓ Common Questions:</b>\n"
            "Q: How many numbers can I get?\n"
            "A: 4-10 depending on your tier\n\n"
            "Q: How long do numbers work?\n"
            "A: 24-48 hours typically\n\n"
            "Q: Is it really free?\n"
            "A: Yes, 100% free!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💬  Support", url=f"https://t.me/{SUPPORT_USER.lstrip('@')}" if SUPPORT_USER else "https://t.me"),
                InlineKeyboardButton("🏠  Back", callback_data="main_menu", style="primary")
            ]]), parse_mode="HTML")
        return
    
    if data=="faq":
        await query.answer()
        await query.edit_message_text(
            "<b>❓ FREQUENTLY ASKED QUESTIONS</b>\n\n"
            "<b>Q: Why can't I use numbers?</b>\n"
            "A: You must join 3 required community groups first.\n\n"
            "<b>Q: How do I get more numbers?</b>\n"
            "A: Use 'Get Number' button after releasing old ones.\n\n"
            "<b>Q: Numbers not working?</b>\n"
            "A: Some services need specific countries. Try another.\n\n"
            "<b>Q: Can I create my own bot?</b>\n"
            "A: Yes! Use 'Create My Bot' to request approval.\n\n"
            "Still need help? Contact support!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠  Back", callback_data="main_menu", style="primary")
            ]]), parse_mode="HTML")
        return
    
    # ════════════════════════════════════════════════════════
    #  ADMIN SPECIFIC CALLBACKS
    # ════════════════════════════════════════════════════════
    
    if data=="admin_home":
        await query.answer()
        perms = await get_admin_permissions(uid)
        is_sup = is_super_admin(uid)
        if not (perms or is_sup):
            await query.edit_message_text("🔒 Admin access required.", parse_mode="HTML")
            return
        # Show admin home
        await query.edit_message_text(
            "<b>👮 ADMIN PANEL</b>\n\n"
            "Select an option below to manage the bot.",
            reply_markup=admin_main_kb(perms, is_sup), parse_mode="HTML")
        return
    
    if data=="admin_tutorials":
        await query.answer()
        is_sup = is_super_admin(uid)
        if not is_sup:
            await query.answer("🔒 Super admin only", show_alert=True)
            return
        
        tutorials = await db.get_all_tutorials()
        
        lines = ["<b>📚 Manage Tutorials</b>", D]
        if tutorials:
            lines.append(f"\n<b>Existing Tutorials ({len(tutorials)}):</b>\n")
            for tut in tutorials[:10]:
                icon = "📄" if tut.content_type == "text" else "🎬" if tut.content_type == "video" else "📚"
                lines.append(f"{icon} {html.escape(tut.title[:30])}")
        else:
            lines.append("\nNo tutorials yet.")
        
        kb_rows = []
        kb_rows.append([InlineKeyboardButton("➕  Add New Tutorial", callback_data="add_tutorial", style="success")])
        
        for tut in tutorials[:5]:
            kb_rows.append([
                InlineKeyboardButton(f"🔍 {tut.title[:20]}", callback_data=f"admin_tut_{tut.id}", style="primary"),
                InlineKeyboardButton("❌", callback_data=f"del_tut_{tut.id}", style="danger")
            ])
        
        kb_rows.append([InlineKeyboardButton("🔙  Back", callback_data="admin_home", style="primary")])
        
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(kb_rows),
            parse_mode="HTML")
        return
    
    if data=="add_tutorial":
        await query.answer()
        is_sup = is_super_admin(uid)
        if not is_sup:
            await query.answer("🔒 Super admin only", show_alert=True)
            return
        
        context.user_data["tutorial_step"] = "name"
        await context.bot.send_message(uid,
            "<b>📚 Create New Tutorial</b>\n" + D + "\n\n"
            "Step 1: Enter tutorial name/title",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="admin_tutorials", style="danger")
            ]])
        )
        return
    
    if data.startswith("admin_tut_"):
        tut_id = int(data.split("_")[-1])
        await query.answer()
        tut = await db.get_tutorial(tut_id)
        
        if not tut:
            await query.answer("Tutorial not found", show_alert=True)
            return
        
        is_sup = is_super_admin(uid)
        if not is_sup:
            await query.answer("🔒 Super admin only", show_alert=True)
            return
        
        lines = [
            f"<b>📚 {html.escape(tut.title)}</b>",
            D,
            f"<b>Type:</b> {tut.content_type}",
            f"<b>Created:</b> {tut.created_at.strftime('%Y-%m-%d') if tut.created_at else 'Unknown'}",
        ]
        
        if tut.description:
            lines.append(f"<b>Description:</b> {html.escape(tut.description[:100])}")
        
        if tut.content_type in ("text", "both"):
            lines.append(f"<b>Text:</b> {html.escape(tut.text_content[:80] if tut.text_content else 'None')}")
        
        if tut.content_type in ("video", "both"):
            lines.append(f"<b>Video:</b> {'📹 Uploaded' if tut.video_file_id else 'None'}")
        
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️  Edit", callback_data=f"edit_tut_{tut.id}", style="primary")],
                [InlineKeyboardButton("❌ Delete", callback_data=f"del_tut_{tut.id}", style="danger")],
                [InlineKeyboardButton("📚  Back", callback_data="admin_tutorials", style="primary")]
            ]),
            parse_mode="HTML")
        return
    
    if data.startswith("del_tut_"):
        tut_id = int(data.split("_")[-1])
        is_sup = is_super_admin(uid)
        if not is_sup:
            await query.answer("🔒 Super admin only", show_alert=True)
            return
        
        await db.delete_tutorial(tut_id)
        await query.answer("✅ Tutorial deleted", show_alert=True)
        
        # Refresh tutorials list
        tutorials = await db.get_all_tutorials()
        lines = ["<b>📚 Manage Tutorials</b>", D]
        if tutorials:
            lines.append(f"\n<b>Existing Tutorials ({len(tutorials)}):</b>\n")
            for tut in tutorials[:10]:
                icon = "📄" if tut.content_type == "text" else "🎬" if tut.content_type == "video" else "📚"
                lines.append(f"{icon} {html.escape(tut.title[:30])}")
        
        kb_rows = []
        kb_rows.append([InlineKeyboardButton("➕  Add New Tutorial", callback_data="add_tutorial", style="success")])
        for tut in tutorials[:5]:
            kb_rows.append([
                InlineKeyboardButton(f"🔍 {tut.title[:20]}", callback_data=f"admin_tut_{tut.id}", style="primary"),
                InlineKeyboardButton("❌", callback_data=f"del_tut_{tut.id}", style="danger")
            ])
        kb_rows.append([InlineKeyboardButton("🔙  Back", callback_data="admin_home", style="primary")])
        
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(kb_rows),
            parse_mode="HTML")
        return
    
    if data=="admin_settings":
        await query.answer()
        perms = await get_admin_permissions(uid)
        is_sup = is_super_admin(uid)
        if not is_sup:
            await query.answer("🔒 Super admin only", show_alert=True)
            return
        await query.edit_message_text(
            "<b>⚙️  Admin Settings</b>\n\n"
            "Current configuration:\n\n"
            f"🎨 OTP Theme: #{OTP_GUI_THEME % 15}\n"
            f"📢 Channel: {CHANNEL_LINK or 'Not set'}\n"
            f"📋 OTP Group: {OTP_GROUP_LINK or 'Not set'}\n"
            f"🔗 Number Bot: {NUMBER_BOT_LINK or 'Not set'}\n\n"
            "Use admin commands to modify settings.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠  Back", callback_data="admin_home", style="primary")
            ]]), parse_mode="HTML")
        return
    
    if data=="admin_stats_menu":
        await query.answer()
        perms = await get_admin_permissions(uid)
        if "view_stats" not in perms and not is_super_admin(uid):
            await query.answer("No permission", show_alert=True)
            return
        analytics = await get_bot_analytics()
        await query.edit_message_text(
            "<b>📊 BOT STATISTICS</b>\n\n"
            f"👥 Active Users: {analytics.get('active_users', 0)}\n"
            f"📱 Total Numbers: {analytics.get('total_numbers', 0)}\n"
            f"✅ Available: {analytics.get('available_numbers', 0)}\n"
            f"🔗 Assigned: {analytics.get('assigned_numbers', 0)}\n"
            f"🔑 OTPs Processed: {analytics.get('total_otps_processed', 0)}\n"
            f"🔌 Active Panels: {analytics.get('active_panels', 0)}/{analytics.get('total_panels', 0)}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄  Refresh", callback_data="admin_stats_menu", style="primary"),
                InlineKeyboardButton("🏠  Back", callback_data="admin_home", style="primary")
            ]]), parse_mode="HTML")
        return
    
    if data=="start":
        await query.answer()
        logger.info(f"🏠 Start button | UID: {uid} | Bot: {'CHILD' if IS_CHILD_BOT else 'MAIN'} | Menu: COMPACT")
        await query.edit_message_text("🏠 <b>Main Menu</b>", reply_markup=main_menu_compact_kb(), parse_mode="HTML")
        return
    
    if data=="cancel":
        await query.answer()
        # Clear any pending states
        if uid in CREATE_BOT_STATES:
            del CREATE_BOT_STATES[uid]
        if uid in PANEL_ADD_STATES:
            del PANEL_ADD_STATES[uid]
        if uid in AWAITING_ADMIN_ID:
            del AWAITING_ADMIN_ID[uid]
        await query.edit_message_text(f"{ui('check')} Cancelled. Return to main menu.", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠  Main Menu", callback_data="main_menu", style="primary")
        ]]), parse_mode="HTML")
        return
    
    # ════════════════════════════════════════════════════════
    #  WHATSAPP SECTION REMOVED (v3.1 - Telegram only)
    # ════════════════════════════════════════════════════════
    if data.startswith("admin_wa") or data.startswith("wa_"):
        await query.answer("⚠️ WhatsApp features removed in v3.1. Telegram-only focus.", show_alert=True)
        return

    # ════════════════════════════════════════════════════════
    #  REQUIRED CHATS MANAGEMENT
    # ════════════════════════════════════════════════════════
    if data=="admin_req_chats":
        if not is_sup: await query.answer("Super admins only",show_alert=True); return
        await query.answer()
        lines = []
        for i, c in enumerate(REQUIRED_CHATS):
            lines.append(f"  <code>{i}</code>. {c['title']}  (<code>{c['id']}</code>)")
        txt = (
            "🚪 <b>Required Chats</b>\n\n"
            "Users must join <b>all</b> these before using the bot.\n\n"
            + ("\n".join(lines) if lines else "  <i>None configured</i>")
        )
        kb_rows = [[InlineKeyboardButton("➕  Add Chat/Channel", callback_data="req_chat_add", style="success")]]
        if REQUIRED_CHATS:
            for i in range(len(REQUIRED_CHATS)):
                kb_rows.append([InlineKeyboardButton(
                    f"🗑  Remove: {REQUIRED_CHATS[i]['title']}",
                    callback_data=f"req_chat_del_{i}", style="danger")])
        kb_rows.append([InlineKeyboardButton("🔙  Back", callback_data="admin_settings", style="primary")])
        await safe_edit(query, txt, reply_markup=InlineKeyboardMarkup(kb_rows))
        return

    if data=="req_chat_add":
        if not is_sup: await query.answer("Super admins only",show_alert=True); return
        await query.answer()
        AWAITING_REQ_CHAT[uid] = True
        await safe_edit(query,
            "🚪 <b>Add Required Chat</b>\n\n"
            "Send the chat data in this format:\n\n"
            "<code>CHAT_ID | Title | https://t.me/invite_link</code>\n\n"
            "Example:\n"
            "<code>-1001234567890 | My Channel | https://t.me/mychannel</code>\n\n"
            "The bot must be a member of the chat.\n/cancel to abort.")
        return

    if data.startswith("req_chat_del_"):
        if not is_sup: await query.answer("Super admins only",show_alert=True); return
        idx = int(data.split("_")[-1])
        if 0 <= idx < len(REQUIRED_CHATS):
            removed = REQUIRED_CHATS.pop(idx)
            save_config_key("REQUIRED_CHATS", REQUIRED_CHATS)
            await query.answer(f"✅ Removed: {removed['title']}", show_alert=True)
        else:
            await query.answer("Invalid index", show_alert=True)
        # Refresh the required chats view
        lines = []
        for i, c in enumerate(REQUIRED_CHATS):
            lines.append(f"  <code>{i}</code>. {c['title']}  (<code>{c['id']}</code>)")
        txt = (
            "🚪 <b>Required Chats</b>\n\n"
            + ("\n".join(lines) if lines else "  <i>None configured</i>")
        )
        kb_rows = [[InlineKeyboardButton("➕  Add Chat/Channel", callback_data="req_chat_add", style="success")]]
        for i in range(len(REQUIRED_CHATS)):
            kb_rows.append([InlineKeyboardButton(
                f"🗑  Remove: {REQUIRED_CHATS[i]['title']}",
                callback_data=f"req_chat_del_{i}", style="danger")])
        kb_rows.append([InlineKeyboardButton("🔙  Back", callback_data="admin_settings", style="primary")])
        await safe_edit(query, txt, reply_markup=InlineKeyboardMarkup(kb_rows))
        return

    # ════════════════════════════════════════════════════════
    #  BROADCAST MENU
    # ════════════════════════════════════════════════════════
    if data=="admin_broadcast_menu":
        if not is_sup: await query.answer("Super admins only",show_alert=True); return
        await query.answer()
        toggle_lbl = "✅ ON" if AUTO_BROADCAST_ON else "❌ OFF"
        await safe_edit(query,
            "📢 <b>Broadcast Center</b>\n"
            f"{'━'*28}\n\n"
            f"🔔 <b>Auto-Broadcast:</b> {toggle_lbl}\n"
            "Auto-broadcast on number upload\n\n"
            "<b>📤 Send Broadcast</b>\n"
            "Choose content type to broadcast\n\n"
            "<b>📚 Manage Tutorials</b>\n"
            "Create & manage user tutorials",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🔔  Toggle Auto: {toggle_lbl}",
                                      callback_data="toggle_auto_broadcast", style="success")],
                [InlineKeyboardButton("📝  Text Broadcast", callback_data="bcast_text", style="primary"),
                 InlineKeyboardButton("🖼️  Image+Text", callback_data="bcast_image", style="primary")],
                [InlineKeyboardButton("🎬  Video+Text", callback_data="bcast_video", style="primary"),
                 InlineKeyboardButton("📚  Tutorial", callback_data="bcast_tutorial", style="primary")],
                [InlineKeyboardButton("👁️  View Templates", callback_data="bcast_templates", style="primary")],
                [InlineKeyboardButton("🔙  Back", callback_data="admin_settings", style="primary")],
            ]))
        return

    if data=="toggle_auto_broadcast":
        if not is_sup: await query.answer("Super admins only",show_alert=True); return
        AUTO_BROADCAST_ON = not AUTO_BROADCAST_ON
        save_config_key("AUTO_BROADCAST_ON", AUTO_BROADCAST_ON)
        await query.answer(f"Auto-broadcast {'enabled' if AUTO_BROADCAST_ON else 'disabled'}!")
        toggle_lbl = "✅ ON" if AUTO_BROADCAST_ON else "❌ OFF"
        await safe_edit(query,
            "📢 <b>Broadcast Center</b>\n"
            f"{'━'*28}\n\n"
            f"🔔 <b>Auto-Broadcast:</b> {toggle_lbl}\n"
            "Auto-broadcast on number upload\n\n"
            "<b>📤 Send Broadcast</b>\n"
            "Choose content type to broadcast\n\n"
            "<b>📚 Manage Tutorials</b>\n"
            "Create & manage user tutorials",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🔔  Toggle Auto: {toggle_lbl}",
                                      callback_data="toggle_auto_broadcast", style="success")],
                [InlineKeyboardButton("📝  Text Broadcast", callback_data="bcast_text", style="primary"),
                 InlineKeyboardButton("🖼️  Image+Text", callback_data="bcast_image", style="primary")],
                [InlineKeyboardButton("🎬  Video+Text", callback_data="bcast_video", style="primary"),
                 InlineKeyboardButton("📚  Tutorial", callback_data="bcast_tutorial", style="primary")],
                [InlineKeyboardButton("👁️  View Templates", callback_data="bcast_templates", style="primary")],
                [InlineKeyboardButton("🔙  Back", callback_data="admin_settings", style="primary")],
            ]))
        return

    # ── BROADCASTS: Text, Image, Video, Tutorial ──
    if data in ("bcast_text", "bcast_image", "bcast_video", "bcast_tutorial"):
        if not is_sup: await query.answer("Super admins only",show_alert=True); return
        await query.answer()
        
        mode_map = {
            "bcast_text": ("📝 TEXT BROADCAST", "Type your announcement message.\n\nUse HTML formatting: <b>bold</b>, <i>italic</i>, <code>code</code>"),
            "bcast_image": ("🖼️  IMAGE + TEXT BROADCAST", "Send an image first, then add optional caption/description"),
            "bcast_video": ("🎬  VIDEO + TEXT BROADCAST", "Send a video, then add caption (title, description, link)"),
            "bcast_tutorial": ("📚  TUTORIAL BROADCAST", "Create a tutorial: send title, description, and optional media"),
        }
        
        title, desc = mode_map.get(data, ("BROADCAST", ""))
        context.user_data["bcast_mode"] = data.replace("bcast_", "")
        
        await safe_edit(query,
            f"<b>{title}</b>\n"
            f"{'━'*28}\n\n"
            f"{desc}\n\n"
            f"<i>Send your content now or click 'Cancel' to go back.</i>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌  Cancel", callback_data="admin_broadcast_menu", style="danger")]
            ]))
        return

    if data=="bcast_templates":
        if not is_sup: await query.answer("Super admins only",show_alert=True); return
        await query.answer()
        templates = (
            "<b>🎯 Announcement Template</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "✨ <b>Important Update</b>\n"
            "📅 Date: ...\n"
            "📝 Description: ...\n\n"
            
            "<b>🎁 Promo Template</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🎉 <b>Limited Time Offer</b>\n"
            "⏰ Expires: ...\n"
            "🎯 Details: ...\n\n"
            
            "<b>📢 Maintenance Template</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ <b>Maintenance Notice</b>\n"
            "🕐 Duration: ...\n"
            "📝 Details: ...\n"
        )
        await safe_edit(query, templates,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙  Back", callback_data="admin_broadcast_menu", style="primary")]
            ]))
        return
    
    # ── BROADCAST: Text Broadcast ──
    if data == "bcast_text":
        if not is_sup: await query.answer("Super admins only", show_alert=True); return
        await query.answer()
        context.user_data["bcast_mode"] = "text"
        context.user_data["awaiting_broadcast"] = True
        await context.bot.send_message(uid,
            "<b>📝 Text Broadcast</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Send the announcement text you want to broadcast to all users.\n\n"
            "<i>Supports HTML formatting: &lt;b&gt;bold&lt;/b&gt;, &lt;i&gt;italic&lt;/i&gt;, &lt;u&gt;underline&lt;/u&gt;</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_action", style="danger")]])
        )
        return
    
    # ── BROADCAST: Image + Text ──
    if data == "bcast_image":
        if not is_sup: await query.answer("Super admins only", show_alert=True); return
        await query.answer()
        context.user_data["bcast_mode"] = "image"
        context.user_data["awaiting_broadcast"] = True
        await context.bot.send_message(uid,
            "<b>🖼️  Image + Text Broadcast</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Send the image you want to broadcast.\n\n"
            "<i>You can add a caption when sending the photo.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_action", style="danger")]])
        )
        return
    
    # ── BROADCAST: Video + Text ──
    if data == "bcast_video":
        if not is_sup: await query.answer("Super admins only", show_alert=True); return
        await query.answer()
        context.user_data["bcast_mode"] = "video"
        context.user_data["awaiting_broadcast"] = True
        await context.bot.send_message(uid,
            "<b>🎬 Video + Text Broadcast</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Send the video you want to broadcast.\n\n"
            "<i>You can add a caption/description when sending the video.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_action", style="danger")]])
        )
        return
    
    # ── BROADCAST: Tutorial ──
    if data == "bcast_tutorial":
        if not is_sup: await query.answer("Super admins only", show_alert=True); return
        await query.answer()
        context.user_data["bcast_mode"] = "tutorial"
        context.user_data["awaiting_broadcast"] = True
        await context.bot.send_message(uid,
            "<b>📚 Tutorial Broadcast</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Send the tutorial content (title + description).\n\n"
            "<b>Format:</b>\n"
            "<code>Tutorial Title\n"
            "Tutorial description and steps...</code>\n\n"
            "<i>This will be broadcast as an educational resource to all users.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_action", style="danger")]])
        )
        return
    
    # ── Tutorial Type Selection ──
    if data.startswith("tut_type_"):
        tut_type = data.split("_")[-1]  # "text", "video", or "both"
        if not is_super_admin(uid):
            await query.answer("Unauthorized", show_alert=True)
            return
        
        await query.answer()
        context.user_data["tutorial_data"]["content_type"] = tut_type
        context.user_data["tutorial_step"] = "text_content" if tut_type in ("text", "both") else "video_content"
        
        if tut_type in ("text", "both"):
            await context.bot.send_message(uid,
                "<b>Step 4: Add Text Content</b>\n\n"
                "Send the tutorial text content or /skip",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data="admin_tutorials", style="danger")
                ]])
            )
        else:
            await context.bot.send_message(uid,
                "<b>Step 4: Upload Video</b>\n\n"
                "Send the video file or /skip",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data="admin_tutorials", style="danger")
                ]])
            )
        return
    
    if data=="cancel_action":
        PANEL_ADD_STATES.pop(uid,None); PANEL_EDIT_STATES.pop(uid,None)
        AWAITING_ADMIN_ID.pop(uid,None); AWAITING_LOG_ID.pop(uid,None); AWAITING_SUPER_ADMIN.pop(uid,None); AWAITING_REQ_CHAT.pop(uid,None)
        BOT_ADD_STATES.pop(uid,None)
        context.user_data["awaiting_broadcast"]=False
        context.user_data["awaiting_prefix"]=False
        context.user_data.pop("awaiting_link",None)
        context.user_data.pop("awaiting_website_setting", None)
        context.user_data.pop("bot_setlink_bid",None)
        context.user_data.pop("bot_setlink_key",None)
        context.user_data.pop("bcast_all_bots",None)
        context.user_data.pop("bcast_single_bot",None)
        await query.edit_message_text("❌ Action cancelled.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin",callback_data="admin_home", style="primary")]]))
        return

    await query.answer()

# ═══════════════════════════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════════════════════════
async def start_watcher_job(ctx):
    asyncio.create_task(active_watcher(ctx.application))

async def _delayed_child_bot_start():
    """
    Auto-restore child bots that were running before shutdown (NOT newly created ones).

    Why 5 minutes:
      The main bot spends the first 60-70 seconds doing initial panel logins.
      At 5 minutes the main bot is fully settled: all logins done, GC has run,
      RAM is at steady-state (~80-100 MB).

    ⚠️  CRITICAL FIX: Only restore bots that have a valid PID from BEFORE this startup.
      Newly approved bots are started immediately by the approval handler.
      This task should NOT auto-start newly created bots (they haven't crashed).
    """
    await asyncio.sleep(300)   # 5 minutes — main bot fully settled by then
    logger.info("🤖 Checking for child bots to restore (crash recovery only)…")

    reg = bm.load_registry()
    restored = 0
    
    for bid, info in reg.items():
        # Only restore bots that:
        # 1. Are marked as running AND
        # 2. Have a valid PID from BEFORE startup (meaning they were running and crashed)
        if info.get("status") == "running" and info.get("pid"):
            # Check if process is actually dead (crashed)
            if not bm.is_running(bid):
                ok, msg = bm.start_bot(bid)
                logger.info(f"{'▶️' if ok else '❌'} Restored crashed bot \"{info.get('name',bid)}\": {msg}")
                restored += 1
    
    if restored == 0:
        logger.info("🤖 No crashed bots to restore.")

# ═══════════════════════════════════════════════════════════
#  API SERVER INTEGRATION (AUTO-START)
# ═══════════════════════════════════════════════════════════

def start_api_server():
    """Start FastAPI server in background thread for Railway deployment."""
    import threading
    def run_api():
        try:
            import uvicorn
            from api_server import app as fastapi_app
            logger.info("🚀 Starting FastAPI API Server on port 8000...")
            uvicorn.run(
                fastapi_app,
                host="0.0.0.0",
                port=int(os.environ.get("API_PORT", 8000)),
                log_level="info"
            )
        except Exception as e:
            logger.error(f"❌ Failed to start API server: {e}")
    
    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()
    logger.info("✅ API Server thread started")

async def post_init(application):
    """Enhanced bot initialization with professional logging."""
    global app
    app = application
    await db.init_db()
    await init_panels_table()
    await migrate_panels_table()
    await init_permissions_table()
    await load_panels_from_dex_to_db()
    await refresh_panels_from_db()
    await start_ivas_workers()
    
    # Professional startup logging
    logger.info("=" * 60)
    logger.info("🚀 CRACK SMS  — PROFESSIONAL TELEGRAM BOT")
    logger.info("=" * 60)
    logger.info(f"✅ Database initialized")
    logger.info(f"✅ {len(PANELS)} SMS panels loaded")
    logger.info(f"✅ IVAS workers started")
    logger.info(f"✅ Permission system active")
    logger.info(f"📊 OTP GUI Theme: #{OTP_GUI_THEME % 15}")
    logger.info("=" * 60)
    
    # Start API server for production deployment
    start_api_server()
    
    if application.job_queue:
        application.job_queue.run_once(start_watcher_job, 10)
        application.job_queue.run_once(website_request_sync_job, 5)
        application.job_queue.run_repeating(website_request_sync_job, interval=15, first=20)
        # Schedule traffic report every 1 hour
        application.job_queue.run_repeating(
            lambda ctx: asyncio.create_task(auto_broadcast_traffic_report(application)),
            interval=3600, first=3610  # Start after 1 minute, then every 1 hour
        )
    else:
        asyncio.create_task(active_watcher(application))
        asyncio.create_task(website_request_sync_job(SimpleNamespace(application=application)))
    
    if not IS_CHILD_BOT:
        asyncio.create_task(_delayed_child_bot_start())
    
    logger.info(f"✅ Bot initialized. IS_CHILD_BOT={IS_CHILD_BOT}")

# ═══════════════════════════════════════════════════════════
#  ADVANCED PROFESSIONAL FUNCTIONS  (Analytics, Monitoring, etc)
# ═══════════════════════════════════════════════════════════

async def get_bot_analytics() -> dict:
    """Comprehensive bot analytics for admin dashboard."""
    try:
        stats = await db.get_stats()
        total_users = await db.get_all_users()
        admins = await list_all_admins()

        # Use AsyncSessionLocal directly — db.async_scalar does not exist
        async with db.AsyncSessionLocal() as _s:
            from sqlalchemy import func as _func
            total_otps = await _s.scalar(
                select(_func.count(db.History.id))
            ) or 0

        return {
            "total_numbers":        stats.get("total", 0),
            "available_numbers":    stats.get("available", 0),
            "assigned_numbers":     stats.get("assigned", 0),
            "active_users":         len(total_users),
            "total_otps_processed": total_otps,
            "admin_count":          len(admins),
            "active_panels":        len([p for p in PANELS if p.is_logged_in]),
            "total_panels":         len(PANELS),
        }
    except Exception as e:
        logger.error(f"Analytics error: {e}")
        return {}

async def get_system_health() -> dict:
    """System health check for monitoring."""
    try:
        import psutil as _psutil
    except ImportError:
        return {"status": "unavailable", "note": "pip install psutil"}
    try:
        process = _psutil.Process()
        return {
            "cpu_percent": process.cpu_percent(interval=0.5),
            "memory_mb": process.memory_info().rss / 1024 / 1024,
            "memory_percent": process.memory_percent(),
            "num_threads": process.num_threads(),
            "open_files": len(process.open_files()),
        }
    except:
        return {"status": "unavailable"}

async def send_analytics_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Professional analytics report for super admins."""
    uid = update.effective_user.id
    if not is_super_admin(uid):
        await update.message.reply_text(get_message("no_permission"), parse_mode="HTML")
        return
    
    analytics = await get_bot_analytics()
    health = await get_system_health()
    
    report = (
        "<b>📊 PROFESSIONAL ANALYTICS REPORT</b>\n"
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
        "<b>📈 Bot Statistics:</b>\n"
        f"👥 Active Users: <code>{analytics.get('active_users', 0)}</code>\n"
        f"📱 Total Numbers: <code>{analytics.get('total_numbers', 0)}</code>\n"
        f"✅ Available: <code>{analytics.get('available_numbers', 0)}</code>\n"
        f"🔗 Assigned: <code>{analytics.get('assigned_numbers', 0)}</code>\n"
        f"🔑 OTPs Processed: <code>{analytics.get('total_otps_processed', 0)}</code>\n\n"
        "<b>🔌 Panel Status:</b>\n"
        f"🟢 Active: <code>{analytics.get('active_panels', 0)}/{analytics.get('total_panels', 0)}</code>\n\n"
        "<b>🖥️  System Health:</b>\n"
        f"💻 CPU: <code>{health.get('cpu_percent', 'N/A')}%</code>\n"
        f"🧠 Memory: <code>{health.get('memory_mb', 0):.1f}MB</code>\n"
        f"🔧 Threads: <code>{health.get('num_threads', 0)}</code>\n\n"
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"⏰ Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    
    await update.message.reply_text(report, parse_mode="HTML")

# Runtime-safe start/admin overrides


async def send_join_required(update_or_query, bot, uid: int, missing: list):
    """Send the membership gate with HTML and true plain-text fallbacks."""
    chat_links = "\n".join(
        f"  • <a href='{html.escape(str(chat.get('link', '')), quote=True)}'>{html.escape(str(chat.get('title', 'Community')))}</a>"
        for chat in missing
    )
    html_text = (
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n"
        f"{ui('lock')} <b>Access Required</b>\n"
        "<b>━━━━━━━━━━━━━━━━━━━━━━━━</b>\n\n"
        "To use <b>Crack SMS</b> you must join our communities:\n\n"
        f"{chat_links}\n\n"
        f"After joining, tap {ui('check')} <b>I've Joined</b> below."
    )
    plain_lines = ["Access Required", "", "Join the following communities first:"]
    for chat in missing:
        plain_lines.append(f"- {chat.get('title', 'Community')}: {chat.get('link', '')}")
    plain_lines.append("")
    plain_lines.append("After joining, tap I've Joined below.")
    plain_text = "\n".join(plain_lines)
    markup = join_required_kb(missing)

    async def _try_send(fn, **kw):
        try:
            await fn(
                html_text,
                reply_markup=markup,
                parse_mode="HTML",
                disable_web_page_preview=True,
                **kw,
            )
            logger.info(f"join-required message sent | UID: {uid} | Missing: {len(missing)}")
        except TelegramBadRequest as e:
            logger.error(f"send_join_required HTML error: {e}")
            try:
                await fn(
                    plain_text,
                    reply_markup=markup,
                    disable_web_page_preview=True,
                    _skip_ui_prepare=True,
                    **kw,
                )
                logger.info(f"join-required plain fallback sent | UID: {uid}")
            except Exception as e2:
                logger.error(f"send_join_required plain fallback: {e2}")
        except Exception as e:
            logger.error(f"send_join_required: {e}")

    if hasattr(update_or_query, "message") and update_or_query.message:
        await _try_send(update_or_query.message.reply_text)
    elif hasattr(update_or_query, "edit_message_text"):
        await _try_send(update_or_query.edit_message_text)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    uid = update.effective_user.id
    perms = await get_admin_permissions(uid)
    sup = is_super_admin(uid)
    if not perms and not sup:
        await update.message.reply_text(
            f"{ui('cancel')} <b>Access Denied</b>\n<i>Admin privileges required</i>",
            parse_mode="HTML",
        )
        return

    if IS_CHILD_BOT and sup:
        await update.message.reply_text(
            f"{ui('info')} <b>Not Available in Child Bot</b>\n\n"
            "This feature is only available in the main bot.\n"
            "Child bots have their own dedicated admin panel.",
            parse_mode="HTML",
        )
        return

    role = f"{ui('crown')} Super Admin" if sup else f"{ui('shield')} Admin"
    stats = await db.get_stats()
    panel_cnt = len(PANELS)
    run_cnt = len([
        p for p in PANELS
        if p.is_logged_in or (p.panel_type == "ivas" and p.name in IVAS_TASKS and not IVAS_TASKS[p.name].done())
    ])
    lines = [
        f"<b>{ui('shield')} Admin Panel</b>",
        f"{ui('user')} {role} • ID: <code>{uid}</code>",
        "",
        f"{ui('phone')} Numbers: <b>{stats.get('available', 0)}</b>",
        f"{ui('satellite')} Panels: <b>{run_cnt}/{panel_cnt}</b> online",
    ]
    if not IS_CHILD_BOT:
        lines.append(f"{ui('robot')} Bots: <b>{len(bm.list_bots())}</b>")

    html_text = "\n".join(lines)
    markup = admin_main_kb(perms, sup)
    try:
        await update.message.reply_text(html_text, reply_markup=markup, parse_mode="HTML")
        logger.info(f"/admin panel sent | UID: {uid}")
        return
    except TelegramBadRequest as e:
        logger.error(f"/admin HTML send failed | UID: {uid} | Error: {e}")

    plain_text = re.sub(r"<[^>]+>", "", html_text)
    try:
        await update.message.reply_text(plain_text, _skip_ui_prepare=True)
        logger.info(f"/admin plain fallback sent | UID: {uid}")
    except Exception as e:
        logger.error(f"/admin plain fallback failed | UID: {uid} | Error: {e}", exc_info=True)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Robust /start with membership gate and safe plain-text fallback."""
    if not update.message:
        return

    uid = update.effective_user.id
    raw_name = (update.effective_user.first_name or "User")[:60]
    safe_name = html.escape(raw_name)
    logger.info(f"/start handler called | UID: {uid}")
    await db.add_user(uid)

    try:
        missing = await check_membership(context.bot, uid)
    except Exception as e:
        logger.error(f"check_membership in /start failed: {e}", exc_info=True)
        missing = []

    if missing:
        await send_join_required(update, context.bot, uid, missing)
        logger.info(f"/start membership gate triggered | UID: {uid} | Missing: {len(missing)}")
        return

    bot_name = f"@{BOT_USERNAME}" if BOT_USERNAME else "@CrackSMSReBot"
    bot_label = "CRACK SMS" if not IS_CHILD_BOT else html.escape(bot_name.lstrip("@"))
    perms = await get_admin_permissions(uid)
    sup = is_super_admin(uid)
    role_line = (
        f"\n{ui('crown')} <b>Super Admin</b>"
        if sup else (f"\n{ui('shield')} <b>Admin</b>" if perms else "")
    )
    limit_badge = f"{ui('copy')} <b>Bot Assign Limit:</b> {get_assign_limit_label()}"

    if IS_CHILD_BOT:
        parts = [
            f"{ui('robot')} <b>{bot_label}</b>\n\n",
            f"{ui('user')} Welcome, <a href='tg://user?id={uid}'><b>{safe_name}</b></a>{role_line}\n\n",
            f"{limit_badge}\n",
        ]
        if CHANNEL_LINK:
            parts.append(f"{ui('megaphone')} <a href='{CHANNEL_LINK}'>Channel</a>\n")
        if OTP_GROUP_LINK:
            parts.append(f"{ui('chat')} <a href='{OTP_GROUP_LINK}'>Community</a>\n")
        if DEVELOPER and DEVELOPER not in ("@", ""):
            parts.append(f"{ui('rocket')} <a href='https://t.me/{DEVELOPER.lstrip('@')}'>Developer</a>\n")
        if SUPPORT_USER and SUPPORT_USER not in ("@", ""):
            parts.append(f"{ui('help')} <a href='https://t.me/{SUPPORT_USER.lstrip('@')}'>Support</a>\n")
        parts.append(f"\n{ui('bolt')} Real-time OTP  |  200+ Countries  |  Auto-assign")
        welcome_html = "".join(parts)
    else:
        welcome_html = (
            f"{ui('diamond')} <b>{bot_label}</b>  {bot_name}{role_line}\n\n"
            f"{limit_badge}\n\n"
            f"{ui('bolt')} Real-time OTP  |  200+ Countries  |  Auto-assign\n"
            f"{ui('next')} <b>Get Started</b>"
        )

    try:
        await update.message.reply_text(
            welcome_html,
            reply_markup=main_menu_kb(),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info(f"/start welcome sent with HTML keyboard | UID: {uid}")
        return
    except TelegramBadRequest as e:
        logger.error(f"/start HTML send failed | UID: {uid} | Error: {e}")

    plain_text = re.sub(r"<[^>]+>", "", welcome_html)
    try:
        await update.message.reply_text(plain_text, _skip_ui_prepare=True)
        logger.info(f"/start plain fallback sent | UID: {uid}")
        return
    except Exception as e:
        logger.error(f"/start plain fallback failed | UID: {uid} | Error: {e}", exc_info=True)

    try:
        await update.message.reply_text(
            "Welcome to the bot. Use /start to see the menu.",
            _skip_ui_prepare=True,
        )
        logger.info(f"/start emergency fallback sent | UID: {uid}")
    except Exception as e:
        logger.error(f"/start emergency fallback failed | UID: {uid} | Error: {e}", exc_info=True)


def build_bot_request_admin_summary(req: dict) -> str:
    source = "Website" if req.get("source") == "website" else "Bot"
    contact_method = _safe_short(req.get("contact_method") or "Pending")
    contact_target = _safe_short(req.get("contact_target") or "—")
    request_id = html.escape(str(req.get("req_id", "?")))
    requester = html.escape(str(req.get("user_name", "Unknown requester")))
    requester_uid = html.escape(str(req.get("uid", "—")))
    created_at = _safe_short(req.get("created_at") or "—", 25)
    header = (
        f"{ui('robot')} <b>Bot Creation Request</b>\n{D}\n"
        f"{ui('desktop')} Source: <b>{html.escape(source)}</b>\n"
        f"{ui('copy')} Request ID: <code>{request_id}</code>\n"
        f"{ui('user')} Requester: <b>{requester}</b> (<code>{requester_uid}</code>)\n"
        f"{ui('calendar')} Created: <code>{html.escape(created_at)}</code>\n"
        f"{ui('phone')} Contact: <b>{html.escape(contact_method.title())}</b> • <code>{html.escape(contact_target)}</code>\n"
    )
    if not req.get("has_panel", False):
        return header + (
            f"{ui('satellite')} Route: <b>Forward From Main Panels</b>\n"
            f"{ui('focus')} Group ID: <code>{html.escape(str(req.get('group_id', '—')))}</code>\n"
            f"{ui('notepad')} This request only needs OTP forwarding from the main panels."
        )
    token_preview = html.escape(str(req.get("token", "?"))[:20])
    return header + (
        f"{ui('satellite')} Route: <b>Own Panel / Separate Bot</b>\n"
        f"{ui('robot')} Bot Name: <code>{html.escape(str(req.get('bot_name', '?')))}</code>\n"
        f"{ui('robot')} Username: <code>@{html.escape(str(req.get('bot_username', '?')).lstrip('@'))}</code>\n"
        f"{ui('key')} Token: <code>{token_preview}...</code>\n"
        f"{ui('people')} Admin ID: <code>{html.escape(str(req.get('admin_id', '?')))}</code>\n"
        f"{ui('megaphone')} Channel: <code>{html.escape(_safe_short(req.get('channel'), 55))}</code>\n"
        f"{ui('chat')} OTP Group: <code>{html.escape(_safe_short(req.get('otp_group'), 55))}</code>\n"
        f"{ui('receiver')} Number Bot: <code>{html.escape(_safe_short(req.get('number_bot'), 55))}</code>\n"
        f"{ui('support')} Support: <code>{html.escape(_safe_short(req.get('support'), 40))}</code>\n"
        f"{ui('focus')} Group ID: <code>{html.escape(str(req.get('group_id', '—')))}</code>"
    )

# Advanced callback handler for analytics

if __name__=="__main__":
    PROCESSED_MESSAGES=init_seen_db()
    application=(ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build())
    application.add_handler(CommandHandler("start",         cmd_start))
    application.add_handler(CommandHandler("skip",           cmd_skip))
    application.add_handler(CommandHandler("cancel",         cmd_cancel))
    application.add_handler(CommandHandler("admin",         cmd_admin))
    application.add_handler(CommandHandler("addadmin",      cmd_add_admin))
    application.add_handler(CommandHandler("removeadmin",   cmd_rm_admin))
    application.add_handler(CommandHandler("listadmins",    cmd_list_admins))
    application.add_handler(CommandHandler("setuserlimit",  cmd_setuserlimit))
    application.add_handler(CommandHandler("addlogchat",    cmd_add_log))
    application.add_handler(CommandHandler("removelogchat", cmd_rm_log))
    application.add_handler(CommandHandler("listlogchats",  cmd_list_logs))
    application.add_handler(CommandHandler("pending",       cmd_pending))
    application.add_handler(CommandHandler("dox",           cmd_dox))
    application.add_handler(CommandHandler("test1",         cmd_test1))
    application.add_handler(CommandHandler("send1",         cmd_send1))
    application.add_handler(CommandHandler("otpfor",        cmd_otpfor))
    application.add_handler(CommandHandler("groups",        cmd_groups))
    application.add_handler(CommandHandler("addgroup",      cmd_addgrp))
    application.add_handler(CommandHandler("removegroup",   cmd_rmgrp))
    application.add_handler(CommandHandler("set_channel",   cmd_set_channel))
    application.add_handler(CommandHandler("set_otpgroup",  cmd_set_otpgroup))
    application.add_handler(CommandHandler("set_numberbot", cmd_set_numbot))
    application.add_handler(CommandHandler("bots",          cmd_bots))
    application.add_handler(CommandHandler("startbot",      cmd_startbot))
    application.add_handler(CommandHandler("stopbot",       cmd_stopbot))
    # ── NEW ENHANCED COMMANDS ───────────────────────────────────
    application.add_handler(CommandHandler("help",          cmd_help))
    application.add_handler(CommandHandler("info",          cmd_info))
    application.add_handler(CommandHandler("perfstats",     cmd_perfstats))
    application.add_handler(CommandHandler("clearcache",    cmd_clearcache))
    application.add_handler(CommandHandler("systemhealth",  cmd_systemhealth))
    application.add_handler(CommandHandler("otpguipreview", cmd_otp_gui_preview))
    application.add_handler(CommandHandler("mystats",       cmd_mystats))
    application.add_handler(CommandHandler("myhistory",     cmd_myhistory))
    application.add_handler(CommandHandler("myprofile",     cmd_profile))
    application.add_handler(CommandHandler("usersearch",    cmd_user_search))
    application.add_handler(CommandHandler("topusers",      cmd_top_users))
    application.add_handler(CommandHandler("panelhealth",   cmd_panel_health))
    application.add_handler(CommandHandler("sysinfo",       cmd_system_info))
    # ────────────────────────────────────────────────────────────
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.Document.MimeType("text/plain"), handle_document))
    application.add_handler(CallbackQueryHandler(callback_handler))

    # ── Global error handler ──────────────────────────────────────
    # Without this, any unhandled exception in a PTB callback prints
    # "No error handlers are registered, logging exception" and the
    # traceback never reaches our logger properly.
    async def ptb_error_handler(update, context):
        logger.error(f"❌ PTB unhandled exception: {context.error}", exc_info=context.error)
        # Optionally notify super admins
        for admin_id in INITIAL_ADMIN_IDS:
            try:
                err_txt = str(context.error)[:300]
                await context.bot.send_message(
                    chat_id=admin_id,
                    text=f"⚠️ <b>Bot Error</b>\n<code>{html.escape(err_txt)}</code>",
                    parse_mode="HTML")
            except Exception:
                pass

    application.add_error_handler(ptb_error_handler)
    application.run_polling(drop_pending_updates=True)
