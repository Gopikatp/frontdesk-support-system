import os
import asyncio
import csv
import hashlib
import hmac
import io
import logging
import secrets
from datetime import datetime, timedelta, time, date, timezone
from pathlib import Path
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, String, DateTime, Boolean, ForeignKey, Text, select, update, delete, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker, Session

def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/frontdesk")
DATABASE_URL = normalize_database_url(DATABASE_URL)
engine_options = {"pool_pre_ping": True}
if DATABASE_URL.startswith("sqlite"):
    engine_options["connect_args"] = {"check_same_thread": False}
engine = create_engine(DATABASE_URL, **engine_options)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"
logger = logging.getLogger(__name__)

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

class Base(DeclarativeBase):
    pass

class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(180))
    customer_name: Mapped[str] = mapped_column(String(120))
    customer_email: Mapped[str] = mapped_column(String(180))
    description: Mapped[str] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(20), default="SMALL")
    status: Mapped[str] = mapped_column(String(30), default="OPEN")
    assigned_agent: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    first_response_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    first_response_due_at: Mapped[datetime] = mapped_column(DateTime)
    resolution_due_at: Mapped[datetime] = mapped_column(DateTime)
    waiting_since: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    messages: Mapped[list["Message"]] = relationship(back_populates="ticket")
    audit_events: Mapped[list["AuditEvent"]] = relationship(back_populates="ticket")

class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(20), default="AGENT")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

class LoginSession(Base):
    __tablename__ = "login_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)

class LoginThrottle(Base):
    __tablename__ = "login_throttles"
    email_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    failed_attempts: Mapped[int] = mapped_column(default=0)
    window_started_at: Mapped[datetime] = mapped_column(DateTime)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    author: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text)
    is_private: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ticket: Mapped[Ticket] = relationship(back_populates="messages")

class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    actor: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(100))
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    ticket: Mapped[Ticket] = relationship(back_populates="audit_events")

# Working calendar: Monday-Friday, 09:30-18:30. Add company festival dates here.
HOLIDAYS: set[date] = set()
OPEN = time(9, 30)
CLOSE = time(18, 30)
PRIORITY_TARGETS = {
    "URGENT": (2, 9),       # response hours, resolution working hours (same working day)
    "MEDIUM": (4.5, 27),    # 3 working days
    "SMALL": (9, 45),       # 1 working day response, 1 working week resolution
}
APP_TIMEZONE = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Kolkata"))

def as_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(APP_TIMEZONE)

def as_utc_naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None)

