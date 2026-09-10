"""
Availability detection via a real rendered browser (Playwright).

Necessary because the retailers that matter most build their buy box in
JavaScript. Measured on Target's page for this console:

    raw HTML:   "add to cart" x1,  "out of stock" x0,  0 JSON-LD blocks
    rendered:   button data-test="Preorder.Disabled", disabled=true
                "Pickup Not available / Shipping Not available / Coming October 29"

So the fetched HTML contains no availability information at all, while the
rendered DOM states it unambiguously.

Two hard-won rules live in here:

  * Site navigation is excluded. GameStop's nav has ENABLED
    <a class="dropdown-link">Pre-Order</a> entries while the real control is
    a DISABLED "Pre-Order for Pick Up" button. Matching nav invents restocks.

  * The fulfilment panel outranks the buttons. A fresh headless browser has
    no saved zip code, so Target can render a superficially enabled Preorder
    control while every fulfilment method still reads "Not available".
    Believing the button there produced a false alert.

Optional. If Playwright isn't installed, check() returns None and the
caller falls back to the HTTP layers.

Install:
    python -m pip install -r requirements-browser.txt
    python -m playwright install chromium
"""

from __future__ import annotations

import re
import threading

import config
import detect
from detect import Result, Status

BUY_TEXT = re.compile(r"add to cart|add for shipping|pre-?order|buy now|ship it|add to bag", re.I)
SOLD_TEXT = re.compile(r"sold out|out of stock|not available|unavailable|notify me|coming soon", re.I)
#: A control that has REPLACED the buy button. Not a buy control itself, but
#: its presence is proof you cannot buy the item -- Best Buy renders a
#: disabled "Coming Soon" in place of "Add to Cart" while its JSON-LD still
#: advertises schema.org/InStock.
BLOCKED_CONTROL = re.compile(r"coming soon|sold out|out of stock|currently unavailable|"
                             r"notify me|join waitlist|email me", re.I)

#: Fulfilment methods named in the panel.
METHOD_WORDS = ("pickup", "delivery", "shipping")
#: Wording meaning that method cannot be used right now.
METHOD_DEAD = re.compile(r"not available|unavailable|sold out|out of stock", re.I)
#: Wording that is neither yes nor no -- Target shows "Check availability"
#: for delivery on items you cannot actually buy, so it must not count as
#: evidence of availability.
METHOD_NEUTRAL = re.compile(r"check availability|see options|choose|select|enter (a )?zip", re.I)

# Raw string: this is JavaScript, and Python must not touch its backslashes.
PROBE = r"""() => {
  // Navigation is full of links that read like buy controls, so exclude it.
  const inChrome = el => !!el.closest(
    'nav, header, footer, [role="navigation"], [role="menu"], .dropdown-menu, .nav, .menu'
  );
  const isDisabled = b =>
       b.disabled === true
    || b.getAttribute('aria-disabled') === 'true'
    // Target encodes state directly in the attribute: "Preorder.Disabled".
    || (b.getAttribute('data-test') || '').toLowerCase().includes('disabled')
    || String(b.className || '').toLowerCase().includes('disabled');

  const btns = [...document.querySelectorAll('button, input[type="submit"], [role="button"]')]
    .filter(b => !inChrome(b))
    .map(b => ({
      dt: b.getAttribute('data-test') || b.getAttribute('data-automation-id') || '',
      text: (b.innerText || b.value || '').replace(/\s+/g, ' ').trim().slice(0, 60),
      disabled: isDisabled(b),
    }))
    .filter(b => b.text || b.dt);

  const fulfil = [...document.querySelectorAll(
      '[data-test*="fulfillment"], [data-test*="fulfilment"], [data-test*="add-to-cart"], [data-test*="AddToCart"]'
    )]
    .filter(e => !inChrome(e))
    .map(e => e.innerText.replace(/\s+/g, ' ').trim().slice(0, 240));

  return { btns, fulfil, title: document.title };
}"""


def _fulfilment_verdict(text: str) -> tuple[str, list[str]]:
    """
    Read the fulfilment panel. Returns ("dead" | "live" | "unclear", details).

    Each method is judged on its own segment of the text, because
    "Pickup Not available" next to a working Shipping option still means you
    can buy it -- reading the blob as a whole turned that into a miss.

    Segments are bounded by the next method keyword rather than a fixed
    character count, which is what previously let "Pickup" swallow the
    "Shipping" that followed it.
    """
    low = text.lower()
    marks = sorted(
        (m.start(), m.end(), word)
        for word in METHOD_WORDS
        for m in re.finditer(word, low)
    )
    if not marks:
        return ("unclear", [])

    details, states = [], []
    for index, (_, end, word) in enumerate(marks):
        stop = marks[index + 1][0] if index + 1 < len(marks) else len(low)
        segment = low[end:stop].strip()[:40]
        if METHOD_DEAD.search(segment):
            state = "dead"
        elif METHOD_NEUTRAL.search(segment) or not segment:
            state = "neutral"
        else:
            state = "live"
        details.append(f"{word}:{segment or '-'}={state}")
        states.append(state)

    if "live" in states:
        return ("live", details)
    if "dead" in states:
        return ("dead", details)
    return ("unclear", details)


