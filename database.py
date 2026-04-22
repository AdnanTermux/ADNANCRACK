# database.py — Crack SMS V11
import os
from datetime import datetime, timedelta
from typing import Optional, List
from sqlalchemy import (Column, Integer, String, DateTime, Float,
                        ForeignKey, select, delete, func, text as stext)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship

DB_FILE   = os.environ.get("DATABASE_URL", "bot_database.db")
# Railway may provide a Postgres URL; fall back to SQLite
if DB_FILE.startswith("postgres://"):
    DB_FILE = DB_FILE.replace("postgres://", "postgresql+asyncpg://", 1)
    ENGINE  = create_async_engine(DB_FILE, pool_pre_ping=True)
elif DB_FILE.startswith("postgresql://"):
    DB_FILE = DB_FILE.replace("postgresql://", "postgresql+asyncpg://", 1)
    ENGINE  = create_async_engine(DB_FILE, pool_pre_ping=True)
else:
    ENGINE = create_async_engine(
        f"sqlite+aiosqlite:///{DB_FILE}",
        connect_args={"check_same_thread": False}
    )

AsyncSessionLocal = async_sessionmaker(ENGINE, class_=AsyncSession, expire_on_commit=False)

class Base(DeclarativeBase): pass

class User(Base):
    __tablename__ = "users"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    user_id      = Column(Integer, unique=True, nullable=False)
    joined_at    = Column(DateTime, default=datetime.now)
    custom_limit = Column(Integer, nullable=True)
    prefix       = Column(String, nullable=True)
    numbers      = relationship("Number", back_populates="user_rel", foreign_keys="Number.assigned_to",
                                 primaryjoin="User.user_id==Number.assigned_to", lazy="select")

class Number(Base):
    __tablename__ = "numbers"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    phone_number = Column(String, unique=True, nullable=False)
    category     = Column(String, nullable=False, default="")
    status       = Column(String, default="AVAILABLE")
    assigned_to  = Column(Integer, ForeignKey("users.user_id"), nullable=True)
    assigned_at  = Column(DateTime, nullable=True)
    retention_until = Column(DateTime, nullable=True)
    message_id   = Column(Integer, nullable=True)
    last_msg     = Column(String, nullable=True)
    last_otp     = Column(String, nullable=True)
    user_rel     = relationship("User", foreign_keys=[assigned_to],
                                 primaryjoin="Number.assigned_to==User.user_id", lazy="select")

class History(Base):
    __tablename__ = "history"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    user_id      = Column(Integer, nullable=False)
    phone_number = Column(String, nullable=False)
    otp          = Column(String, nullable=True)
    category     = Column(String, nullable=True)
    created_at   = Column(DateTime, default=datetime.now)

class LogChat(Base):
    __tablename__ = "log_chats"
    id      = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(Integer, unique=True, nullable=False)
    label   = Column(String, nullable=True)

class AdminPermission(Base):
    __tablename__ = "admin_permissions"
    id          = Column(Integer, primary_key=True, autoincrement=True)
    user_id     = Column(Integer, unique=True, nullable=False)
    permissions = Column(String, default="")   # JSON list of permission keys

class Tutorial(Base):
    __tablename__ = "tutorials"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    title        = Column(String, nullable=False)
    description  = Column(String, nullable=True)
    content_type = Column(String, default="text")  # "text", "video", "photo", "both"
    text_content = Column(String, nullable=True)
    photo_file_id = Column(String, nullable=True)
    video_file_id = Column(String, nullable=True)
    created_by   = Column(Integer, nullable=False)
    created_at   = Column(DateTime, default=datetime.now)

class APIToken(Base):
    __tablename__ = "api_tokens"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    token        = Column(String, unique=True, nullable=False)  # Random token
    name         = Column(String, nullable=False)  # Display name
    created_by   = Column(Integer, nullable=False)  # User/Admin who created it
    created_at   = Column(DateTime, default=datetime.now)
    last_used    = Column(DateTime, nullable=True)
    status       = Column(String, default="ACTIVE")  # ACTIVE, BLOCKED, DELETED
    api_dev      = Column(String, nullable=True)  # Developer name/identifier
    panels_data  = Column(String, nullable=True)  # JSON list of selected panel IDs

async def init_db():
    async with ENGINE.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# ── Users ─────────────────────────────────────────────────────