def is_workday(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS

def next_open(dt: datetime) -> datetime:
    current = as_local(dt)
    while True:
        start = datetime.combine(current.date(), OPEN, APP_TIMEZONE)
        end = datetime.combine(current.date(), CLOSE, APP_TIMEZONE)
        if is_workday(current.date()):
            if current < start:
                return as_utc_naive(start)
            if current < end:
                return as_utc_naive(current)
        current = datetime.combine(current.date() + timedelta(days=1), OPEN, APP_TIMEZONE)

def add_working_hours(start: datetime, hours: float) -> datetime:
    if hours <= 0:
        return start
    current = as_local(next_open(start))
    remaining = hours * 3600
    while remaining > 0:
        end = datetime.combine(current.date(), CLOSE, APP_TIMEZONE)
        available = max(0, (end.astimezone(timezone.utc) - current.astimezone(timezone.utc)).total_seconds())
        if remaining <= available:
            return as_utc_naive(current.astimezone(timezone.utc) + timedelta(seconds=remaining))
        remaining -= available
        current = as_local(next_open(as_utc_naive(
            datetime.combine(current.date() + timedelta(days=1), OPEN, APP_TIMEZONE)
        )))
    return as_utc_naive(current.astimezone(timezone.utc))

def resolution_deadline(priority: str, now: datetime) -> datetime:
    # "Same working day" means the end of the current working day.
    if priority == "URGENT":
        active = as_local(next_open(now))
        return as_utc_naive(datetime.combine(active.date(), CLOSE, APP_TIMEZONE))
    return add_working_hours(now, PRIORITY_TARGETS[priority][1])

def working_seconds_between(start: datetime, end: datetime) -> float:
    if end <= start:
        return 0
    total = 0.0
    start_local = as_local(start)
    end_local = as_local(end)
    cursor = start_local
    while cursor.date() <= end_local.date():
        if is_workday(cursor.date()):
            day_start = datetime.combine(cursor.date(), OPEN, APP_TIMEZONE)
            day_end = datetime.combine(cursor.date(), CLOSE, APP_TIMEZONE)
            left = max(cursor.astimezone(timezone.utc), day_start.astimezone(timezone.utc))
            right = min(end_local.astimezone(timezone.utc), day_end.astimezone(timezone.utc))
            if right > left:
                total += (right - left).total_seconds()
        cursor = datetime.combine(cursor.date() + timedelta(days=1), time.min, APP_TIMEZONE)
    return total

def resume_sla_after_wait(ticket: Ticket, now: datetime) -> None:
    if ticket.waiting_since:
        paused = working_seconds_between(ticket.waiting_since, now) / 3600
        ticket.first_response_due_at = add_working_hours(ticket.first_response_due_at, paused)
        ticket.resolution_due_at = add_working_hours(ticket.resolution_due_at, paused)
    ticket.waiting_since = None

def audit(db: Session, ticket_id: int, actor: str, action: str, details: str = ""):
    db.add(AuditEvent(ticket_id=ticket_id, actor=actor, action=action, details=details))

PASSWORD_ITERATIONS = 600_000
SESSION_HOURS = 8
LOGIN_FAILURE_LIMIT = 8
LOGIN_WINDOW = timedelta(minutes=15)
bearer_scheme = HTTPBearer(auto_error=False)

def normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    local, separator, domain = normalized.partition("@")
    if not separator or not local or "." not in domain or any(char.isspace() for char in normalized):
        raise HTTPException(422, "Enter a valid email address")
    return normalized

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt.hex()}${digest.hex()}"

def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False

DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))

def account_dict(account: Account):
    return {
        "id": account.id,
        "email": account.email,
        "display_name": account.display_name,
        "role": account.role,
        "is_active": account.is_active,
        "created_at": account.created_at.isoformat(),
    }

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_current_account(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> Account:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(401, "Sign in required", headers={"WWW-Authenticate": "Bearer"})
    token_hash = hashlib.sha256(credentials.credentials.encode("utf-8")).hexdigest()
    login_session = db.scalar(
        select(LoginSession).where(
            LoginSession.token_hash == token_hash,
            LoginSession.expires_at > utcnow(),
        )
    )
    account = db.get(Account, login_session.account_id) if login_session else None
    if not account or not account.is_active:
        raise HTTPException(401, "Session expired or account disabled", headers={"WWW-Authenticate": "Bearer"})
    return account

def require_admin(account: Account = Depends(get_current_account)) -> Account:
    if account.role != "ADMIN":
        raise HTTPException(403, "Administrator access required")
    return account

class TicketCreate(BaseModel):
    title: str = Field(min_length=3, max_length=180)
    customer_name: str = Field(min_length=1, max_length=120, strip_whitespace=True)
    customer_email: str = Field(
        min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    )
    description: str = Field(min_length=1, max_length=10000, strip_whitespace=True)
    priority: str = "SMALL"
class ClaimRequest(BaseModel):
    pass

class MessageCreate(BaseModel):
    body: str = Field(min_length=1, max_length=10000, strip_whitespace=True)
    is_private: bool = False
    waiting_for_customer: bool = False

class StatusUpdate(BaseModel):
    status: str = Field(min_length=1, max_length=30)

class PriorityUpdate(BaseModel):
    priority: str = Field(min_length=1, max_length=20)

class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=1, max_length=128)
    role: Literal["ADMIN", "AGENT"] = "AGENT"

