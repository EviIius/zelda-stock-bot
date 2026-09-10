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


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


# On macOS, the loop starts a matching `caffeinate` assertion so an unattended
# desktop Mac does not enter idle sleep. Closing a MacBook lid still sleeps it.
PREVENT_MACOS_SLEEP = _bool("PREVENT_MACOS_SLEEP", True)


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
#   group     Routes alerts/status to the console or controller Discord webhook
#   product_name  Human-readable item name shown prominently in notifications
# ---------------------------------------------------------------------------
PRODUCTS = [
    {
        "key": "target",
        "name": "Target",
        "product_name": "Nintendo Switch 2 — Zelda 40th Anniversary Console",
        "group": "console",
        "url": "https://www.target.com/p/-/A-1013322047",
        "kind": "product",
        "tcin": "1013322047",
        "cart_url": None,  # Target has no public add-to-cart URL
        "enabled": _bool("ENABLE_TARGET", True),
        "interval": _int("TARGET_INTERVAL", 25),
    },
    {
        "key": "walmart",
        "name": "Walmart",
        "product_name": "Nintendo Switch 2 — Zelda 40th Anniversary Console",
        "group": "console",
        "url": "https://www.walmart.com/ip/Nintendo-Switch-2-The-Legend-of-Zelda-40th-Anniversary-Edition/21002656445",
        # The exact-ID search response carries the same item-scoped inventory
        # state and is less aggressively challenged than the product page.
        "status_url": "https://www.walmart.com/search?q=21002656445",
        "kind": "product",
        "item_id": "21002656445",
        "cart_url": "https://affil.walmart.com/cart/addToCart?items=21002656445",
        # Keep Walmart HTTP-only. If it challenges these exact-item requests,
        # fail closed instead of opening a visible browser or guessing from
        # recommendation inventory.
        "request_timeout": 12,
        "http_attempts": 0,
        "use_browser": False,
        "enabled": _bool("ENABLE_WALMART", True),
        "interval": _int("WALMART_INTERVAL", 60),
    },
    {
        "key": "bestbuy",
        "name": "Best Buy",
        "product_name": "Nintendo Switch 2 — Zelda 40th Anniversary Console",
        "group": "console",
        "url": "https://www.bestbuy.com/product/switch-2-the-legend-of-zelda-40th-anniversary-edition/J7GSL57HTY",
        # Best Buy's full PDP intermittently resets HTTP/2. Its official Q&A
        # page server-renders the same exact-SKU fulfillment button.
        "status_url": "https://www.bestbuy.com/site/questions/switch-2-the-legend-of-"
                      "zelda-40th-anniversary-edition/6691841",
        "kind": "product",
        "sku": "6691841",
        "cart_url": "https://api.bestbuy.com/click/-/6691841/cart",
        "request_timeout": 8,
        "http_attempts": 0,
        "use_browser": False,
        "enabled": _bool("ENABLE_BESTBUY", True),
        "interval": _int("BESTBUY_INTERVAL", 60),
    },
    {
        "key": "gamestop",
        "name": "GameStop",
        "product_name": "Nintendo Switch 2 — Zelda 40th Anniversary Console",
        "group": "console",
        "url": "https://www.gamestop.com/consoles-hardware/nintendo-switch-2/products/"
               "nintendo-switch-2-the-legend-of-zelda-40th-anniversary-edition/451607.html",
        "kind": "product",
        # GameStop ships schema.org availability in its raw HTML, so plain
        # HTTP resolves this one cleanly -- no browser rendering needed.
        "cart_url": None,
        "enabled": _bool("ENABLE_GAMESTOP", True),
        "interval": _int("GAMESTOP_INTERVAL", 25),
    },
    {
        "key": "nintendo",
        "name": "Nintendo Store",
        "product_name": "Nintendo Switch 2 — Zelda 40th Anniversary Console",
        "group": "console",
        "url": "https://www.nintendo.com/us/store/products/nintendo-switch-2-the-legend-of-zelda-40th-anniversary-edition-121642/",
        "kind": "product",
        "cart_url": None,
        "enabled": _bool("ENABLE_NINTENDO", True),
        "interval": _int("NINTENDO_INTERVAL", 25),
    },
    {
        "key": "costco",
        "name": "Costco (watching for the edition to appear)",
        "product_name": "Nintendo Switch 2 — Zelda 40th Anniversary Console",
        "group": "console",
        "url": "https://www.costco.com/nintendo-switch-2.html",
        "kind": "appears",
        "phrase": "40th anniversary",
        "cart_url": None,
        "enabled": _bool("ENABLE_COSTCO", False),
        "interval": _int("COSTCO_INTERVAL", 600),
    },
    {
        "key": "controller_target",
        "name": "Target",
        "product_name": "Zelda 40th Anniversary Switch 2 Pro Controller",
        "group": "controller",
        "url": "https://www.target.com/p/-/A-1013213521",
        "kind": "product",
        "tcin": "1013213521",
        "cart_url": None,
        "enabled": _bool("ENABLE_CONTROLLER_TARGET", True),
        "interval": _int("CONTROLLER_TARGET_INTERVAL", 25),
    },
    {
        "key": "controller_walmart",
        "name": "Walmart",
        "product_name": "Zelda 40th Anniversary Switch 2 Pro Controller",
        "group": "controller",
        "url": "https://www.walmart.com/ip/20954470204",
        "status_url": "https://www.walmart.com/search?q=20954470204",
        "kind": "product",
        "item_id": "20954470204",
        "cart_url": "https://affil.walmart.com/cart/addToCart?items=20954470204",
        "request_timeout": 12,
        "http_attempts": 0,
        "use_browser": False,
        "enabled": _bool("ENABLE_CONTROLLER_WALMART", True),
        "interval": _int("CONTROLLER_WALMART_INTERVAL", 60),
    },
    {
        "key": "controller_gamestop",
        "name": "GameStop",
        "product_name": "Zelda 40th Anniversary Switch 2 Pro Controller",
        "group": "controller",
        "url": "https://www.gamestop.com/gaming-accessories/controllers/nintendo-switch-2/"
               "products/nintendo-switch-2-pro-controller-the-legend-of-zelda---"
               "40th-anniversary-edition/451609.html",
        "kind": "product",
        "cart_url": None,
        "enabled": _bool("ENABLE_CONTROLLER_GAMESTOP", True),
        "interval": _int("CONTROLLER_GAMESTOP_INTERVAL", 25),
    },
    {
        "key": "controller_nintendo",
        "name": "Nintendo Store",
        "product_name": "Zelda 40th Anniversary Switch 2 Pro Controller",
        "group": "controller",
        "url": "https://www.nintendo.com/us/store/products/nintendo-switch-2-pro-controller-"
               "the-legend-of-zelda-40th-anniversary-edition-127074/",
        "kind": "product",
        "cart_url": None,
        "enabled": _bool("ENABLE_CONTROLLER_NINTENDO", True),
        "interval": _int("CONTROLLER_NINTENDO_INTERVAL", 25),
    },
    {
        "key": "controller_bestbuy",
        "name": "Best Buy",
        "product_name": "Zelda 40th Anniversary Switch 2 Pro Controller",
        "group": "controller",
        "url": "https://www.bestbuy.com/product/nintendo-switch-2-pro-controller-the-legend-"
               "of-zelda-40th-anniversary-edition-multi/J7GSL57W27",
        "status_url": "https://www.bestbuy.com/site/questions/nintendo-switch-2-pro-controller-"
                      "the-legend-of-zelda-40th-anniversary-edition-multi/6691849",
        "kind": "product",
        "sku": "6691849",
        "cart_url": "https://api.bestbuy.com/click/-/6691849/cart",
        "request_timeout": 8,
        # Best Buy stalls some plain HTTP clients on this connection. Keep the
        # fast HTTP TLS-profile attempt, then report degraded without launching
        # Chromium.
        "http_attempts": 0,
        "use_browser": False,
        "enabled": _bool("ENABLE_CONTROLLER_BESTBUY", True),
        "interval": _int("CONTROLLER_BESTBUY_INTERVAL", 60),
    },
]

