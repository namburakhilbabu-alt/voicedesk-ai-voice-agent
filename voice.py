"""Voice for the browser call: speech-to-text (Groq Whisper) and text-to-speech (Edge-TTS).

Phone calls don't use this file: Twilio does speech-to-text and text-to-speech itself.
"""

import os
import tempfile
from pathlib import Path

import edge_tts
from groq import AsyncGroq

import config

_groq: AsyncGroq | None = None


def stt_enabled() -> bool:
    return bool(config.GROQ_API_KEY)


async def transcribe(audio_path: str) -> str:
    global _groq
    if _groq is None:
        _groq = AsyncGroq(api_key=config.GROQ_API_KEY)
    path = Path(audio_path)
    result = await _groq.audio.transcriptions.create(
        model="whisper-large-v3-turbo",
        file=(path.name, path.read_bytes()),
        language="en",
    )
    return result.text.strip()


async def speak(text: str) -> str | None:
    """Turn text into an MP3 file and return its path (None if TTS fails)."""
    if not text:
        return None
    fd, out = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)  # edge-tts writes the file itself (closing first also avoids file locks on Windows)
    try:
        await edge_tts.Communicate(text, config.TTS_VOICE).save(out)
    except Exception:  # noqa: BLE001 - the call still works as text if TTS is unavailable
        return None
    return out