async def add_user(user_id: int):
    async with AsyncSessionLocal() as s:
        if not await s.scalar(select(User).where(User.user_id == user_id)):
            s.add(User(user_id=user_id))
            await s.commit()

async def get_all_users() -> List[int]:
    async with AsyncSessionLocal() as s:
        rows = await s.execute(select(User.user_id))
        return [r[0] for r in rows.fetchall()]

async def get_stats() -> dict:
    async with AsyncSessionLocal() as s:
        total     = await s.scalar(select(func.count(Number.id))) or 0
        available = await s.scalar(select(func.count(Number.id)).where(Number.status == "AVAILABLE")) or 0
        assigned  = await s.scalar(select(func.count(Number.id)).where(Number.status == "ASSIGNED"))  or 0
        users     = await s.scalar(select(func.count(User.id))) or 0
        return {"total": total, "available": available, "assigned": assigned, "users": users}

async def get_user_stats(user_id: int) -> dict:
    async with AsyncSessionLocal() as s:
        total = await s.scalar(select(func.count(History.id)).where(History.user_id == user_id)) or 0
        return {"success": total, "total": total}

async def set_user_prefix(user_id: int, prefix):
    async with AsyncSessionLocal() as s:
        u = await s.scalar(select(User).where(User.user_id == user_id))
        if u: u.prefix = prefix; await s.commit()

async def get_user_prefix(user_id: int):
    async with AsyncSessionLocal() as s:
        return await s.scalar(select(User.prefix).where(User.user_id == user_id))

async def set_user_limit(user_id: int, limit):
    async with AsyncSessionLocal() as s:
        u = await s.scalar(select(User).where(User.user_id == user_id))
        if u: u.custom_limit = limit; await s.commit()

async def get_user_limit(user_id: int):
    async with AsyncSessionLocal() as s:
        return await s.scalar(select(User.custom_limit).where(User.user_id == user_id))

# ── Numbers ───────────────────────────────────────────────────
async def add_numbers_bulk(lines: list, category: str) -> int:
    """Fast bulk insert via INSERT OR IGNORE — skips duplicates."""
    nums = []
    for line in lines:
        n = str(line).strip().replace(" ", "").replace("-", "").lstrip("+")
        if n.isdigit() and 5 <= len(n) <= 20:
            nums.append(n)
    if not nums:
        return 0
    added = 0
    chunk = 500
    async with AsyncSessionLocal() as s:
        for i in range(0, len(nums), chunk):
            batch = nums[i:i+chunk]
            result = await s.execute(
                stext("INSERT OR IGNORE INTO numbers (phone_number, category, status) "
                      "VALUES (:num, :cat, 'AVAILABLE')"),
                [{"num": n, "cat": category} for n in batch]
            )
            added += result.rowcount
            await s.commit()
    return added

async def count_available(category: str) -> int:
    async with AsyncSessionLocal() as s:
        return await s.scalar(
            select(func.count(Number.id)).where(
                Number.category == category, Number.status == "AVAILABLE")) or 0

async def get_categories_summary() -> list:
    async with AsyncSessionLocal() as s:
        return (await s.execute(
            select(Number.category, func.count(Number.id))
            .where(Number.status == "AVAILABLE")
            .group_by(Number.category)
        )).all()

async def delete_category(category: str) -> int:
    async with AsyncSessionLocal() as s:
        r = await s.execute(delete(Number).where(Number.category == category))
        await s.commit()
        return r.rowcount

async def check_prefix_availability(category: str, prefix: str) -> int:
    async with AsyncSessionLocal() as s:
        return await s.scalar(
            select(func.count(Number.id)).where(
                Number.category == category,
                Number.status   == "AVAILABLE",
                Number.phone_number.like(f"{prefix}%"))) or 0

async def request_number(user_id: int, category_hint: str = None):
    async with AsyncSessionLocal() as s:
        if category_hint and category_hint != "Check":
            active = await s.scalar(select(Number).where(
                Number.assigned_to == user_id,
                Number.status.in_(["ASSIGNED", "RETENTION"]),
                Number.category == category_hint))
        else:
            active = await s.scalar(select(Number).where(
                Number.assigned_to == user_id,
                Number.status.in_(["ASSIGNED", "RETENTION"])))
        if active:
            return active.phone_number, active.category, "active"
        q = select(Number).where(Number.status == "AVAILABLE")
        if category_hint and category_hint != "Check":
            q = q.where(Number.category == category_hint)
        q = q.order_by(func.random()).limit(1)
        num = await s.scalar(q)
        if num:
            num.status = "ASSIGNED"; num.assigned_to = user_id; num.assigned_at = datetime.now()
            await s.commit()
            return num.phone_number, num.category, "new"
        return None, None, "unavailable"

