#!/usr/bin/env bash
# Build standalone Linux x86_64 AppImage and portable .tar.gz bundle.
# Preserves host PipeWire/PulseAudio system audio capture (no root or mic required).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
  PYTHON="python3"
fi

ARCH="$(uname -m)"
VERSION="$("$PYTHON" -c "import pathlib, re; t = pathlib.Path('chocolate_egg_cream/__init__.py').read_text(); print(re.search(r'__version__\s*=\s*[\"\x27]([^\"\x27]+)', t).group(1))")"
echo "Building Chocolate Egg Cream $VERSION for Linux ($ARCH)..."

rm -rf build dist

BUNDLED_CREDS="chocolate_egg_cream/_bundled_lastfm.py"
cleanup_creds() { [ -f "$BUNDLED_CREDS" ] && rm -f "$BUNDLED_CREDS"; }
trap cleanup_creds EXIT
if [ -n "${CEC_LASTFM_API_KEY:-}" ] && [ -n "${CEC_LASTFM_API_SECRET:-}" ]; then
  "$PYTHON" - "$BUNDLED_CREDS" <<'PY'
import os, pathlib, sys
k = os.environ["CEC_LASTFM_API_KEY"].strip()
s = os.environ["CEC_LASTFM_API_SECRET"].strip()
pathlib.Path(sys.argv[1]).write_text(f"API_KEY = {k!r}\nAPI_SECRET = {s!r}\n", encoding="utf-8")
PY
  echo "Injected build-time Last.fm app credentials (gitignored)."
fi

"$PYTHON" -m PyInstaller --clean --noconfirm packaging/chocolate_egg_cream.spec
cleanup_creds

BUNDLE_DIR="dist/chocolate-egg-cream"
if [ ! -x "$BUNDLE_DIR/chocolate-egg-cream" ]; then
  echo "ERROR: Expected executable not found at $BUNDLE_DIR/chocolate-egg-cream" >&2
  exit 1
fi

# Artifact gate: run the packaged executable's own diagnostics. Proves the
# shipped bundle carries its CA certs, recognition stack and does a real
# TLS handshake through both HTTP stacks (requests + aiohttp/Shazam).
VERIFY_REPORT="dist/verify-report.json"
echo "Running packaged verify on $BUNDLE_DIR/chocolate-egg-cream ..."
if ! "$BUNDLE_DIR/chocolate-egg-cream" verify --report "$VERIFY_REPORT"; then
  echo "ERROR: packaged verify failed" >&2
  [ -f "$VERIFY_REPORT" ] && cat "$VERIFY_REPORT" >&2
  exit 1
fi
if [ ! -f "$VERIFY_REPORT" ]; then
  echo "ERROR: packaged verify wrote no report" >&2
  exit 1
fi
"$PYTHON" - "$VERIFY_REPORT" "$VERSION" <<'PY'
import json, sys
report = json.load(open(sys.argv[1], encoding="utf-8"))
if not report.get("ok"):
    print(json.dumps(report, indent=2)); sys.exit("packaged verify reported failures")
if report.get("version") != sys.argv[2]:
    sys.exit(f"packaged verify reports {report['version']}, expected {sys.argv[2]}")
print(f"Packaged verify: OK ({len(report['checks'])} checks, version {report['version']})")
PY

RELEASE_DIR="dist/release"
mkdir -p "$RELEASE_DIR"

# 1. Portable tar.gz archive (works even on systems without FUSE).
TAR_NAME="ChocolateEggCream-${VERSION}-linux-${ARCH}.tar.gz"
tar -C dist -czf "${RELEASE_DIR}/${TAR_NAME}" chocolate-egg-cream
(
  cd "$RELEASE_DIR"
  sha256sum "$TAR_NAME" > "${TAR_NAME}.sha256"
)

# 2. AppImage (built with appimagetool when available).
APPDIR="dist/ChocolateEggCream.AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib/chocolate-egg-cream"

cp -a "$BUNDLE_DIR/." "$APPDIR/usr/lib/chocolate-egg-cream/"
cp packaging/linux/chocolate-egg-cream.desktop "$APPDIR/chocolate-egg-cream.desktop"
cp packaging/icons/png/icon-256.png "$APPDIR/chocolate-egg-cream.png"
cp packaging/icons/png/icon-256.png "$APPDIR/.DirIcon"
mkdir -p "$APPDIR/usr/share/icons/hicolor/scalable/apps"
cp packaging/linux/chocolate-egg-cream.svg "$APPDIR/usr/share/icons/hicolor/scalable/apps/chocolate-egg-cream.svg"
for sz in 16 32 48 64 128 256 512; do
  if [ -f "packaging/icons/png/icon-${sz}.png" ]; then
    mkdir -p "$APPDIR/usr/share/icons/hicolor/${sz}x${sz}/apps"
    cp "packaging/icons/png/icon-${sz}.png" "$APPDIR/usr/share/icons/hicolor/${sz}x${sz}/apps/chocolate-egg-cream.png"
  fi
done

cat > "$APPDIR/AppRun" <<'EOF'
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "${0}")")"
exec "${HERE}/usr/lib/chocolate-egg-cream/chocolate-egg-cream" "$@"
EOF
chmod +x "$APPDIR/AppRun"

if command -v appimagetool >/dev/null 2>&1; then
  APPIMAGE_NAME="ChocolateEggCream-${VERSION}-linux-${ARCH}.AppImage"
  ARCH="$ARCH" APPIMAGE_EXTRACT_AND_RUN=1 appimagetool "$APPDIR" "${RELEASE_DIR}/${APPIMAGE_NAME}"
  chmod +x "${RELEASE_DIR}/${APPIMAGE_NAME}"
  (
    cd "$RELEASE_DIR"
    sha256sum "$APPIMAGE_NAME" > "${APPIMAGE_NAME}.sha256"
  )
  echo "Created: ${RELEASE_DIR}/${APPIMAGE_NAME} ($(du -h "${RELEASE_DIR}/${APPIMAGE_NAME}" | cut -f1))"

  # Gate the *shipped* AppImage too: it must start and pass its own checks.
  APPIMAGE_VERIFY="dist/verify-appimage.json"
  echo "Running packaged verify on the AppImage ..."
  if ! ARCH="$ARCH" APPIMAGE_EXTRACT_AND_RUN=1 \
      "${RELEASE_DIR}/${APPIMAGE_NAME}" verify --report "$APPIMAGE_VERIFY"; then
    echo "ERROR: AppImage verify failed" >&2
    [ -f "$APPIMAGE_VERIFY" ] && cat "$APPIMAGE_VERIFY" >&2
    exit 1
  fi
else
  echo "Note: appimagetool not installed; skipped .AppImage (portable .tar.gz was created)."
fi

echo "Created: ${RELEASE_DIR}/${TAR_NAME} ($(du -h "${RELEASE_DIR}/${TAR_NAME}" | cut -f1))"