# User-added links are stored separately so upgrading the built-in Zelda
# monitors can never overwrite the personal catalog.
from product_catalog import load_custom_products  # noqa: E402

_BUILTIN_PRODUCT_KEYS = {product["key"] for product in PRODUCTS}
CUSTOM_PRODUCTS, CUSTOM_PRODUCT_ERRORS = load_custom_products()
PRODUCTS.extend(product for product in CUSTOM_PRODUCTS
                if product["key"] not in _BUILTIN_PRODUCT_KEYS)


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

# Status and supervision. A single Discord status message is edited in place
# so silence can never be mistaken for a stopped monitor.
STATUS_UPDATE_SECONDS = _int("STATUS_UPDATE_SECONDS", 60)
RUNTIME_HEARTBEAT_SECONDS = _int("RUNTIME_HEARTBEAT_SECONDS", 15)
WATCHDOG_STALE_SECONDS = _int("WATCHDOG_STALE_SECONDS", 120)

REQUEST_TIMEOUT = _int("REQUEST_TIMEOUT", 20)
MAX_RETRIES = _int("MAX_RETRIES", 3)

# ---------------------------------------------------------------------------
# Alert policy
# ---------------------------------------------------------------------------
# Keep re-alerting while an item stays buyable -- one missed notification
# shouldn't cost you the console.
# Re-check once before firing an alert. Every false positive this bot has
# produced came from a single weak reading; a second look a few seconds
# later costs nothing and catches transient hydration states, a page that
# rendered mid-update, and one-off network oddities.
CONFIRM_BEFORE_ALERT = (os.environ.get("CONFIRM_BEFORE_ALERT", "1").strip().lower()
                        not in ("0", "false", "no", ""))
