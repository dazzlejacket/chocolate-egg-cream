"""One running instance per user.

Two instances at once would both recognise the same music and both scrobble
it, and their interleaved writes could corrupt `settings.json`. A small lock
file in the config directory carries the owner's PID; a live owner refuses a
second start, a dead owner (crash, kill, upgrade) is taken over.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from . import settings as settings_module

log = logging.getLogger(__name__)

LOCK_NAME = "instance.lock"
_WRITE_RETRIES = 5


class AlreadyRunning(RuntimeError):
    """Another live instance of the app holds the lock."""

    def __init__(self, pid: int) -> None:
        super().__init__(
            f"Chocolate Egg Cream is already running (process {pid}). "
            "Quit that instance before starting another one."
        )
        self.pid = pid


def lock_file() -> Path:
    return settings_module.config_dir() / LOCK_NAME


def _read_lock(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _pid_alive(pid: int) -> bool:
    """Best-effort 'is this process still running' check."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

        # PROCESS_QUERY_LIMITED_INFORMATION
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            # Cannot open it: assume it is alive so we never run twice. A
            # stale lock from a deleted user is the only way this sticks.
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER
        try:
            code = ctypes.c_uint32(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def acquire() -> Path:
    """Take the instance lock, or raise AlreadyRunning if a live one exists."""
    path = lock_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"pid": os.getpid(), "at": int(time.time()), "app": "chocolate-egg-cream"}
    )

    for _ in range(_WRITE_RETRIES):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = _read_lock(path)
            if existing is None:
                # Another instance may be mid-write; give it a moment before
                # declaring the file stale.
                time.sleep(0.2)
                existing = _read_lock(path)
            pid = existing.get("pid") if existing else None
            if isinstance(pid, int) and pid != os.getpid() and _pid_alive(pid):
                raise AlreadyRunning(pid)
            log.info("Taking over a stale instance lock (pid %r)", pid)
            try:
                path.unlink()
            except OSError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        log.debug("Instance lock taken: %s", path)
        return path

    raise RuntimeError(f"Could not create the instance lock at {path}")


def release() -> None:
    """Drop the lock, but only if we still own it."""
    path = lock_file()
    existing = _read_lock(path)
    if existing is None:
        return
    if existing.get("pid") != os.getpid():
        return
    try:
        path.unlink()
        log.debug("Instance lock released: %s", path)
    except OSError as exc:
        log.debug("Could not remove the instance lock: %s", type(exc).__name__)


def current_holder() -> int | None:
    """PID of the instance that holds the lock, if any."""
    existing = _read_lock(lock_file())
    if not existing:
        return None
    pid = existing.get("pid")
    return pid if isinstance(pid, int) and _pid_alive(pid) else None
