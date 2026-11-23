"""A small window: the current song, with Start and Stop.

The engine runs in its own thread with its own asyncio loop, so the
window stays responsive while a sample is being captured and recognised.
"""

from __future__ import annotations

import asyncio
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import ttk

from . import settings as settings_module
from .app import EggCream

BACKGROUND = "#fbf7f2"
ACCENT = "#7a4a2b"
TEXT = "#2b2019"
MUTED = "#8b7d72"


def format_time(seconds: float) -> str:
    minutes, rest = divmod(int(max(0, seconds)), 60)
    return f"{minutes}:{rest:02d}"


class SetupDialog(tk.Toplevel):
    """Set the Last.fm and ListenBrainz details without editing JSON."""

    def __init__(self, parent, settings: dict, on_save) -> None:
        super().__init__(parent)
        self.settings = settings
        self.on_save = on_save
        self.title("Chocolate Egg Cream - setup")
        self.configure(background=BACKGROUND)
        self.resizable(False, False)
        self.transient(parent)

        self.message = tk.StringVar(value="")

        frame = tk.Frame(self, background=BACKGROUND, padx=18, pady=14)
        frame.grid(row=0, column=0, sticky="nsew")

        tk.Label(
            frame,
            text="LAST.FM",
            background=BACKGROUND,
            foreground=ACCENT,
            font=("Segoe UI", 9, "bold"),
        ).grid(row=0, column=0, columnspan=2, sticky="w")

        self.lastfm_key = self._entry(frame, 1, "API key", self.settings.get("lastfm_api_key", ""))
        self.lastfm_secret = self._entry(
            frame, 2, "API secret", self.settings.get("lastfm_api_secret", "")
        )
        self.lastfm_token = self._entry(frame, 3, "Authorisation token", "")

        tk.Label(
            frame,
            text="LISTENBRAINZ",
            background=BACKGROUND,
            foreground=ACCENT,
            font=("Segoe UI", 9, "bold"),
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(14, 0))

        self.lb_token = self._entry(
            frame, 5, "User token", self.settings.get("listenbrainz_token", "")
        )

        tk.Label(
            frame,
            textvariable=self.message,
            background=BACKGROUND,
            foreground=MUTED,
            font=("Segoe UI", 8),
            wraplength=340,
            justify="left",
            anchor="w",
        ).grid(row=6, column=0, columnspan=2, sticky="ew", pady=(10, 6))

        buttons = tk.Frame(frame, background=BACKGROUND)
        buttons.grid(row=7, column=0, columnspan=2, sticky="ew")

        ttk.Button(
            buttons, text="Authorise Last.fm", command=self._authorise_lastfm
        ).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(buttons, text="Save", command=self._save).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(buttons, text="Close", command=self.destroy).grid(row=0, column=2)

    def _entry(self, parent, row: int, label: str, value: str) -> tk.Entry:
        tk.Label(
            parent,
            text=label,
            background=BACKGROUND,
            foreground=TEXT,
            font=("Segoe UI", 9),
            anchor="w",
        ).grid(row=row, column=0, sticky="w", pady=3)
        entry = tk.Entry(parent, font=("Segoe UI", 9), width=34)
        entry.insert(0, value)
        entry.grid(row=row, column=1, sticky="ew", padx=(10, 0), pady=3)
        return entry

    def _save(self) -> None:
        self.settings["lastfm_api_key"] = self.lastfm_key.get().strip()
        self.settings["lastfm_api_secret"] = self.lastfm_secret.get().strip()
        self.settings["listenbrainz_token"] = self.lb_token.get().strip()
        self.on_save(self.settings)
        self.message.set("Saved.")

    def _authorise_lastfm(self) -> None:
        from .scrobble.lastfm import LastFM

        api_key = self.lastfm_key.get().strip()
        api_secret = self.lastfm_secret.get().strip()
        if not api_key or not api_secret:
            self.message.set("An API key and secret are needed first.")
            return

        lastfm = LastFM(api_key=api_key, api_secret=api_secret)
        url = lastfm.auth_url()
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
        self.clipboard_clear()
        self.clipboard_append(url)
        self.message.set(
            "The page opened in your browser and the link was copied. Allow the "
            "app there, then paste the token it shows you below and press "
            "Authorise again."
        )

        token = self.lastfm_token.get().strip()
        if not token:
            return

        try:
            session_key = lastfm.exchange_token(token)
        except Exception as exc:  # noqa: BLE001
            self.message.set(f"Last.fm refused the token: {exc}")
            return

        self.settings["lastfm_api_key"] = api_key
        self.settings["lastfm_api_secret"] = api_secret
        self.settings["lastfm_session_key"] = session_key
        self.on_save(self.settings)
        self.lastfm_token.delete(0, "end")
        self.message.set("Last.fm is authorised.")


