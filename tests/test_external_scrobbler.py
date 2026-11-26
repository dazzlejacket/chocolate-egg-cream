"""Tests for M6 external media session (Windows SMTC, Linux MPRIS2, engine lifecycle)."""

from __future__ import annotations

import asyncio
import struct
import sys
import time
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chocolate_egg_cream.app import EggCream
from chocolate_egg_cream.recognize import Recognition
from chocolate_egg_cream.scrobble import external
from chocolate_egg_cream import settings as settings_module

FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAILED
    if cond:
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name}" + (f" -- {detail}" if detail else ""))


def _make_dbus_call(
    serial: int,
    *,
    sender: str = ":1.99",
    interface: str = "org.freedesktop.DBus.Properties",
    member: str = "Get",
    args: list[str] = (),
) -> tuple[bytes, bytes]:
    """Build raw D-Bus header fields + body for unit-testing _build_method_reply."""
    fields = bytearray()
    for code, sig, val in [
        (1, "o", "/org/mpris/MediaPlayer2"),
        (2, "s", interface),
        (3, "s", member),
        (7, "s", sender),
    ]:
        external._dbus_pad(fields, 8)
        fields.append(code)
        external._dbus_pack_variant(fields, sig, val)
    body = bytearray()
    for s in args:
        external._dbus_pack_string(body, s)
    return bytes(fields), bytes(body)


def test_honest_labeling() -> None:
    print("1. honest platform support labeling")
    supported, note = external.platform_support()
    check("platform_support does not prematurely claim Pano compatibility", "pano" not in note.lower(), note)
    if sys.platform == "win32":
        check("Windows reports SMTC support", supported and "SMTC" in note, note)


def test_windows_smtc_live_com() -> None:
    print("2. Windows SMTC WinRT COM registration, 5 tracks, dedup, clear, restart, close")
    if sys.platform != "win32":
        print("  SKIP  not Windows")
        return

    import ctypes
    import tkinter as tk

    ok_id = external.ensure_windows_app_identity()
    check("ensure_windows_app_identity succeeds", ok_id)

    root = tk.Tk()
    root.title("Chocolate Egg Cream SMTC Test")
    root.update()
    winfo = int(root.winfo_id())
    hwnd = int(ctypes.windll.user32.GetAncestor(winfo, 2) or ctypes.windll.user32.GetParent(winfo) or winfo)

    sess = external.WindowsSMTCMediaSession(hwnd_getter=lambda: hwnd)

    tracks = [
        ("Miles Davis", "So What", "Kind of Blue", 562.0, "t1"),
        ("John Coltrane", "Giant Steps", "Giant Steps", 283.0, "t2"),
        ("Bill Evans Trio", "Waltz for Debby", "Waltz for Debby", 418.0, "t3"),
        ("Herbie Hancock", "Maiden Voyage", "Maiden Voyage", 473.0, "t4"),
        ("Thelonious Monk", "Straight, No Chaser", None, None, "t5"),
    ]

    for idx, (artist, title, album, dur, tid) in enumerate(tracks, 1):
        ok = sess.update_now_playing(artist, title, album=album, duration=dur, track_id=tid, position=5.0)
        st = sess.read_state()
        check(
            f"track {idx}/5 ({artist} - {title}) published",
            ok
            and sess.active
            and st is not None
            and st["playback_status"] == 3
            and st["is_enabled"] is True
            and st["is_play_enabled"] is True
            and st["is_pause_enabled"] is True
            and st["is_stop_enabled"] is True
            and st["button_pressed_registered"] is True
            and st["timeline_supported"] is True
            and st["title"] == title
            and st["artist"] == artist
            and st["album"] == (album or ""),
            f"ok={ok} err={sess.last_error} state={st}",
        )

    # Dedup: calling update_now_playing with the exact same track is a no-op returning True
    ok_dup = sess.update_now_playing("Thelonious Monk", "Straight, No Chaser", album=None, duration=None, track_id="t5", position=0.0)
    check("duplicate refresh of active track returns True cleanly", ok_dup and sess.active)

    # Clear (Stop)
    sess.clear()
    st_cleared = sess.read_state()
    check(
        "clear() sets Stopped (2) and IsEnabled=False",
        not sess.active
        and st_cleared is not None
        and st_cleared["playback_status"] == 2
        and st_cleared["is_enabled"] is False,
        f"state={st_cleared}",
    )

    # Restart after Stop
    ok_restart = sess.update_now_playing("Charles Mingus", "Goodbye Pork Pie Hat", album="Mingus Ah Um", duration=344.0, track_id="t6", position=0.0)
    st_restart = sess.read_state()
    check(
        "restart after clear() publishes cleanly",
        ok_restart
        and sess.active
        and st_restart is not None
        and st_restart["playback_status"] == 3
        and st_restart["title"] == "Goodbye Pork Pie Hat",
        f"state={st_restart}",
    )

    # Close (Quit)
    sess.close()
    check("close() deactivates and releases COM objects", not sess.active and sess._smtc is None and sess._updater is None and sess._button_token is None)
    root.destroy()