class AccountCreate(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    display_name: str = Field(min_length=1, max_length=120, strip_whitespace=True)
    password: str = Field(min_length=12, max_length=128)
    role: str = "AGENT"

class AccountUpdate(BaseModel):
    is_active: bool

class PasswordUpdate(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)

class PasswordReset(BaseModel):
    password: str = Field(min_length=12, max_length=128)

app = FastAPI(title="Frontdesk Support System", version="1.0.0")
frontend_origins = [origin.strip() for origin in os.getenv("FRONTEND_ORIGIN", "").split(",") if origin.strip()]
if frontend_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=frontend_origins,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Authorization", "Content-Type"],
    )
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response

@app.get("/")
def home():
    return FileResponse(FRONTEND_DIR / "index.html")

@app.get("/admin/login")
def admin_login_page():
    return FileResponse(FRONTEND_DIR / "index.html")

@app.get("/staff/login")
def staff_login_page():
    return FileResponse(FRONTEND_DIR / "index.html")

@app.post("/api/auth/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    email = normalize_email(payload.email)
    email_hash = hashlib.sha256(email.encode("utf-8")).hexdigest()
    now = utcnow()
    throttle = db.get(LoginThrottle, email_hash)
    if throttle and throttle.locked_until and throttle.locked_until > now:
        retry_seconds = max(1, int((throttle.locked_until - now).total_seconds()))
        raise HTTPException(
            429,
            "Too many sign-in attempts. Try again later.",
            headers={"Retry-After": str(retry_seconds)},
        )
    if throttle and now - throttle.window_started_at >= LOGIN_WINDOW:
        throttle.failed_attempts = 0
        throttle.window_started_at = now
        throttle.locked_until = None
    account = db.scalar(select(Account).where(Account.email == email))
    password_matches = verify_password(
        payload.password, account.password_hash if account else DUMMY_PASSWORD_HASH
    )
    if (
        not account
        or not account.is_active
        or not password_matches
        or account.role != payload.role
    ):
        if not throttle:
            throttle = LoginThrottle(email_hash=email_hash, failed_attempts=0, window_started_at=now)
            db.add(throttle)
        throttle.failed_attempts += 1
        if throttle.failed_attempts >= LOGIN_FAILURE_LIMIT:
            throttle.locked_until = now + LOGIN_WINDOW
        db.commit()
        raise HTTPException(401, "Invalid email or password")
    token = secrets.token_urlsafe(32)
    expires_at = utcnow() + timedelta(hours=SESSION_HOURS)
    db.add(LoginSession(
        account_id=account.id,
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        expires_at=expires_at,
    ))
    if throttle:
        db.delete(throttle)
    db.execute(delete(LoginSession).where(LoginSession.expires_at <= utcnow()))
    db.commit()
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": expires_at.isoformat(),
        "user": account_dict(account),
    }

@app.get("/api/auth/me")
def current_account(account: Account = Depends(get_current_account)):
    return account_dict(account)

@app.post("/api/auth/logout")
def logout(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
    _account: Account = Depends(get_current_account),
):
    if credentials:
        token_hash = hashlib.sha256(credentials.credentials.encode("utf-8")).hexdigest()
        db.execute(delete(LoginSession).where(LoginSession.token_hash == token_hash))
        db.commit()
    return {"message": "Signed out"}

@app.post("/api/auth/password")
def change_password(
    payload: PasswordUpdate,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_account),
):
    if not verify_password(payload.current_password, account.password_hash):
        raise HTTPException(400, "Current password is incorrect")
    account.password_hash = hash_password(payload.new_password)
    db.execute(delete(LoginSession).where(LoginSession.account_id == account.id))
    db.execute(delete(LoginThrottle).where(
        LoginThrottle.email_hash == hashlib.sha256(account.email.encode("utf-8")).hexdigest()
    ))
    db.commit()
    return {"message": "Password changed. Sign in again."}

