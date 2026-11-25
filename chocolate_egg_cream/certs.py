"""Point every HTTP stack in the app at one explicit CA certificate bundle.

Why this exists
---------------
Packaged builds (PyInstaller .exe / .app / AppImage) ship their own Python
but **not** the certificate store of the machine that built them:

* ``aiohttp`` (used by ShazamIO) calls ``ssl.create_default_context()``,
  which trusts whatever the bundled OpenSSL was configured with. Inside a
  bundle that path does not exist, so every Shazam request fails with
  ``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate``.
* ``requests`` (used by the scrobblers) trusts ``certifi`` instead, which
  works as long as ``cacert.pem`` is bundled.

The fix is to point the standard knobs (``SSL_CERT_FILE`` for
``ssl.create_default_context()``, ``REQUESTS_CA_BUNDLE`` / ``CURL_CA_BUNDLE``
for requests and curl-style clients) at certifi's ``cacert.pem``.

SSL verification is **never** disabled anywhere in this project: no
``verify=False``, no ``CERT_NONE``, no ``check_hostname = False``.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

#: Environment variables that make Python's ssl module, requests and curl-style
#: clients all use the same CA bundle. Read at call time, so they may be set
#: before any TLS connection is made.
CA_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")


def _frozen_roots() -> list[Path]:
    """Directories where a PyInstaller bundle may keep ``cacert.pem``."""
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    return roots


def find_ca_bundle() -> str | None:
    """Return the path of the CA bundle to use, or None when there is none.

    Order: certifi (works from source and, normally, from a bundle), then the
    usual locations inside a PyInstaller bundle.
    """
    candidates: list[Path] = []
    try:
        import certifi

        candidates.append(Path(certifi.where()))
    except Exception as exc:  # noqa: BLE001 - a broken certifi must not crash us
        log.debug("certifi could not provide a CA bundle: %s", type(exc).__name__)

    for root in _frozen_roots():
        candidates.append(root / "certifi" / "cacert.pem")
        candidates.append(root / "cacert.pem")

    for candidate in candidates:
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return str(candidate)
        except OSError:
            continue
    return None


def _seed_certifi(path: str) -> None:
    """Make ``certifi.where()`` return `path` even when resources are frozen.

    ``certifi.where()`` resolves the file through ``importlib.resources``.
    If that cannot see the bundled data file, requests (which calls
    ``certifi.where()`` itself) would fail, so the resolved path is written
    into certifi's cache directly.
    """
    try:
        import certifi

        current = certifi.where()
        if current and os.path.isfile(current):
            return
    except Exception:  # noqa: BLE001
        pass
    try:
        import certifi.core as core

        if getattr(core, "_CACERT_PATH", None):
            return

        class _FixedResource:
            def __enter__(self) -> str:
                return path

            def __exit__(self, *exc_info: object) -> bool:
                return False

        core._CACERT_CTX = _FixedResource()
        core._CACERT_PATH = path
        log.info("Pinned certifi to the bundled CA bundle at %s", path)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not point certifi at the bundled CA bundle: %s", type(exc).__name__)


def configure_ca_bundle() -> str | None:
    """Configure TLS trust for ssl/aiohttp/requests. Returns the bundle path.

    An environment variable that already points at an existing file is left
    alone so user configuration still wins; anything else is set to the
    bundled bundle. Verification stays fully enabled.
    """
    bundle = find_ca_bundle()
    if bundle is None:
        log.warning(
            "No CA certificate bundle found; HTTPS requests (Shazam, Last.fm, "
            "ListenBrainz) will fail certificate verification."
        )
        return None

    _seed_certifi(bundle)

    for name in CA_ENV_VARS:
        current = os.environ.get(name)
        if current and os.path.isfile(current) and os.path.abspath(current) != os.path.abspath(bundle):
            # An explicit, existing user/system setting wins.
            continue
        if current != bundle:
            os.environ[name] = bundle

    log.debug("Using CA bundle %s", bundle)
    return bundle
