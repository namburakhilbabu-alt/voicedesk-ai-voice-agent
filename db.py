"""SQLite storage: appointments, call requests, call logs, and the owner's inbox.

Both the web app and the MCP server open this same file.
"""

import json
import sqlite3
from datetime import date, datetime, time, timedelta

import config

# Clinic hours: Mon-Sat 10:00-19:00, lunch 13:00-14:00, 30-minute slots. Closed Sunday.
OPEN, CLOSE, LUNCH = time(10, 0), time(19, 0), (time(13, 0), time(14, 0))
SLOT_MINUTES = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, phone TEXT, email TEXT, service TEXT,
    date TEXT NOT NULL, time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'booked',          -- booked | cancelled
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS call_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, phone TEXT, email TEXT, reason TEXT,
    scheduled_for TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',         -- pending | calling | in_call | done | failed
    note TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER,                              -- the call-back request, for phone calls
    channel TEXT, name TEXT, phone TEXT,
    transcript TEXT, insight TEXT, alerted INTEGER DEFAULT 0,
    started_at TEXT, ended_at TEXT
);
CREATE TABLE IF NOT EXISTS owner_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    priority TEXT, message TEXT, delivered_via TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    visitor TEXT, kind TEXT, created_at TEXT NOT NULL   -- kind: browser_call | phone_request
);
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    to_addr TEXT, subject TEXT, created_at TEXT NOT NULL
);
"""


def now() -> datetime:
    return datetime.now(config.TIMEZONE)


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create tables, and add a few sample bookings so the calendar isn't empty."""
    with connect() as conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 0:
            day1, day2 = next_open_day(now().date()), next_open_day(next_open_day(now().date()))
            samples = [
                ("Rahul Sharma", "Check-up", day1, "10:30"),
                ("Ananya Iyer", "Teeth cleaning", day1, "17:00"),
                ("Vikram Rao", "Root canal (follow-up)", day2, "11:00"),
                ("Meera Nair", "Whitening consultation", day2, "18:00"),
            ]
            conn.executemany(
                "INSERT INTO appointments (name, service, date, time, created_at) VALUES (?, ?, ?, ?, ?)",
                [(n, s, d.isoformat(), t, now().isoformat()) for n, s, d, t in samples],
            )


def next_open_day(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() == 6:  # Sunday
        d += timedelta(days=1)
    return d


# ---------- Appointments ----------

def all_slots(d: date) -> list[str]:
    if d.weekday() == 6:
        return []
    slots, t = [], datetime.combine(d, OPEN)
    while t.time() < CLOSE:
        if not (LUNCH[0] <= t.time() < LUNCH[1]):
            slots.append(t.strftime("%H:%M"))
        t += timedelta(minutes=SLOT_MINUTES)
    return slots


def free_slots(d: date) -> list[str]:
    with connect() as conn:
        taken = {r["time"] for r in conn.execute(
            "SELECT time FROM appointments WHERE date = ? AND status = 'booked'", (d.isoformat(),))}
    slots = [s for s in all_slots(d) if s not in taken]
    if d == now().date():  # no slots in the past
        slots = [s for s in slots if s > now().strftime("%H:%M")]
    return slots


def add_appointment(name, phone, email, service, d: date, t: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO appointments (name, phone, email, service, date, time, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, phone, email, service, d.isoformat(), t, now().isoformat()))
        return cur.lastrowid


def get_appointment(appt_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM appointments WHERE id = ?", (appt_id,)).fetchone()


def cancel_appointment(appt_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appt_id,))


def appointments_for_phone(phone: str) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM appointments WHERE phone = ? AND status = 'booked' AND date >= ? "
            "ORDER BY date, time", (phone, now().date().isoformat())).fetchall()


def appointments_on(d: date) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM appointments WHERE date = ? AND status = 'booked' ORDER BY time",
            (d.isoformat(),)).fetchall()


def upcoming_appointments(limit: int = 50) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM appointments WHERE date >= ? ORDER BY date, time LIMIT ?",
            (now().date().isoformat(), limit)).fetchall()


# ---------- Call requests ("call me back") ----------

def add_call_request(name, phone, email, reason, when: datetime) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO call_requests (name, phone, email, reason, scheduled_for, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, phone, email, reason, when.isoformat(), now().isoformat()))
        return cur.lastrowid


def get_call_request(req_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM call_requests WHERE id = ?", (req_id,)).fetchone()


def due_call_requests() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM call_requests WHERE status = 'pending' AND scheduled_for <= ? ORDER BY id",
            (now().isoformat(),)).fetchall()


def set_call_request_status(req_id: int, status: str, note: str = "") -> None:
    with connect() as conn:
        conn.execute("UPDATE call_requests SET status = ?, note = ? WHERE id = ?", (status, note, req_id))


def call_requests_today(phone: str | None = None) -> int:
    """Requests made today that weren't rejected (optionally for one phone number)."""
    sql = "SELECT COUNT(*) FROM call_requests WHERE status != 'failed' AND created_at >= ?"
    args = [now().date().isoformat()]
    if phone:
        sql += " AND phone = ?"
        args.append(phone)
    with connect() as conn:
        return conn.execute(sql, args).fetchone()[0]


def recent_call_requests(limit: int = 20) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM call_requests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


# ---------- Call log ----------

def save_call(channel, name, phone, transcript, insight, alerted, started_at, request_id=None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO calls (request_id, channel, name, phone, transcript, insight, alerted, started_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (request_id, channel, name, phone, json.dumps(transcript), json.dumps(insight), int(alerted),
             started_at.isoformat(), now().isoformat()))
        return cur.lastrowid


def call_for_request(req_id: int):
    with connect() as conn:
        return conn.execute("SELECT * FROM calls WHERE request_id = ? ORDER BY id DESC LIMIT 1", (req_id,)).fetchone()


# ---------- Usage per visitor (for the demo limits) ----------

def record_usage(visitor: str, kind: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO usage (visitor, kind, created_at) VALUES (?, ?, ?)",
                     (visitor, kind, now().isoformat()))


def usage_today(kind: str, visitor: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM usage WHERE kind = ? AND created_at >= ?"
    args = [kind, now().date().isoformat()]
    if visitor:
        sql += " AND visitor = ?"
        args.append(visitor)
    with connect() as conn:
        return conn.execute(sql, args).fetchone()[0]


def recent_calls(limit: int = 20) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM calls ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


# ---------- Owner inbox & email log ----------

def add_owner_message(priority: str, message: str, delivered_via: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO owner_inbox (priority, message, delivered_via, created_at) VALUES (?, ?, ?, ?)",
                     (priority, message, delivered_via, now().isoformat()))


def owner_messages(limit: int = 20) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM owner_inbox ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def log_email(to_addr: str, subject: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO emails (to_addr, subject, created_at) VALUES (?, ?, ?)",
                     (to_addr, subject, now().isoformat()))


def emails_last_hour() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM emails WHERE created_at >= ?",
                            ((now() - timedelta(hours=1)).isoformat(),)).fetchone()[0]
