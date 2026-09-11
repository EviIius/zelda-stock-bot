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
import logging
import os
import random
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import config
import detect
import feeds
import notify
import requests
from detect import Status

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

STATE_VERSION = 2
STATE_LOCK = threading.RLock()
CSV_LOCK = threading.Lock()
_SINGLE_INSTANCE_GUARD = None
_MACOS_CAFFEINATE = None


class _LoggerStream:
    """Line-buffered file-like adapter for a rotating service logger."""

    encoding = "utf-8"

    def __init__(self, logger: logging.Logger, level: int):
        self.logger = logger
        self.level = level
        self.pending = ""
        self.lock = threading.Lock()

    def write(self, text: str) -> int:
        with self.lock:
            self.pending += text
            while "\n" in self.pending:
                line, self.pending = self.pending.split("\n", 1)
                if line:
                    self.logger.log(self.level, line)
        return len(text)

    def flush(self) -> None:
        with self.lock:
            if self.pending:
                self.logger.log(self.level, self.pending)
                self.pending = ""
        for handler in self.logger.handlers:
            handler.flush()

    def isatty(self) -> bool:
        return False


def _configure_service_logging() -> None:
    """Redirect daemon output to a bounded rotating log when requested."""
    path = os.environ.get("SERVICE_LOG_FILE", "").strip()
    if not path:
        return
    from logging.handlers import RotatingFileHandler

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    logger = logging.getLogger("zelda-stock-monitor-service")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            path,
            maxBytes=int(os.environ.get("SERVICE_LOG_MAX_BYTES", 5 * 1024 * 1024)),
            backupCount=int(os.environ.get("SERVICE_LOG_BACKUPS", 5)),
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    sys.stdout = _LoggerStream(logger, logging.INFO)
    sys.stderr = _LoggerStream(logger, logging.ERROR)

ICON = {
    Status.IN_STOCK: "\U0001f7e2",
    Status.PREORDER: "\U0001f7e1",
    Status.OUT_OF_STOCK: "⚪",
    Status.BLOCKED: "\U0001f6ab",
    Status.ERROR: "\U0001f534",
    Status.UNKNOWN: "❓",
}
CONSOLE_MARK = {
    Status.IN_STOCK: "[STOCK]",
    Status.PREORDER: "[PRE]",
    Status.OUT_OF_STOCK: "[OUT]",
    Status.BLOCKED: "[BLOCK]",
    Status.ERROR: "[ERR]",
    Status.UNKNOWN: "[?]",
}


def now() -> float:
    return time.time()


def _claim_single_instance() -> bool:
    """Prevent duplicate looping monitors on Windows, macOS and Linux."""
    global _SINGLE_INSTANCE_GUARD
    if os.name != "nt":
        import fcntl

        lock_path = os.path.join(os.path.dirname(config.STATE_FILE),
                                 ".zelda-stock-monitor.lock")
        handle = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return False
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        _SINGLE_INSTANCE_GUARD = handle
        return True

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateMutexW(None, False, "Local\\ZeldaStockMonitorMain")
    if not handle:
        raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _SINGLE_INSTANCE_GUARD = handle
    return True


