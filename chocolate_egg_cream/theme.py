"""Crisp, high-DPI Light, Dark, and System theme support with subtle purple accents.

Zero external dependencies:
- Enables Per-Monitor V2 DPI awareness on Windows so Tkinter renders sharp,
  native-resolution text, borders, and icons at 100%–200% scaling.
- Detects the OS light/dark preference using `winreg` on Windows, `defaults`
  on macOS, and `gsettings`/env on Linux.
- Applies a clean black/white/gray palette with subtle purple accents plus
  native Windows 10/11 dark title bars.
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
    accent_soft: str
    entry_bg: str
    entry_fg: str
    select_bg: str
    select_fg: str
    ok: str
    error: str
    error_bg: str


LIGHT = Palette(
    name="light",
    bg="#fafafa",
    surface="#ffffff",
    surface_alt="#f0f0f4",
    border="#dcdce2",
    fg="#141418",
    muted="#64646e",
    accent="#6d28d9",
    accent_fg="#ffffff",
    accent_hover="#5b21b6",
    accent_soft="#f3e8ff",
    entry_bg="#ffffff",
    entry_fg="#141418",
    select_bg="#6d28d9",
    select_fg="#ffffff",
    ok="#15803d",
    error="#b91c1c",
    error_bg="#fef2f2",
)

DARK = Palette(
    name="dark",
    bg="#121216",
    surface="#1a1a20",
    surface_alt="#25252e",
    border="#31313c",
    fg="#f4f4f6",
    muted="#9898a6",
    accent="#8b5cf6",
    accent_fg="#ffffff",
    accent_hover="#7c3aed",
    accent_soft="#261c3d",
    entry_bg="#16161b",
    entry_fg="#f4f4f6",
    select_bg="#7c3aed",
    select_fg="#ffffff",
    ok="#4ade80",
    error="#f87171",
    error_bg="#2b1619",
)


def enable_dpi_awareness() -> None:
    """Enable Per-Monitor V2 DPI awareness on Windows before Tk initializes.

    Without this, Windows bitmap-stretches the entire Tk window on 125%–200%
    displays, making text, borders, buttons, and icons blurry and pixelated.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = (DPI_AWARENESS_CONTEXT)-4
        user32 = ctypes.windll.user32
        if hasattr(user32, "SetProcessDpiAwarenessContext"):
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
    except Exception:  # noqa: BLE001
        pass
    try:
        import ctypes

        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        import ctypes

        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


def get_dpi_scale(window) -> float:
    """Return the display scale factor (1.0 at 96 DPI, 1.5 at 144 DPI, 2.0 at 192 DPI)."""
    if sys.platform == "win32":
        try:
            import ctypes

            window.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(window.winfo_id()) or window.winfo_id()
            user32 = ctypes.windll.user32
            if hwnd and hasattr(user32, "GetDpiForWindow"):
                dpi = int(user32.GetDpiForWindow(hwnd))
                if dpi >= 72:
                    return max(1.0, dpi / 96.0)
        except Exception:  # noqa: BLE001
            pass
    try:
        dpi = float(window.winfo_fpixels("1i"))
        if dpi >= 72.0:
            return max(1.0, dpi / 96.0)
    except Exception:  # noqa: BLE001
        pass
    return 1.0


def apply_tk_scaling(window) -> float:
    """Synchronize Tk's internal scaling with the monitor's actual DPI."""
    scale = get_dpi_scale(window)
    if sys.platform == "win32":
        try:
            # Tk expects points-per-pixel = (dpi / 72.0) = scale * (96.0 / 72.0)
            window.tk.call("tk", "scaling", scale * (96.0 / 72.0))
        except Exception:  # noqa: BLE001
            pass
    return scale


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
    """Resolve `'system'`, `'light'`, or `'dark'` to a `Palette`."""
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


def configure_ttk_styles(
    style, palette: Palette, font_family: str, *, scale: float = 1.0, root=None
) -> None:
    """Configure crisp, hairline-bordered ttk styles with subtle purple accents."""
    try:
        style.theme_use("clam")
    except Exception:  # noqa: BLE001
        pass

    def px(n: int) -> int:
        return max(1, int(round(n * scale)))

    if root is not None:
        try:
            root.option_add("*TCombobox*Listbox*Background", palette.surface)
            root.option_add("*TCombobox*Listbox*Foreground", palette.fg)
            root.option_add("*TCombobox*Listbox*selectBackground", palette.select_bg)
            root.option_add("*TCombobox*Listbox*selectForeground", palette.select_fg)
            root.option_add("*TCombobox*Listbox*Font", (font_family, 9))
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
        focuscolor=palette.accent,
        font=(font_family, 9),
    )

    # Secondary / default button: crisp flat surface with 1px hairline border
    style.configure(
        "TButton",
        background=palette.surface,
        foreground=palette.fg,
        bordercolor=palette.border,
        lightcolor=palette.border,
        darkcolor=palette.border,
        borderwidth=1,
        relief="solid",
        padding=(px(11), px(5)),
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
        bordercolor=[("disabled", palette.border), ("active", palette.accent)],
    )

    # Compact subtle button (header / toolbar / quick actions)
    style.configure(
        "Compact.TButton",
        background=palette.surface,
        foreground=palette.fg,
        bordercolor=palette.border,
        lightcolor=palette.border,
        darkcolor=palette.border,
        borderwidth=1,
        relief="solid",
        padding=(px(9), px(4)),
        font=(font_family, 8),
    )
    style.map(
        "Compact.TButton",
        background=[
            ("disabled", palette.bg),
            ("pressed", palette.surface_alt),
            ("active", palette.surface_alt),
        ],
        foreground=[("disabled", palette.muted)],
        bordercolor=[("disabled", palette.border), ("active", palette.accent)],
    )

    # Primary purple-accent action button (Start / Save / Authorize)
    style.configure(
        "Primary.TButton",
        background=palette.accent,
        foreground=palette.accent_fg,
        bordercolor=palette.accent,
        lightcolor=palette.accent,
        darkcolor=palette.accent,
        borderwidth=1,
        relief="solid",
        padding=(px(13), px(5)),
        font=(font_family, 9, "bold"),
    )
    style.map(
        "Primary.TButton",
        background=[
            ("disabled", palette.surface_alt),
            ("pressed", palette.accent_hover),
            ("active", palette.accent_hover),
        ],
        foreground=[("disabled", palette.muted)],
        bordercolor=[("disabled", palette.border), ("active", palette.accent_hover)],
    )

    # Combobox (Theme selector)
    style.configure(
        "TCombobox",
        fieldbackground=palette.surface,
        background=palette.surface,
        foreground=palette.fg,
        arrowcolor=palette.muted,
        bordercolor=palette.border,
        lightcolor=palette.border,
        darkcolor=palette.border,
        selectbackground=palette.select_bg,
        selectforeground=palette.select_fg,
        padding=(px(7), px(3)),
        arrowsize=px(12),
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", palette.surface)],
        foreground=[("readonly", palette.fg)],
        bordercolor=[("active", palette.accent), ("focus", palette.accent)],
        arrowcolor=[("active", palette.accent)],
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
        width=px(11),
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
        rowheight=px(26),
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
        padding=(px(6), px(4)),
    )
    style.map(
        "Treeview.Heading",
        background=[("active", palette.surface_alt)],
        foreground=[("active", palette.fg)],
    )