@app.get("/api/users")
def list_accounts(db: Session = Depends(get_db), _admin: Account = Depends(require_admin)):
    return [account_dict(account) for account in db.scalars(select(Account).order_by(Account.email)).all()]

@app.post("/api/users", status_code=201)
def create_account(
    payload: AccountCreate,
    db: Session = Depends(get_db),
    admin: Account = Depends(require_admin),
):
    email = normalize_email(payload.email)
    if payload.role not in {"AGENT", "ADMIN"}:
        raise HTTPException(400, "Role must be AGENT or ADMIN")
    display_name = payload.display_name.strip()
    if not display_name:
        raise HTTPException(422, "Name cannot be blank")
    if db.scalar(select(Account).where(Account.email == email)):
        raise HTTPException(409, "An account with this email already exists")
    if db.scalar(select(Account).where(Account.display_name == display_name)):
        raise HTTPException(409, "An account with this display name already exists")
    account = Account(
        email=email,
        display_name=display_name,
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    db.add(account)
    db.flush()
    db.commit()
    return account_dict(account)

@app.patch("/api/users/{account_id}")
def update_account(
    account_id: int,
    payload: AccountUpdate,
    db: Session = Depends(get_db),
    admin: Account = Depends(require_admin),
):
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")
    if account.id == admin.id and not payload.is_active:
        raise HTTPException(409, "You cannot disable your own account")
    if account.role == "ADMIN" and account.is_active and not payload.is_active:
        active_admins = db.scalar(
            select(func.count()).select_from(Account).where(
                Account.role == "ADMIN", Account.is_active.is_(True)
            )
        )
        if active_admins <= 1:
            raise HTTPException(409, "At least one active administrator is required")
    if account.is_active and not payload.is_active:
        owned_tickets = db.scalars(select(Ticket).where(
            Ticket.assigned_agent == account.display_name,
            Ticket.status.not_in(("RESOLVED", "CLOSED")),
        )).all()
        for ticket in owned_tickets:
            ticket.assigned_agent = admin.display_name
            ticket.updated_at = utcnow()
            audit(db, ticket.id, admin.display_name, "OWNER_REASSIGNED", f"Owner removed: {account.display_name}")
    account.is_active = payload.is_active
    if not account.is_active:
        db.execute(delete(LoginSession).where(LoginSession.account_id == account.id))
        db.execute(delete(LoginThrottle).where(
            LoginThrottle.email_hash == hashlib.sha256(account.email.encode("utf-8")).hexdigest()
        ))
    db.commit()
    return account_dict(account)

@app.post("/api/users/{account_id}/password")
def reset_account_password(
    account_id: int,
    payload: PasswordReset,
    db: Session = Depends(get_db),
    _admin: Account = Depends(require_admin),
):
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(404, "Account not found")
    account.password_hash = hash_password(payload.password)
    db.execute(delete(LoginSession).where(LoginSession.account_id == account.id))
    db.execute(delete(LoginThrottle).where(
        LoginThrottle.email_hash == hashlib.sha256(account.email.encode("utf-8")).hexdigest()
    ))
    db.commit()
    return {"message": "Password reset; existing sessions have been revoked"}

def ticket_dict(t: Ticket):
    now = utcnow()
    first_response_due_at = t.first_response_due_at
    resolution_due_at = t.resolution_due_at
    if t.status == "WAITING_CUSTOMER" and t.waiting_since:
        paused_hours = working_seconds_between(t.waiting_since, now) / 3600
        first_response_due_at = add_working_hours(first_response_due_at, paused_hours)
        resolution_due_at = add_working_hours(resolution_due_at, paused_hours)
    return {
        "id": t.id, "title": t.title, "customer_name": t.customer_name,
        "customer_email": t.customer_email, "description": t.description,
        "priority": t.priority, "status": t.status, "assigned_agent": t.assigned_agent,
        "created_at": t.created_at.isoformat(), "updated_at": t.updated_at.isoformat(),
        "first_response_due_at": first_response_due_at.isoformat(),
        "resolution_due_at": resolution_due_at.isoformat(),
        "first_response_at": t.first_response_at.isoformat() if t.first_response_at else None,
        "is_waiting": t.status == "WAITING_CUSTOMER",
        "response_late": t.status != "WAITING_CUSTOMER" and t.first_response_at is None and now > first_response_due_at,
        "resolution_late": t.status not in ("RESOLVED", "CLOSED", "WAITING_CUSTOMER") and now > resolution_due_at,
    }

def ticket_statement(
    status: Optional[str],
    priority: Optional[str],
    q: Optional[str],
    assigned_agent: Optional[str],
    unassigned: bool = False,
):
    stmt = select(Ticket).order_by(Ticket.created_at.desc())
    if status:
        normalized_status = status.upper()
        if normalized_status not in {"OPEN", "IN_PROGRESS", "WAITING_CUSTOMER", "RESOLVED", "CLOSED"}:
            raise HTTPException(400, "Invalid status filter")
        stmt = stmt.where(Ticket.status == normalized_status)
    if priority:
        normalized_priority = priority.upper()
        if normalized_priority not in PRIORITY_TARGETS:
            raise HTTPException(400, "Invalid priority filter")
        stmt = stmt.where(Ticket.priority == normalized_priority)
    if assigned_agent:
        stmt = stmt.where(Ticket.assigned_agent == assigned_agent)
    if unassigned:
        stmt = stmt.where(Ticket.assigned_agent.is_(None))
    if q and q.strip():
        term = f"%{q.strip()}%"
        stmt = stmt.where(
            Ticket.title.ilike(term)
            | Ticket.customer_name.ilike(term)
            | Ticket.customer_email.ilike(term)
        )
    return stmt

def csv_safe(value):
    text = "" if value is None else str(value)
    if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r")):
        return f"'{text}"
    return text

def ticket_csv(db: Session, stmt) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("ID", "Title", "Customer", "Email", "Priority", "Status", "Assignee", "Created"))
    for ticket in db.scalars(stmt).all():
        writer.writerow((
            ticket.id, csv_safe(ticket.title), csv_safe(ticket.customer_name), csv_safe(ticket.customer_email),
            ticket.priority, ticket.status, csv_safe(ticket.assigned_agent), ticket.created_at.isoformat(),
        ))
    return output.getvalue()

