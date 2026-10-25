"""Real phone calls with Twilio.

How a phone call works:
1. place_due_calls() finds due "call me back" requests and asks Twilio to ring the customer.
2. /twilio/answer    Twilio calls this when they pick up; we answer with Claude's greeting (TwiML).
3. /twilio/turn      Twilio sends what the customer said (its own speech-to-text); we answer with Claude's reply.
4. /twilio/status    Twilio tells us the call is over; we summarize it and alert the owner if needed.
"""

import asyncio
import logging
import re

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from twilio.request_validator import RequestValidator
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse

import config
import db
from agent import Receptionist, ToolBox, wrap_up

log = logging.getLogger("voicedesk.phone")
router = APIRouter(prefix="/twilio")

toolbox: ToolBox | None = None             # set by app.py at startup
active_calls: dict[str, Receptionist] = {}  # Twilio CallSid -> conversation
silences: dict[str, int] = {}               # CallSid -> times the customer said nothing

PHONE_RE = re.compile(r"^\+\d{8,15}$")
CALL_PROBLEMS = {"no-answer": "No answer. We'll let the clinic know.", "busy": "Your line was busy.",
                 "failed": "The call couldn't be connected.", "canceled": "The call was cancelled.",
                 "completed": "The call ended before the conversation started."}


def can_call(phone: str) -> tuple[bool, str]:
    """Checked when a call-back is requested: setup, number format, and the demo's daily limits."""
    if not config.phone_calls_enabled():
        return False, "Phone calls are not set up on this demo (Twilio keys missing)."
    if not PHONE_RE.match(phone):
        return False, "Use international format, e.g. +919876543210."
    if config.ALLOWED_PHONE_NUMBERS and phone not in config.ALLOWED_PHONE_NUMBERS:
        return False, "This demo can only call pre-approved numbers. Try the browser call instead."
    if db.call_requests_today() >= config.MAX_PHONE_CALLS_PER_DAY:
        return False, "Today's phone-call limit for this demo is reached. Try the browser call."
    if db.call_requests_today(phone) >= config.MAX_CALLS_PER_NUMBER:
        return False, "This number has already had today's demo calls. Try the browser call."
    return True, ""


def place_call(request_row) -> str:
    """Ask Twilio to ring the customer. Returns the Twilio CallSid."""
    client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
    call = client.calls.create(
        to=request_row["phone"],
        from_=config.TWILIO_PHONE_NUMBER,
        url=f"{config.PUBLIC_URL}/twilio/answer?req={request_row['id']}",
        status_callback=f"{config.PUBLIC_URL}/twilio/status?req={request_row['id']}",
        status_callback_event=["completed"],
        timeout=30,                         # stop ringing after 30 seconds
        time_limit=config.MAX_CALL_SECONDS,  # hard cap on call length (and on Twilio cost)
    )
    return call.sid


async def place_due_calls() -> None:
    """Ring every customer whose call-back time has come."""
    for r in db.due_call_requests():
        if not config.phone_calls_enabled():  # limits were already checked when the call was requested
            db.set_call_request_status(r["id"], "failed", "Phone calls are not set up on this demo.")
            continue
        try:
            sid = await asyncio.to_thread(place_call, r)
            db.set_call_request_status(r["id"], "calling", sid)
        except Exception as e:  # noqa: BLE001
            log.exception("Twilio call failed")
            db.set_call_request_status(r["id"], "failed", f"Twilio error: {getattr(e, 'msg', e)}"[:300])


async def scheduler_loop() -> None:
    """Every 5 seconds, place the calls that are due."""
    while True:
        await asyncio.sleep(5)
        try:
            await place_due_calls()
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            log.exception("scheduler error")


# ---------------------------------------------------------------- Twilio webhooks

async def _verified_form(request: Request) -> dict:
    """Read Twilio's form data and check it really came from Twilio."""
    form = dict(await request.form())
    url = config.PUBLIC_URL + request.url.path + (f"?{request.url.query}" if request.url.query else "")
    signature = request.headers.get("X-Twilio-Signature", "")
    if not RequestValidator(config.TWILIO_AUTH_TOKEN).validate(url, form, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")
    return form


def _twiml(vr: VoiceResponse) -> Response:
    return Response(content=str(vr), media_type="application/xml")


def _say_and_listen(text: str) -> Response:
    vr = VoiceResponse()
    gather = vr.gather(input="speech", action="/twilio/turn", method="POST", speech_timeout="auto",
                       language=config.TWILIO_LANGUAGE, action_on_empty_result=True)
    gather.say(text, voice=config.TWILIO_VOICE)
    return _twiml(vr)


def _say_and_hang_up(text: str) -> Response:
    vr = VoiceResponse()
    vr.say(text, voice=config.TWILIO_VOICE)
    vr.hangup()
    return _twiml(vr)


@router.post("/answer")
async def answer(request: Request, req: int):
    form = await _verified_form(request)
    r = db.get_call_request(req)
    if r is None:
        return _say_and_hang_up("Sorry, something went wrong. Goodbye.")
    conv = Receptionist(toolbox, "phone", r["name"], r["phone"], r["email"] or "", r["reason"] or "", req)
    active_calls[form["CallSid"]] = conv
    db.set_call_request_status(req, "in_call", form["CallSid"])
    return _say_and_listen(await conv.start())


@router.post("/turn")
async def turn(request: Request):
    form = await _verified_form(request)
    sid = form.get("CallSid", "")
    conv = active_calls.get(sid)
    if conv is None:
        return _say_and_hang_up("Sorry, this call has expired. Goodbye.")

    said = (form.get("SpeechResult") or "").strip()
    if not said:
        silences[sid] = silences.get(sid, 0) + 1
        if silences[sid] >= 2:
            return _say_and_hang_up("I can't hear you, so I'll end the call. We'll reach out again. Goodbye!")
        return _say_and_listen("Sorry, I didn't catch that. Could you say it again?")

    reply = await conv.reply(said)
    return _say_and_hang_up(reply) if conv.ended else _say_and_listen(reply)


@router.post("/status")
async def status(request: Request, req: int, background: BackgroundTasks):
    form = await _verified_form(request)
    sid, call_status = form.get("CallSid", ""), form.get("CallStatus", "")
    silences.pop(sid, None)
    conv = active_calls.pop(sid, None)
    if conv is None:  # never answered: busy / no-answer / failed
        r = db.get_call_request(req)
        if r is None:
            return Response(status_code=204)
        conv = Receptionist(toolbox, "phone", r["name"], r["phone"], r["email"] or "", r["reason"] or "", req)
    if call_status == "completed" and conv.transcript:
        db.set_call_request_status(req, "done", "Call completed")
    else:
        db.set_call_request_status(req, "failed", CALL_PROBLEMS.get(call_status, "The call didn't connect."))
    background.add_task(wrap_up, conv)  # summarize after replying, so Twilio isn't kept waiting
    return Response(status_code=204)
