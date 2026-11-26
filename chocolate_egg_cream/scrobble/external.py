"""Optional external scrobbler mode (Pano Scrobbler / native OS media session).

Publishes confirmed Now Playing track metadata to the OS's native media
session API without playing fake/silent audio and without interfering with
other running media players:

* **Windows 10/11**: Native Windows SMTC (`SystemMediaTransportControls`) via
  `ISystemMediaTransportControlsInterop::GetForWindow(hwnd)` using stdlib
  `ctypes` + `combase.dll`. Creates a separate Chocolate Egg Cream session
  visible to Pano Scrobbler Desktop and Windows media controls.
* **Linux**: Native MPRIS2 (`org.mpris.MediaPlayer2.chocolate_egg_cream`) over
  the user's D-Bus session bus using pure Python stdlib (`socket` + `struct`).
  Each MPRIS player has its own bus name, so existing players are untouched.
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
import uuid
from typing import Callable

log = logging.getLogger(__name__)


def platform_support() -> tuple[bool, str]:
    """Return `(supported, explanation)` for external scrobbler mode on this OS."""
    if sys.platform == "win32":
        return (
            True,
            "Publishes track metadata to Windows SMTC (compatible with Pano Scrobbler). "
            "No fake audio; leaves other media sessions untouched.",
        )
    if sys.platform.startswith("linux"):
        return (
            True,
            "Publishes track metadata via Linux MPRIS2 D-Bus (compatible with Pano Scrobbler "
            "and MPRIS scrobblers). No fake audio; leaves other players untouched.",
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
    """Base interface for an OS-native external scrobbler media session."""

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
    ) -> bool:
        return False

    def clear(self) -> None:
        self.active = False

    def close(self) -> None:
        self.clear()


# ================================================================ Windows SMTC


class WindowsSMTCMediaSession(ExternalMediaSession):
    """Publishes track metadata to Windows SMTC via `ISystemMediaTransportControlsInterop`."""

    backend_name = "Windows SMTC"

    def __init__(
        self,
        hwnd_getter: Callable[[], int | None] | None = None,
        ui_dispatch: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        super().__init__()
        self._hwnd_getter = hwnd_getter
        self._ui_dispatch = ui_dispatch
        self._smtc = None
        self._updater = None
        self._com_initialized = False
        self._lock = threading.Lock()

    def _run_on_owner_thread(self, fn: Callable[[], bool]) -> bool:
        """Run a WinRT SMTC call on the HWND's thread when a UI dispatcher is set."""
        if self._ui_dispatch is None:
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
        done.wait(timeout=3.0)
        return result_box[0]

    def _ensure_smtc(self) -> bool:
        if self._smtc is not None and self._updater is not None:
            return True

        import ctypes
        import ctypes.wintypes as wt

        hwnd = self._hwnd_getter() if self._hwnd_getter else None
        if not hwnd:
            self.last_error = "No application window handle available for Windows SMTC"
            return False

        combase = ctypes.windll.combase

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wt.DWORD),
                ("Data2", wt.WORD),
                ("Data3", wt.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        def make_guid(s: str) -> GUID:
            g = GUID()
            ctypes.memmove(ctypes.byref(g), uuid.UUID(s).bytes_le, 16)
            return g

        def vt(ptr: ctypes.c_void_p, idx: int, restype, *argtypes):
            vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[idx])

        # S_OK (0) or S_FALSE (1) or RPC_E_CHANGED_MODE (0x80010106) are all fine.
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

        iid_interop = make_guid("ddb0472d-c911-4a1f-86d9-dc3d71a95f5a")
        iid_smtc = make_guid("99fa3ff4-1742-42a6-902e-087d41f965ec")
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
            hr = vt(
                interop,
                6,
                ctypes.c_long,
                wt.HWND,
                ctypes.POINTER(GUID),
                ctypes.POINTER(ctypes.c_void_p),
            )(interop, int(hwnd), ctypes.byref(iid_smtc), ctypes.byref(smtc))
        finally:
            vt(interop, 2, ctypes.c_ulong)(interop)  # Release interop

        if hr != 0 or not smtc.value:
            self.last_error = f"SMTC GetForWindow failed ({hr & 0xFFFFFFFF:#x})"
            return False

        updater = ctypes.c_void_p()
        hr = vt(smtc, 8, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
            smtc, ctypes.byref(updater)
        )
        if hr != 0 or not updater.value:
            vt(smtc, 2, ctypes.c_ulong)(smtc)
            self.last_error = f"SMTC get_DisplayUpdater failed ({hr & 0xFFFFFFFF:#x})"
            return False

        self._smtc = smtc
        self._updater = updater
        self.last_error = None
        return True

    def update_now_playing(
        self,
        artist: str,
        title: str,
        album: str | None = None,
        duration: float | None = None,
        track_id: str | None = None,
    ) -> bool:
        del duration, track_id

        def _do() -> bool:
            import ctypes
            import ctypes.wintypes as wt

            with self._lock:
                if not self._ensure_smtc():
                    return False

                combase = ctypes.windll.combase

                class GUID(ctypes.Structure):
                    _fields_ = [
                        ("Data1", wt.DWORD),
                        ("Data2", wt.WORD),
                        ("Data3", wt.WORD),
                        ("Data4", ctypes.c_ubyte * 8),
                    ]

                def make_guid(s: str) -> GUID:
                    g = GUID()
                    ctypes.memmove(ctypes.byref(g), uuid.UUID(s).bytes_le, 16)
                    return g

                def vt(ptr: ctypes.c_void_p, idx: int, restype, *argtypes):
                    vtable = ctypes.cast(
                        ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
                    ).contents
                    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[idx])

                def set_hstr(ptr: ctypes.c_void_p, idx: int, text: str) -> int:
                    hs = ctypes.c_void_p()
                    hr_s = combase.WindowsCreateString(
                        ctypes.c_wchar_p(text), len(text), ctypes.byref(hs)
                    )
                    if hr_s != 0:
                        return hr_s
                    try:
                        return vt(ptr, idx, ctypes.c_long, ctypes.c_void_p)(ptr, hs)
                    finally:
                        combase.WindowsDeleteString(hs)

                # Enable SMTC and mark Playing (3)
                vt(self._smtc, 11, ctypes.c_long, ctypes.c_ubyte)(self._smtc, 1)
                vt(self._smtc, 7, ctypes.c_long, ctypes.c_int)(self._smtc, 3)

                # DisplayUpdater: Type = Music (1), get_MusicProperties (12)
                vt(self._updater, 7, ctypes.c_long, ctypes.c_int)(self._updater, 1)
                music = ctypes.c_void_p()
                hr = vt(self._updater, 12, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(
                    self._updater, ctypes.byref(music)
                )
                if hr != 0 or not music.value:
                    self.last_error = f"get_MusicProperties failed ({hr & 0xFFFFFFFF:#x})"
                    return False

                try:
                    # 7: put_Title, 9: put_AlbumArtist, 11: put_Artist
                    set_hstr(music, 7, title)
                    set_hstr(music, 11, artist)
                    set_hstr(music, 9, artist)

                    # IMusicDisplayProperties2 for AlbumTitle
                    iid_m2 = make_guid("00368462-97d3-44b9-b00f-008afcefaf18")
                    music2 = ctypes.c_void_p()
                    hr_qi = vt(
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
                            vt(music2, 2, ctypes.c_ulong)(music2)
                finally:
                    vt(music, 2, ctypes.c_ulong)(music)

                hr_upd = vt(self._updater, 17, ctypes.c_long)(self._updater)
                if hr_upd != 0:
                    self.last_error = f"DisplayUpdater.Update failed ({hr_upd & 0xFFFFFFFF:#x})"
                    return False

                self.active = True
                self.last_error = None
                return True

        try:
            return self._run_on_owner_thread(_do)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            log.warning("Windows SMTC update failed: %s", self.last_error)
            return False

    def clear(self) -> None:
        if self._smtc is None or self._updater is None:
            self.active = False
            return

        def _do() -> bool:
            import ctypes

            with self._lock:
                if self._smtc is None or self._updater is None:
                    return True

                def vt(ptr: ctypes.c_void_p, idx: int, restype, *argtypes):
                    vtable = ctypes.cast(
                        ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
                    ).contents
                    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[idx])

                # PlaybackStatus = Stopped (2), ClearAll (16), Update (17), IsEnabled = False (11)
                vt(self._smtc, 7, ctypes.c_long, ctypes.c_int)(self._smtc, 2)
                vt(self._updater, 16, ctypes.c_long)(self._updater)
                vt(self._updater, 17, ctypes.c_long)(self._updater)
                vt(self._smtc, 11, ctypes.c_long, ctypes.c_ubyte)(self._smtc, 0)
                self.active = False
                return True

        try:
            self._run_on_owner_thread(_do)
        except Exception:  # noqa: BLE001
            self.active = False

    def close(self) -> None:
        self.clear()

        def _release() -> bool:
            import ctypes

            with self._lock:
                def vt(ptr: ctypes.c_void_p, idx: int, restype, *argtypes):
                    vtable = ctypes.cast(
                        ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
                    ).contents
                    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[idx])

                if self._updater is not None:
                    vt(self._updater, 2, ctypes.c_ulong)(self._updater)
                    self._updater = None
                if self._smtc is not None:
                    vt(self._smtc, 2, ctypes.c_ulong)(self._smtc)
                    self._smtc = None
                return True

        try:
            self._run_on_owner_thread(_release)
        except Exception:  # noqa: BLE001
            pass


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
    elif sig == "x":
        _dbus_pad(buf, 8)
        buf.extend(struct.pack("<q", int(value)))
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
        """Minimal D-Bus responder for Properties.Get / GetAll so scrobblers can query us."""
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
                    self._handle_method_call(sock, serial, rest[:fields_len], rest[fields_len + pad :])
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

    def _handle_method_call(
        self, sock: socket.socket, serial: int, fields_raw: bytes, body_raw: bytes
    ) -> None:
        # Extract sender, member, and interface strings cleanly from ASCII/UTF-8 fields
        text = fields_raw.decode("utf-8", errors="ignore")
        body_text = body_raw.decode("utf-8", errors="ignore")
        # Find sender bus name (e.g. ":1.42")
        m_sender = re.search(r":\d+\.\d+", text)
        sender = m_sender.group(0) if m_sender else None
        if not sender:
            return

        if "GetAll" in text:
            props = self._player_props() if "Player" in body_text else self._root_props()
            body = bytearray()
            _dbus_pack_dict_sv(body, props)
            reply = _dbus_build_message(
                2,
                self._next_serial(),
                [(5, "u", serial), (6, "s", sender), (8, "s", "a{sv}")],
                bytes(body),
            )
            try:
                with self._lock:
                    sock.sendall(reply)
            except OSError:
                pass

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
    ) -> bool:
        with self._lock:
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
            ok = self._emit_properties_changed()
            self.active = ok
            return ok

    def clear(self) -> None:
        with self._lock:
            self._status = "Stopped"
            self._metadata = {}
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
    """Create the native external scrobbler media session for the current OS."""
    if sys.platform == "win32":
        return WindowsSMTCMediaSession(hwnd_getter=hwnd_getter, ui_dispatch=ui_dispatch)
    if sys.platform.startswith("linux"):
        return LinuxMPRISMediaSession()
    session = ExternalMediaSession()
    _, reason = platform_support()
    session.last_error = reason
    return session