@app.post("/api/tickets")
def create_ticket(
    payload: TicketCreate,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_account),
):
    priority = payload.priority.upper()
    if priority not in PRIORITY_TARGETS:
        raise HTTPException(400, "Priority must be URGENT, MEDIUM or SMALL")
    now = utcnow()
    response_hours, _ = PRIORITY_TARGETS[priority]
    t = Ticket(
        title=payload.title, customer_name=payload.customer_name,
        customer_email=payload.customer_email, description=payload.description,
        priority=priority, status="OPEN", created_at=now, updated_at=now,
        first_response_due_at=add_working_hours(now, response_hours),
        resolution_due_at=resolution_deadline(priority, now),
    )
    db.add(t)
    db.flush()
    audit(db, t.id, account.display_name, "TICKET_CREATED", f"Priority: {priority}")
    db.commit()
    return ticket_dict(t)

@app.get("/api/tickets")
def list_tickets(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    q: Optional[str] = Query(default=None, max_length=180),
    assigned_agent: Optional[str] = Query(default=None, max_length=120),
    unassigned: bool = False,
    db: Session = Depends(get_db),
    _account: Account = Depends(get_current_account),
):
    stmt = ticket_statement(status, priority, q, assigned_agent, unassigned)
    return [ticket_dict(t) for t in db.scalars(stmt).all()]

