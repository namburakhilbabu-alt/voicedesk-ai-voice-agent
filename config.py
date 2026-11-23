"""All settings in one place. Values come from the .env file (see .env.example)."""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


# --- The business the receptionist works for ---
BUSINESS_NAME = _env("BUSINESS_NAME", "Smile Dental Clinic")
AGENT_NAME = _env("AGENT_NAME", "Aria")
TIMEZONE = ZoneInfo(_env("TIMEZONE", "Asia/Kolkata"))

# --- AI ---
# LLM_PROVIDER=anthropic (default, Claude) or groq (free tier, uses GROQ_API_KEY)
LLM_PROVIDER = _env("LLM_PROVIDER", "anthropic").lower()
CLAUDE_MODEL = _env("CLAUDE_MODEL", "claude-haiku-5-5")
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
GROQ_MODEL = _env("GROQ_MODEL", "openai/gpt-oss-20b")

# --- Voice in the browser ---
GROQ_API_KEY = _env("GROQ_API_KEY")          # speech-to-text (Whisper), and the LLM when LLM_PROVIDER=groq
TTS_VOICE = _env("TTS_VOICE", "en-IN-NeerjaNeural")  # text-to-speech (Edge-TTS, free)

# --- Real phone calls (Twilio) ---
TWILIO_ACCOUNT_SID = _env("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = _env("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = _env("TWILIO_PHONE_NUMBER")
TWILIO_VOICE = _env("TWILIO_VOICE", "Polly.Kajal-Neural")  # Indian-English neural voice
TWILIO_LANGUAGE = _env("TWILIO_LANGUAGE", "en-IN")
PUBLIC_URL = _env("PUBLIC_URL").rstrip("/")  # e.g. https://you-voicedesk.hf.space

# --- Notifications ---
GMAIL_ADDRESS = _env("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = _env("GMAIL_APP_PASSWORD").replace(" ", "")
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")
# "interesting" = only calls the AI flags as worth the owner's time; "all" = every call
OWNER_ALERTS = _env("OWNER_ALERTS", "interesting")

# --- Safety limits for a public demo (they cap what visitors can spend on your keys) ---
ALLOWED_PHONE_NUMBERS = {n.strip() for n in _env("ALLOWED_PHONE_NUMBERS").split(",") if n.strip()}  # empty = anyone
MAX_BROWSER_CALLS_PER_DAY = int(_env("MAX_BROWSER_CALLS_PER_DAY", "30"))        # all visitors together
MAX_BROWSER_CALLS_PER_VISITOR = int(_env("MAX_BROWSER_CALLS_PER_VISITOR", "3"))  # per visitor per day
MAX_PHONE_CALLS_PER_DAY = int(_env("MAX_PHONE_CALLS_PER_DAY", "10"))
MAX_PHONE_CALLS_PER_VISITOR = int(_env("MAX_PHONE_CALLS_PER_VISITOR", "2"))
MAX_CALLS_PER_NUMBER = int(_env("MAX_CALLS_PER_NUMBER", "2"))                    # same phone number per day
MAX_CALL_SECONDS = int(_env("MAX_CALL_SECONDS", "240"))                          # Twilio hangs up after this
MAX_TURNS_PER_CALL = int(_env("MAX_TURNS_PER_CALL", "12"))
MAX_MESSAGE_CHARS = int(_env("MAX_MESSAGE_CHARS", "500"))                        # longer typed text is cut
MAX_EMAILS_PER_HOUR = int(_env("MAX_EMAILS_PER_HOUR", "5"))

DB_PATH = Path(_env("DB_PATH") or ROOT / "data" / "voicedesk.db")
HOST = _env("HOST", "127.0.0.1")  # local only; set HOST=0.0.0.0 when hosting (e.g. in Docker)
PORT = int(_env("PORT", "7860"))


def model_name() -> str:
    return f"{GROQ_MODEL} (Groq)" if LLM_PROVIDER == "groq" else CLAUDE_MODEL


def phone_calls_enabled() -> bool:
    return all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, PUBLIC_URL])
