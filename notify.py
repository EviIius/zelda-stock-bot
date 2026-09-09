"""
Notification channels.

Every configured channel gets every alert. A channel that fails logs the
failure and does not prevent the others from firing -- the whole point is
that you find out, so no single broken integration should be able to
swallow the news.

Configure by setting environment variables (or .env); a channel turns
itself on when its variables are present.

  Discord   DISCORD_WEBHOOK_URL
  Pushover  PUSHOVER_TOKEN, PUSHOVER_USER
  SMS       SMS_TO_NUMBER, SMS_CARRIER_GATEWAY, GMAIL_USER, GMAIL_APP_PASSWORD
"""

from __future__ import annotations

import json
import os
import smtplib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.text import MIMEText

import requests

TIMEOUT = 15

# Colour carries meaning at a glance on a phone, before any text is read.
COLORS = {
    "in_stock": 0x2ECC71,   # green  - go now
    "preorder": 0xF1C40F,   # yellow - go now, it's a pre-order
    "health": 0xE67E22,     # orange - something is degraded
    "feed": 0x5865F2,       # blurple - social tip, unverified
    "heartbeat": 0x95A5A6,  # grey   - routine
    "test": 0x3498DB,       # blue   - test
}


def _clean(name: str) -> str:
    """
    Read an env var, tolerating the ways a URL gets mangled on the way in:
    stray surrounding quotes, trailing whitespace, a newline from a paste.
    """
    return os.environ.get(name, "").strip().strip('"').strip("'").strip()


@dataclass
class Alert:
    title: str
    body: str
    url: str | None = None
    cart_url: str | None = None
    price: str | None = None
    urgent: bool = True
    kind: str = "in_stock"
    #: [(name, value, inline)] rendered as embed fields.
    fields: list[tuple[str, str, bool]] = field(default_factory=list)
    #: PNG bytes of the buy box, attached so you can judge it yourself.
    image_png: bytes | None = None
    footer: str | None = None


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
def _discord(alert: Alert) -> None:
    webhook = _clean("DISCORD_WEBHOOK_URL")
    if not webhook:
        return

    fields = [
        {"name": name, "value": value[:1024], "inline": inline}
        for name, value, inline in alert.fields
        if value
    ][:25]

    embed = {
        "title": alert.title[:256],
        "description": alert.body[:4000],
        "color": COLORS.get(alert.kind, 0x95A5A6),
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": (alert.footer or "Zelda Switch 2 monitor")[:2048]},
    }
    if alert.url:
        embed["url"] = alert.url
    if alert.image_png:
        embed["image"] = {"url": "attachment://buybox.png"}

    payload = {"embeds": [embed]}
    # Plain content as well as the embed, so the phone's lock-screen preview
    # shows the news rather than an empty "sent an embed" line.
    if alert.urgent:
        bits = ["@here", alert.title]
        if alert.cart_url:
            bits.append(f"\nCart: {alert.cart_url}")
        elif alert.url:
            bits.append(f"\n{alert.url}")
        payload["content"] = " ".join(bits)[:2000]
    else:
        payload["content"] = alert.title[:2000]

    if alert.image_png:
        resp = requests.post(
            webhook,
            data={"payload_json": json.dumps(payload)},
            files={"files[0]": ("buybox.png", alert.image_png, "image/png")},
            timeout=TIMEOUT + 15,
        )
    else:
        resp = requests.post(webhook, json=payload, timeout=TIMEOUT)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Pushover
# ---------------------------------------------------------------------------
def _pushover(alert: Alert) -> None:
    token = _clean("PUSHOVER_TOKEN")
    user = _clean("PUSHOVER_USER")
    if not (token and user):
        return

    data = {
        "token": token,
        "user": user,
        "title": alert.title[:250],
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

    files = {"attachment": ("buybox.png", alert.image_png, "image/png")} if alert.image_png else None
    resp = requests.post("https://api.pushover.net/1/messages.json",
                         data=data, files=files, timeout=TIMEOUT + 15)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Carrier email-to-SMS gateway (free, but slow -- backup only)
# ---------------------------------------------------------------------------
def _sms(alert: Alert) -> None:
    to_number = _clean("SMS_TO_NUMBER")
    gateway = _clean("SMS_CARRIER_GATEWAY")
    gmail_user = _clean("GMAIL_USER")
    gmail_pass = _clean("GMAIL_APP_PASSWORD")
    if not all([to_number, gateway, gmail_user, gmail_pass]):
        return
    if not alert.urgent:
        return  # don't burn texts on routine messages

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
    if _clean("DISCORD_WEBHOOK_URL"):
        names.append("discord")
    if _clean("PUSHOVER_TOKEN") and _clean("PUSHOVER_USER"):
        names.append("pushover")
    if all(_clean(k) for k in ("SMS_TO_NUMBER", "SMS_CARRIER_GATEWAY", "GMAIL_USER", "GMAIL_APP_PASSWORD")):
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