@app.get("/api/tickets/export.csv")
def export_tickets(
    status: Optional[str] = None,
    priority: Optional[str] = None,
    q: Optional[str] = Query(default=None, max_length=180),
    assigned_agent: Optional[str] = Query(default=None, max_length=120),
    unassigned: bool = False,
    db: Session = Depends(get_db),
    _account: Account = Depends(get_current_account),
):
    stmt = ticket_statement(status, priority, q, assigned_agent, unassigned)
    return StreamingResponse(
        iter([ticket_csv(db, stmt)]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="frontdesk-tickets.csv"'},
    )

@app.get("/api/tickets/{ticket_id}")
def get_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    _account: Account = Depends(get_current_account),
):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404, "Ticket not found")
    return ticket_dict(t)

@app.post("/api/tickets/{ticket_id}/claim")
def claim_ticket(
    ticket_id: int,
    payload: ClaimRequest,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_account),
):
    # Conditional UPDATE makes claiming atomic: only one agent can win.
    result = db.execute(
        update(Ticket).where(
            Ticket.id == ticket_id,
            Ticket.assigned_agent.is_(None),
            Ticket.status == "OPEN",
        )
        .values(assigned_agent=account.display_name, status="IN_PROGRESS", updated_at=utcnow())
    )
    if result.rowcount != 1:
        db.rollback()
        if not db.get(Ticket, ticket_id):
            raise HTTPException(404, "Ticket not found")
        raise HTTPException(409, "Ticket is already assigned or does not exist")
    audit(db, ticket_id, account.display_name, "TICKET_CLAIMED", "Agent claimed ticket")
    db.commit()
    return {"message": "Ticket assigned", "assigned_agent": account.display_name}

@app.post("/api/tickets/{ticket_id}/messages")
def add_message(
    ticket_id: int,
    payload: MessageCreate,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_account),
):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404, "Ticket not found")
    if t.status in ("CLOSED", "RESOLVED"):
        raise HTTPException(409, "Reopen the ticket before adding a message")
    if t.assigned_agent != account.display_name:
        raise HTTPException(409, "Claim this ticket before adding messages")
    if payload.waiting_for_customer and payload.is_private:
        raise HTTPException(400, "A private note cannot put a ticket into customer-waiting status")
    m = Message(ticket_id=ticket_id, author=account.display_name, body=payload.body, is_private=payload.is_private)
    db.add(m)
    now = utcnow()
    t.updated_at = now
    if not payload.is_private and t.first_response_at is None:
        t.first_response_at = now
    audit(db, ticket_id, account.display_name, "PRIVATE_NOTE_ADDED" if payload.is_private else "PUBLIC_REPLY_SENT",
          "Internal note" if payload.is_private else "Customer-visible reply")
    if payload.waiting_for_customer and t.status != "WAITING_CUSTOMER":
        old_status = t.status
        t.status = "WAITING_CUSTOMER"
        t.waiting_since = now
        audit(db, ticket_id, account.display_name, "STATUS_CHANGED", f"{old_status} -> WAITING_CUSTOMER")
    elif payload.waiting_for_customer:
        t.waiting_since = now
        audit(db, ticket_id, account.display_name, "WAITING_TIMER_RESET", "Agent requested another customer response")
    elif not payload.is_private and t.status == "WAITING_CUSTOMER":
        resume_sla_after_wait(t, now)
        t.status = "IN_PROGRESS"
        audit(db, ticket_id, account.display_name, "STATUS_CHANGED", "WAITING_CUSTOMER -> IN_PROGRESS")
    db.commit()
    return {"id": m.id, "message": "Saved", "is_private": m.is_private}

@app.get("/api/tickets/{ticket_id}/messages")
def list_messages(
    ticket_id: int,
    include_private: bool = False,
    db: Session = Depends(get_db),
    _account: Account = Depends(get_current_account),
):
    if not db.get(Ticket, ticket_id):
        raise HTTPException(404, "Ticket not found")
    stmt = select(Message).where(Message.ticket_id == ticket_id).order_by(Message.created_at)
    if not include_private:
        stmt = stmt.where(Message.is_private.is_(False))
    return [{"id": m.id, "author": m.author, "body": m.body, "is_private": m.is_private,
             "created_at": m.created_at.isoformat()} for m in db.scalars(stmt).all()]

