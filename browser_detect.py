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

import config
from detect import Result, Status

BUY_TEXT = re.compile(r"add to cart|add for shipping|pre-?order|buy now|ship it|add to bag", re.I)
SOLD_TEXT = re.compile(r"sold out|out of stock|not available|unavailable|notify me|coming soon", re.I)

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

    if verdict == "live":
        return Result(Status.UNKNOWN,
                      f"fulfilment looks available but no buy control found ({details})", "browser")

    return Result(Status.UNKNOWN, "rendered page exposed no recognisable buy control", "browser")


def probe_page(product: dict) -> dict | None:
    """Render the page and return the raw probe. Used by --probe for debugging."""
    try:
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        try:
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1440, "height": 900},
                locale="en-US",
            )
            page = context.new_page()
            page.goto(product["url"], wait_until="domcontentloaded",
                      timeout=config.BROWSER_TIMEOUT_MS)

            # Wait for the buy box specifically, not just the network. The
            # fulfilment module hydrates after first paint, and reading too
            # early sees a control that has not been disabled yet.
            for selector in ('[data-test*="fulfillment"]', '[data-test*="add-to-cart"]',
                             'button:has-text("Add to cart")', 'button:has-text("Pre-order")'):
                try:
                    page.wait_for_selector(selector, timeout=6000)
                    break
                except PWTimeout:
                    continue
            page.wait_for_timeout(1500)  # let hydration settle

            body = (page.inner_text("body") or "").lower()
            probe = page.evaluate(PROBE)
            probe["_blocked"] = any(m in body for m in (
                "pardon our interruption", "are you a human",
                "verify you are a human", "unusual traffic", "access denied"))
            return probe
        finally:
            browser.close()


def capture_buybox(product: dict) -> bytes | None:
    """
    Screenshot the buy box for an alert.

    This bot has produced false positives, so an alert that shows you the
    actual page lets you judge it in one glance instead of trusting a
    verdict. Best effort -- never let a screenshot failure block an alert.
    """
    try:
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                page = browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
                page.goto(product["url"], wait_until="domcontentloaded",
                          timeout=config.BROWSER_TIMEOUT_MS)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except PWTimeout:
                    pass
                page.wait_for_timeout(1200)
                for selector in ('[data-test*="fulfillment"]', '[data-test*="add-to-cart"]',
                                 '[data-test*="AddToCart"]', "main"):
                    try:
                        element = page.query_selector(selector)
                        if element:
                            return element.screenshot(type="png")
                    except Exception:  # noqa: BLE001
                        continue
                return page.screenshot(type="png")
            finally:
                browser.close()
    except Exception:  # noqa: BLE001
        return None


def check(product: dict) -> Result | None:
    if product.get("kind") == "appears":
        return None  # listing-page watching is fine over plain HTTP
    try:
        probe = probe_page(product)
    except Exception as exc:  # noqa: BLE001
        return Result(Status.ERROR, f"browser check failed: {type(exc).__name__}: {exc}", "browser")
    if probe is None:
        return None
    if probe.get("_blocked"):
        return Result(Status.BLOCKED, "anti-bot interstitial in rendered page", "browser")
    return _decide(probe)
