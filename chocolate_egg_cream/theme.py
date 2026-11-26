"""Minimalist black-and-white Light, Dark, and System theme support.

Zero external dependencies: detects the OS light/dark preference using
`winreg` on Windows, `defaults` on macOS, and `gsettings`/env on Linux,
and applies a crisp monochrome palette plus Windows dark title bars.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    name: str  # "light" or "dark"
    bg: str
    surface: str
    surface_alt: str
    border: str
    fg: str
    muted: str
    accent: str
    accent_fg: str
    accent_hover: str
    entry_bg: str
    entry_fg: str
    select_bg: str
    select_fg: str
    error: str
    error_bg: str


LIGHT = Palette(
    name="light",
    bg="#ffffff",
    surface="#f5f5f5",
    surface_alt="#ebebeb",
    border="#d8d8d8",
    fg="#111111",
    muted="#666666",
    accent="#111111",
    accent_fg="#ffffff",
    accent_hover="#2b2b2b",
    entry_bg="#ffffff",
    entry_fg="#111111",
    select_bg="#111111",
    select_fg="#ffffff",
    error="#b91c1c",
    error_bg="#fef2f2",
)

DARK = Palette(
    name="dark",
    bg="#111111",
    surface="#1a1a1a",
    surface_alt="#262626",
    border="#2e2e2e",
    fg="#f5f5f5",
    muted="#929292",
    accent="#f5f5f5",
    accent_fg="#111111",
    accent_hover="#dcdcdc",
    entry_bg="#171717",
    entry_fg="#f5f5f5",
    select_bg="#f5f5f5",
    select_fg="#111111",
    error="#f87171",
    error_bg="#261414",
)


def detect_system_theme() -> str:
    """Return `'dark'` or `'light'` matching the host operating system."""
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return "light" if int(value) else "dark"
        except OSError:
            return "light"

    if sys.platform == "darwin":
        try:
            res = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            if res.returncode == 0 and "dark" in res.stdout.lower():
                return "dark"
        except Exception:  # noqa: BLE001
            pass
        return "light"

    if sys.platform.startswith("linux"):
        gtk_theme = os.environ.get("GTK_THEME", "")
        if "dark" in gtk_theme.lower():
            return "dark"
        try:
            res = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            if res.returncode == 0 and "dark" in res.stdout.lower():
                return "dark"
        except Exception:  # noqa: BLE001
            pass
        return "light"

    return "light"


def resolve_palette(preference: str | None) -> Palette:
    """Resolve `'system'`, `'light'`, or `'dark'` to a monochrome `Palette`."""
    pref = str(preference or "system").strip().lower()
    if pref == "dark":
        return DARK
    if pref == "light":
        return LIGHT
    return DARK if detect_system_theme() == "dark" else LIGHT


def apply_window_chrome(window, palette: Palette) -> None:
    """Match the native Windows 10/11 title bar to the active Light/Dark theme."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
        if not hwnd:
            return
        use_dark = ctypes.c_int(1 if palette.name == "dark" else 0)
        dwmapi = ctypes.windll.dwmapi
        # DWMWA_USE_IMMERSIVE_DARK_MODE = 20 (Win11 / Win10 2004+), fallback 19
        res = dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(use_dark), ctypes.sizeof(use_dark)
        )
        if res != 0:
            dwmapi.DwmSetWindowAttribute(
                hwnd, 19, ctypes.byref(use_dark), ctypes.sizeof(use_dark)
            )
    except Exception:  # noqa: BLE001
        pass


def configure_ttk_styles(style, palette: Palette, font_family: str) -> None:
    """Configure flat, hairline-bordered monochrome ttk styles."""
    try:
        style.theme_use("clam")
    except Exception:  # noqa: BLE001
        pass

    style.configure(
        ".",
        background=palette.bg,
        foreground=palette.fg,
        bordercolor=palette.border,
        lightcolor=palette.border,
        darkcolor=palette.border,
        troughcolor=palette.surface,
        focuscolor=palette.border,
        font=(font_family, 9),
    )

    # Secondary / default button: crisp flat surface with 1px border
    style.configure(
        "TButton",
        background=palette.surface,
        foreground=palette.fg,
        bordercolor=palette.border,
        lightcolor=palette.border,
        darkcolor=palette.border,
        borderwidth=1,
        relief="solid",
        padding=(10, 5),
        font=(font_family, 9),
    )
    style.map(
        "TButton",
        background=[
            ("disabled", palette.bg),
            ("pressed", palette.surface_alt),
            ("active", palette.surface_alt),
        ],
        foreground=[("disabled", palette.muted)],
        bordercolor=[("disabled", palette.border), ("active", palette.fg)],
    )

    # Compact subtle button (header / toolbar actions)
    style.configure(
        "Compact.TButton",
        background=palette.bg,
        foreground=palette.fg,
        bordercolor=palette.border,
        lightcolor=palette.border,
        darkcolor=palette.border,
        borderwidth=1,
        relief="solid",
        padding=(8, 3),
        font=(font_family, 8),
    )
    style.map(
        "Compact.TButton",
        background=[
            ("disabled", palette.bg),
            ("pressed", palette.surface_alt),
            ("active", palette.surface),
        ],
        foreground=[("disabled", palette.muted)],
        bordercolor=[("active", palette.fg)],
    )

    # Primary high-contrast action button (Start / Save / Reconnect)
    style.configure(
        "Primary.TButton",
        background=palette.accent,
        foreground=palette.accent_fg,
        bordercolor=palette.accent,
        lightcolor=palette.accent,
        darkcolor=palette.accent,
        borderwidth=1,
        relief="solid",
        padding=(12, 5),
        font=(font_family, 9, "bold"),
    )
    style.map(
        "Primary.TButton",
        background=[
            ("disabled", palette.surface),
            ("pressed", palette.accent_hover),
            ("active", palette.accent_hover),
        ],
        foreground=[("disabled", palette.muted)],
        bordercolor=[("disabled", palette.border)],
    )

    # Combobox (Theme selector)
    style.configure(
        "TCombobox",
        fieldbackground=palette.entry_bg,
        background=palette.surface,
        foreground=palette.entry_fg,
        arrowcolor=palette.fg,
        bordercolor=palette.border,
        lightcolor=palette.border,
        darkcolor=palette.border,
        selectbackground=palette.select_bg,
        selectforeground=palette.select_fg,
        padding=(6, 3),
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", palette.surface)],
        foreground=[("readonly", palette.fg)],
    )

    # Scrollbar
    style.configure(
        "Vertical.TScrollbar",
        background=palette.surface_alt,
        troughcolor=palette.surface,
        bordercolor=palette.surface,
        arrowcolor=palette.muted,
        relief="flat",
        borderwidth=0,
    )
    style.map(
        "Vertical.TScrollbar",
        background=[("active", palette.muted)],
    )

    # Treeview (Listening History table)
    style.configure(
        "Treeview",
        background=palette.surface,
        foreground=palette.fg,
        fieldbackground=palette.surface,
        bordercolor=palette.border,
        borderwidth=0,
        rowheight=24,
        font=(font_family, 9),
    )
    style.map(
        "Treeview",
        background=[("selected", palette.select_bg)],
        foreground=[("selected", palette.select_fg)],
    )
    style.configure(
        "Treeview.Heading",
        background=palette.bg,
        foreground=palette.muted,
        bordercolor=palette.border,
        relief="flat",
        font=(font_family, 8, "bold"),
        padding=(6, 4),
    )
    style.map(
        "Treeview.Heading",
        background=[("active", palette.surface)],
        foreground=[("active", palette.fg)],
    )
