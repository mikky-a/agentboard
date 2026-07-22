#!/bin/bash
# Сборка AgentBoard.app: swiftc + иконка + бандл. Запуск: ./build_app.sh
set -e
cd "$(dirname "$0")"

echo "· иконка"
(cd app && swift make_icon.swift)
rm -rf app/AppIcon.iconset && mkdir app/AppIcon.iconset
for sz in 16 32 128 256 512; do
  sips -z $sz $sz app/icon_1024.png --out "app/AppIcon.iconset/icon_${sz}x${sz}.png" > /dev/null
  sips -z $((sz*2)) $((sz*2)) app/icon_1024.png --out "app/AppIcon.iconset/icon_${sz}x${sz}@2x.png" > /dev/null
done
iconutil -c icns app/AppIcon.iconset -o app/AppIcon.icns

echo "· компиляция"
swiftc -O -swift-version 5 -target arm64-apple-macos13.0 app/main.swift -o app/AgentBoard-bin \
  -framework Cocoa -framework WebKit

echo "· бандл"
APP=AgentBoard.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
mv app/AgentBoard-bin "$APP/Contents/MacOS/AgentBoard"
cp app/Info.plist "$APP/Contents/Info.plist"
cp app/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
codesign --force -s - "$APP" 2>/dev/null || true

echo "готово: $(pwd)/$APP"
