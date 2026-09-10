"""Independent dead-man check for the local stock monitor."""

from __future__ import annotations

import argparse
import json
import os
import time

import config
import notify


def _load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: str, value: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2)
    os.replace(tmp, path)


def check(test_alert: bool = False) -> int:
    heartbeat = _load(config.RUNTIME_HEARTBEAT_FILE)
    state = _load(config.WATCHDOG_STATE_FILE)
    age = time.time() - float(heartbeat.get("timestamp", 0))
    stale = test_alert or not heartbeat or age > config.WATCHDOG_STALE_SECONDS

    if stale:
        if not test_alert and time.time() - float(state.get("last_alert_ts", 0)) < 600:
            return 1
        reason = "Watchdog test requested." if test_alert else (
            "No runtime heartbeat exists." if not heartbeat else
            f"The last runtime heartbeat is {int(age)} seconds old."
        )
        results = notify.send(
            notify.Alert(
                title=("✅ Zelda watchdog test" if test_alert
                       else "🚨 Zelda monitor heartbeat is stale"),
                body=(f"{reason}\n\nThe watchdog is alive, but the monitor may be stopped "
                      "or stalled. Windows will attempt to restart it automatically."),
                urgent=True, kind="health",
                group="all",
            )
        )
        delivered = any(value == "ok" for value in results.values())
        print(f"watchdog alert: {results}")
        if delivered and not test_alert:
            state.update({"last_alert_ts": time.time(), "was_stale": True})
            _save(config.WATCHDOG_STATE_FILE, state)
        return 0 if delivered else 2

    if state.get("was_stale"):
        results = notify.send(
            notify.Alert(
                title="✅ Zelda monitor recovered",
                body=f"Runtime heartbeat is current again ({int(max(age, 0))} seconds old).",
                urgent=False, kind="heartbeat",
                group="all",
            )
        )
        if any(value == "ok" for value in results.values()):
            state["was_stale"] = False
            _save(config.WATCHDOG_STATE_FILE, state)
    print(f"monitor heartbeat healthy: {int(max(age, 0))}s old")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-alert", action="store_true")
    args = parser.parse_args()
    return check(args.test_alert)


if __name__ == "__main__":
    raise SystemExit(main())
