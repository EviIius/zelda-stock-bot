#!/bin/bash
set -Eeuo pipefail

LABEL="com.eviiius.zelda-stock-monitor"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PLIST_PATH="/Library/LaunchDaemons/${LABEL}.plist"
VENV_PYTHON="${REPO_DIR}/.venv/bin/python"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This helper must be run on macOS."
    exit 1
fi

status() {
    if launchctl print "system/${LABEL}" >/dev/null 2>&1; then
        echo "launchd service: loaded"
    else
        echo "launchd service: not loaded"
    fi
    if [[ -x "${VENV_PYTHON}" ]]; then
        "${VENV_PYTHON}" - "${REPO_DIR}/runtime_heartbeat.json" <<'PY'
import json
import os
import pathlib
import sys
import time

path = pathlib.Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
    pid = int(data["pid"])
    age = max(0, time.time() - float(data["timestamp"]))
    os.kill(pid, 0)
    print(f"monitor process: running (PID {pid})")
    print(f"heartbeat age: {age:.1f} seconds")
    raise SystemExit(0 if age < 120 else 2)
except FileNotFoundError:
    print("monitor process: no heartbeat file")
except ProcessLookupError:
    print("monitor process: heartbeat PID is not running")
except (KeyError, ValueError, json.JSONDecodeError) as exc:
    print(f"monitor process: invalid heartbeat ({type(exc).__name__})")
raise SystemExit(1)
PY
    else
        echo "monitor process: virtual environment not installed"
        return 1
    fi
}

case "${1:-status}" in
    status)
        status
        ;;
    start)
        [[ -f "${PLIST_PATH}" ]] || { echo "Run ./install_macos_service.sh first."; exit 1; }
        sudo launchctl bootstrap system "${PLIST_PATH}" >/dev/null 2>&1 || true
        sudo launchctl enable "system/${LABEL}"
        sudo launchctl kickstart -k "system/${LABEL}"
        sleep 3
        status
        ;;
    restart)
        [[ -f "${PLIST_PATH}" ]] || { echo "Run ./install_macos_service.sh first."; exit 1; }
        sudo launchctl kickstart -k "system/${LABEL}"
        sleep 3
        status
        ;;
    stop)
        sudo launchctl bootout "system/${LABEL}" >/dev/null 2>&1 || true
        echo "Service stopped and unloaded. Use '$0 start' to load it again."
        ;;
    test)
        [[ -x "${VENV_PYTHON}" ]] || { echo "Run ./install_macos_service.sh first."; exit 1; }
        "${VENV_PYTHON}" "${REPO_DIR}/stock_monitor.py" --test-alert
        ;;
    logs)
        mkdir -p "${REPO_DIR}/logs"
        touch "${REPO_DIR}/logs/monitor.log" "${REPO_DIR}/logs/launchd.stdout.log" \
            "${REPO_DIR}/logs/launchd.stderr.log"
        tail -n 100 "${REPO_DIR}/logs/monitor.log" \
            "${REPO_DIR}/logs/launchd.stdout.log" "${REPO_DIR}/logs/launchd.stderr.log"
        ;;
    uninstall)
        sudo launchctl bootout "system/${LABEL}" >/dev/null 2>&1 || true
        sudo rm -f "${PLIST_PATH}"
        echo "Service removed. The repository, .env, logs, and virtual environment were preserved."
        ;;
    *)
        echo "Usage: $0 {status|start|restart|stop|test|logs|uninstall}"
        exit 2
        ;;
esac
