# Agent Board on a Linux server

Run the board on your own server: Claude Code and Codex work there around the
clock in tmux, you watch and answer them from any browser over HTTPS, and
Telegram tells you when an agent finished or is waiting for you. You can reply
to it right from Telegram.

Tested on Ubuntu 24.04 with Python 3.12 and tmux 3.4. Any Linux with systemd
should do. The board itself is one Python file with no dependencies.

> **Agents on the server run without permission prompts.** There is no
> terminal to click "Yes" in, so the board starts Claude Code with
> `--dangerously-skip-permissions` and Codex with
> `--dangerously-bypass-approvals-and-sandbox`. An agent can do anything your
> Linux user can. Use a separate server or at least a separate user, and do not
> keep anything on it that an agent must not touch.

## 1. Packages and a user

Work as a regular user with sudo, not as root. The examples below call it
`YOUR_USER`.

```bash
sudo apt update
sudo apt install -y git tmux python3 curl
mkdir -p ~/projects            # your repositories live here
```

## 2. Claude Code and Codex

Install both CLIs, then run each once by hand to log in. Logging in works
without a browser on the server: the CLI prints a link, you open it on your
laptop or phone and confirm.

```bash
# Claude Code (installs into ~/.local/bin)
curl -fsSL https://claude.ai/install.sh | bash
claude auth login              # Claude subscription; then run `claude` once and finish the first-run setup

# Codex CLI, see https://github.com/openai/codex for other ways to install
sudo apt install -y nodejs npm && sudo npm install -g @openai/codex
codex login --device-auth      # sign in with ChatGPT; then run `codex` once
```

Check that `which claude codex` finds both. The board finds whatever is
installed, so you can skip one of them.

## 3. The board

```bash
git clone https://github.com/mikky-a/agentboard.git ~/agentboard
cd ~/agentboard

sed "s/YOUR_USER/$USER/g" deploy/agentboard.env.example | sudo tee /etc/agentboard.env >/dev/null
sudo chmod 600 /etc/agentboard.env
sudo nano /etc/agentboard.env  # set the token and the address, see below

sed "s/YOUR_USER/$USER/g" deploy/agentboard.service | sudo tee /etc/systemd/system/agentboard.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now agentboard
journalctl -u agentboard -f    # should print: Agent Board → http://localhost:8787
```

In `/etc/agentboard.env`, set two lines before starting the service:

| Variable | What to put |
| --- | --- |
| `AGENTBOARD_TOKEN` | Password for the web board, for example the output of `openssl rand -hex 24` |
| `AGENTBOARD_URL` | Your board's address, for example `https://board.example.com` |

Leave the Telegram lines empty for now; step 5 fills them.

## 4. HTTPS

Point a domain's DNS A record at the server and open ports 80 and 443, for
example with `sudo ufw allow 80,443/tcp` if you use ufw. Then put Caddy in
front of the board. Caddy gets and renews the certificate by itself.

```bash
sudo apt install -y caddy
sudo cp deploy/Caddyfile.example /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile # put your domain instead of board.example.com
sudo systemctl reload caddy
```

Open `https://<your domain>` and enter the token. The browser stays logged in
for a year.

A banner in the top bar offers to install the status hooks. Click it once.
After that cards turn green while an agent works, amber when it waits for you,
and grey when it is done. The first time Codex starts after this, it asks
whether to trust the new hooks: choose "Trust all and continue".

Start agents with **＋ agent**: pick a folder from `~/projects`, choose Claude or
Codex and type the task. A git URL in the picker clones the repository into
`~/projects` first.

## 5. Telegram

1. In Telegram, open [@BotFather](https://t.me/BotFather), send `/newbot` and
   copy the token. The bot must be new and used by nothing else, because the
   board long-polls it and Telegram allows only one poller per bot.
2. Send your new bot any message, then get your chat id:

   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":[0-9-]*'
   ```

3. Put both into `/etc/agentboard.env` and restart:

   ```bash
   AGENTBOARD_TG_TOKEN=<TOKEN>
   AGENTBOARD_TG_CHAT=<chat id>
   ```

   ```bash
   sudo systemctl restart agentboard
   journalctl -u agentboard -n 5   # tg: слушаю ответы из Telegram ...
   ```

What you get:

- **A notification** when an agent finishes its turn or waits for an answer,
  unless its card is open in a visible board tab or you are attached to its
  terminal. It carries the agent's last reply in full and a link to the card.
  Codex also sends its progress messages.
- **Reply to a notification** and the text goes into that agent's terminal.
- **`@name text`** goes to the session with that name; `/help` lists them.
- **No reply and no `@name`**: the message goes to the session whose
  notification came last. When that is not where your previous message went,
  the bot says where this one went.
- **Photos and files** are saved on the server, and the agent gets the path
  together with the caption.
- **`/enter` `/esc` `/up` `/down` `/tab`** press that key in the agent's
  terminal, for menus that wait for a choice.

## Everyday use

- **Restarting the board never kills agents.** They live in tmux, and the unit
  has `KillMode=process`. Keep that line.
- **Updates:** `cd ~/agentboard && git pull && sudo systemctl restart agentboard`.
  When a new release is out, a badge in the top bar does the same in one click.
- **Watch an agent's terminal live:** `ssh` to the server and run
  `tmux attach -t <name>`. Detach with `Ctrl-b d`; the agent keeps running.
- **Logs:** `journalctl -u agentboard -f`. Every Telegram notification and
  every message routed to an agent is logged with a `tg:` prefix.

## Troubleshooting

- **The service exits with "set AGENTBOARD_TOKEN too":** the board is reachable
  from the network and has no password. Set `AGENTBOARD_TOKEN`.
- **A card stays "working" forever:** the agent was started before the hooks
  were installed, or they are not installed. Click the banner, then stop that
  agent and resume it from its card.
- **`409 Conflict` in the log:** something else polls the same bot. Give the
  board a bot of its own.
- **No notifications:** check that the card is not open in a visible browser
  tab, and look for `tg:` lines in the log.
- **Ubuntu restarts the board every morning:** needrestart does that after
  unattended upgrades. It is harmless because the agents survive. To stop it:

  ```bash
  echo '$nrconf{override_rc}{qr(^agentboard\.service$)} = 0;' | sudo tee /etc/needrestart/conf.d/agentboard.conf
  ```
