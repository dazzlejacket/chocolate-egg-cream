"""Regression tests for v1.1.1: Last.fm auth/reconnect, scrobble queue, DPI & UX."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chocolate_egg_cream import history, settings as settings_module, theme
from chocolate_egg_cream.app import EggCream
from chocolate_egg_cream.recognize import Recognition
from chocolate_egg_cream.scrobble import lastfm_app
from chocolate_egg_cream.scrobble.lastfm import LastFM
from chocolate_egg_cream.track import FinishedTrack


def _run(coro):
    return asyncio.run(coro)


def test_reconnect_bundled_overrides_leftover_custom_keys():
    """Reconnecting with bundled creds must NOT fall back to leftover custom keys."""
    cfg = dict(settings_module.DEFAULTS)
    cfg.update(
        {
            "lastfm_api_key": "old_custom_key",
            "lastfm_api_secret": "old_custom_secret",
            "lastfm_session_key": "new_bundled_sk",
            "lastfm_use_custom_credentials": False,
            "lastfm_session_mode": "bundled",
        }
    )
    with mock.patch.dict(
        os.environ,
        {"CEC_LASTFM_API_KEY": "bundled_key", "CEC_LASTFM_API_SECRET": "bundled_secret"},
    ):
        key, secret, using_custom = lastfm_app.resolve_credentials(cfg)
    assert (key, secret, using_custom) == ("bundled_key", "bundled_secret", False), (
        key,
        secret,
        using_custom,
    )


def test_legacy_v10x_upgrade_preserves_custom_keys():
    """Existing v1.0.x config (no lastfm_session_mode) keeps working on upgrade."""
    cfg = dict(settings_module.DEFAULTS)
    cfg.update(
        {
            "lastfm_api_key": "legacy_custom_key",
            "lastfm_api_secret": "legacy_custom_secret",
            "lastfm_session_key": "legacy_sk",
            "lastfm_use_custom_credentials": False,
            "lastfm_session_mode": "",
        }
    )
    with mock.patch.dict(
        os.environ,
        {"CEC_LASTFM_API_KEY": "bundled_key", "CEC_LASTFM_API_SECRET": "bundled_secret"},
    ):
        key, secret, using_custom = lastfm_app.resolve_credentials(cfg)
    assert (key, secret, using_custom) == ("legacy_custom_key", "legacy_custom_secret", True)


def test_lastfm_ignored_scrobble_is_not_fake_success():
    """Last.fm HTTP 200 with ignored=1 must return False with a real error."""
    lfm = LastFM(api_key="k", api_secret="s", session_key="sk")
    fake_resp = mock.Mock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "scrobbles": {
            "@attr": {"accepted": 0, "ignored": 1},
            "scrobble": {"ignoredMessage": {"code": "1", "#text": "Artist was ignored"}},
        }
    }
    with mock.patch.object(lfm._session, "post", return_value=fake_resp):
        ok = lfm.scrobble("Artist", "Track", timestamp=1700000000)
    assert not ok
    assert lfm.last_error and "ignored" in lfm.last_error.lower()
    lfm.close()


def test_ten_consecutive_songs_outage_reconnect_restart_no_duplicates():
    """10 consecutive songs across Last.fm error 9 outage, restart, reconnect, recovery."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with mock.patch.object(settings_module, "config_dir", return_value=tmp_path):
            lfm_submitted: list[tuple[str, int]] = []
            lb_submitted: list[tuple[str, int]] = []

            class FakeLastFM:
                name = "Last.fm"
                service_key = "lastfm"

                def __init__(self, fail_error9: bool = False):
                    self.configured = True
                    self.session_stale = False
                    self.last_error = None
                    self.fail_error9 = fail_error9

                def scrobble(self, artist, title, album=None, timestamp=None, duration=None, isrc=None):
                    if self.fail_error9:
                        self.session_stale = True
                        self.last_error = "error 9: Invalid session key - Please re-authenticate"
                        return False
                    lfm_submitted.append((f"{artist} — {title}", int(timestamp)))
                    return True

                def now_playing(self, *a, **kw):
                    return True

                def close(self):
                    pass

            class FakeLB:
                name = "ListenBrainz"
                service_key = "listenbrainz"
                configured = True
                last_error = None

                def scrobble(self, artist, title, album=None, timestamp=None, duration=None, isrc=None):
                    lb_submitted.append((f"{artist} — {title}", int(timestamp)))
                    return True

                def now_playing(self, *a, **kw):
                    return True

                def close(self):
                    pass

            cfg = dict(settings_module.DEFAULTS)
            cfg["enabled_services"] = ["lastfm", "listenbrainz"]
            saved_cfgs: list[dict] = []

            engine = EggCream(
                cfg,
                capture=object(),
                recognizer=object(),
                save_settings=lambda d: saved_cfgs.append(dict(d)),
            )
            fake_lfm = FakeLastFM(fail_error9=False)
            fake_lb = FakeLB()
            engine.scrobblers = [fake_lfm, fake_lb]

            stale_events: list[str] = []
            engine.on_event = lambda k, p: stale_events.append(str(p)) if k == "lastfm_stale" else None

            base_ts = int(time.time()) - 3600
            for idx in range(1, 11):
                # Songs 4, 5, 6 hit Last.fm error 9; ListenBrainz must keep succeeding!
                fake_lfm.fail_error9 = idx in (4, 5, 6)
                rec = Recognition(
                    artist=f"Artist {idx}",
                    title=f"Song {idx}",
                    album=f"Album {idx}",
                    duration=200.0,
                    shazam_key=f"key-{idx}",
                )
                ft = FinishedTrack(
                    recognition=rec,
                    started_at=float(base_ts + idx * 210),
                    played_seconds=180.0,
                )
                _run(engine._finish(ft))

            # ListenBrainz got all 10; Last.fm got 7 (1-3, 7-10) and queued 3 (4-6)
            assert len(lb_submitted) == 10, len(lb_submitted)
            assert len(lfm_submitted) == 7, len(lfm_submitted)
            assert len(stale_events) == 3, stale_events
            pending = history.load_pending()
            assert len(pending) == 3, len(pending)
            assert [p["title"] for p in pending] == ["Song 4", "Song 5", "Song 6"]

            # Simulate app restart: new EggCream instance loads persisted scrobble_events
            restarted_cfg = saved_cfgs[-1] if saved_cfgs else cfg
            engine2 = EggCream(
                restarted_cfg,
                capture=object(),
                recognizer=object(),
                save_settings=lambda d: saved_cfgs.append(dict(d)),
            )
            engine2.scrobblers = [FakeLastFM(fail_error9=False), FakeLB()]

            # Retry pending after reconnect
            succeeded, remaining = _run(engine2.retry_pending())
            assert succeeded == 3 and remaining == 0, (succeeded, remaining)
            assert len(lfm_submitted) == 10, len(lfm_submitted)
            assert len(lb_submitted) == 10, "ListenBrainz must not receive duplicates"

            # Verify original timestamps preserved for recovered songs 4, 5, 6
            lfm_by_title = dict(lfm_submitted)
            for idx in (4, 5, 6):
                assert lfm_by_title[f"Artist {idx} — Song {idx}"] == base_ts + idx * 210

            # Verify all 10 are in local history with submitted status for both services
            records = history.load_history(newest_first=False)
            assert len(records) == 10
            for rec in records:
                assert rec["services"]["lastfm"]["status"] == "submitted"
                assert rec["services"]["listenbrainz"]["status"] == "submitted"


def test_theme_palettes_have_purple_accents():
    assert theme.LIGHT.accent == "#6d28d9"
    assert theme.DARK.accent == "#8b5cf6"


if __name__ == "__main__":
    tests = [
        test_reconnect_bundled_overrides_leftover_custom_keys,
        test_legacy_v10x_upgrade_preserves_custom_keys,
        test_lastfm_ignored_scrobble_is_not_fake_success,
        test_ten_consecutive_songs_outage_reconnect_restart_no_duplicates,
        test_theme_palettes_have_purple_accents,
    ]
    for fn in tests:
        fn()
        print(f"PASS: {fn.__name__}")
    print(f"\nAll {len(tests)} v1.1.1 regression tests passed.")
