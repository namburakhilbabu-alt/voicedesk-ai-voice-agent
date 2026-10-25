"""VoiceDesk MCP server: the clinic's tools, usable by any MCP client.

- The voice receptionist (agent.py) launches this file and calls the tools.
- Claude Desktop / Claude Code / Cursor can connect to the same file (see README).

Run:  python mcp_server.py           (stdio, for local MCP clients)
      python mcp_server.py --http    (streamable HTTP on http://127.0.0.1:8000/mcp)
"""

import json
import re
import sys
from datetime import date

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import config
import db
import notify

mcp = MCPServer(
    name="voicedesk",
    instructions=f"Front-desk tools for {config.BUSINESS_NAME}: appointments, clinic FAQ, owner alerts.",
)

PHONE_RE = re.compile(r"^\+\d{8,15}$")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ToolError(f"'{value}' is not a valid date. Use YYYY-MM-DD.") from None


def _nice_date(d: date) -> str:
    return d.strftime("%A, %d %B %Y")


def _nice_time(t: str) -> str:
    h, m = map(int, t.split(":"))
    return f"{(h - 1) % 12 + 1}{':%02d' % m if m else ''} {'AM' if h < 12 else 'PM'}"


# ---------------------------------------------------------------- receptionist tools

@mcp.tool()
def check_availability(date: str) -> str:
    """List free 30-minute appointment slots on a date.

    Args:
        date: The day to check, as YYYY-MM-DD.
    """
    d = _parse_date(date)
    if d < db.now().date():
        return f"{_nice_date(d)} is in the past."
    if d.weekday() == 6:
        return f"The clinic is closed on Sundays ({_nice_date(d)})."
    slots = db.free_slots(d)
    if not slots:
        return f"No free slots on {_nice_date(d)}."
    return f"Free slots on {_nice_date(d)} (24h times): {', '.join(slots)}"


@mcp.tool()
def book_appointment(name: str, phone: str, date: str, time: str, service: str, email: str = "") -> str:
    """Book an appointment in a free slot. Sends a confirmation email if an email is given.

    Args:
        name: Patient's full name.
        phone: Patient's phone number in international format, e.g. +919876543210.
        date: Appointment day, YYYY-MM-DD.
        time: Slot start time in 24h HH:MM, exactly as returned by check_availability.
        service: What the visit is for, e.g. "Check-up", "Root canal", "Braces consultation".
        email: Patient's email for the confirmation (optional).
    """
    d = _parse_date(date)
    t = time.strip().zfill(5)
    if t not in db.free_slots(d):
        return f"Cannot book: {t} on {_nice_date(d)} is not a free slot. Check availability again."
    appt_id = db.add_appointment(name.strip(), phone.strip(), email.strip(), service.strip(), d, t)

    result = f"Booked appointment #{appt_id}: {name} for {service} on {_nice_date(d)} at {_nice_time(t)}."
    if email.strip():
        sent, why = notify.send_email(
            email.strip(),
            f"Appointment confirmed: {_nice_date(d)} at {_nice_time(t)}",
            f"Hi {name},\n\nYour appointment at {config.BUSINESS_NAME} is confirmed.\n\n"
            f"  What:  {service}\n  When:  {_nice_date(d)} at {_nice_time(t)}\n  Booking ID: #{appt_id}\n\n"
            "Please arrive 10 minutes early. To cancel or reschedule, just reply to this email.\n\n"
            f"See you soon,\n{config.AGENT_NAME}, {config.BUSINESS_NAME}",
        )
        result += " Confirmation email sent." if sent else f" (Confirmation email not sent: {why}.)"
    return result


@mcp.tool()
def find_appointments(phone: str) -> str:
    """Find a patient's upcoming appointments by their phone number.

    Args:
        phone: Phone number in international format, e.g. +919876543210.
    """
    rows = db.appointments_for_phone(phone.strip())
    if not rows:
        return "No upcoming appointments for this phone number."
    return "\n".join(
        f"#{r['id']}: {r['service']} on {_nice_date(date.fromisoformat(r['date']))} at {_nice_time(r['time'])}"
        for r in rows)


