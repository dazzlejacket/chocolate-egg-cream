"""A small window: the current song, recent history, and quick lookups.

The engine runs in its own thread with its own asyncio loop, so the
window stays responsive while a sample is being captured and recognised.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import sys
import threading
import tkinter as tk
import urllib.parse
import webbrowser
from tkinter import ttk

from . import credentials, settings as settings_module
from .app import EggCream
from .recognize import Recognition

log = logging.getLogger(__name__)

BACKGROUND = "#fbf7f2"
SURFACE = "#f3ece3"
ACCENT = "#7a4a2b"
TEXT = "#2b2019"
MUTED = "#7b6d62"
ERROR_TEXT = "#a33327"


def _log_callback_failure(exc: BaseException) -> None:
    """A deferred callback must never take the window down with it."""
    log.warning("Deferred GUI callback failed: %s: %s", type(exc).__name__, str(exc)[:160])


def _ui_font() -> str:
    if sys.platform == "darwin":
        return "Helvetica Neue"
    if sys.platform == "win32":
        return "Segoe UI"
    return "Sans"


FONT = _ui_font()


def format_time(seconds: float) -> str:
    minutes, rest = divmod(int(max(0, seconds)), 60)
    return f"{minutes}:{rest:02d}"


def build_song_urls(track: Recognition) -> dict[str, str]:
    """Build helpful language-neutral lookup URLs for a recognised song."""
    full_q = urllib.parse.quote_plus(f"{track.artist} {track.title}")
    lyrics_q = urllib.parse.quote_plus(f"{track.artist} {track.title} lyrics")
    return {
        "lyrics": f"https://www.google.com/search?q={lyrics_q}",
        "youtube": f"https://www.youtube.com/results?search_query={full_q}",
    }


def schedule_after(widget, callback, *args) -> None:
    """Run `callback(*args)` on the Tk thread on the next tick.

    Arguments are bound *now*, by value. Scheduling `lambda: callback(exc)`
    instead would capture the `exc` name of an `except` block, which Python
    unbinds when the block ends -- the callback then raised `NameError` and
    the real Last.fm error was never shown.

    When the widget owns a deferred queue (see `SetupDialog`), the callable is
    queued and drained by the Tk thread itself, so a worker thread never calls
    into Tk directly.
    """
    deferred = getattr(widget, "_deferred", None)
    if deferred is not None:
        deferred.put((callback, args))
        return
    try:
        widget.after(0, callback, *args)
    except (RuntimeError, tk.TclError):
        # The window is already gone; nothing left to report to.
        pass


class SetupDialog(tk.Toplevel):
    """Set the Last.fm and ListenBrainz details without editing JSON."""

    def __init__(self, parent, settings: dict, on_save) -> None:
        super().__init__(parent)
        self.settings = settings
        self.on_save = on_save
        self._pending_token: str | None = None
        # Worker threads never touch Tk directly: they hand us callables here
        # and the Tk thread drains the queue.
        self._deferred: queue.Queue = queue.Queue()

        self.title("Chocolate Egg Cream — Setup")
        self.configure(background=BACKGROUND)
        self.resizable(False, False)
        self.transient(parent)

        enabled = set(self.settings.get("enabled_services", []))
        self.use_lastfm = tk.BooleanVar(value="lastfm" in enabled)
        self.use_lb = tk.BooleanVar(value="listenbrainz" in enabled)
        self.message = tk.StringVar(value="")

        frame = tk.Frame(self, background=BACKGROUND, padx=18, pady=14)
        frame.grid(row=0, column=0, sticky="nsew")

        # --- Last.fm ---
        lastfm_header = tk.Frame(frame, background=BACKGROUND)
        lastfm_header.grid(row=0, column=0, columnspan=2, sticky="ew")
        tk.Checkbutton(
            lastfm_header,
            text="LAST.FM",
            variable=self.use_lastfm,
            background=BACKGROUND,
            activebackground=BACKGROUND,
            foreground=ACCENT,
            font=(FONT, 9, "bold"),
        ).pack(side="left")
        ttk.Button(
            lastfm_header,
            text="Get API key",
            command=lambda: webbrowser.open("https://www.last.fm/api/account/create"),
        ).pack(side="right")

        self.lastfm_key = self._entry(frame, 1, "API key", self.settings.get("lastfm_api_key", ""))
        self.lastfm_secret = self._entry(
            frame, 2, "API secret", self.settings.get("lastfm_api_secret", ""), secret=True
        )

        auth_row = tk.Frame(frame, background=BACKGROUND)
        auth_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.auth_step1_btn = ttk.Button(
            auth_row, text="1. Open Last.fm", command=self._start_lastfm_auth
        )
        self.auth_step1_btn.pack(side="left", padx=(0, 6))
        self.auth_step2_btn = ttk.Button(
            auth_row,
            text="2. Finish auth",
            command=self._finish_lastfm_auth,
            state="disabled",
        )
        self.auth_step2_btn.pack(side="left")

        # --- ListenBrainz ---
        lb_header = tk.Frame(frame, background=BACKGROUND)
        lb_header.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        tk.Checkbutton(
            lb_header,
            text="LISTENBRAINZ",
            variable=self.use_lb,
            background=BACKGROUND,
            activebackground=BACKGROUND,
            foreground=ACCENT,
            font=(FONT, 9, "bold"),
        ).pack(side="left")
        ttk.Button(
            lb_header,
            text="Get user token",
            command=lambda: webbrowser.open("https://listenbrainz.org/settings/"),
        ).pack(side="right")

        self.lb_token = self._entry(
            frame, 5, "User token", self.settings.get("listenbrainz_token", ""), secret=True
        )

        self._show_secrets = tk.BooleanVar(value=False)
        tk.Checkbutton(
            frame,
            text="Show secrets",
            variable=self._show_secrets,
            command=self._toggle_secrets,
            background=BACKGROUND,
            activebackground=BACKGROUND,
            foreground=MUTED,
            font=(FONT, 8),
        ).grid(row=6, column=1, sticky="w", padx=(10, 0), pady=(2, 0))

        tk.Label(
            frame,
            text=(
                "Privacy: short in-memory system audio samples are sent to Shazam "
                "for recognition and are never saved to disk. No microphone or screen "
                "capture is used."
            ),
            background=BACKGROUND,
            foreground=MUTED,
            font=(FONT, 8),
            wraplength=360,
            justify="left",
            anchor="w",
        ).grid(row=7, column=0, columnspan=2, sticky="ew", pady=(8, 4))

        tk.Label(
            frame,
            textvariable=self.message,
            background=BACKGROUND,
            foreground=MUTED,
            font=(FONT, 9),
            wraplength=360,
            justify="left",
            anchor="w",
        ).grid(row=8, column=0, columnspan=2, sticky="ew", pady=(4, 8))

        buttons = tk.Frame(frame, background=BACKGROUND)
        buttons.grid(row=9, column=0, columnspan=2, sticky="ew")

        ttk.Button(buttons, text="Save", command=self._save).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side="left")

        self._drain()

    # ------------------------------------------------------- thread bridge

    def _drain(self) -> None:
        """Run everything the worker threads scheduled, on the Tk thread."""
        try:
            while True:
                callback, args = self._deferred.get_nowait()
                callback(*args)
        except queue.Empty:
            pass
        except Exception as exc:  # noqa: BLE001 - keep the dialog responsive
            _log_callback_failure(exc)
        try:
            if self.winfo_exists():
                self.after(100, self._drain)
        except (tk.TclError, RuntimeError):
            pass

    def _entry(self, parent, row: int, label: str, value: str, *, secret: bool = False) -> tk.Entry:
        tk.Label(
            parent,
            text=label,
            background=BACKGROUND,
            foreground=TEXT,
            font=(FONT, 9),
            anchor="w",
        ).grid(row=row, column=0, sticky="w", pady=3)
        entry = tk.Entry(parent, font=(FONT, 9), width=34, show="•" if secret else "")
        entry.insert(0, value)
        entry.grid(row=row, column=1, sticky="ew", padx=(10, 0), pady=3)
        return entry

    def _toggle_secrets(self) -> None:
        mask = "" if self._show_secrets.get() else "•"
        self.lastfm_secret.configure(show=mask)
        self.lb_token.configure(show=mask)

    def _collect_enabled(self) -> list[str]:
        enabled = []
        if self.use_lastfm.get():
            enabled.append("lastfm")
        if self.use_lb.get():
            enabled.append("listenbrainz")
        return enabled

    def _save(self) -> None:
        labels = {
            "lastfm_api_key": "Last.fm API key",
            "lastfm_api_secret": "Last.fm API secret",
            "listenbrainz_token": "ListenBrainz token",
        }
        new_values = {
            "lastfm_api_key": credentials.normalize_secret(self.lastfm_key.get()),
            "lastfm_api_secret": credentials.normalize_secret(self.lastfm_secret.get()),
            "listenbrainz_token": credentials.normalize_secret(self.lb_token.get()),
        }

        problems: list[str] = []
        for key, value in new_values.items():
            problem = credentials.secret_problem(value)
            if problem:
                # Never store a masked placeholder or a corrupted paste: it
                # used to end up in an Authorization header and crash.
                problems.append(f"{labels[key]} {problem}")
                new_values[key] = ""

        # Emptying a field actively forgets the stored credential; a plain
        # save with an empty value must never wipe it behind the user's back.
        for key in credentials.SECRET_KEYS:
            if not new_values[key] and self.settings.get(key):
                settings_module.clear_secret(key)

        self.settings.update(new_values)
        self.settings["enabled_services"] = self._collect_enabled()

        # Show what was actually stored.
        self.lastfm_key.delete(0, "end")
        self.lastfm_key.insert(0, new_values["lastfm_api_key"])
        self.lastfm_secret.delete(0, "end")
        self.lastfm_secret.insert(0, new_values["lastfm_api_secret"])
        self.lb_token.delete(0, "end")
        self.lb_token.insert(0, new_values["listenbrainz_token"])

        self.on_save(self.settings)
        if problems:
            self.message.set(
                "Saved, but not used: " + "; ".join(problems)
                + ". Paste the real value and save again."
            )
        else:
            self.message.set("Saved! Changes apply right away.")

    def _start_lastfm_auth(self) -> None:
        from .scrobble.lastfm import LastFM

        api_key = self.lastfm_key.get().strip()
        api_secret = self.lastfm_secret.get().strip()
        if not api_key or not api_secret:
            self.message.set("Enter your Last.fm API key and secret first.")
            return

        self.auth_step1_btn.configure(state="disabled")
        self.message.set("Contacting Last.fm…")

        def worker() -> None:
            try:
                lastfm = LastFM(api_key=api_key, api_secret=api_secret)
                token = lastfm.fetch_token()
                url = lastfm.auth_url(token)
            except Exception as exc:  # noqa: BLE001
                # Bind the message now: `exc` is unbound once this block ends.
                schedule_after(self, self._on_auth_step1_error, str(exc))
                return
            schedule_after(self, self._on_auth_step1_ready, token, url)

        threading.Thread(target=worker, daemon=True, name="lastfm-auth-1").start()

    def _on_auth_step1_error(self, error: str) -> None:
        self.auth_step1_btn.configure(state="normal")
        self.message.set(f"Could not start Last.fm auth: {error}")

    def _on_auth_step1_ready(self, token: str, url: str) -> None:
        self._pending_token = token
        self.auth_step1_btn.configure(state="normal")
        self.auth_step2_btn.configure(state="normal")
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.clipboard_clear()
            self.clipboard_append(url)
        except Exception:  # noqa: BLE001
            pass
        self.message.set(
            "Last.fm opened in your browser. Click 'Yes, allow access' there, "
            "then come back here and click '2. Finish auth'."
        )

    def _finish_lastfm_auth(self) -> None:
        from .scrobble.lastfm import LastFM

        if not self._pending_token:
            self.message.set("Click '1. Open Last.fm' first.")
            return

        api_key = self.lastfm_key.get().strip()
        api_secret = self.lastfm_secret.get().strip()
        token = self._pending_token
        self.auth_step2_btn.configure(state="disabled")
        self.message.set("Finishing authorisation…")

        def worker() -> None:
            try:
                lastfm = LastFM(api_key=api_key, api_secret=api_secret)
                session_key = lastfm.exchange_token(token)
            except Exception as exc:  # noqa: BLE001
                # Bind the message now: `exc` is unbound once this block ends.
                schedule_after(self, self._on_auth_step2_error, str(exc))
                return
            schedule_after(self, self._on_auth_step2_done, api_key, api_secret, session_key)

        threading.Thread(target=worker, daemon=True, name="lastfm-auth-2").start()

    def _on_auth_step2_error(self, error: str) -> None:
        self.auth_step2_btn.configure(state="normal")
        self.message.set(
            f"Last.fm hasn't approved yet ({error}). Make sure you clicked Allow in the browser, then try again."
        )

    def _on_auth_step2_done(self, api_key: str, api_secret: str, session_key: str) -> None:
        self._pending_token = None
        self.use_lastfm.set(True)
        self.settings["enabled_services"] = self._collect_enabled()
        self.settings["lastfm_api_key"] = api_key
        self.settings["lastfm_api_secret"] = api_secret
        self.settings["lastfm_session_key"] = session_key
        self.on_save(self.settings)
        self.message.set("Last.fm is authorised and ready to scrobble!")


class EggCreamWindow:
    """The whole GUI: compact, lightweight, and distraction-free."""

    def __init__(self, settings: dict) -> None:
        self.settings = settings
        self.events: queue.Queue = queue.Queue()
        self.engine: EggCream | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._closing = False
        self._shut_down = False
        self._error_message: str | None = None
        self._history: list[Recognition] = []
        self._selected_track: Recognition | None = None
        self._scrobble_count = 0
        self._poll_id: str | None = None

        self.root = tk.Tk()
        self.root.title("Chocolate Egg Cream")
        self.root.configure(background=BACKGROUND)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.song_var = tk.StringVar(value="Press Start, then play your radio")
        self.status_var = tk.StringVar(value="stopped")
        self.hint_var = tk.StringVar(value="")
        self.services_var = tk.StringVar(value=self._services_text())

        self._build()
        self._poll_id = self.root.after(150, self._poll)

    # ------------------------------------------------------------- layout

    def _build(self) -> None:
        outer = tk.Frame(self.root, background=BACKGROUND, padx=18, pady=14)
        outer.grid(row=0, column=0, sticky="nsew")

        style = ttk.Style()
        if sys.platform == "win32":
            style.theme_use("clam")

        header = tk.Frame(outer, background=BACKGROUND)
        header.grid(row=0, column=0, sticky="ew")
        tk.Label(
            header,
            text="CHOCOLATE EGG CREAM",
            background=BACKGROUND,
            foreground=ACCENT,
            font=(FONT, 9, "bold"),
        ).pack(side="left")

        self.song_label = tk.Label(
            outer,
            textvariable=self.song_var,
            background=BACKGROUND,
            foreground=TEXT,
            font=(FONT, 14, "bold"),
            justify="left",
            wraplength=420,
            anchor="w",
        )
        self.song_label.grid(row=1, column=0, sticky="ew", pady=(8, 2))

        self.status_label = tk.Label(
            outer,
            textvariable=self.status_var,
            background=BACKGROUND,
            foreground=MUTED,
            font=(FONT, 9),
            justify="left",
            wraplength=420,
            anchor="w",
        )
        self.status_label.grid(row=2, column=0, sticky="ew")

        # Main controls
        buttons = tk.Frame(outer, background=BACKGROUND)
        buttons.grid(row=3, column=0, sticky="ew", pady=(12, 8))

        self.start_button = ttk.Button(
            buttons, text="Start", width=10, command=self.start
        )
        self.start_button.pack(side="left", padx=(0, 6))

        self.stop_button = ttk.Button(
            buttons, text="Stop", width=10, command=self.stop, state="disabled"
        )
        self.stop_button.pack(side="left", padx=(0, 6))

        ttk.Button(buttons, text="Setup…", command=self.open_setup).pack(side="right")

        # Quick lookup actions for the current or selected song
        actions = tk.Frame(outer, background=BACKGROUND)
        actions.grid(row=4, column=0, sticky="ew", pady=(2, 8))

        self.copy_button = ttk.Button(
            actions, text="Copy", command=self._copy_track, state="disabled"
        )
        self.copy_button.pack(side="left", padx=(0, 5))

        self.lyrics_button = ttk.Button(
            actions,
            text="Lyrics",
            command=lambda: self._open_track_url("lyrics"),
            state="disabled",
        )
        self.lyrics_button.pack(side="left", padx=(0, 5))

        self.youtube_button = ttk.Button(
            actions,
            text="YouTube",
            command=lambda: self._open_track_url("youtube"),
            state="disabled",
        )
        self.youtube_button.pack(side="left")

        # Recent songs list
        tk.Label(
            outer,
            text="RECENT SONGS (click any to select)",
            background=BACKGROUND,
            foreground=MUTED,
            font=(FONT, 8, "bold"),
            anchor="w",
        ).grid(row=5, column=0, sticky="ew", pady=(6, 3))

        list_frame = tk.Frame(outer, background=SURFACE, bd=1, relief="solid")
        list_frame.grid(row=6, column=0, sticky="ew")

        self.history_list = tk.Listbox(
            list_frame,
            height=5,
            width=52,
            background=SURFACE,
            foreground=TEXT,
            selectbackground=ACCENT,
            selectforeground="#ffffff",
            borderwidth=0,
            highlightthickness=0,
            font=(FONT, 9),
            activestyle="none",
        )
        self.history_list.pack(side="left", fill="both", expand=True, padx=4, pady=3)
        self.history_list.bind("<<ListboxSelect>>", self._on_history_select)

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.history_list.yview)
        scrollbar.pack(side="right", fill="y")
        self.history_list.configure(yscrollcommand=scrollbar.set)

        # Footer: scrobble services & brief feedback
        footer = tk.Frame(outer, background=BACKGROUND)
        footer.grid(row=7, column=0, sticky="ew", pady=(8, 0))
        tk.Label(
            footer,
            textvariable=self.services_var,
            background=BACKGROUND,
            foreground=MUTED,
            font=(FONT, 8),
            anchor="w",
        ).pack(side="left")
        tk.Label(
            footer,
            textvariable=self.hint_var,
            background=BACKGROUND,
            foreground=ACCENT,
            font=(FONT, 8),
            anchor="e",
        ).pack(side="right")

    def open_setup(self) -> None:
        """Open the setup window."""
        SetupDialog(self.root, self.settings, self._save_settings)

    def _save_settings(self, settings: dict) -> None:
        settings_module.save(settings)
        if self.engine is not None and self.loop is not None:
            try:
                self.loop.call_soon_threadsafe(self.engine.reload_settings, settings)
            except RuntimeError:
                pass
        self.services_var.set(self._services_text())

    def _services_text(self) -> str:
        enabled = self.settings.get("enabled_services", [])
        parts = []
        if "lastfm" in enabled:
            keys = ("lastfm_api_key", "lastfm_api_secret", "lastfm_session_key")
            ready = all(
                credentials.normalize_secret(self.settings.get(key))
                and not credentials.secret_problem(self.settings.get(key))
                for key in keys
            )
            parts.append("Last.fm " + ("ready" if ready else "needs setup"))
        if "listenbrainz" in enabled:
            token = self.settings.get("listenbrainz_token")
            ready = bool(credentials.normalize_secret(token)) and not credentials.secret_problem(
                token
            )
            parts.append("ListenBrainz " + ("ready" if ready else "needs setup"))
        base = " · ".join(parts) if parts else "scrobbling off (just identifying)"
        if self._scrobble_count:
            base += f" · {self._scrobble_count} scrobbled"
        return base

    # ------------------------------------------------------------- song actions

    def _active_track(self) -> Recognition | None:
        if self._selected_track is not None:
            return self._selected_track
        if self.engine is not None and self.engine.current is not None:
            return self.engine.current
        if self._history:
            return self._history[0]
        return None

    def _set_song_actions_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for btn in (
            self.copy_button,
            self.lyrics_button,
            self.youtube_button,
        ):
            btn.configure(state=state)

    def _on_history_select(self, _event=None) -> None:
        selection = self.history_list.curselection()
        if not selection:
            return
        idx = selection[0]
        if 0 <= idx < len(self._history):
            self._selected_track = self._history[idx]
            self._set_song_actions_enabled(True)
            self._flash_hint(f"Selected: {self._selected_track.title[:28]}")

    def _copy_track(self) -> None:
        track = self._active_track()
        if track is None:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(track.label)
            self._flash_hint("Copied!")
        except Exception:  # noqa: BLE001
            pass

    def _open_track_url(self, kind: str) -> None:
        track = self._active_track()
        if track is None:
            return
        url = build_song_urls(track).get(kind)
        if url:
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001
                pass

    def _flash_hint(self, text: str) -> None:
        self.hint_var.set(text)
        self.root.after(2500, lambda: self.hint_var.set("") if self.hint_var.get() == text else None)

    # ------------------------------------------------------------- engine

    def _engine_thread(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.engine = EggCream(self.settings)
            self.engine.on_event = self._queue_event
        except Exception as exc:  # noqa: BLE001
            self._queue_event("error", str(exc))
            self._ready.set()
            return
        self._ready.set()
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()

    def _queue_event(self, kind: str, payload) -> None:
        self.events.put((kind, payload))

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
            future = asyncio.run_coroutine_threadsafe(coro, self.loop)
            future.add_done_callback(self._on_future_done)
        except RuntimeError:
            pass

    def _on_future_done(self, future) -> None:
        try:
            future.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            # `EggCream.start()` already emits an error event, but catch any
            # unexpected exception so it is never silently swallowed.
            self._queue_event("error", str(exc))

    # ------------------------------------------------------------ actions

    def start(self) -> None:
        if self.engine is not None and self.engine.running:
            return
        self._error_message = None
        self.status_label.configure(foreground=MUTED)
        self._start_engine_thread()
        if self.engine is None:
            return
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("starting system audio capture…")
        self._call_engine(self.engine.start())

    def stop(self) -> None:
        if self.engine is None or not self.engine.running:
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self.status_var.set("stopped")
            return
        self._call_engine(self.engine.stop())
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set("stopped")

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._poll_id is not None:
            try:
                self.root.after_cancel(self._poll_id)
            except Exception:  # noqa: BLE001
                pass
            self._poll_id = None
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass
        self._shutdown_engine()

    def _shutdown_engine(self) -> None:
        if self._shut_down:
            return
        self._shut_down = True
        if self.engine is not None and self.loop is not None:
            try:
                # Long enough for the worst case of two scrobblers timing out
                # on Stop/Quit, so the window never leaves an engine behind.
                asyncio.run_coroutine_threadsafe(self.engine.shutdown(), self.loop).result(
                    timeout=40
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Engine shutdown did not finish cleanly: %s", type(exc).__name__)
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
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle(kind, payload)

        self._refresh_status()
        if not self._closing:
            self._poll_id = self.root.after(150, self._poll)

    def _handle(self, kind: str, payload) -> None:
        if kind == "now_playing" and payload is not None:
            self._error_message = None
            self.status_label.configure(foreground=MUTED)
            self.song_var.set(payload.label)
            self._selected_track = payload
            self._set_song_actions_enabled(True)
            if not self._history or self._history[0].identity != payload.identity:
                self._history.insert(0, payload)
                del self._history[25:]
                self.history_list.insert(0, payload.label)
                if self.history_list.size() > 25:
                    self.history_list.delete(25, "end")
            self.history_list.selection_clear(0, "end")
            self.history_list.selection_set(0)
        elif kind == "scrobbled" and payload is not None:
            self._scrobble_count += 1
            self.services_var.set(self._services_text())
            self._flash_hint(f"Scrobbled {payload.title[:24]}")
        elif kind == "started":
            self._error_message = None
            self.status_label.configure(foreground=MUTED)
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
            self.status_var.set("listening for music…")
        elif kind == "stopped":
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            if not self._error_message:
                self.status_var.set("stopped")
        elif kind == "error":
            detail = str(payload) if payload else "Something went wrong — see the log."
            self._error_message = detail
            self.status_label.configure(foreground=ERROR_TEXT)
            self.status_var.set(detail)
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")

    def _refresh_status(self) -> None:
        if self._error_message:
            return
        engine = self.engine
        if engine is None or not engine.running:
            return

        track = engine.current
        candidate = engine.candidate
        backoff = engine.recognizer_backoff
        if track is None:
            if backoff > 0:
                self.status_var.set(
                    f"recognition unavailable · retrying in {int(backoff) + 1}s"
                )
            elif candidate is not None:
                self.status_var.set(f"hearing: {candidate.label} (confirming…)")
            elif engine.heard_audio is False:
                self.status_var.set("listening · quiet (play your radio!)")
            else:
                self.status_var.set("listening…")
            return

        base = f"listening · {format_time(engine.playtime)}"
        if backoff > 0:
            self.status_var.set(f"{base} · recognition retrying in {int(backoff) + 1}s")
        elif candidate is not None and candidate.identity != track.identity:
            self.status_var.set(f"{base} · next: {candidate.label} (confirming…)")
        elif engine.heard_audio is False:
            self.status_var.set(f"{base} · quiet")
        else:
            self.status_var.set(base)

    # --------------------------------------------------------------- main

    def run(self) -> None:
        self.root.mainloop()
        self._shutdown_engine()


def run(settings: dict) -> int:
    """Open the window."""
    window = EggCreamWindow(settings)
    window.run()
    return 0
