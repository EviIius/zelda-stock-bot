"""
Tests for the availability detection layers.

Run with:  python test_detect.py

These use synthetic pages modelled on how real retailer HTML is actually
shaped -- in particular the "recommended products" rail that contains
buy/sold-out language for *other* items, which is what made the previous
version of this bot unable to ever report a product as in stock.
"""

from __future__ import annotations

import sys

import config

config.USE_BROWSER = False  # exercise the HTTP layers deterministically

import browser_detect  # noqa: E402
import detect  # noqa: E402
from detect import Status  # noqa: E402

FILLER = "<p>lorem ipsum product details specifications reviews shipping</p>" * 200

REC_RAIL = """
<section class="recommendations">
  <h2>Similar items</h2>
  <div class="card"><span>Switch 2 Pro Controller</span><button>Add to cart</button></div>
  <div class="card"><span>Mario Kart World</span><span>Out of stock</span></div>
  <div class="card"><span>Switch 2 Dock</span><span>Sold out</span></div>
</section>
"""


def page(jsonld: str = "", main: str = "", extra: str = "") -> str:
    ld = f'<script type="application/ld+json">{jsonld}</script>' if jsonld else ""
    return f"<html><head>{ld}</head><body>{main}{REC_RAIL}{extra}{FILLER}</body></html>".lower()


def offer(availability: str, price: str = "519.99") -> str:
    return (
        '{"@context":"https://schema.org","@type":"Product",'
        '"name":"Nintendo Switch 2 Zelda 40th Anniversary Edition",'
        f'"offers":{{"@type":"Offer","price":"{price}",'
        f'"availability":"https://schema.org/{availability}"}}}}'
    )


CASES: list[tuple[str, str, Status]] = [
    (
        "JSON-LD OutOfStock wins over a rail full of 'add to cart'",
        page(jsonld=offer("OutOfStock"), main="<h1>Zelda Switch 2</h1><span>Sold out</span>"),
        Status.OUT_OF_STOCK,
    ),
    (
        "JSON-LD InStock wins over a rail full of 'sold out'",
        page(jsonld=offer("InStock"), main="<h1>Zelda Switch 2</h1><button>Add to cart</button>"),
        Status.IN_STOCK,
    ),
    (
        "JSON-LD PreOrder is treated as alertable, not as out of stock",
        page(jsonld=offer("PreOrder"), main="<h1>Zelda Switch 2</h1><button>Pre-order</button>"),
        Status.PREORDER,
    ),
    (
        "no JSON-LD, buy and sold language both present -> refuses to guess",
        page(main="<h1>Zelda Switch 2</h1><button>Add to cart</button>"),
        Status.UNKNOWN,
    ),
    (
        "no JSON-LD, only sold-out language -> out of stock",
        "<html><body><h1>Zelda Switch 2</h1><span>Out of stock</span>" + FILLER + "</body></html>",
        Status.OUT_OF_STOCK,
    ),
    (
        "no JSON-LD, only pre-order language -> refuses to claim buyable",
        "<html><body><h1>Zelda Switch 2</h1><button>Pre-order</button>" + FILLER + "</body></html>",
        Status.UNKNOWN,
    ),
    (
        "REGRESSION: Target-shaped page (one 'add to cart', no sold-out text, "
        "no JSON-LD) must NOT report in stock",
        "<html><body><h1>Zelda Switch 2</h1>"
        "<div class='rail'><button>Add to cart</button></div>" + FILLER + "</body></html>",
        Status.UNKNOWN,
    ),
    (
        "anti-bot interstitial is BLOCKED, not out of stock",
        "<html><body><h1>Pardon Our Interruption</h1><p>Are you a human?</p>" + FILLER + "</body></html>",
        Status.BLOCKED,
    ),
    (
        "truncated response is BLOCKED, not out of stock",
        "<html><body>nope</body></html>",
        Status.BLOCKED,
    ),
    (
        "WALMART: app state availabilityStatus=OUT_OF_STOCK (no Product JSON-LD, "
        "no cart button) -> out of stock",
        "<html><body><h1>Zelda Switch 2</h1><button>Add to list</button>"
        '<script>{"availabilityStatus":"OUT_OF_STOCK","isPreOrder":true}</script>'
        + FILLER + "</body></html>",
        Status.OUT_OF_STOCK,
    ),
    (
        "WALMART: availabilityStatus=IN_STOCK + isPreOrder -> preorder (alertable)",
        "<html><body><h1>Zelda Switch 2</h1>"
        '<script>{"availabilityStatus":"IN_STOCK","isPreOrder":true}</script>'
        + FILLER + "</body></html>",
        Status.PREORDER,
    ),
    (
        "availabilityStatus=IN_STOCK without preorder flag -> in stock",
        "<html><body><h1>Zelda Switch 2</h1>"
        '<script>{"availabilityStatus":"IN_STOCK"}</script>' + FILLER + "</body></html>",
        Status.IN_STOCK,
    ),
    (
        "embedded app state purchasable=false",
        '<html><head></head><body><script id="__NEXT_DATA__">{"props":{"ispurchasable":false}}</script>'
        + FILLER + "</body></html>",
        Status.OUT_OF_STOCK,
    ),
]