async def request_numbers(user_id: int, category: str, count: int, message_id: int = None):
    assigned = []
    async with AsyncSessionLocal() as s:
        existing = (await s.execute(
            select(Number).where(
                Number.assigned_to == user_id,
                Number.status.in_(["ASSIGNED", "RETENTION"]),
                Number.category == category)
        )).scalars().all()
        assigned.extend(existing)
        need = count - len(assigned)
        if need > 0:
            prefix = await s.scalar(select(User.prefix).where(User.user_id == user_id))
            q = select(Number).where(Number.category == category, Number.status == "AVAILABLE")
            if prefix:
                q = q.where(Number.phone_number.like(f"{prefix}%"))
            q = q.order_by(func.random()).limit(need)
            for num in (await s.execute(q)).scalars().all():
                num.status = "ASSIGNED"; num.assigned_to = user_id; num.assigned_at = datetime.now()
                if message_id: num.message_id = message_id
                assigned.append(num)
        if assigned:
            await s.commit()
            return [n.phone_number for n in assigned], category, "ok"
        return [], category, "unavailable"

async def get_active_numbers(user_id: int) -> list:
    async with AsyncSessionLocal() as s:
        return (await s.execute(
            select(Number).where(
                Number.assigned_to == user_id,
                Number.status.in_(["ASSIGNED", "RETENTION"]))
        )).scalars().all()

async def release_number(user_id: int):
    async with AsyncSessionLocal() as s:
        nums = (await s.execute(
            select(Number).where(
                Number.assigned_to == user_id,
                Number.status.in_(["ASSIGNED", "RETENTION"]))
        )).scalars().all()
        if not nums: return False, None
        cat = nums[0].category
        for n in nums:
            n.status = "RETENTION"; n.retention_until = datetime.now() + timedelta(hours=1)
            n.assigned_to = None
        await s.commit()
        return True, cat

async def block_number(user_id: int):
    """Block all numbers assigned to user. Returns (True, category) or (False, None)."""
    async with AsyncSessionLocal() as s:
        nums = (await s.execute(
            select(Number).where(
                Number.assigned_to == user_id,
                Number.status.in_(["ASSIGNED", "RETENTION"]))
        )).scalars().all()
        if not nums: return False, None
        cat = nums[0].category
        for n in nums:
            n.status = "BLOCKED"; n.assigned_to = None
        await s.commit()
        return True, cat

async def record_success(phone: str, otp: str):
    async with AsyncSessionLocal() as s:
        num = await s.scalar(select(Number).where(Number.phone_number == phone))
        if not num: return None, None, None
        cat     = num.category
        user_id = num.assigned_to
        msg_id  = num.message_id
        return cat, user_id, msg_id

async def clean_cooldowns():
    async with AsyncSessionLocal() as s:
        now = datetime.now()
        expired = (await s.execute(
            select(Number).where(
                Number.status == "RETENTION",
                Number.retention_until < now)
        )).scalars().all()
        for n in expired:
            n.status = "AVAILABLE"; n.retention_until = None; n.assigned_to = None
        if expired:
            await s.commit()

async def get_number_status(phone: str) -> Optional[str]:
    async with AsyncSessionLocal() as s:
        return await s.scalar(select(Number.status).where(Number.phone_number == phone))

async def get_all_log_chats() -> List[int]:
    async with AsyncSessionLocal() as s:
        rows = await s.execute(select(LogChat.chat_id))
        return [r[0] for r in rows.fetchall()]

async def add_log_chat(chat_id: int, label: str = None):
    async with AsyncSessionLocal() as s:
        if not await s.scalar(select(LogChat).where(LogChat.chat_id == chat_id)):
            s.add(LogChat(chat_id=chat_id, label=label))
            await s.commit()

async def remove_log_chat(chat_id: int):
    async with AsyncSessionLocal() as s:
        await s.execute(delete(LogChat).where(LogChat.chat_id == chat_id))
        await s.commit()

