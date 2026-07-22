#!/bin/bash
# Agent Board — сборка распространяемого DMG: самодостаточный .app,
# внутри сервер + python-рантайм + собственный tmux (юзеру не нужны ни CLT, ни brew).
#   ./build_dmg.sh                     — неподписанный (для локальной проверки)
#   SIGN_ID="Developer ID Application: ..." ./build_dmg.sh   — подписанный
#   ... NOTARY_PROFILE=agentboard ./build_dmg.sh             — + нотаризация
# Пока только Apple Silicon (aarch64).
set -e
cd "$(dirname "$0")"

PBS_TAG=20260718
PY_VER=3.12.13
PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG/cpython-$PY_VER+$PBS_TAG-aarch64-apple-darwin-install_only_stripped.tar.gz"
TMUX_VER=3.7b
LIBEVENT_VER=2.1.13
UTF8PROC_VER=2.11.3
CACHE=.build-cache
mkdir -p "$CACHE"

# ---------- python-рантайм (кэшируется) ----------
if [ ! -x "$CACHE/python/bin/python3" ]; then
  echo "· python $PY_VER"
  curl -fsSL "$PY_URL" | tar -xzf - -C "$CACHE"   # распакуется в $CACHE/python
fi

# ---------- tmux со статическим libevent (кэшируется) ----------
if [ ! -x "$CACHE/tmux/bin/tmux" ]; then
  echo "· tmux $TMUX_VER (static libevent $LIBEVENT_VER)"
  SRC="$CACHE/src"; DEPS="$(pwd)/$CACHE/deps"
  rm -rf "$SRC" "$DEPS"; mkdir -p "$SRC"
  curl -fsSL "https://github.com/libevent/libevent/releases/download/release-$LIBEVENT_VER-stable/libevent-$LIBEVENT_VER-stable.tar.gz" | tar -xzf - -C "$SRC"
  (cd "$SRC"/libevent-* &&
   ./configure --prefix="$DEPS" --disable-shared --disable-openssl \
               --disable-samples --disable-libevent-regress > /dev/null &&
   make -j"$(sysctl -n hw.ncpu)" > /dev/null && make install > /dev/null)
  curl -fsSL "https://github.com/JuliaStrings/utf8proc/releases/download/v$UTF8PROC_VER/utf8proc-$UTF8PROC_VER.tar.gz" | tar -xzf - -C "$SRC"
  (cd "$SRC"/utf8proc-* &&
   make -j"$(sysctl -n hw.ncpu)" prefix="$DEPS" install > /dev/null)
  rm -f "$DEPS"/lib/*.dylib   # линкуемся только со статикой
  curl -fsSL "https://github.com/tmux/tmux/releases/download/$TMUX_VER/tmux-$TMUX_VER.tar.gz" | tar -xzf - -C "$SRC"
  (cd "$SRC"/tmux-* &&
   PKG_CONFIG_PATH="$DEPS/lib/pkgconfig" ./configure --enable-utf8proc > /dev/null &&
   make -j"$(sysctl -n hw.ncpu)" > /dev/null)
  mkdir -p "$CACHE/tmux/bin"
  cp "$SRC"/tmux-*/tmux "$CACHE/tmux/bin/"
  # бандл-бинарь не должен тянуть ничего, кроме системных /usr/lib
  if otool -L "$CACHE/tmux/bin/tmux" | tail -n +2 | grep -qv "/usr/lib"; then
    echo "! tmux links non-system libraries:"; otool -L "$CACHE/tmux/bin/tmux"; exit 1
  fi
  rm -rf "$SRC" "$DEPS"
fi

# ---------- .app: обёртка + начинка ----------
./build_app.sh
RES=AgentBoard.app/Contents/Resources
mkdir -p "$RES/server"
cp agentboard.py index.html "$RES/server/"
cp -R skins assets "$RES/server/"
cp -R "$CACHE/python" "$RES/python"
# серверу (stdlib, без GUI) не нужны Tcl/Tk и тесты — минус ~10 МБ
rm -rf "$RES/python/lib/python3.12"/{tkinter,idlelib,turtledemo,test} \
       "$RES/python/lib/python3.12/lib-dynload/_tkinter"* \
       "$RES/python/lib"/{itcl,tcl,tk,thread,sqlite,libtcl,libtk}* "$RES/python/lib"/Tk* \
       "$RES/python/share"
find "$RES" -name __pycache__ -type d -prune -exec rm -rf {} \;
cp -R "$CACHE/tmux" "$RES/tmux"

# ---------- подпись ----------
if [ -n "$SIGN_ID" ]; then
  echo "· codesign ($SIGN_ID)"
  # изнутри наружу: каждый Mach-O в бандле, потом весь .app
  find "$RES/python" "$RES/tmux" -type f \( -perm +111 -o -name "*.dylib" -o -name "*.so" \) | while read -r f; do
    file "$f" | grep -q Mach-O || continue
    codesign --force --options runtime --timestamp -s "$SIGN_ID" "$f"
  done
  codesign --force --options runtime --timestamp -s "$SIGN_ID" AgentBoard.app
else
  codesign --force --deep -s - AgentBoard.app 2>/dev/null || true
fi

# ---------- DMG ----------
STAGE="$CACHE/dmg"; rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R AgentBoard.app "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f AgentBoard.dmg
hdiutil create -volname "Agent Board" -srcfolder "$STAGE" -format UDZO -quiet AgentBoard.dmg
rm -rf "$STAGE"

# ---------- нотаризация (нужен профиль: xcrun notarytool store-credentials) ----------
if [ -n "$NOTARY_PROFILE" ]; then
  echo "· notarize"
  xcrun notarytool submit AgentBoard.dmg --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple AgentBoard.dmg
fi

echo "готово: $(pwd)/AgentBoard.dmg ($(du -h AgentBoard.dmg | cut -f1))"