# --- the regression that motivated the rewrite -----------------------------
OLD_OUT_PHRASES = ["out of stock", "sold out", "currently unavailable", "coming soon",
                   "notify me when available"]
OLD_IN_PHRASES = ["add to cart", "ship it"]


def old_check(text: str):
    for phrase in OLD_OUT_PHRASES:
        if phrase in text:
            return False
    for phrase in OLD_IN_PHRASES:
        if phrase in text:
            return True
    return None


# Captured live from target.com on 2026-09-09 for this exact product.
TARGET_SOLD_OUT_PROBE = {
    "btns": [
        {"dt": "@web/ZipCodeButton/StyledZipCodeButton", "text": "Ship to 28277", "disabled": False},
        {"dt": "", "text": "Pickup Not available", "disabled": False},
        {"dt": "", "text": "Delivery Check availability", "disabled": False},
        {"dt": "", "text": "Shipping Not available", "disabled": False},
        {"dt": "Preorder.Disabled", "text": "Preorder", "disabled": True},
    ],
    "fulfil": ["Pickup Not available Delivery Check availability Shipping Not available Coming October 29"],
}

# The same page once the pre-order window opens.
TARGET_LIVE_PROBE = {
    "btns": [
        {"dt": "@web/ZipCodeButton/StyledZipCodeButton", "text": "Ship to 28277", "disabled": False},
        {"dt": "Preorder", "text": "Preorder", "disabled": False},
    ],
    "fulfil": ["Shipping Arrives by October 29 Preorder"],
}

# The false positive that shipped: a fresh headless browser with no saved
# zip code renders an enabled-looking Preorder control while every
# fulfilment method still reads "Not available".
TARGET_HEADLESS_TRAP = {
    "btns": [
        {"dt": "", "text": "Preorder", "disabled": False},
    ],
    "fulfil": ["Pickup Not available Delivery Not available Shipping Not available Coming October 29"],
}

# Pickup unavailable but shipping fine -- must still count as buyable.
MIXED_FULFILMENT = {
    "btns": [{"dt": "", "text": "Add to cart", "disabled": False}],
    "fulfil": ["Pickup Not available Shipping Arrives by Oct 29"],
}

# A disabled replacement control must be treated as unavailable.
COMING_SOON_CONTROL = {
    "btns": [{"dt": "", "text": "Coming Soon", "disabled": True}],
    "fulfil": [],
}

BROWSER_CASES = [
    (
        "disabled 'Coming Soon' replacement control -> out of stock",
        COMING_SOON_CONTROL,
        Status.OUT_OF_STOCK,
    ),
    (
        "REGRESSION: enabled button + all fulfilment dead -> out of stock",
        TARGET_HEADLESS_TRAP,
        Status.OUT_OF_STOCK,
    ),
    ("mixed fulfilment (shipping live) -> in stock", MIXED_FULFILMENT, Status.IN_STOCK),
    (
        "data-test 'Preorder.Disabled' counts as disabled",
        {"btns": [{"dt": "Preorder.Disabled", "text": "Preorder", "disabled": True}], "fulfil": []},
        Status.OUT_OF_STOCK,
    ),
    ("browser: disabled Preorder button -> out of stock", TARGET_SOLD_OUT_PROBE, Status.OUT_OF_STOCK),
    ("browser: enabled Preorder button -> preorder", TARGET_LIVE_PROBE, Status.PREORDER),
    (
        "browser: enabled Add to cart -> in stock",
        {"btns": [{"dt": "", "text": "Add to cart", "disabled": False}], "fulfil": []},
        Status.IN_STOCK,
    ),
    ("browser: nothing recognisable -> unknown", {"btns": [], "fulfil": []}, Status.UNKNOWN),
]


def main() -> int:
    failures = 0
    product = {"key": "t", "name": "Test", "url": "https://example.test/p", "kind": "product"}

    for label, html, expected in CASES:
        body = html.lower()
        detect.fetch = lambda url, timeout=None, _b=body: (_b, 200, "ok")  # noqa: E731
        got = detect.check_product(product)
        ok = got.status is expected
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            print(f"        expected {expected.value}, got {got.status.value} ({got.source}: {got.reason})")

    print()
    for label, probe, expected in BROWSER_CASES:
        got = browser_detect._decide(probe)
        ok = got.status is expected
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            print(f"        expected {expected.value}, got {got.status.value} ({got.reason})")

    print()
    live = page(jsonld=offer("InStock"), main="<h1>Zelda Switch 2</h1><button>Add to cart</button>")
    old = old_check(live)
    detect.fetch = lambda url, timeout=None, _b=live: (_b, 200, "ok")  # noqa: E731
    new = detect.check_product(product).status
    print("Regression check -- a genuinely in-stock page with a recommendation rail:")
    print(f"  old logic: {old!r}  (False/None = you get no text)")
    print(f"  new logic: {new.value}")
    if old is not False or new is not Status.IN_STOCK:
        print("  !! regression check did not behave as expected")
        failures += 1

    print()
    print(f"{len(CASES) + len(BROWSER_CASES) + 1} checks, {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
