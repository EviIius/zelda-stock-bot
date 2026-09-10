"""Small Windows control panel for the Zelda stock monitor."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parent
LOG_FILE = ROOT / "logs" / "windows-monitor.log"
HEARTBEAT_FILE = ROOT / "runtime_heartbeat.json"
MONITOR_TASK = "Zelda Stock Monitor"
WATCHDOG_TASK = "Zelda Stock Monitor Watchdog"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _run_task_command(*arguments: str) -> subprocess.CompletedProcess[str]:
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "schtasks.exe"
    return subprocess.run(
        [str(executable), *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=20,
        check=False,
        creationflags=CREATE_NO_WINDOW,
    )


def _task_status(task_name: str) -> tuple[str, bool]:
    status_result = _run_task_command("/Query", "/TN", task_name, "/FO", "CSV", "/NH")
    if status_result.returncode != 0:
        return "Not installed", False
    try:
        row = next(csv.reader(status_result.stdout.splitlines()))
        state = row[2] if len(row) > 2 else "Unknown"
    except (StopIteration, csv.Error):
        state = "Unknown"

    enabled = True
    xml_result = _run_task_command("/Query", "/TN", task_name, "/XML")
    if xml_result.returncode == 0:
        try:
            root = ElementTree.fromstring(xml_result.stdout)
            node = root.find("{http://schemas.microsoft.com/windows/2004/02/mit/task}Settings/"
                             "{http://schemas.microsoft.com/windows/2004/02/mit/task}Enabled")
            enabled = node is None or (node.text or "true").lower() != "false"
        except ElementTree.ParseError:
            pass
    return state, enabled


def _heartbeat() -> tuple[int | None, float | None]:
    try:
        data = json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8"))
        return int(data["pid"]), max(0.0, time.time() - float(data["timestamp"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


def _change_task(task_name: str, enabled: bool) -> None:
    result = _run_task_command("/Change", "/TN", task_name,
                               "/ENABLE" if enabled else "/DISABLE")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(detail or f"Could not update {task_name}")


def start_bot() -> str:
    _change_task(MONITOR_TASK, True)
    _change_task(WATCHDOG_TASK, True)
    result = _run_task_command("/Run", "/TN", MONITOR_TASK)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or "Start failed")
    return "Bot started; monitor and watchdog are enabled."


def stop_bot() -> str:
    # Disable first so neither the watchdog nor the logon trigger can undo a
    # deliberate stop. Ending a task that is already idle is harmless.
    _change_task(WATCHDOG_TASK, False)
    _change_task(MONITOR_TASK, False)
    _run_task_command("/End", "/TN", WATCHDOG_TASK)
    _run_task_command("/End", "/TN", MONITOR_TASK)
    return "Bot stopped; monitor and watchdog are disabled."


def restart_bot() -> str:
    _change_task(MONITOR_TASK, True)
    _change_task(WATCHDOG_TASK, True)
    _run_task_command("/End", "/TN", MONITOR_TASK)
    time.sleep(0.7)
    result = _run_task_command("/Run", "/TN", MONITOR_TASK)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or "Restart failed")
    return "Bot restarted; monitor and watchdog are enabled."


def _retained_logs() -> list[Path]:
    backups: list[tuple[int, Path]] = []
    for path in LOG_FILE.parent.glob(f"{LOG_FILE.name}.*"):
        try:
            backups.append((int(path.suffix[1:]), path))
        except ValueError:
            continue
    return [path for _, path in sorted(backups, reverse=True)] + [LOG_FILE]


class StockBotPanel(tk.Tk):
    BG = "#10131a"
    PANEL = "#181d27"
    TEXT = "#e6edf3"
    MUTED = "#9099a8"

    def __init__(self) -> None:
        super().__init__()
        self.title("Zelda Stock Bot")
        self.geometry("980x620")
        self.minsize(760, 440)
        self.configure(bg=self.BG)
        self.log_offset = 0
        self.follow_feed = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Checking bot status…")
        self.heartbeat_var = tk.StringVar(value="Heartbeat: checking…")
        self._build_styles()
        self._build_ui()
        self._load_existing_feed()
        self.after(250, self._poll_feed)
        self.after(300, self._refresh_status)

    def _build_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Panel.TFrame", background=self.PANEL)
        style.configure("Title.TLabel", background=self.BG, foreground=self.TEXT,
                        font=("Segoe UI Semibold", 18))
        style.configure("Status.TLabel", background=self.PANEL, foreground=self.TEXT,
                        font=("Segoe UI Semibold", 11))
        style.configure("Muted.TLabel", background=self.PANEL, foreground=self.MUTED,
                        font=("Segoe UI", 9))
        style.configure("Action.TButton", font=("Segoe UI Semibold", 10), padding=(15, 9))
        style.configure("TCheckbutton", background=self.PANEL, foreground=self.TEXT)
        style.map("TCheckbutton", background=[("active", self.PANEL)])

    def _build_ui(self) -> None:
        header = ttk.Frame(self, style="Panel.TFrame", padding=(18, 14))
        header.pack(fill="x", padx=14, pady=(14, 8))
        ttk.Label(header, text="Zelda Stock Bot", style="Status.TLabel",
                  font=("Segoe UI Semibold", 15)).grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.status_var, style="Status.TLabel").grid(
            row=1, column=0, sticky="w", pady=(7, 0))
        ttk.Label(header, textvariable=self.heartbeat_var, style="Muted.TLabel").grid(
            row=2, column=0, sticky="w", pady=(2, 0))

        actions = ttk.Frame(header, style="Panel.TFrame")
        actions.grid(row=0, column=1, rowspan=3, sticky="e")
        self.start_button = ttk.Button(actions, text="Start Bot", style="Action.TButton",
                                       command=lambda: self._run_action("Starting", start_bot))
        self.start_button.pack(side="left", padx=4)
        self.restart_button = ttk.Button(actions, text="Restart", style="Action.TButton",
                                         command=lambda: self._run_action("Restarting", restart_bot))
        self.restart_button.pack(side="left", padx=4)
        self.stop_button = ttk.Button(actions, text="Stop Bot", style="Action.TButton",
                                      command=self._confirm_stop)
        self.stop_button.pack(side="left", padx=4)
        header.columnconfigure(0, weight=1)

        feed_header = ttk.Frame(self, style="Panel.TFrame", padding=(12, 8))
        feed_header.pack(fill="x", padx=14)
        ttk.Label(feed_header, text="Live retailer feed", style="Status.TLabel").pack(side="left")
        ttk.Button(feed_header, text="Open logs", command=self._open_logs).pack(side="right", padx=(6, 0))
        ttk.Button(feed_header, text="Clear view", command=lambda: self.feed.delete("1.0", "end")).pack(
            side="right", padx=(6, 0))
        ttk.Checkbutton(feed_header, text="Follow newest", variable=self.follow_feed).pack(side="right")

        feed_frame = tk.Frame(self, bg=self.PANEL)
        feed_frame.pack(fill="both", expand=True, padx=14, pady=(0, 8))
        scrollbar = ttk.Scrollbar(feed_frame)
        scrollbar.pack(side="right", fill="y")
        self.feed = tk.Text(
            feed_frame, bg="#090c11", fg="#d8dee9", insertbackground="white",
            selectbackground="#334155", relief="flat", wrap="none",
            font=("Cascadia Mono", 9), padx=10, pady=10,
            yscrollcommand=scrollbar.set,
        )
        self.feed.pack(fill="both", expand=True)
        scrollbar.config(command=self.feed.yview)
        for tag, color in {
            "stock": "#42d392", "preorder": "#ffd166", "out": "#a9b1bc",
            "blocked": "#ff9f43", "error": "#ff667a", "unknown": "#c792ea",
            "delivery": "#68a7ff", "system": "#7dd3fc",
        }.items():
            self.feed.tag_configure(tag, foreground=color)

        ttk.Label(
            self, text="Closing this window leaves the bot running. Use Stop Bot to disable it.",
            style="Title.TLabel", font=("Segoe UI", 9),
        ).pack(anchor="w", padx=18, pady=(0, 12))

    def _line_tag(self, line: str) -> str | None:
        if "[STOCK]" in line:
            return "stock"
        if "[PRE]" in line:
            return "preorder"
        if "[OUT]" in line:
            return "out"
        if "[BLOCK]" in line:
            return "blocked"
        if "[ERR]" in line:
            return "error"
        if "[?]" in line:
            return "unknown"
        if "delivery:" in line or "DELIVERY:" in line:
            return "delivery"
        if "notification channels:" in line:
            return "system"
        return None

    def _append(self, content: str) -> None:
        if not content:
            return
        for line in content.splitlines(keepends=True):
            self.feed.insert("end", line, self._line_tag(line))
        if self.follow_feed.get():
            self.feed.see("end")

    def _load_existing_feed(self) -> None:
        for path in _retained_logs():
            try:
                self._append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        try:
            self.log_offset = LOG_FILE.stat().st_size
        except OSError:
            self.log_offset = 0

    def _poll_feed(self) -> None:
        try:
            size = LOG_FILE.stat().st_size
            if size < self.log_offset:
                self.log_offset = 0
            if size > self.log_offset:
                with LOG_FILE.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(self.log_offset)
                    self._append(handle.read())
                    self.log_offset = handle.tell()
        except OSError:
            pass
        self.after(750, self._poll_feed)

    def _refresh_status(self) -> None:
        def read() -> None:
            try:
                monitor_state, monitor_enabled = _task_status(MONITOR_TASK)
                _, watchdog_enabled = _task_status(WATCHDOG_TASK)
                pid, age = _heartbeat()
                enabled_text = "enabled" if monitor_enabled else "disabled"
                watchdog_text = "armed" if watchdog_enabled else "off"
                status = f"Monitor: {monitor_state} ({enabled_text})  •  Watchdog: {watchdog_text}"
                heartbeat = (f"Heartbeat: PID {pid} • {age:.0f} seconds ago" if age is not None
                             else "Heartbeat: no data yet")
            except Exception as exc:  # keep the panel alive if Windows returns a transient error
                status, heartbeat = f"Status unavailable: {exc}", "Heartbeat: unknown"
            self.after(0, lambda: self._set_status(status, heartbeat))

        threading.Thread(target=read, daemon=True).start()
        self.after(2500, self._refresh_status)

    def _set_status(self, status: str, heartbeat: str) -> None:
        self.status_var.set(status)
        self.heartbeat_var.set(heartbeat)

    def _set_buttons(self, state: str) -> None:
        for button in (self.start_button, self.restart_button, self.stop_button):
            button.configure(state=state)

    def _run_action(self, label: str, action) -> None:
        self._set_buttons("disabled")
        self.status_var.set(f"{label}…")

        def worker() -> None:
            try:
                message = action()
                self.after(0, lambda: self._append(f"\n[UI] {message}\n"))
            except Exception as exc:
                self.after(0, lambda exc=exc: messagebox.showerror("Zelda Stock Bot", str(exc)))
            finally:
                self.after(0, lambda: self._set_buttons("normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _confirm_stop(self) -> None:
        if messagebox.askyesno(
            "Stop Zelda Stock Bot?",
            "This disables automatic monitoring and the watchdog until you press Start Bot.",
        ):
            self._run_action("Stopping", stop_bot)

    def _open_logs(self) -> None:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        os.startfile(LOG_FILE.parent)  # type: ignore[attr-defined]


def main() -> None:
    if os.name != "nt":
        raise SystemExit("The desktop control panel is currently available on Windows only.")
    StockBotPanel().mainloop()


if __name__ == "__main__":
    main()
