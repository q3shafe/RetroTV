#!/usr/bin/env bash
# Build the RetroTV Linux binary and .deb installer.
#
# Run on Linux (or inside WSL Ubuntu). PyInstaller can't cross-compile, so this
# can't be run from Windows directly. From Windows PowerShell:
#   wsl -d Ubuntu -u root -- bash /mnt/j/py-git/shafe-tv/linux-build/build-linux.sh
#
# Build on the OLDEST distro you want to support (glibc compatibility).
# Output (repo root):
#   Output/retrotv-linux-x86_64
#   Output/retrotv_<version>_amd64.deb
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
WORK="${RTV_WORK:-$HOME/rtv-build}"   # build on the native Linux fs, not /mnt/*
OUT="$REPO/Output"

# ---- system dependencies (apt) ------------------------------------------------
# WSL has no IPv6 route; ForceIPv4 avoids failed mirror fetches there.
need=(python3 python3-venv python3-pip python3-tk libvlc5 vlc-plugin-base vlc-plugin-video-output binutils)
missing=()
for p in "${need[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
if [ ${#missing[@]} -gt 0 ]; then
  echo ">> Installing: ${missing[*]}"
  SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  $SUDO apt-get -o Acquire::ForceIPv4=true update -qq
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get -o Acquire::ForceIPv4=true install -y -qq "${missing[@]}"
fi

# ---- version from tv.py -------------------------------------------------------
VERSION="$(sed -n 's/^APP_VERSION *= *"\(.*\)"/\1/p' "$REPO/tv.py" | head -1)"
[ -n "$VERSION" ] || { echo "Could not read APP_VERSION from tv.py" >&2; exit 1; }
echo ">> Building RetroTV $VERSION"

# ---- stage sources ------------------------------------------------------------
rm -rf "$WORK/src" "$WORK/dist" "$WORK/build"
mkdir -p "$WORK/src"
cp "$REPO"/{tv.py,remote_control.py,retrotv_epg.py,dvr_scheduler.py,retrotv.png} "$HERE/retrotv-linux.spec" "$WORK/src/"
python3 -m py_compile "$WORK/src/tv.py" "$WORK/src/remote_control.py" "$WORK/src/retrotv_epg.py" "$WORK/src/dvr_scheduler.py"

# ---- venv + PyInstaller -------------------------------------------------------
[ -d "$WORK/venv" ] || python3 -m venv "$WORK/venv"
# shellcheck disable=SC1091
. "$WORK/venv/bin/activate"
pip install -q --upgrade pyinstaller python-vlc qrcode
python -c "import tkinter" || { echo "tkinter missing (apt install python3-tk)" >&2; exit 1; }

cd "$WORK/src"
pyinstaller retrotv-linux.spec --noconfirm --clean --distpath "$WORK/dist" --workpath "$WORK/build"
if grep -qi "missing module named tkinter" "$WORK"/build/retrotv-linux/warn-*.txt; then
  echo "tkinter was dropped from the bundle" >&2; exit 1
fi

# ---- .deb ---------------------------------------------------------------------
PKG="retrotv_${VERSION}_amd64"
ROOT="$WORK/$PKG"
rm -rf "$ROOT"
mkdir -p "$ROOT"/{DEBIAN,opt/retrotv,usr/bin,usr/share/applications,usr/share/icons/hicolor/256x256/apps}
install -m 755 "$WORK/dist/retrotv" "$ROOT/opt/retrotv/retrotv"
install -m 644 "$WORK/src/retrotv.png" "$ROOT/usr/share/icons/hicolor/256x256/apps/retrotv.png"
ln -s /opt/retrotv/retrotv "$ROOT/usr/bin/retrotv"
cat > "$ROOT/usr/share/applications/retrotv.desktop" <<EOF
[Desktop Entry]
Name=RetroTV
Comment=Retro-style TV channel player
Exec=/opt/retrotv/retrotv
Icon=retrotv
Terminal=false
Type=Application
Categories=AudioVideo;Video;Player;
EOF
cat > "$ROOT/DEBIAN/control" <<EOF
Package: retrotv
Version: $VERSION
Section: video
Priority: optional
Architecture: amd64
Depends: libvlc5, vlc-plugin-base, vlc-plugin-video-output, python3-tk
Maintainer: N5 Networks <support@n5net.com>
Description: RetroTV - retro-style TV channel player
 Plays playlists as virtual TV channels with an EPG guide.
EOF
dpkg-deb --build --root-owner-group "$ROOT" "$WORK/$PKG.deb" >/dev/null

# ---- publish ------------------------------------------------------------------
mkdir -p "$OUT"
cp "$WORK/dist/retrotv" "$OUT/retrotv-linux-x86_64"
cp "$WORK/$PKG.deb" "$OUT/"
echo ">> Done:"
ls -lh "$OUT/retrotv-linux-x86_64" "$OUT/$PKG.deb"
