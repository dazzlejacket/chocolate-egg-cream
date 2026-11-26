"""Chocolate Egg Cream: identify music playing on the computer and scrobble it."""

__version__ = "1.1.0"

# Point every HTTP stack at one explicit CA bundle before anything opens a
# socket. Packaged builds have no usable system certificate store for the
# bundled interpreter, which is what made Shazam fail with
# CERTIFICATE_VERIFY_FAILED in the DMG. Verification is never disabled.
from .certs import configure_ca_bundle as _configure_ca_bundle

_configure_ca_bundle()