def _decide(probe: dict) -> Result:
    buttons = probe.get("btns", [])
    fulfil_text = " ".join(probe.get("fulfil", []))
    verdict, details = _fulfilment_verdict(fulfil_text)

    candidates = [b for b in buttons if BUY_TEXT.search(b["text"]) or BUY_TEXT.search(b["dt"])]
    live_buttons = [b for b in candidates if not b["disabled"]]

    # The fulfilment panel wins. A headless browser with no saved zip code
    # can render an enabled-looking control while nothing is actually
    # purchasable, which is exactly how the Target false positive happened.
    if verdict == "dead":
        return Result(Status.OUT_OF_STOCK,
                      f"all fulfilment methods unavailable ({details})", "browser")

    if live_buttons:
        label = live_buttons[0]["text"] or live_buttons[0]["dt"]
        status = Status.PREORDER if re.search(r"pre-?order", label, re.I) else Status.IN_STOCK
        corroboration = f"; fulfilment: {details}" if details else ""
        return Result(status, f"enabled buy control {label!r}{corroboration}", "browser")

    if candidates:
        labels = [b["text"] or b["dt"] for b in candidates][:3]
        return Result(Status.OUT_OF_STOCK, f"buy control(s) disabled: {labels}", "browser")

    # No buy control, but a control that stands in its place.
    blocked = [b for b in buttons if BLOCKED_CONTROL.search(b["text"])]
    if blocked:
        labels = [b["text"] for b in blocked][:3]
        return Result(Status.OUT_OF_STOCK,
                      f"buy button replaced by {labels}", "browser")

    if verdict == "live":
        return Result(Status.UNKNOWN,
                      f"fulfilment looks available but no buy control found ({details})", "browser")

    return Result(Status.UNKNOWN, "rendered page exposed no recognisable buy control", "browser")


STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_TLS = threading.local()


def _launch(pw):
    """
    Prefer the real Chrome install over Playwright's bundled Chromium.

    Walmart's bot detection fingerprints the bundled build and serves an
    interstitial to it. Real Chrome, driven the same way, usually passes.
    Falls back to Chromium when Chrome isn't installed.
    """
    # --disable-http2: Best Buy's edge terminates Chromium's HTTP/2 handshake
    # with ERR_HTTP2_PROTOCOL_ERROR. Forcing HTTP/1.1 gets the page. It is
    # almost certainly the same fault that makes plain `requests` hang until
    # it times out rather than returning an error.
    args = ["--disable-blink-features=AutomationControlled", "--no-sandbox",
            "--disable-dev-shm-usage", "--disable-http2"]
    try:
        return pw.chromium.launch(headless=True, channel="chrome", args=args), "chrome"
    except Exception:  # noqa: BLE001
        return pw.chromium.launch(headless=True, args=args), "chromium"


def _context(browser):
    context = browser.new_context(
        user_agent=UA,
        viewport={"width": 1440, "height": 900},
        locale="en-US",
        timezone_id="America/New_York",
        extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
    )
    context.add_init_script(STEALTH)
    return context


def _session():
    """Return a browser page owned by the current worker thread."""
    cached = getattr(_TLS, "browser_session", None)
    if cached is not None:
        return cached

    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser, flavour = _launch(playwright)
    context = _context(browser)
    cached = {
        "playwright": playwright,
        "browser": browser,
        "context": context,
        "page": context.new_page(),
        "flavour": flavour,
    }
    _TLS.browser_session = cached
    return cached


def close_thread_session() -> None:
    """Close the persistent browser belonging to the current thread."""
    cached = getattr(_TLS, "browser_session", None)
    if cached is None:
        return
    for key in ("context", "browser", "playwright"):
        try:
            cached[key].close() if key != "playwright" else cached[key].stop()
        except Exception:  # noqa: BLE001
            pass
    _TLS.browser_session = None


