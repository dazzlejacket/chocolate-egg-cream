"""Zero-dependency OS credential storage (Windows Credential Manager, macOS Keychain, Linux Secret Service).

Stores scrobbler secrets (Last.fm API secret, Last.fm session key, ListenBrainz
user token) in the operating system's native secret store when available. If the
native store is unavailable (for example, a headless Linux environment without
D-Bus / secret-tool), callers fall back cleanly to restricted local settings.
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger(__name__)

SERVICE_NAME = "ChocolateEggCream"
SECRET_KEYS = (
    "lastfm_api_secret",
    "lastfm_session_key",
    "listenbrainz_token",
)

# Characters that show up when a masked/placeholder value was pasted instead
# of the real secret ("••••••••", "********").
_MASK_CHARS = "*•·●▪∙○◦∙—-#"


def normalize_secret(value: object) -> str:
    """Strip whitespace, newlines and a stray BOM from a pasted secret.

    A trailing newline or a copied BOM used to end up inside an HTTP header
    and made the request fail before it left the machine.
    """
    if value is None:
        return ""
    text = str(value)
    # U+FEFF is the byte order mark: not whitespace, so strip() alone leaves
    # it behind and it then poisons an HTTP header.
    return text.replace("\ufeff", "").strip()


def secret_problem(value: object) -> str | None:
    """Explain why a secret cannot be used, or None when it looks usable.

    The message never contains the value itself, so it is safe to log and to
    show in the GUI. HTTP header values must be Latin-1 encodable; a
    non-ASCII token (a masked placeholder, a corrupted Keychain read, or a
    wrong paste) used to crash inside `http.client.putheader`.
    """
    text = normalize_secret(value)
    if not text:
        return None  # unset, not invalid
    if any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in text):
        return (
            "contains characters that cannot go into an HTTP header "
            "(usually a masked placeholder such as •••• or a corrupted paste)"
        )
    if len(text) >= 4 and len(set(text)) == 1 and text[0] in _MASK_CHARS:
        return "looks like a masked placeholder instead of the real value"
    return None



# ----------------------------------------------------------------- Windows


def _win_target(key: str) -> str:
    return f"{SERVICE_NAME}:{key}"


def _win_get(key: str) -> str | None:
    import ctypes
    from ctypes import wintypes

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.windll.advapi32
    advapi32.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(CREDENTIALW)),
    ]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    advapi32.CredFree.restype = None

    cred_ptr = ctypes.POINTER(CREDENTIALW)()
    ok = advapi32.CredReadW(_win_target(key), 1, 0, ctypes.byref(cred_ptr))
    if not ok:
        return None
    try:
        cred = cred_ptr.contents
        if not cred.CredentialBlob or cred.CredentialBlobSize == 0:
            return ""
        raw = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return raw.decode("utf-8", errors="replace")
    finally:
        advapi32.CredFree(cred_ptr)


def _win_set(key: str, value: str) -> bool:
    import ctypes
    from ctypes import wintypes

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi32 = ctypes.windll.advapi32
    advapi32.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    advapi32.CredWriteW.restype = wintypes.BOOL

    blob = value.encode("utf-8")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = CREDENTIALW()
    cred.Flags = 0
    cred.Type = 1  # CRED_TYPE_GENERIC
    cred.TargetName = _win_target(key)
    cred.UserName = key
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = buf
    cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
    return bool(advapi32.CredWriteW(ctypes.byref(cred), 0))


def _win_delete(key: str) -> bool:
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.windll.advapi32
    advapi32.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi32.CredDeleteW.restype = wintypes.BOOL
    advapi32.CredDeleteW(_win_target(key), 1, 0)
    return True


# ------------------------------------------------------------------- macOS

# macOS `security` prints password data as a hex string whenever it is not
# printable ASCII, which silently corrupts secrets that contain non-ASCII
# characters. Secrets are therefore stored base64-encoded (pure ASCII) behind
# this marker, so the value always round-trips unchanged.
MAC_ENCODING_PREFIX = "b64:"


def _mac_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _mac_decode(raw: str) -> str:
    """Decode a stored macOS secret, returning it unchanged if unmarked."""
    if not raw.startswith(MAC_ENCODING_PREFIX):
        return raw
    payload = raw[len(MAC_ENCODING_PREFIX) :]
    try:
        return base64.b64decode(payload, validate=True).decode("utf-8")
    except ValueError:
        return raw


def _mac_get(key: str) -> str | None:
    result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", SERVICE_NAME, "-a", key, "-w"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        return None
    return _mac_decode(result.stdout.rstrip("\r\n"))


def _mac_set(key: str, value: str) -> bool:
    payload = MAC_ENCODING_PREFIX + base64.b64encode(value.encode("utf-8")).decode("ascii")
    # Use interactive mode (-i) so the secret is sent over stdin and never
    # appears in the process argument list.
    cmd = (
        f"add-generic-password -U -s {_mac_quote(SERVICE_NAME)} "
        f"-a {_mac_quote(key)} -w {_mac_quote(payload)}\n"
    )
    result = subprocess.run(
        ["/usr/bin/security", "-i"],
        input=cmd,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.returncode == 0


def _mac_delete(key: str) -> bool:
    subprocess.run(
        ["/usr/bin/security", "delete-generic-password", "-s", SERVICE_NAME, "-a", key],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return True


# ------------------------------------------------------------------- Linux


def _linux_available() -> bool:
    return bool(shutil.which("secret-tool")) and bool(
        os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    )


def _linux_get(key: str) -> str | None:
    if not _linux_available():
        return None
    result = subprocess.run(
        ["secret-tool", "lookup", "service", SERVICE_NAME, "account", key],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        return None
    return result.stdout.rstrip("\r\n")


def _linux_set(key: str, value: str) -> bool:
    if not _linux_available():
        return False
    result = subprocess.run(
        [
            "secret-tool",
            "store",
            f"--label=Chocolate Egg Cream ({key})",
            "service",
            SERVICE_NAME,
            "account",
            key,
        ],
        input=value,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.returncode == 0


def _linux_delete(key: str) -> bool:
    if not _linux_available():
        return False
    subprocess.run(
        ["secret-tool", "clear", "service", SERVICE_NAME, "account", key],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return True


# -------------------------------------------------------------- public API


def get_secret(key: str) -> str | None:
    """Read a secret from the OS credential store, or None if unavailable/missing."""
    try:
        if sys.platform == "win32":
            return _win_get(key)
        if sys.platform == "darwin":
            return _mac_get(key)
        if sys.platform.startswith("linux"):
            return _linux_get(key)
    except Exception as exc:  # noqa: BLE001
        log.debug("OS credential lookup failed for %s: %s", key, exc.__class__.__name__)
    return None


def set_secret(key: str, value: str) -> bool:
    """Store (or clear, when empty) a secret in the OS credential store.

    Returns True if the OS credential store handled the operation, False if
    the caller should fall back to local settings storage.
    """
    try:
        if not value:
            return delete_secret(key)
        if sys.platform == "win32":
            return _win_set(key, value)
        if sys.platform == "darwin":
            return _mac_set(key, value)
        if sys.platform.startswith("linux"):
            return _linux_set(key, value)
    except Exception as exc:  # noqa: BLE001
        log.debug("OS credential write failed for %s: %s", key, exc.__class__.__name__)
    return False


def delete_secret(key: str) -> bool:
    """Delete a secret from the OS credential store."""
    try:
        if sys.platform == "win32":
            return _win_delete(key)
        if sys.platform == "darwin":
            return _mac_delete(key)
        if sys.platform.startswith("linux"):
            return _linux_delete(key)
    except Exception as exc:  # noqa: BLE001
        log.debug("OS credential delete failed for %s: %s", key, exc.__class__.__name__)
    return False
