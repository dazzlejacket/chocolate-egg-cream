"""Optional external media session mode (Windows SMTC / Linux MPRIS2).

Publishes confirmed Now Playing track metadata to the OS's native media
session API without playing fake/silent audio and without interfering with
other running media players:

* **Windows 10/11**: Native Windows SMTC (`SystemMediaTransportControls`) via
  `ISystemMediaTransportControlsInterop::GetForWindow(hwnd)` using stdlib
  `ctypes` + `combase.dll`, with explicit AppUserModelID, `ButtonPressed`
  registration, transport capabilities, music metadata, and timeline
  properties (`ISystemMediaTransportControls2::UpdateTimelineProperties`).
* **Linux**: Native MPRIS2 (`org.mpris.MediaPlayer2.chocolate_egg_cream`) over
  the user's D-Bus session bus using pure Python stdlib (`socket` + `struct`),
  implementing `Properties.Get`, `Properties.GetAll`, `PropertiesChanged`,
  `Introspect`, and `Peer.Ping`.
* **macOS**: Not enabled. macOS's `MPNowPlayingInfoCenter` is a single global
  Now Playing slot that requires claiming audio/remote-command focus, which
  would hijack media keys and interfere with existing players. We report this
  limitation honestly rather than playing silent audio or stealing focus.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import struct
import sys
import threading
import time
import uuid
from typing import Callable

log = logging.getLogger(__name__)

APP_USER_MODEL_ID = "com.chocolateeggcream.app"

_IDENTITY_LOCK = threading.Lock()
_IDENTITY_SET = False


def ensure_windows_app_identity(app_id: str = APP_USER_MODEL_ID) -> bool:
    """Set the Windows process AppUserModelID before creating top-level windows."""
    global _IDENTITY_SET
    if sys.platform != "win32":
        return False
    with _IDENTITY_LOCK:
        if _IDENTITY_SET:
            return True
        try:
            import ctypes

            hr = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                ctypes.c_wchar_p(app_id)
            )
            if hr == 0:
                _IDENTITY_SET = True
                return True
            log.debug(
                "SetCurrentProcessExplicitAppUserModelID returned %#x",
                hr & 0xFFFFFFFF,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not set Windows AppUserModelID: %s", exc)
            return False


def platform_support() -> tuple[bool, str]:
    """Return `(supported, explanation)` for external media session mode on this OS."""
    if sys.platform == "win32":
        return (
            True,
            "Publishes track metadata, playback status, and timeline to Windows SMTC "
            "(global media session). No fake audio; leaves other media sessions untouched.",
        )
    if sys.platform.startswith("linux"):
        return (
            True,
            "Publishes track metadata, playback status, and position via Linux MPRIS2 "
            "D-Bus. No fake audio; leaves other players untouched.",
        )
    if sys.platform == "darwin":
        return (
            False,
            "Not available on macOS: MPNowPlayingInfoCenter is a single global slot that "
            "would hijack media keys from your active player. Use Last.fm or ListenBrainz "
            "(including custom Multi-Scrobbler URL) instead.",
        )
    return (False, f"External media session publishing is not supported on {sys.platform}.")


class ExternalMediaSession:
    """Base interface for an OS-native external media session."""

    backend_name: str = "None"

    def __init__(self) -> None:
        self.last_error: str | None = None
        self.active: bool = False

    def update_now_playing(
        self,
        artist: str,
        title: str,
        album: str | None = None,
        duration: float | None = None,
        track_id: str | None = None,
        position: float = 0.0,
    ) -> bool:
        return False

    def clear(self) -> None:
        self.active = False

    def close(self) -> None:
        self.clear()


# ================================================================ Windows SMTC

# Exact WinRT/COM GUIDs verified against Windows SDK 10.0.26100.0 windows.media.h
_IID_IUNKNOWN = "00000000-0000-0000-c000-000000000046"
_IID_IAGILEOBJECT = "94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90"
_IID_SMTC_INTEROP = "ddb0472d-c911-4a1f-86d9-dc3d71a95f5a"
_IID_SMTC = "99fa3ff4-1742-42a6-902e-087d41f965ec"
_IID_SMTC2 = "ea98d2f6-7f3c-4af2-a586-72889808efb1"
_IID_SMTC_TIMELINE = "5125316a-c3a2-475b-8507-93534dc88f15"
_IID_SMTC_BUTTON_HANDLER = "0557e996-7b23-5bae-aa81-ea0d671143a4"
_IID_MUSIC_PROPS2 = "00368462-97d3-44b9-b00f-008afcefaf18"
_IID_IPROPERTYSTORE = "886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"
_FMTID_APPUSERMODEL = "9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3"

_E_NOINTERFACE = -2147467262  # 0x80004002
_MEDIA_PLAYBACK_STATUS_STOPPED = 2
_MEDIA_PLAYBACK_STATUS_PLAYING = 3
_MEDIA_PLAYBACK_TYPE_MUSIC = 1
_TICKS_PER_SECOND = 10_000_000  # WinRT TimeSpan duration is in 100ns ticks


_WIN_GUID_CLS = None


def _win_guid_class():
    global _WIN_GUID_CLS
    if _WIN_GUID_CLS is None:
        import ctypes
        import ctypes.wintypes as wt

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wt.DWORD),
                ("Data2", wt.WORD),
                ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        _WIN_GUID_CLS = GUID
    return _WIN_GUID_CLS


def _win_make_guid(guid_str: str):
    import ctypes

    GUID = _win_guid_class()
    g = GUID()
    ctypes.memmove(ctypes.byref(g), uuid.UUID(guid_str).bytes_le, 16)
    return g, GUID


def _win_guid_to_str(guid_ptr) -> str:
    import ctypes

    raw = bytes(ctypes.cast(guid_ptr, ctypes.POINTER(ctypes.c_ubyte * 16)).contents)
    return str(uuid.UUID(bytes_le=raw))


def _win_vt(ptr, idx: int, restype, *argtypes):
    import ctypes

    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[idx])


def _set_window_app_user_model_id(hwnd: int, app_id: str) -> bool:
    """Stamp `System.AppUserModel.ID` onto the window's property store."""
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        import ctypes
        import ctypes.wintypes as wt

        iid_store, GUID = _win_make_guid(_IID_IPROPERTYSTORE)
        fmtid, _ = _win_make_guid(_FMTID_APPUSERMODEL)

        class PROPERTYKEY(ctypes.Structure):
            _fields_ = [("fmtid", GUID), ("pid", wt.DWORD)]

        class PROPVARIANT(ctypes.Structure):
            _fields_ = [
                ("vt", wt.WORD),
                ("wReserved1", wt.WORD),
                ("wReserved2", wt.WORD),
                ("wReserved3", wt.WORD),
                ("pwszVal", ctypes.c_wchar_p),
                ("pad", ctypes.c_uint64),
            ]

        store = ctypes.c_void_p()
        hr = ctypes.windll.shell32.SHGetPropertyStoreForWindow(
            wt.HWND(int(hwnd)),
            ctypes.byref(iid_store),
            ctypes.byref(store),
        )
        if hr != 0 or not store.value:
            return False

        try:
            pkey = PROPERTYKEY(fmtid, 5)  # PKEY_AppUserModel_ID
            pv = PROPVARIANT()
            pv.vt = 31  # VT_LPWSTR
            pv.pwszVal = app_id
            # IPropertyStore: [0]QI [1]AddRef [2]Release [3]GetCount [4]GetAt [5]GetValue [6]SetValue [7]Commit
            hr_set = _win_vt(
                store, 6, ctypes.c_long, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT)
            )(store, ctypes.byref(pkey), ctypes.byref(pv))
            if hr_set == 0:
                _win_vt(store, 7, ctypes.c_long)(store)
                return True
            return False
        finally:
            _win_vt(store, 2, ctypes.c_ulong)(store)
    except Exception as exc:  # noqa: BLE001
        log.debug("SHGetPropertyStoreForWindow failed: %s", exc)
        return False


