"""Play a source, capture the system audio and report what Shazam makes of it.

Useful for checking the capture and recognition path by hand:

    python tests/recognise_source.py <url-or-file> [label]

The source is played with ffplay, which is not a dependency of the app
itself; any player that sends audio to the default output device works.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chocolate_egg_cream.capture import get_capture
from chocolate_egg_cream.recognize import ShazamRecognizer


async def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    source = sys.argv[1]
    label = sys.argv[2] if len(sys.argv) > 2 else source

    player = subprocess.Popen(
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "warning", source],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(6)

    try:
        capture = get_capture(sample_rate=48000)
        capture.open()
        samples = await asyncio.to_thread(capture.read, 12)
        capture.close()

        rms = float(np.sqrt(np.mean(np.square(samples))))
        print(f"captured rms={rms:.4f} peak={float(np.max(np.abs(samples))):.4f}")

        recognizer = ShazamRecognizer(segment_seconds=12)
        started = time.time()
        track = await recognizer.recognize(samples, capture.sample_rate)
        took = time.time() - started
        await recognizer.close()

        if track is None:
            print(f"{label}: no match ({took:.2f}s)")
            return 1
        print(f"{label}: {track.label} ({took:.2f}s)")
        return 0
    finally:
        player.terminate()
        try:
            player.wait(timeout=5)
        except subprocess.TimeoutExpired:
            player.kill()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
