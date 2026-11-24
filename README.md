# Chocolate Egg Cream

A small, fast, standalone desktop app that listens to what your computer is
playing, identifies the song with Shazam, and scrobbles it to Last.fm and/or
ListenBrainz.

It never captures your screen, video, or microphone. It captures the audio your
system is playing and nothing else.

```
AudioCapture  ->  Recognizer (ShazamIO)  ->  TrackState  ->  Scrobblers
 (per OS)          (retries + backoff)       (confirm 2x)    (Last.fm, ListenBrainz)
```

## Download & Install

Download the release for your platform from **GitHub Releases** and verify the
SHA-256 checksum in `SHA256SUMS.txt` if desired. No Python, Homebrew, Terminal,
or Xcode installation is required.

### macOS (Apple Silicon & Intel)

1. Download **`ChocolateEggCream-<version>-macos-arm64.dmg`** (Apple Silicon) or
   **`ChocolateEggCream-<version>-macos-x86_64.dmg`** (Intel).
2. Open the `.dmg` and drag **Chocolate Egg Cream** into **Applications**.
3. Double-click **Chocolate Egg Cream** in **Applications**.
   - *If the release is ad-hoc signed (not notarized):* macOS Gatekeeper may ask
     you to confirm on first launch via **System Settings → Privacy & Security →
     Open Anyway**.
4. Click **Start**. On macOS 14.2+, Chocolate Egg Cream uses **Core Audio Taps**
   and asks macOS for **System Audio Recording Only** permission for **Chocolate
   Egg Cream** itself (never Terminal, never screen video, never microphone).
   On macOS 13.0–14.1, it automatically falls back to ScreenCaptureKit audio.

### Windows (x64)

1. Download **`ChocolateEggCream-<version>-windows-x64.exe`**.
2. Double-click to launch (no installer, no console window, no Python required).
3. Play music or internet radio and click **Start**.

### Linux (x86_64)

1. Download **`ChocolateEggCream-<version>-linux-x86_64.AppImage`** (or the
   portable `.tar.gz` archive if FUSE is not installed).
2. Make it executable and run it:
   ```bash
   chmod +x ChocolateEggCream-<version>-linux-x86_64.AppImage
   ./ChocolateEggCream-<version>-linux-x86_64.AppImage
   ```
3. Captures your default PipeWire or PulseAudio sink monitor (`*.monitor`) with
   no root privileges and no microphone permission.

## Using the App

The window stays deliberately small:

- **Start / Stop** — start or stop system audio capture (Stop and Quit release
  the audio device and stop all background work immediately).
- **Copy**, **Lyrics**, **YouTube** — quick actions for the current song or any
  song selected in **Recent Songs** (language-neutral search, full Unicode
  support).
- **Setup…** — configure Last.fm and/or ListenBrainz without editing JSON.

### Scrobbling Setup

- **Last.fm**: Create an API account at https://www.last.fm/api/account/create,
  enter your API key and secret in **Setup…**, click **1. Open Last.fm**, click
  **Yes, allow access** in your browser, then click **2. Finish auth**.
- **ListenBrainz**: Create a user token at https://listenbrainz.org/settings/
  and paste it into **Setup…**.

## Privacy & Security

- **System audio only**: never accesses the microphone, camera, or screen video.
- **No permanent recordings**: short 12-second audio samples are held in memory
  only, sent to Shazam for recognition, and immediately discarded.
- **OS credential storage**: Last.fm API secret, Last.fm session key, and
  ListenBrainz user token are stored in Windows Credential Manager, macOS
  Keychain, or Linux Secret Service (`secret-tool`) when available, rather than
  plaintext `settings.json`.
- **No telemetry**, **no analytics**, **no runtime downloads**, and **no
  credentials in logs**.

## How Recognition & Scrobbling Work

- **Confirm twice**: the same song must be identified twice before becoming the
  current track.
- **Outage resilience**: timeouts, HTTP 429 rate limits, and Shazam outages use
  bounded retries and exponential backoff with automatic recovery. Network
  failures never end the current track or trigger false song changes.
- **Duplicate protection**: each track identity (ISRC or normalized
  artist/title) is remembered so stale or repeated matches are never scrobbled
  twice.
- **Playtime rule**: playtime only advances while audio is actually heard. A
  track scrobbles when it plays at least half its duration (capped at 4
  minutes), or `min_play_seconds` (default 120s) when duration is unknown.
- **Pluggable provider**: ShazamIO is the default; see
  [docs/RECOGNITION_PROVIDERS.md](docs/RECOGNITION_PROVIDERS.md) for our
  evaluation of ShazamKit, AudD, ACRCloud, and AcoustID.

## Settings & Logs

Non-secret settings live in:
- Windows: `%APPDATA%\chocolate-egg-cream\settings.json`
- macOS / Linux: `~/.config/chocolate-egg-cream/settings.json`

A rotating log (`eggcream.log`, capped at 1 MB × 3 files) sits next to it.

| Key | Meaning | Default |
| --- | --- | --- |
| `enabled_services` | `["lastfm", "listenbrainz"]` | both |
| `interval_seconds` | Seconds between samples | `25` |
| `sample_seconds` | Length of each sample in seconds | `12` |
| `confirmations_required` | Matches needed to confirm a track | `2` |
| `min_play_seconds` | Minimum playtime when duration is unknown | `120` |
| `max_skew` | Max Shazam time/frequency skew (`0` disables) | `0` |
| `macos_capture_backend` | `auto` (Core Audio Taps on 14.2+), `coreaudio`, or `screencapturekit` | `auto` |

## Running & Building from Source

Python 3.10+ is required for source installs.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-release.txt   # Windows: .venv\Scripts\pip
python -m chocolate_egg_cream
```

CLI commands:
- `python -m chocolate_egg_cream` — open GUI
- `python -m chocolate_egg_cream console` — terminal mode
- `python -m chocolate_egg_cream check` — verify capture & scrobbler readiness
- `python -m chocolate_egg_cream test` — capture one sample and identify it
- `python -m chocolate_egg_cream auth lastfm` — terminal Last.fm auth
- `python -m chocolate_egg_cream set <key> <value>` — update a setting
- `python -m chocolate_egg_cream settings` — print settings file path
- `python -m chocolate_egg_cream --version` — print version

### Reproducible Release Builds

Install build dependencies (`pip install -r requirements-release.txt -r requirements-build.txt`) and run the native script for your OS:

- Windows x64: `pwsh -File scripts/build_windows.ps1`
- macOS (Apple Silicon / Intel): `bash scripts/build_macos.sh`
- Linux x86_64: `bash scripts/build_linux.sh`

See [docs/RELEASE_VERIFICATION.md](docs/RELEASE_VERIFICATION.md) for signing,
notarization, and native hardware verification checklists.

## Tests

```bash
python tests/test_core.py
python tests/test_scrobble_flow.py
```
