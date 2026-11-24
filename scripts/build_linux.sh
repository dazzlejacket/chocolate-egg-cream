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
"$PYTHON" -m PyInstaller --clean --noconfirm packaging/chocolate_egg_cream.spec

BUNDLE_DIR="dist/chocolate-egg-cream"
if [ ! -x "$BUNDLE_DIR/chocolate-egg-cream" ]; then
  echo "ERROR: Expected executable not found at $BUNDLE_DIR/chocolate-egg-cream" >&2
  exit 1
fi

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
cp packaging/linux/chocolate-egg-cream.svg "$APPDIR/chocolate-egg-cream.svg"

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
else
  echo "Note: appimagetool not installed; skipped .AppImage (portable .tar.gz was created)."
fi

echo "Created: ${RELEASE_DIR}/${TAR_NAME} ($(du -h "${RELEASE_DIR}/${TAR_NAME}" | cut -f1))"
