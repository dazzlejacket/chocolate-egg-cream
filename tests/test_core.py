"""Tests for the parts that have no network or audio device involved.

    python tests/test_core.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chocolate_egg_cream.recognize import Recognition, to_wav_bytes
from chocolate_egg_cream.track import TrackState

FAILED = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name} {detail}")
        FAILED.append(name)


def song(artist: str, title: str, isrc: str | None = None) -> Recognition:
    return Recognition(artist=artist, title=title, isrc=isrc)


def test_confirm_twice() -> None:
    print("confirm twice")
    state = TrackState(confirmations_required=2)
    a = song("Artist A", "Song A")

    check("a single match is not enough", state.observe(a, 0.0) is None)
    check("nothing is current yet", state.current is None)

    state.observe(a, 1.0)
    check("the second match confirms", state.current is not None)
    check("the confirmed track is the right one", state.current.identity == a.identity)


def test_failure_keeps_the_track() -> None:
    print("a failed recognition keeps the current track")
    state = TrackState(confirmations_required=2)
    a = song("Artist A", "Song A")
    state.observe(a, 0.0)
    state.observe(a, 1.0)

    for at in (2.0, 3.0, 4.0):
        state.observe(None, at)

    check("the track is still current", state.current is not None)
    check("it is still the same track", state.current.identity == a.identity)


def test_song_change() -> None:
    print("a song change is detected")
    state = TrackState(confirmations_required=2)
    a = song("Artist A", "Song A")
    b = song("Artist B", "Song B")

    state.observe(a, 0.0)
    state.observe(a, 1.0)

    check("one sighting of B changes nothing", state.observe(b, 2.0) is None)
    check("A is still current", state.current.identity == a.identity)

    finished = state.observe(b, 3.0)
    check("the second sighting of B confirms it", finished is not None)
    check("B is now current", state.current.identity == b.identity)
    check("the finished track is A", finished.recognition.identity == a.identity)


def test_playtime() -> None:
    print("playtime")
    state = TrackState(confirmations_required=2)
    a = song("Artist A", "Song A")
    b = song("Artist B", "Song B")

    state.observe(a, 0.0)
    state.observe(a, 10.0)
    state.observe(a, 20.0)
    check("playtime is counted", abs(state.playtime - 10.0) < 0.001, f"got {state.playtime}")

    state.observe(a, 25.0, heard_audio=False)
    check(
        "silence does not count as playtime",
        abs(state.playtime - 10.0) < 0.001,
        f"got {state.playtime}",
    )

    check("one sighting of B changes nothing", state.observe(b, 30.0) is None)
    finished = state.observe(b, 31.0)
    check("the second sighting of B finishes A", finished is not None)
    if finished is not None:
        # 10s from t=10 to t=20, the silent 20s to 25s is skipped, 5s from
        # t=25 to t=30 and 1s more while B was being confirmed.
        check(
            "the finished track carries its playtime",
            abs(finished.played_seconds - 16.0) < 0.001,
            f"got {finished.played_seconds}",
        )


def test_identity() -> None:
    print("identity")
    one = song("Artist A", "Song A", isrc="AAA")
    two = song("Artist A", "Song A", isrc="AAA")
    three = song("artist a", "song a")
    four = song("Artist A", "Song B")
    five = song("Artist C", "Song D")

    check("the same isrc is the same song", one.identity == two.identity)
    check("no isrc falls back to artist and title", three.identity == "artist a::song a")
    check("the fallback ignores case", three.identity == song("ARTIST A", "SONG A").identity)
    check("different songs are different", four.identity != five.identity)
    check("a song without an isrc is not an isrc song", four.identity != one.identity)


def test_wav_roundtrip() -> None:
    print("wav encoding")
    import numpy as np

    samples = np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype=np.float32)
    data = to_wav_bytes(samples, 48000)
    check("the wav starts with a RIFF header", data[:4] == b"RIFF")
    check("the wav has the right length", len(data) == 44 + 10)


def test_lastfm_sign_and_spanish_utf8() -> None:
    print("lastfm signing & Spanish UTF-8")
    from chocolate_egg_cream.scrobble.lastfm import sign

    params = {
        "method": "track.scrobble",
        "artist": "Café Tacvba",
        "track": "¿Cómo Te Extraño Mi Amor?",
        "api_key": "abc",
    }
    sig_without_format = sign(params, "secret")
    params_with_format = dict(params, format="json", callback="cb")
    sig_with_format = sign(params_with_format, "secret")
    check("format and callback are excluded from api_sig", sig_without_format == sig_with_format)
    check("api_sig is a 32-char md5 hex digest", len(sig_without_format) == 32)


def test_song_urls_unicode_and_language_neutral() -> None:
    print("song lookup urls (Unicode, language-neutral)")
    from chocolate_egg_cream.gui import build_song_urls

    track = song("Natalia Lafourcade", "Hasta la Raíz")
    urls = build_song_urls(track)
    check("translate url is removed", "translate" not in urls)
    check("lyrics url is language-neutral", "english+translation" not in urls["lyrics"])
    check("lyrics url ends with lyrics", "lyrics" in urls["lyrics"])
    check("lyrics url encodes Unicode title", "Hasta+la+Ra%C3%ADz" in urls["lyrics"])
    check("youtube url targets search_query", "youtube.com/results?search_query=" in urls["youtube"])

    jp = song("宇多田ヒカル", "First Love")
    jp_urls = build_song_urls(jp)
    check("Japanese Unicode encoded in youtube url", "%E5%AE%87%E5%A4%9A%E7%94%B0" in jp_urls["youtube"])


def test_mac_capture_streaming_and_stderr() -> None:
    print("mac capture stream & stderr")
    import io
    from chocolate_egg_cream.capture.macos import MacCapture

    class FakeProc:
        def __init__(self, stdout_bytes: bytes, stderr_bytes: bytes = b"") -> None:
            self.stdout = io.BytesIO(stdout_bytes)
            self.stderr = io.BytesIO(stderr_bytes)
            self._rc = None

        def poll(self):
            return self._rc

        def terminate(self):
            self._rc = 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self._rc = -9

    # Test info: lines are separated from error lines and errors surface cleanly
    cap = MacCapture(sample_rate=48000, backend="coreaudio")
    check("backend is preserved", cap.backend == "coreaudio")
    cap._process = FakeProc(
        b"",
        b"info: backend=coreaudio native_rate=48000\n"
        b"error: System Audio Recording Only permission is required.\n",
    )
    cap._stderr_lines.clear()
    cap._info_lines.clear()
    cap._eof = False
    cap._drain_stderr()
    cap._eof = True
    check("info lines are kept separate", cap._info_lines == ["backend=coreaudio native_rate=48000"])
    try:
        cap.read(0.1)
        check("read raises on helper exit", False)
    except RuntimeError as exc:
        check(
            "stderr message is surfaced in RuntimeError",
            "System Audio Recording Only" in str(exc),
            str(exc),
        )
        check(
            "info line is not mixed into error text",
            "native_rate" not in str(exc),
            str(exc),
        )


def test_shazam_outage_backoff_and_recovery() -> None:
    print("shazam outage, rate-limit, backoff, and recovery")
    import asyncio
    from chocolate_egg_cream.shazam_client import ShazamClient

    now = [100.0]

    class Fake429Error(Exception):
        status = 429
        headers = {"Retry-After": "45"}

    class FlakyShazam:
        def __init__(self, behaviors):
            self.behaviors = list(behaviors)
            self.calls = 0

        async def recognize(self, audio, **kwargs):
            self.calls += 1
            item = self.behaviors.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        async def close(self):
            pass

    async def run() -> None:
        # 1. Transient failure with 1 bounded retry -> backoff -> recovery
        client = ShazamClient(
            timeout=5.0,
            max_retries=1,
            retry_delay=0.0,
            base_backoff=15.0,
            max_backoff=120.0,
            clock=lambda: now[0],
        )
        fake = FlakyShazam([
            ConnectionError("offline"),
            ConnectionError("still offline"),
            {"track": {"title": "Recovered Song", "subtitle": "Artist R"}},
        ])
        client._shazam = fake
        client._accepts_options = False
        client._accepts_max_skew = False

        res1 = await client.recognize(b"wav")
        check("outage returns None", res1 is None)
        check("bounded retry ran exactly 2 attempts", fake.calls == 2, f"calls={fake.calls}")
        check("client enters backoff", client.in_backoff)
        check("backoff is 15s", abs(client.backoff_remaining - 15.0) < 0.01)

        # Call during backoff must NOT hit network
        res_skipped = await client.recognize(b"wav")
        check("during backoff returns None", res_skipped is None)
        check("no network call during backoff", fake.calls == 2, f"calls={fake.calls}")

        # Advance clock past backoff -> recovers automatically
        now[0] += 16.0
        res_ok = await client.recognize(b"wav")
        check("recovers after backoff", res_ok is not None and res_ok["track"]["title"] == "Recovered Song")
        check("consecutive failures reset to 0", client.consecutive_failures == 0)
        check("no longer in backoff", not client.in_backoff)

        # 2. HTTP 429 rate-limit respects Retry-After and does not immediately retry
        fake429 = FlakyShazam([Fake429Error("too many requests")])
        client._shazam = fake429
        res429 = await client.recognize(b"wav")
        check("429 returns None", res429 is None)
        check("429 did not immediately retry", fake429.calls == 1, f"calls={fake429.calls}")
        check("429 respected Retry-After 45s", abs(client.backoff_remaining - 45.0) < 0.01)

    asyncio.run(run())


def test_os_credentials_and_settings_roundtrip() -> None:
    print("os credential store & settings roundtrip")
    import json
    import tempfile
    from chocolate_egg_cream import credentials, settings as settings_module

    # Test real OS credential store write/read/delete using a dedicated test key
    test_key = "__eggcream_unit_test_secret__"
    stored = credentials.set_secret(test_key, "s3cr3t-value-日本語")
    if stored:
        got = credentials.get_secret(test_key)
        check("OS credential store round-trips Unicode secret", got == "s3cr3t-value-日本語", repr(got))
        credentials.delete_secret(test_key)
        after = credentials.get_secret(test_key)
        check("OS credential store deletes secret", after is None, repr(after))
    else:
        print("  skip  OS credential store unavailable in this environment")

    # Test settings save strips secrets from JSON when keyring succeeds
    orig_dir = settings_module.config_dir
    orig_set = credentials.set_secret
    orig_get = credentials.get_secret
    fake_vault: dict[str, str] = {}

    def fake_set(k: str, v: str) -> bool:
        if v:
            fake_vault[k] = v
        else:
            fake_vault.pop(k, None)
        return True

    def fake_get(k: str) -> str | None:
        return fake_vault.get(k)

    with tempfile.TemporaryDirectory() as tmp:
        try:
            settings_module.config_dir = lambda: Path(tmp)
            credentials.set_secret = fake_set
            credentials.get_secret = fake_get

            cfg = dict(settings_module.DEFAULTS)
            cfg["lastfm_api_key"] = "pub-key"
            cfg["lastfm_api_secret"] = "top-secret"
            cfg["lastfm_session_key"] = "sess-123"
            cfg["listenbrainz_token"] = "lb-tok-456"
            settings_module.save(cfg)

            raw_json = json.loads((Path(tmp) / "settings.json").read_text(encoding="utf-8"))
            check("settings.json keeps public api key", raw_json["lastfm_api_key"] == "pub-key")
            check("settings.json does not store api secret", raw_json["lastfm_api_secret"] == "")
            check("settings.json does not store session key", raw_json["lastfm_session_key"] == "")
            check("settings.json does not store listenbrainz token", raw_json["listenbrainz_token"] == "")

            loaded = settings_module.load()
            check("load restores api secret from vault", loaded["lastfm_api_secret"] == "top-secret")
            check("load restores session key from vault", loaded["lastfm_session_key"] == "sess-123")
            check("load restores listenbrainz token from vault", loaded["listenbrainz_token"] == "lb-tok-456")
        finally:
            settings_module.config_dir = orig_dir
            credentials.set_secret = orig_set
            credentials.get_secret = orig_get


def main() -> int:
    for test in (
        test_confirm_twice,
        test_failure_keeps_the_track,
        test_song_change,
        test_playtime,
        test_identity,
        test_wav_roundtrip,
        test_lastfm_sign_and_spanish_utf8,
        test_song_urls_unicode_and_language_neutral,
        test_mac_capture_streaming_and_stderr,
        test_shazam_outage_backoff_and_recovery,
        test_os_credentials_and_settings_roundtrip,
    ):
        test()
    print()
    if FAILED:
        print(f"{len(FAILED)} failed: {', '.join(FAILED)}")
        return 1
    print("all good")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
