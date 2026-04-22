# bot_manager.py � 
"""
Creates and manages independent child bot processes.

Each child bot runs in its own folder with its own database,
config.json, and log file. IS_CHILD_BOT=True in config hides
the multi-bot management panel from child bot admins.
"""
import os, re, json, shutil, logging, subprocess
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

BOTS_DIR      = "bots"
REGISTRY_FILE = "bots_registry.json"
COPY_FILES    = ["bot.py","bot_manager.py","database.py","utils.py","requirements.txt","countries.json"]

PROCESSES: dict = {}   # {bot_id: subprocess.Popen}


# -- Registry --------------------------------------------------
def load_registry() -> dict:
    if os.path.exists(REGISTRY_FILE):
        try:
            with open(REGISTRY_FILE) as f: return json.load(f)
        except Exception: pass
    return {}

def _save_reg(reg: dict):
    with open(REGISTRY_FILE,"w") as f: json.dump(reg, f, indent=2)

# Public alias used by bot.py callback handler
save_registry = _save_reg

def _set(bot_id: str, key: str, val):
    reg = load_registry()
    if bot_id in reg: reg[bot_id][key]=val; _save_reg(reg)


# -- Queries ---------------------------------------------------
def get_bot_info(bot_id: str) -> Optional[dict]:
    return load_registry().get(bot_id)

def list_bots() -> list:
    reg = load_registry()
    out = []
    for bid, info in reg.items():
        e = dict(info); e["id"]=bid; e["running"]=is_running(bid)
        out.append(e)
    return out

def is_running(bot_id: str) -> bool:
    p = PROCESSES.get(bot_id)
    return p is not None and p.poll() is None

def get_bot_log(bot_id: str, lines: int = 35) -> str:
    info = get_bot_info(bot_id)
    if not info: return "Bot not found."
    lp = os.path.join(info["folder"],"bot.log")
    if not os.path.exists(lp): return "No log yet."
    try:
        all_l = open(lp, errors="replace").readlines()
        return "".join(all_l[-lines:]).strip() or "(empty)"
    except Exception as e: return f"Error: {e}"


# -- Lifecycle -------------------------------------------------
def start_bot(bot_id: str) -> tuple:
    info = get_bot_info(bot_id)
    if not info: return False, "Bot not found."
    if is_running(bot_id): return False, "Already running."
    folder = info.get("folder","")
    bp = os.path.join(folder,"bot.py")
    if not os.path.exists(bp): return False, f"bot.py not found at {bp}"
    try:
        lf = open(os.path.join(folder,"bot.log"),"a")
        proc = subprocess.Popen(["python3","-u","bot.py"], cwd=folder, stdout=lf, stderr=lf, text=True)
        PROCESSES[bot_id] = proc
        _set(bot_id,"status","running"); _set(bot_id,"pid",proc.pid)
        logger.info(f"?? Started '{info['name']}' PID={proc.pid}")
        return True, f"Started (PID {proc.pid})"
    except Exception as e:
        logger.error(f"Start fail '{bot_id}': {e}")
        return False, str(e)

def stop_bot(bot_id: str) -> tuple:
    proc = PROCESSES.get(bot_id)
    if proc is None or proc.poll() is not None: return False, "Not running."
    try:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill()
        PROCESSES.pop(bot_id, None)
        _set(bot_id,"status","stopped"); _set(bot_id,"pid",None)
        info = get_bot_info(bot_id) or {}
        logger.info(f"? Stopped '{info.get('name',bot_id)}'")
        return True, "Stopped."
    except Exception as e: return False, str(e)

def restart_bot(bot_id: str) -> tuple:
    stop_bot(bot_id)
    return start_bot(bot_id)

def delete_bot(bot_id: str) -> tuple:
    stop_bot(bot_id)
    info = get_bot_info(bot_id)
    if info:
        folder = info.get("folder","")
        if folder and os.path.isdir(folder):
            try: shutil.rmtree(folder)
            except Exception as e: logger.warning(f"Folder delete fail: {e}")
    reg = load_registry(); reg.pop(bot_id,None); _save_reg(reg)
    return True, "Bot deleted."

def restart_all_bots():
    """Called on master startup � relaunch bots that were running."""
    for bid, info in load_registry().items():
        if info.get("status") == "running":
            ok, msg = start_bot(bid)
            logger.info(f"Auto-restore '{info['name']}': {msg}")


# -- Creation --------------------------------------------------
def create_bot_folder(bot_id: str, name: str, token: str, bot_username: str,
                      admin_ids: list, channel_link: str, otp_group_link: str,
                      number_bot_link: str, support_user: str,
                      developer: str, get_number_url: str) -> tuple:
    """
    Copy files into a new child bot folder and write config.json.
    Returns (success: bool, folder_path: str, error_msg: str)
    """
    os.makedirs(BOTS_DIR, exist_ok=True)
    folder = os.path.join(BOTS_DIR, f"BOT_{_safe(name)}_{bot_id[:6]}")
    os.makedirs(folder, exist_ok=True)

    parent  = os.path.dirname(os.path.abspath(__file__))
    missing = []
    for fname in COPY_FILES:
        src = os.path.join(parent, fname)
        dst = os.path.join(folder, fname)
        if os.path.exists(src):
            shutil.copy2(src, dst)
        elif fname in ("bot.py","database.py","utils.py","bot_manager.py"):
            missing.append(fname)

    if missing:
        shutil.rmtree(folder, ignore_errors=True)
        return False, folder, f"Missing required files: {', '.join(missing)}"

    # Build child config � all keys the bot reads from config.json
    config = {
        # -- Bot identity ------------------------------
        "IS_CHILD_BOT":    True,
        "BOT_TOKEN":       token,
        "BOT_USERNAME":    bot_username.lstrip("@"),
        "bot_name":        name,
        # -- Admins -----------------------------------
        "ADMIN_IDS":       admin_ids,
        # -- Links shown in OTP messages & menus ------
        # NOTE: Child bots use empty defaults - admin must configure these from child bot settings
        "CHANNEL_LINK":    channel_link or "",
        "OTP_GROUP_LINK":  otp_group_link or "",
        "GET_NUMBER_URL":  get_number_url or number_bot_link or "",
        "NUMBER_BOT_LINK": number_bot_link or "",
        "SUPPORT_USER":    support_user    or "@ownersigma",
        "DEVELOPER":       developer       or "@NONEXPERTCODER",
        # -- Defaults ---------------------------------
        "default_limit":   4,
        "created_at":      datetime.now().isoformat(),
    }
    with open(os.path.join(folder,"config.json"),"w") as f:
        json.dump(config, f, indent=2)

    # Register
    reg = load_registry()
    reg[bot_id] = {
        "name":           name,
        "token":          token,
        "bot_username":   bot_username,
        "admin_ids":      admin_ids,
        "channel_link":   channel_link,
        "otp_group_link": otp_group_link,
        "number_bot_link":number_bot_link,
        "support_user":   support_user,
        "developer":      developer,
        "folder":         folder,
        "status":         "stopped",
        "created_at":     datetime.now().isoformat(),
    }
    _save_reg(reg)
    logger.info(f"? Created child bot '{name}' at {folder}")
    return True, folder, ""


def _safe(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]","_",name)[:32]
