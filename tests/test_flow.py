"""End-to-end test of VoiceDesk with a scripted fake Claude, so no API keys or money are needed.

It runs the real MCP server, database, browser-call handlers, and Twilio webhooks.

Run:  .venv/bin/python tests/test_flow.py
"""

import asyncio
import html
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = ROOT / "data" / "test.db"
for suffix in ("", "-wal", "-shm"):
    Path(f"{TEST_DB}{suffix}").unlink(missing_ok=True)

# Fake settings, set before the app modules read them. No real service is contacted.
os.environ.update({
    "DB_PATH": str(TEST_DB), "ANTHROPIC_API_KEY": "test-key", "GROQ_API_KEY": "",
    "TWILIO_ACCOUNT_SID": "ACtest", "TWILIO_AUTH_TOKEN": "test-token", "TWILIO_PHONE_NUMBER": "+15550001111",
    "PUBLIC_URL": "https://demo.example.com", "GMAIL_ADDRESS": "", "TELEGRAM_BOT_TOKEN": "",
    "GRADIO_ANALYTICS_ENABLED": "False",
})
sys.path.insert(0, str(ROOT))

import gradio as gr  # noqa: E402
import httpx  # noqa: E402
from twilio.request_validator import RequestValidator  # noqa: E402

import agent  # noqa: E402
import app as webapp  # noqa: E402
import db  # noqa: E402
import phone  # noqa: E402

DAY = db.next_open_day(db.now().date()).isoformat()


# ---------------------------------------------------------------- fake Claude

def text(t):
    return NS(type="text", text=t)


def tool(tool_id, name, tool_input):
    return NS(type="tool_use", id=tool_id, name=name, input=tool_input)


class FakeMessages:
    """Plays back a script of Claude responses and records what was sent."""

    def __init__(self):
        self.script, self.sent_roles = [], []

    async def create(self, **kw):
        self.sent_roles.append([m["role"] for m in kw["messages"]])
        self.last_request = kw
        content, stop_reason = self.script.pop(0)
        return NS(content=content, stop_reason=stop_reason)

    async def parse(self, **kw):
        return NS(parsed_output=agent.CallInsight(
            summary="Priya asked about braces and booked a consultation.", caller_need="Braces consultation",
            outcome="booked", sentiment="positive", lead_score="hot", alert_owner=True,
            alert_reason="High-value braces interest", suggested_follow_up="Share aligner pricing on WhatsApp"))


fake = NS(messages=FakeMessages())
agent.claude = lambda: fake


def booking_call_script():
    return [
        ([text("Hi Priya, this is Aria from Smile Dental. Is now a good time?")], "end_turn"),
        ([text("Let me check."), tool("t1", "check_availability", {"date": DAY})], "tool_use"),
        ([text("I have 6 PM or 6:30 PM free tomorrow. Which works?")], "end_turn"),
        ([tool("t2", "book_appointment", {"name": "Priya", "phone": "+919812345678", "date": DAY,
                                          "time": "18:00", "service": "Braces consultation"})], "tool_use"),
        ([text("You're booked for 6 PM. Goodbye!"), tool("t3", "end_call", {"reason": "done"})], "tool_use"),
    ]


def twilio_headers(path: str, params: dict) -> dict:
    url = "https://demo.example.com" + path
    return {"X-Twilio-Signature": RequestValidator("test-token").compute_signature(url, params)}


# ---------------------------------------------------------------- the test

failures = []


def check(ok: bool, what: str):
    print(("  PASS  " if ok else "  FAIL  ") + what)
    if not ok:
        failures.append(what)


