#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h}"
BUNDLE="$ROOT/dist/3D Matrix Code Fullscreen.saver"
CONTENTS="$BUNDLE/Contents"
TARGET="$HOME/Library/Screen Savers/3D Matrix Code Fullscreen.saver"
rm -rf "$BUNDLE"
mkdir -p "$CONTENTS/MacOS"
cp "$ROOT/Info.plist" "$CONTENTS/Info.plist"
xcrun clang -arch x86_64 -fobjc-arc -framework AppKit -framework IOKit -framework ScreenSaver -bundle \
  "$ROOT/Sources/MatrixCodeFullscreenView.m" \
  "$ROOT/Sources/AccentPolicy.c" \
  -o "$CONTENTS/MacOS/3D Matrix Code Fullscreen"
codesign --force --sign - "$BUNDLE"
rm -rf "$TARGET"
cp -R "$BUNDLE" "$TARGET"
codesign --verify --deep --strict "$TARGET"
file "$TARGET/Contents/MacOS/3D Matrix Code Fullscreen"
printf 'Installed: %s\n' "$TARGET"
