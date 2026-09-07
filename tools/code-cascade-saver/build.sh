#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h}"
BUNDLE="$ROOT/dist/Code Cascade.saver"
CONTENTS="$BUNDLE/Contents"

rm -rf "$BUNDLE"
mkdir -p "$CONTENTS/MacOS"
cp "$ROOT/Info.plist" "$CONTENTS/Info.plist"

xcrun clang \
  -arch arm64 \
  -fobjc-arc \
  -framework AppKit \
  -framework ScreenSaver \
  -bundle \
  "$ROOT/Sources/CodeCascadeSaverView.m" \
  "$ROOT/Sources/CascadeMath.c" \
  -o "$CONTENTS/MacOS/Code Cascade"

codesign --force --sign - "$BUNDLE"
plutil -lint "$CONTENTS/Info.plist"
file "$CONTENTS/MacOS/Code Cascade"

target="$HOME/Library/Screen Savers/Code Cascade.saver"
mkdir -p "$HOME/Library/Screen Savers"
rm -rf "$target"
cp -R "$BUNDLE" "$target"
codesign --verify --deep --strict "$target"
printf 'Installed: %s\n' "$target"
