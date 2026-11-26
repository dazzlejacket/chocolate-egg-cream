"""Generate the Chocolate Egg Cream app icon set for Windows, macOS, and Linux.

Uses only Python standard library + numpy (already a project dependency) to
render crisp supersampled anti-aliased RGBA PNGs, a multi-resolution Windows
.ico, a multi-resolution macOS .icns, Linux hicolor PNGs, and tray/window icons.

Run from the repo root:
    python scripts/generate_icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
ICONS_DIR = ROOT / "packaging" / "icons"
PNG_DIR = ICONS_DIR / "png"
LINUX_DIR = ROOT / "packaging" / "linux"
ASSETS_DIR = ROOT / "chocolate_egg_cream" / "assets"


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def encode_png(rgba: np.ndarray) -> bytes:
    """Encode an (H, W, 4) uint8 numpy array to a PNG byte string."""
    h, w, c = rgba.shape
    assert c == 4
    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter type 0 (None)
        raw.extend(rgba[y].tobytes())
    compressed = zlib.compress(bytes(raw), level=9)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", compressed)
        + _png_chunk(b"IEND", b"")
    )


def _blend(dst: np.ndarray, color: tuple[int, int, int, int], mask: np.ndarray) -> None:
    """Alpha-blend an RGBA color onto dst (float32 0..1) using coverage mask (0..1)."""
    a = np.clip(mask, 0.0, 1.0) * (color[3] / 255.0)
    if not np.any(a > 0):
        return
    src_r, src_g, src_b = color[0] / 255.0, color[1] / 255.0, color[2] / 255.0
    inv = 1.0 - a
    out_a = a + dst[..., 3] * inv
    safe_a = np.where(out_a > 1e-6, out_a, 1.0)
    dst[..., 0] = (src_r * a + dst[..., 0] * dst[..., 3] * inv) / safe_a
    dst[..., 1] = (src_g * a + dst[..., 1] * dst[..., 3] * inv) / safe_a
    dst[..., 2] = (src_b * a + dst[..., 2] * dst[..., 3] * inv) / safe_a
    dst[..., 3] = out_a


def _sdf_rounded_rect(x: np.ndarray, y: np.ndarray, cx: float, cy: float, hw: float, hh: float, r: float) -> np.ndarray:
    dx = np.abs(x - cx) - (hw - r)
    dy = np.abs(y - cy) - (hh - r)
    outside = np.hypot(np.maximum(dx, 0.0), np.maximum(dy, 0.0))
    inside = np.minimum(np.maximum(dx, dy), 0.0)
    return outside + inside - r


def _egg_sdf(x: np.ndarray, y: np.ndarray, cx: float, cy: float, rx: float, ry: float, taper: float = 0.24) -> np.ndarray:
    """Smooth egg signed distance approximation in normalized coordinates."""
    ny = (y - cy) / ry  # -1 at top, +1 at bottom
    # Narrower near top (-1), fuller near bottom (+0.3)
    width_scale = 1.0 + taper * ny - 0.06 * (ny ** 2)
    width_scale = np.maximum(width_scale, 0.35)
    nx = (x - cx) / (rx * width_scale)
    d = np.hypot(nx, ny) - 1.0
    return d * min(rx, ry)


def render_icon(size: int, *, tray: bool = False) -> np.ndarray:
    """Render the original purple egg icon at `size`x`size` with 4x supersampling."""
    ss = 4 if size <= 128 else 2
    n = size * ss
    # Coordinates in [0, 512]
    coords = (np.arange(n, dtype=np.float32) + 0.5) * (512.0 / n)
    xx, yy = np.meshgrid(coords, coords)
    px = 512.0 / n  # 1 subpixel in viewbox units

    canvas = np.zeros((n, n, 4), dtype=np.float32)

    if not tray:
        # Outer dark plum rounded square badge
        d_outer = _sdf_rounded_rect(xx, yy, 256.0, 256.0, 232.0, 232.0, 108.0)
        _blend(canvas, (24, 18, 36, 255), np.clip(0.5 - d_outer / px, 0.0, 1.0))

        # Inner deep plum face
        d_inner = _sdf_rounded_rect(xx, yy, 256.0, 256.0, 220.0, 220.0, 96.0)
        _blend(canvas, (35, 25, 56, 255), np.clip(0.5 - d_inner / px, 0.0, 1.0))

        # Soft ground shadow under egg
        d_shadow = (np.hypot((xx - 256.0) / 96.0, (yy - 416.0) / 18.0) - 1.0) * 18.0
        _blend(canvas, (15, 10, 26, 200), np.clip(0.5 - d_shadow / (px * 2.0), 0.0, 1.0))

    # Egg geometry (slightly larger & bolder for tray icon)
    if tray:
        cx, cy, rx, ry = 256.0, 260.0, 164.0, 212.0
        outline_w = 24.0
    else:
        cx, cy, rx, ry = 256.0, 256.0, 126.0, 162.0
        outline_w = 14.0

    d_egg = _egg_sdf(xx, yy, cx, cy, rx, ry, taper=0.22)

    # Crisp outline around the egg (dark plum for tray so it pops on light & dark taskbars)
    outline_color = (35, 25, 56, 255) if tray else (246, 241, 255, 255)
    outline_mask = np.clip(0.5 - (d_egg - outline_w * 0.5) / px, 0.0, 1.0)
    _blend(canvas, outline_color, outline_mask)

    # Main cozy purple body
    body_mask = np.clip(0.5 - (d_egg + outline_w * 0.5) / px, 0.0, 1.0)
    _blend(canvas, (124, 77, 219, 255), body_mask)

    # Deep violet shading crescent on the right/bottom inside the egg
    d_light_offset = _egg_sdf(xx + 26.0, yy + 18.0, cx, cy, rx * 0.96, ry * 0.96, taper=0.22)
    shade_mask = body_mask * np.clip(0.5 + d_light_offset / (px * 2.0), 0.0, 1.0)
    _blend(canvas, (91, 50, 184, 255), shade_mask)

    # Playful cream-froth wave stripe across lower belly (clipped inside egg)
    wave_center = cy + ry * 0.16 + 14.0 * np.sin((xx - cx) / 34.0)
    band_half = ry * (0.16 if tray else 0.15)
    d_band = np.abs(yy - wave_center) - band_half
    band_mask = body_mask * np.clip(0.5 - d_band / px, 0.0, 1.0)
    _blend(canvas, (246, 241, 255, 255), band_mask)

    # Subtle lavender underside tint on the cream wave
    d_band_low = (yy - (wave_center + band_half * 0.32))
    band_low_mask = band_mask * np.clip(0.5 + d_band_low / px, 0.0, 1.0)
    _blend(canvas, (217, 200, 255, 255), band_low_mask)

    # Tilted glossy highlight oval + playful dot on upper-left shoulder
    angle = np.deg2rad(16.0)
    hx = (xx - (cx - rx * 0.34)) * np.cos(angle) - (yy - (cy - ry * 0.45)) * np.sin(angle)
    hy = (xx - (cx - rx * 0.34)) * np.sin(angle) + (yy - (cy - ry * 0.45)) * np.cos(angle)
    d_hl = (np.hypot(hx / (rx * 0.13), hy / (ry * 0.19)) - 1.0) * (rx * 0.13)
    _blend(canvas, (246, 241, 255, 235), body_mask * np.clip(0.5 - d_hl / px, 0.0, 1.0))

    d_dot = np.hypot(xx - (cx - rx * 0.46), yy - (cy - ry * 0.18)) - (rx * 0.065)
    _blend(canvas, (246, 241, 255, 225), body_mask * np.clip(0.5 - d_dot / px, 0.0, 1.0))

    # Downsample by box filter (ss x ss)
    # Premultiply before box averaging for clean edges
    premul = canvas.copy()
    premul[..., :3] *= premul[..., 3:4]
    premul = premul.reshape(size, ss, size, ss, 4).mean(axis=(1, 3))
    out_a = premul[..., 3:4]
    safe = np.where(out_a > 1e-6, out_a, 1.0)
    out = np.zeros_like(premul)
    out[..., :3] = np.clip(premul[..., :3] / safe, 0.0, 1.0)
    out[..., 3:4] = np.clip(out_a, 0.0, 1.0)
    return (out * 255.0 + 0.5).astype(np.uint8)


def write_ico(path: Path, png_by_size: dict[int, bytes]) -> None:
    """Pack PNG-compressed icon frames into a Windows .ico file."""
    sizes = sorted(png_by_size)
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = []
    blobs = []
    for sz in sizes:
        blob = png_by_size[sz]
        dim = 0 if sz >= 256 else sz
        entries.append(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset))
        blobs.append(blob)
        offset += len(blob)
    path.write_bytes(header + b"".join(entries) + b"".join(blobs))


def write_icns(path: Path, png_by_size: dict[int, bytes]) -> None:
    """Pack PNG-compressed icon frames into a macOS .icns file."""
    type_map = {
        16: b"icp4",
        32: b"icp5",
        64: b"icp6",
        128: b"ic07",
        256: b"ic08",
        512: b"ic09",
        1024: b"ic10",
    }
    chunks = []
    for sz, code in type_map.items():
        if sz in png_by_size:
            blob = png_by_size[sz]
            chunks.append(code + struct.pack(">I", 8 + len(blob)) + blob)
    body = b"".join(chunks)
    path.write_bytes(b"icns" + struct.pack(">I", 8 + len(body)) + body)


def main() -> None:
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    PNG_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    # Copy SVG to packaging/linux as well so AppImage picks up the new icon
    svg_src = ICONS_DIR / "chocolate-egg-cream.svg"
    if svg_src.exists():
        (LINUX_DIR / "chocolate-egg-cream.svg").write_bytes(svg_src.read_bytes())

    all_sizes = (16, 24, 32, 48, 64, 128, 256, 512, 1024)
    png_bytes: dict[int, bytes] = {}
    for sz in all_sizes:
        rgba = render_icon(sz, tray=False)
        data = encode_png(rgba)
        png_bytes[sz] = data
        if sz in (16, 32, 48, 64, 128, 256, 512):
            (PNG_DIR / f"icon-{sz}.png").write_bytes(data)

    # Windows .ico (16..256)
    ico_sizes = {sz: png_bytes[sz] for sz in (16, 24, 32, 48, 64, 128, 256)}
    write_ico(ICONS_DIR / "chocolate-egg-cream.ico", ico_sizes)

    # macOS .icns (16..1024)
    icns_sizes = {sz: png_bytes[sz] for sz in (16, 32, 64, 128, 256, 512, 1024)}
    write_icns(ICONS_DIR / "chocolate-egg-cream.icns", icns_sizes)

    # Simple tray/taskbar icons
    for sz in (16, 32):
        (ICONS_DIR / f"tray-{sz}.png").write_bytes(encode_png(render_icon(sz, tray=True)))

    # Runtime window icons (tiny PNGs bundled with the package for Tk iconphoto)
    (ASSETS_DIR / "icon-32.png").write_bytes(png_bytes[32])
    (ASSETS_DIR / "icon-64.png").write_bytes(png_bytes[64])
    (ASSETS_DIR / "chocolate-egg-cream.ico").write_bytes(
        (ICONS_DIR / "chocolate-egg-cream.ico").read_bytes()
    )

    print("Generated icon set:")
    for p in sorted([*ICONS_DIR.glob("*.*"), *PNG_DIR.glob("*.png"), *ASSETS_DIR.glob("*.*")]):
        print(f"  {p.relative_to(ROOT)} ({p.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
