"""
Availability detection via a real rendered browser (Playwright).

Necessary because the retailers that matter most here build their buy box
in JavaScript. Measured on Target's page for this console:

    raw HTML:   "add to cart" x1,  "out of stock" x0,  0 JSON-LD blocks
    rendered:   button data-test="Preorder.Disabled", disabled=true
                "Pickup Not available / Shipping Not available / Coming October 29"

So the fetched HTML contains no availability information at all, while the
rendered DOM states it unambiguously. Plain HTTP cannot win there; this can.

Optional. If Playwright isn't installed, `check()` returns None and the
caller falls back to the HTTP layers.

Install:
    pip install playwright
    playwright install chromium
"""

from __future__ import annotations

import re

import config
from detect import Result, Status

BUY_TEXT = re.compile(r"add to cart|add for shipping|pre-?order|buy now|ship it|add to bag", re.I)
SOLD_TEXT = re.compile(r"sold out|out of stock|unavailable|notify me|coming soon", re.I)

# Reads the buy box out of the rendered page. Returns buttons with their
# disabled state, which is the signal that actually flips on a restock.
PROBE = """() => {
  // Navigation menus are full of links that read like buy controls -- on
  // GameStop the site nav contains enabled <a class="dropdown-link">Pre-Order</a>
  // entries while the real button is a DISABLED "Pre-Order for Pick Up".
  // Matching those would invent a restock that isn't there, so anything
  // inside chrome is excluded and only real controls are considered.
  const inChrome = el => !!el.closest('nav, header, footer, [role="navigation"], [role="menu"], .dropdown-menu, .nav, .menu');
  const btns = [...document.querySelectorAll('button, input[type="submit"], [role="button"]')]
    .filter(b => !inChrome(b))
    .filter(b => b.tagName !== 'A' || b.getAttribute('role') === 'button')
    .map(b => ({
      dt: b.getAttribute('data-test') || b.getAttribute('data-automation-id') || '',
      text: (b.innerText || b.value || '').replace(/\s+/g, ' ').trim().slice(0, 60),
      disabled: b.disabled === true
                || b.getAttribute('aria-disabled') === 'true'
                || String(b.className).toLowerCase().includes('disabled'),
    }))
    .filter(b => b.text || b.dt);
  const fulfil = [...document.querySelectorAll('[data-test*="fulfillment"], [data-test*="add-to-cart"]')]
    .filter(e => !inChrome(e))
    .map(e => e.innerText.replace(/\s+/g, ' ').trim().slice(0, 200));
  return { btns, fulfil, title: document.title };
}"""


def _decide(probe: dict) -> Result:
    buttons = probe.get("btns", [])
    fulfil_text = " ".join(probe.get("fulfil", []))

    candidates = [
        b for b in buttons
        if BUY_TEXT.search(b["text"]) or BUY_TEXT.search(b["dt"])
    ]

    if candidates:
        live = [b for b in candidates if not b["disabled"]]
        if live:
            label = (live[0]["text"] or live[0]["dt"])
            status = Status.PREORDER if re.search(r"pre-?order", label, re.I) else Status.IN_STOCK
            return Result(status, f"rendered buy button enabled: {label!r}", "browser")
        labels = [b["text"] or b["dt"] for b in candidates][:3]
        return Result(Status.OUT_OF_STOCK, f"rendered buy button(s) disabled: {labels}", "browser")

    # No buy button at all. Fall back to the fulfilment panel's own wording.
    if fulfil_text:
        if SOLD_TEXT.search(fulfil_text):
            return Result(Status.OUT_OF_STOCK,
                          f"fulfilment panel: {fulfil_text[:120]!r}", "browser")
        if BUY_TEXT.search(fulfil_text):
            return Result(Status.IN_STOCK,
                          f"fulfilment panel: {fulfil_text[:120]!r}", "browser")

    return Result(Status.UNKNOWN, "rendered page exposed no recognisable buy control", "browser")


def check(product: dict) -> Result | None:
    """Render the product page and read its buy box. None if unavailable."""
    try:
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    if product.get("kind") == "appears":
        return None  # listing-page watching is fine over plain HTTP

    try:
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
                # Give the buy box time to hydrate. Best effort -- if the
                # network never settles we still read whatever rendered.
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except PWTimeout:
                    pass

                body = (page.inner_text("body") or "").lower()
                if any(m in body for m in ("pardon our interruption", "are you a human",
                                           "verify you are a human", "unusual traffic")):
                    return Result(Status.BLOCKED, "anti-bot interstitial in rendered page", "browser")

                return _decide(page.evaluate(PROBE))
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001
        return Result(Status.ERROR, f"browser check failed: {type(exc).__name__}: {exc}", "browser")
