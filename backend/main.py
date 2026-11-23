import os
import asyncio
import csv
import io
import logging
from datetime import datetime, timedelta, time, date, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import (
    create_engine, String, DateTime, Boolean, ForeignKey, Text, select, update
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker, Session

def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/frontdesk")
engine = create_engine(normalize_database_url(DATABASE_URL), pool_pre_ping=True)
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

def audit(db: Session, ticket_id: int, actor: str, action: str, details: str = ""):
    db.add(AuditEvent(ticket_id=ticket_id, actor=actor, action=action, details=details))

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

class TicketCreate(BaseModel):
    title: str = Field(min_length=3, max_length=180)
    customer_name: str = Field(min_length=1, max_length=120)
    customer_email: str = Field(min_length=3, max_length=180)
    description: str = Field(min_length=1, max_length=10000)
    priority: str = "SMALL"
    actor: str = Field(default="Support Agent", min_length=1, max_length=120)

class ClaimRequest(BaseModel):
    agent: str = Field(min_length=1, max_length=120)

class MessageCreate(BaseModel):
    author: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=10000)
    is_private: bool = False
    waiting_for_customer: bool = False

class StatusUpdate(BaseModel):
    status: str = Field(min_length=1, max_length=30)
    actor: str = Field(min_length=1, max_length=120)

class PriorityUpdate(BaseModel):
    priority: str = Field(min_length=1, max_length=20)
    actor: str = Field(min_length=1, max_length=120)

app = FastAPI(title="Frontdesk Support System", version="1.0.0")
frontend_origins = [origin.strip() for origin in os.getenv("FRONTEND_ORIGIN", "").split(",") if origin.strip()]
if frontend_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=frontend_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

@app.get("/")
def home():
    return FileResponse(FRONTEND_DIR / "index.html")

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

@app.post("/api/tickets")
def create_ticket(payload: TicketCreate, db: Session = Depends(get_db)):
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
    audit(db, t.id, payload.actor, "TICKET_CREATED", f"Priority: {priority}")
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
):
    stmt = ticket_statement(status, priority, q, assigned_agent, unassigned)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("ID", "Title", "Customer", "Email", "Priority", "Status", "Assignee", "Created"))
    for ticket in db.scalars(stmt).all():
        writer.writerow((
            ticket.id, csv_safe(ticket.title), csv_safe(ticket.customer_name), csv_safe(ticket.customer_email),
            ticket.priority, ticket.status, csv_safe(ticket.assigned_agent), ticket.created_at.isoformat(),
        ))
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="frontdesk-tickets.csv"'},
    )

@app.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: int, db: Session = Depends(get_db)):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404, "Ticket not found")
    return ticket_dict(t)

@app.post("/api/tickets/{ticket_id}/claim")
def claim_ticket(ticket_id: int, payload: ClaimRequest, db: Session = Depends(get_db)):
    # Conditional UPDATE makes claiming atomic: only one agent can win.
    result = db.execute(
        update(Ticket).where(
            Ticket.id == ticket_id,
            Ticket.assigned_agent.is_(None),
            Ticket.status == "OPEN",
        )
        .values(assigned_agent=payload.agent, status="IN_PROGRESS", updated_at=utcnow())
    )
    if result.rowcount != 1:
        db.rollback()
        if not db.get(Ticket, ticket_id):
            raise HTTPException(404, "Ticket not found")
        raise HTTPException(409, "Ticket is already assigned or does not exist")
    audit(db, ticket_id, payload.agent, "TICKET_CLAIMED", "Agent claimed ticket")
    db.commit()
    return {"message": "Ticket assigned", "assigned_agent": payload.agent}

