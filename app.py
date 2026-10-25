"""VoiceDesk web app: the call-back request page, the in-browser call, and the owner dashboard.

Run:  python app.py   → open http://localhost:7860
"""

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")  # keep the app from sending usage stats
import gradio as gr
import uvicorn
from fastapi import FastAPI

import config
import db
import phone
import voice
from agent import Receptionist, ToolBox, wrap_up

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

toolbox = ToolBox()
browser_calls: dict[str, Receptionist] = {}  # session id -> live browser call

WHEN = {"As soon as possible": 0, "In 5 minutes": 5, "In 15 minutes": 15}
REASONS = ["General check-up", "Teeth cleaning", "Tooth pain", "Braces / aligners", "Teeth whitening",
           "Dental implant", "Price enquiry", "Something else"]
STEPS = ["Requested", "Calling you", "On the call", "Call summary"]

CSS = """
.vd-steps {display: flex; gap: 6px; margin: 4px 0 8px;}
.vd-step {flex: 1; padding: 9px 6px; border-radius: 10px; text-align: center; font-size: 13px;
          background: #f1f5f9; color: #64748b;}
.vd-step.done {background: #ccfbf1; color: #0f766e;}
.vd-step.active {background: #0d9488; color: #fff; font-weight: 600; animation: vd-pulse 1.4s ease-in-out infinite;}
.vd-step.failed {background: #fee2e2; color: #b91c1c; font-weight: 600;}
.vd-msg {font-size: 14px; line-height: 1.5; padding: 2px;}
.vd-notice {padding: 10px 12px; border-radius: 10px; background: #fef3c7; color: #92400e; font-size: 14px;}
@keyframes vd-pulse {50% {opacity: 0.65;}}
"""


# ---------------------------------------------------------------- helpers

def mask_phone(text: str) -> str:
    """Hide most of each phone number on the public dashboard."""
    return re.sub(r"\+\d{4,}(\d{4})", lambda m: "+••••••" + m.group(1), text or "")


def visitor_id(request: gr.Request | None) -> str:
    """A short anonymous ID per visitor (a hash of their IP address), used for the per-visitor limits.
    On Hugging Face the real IP is in the X-Forwarded-For header."""
    if request is None:
        return "local"
    ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or getattr(request.client, "host", "")
    return hashlib.sha256(f"voicedesk:{ip}".encode()).hexdigest()[:16]


def clean_phone(raw: str) -> str:
    """'98765 43210' → '+919876543210'. Numbers with a country code are kept as they are."""
    digits = re.sub(r"[\s\-()]", "", raw or "")
    return "+91" + digits if re.fullmatch(r"\d{10}", digits) else digits


def chat_view(conv: Receptionist) -> list[dict]:
    messages = []
    for t in conv.transcript:
        if t["role"] == "caller":
            messages.append({"role": "user", "content": t["text"]})
        elif t["role"] == "agent":
            messages.append({"role": "assistant", "content": t["text"]})
        else:
            messages.append({"role": "assistant", "content": t["text"],
                             "metadata": {"title": f"🔧 MCP tool: {t['name']}"}})
    return messages


def insight_view(i: dict) -> str:
    sent = ("✅ **Sent to the owner** (worth their attention)" if i["alerted"]
            else "🗂️ **Logged only** (routine call, owner not disturbed)")
    lead = {"hot": "🔥 Hot", "warm": "🌤 Warm", "cold": "❄️ Cold"}[i["lead_score"]]
    return (f"### 📋 Call insight\n{sent}\n\n"
            f"| Lead | Outcome | Mood |\n|---|---|---|\n"
            f"| {lead} | {i['outcome'].replace('_', ' ')} | {i['sentiment']} |\n\n"
            f"**Summary:** {i['summary']}\n\n"
            + (f"**Why the owner sees it:** {i['alert_reason']}\n\n" if i["alerted"] else "")
            + f"**Next step:** {i['suggested_follow_up']}")


def notice(text: str) -> str:
    return f'<div class="vd-notice">⚠️ {html.escape(text)}</div>'


def stepper(states: list[str], message: str) -> str:
    """The 4-step progress bar shown after a call-back request."""
    icons = {"done": "✓", "active": "●", "todo": "○", "failed": "✕"}
    cells = "".join(f'<div class="vd-step {s}">{icons[s]} {label}</div>' for label, s in zip(STEPS, states))
    return f'<div class="vd-steps">{cells}</div><div class="vd-msg">{message}</div>'


