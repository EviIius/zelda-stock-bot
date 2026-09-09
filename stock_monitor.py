"""
Stock monitor for the Zelda 40th Anniversary Nintendo Switch 2.

Watches Target, Walmart, Best Buy and the Nintendo Store for the console
becoming buyable -- in stock *or* a pre-order window reopening -- and
pushes an alert to every notification channel you've configured. Also
watches Costco's Switch 2 listing in case they start carrying the edition
at all.

Usage
-----
  python stock_monitor.py                 one pass, then exit (good for cron)
  python stock_monitor.py --loop          check continuously until stopped
  python stock_monitor.py --loop --duration 3300
                                          loop for 55 minutes, then exit
  python stock_monitor.py --debug         one pass, showing how each verdict
                                          was reached (start here when
                                          something looks wrong)
  python stock_monitor.py --test-alert    prove your notifications work
  python stock_monitor.py --status        print what the bot currently thinks
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timezone

import config
import detect
import feeds
import notify
from detect import Status

STATE_VERSION = 2

ICON = {
    Status.IN_STOCK: "\U0001f7e2",
    Status.PREORDER: "\U0001f7e1",
    Status.OUT_OF_STOCK: "⚪",
    Status.BLOCKED: "\U0001f6ab",
    Status.ERROR: "\U0001f534",
    Status.UNKNOWN: "❓",
}


def now() -> float:
    return time.time()


def stamp() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if not os.path.exists(config.STATE_FILE):
        return {"version": STATE_VERSION, "products": {}}
    try:
        with open(config.STATE_FILE) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"version": STATE_VERSION, "products": {}}
    if data.get("version") != STATE_VERSION:
        # Old flat {name: bool} format -- start clean rather than
        # misinterpreting it.
        return {"version": STATE_VERSION, "products": {}}
    return data


def save_state(state: dict) -> None:
    tmp = config.STATE_FILE + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(state, handle, indent=2)
    os.replace(tmp, config.STATE_FILE)  # atomic; never leaves a half-written file


def entry_for(state: dict, key: str) -> dict:
    return state["products"].setdefault(
        key,
        {
            "status": None,
            "since": None,
            "last_alert_ts": 0.0,
            "alert_count": 0,
            "bad_since": None,
            "last_health_alert_ts": 0.0,
        },
    )


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def confirmed(product: dict, first: detect.Result) -> bool:
    """
    Re-check a positive reading before waking someone up.

    Asymmetric on purpose: this gate only ever suppresses alerts, never
    creates them. A restock that is real will still be there six seconds
    later; a rendering artefact will not.
    """
    if not config.CONFIRM_BEFORE_ALERT:
        return True

    print(f"    confirming {first.status.value} ({first.source})...")
    time.sleep(config.CONFIRM_DELAY_SECONDS)
    second = detect.check(product)

    if second.alertable:
        print(f"    confirmed by {second.source}: {second.status.value}")
        return True

    print(f"    NOT confirmed - second check said {second.status.value} "
          f"({second.source}: {second.reason[:110]}). Suppressing alert.")
    return False


def build_alert(product: dict, result: detect.Result, repeat: int) -> notify.Alert:
    headline = "IN STOCK" if result.status is Status.IN_STOCK else "PRE-ORDER OPEN"
    if product.get("kind") == "appears":
        headline = "SHOWED UP"

    title = f"{ICON[result.status]} {headline} - {product['name']}"
    if repeat > 1:
        title += f" (reminder {repeat})"

    fields = [
        ("Retailer", product["name"], True),
        ("Price", result.price or "-", True),
        ("Detected via", result.source, True),
    ]
    if product.get("cart_url"):
        fields.append(("Add to cart", f"[Tap here]({product['cart_url']})", False))
    fields.append(("Product page", f"[{product['name']}]({product['url']})", False))
    fields.append(("Why", result.reason[:1000], False))

    screenshot = None
    if config.ALERT_SCREENSHOTS:
        try:
            import browser_detect

            screenshot = browser_detect.capture_buybox(product)
        except Exception:  # noqa: BLE001 - a screenshot must never block an alert
            screenshot = None

    body = (
        f"**{product['name']}** is showing as **{headline}**.\n\n"
        "Go now - these sell out in minutes."
    )
    if screenshot is None and config.ALERT_SCREENSHOTS:
        body += "\n\n_(Couldn't capture the page; verify before trusting this.)_"

    return notify.Alert(
        title=title,
        body=body,
        url=product["url"],
        cart_url=product.get("cart_url"),
        price=result.price,
        urgent=True,
        kind=result.status.value,
        fields=fields,
        image_png=screenshot,
        footer=f"Confirmed by a second check - {stamp()}",
    )


def maybe_health_alert(product: dict, entry: dict, result: detect.Result) -> bool:
    """Warn once if a retailer has been unreadable long enough to matter."""
    bad_for = now() - (entry["bad_since"] or now())
    if bad_for < config.HEALTH_ALERT_AFTER_MINUTES * 60:
        return False
    if now() - entry["last_health_alert_ts"] < config.HEALTH_ALERT_COOLDOWN_MINUTES * 60:
        return False

    minutes = int(bad_for // 60)
    alert = notify.Alert(
        kind="health",
        fields=[("Retailer", product["name"], True),
                ("Status", result.status.value, True),
                ("Unreadable for", f"{minutes} min", True),
                ("Last reason", result.reason[:1000], False)],
        title=f"{ICON[result.status]} Monitor can't read {product['name']}",
        body=(
            f"{product['name']} has returned no usable answer for {minutes} minutes.\n\n"
            f"Last result: {result.status.value} - {result.reason}\n\n"
            "This is a heads-up that the check is degraded, not that the item is "
            "unavailable. Nothing has been missed yet, but this retailer isn't "
            "currently being watched reliably."
        ),
        url=product["url"],
        urgent=False,
    )
    results = notify.send(alert)
    print(f"    health alert sent: {results}")
    entry["last_health_alert_ts"] = now()
    return True




def maybe_heartbeat(state: dict) -> None:
    """
    Once a day, report that the monitor is alive and what it currently sees.

    Without this, "no alerts" is ambiguous: it could mean the console is
    still unavailable, or that the terminal was closed three days ago.
    """
    if not config.HEARTBEAT_ENABLED:
        return

    now_local = datetime.now()
    today = now_local.strftime("%Y-%m-%d")
    if state.get("last_heartbeat_date") == today:
        return
    # A bare "hour >= HEARTBEAT_HOUR" is true all evening, so restarting at
    # 11pm fired the morning check-in immediately. Bound it to a window.
    if not (config.HEARTBEAT_HOUR
            <= now_local.hour
            < config.HEARTBEAT_HOUR + config.HEARTBEAT_WINDOW_HOURS):
        return

    fields, degraded = [], 0
    for product in config.PRODUCTS:
        entry = state["products"].get(product["key"], {})
        reading = entry.get("last_reading") or "never checked"
        known_good = entry.get("status")
        icon = ICON.get(Status(reading), "?") if reading in {s.value for s in Status} else "?"

        value = f"{icon} {reading}"
        # Say so plainly when the displayed status is just the last thing we
        # managed to read, rather than what the retailer says right now.
        if reading in {s.value for s in detect.UNUSABLE}:
            degraded += 1
            if known_good and known_good != reading:
                age = entry.get("last_good_ts")
                ago = f", last read {int((now() - age) // 3600)}h ago" if age else ""
                value += f"\n(last known: {known_good}{ago})"
        fields.append((product["name"][:40], value, True))

    summary = ("All retailers reading cleanly."
               if not degraded else
               f"{degraded} of {len(config.PRODUCTS)} retailers unreadable right now.")

    notify.send(
        notify.Alert(
            title="\U0001f4a4 Daily check-in",
            body=f"Monitor is running. {summary}",
            urgent=False,
            kind="heartbeat",
            fields=fields,
            footer=f"Next check-in tomorrow ~{config.HEARTBEAT_HOUR:02d}:00",
        )
    )
    state["last_heartbeat_date"] = today
    print("    daily heartbeat sent")


def log_check(product: dict, result: detect.Result) -> None:
    """Append every reading to a CSV so history is diagnosable after the fact."""
    if not config.LOG_ENABLED:
        return
    try:
        import csv

        new = not os.path.exists(config.LOG_CSV)
        with open(config.LOG_CSV, "a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if new:
                writer.writerow(["timestamp", "retailer", "status", "source", "price", "reason"])
            writer.writerow([stamp(), product["key"], result.status.value,
                             result.source, result.price or "", result.reason[:300]])
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Social feeds (@Wario64 / @IGNDeals)
# ---------------------------------------------------------------------------
def check_feeds(state: dict) -> None:
    """
    Poll the watched accounts and forward anything that matches.

    Deliberately quiet about its own failures: the mirrors this depends on
    go down routinely, and a bonus signal shouldn't generate noise or
    imply the retailer checks are broken.
    """
    if not config.ENABLE_FEED_WATCH:
        return

    bucket = state.setdefault("feeds", {"seen": [], "last_check": 0.0})
    if now() - bucket.get("last_check", 0.0) < config.FEED_INTERVAL_MINUTES * 60:
        return
    bucket["last_check"] = now()

    try:
        posts, seen = feeds.poll(bucket.get("seen", []))
    except Exception as exc:  # noqa: BLE001
        print(f"  \U0001f4f0 feed watch unavailable ({type(exc).__name__}) - ignoring")
        return

    bucket["seen"] = seen
    if not posts:
        print(f"  \U0001f4f0 feeds: nothing new matching Zelda Switch 2")
        return

    for post in posts:
        print(f"    >>> FEED HIT from @{post['account']}: {post['text'][:80]}")
        results = notify.send(
            notify.Alert(
                title=f"\U0001f4f0 @{post['account']} posted about the Zelda Switch 2",
                body=(f"{post['text']}\n\n{post['link']}\n\n"
                      "This is a social-feed tip, not a confirmed stock check - "
                      "go look now, it may already be gone."),
                url=post["link"] or None,
                urgent=True,
                kind="feed",
                fields=[("Account", f"@{post['account']}", True),
                        ("Confidence", "Unverified social tip", True)],
            )
        )
        print(f"        sent: {results}")


# ---------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------
def run_pass(state: dict, debug: bool = False) -> None:
    for index, product in enumerate(config.PRODUCTS):
        if index and config.STAGGER:
            time.sleep(config.STAGGER + random.uniform(0, 2))

        entry = entry_for(state, product["key"])
        result = detect.check(product)
        previous = entry["status"]
        entry["last_reading"] = result.status.value
        log_check(product, result)

        line = f"  {ICON[result.status]} {product['name']:<42} {result.status.value}"
        if result.price:
            line += f"  {result.price}"
        print(line)
        if not debug and result.status in detect.UNUSABLE:
            print(f"      why: {result.reason[:160]}")
        if debug:
            print(f"      source : {result.source}")
            print(f"      reason : {result.reason}")
            print(f"      http   : {result.http_status}   body: {result.body_len} bytes")
            print(f"      last   : {previous}")

        # --- health tracking -------------------------------------------
        if result.status in detect.UNUSABLE:
            if entry["bad_since"] is None:
                entry["bad_since"] = now()
            maybe_health_alert(product, entry, result)
            # Deliberately do NOT overwrite the last known-good status: a
            # transient block must not erase what we knew.
            continue

        entry["bad_since"] = None

        # --- alerting ---------------------------------------------------
        if result.alertable:
            was_alertable = previous in {s.value for s in detect.ALERTABLE}
            due = now() - entry["last_alert_ts"] >= config.REALERT_MINUTES * 60
            first_time = not was_alertable

            if (first_time or (due and entry["alert_count"] < config.MAX_REALERTS)) \
                    and not confirmed(product, result):
                # Second look disagreed. Don't alert, and don't record it as
                # buyable either -- leave the last known-good status intact
                # so a real restock still reads as a fresh transition.
                continue

            if first_time or (due and entry["alert_count"] < config.MAX_REALERTS):
                entry["alert_count"] = 1 if first_time else entry["alert_count"] + 1
                entry["last_alert_ts"] = now()
                alert = build_alert(product, result, entry["alert_count"])
                results = notify.send(alert)
                print(f"    >>> ALERT SENT: {results}")
        elif previous in {s.value for s in detect.ALERTABLE}:
            print("    (was buyable, now gone -- resetting alert counter)")
            entry["alert_count"] = 0

        if result.status.value != previous:
            entry["since"] = now()
        entry["status"] = result.status.value
        entry["last_good_ts"] = now()

    check_feeds(state)
    maybe_heartbeat(state)
    save_state(state)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def cmd_test_alert() -> int:
    channels = notify.configured_channels()
    if not channels:
        print("No notification channels configured. Set DISCORD_WEBHOOK_URL "
              "(and optionally PUSHOVER_TOKEN / PUSHOVER_USER) and try again.")
        return 1
    print(f"Configured channels: {', '.join(channels)}")
    results = notify.send(
        notify.Alert(
            title="✅ Zelda stock bot - test alert",
            body=("If you're reading this on your phone, the alert path works.\n\n"
                  "This is only a test - nothing is actually in stock."),
            url="https://www.nintendo.com/us/store/products/nintendo-switch-2-the-legend-of-zelda-40th-anniversary-edition-121642/",
            urgent=False,
        )
    )
    ok = all(value == "ok" for value in results.values())
    for name, value in results.items():
        print(f"  {name}: {value}")
    return 0 if ok else 1


def cmd_probe(key: str) -> int:
    """Dump the rendered buy box for one retailer, plus the verdict it produces."""
    import json as _json

    product = next((p for p in config.PRODUCTS if p["key"] == key), None)
    if product is None:
        print(f"Unknown retailer {key!r}. Options: {', '.join(p['key'] for p in config.PRODUCTS)}")
        return 1

    try:
        import browser_detect
    except ImportError:
        print("Playwright isn't installed. Run:")
        print("    python -m pip install -r requirements-browser.txt")
        print("    python -m playwright install chromium")
        return 1

    print(f"Rendering {product['url']}\n")
    try:
        probe = browser_detect.probe_page(product)
    except Exception as exc:  # noqa: BLE001 - a debugging tool must not traceback
        print(f"Render failed: {type(exc).__name__}")
        print(f"  {str(exc).splitlines()[0][:300]}")
        return 1
    if probe is None:
        print("Playwright unavailable.")
        return 1

    print(f"browser      : {probe.get('_browser')}")
    print(f"page title   : {probe.get('_title')!r}")
    print(f"final url    : {probe.get('_final_url')}")
    print(f"body text    : {probe.get('_body_len', 0)} chars")
    print(f"blocked      : {probe.get('_blocked')}")
    if probe.get("_nav_error"):
        print(f"nav error    : {probe['_nav_error']}")
    if not probe.get("btns"):
        # The most useful thing when a page yields nothing at all.
        print(f"\nPage text (first 600 chars):\n  {probe.get('_body', '')[:600]!r}")
    print()

    print("Buy-ish controls found:")
    for button in probe.get("btns", []):
        if browser_detect.BUY_TEXT.search(button["text"]) or browser_detect.BUY_TEXT.search(button["dt"]):
            flag = "DISABLED" if button["disabled"] else "enabled "
            print(f"  [{flag}] text={button['text']!r} data-test={button['dt']!r}")
    print("\nFulfilment panel:")
    for text in probe.get("fulfil", []):
        print(f"  {text}")
    print(f"\nFulfilment verdict: {browser_detect._fulfilment_verdict(' '.join(probe.get('fulfil', [])))}")
    result = browser_detect._decide(probe)
    print(f"\nVERDICT: {result.status.value}\nREASON : {result.reason}")
    print(f"\nAll buttons (raw):\n{_json.dumps(probe.get('btns', []), indent=2)[:2500]}")
    return 0


def cmd_status() -> int:
    state = load_state()
    if not state["products"]:
        print("No state recorded yet - run a check first.")
        return 0
    for product in config.PRODUCTS:
        entry = state["products"].get(product["key"])
        if not entry:
            continue
        age = f"{int((now() - entry['since']) // 60)}m ago" if entry.get("since") else "?"
        print(f"  {product['name']:<42} {entry['status'] or 'unknown':<14} since {age}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--loop", action="store_true", help="keep checking until stopped")
    parser.add_argument("--duration", type=int, default=0,
                        help="with --loop, stop after this many seconds (0 = forever)")
    parser.add_argument("--interval", type=int, default=config.CHECK_INTERVAL,
                        help="seconds between passes when looping")
    parser.add_argument("--debug", action="store_true", help="explain every verdict")
    parser.add_argument("--test-alert", action="store_true", help="send a test notification and exit")
    parser.add_argument("--status", action="store_true", help="print current state and exit")
    parser.add_argument("--probe", metavar="RETAILER",
                        help="render one retailer and dump what the detector saw "
                             "(e.g. --probe target). The tool to reach for when a "
                             "verdict looks wrong.")
    args = parser.parse_args()

    if args.test_alert:
        return cmd_test_alert()
    if args.status:
        return cmd_status()
    if args.probe:
        return cmd_probe(args.probe)

    channels = notify.configured_channels()
    print(f"[{stamp()}] notification channels: {', '.join(channels) or 'NONE CONFIGURED'}")
    if not channels:
        print("  !! Nothing will reach you. Set DISCORD_WEBHOOK_URL at minimum.")

    state = load_state()

    if not args.loop:
        run_pass(state, debug=args.debug)
        return 0

    deadline = now() + args.duration if args.duration else None
    passes = 0
    try:
        while True:
            passes += 1
            print(f"[{stamp()}] pass {passes}")
            run_pass(state, debug=args.debug)

            wait = args.interval + random.uniform(0, config.JITTER)
            if deadline and now() + wait > deadline:
                print(f"[{stamp()}] duration reached after {passes} passes")
                return 0
            time.sleep(wait)
    except KeyboardInterrupt:
        print(f"\n[{stamp()}] stopped after {passes} passes")
        return 0


if __name__ == "__main__":
    sys.exit(main())
