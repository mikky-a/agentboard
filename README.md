# Agent Board

A spatial board for your AI coding agents (Miro-style): one card = one conversation.
Agents run in tmux underneath; drag cards around, group them by project, see at a
glance who is working, who finished, and who is waiting for your reply.

Supported today: **Claude Code**, **Codex CLI**, **Cursor CLI**, and **opencode**.

- **＋ agent** — pick a project folder, type a task → the agent starts as a tile
  and works in the background, no terminal window. When the card blinks yellow,
  it needs you: click to open the live terminal right on the board.
- Cards survive reboots: a live agent becomes "paused" and can be resumed
  (`claude --resume`) with one click.
- Removing a card never deletes the underlying conversation history.
- Three skins: terminal (phosphor glow), macOS glass, and a Soviet retro console.

macOS only for now (uses tmux, AppleScript and a Swift/WKWebView wrapper).
The UI is currently in Russian — English localization is in progress.

## Requirements

- macOS 13+
- `tmux` (`brew install tmux`)
- Any of: [Claude Code](https://claude.com/claude-code), [Codex CLI](https://github.com/openai/codex),
  [Cursor CLI](https://cursor.com/docs/cli), [opencode](https://opencode.ai)
- Python 3 (system one is fine, stdlib only — no pip packages)

## Quickstart

```bash
git clone https://github.com/mikky-a/agentboard.git
cd agentboard
python3 agentboard.py        # → http://localhost:8787
```

Open the board in a browser. If status hooks are not installed yet, a banner
appears in the top bar — click it once. It idempotently adds lifecycle hooks
for every CLI you have installed (backing up your originals as
`*.agentboard-bak`): `~/.claude/settings.json`, `~/.codex/hooks.json`,
`~/.cursor/hooks.json` (plus a `Shell(tee)` allowlist entry — Cursor's CLI
ignores hook permission responses), and an opencode plugin in
`~/.config/opencode/plugins/`. Restart any live agent sessions after
installing; Codex will ask to trust the new hooks — choose "Trust all and
continue". Cursor shows a one-time Workspace Trust prompt the first time it
runs in a new folder.

## Statuses

🟢 working · 🟡 waiting for you (blinks + sound) · ⚪ idle · 🔵 paused

Statuses come from the CLIs' own lifecycle hooks (e.g. the `PermissionRequest`
event) writing to `~/.claude/agent-status/<tmux-session>` — no fragile parsing
of terminal output.

## Native app (optional)

```bash
./build_app.sh   # swiftc + icon → AgentBoard.app, drag it to /Applications
```

A dock icon with a badge showing how many agents are waiting for you.

## Start on login (optional)

Create `~/Library/LaunchAgents/com.agentboard.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.agentboard</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/python3</string><string>/FULL/PATH/TO/agentboard/agentboard.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/agentboard.log</string>
  <key>StandardErrorPath</key><string>/tmp/agentboard.log</string>
</dict></plist>
```

Then `launchctl load ~/Library/LaunchAgents/com.agentboard.plist`.

## Configuration

- `AGENTBOARD_DIRS` — colon-separated list of folders to scan for projects in
  the "＋ agent" picker (default: `~/Documents/dev:~/Documents`). Any other
  folder is always reachable via the native "other folder…" dialog.

## Files

- `agentboard.py` — server (Python stdlib, localhost:8787)
- `index.html` — the whole UI
- `skins/` — macOS and Soviet themes on top of the base terminal one
- `board.json` — your board state (created on first run, not in the repo)
- `app/` + `build_app.sh` — native wrapper (Swift + WKWebView)

## Roadmap

- Onboarding screen: pick which agents to connect
- English UI
- One-line installer and auto-update
- Resume for paused Codex / Cursor / opencode cards (Claude only for now)

## License

MIT
