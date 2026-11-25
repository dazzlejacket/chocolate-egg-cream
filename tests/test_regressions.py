"""Regression tests for the v1.0.1 bug fixes.

One section per reported bug:

  1. packaged builds: CA bundle configuration (SSL_CERT_FILE etc.)
  2. ListenBrainz now-playing 404 (correct endpoint + payload)
  3. UnicodeEncodeError from a non-Latin-1 token / masked placeholder
  4. one scrobbler's failure must not stop recognition (per-service isolation)
  5. scrobble de-duplication is per service and per playback event
  6. settings: atomic writes, secret handling, corrupt-file quarantine
  7. single-instance lock (second instance refuses to start)
  8. GUI deferred callbacks must not raise NameError
  9. shazamio retry storm is bounded
 10. Stop / Quit / restart always complete
 11. the packaged `verify` command runs end-to-end

    python tests/test_regressions.py
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import runpy
import socket
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from queue import Queue

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from chocolate_egg_cream import credentials, settings as settings_module
from chocolate_egg_cream.app import EggCream
from chocolate_egg_cream.certs import CA_ENV_VARS, configure_ca_bundle, find_ca_bundle
from chocolate_egg_cream.recognize import Recognition
from chocolate_egg_cream.track import FinishedTrack

REPO = Path(__file__).resolve().parents[1]

FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILED.append(name)


def section(title: str) -> None:
    print(f"\n{title}")


# ------------------------------------------------------------------ helpers


def base_settings(**over) -> dict:
    settings = {
        "lastfm_api_key": "",
        "lastfm_api_secret": "",
        "lastfm_session_key": "",
        "listenbrainz_token": "",
        "enabled_services": [],
        "interval_seconds": 0,
        "sample_seconds": 0.05,
        "confirmations_required": 2,
        "min_play_seconds": 0,
        "max_skew": 0,
        "scrobble_events": [],
    }
    settings.update(over)
    return settings


def song(artist: str, title: str, isrc: str, duration: float | None = None) -> Recognition:
    return Recognition(
        artist=artist, title=title, album=f"{artist} Album", isrc=isrc, duration=duration
    )


class FakeCapture:
    sample_rate = 48000

    def __init__(self):
        self.opened = 0
        self.closed = 0

    def open(self):
        self.opened += 1

    def close(self):
        self.closed += 1

    def read(self, seconds):
        return np.full(int(self.sample_rate * seconds), 0.4, dtype=np.float32)


class FakeRecognizer:
    """Hands out a scripted list of recognitions (last one repeats)."""

    def __init__(self, results):
        self.results = list(results)
        self.index = 0
        self.closed = False

    async def recognize(self, samples, sample_rate):
        item = self.results[min(self.index, len(self.results) - 1)]
        self.index += 1
        return item

    async def close(self):
        self.closed = True


class GoodService:
    """A well-behaved scrobbler that records what it was asked to do."""

    def __init__(self, name: str = "Good"):
        self.name = name
        self.configured = True
        self.last_error = None
        self.scrobbles: list[dict] = []
        self.now_playing_calls: list[dict] = []
        self.closed = False

    def scrobble(self, **kwargs):
        self.scrobbles.append(kwargs)
        return True

    def now_playing(self, **kwargs):
        self.now_playing_calls.append(kwargs)
        return True

    def close(self):
        self.closed = True


class BoomService:
    """A scrobbler whose every operation raises, as a broken API does."""

    name = "Boom"
    configured = True
    last_error = "boom"

    def scrobble(self, **kwargs):
        raise RuntimeError("service exploded")

    def now_playing(self, **kwargs):
        raise RuntimeError("service exploded")

    def close(self):
        raise RuntimeError("close exploded")


class RefusingService:
    """A scrobbler that is configured but rejects every submission."""

    name = "Refusing"
    configured = True
    last_error = "HTTP 401: unauthorized"

    def scrobble(self, **kwargs):
        return False

    def now_playing(self, **kwargs):
        return False

    def close(self):
        pass


def closed_port() -> int:
    """A local port nothing listens on, for connection-refused tests."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def make_app(settings, capture=None, recognizer=None, saved=None) -> EggCream:
    return EggCream(
        settings,
        capture=capture if capture is not None else FakeCapture(),
        recognizer=recognizer if recognizer is not None else FakeRecognizer([]),
        save_settings=(saved.append if saved is not None else (lambda data: None)),
    )


# ------------------------------------------- 1. packaged TLS / CA certificates