def _settle(page):
    from playwright.sync_api import TimeoutError as PWTimeout

    for selector in ('[data-test*="fulfillment"]', '[data-test*="add-to-cart"]',
                     'button:has-text("Add to cart")', 'button:has-text("Pre-order")'):
        try:
            page.wait_for_selector(selector, timeout=6000)
            break
        except PWTimeout:
            continue
    page.wait_for_timeout(1500)


BLOCK_MARKERS = ("pardon our interruption", "are you a human", "verify you are a human",
                 "unusual traffic", "access denied", "robot or human",
                 "activity on this site has been disabled", "verify your identity",
                 "please verify", "px-captcha", "checking your browser",
                 "enable javascript and cookies", "attention required!", "cloudflare ray id",
                 "cf-chl-")


def probe_page(product: dict) -> dict | None:
    """Render the page and return the raw probe. Used by --probe for debugging."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    cached = _session()
    page = cached["page"]

    nav_error = None
    for wait_until in ("domcontentloaded", "load", "commit"):
        try:
            page.goto(product["url"], wait_until=wait_until,
                      timeout=config.BROWSER_TIMEOUT_MS)
            nav_error = None
            break
        except Exception as exc:  # noqa: BLE001
            nav_error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:150]}"
    _settle(page)

    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:  # noqa: BLE001
        body = ""
    try:
        probe = page.evaluate(PROBE)
    except Exception:  # noqa: BLE001
        probe = {"btns": [], "fulfil": []}

    probe["_blocked"] = any(m in body for m in BLOCK_MARKERS)
    probe["_browser"] = cached["flavour"]
    probe["_body"] = body
    try:
        probe["_html"] = page.content().lower()
    except Exception:  # noqa: BLE001
        probe["_html"] = ""
    probe["_title"] = page.title()
    probe["_final_url"] = page.url
    probe["_nav_error"] = nav_error
    probe["_body_len"] = len(body)
    return probe


def capture_buybox(product: dict) -> bytes | None:
    """
    Screenshot the buy box for an alert.

    This bot has produced false positives, so an alert that shows you the
    actual page lets you judge it in one glance instead of trusting a
    verdict. Best effort -- never let a screenshot failure block an alert.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    try:
        page = _session()["page"]
        page.goto(product["url"], wait_until="domcontentloaded",
                  timeout=config.BROWSER_TIMEOUT_MS)
        _settle(page)
        for selector in ('[data-test*="fulfillment"]', '[data-test*="add-to-cart"]',
                         '[data-test*="AddToCart"]', "main"):
            try:
                element = page.query_selector(selector)
                if element:
                    return element.screenshot(type="png")
            except Exception:  # noqa: BLE001
                continue
        return page.screenshot(type="png")
    except Exception:  # noqa: BLE001
        close_thread_session()
        return None


def check(product: dict) -> Result | None:
    try:
        probe = probe_page(product)
    except Exception as exc:  # noqa: BLE001
        return Result(Status.ERROR, f"browser check failed: {type(exc).__name__}: {exc}", "browser")
    if probe is None:
        return None
    if probe.get("_blocked"):
        return Result(Status.BLOCKED,
                      f"anti-bot interstitial in rendered page ({probe.get('_browser')})", "browser")

    # Listing-page watching ("has the edition appeared at all?") also works
    # here, and matters because Costco returns 403 to plain HTTP.
    if product.get("kind") == "appears":
        phrase = product["phrase"].lower()
        if phrase in probe.get("_body", ""):
            return Result(Status.IN_STOCK,
                          f"phrase {phrase!r} appeared on the rendered listing page", "browser")
        return Result(Status.OUT_OF_STOCK,
                      f"phrase {phrase!r} not on the rendered listing page yet", "browser")

    # Structured data in the RENDERED html beats any DOM heuristic, and is
    # the only thing that answers Walmart -- it ships no Product JSON-LD and
    # renders no cart button at all when unavailable, but its app state says
    # "availabilityStatus":"OUT_OF_STOCK" plainly.
    html = probe.get("_html") or ""
    dom = _decide(probe)

    if html:
        for layer in (detect._jsonld, detect._embedded_state):
            structured = layer(html)
            if structured is None:
                continue
            structured.source = "browser+" + structured.source
            # The DOM is the tie-breaker for optimistic structured data:
            # Best Buy advertises schema.org/InStock next to a disabled
            # "Coming Soon" button. A disabled buy control is proof you
            # cannot buy it, whatever the metadata says.
            if structured.status in detect.ALERTABLE and dom.status is Status.OUT_OF_STOCK:
                dom.reason = (f"{dom.reason} (overrides {structured.source} "
                              f"claiming {structured.status.value})")
                return dom
            return structured

    return dom
