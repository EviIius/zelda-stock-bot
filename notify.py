"""
Notification channels.

Every configured channel gets every alert. A channel that fails logs the
failure and does not prevent the others from firing -- the whole point is
that you find out, so no single broken integration should be able to
swallow the news.

Configure by setting environment variables; a channel turns itself on when
its variables are present.

  Discord   DISCORD_WEBHOOK_URL
  Pushover  PUSHOVER_TOKEN, PUSHOVER_USER
  SMS       SMS_TO_NUMBER, SMS_CARRIER_GATEWAY, GMAIL_USER, GMAIL_APP_PASSWORD
"""

from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from email.mime.text import MIMEText

import requests

TIMEOUT = 15


@dataclass
class Alert:
    title: str
    body: str
    url: str | None = None
    cart_url: str | None = None
    price: str | None = None
    urgent: bool = True


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
def _discord(alert: Alert) -> None:
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        return

    fields = []
    if alert.price:
        fields.append({"name": "Price", "value": alert.price, "inline": True})
    if alert.cart_url:
        fields.append(
            {"name": "Add to cart", "value": f"[Tap here]({alert.cart_url})", "inline": True}
        )

    payload = {
        # Plain content (not just the embed) so the mobile push preview
        # actually shows the news on your lock screen.
        "content": ("@here " if alert.urgent else "") + alert.title,
        "embeds": [
            {
                "title": alert.title,
                "description": alert.body[:4000],
                "url": alert.url,
                "color": 0xE03131 if alert.urgent else 0x868E96,
                "fields": fields,
            }
        ],
    }
    resp = requests.post(webhook, json=payload, timeout=TIMEOUT)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Pushover
# ---------------------------------------------------------------------------
def _pushover(alert: Alert) -> None:
    token = os.environ.get("PUSHOVER_TOKEN", "").strip()
    user = os.environ.get("PUSHOVER_USER", "").strip()
    if not (token and user):
        return

    data = {
        "token": token,
        "user": user,
        "title": alert.title,
        "message": alert.body[:1000],
        "url": alert.cart_url or alert.url or "",
        "url_title": "Add to cart" if alert.cart_url else "Open product page",
    }
    if alert.urgent:
        # Priority 2 = emergency: overrides silent mode and keeps re-alerting
        # every 30s for up to an hour until you acknowledge it on your phone.
        data.update({"priority": 2, "retry": 30, "expire": 3600, "sound": "persistent"})
    else:
        data["priority"] = -1  # quiet, no sound

    resp = requests.post("https://api.pushover.net/1/messages.json", data=data, timeout=TIMEOUT)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Carrier email-to-SMS gateway (free, but slow -- keep as a backup only)
# ---------------------------------------------------------------------------
def _sms(alert: Alert) -> None:
    to_number = os.environ.get("SMS_TO_NUMBER", "").strip()
    gateway = os.environ.get("SMS_CARRIER_GATEWAY", "").strip()
    gmail_user = os.environ.get("GMAIL_USER", "").strip()
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not all([to_number, gateway, gmail_user, gmail_pass]):
        return
    if not alert.urgent:
        return  # don't burn texts on health warnings

    text = f"{alert.title}\n{alert.cart_url or alert.url or ''}"
    msg = MIMEText(text)
    msg["From"] = gmail_user
    msg["To"] = f"{to_number}@{gateway}"
    msg["Subject"] = ""

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=TIMEOUT) as server:
        server.login(gmail_user, gmail_pass)
        server.sendmail(gmail_user, [msg["To"]], msg.as_string())


CHANNELS = {"discord": _discord, "pushover": _pushover, "sms": _sms}


def configured_channels() -> list[str]:
    names = []
    if os.environ.get("DISCORD_WEBHOOK_URL", "").strip():
        names.append("discord")
    if os.environ.get("PUSHOVER_TOKEN", "").strip() and os.environ.get("PUSHOVER_USER", "").strip():
        names.append("pushover")
    if all(
        os.environ.get(k, "").strip()
        for k in ("SMS_TO_NUMBER", "SMS_CARRIER_GATEWAY", "GMAIL_USER", "GMAIL_APP_PASSWORD")
    ):
        names.append("sms")
    return names


def send(alert: Alert) -> dict[str, str]:
    """Fan out to every configured channel. Returns {channel: 'ok'|error}."""
    results: dict[str, str] = {}
    for name in configured_channels():
        try:
            CHANNELS[name](alert)
            results[name] = "ok"
        except Exception as exc:  # noqa: BLE001 - never let one channel kill the rest
            results[name] = f"{type(exc).__name__}: {exc}"
    if not results:
        results["(none)"] = "no notification channels configured"
    return results