@mcp.tool()
def cancel_appointment(appointment_id: int, phone: str) -> str:
    """Cancel an appointment. The phone number must match the booking.

    Args:
        appointment_id: The booking number, e.g. 12.
        phone: Phone number the appointment was booked with.
    """
    row = db.get_appointment(appointment_id)
    if not row or row["status"] != "booked":
        return f"No active appointment #{appointment_id}."
    if row["phone"] != phone.strip():
        return "That appointment is not booked under this phone number."
    db.cancel_appointment(appointment_id)
    return f"Cancelled appointment #{appointment_id} ({row['service']} on {row['date']} at {row['time']})."


@mcp.tool()
def answer_faq(question: str) -> str:
    """Look up clinic information: prices, timings, location, parking, payment, insurance,
    doctors, emergencies, cancellations, children, first visit.

    Args:
        question: The customer's question in plain words.
    """
    return "\n\n".join(search_faq(question)) or "No matching information in the clinic FAQ."


@mcp.tool()
def notify_owner(message: str, priority: str = "normal") -> str:
    """Send a message to the clinic owner's phone (Telegram) and dashboard inbox.

    Args:
        message: What the owner needs to know.
        priority: "urgent" for emergencies or angry customers, otherwise "normal".
    """
    prefix = "🚨 URGENT\n" if priority == "urgent" else ""
    sent, why = notify.send_telegram(prefix + message)
    db.add_owner_message(priority, message, "telegram" if sent else f"dashboard only ({why})")
    return "Owner notified." if sent else f"Saved to the owner's dashboard ({why})."


# ---------------------------------------------------------------- owner tools
# Not given to the receptionist (they expose other patients' data). Useful from
# Claude Desktop: "What bookings do I have tomorrow?"

@mcp.tool()
def list_bookings(date: str) -> str:
    """List all booked appointments on a date (owner/staff use).

    Args:
        date: The day, as YYYY-MM-DD.
    """
    d = _parse_date(date)
    rows = db.appointments_on(d)
    if not rows:
        return f"No bookings on {_nice_date(d)}."
    return f"Bookings on {_nice_date(d)}:\n" + "\n".join(
        f"{_nice_time(r['time'])}: {r['name']} ({r['service']}) {r['phone'] or ''}" for r in rows)


@mcp.tool()
def recent_call_insights(limit: int = 10) -> str:
    """Summaries of the most recent calls the AI receptionist handled (owner use).

    Args:
        limit: How many calls to return.
    """
    rows = db.recent_calls(limit)
    if not rows:
        return "No calls yet."
    lines = []
    for r in rows:
        i = json.loads(r["insight"] or "{}")
        lines.append(f"{r['started_at'][:16]} | {r['name']} ({r['channel']}) | {i.get('lead_score', '?')} lead | "
                     f"{i.get('outcome', '?')} | {i.get('summary', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------- tiny RAG over faq.md

STOPWORDS = set("the are you your our for and what how much does can have any there this that with".split())
# Everyday words a caller might use -> words that appear in the FAQ
SYNONYMS = {"cost": "price", "fee": "price", "charge": "price", "rate": "price", "expensive": "price",
            "time": "timings", "hour": "timings", "open": "timings", "close": "timings", "sunday": "closed",
            "where": "location", "address": "location", "reach": "metro", "car": "parking",
            "pay": "payment", "card": "payment", "emi": "payment", "kid": "children", "child": "children",
            "dentist": "doctors", "doctor": "doctors", "pain": "emergencies", "urgent": "emergencies"}


def _stems(text: str) -> set[str]:
    """Lower-case words cut to 4 letters, so 'park' matches 'parking' and 'prices' matches 'price'."""
    words = [w for w in re.findall(r"[a-z]+", text.lower()) if w not in STOPWORDS and len(w) > 2]
    singular = [w[:-1] if w.endswith("s") and not w.endswith("ss") else w for w in words]
    words += [SYNONYMS[w] for w in singular if w in SYNONYMS]
    return {w[:4] for w in words}


def search_faq(question: str, top_k: int = 2) -> list[str]:
    """Tiny RAG retriever: return the FAQ sections that share the most words with the question.
    Words in a section's title count double."""
    sections = (config.ROOT / "faq.md").read_text(encoding="utf-8").split("\n## ")[1:]
    q = _stems(question)
    scored = []
    for s in sections:
        title, _, body = s.partition("\n")
        scored.append((2 * len(q & _stems(title)) + len(q & _stems(body)), s))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [s.strip() for score, s in scored[:top_k] if score > 0]


if __name__ == "__main__":
    db.init_db()
    if "--http" in sys.argv:
        mcp.run(transport="streamable-http", host="127.0.0.1", port=8000)
    else:
        mcp.run()