@app.post("/api/tickets/{ticket_id}/status")
def update_status(
    ticket_id: int,
    payload: StatusUpdate,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_account),
):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404, "Ticket not found")
    new_status = payload.status.upper()
    transitions = {
        "OPEN": {"IN_PROGRESS", "WAITING_CUSTOMER", "RESOLVED", "CLOSED"},
        "IN_PROGRESS": {"OPEN", "WAITING_CUSTOMER", "RESOLVED", "CLOSED"},
        "WAITING_CUSTOMER": {"IN_PROGRESS", "RESOLVED", "CLOSED"},
        "RESOLVED": {"IN_PROGRESS", "CLOSED"},
        "CLOSED": {"IN_PROGRESS"},
    }
    if new_status not in transitions:
        raise HTTPException(400, "Invalid status")
    if t.assigned_agent != account.display_name:
        raise HTTPException(409, "Claim this ticket before changing its status")
    now = utcnow()
    old = t.status
    if old not in transitions:
        raise HTTPException(409, f"Ticket has unsupported current status: {old}")
    if old == new_status:
        return ticket_dict(t)
    if new_status not in transitions[old]:
        raise HTTPException(409, f"Cannot change status from {old} to {new_status}")
    if old in ("RESOLVED", "CLOSED") and new_status == "IN_PROGRESS":
        if not t.resolved_at or (now - t.resolved_at) > timedelta(days=7):
            raise HTTPException(409, "Reopen window expired; create a new ticket")
    if new_status == "WAITING_CUSTOMER" and old != "WAITING_CUSTOMER":
        t.waiting_since = now
    elif old == "WAITING_CUSTOMER" and new_status != "WAITING_CUSTOMER" and t.waiting_since:
        resume_sla_after_wait(t, now)
    if new_status == "RESOLVED":
        t.resolved_at = now
    if new_status == "CLOSED":
        t.resolved_at = t.resolved_at or now
    if old in ("RESOLVED", "CLOSED") and new_status == "IN_PROGRESS":
        t.resolved_at = None
    t.status = new_status
    t.updated_at = now
    audit(db, ticket_id, account.display_name, "STATUS_CHANGED", f"{old} -> {new_status}")
    db.commit()
    return ticket_dict(t)

@app.post("/api/tickets/{ticket_id}/priority")
def update_priority(
    ticket_id: int,
    payload: PriorityUpdate,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_account),
):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404, "Ticket not found")
    priority = payload.priority.upper()
    if priority not in PRIORITY_TARGETS:
        raise HTTPException(400, "Invalid priority")
    if t.assigned_agent != account.display_name:
        raise HTTPException(409, "Claim this ticket before changing its priority")
    if t.status in ("RESOLVED", "CLOSED"):
        raise HTTPException(409, "Reopen the ticket before changing its priority")
    old = t.priority
    if old == priority:
        return ticket_dict(t)
    # Documented assumption: new SLA target is calculated from change time.
    response_hours, _ = PRIORITY_TARGETS[priority]
    now = utcnow()
    if t.first_response_at is None:
        t.first_response_due_at = add_working_hours(now, response_hours)
    t.resolution_due_at = resolution_deadline(priority, now)
    if t.status == "WAITING_CUSTOMER":
        t.waiting_since = now
    t.priority = priority
    t.updated_at = now
    audit(db, t.id, account.display_name, "PRIORITY_CHANGED", f"{old} -> {priority}; targets recalculated from change time")
    db.commit()
    return ticket_dict(t)

