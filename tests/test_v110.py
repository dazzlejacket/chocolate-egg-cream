"""Tests for Chocolate Egg Cream v1.1.0 features.

    python tests/test_v110.py
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chocolate_egg_cream import __version__, history, settings as settings_module, theme
from chocolate_egg_cream.app import EggCream
from chocolate_egg_cream.recognize import Recognition
from chocolate_egg_cream.scrobble import external, lastfm_app
from chocolate_egg_cream.scrobble.lastfm import LastFM
from chocolate_egg_cream.scrobble.listenbrainz import ListenBrainz, normalize_submit_url
from chocolate_egg_cream.track import FinishedTrack

FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILED.append(name)


def test_version_and_themes() -> None:
    print("1. version and monochrome themes")
    check("version is 1.1.0", __version__ == "1.1.0", f"got {__version__}")
    check("light palette resolves to light", theme.resolve_palette("light") is theme.LIGHT)
    check("dark palette resolves to dark", theme.resolve_palette("dark") is theme.DARK)
    sys_pal = theme.resolve_palette("system")
    check("system palette resolves to LIGHT or DARK", sys_pal in (theme.LIGHT, theme.DARK))

    validated = settings_module.validate({"theme": "dark", "external_scrobbler": "true"})
    check("settings validates theme=dark", validated["theme"] == "dark")
    check("settings coerces external_scrobbler bool", validated["external_scrobbler"] is True)
    bad = settings_module.validate({"theme": "neon-rainbow"})
    check("invalid theme falls back to system", bad["theme"] == "system")


def test_bundled_lastfm_credentials() -> None:
    print("2. bundled Last.fm credentials & custom override")
    old_k = os.environ.pop("CEC_LASTFM_API_KEY", None)
    old_s = os.environ.pop("CEC_LASTFM_API_SECRET", None)
    try:
        os.environ["CEC_LASTFM_API_KEY"] = "bundled_key_123"
        os.environ["CEC_LASTFM_API_SECRET"] = "bundled_secret_456"
        check("has_bundled_credentials True when env set", lastfm_app.has_bundled_credentials())

        # Default uses bundled credentials
        k, s, is_custom = lastfm_app.resolve_credentials({"lastfm_use_custom_credentials": False})
        check("uses bundled key by default", k == "bundled_key_123" and not is_custom)
        check("uses bundled secret by default", s == "bundled_secret_456")

        # Advanced custom credentials override bundled
        k2, s2, is_custom2 = lastfm_app.resolve_credentials(
            {
                "lastfm_use_custom_credentials": True,
                "lastfm_api_key": "custom_k",
                "lastfm_api_secret": "custom_s",
            }
        )
        check("custom override uses custom key", k2 == "custom_k" and is_custom2)
        check("custom override uses custom secret", s2 == "custom_s")

        # v1.0.x upgrade compatibility: existing custom key+secret+session keeps working
        k3, s3, is_custom3 = lastfm_app.resolve_credentials(
            {
                "lastfm_use_custom_credentials": False,
                "lastfm_api_key": "v10_key",
                "lastfm_api_secret": "v10_sec",
                "lastfm_session_key": "v10_sk",
            }
        )
        check("preserves existing v1.0.x custom session", k3 == "v10_key" and is_custom3)
    finally:
        if old_k is None:
            os.environ.pop("CEC_LASTFM_API_KEY", None)
        else:
            os.environ["CEC_LASTFM_API_KEY"] = old_k
        if old_s is None:
            os.environ.pop("CEC_LASTFM_API_SECRET", None)
        else:
            os.environ["CEC_LASTFM_API_SECRET"] = old_s


def test_lastfm_stale_session_and_username() -> None:
    print("3. Last.fm stale session detection & username")

    class FakeLastFMHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if "method=auth.getSession" in self.path:
                body = json.dumps({"session": {"name": "eggfan99", "key": "sk_live_1"}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            elif "method=user.getInfo" in self.path and "sk=sk_stale" in self.path:
                body = json.dumps(
                    {"error": 9, "message": "Invalid session key - Please re-authenticate"}
                ).encode()
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            elif "method=user.getInfo" in self.path:
                body = json.dumps({"user": {"name": "eggfan99"}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            if "sk=sk_stale" in raw:
                body = json.dumps(
                    {"error": 9, "message": "Invalid session key - Please re-authenticate"}
                ).encode()
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                body = json.dumps({"scrobbles": {"@attr": {"accepted": 1}}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), FakeLastFMHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    import chocolate_egg_cream.scrobble.lastfm as lfm_mod

    orig_root = lfm_mod.API_ROOT
    lfm_mod.API_ROOT = f"http://127.0.0.1:{port}/2.0/"
    try:
        client = LastFM("key1", "sec1")
        sk, user = client.exchange_token_with_user("tok1")
        check("exchange_token_with_user returns session key", sk == "sk_live_1")
        check("exchange_token_with_user returns username", user == "eggfan99")

        ok_v, uname, err_v = client.verify_session()
        check("verify_session succeeds for valid session", ok_v and uname == "eggfan99" and err_v is None)

        stale_client = LastFM("key1", "sec1", session_key="sk_stale")
        ok_sc = stale_client.scrobble("Artist", "Song", timestamp=1000)
        check("stale scrobble returns False", ok_sc is False)
        check("stale session flagged on HTTP 403 error 9", stale_client.session_stale is True)
        check("actual API error preserved", "error 9" in (stale_client.last_error or ""))

        ok_v2, _u2, err_v2 = stale_client.verify_session()
        check("verify_session flags stale session", not ok_v2 and stale_client.session_stale and "error 9" in (err_v2 or ""))
        client.close()
        stale_client.close()
    finally:
        lfm_mod.API_ROOT = orig_root
        server.shutdown()


def test_listenbrainz_custom_url() -> None:
    print("4. ListenBrainz custom server URL (Multi-Scrobbler)")
    u1, e1 = normalize_submit_url("")
    check("empty URL uses official ListenBrainz", u1 == "https://api.listenbrainz.org/1/submit-listens" and e1 is None)

    u2, e2 = normalize_submit_url("http://localhost:9078")
    check("base host appends /1/submit-listens", u2 == "http://localhost:9078/1/submit-listens" and e2 is None)

    u3, e3 = normalize_submit_url("https://ms.example.com/1/")
    check("/1/ appends submit-listens", u3 == "https://ms.example.com/1/submit-listens" and e3 is None)

    u4, e4 = normalize_submit_url("http://127.0.0.1:9078/1/submit-listens")
    check("full submit URL preserved", u4 == "http://127.0.0.1:9078/1/submit-listens" and e4 is None)

    _u5, e5 = normalize_submit_url("not-a-url")
    check("invalid URL returns clear error", e5 is not None)

    received: list[dict] = []

    class FakeMSHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            received.append(
                {
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": json.loads(self.rfile.read(length).decode("utf-8")),
                }
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

    server = HTTPServer(("127.0.0.1", 0), FakeMSHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        lb = ListenBrainz(token="ms_token_xyz", server_url=f"http://127.0.0.1:{port}")
        ok = lb.scrobble("Boards of Canada", "Roygbiv", album="MHTRTC", timestamp=1700000000, duration=151.0, isrc="GBBKS9800001")
        check("custom server scrobble succeeded", ok is True)
        check("hit /1/submit-listens on custom server", len(received) == 1 and received[0]["path"] == "/1/submit-listens")
        meta = received[0]["body"]["payload"][0]["track_metadata"]
        check("ISRC included in additional_info", meta["additional_info"].get("isrc") == "GBBKS9800001")
        lb.close()
    finally:
        server.shutdown()


def test_history_append_export_and_retry() -> None:
    print("5. local history append-only, exports, retry queue, and dedup")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        orig_cfg = settings_module.config_dir
        settings_module.config_dir = lambda: tmp_dir
        try:
            # 5a. Eligible listen saved locally even with NO scrobbling accounts
            cfg = settings_module.validate({"enabled_services": [], "min_play_seconds": 30})
            engine = EggCream(cfg, capture=object(), recognizer=object(), save_settings=lambda d: None)
            t1 = Recognition(artist="Café Tacvba", title="Eres", album="Cuatro Caminos", isrc="MXUM70300001", duration=268.0)
            asyncio.run(engine._finish(FinishedTrack(recognition=t1, played_seconds=180.0, started_at=1700001000.0)))

            hpath = history.history_path()
            check("history.jsonl created without scrobblers", hpath.is_file())
            first_bytes = hpath.read_bytes()
            recs = history.load_history(newest_first=False)
            check("1 listen saved locally", len(recs) == 1)
            check("stores artist, title, album, isrc, duration, timestamp",
                  recs[0]["artist"] == "Café Tacvba"
                  and recs[0]["isrc"] == "MXUM70300001"
                  and recs[0]["timestamp"] == 1700001000
                  and recs[0]["duration"] == 268.0)

            # 5b. Failed scrobble preserved in pending queue + appended (never rewrites first_bytes)
            class FlakyScrobbler:
                name = "Last.fm"
                service_key = "lastfm"
                configured = True
                session_stale = False
                last_error = "network timeout"
                calls = 0
                should_succeed = False

                def scrobble(self, **kw):
                    self.calls += 1
                    if self.should_succeed:
                        self.last_error = None
                        return True
                    return False

            flaky = FlakyScrobbler()
            engine.scrobblers = [flaky]
            t2 = Recognition(artist="Stereolab", title="French Disko", album="Switched On", isrc="GBDUW9300002", duration=215.0)
            asyncio.run(engine._finish(FinishedTrack(recognition=t2, played_seconds=150.0, started_at=1700002000.0)))

            second_bytes = hpath.read_bytes()
            check("history.jsonl strictly appended (prefix unchanged)", second_bytes.startswith(first_bytes))
            pending = history.load_pending()
            check("failed scrobble queued in pending_scrobbles.json", len(pending) == 1 and pending[0]["title"] == "French Disko")

            # 5c. Retry succeeds -> status updated via append, removed from pending, no duplicate on second retry
            flaky.should_succeed = True
            succ, rem = asyncio.run(engine.retry_pending())
            check("retry_pending submitted 1, 0 remaining", succ == 1 and rem == 0)
            third_bytes = hpath.read_bytes()
            check("status_update also appended (prefix unchanged)", third_bytes.startswith(second_bytes))

            resolved = history.load_history(newest_first=False)
            check("resolved history shows submitted after retry", resolved[1]["services"]["lastfm"]["status"] == "submitted")

            # Second retry does nothing (no duplicate)
            calls_before = flaky.calls
            succ2, rem2 = asyncio.run(engine.retry_pending())
            check("no duplicate on subsequent retry", succ2 == 0 and rem2 == 0 and flaky.calls == calls_before)

            # 5d. Exports: TXT, CSV, JSONL
            txt_p = tmp_dir / "out.txt"
            csv_p = tmp_dir / "out.csv"
            jsonl_p = tmp_dir / "out.jsonl"
            check("export TXT writes 2 listens", history.export_history(txt_p, "txt") == 2)
            check("export CSV writes 2 listens", history.export_history(csv_p, "csv") == 2)
            check("export JSONL writes 2 listens", history.export_history(jsonl_p, "jsonl") == 2)

            check("TXT contains Unicode artist & ISRC", "Café Tacvba" in txt_p.read_text(encoding="utf-8") and "MXUM70300001" in txt_p.read_text(encoding="utf-8"))
            with csv_p.open("r", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            check("CSV has 2 rows with lastfm_status=submitted on row 2", len(rows) == 2 and rows[1]["lastfm_status"] == "submitted")
            jsonl_lines = [json.loads(ln) for ln in jsonl_p.read_text(encoding="utf-8").splitlines() if ln.strip()]
            check("JSONL has 2 valid JSON objects", len(jsonl_lines) == 2 and jsonl_lines[1]["isrc"] == "GBDUW9300002")
        finally:
            settings_module.config_dir = orig_cfg


def test_external_scrobbler_and_icons() -> None:
    print("6. external scrobbler mode & icon set")
    supported, note = external.platform_support()
    if sys.platform == "win32":
        check("Windows reports SMTC support", supported and "SMTC" in note)
        import tkinter as tk
        import ctypes

        root = tk.Tk()
        root.withdraw()
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        sess = external.create_external_session(hwnd_getter=lambda: hwnd)
        ok = sess.update_now_playing("Aphex Twin", "Xtal", album="SAW 85-92", duration=294.0)
        check("Windows SMTC update_now_playing succeeds without fake audio", ok is True and sess.active is True, f"err={sess.last_error}")
        sess.close()
        check("Windows SMTC clear/close deactivates cleanly", sess.active is False)
        root.destroy()
    elif sys.platform.startswith("linux"):
        check("Linux reports MPRIS2 support", supported and "MPRIS2" in note)
    elif sys.platform == "darwin":
        check("macOS honestly reports non-interfering limitation", not supported)

    # Linux D-Bus address parser sanity check (runs on all OSes)
    parsed = external._parse_dbus_unix_address("unix:path=/run/user/1000/bus")
    check("D-Bus unix:path parser works", parsed == ("/run/user/1000/bus", False))

    # Icon assets check
    repo = Path(__file__).resolve().parents[1]
    ico = repo / "packaging" / "icons" / "chocolate-egg-cream.ico"
    icns = repo / "packaging" / "icons" / "chocolate-egg-cream.icns"
    svg = repo / "packaging" / "icons" / "chocolate-egg-cream.svg"
    pngs = [repo / "packaging" / "icons" / "png" / f"icon-{s}.png" for s in (16, 32, 48, 64, 128, 256, 512)]
    trays = [repo / "packaging" / "icons" / f"tray-{s}.png" for s in (16, 32)]
    check("Windows .ico exists and valid header", ico.is_file() and ico.read_bytes()[:4] == b"\x00\x00\x01\x00")
    check("macOS .icns exists and valid header", icns.is_file() and icns.read_bytes()[:4] == b"icns")
    check("SVG exists", svg.is_file() and "<svg" in svg.read_text(encoding="utf-8"))
    check("All 7 Linux PNG sizes valid", all(p.is_file() and p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" for p in pngs))
    check("Tray PNGs valid", all(p.is_file() and p.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" for p in trays))


def main() -> int:
    test_version_and_themes()
    test_bundled_lastfm_credentials()
    test_lastfm_stale_session_and_username()
    test_listenbrainz_custom_url()
    test_history_append_export_and_retry()
    test_external_scrobbler_and_icons()
    print()
    if FAILED:
        print(f"FAILED ({len(FAILED)}): {', '.join(FAILED)}")
        return 1
    print("all good")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