LIVE = "🟢 **On call with {agent}.** Press record 🎤, speak, then press stop. Or type below."
ENDED = "⚪ **No active call.** Start one any time."


# ---------------------------------------------------------------- call-back request (real phone)

async def request_phone_call(name, phone_no, email, reason, when, consent, request: gr.Request = None):
    name, phone_no = (name or "").strip(), clean_phone(phone_no)
    if not name or not phone_no:
        return None, notice("Please enter your name and phone number.")
    if not consent:
        return None, notice("Please tick the consent box so we're allowed to call you.")
    visitor = visitor_id(request)
    if db.usage_today("phone_request", visitor) >= config.MAX_PHONE_CALLS_PER_VISITOR:
        return None, notice("You've used today's demo phone calls. Try the browser call, or come back tomorrow.")
    ok, why = phone.can_call(phone_no)
    if not ok:
        return None, notice(why)
    db.record_usage(visitor, "phone_request")
    at = db.now() + timedelta(minutes=WHEN[when])
    req_id = db.add_call_request(name, phone_no, (email or "").strip(), reason or "", at)
    return req_id, track_request(req_id)


def track_request(req_id):
    """Live status of a call-back request (refreshed every 2 seconds)."""
    if not req_id:
        return gr.skip()
    r = db.get_call_request(req_id)
    if r is None:
        return ""
    agent, number = config.AGENT_NAME, html.escape(r["phone"])
    status = r["status"]
    if status == "failed":
        return stepper(["done", "failed", "todo", "todo"], f"⚠️ {html.escape(r['note'] or 'The call failed.')}")
    if status == "pending":
        at = datetime.fromisoformat(r["scheduled_for"])
        when = "in a few seconds" if at <= db.now() + timedelta(seconds=30) else f"at <b>{at:%I:%M %p}</b>"
        return stepper(["done", "todo", "todo", "todo"],
                       f"⏰ {agent} will call <b>{number}</b> {when}. Keep your phone close.")
    if status == "calling":
        return stepper(["done", "active", "todo", "todo"], f"📳 Your phone should be ringing now. Pick up to talk to {agent}!")
    if status == "in_call":
        return stepper(["done", "done", "active", "todo"], f"🗣️ You're on the call with {agent}.")
    call = db.call_for_request(req_id)
    if call is None:
        return stepper(["done", "done", "done", "active"], "⏳ Call finished. Writing the call summary…")
    i = json.loads(call["insight"])
    owner = ("📲 <b>Sent to the owner's Telegram</b> (worth their attention)." if call["alerted"]
             else "🗂️ Logged on the dashboard (routine call, owner not disturbed).")
    return stepper(["done"] * 4, f"✅ <b>Call complete.</b> Here's what the clinic owner got:<br>"
                                 f"<i>{html.escape(i['summary'])}</i><br>{owner}")


# ---------------------------------------------------------------- in-browser call

async def start_browser_call(name, phone_no, email, reason, session_id, request: gr.Request = None):
    if session_id in browser_calls:  # restart: close the previous call first
        await end_browser_call(session_id)
    if not (name or "").strip():
        return None, [], None, "⚠️ Please enter your name first (left side).", ""
    visitor = visitor_id(request)
    if db.usage_today("browser_call", visitor) >= config.MAX_BROWSER_CALLS_PER_VISITOR:
        return None, [], None, "⚠️ You've used today's demo calls. Please come back tomorrow.", ""
    if db.usage_today("browser_call") >= config.MAX_BROWSER_CALLS_PER_DAY:
        return None, [], None, "⚠️ Today's demo call limit is reached. Please try again tomorrow.", ""
    db.record_usage(visitor, "browser_call")
    conv = Receptionist(toolbox, "browser", name.strip(), clean_phone(phone_no) or "not given",
                        (email or "").strip(), reason or "")
    try:
        greeting = await conv.start()
    except Exception as e:  # noqa: BLE001 - show setup problems (e.g. missing API key) on the page
        return None, [], None, f"⚠️ Could not start the call: {e}", ""
    session_id = uuid.uuid4().hex
    browser_calls[session_id] = conv
    return session_id, chat_view(conv), await voice.speak(greeting), LIVE.format(agent=config.AGENT_NAME), ""


