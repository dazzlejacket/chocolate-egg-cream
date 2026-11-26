"""Lightweight append-only local listening history, retry queue, and exporters.

Every eligible completed listen is appended as one JSON line to `history.jsonl`
(even when no scrobbling services are configured). Status updates from later
retries are also appended as small `status_update` lines so the history file is
never rewritten in full.

Failed scrobbles are tracked in a small, bounded `pending_scrobbles.json` queue
so they can be retried automatically once connectivity or auth is restored,
while deduplication prevents double-scrobbling.
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from . import settings as settings_module

log = logging.getLogger(__name__)

_HISTORY_LOCK = threading.Lock()
_PENDING_LOCK = threading.Lock()

# Keep the retry queue bounded so an offline machine never grows unboundedly.
MAX_PENDING_SCROBBLES = 500


def history_path() -> Path:
    return settings_module.config_dir() / "history.jsonl"


def pending_path() -> Path:
    return settings_module.config_dir() / "pending_scrobbles.json"


def _restrict_permissions(path: Path) -> None:
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _format_iso(timestamp: int | float) -> str:
    return (
        _dt.datetime.fromtimestamp(float(timestamp), tz=_dt.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _format_local_time(timestamp: int | float) -> str:
    return _dt.datetime.fromtimestamp(float(timestamp)).strftime("%Y-%m-%d %H:%M:%S")


def _format_duration(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return ""
    mins, secs = divmod(int(round(seconds)), 60)
    return f"{mins}:{secs:02d}"


# ------------------------------------------------------------ append-only log


def append_listen(
    *,
    artist: str,
    title: str,
    album: str | None,
    timestamp: int,
    duration: float | None,
    isrc: str | None,
    identity: str,
    services: dict[str, dict[str, Any]],
    listen_id: str | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Append one completed listen to `history.jsonl` without rewriting it."""
    record: dict[str, Any] = {
        "type": "listen",
        "id": listen_id or uuid.uuid4().hex,
        "artist": artist,
        "title": title,
        "album": album,
        "timestamp": int(timestamp),
        "duration": round(float(duration), 2) if duration else None,
        "isrc": isrc or None,
        "identity": identity,
        "services": services,
    }
    target = path or history_path()
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _HISTORY_LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
        _restrict_permissions(target)
    return record


