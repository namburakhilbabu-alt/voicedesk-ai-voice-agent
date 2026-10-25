"""Send emails (Gmail) and phone notifications (Telegram).

If a service isn't configured, the message is skipped and the reason is returned,
so the app still works for a demo with no keys set.
"""

import smtplib
from email.message import EmailMessage

import httpx

import config
import db


def send_email(to_addr: str, subject: str, body: str) -> tuple[bool, str]:
    if not (config.GMAIL_ADDRESS and config.GMAIL_APP_PASSWORD):
        return False, "email not configured"
    if db.emails_last_hour() >= config.MAX_EMAILS_PER_HOUR:
        return False, "hourly email limit reached"
    msg = EmailMessage()
    msg["From"] = f"{config.BUSINESS_NAME} <{config.GMAIL_ADDRESS}>"
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
            smtp.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
            smtp.send_message(msg)
    except Exception as e:  # noqa: BLE001 - report any SMTP failure back to the caller
        return False, f"email failed: {e}"
    db.log_email(to_addr, subject)
    return True, "sent"


def send_telegram(text: str) -> tuple[bool, str]:
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return False, "telegram not configured"
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text},
            timeout=15,
        )
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return False, f"telegram failed: {e}"
    return True, "sent"