def _prevent_macos_sleep() -> None:
    """Keep an always-on Mac awake while the monitor process is healthy."""
    global _MACOS_CAFFEINATE
    if sys.platform != "darwin" or not config.PREVENT_MACOS_SLEEP:
        return
    try:
        _MACOS_CAFFEINATE = subprocess.Popen(
            ["/usr/bin/caffeinate", "-is", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("macOS idle sleep prevention active while monitor is running.", flush=True)
    except OSError as exc:
        print(f"macOS sleep prevention unavailable: {exc}", flush=True)


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
    defaults = {
            "status": None,
            "since": None,
            "last_alert_ts": 0.0,
            "alert_count": 0,
            "bad_since": None,
            "last_health_alert_ts": 0.0,
            "last_check_ts": 0.0,
            "last_latency_seconds": None,
            "next_check_ts": 0.0,
            "pending_alert_id": None,
        }
    entry = state["products"].setdefault(key, {})
    for name, value in defaults.items():
        entry.setdefault(name, value)
    return entry


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def confirmed(product: dict, first: detect.Result) -> bool:
    """Re-check a positive reading after the immediate alert is delivered."""
    if not config.CONFIRM_BEFORE_ALERT:
        return True

    print(f"    confirming {first.status.value} ({first.source})...")
    time.sleep(config.CONFIRM_DELAY_SECONDS)
    second = detect.check(product)

    if second.alertable:
        print(f"    confirmed by {second.source}: {second.status.value}")
        return True

    print(f"    NOT confirmed - second check said {second.status.value} "
          f"({second.source}: {second.reason[:110]}). Sending correction.")
    return False


def build_alert(product: dict, result: detect.Result, repeat: int,
                image_png: bytes | None = None, confirmed_result: bool = False) -> notify.Alert:
    headline = "IN STOCK" if result.status is Status.IN_STOCK else "PRE-ORDER OPEN"
    if product.get("kind") == "appears":
        headline = "SHOWED UP"

    group = product.get("group", "console")
    item = product.get("product_name", product["name"])
    item_icon = ("📦" if product.get("custom") else
                 "🎮" if group == "controller" else "🖥️")
    title = f"{ICON[result.status]} {headline} • {product['name']}"
    if repeat > 1:
        title += f" (reminder {repeat})"

    fields = [
        ("🏬 Retailer", product["name"], True),
        ("💵 Price", result.price or "See retailer", True),
        ("📡 Signal", result.source, True),
    ]
    if product.get("cart_url"):
        fields.append(("🛒 BUY NOW", f"[**Add directly to cart**]({product['cart_url']})", False))
    else:
        fields.append(("🔗 BUY NOW", f"[**Open {product['name']} product page**]({product['url']})", False))
    fields.append(("Detection evidence", result.reason[:1000], False))

    body = (
        f"## {item_icon} {item}\n"
        f"**{headline} at {product['name']}**\n\n"
        "### ⚡ Act now — limited releases can disappear in seconds."
    )
    if not confirmed_result:
        body += "\n\n_This is the immediate first signal. A verification check is running now._"

    return notify.Alert(
        title=title,
        body=body,
        url=product["url"],
        cart_url=product.get("cart_url"),
        price=result.price,
        urgent=True,
        kind=result.status.value,
        fields=fields,
        image_png=image_png,
        group=group,
        footer=(f"Confirmed by a second check - {stamp()}" if confirmed_result
                else f"Immediate signal - {stamp()}"),
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
        fields=[("Item", product.get("product_name", product["name"]), False),
                ("Retailer", product["name"], True),
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
        group=product.get("group", "console"),
    )
    alert_id = f"health:{product['key']}:{int(entry['bad_since'] or now())}"
    if notify.has_pending(alert_id):
        return False
    results, delivered = notify.send_reliable(
        alert, alert_id,
        {"event_type": "health", "product_key": product["key"]},
    )
    print(f"    health alert delivery: {results}")
    if delivered:
        entry["last_health_alert_ts"] = now()
    return delivered




def maybe_heartbeat(state: dict) -> None:
    """Once a day, post a separate health summary to each product channel."""
    if not config.HEARTBEAT_ENABLED:
        return

    now_local = datetime.now()
    today = now_local.strftime("%Y-%m-%d")
    # A bare "hour >= HEARTBEAT_HOUR" is true all evening, so restarting at
    # 11pm fired the morning check-in immediately. Bound it to a window.
    if not (config.HEARTBEAT_HOUR
            <= now_local.hour
            < config.HEARTBEAT_HOUR + config.HEARTBEAT_WINDOW_HOURS):
        return

    enabled = [product for product in config.PRODUCTS if product.get("enabled", True)]
    dates = state.setdefault("last_heartbeat_dates", {})
    if state.get("last_heartbeat_date") and "console" not in dates:
        dates["console"] = state["last_heartbeat_date"]

    for group in sorted({product.get("group", "console") for product in enabled}):
        if dates.get(group) == today:
            continue
        products = [p for p in enabled if p.get("group", "console") == group]
        fields, degraded = [], 0
        for product in products:
            entry = state["products"].get(product["key"], {})
            reading = entry.get("last_reading") or "never checked"
            known_good = entry.get("status")
            icon = ICON.get(Status(reading), "?") if reading in {s.value for s in Status} else "?"
            value = f"{icon} {reading.replace('_', ' ')}"
            if reading in {s.value for s in detect.UNUSABLE}:
                degraded += 1
                if known_good and known_good != reading:
                    age = entry.get("last_good_ts")
                    ago = f", last read {int((now() - age) // 3600)}h ago" if age else ""
                    value += f"\n(last known: {known_good}{ago})"
            fields.append((product["name"][:40], value, True))

        label = "Controller" if group == "controller" else "Console"
        summary = ("All retailer checks are readable."
                   if not degraded else
                   f"{degraded} of {len(products)} retailer checks are unreadable right now.")
        alert = notify.Alert(
            title=f"💤 {label} monitor daily check-in",
            body=f"**Monitor running normally.** {summary}",
            urgent=False, kind="heartbeat", fields=fields, group=group,
            footer=f"Next check-in tomorrow ~{config.HEARTBEAT_HOUR:02d}:00",
        )
        alert_id = f"heartbeat:{group}:{today}"
        if notify.has_pending(alert_id):
            continue
        results, delivered = notify.send_reliable(
            alert, alert_id,
            {"event_type": "heartbeat", "date": today, "group": group},
        )
        if delivered:
            dates[group] = today
        print(f"    {group} daily heartbeat delivery: {results}")


def log_check(product: dict, result: detect.Result) -> None:
    """Append every reading to a CSV so history is diagnosable after the fact."""
    if not config.LOG_ENABLED:
        return
    try:
        import csv

        new = not os.path.exists(config.LOG_CSV)
        with CSV_LOCK:
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
# Independent retailer workers
# ---------------------------------------------------------------------------
def _delivery_recorded(entry: dict, repeat: int) -> None:
    entry["alert_count"] = repeat
    entry["last_alert_ts"] = now()
    entry["pending_alert_id"] = None


def _confirmation_notice(product: dict, result: detect.Result, repeat: int,
                         confirmed_ok: bool) -> None:
    if confirmed_ok:
        screenshot = None
        if config.ALERT_SCREENSHOTS:
            try:
                import browser_detect
                screenshot = browser_detect.capture_buybox(product)
            except Exception:  # noqa: BLE001
                screenshot = None
        alert = build_alert(product, result, repeat, screenshot, confirmed_result=True)
        alert.urgent = False
        alert.title = f"✅ STILL AVAILABLE • {product['name']}"
        alert.body = (f"## {product.get('product_name', product['name'])}\n"
                      f"A second check confirms it is still buyable at **{product['name']}**.\n\n"
                      f"[**Open {product['name']} now →**]({product['url']})")
        notify.send_reliable(
            alert, f"confirm:{product['key']}:{int(now())}",
            {"event_type": "followup"},
        )
        return

    correction = notify.Alert(
        title=f"⚠️ SIGNAL ENDED • {product['name']}",
        body=(f"## {product.get('product_name', product['name'])}\n"
              "The availability signal disappeared on the verification check. "
              "The first alert was intentionally immediate; the item may have sold out."),
        url=product["url"], urgent=False, kind="health",
        group=product.get("group", "console"),
    )
    notify.send_reliable(
        correction, f"correction:{product['key']}:{int(now())}",
        {"event_type": "followup"},
    )


def process_result(product: dict, state: dict, result: detect.Result,
                   latency: float, debug: bool = False) -> None:
    current = now()
    log_check(product, result)

    with STATE_LOCK:
        entry = entry_for(state, product["key"])
        previous = entry["status"]
        entry["last_reading"] = result.status.value
        entry["last_check_ts"] = current
        entry["last_latency_seconds"] = round(latency, 2)
        entry["next_check_ts"] = current + int(product.get("interval", config.CHECK_INTERVAL))

    group_mark = ("ITEM" if product.get("custom") else
                  "CTRL" if product.get("group") == "controller" else "CONS")
    item_name = product.get("product_name", product["name"])
    line = (f"  {CONSOLE_MARK[result.status]:<7} [{group_mark}] {product['name']} — "
            f"{item_name}  {result.status.value} ({latency:.1f}s)")
    if result.price:
        line += f"  {result.price}"
    print(line, flush=True)
    if not debug and result.status in detect.UNUSABLE:
        print(f"      why: {result.reason[:160]}", flush=True)
    if debug:
        print(f"      source : {result.source}")
        print(f"      reason : {result.reason}")
        print(f"      http   : {result.http_status}   body: {result.body_len} bytes")
        print(f"      last   : {previous}")

    if result.status in detect.UNUSABLE:
        with STATE_LOCK:
            if entry["bad_since"] is None:
                entry["bad_since"] = current
            maybe_health_alert(product, entry, result)
            save_state(state)
        return

    with STATE_LOCK:
        entry["bad_since"] = None
        was_alertable = previous in {s.value for s in detect.ALERTABLE}
        due = current - entry["last_alert_ts"] >= config.REALERT_MINUTES * 60
        first_time = result.alertable and not was_alertable
        should_alert = result.alertable and (
            first_time or (due and entry["alert_count"] < config.MAX_REALERTS)
        ) and not notify.has_pending(entry.get("pending_alert_id"))
        repeat = 1 if first_time else entry["alert_count"] + 1

        if result.status.value != previous:
            entry["since"] = current
        entry["status"] = result.status.value
        entry["last_good_ts"] = current
        if not result.alertable and was_alertable:
            print("    (was buyable, now gone -- resetting alert counter)", flush=True)
            entry["alert_count"] = 0
            if entry.get("pending_alert_id"):
                notify.cancel_pending(entry["pending_alert_id"])
            entry["pending_alert_id"] = None
        save_state(state)

    if not should_alert:
        return

    # Speed wins here: deliver the strong first signal before confirmation or
    # screenshot work. Both happen afterward and cannot delay the phone alert.
    alert_id = f"stock:{product['key']}:{int(current)}:{repeat}"
    alert = build_alert(product, result, repeat)
    results, delivered = notify.send_reliable(
        alert, alert_id,
        {"event_type": "stock", "product_key": product["key"], "repeat": repeat},
    )
    with STATE_LOCK:
        if delivered:
            _delivery_recorded(entry, repeat)
        else:
            entry["pending_alert_id"] = alert_id
        save_state(state)
    print(f"    >>> IMMEDIATE ALERT DELIVERY: {results}", flush=True)

    confirmed_ok = confirmed(product, result)
    if not confirmed_ok and not delivered:
        notify.cancel_pending(alert_id)
        with STATE_LOCK:
            entry["pending_alert_id"] = None
            save_state(state)
        return
    if delivered and config.CONFIRM_BEFORE_ALERT:
        _confirmation_notice(product, result, repeat, confirmed_ok)


def run_product_once(product: dict, state: dict, debug: bool = False) -> None:
    started = time.monotonic()
    result = detect.check(product)
    process_result(product, state, result, time.monotonic() - started, debug)


def run_pass(state: dict, debug: bool = False) -> None:
    enabled = [product for product in config.PRODUCTS if product.get("enabled", True)]
    for index, product in enumerate(enabled):
        if index and config.STAGGER:
            time.sleep(config.STAGGER + random.uniform(0, 2))
        run_product_once(product, state, debug)
    with STATE_LOCK:
        check_feeds(state)
        maybe_heartbeat(state)
        save_state(state)


def _worker(product: dict, state: dict, stop: threading.Event, debug: bool) -> None:
    interval = max(10, int(product.get("interval", config.CHECK_INTERVAL)))
    try:
        while not stop.is_set():
            cycle_started = time.monotonic()
            try:
                run_product_once(product, state, debug)
            except Exception as exc:  # noqa: BLE001 - one worker must never kill the service
                result = detect.Result(Status.ERROR,
                                       f"worker exception: {type(exc).__name__}: {exc}", "worker")
                process_result(product, state, result, time.monotonic() - cycle_started, debug)
            elapsed = time.monotonic() - cycle_started
            delay = max(1.0, interval - elapsed) + random.uniform(0, min(3, config.JITTER))
            stop.wait(delay)
    finally:
        try:
            import browser_detect
            browser_detect.close_thread_session()
        except Exception:  # noqa: BLE001
            pass


def _status_alert(state: dict, group: str = "console") -> notify.Alert:
    fields = []
    current = now()
    enabled = [p for p in config.PRODUCTS
               if p.get("enabled", True) and p.get("group", "console") == group]
    status_icon = {
        Status.IN_STOCK.value: "🟢",
        Status.PREORDER.value: "🟡",
        Status.OUT_OF_STOCK.value: "⚫",
        Status.BLOCKED.value: "🟠",
        Status.ERROR.value: "🔴",
        Status.UNKNOWN.value: "❔",
    }
    for product in enabled:
        entry = entry_for(state, product["key"])
        reading = entry.get("last_reading") or "starting"
        checked = entry.get("last_check_ts") or 0
        age = int(current - checked) if checked else None
        latency = entry.get("last_latency_seconds")
        value = f"{status_icon.get(reading, '⏳')} **{reading.replace('_', ' ').upper()}**"
        if age is not None:
            value += f"\nChecked {age}s ago"
        if latency is not None:
            value += f" · took {latency}s"
        fields.append((product["name"], value, False))
    disabled = [p["name"] for p in config.PRODUCTS
                if not p.get("enabled", True) and p.get("group", "console") == group]
    label = "Controller" if group == "controller" else "Console"
    item_icon = "🎮" if group == "controller" else "🖥️"
    body = f"## {item_icon} {label} monitor is LIVE\nWatching {len(enabled)} independent retailer workers."
    if disabled:
        body += "\n\n**Disabled:** " + ", ".join(disabled)
    return notify.Alert(
        title=f"🛰️ {label} stock — live status", body=body, urgent=False, kind="heartbeat",
        fields=fields, group=group,
        footer=f"Updated {stamp()} · refreshes every {config.STATUS_UPDATE_SECONDS}s",
    )


def _write_runtime_heartbeat() -> None:
    """Atomically publish local liveness without waiting on any network call."""
    payload = {
        "timestamp": now(),
        "pid": os.getpid(),
        "enabled": [p["key"] for p in config.PRODUCTS if p.get("enabled", True)],
    }
    tmp = config.RUNTIME_HEARTBEAT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp, config.RUNTIME_HEARTBEAT_FILE)


def _heartbeat_worker(stop: threading.Event) -> None:
    """Keep the dead-man heartbeat independent of Discord and retailer I/O."""
    next_external_ping = 0.0
    while not stop.is_set():
        try:
            _write_runtime_heartbeat()
        except OSError as exc:
            print(f"  local heartbeat write failed: {exc}", flush=True)

        current = time.monotonic()
        if config.HEALTHCHECK_URL and current >= next_external_ping:
            try:
                requests.get(config.HEALTHCHECK_URL, timeout=5)
            except requests.RequestException:
                pass
            next_external_ping = current + 60
        stop.wait(config.RUNTIME_HEARTBEAT_SECONDS)


def _apply_delivery_events(state: dict, events: list[dict]) -> None:
    for event in events:
        kind = event.get("event_type")
        if kind == "stock":
            entry = entry_for(state, event["product_key"])
            _delivery_recorded(entry, int(event.get("repeat", 1)))
        elif kind == "health":
            entry_for(state, event["product_key"])["last_health_alert_ts"] = now()
        elif kind == "heartbeat":
            group = event.get("group", "console")
            state.setdefault("last_heartbeat_dates", {})[group] = event.get("date")


def run_workers(state: dict, duration: int, debug: bool = False) -> int:
    enabled = [product for product in config.PRODUCTS if product.get("enabled", True)]
    if not enabled:
        print("No retailers enabled. Set ENABLE_TARGET=1 or another ENABLE_* value.")
        return 1

    stop = threading.Event()
    threads = [
        threading.Thread(target=_worker, args=(product, state, stop, debug),
                         name=f"retailer-{product['key']}", daemon=True)
        for product in enabled
    ]
    for thread in threads:
        thread.start()
    heartbeat_thread = threading.Thread(
        target=_heartbeat_worker, args=(stop,), name="runtime-heartbeat", daemon=True
    )
    heartbeat_thread.start()

    deadline = time.monotonic() + duration if duration else None
    next_status = 0.0
    groups = sorted({product.get("group", "console") for product in enabled})
    with STATE_LOCK:
        status_ids = state.setdefault("discord_status_message_ids", {})
        if state.get("discord_status_message_id") and "console" not in status_ids:
            status_ids["console"] = state["discord_status_message_id"]
    try:
        while not deadline or time.monotonic() < deadline:
            events = notify.retry_outbox()
            with STATE_LOCK:
                _apply_delivery_events(state, events)
                save_state(state)

            if time.monotonic() >= next_status:
                for group in groups:
                    with STATE_LOCK:
                        status_alert = _status_alert(state, group)
                        status_id = state["discord_status_message_ids"].get(group)
                    try:
                        updated_id = notify.update_discord_status(status_alert, status_id)
                        with STATE_LOCK:
                            state["discord_status_message_ids"][group] = updated_id
                            if group == "console":
                                state["discord_status_message_id"] = updated_id
                            save_state(state)
                    except Exception as exc:  # noqa: BLE001
                        print(f"  {group} status update failed: {type(exc).__name__}: {exc}",
                              flush=True)
                next_status = time.monotonic() + config.STATUS_UPDATE_SECONDS

            # Daily routine messaging runs on the coordinator, never in a
            # retailer worker. It may be slow without delaying stock checks.
            maybe_heartbeat(state)
            stop.wait(1)
    except KeyboardInterrupt:
        print("\nStopping retailer workers...", flush=True)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=10)
        heartbeat_thread.join(timeout=10)
        with STATE_LOCK:
            save_state(state)
    return 0


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
    tests = [
        notify.Alert(
            title="✅ Console alerts are connected",
            body=("## 🖥️ Console stock monitor\n"
                  "This channel is receiving notifications correctly.\n\n"
                  "_Test only — this is not a stock signal._"),
            url="https://www.nintendo.com/us/store/products/nintendo-switch-2-the-legend-of-zelda-40th-anniversary-edition-121642/",
            urgent=False,
            kind="test",
            group="console",
        ),
        notify.Alert(
            title="✅ Controller alerts are connected",
            body=("## 🎮 Zelda controller stock monitor\n"
                  "This channel is receiving notifications correctly.\n\n"
                  "_Test only — this is not a stock signal._"),
            url="https://www.nintendo.com/us/store/products/nintendo-switch-2-pro-controller-the-legend-of-zelda-40th-anniversary-edition-127074/",
            urgent=False,
            kind="test",
            group="controller",
        ),
    ]
    combined = {}
    for alert in tests:
        results = notify.send(alert)
        for name, value in results.items():
            combined[f"{alert.group}:{name}"] = value
    ok = all(value == "ok" for value in combined.values())
    for name, value in combined.items():
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
    parser.add_argument("--service-log", metavar="PATH", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.service_log:
        os.environ["SERVICE_LOG_FILE"] = os.path.abspath(args.service_log)
    _configure_service_logging()

    if args.test_alert:
        return cmd_test_alert()
    if args.status:
        return cmd_status()
    if args.probe:
        return cmd_probe(args.probe)

    if args.loop and not _claim_single_instance():
        print("Another looping Zelda stock monitor is already running; exiting.")
        return 3
    if args.loop:
        _prevent_macos_sleep()

    channels = notify.configured_channels()
    print(f"[{stamp()}] notification channels: {', '.join(channels) or 'NONE CONFIGURED'}")
    for catalog_error in getattr(config, "CUSTOM_PRODUCT_ERRORS", []):
        print(f"  custom catalog warning: {catalog_error}", flush=True)
    if not channels:
        print("  !! Nothing will reach you. Set DISCORD_WEBHOOK_URL at minimum.")

    state = load_state()

    if not args.loop:
        run_pass(state, debug=args.debug)
        return 0

    # --interval remains a convenient global override for local testing.
    if args.interval != config.CHECK_INTERVAL:
        for product in config.PRODUCTS:
            if product.get("enabled", True):
                product["interval"] = args.interval
    return run_workers(state, args.duration, debug=args.debug)


if __name__ == "__main__":
    sys.exit(main())