def append_status_update(
    listen_id: str,
    service: str,
    status: str,
    *,
    error: str | None = None,
    at: float | None = None,
    path: Path | None = None,
) -> None:
    """Append a lightweight service-status update without rewriting history."""
    if not listen_id:
        return
    update = {
        "type": "status_update",
        "id": listen_id,
        "service": service,
        "status": status,
        "error": error,
        "at": int(at if at is not None else time.time()),
    }
    target = path or history_path()
    line = json.dumps(update, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _HISTORY_LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
        _restrict_permissions(target)


def load_history(
    path: Path | None = None,
    *,
    newest_first: bool = True,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Read `history.jsonl`, folding any appended status updates in memory."""
    target = path or history_path()
    if not target.exists():
        return []

    records: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}

    with _HISTORY_LOCK:
        try:
            with target.open("r", encoding="utf-8", errors="replace") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    kind = obj.get("type", "listen")
                    if kind == "status_update":
                        lid = obj.get("id")
                        svc = obj.get("service")
                        if isinstance(lid, str) and isinstance(svc, str) and lid in by_id:
                            services = by_id[lid].setdefault("services", {})
                            services[svc] = {
                                "status": obj.get("status", "unknown"),
                                "error": obj.get("error"),
                                "at": obj.get("at"),
                            }
                    else:
                        rec = {
                            "id": str(obj.get("id") or ""),
                            "artist": str(obj.get("artist") or ""),
                            "title": str(obj.get("title") or ""),
                            "album": obj.get("album"),
                            "timestamp": int(obj.get("timestamp") or 0),
                            "duration": obj.get("duration"),
                            "isrc": obj.get("isrc"),
                            "identity": str(obj.get("identity") or ""),
                            "services": dict(obj.get("services") or {}),
                        }
                        records.append(rec)
                        if rec["id"]:
                            by_id[rec["id"]] = rec
        except OSError as exc:
            log.warning("Could not read listening history %s: %s", target, exc)
            return []

    if newest_first:
        records.reverse()
    if limit is not None and limit >= 0:
        return records[:limit]
    return records


def count_history(path: Path | None = None) -> int:
    """Count completed listens in `history.jsonl`."""
    return len(load_history(path=path, newest_first=False))


# ------------------------------------------------------------------ exporters


def _service_status_str(record: dict[str, Any], key: str) -> str:
    services = record.get("services") or {}
    entry = services.get(key)
    if isinstance(entry, dict):
        return str(entry.get("status") or "")
    if isinstance(entry, str):
        return entry
    return ""


def export_jsonl(records: Iterable[dict[str, Any]], dest: Path) -> int:
    """Export resolved listen records as clean JSONL."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with dest.open("w", encoding="utf-8") as handle:
        for rec in records:
            out = {
                "id": rec.get("id", ""),
                "timestamp": int(rec.get("timestamp") or 0),
                "timestamp_iso": _format_iso(rec.get("timestamp") or 0),
                "artist": rec.get("artist", ""),
                "title": rec.get("title", ""),
                "album": rec.get("album"),
                "duration": rec.get("duration"),
                "isrc": rec.get("isrc"),
                "services": rec.get("services") or {},
            }
            handle.write(json.dumps(out, ensure_ascii=False) + "\n")
            count += 1
    return count


def export_csv(records: Iterable[dict[str, Any]], dest: Path) -> int:
    """Export listen records as UTF-8 CSV."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "timestamp_iso",
        "timestamp",
        "artist",
        "title",
        "album",
        "duration_seconds",
        "isrc",
        "lastfm_status",
        "listenbrainz_status",
        "external_status",
    ]
    count = 0
    with dest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for rec in records:
            ts = int(rec.get("timestamp") or 0)
            dur = rec.get("duration")
            writer.writerow(
                {
                    "timestamp_iso": _format_iso(ts),
                    "timestamp": ts,
                    "artist": rec.get("artist", ""),
                    "title": rec.get("title", ""),
                    "album": rec.get("album") or "",
                    "duration_seconds": "" if dur is None else dur,
                    "isrc": rec.get("isrc") or "",
                    "lastfm_status": _service_status_str(rec, "lastfm"),
                    "listenbrainz_status": _service_status_str(rec, "listenbrainz"),
                    "external_status": _service_status_str(rec, "external"),
                }
            )
            count += 1
    return count


def export_txt(records: Iterable[dict[str, Any]], dest: Path) -> int:
    """Export listen records as a clean, readable plain-text log."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with dest.open("w", encoding="utf-8") as handle:
        for rec in records:
            ts = int(rec.get("timestamp") or 0)
            when = _format_local_time(ts)
            artist = rec.get("artist", "")
            title = rec.get("title", "")
            album = rec.get("album")
            dur = _format_duration(rec.get("duration"))
            isrc = rec.get("isrc")

            parts = [f"{when}  {artist} — {title}"]
            if album:
                parts.append(f"[{album}]")
            if dur:
                parts.append(f"({dur})")
            if isrc:
                parts.append(f"ISRC:{isrc}")

            services = rec.get("services") or {}
            svc_bits = []
            for svc_key, label in (
                ("lastfm", "Last.fm"),
                ("listenbrainz", "ListenBrainz"),
                ("external", "External"),
            ):
                st = _service_status_str(rec, svc_key)
                if st:
                    svc_bits.append(f"{label}:{st}")
            if svc_bits:
                parts.append("| " + ", ".join(svc_bits))

            handle.write(" ".join(parts) + "\n")
            count += 1
    return count


def export_history(
    dest: Path,
    fmt: str | None = None,
    *,
    source_path: Path | None = None,
) -> int:
    """Export the local listening history to TXT, CSV, or JSONL."""
    resolved_fmt = (fmt or dest.suffix.lstrip(".")).strip().lower()
    records = load_history(path=source_path, newest_first=False)
    if resolved_fmt == "jsonl":
        return export_jsonl(records, dest)
    if resolved_fmt == "csv":
        return export_csv(records, dest)
    if resolved_fmt == "txt":
        return export_txt(records, dest)
    raise ValueError(f"Unsupported export format {resolved_fmt!r}. Use txt, csv, or jsonl.")


# --------------------------------------------------------- retry queue (JSON)


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    _restrict_permissions(tmp)
    os.replace(tmp, path)
    _restrict_permissions(path)


def load_pending(path: Path | None = None) -> list[dict[str, Any]]:
    """Load failed scrobbles waiting to be retried."""
    target = path or pending_path()
    with _PENDING_LOCK:
        if not target.exists():
            return []
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]


def save_pending(items: list[dict[str, Any]], path: Path | None = None) -> None:
    """Atomically persist the bounded pending-scrobbles queue."""
    target = path or pending_path()
    bounded = items[-MAX_PENDING_SCROBBLES:]
    with _PENDING_LOCK:
        try:
            _atomic_write_json(target, bounded)
        except OSError as exc:
            log.warning("Could not save pending scrobbles: %s", exc)


def enqueue_pending(
    *,
    listen_id: str,
    service: str,
    artist: str,
    title: str,
    album: str | None,
    timestamp: int,
    duration: float | None,
    isrc: str | None,
    identity: str,
    error: str | None = None,
    path: Path | None = None,
) -> bool:
    """Add a failed scrobble to the retry queue if not already queued."""
    items = load_pending(path=path)
    ts = int(timestamp)
    for existing in items:
        if (
            existing.get("service") == service
            and existing.get("identity") == identity
            and int(existing.get("timestamp") or 0) == ts
        ):
            existing["attempts"] = int(existing.get("attempts") or 1) + 1
            existing["last_error"] = error
            existing["updated_at"] = int(time.time())
            save_pending(items, path=path)
            return False

    items.append(
        {
            "listen_id": listen_id,
            "service": service,
            "artist": artist,
            "title": title,
            "album": album,
            "timestamp": ts,
            "duration": round(float(duration), 2) if duration else None,
            "isrc": isrc or None,
            "identity": identity,
            "attempts": 1,
            "last_error": error,
            "queued_at": int(time.time()),
            "updated_at": int(time.time()),
        }
    )
    save_pending(items, path=path)
    return True
