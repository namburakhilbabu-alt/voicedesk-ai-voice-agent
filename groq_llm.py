"""Free alternative to Claude: run the receptionist on Groq (free tier, OpenAI-style tool calling).

Switch it on with LLM_PROVIDER=groq (it uses the same GROQ_API_KEY as voice input).

agent.py is written against the Anthropic Messages API. This adapter offers the same two calls,
`messages.create(...)` and `messages.parse(...)`, and translates to and from Groq's chat format,
so the conversation loop in agent.py doesn't change.
"""

import json
from types import SimpleNamespace as NS

from groq import AsyncGroq

import config


def _block_get(block, key, default=None):
    """Content blocks are SDK objects (Claude) or plain objects/dicts (this adapter)."""
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    """Anthropic-style history → OpenAI-style chat messages."""
    out = [{"role": "system", "content": system}]
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        if m["role"] == "assistant":
            text = " ".join(_block_get(b, "text") for b in content if _block_get(b, "type") == "text")
            calls = [{"id": _block_get(b, "id"), "type": "function",
                      "function": {"name": _block_get(b, "name"), "arguments": json.dumps(_block_get(b, "input"))}}
                     for b in content if _block_get(b, "type") == "tool_use"]
            msg = {"role": "assistant", "content": text or None}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:  # user turn holding tool results
            for b in content:
                if _block_get(b, "type") == "tool_result":
                    result = _block_get(b, "content")
                    if _block_get(b, "is_error"):
                        result = f"ERROR: {result}"
                    out.append({"role": "tool", "tool_call_id": _block_get(b, "tool_use_id"), "content": result})
                elif _block_get(b, "type") == "text":
                    out.append({"role": "user", "content": _block_get(b, "text")})
    return out


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                              "parameters": t["input_schema"]}} for t in tools]


def _extra(model: str) -> dict:
    # gpt-oss models reason before answering; keep that short so replies stay fast on a call.
    return {"reasoning_effort": "low"} if model.startswith("openai/gpt-oss") else {}


class _Messages:
    def __init__(self, client: AsyncGroq):
        self.client = client

    async def create(self, *, system: str, messages: list[dict], tools: list[dict] | None = None,
                     max_tokens: int = 1024, **_ignored):
        """Same shape of result as Claude: .content (text / tool_use blocks) and .stop_reason."""
        model = config.GROQ_MODEL
        kwargs = {"tools": to_openai_tools(tools), "tool_choice": "auto"} if tools else {}
        resp = await self.client.chat.completions.create(
            model=model, messages=to_openai_messages(system, messages), max_tokens=max_tokens,
            temperature=0.3, **kwargs, **_extra(model))
        msg = resp.choices[0].message
        content = []
        if msg.content and msg.content.strip():
            content.append(NS(type="text", text=msg.content.strip()))
        for call in msg.tool_calls or []:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            content.append(NS(type="tool_use", id=call.id, name=call.function.name, input=args))
        stop = "tool_use" if any(b.type == "tool_use" for b in content) else "end_turn"
        return NS(content=content, stop_reason=stop)

    async def parse(self, *, system: str, messages: list[dict], output_format, max_tokens: int = 1024, **_ignored):
        """Structured output: ask for JSON matching the Pydantic model and validate it."""
        model = config.GROQ_MODEL
        schema = json.dumps(output_format.model_json_schema())
        prompt = f"{system}\n\nReply with only a JSON object that matches this JSON schema:\n{schema}"
        resp = await self.client.chat.completions.create(
            model=model, messages=to_openai_messages(prompt, messages), max_tokens=max_tokens,
            temperature=0, response_format={"type": "json_object"}, **_extra(model))
        try:
            parsed = output_format.model_validate_json(resp.choices[0].message.content or "")
        except ValueError:
            parsed = None
        return NS(parsed_output=parsed)


class GroqClient:
    def __init__(self, api_key: str):
        self.messages = _Messages(AsyncGroq(api_key=api_key))