@app.get("/api/tickets/{ticket_id}/audit")
def list_audit(
    ticket_id: int,
    db: Session = Depends(get_db),
    _account: Account = Depends(get_current_account),
):
    if not db.get(Ticket, ticket_id):
        raise HTTPException(404, "Ticket not found")
    rows = db.scalars(select(AuditEvent).where(AuditEvent.ticket_id == ticket_id).order_by(AuditEvent.created_at)).all()
    return [{"id": x.id, "actor": x.actor, "action": x.action, "details": x.details,
             "created_at": x.created_at.isoformat()} for x in rows]

@app.get("/api/health")
def health(db: Session = Depends(get_db)):
    db.execute(select(1))
    return {"status": "ok", "service": "Frontdesk"}

def migrate_legacy_ticket_owners(db: Session, admin: Account) -> None:
    active_names = set(db.scalars(
        select(Account.display_name).where(Account.is_active.is_(True))
    ).all())
    legacy_tickets = db.scalars(select(Ticket).where(
        Ticket.assigned_agent.is_not(None),
        Ticket.status.not_in(("RESOLVED", "CLOSED")),
    )).all()
    for ticket in legacy_tickets:
        if ticket.assigned_agent not in active_names:
            previous_owner = ticket.assigned_agent
            ticket.assigned_agent = admin.display_name
            ticket.updated_at = utcnow()
            audit(db, ticket.id, admin.display_name, "OWNER_MIGRATED", f"Previous owner: {previous_owner}")
    db.commit()

def ensure_initial_admin():
    with SessionLocal() as db:
        admin = db.scalar(select(Account).where(
            Account.role == "ADMIN", Account.is_active.is_(True)
        ).order_by(Account.id).limit(1))
        if admin:
            migrate_legacy_ticket_owners(db, admin)
            return
        if db.scalar(select(Account.id).where(Account.role == "ADMIN").limit(1)):
            raise RuntimeError("No active administrator exists; restore an administrator account before startup")
        email = os.getenv("ADMIN_EMAIL", "").strip()
        password = os.getenv("ADMIN_PASSWORD", "")
        if not email or len(password) < 12:
            raise RuntimeError(
                "Set ADMIN_EMAIL and an ADMIN_PASSWORD of at least 12 characters before first startup"
            )
        account = Account(
            email=normalize_email(email),
            display_name=os.getenv("ADMIN_NAME", "Frontdesk Admin").strip() or "Frontdesk Admin",
            password_hash=hash_password(password),
            role="ADMIN",
        )
        db.add(account)
        db.commit()
        logger.info("Created initial administrator account %s", account.email)
        migrate_legacy_ticket_owners(db, account)

async def auto_close_waiting_tickets():
    # Check every 15 minutes. Close after 5 working days of customer inactivity.
    while True:
        try:
            with SessionLocal() as db:
                now = utcnow()
                waiting = db.scalars(select(Ticket).where(Ticket.status == "WAITING_CUSTOMER")).all()
                for t in waiting:
                    if t.waiting_since and working_seconds_between(t.waiting_since, now) >= 5 * 9 * 3600:
                        paused = working_seconds_between(t.waiting_since, now) / 3600
                        t.first_response_due_at = add_working_hours(t.first_response_due_at, paused)
                        t.resolution_due_at = add_working_hours(t.resolution_due_at, paused)
                        t.status = "CLOSED"
                        t.resolved_at = now
                        t.waiting_since = None
                        t.updated_at = now
                        audit(db, t.id, "SYSTEM", "AUTO_CLOSED", "No customer response for five working days")
                db.commit()
        except Exception:
            logger.exception("Auto-close task failed")
        await asyncio.sleep(900)

@app.on_event("startup")
async def startup_task():
    Base.metadata.create_all(bind=engine)
    ensure_initial_admin()
    app.state.auto_close_task = asyncio.create_task(auto_close_waiting_tickets())

@app.on_event("shutdown")
async def stop_background_task():
    task = getattr(app.state, "auto_close_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
