"""
Stock monitor for the Zelda 40th Anniversary Nintendo Switch 2.

Checks Target, Walmart, Best Buy, and the Nintendo Store for in-stock
status, and watches Costco's Switch 2 listing page in case they start
carrying this edition at all. Texts you (via your carrier's free
email-to-SMS gateway) the moment something changes in your favor.
Designed to be run on a schedule by GitHub Actions (see
.github/workflows/stock-monitor.yml), but you can also run it manually
with `python stock_monitor.py`.

State is kept in stock_state.json so you only get a text on the
*transition*, not every single check.
"""

import json
import os
import smtplib
from email.mime.text import MIMEText

import requests

# ---------------------------------------------------------------------------
# Products to watch. Add/remove entries here.
#
#   type "stock"   -> dedicated product page; alerts when it flips from
#                      out-of-stock to in-stock (Target, Walmart, Best Buy,
#                      Nintendo Store).
#   type "appears" -> no dedicated product page exists yet; alerts the
#                      first time a distinctive phrase shows up on a
#                      listing/category page (Costco, until they have an
#                      actual product page for this edition).
# ---------------------------------------------------------------------------
PRODUCTS = {
    "Target - Zelda Switch 2": {
        "type": "stock",
        "url": "https://www.target.com/p/-/A-1013322047",
    },
    "Walmart - Zelda Switch 2": {
        "type": "stock",
        "url": "https://www.walmart.com/ip/Nintendo-Switch-2-The-Legend-of-Zelda-40th-Anniversary-Edition/21002656445",
    },
    "Best Buy - Zelda Switch 2": {
        "type": "stock",
        "url": "https://www.bestbuy.com/product/switch-2-the-legend-of-zelda-40th-anniversary-edition/J7GSL57HTY",
    },
    "Nintendo Store - Zelda Switch 2": {
        "type": "stock",
        "url": "https://www.nintendo.com/us/store/products/nintendo-switch-2-the-legend-of-zelda-40th-anniversary-edition-121642/",
    },
    "Costco - Switch 2 listing (watching for Zelda edition)": {
        "type": "appears",
        "url": "https://www.costco.com/nintendo-switch-2.html",
        "phrase": "40th anniversary",
    },
}

# Phrases that mean "not buyable" if found in the page.
OUT_OF_STOCK_PHRASES = [
    "out of stock",
    "sold out",
    "currently unavailable",
    "coming soon",
    "notify me when available",
]

# Phrases that suggest it IS buyable (used as a positive signal).
IN_STOCK_PHRASES = [
    "add to cart",
    "ship it",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

STATE_FILE = os.path.join(os.path.dirname(__file__), "stock_state.json")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def fetch(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        return resp.text.lower()
    except requests.RequestException as e:
        print(f"  [warn] request failed for {url}: {e}")
        return None


def check_stock(url: str) -> bool | None:
    """Returns True (in stock), False (out of stock), or None (couldn't tell)."""
    text = fetch(url)
    if text is None:
        return None

    for phrase in OUT_OF_STOCK_PHRASES:
        if phrase in text:
            return False

    for phrase in IN_STOCK_PHRASES:
        if phrase in text:
            return True

    # Neither signal found (page structure changed, JS-rendered content,
    # bot-detection page returned instead of the real page, etc.)
    return None


def check_appears(url: str, phrase: str) -> bool | None:
    """Returns True if `phrase` is found on the page, False if not, None on error."""
    text = fetch(url)
    if text is None:
        return None
    return phrase.lower() in text


def send_sms_via_email_gateway(message: str) -> None:
    """
    Free SMS via carrier email-to-text gateway. Requires these env vars:
      SMS_TO_NUMBER        e.g. "5551234567" (10 digits, no dashes)
      SMS_CARRIER_GATEWAY  e.g. "vtext.com" (see README for your carrier)
      GMAIL_USER           the Gmail address sending the alert
      GMAIL_APP_PASSWORD   a Gmail App Password (not your normal password)
    """
    to_number = os.environ["SMS_TO_NUMBER"]
    carrier_gateway = os.environ["SMS_CARRIER_GATEWAY"]
    gmail_user = os.environ["GMAIL_USER"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]

    to_addr = f"{to_number}@{carrier_gateway}"

    msg = MIMEText(message)
    msg["From"] = gmail_user
    msg["To"] = to_addr
    msg["Subject"] = ""

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_app_password)
        server.sendmail(gmail_user, [to_addr], msg.as_string())


def main() -> None:
    state = load_state()
    changed = False

    for name, cfg in PRODUCTS.items():
        url = cfg["url"]

        if cfg["type"] == "stock":
            result = check_stock(url)
            label = "IN STOCK" if result else "out of stock" if result is False else "unknown"
        else:  # "appears"
            result = check_appears(url, cfg["phrase"])
            label = "PHRASE FOUND" if result else "not found yet" if result is False else "unknown"

        print(f"{name}: {label}")

        was_true = state.get(name, False)
        if result is True and not was_true:
            message = f"UPDATE: {name}\n{url}"
            print(f"  -> sending alert: {message}")
            try:
                send_sms_via_email_gateway(message)
            except Exception as e:
                print(f"  [error] failed to send SMS: {e}")

        # Only update state on a clear reading, so a temporary "unknown"
        # (site hiccup, bot-detection page, etc.) doesn't erase what we
        # last knew.
        if result is not None:
            state[name] = result
            changed = True

    if changed:
        save_state(state)


if __name__ == "__main__":
    main()
