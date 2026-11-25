"""Artifact verification: run the *packaged* executable and prove it works.

    Chocolate Egg Cream verify                  # packaged
    python -m chocolate_egg_cream verify        # source

This is what CI and the build scripts run after packaging, so a broken
bundle (missing CA certificates, missing audio helper, missing modules)
fails the build instead of reaching users. It performs real TLS handshakes
through both HTTP stacks the app uses; it never disables verification.

A JSON report is always written, because a windowed Windows executable has
no stdout to print to.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Endpoints that only ever need a successful TLS handshake; any HTTP status
# (even 404) proves the certificate chain validated.
REQUESTS_PROBE_URL = "https://ws.audioscrobbler.com/2.0/"
AIOHTTP_PROBE_URL = "https://amp.shazam.com/"


def _check(checks: list[dict], name: str, ok: bool, detail: str) -> bool:
    checks.append({"name": name, "ok": bool(ok), "detail": detail})
    return bool(ok)


def check_certificates(checks: list[dict], *, configure: bool = True) -> str | None:
    """The bundle must carry its own CA file and point ssl/requests at it."""
    from .certs import CA_ENV_VARS, configure_ca_bundle, find_ca_bundle

    bundle = configure_ca_bundle() if configure else find_ca_bundle()
    if not bundle:
        _check(checks, "ca bundle", False, "cacert.pem not found in this build")
        return None
    size = Path(bundle).stat().st_size
    _check(
        checks,
        "ca bundle",
        size > 50_000,
        f"{bundle} ({size} bytes)",
    )

    import ssl

    paths = ssl.get_default_verify_paths()
    using_bundle = bool(paths.cafile) and os.path.isfile(paths.cafile)
    _check(
        checks,
        "ssl default trust",
        using_bundle,
        (
            f"ssl will load CAs from {paths.cafile}"
            if using_bundle
            else "ssl.create_default_context() has no CA file; HTTPS would fail "
            "with CERTIFICATE_VERIFY_FAILED"
        ),
    )
    for name in CA_ENV_VARS:
        value = os.environ.get(name)
        _check(
            checks,
            f"env {name}",
            bool(value) and os.path.isfile(value),
            value or "not set",
        )
    return bundle


def check_requests_tls(checks: list[dict], timeout: float = 20.0) -> None:
    """The scrobblers' stack must complete a real TLS handshake."""
    import requests

    try:
        response = requests.get(REQUESTS_PROBE_URL, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        _check(
            checks,
            "requests TLS",
            False,
            f"{type(exc).__name__}: {str(exc)[:200]}",
        )
        return
    _check(
        checks,
        "requests TLS",
        True,
        f"{REQUESTS_PROBE_URL} answered HTTP {response.status_code}",
    )


def check_aiohttp_tls(checks: list[dict], timeout: float = 20.0) -> None:
    """Shazam's stack (aiohttp) must complete a real TLS handshake."""
    import asyncio

    import aiohttp

    async def probe() -> str:
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.get(AIOHTTP_PROBE_URL) as response:
                return f"{AIOHTTP_PROBE_URL} answered HTTP {response.status}"

    try:
        detail = asyncio.run(probe())
    except Exception as exc:  # noqa: BLE001
        _check(
            checks,
            "aiohttp TLS (Shazam stack)",
            False,
            f"{type(exc).__name__}: {str(exc)[:200]}",
        )
        return
    _check(checks, "aiohttp TLS (Shazam stack)", True, detail)


def check_recognition_stack(checks: list[dict]) -> None:
    try:
        import numpy  # noqa: F401
        import shazamio  # noqa: F401
        import shazamio_core  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        _check(
            checks,
            "recognition stack",
            False,
            f"{type(exc).__name__}: {str(exc)[:200]}",
        )
        return
    from .recognize import ShazamRecognizer

    recognizer = ShazamRecognizer()
    _check(
        checks,
        "recognition stack",
        True,
        f"shazamio + shazamio_core + numpy import; provider {recognizer.name}",
    )


def check_capture(checks: list[dict]) -> None:
    try:
        from .capture import get_capture

        capture = get_capture()
    except Exception as exc:  # noqa: BLE001
        _check(checks, "audio capture", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        return
    _check(checks, "audio capture", True, type(capture).__name__)


def check_gui(checks: list[dict]) -> None:
    try:
        import tkinter  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        _check(checks, "gui (tkinter)", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        return
    _check(checks, "gui (tkinter)", True, "importable")


def check_macos_helper(checks: list[dict]) -> None:
    if sys.platform != "darwin":
        _check(checks, "macOS audio helper", True, f"not needed on {sys.platform}")
        return
    try:
        from .capture.macos import helper_path

        path = helper_path()
    except Exception as exc:  # noqa: BLE001
        _check(checks, "macOS audio helper", False, f"{type(exc).__name__}: {str(exc)[:160]}")
        return
    _check(checks, "macOS audio helper", path.is_file(), str(path))


def check_settings_writable(checks: list[dict]) -> None:
    from .settings import config_dir

    try:
        directory = config_dir()
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".verify-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:  # noqa: BLE001
        _check(checks, "settings directory writable", False, f"{type(exc).__name__}: {exc}")
        return
    _check(checks, "settings directory writable", True, str(config_dir()))


def run_checks(*, network: bool = True) -> dict[str, Any]:
    """Run every packaged-build check. Never raises."""
    from . import __version__

    checks: list[dict] = []
    result: dict[str, Any] = {
        "version": __version__,
        "platform": sys.platform,
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "checks": checks,
    }

    try:
        check_certificates(checks)
    except Exception as exc:  # noqa: BLE001
        _check(checks, "ca bundle", False, f"{type(exc).__name__}: {str(exc)[:160]}")

    if network:
        check_requests_tls(checks)
        check_aiohttp_tls(checks)
    else:
        _check(checks, "requests TLS", True, "skipped (--skip-network)")
        _check(checks, "aiohttp TLS (Shazam stack)", True, "skipped (--skip-network)")

    check_recognition_stack(checks)
    check_capture(checks)
    check_gui(checks)
    check_macos_helper(checks)
    check_settings_writable(checks)

    result["ok"] = all(item["ok"] for item in checks)
    return result


def default_report_path() -> Path:
    from .settings import config_dir

    return config_dir() / "verify-report.json"


def write_report(result: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")


def format_lines(result: dict[str, Any]) -> list[str]:
    lines = [
        f"Chocolate Egg Cream {result['version']} verify "
        f"({'packaged' if result['frozen'] else 'source'}, {result['platform']})"
    ]
    for item in result["checks"]:
        lines.append(f"  {'ok  ' if item['ok'] else 'FAIL'}  {item['name']}: {item['detail']}")
    lines.append("RESULT: " + ("all checks passed" if result["ok"] else "FAILED"))
    return lines


def main(argv: list[str] | None = None) -> int:
    """Entry point for the `verify` command. Returns a process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    network = "--skip-network" not in args
    report_path: Path | None = None
    if "--report" in args:
        index = args.index("--report")
        if index + 1 >= len(args):
            print("verify: --report needs a path", flush=True)
            return 2
        report_path = Path(args[index + 1])

    result = run_checks(network=network)

    if report_path is None:
        report_path = default_report_path()
    try:
        write_report(result, report_path)
        report_note = str(report_path)
    except OSError as exc:
        report_note = f"could not write the report: {exc}"

    # Windowed builds have no stdout; guard every print.
    if sys.stdout is not None:
        for line in format_lines(result):
            print(line, flush=True)
        print(f"  report: {report_note}", flush=True)

    return 0 if result["ok"] else 1
