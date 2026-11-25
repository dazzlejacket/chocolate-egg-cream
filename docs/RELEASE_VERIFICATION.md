# Platform Release Verification & Signing Guide

CI compilation verifies that binaries build cleanly, **not** that live audio capture, permissions, and GUI behavior work on real hardware. Run the checklists below on native macOS and Linux machines before publishing a release.

## Automated Artifact Gate (all platforms)

Every build script (and the CI `package` job on every push) finishes by running
the **packaged executable's own diagnostics**:

```text
Chocolate Egg Cream verify            # packaged build (or: python -m chocolate_egg_cream verify)
```

It writes a JSON report (`dist/verify-report.json`) and exits non-zero on
failure. It checks, inside the shipped bundle:

- certifi's `cacert.pem` is present and is what `ssl`/`requests`/`aiohttp` will load;
- **real TLS handshakes** through `requests` (scrobblers) *and* `aiohttp`
  (the Shazam stack) — so a `CERTIFICATE_VERIFY_FAILED` bundle cannot ship;
- the recognition stack (`shazamio`, `shazamio_core`, `numpy`) imports;
- audio capture constructs and (macOS) the bundled `SystemAudioCapture` helper exists;
- tkinter imports and the settings directory is writable.

SSL verification is never disabled anywhere in this project; the gate proves it
works, not that it was turned off.

Use `verify --skip-network` for an offline check, and `verify --report <path>`
to choose where the JSON report goes (a windowed Windows build has no stdout,
so the report file is the source of truth).

## macOS Verification (Apple Silicon & Intel)

### 1. Critical Permission Test (Revoked Terminal Permission)

Chocolate Egg Cream on macOS 14.2+ uses **Core Audio Taps** (`AudioHardwareCreateProcessTap`), which requests audio-only permission (`NSAudioCaptureUsageDescription`) scoped to **Chocolate Egg Cream.app** — never Terminal, never Screen video, never the microphone.

1. Open **System Settings → Privacy & Security → Screen & System Audio Recording**.
2. **Revoke / toggle OFF** permission for **Terminal** (and iTerm2 / VS Code if present).
3. Double-click `ChocolateEggCream-<version>-macos-<arch>.dmg` and drag **Chocolate Egg Cream.app** into **Applications**.
4. Launch **Chocolate Egg Cream** from `/Applications` by double-clicking it (do **not** launch from Terminal).
   - *If the build is ad-hoc signed (not notarized):* macOS Gatekeeper will warn on first launch. Open **System Settings → Privacy & Security** and click **Open Anyway** (never disable Gatekeeper globally).
5. Play music or internet radio, then click **Start**.
6. macOS should prompt for **System Audio Recording** for **Chocolate Egg Cream** (listed under *System Audio Recording Only* on macOS 14.2+). Allow it.
7. **Verify:**
   - System audio is captured and songs are identified **while Terminal's Screen & System Audio Recording permission remains OFF**.
   - No microphone indicator (orange dot) appears in the macOS menu bar.
   - No screen recording indicator appears in the macOS menu bar (on macOS 14.2+ using Core Audio Taps).
   - Clicking **Stop** immediately stops the `SystemAudioCapture` helper process (`pgrep SystemAudioCapture` returns nothing).
   - Quitting the app leaves zero background processes.

### 2. macOS 13.0–14.1 Compatibility Fallback

- On macOS 13.0–14.1, `SystemAudioCapture` automatically falls back to ScreenCaptureKit (audio-only stream, no microphone).
- Users on macOS 14.2+ can also explicitly select ScreenCaptureKit if needed via `macos_capture_backend: "screencapturekit"` in `settings.json`.

### 3. Signing & Notarization Notes

- `scripts/build_macos.sh` and `.github/workflows/release.yml` support full **Developer ID Application** signing (`--options runtime --timestamp`) and **Apple notarization + stapling** (`xcrun notarytool` + `xcrun stapler`) when these GitHub Secrets are configured:
  - `APPLE_CERT_P12_BASE64`
  - `APPLE_CERT_PASSWORD`
  - `APPLE_SIGNING_IDENTITY`
  - `APPLE_ID`
  - `APPLE_TEAM_ID`
  - `APPLE_APP_PASSWORD`
- Without an Apple Developer ID certificate, builds are **ad-hoc signed** (`codesign --sign -`). Limitation: Gatekeeper requires a one-time **Open Anyway** confirmation on first launch, and macOS may re-prompt for System Audio Recording permission after upgrading to a newly ad-hoc-signed version. We never disable Gatekeeper, SIP, or hardened runtime protections.

## Linux Verification (x86_64, PipeWire / PulseAudio)

1. Download `ChocolateEggCream-<version>-linux-x86_64.AppImage` (or `.tar.gz` if FUSE is unavailable).
2. `chmod +x ChocolateEggCream-<version>-linux-x86_64.AppImage`
3. Run `./ChocolateEggCream-<version>-linux-x86_64.AppImage` as a normal (non-root) user with **no** Python venv or source tree present.
4. **Verify:**
   - GUI opens cleanly without a terminal window requirement.
   - System audio is captured from the default PipeWire/PulseAudio sink monitor (`*.monitor`), **never** the microphone.
   - No root privileges and no microphone permission are required.
   - Play a song -> confirms twice -> displays track -> Lyrics/YouTube/Copy work with Unicode titles.
   - Scrobbling to Last.fm / ListenBrainz works; credentials are stored in GNOME Keyring / KWallet via `secret-tool` when available (or `0600`-permissioned `settings.json` otherwise).
   - **Stop** and **Quit** release the PipeWire/PulseAudio capture stream immediately (verify in `pavucontrol` / `wpctl status`).
