#!/bin/bash
set -Eeuo pipefail

LABEL="com.eviiius.zelda-stock-monitor"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PLIST_PATH="/Library/LaunchDaemons/${LABEL}.plist"
LOG_DIR="${REPO_DIR}/logs"
BROWSER_DIR="${REPO_DIR}/.playwright-browsers"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This installer must be run on macOS."
    exit 1
fi
if [[ "${EUID}" -eq 0 ]]; then
    echo "Run this script as your normal Mac user, without sudo."
    echo "It will request sudo only when installing the launchd service."
    exit 1
fi

CURRENT_USER="$(id -un)"
CURRENT_GROUP="$(id -gn)"
USER_HOME="${HOME}"

case "${REPO_DIR}/" in
    "${USER_HOME}/Documents/"*|"${USER_HOME}/Desktop/"*|"${USER_HOME}/Downloads/"*)
        echo "Move this repository out of Documents, Desktop, or Downloads first."
        echo "Recommended location: ${USER_HOME}/zelda-stock-bot"
        echo "macOS privacy controls can prevent a boot service from reading those folders."
        exit 1
        ;;
esac

find_python() {
    local candidate
    for candidate in "${PYTHON_BIN:-}" python3.14 python3.13 python3.12 python3.11 python3; do
        [[ -n "${candidate}" ]] || continue
        command -v "${candidate}" >/dev/null 2>&1 || continue
        if "${candidate}" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
            command -v "${candidate}"
            return 0
        fi
    done
    return 1
}

SYSTEM_PYTHON="$(find_python || true)"
if [[ -z "${SYSTEM_PYTHON}" ]]; then
    echo "Python 3.11 or newer is required."
    echo "Install it from https://www.python.org/downloads/macos/ or run: brew install python"
    echo "Then run this installer again."
    exit 1
fi

echo "Using ${SYSTEM_PYTHON} ($("${SYSTEM_PYTHON}" --version 2>&1))"
"${SYSTEM_PYTHON}" -m venv "${REPO_DIR}/.venv"
VENV_PYTHON="${REPO_DIR}/.venv/bin/python"

"${VENV_PYTHON}" -m pip install --upgrade pip
"${VENV_PYTHON}" -m pip install -r "${REPO_DIR}/requirements.txt" \
    -r "${REPO_DIR}/requirements-browser.txt"
mkdir -p "${BROWSER_DIR}" "${LOG_DIR}"
export PLAYWRIGHT_BROWSERS_PATH="${BROWSER_DIR}"
"${VENV_PYTHON}" -m playwright install chromium
"${VENV_PYTHON}" -m unittest discover -s "${REPO_DIR}" -p 'test_*.py'

webhooks_valid() {
    "${VENV_PYTHON}" - "${REPO_DIR}/.env" <<'PY'
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
if not path.exists():
    raise SystemExit(1)
values = {}
for raw in path.read_text(encoding="utf-8-sig").splitlines():
    if not raw.strip() or raw.lstrip().startswith("#") or "=" not in raw:
        continue
    key, value = raw.split("=", 1)
    values[key.strip()] = value.strip().strip('"').strip("'")
pattern = re.compile(r"^https://(?:discord(?:app)?\.com)/api/webhooks/\d+/[A-Za-z0-9_-]+$")
needed = ("DISCORD_WEBHOOK_URL", "CONTROLLER_DISCORD_WEBHOOK_URL")
raise SystemExit(0 if all(pattern.match(values.get(key, "")) for key in needed) else 1)
PY
}

if ! webhooks_valid; then
    echo
    echo "Enter the two Discord webhook URLs. Input is hidden and is saved only in .env."
    read -r -s -p "Console webhook URL: " CONSOLE_WEBHOOK
    echo
    read -r -s -p "Controller webhook URL: " CONTROLLER_WEBHOOK
    echo
    export ZSB_CONSOLE_WEBHOOK="${CONSOLE_WEBHOOK}"
    export ZSB_CONTROLLER_WEBHOOK="${CONTROLLER_WEBHOOK}"
    "${VENV_PYTHON}" - "${REPO_DIR}/.env" <<'PY'
import os
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
pattern = re.compile(r"^https://(?:discord(?:app)?\.com)/api/webhooks/\d+/[A-Za-z0-9_-]+$")
values = {
    "DISCORD_WEBHOOK_URL": os.environ.get("ZSB_CONSOLE_WEBHOOK", ""),
    "CONTROLLER_DISCORD_WEBHOOK_URL": os.environ.get("ZSB_CONTROLLER_WEBHOOK", ""),
}
if not all(pattern.match(value) for value in values.values()):
    print("One or both webhook URLs are not valid Discord webhook URLs.")
    raise SystemExit(2)
old = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
kept = [line for line in old if line.split("=", 1)[0].strip() not in values]
kept.extend(f"{key}={value}" for key, value in values.items())
path.write_text("\n".join(kept) + "\n", encoding="utf-8")
path.chmod(0o600)
PY
    unset CONSOLE_WEBHOOK CONTROLLER_WEBHOOK ZSB_CONSOLE_WEBHOOK ZSB_CONTROLLER_WEBHOOK
fi
chmod 600 "${REPO_DIR}/.env"

echo "Sending a test to both Discord channels before installing the service..."
"${VENV_PYTHON}" "${REPO_DIR}/stock_monitor.py" --test-alert

PLIST_TMP="$(mktemp -t zelda-stock-monitor.XXXXXX)"
trap 'rm -f "${PLIST_TMP}"' EXIT
export ZSB_PLIST_LABEL="${LABEL}"
export ZSB_REPO_DIR="${REPO_DIR}"
export ZSB_PYTHON="${VENV_PYTHON}"
export ZSB_USER="${CURRENT_USER}"
export ZSB_GROUP="${CURRENT_GROUP}"
export ZSB_HOME="${USER_HOME}"
export ZSB_BROWSER_DIR="${BROWSER_DIR}"
export ZSB_LOG_DIR="${LOG_DIR}"
"${VENV_PYTHON}" - "${PLIST_TMP}" <<'PY'
import os
import plistlib
import sys

payload = {
    "Label": os.environ["ZSB_PLIST_LABEL"],
    "ProgramArguments": [
        os.environ["ZSB_PYTHON"], "-u",
        os.path.join(os.environ["ZSB_REPO_DIR"], "stock_monitor.py"), "--loop",
    ],
    "WorkingDirectory": os.environ["ZSB_REPO_DIR"],
    "UserName": os.environ["ZSB_USER"],
    "GroupName": os.environ["ZSB_GROUP"],
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 10,
    "ProcessType": "Background",
    "EnvironmentVariables": {
        "HOME": os.environ["ZSB_HOME"],
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
        "PYTHONUTF8": "1",
        "PLAYWRIGHT_BROWSERS_PATH": os.environ["ZSB_BROWSER_DIR"],
        "SERVICE_LOG_FILE": os.path.join(os.environ["ZSB_LOG_DIR"], "monitor.log"),
        "SERVICE_LOG_MAX_BYTES": "5242880",
        "SERVICE_LOG_BACKUPS": "5",
    },
    "StandardOutPath": os.path.join(os.environ["ZSB_LOG_DIR"], "launchd.stdout.log"),
    "StandardErrorPath": os.path.join(os.environ["ZSB_LOG_DIR"], "launchd.stderr.log"),
}
with open(sys.argv[1], "wb") as handle:
    plistlib.dump(payload, handle, sort_keys=False)
PY
unset ZSB_PLIST_LABEL ZSB_REPO_DIR ZSB_PYTHON ZSB_USER ZSB_GROUP ZSB_HOME \
    ZSB_BROWSER_DIR ZSB_LOG_DIR

plutil -lint "${PLIST_TMP}"
sudo -v
sudo launchctl bootout "system/${LABEL}" >/dev/null 2>&1 || true
sudo install -o root -g wheel -m 0644 "${PLIST_TMP}" "${PLIST_PATH}"
sudo launchctl bootstrap system "${PLIST_PATH}"
sudo launchctl enable "system/${LABEL}"
sudo launchctl kickstart -k "system/${LABEL}"

echo "Waiting for a fresh service heartbeat..."
if ! "${VENV_PYTHON}" - "${REPO_DIR}/runtime_heartbeat.json" <<'PY'
import json
import os
import pathlib
import sys
import time

path = pathlib.Path(sys.argv[1])
deadline = time.time() + 45
while time.time() < deadline:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        age = time.time() - float(data["timestamp"])
        pid = int(data["pid"])
        os.kill(pid, 0)
        if age < 25:
            print(f"Monitor heartbeat is healthy (PID {pid}, {age:.1f}s old).")
            raise SystemExit(0)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        pass
    time.sleep(2)
print("The daemon loaded, but a healthy heartbeat was not observed within 45 seconds.")
raise SystemExit(1)
PY
then
    echo "Check ${LOG_DIR}/launchd.stderr.log for the startup error."
    exit 1
fi

echo
echo "Installed successfully. The monitor now starts at boot and restarts automatically."
echo "Status:    ${REPO_DIR}/macos_service.sh status"
echo "Logs:      ${REPO_DIR}/macos_service.sh logs"
echo "Restart:   ${REPO_DIR}/macos_service.sh restart"
echo "Test:      ${REPO_DIR}/macos_service.sh test"
echo "Uninstall: ${REPO_DIR}/macos_service.sh uninstall"
echo "Keep this Mac powered, connected to the internet, and—if it is a MacBook—open."
