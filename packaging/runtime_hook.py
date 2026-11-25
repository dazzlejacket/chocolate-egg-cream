"""PyInstaller runtime hook: configure TLS trust before any request is made.

Runs before the application's own modules are imported, so `ssl`,
`aiohttp` and `requests` all see the bundled CA bundle from their very
first connection. SSL verification stays fully enabled; nothing here
disables certificate checks.

Kept self-contained on purpose: if the app package cannot be imported yet
from the hook, the same lookup is repeated inline.
"""

import os
import sys


def _find_ca_bundle():
    try:
        from chocolate_egg_cream.certs import find_ca_bundle

        bundle = find_ca_bundle()
        if bundle:
            return bundle
    except Exception:
        pass

    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(meipass)
    roots.append(os.path.dirname(sys.executable))
    for root in roots:
        for candidate in (
            os.path.join(root, "certifi", "cacert.pem"),
            os.path.join(root, "cacert.pem"),
        ):
            if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
                return candidate
    return None


def _configure() -> None:
    try:
        from chocolate_egg_cream.certs import configure_ca_bundle

        configure_ca_bundle()
        return
    except Exception:
        pass

    bundle = _find_ca_bundle()
    if not bundle:
        return
    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        current = os.environ.get(name)
        if current and os.path.isfile(current):
            continue
        os.environ[name] = bundle


_configure()