def test_ca_bundle_configuration() -> None:
    section("1. CA bundle configuration (packaged SSL failures)")

    bundle = find_ca_bundle()
    check(
        "a CA bundle is found",
        bool(bundle) and Path(bundle).is_file() and Path(bundle).stat().st_size > 50_000,
        str(bundle),
    )

    saved_env = {name: os.environ.pop(name, None) for name in CA_ENV_VARS}
    try:
        configured = configure_ca_bundle()
        check("configure_ca_bundle returns the bundle", configured == bundle, str(configured))
        check(
            "all TLS env vars point at the bundle",
            all(os.environ.get(name) == bundle for name in CA_ENV_VARS),
            str({name: os.environ.get(name) for name in CA_ENV_VARS}),
        )

        # An explicit, existing user setting must win over ours.
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as handle:
            user_pem = handle.name
            handle.write(b"# pretend user CA\n")
        os.environ["SSL_CERT_FILE"] = user_pem
        try:
            configure_ca_bundle()
            check(
                "an existing user CA file is respected",
                os.environ["SSL_CERT_FILE"] == user_pem,
                os.environ.get("SSL_CERT_FILE", ""),
            )
            check(
                "the other vars are still configured",
                os.environ.get("REQUESTS_CA_BUNDLE") == bundle
                and os.environ.get("CURL_CA_BUNDLE") == bundle,
            )
        finally:
            os.environ.pop("SSL_CERT_FILE", None)

        # A stale env var pointing nowhere must be repaired, not trusted.
        os.environ["SSL_CERT_FILE"] = str(Path(tempfile.gettempdir()) / "does-not-exist.pem")
        configure_ca_bundle()
        check(
            "a broken env var is repaired",
            os.environ["SSL_CERT_FILE"] == bundle,
            os.environ.get("SSL_CERT_FILE", ""),
        )
    finally:
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    # Nothing anywhere in the package may disable verification.
    violations: list[str] = []
    for path in (REPO / "chocolate_egg_cream").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.keyword)
                and node.arg == "verify"
                and isinstance(node.value, ast.Constant)
                and node.value.value is False
            ):
                violations.append(f"{path.name}: verify=False")
            if isinstance(node, ast.Attribute) and node.attr == "CERT_NONE":
                violations.append(f"{path.name}: ssl.CERT_NONE")
            if (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Attribute) and t.attr == "check_hostname"
                    for t in node.targets
                )
                and isinstance(node.value, ast.Constant)
                and node.value.value is False
            ):
                violations.append(f"{path.name}: check_hostname = False")
    check(
        "SSL verification is never disabled anywhere in the package",
        not violations,
        str(violations),
    )


def test_runtime_hook_and_spec() -> None:
    section("1b. PyInstaller runtime hook wiring")

    hook = REPO / "packaging" / "runtime_hook.py"
    spec = REPO / "packaging" / "chocolate_egg_cream.spec"
    check("runtime hook exists", hook.is_file(), str(hook))
    check("spec file exists", spec.is_file(), str(spec))
    if not hook.is_file() or not spec.is_file():
        return

    spec_text = spec.read_text(encoding="utf-8")
    check(
        "the spec registers the runtime hook",
        "runtime_hook.py" in spec_text,
    )
    check(
        "the spec bundles the verify/certs/instance modules",
        all(
            name in spec_text
            for name in (
                "chocolate_egg_cream.certs",
                "chocolate_egg_cream.verify",
                "chocolate_egg_cream.instance",
            )
        ),
    )
    check(
        "the spec refuses to build without certifi's cacert.pem",
        "Refusing to build a broken artifact" in spec_text,
    )

    saved_env = {name: os.environ.pop(name, None) for name in CA_ENV_VARS}
    try:
        runpy.run_path(str(hook))
        check(
            "the hook configures TLS before the app starts",
            all(
                os.environ.get(name) and Path(os.environ[name]).is_file()
                for name in CA_ENV_VARS
            ),
            str({name: os.environ.get(name) for name in CA_ENV_VARS}),
        )
    finally:
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# ------------------------------------------------- 2. ListenBrainz 404 endpoint


