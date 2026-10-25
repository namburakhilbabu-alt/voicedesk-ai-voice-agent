"""The brain: Claude + the MCP tools.

ToolBox       starts mcp_server.py and talks to it over MCP.
Receptionist  one conversation (phone or browser). Claude decides when to use tools.
wrap_up()     after the call: summarize it, save it, and alert the owner only if it matters.
"""

import os
import sys
from contextlib import AsyncExitStack
from typing import Literal

import anthropic
from mcp import Client, StdioServerParameters
from pydantic import BaseModel, Field

import config
import db

# Tools the receptionist may use. The MCP server has more (list_bookings, recent_call_insights),
# but those expose other patients' data, so only the owner's MCP clients should use them.
RECEPTIONIST_TOOLS = {"check_availability", "book_appointment", "find_appointments",
                      "cancel_appointment", "answer_faq", "notify_owner"}

# A local tool (not MCP): lets Claude decide when the call is over.
END_CALL_TOOL = {
    "name": "end_call",
    "description": "Hang up. Use right after you have said goodbye.",
    "input_schema": {
        "type": "object",
        "properties": {"reason": {"type": "string", "description": "Why the call is ending"}},
        "required": ["reason"],
    },
}

SYSTEM_PROMPT = f"""You are {config.AGENT_NAME}, the AI receptionist of {config.BUSINESS_NAME}, a dental clinic \
in Bengaluru. You are on a live voice call. The customer filled in a "call me back" form on the clinic's \
website, so you are calling them.

How to speak:
- Everything you write is read aloud. Keep each reply to one to three short sentences.
- No lists, markdown, emojis, or links. Ask one question at a time.
- Say dates like "Tuesday the 28th" and times like "5 PM", never "2026-10-28" or "17:00".
- Be warm, natural, and efficient, like a good front-desk person.

Call plan:
1. Greet them by name and introduce yourself as {config.AGENT_NAME}, {config.BUSINESS_NAME}'s AI assistant, \
calling because they asked for a call back. Check it's a good time. If it isn't, offer to call later, say \
goodbye, and end the call. Example: "Hi Priya, this is {config.AGENT_NAME}, {config.BUSINESS_NAME}'s AI \
assistant. You asked us to call you back. Is now a good time?"
2. Find out what they need and how urgent it is.
3. If they want an appointment, ask their preferred day and time of day, check availability, offer at most \
two slots, and book once they agree. Use the phone and email from the caller details; don't ask for them again.
4. Answer questions about prices, timings, location, payment, or insurance with answer_faq. Never invent \
prices or policies; if the FAQ doesn't cover something, say the team will follow up.
5. When everything is handled, confirm the next step in one sentence, say goodbye, and use end_call.

Rules:
- Never claim something is booked or cancelled unless the tool confirmed it.
- No medical diagnosis. For severe pain, swelling, bleeding, or a broken tooth, ask them to come in today \
and use notify_owner with priority "urgent".
- Use notify_owner during the call only for urgent matters. The owner gets a call summary automatically.
- If the customer is rude or the call goes off-topic, steer back politely; if it continues, end the call."""


INSIGHT_PROMPT = f"""You review calls handled by the AI receptionist of {config.BUSINESS_NAME} \
(a dental clinic) and decide what the busy owner needs to know.

Set alert_owner to true only when the call needs the owner's attention or is a business opportunity:
- a hot lead or high-value treatment interest (implants, braces/aligners, root canal, crowns, whitening)
- the customer wanted to book but couldn't (no suitable slot, undecided, price concern)
- an unhappy or angry customer, a complaint, or an emergency
- a request the receptionist couldn't handle
Routine check-up or cleaning bookings and simple questions that were fully answered do not need an alert."""


