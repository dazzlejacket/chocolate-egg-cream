# Recognition Provider Comparison & Resilience

Chocolate Egg Cream uses **ShazamIO** by default because it is free, requires no API keys, works across Windows/macOS/Linux, and reliably identifies short (10–12 second) system audio samples.

Because ShazamIO relies on an unofficial endpoint that can stall, rate-limit, or break, Chocolate Egg Cream:

1. **Isolates recognition behind a minimal `Recognizer` protocol** (`chocolate_egg_cream/recognize.py`) and `create_recognizer(settings)` factory, so swapping providers touches one file.
2. **Handles outages gracefully** (`chocolate_egg_cream/shazam_client.py`):
   - 45-second per-request timeout.
   - Bounded retries (at most 1 retry on transient network errors; never immediately retries HTTP 429 rate limits).
   - Exponential backoff (`15s -> 30s -> 60s -> 120s -> 300s` max, honoring `Retry-After` on HTTP 429).
   - Automatic recovery on the next successful response.
   - Network failures never trigger fake song changes and never cause duplicate scrobbles.

## Alternative Providers Evaluated

| Provider | Short-Sample (10–12s) Accuracy | Cost & Free Tier | Platform Support | Verdict |
| --- | --- | --- | --- | --- |
| **ShazamIO** *(current default)* | Excellent (Shazam catalog) | Free, no API key | Windows, macOS, Linux | **Default.** Best zero-cost experience; hardened with backoff & retries. |
| **Apple ShazamKit** | Excellent (official Shazam catalog) | Free per request, requires **$99/yr Apple Developer Program** membership & signed entitlements | macOS / iOS / Android SDK only (**no Windows or Linux**) | Not suitable as a cross-platform default. Viable future macOS-only native provider if distributed with an Apple Developer ID. |
| **AudD** | Very good (built for short radio/stream clips) | 300 free requests (no card), then **\$5/month** (~1,000 reqs) | Cross-platform REST API | **Best paid fallback** if ShazamIO permanently stops working; not bundled by default to avoid mandatory paid accounts. |
| **ACRCloud** | Excellent (broadcast-grade, noise-robust) | **No free tier** (14-day trial only; custom commercial pricing, ~\$5–6 per 1,000 reqs) | Cross-platform REST/SDK | Unsuitable for a free personal desktop app due to commercial-only pricing. |
| **AcoustID (Chromaprint)** | **Poor for short snippets** (designed for full files / first 1–2 minutes with known track duration) | Free for non-commercial use | Cross-platform | **Not suitable** for 10–12 second live radio/stream capture. |

## Conclusion

No paid fallback is mandated or bundled. If ShazamIO experiences a temporary outage or rate limit, Chocolate Egg Cream backs off cleanly and recovers automatically. If ShazamIO ever permanently shuts down, the `Recognizer` protocol in `chocolate_egg_cream/recognize.py` allows dropping in an optional AudD provider (user-supplied API token) or a macOS-only ShazamKit bridge without changing capture, track confirmation, UI, or scrobbling.