async def caller_says(text: str, session_id):
    """Shared by the mic and the text box: send the customer's words, get the agent's reply."""
    conv = browser_calls.get(session_id)
    if conv is None:
        return [], None, "⚠️ No active call. Press **Start call in browser** first.", ""
    if not text:
        return chat_view(conv), None, LIVE.format(agent=config.AGENT_NAME) + "\n\n_Didn't catch that, try again._", ""
    try:
        reply = await conv.reply(text)
    except Exception as e:  # noqa: BLE001
        return chat_view(conv), None, f"⚠️ {config.AGENT_NAME} couldn't reply: {e}", ""
    audio = await voice.speak(reply)
    if conv.ended:
        insight = await end_browser_call(session_id)
        return chat_view(conv), audio, ENDED, insight
    return chat_view(conv), audio, LIVE.format(agent=config.AGENT_NAME), ""


async def on_mic(audio_path, session_id):
    if not audio_path:
        return gr.skip(), gr.skip(), gr.skip(), gr.skip(), None
    if not voice.stt_enabled():
        return gr.skip(), gr.skip(), "⚠️ Voice input needs a GROQ_API_KEY. Type your reply instead.", gr.skip(), None
    if os.path.getsize(audio_path) > 10_000_000:  # roughly over a minute of audio
        return gr.skip(), gr.skip(), "⚠️ That recording is too long. Please keep it under a minute.", gr.skip(), None
    try:
        text = await voice.transcribe(audio_path)
    except Exception as e:  # noqa: BLE001
        return gr.skip(), gr.skip(), f"⚠️ Couldn't understand the audio ({e}). Try again or type.", gr.skip(), None
    return *(await caller_says(text, session_id)), None  # last value clears the recorder


async def on_typed(text, session_id):
    return *(await caller_says((text or "").strip(), session_id)), ""


async def end_browser_call(session_id) -> str:
    conv = browser_calls.pop(session_id, None)
    if conv is None:
        return ""
    return insight_view(await wrap_up(conv))


async def on_hang_up(session_id):
    insight = await end_browser_call(session_id)
    return None, ENDED, insight


# ---------------------------------------------------------------- owner dashboard

def dashboard():
    inbox = [[r["created_at"][5:16].replace("T", " "), r["priority"], mask_phone(r["message"]), r["delivered_via"]]
             for r in db.owner_messages()]
    bookings = [[r["date"], r["time"], r["name"], r["service"], mask_phone(r["phone"]), r["status"]]
                for r in db.upcoming_appointments()]
    calls = []
    for r in db.recent_calls():
        i = json.loads(r["insight"] or "{}")
        calls.append([r["started_at"][5:16].replace("T", " "), r["channel"], r["name"], i.get("lead_score"),
                      (i.get("outcome") or "").replace("_", " "), "✅" if r["alerted"] else "", i.get("summary")])
    requests = [[r["created_at"][5:16].replace("T", " "), r["name"], mask_phone(r["phone"]),
                 r["scheduled_for"][11:16], r["status"], mask_phone(r["note"] or "")[:60]]
                for r in db.recent_call_requests()]
    return inbox, bookings, calls, requests


HOW_IT_WORKS = f"""
### How VoiceDesk works

```
 📱 Phone call (Twilio)        🎧 Browser call
  Twilio speech-to-text         Groq Whisper speech-to-text
            \\                    /
             ▼                  ▼
        🤖 Agent: Claude ({config.CLAUDE_MODEL})
             │  decides which tool to use
             ▼   (Model Context Protocol)
        🔌 MCP server (mcp_server.py)
           check_availability · book_appointment · find_appointments
           cancel_appointment · answer_faq (RAG) · notify_owner
             │
             ▼
        🗄️ SQLite  ·  📧 Gmail confirmation  ·  📲 Telegram owner alert
```

**After every call** Claude reads the transcript and writes a structured insight (need, outcome,
mood, lead score). Only calls worth the owner's time are pushed to their phone; the rest are just logged.

**Why MCP?** The same tool server also plugs into Claude Desktop, Claude Code, or Cursor, so the owner
can ask *"What bookings do I have tomorrow?"* without any extra code.
"""


# ---------------------------------------------------------------- page layout

