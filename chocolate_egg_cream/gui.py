"""Minimalist, compact, monochrome Light/Dark/System GUI for Chocolate Egg Cream.

The engine runs in its own thread with its own asyncio loop, so the window
stays responsive while samples are captured, recognised, and scrobbled.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import urllib.parse
import webbrowser
from pathlib import Path
from tkinter import filedialog, ttk

from . import (
    __version__,
    credentials,
    history as history_module,
    settings as settings_module,
    theme as theme_module,
)
from .app import EggCream
from .recognize import Recognition
from .scrobble import external as external_module
from .scrobble import lastfm_app
from .scrobble.lastfm import LastFM
from .scrobble.listenbrainz import normalize_submit_url

log = logging.getLogger(__name__)

# Backwards-compatible palette constants (light defaults)
BACKGROUND = theme_module.LIGHT.bg
SURFACE = theme_module.LIGHT.surface
ACCENT = theme_module.LIGHT.accent
TEXT = theme_module.LIGHT.fg
MUTED = theme_module.LIGHT.muted
ERROR_TEXT = theme_module.LIGHT.error


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


def _assets_dir() -> Path:
    """Locate bundled icon assets in both source and PyInstaller builds."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / "chocolate_egg_cream" / "assets"
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parent / "assets"


def _apply_app_icon(window: tk.Misc) -> tk.PhotoImage | None:
    """Apply the purple egg icon to the window title bar / taskbar."""
    assets = _assets_dir()
    ico = assets / "chocolate-egg-cream.ico"
    if sys.platform == "win32" and ico.is_file():
        try:
            window.iconbitmap(default=str(ico))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    png64 = assets / "icon-64.png"
    png32 = assets / "icon-32.png"
    for candidate in (png64, png32):
        if candidate.is_file():
            try:
                img = tk.PhotoImage(file=str(candidate))
                window.iconphoto(False, img)  # type: ignore[attr-defined]
                return img
            except Exception:  # noqa: BLE001
                pass
    return None


def _load_header_mark() -> tk.PhotoImage | None:
    """Load a crisp 16x16 header mark from the bundled 32x32 egg icon."""
    png32 = _assets_dir() / "icon-32.png"
    if not png32.is_file():
        return None
    try:
        full = tk.PhotoImage(file=str(png32))
        return full.subsample(2, 2)
    except Exception:  # noqa: BLE001
        return None