async def main():
    async with webapp.app.router.lifespan_context(webapp.app):
        print("\nMCP")
        check({t["name"] for t in webapp.toolbox.tools} == agent.RECEPTIONIST_TOOLS,
              "receptionist sees only its 6 MCP tools (owner tools hidden)")

        print("\nBrowser call")
        fake.messages.script = booking_call_script()
        sid, chat, audio, status, _ = await webapp.start_browser_call("Priya", "+919812345678", "", "braces", None)
        check(bool(sid) and chat[0]["content"].startswith("Hi Priya"), "call starts with Aria's greeting")
        check(audio is not None and os.path.getsize(audio) > 1000, "greeting turned into speech (Edge-TTS)")
        req = fake.messages.last_request
        check(req["model"] == "claude-haiku-5-5" and req["output_config"] == {"effort": "low"},
              "uses claude-haiku-5-5 at low effort")
        check("end_call" in [t["name"] for t in req["tools"]], "end_call tool is offered")

        chat, audio, status, insight, _ = await webapp.on_typed("Tomorrow evening please", sid)
        check(any(m.get("metadata", {}).get("title") == "🔧 MCP tool: check_availability" for m in chat),
              "tool call is shown in the transcript")
        check("6 PM" in chat[-1]["content"], "agent offers slots after checking availability")
        check(len(gr.Chatbot().postprocess(chat).root) == len(chat), "transcript format accepted by Gradio")

        chat, audio, status, insight, _ = await webapp.on_typed("6 PM works", sid)
        check(status.startswith("⚪"), "call ends after Claude uses end_call")
        check("Sent to the owner" in insight, "insight shown and owner alerted")
        check(sid not in webapp.browser_calls, "call session cleaned up")
        booked = [r for r in db.appointments_on(db.date.fromisoformat(DAY)) if r["name"] == "Priya"]
        check(len(booked) == 1 and booked[0]["time"] == "18:00", "booking saved through MCP")
        check("HOT LEAD" in db.owner_messages()[0]["message"], "owner inbox has the hot-lead alert")
        expected = ["user", "assistant"] * 4 + ["user"]
        check(fake.messages.sent_roles[-1] == expected, "messages alternate user/assistant correctly")

        print("\nCall-back request card")
        _, msg = await webapp.request_phone_call("Priya", "98123 45678", "", "Braces / aligners",
                                                 "As soon as possible", False)
        check("consent" in msg, "consent box is required")
        req_id, msg = await webapp.request_phone_call("Priya", "98123 45678", "", "Braces / aligners",
                                                      "As soon as possible", True)
        check(db.get_call_request(req_id)["phone"] == "+919812345678", "10-digit number gets +91 added")
        check("in a few seconds" in msg and "vd-steps" in msg, "tracker: call scheduled")

        placed = []

        class FakeTwilio:  # stands in for twilio.rest.Client
            def __init__(self, *_):
                self.calls = NS(create=lambda **kw: placed.append(kw) or NS(sid="CA1"))

        phone.Client = FakeTwilio
        await phone.place_due_calls()
        check(len(placed) == 1 and placed[0]["to"] == "+919812345678", "scheduler rings the customer")
        check(placed[0]["time_limit"] == 240 and placed[0]["url"].startswith("https://demo.example.com/twilio/answer"),
              "Twilio gets the 4-minute call cap and our webhook URL")
        check("ringing" in webapp.track_request(req_id), "tracker: phone ringing")

        print("\nPhone call (Twilio webhooks)")
        fake.messages.script = booking_call_script()
        transport = httpx.ASGITransport(app=webapp.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            answer_path, params = f"/twilio/answer?req={req_id}", {"CallSid": "CA1", "CallStatus": "in-progress"}
            r = await client.post(answer_path, data=params)
            check(r.status_code == 403, "request without Twilio signature is rejected")
            r = await client.post(answer_path, data=params, headers=twilio_headers(answer_path, params))
            check(r.status_code == 200 and "<Gather" in r.text and "Hi Priya" in r.text,
                  "pick-up → greeting + listen (TwiML)")
            check('voice="Polly.Kajal-Neural"' in r.text, "Indian-English neural voice")
            check("on the call" in webapp.track_request(req_id), "tracker: on the call")

            async def say(words):
                p = {"CallSid": "CA1", "SpeechResult": words}
                return await client.post("/twilio/turn", data=p, headers=twilio_headers("/twilio/turn", p))

            check("didn't catch that" in (await say("")).text, "silence → asks again")
            r = await say("Tomorrow evening please")
            check("6 PM" in r.text and "<Gather" in r.text, "customer speaks → Claude's reply (TwiML)")
            r = await say("6 PM works")
            check("<Hangup" in r.text, "goodbye → hang up")

            status_path, p = f"/twilio/status?req={req_id}", {"CallSid": "CA1", "CallStatus": "completed"}
            r = await client.post(status_path, data=p, headers=twilio_headers(status_path, p))
            check(r.status_code == 204, "call-ended callback accepted")
            await asyncio.sleep(0.5)
            check(db.get_call_request(req_id)["status"] == "done", "call request marked done")
            check("Call complete" in webapp.track_request(req_id) and "Telegram" in webapp.track_request(req_id),
                  "tracker: summary shown, owner alerted")
            last = db.recent_calls()[0]
            check(last["channel"] == "phone" and last["alerted"] == 1, "phone call saved with insight")

            req2 = db.add_call_request("Ravi", "+919800000000", "", "", db.now())
            status_path, p = f"/twilio/status?req={req2}", {"CallSid": "CA2", "CallStatus": "no-answer"}
            await client.post(status_path, data=p, headers=twilio_headers(status_path, p))
            await asyncio.sleep(0.3)
            check("Missed call-back" in db.owner_messages()[0]["message"], "unanswered call alerts the owner")
            check("No answer" in webapp.track_request(req2), "tracker: shows 'No answer'")

            print("\nWeb server")
            r = await client.get("/health")
            check(r.status_code == 200 and r.json()["phone_calls"] is True, f"/health → {r.json()}")
            r = await client.get("/")
            check(r.status_code == 200 and "gradio" in r.text.lower(), "web page served at /")

        print("\nDemo limits (protect your API bill)")
        visitor = lambda ip: NS(headers={"x-forwarded-for": ip}, client=NS(host="10.0.0.1"))  # noqa: E731
        for _ in range(2):
            db.record_usage("local", "browser_call")  # "local" already made 1 browser call above
        *_, status, _ = await webapp.start_browser_call("Priya", "", "", "", None)
        check("used today's demo calls" in status, "4th browser call from the same visitor is blocked")
        fake.messages.script = booking_call_script()[:1]
        sid, *_ = await webapp.start_browser_call("Ravi", "", "", "", None, visitor("5.6.7.8"))
        check(sid is not None, "a different visitor can still call")
        check(webapp.visitor_id(visitor("5.6.7.8")) != webapp.visitor_id(visitor("9.9.9.9"))
              and "5.6.7.8" not in webapp.visitor_id(visitor("5.6.7.8")), "visitors told apart by hashed IP")
        await webapp.end_browser_call(sid)

        args = ("Priya", "9812345678", "", "Check-up", "As soon as possible", True)
        req, _ = await webapp.request_phone_call(*args, visitor("1.1.1.1"))
        check(req is not None, "2nd call to the same number is allowed")
        req, msg = await webapp.request_phone_call(*args, visitor("2.2.2.2"))
        check(req is None and "already had today's demo calls" in html.unescape(msg), "3rd call to the same number is blocked")
        req, msg = await webapp.request_phone_call("Asha", "9000000001", "", "Check-up", "As soon as possible", True)
        check(req is not None, "visitor's 2nd phone request is allowed")
        req, msg = await webapp.request_phone_call("Asha", "9000000002", "", "Check-up", "As soon as possible", True)
        check(req is None and "used today's demo phone calls" in html.unescape(msg), "visitor's 3rd phone request is blocked")

        conv = agent.Receptionist(webapp.toolbox, "browser", "Test", "+910000000000")
        conv.turns = 13
        fake.messages.script = []  # any Claude call would fail the test
        reply = await conv.reply("x" * 5000)
        check(conv.ended and "time limit" in reply, "runaway call is ended without another AI call")
        check(len(conv.transcript[0]["text"]) == 500, "very long input is cut to 500 characters")

        inbox, bookings, calls, requests = webapp.dashboard()
        priya = next(b for b in bookings if b[2] == "Priya")
        check("••••" in priya[4], "phone numbers masked on the public dashboard")

    print("\nALL PASSED ✅" if not failures else f"\n{len(failures)} FAILED ❌: {failures}")
    sys.exit(1 if failures else 0)


asyncio.run(main())