def test_listenbrainz_endpoint() -> None:
    section("2. ListenBrainz now-playing goes to /1/submit-listens")

    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            received.append(
                {
                    "path": self.path,
                    "auth": self.headers.get("Authorization", ""),
                    "body": json.loads(self.rfile.read(length).decode("utf-8")),
                }
            )
            body = json.dumps({"status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    import threading

    threading.Thread(target=server.serve_forever, daemon=True).start()

    import chocolate_egg_cream.scrobble.listenbrainz as lb_module

    orig_root = lb_module.API_ROOT
    lb_module.API_ROOT = f"http://127.0.0.1:{port}/1/"
    try:
        lb = lb_module.ListenBrainz("test-token")
        lb.now_playing("Café Tacvba", "¿Cómo Te Extraño Mi Amor?", "Re", duration=240.0)
        lb.scrobble("Café Tacvba", "¿Cómo Te Extraño Mi Amor?", "Re", timestamp=1700000000)
        lb.close()

        check("both calls reached the server", len(received) == 2, str(len(received)))
        check(
            "every call uses /1/submit-listens (no more 404 /playing-now)",
            all(item["path"].endswith("/submit-listens") for item in received),
            str([item["path"] for item in received]),
        )
        if len(received) == 2:
            now_playing, scrobble = received
            check(
                "now playing uses listen_type=playing_now",
                now_playing["body"].get("listen_type") == "playing_now",
                str(now_playing["body"].get("listen_type")),
            )
            check(
                "now-playing payload has no listened_at",
                "listened_at" not in now_playing["body"]["payload"][0],
            )
            check(
                "now-playing payload carries track_metadata",
                now_playing["body"]["payload"][0]["track_metadata"]["track_name"]
                == "¿Cómo Te Extraño Mi Amor?",
            )
            check(
                "completed scrobble uses listen_type=single",
                scrobble["body"].get("listen_type") == "single",
                str(scrobble["body"].get("listen_type")),
            )
            check(
                "completed scrobble carries listened_at",
                isinstance(scrobble["body"]["payload"][0].get("listened_at"), int),
            )
            check(
                "the token is sent as a Token header",
                all(item["auth"] == "Token test-token" for item in received),
            )
    finally:
        lb_module.API_ROOT = orig_root
        server.shutdown()


# --------------------------------------- 3. UnicodeEncodeError / bad credentials


def test_credential_validation() -> None:
    section("3. invalid credentials are refused, never crash a request")

    check(
        "a masked placeholder is detected",
        bool(credentials.secret_problem("••••••••")),
        str(credentials.secret_problem("••••••••")),
    )
    check(
        "a non-Latin-1 token is detected",
        bool(credentials.secret_problem("日本語トークン")),
        str(credentials.secret_problem("日本語トークン")),
    )
    check(
        "control characters inside a value are detected",
        bool(credentials.secret_problem("ab\ncd")),
        str(credentials.secret_problem("ab\ncd")),
    )
    check("a normal token passes", credentials.secret_problem("abc123XYZ-_/") is None)
    check("an unset value is not an error", credentials.secret_problem("") is None)
    check(
        "a pasted BOM and newlines are stripped",
        credentials.normalize_secret("﻿  abc123 \n") == "abc123",
        repr(credentials.normalize_secret("﻿  abc123 \n")),
    )

    from chocolate_egg_cream.scrobble.lastfm import LastFM
    from chocolate_egg_cream.scrobble.listenbrainz import ListenBrainz

    lb = ListenBrainz("••••••••")
    check("ListenBrainz refuses a placeholder token", not lb.configured)
    check(
        "the refusal has a human-readable reason (no value leaked)",
        bool(lb.problem) and "••" not in (lb.problem or "").replace("••••", ""),
        str(lb.problem),
    )
    check(
        "submitting with a bad token returns False instead of raising",
        lb.now_playing("A", "B") is False and lb.scrobble("A", "B") is False,
    )
    check("the failure is recorded", bool(lb.last_error), str(lb.last_error))

    lb_unicode = ListenBrainz("日本語トークン")
    check(
        "a non-Latin-1 token is refused before it reaches http.client",
        not lb_unicode.configured and lb_unicode.scrobble("A", "B") is False,
    )

    # Round-trip: a valid token must produce Latin-1-encodable headers.
    lb_ok = ListenBrainz("﻿  abc123  \n")
    check(
        "a valid token is normalised and accepted",
        lb_ok.configured and lb_ok.token == "abc123",
        repr(lb_ok.token),
    )
    if lb_ok.configured:
        try:
            for value in lb_ok._headers().values():
                value.encode("latin-1")
            encoded = True
        except UnicodeEncodeError:
            encoded = False
        check("the Authorization header is Latin-1 encodable", encoded)
    lb_ok.close()

    lfm_bad = LastFM(api_key="key", api_secret="••••••••", session_key="session")
    check("Last.fm refuses a placeholder secret", not lfm_bad.configured)
    check(
        "Last.fm explains the problem without leaking the value",
        "placeholder" in (lfm_bad.problem or ""),
        str(lfm_bad.problem),
    )
    check(
        "Last.fm scrobble returns False instead of raising",
        lfm_bad.scrobble("A", "B") is False,
    )
    check("Last.fm records the failure", bool(lfm_bad.last_error), str(lfm_bad.last_error))
    lfm_bad.close()

    lfm_missing = LastFM(api_key="", api_secret="", session_key="")
    check(
        "Last.fm reports missing credentials clearly",
        not lfm_missing.configured and "missing" in (lfm_missing.problem or ""),
        str(lfm_missing.problem),
    )
    lfm_missing.close()

    # An authorisation attempt with unusable credentials fails locally.
    try:
        lfm_bad.fetch_token()
        raised = False
    except RuntimeError as exc:
        raised = True
        check(
            "fetch_token fails fast with a clear message",
            "cannot be used" in str(exc) or "needs an API key" in str(exc),
            str(exc),
        )
    check("fetch_token raises before any network call", raised)


def test_network_failures_are_contained() -> None:
    section("3b. network failures return False, never raise")

    from chocolate_egg_cream.scrobble.lastfm import LastFM
    import chocolate_egg_cream.scrobble.lastfm as lastfm_module
    from chocolate_egg_cream.scrobble.listenbrainz import ListenBrainz
    import chocolate_egg_cream.scrobble.listenbrainz as lb_module

    port = closed_port()
    saved = (lastfm_module.API_ROOT, lb_module.API_ROOT)
    lastfm_module.API_ROOT = f"http://127.0.0.1:{port}/2.0/"
    lb_module.API_ROOT = f"http://127.0.0.1:{port}/1/"
    try:
        lfm = LastFM(api_key="key", api_secret="secret", session_key="session", timeout=2.0)
        ok = lfm.scrobble("Artist", "Song", timestamp=int(time.time()))
        check("Last.fm connection failure returns False", ok is False)
        check(
            "Last.fm records a credential-free error",
            bool(lfm.last_error) and "session" not in lfm.last_error.lower(),
            str(lfm.last_error),
        )
        lfm.close()

        lb = ListenBrainz("valid-token", timeout=2.0)
        ok = lb.scrobble("Artist", "Song")
        check("ListenBrainz connection failure returns False", ok is False)
        check("ListenBrainz records the error", bool(lb.last_error), str(lb.last_error))
        lb.close()
    finally:
        lastfm_module.API_ROOT, lb_module.API_ROOT = saved


# --------------------------------- 4. one service must not stop recognition


async def test_service_isolation() -> None:
    section("4. a failing service never stops recognition or the other services")

    a = song("Café Tacvba", "¿Cómo Te Extraño Mi Amor?", "UNI001")

    # _finish: the raising service is skipped, the healthy one still scrobbles.
    saved: list[dict] = []
    app = make_app(base_settings(), saved=saved)
    good, boom = GoodService(), BoomService()
    app.scrobblers = [boom, good]
    finished = FinishedTrack(recognition=a, played_seconds=30.0, started_at=1000.0)
    try:
        await app._finish(finished)
        raised = False
    except Exception:  # noqa: BLE001
        raised = True
    check("_finish does not raise when a service explodes", not raised)
    check(
        "the healthy service still received the scrobble",
        len(good.scrobbles) == 1,
        str(good.scrobbles),
    )
    if good.scrobbles:
        check(
            "the scrobble carries the accented title unchanged",
            good.scrobbles[0].get("title") == "¿Cómo Te Extraño Mi Amor?",
            str(good.scrobbles[0].get("title")),
        )
    check(
        "only the successful service is recorded in the dedup history",
        [(e["s"], e["i"]) for e in app.settings.get("scrobble_events", [])]
        == [("Good", "isrc:UNI001")],
        str(app.settings.get("scrobble_events")),
    )

    # A service that refuses (returns False) also must not affect the other.
    saved2: list[dict] = []
    app2 = make_app(base_settings(), saved=saved2)
    good2, refusing = GoodService(), RefusingService()
    app2.scrobblers = [refusing, good2]
    await app2._finish(FinishedTrack(recognition=a, played_seconds=30.0, started_at=2000.0))
    check(
        "a refusing service does not block the healthy one",
        len(good2.scrobbles) == 1,
        str(good2.scrobbles),
    )
    check(
        "the refusing service is not recorded as having scrobbled",
        all(e["s"] != "Refusing" for e in app2.settings.get("scrobble_events", [])),
        str(app2.settings.get("scrobble_events")),
    )

    # Now playing: one service raising must not stop the others.
    app3 = make_app(base_settings())
    good3, boom3 = GoodService(), BoomService()
    app3.scrobblers = [boom3, good3]
    app3.tracks.observe(a, time.time())
    app3.tracks.observe(a, time.time())
    try:
        await app3._announce_now_playing()
        raised = False
    except Exception:  # noqa: BLE001
        raised = True
    check("_announce_now_playing does not raise when a service explodes", not raised)
    check(
        "the healthy service still got the now-playing update",
        len(good3.now_playing_calls) == 1,
        str(good3.now_playing_calls),
    )


async def test_loop_survives_service_failure() -> None:
    """The real `_loop`, with a service that explodes on every call."""
    section("4b. the real recognition loop keeps running after a service failure")

    a = song("Café Tacvba", "¿Cómo Te Extraño Mi Amor?", "UNI001")
    b = song("Artist B", "Song B", "BBB222")

    saved: list[dict] = []
    app = make_app(
        base_settings(),
        capture=FakeCapture(),
        recognizer=FakeRecognizer([a, a, b, b]),
        saved=saved,
    )
    good, boom = GoodService(), BoomService()
    app.scrobblers = [boom, good]

    await app.start()
    try:
        for _ in range(400):
            if good.scrobbles:
                break
            await asyncio.sleep(0.02)
        still_running = app.running
        check(
            "the loop confirmed and scrobbled a track despite the exploding service",
            len(good.scrobbles) >= 1,
            str(good.scrobbles),
        )
        check("the recognition loop is still running", still_running)
        check(
            "the accented Spanish title survived the whole pipeline",
            bool(good.scrobbles)
            and good.scrobbles[0].get("title") == "¿Cómo Te Extraño Mi Amor?",
            str(good.scrobbles[:1]),
        )
    finally:
        await app.shutdown()

    check("shutdown completed with the exploding service present", True)
    check("the capture was released", app.capture.closed >= 1, str(app.capture.closed))


# ------------------------------------- 5. per-service, per-event deduplication


def test_deduplication_rules() -> None:
    section("5. scrobble deduplication is per service and per playback event")

    now = time.time()
    settings = base_settings(
        scrobble_events=[
            # Same service, submitted 100s ago (inside the 600s window).
            {"s": "Good", "i": "isrc:X", "t": int(now) - 100, "started": 500},
            # Same service, 700s ago (outside the window): a genuine repeat.
            {"s": "Good", "i": "isrc:Y", "t": int(now) - 700, "started": 600},
            # Too old to remember (49h): must be forgotten.
            {"s": "Good", "i": "isrc:OLD", "t": int(now) - 49 * 3600, "started": 700},
            # Malformed entries must be ignored, not crash the load.
            "not-a-dict",
            {"i": "no-service"},
        ]
    )
    app = make_app(settings)

    check(
        "malformed and expired events are dropped on load",
        {(e["s"], e["i"]) for e in app._scrobble_events}
        == {("Good", "isrc:X"), ("Good", "isrc:Y")},
        str(app._scrobble_events),
    )
    check(
        "the same playback event is not sent twice",
        not app._may_submit("Good", "isrc:X", 500, now),
    )
    check(
        "the same song moments ago counts as the same listen",
        not app._may_submit("Good", "isrc:X", 999, now),
    )
    check(
        "another service is unaffected by this service's history",
        app._may_submit("Other", "isrc:X", 500, now),
    )
    check(
        "a song played again after the window is a new listen",
        app._may_submit("Good", "isrc:Y", 700, now),
    )
    check("a never-seen song is scrobbled", app._may_submit("Good", "isrc:Z", 999, now))
    check(
        "an expired event no longer blocks its song",
        app._may_submit("Good", "isrc:OLD", 700, now),
    )

    # The legacy forever-list must be ignored and then removed.
    legacy = make_app(base_settings(scrobbled=["isrc:X"]))
    check(
        "the legacy 'scrobbled' list no longer blocks anything",
        legacy._may_submit("Good", "isrc:X", 500, now),
    )
    saved: list[dict] = []
    legacy.settings["scrobble_events"] = []
    legacy._record_submission("Good", "isrc:X", 500, now)
    check(
        "the legacy key is dropped when the history is saved",
        "scrobbled" not in legacy.settings,
        str(legacy.settings.get("scrobbled")),
    )


async def test_restart_and_repeated_songs() -> None:
    section("5b. restart cannot resend; a real repeat is still scrobbled")

    a = song("Artist A", "Song A", "AAA111")

    # First session scrobbles the track.
    saved: list[dict] = []
    app1 = make_app(base_settings(), saved=saved)
    good1 = GoodService()
    app1.scrobblers = [good1]
    await app1._finish(FinishedTrack(recognition=a, played_seconds=60.0, started_at=5000.0))
    check("the first session scrobbled once", len(good1.scrobbles) == 1, str(good1.scrobbles))
    check("the history was persisted", len(saved) >= 1)

    # A restart builds a fresh app from the saved settings.
    if saved:
        persisted = saved[-1]
        app2 = make_app(base_settings(**persisted))
        good2 = GoodService()
        app2.scrobblers = [good2]
        await app2._finish(
            FinishedTrack(recognition=a, played_seconds=60.0, started_at=5000.0)
        )
        check(
            "after a restart the same playback event is not resent",
            len(good2.scrobbles) == 0,
            str(good2.scrobbles),
        )

        # ... but the same song played again (new start time, past the
        # window) is a legitimate new listen.
        app2._scrobble_events = [
            {**event, "t": int(time.time()) - 700} for event in app2._scrobble_events
        ]
        await app2._finish(
            FinishedTrack(recognition=a, played_seconds=60.0, started_at=9000.0)
        )
        check(
            "the same song played again is scrobbled as a new listen",
            len(good2.scrobbles) == 1,
            str(good2.scrobbles),
        )


# ------------------------------------------------------------ 6. settings safety


def test_settings_safety() -> None:
    section("6. settings: atomic writes, secret handling, corrupt files")

    with tempfile.TemporaryDirectory() as tmp:
        fake_dir = Path(tmp)
        orig_config_dir = settings_module.config_dir
        settings_module.config_dir = lambda: fake_dir
        orig_secrets = (
            credentials.get_secret,
            credentials.set_secret,
            credentials.delete_secret,
        )
        stored: dict[str, str] = {"listenbrainz_token": "real-token"}
        try:
            # -- atomic, valid JSON on every save
            data = dict(settings_module.DEFAULTS)
            data["interval_seconds"] = 30
            settings_module.save(data, use_os_keyring=False)
            settings_file = fake_dir / "settings.json"
            check("settings.json was written", settings_file.is_file())
            if settings_file.is_file():
                parsed = json.loads(settings_file.read_text(encoding="utf-8"))
                check("the file is valid JSON", parsed.get("interval_seconds") == 30)
            leftovers = list(fake_dir.glob("*.tmp"))
            check("no temp file was left behind", not leftovers, str(leftovers))

            # -- an empty value must never wipe a stored secret
            credentials.get_secret = lambda key: stored.get(key)
            credentials.set_secret = lambda key, value: (
                stored.__setitem__(key, value) or True
            )
            credentials.delete_secret = lambda key: stored.pop(key, None) is not None

            empty = dict(settings_module.DEFAULTS)
            empty["listenbrainz_token"] = ""
            settings_module.save(empty, use_os_keyring=True)
            check(
                "a save with an empty value keeps the stored secret",
                stored.get("listenbrainz_token") == "real-token",
                str(stored),
            )

            # -- a real value goes to the credential store, not the JSON file
            filled = dict(settings_module.DEFAULTS)
            filled["listenbrainz_token"] = "new-token"
            settings_module.save(filled, use_os_keyring=True)
            check(
                "the new secret reached the credential store",
                stored.get("listenbrainz_token") == "new-token",
                str(stored),
            )
            if settings_file.is_file():
                on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
                check(
                    "the secret is not stored in plain text",
                    on_disk.get("listenbrainz_token", "") == "",
                    repr(on_disk.get("listenbrainz_token")),
                )

            # -- clearing is explicit
            settings_module.clear_secret("listenbrainz_token")
            check(
                "clear_secret really removes the stored secret",
                "listenbrainz_token" not in stored,
                str(stored),
            )

            # -- a corrupt settings file is quarantined, not fatal
            settings_file.write_text("{ this is not json", encoding="utf-8")
            loaded = settings_module.load(use_os_keyring=False)
            check(
                "a corrupt file falls back to defaults",
                loaded.get("interval_seconds") == settings_module.DEFAULTS["interval_seconds"],
                str(loaded.get("interval_seconds")),
            )
            check(
                "the corrupt file is kept for inspection",
                (fake_dir / "settings.json.invalid").is_file(),
            )

            # -- type coercion
            coerced = settings_module.validate(
                {
                    "interval_seconds": "abc",
                    "confirmations_required": "3",
                    "enabled_services": "lastfm",
                }
            )
            check(
                "bad types fall back to defaults / are coerced",
                coerced["interval_seconds"]
                == settings_module.DEFAULTS["interval_seconds"]
                and coerced["confirmations_required"] == 3
                and coerced["enabled_services"] == ["lastfm"],
                str(
                    (
                        coerced["interval_seconds"],
                        coerced["confirmations_required"],
                        coerced["enabled_services"],
                    )
                ),
            )
            check(
                "unknown services are filtered out",
                settings_module.validate({"enabled_services": ["evil"]})[
                    "enabled_services"
                ]
                == [],
            )
        finally:
            settings_module.config_dir = orig_config_dir
            (
                credentials.get_secret,
                credentials.set_secret,
                credentials.delete_secret,
            ) = orig_secrets


# ------------------------------------------------------- 7. single-instance lock


def test_single_instance() -> None:
    section("7. only one instance may run at a time")

    from chocolate_egg_cream import instance

    with tempfile.TemporaryDirectory() as tmp:
        fake_dir = Path(tmp)
        orig_config_dir = settings_module.config_dir
        settings_module.config_dir = lambda: fake_dir
        other = None
        try:
            path = instance.acquire()
            check("the lock file was created", path.is_file(), str(path))
            check("current_holder reports our pid", instance.current_holder() == os.getpid())

            # A second, live process must be refused.
            other = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"]
            )
            path.write_text(
                json.dumps({"pid": other.pid, "at": int(time.time())}),
                encoding="utf-8",
            )
            try:
                instance.acquire()
                refused = False
            except instance.AlreadyRunning:
                refused = True
            check("a second live instance is refused", refused)

            # A dead owner (crash, upgrade) must be taken over.
            other.kill()
            other.wait(timeout=10)
            other = None
            time.sleep(0.2)
            try:
                instance.acquire()
                took_over = True
            except instance.AlreadyRunning:
                took_over = False
            check("a stale lock from a dead process is taken over", took_over)

            # release() must not delete someone else's lock.
            instance.release()
            check(
                "our own lock is released",
                instance.current_holder() is None,
                str(instance.current_holder()),
            )
            path.write_text(json.dumps({"pid": 999999999}), encoding="utf-8")
            instance.release()
            check(
                "release() leaves another owner's lock alone",
                path.is_file(),
            )
        finally:
            if other is not None:
                other.kill()
                other.wait(timeout=10)
            settings_module.config_dir = orig_config_dir
            try:
                (fake_dir / instance.LOCK_NAME).unlink(missing_ok=True)
            except OSError:
                pass


# ------------------------------------------------ 8. GUI deferred callback bug


def test_gui_deferred_callbacks() -> None:
    section("8. deferred GUI callbacks never raise NameError")

    try:
        from chocolate_egg_cream.gui import schedule_after
    except ImportError as exc:  # noqa: BLE001 - headless runner without tkinter
        print(f"  skip  tkinter unavailable here ({exc}); GUI callback tests skipped")
        return

    class FakeWidget:
        def __init__(self):
            self.calls: list[tuple] = []

        def after(self, delay, callback, *args):
            self.calls.append((delay, callback, args))

    # Arguments must be bound at schedule time, exactly like the bug fix:
    # inside an except block, `exc` is unbound once the block ends.
    widget = FakeWidget()
    errors: list[str] = []

    def raise_and_schedule(w) -> None:
        try:
            raise RuntimeError("Last.fm said no")
        except RuntimeError as exc:
            schedule_after(w, errors.append, str(exc))

    raise_and_schedule(widget)
    check("one callback was scheduled", len(widget.calls) == 1, str(widget.calls))
    check(
        "the error text was bound by value, not by the exc name",
        widget.calls and widget.calls[0][2] == ("Last.fm said no",),
        str(widget.calls),
    )
    if widget.calls:
        _, callback, args = widget.calls[0]
        callback(*args)  # would raise NameError with the old lambda approach
        check(
            "running the callback later works without the except block",
            errors == ["Last.fm said no"],
            str(errors),
        )

    # A dead window must not blow up the scheduler either.
    class DeadWidget:
        def after(self, delay, callback, *args):
            raise RuntimeError("application has been destroyed")

    try:
        schedule_after(DeadWidget(), lambda: None)
        raised = False
    except Exception:  # noqa: BLE001
        raised = True
    check("a destroyed window is handled quietly", not raised)

    # SetupDialog's queue bridge: a worker thread never touches Tk directly.
    class QueuedWidget:
        def __init__(self):
            self._deferred: Queue = Queue()

    queued = QueuedWidget()
    schedule_after(queued, errors.append, "from-worker")
    check(
        "a widget with a deferred queue receives the callback for the Tk thread",
        queued._deferred.qsize() == 1 and errors == ["Last.fm said no"],
        str(errors),
    )
    callback, args = queued._deferred.get_nowait()
    callback(*args)
    check(
        "draining the queue runs the callback",
        errors[-1] == "from-worker",
        str(errors),
    )


# --------------------------------------------------- 9. bounded shazamio retries


def test_shazam_retries_bounded() -> None:
    section("9. shazamio's retry storm is bounded")

    from chocolate_egg_cream.shazam_client import _bounded_http_client, _extract_http_status

    client = _bounded_http_client()
    check("a bounded HTTP client is created", client is not None)
    if client is not None:
        attempts = getattr(getattr(client, "retry_options", None), "attempts", None)
        check("it retries at most twice", attempts == 2, str(attempts))

    status, retry_after = _extract_http_status(RuntimeError("boom (status=503)"))
    check("a status embedded in a message is extracted", status == 503, str(status))

    try:
        from shazamio.exceptions import RateLimited
    except Exception:  # noqa: BLE001
        RateLimited = None
    if RateLimited is not None:
        status, _ = _extract_http_status(RateLimited("rate limited"))
        check("RateLimited maps to 429", status == 429, str(status))

    status, _ = _extract_http_status(ConnectionError("no route"))
    check("an unknown error yields no status", status is None, str(status))


# --------------------------------------------- 10. Stop / Quit always complete


async def test_stop_and_quit() -> None:
    section("10. Stop and Quit always complete, even with broken services")

    a = song("Artist A", "Song A", "AAA111")
    b = song("Artist B", "Song B", "BBB222")

    # Stop with an exploding service: must still stop and release the device.
    app = make_app(
        base_settings(),
        capture=FakeCapture(),
        recognizer=FakeRecognizer([a, a, b]),
    )
    boom, good = BoomService(), GoodService()
    app.scrobblers = [boom, good]
    await app.start()
    for _ in range(200):
        if app.tracks.current is not None:
            break
        await asyncio.sleep(0.02)
    check("a track was confirmed before Stop", app.tracks.current is not None)
    try:
        await app.stop()
        raised = False
    except Exception:  # noqa: BLE001
        raised = True
    check("Stop completes even when finishing the track explodes", not raised)
    check("Stop releases the capture", app.capture.closed >= 1, str(app.capture.closed))
    check("Stop marks the app as not running", not app.running)

    # Quit with a recognizer and a service whose close() explodes.
    class ExplodingRecognizer(FakeRecognizer):
        async def close(self):
            raise RuntimeError("close failed")

    app2 = make_app(
        base_settings(),
        capture=FakeCapture(),
        recognizer=ExplodingRecognizer([a, a]),
    )
    app2.scrobblers = [BoomService()]
    await app2.start()
    for _ in range(200):
        if app2.tracks.current is not None:
            break
        await asyncio.sleep(0.02)
    try:
        await app2.shutdown()
        raised = False
    except Exception:  # noqa: BLE001
        raised = True
    check("Quit/shutdown completes despite exploding close()", not raised)
    check("shutdown released the capture", app2.capture.closed >= 1, str(app2.capture.closed))

    # Stop still scrobbles the current track through the healthy service.
    app3 = make_app(
        base_settings(),
        capture=FakeCapture(),
        recognizer=FakeRecognizer([b, b]),
    )
    good3 = GoodService()
    app3.scrobblers = [good3]
    await app3.start()
    for _ in range(200):
        if app3.tracks.current is not None:
            break
        await asyncio.sleep(0.02)
    await app3.stop()
    check(
        "Stop finishes (and scrobbles) the current track",
        len(good3.scrobbles) == 1,
        str(good3.scrobbles),
    )


# ------------------------------------------ 11. the packaged verify command


def test_verify_command() -> None:
    section("11. the verify command runs end-to-end")

    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "verify.json"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "chocolate_egg_cream",
                "verify",
                "--skip-network",
                "--report",
                str(report),
            ],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=300,
        )
        check(
            "verify exits 0 from the source tree",
            proc.returncode == 0,
            f"rc={proc.returncode} out={proc.stdout[-400:]} err={proc.stderr[-400:]}",
        )
        check("verify writes a JSON report", report.is_file(), str(report))
        if report.is_file():
            data = json.loads(report.read_text(encoding="utf-8"))
            check("the report says it passed", data.get("ok") is True, str(data.get("ok")))
            check(
                "the source build is labelled as not frozen",
                data.get("frozen") is False,
                str(data.get("frozen")),
            )
            names = {item["name"] for item in data.get("checks", [])}
            for required in (
                "ca bundle",
                "requests TLS",
                "aiohttp TLS (Shazam stack)",
                "recognition stack",
                "audio capture",
            ):
                check(f"report contains '{required}'", required in names, str(sorted(names)))

        # The command must be wired into the CLI dispatcher.
        source = (REPO / "chocolate_egg_cream" / "__main__.py").read_text(encoding="utf-8")
        check("the CLI dispatches 'verify'", 'command == "verify"' in source)


async def main() -> int:
    test_ca_bundle_configuration()
    test_runtime_hook_and_spec()
    test_listenbrainz_endpoint()
    test_credential_validation()
    test_network_failures_are_contained()
    await test_service_isolation()
    await test_loop_survives_service_failure()
    test_deduplication_rules()
    await test_restart_and_repeated_songs()
    test_settings_safety()
    test_single_instance()
    test_gui_deferred_callbacks()
    test_shazam_retries_bounded()
    await test_stop_and_quit()
    test_verify_command()

    print()
    if FAILED:
        print(f"{len(FAILED)} failed: {', '.join(FAILED)}")
        return 1
    print("all good")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
