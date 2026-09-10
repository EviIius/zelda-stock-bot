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
import base64
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.text import MIMEText

import requests

import config

TIMEOUT = 15
_OUTBOX_LOCK = threading.RLock()

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
    #: "console", "controller", or "all" controls Discord routing.
    group: str = "console"


def _alert_dict(alert: Alert) -> dict:
    data = {
        "title": alert.title,
        "body": alert.body,
        "url": alert.url,
        "cart_url": alert.cart_url,
        "price": alert.price,
        "urgent": alert.urgent,
        "kind": alert.kind,
        "fields": alert.fields,
        "footer": alert.footer,
        "group": alert.group,
    }
    if alert.image_png:
        data["image_png"] = base64.b64encode(alert.image_png).decode("ascii")
    return data


def _alert_from_dict(data: dict) -> Alert:
    image = data.get("image_png")
    fields = [tuple(field) for field in data.get("fields", [])]
    return Alert(
        title=data["title"], body=data["body"], url=data.get("url"),
        cart_url=data.get("cart_url"), price=data.get("price"),
        urgent=bool(data.get("urgent", True)), kind=data.get("kind", "in_stock"),
        fields=fields, image_png=base64.b64decode(image) if image else None,
        footer=data.get("footer"), group=data.get("group", "console"),
    )


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------
def _discord_payload(alert: Alert) -> dict:
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
        "footer": {"text": (alert.footer or "Stock Watch")[:2048]},
        "author": {"name": ("📦 PRODUCT STOCK WATCH" if alert.body.startswith("## 📦")
                            else "🎮 CONTROLLER STOCK WATCH" if alert.group == "controller"
                            else "🖥️ CONSOLE STOCK WATCH" if alert.group == "console"
                            else "🛰️ STOCK WATCH")},
    }
    if alert.url:
        embed["url"] = alert.url
    if alert.image_png:
        embed["image"] = {"url": "attachment://buybox.png"}

    payload = {"embeds": [embed]}
    # Plain content as well as the embed, so the phone's lock-screen preview
    # shows the news rather than an empty "sent an embed" line.
    if alert.urgent:
        bits = ["@here", f"**{alert.title}**"]
        if alert.cart_url:
            bits.append(f"\n🛒 **ADD TO CART:** {alert.cart_url}")
        elif alert.url:
            bits.append(f"\n🔗 **OPEN PRODUCT:** {alert.url}")
        payload["content"] = "\n".join(bits)[:2000]
    else:
        payload["content"] = alert.title[:2000]

    payload["allowed_mentions"] = {"parse": ["everyone"]} if alert.urgent else {"parse": []}
    return payload


def _webhook_for_group(group: str) -> str:
    if group == "controller":
        return _clean("CONTROLLER_DISCORD_WEBHOOK_URL") or _clean("DISCORD_WEBHOOK_URL")
    return _clean("DISCORD_WEBHOOK_URL")


def _discord_to(alert: Alert, group: str) -> None:
    webhook = _webhook_for_group(group)
    if not webhook:
        raise RuntimeError(f"no Discord webhook configured for {group}")

    payload = _discord_payload(alert)

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


def _discord_console(alert: Alert) -> None:
    _discord_to(alert, "console")


def _discord_controller(alert: Alert) -> None:
    _discord_to(alert, "controller")


# Backward-compatible name used by older integrations/tests.
_discord = _discord_console


