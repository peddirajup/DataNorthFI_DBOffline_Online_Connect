"""Desktop launcher for the packaged local lineage app."""

from __future__ import annotations

import json
import socket
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from tkinter import ttk

import uvicorn

from server import app


HOST = "127.0.0.1"
PORT = 8765
APP_URL = f"http://{HOST}:{PORT}"
HEALTH_URL = f"{APP_URL}/api/health"


def has_sql_server_odbc_drivers(installed_drivers: list[str]) -> bool:
    return any(
        driver in installed_drivers
        for driver in ("ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server")
    )


def _installed_odbc_drivers() -> list[str]:
    try:
        import pyodbc
    except ImportError:
        return []
    return pyodbc.drivers()


class DesktopLauncher:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("Database & Offline Lineage")
        self.root.geometry("500x235")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        self.server: uvicorn.Server | None = None
        self.server_thread: threading.Thread | None = None
        self.owns_server = False
        self.browser_opened = False
        self.deadline = time.monotonic() + 20
        self.status = tk.StringVar(value="Starting the local service...")

        frame = ttk.Frame(self.root, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Database & Offline Lineage", font=("Segoe UI", 13, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text="The app runs locally on this computer. Your files and database credentials are not sent to a shared server.",
            wraplength=450,
            justify="left",
        ).pack(anchor="w", pady=(8, 12))

        driver_note = ""
        if not has_sql_server_odbc_drivers(_installed_odbc_drivers()):
            driver_note = "Live SQL Server needs Microsoft ODBC Driver 17 or 18. Snowflake needs network access and a metadata-access role/warehouse. Offline modes work without SQL Server's ODBC driver."
        else:
            driver_note = "SQL Server ODBC driver detected. Use a dedicated read-only account; Snowflake also needs a warehouse and metadata-access role."
        ttk.Label(frame, text=driver_note, wraplength=450, justify="left").pack(anchor="w", pady=(0, 10))
        ttk.Label(frame, textvariable=self.status, wraplength=450).pack(anchor="w", pady=(0, 12))

        buttons = ttk.Frame(frame)
        buttons.pack(anchor="e")
        self.open_button = ttk.Button(buttons, text="Open app", command=self._open_browser, state="disabled")
        self.open_button.pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Quit", command=self._quit).pack(side="left")

        self._start_service()

    def run(self) -> None:
        self.root.mainloop()

    @staticmethod
    def _port_is_open() -> bool:
        try:
            with socket.create_connection((HOST, PORT), timeout=0.25):
                return True
        except OSError:
            return False

    @staticmethod
    def _service_is_ready() -> bool:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=0.5) as response:
                return response.status == 200 and json.loads(response.read()).get("status") == "ready"
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return False

    def _start_service(self) -> None:
        if self._port_is_open():
            if self._service_is_ready():
                self.status.set("The local app is already running.")
                self.open_button.state(["!disabled"])
                self.root.after(250, self._open_browser)
            else:
                self.status.set("Port 8765 is already in use. Close the other app and try again.")
            return

        # In windowed/frozen builds, stdio may be None and Uvicorn's default
        # formatter can fail while probing stream capabilities.
        config = uvicorn.Config(
            app,
            host=HOST,
            port=PORT,
            log_level="error",
            access_log=False,
            log_config=None,
        )
        self.server = uvicorn.Server(config)
        self.server_thread = threading.Thread(target=self.server.run, name="lineage-api", daemon=True)
        self.owns_server = True
        self.server_thread.start()
        self.root.after(100, self._wait_until_ready)

    def _wait_until_ready(self) -> None:
        if self._service_is_ready():
            self.status.set("Ready. The app opened in your browser.")
            self.open_button.state(["!disabled"])
            if not self.browser_opened:
                self._open_browser()
            return
        if time.monotonic() >= self.deadline:
            self.status.set("The local service did not start. Quit and try again, or contact your support team.")
            return
        self.root.after(150, self._wait_until_ready)

    def _open_browser(self) -> None:
        self.browser_opened = True
        webbrowser.open(APP_URL)

    def _quit(self) -> None:
        if self.owns_server and self.server:
            self.server.should_exit = True
        if self.server_thread:
            self.server_thread.join(timeout=3)
        self.root.destroy()


if __name__ == "__main__":
    DesktopLauncher().run()