@app.post("/api/tickets/{ticket_id}/messages")
def add_message(ticket_id: int, payload: MessageCreate, db: Session = Depends(get_db)):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404, "Ticket not found")
    if t.status in ("CLOSED", "RESOLVED"):
        raise HTTPException(409, "Reopen the ticket before adding a message")
    if t.assigned_agent and t.assigned_agent != payload.author:
        raise HTTPException(409, "Only the assigned agent can add messages")
    if payload.waiting_for_customer and payload.is_private:
        raise HTTPException(400, "A private note cannot put a ticket into customer-waiting status")
    m = Message(ticket_id=ticket_id, author=payload.author, body=payload.body, is_private=payload.is_private)
    db.add(m)
    now = utcnow()
    t.updated_at = now
    if not payload.is_private and t.first_response_at is None:
        t.first_response_at = now
    audit(db, ticket_id, payload.author, "PRIVATE_NOTE_ADDED" if payload.is_private else "PUBLIC_REPLY_SENT",
          "Internal note" if payload.is_private else "Customer-visible reply")
    if payload.waiting_for_customer and t.status != "WAITING_CUSTOMER":
        old_status = t.status
        t.status = "WAITING_CUSTOMER"
        t.waiting_since = now
        audit(db, ticket_id, payload.author, "STATUS_CHANGED", f"{old_status} -> WAITING_CUSTOMER")
    db.commit()
    return {"id": m.id, "message": "Saved", "is_private": m.is_private}

@app.get("/api/tickets/{ticket_id}/messages")
def list_messages(ticket_id: int, include_private: bool = False, db: Session = Depends(get_db)):
    if not db.get(Ticket, ticket_id):
        raise HTTPException(404, "Ticket not found")
    stmt = select(Message).where(Message.ticket_id == ticket_id).order_by(Message.created_at)
    if not include_private:
        stmt = stmt.where(Message.is_private.is_(False))
    return [{"id": m.id, "author": m.author, "body": m.body, "is_private": m.is_private,
             "created_at": m.created_at.isoformat()} for m in db.scalars(stmt).all()]

@app.post("/api/tickets/{ticket_id}/status")
def update_status(ticket_id: int, payload: StatusUpdate, db: Session = Depends(get_db)):
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
    if t.assigned_agent and t.assigned_agent != payload.actor:
        raise HTTPException(409, "Only the assigned agent can change ticket status")
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
        paused = working_seconds_between(t.waiting_since, now) / 3600
        t.first_response_due_at = add_working_hours(t.first_response_due_at, paused)
        t.resolution_due_at = add_working_hours(t.resolution_due_at, paused)
        t.waiting_since = None
    if new_status == "RESOLVED":
        t.resolved_at = now
    if new_status == "CLOSED":
        t.resolved_at = t.resolved_at or now
    if old in ("RESOLVED", "CLOSED") and new_status == "IN_PROGRESS":
        t.resolved_at = None
    t.status = new_status
    t.updated_at = now
    audit(db, ticket_id, payload.actor, "STATUS_CHANGED", f"{old} -> {new_status}")
    db.commit()
    return ticket_dict(t)

@app.post("/api/tickets/{ticket_id}/priority")
def update_priority(ticket_id: int, payload: PriorityUpdate, db: Session = Depends(get_db)):
    t = db.get(Ticket, ticket_id)
    if not t:
        raise HTTPException(404, "Ticket not found")
    priority = payload.priority.upper()
    if priority not in PRIORITY_TARGETS:
        raise HTTPException(400, "Invalid priority")
    if t.assigned_agent and t.assigned_agent != payload.actor:
        raise HTTPException(409, "Only the assigned agent can change ticket priority")
    old = t.priority
    if old == priority:
        return ticket_dict(t)
    # Documented assumption: new SLA target is calculated from change time.
    response_hours, _ = PRIORITY_TARGETS[priority]
    now = utcnow()
    if t.first_response_at is None:
        t.first_response_due_at = add_working_hours(now, response_hours)
    t.resolution_due_at = resolution_deadline(priority, now)
    t.priority = priority
    t.updated_at = now
    audit(db, t.id, payload.actor, "PRIORITY_CHANGED", f"{old} -> {priority}; targets recalculated from change time")
    db.commit()
    return ticket_dict(t)

@app.get("/api/tickets/{ticket_id}/audit")
def list_audit(ticket_id: int, db: Session = Depends(get_db)):
    if not db.get(Ticket, ticket_id):
        raise HTTPException(404, "Ticket not found")
    rows = db.scalars(select(AuditEvent).where(AuditEvent.ticket_id == ticket_id).order_by(AuditEvent.created_at)).all()
    return [{"id": x.id, "actor": x.actor, "action": x.action, "details": x.details,
             "created_at": x.created_at.isoformat()} for x in rows]

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "Frontdesk"}

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