def test_linux_mpris_dbus_protocol() -> None:
    print("3. Linux MPRIS2 D-Bus Properties.Get / GetAll / Introspect / Ping")
    sess = external.LinuxMPRISMediaSession()
    sess._status = "Playing"
    sess._track_started_mono = time.monotonic() - 12.5
    sess._track_base_pos = 0.0
    sess._metadata = {
        "mpris:trackid": ("o", "/com/chocolateeggcream/track/t1"),
        "xesam:title": ("s", "So What"),
        "xesam:artist": ("as", ["Miles Davis"]),
        "xesam:album": ("s", "Kind of Blue"),
        "mpris:length": ("x", 562_000_000),
    }

    # Properties.Get(org.mpris.MediaPlayer2, Identity) -> METHOD_RETURN (2) containing "Chocolate Egg Cream"
    f, b = _make_dbus_call(1, member="Get", args=["org.mpris.MediaPlayer2", "Identity"])
    reply = sess._build_method_reply(1, f, b)
    check("Properties.Get Identity returns METHOD_RETURN", reply is not None and reply[1] == 2 and b"Chocolate Egg Cream" in reply)

    # Properties.Get(org.mpris.MediaPlayer2.Player, PlaybackStatus)
    f, b = _make_dbus_call(2, member="Get", args=["org.mpris.MediaPlayer2.Player", "PlaybackStatus"])
    reply = sess._build_method_reply(2, f, b)
    check("Properties.Get PlaybackStatus returns Playing", reply is not None and reply[1] == 2 and b"Playing" in reply)

    # Properties.Get(org.mpris.MediaPlayer2.Player, Position) -> variant 'x'
    f, b = _make_dbus_call(3, member="Get", args=["org.mpris.MediaPlayer2.Player", "Position"])
    reply = sess._build_method_reply(3, f, b)
    check("Properties.Get Position returns variant x", reply is not None and reply[1] == 2 and b"\x01x\x00" in reply)

    # Properties.Get(org.mpris.MediaPlayer2.Player, Volume) -> variant 'd'
    f, b = _make_dbus_call(4, member="Get", args=["org.mpris.MediaPlayer2.Player", "Volume"])
    reply = sess._build_method_reply(4, f, b)
    check("Properties.Get Volume returns variant d", reply is not None and reply[1] == 2 and b"\x01d\x00" in reply)

    # Properties.Get unknown property -> ERROR (3)
    f, b = _make_dbus_call(5, member="Get", args=["org.mpris.MediaPlayer2.Player", "DoesNotExist"])
    reply = sess._build_method_reply(5, f, b)
    check("Properties.Get unknown property returns D-Bus ERROR", reply is not None and reply[1] == 3)

    # Properties.GetAll
    f, b = _make_dbus_call(6, member="GetAll", args=["org.mpris.MediaPlayer2.Player"])
    reply = sess._build_method_reply(6, f, b)
    check("Properties.GetAll Player returns metadata + position", reply is not None and reply[1] == 2 and b"Miles Davis" in reply and b"Position" in reply)

    # Introspect + Ping
    f, b = _make_dbus_call(7, interface="org.freedesktop.DBus.Introspectable", member="Introspect")
    reply = sess._build_method_reply(7, f, b)
    check("Introspect returns MPRIS2 XML", reply is not None and reply[1] == 2 and b"org.mpris.MediaPlayer2.Player" in reply)

    f, b = _make_dbus_call(8, interface="org.freedesktop.DBus.Peer", member="Ping")
    reply = sess._build_method_reply(8, f, b)
    check("Peer.Ping returns METHOD_RETURN", reply is not None and reply[1] == 2)


