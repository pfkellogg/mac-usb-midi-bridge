#!/bin/bash
# Builds "MIDI Bridge.app" (a small bundle that runs midi_bridge_mac.py on the native
# Apple-silicon Python, which has mido + python-rtmidi) and installs it in /Applications.
#     ./make_app.sh
set -euo pipefail
cd "$(dirname "$0")"
PY="${PY:-$HOME/miniforge3/bin/python3}"
"$PY" -c "import mido, rtmidi, tkinter" || { echo "error: $PY needs mido, python-rtmidi and tkinter" >&2; exit 1; }
VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' midi_bridge_mac.py)
APP="dist/MIDI Bridge.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp midi_bridge_mac.py "$APP/Contents/Resources/"
cat > "$APP/Contents/MacOS/MIDI Bridge" <<SH
#!/bin/bash
exec "$PY" "\$(dirname "\$0")/../Resources/midi_bridge_mac.py"
SH
chmod +x "$APP/Contents/MacOS/MIDI Bridge"
cat > "$APP/Contents/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>MIDI Bridge</string>
  <key>CFBundleDisplayName</key><string>MIDI Bridge</string>
  <key>CFBundleExecutable</key><string>MIDI Bridge</string>
  <key>CFBundleIdentifier</key><string>com.pfkellogg.midibridge.mac</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSMultipleInstancesProhibited</key><true/>
</dict></plist>
PL
rm -rf "/Applications/MIDI Bridge.app"
cp -R "$APP" /Applications/
echo "installed /Applications/MIDI Bridge.app (v$VERSION, $("$PY" -c 'import platform; print(platform.machine())'))"