class EggCreamWindow:
    """The whole GUI. Small on purpose."""

    def __init__(self, settings: dict) -> None:
        self.settings = settings
        self.events: queue.Queue = queue.Queue()
        self.engine: EggCream | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._closing = False
        self._shut_down = False
        self._song = ""

        self.root = tk.Tk()
        self.root.title("Chocolate Egg Cream")
        self.root.configure(background=BACKGROUND)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.song_var = tk.StringVar(value="nothing playing yet")
        self.status_var = tk.StringVar(value="stopped")
        self.services_var = tk.StringVar(value=self._services_text())

        self._build()
        self.root.after(150, self._poll)

    # ------------------------------------------------------------- layout

    def _build(self) -> None:
        outer = tk.Frame(self.root, background=BACKGROUND, padx=18, pady=14)
        outer.grid(row=0, column=0, sticky="nsew")

        tk.Label(
            outer,
            text="CHOCOLATE EGG CREAM",
            background=BACKGROUND,
            foreground=MUTED,
            font=("Segoe UI", 8, "bold"),
        ).grid(row=0, column=0, sticky="w")

        self.song_label = tk.Label(
            outer,
            textvariable=self.song_var,
            background=BACKGROUND,
            foreground=TEXT,
            font=("Segoe UI", 13, "bold"),
            justify="left",
            wraplength=380,
            anchor="w",
        )
        self.song_label.grid(row=1, column=0, sticky="ew", pady=(8, 2))

        self.status_label = tk.Label(
            outer,
            textvariable=self.status_var,
            background=BACKGROUND,
            foreground=MUTED,
            font=("Segoe UI", 9),
            anchor="w",
        )
        self.status_label.grid(row=2, column=0, sticky="ew")

        buttons = tk.Frame(outer, background=BACKGROUND)
        buttons.grid(row=3, column=0, sticky="ew", pady=(12, 10))

        style = ttk.Style()
        if sys.platform == "win32":
            # The default Windows theme draws flat grey buttons.
            style.theme_use("clam")

        self.start_button = ttk.Button(
            buttons, text="Start", width=10, command=self.start
        )
        self.start_button.grid(row=0, column=0, padx=(0, 6))

        self.stop_button = ttk.Button(
            buttons, text="Stop", width=10, command=self.stop, state="disabled"
        )
        self.stop_button.grid(row=0, column=1)

        tk.Label(
            outer,
            textvariable=self.services_var,
            background=BACKGROUND,
            foreground=MUTED,
            font=("Segoe UI", 8),
            anchor="w",
        ).grid(row=4, column=0, sticky="ew")

        ttk.Button(outer, text="Setup…", command=self.open_setup).grid(
            row=5, column=0, sticky="w", pady=(10, 0)
        )

    def open_setup(self) -> None:
        """Open the setup window."""
        SetupDialog(self.root, self.settings, self._save_settings)

    def _save_settings(self, settings: dict) -> None:
        settings_module.save(settings)
        self.services_var.set(self._services_text())

    def _services_text(self) -> str:
        enabled = self.settings.get("enabled_services", [])
        parts = []
        if "lastfm" in enabled:
            ready = all(
                self.settings.get(key)
                for key in ("lastfm_api_key", "lastfm_api_secret", "lastfm_session_key")
            )
            parts.append("Last.fm " + ("ready" if ready else "needs setup"))
        if "listenbrainz" in enabled:
            ready = bool(self.settings.get("listenbrainz_token"))
            parts.append("ListenBrainz " + ("ready" if ready else "needs setup"))
        return " · ".join(parts) if parts else "no scrobbling service enabled"

    # ------------------------------------------------------------- engine

    def _engine_thread(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.engine = EggCream(self.settings)
        self.engine.on_event = self._queue_event
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()

    def _queue_event(self, kind: str, track) -> None:
        self.events.put((kind, track))

    def _start_engine_thread(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._engine_thread, daemon=True)
        self.thread.start()
        self._ready.wait(timeout=10)

    def _call_engine(self, coro) -> None:
        if self.loop is None or self.engine is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, self.loop)
        except RuntimeError:
            pass

    # ------------------------------------------------------------ actions

    def start(self) -> None:
        if self.engine is not None and self.engine.running:
            return
        self._start_engine_thread()
        self._call_engine(self.engine.start())
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("capturing system audio")

    def stop(self) -> None:
        if self.engine is None or not self.engine.running:
            return
        self._call_engine(self.engine.stop())
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set("stopped")

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.root.destroy()
        self._shutdown_engine()

    def _shutdown_engine(self) -> None:
        if self._shut_down:
            return
        self._shut_down = True
        if self.engine is not None and self.loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(self.engine.shutdown(), self.loop).result(
                    timeout=20
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                self.loop.call_soon_threadsafe(self.loop.stop)
            except RuntimeError:
                pass
        if self.thread is not None:
            self.thread.join(timeout=5)

    # -------------------------------------------------------------- events

    def _poll(self) -> None:
        while True:
            try:
                kind, track = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle(kind, track)

        self._refresh_status()
        if not self._closing:
            self.root.after(150, self._poll)

    def _handle(self, kind: str, track) -> None:
        if kind == "now_playing" and track is not None:
            self._song = track.label
            self.song_var.set(track.label)
        elif kind == "started":
            self.status_var.set("capturing system audio")
        elif kind == "stopped":
            self.status_var.set("stopped")
        elif kind == "error":
            self.status_var.set("something went wrong, see the log")

    def _refresh_status(self) -> None:
        engine = self.engine
        if engine is None or not engine.running:
            return
        track = engine.current
        if track is None:
            self.status_var.set("listening…")
            return
        self.status_var.set(f"listening · {format_time(engine.playtime)}")

    # --------------------------------------------------------------- main

    def run(self) -> None:
        self.root.mainloop()
        self._shutdown_engine()


def run(settings: dict) -> int:
    """Open the window."""
    window = EggCreamWindow(settings)
    window.run()
    return 0