with gr.Blocks(title=f"{config.BUSINESS_NAME}: AI Receptionist", analytics_enabled=False) as ui:
    session = gr.State(None)     # live browser call
    request_id = gr.State(None)  # call-back request being tracked

    gr.Markdown(f"# 🦷 {config.BUSINESS_NAME}\n"
                f"### Get a call back from **{config.AGENT_NAME}**, our AI receptionist: questions answered and "
                "appointments booked in under two minutes.\n"
                "*VoiceDesk demo with sample clinic data.*")

    with gr.Tab("📞 Get a call back"):
        with gr.Row(equal_height=False):
            with gr.Column(scale=2):
                gr.Markdown("#### Request a call back")
                name = gr.Textbox(label="Your name", placeholder="Priya Sharma")
                phone_no = gr.Textbox(label="Mobile number", placeholder="+91 98765 43210",
                                      info="10-digit Indian numbers get +91 added automatically.")
                email = gr.Textbox(label="Email (optional)", placeholder="For the booking confirmation")
                reason = gr.Dropdown(REASONS, label="What can we help with?", value=REASONS[0])
                when = gr.Radio(list(WHEN), value="As soon as possible", label="When should we call?")
                consent = gr.Checkbox(label=f"I agree to receive a call from {config.BUSINESS_NAME}'s AI assistant "
                                            "on this number.")
                call_btn = gr.Button("📞 Request a call back", variant="primary", size="lg")
                tracker = gr.HTML()
                gr.Markdown("---\n**No phone handy?** Have the same conversation right here:")
                start_btn = gr.Button("🎧 Start call in browser")
            with gr.Column(scale=3):
                call_status = gr.Markdown(ENDED)
                chat = gr.Chatbot(label="Live transcript", height=380)
                agent_audio = gr.Audio(label=f"{config.AGENT_NAME}'s voice", autoplay=True, interactive=False)
                mic = gr.Audio(sources=["microphone"], type="filepath", label="🎤 Your voice")
                with gr.Row():
                    typed = gr.Textbox(placeholder="...or type your reply and press Enter", show_label=False, scale=4)
                    hang_btn = gr.Button("🔴 Hang up", variant="stop", scale=1)
                insight_box = gr.Markdown()

    with gr.Tab("📊 Owner dashboard"):
        gr.Markdown("What the clinic owner sees. Updates every few seconds. Phone numbers are masked.")
        inbox_tbl = gr.Dataframe(headers=["Time", "Priority", "Message", "Delivered via"],
                                 label="📲 Owner alerts", wrap=True)
        calls_tbl = gr.Dataframe(headers=["Time", "Channel", "Caller", "Lead", "Outcome", "Alerted", "Summary"],
                                 label="📞 Call log", wrap=True)
        bookings_tbl = gr.Dataframe(headers=["Date", "Time", "Name", "Service", "Phone", "Status"],
                                    label="🗓️ Upcoming appointments")
        requests_tbl = gr.Dataframe(headers=["Requested", "Name", "Phone", "Call at", "Status", "Note"],
                                    label="⏰ Call-back requests")

    with gr.Tab("🔌 How it works"):
        gr.Markdown(HOW_IT_WORKS)

    # ---- events
    call_btn.click(request_phone_call, [name, phone_no, email, reason, when, consent], [request_id, tracker])
    gr.Timer(2).tick(track_request, request_id, tracker)

    call_outputs = [chat, agent_audio, call_status, insight_box]
    start_btn.click(start_browser_call, [name, phone_no, email, reason, session],
                    [session, chat, agent_audio, call_status, insight_box], concurrency_limit=10)
    mic.stop_recording(on_mic, [mic, session], call_outputs + [mic], concurrency_limit=10)
    typed.submit(on_typed, [typed, session], call_outputs + [typed], concurrency_limit=10)
    hang_btn.click(on_hang_up, session, [session, call_status, insight_box], concurrency_limit=10)

    dash_outputs = [inbox_tbl, bookings_tbl, calls_tbl, requests_tbl]
    gr.Timer(5).tick(dashboard, None, dash_outputs)
    ui.load(dashboard, None, dash_outputs)


# ---------------------------------------------------------------- server

@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    await toolbox.start()
    phone.toolbox = toolbox
    scheduler = asyncio.create_task(phone.scheduler_loop())
    logging.getLogger("voicedesk").info("MCP tools ready: %s", [t["name"] for t in toolbox.tools])
    yield
    scheduler.cancel()
    await toolbox.stop()


app = FastAPI(lifespan=lifespan)
app.include_router(phone.router)


@app.get("/health")
def health():
    return {"ok": True, "phone_calls": config.phone_calls_enabled(), "voice_input": voice.stt_enabled(),
            "model": config.CLAUDE_MODEL}


app = gr.mount_gradio_app(app, ui, path="/", theme=gr.themes.Soft(primary_hue="teal"), css=CSS)

if __name__ == "__main__":
    print(f"\n  VoiceDesk is starting → open http://localhost:{config.PORT}\n")
    uvicorn.run(app, host=config.HOST, port=config.PORT)
