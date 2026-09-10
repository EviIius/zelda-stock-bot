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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # optional HTTP TLS-profile fallback
    curl_requests = None

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
    "robot or human",
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
    "attention required!",
    "cloudflare ray id",
    "cf-chl-",
)

_session = requests.Session()

DNS_FAILURE_MARKERS = (
    "could not resolve host",
    "failed to resolve",
    "getaddrinfo failed",
    "name or service not known",
    "nameresolutionerror",
    "temporary failure in name resolution",
)


def _dns_failed(note: str) -> bool:
    lowered = note.lower()
    return any(marker in lowered for marker in DNS_FAILURE_MARKERS)


def _with_query(url: str, **updates: str) -> str:
    """Return *url* with query parameters added or replaced."""
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(updates)
    return urlunsplit((parts.scheme, parts.netloc, parts.path,
                       urlencode(query), parts.fragment))


def fetch(url: str, timeout: int | None = None,
          attempts: int | None = None) -> tuple[str | None, int | None, str]:
    """Return (lowercased_body, http_status, note). body is None on failure."""
    timeout = timeout or config.REQUEST_TIMEOUT
    last_note = "no attempt made"

    # Cloudflare increasingly rejects Python/OpenSSL before page content is
    # considered. curl_cffi presents a current HTTP TLS profile without
    # launching a browser.
    if curl_requests is not None:
        tls_requests = [("chrome", url)]
        if "walmart.com" in url:
            # Walmart challenges HTTP TLS profiles selectively. These
            # remain the same public product page, but alternate a normal
            # navigation query and fingerprint. In live tests one can be
            # challenged while the next returns the full item payload.
            if urlsplit(url).path.startswith("/search"):
                tls_requests.extend([
                    ("chrome_android", url),
                    ("safari", url),
                ])
            else:
                tls_requests.extend([
                    ("chrome_android", _with_query(url, **{"from": "/search"})),
                    ("safari", _with_query(url, selectedSellerId="0")),
                ])

        for impersonation, tls_url in tls_requests:
            try:
                resp = curl_requests.get(
                    tls_url,
                    impersonate=impersonation,
                    timeout=timeout,
                    headers={
                        "Accept-Language": "en-US,en;q=0.9",
                        "Referer": "https://www.walmart.com/search?q=nintendo+switch+2+zelda"
                        if "walmart.com" in url else url,
                    },
                )
                response_body = resp.text.lower()
                if (resp.status_code < 400
                        and not any(marker in response_body for marker in BOT_WALL)):
                    return response_body, resp.status_code, \
                        f"ok (HTTP TLS profile: {impersonation})"
                if resp.status_code < 400:
                    last_note = f"anti-bot interstitial via HTTP TLS profile: {impersonation}"
                else:
                    last_note = f"HTTP {resp.status_code} via HTTP TLS profile: {impersonation}"
            except Exception as exc:  # noqa: BLE001
                last_note = f"HTTP TLS profile {type(exc).__name__}: {exc}"
                # A second HTTP stack and a browser cannot repair a system DNS
                # outage. Return promptly so the worker keeps a useful cadence.
                if _dns_failed(last_note):
                    return None, None, last_note

    # Best Buy stalls plain HTTP clients rather than refusing them, so three
    # 20-second retries burned ~60s -- longer than the whole check interval.
    # When a browser can rescue the check anyway, don't grind through them.
    if attempts is None:
        attempts = min(2, config.MAX_RETRIES) if config.USE_BROWSER else config.MAX_RETRIES
    else:
        attempts = max(0, int(attempts))

    for attempt in range(1, attempts + 1):
        headers = dict(BASE_HEADERS)
        headers["User-Agent"] = random.choice(USER_AGENTS)
        try:
            resp = _session.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_note = f"{type(exc).__name__}: {exc}"
            if _dns_failed(last_note):
                break
        else:
            if resp.status_code in (403, 429, 503):
                last_note = f"HTTP {resp.status_code} (likely bot protection)"
            elif resp.status_code >= 400:
                last_note = f"HTTP {resp.status_code}"
            else:
                return resp.text.lower(), resp.status_code, "ok"

        if attempt < attempts:
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
def _embedded_state(body: str) -> Result | None:
    """
    Read availability out of the embedded app state.

    Walmart is the case that motivated this: its JSON-LD contains only
    WebPage and BreadcrumbList (no Product offers), and it renders no
    "Add to cart" button at all when an item is unavailable -- but its
    __NEXT_DATA__ states the answer outright:

        "availabilityStatus":"OUT_OF_STOCK"   (x8)
        "isPreOrder":true

    Searched across the whole body rather than only inside a __NEXT_DATA__
    tag, because these blobs also arrive via other inline script shapes.
    """
    preorder = '"ispreorder":true' in body or '"is_pre_order":true' in body

    # Explicit availability enum -- the strongest signal available here.
    match = re.search(r'"availability_?status"\s*:\s*"([a-z_]+)"', body)
    if match:
        value = match.group(1)
        if value in ("in_stock", "available"):
            status = Status.PREORDER if preorder else Status.IN_STOCK
            return Result(status, f'app state availabilityStatus="{value.upper()}"'
                                  + (" with isPreOrder=true" if preorder else ""), "app-state")
        if value in ("out_of_stock", "unavailable", "sold_out", "retired"):
            return Result(Status.OUT_OF_STOCK,
                          f'app state availabilityStatus="{value.upper()}"', "app-state")

    # Boolean purchasable flags used by other storefronts.
    if '"ispurchasable":true' in body or '"purchasable":true' in body:
        status = Status.PREORDER if preorder else Status.IN_STOCK
        return Result(status, "app state: purchasable=true", "app-state")
    if '"ispurchasable":false' in body or '"purchasable":false' in body:
        return Result(Status.OUT_OF_STOCK, "app state: purchasable=false", "app-state")
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

    # IMPORTANT ASYMMETRY: this layer is allowed to say "definitely not
    # buyable" or "I don't know", but it is NEVER allowed to say "buyable".
    #
    # Why: Target's product page contains the string "add to cart" exactly
    # once in 334KB of HTML, and the string "out of stock" zero times --
    # because the real state ("Shipping: Not available", a *disabled*
    # Preorder button) is rendered by JavaScript after load. Naive phrase
    # matching therefore reported a sold-out pre-order as IN STOCK.
    #
    # A false negative costs one missed alert, and the health warning tells
    # you the retailer has gone unreadable. A false positive trains you to
    # ignore the notification that actually matters. Positive claims must
    # come from a structured source: an API, JSON-LD, embedded app state,
    # or a real rendered DOM (see browser_detect.py).
    if sold and not buy:
        return Result(Status.OUT_OF_STOCK, f"page text shows {sold}", "phrases")
    if buy and not sold:
        return Result(
            Status.UNKNOWN,
            f"saw {buy} but no structured availability data; refusing to claim "
            f"in-stock from page text alone (install Playwright for a real answer)",
            "phrases",
        )
    if buy and sold:
        return Result(
            Status.UNKNOWN,
            f"ambiguous: buy={buy} and sold={sold} both present (recommendation rails)",
            "phrases",
        )
    return Result(Status.UNKNOWN, "no recognisable buy or sold-out language on page", "phrases")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def _browser_check(product: dict) -> Result | None:
    """Returns whatever the browser concluded, including failures."""
    if not config.USE_BROWSER or product.get("use_browser") is False:
        return None
    try:
        import browser_detect
    except ImportError:
        return None
    return browser_detect.check(product)