def update_discord_status(alert: Alert, message_id: str | None = None) -> str | None:
    """Create or edit one quiet status message and return its Discord id."""
    webhook = _webhook_for_group(alert.group)
    if not webhook:
        return None
    payload = _discord_payload(alert)

    if message_id:
        base, sep, query = webhook.partition("?")
        url = f"{base.rstrip('/')}/messages/{message_id}"
        if sep:
            url += "?" + query
        resp = requests.patch(url, json=payload, timeout=TIMEOUT)
        if resp.status_code != 404:
            resp.raise_for_status()
            return str(resp.json()["id"])

    resp = requests.post(webhook, params={"wait": "true"}, json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    return str(resp.json()["id"])


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


CHANNELS = {
    "discord": _discord_console,
    "discord_console": _discord_console,
    "discord_controller": _discord_controller,
    "pushover": _pushover,
    "sms": _sms,
}


def configured_channels(group: str | None = None) -> list[str]:
    names = []
    console_webhook = _clean("DISCORD_WEBHOOK_URL")
    controller_webhook = _clean("CONTROLLER_DISCORD_WEBHOOK_URL")
    if group == "controller":
        if controller_webhook:
            names.append("discord_controller")
        elif console_webhook:  # never drop controller alerts during misconfiguration
            names.append("discord_console")
    elif group == "console":
        if console_webhook:
            names.append("discord_console")
    else:  # diagnostics/watchdog should reach every distinct Discord destination
        if console_webhook:
            names.append("discord_console")
        if controller_webhook and controller_webhook != console_webhook:
            names.append("discord_controller")
    if _clean("PUSHOVER_TOKEN") and _clean("PUSHOVER_USER"):
        names.append("pushover")
    if all(_clean(k) for k in ("SMS_TO_NUMBER", "SMS_CARRIER_GATEWAY", "GMAIL_USER", "GMAIL_APP_PASSWORD")):
        names.append("sms")
    return names


def send(alert: Alert, channels: list[str] | None = None) -> dict[str, str]:
    """Fan out to every configured channel. Returns {channel: 'ok'|error}."""
    results: dict[str, str] = {}
    selected = channels if channels is not None else configured_channels(alert.group)
    for name in selected:
        if name not in CHANNELS:
            results[name] = "unknown channel"
            continue
        try:
            CHANNELS[name](alert)
            results[name] = "ok"
        except Exception as exc:  # noqa: BLE001 - never let one channel kill the rest
            results[name] = f"{type(exc).__name__}: {exc}"
    if not results:
        results["(none)"] = "no notification channels configured"
    return results


def _load_outbox() -> list[dict]:
    try:
        with open(config.OUTBOX_FILE, encoding="utf-8") as handle:
            value = json.load(handle)
            return value if isinstance(value, list) else []
    except (OSError, ValueError):
        return []


def _save_outbox(items: list[dict]) -> None:
    tmp = config.OUTBOX_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(items, handle, indent=2)
    os.replace(tmp, config.OUTBOX_FILE)


def _enqueue(alert_id: str, alert: Alert, channels: list[str], metadata: dict,
             delivery_recorded: bool) -> None:
    if not channels:
        return
    with _OUTBOX_LOCK:
        items = _load_outbox()
        existing = next((item for item in items if item.get("id") == alert_id), None)
        if existing:
            existing["channels"] = sorted(set(existing.get("channels", [])) | set(channels))
        else:
            items.append({
                "id": alert_id,
                "alert": _alert_dict(alert),
                "channels": channels,
                "metadata": metadata,
                "attempts": 0,
                "next_retry_ts": time.time() + 5,
                "delivery_recorded": delivery_recorded,
                "created_ts": time.time(),
            })
        _save_outbox(items)


def send_reliable(alert: Alert, alert_id: str, metadata: dict | None = None) -> tuple[dict[str, str], bool]:
    """Send now and durably queue every failed channel for retry."""
    channels = configured_channels(alert.group)
    results = send(alert, channels)
    delivered = any(value == "ok" for value in results.values())
    failed = [name for name in channels if results.get(name) != "ok"]
    _enqueue(alert_id, alert, failed, metadata or {}, delivered)
    return results, delivered


def has_pending(alert_id: str | None) -> bool:
    if not alert_id:
        return False
    with _OUTBOX_LOCK:
        return any(item.get("id") == alert_id for item in _load_outbox())


def cancel_pending(alert_id: str) -> None:
    with _OUTBOX_LOCK:
        items = _load_outbox()
        kept = [item for item in items if item.get("id") != alert_id]
        if len(kept) != len(items):
            _save_outbox(kept)


def retry_outbox() -> list[dict]:
    """Retry due deliveries and return metadata for newly delivered alerts."""
    events: list[dict] = []
    with _OUTBOX_LOCK:
        items = _load_outbox()
        kept: list[dict] = []
        changed = False
        current = time.time()
        for item in items:
            if current < float(item.get("next_retry_ts", 0)):
                kept.append(item)
                continue

            results = send(_alert_from_dict(item["alert"]), list(item.get("channels", [])))
            successful = [name for name, value in results.items() if value == "ok"]
            failed = [name for name in item.get("channels", []) if name not in successful]
            if successful and not item.get("delivery_recorded"):
                event = dict(item.get("metadata") or {})
                event["alert_id"] = item.get("id")
                events.append(event)
                item["delivery_recorded"] = True

            if failed:
                item["channels"] = failed
                item["attempts"] = int(item.get("attempts", 0)) + 1
                delays = (5, 15, 30, 60, 120, 300)
                item["next_retry_ts"] = current + delays[min(item["attempts"], len(delays) - 1)]
                kept.append(item)
            changed = True

        if changed or len(kept) != len(items):
            _save_outbox(kept)
    return events