CONFIRM_DELAY_SECONDS = _int("CONFIRM_DELAY_SECONDS", 6)

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
# Only send within this many hours of HEARTBEAT_HOUR. Without a window,
# "hour >= 8" is true all evening, so restarting at 11pm fired it instantly.
HEARTBEAT_WINDOW_HOURS = _int("HEARTBEAT_WINDOW_HOURS", 3)

# Append every check to a CSV so questions like "was it ever briefly
# available overnight?" are answerable after the fact.
LOG_CSV = os.environ.get("LOG_CSV", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "checks.csv"))
LOG_ENABLED = (os.environ.get("LOG_ENABLED", "1").strip().lower()
               not in ("0", "false", "no", ""))

# Attach a screenshot of the buy box to stock alerts.
ALERT_SCREENSHOTS = (os.environ.get("ALERT_SCREENSHOTS", "1").strip().lower()
                     not in ("0", "false", "no", ""))

# ---------------------------------------------------------------------------
# Social feed watching (@Wario64 etc). Best-effort -- see feeds.py.
# ---------------------------------------------------------------------------
ENABLE_FEED_WATCH = (os.environ.get("ENABLE_FEED_WATCH", "0").strip().lower()
                     not in ("0", "false", "no", ""))
# Feed mirrors rate-limit aggressively, so poll them far less often than
# the retailers themselves.
FEED_INTERVAL_MINUTES = _int("FEED_INTERVAL_MINUTES", 5)

STATE_FILE = os.environ.get(
    "STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_state.json")
)
OUTBOX_FILE = os.environ.get(
    "OUTBOX_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "notification_outbox.json")
)
RUNTIME_HEARTBEAT_FILE = os.environ.get(
    "RUNTIME_HEARTBEAT_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime_heartbeat.json"),
)
WATCHDOG_STATE_FILE = os.environ.get(
    "WATCHDOG_STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchdog_state.json"),
)
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL", "").strip()

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

BROWSER_TIMEOUT_MS = _int("BROWSER_TIMEOUT_MS", 12000)

BESTBUY_API_KEY = os.environ.get("BESTBUY_API_KEY", "").strip()
TARGET_API_KEY = os.environ.get("TARGET_API_KEY", "").strip()
