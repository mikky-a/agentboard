#!/bin/sh
# Agent Board — установка одной командой (macOS):
#   curl -fsSL https://raw.githubusercontent.com/mikky-a/agentboard/main/install.sh | sh
# Кладёт код в ~/.agentboard, ставит автозапуск (launchd) и открывает доску.
set -e

DIR="${AGENTBOARD_DIR:-$HOME/.agentboard}"
REPO="https://github.com/mikky-a/agentboard.git"
PLIST="$HOME/Library/LaunchAgents/com.agentboard.plist"

[ "$(uname)" = "Darwin" ] || { echo "Agent Board is macOS-only for now"; exit 1; }
command -v git >/dev/null || { echo "git is required (xcode-select --install)"; exit 1; }

PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "python3 is required"; exit 1; }

# tmux обязателен — агентам не в чем жить; ставим сами, при нужде вместе с Homebrew
if ! command -v tmux >/dev/null; then
  BREW="$(command -v brew || true)"
  [ -z "$BREW" ] && [ -x /opt/homebrew/bin/brew ] && BREW=/opt/homebrew/bin/brew
  [ -z "$BREW" ] && [ -x /usr/local/bin/brew ] && BREW=/usr/local/bin/brew
  if [ -z "$BREW" ]; then
    echo "Homebrew not found — installing it (needed for tmux; it may ask for your password) ..."
    NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || true
    [ -x /opt/homebrew/bin/brew ] && BREW=/opt/homebrew/bin/brew
    [ -z "$BREW" ] && [ -x /usr/local/bin/brew ] && BREW=/usr/local/bin/brew
    [ -n "$BREW" ] || { echo "! Homebrew install failed — install tmux yourself, then re-run this script"; exit 1; }
    # свежий brew в PATH новых терминалов — как советует его же инсталлер
    grep -qs 'brew shellenv' "$HOME/.zprofile" || echo "eval \"\$($BREW shellenv)\"" >> "$HOME/.zprofile"
  fi
  echo "Installing tmux ..."
  "$BREW" install tmux || { echo "! tmux install failed — run: $BREW install tmux, then re-run this script"; exit 1; }
fi

if [ -d "$DIR/.git" ]; then
  echo "Updating $DIR ..."
  git -C "$DIR" pull --ff-only
else
  echo "Cloning into $DIR ..."
  git clone --depth 1 "$REPO" "$DIR"
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.agentboard</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>-u</string><string>$DIR/agentboard.py</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/agentboard.log</string>
  <key>StandardErrorPath</key><string>/tmp/agentboard.log</string>
</dict></plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

# ждём, пока сервер реально ответит (sleep 1 не хватало)
printf "Waiting for the server"
i=0
while ! curl -sf -o /dev/null --max-time 1 "http://localhost:8787/api/agents"; do
  i=$((i+1))
  if [ "$i" -ge 30 ]; then break; fi
  printf "."
  sleep 0.5
done
echo ""
if ! curl -sf -o /dev/null --max-time 1 "http://localhost:8787/api/agents"; then
  echo "! the server did not come up; log tail:"
  tail -20 /tmp/agentboard.log 2>/dev/null || true
  echo "  If the log is empty, macOS may have blocked the login item:"
  echo "  System Settings → General → Login Items → allow Agent Board,"
  echo "  or run by hand: $PY $DIR/agentboard.py"
  echo "  (the app will also try to start the server itself)"
fi

# Главный сценарий — нативное окно с бейджем в доке; браузер — запасной путь.
if command -v swiftc >/dev/null; then
  echo "Building AgentBoard.app ..."
  (cd "$DIR" && ./build_app.sh > /dev/null)
  rm -rf "$HOME/Applications/AgentBoard.app"
  mkdir -p "$HOME/Applications"
  cp -R "$DIR/AgentBoard.app" "$HOME/Applications/AgentBoard.app"
  echo "Agent Board installed → ~/Applications/AgentBoard.app (drag to the Dock!)"
  open "$HOME/Applications/AgentBoard.app"
else
  echo "! swiftc not found — skipping the native app (install Xcode Command"
  echo "  Line Tools: xcode-select --install, then re-run this script)."
  echo "Agent Board is running → http://localhost:8787"
  open "http://localhost:8787" 2>/dev/null || true
fi
