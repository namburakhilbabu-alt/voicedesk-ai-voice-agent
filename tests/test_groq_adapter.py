"""Test the free Groq provider (LLM_PROVIDER=groq) with a fake Groq API, so no keys are needed.

Checks that a full tool-using turn of the receptionist works through the adapter, and that
the call summary is parsed from JSON.

Run:  .venv/bin/python tests/test_groq_adapter.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
TEST_DB = ROOT / "data" / "test_groq.db"
for suffix in ("", "-wal", "-shm"):
    Path(f"{TEST_DB}{suffix}").unlink(missing_ok=True)
os.environ.update({"DB_PATH": str(TEST_DB), "LLM_PROVIDER": "groq", "GROQ_API_KEY": "test-key",
                   "GROQ_MODEL": "openai/gpt-oss-20b", "ANTHROPIC_API_KEY": ""})
sys.path.insert(0, str(ROOT))

import agent  # noqa: E402
import db  # noqa: E402
import groq_llm  # noqa: E402


def reply(content=None, tool_calls=None):
    return NS(choices=[NS(message=NS(content=content, tool_calls=tool_calls))])


def call(cid, name, args):
    return NS(id=cid, function=NS(name=name, arguments=json.dumps(args)))


class FakeCompletions:
    def __init__(self, script):
        self.script, self.requests = script, []

    async def create(self, **kw):
        self.requests.append(kw)
        return self.script.pop(0)


class FakeToolBox:
    tools = [{"name": "check_availability", "description": "Free slots",
              "input_schema": {"type": "object", "properties": {"date": {"type": "string"}}}}]

    def __init__(self):
        self.calls = []

    async def call(self, name, args):
        self.calls.append((name, args))
        return "Free slots: 18:00, 18:30", False


failures = []


def check(ok, what):
    print(("  PASS  " if ok else "  FAIL  ") + what)
    if not ok:
        failures.append(what)


async def main():
    db.init_db()
    client = agent.claude()
    check(isinstance(client, groq_llm.GroqClient), "LLM_PROVIDER=groq selects the Groq adapter")

    insight = {"summary": "Asked for an evening slot.", "caller_need": "Check-up", "outcome": "booked",
               "sentiment": "positive", "lead_score": "warm", "alert_owner": False, "alert_reason": "",
               "suggested_follow_up": "None"}
    fake = FakeCompletions([
        reply("Hi Priya, this is Aria. Is now a good time?"),
        reply(None, [call("c1", "check_availability", {"date": "2030-01-07"})]),
        reply("I have 6 PM or 6:30 PM. Which works?"),
        reply(None, [call("c2", "end_call", {"reason": "done"})]),
        reply(json.dumps(insight)),
    ])
    client.messages.client = NS(chat=NS(completions=fake))

    toolbox = FakeToolBox()
    conv = agent.Receptionist(toolbox, "browser", "Priya", "+919812345678")
    greeting = await conv.start()
    check(greeting.startswith("Hi Priya"), "greeting comes back as text")

    answer = await conv.reply("Tomorrow evening please")
    check(toolbox.calls == [("check_availability", {"date": "2030-01-07"})], "tool call reaches the MCP toolbox")
    check("6 PM" in answer, "reply after the tool result")
    sent = fake.requests[2]
    roles = [m["role"] for m in sent["messages"]]
    check(roles == ["system", "user", "assistant", "user", "assistant", "tool"], f"history translated: {roles}")
    check(sent["messages"][4]["tool_calls"][0]["function"]["name"] == "check_availability"
          and sent["messages"][5]["tool_call_id"] == "c1", "tool call and result are linked by id")
    check({t["function"]["name"] for t in sent["tools"]} == {"check_availability", "end_call"},
          "tools sent in OpenAI function format")
    check(sent["reasoning_effort"] == "low", "gpt-oss runs at low reasoning effort")

    await conv.reply("Thanks, bye")
    check(conv.ended, "end_call hangs up")

    result = await agent.wrap_up(conv)
    check(result["outcome"] == "booked" and result["alerted"] is False, "call summary parsed from JSON")
    check(fake.requests[-1]["response_format"] == {"type": "json_object"}, "summary uses JSON mode")

    async def broken(**_kw):
        raise RuntimeError("rate limited")
    client.messages.client = NS(chat=NS(completions=NS(create=broken)))
    conv2 = agent.Receptionist(toolbox, "browser", "Ravi", "+919800000000")
    conv2.transcript.append({"role": "caller", "text": "hello"})
    result = await agent.wrap_up(conv2)
    check(result["summary"] == "Could not summarize this call." and db.recent_calls()[0]["name"] == "Ravi",
          "LLM outage during the summary still saves the call")

    print("\nALL PASSED ✅" if not failures else f"\n{len(failures)} FAILED ❌: {failures}")
    sys.exit(1 if failures else 0)


asyncio.run(main())