async def delete_used_numbers() -> int:
    async with AsyncSessionLocal() as s:
        r = await s.execute(delete(Number).where(Number.status.in_(["USED", "BLOCKED"])))
        await s.commit()
        return r.rowcount

async def delete_blocked_numbers() -> int:
    async with AsyncSessionLocal() as s:
        r = await s.execute(delete(Number).where(Number.status == "BLOCKED"))
        await s.commit()
        return r.rowcount

async def delete_retention_numbers() -> int:
    async with AsyncSessionLocal() as s:
        r = await s.execute(delete(Number).where(Number.status == "RETENTION"))
        await s.commit()
        return r.rowcount

async def get_db_summary() -> dict:
    async with AsyncSessionLocal() as s:
        total     = await s.scalar(select(func.count(Number.id))) or 0
        available = await s.scalar(select(func.count(Number.id)).where(Number.status=="AVAILABLE")) or 0
        assigned  = await s.scalar(select(func.count(Number.id)).where(Number.status=="ASSIGNED"))  or 0
        retention = await s.scalar(select(func.count(Number.id)).where(Number.status=="RETENTION")) or 0
        blocked   = await s.scalar(select(func.count(Number.id)).where(Number.status=="BLOCKED"))   or 0
        users     = await s.scalar(select(func.count(User.id))) or 0
        history   = await s.scalar(select(func.count(History.id))) or 0
        return {
            "total": total, "available": available, "assigned": assigned,
            "retention": retention, "blocked": blocked, "users": users, "history": history
        }

async def get_otp_history(user_id: int = None, limit: int = 20) -> list:
    async with AsyncSessionLocal() as s:
        q = select(History).order_by(History.created_at.desc()).limit(limit)
        if user_id:
            q = q.where(History.user_id == user_id)
        return (await s.execute(q)).scalars().all()


# ── Extra helpers used by bot.py ─────────────────────────────

def _split_category_parts(category: str) -> tuple[str, str, str]:
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


async def get_distinct_services() -> list:
    """Return list of unique service short-codes from available numbers.
    Category format: "🇵🇰 Pakistan - WhatsApp"  →  service = "WhatsApp"
    """
    async with AsyncSessionLocal() as s:
        rows = await s.execute(
            select(Number.category)
            .where(Number.status == "AVAILABLE")
            .distinct()
        )
        services = set()
        for (cat,) in rows.fetchall():
            _, _, service = _split_category_parts(cat)
            if service:
                services.add(service)
        return sorted(services)


async def get_countries_for_service(service: str) -> list:
    """Return list of (flag, country_name) tuples for a given service.
    Category format: "🇵🇰 Pakistan - WhatsApp"
    """
    async with AsyncSessionLocal() as s:
        rows = await s.execute(
            select(Number.category)
            .where(Number.status == "AVAILABLE",
                   Number.category.like(f"% - {service}"))
            .distinct()
        )
        result = []
        seen = set()
        for (cat,) in rows.fetchall():
            flag, country, _ = _split_category_parts(cat)
            if country not in seen:
                seen.add(country)
                result.append((flag, country))
        return sorted(result, key=lambda x: x[1])


async def get_distinct_countries() -> list:
    """Return unique available countries as (flag, country_name)."""
    async with AsyncSessionLocal() as s:
        rows = await s.execute(
            select(Number.category)
            .where(Number.status == "AVAILABLE")
            .distinct()
        )
        seen = set()
        result = []
        for (cat,) in rows.fetchall():
            flag, country, _ = _split_category_parts(cat)
            if country not in seen:
                seen.add(country)
                result.append((flag, country))
        return sorted(result, key=lambda x: x[1])


async def get_services_for_country(country: str) -> list:
    """Return available service names for a given country."""
    async with AsyncSessionLocal() as s:
        rows = await s.execute(
            select(Number.category)
            .where(Number.status == "AVAILABLE")
            .distinct()
        )
        services = set()
        wanted = (country or "").strip().lower()
        for (cat,) in rows.fetchall():
            _, cat_country, service = _split_category_parts(cat)
            if cat_country.strip().lower() == wanted and service:
                services.add(service)
        return sorted(services)


async def get_recent_history_for_number(phone_number: str, limit: int = 5) -> list:
    """Return recent OTP history rows for a phone number, newest first."""
    async with AsyncSessionLocal() as s:
        q = (
            select(History)
            .where(
                History.phone_number == phone_number,
                History.otp.is_not(None),
                History.otp != "",
            )
            .order_by(History.created_at.desc())
            .limit(limit)
        )
        return (await s.execute(q)).scalars().all()


