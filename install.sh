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

if command -v tmux >/dev/null; then :; else
  echo "! tmux not found — the board needs it: brew install tmux"
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
  <array><string>$PY</string><string>$DIR/agentboard.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/agentboard.log</string>
  <key>StandardErrorPath</key><string>/tmp/agentboard.log</string>
</dict></plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"

sleep 1
echo "Agent Board is running → http://localhost:8787"
echo "Optional native app: cd $DIR && ./build_app.sh"
open "http://localhost:8787" 2>/dev/null || true
