# Chocolate Egg Cream

A very small desktop app that listens to what your computer is playing, works
out the song with Shazam, and scrobbles it to Last.fm and/or ListenBrainz.

It never uses the microphone. It captures the audio your system is playing
and nothing else.

```
AudioCapture  ->  ShazamRecognizer  ->  TrackState  ->  Scrobblers
 (per OS)          (ShazamIO)          (confirm twice)   (Last.fm, ListenBrainz)
```

## Install

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt          # Windows: .venv\Scripts\pip
```

Python 3.10 or newer.

## Run

```bash
python -m chocolate_egg_cream
```

That opens a small window:

```
  CHOCOLATE EGG CREAM
  Gold Lounge — Birds
  listening · 1:23

  [ Start ]  [ Stop ]

  Last.fm ready · ListenBrainz needs setup
  [ Setup… ]
```

Press **Start** to begin capturing and recognising, **Stop** to pause. The
**Setup…** button opens a small window where the Last.fm and ListenBrainz
details can be filled in without touching the settings file by hand.

The window is deliberately small: the current song, how long it has been
playing, and the two buttons. Nothing else.

Other commands:

| Command | What it does |
| --- | --- |
| `python -m chocolate_egg_cream console` | Run in the terminal instead of a window |
| `python -m chocolate_egg_cream test` | Take one sample and print what Shazam makes of it |
| `python -m chocolate_egg_cream auth lastfm` | Run the Last.fm authorisation in the terminal |
| `python -m chocolate_egg_cream set <key> <value>` | Change one setting |
| `python -m chocolate_egg_cream settings` | Print the path of the settings file |

The window needs `tkinter`, which comes with Python on Windows and macOS.
On Linux it may need `python3-tk`. If it is missing, the app says so and
falls back to the terminal.

## Settings

Settings live in a small JSON file:

* Windows: `%APPDATA%\chocolate-egg-cream\settings.json`
* macOS and Linux: `~/.config/chocolate-egg-cream/settings.json`

The log is written next to it as `eggcream.log`.

| Key | Meaning |
| --- | --- |
| `lastfm_api_key`, `lastfm_api_secret` | From https://www.last.fm/api/account/create |
| `lastfm_session_key` | Filled in by `auth lastfm` |
| `listenbrainz_token` | User token from https://listenbrainz.org/settings/ |
| `enabled_services` | Any of `lastfm` and `listenbrainz` |
| `interval_seconds` | How often to take a sample (default 25) |
| `sample_seconds` | How long each sample is (default 12) |
| `confirmations_required` | Matching results needed to confirm a track (default 2) |
| `min_play_seconds` | How long a track without a known duration must play (default 120) |
| `max_skew` | Reject a match that is badly aligned in time or frequency. 0 turns the check off (default 0) |

### Last.fm

1. Create an API account at https://www.last.fm/api/account/create and put
   the key and secret into the settings.
2. Run `python -m chocolate_egg_cream auth lastfm`, or press **Setup…** in
   the window. The authorisation page opens in your browser; paste back the
   token it shows you.

### ListenBrainz

Create a user token at https://listenbrainz.org/settings/ and put it into the
settings:

```bash
python -m chocolate_egg_cream set listenbrainz_token <your-token>
```

or paste it into **Setup…** in the window.

## How it decides what is playing

* A recognition on its own is never enough. The same song has to come back
  twice before it becomes the current track.
* A failed recognition never ends the current track. Recognition failures are
  normal, so the app keeps the track it has and tries again next time.
* Playtime only grows while audio was actually heard, so a stopped player
  does not make a track look long enough to scrobble.
* When a new track is confirmed, the previous one is scrobbled only if it
  played long enough: half its length (capped at four minutes) when the
  length is known, `min_play_seconds` when it is not. Shazam does not report
  track lengths, so in practice the fallback is used.
* A song is only ever scrobbled once. The list of scrobbled songs is kept in
  the settings file, so repeated Shazam matches never turn into repeated
  scrobbles.

## Platform support

| Platform | Capture method | Status |
| --- | --- | --- |
| Windows | WASAPI loopback (`IAudioClient` with the loopback flag), via `soundcard` | **Tested** |
| macOS | ScreenCaptureKit, via a small Swift helper in `tools/` | **Written, never compiled** |
| Linux | PipeWire/PulseAudio monitor source, via `soundcard` | **Written, not tested** |

### macOS

**This part is written but has never been run.** It was developed on Windows,
where no Swift compiler exists, so the helper has not even been compiled yet.
Expect to fix a compile error or two on the first try.

ScreenCaptureKit only hands out sample buffers that native code can decode,
so the capture lives in `tools/SystemAudioCapture.swift`. Build it once:

```bash
swiftc -O -o tools/SystemAudioCapture tools/SystemAudioCapture.swift
```

The app builds it automatically the first time it runs if `swiftc` is
available. macOS asks for Screen Recording permission the first time the
capture starts, and ScreenCaptureKit needs macOS 13 or newer.

If you do get it working, or hit a problem, an issue on the repository would
be the place to say so.

### Linux

**This part is written but has never been run.** It was developed on Windows,
so it has not been tested against a real PipeWire or PulseAudio server.

PipeWire and PulseAudio both expose a monitor source for the default output
sink, and `soundcard` opens that source. If the monitor source is missing,
the app says so and stops rather than falling back to the microphone.

## What was tested on Windows

Everything below was run on a Windows 11 machine with a Soundwire speaker
device, against live internet radio and local audio files.

* **System audio capture works.** WASAPI loopback returned the audio the
  system was playing, never the microphone. A 440 Hz test tone played through
  the speakers came back with an RMS of 0.42.
* **Recognition works.** Live radio and local files were identified by
  ShazamIO, typically in 0.2 to 0.8 seconds.
* **The full loop works.** Running the app against live radio printed
  `now playing: Klangstein — Querbeat` after the song had been recognised
  twice. The window shows the same thing, and was checked on screen with a
  live recognition showing *State Azure — Rise*.
* **A song change is detected.** Playing Vivaldi and then Beethoven, the app
  confirmed the first track, noticed the second one, and reported both:

  ```
  now playing: Vladimir Spivakov & Moscow Virtuosi — 12 Violin Concertos,
                Op. 8 "Il cimento dell'armonia e dell' invenzione" -
                Concerto No. 1 in E for Solo Violin "La Primavera": I. Allegro
  now playing: Rousseau — Moonlight Sonata, Mvt. 1
  ```

* **The scrobble rule holds.** A track that only played for 15 seconds was
  not scrobbled (`Not scrobbling Rousseau — Moonlight Sonata, Mvt. 1 (played
  15s)`), while one that played long enough was passed to the scrobblers.

Songs identified during testing: *Desert Phase* (Kaya Project), *Gold
Lounge – Birds*, *Klangstein – Querbeat*, *The Four Seasons, Concerto No. 1
"La Primavera": I. Allegro* (Vivaldi), *Moonlight Sonata, Mvt. 1* (Rousseau).

### Limits worth knowing about

* **Shazam's endpoint stalls.** Responses ranged from 0.2 seconds to over
  100 seconds, and after a burst of requests the endpoint stopped answering
  for a while. Requests therefore have a 45 second timeout, and a timeout
  counts as "did not recognise it" rather than as an error.
* **Shazam does not know every song.** Radio stations play plenty of tracks
  that are not in its catalogue; those simply come back as "no match".
* **Quiet recordings can match the wrong song.** A very quiet recording of
  Beethoven's Moonlight Sonata was consistently reported as Debussy's
  *Clair de Lune* (under its Italian title *Nuvole Bianche*), while later
  parts of the same file matched correctly. Confirming a result twice does
  not catch this, because the wrong answer is stable too. The `max_skew`
  setting can reject badly aligned matches, but it was not reliable to
  measure while the endpoint was stalling.
* **Track lengths are unknown.** Shazam does not return a duration, so the
  scrobble rule falls back to `min_play_seconds`.

## Tests

```bash
python tests/test_core.py            # the state machine, no network or audio needed
python tests/test_scrobble_flow.py   # the real loop against a fake Last.fm API
python tests/recognise_source.py <url-or-file>   # play something and see what it says
```