def test_engine_external_only_no_direct_scrobble() -> None:
    print("4. Engine with Last.fm & ListenBrainz disabled + external session lifecycle")

    class FakeSession(external.ExternalMediaSession):
        backend_name = "FakeSMTC"

        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple] = []

        def update_now_playing(self, artist, title, album=None, duration=None, track_id=None, position=0.0) -> bool:
            self.calls.append(("update", artist, title, album, duration, track_id))
            self.active = True
            return True

        def clear(self) -> None:
            self.calls.append(("clear",))
            self.active = False

        def close(self) -> None:
            self.calls.append(("close",))
            self.active = False

    class FakeCapture:
        sample_rate = 48000
        def open(self): pass
        def close(self): pass
        def read(self, s): return np.ones(4800, dtype=np.float32) * 0.1

    class FakeRecognizer:
        async def recognize(self, samples, sr): return None
        async def close(self): pass

    fake_ext = FakeSession()
    cfg = dict(settings_module.DEFAULTS)
    cfg["enabled_services"] = []  # Direct Last.fm and ListenBrainz disabled
    cfg["external_scrobbler"] = True
    cfg["confirmations_required"] = 1

    with mock.patch.object(external, "platform_support", return_value=(True, "ok")):
        with mock.patch.object(external, "create_external_session", return_value=fake_ext):
            engine = EggCream(
                cfg,
                capture=FakeCapture(),
                recognizer=FakeRecognizer(),
                save_settings=lambda d: None,
                record_history=False,
            )

    check("no direct scrobblers built when enabled_services=[]", len(engine.scrobblers) == 0)

    async def run_seq() -> None:
        songs = [
            Recognition(artist=f"Artist {i}", title=f"Song {i}", album=f"Album {i}", duration=200.0)
            for i in range(1, 6)
        ]
        now = time.time()
        for idx, rec in enumerate(songs):
            t0 = now + idx * 90
            engine.tracks.observe(rec, t0, heard_audio=True)
            engine.tracks.observe(rec, t0 + 15, heard_audio=True)
            await engine._announce_now_playing()
            # Simulate 300s NOW_PLAYING_REFRESH for the SAME track: must NOT re-push to external_session!
            await engine._announce_now_playing()

        await engine.stop()
        await engine.shutdown()

    asyncio.run(run_seq())

    updates = [c for c in fake_ext.calls if c[0] == "update"]
    check("exactly 5 external updates for 5 consecutive tracks (no duplicate refresh)", len(updates) == 5, f"updates={len(updates)}")
    check("stop() cleared external session", ("clear",) in fake_ext.calls)
    check("shutdown() closed external session", ("close",) in fake_ext.calls)


if __name__ == "__main__":
    test_honest_labeling()
    test_windows_smtc_live_com()
    test_linux_mpris_dbus_protocol()
    test_engine_external_only_no_direct_scrobble()
    print()
    if FAILED:
        print(f"FAILED: {FAILED} check(s)")
        raise SystemExit(1)
    print("ALL EXTERNAL SCROBBLER CHECKS PASSED")
