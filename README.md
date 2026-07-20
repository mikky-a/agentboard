# Agent Board

A spatial board for your AI coding agents (Miro-style): one card = one conversation.
Agents run in tmux underneath; drag cards around, group them by project, see at a
glance who is working, who finished, and who is waiting for your reply.

Supported today: **Claude Code** and **Codex CLI**. On the roadmap: Cursor CLI and opencode.

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
- [Claude Code](https://claude.com/claude-code) and/or [Codex CLI](https://github.com/openai/codex)
- Python 3 (system one is fine, stdlib only — no pip packages)

## Quickstart

```bash
git clone https://github.com/mikky-a/agentboard.git
cd agentboard
python3 agentboard.py        # → http://localhost:8787
```

Open the board in a browser. If status hooks are not installed yet, a banner
appears in the top bar — click it once. It idempotently adds lifecycle hooks to
`~/.claude/settings.json` and `~/.codex/hooks.json` (backing up your originals
as `*.agentboard-bak`) so agents can report their status to the board. Restart
any live agent sessions after installing; Codex will ask to trust the new
hooks — choose "Trust all and continue".

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

- Onboarding: pick which agents to connect (Claude Code / Codex / Cursor / opencode)
- Cursor CLI and opencode support
- English UI
- One-line installer and auto-update

## License

MIT