def _open_folder(path: Path) -> None:
    """Open a directory in the OS file manager."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not open folder %s: %s", path, exc)


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


# ============================================================ History Dialog


class HistoryDialog(tk.Toplevel):
    """Local listening history viewer, exporter (TXT/CSV/JSONL), and retry control."""

    def __init__(
        self,
        parent,
        palette: theme_module.Palette,
        on_retry_request=None,
    ) -> None:
        super().__init__(parent)
        self.palette = palette
        self._on_retry_request = on_retry_request
        self._deferred: queue.Queue = queue.Queue()
        self._icon_ref = _apply_app_icon(self)

        self.title("Listening History — Chocolate Egg Cream")
        self.geometry("760x420")
        self.minsize(640, 320)
        self.transient(parent)

        self.summary_var = tk.StringVar(value="")
        self.feedback_var = tk.StringVar(value="")

        self._build()
        self.apply_theme(palette)
        self.refresh()
        self._drain()

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        # Header + toolbar
        self.top_bar = tk.Frame(self, padx=14, pady=10)
        self.top_bar.grid(row=0, column=0, sticky="ew")

        self.title_lbl = tk.Label(
            self.top_bar, text="LISTENING HISTORY", font=(FONT, 9, "bold"), anchor="w"
        )
        self.title_lbl.pack(side="left")

        self.summary_lbl = tk.Label(
            self.top_bar, textvariable=self.summary_var, font=(FONT, 8), anchor="w"
        )
        self.summary_lbl.pack(side="left", padx=(10, 0))

        self.toolbar = tk.Frame(self, padx=14, pady=6)
        self.toolbar.grid(row=1, column=0, sticky="ew")

        ttk.Button(
            self.toolbar,
            text="Export TXT",
            style="Compact.TButton",
            command=lambda: self._export("txt"),
        ).pack(side="left", padx=(0, 5))
        ttk.Button(
            self.toolbar,
            text="Export CSV",
            style="Compact.TButton",
            command=lambda: self._export("csv"),
        ).pack(side="left", padx=(0, 5))
        ttk.Button(
            self.toolbar,
            text="Export JSONL",
            style="Compact.TButton",
            command=lambda: self._export("jsonl"),
        ).pack(side="left", padx=(0, 10))

        self.retry_btn = ttk.Button(
            self.toolbar,
            text="Retry Failed",
            style="Compact.TButton",
            command=self._retry_failed,
            state="disabled",
        )
        self.retry_btn.pack(side="left", padx=(0, 5))

        ttk.Button(
            self.toolbar,
            text="Open Folder",
            style="Compact.TButton",
            command=lambda: _open_folder(settings_module.config_dir()),
        ).pack(side="right")
        ttk.Button(
            self.toolbar,
            text="Refresh",
            style="Compact.TButton",
            command=self.refresh,
        ).pack(side="right", padx=(0, 5))

        # Table container
        self.table_wrap = tk.Frame(self, padx=14, pady=4)
        self.table_wrap.grid(row=2, column=0, sticky="nsew")
        self.table_wrap.columnconfigure(0, weight=1)
        self.table_wrap.rowconfigure(0, weight=1)

        self.table_border = tk.Frame(self.table_wrap, bd=1, relief="solid")
        self.table_border.grid(row=0, column=0, sticky="nsew")
        self.table_border.columnconfigure(0, weight=1)
        self.table_border.rowconfigure(0, weight=1)

        cols = ("time", "artist", "title", "album", "duration", "status")
        self.tree = ttk.Treeview(self.table_border, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("time", text="TIME")
        self.tree.heading("artist", text="ARTIST")
        self.tree.heading("title", text="TITLE")
        self.tree.heading("album", text="ALBUM")
        self.tree.heading("duration", text="LEN")
        self.tree.heading("status", text="SERVICES")

        self.tree.column("time", width=124, minwidth=110, stretch=False)
        self.tree.column("artist", width=140, minwidth=90)
        self.tree.column("title", width=170, minwidth=100)
        self.tree.column("album", width=120, minwidth=70)
        self.tree.column("duration", width=46, minwidth=40, stretch=False, anchor="e")
        self.tree.column("status", width=120, minwidth=90)

        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(self.table_border, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)

        # Footer
        self.footer = tk.Frame(self, padx=14, pady=8)
        self.footer.grid(row=3, column=0, sticky="ew")
        self.feedback_lbl = tk.Label(
            self.footer, textvariable=self.feedback_var, font=(FONT, 8), anchor="w"
        )
        self.feedback_lbl.pack(side="left")
        ttk.Button(
            self.footer, text="Close", style="Compact.TButton", command=self.destroy
        ).pack(side="right")

    def apply_theme(self, p: theme_module.Palette) -> None:
        self.palette = p
        self.configure(background=p.bg)
        for frame in (self.top_bar, self.toolbar, self.table_wrap, self.footer):
            frame.configure(background=p.bg)
        self.table_border.configure(background=p.border, highlightbackground=p.border)
        self.title_lbl.configure(background=p.bg, foreground=p.fg)
        self.summary_lbl.configure(background=p.bg, foreground=p.muted)
        self.feedback_lbl.configure(background=p.bg, foreground=p.muted)
        theme_module.apply_window_chrome(self, p)

    def _drain(self) -> None:
        try:
            while True:
                callback, args = self._deferred.get_nowait()
                callback(*args)
        except queue.Empty:
            pass
        except Exception as exc:  # noqa: BLE001
            _log_callback_failure(exc)
        try:
            if self.winfo_exists():
                self.after(120, self._drain)
        except (tk.TclError, RuntimeError):
            pass

    @staticmethod
    def _format_services(services: dict) -> str:
        if not services:
            return "local only"
        parts = []
        labels = {"lastfm": "LFM", "listenbrainz": "LB", "external": "Ext"}
        for key, short in labels.items():
            entry = services.get(key)
            if not entry:
                continue
            st = entry.get("status") if isinstance(entry, dict) else str(entry)
            if st == "submitted":
                parts.append(f"{short}: ok")
            elif st == "published":
                parts.append(f"{short}: sent")
            elif st == "failed":
                parts.append(f"{short}: failed")
            elif st == "skipped_duplicate":
                parts.append(f"{short}: dup")
            elif st:
                parts.append(f"{short}: {st}")
        return ", ".join(parts) if parts else "local only"

    def refresh(self) -> None:
        for row_id in self.tree.get_children():
            self.tree.delete(row_id)

        records = history_module.load_history(newest_first=True)
        pending = history_module.load_pending()

        for rec in records:
            ts = int(rec.get("timestamp") or 0)
            when = (
                _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                if ts
                else ""
            )
            dur = rec.get("duration")
            dur_str = format_time(float(dur)) if dur else ""
            status_str = self._format_services(rec.get("services") or {})
            self.tree.insert(
                "",
                "end",
                values=(
                    when,
                    rec.get("artist", ""),
                    rec.get("title", ""),
                    rec.get("album") or "",
                    dur_str,
                    status_str,
                ),
            )

        n_saved = len(records)
        n_pend = len(pending)
        summary = f"{n_saved} saved listen" + ("" if n_saved == 1 else "s")
        if n_pend:
            summary += f" · {n_pend} pending retry"
        self.summary_var.set(summary)
        self.retry_btn.configure(
            text=f"Retry Failed ({n_pend})" if n_pend else "Retry Failed",
            state="normal" if n_pend else "disabled",
        )

    def _export(self, fmt: str) -> None:
        ext_map = {
            "txt": ("Text file", "*.txt"),
            "csv": ("CSV spreadsheet", "*.csv"),
            "jsonl": ("JSON Lines", "*.jsonl"),
        }
        label, pattern = ext_map[fmt]
        default_name = f"listening-history.{fmt}"
        chosen = filedialog.asksaveasfilename(
            parent=self,
            title=f"Export Listening History ({fmt.upper()})",
            initialfile=default_name,
            defaultextension=f".{fmt}",
            filetypes=[(label, pattern), ("All files", "*.*")],
        )
        if not chosen:
            return
        try:
            count = history_module.export_history(Path(chosen), fmt=fmt)
            self.feedback_var.set(f"Exported {count} listens to {Path(chosen).name}")
        except Exception as exc:  # noqa: BLE001
            self.feedback_var.set(f"Export failed: {exc}")

    def _retry_failed(self) -> None:
        if not self._on_retry_request:
            return
        self.retry_btn.configure(state="disabled")
        self.feedback_var.set("Retrying failed scrobbles…")

        def done(succeeded: int, remaining: int) -> None:
            self.refresh()
            if succeeded:
                self.feedback_var.set(
                    f"Retried: {succeeded} submitted, {remaining} remaining."
                )
            else:
                self.feedback_var.set(
                    f"No pending scrobbles succeeded ({remaining} remaining)."
                )

        self._on_retry_request(
            lambda s, r: schedule_after(self, done, s, r)
        )


# ============================================================== Setup Dialog


class SetupDialog(tk.Toplevel):
    """Compact settings dialog: Last.fm, ListenBrainz, External mode, Theme."""

    def __init__(
        self,
        parent,
        settings: dict,
        on_save,
        palette: theme_module.Palette | None = None,
        on_theme_change=None,
        lastfm_stale_error: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.on_save = on_save
        self.on_theme_change = on_theme_change
        self.palette = palette or theme_module.resolve_palette(settings.get("theme"))
        self._lastfm_stale_error = lastfm_stale_error
        self._pending_token: str | None = None
        # Worker threads never touch Tk directly: they hand us callables here
        # and the Tk thread drains the queue.
        self._deferred: queue.Queue = queue.Queue()
        self._icon_ref = _apply_app_icon(self)

        self.title("Setup — Chocolate Egg Cream")
        self.resizable(False, False)
        self.transient(parent)

        enabled = set(self.settings.get("enabled_services", []))
        self.use_lastfm = tk.BooleanVar(value="lastfm" in enabled)
        self.use_lb = tk.BooleanVar(value="listenbrainz" in enabled)
        self.use_custom_lfm = tk.BooleanVar(
            value=bool(self.settings.get("lastfm_use_custom_credentials", False))
        )
        ext_supported, self._ext_note = external_module.platform_support()
        self._ext_supported = ext_supported
        self.use_external = tk.BooleanVar(
            value=bool(self.settings.get("external_scrobbler", False)) and ext_supported
        )
        self.theme_var = tk.StringVar(
            value=str(self.settings.get("theme", "system")).capitalize()
        )
        self.message = tk.StringVar(value="")
        self.lfm_status_var = tk.StringVar(value="")
        self._show_secrets = tk.BooleanVar(value=False)
        self._advanced_open = (
            not lastfm_app.has_bundled_credentials()
            or bool(self.settings.get("lastfm_use_custom_credentials", False))
        )

        self._themed_frames: list[tk.Widget] = []
        self._themed_labels: list[tuple[tk.Label, str]] = []
        self._themed_checks: list[tuple[tk.Checkbutton, str]] = []
        self._themed_entries: list[tk.Entry] = []
        self._dividers: list[tk.Frame] = []

        self._build()
        self._refresh_lfm_status()
        self.apply_theme(self.palette)
        self._drain()

    def _frame(self, parent, **kw) -> tk.Frame:
        f = tk.Frame(parent, **kw)
        self._themed_frames.append(f)
        return f

    def _label(self, parent, role: str = "fg", **kw) -> tk.Label:
        lbl = tk.Label(parent, **kw)
        self._themed_labels.append((lbl, role))
        return lbl

    def _check(self, parent, role: str = "fg", **kw) -> tk.Checkbutton:
        cb = tk.Checkbutton(parent, **kw)
        self._themed_checks.append((cb, role))
        return cb

    def _divider(self, parent, row: int) -> None:
        line = tk.Frame(parent, height=1)
        line.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(10, 8))
        self._dividers.append(line)

    def _build(self) -> None:
        frame = self._frame(self, padx=18, pady=14)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)

        # ---- Last.fm ----
        lastfm_header = self._frame(frame)
        lastfm_header.grid(row=0, column=0, columnspan=2, sticky="ew")
        self._check(
            lastfm_header,
            role="fg",
            text="LAST.FM",
            variable=self.use_lastfm,
            font=(FONT, 9, "bold"),
        ).pack(side="left")

        self.lfm_status_lbl = self._label(
            lastfm_header,
            role="muted",
            textvariable=self.lfm_status_var,
            font=(FONT, 8),
            anchor="e",
        )
        self.lfm_status_lbl.pack(side="right")

        auth_row = self._frame(frame)
        auth_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.auth_step1_btn = ttk.Button(
            auth_row,
            text="1. Authorize Last.fm",
            style="Primary.TButton",
            command=self._start_lastfm_auth,
        )
        self.auth_step1_btn.pack(side="left", padx=(0, 6))
        self.auth_step2_btn = ttk.Button(
            auth_row,
            text="2. Finish auth",
            command=self._finish_lastfm_auth,
            state="disabled",
        )
        self.auth_step2_btn.pack(side="left", padx=(0, 6))
        self.verify_lfm_btn = ttk.Button(
            auth_row,
            text="Verify",
            style="Compact.TButton",
            command=self._verify_lastfm_session,
        )
        self.verify_lfm_btn.pack(side="left", padx=(0, 4))
        self.disconnect_lfm_btn = ttk.Button(
            auth_row,
            text="Disconnect",
            style="Compact.TButton",
            command=self._disconnect_lastfm,
        )
        self.disconnect_lfm_btn.pack(side="right")

        # Expandable Advanced (custom Last.fm API credentials)
        adv_toggle_row = self._frame(frame)
        adv_toggle_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.adv_toggle_btn = ttk.Button(
            adv_toggle_row,
            text="",
            style="Compact.TButton",
            command=self._toggle_advanced,
        )
        self.adv_toggle_btn.pack(side="left")

        self.adv_frame = self._frame(frame)
        self.adv_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.adv_frame.columnconfigure(1, weight=1)

        adv_top = self._frame(self.adv_frame)
        adv_top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 2))
        self._check(
            adv_top,
            role="muted",
            text="Use custom Last.fm API credentials",
            variable=self.use_custom_lfm,
            font=(FONT, 8),
        ).pack(side="left")
        ttk.Button(
            adv_top,
            text="Get API key",
            style="Compact.TButton",
            command=lambda: webbrowser.open("https://www.last.fm/api/account/create"),
        ).pack(side="right")

        self.lastfm_key = self._entry(
            self.adv_frame, 1, "API key", self.settings.get("lastfm_api_key", "")
        )
        self.lastfm_secret = self._entry(
            self.adv_frame, 2, "API secret", self.settings.get("lastfm_api_secret", ""), secret=True
        )
        self._sync_advanced_visibility()

        self._divider(frame, 4)

        # ---- ListenBrainz ----
        lb_header = self._frame(frame)
        lb_header.grid(row=5, column=0, columnspan=2, sticky="ew")
        self._check(
            lb_header,
            role="fg",
            text="LISTENBRAINZ",
            variable=self.use_lb,
            font=(FONT, 9, "bold"),
        ).pack(side="left")
        ttk.Button(
            lb_header,
            text="Get user token",
            style="Compact.TButton",
            command=lambda: webbrowser.open("https://listenbrainz.org/settings/"),
        ).pack(side="right")

        self.lb_token = self._entry(
            frame, 6, "User token", self.settings.get("listenbrainz_token", ""), secret=True
        )
        self.lb_url = self._entry(
            frame, 7, "Server URL", self.settings.get("listenbrainz_url", "")
        )
        self._label(
            frame,
            role="muted",
            text="Leave blank for api.listenbrainz.org, or enter a Multi-Scrobbler URL.",
            font=(FONT, 8),
            anchor="w",
        ).grid(row=8, column=1, sticky="w", padx=(10, 0))

        self._divider(frame, 9)

        # ---- External Scrobbler Mode ----
        ext_row = self._frame(frame)
        ext_row.grid(row=10, column=0, columnspan=2, sticky="ew")
        ext_cb = self._check(
            ext_row,
            role="fg" if self._ext_supported else "muted",
            text="EXTERNAL SCROBBLER MODE (Pano Scrobbler / OS media session)",
            variable=self.use_external,
            font=(FONT, 8, "bold"),
            state="normal" if self._ext_supported else "disabled",
        )
        ext_cb.pack(side="left")

        self._label(
            frame,
            role="muted",
            text=self._ext_note,
            font=(FONT, 8),
            wraplength=400,
            justify="left",
            anchor="w",
        ).grid(row=11, column=0, columnspan=2, sticky="ew", pady=(2, 0))

        self._divider(frame, 12)

        # ---- Appearance & Secrets ----
        opts_row = self._frame(frame)
        opts_row.grid(row=13, column=0, columnspan=2, sticky="ew")

        self._label(opts_row, role="fg", text="Theme", font=(FONT, 9)).pack(
            side="left", padx=(0, 8)
        )
        self.theme_combo = ttk.Combobox(
            opts_row,
            textvariable=self.theme_var,
            values=("System", "Light", "Dark"),
            state="readonly",
            width=10,
        )
        self.theme_combo.pack(side="left")
        self.theme_combo.bind("<<ComboboxSelected>>", self._on_theme_selected)

        self._check(
            opts_row,
            role="muted",
            text="Show secrets",
            variable=self._show_secrets,
            command=self._toggle_secrets,
            font=(FONT, 8),
        ).pack(side="right")

        self._label(
            frame,
            role="muted",
            text=(
                "Privacy: short in-memory system audio samples go to Shazam and are never "
                "saved. Every eligible listen is appended locally to history.jsonl."
            ),
            font=(FONT, 8),
            wraplength=400,
            justify="left",
            anchor="w",
        ).grid(row=14, column=0, columnspan=2, sticky="ew", pady=(8, 4))

        self.message_lbl = self._label(
            frame,
            role="muted",
            textvariable=self.message,
            font=(FONT, 9),
            wraplength=400,
            justify="left",
            anchor="w",
        )
        self.message_lbl.grid(row=15, column=0, columnspan=2, sticky="ew", pady=(2, 8))

        buttons = self._frame(frame)
        buttons.grid(row=16, column=0, columnspan=2, sticky="ew")
        ttk.Button(
            buttons, text="Save", style="Primary.TButton", command=self._save
        ).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side="left")

    def apply_theme(self, p: theme_module.Palette) -> None:
        self.palette = p
        self.configure(background=p.bg)
        for f in self._themed_frames:
            f.configure(background=p.bg)
        for d in self._dividers:
            d.configure(background=p.border)
        for lbl, role in self._themed_labels:
            fg = p.error if role == "error" else (p.muted if role == "muted" else p.fg)
            lbl.configure(background=p.bg, foreground=fg)
        for cb, role in self._themed_checks:
            fg = p.muted if role == "muted" else p.fg
            cb.configure(
                background=p.bg,
                activebackground=p.bg,
                foreground=fg,
                activeforeground=p.fg,
                selectcolor=p.surface,
                disabledforeground=p.muted,
            )
        for entry in self._themed_entries:
            entry.configure(
                background=p.entry_bg,
                foreground=p.entry_fg,
                insertbackground=p.fg,
                highlightthickness=1,
                highlightbackground=p.border,
                highlightcolor=p.fg,
                relief="flat",
            )
        if self._lastfm_stale_error:
            self.lfm_status_lbl.configure(foreground=p.error)
        theme_module.apply_window_chrome(self, p)

    def _toggle_advanced(self) -> None:
        self._advanced_open = not self._advanced_open
        self._sync_advanced_visibility()

    def _sync_advanced_visibility(self) -> None:
        arrow = "▾" if self._advanced_open else "▸"
        if lastfm_app.has_bundled_credentials():
            label = f"{arrow} Advanced (custom Last.fm API credentials)"
        else:
            label = f"{arrow} Advanced — custom Last.fm API credentials (required in dev build)"
        self.adv_toggle_btn.configure(text=label)
        if self._advanced_open:
            self.adv_frame.grid()
        else:
            self.adv_frame.grid_remove()

    def _refresh_lfm_status(self) -> None:
        sk = credentials.normalize_secret(self.settings.get("lastfm_session_key", ""))
        user = str(self.settings.get("lastfm_username", "") or "").strip()
        if self._lastfm_stale_error:
            self.lfm_status_var.set("Session expired — reconnect")
            self.auth_step1_btn.configure(text="1. Reconnect Last.fm")
            self.message.set(f"Last.fm needs reconnect: {self._lastfm_stale_error}")
            self.verify_lfm_btn.configure(state="normal" if sk else "disabled")
            self.disconnect_lfm_btn.configure(state="normal" if sk else "disabled")
        elif sk:
            self.lfm_status_var.set(f"Connected as @{user}" if user else "Connected")
            self.auth_step1_btn.configure(text="1. Reconnect Last.fm")
            self.verify_lfm_btn.configure(state="normal")
            self.disconnect_lfm_btn.configure(state="normal")
        else:
            self.lfm_status_var.set("Not connected")
            self.auth_step1_btn.configure(text="1. Authorize Last.fm")
            self.verify_lfm_btn.configure(state="disabled")
            self.disconnect_lfm_btn.configure(state="disabled")

    def _on_theme_selected(self, _event=None) -> None:
        pref = self.theme_var.get().strip().lower()
        self.settings["theme"] = pref
        if self.on_theme_change:
            self.on_theme_change(pref)

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

    def _entry(
        self, parent, row: int, label: str, value: str, *, secret: bool = False
    ) -> tk.Entry:
        self._label(
            parent,
            role="fg",
            text=label,
            font=(FONT, 9),
            anchor="w",
        ).grid(row=row, column=0, sticky="w", pady=3)
        entry = tk.Entry(parent, font=(FONT, 9), width=34, show="•" if secret else "")
        entry.insert(0, value)
        entry.grid(row=row, column=1, sticky="ew", padx=(10, 0), pady=3, ipady=3)
        self._themed_entries.append(entry)
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

    def _effective_lastfm_creds(self) -> tuple[str, str]:
        """Resolve the Last.fm API key/secret to use for auth or verification."""
        if self.use_custom_lfm.get() or not lastfm_app.has_bundled_credentials():
            return (
                credentials.normalize_secret(self.lastfm_key.get()),
                credentials.normalize_secret(self.lastfm_secret.get()),
            )
        return lastfm_app.bundled_credentials()

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
                problems.append(f"{labels[key]} {problem}")
                new_values[key] = ""

        lb_url = self.lb_url.get().strip()
        if lb_url:
            _url, url_problem = normalize_submit_url(lb_url)
            if url_problem:
                problems.append(f"ListenBrainz {url_problem}")
                lb_url = ""

        # Emptying a field actively forgets the stored credential; a plain
        # save with an empty value must never wipe it behind the user's back.
        for key in ("lastfm_api_secret", "listenbrainz_token"):
            if not new_values[key] and self.settings.get(key):
                settings_module.clear_secret(key)

        self.settings.update(new_values)
        self.settings["listenbrainz_url"] = lb_url
        self.settings["lastfm_use_custom_credentials"] = bool(self.use_custom_lfm.get())
        self.settings["external_scrobbler"] = bool(
            self.use_external.get() and self._ext_supported
        )
        self.settings["theme"] = self.theme_var.get().strip().lower()
        self.settings["enabled_services"] = self._collect_enabled()

        # Show what was actually stored.
        self.lastfm_key.delete(0, "end")
        self.lastfm_key.insert(0, new_values["lastfm_api_key"])
        self.lastfm_secret.delete(0, "end")
        self.lastfm_secret.insert(0, new_values["lastfm_api_secret"])
        self.lb_token.delete(0, "end")
        self.lb_token.insert(0, new_values["listenbrainz_token"])
        self.lb_url.delete(0, "end")
        self.lb_url.insert(0, lb_url)

        self.on_save(self.settings)
        if problems:
            self.message_lbl.configure(foreground=self.palette.error)
            self.message.set(
                "Saved, but not used: " + "; ".join(problems)
                + ". Fix and save again."
            )
        else:
            self.message_lbl.configure(foreground=self.palette.muted)
            self.message.set("Saved. Changes apply immediately.")

    def _start_lastfm_auth(self) -> None:
        api_key, api_secret = self._effective_lastfm_creds()
        if not api_key or not api_secret:
            self._advanced_open = True
            self._sync_advanced_visibility()
            self.message_lbl.configure(foreground=self.palette.error)
            self.message.set(
                "No bundled Last.fm app credentials in this build. "
                "Enter your API key and secret under Advanced first."
            )
            return

        self.auth_step1_btn.configure(state="disabled")
        self.message_lbl.configure(foreground=self.palette.muted)
        self.message.set("Contacting Last.fm…")

        def worker() -> None:
            try:
                lastfm = LastFM(api_key=api_key, api_secret=api_secret)
                token = lastfm.fetch_token()
                url = lastfm.auth_url(token)
                lastfm.close()
            except Exception as exc:  # noqa: BLE001
                schedule_after(self, self._on_auth_step1_error, str(exc))
                return
            schedule_after(self, self._on_auth_step1_ready, token, url)

        threading.Thread(target=worker, daemon=True, name="lastfm-auth-1").start()

    def _on_auth_step1_error(self, error: str) -> None:
        self.auth_step1_btn.configure(state="normal")
        self.message_lbl.configure(foreground=self.palette.error)
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
        self.message_lbl.configure(foreground=self.palette.muted)
        self.message.set(
            "Last.fm opened in your browser. Click 'Yes, allow access', "
            "then click '2. Finish auth'."
        )

    def _finish_lastfm_auth(self) -> None:
        if not self._pending_token:
            self.message.set("Click '1. Authorize Last.fm' first.")
            return

        api_key, api_secret = self._effective_lastfm_creds()
        use_custom = bool(self.use_custom_lfm.get() or not lastfm_app.has_bundled_credentials())
        token = self._pending_token
        self.auth_step2_btn.configure(state="disabled")
        self.message_lbl.configure(foreground=self.palette.muted)
        self.message.set("Finishing authorisation…")

        def worker() -> None:
            try:
                lastfm = LastFM(api_key=api_key, api_secret=api_secret)
                session_key, username = lastfm.exchange_token_with_user(token)
                lastfm.close()
            except Exception as exc:  # noqa: BLE001
                schedule_after(self, self._on_auth_step2_error, str(exc))
                return
            schedule_after(
                self,
                self._on_auth_step2_done,
                api_key,
                api_secret,
                session_key,
                username,
                use_custom,
            )

        threading.Thread(target=worker, daemon=True, name="lastfm-auth-2").start()

    def _on_auth_step2_error(self, error: str) -> None:
        self.auth_step2_btn.configure(state="normal")
        self.message_lbl.configure(foreground=self.palette.error)
        self.message.set(
            f"Last.fm hasn't approved yet ({error}). Click Allow in your browser, then try again."
        )

    def _on_auth_step2_done(
        self,
        api_key: str,
        api_secret: str,
        session_key: str,
        username: str,
        use_custom: bool,
    ) -> None:
        self._pending_token = None
        self._lastfm_stale_error = None
        self.use_lastfm.set(True)
        self.settings["enabled_services"] = self._collect_enabled()
        if use_custom:
            self.settings["lastfm_api_key"] = api_key
            self.settings["lastfm_api_secret"] = api_secret
            self.settings["lastfm_use_custom_credentials"] = True
        else:
            self.settings["lastfm_use_custom_credentials"] = False
        self.settings["lastfm_session_key"] = session_key
        self.settings["lastfm_username"] = username
        self.on_save(self.settings)
        self._refresh_lfm_status()
        self.lfm_status_lbl.configure(foreground=self.palette.muted)
        self.message_lbl.configure(foreground=self.palette.muted)
        who = f" as @{username}" if username else ""
        self.message.set(f"Last.fm connected{who} and ready to scrobble.")

    def _verify_lastfm_session(self) -> None:
        api_key, api_secret = self._effective_lastfm_creds()
        sk = credentials.normalize_secret(self.settings.get("lastfm_session_key", ""))
        if not sk:
            self.message.set("No Last.fm session stored yet.")
            return
        self.verify_lfm_btn.configure(state="disabled")
        self.message_lbl.configure(foreground=self.palette.muted)
        self.message.set("Checking Last.fm session…")

        def worker() -> None:
            lastfm = LastFM(api_key=api_key, api_secret=api_secret, session_key=sk)
            ok, username, err = lastfm.verify_session()
            stale = lastfm.session_stale
            lastfm.close()
            schedule_after(self, self._on_verify_done, ok, username or "", err or "", stale)

        threading.Thread(target=worker, daemon=True, name="lastfm-verify").start()

    def _on_verify_done(self, ok: bool, username: str, error: str, stale: bool) -> None:
        self.verify_lfm_btn.configure(state="normal")
        if ok:
            self._lastfm_stale_error = None
            if username:
                self.settings["lastfm_username"] = username
                self.on_save(self.settings)
            self._refresh_lfm_status()
            self.lfm_status_lbl.configure(foreground=self.palette.muted)
            self.message_lbl.configure(foreground=self.palette.muted)
            who = f"@{username}" if username else "valid"
            self.message.set(f"Last.fm session verified ({who}).")
        else:
            if stale:
                self._lastfm_stale_error = error
                self._refresh_lfm_status()
                self.lfm_status_lbl.configure(foreground=self.palette.error)
            self.message_lbl.configure(foreground=self.palette.error)
            self.message.set(f"Last.fm check failed: {error}")

    def _disconnect_lastfm(self) -> None:
        settings_module.clear_secret("lastfm_session_key")
        self.settings["lastfm_session_key"] = ""
        self.settings["lastfm_username"] = ""
        self._lastfm_stale_error = None
        self.on_save(self.settings)
        self._refresh_lfm_status()
        self.lfm_status_lbl.configure(foreground=self.palette.muted)
        self.message_lbl.configure(foreground=self.palette.muted)
        self.message.set("Disconnected Last.fm session.")


# ================================================================ Main Window


class EggCreamWindow:
    """Compact, minimalist, monochrome desktop window."""

    def __init__(self, settings: dict) -> None:
        self.settings = settings
        self.palette = theme_module.resolve_palette(settings.get("theme"))
        self.events: queue.Queue = queue.Queue()
        self.engine: EggCream | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._closing = False
        self._shut_down = False
        self._error_message: str | None = None
        self._lastfm_stale_error: str | None = None
        self._history: list[Recognition] = []
        self._selected_track: Recognition | None = None
        self._scrobble_count = 0
        self._poll_id: str | None = None
        self._setup_dialog: SetupDialog | None = None
        self._history_dialog: HistoryDialog | None = None

        self.root = tk.Tk()
        self.root.title("Chocolate Egg Cream")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._icon_ref = _apply_app_icon(self.root)
        self._mark_ref = _load_header_mark()

        self.style = ttk.Style(self.root)
        self.song_var = tk.StringVar(value="Press Start, then play your audio")
        self.status_var = tk.StringVar(value="○  stopped")
        self.hint_var = tk.StringVar(value="")
        self.services_var = tk.StringVar(value=self._services_text())
        self.stale_var = tk.StringVar(value="")
        self.theme_var = tk.StringVar(
            value=str(self.settings.get("theme", "system")).capitalize()
        )

        self._themed_frames: list[tk.Widget] = []
        self._themed_labels: list[tuple[tk.Label, str]] = []
        self._dividers: list[tk.Frame] = []

        self._build()
        self._apply_theme(self.palette)
        self._poll_id = self.root.after(150, self._poll)

    # ------------------------------------------------------------- layout

    def _frame(self, parent, **kw) -> tk.Frame:
        f = tk.Frame(parent, **kw)
        self._themed_frames.append(f)
        return f

    def _label(self, parent, role: str = "fg", **kw) -> tk.Label:
        lbl = tk.Label(parent, **kw)
        self._themed_labels.append((lbl, role))
        return lbl

    def _build(self) -> None:
        outer = self._frame(self.root, padx=16, pady=12)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)

        # ---- Header: icon + title + Theme selector + Setup ----
        header = self._frame(outer)
        header.grid(row=0, column=0, sticky="ew")

        brand = self._frame(header)
        brand.pack(side="left")
        if self._mark_ref is not None:
            mark = tk.Label(brand, image=self._mark_ref, borderwidth=0)
            mark.pack(side="left", padx=(0, 7))
            self._themed_labels.append((mark, "fg"))

        self._label(
            brand,
            role="fg",
            text="CHOCOLATE EGG CREAM",
            font=(FONT, 9, "bold"),
        ).pack(side="left")

        right_hdr = self._frame(header)
        right_hdr.pack(side="right")

        self.theme_combo = ttk.Combobox(
            right_hdr,
            textvariable=self.theme_var,
            values=("System", "Light", "Dark"),
            state="readonly",
            width=7,
        )
        self.theme_combo.pack(side="left", padx=(0, 6))
        self.theme_combo.bind("<<ComboboxSelected>>", self._on_theme_selected)

        ttk.Button(
            right_hdr,
            text="Setup",
            style="Compact.TButton",
            command=self.open_setup,
        ).pack(side="left")

        # Hairline divider
        d1 = tk.Frame(outer, height=1)
        d1.grid(row=1, column=0, sticky="ew", pady=(10, 10))
        self._dividers.append(d1)

        # ---- Now Playing ----
        self.status_label = self._label(
            outer,
            role="muted",
            textvariable=self.status_var,
            font=(FONT, 8),
            justify="left",
            wraplength=420,
            anchor="w",
        )
        self.status_label.grid(row=2, column=0, sticky="ew")

        self.song_label = self._label(
            outer,
            role="fg",
            textvariable=self.song_var,
            font=(FONT, 14, "bold"),
            justify="left",
            wraplength=420,
            anchor="w",
        )
        self.song_label.grid(row=3, column=0, sticky="ew", pady=(3, 0))

        # Stale Last.fm session banner (hidden unless needed)
        self.stale_banner = tk.Frame(outer, padx=8, pady=6, bd=1, relief="solid")
        self.stale_banner.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.stale_lbl = tk.Label(
            self.stale_banner,
            textvariable=self.stale_var,
            font=(FONT, 8),
            wraplength=310,
            justify="left",
            anchor="w",
        )
        self.stale_lbl.pack(side="left", fill="x", expand=True)
        ttk.Button(
            self.stale_banner,
            text="Reconnect",
            style="Compact.TButton",
            command=self.open_setup,
        ).pack(side="right", padx=(8, 0))
        self.stale_banner.grid_remove()

        # ---- Controls + Quick Track Actions ----
        controls = self._frame(outer)
        controls.grid(row=5, column=0, sticky="ew", pady=(12, 10))

        self.start_button = ttk.Button(
            controls, text="Start", style="Primary.TButton", width=8, command=self.start
        )
        self.start_button.pack(side="left", padx=(0, 6))

        self.stop_button = ttk.Button(
            controls, text="Stop", width=8, command=self.stop, state="disabled"
        )
        self.stop_button.pack(side="left", padx=(0, 12))

        self.copy_button = ttk.Button(
            controls,
            text="Copy",
            style="Compact.TButton",
            command=self._copy_track,
            state="disabled",
        )
        self.copy_button.pack(side="left", padx=(0, 4))

        self.lyrics_button = ttk.Button(
            controls,
            text="Lyrics",
            style="Compact.TButton",
            command=lambda: self._open_track_url("lyrics"),
            state="disabled",
        )
        self.lyrics_button.pack(side="left", padx=(0, 4))

        self.youtube_button = ttk.Button(
            controls,
            text="YouTube",
            style="Compact.TButton",
            command=lambda: self._open_track_url("youtube"),
            state="disabled",
        )
        self.youtube_button.pack(side="left")

        # ---- Recent Songs + Open Listening History ----
        recent_hdr = self._frame(outer)
        recent_hdr.grid(row=6, column=0, sticky="ew", pady=(2, 4))
        self._label(
            recent_hdr,
            role="muted",
            text="RECENT",
            font=(FONT, 8, "bold"),
            anchor="w",
        ).pack(side="left")
        ttk.Button(
            recent_hdr,
            text="Open Listening History",
            style="Compact.TButton",
            command=self.open_history,
        ).pack(side="right")

        self.list_frame = tk.Frame(outer, bd=1, relief="solid")
        self.list_frame.grid(row=7, column=0, sticky="ew")

        self.history_list = tk.Listbox(
            self.list_frame,
            height=5,
            width=54,
            borderwidth=0,
            highlightthickness=0,
            font=(FONT, 9),
            activestyle="none",
        )
        self.history_list.pack(side="left", fill="both", expand=True, padx=6, pady=4)
        self.history_list.bind("<<ListboxSelect>>", self._on_history_select)

        scrollbar = ttk.Scrollbar(
            self.list_frame, orient="vertical", command=self.history_list.yview
        )
        scrollbar.pack(side="right", fill="y")
        self.history_list.configure(yscrollcommand=scrollbar.set)

        # Hairline divider
        d2 = tk.Frame(outer, height=1)
        d2.grid(row=8, column=0, sticky="ew", pady=(10, 6))
        self._dividers.append(d2)

        # ---- Footer ----
        footer = self._frame(outer)
        footer.grid(row=9, column=0, sticky="ew")
        self._label(
            footer,
            role="muted",
            textvariable=self.services_var,
            font=(FONT, 8),
            anchor="w",
        ).pack(side="left")
        self._label(
            footer,
            role="fg",
            textvariable=self.hint_var,
            font=(FONT, 8),
            anchor="e",
        ).pack(side="right")

    # ------------------------------------------------------------- theming

    def _on_theme_selected(self, _event=None) -> None:
        pref = self.theme_var.get().strip().lower()
        self._set_theme(pref)

    def _set_theme(self, preference: str) -> None:
        pref = preference if preference in settings_module.KNOWN_THEMES else "system"
        self.settings["theme"] = pref
        self.theme_var.set(pref.capitalize())
        settings_module.save(self.settings)
        palette = theme_module.resolve_palette(pref)
        self._apply_theme(palette)

    def _apply_theme(self, p: theme_module.Palette) -> None:
        self.palette = p
        theme_module.configure_ttk_styles(self.style, p, FONT)
        self.root.configure(background=p.bg)
        for f in self._themed_frames:
            f.configure(background=p.bg)
        for d in self._dividers:
            d.configure(background=p.border)
        for lbl, role in self._themed_labels:
            fg = p.error if role == "error" else (p.muted if role == "muted" else p.fg)
            lbl.configure(background=p.bg, foreground=fg)
        if self._error_message:
            self.status_label.configure(foreground=p.error)
        self.list_frame.configure(background=p.border, highlightbackground=p.border)
        self.history_list.configure(
            background=p.surface,
            foreground=p.fg,
            selectbackground=p.select_bg,
            selectforeground=p.select_fg,
        )
        self.stale_banner.configure(background=p.error_bg, highlightbackground=p.error)
        self.stale_lbl.configure(background=p.error_bg, foreground=p.error)
        theme_module.apply_window_chrome(self.root, p)

        if self._setup_dialog is not None and self._setup_dialog.winfo_exists():
            self._setup_dialog.apply_theme(p)
        if self._history_dialog is not None and self._history_dialog.winfo_exists():
            self._history_dialog.apply_theme(p)

    # ------------------------------------------------------------- dialogs

    def open_setup(self) -> None:
        """Open (or focus) the Setup dialog."""
        if self._setup_dialog is not None and self._setup_dialog.winfo_exists():
            self._setup_dialog.lift()
            return
        self._setup_dialog = SetupDialog(
            self.root,
            self.settings,
            self._save_settings,
            palette=self.palette,
            on_theme_change=self._set_theme,
            lastfm_stale_error=self._lastfm_stale_error,
        )

    def open_history(self) -> None:
        """Open (or focus) the Listening History window."""
        if self._history_dialog is not None and self._history_dialog.winfo_exists():
            self._history_dialog.refresh()
            self._history_dialog.lift()
            return
        self._history_dialog = HistoryDialog(
            self.root,
            self.palette,
            on_retry_request=self._request_retry_pending,
        )

    def _request_retry_pending(self, on_done) -> None:
        """Trigger a retry of failed scrobbles from the History window."""
        if self.engine is not None and self.loop is not None:
            def _run_on_engine() -> None:
                fut = asyncio.run_coroutine_threadsafe(self.engine.retry_pending(), self.loop)
                try:
                    s, r = fut.result(timeout=30)
                except Exception:  # noqa: BLE001
                    s, r = 0, len(history_module.load_pending())
                on_done(s, r)

            threading.Thread(target=_run_on_engine, daemon=True, name="retry-pending").start()
            return

        # Engine not started yet: run a lightweight one-shot retry in a worker thread.
        def _standalone_retry() -> None:
            tmp_engine = EggCream(
                self.settings,
                capture=object(),
                recognizer=object(),
                save_settings=settings_module.save,
            )
            try:
                s, r = asyncio.run(tmp_engine.retry_pending())
            except Exception:  # noqa: BLE001
                s, r = 0, len(history_module.load_pending())
            finally:
                for sc in tmp_engine.scrobblers:
                    close_fn = getattr(sc, "close", None)
                    if callable(close_fn):
                        try:
                            close_fn()
                        except Exception:  # noqa: BLE001
                            pass
            on_done(s, r)

        threading.Thread(target=_standalone_retry, daemon=True, name="retry-pending").start()

    def _save_settings(self, settings: dict) -> None:
        settings_module.save(settings)
        # Clear stale alert if Last.fm was reconnected or disabled
        if self._lastfm_stale_error and (
            "lastfm" not in settings.get("enabled_services", [])
            or settings.get("lastfm_session_key")
        ):
            self._lastfm_stale_error = None
            self.stale_banner.grid_remove()

        if self.engine is not None and self.loop is not None:
            try:
                self.loop.call_soon_threadsafe(self.engine.reload_settings, settings)
                # If failed scrobbles are waiting, retry them now that credentials updated.
                if history_module.load_pending():
                    self._call_engine(self.engine.retry_pending())
            except RuntimeError:
                pass
        self.services_var.set(self._services_text())

    def _services_text(self) -> str:
        enabled = self.settings.get("enabled_services", [])
        parts = []
        if "lastfm" in enabled:
            api_key, api_secret, _custom = lastfm_app.resolve_credentials(self.settings)
            sk = credentials.normalize_secret(self.settings.get("lastfm_session_key"))
            ready = bool(
                api_key
                and api_secret
                and sk
                and not credentials.secret_problem(api_key)
                and not credentials.secret_problem(api_secret)
                and not credentials.secret_problem(sk)
            )
            user = str(self.settings.get("lastfm_username") or "").strip()
            if self._lastfm_stale_error:
                parts.append("Last.fm (reconnect)")
            elif ready:
                parts.append(f"Last.fm (@{user})" if user else "Last.fm")
            else:
                parts.append("Last.fm (setup)")

        if "listenbrainz" in enabled:
            token = self.settings.get("listenbrainz_token")
            lb_url = str(self.settings.get("listenbrainz_url") or "").strip()
            _u, url_err = normalize_submit_url(lb_url)
            ready = (
                bool(credentials.normalize_secret(token))
                and not credentials.secret_problem(token)
                and url_err is None
            )
            if ready:
                host = urllib.parse.urlparse(lb_url).netloc if lb_url else ""
                parts.append(f"LB ({host})" if host else "ListenBrainz")
            else:
                parts.append("ListenBrainz (setup)")

        if self.settings.get("external_scrobbler"):
            ok_ext, _ = external_module.platform_support()
            if ok_ext:
                parts.append("SMTC" if sys.platform == "win32" else "MPRIS")

        base = " · ".join(parts) if parts else "Local history only"
        if self._scrobble_count:
            base += f" · {self._scrobble_count} scrobbled"
        pending_count = len(history_module.load_pending())
        if pending_count:
            base += f" · {pending_count} pending"
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
        for btn in (self.copy_button, self.lyrics_button, self.youtube_button):
            btn.configure(state=state)

    def _on_history_select(self, _event=None) -> None:
        selection = self.history_list.curselection()
        if not selection:
            return
        idx = selection[0]
        if 0 <= idx < len(self._history):
            self._selected_track = self._history[idx]
            self._set_song_actions_enabled(True)
            self._flash_hint(f"Selected: {self._selected_track.title[:26]}")

    def _copy_track(self) -> None:
        track = self._active_track()
        if track is None:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(track.label)
            self._flash_hint("Copied")
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
        self.root.after(
            2500, lambda: self.hint_var.set("") if self.hint_var.get() == text else None
        )

    # ------------------------------------------------------------- engine

    def _get_hwnd(self) -> int | None:
        if sys.platform != "win32":
            return None
        try:
            import ctypes

            return ctypes.windll.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        except Exception:  # noqa: BLE001
            return None

    def _dispatch_ui(self, fn) -> None:
        schedule_after(self.root, fn)

    def _engine_thread(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.engine = EggCream(
                self.settings,
                hwnd_getter=self._get_hwnd,
                ui_dispatch=self._dispatch_ui,
            )
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
            self._queue_event("error", str(exc))

    # ------------------------------------------------------------ actions

    def start(self) -> None:
        if self.engine is not None and self.engine.running:
            return
        self._error_message = None
        self.status_label.configure(foreground=self.palette.muted)
        self._start_engine_thread()
        if self.engine is None:
            return
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("●  starting system audio capture…")
        self._call_engine(self.engine.start())

    def stop(self) -> None:
        if self.engine is None or not self.engine.running:
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self.status_var.set("○  stopped")
            return
        self._call_engine(self.engine.stop())
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set("○  stopped")

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
            self.status_label.configure(foreground=self.palette.muted)
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
            self._flash_hint(f"Scrobbled {payload.title[:22]}")
        elif kind == "listen_saved":
            self.services_var.set(self._services_text())
            if self._history_dialog is not None and self._history_dialog.winfo_exists():
                self._history_dialog.refresh()
        elif kind == "retried" and isinstance(payload, dict):
            self.services_var.set(self._services_text())
            self._flash_hint(f"Retried {payload.get('succeeded', 0)} scrobble(s)")
            if self._history_dialog is not None and self._history_dialog.winfo_exists():
                self._history_dialog.refresh()
        elif kind == "lastfm_stale":
            detail = str(payload) if payload else "Invalid session key"
            self._lastfm_stale_error = detail
            self.stale_var.set(f"Last.fm session expired ({detail})")
            self.stale_banner.grid()
            self.services_var.set(self._services_text())
        elif kind == "started":
            self._error_message = None
            self.status_label.configure(foreground=self.palette.muted)
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
            self.status_var.set("●  listening for music…")
        elif kind == "stopped":
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            if not self._error_message:
                self.status_var.set("○  stopped")
        elif kind == "error":
            detail = str(payload) if payload else "Something went wrong — see the log."
            self._error_message = detail
            self.status_label.configure(foreground=self.palette.error)
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
                    f"●  recognition unavailable · retrying in {int(backoff) + 1}s"
                )
            elif candidate is not None:
                self.status_var.set(f"●  hearing: {candidate.label} (confirming…)")
            elif engine.heard_audio is False:
                self.status_var.set("●  listening · quiet")
            else:
                self.status_var.set("●  listening…")
            return

        base = f"●  listening · {format_time(engine.playtime)}"
        if backoff > 0:
            self.status_var.set(f"{base} · retrying in {int(backoff) + 1}s")
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
