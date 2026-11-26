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
- **Theme** — switch between monochrome **Light**, **Dark**, and **System**
  themes (remembered across launches).
- **Copy**, **Lyrics**, **YouTube** — quick actions for the current song or any
  song selected in **Recent** (language-neutral search, full Unicode support).
- **Open Listening History** — view every locally saved listen, retry failed
  scrobbles, and export to **TXT**, **CSV**, or **JSONL**.
- **Setup** — connect Last.fm, ListenBrainz (including custom Multi-Scrobbler
  URLs), and optional external scrobbler mode without editing JSON.

### Scrobbling & External Mode

- **Last.fm**: Official release builds bundle dedicated Chocolate Egg Cream
  application credentials — just click **1. Authorize Last.fm**, approve in your
  browser, and click **2. Finish auth**. Stale or revoked sessions are detected
  automatically with actual API errors and a quick reconnect prompt. Custom API
  credentials remain available under **Advanced**. (For local release builds,
  set `CEC_LASTFM_API_KEY` and `CEC_LASTFM_API_SECRET` at build time; private
  credentials are never committed to git.)
- **ListenBrainz**: Paste your user token from
  https://listenbrainz.org/settings/ into **Setup**. Optionally set a custom
  **Server URL** for Multi-Scrobbler or self-hosted ListenBrainz-compatible
  servers.
- **External scrobbler mode (optional)**: Publishes confirmed Now Playing
  metadata to **Windows SMTC** or **Linux MPRIS2** (compatible with Pano
  Scrobbler) without playing fake audio or interfering with other media
  sessions. *(Not enabled on macOS because `MPNowPlayingInfoCenter` is a single
  global slot that would hijack media keys from your active player.)*
- **Automatic local history & retries**: Every eligible completed listen is
  appended locally to `history.jsonl` (artist, title, album, timestamp,
  duration, ISRC, and service status) even with no scrobbling accounts
  configured. Failed scrobbles are preserved in `pending_scrobbles.json` and
  retried automatically without creating duplicates.

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
- **Duplicate protection**: scrobble history is tracked **per service, per
  playback event**: the same listen is never submitted twice (across restarts
  too), but a song played again later is scrobbled again as a new listen, and
  one service's history never blocks the other.
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
- `python -m chocolate_egg_cream verify` — verify this build: CA bundle, real TLS through both HTTP stacks, recognition stack, audio capture (used as the packaging gate; run it on the packaged executable too)
- `python -m chocolate_egg_cream test` — capture one sample and identify it
- `python -m chocolate_egg_cream auth lastfm` — terminal Last.fm auth
- `python -m chocolate_egg_cream set <key> <value>` — update a setting
- `python -m chocolate_egg_cream settings` — print settings file path
- `python -m chocolate_egg_cream --version` — print version

Only one instance runs at a time: starting a second one shows a warning instead
of recognising (and scrobbling) the same music twice.

### Reproducible Release Builds

Install build dependencies (`pip install -r requirements-release.txt -r requirements-build.txt`) and run the native script for your OS:

- Windows x64: `pwsh -File scripts/build_windows.ps1`
- macOS (Apple Silicon / Intel): `bash scripts/build_macos.sh`
- Linux x86_64: `bash scripts/build_linux.sh`

Each script finishes by running the **packaged executable's own `verify`
command** (CA bundle present, real TLS handshake through `requests` and
`aiohttp`, recognition stack, audio helper) and fails the build if the
artifact would ship broken. CI runs the same gate on every push.

See [docs/RELEASE_VERIFICATION.md](docs/RELEASE_VERIFICATION.md) for signing,
notarization, and native hardware verification checklists.

## Tests

```bash
python tests/test_core.py
python tests/test_scrobble_flow.py
python tests/test_regressions.py   # one test per v1.0.1 bug fix
```