async def update_message_id(phone_number: str, message_id: int):
    """Update the Telegram message_id stored on a number row."""
    async with AsyncSessionLocal() as s:
        num = await s.scalar(
            select(Number).where(Number.phone_number == phone_number))
        if num:
            num.message_id = message_id
            await s.commit()


async def delete_all_numbers() -> int:
    """Delete ALL numbers (used + available + assigned)."""
    async with AsyncSessionLocal() as s:
        r = await s.execute(delete(Number))
        await s.commit()
        return r.rowcount

# ── Tutorials ────────────────────────────────────────────────
async def add_tutorial(title: str, description: str, content_type: str, 
                      text_content: str = None, photo_file_id: str = None,
                      video_file_id: str = None, 
                      created_by: int = None) -> int:
    """Add a new tutorial to database."""
    async with AsyncSessionLocal() as s:
        tutorial = Tutorial(
            title=title,
            description=description,
            content_type=content_type,
            text_content=text_content,
            photo_file_id=photo_file_id,
            video_file_id=video_file_id,
            created_by=created_by or 0
        )
        s.add(tutorial)
        await s.commit()
        return tutorial.id

async def get_all_tutorials() -> list:
    """Get all tutorials."""
    async with AsyncSessionLocal() as s:
        return (await s.execute(
            select(Tutorial).order_by(Tutorial.created_at.desc())
        )).scalars().all()

async def get_tutorial(tutorial_id: int):
    """Get specific tutorial by ID."""
    async with AsyncSessionLocal() as s:
        return await s.scalar(select(Tutorial).where(Tutorial.id == tutorial_id))

async def delete_tutorial(tutorial_id: int) -> bool:
    """Delete a tutorial."""
    async with AsyncSessionLocal() as s:
        result = await s.execute(
            delete(Tutorial).where(Tutorial.id == tutorial_id))
        await s.commit()
        return result.rowcount > 0

async def update_tutorial(tutorial_id: int, **kwargs) -> bool:
    """Update tutorial fields."""
    async with AsyncSessionLocal() as s:
        t = await s.scalar(select(Tutorial).where(Tutorial.id == tutorial_id))
        if t:
            for key, value in kwargs.items():
                if hasattr(t, key):
                    setattr(t, key, value)
            await s.commit()
            return True
        return False

# ── API Tokens ───────────────────────────────────────────────
async def create_api_token(token: str, name: str, created_by: int, api_dev: str = None, panels_data: str = None) -> int:
    """Create a new API token."""
    async with AsyncSessionLocal() as s:
        api_token = APIToken(
            token=token,
            name=name,
            created_by=created_by,
            api_dev=api_dev,
            panels_data=panels_data,
            status="ACTIVE"
        )
        s.add(api_token)
        await s.commit()
        return api_token.id

async def get_api_token(token: str):
    """Get API token by token string."""
    async with AsyncSessionLocal() as s:
        return await s.scalar(select(APIToken).where(APIToken.token == token))

async def get_all_api_tokens(created_by: int = None) -> list:
    """Get all API tokens (optionally filtered by creator)."""
    async with AsyncSessionLocal() as s:
        query = select(APIToken)
        if created_by:
            query = query.where(APIToken.created_by == created_by)
        return (await s.execute(query.order_by(APIToken.created_at.desc()))).scalars().all()

async def update_api_token_status(token: str, status: str) -> bool:
    """Update API token status (ACTIVE, BLOCKED, DELETED)."""
    async with AsyncSessionLocal() as s:
        api_token = await s.scalar(select(APIToken).where(APIToken.token == token))
        if api_token:
            api_token.status = status
            await s.commit()
            return True
        return False

async def update_api_token_last_used(token: str) -> bool:
    """Update last_used timestamp for API token."""
    async with AsyncSessionLocal() as s:
        api_token = await s.scalar(select(APIToken).where(APIToken.token == token))
        if api_token:
            api_token.last_used = datetime.now()
            await s.commit()
            return True
        return False

async def delete_api_token(token: str) -> bool:
    """Delete an API token."""
    async with AsyncSessionLocal() as s:
        result = await s.execute(delete(APIToken).where(APIToken.token == token))
        await s.commit()
        return result.rowcount > 0
