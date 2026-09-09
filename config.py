"""
Products, tunables and channel configuration.

Everything you'd realistically want to tweak lives in this file or in
environment variables. Nothing here is secret -- secrets come from env.
"""

from __future__ import annotations

import os


def _load_dotenv() -> None:
    """
    Load KEY=VALUE lines from a .env file next to this script.

    This exists so secrets never have to be typed into a .bat file, where
    Windows batch quoting rules are a genuine trap: quotes you add become
    part of the value, and a paste landing on the wrong side of an existing
    quote silently produces nonsense.

    Values may be quoted or not, either works. Real environment variables
    always win, so GitHub Actions secrets are unaffected by this.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    # utf-8-sig because Notepad writes a byte-order mark that would
    # otherwise end up glued to the first key name.
    with open(path, encoding="utf-8-sig") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'").strip()
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# What we're watching.
#
#   kind "product" -> a real product page; alert when it becomes buyable
#                     (in stock OR pre-order window open).
#   kind "appears" -> no product page exists yet; alert the first time a
#                     distinctive phrase shows up on a listing page.
#
# Optional per-product keys:
#   sku       Best Buy SKU -> enables the official Best Buy API (most reliable)
#   tcin      Target item number -> used for the RedSky API when a key is set
#   item_id   Walmart item id -> used to build an add-to-cart link
#   cart_url  Tap-once "put it in my cart" link included in the alert
# ---------------------------------------------------------------------------
PRODUCTS = [
    {
        "key": "target",
        "name": "Target",
        "url": "https://www.target.com/p/-/A-1013322047",
        "kind": "product",
        "tcin": "1013322047",
        "cart_url": None,  # Target has no public add-to-cart URL
    },
    {
        "key": "walmart",
        "name": "Walmart",
        "url": "https://www.walmart.com/ip/Nintendo-Switch-2-The-Legend-of-Zelda-40th-Anniversary-Edition/21002656445",
        "kind": "product",
        "item_id": "21002656445",
        "cart_url": "https://affil.walmart.com/cart/addToCart?items=21002656445",
    },
    {
        "key": "bestbuy",
        "name": "Best Buy",
        "url": "https://www.bestbuy.com/product/switch-2-the-legend-of-zelda-40th-anniversary-edition/J7GSL57HTY",
        "kind": "product",
        "sku": "6691841",
        "cart_url": "https://api.bestbuy.com/click/-/6691841/cart",
    },
    {
        "key": "gamestop",
        "name": "GameStop",
        "url": "https://www.gamestop.com/consoles-hardware/nintendo-switch-2/products/"
               "nintendo-switch-2-the-legend-of-zelda-40th-anniversary-edition/451607.html",
        "kind": "product",
        # GameStop ships schema.org availability in its raw HTML, so plain
        # HTTP resolves this one cleanly -- no browser rendering needed.
        "cart_url": None,
    },
    {
        "key": "nintendo",
        "name": "Nintendo Store",
        "url": "https://www.nintendo.com/us/store/products/nintendo-switch-2-the-legend-of-zelda-40th-anniversary-edition-121642/",
        "kind": "product",
        "cart_url": None,
    },
    {
        "key": "costco",
        "name": "Costco (watching for the edition to appear)",
        "url": "https://www.costco.com/nintendo-switch-2.html",
        "kind": "appears",
        "phrase": "40th anniversary",
        "cart_url": None,
    },
]


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
# Seconds between full check passes when running with --loop.
CHECK_INTERVAL = _int("CHECK_INTERVAL", 60)
# Random 0..JITTER seconds added to each interval, so requests don't arrive
# on a perfectly predictable cadence (which is itself a bot signal).
JITTER = _int("JITTER", 20)
# Seconds to pause between individual retailers within one pass.
STAGGER = _int("STAGGER", 3)

REQUEST_TIMEOUT = _int("REQUEST_TIMEOUT", 20)
MAX_RETRIES = _int("MAX_RETRIES", 3)

# ---------------------------------------------------------------------------
# Alert policy
# ---------------------------------------------------------------------------
# Keep re-alerting while an item stays buyable -- one missed notification
# shouldn't cost you the console.
REALERT_MINUTES = _int("REALERT_MINUTES", 20)
MAX_REALERTS = _int("MAX_REALERTS", 6)

# If a retailer returns nothing usable (blocked / error / unknown) for this
# long, send a quiet heads-up. This is what stops the bot from being silently
# broken for a week while you assume it's just "not in stock yet".
HEALTH_ALERT_AFTER_MINUTES = _int("HEALTH_ALERT_AFTER_MINUTES", 90)
HEALTH_ALERT_COOLDOWN_MINUTES = _int("HEALTH_ALERT_COOLDOWN_MINUTES", 360)

# Daily "still alive" summary, so silence means healthy rather than
# ambiguous. The health alert above catches hard failures; this catches the
# softer ones -- a closed laptop, a killed terminal, a machine that rebooted
# overnight. Sent at or after this local hour, once per calendar day.
HEARTBEAT_ENABLED = (os.environ.get("HEARTBEAT_ENABLED", "1").strip().lower()
                     not in ("0", "false", "no", ""))
HEARTBEAT_HOUR = _int("HEARTBEAT_HOUR", 8)

# ---------------------------------------------------------------------------
# Social feed watching (@Wario64 etc). Best-effort -- see feeds.py.
# ---------------------------------------------------------------------------
ENABLE_FEED_WATCH = (os.environ.get("ENABLE_FEED_WATCH", "1").strip().lower()
                     not in ("0", "false", "no", ""))
# Feed mirrors rate-limit aggressively, so poll them far less often than
# the retailers themselves.
FEED_INTERVAL_MINUTES = _int("FEED_INTERVAL_MINUTES", 5)

STATE_FILE = os.environ.get(
    "STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_state.json")
)

# ---------------------------------------------------------------------------
# Optional API keys (set as env vars / GitHub secrets). Each one upgrades a
# retailer from "scrape the HTML and hope" to a real structured answer.
# ---------------------------------------------------------------------------
# Render pages in a real headless browser (Playwright) before falling back
# to plain HTTP. Required for Target, which builds its buy box in JS.
# "auto" = use it when Playwright is installed. Set 0 to force it off.
_ub = os.environ.get("USE_BROWSER", "auto").strip().lower()
if _ub in ("0", "false", "no", "off"):
    USE_BROWSER = False
elif _ub in ("1", "true", "yes", "on"):
    USE_BROWSER = True
else:
    try:
        import playwright.sync_api  # noqa: F401

        USE_BROWSER = True
    except ImportError:
        USE_BROWSER = False

BROWSER_TIMEOUT_MS = _int("BROWSER_TIMEOUT_MS", 30000)

BESTBUY_API_KEY = os.environ.get("BESTBUY_API_KEY", "").strip()
TARGET_API_KEY = os.environ.get("TARGET_API_KEY", "").strip()
