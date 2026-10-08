#!/bin/sh
# Builds the Mac version of AstraNova:
#   release/AstraNova-macOS.zip   the app, zipped (what the updater downloads)
#   release/AstraNova-macOS.dmg   the usual Mac download: open it, drag AstraNova into Applications
# Needs a Mac with Python 3.12 (python.org or Homebrew). GitHub runs this on every release.
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3.12}
command -v "$PY" >/dev/null 2>&1 || PY=python3

echo "[1/4] Python environment"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r requirements.txt pyinstaller

echo "[2/4] Building AstraNova.app"
rm -rf build dist
.venv/bin/python -m PyInstaller --noconfirm --clean AstraNova.spec

if [ -n "$MAC_SIGN_ID" ]; then
  echo "Signing as $MAC_SIGN_ID"
  codesign --force --deep --options runtime --timestamp --entitlements installer/entitlements.plist \
    --sign "$MAC_SIGN_ID" dist/AstraNova.app
  codesign --verify --deep --strict dist/AstraNova.app
fi

echo "[3/4] Self-test"
rm -rf /tmp/astranova-selftest && cp -R dist/AstraNova.app /tmp/astranova-selftest.app
/tmp/astranova-selftest.app/Contents/MacOS/AstraNova --selftest || {
  cat /tmp/astranova-selftest.app/Contents/MacOS/selftest.txt 2>/dev/null; echo "Self-test failed"; exit 1; }
cat /tmp/astranova-selftest.app/Contents/MacOS/selftest.txt; echo
rm -rf /tmp/astranova-selftest.app

echo "[4/4] Packing"
mkdir -p release
rm -f release/AstraNova-macOS.zip release/AstraNova-macOS.dmg
ditto -c -k --keepParent dist/AstraNova.app release/AstraNova-macOS.zip
if [ -n "$MAC_SIGN_ID" ] && [ -n "$APPLE_ID" ] && [ -n "$APPLE_APP_PASSWORD" ] && [ -n "$APPLE_TEAM_ID" ]; then
  echo "Notarizing with Apple (a few minutes)"
  xcrun notarytool submit release/AstraNova-macOS.zip --apple-id "$APPLE_ID" --password "$APPLE_APP_PASSWORD" \
    --team-id "$APPLE_TEAM_ID" --wait
  xcrun stapler staple dist/AstraNova.app
  rm -f release/AstraNova-macOS.zip && ditto -c -k --keepParent dist/AstraNova.app release/AstraNova-macOS.zip
fi
rm -rf build/dmg && mkdir -p build/dmg && cp -R dist/AstraNova.app build/dmg/ && ln -s /Applications build/dmg/Applications
hdiutil create -volname AstraNova -srcfolder build/dmg -ov -format UDZO release/AstraNova-macOS.dmg >/dev/null
echo "Done: release/AstraNova-macOS.dmg and release/AstraNova-macOS.zip"