def _decisive(result: Result | None) -> bool:
    return result is not None and result.status not in (Status.ERROR, Status.UNKNOWN)


def _retailer_consistency_guard(product: dict, body: str) -> Result | None:
    """Let explicit product-page inventory override known-stale metadata."""
    if "bestbuy.com" in product.get("url", ""):
        # Best Buy can advertise schema.org/InStock before a preorder window
        # opens.  Its embedded product payload is more specific: the actual
        # SKU's fulfillment button remains `coming_soon` until it is buyable.
        # Scope the match to this product's SKU so a recommendation rail for a
        # different item cannot turn the primary product into a false negative.
        sku = str(product.get("sku", "")).strip()
        if sku:
            # Best Buy's lighter server-rendered pages expose the primary
            # product button directly as HTML attributes. This endpoint is a
            # reliable fallback when the full product-detail edge resets its
            # HTTP/2 stream.
            for tag in re.findall(r"<(?:button|a)\b[^>]{0,2500}>", body, re.S):
                sku_attr = re.search(r'data-sku-id\s*=\s*["\']([^"\']+)', tag)
                state_attr = re.search(r'data-button-state\s*=\s*["\']([^"\']+)', tag)
                if sku_attr and state_attr and sku_attr.group(1) == sku:
                    button_state = state_attr.group(1).lower()
                    unavailable_states = {
                        "coming_soon", "sold_out", "out_of_stock", "unavailable",
                        "not_available", "see_details",
                    }
                    preorder_states = {"preorder", "pre_order", "pre-order"}
                    in_stock_states = {"add_to_cart", "addtocart"}
                    if button_state in unavailable_states:
                        status = Status.OUT_OF_STOCK
                    elif button_state in preorder_states:
                        status = Status.PREORDER
                    elif button_state in in_stock_states:
                        status = Status.IN_STOCK
                    else:
                        continue
                    structured = _jsonld(body)
                    return Result(
                        status,
                        f"Best Buy exact-SKU button state={button_state}",
                        "bestbuy-product-button",
                        price=structured.price if structured else None,
                    )

            # Best Buy repeats a product in several serialized GraphQL
            # payloads, and the amount of data between `skuid` and
            # `fulfillmentoptions` varies between responses.  For each
            # fulfillment block, associate it with the nearest preceding
            # skuid instead of depending on a brittle fixed-width regex.
            fulfillment = re.compile(
                r'"fulfillmentoptions"\s*:\s*\{.{0,2500}?'
                r'"buttonstate"\s*:\s*"([a-z_]+)"',
                re.S,
            )
            sku_pattern = re.compile(r'"skuid"\s*:\s*"([^"\\]+)"')
            unavailable_states = {
                "coming_soon", "sold_out", "out_of_stock", "unavailable",
            }
            preorder_states = {"preorder", "pre_order", "pre-order"}
            in_stock_states = {"add_to_cart", "addtocart"}
            for match in fulfillment.finditer(body):
                prefix = body[max(0, match.start() - 8000):match.start()]
                preceding_skus = sku_pattern.findall(prefix)
                if not preceding_skus or preceding_skus[-1] != sku:
                    continue
                button_state = match.group(1)
                if button_state in unavailable_states:
                    status = Status.OUT_OF_STOCK
                elif button_state in preorder_states:
                    status = Status.PREORDER
                elif button_state in in_stock_states:
                    status = Status.IN_STOCK
                else:
                    continue
                structured = _jsonld(body)
                return Result(
                    status,
                    f"Best Buy fulfillment buttonState={button_state}",
                    "bestbuy-product-state",
                    price=structured.price if structured else None,
                )

    if "walmart.com" in product.get("url", ""):
        item_id = re.escape(str(product.get("item_id", "")).strip())
        if item_id:
            # The page contains recommendation inventory too. Anchor the
            # decision to this item's canonical URL or usItemId, then read the
            # nearest preceding product-level availability value.
            anchors = list(re.finditer(
                rf'(?:"canonicalurl"\s*:\s*"[^"\n]*/{item_id}"|'
                rf'"usitemid"\s*:\s*"{item_id}")',
                body,
            ))
            for anchor in anchors:
                scope = body[max(0, anchor.start() - 14000):anchor.end() + 1000]
                states = re.findall(
                    r'"(?:itempage)?availabilitystatus"\s*:\s*"([a-z_]+)"',
                    scope,
                )
                if not states:
                    continue
                availability = states[-1]
                if availability in {"out_of_stock", "not_available", "unavailable"}:
                    status = Status.OUT_OF_STOCK
                elif availability in {"in_stock", "available"}:
                    is_preorder = bool(re.search(
                        r'"preorder"\s*:\s*\{.{0,500}?"ispreorder"\s*:\s*true',
                        scope,
                        re.S,
                    ))
                    status = Status.PREORDER if is_preorder else Status.IN_STOCK
                else:
                    continue
                return Result(
                    status,
                    f"Walmart item {product['item_id']} availabilityStatus={availability}",
                    "walmart-product-state",
                )

    if "gamestop.com" in product.get("url", ""):
        zero_preorders = re.search(r"\b0\s+item\(s\)\s+are available for pre-?order\b", body)
        unavailable_box = 'data-available="false"' in body
        if zero_preorders and unavailable_box:
            structured = _jsonld(body)
            return Result(
                Status.OUT_OF_STOCK,
                "GameStop product inventory explicitly reports 0 items available for pre-order",
                "gamestop-inventory",
                price=structured.price if structured else None,
            )
    return None


