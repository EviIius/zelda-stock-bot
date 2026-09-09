"""
Availability detection.

The old version searched the raw HTML for "out of stock" and called it a
day. That doesn't work: a Target or Best Buy product page contains those
words in recommendation carousels, Q&A answers and "similar items" rails
even when the item itself is perfectly buyable -- so the check returned
"out of stock" essentially always, and would never have fired.

This version tries progressively weaker signals and stops at the first one
that gives a trustworthy answer:

  1. Official retailer API      (Best Buy; Target if you have a key)
  2. schema.org JSON-LD offers  (structured, unambiguous, widely present)
  3. Embedded app state JSON    (__NEXT_DATA__ and friends)
  4. Scoped phrase matching     (last resort, and it refuses to guess when
                                 the page contains both buy and sold-out
                                 language)

It also distinguishes "the retailer blocked us" from "it's out of stock",
which the old version could not do -- that difference is the whole
difference between a bot that works and a bot that is quietly dead.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from enum import Enum

import requests

import config


class Status(str, Enum):
    IN_STOCK = "in_stock"
    PREORDER = "preorder"
    OUT_OF_STOCK = "out_of_stock"
    BLOCKED = "blocked"
    ERROR = "error"
    UNKNOWN = "unknown"


#: States that should wake you up. Pre-order counts -- for this console the
#: pre-order window reopening *is* the event, not a consolation prize.
ALERTABLE = {Status.IN_STOCK, Status.PREORDER}
#: States that mean "we learned nothing", tracked for health alerting.
UNUSABLE = {Status.BLOCKED, Status.ERROR, Status.UNKNOWN}


@dataclass
class Result:
    status: Status
    reason: str
    source: str
    price: str | None = None
    http_status: int | None = None
    body_len: int = 0

    @property
    def alertable(self) -> bool:
        return self.status in ALERTABLE


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
]

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "no-cache",
}

# Fingerprints of interstitial / anti-bot pages. If we see one of these we
# report BLOCKED rather than pretending we learned something.
BOT_WALL = (
    "pardon our interruption",
    "are you a human",
    "verify you are a human",
    "verifying you are human",
    "unusual traffic",
    "px-captcha",
    "perimeterx",
    "captcha-delivery",
    "request unsuccessful",
    "incapsula",
    "checking your browser",
    "enable javascript and cookies to continue",
    "access to this page has been denied",
    "activity on this site has been disabled",
)

_session = requests.Session()


def fetch(url: str, timeout: int | None = None) -> tuple[str | None, int | None, str]:
    """Return (lowercased_body, http_status, note). body is None on failure."""
    timeout = timeout or config.REQUEST_TIMEOUT
    last_note = "no attempt made"

    for attempt in range(1, config.MAX_RETRIES + 1):
        headers = dict(BASE_HEADERS)
        headers["User-Agent"] = random.choice(USER_AGENTS)
        try:
            resp = _session.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_note = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code in (403, 429, 503):
                last_note = f"HTTP {resp.status_code} (likely bot protection)"
            elif resp.status_code >= 400:
                last_note = f"HTTP {resp.status_code}"
            else:
                return resp.text.lower(), resp.status_code, "ok"

        if attempt < config.MAX_RETRIES:
            time.sleep(min(2**attempt, 8) + random.uniform(0, 1.5))

    return None, None, last_note


# ---------------------------------------------------------------------------
# Layer 1: official APIs
# ---------------------------------------------------------------------------
def _bestbuy_api(sku: str) -> Result | None:
    if not config.BESTBUY_API_KEY:
        return None
    url = f"https://api.bestbuy.com/v1/products/{sku}.json"
    params = {
        "apiKey": config.BESTBUY_API_KEY,
        "show": "sku,name,salePrice,onlineAvailability,orderable,releaseDate",
    }
    try:
        resp = _session.get(url, params=params, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return Result(Status.ERROR, f"Best Buy API: {type(exc).__name__}: {exc}", "bestbuy-api")

    orderable = str(data.get("orderable", "")).lower()
    price = data.get("salePrice")
    price_str = f"${price}" if price else None

    if orderable in ("available", "preorder"):
        status = Status.PREORDER if orderable == "preorder" else Status.IN_STOCK
        return Result(status, f"Best Buy API orderable={data.get('orderable')}", "bestbuy-api", price_str)
    if orderable in ("soldout", "comingsoon", "backorder"):
        return Result(
            Status.OUT_OF_STOCK, f"Best Buy API orderable={data.get('orderable')}", "bestbuy-api", price_str
        )
    return Result(Status.UNKNOWN, f"Best Buy API orderable={data.get('orderable')!r}", "bestbuy-api", price_str)


def _target_api(tcin: str) -> Result | None:
    if not config.TARGET_API_KEY:
        return None
    url = "https://redsky.target.com/redsky_aggregations/v1/web/pdp_fulfillment_v1"
    params = {
        "key": config.TARGET_API_KEY,
        "tcin": tcin,
        "is_bot": "false",
        "channel": "WEB",
        "pricing_store_id": "3991",
    }
    try:
        resp = _session.get(url, params=params, timeout=config.REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return Result(Status.ERROR, f"Target API: {type(exc).__name__}: {exc}", "target-api")

    blob = json.dumps(data).lower()
    if '"availability_status":"in_stock"' in blob or '"available_to_promise_quantity"' in blob and '"in_stock"' in blob:
        return Result(Status.IN_STOCK, "Target RedSky reports in_stock", "target-api")
    if "preorder" in blob and "unavailable" not in blob:
        return Result(Status.PREORDER, "Target RedSky reports preorder", "target-api")
    if "out_of_stock" in blob or "unavailable" in blob:
        return Result(Status.OUT_OF_STOCK, "Target RedSky reports out_of_stock", "target-api")
    return Result(Status.UNKNOWN, "Target RedSky response not recognised", "target-api")


# ---------------------------------------------------------------------------
# Layer 2: schema.org JSON-LD
# ---------------------------------------------------------------------------
_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', re.S | re.I
)

_AVAILABILITY_MAP = {
    "instock": Status.IN_STOCK,
    "onlineonly": Status.IN_STOCK,
    "limitedavailability": Status.IN_STOCK,
    "preorder": Status.PREORDER,
    "presale": Status.PREORDER,
    "outofstock": Status.OUT_OF_STOCK,
    "soldout": Status.OUT_OF_STOCK,
    "backorder": Status.OUT_OF_STOCK,
    "discontinued": Status.OUT_OF_STOCK,
    "instoreonly": Status.OUT_OF_STOCK,
}


def _collect_offers(node, out: list[dict]) -> None:
    if isinstance(node, dict):
        if isinstance(node.get("availability"), str):
            out.append(node)
        for value in node.values():
            _collect_offers(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect_offers(value, out)


def _jsonld(body: str) -> Result | None:
    offers: list[dict] = []
    for raw in _LD_RE.findall(body):
        try:
            _collect_offers(json.loads(raw.strip()), offers)
        except (ValueError, RecursionError):
            continue
    if not offers:
        return None

    statuses, price = [], None
    for offer in offers:
        token = str(offer["availability"]).rsplit("/", 1)[-1].replace("_", "").lower()
        mapped = _AVAILABILITY_MAP.get(token)
        if mapped:
            statuses.append(mapped)
        if price is None and offer.get("price"):
            price = f"${offer['price']}"

    if not statuses:
        return None
    # Any buyable offer wins -- one sellable variant is enough.
    for good in (Status.IN_STOCK, Status.PREORDER):
        if good in statuses:
            return Result(good, f"JSON-LD offer availability={good.value}", "json-ld", price)
    return Result(Status.OUT_OF_STOCK, "JSON-LD offers all unavailable", "json-ld", price)


# ---------------------------------------------------------------------------
# Layer 3: embedded application state
# ---------------------------------------------------------------------------
_NEXT_DATA_RE = re.compile(r'<script id="__next_data__"[^>]*>(.*?)</script>', re.S | re.I)


def _embedded_state(body: str) -> Result | None:
    match = _NEXT_DATA_RE.search(body)
    if not match:
        return None
    blob = match.group(1)
    # Look for the unambiguous flags these storefronts expose.
    if '"ispurchasable":true' in blob or '"purchasable":true' in blob:
        return Result(Status.IN_STOCK, "embedded state: purchasable=true", "app-state")
    if '"ispreorder":true' in blob or '"preorder":true' in blob:
        return Result(Status.PREORDER, "embedded state: preorder=true", "app-state")
    if '"ispurchasable":false' in blob or '"purchasable":false' in blob:
        return Result(Status.OUT_OF_STOCK, "embedded state: purchasable=false", "app-state")
    return None


# ---------------------------------------------------------------------------
# Layer 4: scoped phrase matching (deliberately conservative)
# ---------------------------------------------------------------------------
_STRIP_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")

BUY_PHRASES = ("add to cart", "add to bag", "pre-order", "preorder", "buy now", "ship it")
SOLD_PHRASES = (
    "out of stock",
    "sold out",
    "currently unavailable",
    "temporarily out of stock",
    "notify me when available",
    "coming soon",
)


def _phrases(body: str) -> Result:
    text = _TAG_RE.sub(" ", _STRIP_RE.sub(" ", body))
    text = re.sub(r"\s+", " ", text)

    buy = [p for p in BUY_PHRASES if p in text]
    sold = [p for p in SOLD_PHRASES if p in text]

    if buy and not sold:
        status = Status.PREORDER if any("order" in p for p in buy) else Status.IN_STOCK
        return Result(status, f"page text shows {buy} and no sold-out language", "phrases")
    if sold and not buy:
        return Result(Status.OUT_OF_STOCK, f"page text shows {sold}", "phrases")
    if buy and sold:
        # Both present -- almost always because of recommendation rails.
        # Refusing to guess here is the single most important fix in this
        # file; guessing is what made the old script useless.
        return Result(
            Status.UNKNOWN,
            f"ambiguous: buy={buy} and sold={sold} both present; need an API key for this retailer",
            "phrases",
        )
    return Result(Status.UNKNOWN, "no recognisable buy or sold-out language on page", "phrases")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def check_product(product: dict) -> Result:
    if product.get("sku"):
        result = _bestbuy_api(product["sku"])
        if result and result.status not in (Status.ERROR, Status.UNKNOWN):
            return result
    if product.get("tcin"):
        result = _target_api(product["tcin"])
        if result and result.status not in (Status.ERROR, Status.UNKNOWN):
            return result

    body, http_status, note = fetch(product["url"])
    if body is None:
        return Result(Status.BLOCKED if "bot protection" in note else Status.ERROR, note, "fetch")

    if any(marker in body for marker in BOT_WALL):
        return Result(Status.BLOCKED, "anti-bot interstitial served instead of the page", "fetch",
                      http_status=http_status, body_len=len(body))
    if len(body) < 5000:
        return Result(Status.BLOCKED, f"suspiciously small response ({len(body)} bytes)", "fetch",
                      http_status=http_status, body_len=len(body))

    for layer in (_jsonld, _embedded_state):
        result = layer(body)
        if result is not None:
            result.http_status, result.body_len = http_status, len(body)
            return result

    result = _phrases(body)
    result.http_status, result.body_len = http_status, len(body)
    return result


def check_appears(product: dict) -> Result:
    body, http_status, note = fetch(product["url"])
    if body is None:
        return Result(Status.BLOCKED if "bot protection" in note else Status.ERROR, note, "fetch")
    if any(marker in body for marker in BOT_WALL):
        return Result(Status.BLOCKED, "anti-bot interstitial", "fetch", http_status=http_status)

    phrase = product["phrase"].lower()
    if phrase in body:
        return Result(Status.IN_STOCK, f"phrase {phrase!r} appeared on the listing page", "phrases",
                      http_status=http_status, body_len=len(body))
    return Result(Status.OUT_OF_STOCK, f"phrase {phrase!r} not on the page yet", "phrases",
                  http_status=http_status, body_len=len(body))


def check(product: dict) -> Result:
    return check_appears(product) if product.get("kind") == "appears" else check_product(product)