class CallInsight(BaseModel):
    summary: str = Field(description="One or two sentences: what happened on the call")
    caller_need: str = Field(description="What the customer wanted, in a few words")
    outcome: Literal["booked", "cancelled_or_rescheduled", "interested_not_booked", "question_answered",
                     "call_back_later", "not_interested", "no_real_conversation"]
    sentiment: Literal["positive", "neutral", "negative"]
    lead_score: Literal["hot", "warm", "cold"]
    alert_owner: bool
    alert_reason: str = Field(description="Why the owner should look at this call (empty if no alert)")
    suggested_follow_up: str = Field(description="One concrete next step for the clinic, or 'None'")


_claude_client: anthropic.AsyncAnthropic | None = None


def claude() -> anthropic.AsyncAnthropic:
    global _claude_client
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to .env (see .env.example).")
    if _claude_client is None:
        _claude_client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _claude_client


class ToolBox:
    """An MCP client connected to mcp_server.py (started as a subprocess)."""

    def __init__(self):
        self.client: Client | None = None
        self.tools: list[dict] = []  # tool definitions in Claude's format
        self._stack = AsyncExitStack()

    async def start(self) -> None:
        server = StdioServerParameters(
            command=sys.executable,
            args=[str(config.ROOT / "mcp_server.py")],
            env=dict(os.environ),  # pass secrets (e.g. on Hugging Face) to the server process
        )
        self.client = await self._stack.enter_async_context(Client(server))
        listed = await self.client.list_tools()
        self.tools = [
            {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
            for t in listed.tools if t.name in RECEPTIONIST_TOOLS
        ]

    async def stop(self) -> None:
        await self._stack.aclose()

    async def call(self, name: str, args: dict) -> tuple[str, bool]:
        """Run an MCP tool. Returns (text result, is_error)."""
        result = await self.client.call_tool(name, args)
        text = "\n".join(c.text for c in result.content if c.type == "text")
        return text, bool(result.is_error)


class Receptionist:
    """One call. `start()` gives the greeting; `reply(text)` answers the customer."""

    def __init__(self, toolbox: ToolBox, channel: str, name: str, phone: str, email: str = "", reason: str = "",
                 request_id: int | None = None):
        self.toolbox = toolbox
        self.channel = channel  # "phone" or "browser"
        self.request_id = request_id  # the call-back request (phone calls)
        self.caller = {"name": name, "phone": phone, "email": email, "reason": reason}
        self.messages: list[dict] = []    # the conversation as Claude sees it
        self.transcript: list[dict] = []  # {"role": "agent" | "caller" | "tool", "text": ..., tools also "name"/"input"}
        self.ended = False
        self.turns = 0
        self.started_at = db.now()

    async def start(self) -> str:
        n = db.now()
        return await self._turn(
            f"[Call connected via {self.channel}. Now: {n:%A %Y-%m-%d %H:%M} ({config.TIMEZONE.key}).\n"
            f"Caller details: name={self.caller['name']}; phone={self.caller['phone']}; "
            f"email={self.caller['email'] or 'not given'}; reason from the form={self.caller['reason'] or 'not given'}.\n"
            "Start the call now.]"
        )

    async def reply(self, caller_text: str) -> str:
        self.transcript.append({"role": "caller", "text": caller_text})
        self.turns += 1
        if self.turns >= config.MAX_TURNS_PER_CALL:
            caller_text += "\n[Call time limit reached: wrap up politely in one sentence and use end_call.]"
        return await self._turn(caller_text)

    async def _turn(self, user_content: str) -> str:
        """Send one message to Claude and run any tools it asks for, until it has a spoken reply."""
        self.messages.append({"role": "user", "content": user_content})
        spoken: list[str] = []

        for _ in range(6):  # at most 6 tool rounds per turn
            response = await claude().messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                tools=self.toolbox.tools + [END_CALL_TOOL],
                messages=self.messages,
                output_config={"effort": "low"},   # fast replies matter more than deep thinking on a call
                cache_control={"type": "ephemeral"},  # reuse the prompt + tools between turns (cheaper)
            )
            self.messages.append({"role": "assistant", "content": response.content})
            spoken += [b.text for b in response.content if b.type == "text"]

            if response.stop_reason == "refusal":
                spoken.append("Sorry, I can't help with that on this call.")
                break
            if response.stop_reason != "tool_use":
                break

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                if block.name == "end_call":
                    self.ended = True
                    text, is_error = "Call ended.", False
                else:
                    try:
                        text, is_error = await self.toolbox.call(block.name, block.input)
                    except Exception as e:  # noqa: BLE001 - let Claude see and recover from tool failures
                        text, is_error = f"Tool failed: {e}", True
                self.transcript.append({"role": "tool", "name": block.name, "input": block.input, "text": text})
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": text,
                                "is_error": is_error})
            self.messages.append({"role": "user", "content": results})
            if self.ended:
                break

        reply = " ".join(s.strip() for s in spoken if s.strip())
        if not reply and self.ended:
            reply = "Thank you for your time. Goodbye!"
        self.transcript.append({"role": "agent", "text": reply})
        return reply


