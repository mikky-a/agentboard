# Agent Board on a Linux server

Run the board on your own server: Claude Code and Codex work there around the
clock in tmux, you watch and answer them from any browser over HTTPS, and
Telegram tells you when an agent finished or is waiting for you. You can reply
to it right from Telegram.

Tested on Ubuntu 24.04 with Python 3.12 and tmux 3.4. Any Linux with systemd
should do. The board itself is one Python file with no dependencies.

> **Agents on the server run without permission prompts.** There is no
> terminal to click "Yes" in, so the board starts Claude Code and Antigravity
> with `--dangerously-skip-permissions` and Codex with
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

Optionally add Google's [Antigravity CLI](https://antigravity.google) (`agy`,
Gemini models with a Google AI Pro or Ultra account). It installs into
`~/.local/bin` without sudo. Run it once by hand: it prints a Google sign-in link,
you open it on your laptop and paste the code back, then pick a color scheme and
accept the terms.

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash
agy                            # sign in, finish the first-run setup, quit with Ctrl-D twice
```

Check that `which claude codex` finds both (and `which agy`, if you installed it).
The board finds whatever is installed, so you can skip any of them. The board
starts `agy` with its background self-update turned off; update it with
`agy update`.

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
2. Still in @BotFather, send `/setprivacy`, pick the bot and choose
   **Disable**. Otherwise the bot sees only commands in group chats, not
   messages like `@name text`. You can skip this if you will use only the
   private chat.
3. Send your new bot any message in a private chat, then get your chat id. It
   is also your user id, so in group chats the bot recognizes you by it:

   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":[0-9-]*'
   ```

4. Put both into `/etc/agentboard.env` and restart:

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
  notification came last in this chat. When that is not where your previous
  message went, the bot says where this one went.
- **Group chats** split the stream of messages. Create a group with yourself
  and the bot (or add the bot to an existing one) and call an agent there by
  `@name`. From then on that agent answers in the group, until you write to it
  from another chat.
- **Other people in a group** can talk to the agents too. Add them to the group
  as usual in Telegram. While the group has anyone besides you and the bot,
  agents answer only when called: `@name`, `@<bot username>`, or a reply to an
  agent's message. Other messages do not wake anyone, but the agent gets them
  as context the next time it is called there, so it knows what was discussed.
  Other members talk only to the agents that live in this group. Starting new
  agents, moving agents between chats and slash commands such as `/model` stay
  yours. The agent sees who wrote each message and that it is not you. The
  bot listens to other people only in groups where you are a member, and their
  private messages to the bot are ignored. Keep in mind that whoever you add
  can make an agent run commands on the server, because agents there work
  without permission prompts.
- **`@project new`** starts a new Claude agent in that project, right in this
  chat: it answers here, and messages here without `@name` go to it (in a group
  with other people, call it by name).
  `@project new codex <task>` starts Codex and gives it the task at once,
  `@project new agy <task>` starts Antigravity. A model goes right after the
  engine, by its full id or a short name: `@project new agy hydrogen-xhigh <task>`,
  `@project new codex gpt-5.6-sol <task>`. Without one the agent gets the
  default model.
- **`@name /model <model>`** switches a running agent's model. For Antigravity
  short names work here too (`/model flash-low`), and the bot replies with the
  model it picked. agy would save it as the default for new sessions, so the
  board puts your default back.
- **Photos and files** are saved on the server, and the agent gets the path
  together with the caption.
- **Voice notes and video circles** reach the agent as text, as if you had
  typed it: Claude and Codex cannot hear audio. The board transcribes them with
  Whisper on Groq (about a second per message) and adds the path to the
  recording. It needs a Groq API key: put `{"groq_api_key": "gsk_…"}` into
  `~/.agentboard/groq.json` (`chmod 600`), or set `GROQ_API_KEY` in the
  service env. The file is read on every message, so no restart is needed.
  Without a key, the agent gets only the file.
- **`/enter` `/esc` `/up` `/down` `/tab`** press that key in the agent's
  terminal, for menus that wait for a choice.
- **Menus are shown, not typed into.** A command such as `/status` or `/model`
  opens a menu in Claude Code. The bot sends you its screen. While a menu is
  open, the bot does not type your messages into it, because they would be lost
  there. `/esc` closes the menu.

## Agents talking to each other

Agents on the board can message each other with the `board` command. The board
puts it into `~/.local/bin` when it starts and describes it in the agents'
memory next to the status hooks, so new sessions know it. To tell an agent that
is already running, write to it once: "there is a `board` command, see
`board --help`".

```bash
board ls                                   # who is running, with status and model
board send scandalio "question" --wait     # ask, wait, print the reply
board read scandalio                       # its last reply
```

The message goes in signed with the sender's session, so the receiving agent
knows another agent wrote it and not you. With `--wait` the reply comes back as
the output of the sender's command, and the receiver does not need to send it
back. You still get the usual Telegram notification for the reply.

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