def check_product(product: dict) -> Result:
    if product.get("sku"):
        result = _bestbuy_api(product["sku"])
        if result and result.status not in (Status.ERROR, Status.UNKNOWN):
            return result
    if product.get("tcin"):
        result = _target_api(product["tcin"])
        if result and result.status not in (Status.ERROR, Status.UNKNOWN):
            return result

    status_url = product.get("status_url", product["url"])
    fetch_args = (status_url, product.get("request_timeout"))
    if "http_attempts" in product:
        body, http_status, note = fetch(*fetch_args, product["http_attempts"])
    else:
        body, http_status, note = fetch(*fetch_args)

    # Avoid an expensive rendered-browser attempt when the host could not be
    # resolved at all. That condition is a network error, not bot protection.
    if body is None and _dns_failed(note):
        return Result(Status.ERROR, note, "fetch", http_status=http_status)

    # Blocked or unfetchable: a rendered browser sometimes gets through
    # where a bare HTTP request does not, so it's worth one attempt.
    if body is None or any(marker in body for marker in BOT_WALL) or len(body) < 5000:
        rendered = _browser_check(product)
        if _decisive(rendered):
            return rendered

        # Both paths failed. Report both reasons -- knowing HTTP timed out
        # AND the browser was blocked is a different problem from either
        # alone, and the old code threw the browser's half away.
        http_reason = (note if body is None else
                       "anti-bot interstitial served instead of the page"
                       if len(body) >= 5000 else f"tiny response ({len(body)} bytes)")
        if rendered is not None:
            combined = f"http: {http_reason} | browser: {rendered.reason}"
            status = rendered.status if rendered.status is Status.BLOCKED else Status.ERROR
            return Result(status, combined, "fetch+browser", http_status=http_status)
        status = Status.BLOCKED if (body is not None or "bot protection" in note) else Status.ERROR
        return Result(status, http_reason, "fetch", http_status=http_status)

    guarded = _retailer_consistency_guard(product, body)
    if guarded is not None:
        guarded.http_status, guarded.body_len = http_status, len(body)
        return guarded

    # Structured signals in the fetched HTML are cheapest and most reliable
    # -- GameStop, for instance, ships schema.org availability in its raw
    # HTML, so there is no reason to spend seconds rendering it.
    for layer in (_jsonld, _embedded_state):
        result = layer(body)
        if result is not None:
            result.http_status, result.body_len = http_status, len(body)
            # Structured data can be optimistic. Best Buy's Pro Controller
            # page ships "availability":"https://schema.org/InStock" while
            # the only buy control reads "Coming Soon" and is disabled --
            # InStock there means "we will sell this", not "buyable now".
            # So a positive claim gets checked against the rendered DOM;
            # a negative one is trusted as-is.
            if result.status in ALERTABLE and config.USE_BROWSER:
                rendered = _browser_check(product)
                if rendered is not None and rendered.status is Status.OUT_OF_STOCK:
                    rendered.reason = (f"{rendered.reason} (overrides "
                                       f"{result.source} claiming {result.status.value})")
                    return rendered
            # Best Buy's schema.org availability describes the product's
            # catalog lifecycle, not necessarily whether its buy button is
            # open.  Never send an alert unless the exact SKU fulfillment
            # state above, or a rendered buy control, corroborates it.
            if ("bestbuy.com" in product.get("url", "")
                    and result.status in ALERTABLE):
                return Result(
                    Status.UNKNOWN,
                    f"Best Buy {result.source} claims {result.status.value}, "
                    "but the exact SKU fulfillment state was absent; refusing "
                    "an unverified stock alert",
                    "bestbuy-unverified",
                    price=result.price,
                    http_status=http_status,
                    body_len=len(body),
                )
            return result

    # Nothing structured in the HTML (Target). Render it properly.
    rendered = _browser_check(product)
    if _decisive(rendered):
        return rendered

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