def transcript_text(conv: Receptionist) -> str:
    who = {"agent": config.AGENT_NAME, "caller": "Customer"}
    lines = [f"[tool {t['name']}({t['input']})] {t['text']}" if t["role"] == "tool" else f"{who[t['role']]}: {t['text']}"
             for t in conv.transcript]
    c = conv.caller
    return (f"Channel: {conv.channel}. Caller: {c['name']} ({c['phone']}). Form reason: {c['reason'] or 'none'}.\n\n"
            + "\n".join(lines))


async def wrap_up(conv: Receptionist) -> dict:
    """After a call: create the insight, alert the owner if it's worth it, and save the call."""
    if not any(t["role"] == "caller" for t in conv.transcript):
        insight = CallInsight(
            summary="The customer requested a call back but didn't pick up or didn't speak.",
            caller_need=conv.caller["reason"] or "unknown", outcome="no_real_conversation",
            sentiment="neutral", lead_score="warm", alert_owner=conv.channel == "phone",
            alert_reason="Missed call-back: the customer asked to be called but didn't answer.",
            suggested_follow_up="Try calling again later or send a WhatsApp message.")
    else:
        response = await claude().messages.parse(
            model=config.CLAUDE_MODEL,
            max_tokens=2048,
            system=INSIGHT_PROMPT,
            messages=[{"role": "user", "content": transcript_text(conv)}],
            output_format=CallInsight,
            output_config={"effort": "low"},
        )
        insight = response.parsed_output or CallInsight(
            summary="Could not summarize this call.", caller_need="unknown", outcome="no_real_conversation",
            sentiment="neutral", lead_score="cold", alert_owner=False, alert_reason="",
            suggested_follow_up="Review the transcript.")

    alerted = insight.alert_owner or config.OWNER_ALERTS == "all"
    if alerted:
        urgent = insight.sentiment == "negative" or "emergenc" in insight.alert_reason.lower()
        await conv.toolbox.call("notify_owner", {"message": owner_message(conv, insight),
                                                 "priority": "urgent" if urgent else "normal"})
    data = insight.model_dump()
    db.save_call(conv.channel, conv.caller["name"], conv.caller["phone"], conv.transcript, data, alerted,
                 conv.started_at, conv.request_id)
    return {**data, "alerted": alerted}


def owner_message(conv: Receptionist, i: CallInsight) -> str:
    badge = {"hot": "🔥 HOT LEAD", "warm": "🌤 WARM LEAD", "cold": "❄️ COLD"}[i.lead_score]
    mood = {"positive": "🙂 positive", "neutral": "😐 neutral", "negative": "😠 negative"}[i.sentiment]
    return (f"📞 Call insight: {badge}\n"
            f"Caller: {conv.caller['name']} ({conv.caller['phone']})\n"
            f"Wanted: {i.caller_need}\n"
            f"Outcome: {i.outcome.replace('_', ' ')}\n"
            f"Mood: {mood}\n"
            f"Summary: {i.summary}\n"
            f"Why you're seeing this: {i.alert_reason or 'all-calls mode'}\n"
            f"👉 Next step: {i.suggested_follow_up}")