class WindowsSMTCMediaSession(ExternalMediaSession):
    """Publishes track metadata and timeline to Windows SMTC via `GetForWindow`."""

    backend_name = "Windows SMTC"

    def __init__(
        self,
        hwnd_getter: Callable[[], int | None] | None = None,
        ui_dispatch: Callable[[Callable[[], None]], None] | None = None,
        app_id: str = APP_USER_MODEL_ID,
    ) -> None:
        super().__init__()
        self._hwnd_getter = hwnd_getter
        self._ui_dispatch = ui_dispatch
        self._app_id = app_id
        self._smtc = None
        self._smtc2 = None
        self._updater = None
        self._button_token: int | None = None
        self._button_handler_refs: tuple | None = None
        self._com_initialized = False
        self._last_track_key: tuple[str, str, str, str] | None = None
        self._lock = threading.Lock()

    def _run_on_owner_thread(self, fn: Callable[[], bool]) -> bool:
        """Run a WinRT SMTC call on the HWND's thread when a UI dispatcher is set."""
        if self._ui_dispatch is None:
            return fn()
        if threading.current_thread() is threading.main_thread():
            return fn()
        done = threading.Event()
        result_box = [False]

        def wrapper() -> None:
            try:
                result_box[0] = fn()
            finally:
                done.set()

        try:
            self._ui_dispatch(wrapper)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"UI dispatch failed: {exc}"
            return False
        if not done.wait(timeout=3.0):
            self.last_error = "Timed out waiting for UI thread SMTC dispatch"
            return False
        return result_box[0]

    def _create_button_pressed_handler(self):
        """Create a COM `ITypedEventHandler<SMTC, ButtonPressedEventArgs>` stub."""
        import ctypes

        _, GUID = _win_make_guid(_IID_IUNKNOWN)
        allowed = {_IID_IUNKNOWN, _IID_IAGILEOBJECT, _IID_SMTC_BUTTON_HANDLER}

        qi_t = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_void_p),
        )
        addref_t = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        release_t = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        invoke_t = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
        )

        class HandlerVtbl(ctypes.Structure):
            _fields_ = [
                ("QueryInterface", qi_t),
                ("AddRef", addref_t),
                ("Release", release_t),
                ("Invoke", invoke_t),
            ]

        class HandlerObj(ctypes.Structure):
            _fields_ = [("lpVtbl", ctypes.POINTER(HandlerVtbl))]

        refcount = [1]

        def _qi(this, riid, ppv):
            if _win_guid_to_str(riid) in allowed:
                ppv[0] = this
                refcount[0] += 1
                return 0
            ppv[0] = 0
            return _E_NOINTERFACE

        def _addref(this):
            refcount[0] += 1
            return refcount[0]

        def _release(this):
            refcount[0] = max(0, refcount[0] - 1)
            return refcount[0]

        def _invoke(this, sender, args):
            # Passive observer: acknowledge without hijacking external players.
            return 0

        cb_qi = qi_t(_qi)
        cb_addref = addref_t(_addref)
        cb_release = release_t(_release)
        cb_invoke = invoke_t(_invoke)
        vtbl = HandlerVtbl(cb_qi, cb_addref, cb_release, cb_invoke)
        obj = HandlerObj(ctypes.pointer(vtbl))
        # Keep every ctypes callback and struct alive while registered with WinRT.
        self._button_handler_refs = (obj, vtbl, cb_qi, cb_addref, cb_release, cb_invoke)
        return obj

    def _ensure_smtc(self) -> bool:
        if self._smtc is not None and self._updater is not None:
            return True

        import ctypes
        import ctypes.wintypes as wt

        ensure_windows_app_identity(self._app_id)

        hwnd = self._hwnd_getter() if self._hwnd_getter else None
        if not hwnd:
            self.last_error = "No application window handle available for Windows SMTC"
            return False

        # Always bind SMTC to the top-level window (GA_ROOT = 2).
        try:
            root_hwnd = ctypes.windll.user32.GetAncestor(wt.HWND(int(hwnd)), 2)
            if root_hwnd:
                hwnd = int(root_hwnd)
        except Exception:  # noqa: BLE001
            pass

        _set_window_app_user_model_id(int(hwnd), self._app_id)

        combase = ctypes.windll.combase

        # S_OK (0), S_FALSE (1), or RPC_E_CHANGED_MODE (0x80010106) are all fine.
        hr_init = combase.RoInitialize(0)
        if hr_init in (0, 1):
            self._com_initialized = True

        cls_name = "Windows.Media.SystemMediaTransportControls"
        hs_cls = ctypes.c_void_p()
        hr = combase.WindowsCreateString(
            ctypes.c_wchar_p(cls_name), len(cls_name), ctypes.byref(hs_cls)
        )
        if hr != 0:
            self.last_error = f"WindowsCreateString failed ({hr & 0xFFFFFFFF:#x})"
            return False

        iid_interop, GUID = _win_make_guid(_IID_SMTC_INTEROP)
        iid_smtc, _ = _win_make_guid(_IID_SMTC)
        interop = ctypes.c_void_p()
        try:
            hr = combase.RoGetActivationFactory(
                hs_cls, ctypes.byref(iid_interop), ctypes.byref(interop)
            )
        finally:
            combase.WindowsDeleteString(hs_cls)

        if hr != 0 or not interop.value:
            self.last_error = f"RoGetActivationFactory failed ({hr & 0xFFFFFFFF:#x})"
            return False

        smtc = ctypes.c_void_p()
        try:
            hr = _win_vt(
                interop,
                6,
                ctypes.c_long,
                wt.HWND,
                ctypes.POINTER(GUID),
                ctypes.POINTER(ctypes.c_void_p),
            )(interop, int(hwnd), ctypes.byref(iid_smtc), ctypes.byref(smtc))
        finally:
            _win_vt(interop, 2, ctypes.c_ulong)(interop)  # Release interop

        if hr != 0 or not smtc.value:
            self.last_error = f"SMTC GetForWindow failed ({hr & 0xFFFFFFFF:#x})"
            return False

        # Subscribe to ButtonPressed (required by Windows SMTC for desktop sessions).
        handler_obj = self._create_button_pressed_handler()
        token = ctypes.c_int64(0)
        hr_bp = _win_vt(
            smtc, 32, ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int64)
        )(smtc, ctypes.byref(handler_obj), ctypes.byref(token))
        if hr_bp == 0:
            self._button_token = int(token.value)
        else:
            log.warning(
                "SMTC add_ButtonPressed failed (%#x); continuing",
                hr_bp & 0xFFFFFFFF,
            )
            self._button_token = None
            self._button_handler_refs = None

        updater = ctypes.c_void_p()
        hr = _win_vt(smtc, 8, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
            smtc, ctypes.byref(updater)
        )
        if hr != 0 or not updater.value:
            if self._button_token is not None:
                _win_vt(smtc, 33, ctypes.c_long, ctypes.c_int64)(smtc, self._button_token)
                self._button_token = None
                self._button_handler_refs = None
            _win_vt(smtc, 2, ctypes.c_ulong)(smtc)
            self.last_error = f"SMTC get_DisplayUpdater failed ({hr & 0xFFFFFFFF:#x})"
            return False

        # Query ISystemMediaTransportControls2 for UpdateTimelineProperties.
        iid_smtc2, _ = _win_make_guid(_IID_SMTC2)
        smtc2 = ctypes.c_void_p()
        hr_q2 = _win_vt(
            smtc,
            0,
            ctypes.c_long,
            ctypes.POINTER(GUID),
            ctypes.POINTER(ctypes.c_void_p),
        )(smtc, ctypes.byref(iid_smtc2), ctypes.byref(smtc2))
        self._smtc2 = smtc2 if (hr_q2 == 0 and smtc2.value) else None

        self._smtc = smtc
        self._updater = updater
        self.last_error = None
        return True

    def _update_timeline(self, duration: float | None, position: float) -> None:
        """Push timeline properties so Windows GSMTC / Pano know track duration & position."""
        if self._smtc2 is None:
            return
        import ctypes

        combase = ctypes.windll.combase
        cls_name = "Windows.Media.SystemMediaTransportControlsTimelineProperties"
        hs = ctypes.c_void_p()
        if combase.WindowsCreateString(
            ctypes.c_wchar_p(cls_name), len(cls_name), ctypes.byref(hs)
        ) != 0:
            return

        insp = ctypes.c_void_p()
        try:
            hr = combase.RoActivateInstance(hs, ctypes.byref(insp))
        finally:
            combase.WindowsDeleteString(hs)

        if hr != 0 or not insp.value:
            return

        iid_tl, GUID = _win_make_guid(_IID_SMTC_TIMELINE)
        tl = ctypes.c_void_p()
        try:
            hr_qi = _win_vt(
                insp,
                0,
                ctypes.c_long,
                ctypes.POINTER(GUID),
                ctypes.POINTER(ctypes.c_void_p),
            )(insp, ctypes.byref(iid_tl), ctypes.byref(tl))
        finally:
            _win_vt(insp, 2, ctypes.c_ulong)(insp)

        if hr_qi != 0 or not tl.value:
            return

        try:
            if duration and duration > 0:
                end_ticks = int(duration * _TICKS_PER_SECOND)
                pos_ticks = max(0, min(int(max(0.0, position) * _TICKS_PER_SECOND), end_ticks))
            else:
                end_ticks = 0
                pos_ticks = 0

            # ISystemMediaTransportControlsTimelineProperties:
            # [7] put_StartTime, [9] put_EndTime, [11] put_MinSeekTime,
            # [13] put_MaxSeekTime, [15] put_Position (TimeSpan is int64 100ns ticks)
            _win_vt(tl, 7, ctypes.c_long, ctypes.c_int64)(tl, 0)
            _win_vt(tl, 9, ctypes.c_long, ctypes.c_int64)(tl, end_ticks)
            _win_vt(tl, 11, ctypes.c_long, ctypes.c_int64)(tl, 0)
            _win_vt(tl, 13, ctypes.c_long, ctypes.c_int64)(tl, end_ticks)
            _win_vt(tl, 15, ctypes.c_long, ctypes.c_int64)(tl, pos_ticks)

            # ISystemMediaTransportControls2::UpdateTimelineProperties [12]
            hr_upd = _win_vt(self._smtc2, 12, ctypes.c_long, ctypes.c_void_p)(
                self._smtc2, tl
            )
            if hr_upd != 0:
                log.debug("SMTC UpdateTimelineProperties failed (%#x)", hr_upd & 0xFFFFFFFF)
        finally:
            _win_vt(tl, 2, ctypes.c_ulong)(tl)

    def update_now_playing(
        self,
        artist: str,
        title: str,
        album: str | None = None,
        duration: float | None = None,
        track_id: str | None = None,
        position: float = 0.0,
    ) -> bool:
        track_key = (artist, title, album or "", track_id or "")
        if self.active and self._last_track_key == track_key:
            # Already published and playing: never re-send Position=0 on refresh,
            # which would cause external scrobblers to treat it as a track repeat.
            return True

        def _do() -> bool:
            import ctypes

            with self._lock:
                if not self._ensure_smtc():
                    return False

                combase = ctypes.windll.combase

                def set_hstr(ptr: ctypes.c_void_p, idx: int, text: str) -> int:
                    hs = ctypes.c_void_p()
                    hr_s = combase.WindowsCreateString(
                        ctypes.c_wchar_p(text), len(text), ctypes.byref(hs)
                    )
                    if hr_s != 0:
                        return hr_s
                    try:
                        return _win_vt(ptr, idx, ctypes.c_long, ctypes.c_void_p)(ptr, hs)
                    finally:
                        combase.WindowsDeleteString(hs)

                # Enable SMTC and basic transport controls (required for GSMTC visibility).
                # Leave ChannelUp/ChannelDown disabled so Pano doesn't classify as Spotify Remote.
                _win_vt(self._smtc, 11, ctypes.c_long, ctypes.c_ubyte)(self._smtc, 1)  # IsEnabled
                _win_vt(self._smtc, 13, ctypes.c_long, ctypes.c_ubyte)(self._smtc, 1)  # IsPlayEnabled
                _win_vt(self._smtc, 15, ctypes.c_long, ctypes.c_ubyte)(self._smtc, 1)  # IsStopEnabled
                _win_vt(self._smtc, 17, ctypes.c_long, ctypes.c_ubyte)(self._smtc, 1)  # IsPauseEnabled

                # DisplayUpdater: Type = Music (1), AppMediaId (9), MusicProperties (12)
                _win_vt(self._updater, 7, ctypes.c_long, ctypes.c_int)(
                    self._updater, _MEDIA_PLAYBACK_TYPE_MUSIC
                )
                if track_id:
                    set_hstr(self._updater, 9, str(track_id))

                music = ctypes.c_void_p()
                hr = _win_vt(
                    self._updater, 12, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p)
                )(self._updater, ctypes.byref(music))
                if hr != 0 or not music.value:
                    self.last_error = f"get_MusicProperties failed ({hr & 0xFFFFFFFF:#x})"
                    return False

                try:
                    # [7] put_Title, [9] put_AlbumArtist, [11] put_Artist
                    set_hstr(music, 7, title)
                    set_hstr(music, 11, artist)
                    set_hstr(music, 9, "")

                    # IMusicDisplayProperties2 for AlbumTitle [7]
                    iid_m2, GUID = _win_make_guid(_IID_MUSIC_PROPS2)
                    music2 = ctypes.c_void_p()
                    hr_qi = _win_vt(
                        music,
                        0,
                        ctypes.c_long,
                        ctypes.POINTER(GUID),
                        ctypes.POINTER(ctypes.c_void_p),
                    )(music, ctypes.byref(iid_m2), ctypes.byref(music2))
                    if hr_qi == 0 and music2.value:
                        try:
                            set_hstr(music2, 7, album or "")
                        finally:
                            _win_vt(music2, 2, ctypes.c_ulong)(music2)
                finally:
                    _win_vt(music, 2, ctypes.c_ulong)(music)

                hr_upd = _win_vt(self._updater, 17, ctypes.c_long)(self._updater)
                if hr_upd != 0:
                    self.last_error = f"DisplayUpdater.Update failed ({hr_upd & 0xFFFFFFFF:#x})"
                    return False

                # Push timeline properties (duration + current position)
                self._update_timeline(duration, position)

                # Mark PlaybackStatus = Playing (3) AFTER metadata & timeline are ready
                hr_st = _win_vt(self._smtc, 7, ctypes.c_long, ctypes.c_int)(
                    self._smtc, _MEDIA_PLAYBACK_STATUS_PLAYING
                )
                if hr_st != 0:
                    self.last_error = f"put_PlaybackStatus failed ({hr_st & 0xFFFFFFFF:#x})"
                    return False

                self._last_track_key = track_key
                self.active = True
                self.last_error = None
                return True

        try:
            return self._run_on_owner_thread(_do)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            log.warning("Windows SMTC update failed: %s", self.last_error)
            return False

    def read_state(self) -> dict[str, object] | None:
        """Read back current SMTC properties from the COM object (for diagnostics/tests)."""
        if self._smtc is None or self._updater is None:
            return None

        state: dict[str, object] = {}

        def _do() -> bool:
            import ctypes

            combase = ctypes.windll.combase

            def get_hstr(ptr: ctypes.c_void_p, idx: int) -> str:
                hs = ctypes.c_void_p()
                if _win_vt(ptr, idx, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
                    ptr, ctypes.byref(hs)
                ) != 0 or not hs.value:
                    return ""
                try:
                    length = ctypes.c_uint32(0)
                    combase.WindowsGetStringRawBuffer.restype = ctypes.c_wchar_p
                    buf = combase.WindowsGetStringRawBuffer(hs, ctypes.byref(length))
                    return str(buf or "")
                finally:
                    combase.WindowsDeleteString(hs)

            with self._lock:
                if self._smtc is None or self._updater is None:
                    return False
                status = ctypes.c_int(0)
                is_enabled = ctypes.c_ubyte(0)
                is_play = ctypes.c_ubyte(0)
                is_pause = ctypes.c_ubyte(0)
                is_stop = ctypes.c_ubyte(0)
                _win_vt(self._smtc, 6, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(
                    self._smtc, ctypes.byref(status)
                )
                _win_vt(self._smtc, 10, ctypes.c_long, ctypes.POINTER(ctypes.c_ubyte))(
                    self._smtc, ctypes.byref(is_enabled)
                )
                _win_vt(self._smtc, 12, ctypes.c_long, ctypes.POINTER(ctypes.c_ubyte))(
                    self._smtc, ctypes.byref(is_play)
                )
                _win_vt(self._smtc, 16, ctypes.c_long, ctypes.POINTER(ctypes.c_ubyte))(
                    self._smtc, ctypes.byref(is_pause)
                )
                _win_vt(self._smtc, 14, ctypes.c_long, ctypes.POINTER(ctypes.c_ubyte))(
                    self._smtc, ctypes.byref(is_stop)
                )
                title = artist = album = ""
                music = ctypes.c_void_p()
                if (
                    _win_vt(
                        self._updater, 12, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p)
                    )(self._updater, ctypes.byref(music))
                    == 0
                    and music.value
                ):
                    try:
                        title = get_hstr(music, 6)
                        artist = get_hstr(music, 10)
                        iid_m2, GUID = _win_make_guid(_IID_MUSIC_PROPS2)
                        music2 = ctypes.c_void_p()
                        if (
                            _win_vt(
                                music,
                                0,
                                ctypes.c_long,
                                ctypes.POINTER(GUID),
                                ctypes.POINTER(ctypes.c_void_p),
                            )(music, ctypes.byref(iid_m2), ctypes.byref(music2))
                            == 0
                            and music2.value
                        ):
                            try:
                                album = get_hstr(music2, 6)
                            finally:
                                _win_vt(music2, 2, ctypes.c_ulong)(music2)
                    finally:
                        _win_vt(music, 2, ctypes.c_ulong)(music)
                state.update(
                    {
                        "app_id": self._app_id,
                        "playback_status": status.value,
                        "is_enabled": bool(is_enabled.value),
                        "is_play_enabled": bool(is_play.value),
                        "is_pause_enabled": bool(is_pause.value),
                        "is_stop_enabled": bool(is_stop.value),
                        "button_pressed_registered": self._button_token is not None,
                        "timeline_supported": self._smtc2 is not None,
                        "title": title,
                        "artist": artist,
                        "album": album,
                    }
                )
                return True

        return state if self._run_on_owner_thread(_do) else None

    def clear(self) -> None:
        if self._smtc is None or self._updater is None:
            self._last_track_key = None
            self.active = False
            return

        def _do() -> bool:
            import ctypes

            with self._lock:
                if self._smtc is None or self._updater is None:
                    return True

                # PlaybackStatus = Stopped (2), ClearAll (16), Update (17), IsEnabled = False (11)
                _win_vt(self._smtc, 7, ctypes.c_long, ctypes.c_int)(
                    self._smtc, _MEDIA_PLAYBACK_STATUS_STOPPED
                )
                self._update_timeline(None, 0.0)
                _win_vt(self._updater, 16, ctypes.c_long)(self._updater)
                _win_vt(self._updater, 17, ctypes.c_long)(self._updater)
                _win_vt(self._smtc, 11, ctypes.c_long, ctypes.c_ubyte)(self._smtc, 0)
                self._last_track_key = None
                self.active = False
                return True

        try:
            self._run_on_owner_thread(_do)
        except Exception:  # noqa: BLE001
            self._last_track_key = None
            self.active = False

    def close(self) -> None:
        self.clear()

        def _release() -> bool:
            import ctypes

            with self._lock:
                if self._smtc is not None and self._button_token is not None:
                    try:
                        _win_vt(self._smtc, 33, ctypes.c_long, ctypes.c_int64)(
                            self._smtc, self._button_token
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    self._button_token = None
                self._button_handler_refs = None

                if self._smtc2 is not None:
                    _win_vt(self._smtc2, 2, ctypes.c_ulong)(self._smtc2)
                    self._smtc2 = None
                if self._updater is not None:
                    _win_vt(self._updater, 2, ctypes.c_ulong)(self._updater)
                    self._updater = None
                if self._smtc is not None:
                    _win_vt(self._smtc, 2, ctypes.c_ulong)(self._smtc)
                    self._smtc = None
                self._last_track_key = None
                self.active = False
                return True

        try:
            self._run_on_owner_thread(_release)
        except Exception:  # noqa: BLE001
            self._last_track_key = None
            self.active = False


# ================================================================ Linux MPRIS2


def _dbus_pad(buf: bytearray, align: int) -> None:
    rem = len(buf) % align
    if rem:
        buf.extend(b"\x00" * (align - rem))


def _dbus_pack_string(buf: bytearray, text: str) -> None:
    _dbus_pad(buf, 4)
    encoded = text.encode("utf-8")
    buf.extend(struct.pack("<I", len(encoded)))
    buf.extend(encoded)
    buf.append(0)


def _dbus_pack_sig(buf: bytearray, sig: str) -> None:
    encoded = sig.encode("ascii")
    buf.append(len(encoded))
    buf.extend(encoded)
    buf.append(0)


def _dbus_pack_variant(buf: bytearray, sig: str, value) -> None:
    _dbus_pack_sig(buf, sig)
    if sig == "s":
        _dbus_pack_string(buf, str(value))
    elif sig == "o":
        _dbus_pack_string(buf, str(value))
    elif sig == "b":
        _dbus_pad(buf, 4)
        buf.extend(struct.pack("<I", 1 if value else 0))
    elif sig == "u":
        _dbus_pad(buf, 4)
        buf.extend(struct.pack("<I", int(value)))
    elif sig == "x":
        _dbus_pad(buf, 8)
        buf.extend(struct.pack("<q", int(value)))
    elif sig == "d":
        _dbus_pad(buf, 8)
        buf.extend(struct.pack("<d", float(value)))
    elif sig == "as":
        _dbus_pad(buf, 4)
        arr = bytearray()
        for item in value:
            _dbus_pack_string(arr, str(item))
        buf.extend(struct.pack("<I", len(arr)))
        buf.extend(arr)
    elif sig == "a{sv}":
        _dbus_pack_dict_sv(buf, value)


def _dbus_pack_dict_sv(buf: bytearray, mapping: dict[str, tuple[str, object]]) -> None:
    _dbus_pad(buf, 4)
    len_pos = len(buf)
    buf.extend(b"\x00\x00\x00\x00")
    _dbus_pad(buf, 8)
    start = len(buf)
    for key, (sig, val) in mapping.items():
        _dbus_pad(buf, 8)
        _dbus_pack_string(buf, key)
        _dbus_pack_variant(buf, sig, val)
    end = len(buf)
    struct.pack_into("<I", buf, len_pos, end - start)


def _dbus_build_message(
    msg_type: int,
    serial: int,
    headers: list[tuple[int, str, object]],
    body: bytes = b"",
    flags: int = 0,
) -> bytes:
    hdr = bytearray()
    hdr.extend(struct.pack("<BBBBII", ord("l"), msg_type, flags, 1, len(body), serial))
    # Header fields array: a(yv)
    arr = bytearray()
    for code, sig, val in headers:
        _dbus_pad(arr, 8)
        arr.append(code)
        _dbus_pack_variant(arr, sig, val)
    hdr.extend(struct.pack("<I", len(arr)))
    hdr.extend(arr)
    _dbus_pad(hdr, 8)
    hdr.extend(body)
    return bytes(hdr)


def _dbus_align(offset: int, align: int) -> int:
    rem = offset % align
    return offset if rem == 0 else offset + (align - rem)


def _dbus_read_string(data: bytes, offset: int, endian: str) -> tuple[str, int] | None:
    offset = _dbus_align(offset, 4)
    if offset + 4 > len(data):
        return None
    (length,) = struct.unpack_from(endian + "I", data, offset)
    offset += 4
    if offset + length + 1 > len(data):
        return None
    text = data[offset : offset + length].decode("utf-8", errors="replace")
    return text, offset + length + 1


def _dbus_parse_header_fields(fields_raw: bytes, endian: str) -> dict[int, object]:
    """Parse D-Bus header fields `a(yv)` into `{field_code: value}`."""
    result: dict[int, object] = {}
    pos = 0
    total = len(fields_raw)
    while pos < total:
        pos = _dbus_align(pos, 8)
        if pos + 2 > total:
            break
        code = fields_raw[pos]
        pos += 1
        sig_len = fields_raw[pos]
        pos += 1
        if pos + sig_len + 1 > total:
            break
        sig = fields_raw[pos : pos + sig_len].decode("ascii", errors="ignore")
        pos += sig_len + 1
        if sig in ("s", "o"):
            parsed = _dbus_read_string(fields_raw, pos, endian)
            if parsed is None:
                break
            val, pos = parsed
            result[code] = val
        elif sig == "g":
            if pos >= total:
                break
            g_len = fields_raw[pos]
            pos += 1
            if pos + g_len + 1 > total:
                break
            result[code] = fields_raw[pos : pos + g_len].decode("ascii", errors="ignore")
            pos += g_len + 1
        elif sig == "u":
            pos = _dbus_align(pos, 4)
            if pos + 4 > total:
                break
            (val,) = struct.unpack_from(endian + "I", fields_raw, pos)
            pos += 4
            result[code] = val
        else:
            break
    return result


def _parse_dbus_unix_address(address: str) -> tuple[str, bool] | None:
    """Parse `unix:path=/run/user/1000/bus` or `unix:abstract=/tmp/dbus-...`."""
    for entry in address.split(";"):
        if not entry.startswith("unix:"):
            continue
        params = entry[5:]
        for part in params.split(","):
            if part.startswith("path="):
                return part[5:], False
            if part.startswith("abstract="):
                return "\x00" + part[9:], True
    return None


_MPRIS_INTROSPECT_XML = """<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
"http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="org.freedesktop.DBus.Introspectable">
    <method name="Introspect"><arg name="data" type="s" direction="out"/></method>
  </interface>
  <interface name="org.freedesktop.DBus.Peer">
    <method name="Ping"/>
  </interface>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get">
      <arg name="interface" type="s" direction="in"/>
      <arg name="property" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="GetAll">
      <arg name="interface" type="s" direction="in"/>
      <arg name="properties" type="a{sv}" direction="out"/>
    </method>
    <signal name="PropertiesChanged">
      <arg name="interface" type="s"/>
      <arg name="changed_properties" type="a{sv}"/>
      <arg name="invalidated_properties" type="as"/>
    </signal>
  </interface>
  <interface name="org.mpris.MediaPlayer2">
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Position" type="x" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""


class LinuxMPRISMediaSession(ExternalMediaSession):
    """Lightweight pure-stdlib MPRIS2 publisher over the Linux D-Bus session bus."""

    backend_name = "Linux MPRIS2"
    BUS_NAME = "org.mpris.MediaPlayer2.chocolate_egg_cream"
    OBJECT_PATH = "/org/mpris/MediaPlayer2"

    def __init__(self) -> None:
        super().__init__()
        self._sock: socket.socket | None = None
        self._serial = 1
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._status = "Stopped"
        self._metadata: dict[str, tuple[str, object]] = {}
        self._track_started_mono: float | None = None
        self._track_base_pos: float = 0.0
        self._last_track_key: tuple[str, str, str, str] | None = None

    def _next_serial(self) -> int:
        s = self._serial
        self._serial += 1
        return s

    def _connect(self) -> bool:
        if self._sock is not None:
            return True
        addr = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
        parsed = _parse_dbus_unix_address(addr)
        if parsed is None:
            self.last_error = "DBUS_SESSION_BUS_ADDRESS is not set or unsupported"
            return False
        path, _abstract = parsed
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect(path)
            uid_hex = str(os.getuid()).encode("ascii").hex().encode("ascii")
            sock.sendall(b"\x00AUTH EXTERNAL " + uid_hex + b"\r\n")
            resp = sock.recv(256)
            if not resp.startswith(b"OK "):
                sock.close()
                self.last_error = f"D-Bus auth refused: {resp[:80]!r}"
                return False
            sock.sendall(b"BEGIN\r\n")

            # Send org.freedesktop.DBus.Hello
            hello = _dbus_build_message(
                1,
                self._next_serial(),
                [
                    (1, "o", "/org/freedesktop/DBus"),
                    (2, "s", "org.freedesktop.DBus"),
                    (3, "s", "Hello"),
                    (6, "s", "org.freedesktop.DBus"),
                ],
            )
            sock.sendall(hello)

            # RequestName org.mpris.MediaPlayer2.chocolate_egg_cream
            req_body = bytearray()
            _dbus_pack_string(req_body, self.BUS_NAME)
            _dbus_pad(req_body, 4)
            req_body.extend(struct.pack("<I", 4))  # DBUS_NAME_FLAG_DO_NOT_QUEUE
            req = _dbus_build_message(
                1,
                self._next_serial(),
                [
                    (1, "o", "/org/freedesktop/DBus"),
                    (2, "s", "org.freedesktop.DBus"),
                    (3, "s", "RequestName"),
                    (6, "s", "org.freedesktop.DBus"),
                    (8, "s", "su"),
                ],
                bytes(req_body),
            )
            sock.sendall(req)

            self._sock = sock
            self._stop.clear()
            self._reader_thread = threading.Thread(
                target=self._serve_loop, daemon=True, name="mpris-dbus"
            )
            self._reader_thread.start()
            self.last_error = None
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            log.warning("Linux MPRIS2 connect failed: %s", self.last_error)
            return False

    def _serve_loop(self) -> None:
        """D-Bus responder for Properties.Get / GetAll / Introspect / Ping."""
        sock = self._sock
        if sock is None:
            return
        sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                head = self._recv_exact(sock, 16)
                if not head:
                    break
                endian = "<" if head[0] == ord("l") else ">"
                msg_type = head[1]
                body_len, serial, fields_len = struct.unpack(endian + "III", head[4:16])
                pad = (8 - (fields_len % 8)) % 8
                rest = self._recv_exact(sock, fields_len + pad + body_len)
                if rest is None:
                    break
                if msg_type == 1:  # METHOD_CALL
                    self._handle_method_call(
                        sock,
                        serial,
                        rest[:fields_len],
                        rest[fields_len + pad :],
                        endian,
                    )
            except socket.timeout:
                continue
            except OSError:
                break

    @staticmethod
    def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
        data = bytearray()
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                return None
            data.extend(chunk)
        return bytes(data)

    def _build_method_reply(
        self, serial: int, fields_raw: bytes, body_raw: bytes, endian: str = "<"
    ) -> bytes | None:
        """Build a D-Bus METHOD_RETURN or ERROR reply for an incoming METHOD_CALL."""
        fields = _dbus_parse_header_fields(fields_raw, endian)
        sender = fields.get(7)  # HEADER_FIELD_SENDER
        member = fields.get(3)  # HEADER_FIELD_MEMBER
        iface = fields.get(2)   # HEADER_FIELD_INTERFACE
        if not isinstance(sender, str) or not isinstance(member, str):
            return None

        if member == "Ping":
            return _dbus_build_message(
                2, self._next_serial(), [(5, "u", serial), (6, "s", sender)]
            )

        if member == "Introspect":
            body = bytearray()
            _dbus_pack_string(body, _MPRIS_INTROSPECT_XML)
            return _dbus_build_message(
                2,
                self._next_serial(),
                [(5, "u", serial), (6, "s", sender), (8, "s", "s")],
                bytes(body),
            )

        if member == "GetAll" and (iface in (None, "org.freedesktop.DBus.Properties")):
            parsed_iface = _dbus_read_string(body_raw, 0, endian)
            req_iface = parsed_iface[0] if parsed_iface else ""
            props = (
                self._player_props()
                if req_iface == "org.mpris.MediaPlayer2.Player"
                else self._root_props()
            )
            body = bytearray()
            _dbus_pack_dict_sv(body, props)
            return _dbus_build_message(
                2,
                self._next_serial(),
                [(5, "u", serial), (6, "s", sender), (8, "s", "a{sv}")],
                bytes(body),
            )

        if member == "Get" and (iface in (None, "org.freedesktop.DBus.Properties")):
            parsed_iface = _dbus_read_string(body_raw, 0, endian)
            if parsed_iface is None:
                return None
            req_iface, next_pos = parsed_iface
            parsed_prop = _dbus_read_string(body_raw, next_pos, endian)
            if parsed_prop is None:
                return None
            prop_name, _ = parsed_prop
            props = (
                self._player_props()
                if req_iface == "org.mpris.MediaPlayer2.Player"
                else self._root_props()
            )
            if prop_name in props:
                sig, val = props[prop_name]
                body = bytearray()
                _dbus_pack_variant(body, sig, val)
                return _dbus_build_message(
                    2,
                    self._next_serial(),
                    [(5, "u", serial), (6, "s", sender), (8, "s", "v")],
                    bytes(body),
                )
            # Unknown property: return D-Bus ERROR so clients like zbus don't block/timeout.
            err_body = bytearray()
            _dbus_pack_string(err_body, f"Unknown property: {req_iface}.{prop_name}")
            return _dbus_build_message(
                3,  # ERROR
                self._next_serial(),
                [
                    (4, "s", "org.freedesktop.DBus.Error.InvalidArgs"),
                    (5, "u", serial),
                    (6, "s", sender),
                    (8, "s", "s"),
                ],
                bytes(err_body),
            )

        return None

    def _handle_method_call(
        self,
        sock: socket.socket,
        serial: int,
        fields_raw: bytes,
        body_raw: bytes,
        endian: str = "<",
    ) -> None:
        reply = self._build_method_reply(serial, fields_raw, body_raw, endian)
        if reply is None:
            return
        try:
            with self._lock:
                sock.sendall(reply)
        except OSError:
            pass

    def _current_position_us(self) -> int:
        if self._status != "Playing" or self._track_started_mono is None:
            return 0
        elapsed = max(0.0, self._track_base_pos + (time.monotonic() - self._track_started_mono))
        return int(elapsed * 1_000_000)

    def _root_props(self) -> dict[str, tuple[str, object]]:
        return {
            "CanQuit": ("b", False),
            "CanRaise": ("b", False),
            "HasTrackList": ("b", False),
            "Identity": ("s", "Chocolate Egg Cream"),
            "DesktopEntry": ("s", "chocolate-egg-cream"),
            "SupportedUriSchemes": ("as", []),
            "SupportedMimeTypes": ("as", []),
        }

    def _player_props(self) -> dict[str, tuple[str, object]]:
        return {
            "PlaybackStatus": ("s", self._status),
            "Metadata": ("a{sv}", self._metadata),
            "Position": ("x", self._current_position_us()),
            "Volume": ("d", 1.0),
            "Rate": ("d", 1.0),
            "MinimumRate": ("d", 1.0),
            "MaximumRate": ("d", 1.0),
            "CanGoNext": ("b", False),
            "CanGoPrevious": ("b", False),
            "CanPlay": ("b", False),
            "CanPause": ("b", False),
            "CanSeek": ("b", False),
            "CanControl": ("b", False),
        }

    def _emit_properties_changed(self) -> bool:
        if self._sock is None:
            return False
        body = bytearray()
        _dbus_pack_string(body, "org.mpris.MediaPlayer2.Player")
        _dbus_pack_dict_sv(
            body,
            {
                "PlaybackStatus": ("s", self._status),
                "Metadata": ("a{sv}", self._metadata),
            },
        )
        _dbus_pad(body, 4)
        body.extend(struct.pack("<I", 0))  # invalidated_properties: empty as

        sig = _dbus_build_message(
            4,  # SIGNAL
            self._next_serial(),
            [
                (1, "o", self.OBJECT_PATH),
                (2, "s", "org.freedesktop.DBus.Properties"),
                (3, "s", "PropertiesChanged"),
                (8, "s", "sa{sv}as"),
            ],
            bytes(body),
        )
        try:
            self._sock.sendall(sig)
            return True
        except OSError as exc:
            self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            return False

    def update_now_playing(
        self,
        artist: str,
        title: str,
        album: str | None = None,
        duration: float | None = None,
        track_id: str | None = None,
        position: float = 0.0,
    ) -> bool:
        track_key = (artist, title, album or "", track_id or "")
        with self._lock:
            if self.active and self._last_track_key == track_key:
                return True
            if not self._connect():
                return False
            safe_id = re.sub(r"[^A-Za-z0-9_]", "_", track_id or uuid.uuid4().hex)[:48]
            meta: dict[str, tuple[str, object]] = {
                "mpris:trackid": ("o", f"/com/chocolateeggcream/track/{safe_id}"),
                "xesam:title": ("s", title),
                "xesam:artist": ("as", [artist]),
            }
            if album:
                meta["xesam:album"] = ("s", album)
            if duration and duration > 0:
                meta["mpris:length"] = ("x", int(duration * 1_000_000))
            self._status = "Playing"
            self._metadata = meta
            self._track_started_mono = time.monotonic()
            self._track_base_pos = max(0.0, float(position or 0.0))
            ok = self._emit_properties_changed()
            self.active = ok
            if ok:
                self._last_track_key = track_key
            return ok

    def clear(self) -> None:
        with self._lock:
            self._status = "Stopped"
            self._metadata = {}
            self._track_started_mono = None
            self._track_base_pos = 0.0
            self._last_track_key = None
            if self._sock is not None:
                self._emit_properties_changed()
            self.active = False

    def close(self) -> None:
        self.clear()
        self._stop.set()
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None


# ===================================================================== factory


def create_external_session(
    *,
    hwnd_getter: Callable[[], int | None] | None = None,
    ui_dispatch: Callable[[Callable[[], None]], None] | None = None,
) -> ExternalMediaSession:
    """Create the native external media session for the current OS."""
    if sys.platform == "win32":
        return WindowsSMTCMediaSession(hwnd_getter=hwnd_getter, ui_dispatch=ui_dispatch)
    if sys.platform.startswith("linux"):
        return LinuxMPRISMediaSession()
    session = ExternalMediaSession()
    _, reason = platform_support()
    session.last_error = reason
    return session
