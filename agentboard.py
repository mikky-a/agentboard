#!/usr/bin/env python3
"""Agent Board — пространственная доска агентов Claude Code поверх tmux.

Карточка = один разговор (агент). Всё, что на доске, автоматически
сохраняется в board.json и переживает перезагрузку: живая карточка после
ребута становится «на паузе», кнопка «продолжить» возобновляет разговор
(claude --resume). «Убрать с доски» снимает карточку, не трогая историю
Claude Code — вернуть можно через «+» из истории.

Запуск: python3 agentboard.py → http://localhost:8787
"""
import glob
import hashlib
import hmac
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlparse

__version__ = "0.2.2"

PORT = int(os.environ.get("AGENTBOARD_PORT", "8787"))
# доска как узел в облаке: задан токен — /api отвечает только по нему
# (cookie после входа в браузере или Bearer от другой доски)
HOST = os.environ.get("AGENTBOARD_HOST", "127.0.0.1")
TOKEN = os.environ.get("AGENTBOARD_TOKEN", "")
TOKEN_HASH = hashlib.sha256(TOKEN.encode()).hexdigest()  # в cookie лежит он, не сам токен
# на сервере нет Terminal и Finder: «открыть» там — диалог прямо в браузере
HEADLESS = sys.platform != "darwin" or bool(os.environ.get("AGENTBOARD_HEADLESS"))
# бандл-версия (.app) приносит свой tmux и живёт на своём сокете,
# чтобы не пересекаться с юзерским tmux-сервером (protocol version mismatch)
TMUX = os.environ.get("AGENTBOARD_TMUX") or shutil.which("tmux") or "/opt/homebrew/bin/tmux"
TMUX_SOCKET = os.environ.get("AGENTBOARD_TMUX_SOCKET", "")
TMUX_CMD = [TMUX] + (["-L", TMUX_SOCKET] if TMUX_SOCKET else [])
# панели tmux наследуют env сервера, а не шелла — задаём PATH явно,
# иначе внутри агентов не находится `claude` и падает его автообновление
AGENT_PATH = ":".join([
    os.path.expanduser("~/.local/bin"),
    os.path.expanduser("~/.opencode/bin"),
    "/opt/homebrew/bin", "/usr/local/bin",
    "/usr/bin", "/bin", "/usr/sbin", "/sbin",
])
CLAUDE = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
CODEX = shutil.which("codex") or "/opt/homebrew/bin/codex"
CURSOR = shutil.which("cursor-agent") or os.path.expanduser("~/.local/bin/cursor-agent")
OPENCODE = shutil.which("opencode") or os.path.expanduser("~/.opencode/bin/opencode")
STATUS_DIR = os.path.expanduser("~/.claude/agent-status")
NAMES_DIR = f"/tmp/agentboard-{os.getuid()}-names"  # сюда агент первой командой пишет имя карточки
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
HERE = os.path.dirname(os.path.abspath(__file__))
# в бандле HERE — read-only внутри .app; данные живут отдельно (AGENTBOARD_DATA)
DATA_DIR = os.environ.get("AGENTBOARD_DATA") or HERE
os.makedirs(DATA_DIR, exist_ok=True)
BOARD_FILE = os.path.join(DATA_DIR, "board.json")

# служебный хром TUI Claude Code — не показываем в превью карточки
CHROME_SNIPPETS = ("⏵⏵", "shift+tab", "esc to interrupt", "/rc active",
                   "← for agents", "auto mode", "plan mode", "bypass permissions",
                   "tokens left", "Context left", "Jump to bottom",
                   "ctrl+o for transcript", "Auto-update failed")
FRAME_CHARS = set("─│╭╮╰╯┌┐└┘━┃╍═║ ")


def is_chrome(line):
    s = line.replace(" ", " ").strip()  # TUI сыплет неразрывными пробелами
    if not s:
        return True
    if set(s) <= FRAME_CHARS:
        return True  # рамки и разделители
    if len(s) >= 4 and sum(c in FRAME_CHARS for c in s) / len(s) > 0.6:
        return True  # строка в основном из линий (обрывки рамок)
    if s.startswith(("❯", "⏸", "▐", "▝", "▘", "⧉", "›")):
        return True  # строка ввода (claude/codex), рекап, баннер, бейджи
    # футер и баннер codex: "gpt-5.6 high fast · ~/path", "model:", ">_ OpenAI Codex"
    if " · ~/" in s or s.startswith(("model:", "directory:", ">_")):
        return True
    if s.startswith("──") or s.endswith("──"):
        return True  # разделитель с именем сессии
    if s.startswith("⏵") and "⏵⏵" in s:
        return True  # футер-подсказка; одиночный ⏵ (шаг работы) оставляем
    if "Claude Code v" in s or "Claude Max" in s:
        return True
    if any(p in s for p in CHROME_SNIPPETS):
        return True
    if s.lstrip("⎿ ").startswith(("Tip:", "※ Tip")):
        return True  # советы TUI
    if s.startswith("Fable ") or s in ("Fable 5", "Opus", "Sonnet"):
        return True  # статус-строка с именем модели
    return False

last_seen = {}   # tmux-сессия -> {"hash", "changed"} для эвристики "работает"

# пока хоть один агент работает, держим caffeinate -i: мак не уснёт от простоя
# (экрану гаснуть можно). Умер сервер — caffeinate выйдет сам благодаря -w.
caffeinate = None


def update_caffeinate(active):
    global caffeinate
    if HEADLESS:
        return
    if active and caffeinate is None:
        try:
            caffeinate = subprocess.Popen(
                ["caffeinate", "-i", "-w", str(os.getpid())])
        except OSError:
            pass
    elif not active and caffeinate is not None:
        caffeinate.terminate()
        caffeinate = None
meta_cache = {}  # путь jsonl -> (mtime, cwd, title)
activity_cache = {}  # (путь, типы записей) -> (mtime, timestamp последнего события)
model_cache = {}     # (путь, агент) -> (mtime, модель)

MODEL_LABELS = {
    "fable": "Fable 5.1",
    "opus": "Opus 4.8",
    "sonnet": "Sonnet 5",
    "haiku": "Haiku 4.5",
    "claude-fable-5": "Fable 5",
    "claude-fable-5-1": "Fable 5.1",
    "claude-opus-4-8": "Opus 4.8",
    "claude-opus-4-7": "Opus 4.7",
    "claude-sonnet-5": "Sonnet 5",
    "claude-haiku-4-5": "Haiku 4.5",
    "gpt-5.6-sol": "GPT-5.6 Sol",
    "gpt-5.6-codex": "GPT-5.6 Codex",
    "gpt-6-astra": "GPT-6 Astra",
    "composer-2.5": "Composer 2.5",
    "composer-2.5-fast": "Composer 2.5 Fast",
    "openrouter/moonshotai/kimi-k3": "Kimi K3",
    "moonshotai/kimi-k3": "Kimi K3",
}


def tmux(*args):
    try:
        r = subprocess.run([*TMUX_CMD, *args], capture_output=True, text=True, timeout=5)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def tmux_ok(*args):
    try:
        return subprocess.run([*TMUX_CMD, *args], capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


def hook_status(name):
    """Статус из файла хука + его возраст (когда хук последний раз писал)."""
    try:
        p = os.path.join(STATUS_DIR, name)
        with open(p) as f:
            return f.read().strip(), os.path.getmtime(p)
    except OSError:
        return "", 0


def log_activity(path, record_types=()):
    """Timestamp последнего настоящего события внутри JSONL, а не mtime файла."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return 0
    key = (path, record_types)
    hit = activity_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    latest = 0
    try:
        with open(path, errors="ignore") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    if record_types and row.get("type") not in record_types:
                        continue
                    stamp = row.get("timestamp")
                    if stamp:
                        latest = max(latest, int(datetime.fromisoformat(
                            stamp.replace("Z", "+00:00")).timestamp()))
                except (ValueError, TypeError):
                    continue
    except OSError:
        return 0
    activity_cache[key] = (mtime, latest)
    return latest


def log_model(path, agent):
    """Модель из самого лога сессии; работает и для старых карточек."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    key = (path, agent)
    hit = model_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    model = ""
    try:
        with open(path, errors="ignore") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if agent == "claude" and row.get("type") == "assistant":
                    found = row.get("message", {}).get("model", "")
                    if found and found != "<synthetic>":
                        model = found
                elif agent == "codex" and row.get("type") == "turn_context":
                    found = row.get("payload", {}).get("model", "")
                    if found and found != "codex-auto-review":
                        model = found
    except OSError:
        return ""
    model_cache[key] = (mtime, model)
    return model


def model_label(model):
    return MODEL_LABELS.get(model, model)


# ---------- статус-хуки: установка в конфиги Claude Code и Codex ----------
#
# Оба CLI умеют lifecycle-хуки одинакового вида. Хук пишет статус в
# ~/.claude/agent-status/<tmux-сессия>, доска его читает. PermissionRequest —
# штатное событие «появился диалог разрешения» (Claude Code 2.x, Codex 0.122+).

CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")
CODEX_HOOKS_FILE = os.path.expanduser("~/.codex/hooks.json")
HOOK_MARK = "agent-status"  # по этой подстроке узнаём свои хуки в чужом конфиге

HOOK_EVENTS = {  # событие CLI -> статус на доске
    "PermissionRequest": "waiting",
    "UserPromptSubmit": "working",
    "PreToolUse": "working",   # разрешение получено, тул пошёл — снимаем «жду»
    "PostToolUse": "working",
    "Stop": "idle",
}


def hook_cmd(status):
    return ('[ -n "$TMUX" ] && mkdir -p ~/.claude/agent-status && '
            f'echo {status} > ~/.claude/agent-status/"$(tmux display -p \'#S\')"'
            '; true')


def _hooks_missing(cfg):
    """Каких событий из HOOK_EVENTS нет в блоке hooks конфига."""
    hooks = cfg.get("hooks") or {}
    return [ev for ev in HOOK_EVENTS
            if not any(HOOK_MARK in h.get("command", "")
                       for grp in hooks.get(ev, []) or []
                       for h in grp.get("hooks", []) or [])]


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f), True
    except FileNotFoundError:
        return {}, True
    except (OSError, ValueError):
        return {}, False  # битый конфиг — не трогаем


def hooks_state():
    claude_cfg, claude_ok = _read_json(CLAUDE_SETTINGS)
    codex_cfg, codex_ok = _read_json(CODEX_HOOKS_FILE)
    cursor_cfg, cursor_ok = _read_json(CURSOR_HOOKS_FILE)
    # «установлено» = хуки статусов + секция самоименования в глобальной памяти
    return {
        "claude": claude_ok and not _hooks_missing(claude_cfg) and _md_installed(CLAUDE_MD),
        "codex": codex_ok and not _hooks_missing(codex_cfg) and _md_installed(CODEX_MD),
        "cursor": cursor_ok and not _cursor_missing(cursor_cfg) and _cursor_script_ok(),
        "opencode": _opencode_plugin_ok() and _md_installed(OPENCODE_MD),
    }


# ---- Cursor: свой формат hooks.json + скрипт (нужен разбор stdin) ----
# У Cursor нет события «жду разрешения», поэтому waiting ставим перед каждым
# shell/MCP-вызовом и снимаем после: одобренная команда снимает его сама, а
# неодобренная так и держит карточку жёлтой. Заодно скрипт молча разрешает
# команду самоименования tee — без него первый же шаг агента упирался бы
# в диалог. Скрипт лежит рядом с конфигом, имя содержит HOOK_MARK.

CURSOR_HOOKS_FILE = os.path.expanduser("~/.cursor/hooks.json")
CURSOR_HOOK_SCRIPT = os.path.expanduser("~/.cursor/agent-status.sh")
CURSOR_HOOK_EVENTS = {
    "beforeSubmitPrompt": "working",
    "beforeShellExecution": "shell",
    "afterShellExecution": "working",
    "beforeMCPExecution": "shell",
    "afterMCPExecution": "working",
    "postToolUse": "working",
    "stop": "idle",
}
CURSOR_HOOK_SCRIPT_TEXT = """#!/bin/sh
# Agent Board: статус агента для доски (ставится с доски, см. agentboard.py)
IN=$(cat)
case "$IN" in  # команду самоименования разрешаем всегда, tmux ей не нужен
*"tee /tmp/agentboard-"*) [ "$1" = shell ] && { printf '{"permission":"allow"}'; exit 0; };;
esac
[ -n "$TMUX" ] || exit 0
S=$(tmux display -p '#S' 2>/dev/null)
[ -n "$S" ] || exit 0
D="$HOME/.claude/agent-status"; mkdir -p "$D"
if [ "$1" = shell ]; then
  echo waiting > "$D/$S"
else
  echo "$1" > "$D/$S"
fi
exit 0
"""


def _cursor_missing(cfg):
    hooks = cfg.get("hooks") or {}
    return [ev for ev in CURSOR_HOOK_EVENTS
            if not any(HOOK_MARK in h.get("command", "")
                       for h in hooks.get(ev, []) or [])]


def _cursor_script_ok():
    try:
        with open(CURSOR_HOOK_SCRIPT) as f:
            return f.read() == CURSOR_HOOK_SCRIPT_TEXT
    except OSError:
        return False


CURSOR_CLI_CONFIG = os.path.expanduser("~/.cursor/cli-config.json")


def _install_cursor():
    if not _cursor_script_ok():
        os.makedirs(os.path.dirname(CURSOR_HOOK_SCRIPT), exist_ok=True)
        with open(CURSOR_HOOK_SCRIPT, "w") as f:
            f.write(CURSOR_HOOK_SCRIPT_TEXT)
        os.chmod(CURSOR_HOOK_SCRIPT, 0o755)
    # tee-имя без диалога: hook-ответ beforeShellExecution курсор игнорирует,
    # поэтому штатный allowlist; tee — та же запись файла, что и его edit-тул
    cfg, ok = _read_json(CURSOR_CLI_CONFIG)
    if ok and "Shell(tee)" not in (cfg.get("permissions", {}).get("allow") or []):
        if os.path.exists(CURSOR_CLI_CONFIG) and \
                not os.path.exists(CURSOR_CLI_CONFIG + ".agentboard-bak"):
            shutil.copy2(CURSOR_CLI_CONFIG, CURSOR_CLI_CONFIG + ".agentboard-bak")
        cfg.setdefault("permissions", {}).setdefault("allow", []).append("Shell(tee)")
        tmp = CURSOR_CLI_CONFIG + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, CURSOR_CLI_CONFIG)
    cfg, ok = _read_json(CURSOR_HOOKS_FILE)
    if not ok:
        return
    missing = _cursor_missing(cfg)
    if not missing:
        return
    if os.path.exists(CURSOR_HOOKS_FILE) and \
            not os.path.exists(CURSOR_HOOKS_FILE + ".agentboard-bak"):
        shutil.copy2(CURSOR_HOOKS_FILE, CURSOR_HOOKS_FILE + ".agentboard-bak")
    cfg.setdefault("version", 1)
    hooks = cfg.setdefault("hooks", {})
    for ev in missing:
        hooks.setdefault(ev, []).append(
            {"command": f"{CURSOR_HOOK_SCRIPT} {CURSOR_HOOK_EVENTS[ev]}"})
    tmp = CURSOR_HOOKS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CURSOR_HOOKS_FILE)


# ---- opencode: плагин со статус-событиями + разрешение на tee в конфиге ----

OPENCODE_PLUGIN = os.path.expanduser("~/.config/opencode/plugins/agent-status.js")
# пишем в тот конфиг, что уже есть у юзера (jsonc с комментариями не парсим — пропустим)
OPENCODE_CONFIG = next(
    (p for p in (os.path.expanduser("~/.config/opencode/opencode.json"),
                 os.path.expanduser("~/.config/opencode/opencode.jsonc"))
     if os.path.exists(p)),
    os.path.expanduser("~/.config/opencode/opencode.json"))
OPENCODE_TEE = "tee /tmp/agentboard-*"
OPENCODE_PLUGIN_TEXT = """// Agent Board: agent-status — пишет статус агента для доски (см. agentboard.py)
import { execSync } from "node:child_process"
import fs from "node:fs"
import os from "node:os"

const MAP = {
  "permission.asked": "waiting",
  "permission.replied": "working",
  "tool.execute.after": "working",
  "message.part.updated": "working",
  "session.idle": "idle",
}

function write(status) {
  try {
    if (!process.env.TMUX) return
    const s = execSync("tmux display -p '#S'", { timeout: 3000 }).toString().trim()
    if (!s) return
    const dir = os.homedir() + "/.claude/agent-status"
    fs.mkdirSync(dir, { recursive: true })
    fs.writeFileSync(dir + "/" + s, status)
  } catch {}
}

export const AgentBoardStatus = async () => ({
  event: async ({ event }) => {
    const status = MAP[event && event.type]
    if (status) write(status)
  },
})
"""


def _opencode_plugin_ok():
    try:
        with open(OPENCODE_PLUGIN) as f:
            return f.read() == OPENCODE_PLUGIN_TEXT
    except OSError:
        return False


def _install_opencode():
    if not _opencode_plugin_ok():
        os.makedirs(os.path.dirname(OPENCODE_PLUGIN), exist_ok=True)
        with open(OPENCODE_PLUGIN, "w") as f:
            f.write(OPENCODE_PLUGIN_TEXT)
    # tee-имя без диалога разрешения (как --settings у claude)
    cfg, ok = _read_json(OPENCODE_CONFIG)
    if not ok:
        return
    perm = cfg.setdefault("permission", {})
    bash = perm.get("bash")
    if bash == "allow" or (isinstance(bash, dict) and OPENCODE_TEE in bash):
        return
    if isinstance(bash, str):  # строка-политика юзера — переносим в шаблоны
        perm["bash"] = {OPENCODE_TEE: "allow", "*": bash}
    elif isinstance(bash, dict):
        bash[OPENCODE_TEE] = "allow"
    else:
        perm["bash"] = {OPENCODE_TEE: "allow"}
    if os.path.exists(OPENCODE_CONFIG) and \
            not os.path.exists(OPENCODE_CONFIG + ".agentboard-bak"):
        shutil.copy2(OPENCODE_CONFIG, OPENCODE_CONFIG + ".agentboard-bak")
    os.makedirs(os.path.dirname(OPENCODE_CONFIG), exist_ok=True)
    tmp = OPENCODE_CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, OPENCODE_CONFIG)


def _install_into(path, extra=None):
    """Дописать недостающие хуки в JSON-конфиг. Чужое не трогаем, бэкап один раз."""
    cfg, ok = _read_json(path)
    if not ok:
        return False
    changed = False
    hooks = cfg.setdefault("hooks", {})
    # наш старый Notification-хук с грепом по тексту — убираем, он и был поломкой
    for grp in list(hooks.get("Notification", []) or []):
        cmds = [h.get("command", "") for h in grp.get("hooks", []) or []]
        if any(HOOK_MARK in c and "grep" in c for c in cmds):
            hooks["Notification"].remove(grp)
            if not hooks["Notification"]:
                del hooks["Notification"]
            changed = True
    for ev in _hooks_missing(cfg):
        entry = {"type": "command", "command": hook_cmd(HOOK_EVENTS[ev])}
        if extra:
            entry.update(extra)
        hooks.setdefault(ev, []).append({"hooks": [entry]})
        changed = True
    if changed:
        if os.path.exists(path) and not os.path.exists(path + ".agentboard-bak"):
            shutil.copy2(path, path + ".agentboard-bak")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    return True


# ---------- самоименование агентов ----------
# Инструкция лежит в глобальной памяти агента (CLAUDE.md / AGENTS.md), в первое
# сообщение уходит только короткий маркер [agentboard:<путь>]. Если секция
# не установлена — фолбэк: инструкция целиком в хвосте сообщения.

CLAUDE_MD = os.path.expanduser("~/.claude/CLAUDE.md")
CODEX_MD = os.path.expanduser("~/.codex/AGENTS.md")
OPENCODE_MD = os.path.expanduser("~/.config/opencode/AGENTS.md")
# у Cursor CLI глобального файла правил нет — он всегда получает фолбэк-хвост
AGENT_MD = {"claude": CLAUDE_MD, "codex": CODEX_MD, "opencode": OPENCODE_MD}
NAME_SECTION_MARK = "## Agent Board (meta-harness)"
OLD_SECTION_MARKS = ("## Agent Board (мета-харнесс)",)  # прежние версии секции
NAME_SECTION = f"""{NAME_SECTION_MARK}

A marker like [agentboard:<path>] in the first message means the session was
started from the Agent Board. The operations below are housekeeping, unrelated
to the task: don't mention them, don't comment on them, and don't repeat your
answer because of them.

1. As the very first action, before answering the task, run exactly one command:
   tee "<path>" <<< "name", where name is a short title of the task from the
   message, 2–4 words, in the task's language. Then work and reply as usual.
2. Only if the marker carries a +logo flag (it means the project has no avatar
   yet — no need to check yourself): at the very end, after the task is done
   and the answer is given, create an avatar — a downscaled copy of the
   project's logo or icon (e.g. `sips -Z 256 <source> --out agent-logo.png`);
   if the project has no media, a simple minimalist square agent-logo.svg in
   the project's spirit. In a git repository add agent-logo.* to
   .git/info/exclude. After the avatar, add no further text."""
# маркер (и старые длинные хвосты) вырезаем из превью и заголовков карточек
NAME_RE = re.compile(r"\s*\[(?:agentboard:|служебное, к задаче не относится:"
                     r"|housekeeping, unrelated to the task:)[^\]]*\]")


def _md_installed(path):
    """Установлена именно актуальная секция — устаревшая требует переустановки."""
    try:
        with open(path) as f:
            return NAME_SECTION in f.read()
    except OSError:
        return False


def _install_md(path):
    """Дописать секцию доски в память агента (или заменить её старую версию)."""
    try:
        with open(path) as f:
            txt = f.read()
    except OSError:
        txt = ""
    if NAME_SECTION in txt:
        return
    if os.path.exists(path) and not os.path.exists(path + ".agentboard-bak"):
        shutil.copy2(path, path + ".agentboard-bak")
    for mark in (NAME_SECTION_MARK,) + OLD_SECTION_MARKS:
        if mark in txt:  # старая версия — вырезаем до следующей секции
            i = txt.index(mark)
            j = txt.find("\n## ", i)
            txt = (txt[:i].rstrip() + (txt[j:] if j != -1 else "\n")).lstrip("\n")
    txt = (txt.rstrip() + "\n\n" if txt.strip() else "") + NAME_SECTION + "\n"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(txt)


AGENT_BINS = {"claude": CLAUDE, "codex": CODEX, "cursor": CURSOR, "opencode": OPENCODE}


def detected_agents():
    return [a for a, b in AGENT_BINS.items() if os.path.exists(b)]


def install_hooks(selected=None):
    """Хуки и память — только выбранным провайдерам (по умолчанию всем найденным)."""
    sel = set(selected if selected is not None else detected_agents())
    sel &= set(detected_agents())  # не мусорим в конфигах неустановленных CLI
    if "claude" in sel:
        # async: хук-маячок не должен задерживать Claude
        _install_into(CLAUDE_SETTINGS, extra={"async": True})
        _install_md(CLAUDE_MD)
    if "codex" in sel:  # Codex поле async не знает — без него
        _install_into(CODEX_HOOKS_FILE)
        _install_md(CODEX_MD)
    if "cursor" in sel:
        _install_cursor()
    if "opencode" in sel:
        _install_opencode()
        _install_md(OPENCODE_MD)
    return hooks_state()


# ---------- board.json: всё, что на доске ----------

# board.json читают и пишут несколько потоков — все read-modify-write под замком
BOARD_LOCK = threading.RLock()


def locked(fn):
    def wrapper(*args, **kwargs):
        with BOARD_LOCK:
            return fn(*args, **kwargs)
    return wrapper


def load_board():
    """board.json: {"workspaces": [...], "cards": [...], "hidden": [проекты]}"""
    try:
        with open(BOARD_FILE) as f:
            data = json.load(f)
    except Exception:
        data = {}
    if isinstance(data, list):  # старый формат — только карточки
        data = {"cards": data}
    data.setdefault("workspaces", [])
    data.setdefault("cards", [])
    data.setdefault("hidden", [])
    data.setdefault("labels", {})  # id разговора -> имя; живёт дольше карточки
    data.setdefault("closed", [])  # недавно убранные с доски — можно вернуть
    data.setdefault("providers", None)  # None = онбординг ещё не пройден
    data.setdefault("models", {})  # agent -> {"favs": [id...], "def": id}
    data.setdefault("remotes", [])  # другие доски-узлы: {"name", "url", "token", "ssh"}
    return data


def save_board(board):
    with open(BOARD_FILE, "w") as f:
        json.dump(board, f, ensure_ascii=False, indent=1)


def sanitize(cwd):
    """Путь проекта -> имя папки в ~/.claude/projects."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def session_file(cwd, sid):
    return os.path.join(PROJECTS_DIR, sanitize(cwd), sid + ".jsonl")


_sid_paths = {}  # sid -> найденный путь транскрипта


def find_session_file(cwd, sid):
    """Транскрипт по id: сперва папка проекта; если разговор резюмили из другой
    папки — файл остаётся в исходной, ищем по всем проектам."""
    p = session_file(cwd, sid)
    if os.path.isfile(p):
        return p
    hit = _sid_paths.get(sid)
    if hit and os.path.isfile(hit):
        return hit
    for p in glob.glob(os.path.join(PROJECTS_DIR, "*", sid + ".jsonl")):
        _sid_paths[sid] = p
        return p
    return ""


# служебный файл проекта — иконка агента на доске (как CLAUDE.md, только для борда)
LOGO_NAMES = ("agent-logo.png", "agent-logo.jpg", "agent-logo.jpeg",
              "agent-logo.webp", "agent-logo.svg")
LOGO_TYPES = {".png": "image/png", ".jpg": "image/jpeg",
              ".jpeg": "image/jpeg", ".webp": "image/webp",
              ".svg": "image/svg+xml"}


def find_logo(cwd):
    for n in LOGO_NAMES:
        p = os.path.join(cwd, n)
        if os.path.isfile(p):
            return p
    return None


def logo_version(cwd):
    p = find_logo(cwd)
    try:
        return int(os.stat(p).st_mtime) if p else 0
    except OSError:
        return 0


def session_records():
    """Живые регистрации Claude Code: pid -> sessionId (~/.claude/sessions)."""
    recs = []
    for p in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            with open(p) as f:
                recs.append(json.load(f))
        except Exception:
            pass
    return recs


bg_cmd_cache = {}  # pid -> командная строка (распознавание bg-форков)


def bg_fork_of(rec, sid):
    """Фоновый форк (Agent View / remote control), продолжающий разговор sid.
    Демон клода запускает его как `claude --fork-session --resume <файл sid>`;
    записи в самом форке переписаны на новый sid, так что родословная видна
    только в командной строке живого процесса."""
    if rec.get("kind") != "bg" or not sid:
        return False
    pid = str(rec.get("pid", ""))
    cmd = bg_cmd_cache.get(pid)
    if cmd is None:
        try:
            cmd = subprocess.run(["ps", "-o", "command=", "-p", pid],
                                 capture_output=True, text=True, timeout=5).stdout
        except Exception:
            cmd = ""
        bg_cmd_cache[pid] = cmd
    return "--fork-session" in cmd and sid in cmd


def pane_pids(name):
    """PID процессов внутри tmux-сессии (панель + два уровня детей)."""
    pids = [p.strip() for p in
            tmux("list-panes", "-t", name, "-F", "#{pane_pid}").split() if p.strip()]
    found = set(pids)
    for _ in range(2):
        kids = []
        for pid in pids:
            try:
                r = subprocess.run(["pgrep", "-P", pid],
                                   capture_output=True, text=True, timeout=3)
                kids += r.stdout.split()
            except Exception:
                pass
        found.update(kids)
        pids = kids
    return found


# ---------- история разговоров Claude Code ----------

def session_meta(path):
    """cwd и заголовок разговора из первых строк jsonl."""
    cwd, title = "", ""
    try:
        with open(path, errors="ignore") as f:
            for _ in range(200):
                line = f.readline()
                if not line:
                    break
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not cwd and rec.get("cwd"):
                    cwd = rec["cwd"]
                if not title and rec.get("type") == "summary":
                    title = rec.get("summary", "")
                if not title and rec.get("type") == "user":
                    c = rec.get("message", {}).get("content")
                    if isinstance(c, list):
                        c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                    if isinstance(c, str):
                        c = c.strip()
                        if c and not c.startswith("<") and not c.startswith("Caveat"):
                            title = NAME_RE.sub("", c).strip()
                if cwd and title:
                    break
    except OSError:
        pass
    return cwd, " ".join(title.split())[:90]


_names = {"t": 0.0, "map": {}}


def session_names():
    """sessionId -> имя, которое юзер дал через /rename (~/.claude/sessions)."""
    now = time.time()
    if now - _names["t"] < 5:
        return _names["map"]
    best, m = {}, {}
    for p in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            with open(p) as f:
                rec = json.load(f)
        except Exception:
            continue
        sid, name = rec.get("sessionId"), rec.get("name")
        if not sid or not name:
            continue
        u = rec.get("updatedAt", 0)
        if u >= best.get(sid, -1):
            best[sid], m[sid] = u, name
    _names["t"], _names["map"] = now, m
    return m


def cached_meta(path):
    try:
        m = os.stat(path).st_mtime
    except OSError:
        return "", ""
    hit = meta_cache.get(path)
    if hit and hit[0] == m:
        return hit[1], hit[2]
    cwd, title = session_meta(path)
    meta_cache[path] = (m, cwd, title)
    return cwd, title


def newest_session(cwd, after=0, exclude=()):
    """Свежайший разговор проекта: по времени последнего сообщения после момента
    after. Не по mtime файла — клод дописывает сервисные записи (ai-title,
    bridge-session, last-prompt) в старые файлы и делает мёртвые «свежими»."""
    best, best_m = None, 0
    for p in glob.glob(os.path.join(PROJECTS_DIR, sanitize(cwd), "*.jsonl")):
        if os.path.basename(p)[:-6] in exclude:
            continue
        m = log_activity(p, ("user", "assistant"))
        if m >= after - 60 and m > best_m:
            best, best_m = p, m
    if not best:
        return "", ""
    _, title = cached_meta(best)
    return os.path.basename(best)[:-6], title


def get_history(cwd=None, days=30, limit=50):
    """Разговоры, которые можно вытащить на доску."""
    pattern = os.path.join(PROJECTS_DIR, sanitize(cwd) if cwd else "*", "*.jsonl")
    now = time.time()
    files = []
    for p in glob.glob(pattern):
        try:
            m = os.stat(p).st_mtime
        except OSError:
            continue
        if now - m <= days * 86400:
            files.append((m, p))
    files.sort(reverse=True)
    names = session_names()
    labels = load_board()["labels"]  # имена с доски главнее имён из /rename
    hist = []
    for m, p in files[:limit]:
        scwd, title = cached_meta(p)
        if not scwd:
            continue
        sid = os.path.basename(p)[:-6]
        hist.append({
            "id": sid,
            "cwd": scwd,
            "project": os.path.basename(scwd.rstrip("/")) or scwd,
            "name": labels.get(sid) or names.get(sid, ""),
            "title": title or "untitled",
            "age": int(now - m),
        })
    return hist


preview_cache = {}  # путь jsonl -> (mtime, превью)


def claude_items(path):
    """Реплики и тул-вызовы из jsonl клода: [("u"|"a"|"t", текст)]; None — файл не читается."""
    try:
        size = os.path.getsize(path)
        with open(path, errors="ignore") as f:
            if size > 2_000_000:
                f.seek(size - 2_000_000)
                f.readline()  # добить обрезанную строку
            lines = f.readlines()
    except OSError:
        return None

    def user_text(c):
        # юзер-контент бывает строкой или списком блоков
        if isinstance(c, str):
            s = c.strip()
        elif isinstance(c, list):
            s = " ".join(x.get("text", "") for x in c
                         if isinstance(x, dict) and x.get("type") == "text").strip()
        else:
            return ""
        s = NAME_RE.sub("", s).strip()
        return "" if not s or s.startswith("<") else s

    def tool_sig(item):
        # "Bash(tee /tmp/…)" — как в TUI; фронт красит строки "⏺ Имя(…)"
        name = item.get("name", "tool")
        parts = name.split("__", 2)
        if name.startswith("mcp__") and len(parts) == 3:
            # mcp__claude_ai_Pushkin__list_files -> "Pushkin: list_files"
            name = f"{parts[1].split('_')[-1]}: {parts[2]}"
        inp = item.get("input") or {}
        detail = (inp.get("command") or inp.get("file_path") or inp.get("path")
                  or inp.get("pattern") or inp.get("description") or "")
        detail = " ".join(str(detail).split())[:60]
        return f'{name}({detail or "…"})'

    items = []  # ("u"|"a"|"t", текст)
    for l in lines:
        try:
            r = json.loads(l)
        except ValueError:
            continue
        if r.get("type") == "user":
            if r.get("isMeta"):
                continue  # инжекты харнесса (скиллы, преамбулы) — не сообщения юзера
            t = user_text(r.get("message", {}).get("content"))
            if t:
                items.append(("u", t))
        elif r.get("type") == "assistant":
            for item in r.get("message", {}).get("content", []):
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text" and item.get("text", "").strip():
                    items.append(("a", item["text"].strip()))
                elif item.get("type") == "tool_use":
                    items.append(("t", tool_sig(item)))
    return items


def render_items(chunk, limit=500):
    """Реплики -> текст превью: «> вопрос», «⏺ Тул(деталь)», ответы как есть."""
    out = []
    for kind, text in chunk:
        if kind == "u":
            out.append("> " + " ".join(text.split())[:500])
        elif kind == "t":
            out.append("⏺ " + text)
        else:
            out.append(text)
    t = "\n\n".join(out)
    return "\n".join(t.splitlines()[-limit:])


def turn_preview(cwd, sid, include_trailing=True):
    """Последний тур из jsonl-транскрипта: полный текст, без терминального хрома.
    Якоримся на последнем ОТВЕЧЕННОМ вопросе: неотвеченный (прерванный) вопрос
    показываем хвостом, а не пустым туром."""
    path = find_session_file(cwd, sid)
    if not path:
        return ""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    hit = preview_cache.get(path)
    if hit and hit[0] == mtime and len(hit) == 3:
        return hit[1] if include_trailing else hit[2]
    items = claude_items(path)
    if items is None:
        return ""
    if not items:
        preview_cache[path] = (mtime, "", "")
        return ""

    # занятый агент: якорь — твоё последнее сообщение (виден вопрос + растущий ответ)
    last_u = max((i for i, (k, _) in enumerate(items) if k == "u"), default=0)
    busy_text = render_items(items[last_u:])

    # тихий агент: последний ЗАВЕРШЁННЫЙ тур; мёртвые неотвеченные хвосты не показываем
    answered = None
    for i, (kind, _) in enumerate(items):
        if kind == "u" and any(k != "u" for k, _ in items[i + 1:]):
            answered = i
    seq = items[answered:] if answered is not None else items[last_u:]
    cut = len(seq)
    while cut > 0 and seq[cut - 1][0] == "u":
        cut -= 1
    idle_text = render_items(seq[:cut]) or busy_text

    preview_cache[path] = (mtime, busy_text, idle_text)
    return busy_text if include_trailing else idle_text


CODEX_SESS = os.path.expanduser("~/.codex/sessions")
codex_meta_cache = {}  # путь rollout -> (cwd, fork) из session_meta


def codex_meta(path):
    """cwd + признак саб-агентского форка (codex ≥0.144 пишет рядом rollout'ы
    subagent-тредов с тем же cwd — карточке они не принадлежат)."""
    if path in codex_meta_cache:
        return codex_meta_cache[path]
    cwd, fork = "", False
    try:
        with open(path, errors="ignore") as f:
            payload = json.loads(f.readline()).get("payload", {})
        cwd = payload.get("cwd", "")
        fork = bool(payload.get("parent_thread_id")) \
            or payload.get("thread_source") == "subagent"
    except Exception:
        pass
    codex_meta_cache[path] = (cwd, fork)
    return cwd, fork


def codex_id(ro):
    """Id разговора codex — uuid в имени rollout-файла (им же берёт `codex resume`)."""
    m = re.search(r"([0-9a-f-]{36})\.jsonl$", ro)
    return m.group(1) if m else ""


def codex_rollout(cwd, created, exclude=()):
    """Свежайший rollout codex для этой папки, начатый после старта tmux-сессии."""
    best, best_m = "", 0.0
    for path in glob.glob(os.path.join(CODEX_SESS, "*", "*", "*", "*.jsonl")):
        if path in exclude:
            continue
        try:
            m = os.path.getmtime(path)
        except OSError:
            continue
        if m < created - 60 or m <= best_m:
            continue
        p_cwd, fork = codex_meta(path)
        if p_cwd == cwd and not fork:
            best, best_m = path, m
    return best


def codex_items(path, window=400_000):
    """Реплики и тул-вызовы из rollout codex: [(роль|"tool", текст)]; None — не читается."""
    try:
        size = os.path.getsize(path)
        with open(path, errors="ignore") as f:
            if size > window:
                f.seek(size - window)
                f.readline()
            lines = f.readlines()
    except OSError:
        return None
    items = []
    for l in lines:
        try:
            r = json.loads(l)
        except ValueError:
            continue
        if r.get("type") != "response_item":
            continue
        pay = r.get("payload", {})
        if pay.get("type") == "message":
            text = " ".join(seg.get("text", "") for seg in pay.get("content", [])
                            if isinstance(seg, dict) and seg.get("text"))
            items.append((pay.get("role"), text))
        elif pay.get("type") in ("function_call", "custom_tool_call",
                                 "local_shell_call", "web_search_call"):
            name = pay.get("name") or pay.get("type").replace("_call", "")
            detail = ""
            if pay.get("type") == "function_call":
                try:
                    args = json.loads(pay.get("arguments") or "{}")
                    detail = args.get("cmd") or args.get("command") or ""
                except ValueError:
                    pass
            elif pay.get("type") == "custom_tool_call":
                # команда зашита в JS-обёртку: tools.exec_command({cmd:"…"})
                m = re.search(r'"?cmd"?\s*:\s*"((?:[^"\\]|\\.)*)"', pay.get("input") or "")
                if m:
                    try:
                        detail = json.loads('"' + m.group(1) + '"')
                    except ValueError:
                        pass
            elif pay.get("type") == "local_shell_call":
                detail = (pay.get("action") or {}).get("command") or ""
            if isinstance(detail, list):
                detail = " ".join(detail)
            detail = " ".join(str(detail).split())[:60]
            items.append(("tool", f"{name}({detail or '…'})"))
    return items


def codex_said(role, text):
    """Настоящая реплика юзера; scaffold-сообщения codex ≥0.144 тоже идут с ролью user."""
    return role == "user" and text.strip() \
        and not text.lstrip().startswith(("<", "# AGENTS.md"))


def codex_turn_preview(path):
    """Последний тур из rollout-файла codex."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    hit = preview_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    items = codex_items(path)
    if items is None:
        return ""
    start = None
    for i, (role, text) in enumerate(items):
        if codex_said(role, text):
            start = i
    if start is None:
        # длинный тур: реплика юзера уехала за 400КБ-окно — показываем хвост работы
        out = []
        tail = items
    else:
        out = ["> " + " ".join(NAME_RE.sub("", items[start][1]).split())[:500]]
        tail = items[start + 1:]
    for role, text in tail:
        if role == "assistant" and text.strip():
            out.append(text.strip())
        elif role == "tool":
            out.append("⏺ " + text)
    if not out:
        preview_cache[path] = (mtime, "")
        return ""
    text = "\n\n".join(out)
    text = "\n".join(text.splitlines()[-500:])
    preview_cache[path] = (mtime, text)
    return text


# ---------- сессии Cursor: ~/.cursor/chats/<md5(cwd)>/<uuid>/store.db ----------

CURSOR_CHATS = os.path.expanduser("~/.cursor/chats")
cursor_model_cache = {}  # путь store.db -> имя модели из блобов


def _cursor_meta(db):
    try:
        with open(os.path.join(os.path.dirname(db), "meta.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def cursor_chat(cwd, created, exclude=()):
    """Свежайший чат Cursor этой папки, тронутый после старта tmux-сессии."""
    h = hashlib.md5(cwd.encode()).hexdigest()
    best, best_t = "", 0
    for db in glob.glob(os.path.join(CURSOR_CHATS, h, "*", "store.db")):
        if db in exclude:
            continue
        t = _cursor_meta(db).get("updatedAtMs", 0)
        if t < (created - 60) * 1000 or t <= best_t:
            continue
        best, best_t = db, t
    return best


def cursor_turn_preview(db):
    """Последний тур из store.db: JSON-блобы сообщений в порядке вставки."""
    stamp = _cursor_meta(db).get("updatedAtMs", 0)  # mtime базы не годится (WAL)
    hit = preview_cache.get(db)
    if hit and hit[0] == stamp:
        return hit[1]
    items = []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
        con.text_factory = bytes  # среди блобов есть protobuf — не декодируется
        rows = con.execute("SELECT data FROM blobs ORDER BY rowid").fetchall()
        con.close()
    except sqlite3.Error:
        return ""
    for (raw,) in rows:
        if not raw or not raw.startswith(b'{"role":'):
            continue
        try:
            r = json.loads(raw.decode("utf-8", "ignore"))
        except ValueError:
            continue
        content = r.get("content")
        if r.get("role") == "user":
            text = content if isinstance(content, str) else " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text")
            m = re.search(r"<user_query>\s*(.*?)\s*</user_query>", text, re.S)
            text = m.group(1) if m else text.strip()
            text = NAME_RE.sub("", text).strip()
            if text and not text.startswith("<"):
                items.append(("user", text))
        elif r.get("role") == "assistant" and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                model = (b.get("providerOptions") or {}).get("cursor", {}).get("modelName")
                if model:
                    cursor_model_cache[db] = model
                if b.get("type") == "text" and b.get("text", "").strip():
                    items.append(("assistant", b["text"].strip()))
                elif b.get("type") in ("tool-call", "tool_call"):
                    args = b.get("args") or b.get("input") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {}
                    detail = (args.get("command") or args.get("cmd")
                              or args.get("file_path") or args.get("path")
                              or args.get("pattern") or args.get("query") or "")
                    detail = " ".join(str(detail).split())[:60]
                    name = b.get("toolName") or b.get("name") or "tool"
                    items.append(("tool", f"{name}({detail or '…'})"))
    start = max((i for i, (k, _) in enumerate(items) if k == "user"), default=None)
    if start is None:
        preview_cache[db] = (stamp, "")
        return ""
    out = ["> " + " ".join(items[start][1].split())[:500]]
    for kind, text in items[start + 1:]:
        out.append("⏺ " + text if kind == "tool" else text)
    text = "\n".join("\n\n".join(out).splitlines()[-500:])
    preview_cache[db] = (stamp, text)
    return text


# ---------- сессии opencode: общий sqlite ~/.local/share/opencode ----------

OPENCODE_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")


def _opencode_q(sql, args=()):
    try:
        con = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True, timeout=1)
        rows = con.execute(sql, args).fetchall()
        con.close()
        return rows
    except sqlite3.Error:
        return []


def opencode_session(cwd, created, exclude=()):
    """id свежайшей сессии opencode этой папки после старта tmux-сессии."""
    for (sid,) in _opencode_q(
            "SELECT id FROM session WHERE directory = ? AND time_updated >= ? "
            "ORDER BY time_updated DESC", (cwd, int((created - 60) * 1000))):
        if sid not in exclude:
            return sid
    return ""


def opencode_meta(sid):
    """(activity, model) сессии — из её строки в базе."""
    rows = _opencode_q("SELECT time_updated, model FROM session WHERE id = ?", (sid,))
    if not rows:
        return 0, ""
    t, model = rows[0]
    if model and model.startswith("{"):  # модель хранится JSON-объектом
        try:
            m = json.loads(model)
            model = m.get("id") or m.get("modelID") or ""
        except ValueError:
            model = ""
    return int((t or 0) / 1000), model or ""


def opencode_turn_preview(sid):
    """Последний тур: message/part из общей базы, в хронологии."""
    stamp = opencode_meta(sid)[0]
    hit = preview_cache.get(sid)
    if hit and hit[0] == stamp:
        return hit[1]
    roles = dict(_opencode_q(
        "SELECT id, json_extract(data, '$.role') FROM message "
        "WHERE session_id = ?", (sid,)))
    items = []
    for mid, raw in _opencode_q(
            "SELECT message_id, CAST(data AS TEXT) FROM part "
            "WHERE session_id = ? ORDER BY time_created", (sid,)):
        try:
            p = json.loads(raw)
        except ValueError:
            continue
        role = roles.get(mid, "")
        if p.get("type") == "text" and p.get("text", "").strip():
            text = p["text"].strip()
            if role == "user":
                text = NAME_RE.sub("", text).strip()
                if not text or text.startswith("<"):
                    continue
            items.append((role, text))
        elif p.get("type") == "tool":
            inp = (p.get("state") or {}).get("input") or {}
            detail = (inp.get("command") or inp.get("cmd") or inp.get("filePath")
                      or inp.get("path") or inp.get("pattern") or "")
            detail = " ".join(str(detail).split())[:60]
            items.append(("tool", f"{p.get('tool', 'tool')}({detail or '…'})"))
    start = max((i for i, (k, _) in enumerate(items) if k == "user"), default=None)
    if start is None:
        preview_cache[sid] = (stamp, "")
        return ""
    out = ["> " + " ".join(items[start][1].split())[:500]]
    for kind, text in items[start + 1:]:
        out.append("⏺ " + text if kind == "tool" else text)
    text = "\n".join("\n\n".join(out).splitlines()[-500:])
    preview_cache[sid] = (stamp, text)
    return text


# ---------- общий интерфейс к структурным логам не-клодов ----------
# card["rollout"]: codex и cursor — путь к файлу лога, opencode — id сессии.

def find_log(agent, cwd, created, exclude):
    if agent == "codex":
        return codex_rollout(cwd, created, exclude)
    if agent == "cursor":
        return cursor_chat(cwd, created, exclude)
    if agent == "opencode":
        return opencode_session(cwd, created, exclude)
    return ""


def log_valid(agent, ro):
    if not ro:
        return False
    if agent == "opencode":
        return bool(_opencode_q("SELECT 1 FROM session WHERE id = ?", (ro,)))
    if agent == "codex":
        # привязанный когда-то форк саб-агента отпускаем — карточка перепривяжется
        return os.path.isfile(ro) and not codex_meta(ro)[1]
    return os.path.isfile(ro)


def log_stamp(agent, ro):
    if agent == "codex":
        return log_activity(ro)
    if agent == "cursor":
        return int(_cursor_meta(ro).get("updatedAtMs", 0) / 1000)
    return opencode_meta(ro)[0]


def log_model_of(agent, ro):
    if agent == "codex":
        return log_model(ro, "codex")
    if agent == "cursor":
        cursor_turn_preview(ro)  # модель добывается по пути разбора блобов
        return cursor_model_cache.get(ro, "")
    return opencode_meta(ro)[1]


def log_preview(agent, ro):
    if agent == "codex":
        return codex_turn_preview(ro)
    if agent == "cursor":
        return cursor_turn_preview(ro)
    return opencode_turn_preview(ro)


# ---------- окно диалога: весь разговор, а не последний тур ----------
# В вебе и на телефоне терминала нет: карточка открывается диалогом. Текст —
# из того же структурного лога, что и превью; экран tmux отдаём рядом, чтобы
# было видно TUI-вопрос, на который отвечают клавишами (send_key).

dialog_cache = {}  # путь лога -> (mtime, текст)
dialog_seen = {}   # tmux-сессия -> когда диалог последний раз опрашивали (окно открыто — каждые 2с)


def dialog_text(card):
    agent = card.get("agent", "claude")
    if agent == "claude":
        path = find_session_file(card["cwd"], card["id"]) if card.get("id") else ""
    else:
        path = card.get("rollout", "")
    if not path:
        return ""
    if agent not in ("claude", "codex"):
        return log_preview(agent, path)  # cursor/opencode — пока последний тур
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    hit = dialog_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    if agent == "claude":
        items = claude_items(path) or []
    else:
        items = [("u" if role == "user" else "t" if role == "tool" else "a",
                  NAME_RE.sub("", text))
                 for role, text in codex_items(path, 2_000_000) or []
                 if role == "tool" or codex_said(role, text)
                 or (role == "assistant" and text.strip())]
    text = render_items(items, 3000)
    dialog_cache[path] = (mtime, text)
    return text


def get_dialog(tname, cid, active=True):
    with BOARD_LOCK:
        cards = load_board()["cards"]
    card = (next((c for c in cards if tname and c.get("tmux") == tname), None)
            or next((c for c in cards if cid and c.get("id") == cid), None))
    if not card:
        return None
    pane = tmux("capture-pane", "-p", "-t", card["tmux"]) if card.get("tmux") else ""
    # фоновая вкладка с открытым диалогом опрашивает так же, но на агента никто
    # не смотрит — иначе уведомление в Telegram молча пропадало
    if card.get("tmux") and active:
        dialog_seen[card["tmux"]] = time.time()
    return {"text": dialog_text(card), "pane": pane.rstrip()}


def being_watched(name):
    """На агента сейчас смотрят: его диалог открыт во вкладке, которая на виду и в фокусе,
    или к tmux-сессии подключён терминал."""
    return (time.time() - dialog_seen.get(name, 0) < 10
            or bool(tmux("list-clients", "-t", name).strip()))


KEYS = {"esc": "Escape", "enter": "Enter", "up": "Up", "down": "Down", "tab": "Tab"}


def send_key(name, key):
    """Клавиша в терминал агента: прервать (esc), выбрать пункт TUI-диалога."""
    if key not in KEYS or not tmux_ok("has-session", "-t", name):
        return False
    tmux("send-keys", "-t", name, KEYS[key])
    return True


# ---------- облако: другие доски-узлы ----------
# На сервере крутится такой же agentboard.py со своим tmux и своим board.json —
# он источник правды для облачных агентов. Эта доска подмешивает его карточки
# к своим (поле host) и пересылает ему действия по ним (Handler.proxy).
# С телефона узел открывают напрямую: там только облачные.

REMOTE = {}             # имя узла -> {"data": его /api/agents, "at": когда получили}
REMOTE_WANTED = [0.0]   # когда доску последний раз опрашивали: закрыта — узлы не дёргаем


def remote_get(r, path, timeout=8):
    req = urllib.request.Request(
        r["url"].rstrip("/") + path,
        headers={"Authorization": "Bearer " + r.get("token", ""),
                 "User-Agent": "agentboard"})
    return urllib.request.urlopen(req, timeout=timeout)


def remote_fetch(r):
    try:
        with remote_get(r, "/api/agents") as resp:
            REMOTE[r["name"]] = {"data": json.load(resp), "at": time.time()}
        return True
    except Exception:
        return False  # нет связи — показываем последний снимок, узел помечен ok=False


def remote_poller():
    while True:
        time.sleep(2)
        if time.time() - REMOTE_WANTED[0] > 10:
            continue
        with BOARD_LOCK:
            remotes = load_board()["remotes"]
        for r in remotes:
            remote_fetch(r)


@locked
def remote_set(url, token, ssh):
    """Подключить узел (пустой url — отключить). Пока узел один, зовётся cloud."""
    board = load_board()
    if not token and board["remotes"]:
        token = board["remotes"][0].get("token", "")  # правят адрес/ssh — токен прежний
    board["remotes"] = []
    REMOTE.clear()
    ok = True
    if url:
        r = {"name": "cloud", "url": url.rstrip("/"), "token": token, "ssh": ssh}
        ok = remote_fetch(r)  # заодно проверка адреса и токена
        if ok:
            board["remotes"] = [r]
    save_board(board)
    return ok


def merge_remotes(board, agents, workspaces):
    """Дописать карточки и папки узлов; вернуть сводку по узлам для фронта."""
    REMOTE_WANTED[0] = time.time()
    out = []
    for r in board["remotes"]:
        hit = REMOTE.get(r["name"])
        d = hit["data"] if hit else {}
        out.append({"name": r["name"], "url": r["url"], "ssh": r.get("ssh", ""),
                    "loaded": bool(hit),
                    "ok": bool(hit) and time.time() - hit["at"] < 20,
                    "claude": d.get("claude", False), "codex": d.get("codex", False),
                    "cursor": d.get("cursor", False), "opencode": d.get("opencode", False),
                    "providers": d.get("providers")})
        # имена tmux-сессий у узла свои — префикс, чтобы не совпали с локальными
        agents += [dict(a, name=r["name"] + "/" + a["name"], host=r["name"])
                   for a in d.get("agents", [])]
        workspaces += [dict(w, host=r["name"]) for w in d.get("workspaces", [])]
    return out


# ---------- живые агенты (tmux) ----------

def get_live():
    rows = tmux("list-sessions", "-F",
                "#{session_name}\t#{session_path}\t#{session_attached}\t#{session_created}")
    agents = []
    now = time.time()
    for line in rows.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        name, path, attached, created = parts
        pane = tmux("capture-pane", "-p", "-t", name, "-S", "-2000")
        content = []
        tip_wrap = False  # Tip: переносится на несколько строк — режем весь абзац
        for raw in pane.splitlines():
            s = raw.replace(" ", " ").strip()
            if tip_wrap:
                if not s or s.startswith(("⏺", "⎿", ">", "✻", "✳", "❯")):
                    tip_wrap = False
                else:
                    continue
            if s.lstrip("⎿ ").startswith("Tip:"):
                tip_wrap = True
                continue
            if not is_chrome(raw):
                content.append(raw.rstrip())
        # последний тур целиком: от последнего сообщения юзера ("> ...") до конца
        start = 0
        for i, l in enumerate(content):
            if l.lstrip().startswith("> "):
                start = i
        preview = "\n".join(content[start:][-500:])

        h = hash(pane)
        rec = last_seen.setdefault(name, {"hash": h, "changed": now})
        hook, hook_at = hook_status(name)
        if rec["hash"] != h:
            # хук на отказ не срабатывает; распознаём ответ юзера в терминале:
            # диалог держит панель неподвижной, ответ её оживляет
            if hook == "waiting" and now - rec["changed"] > 4:
                rec["answered"] = hook_at
            rec["hash"], rec["changed"] = h, now
        if hook == "waiting" and rec.get("answered") == hook_at:
            hook = "working"  # на этот вопрос уже ответили руками
        if hook == "waiting":
            status = "waiting"
        elif now - rec["changed"] < 10:
            status = "working"
        elif hook == "working" and now - hook_at < 900 and now - rec["changed"] < 120:
            # хук сказал "работает", панель тихая — верим ещё 2 минуты. Но не
            # старому файлу: Stop из фоновых форков (Agent View) не приходит
            # (нет $TMUX), и «working» может висеть часами
            status = "working"
        else:
            status = "idle"

        agents.append({
            "name": name,
            "project": os.path.basename(path.rstrip("/")) or name,
            "path": path,
            "attached": attached != "0",
            "created": int(created or 0),
            "status": status,
            "preview": preview,
            "activity": int(created or 0),
            # когда «ждёт» сказал хук — момент вопроса; нужен ниже, чтобы
            # отлеплять протухший waiting по активности структурного лога
            "hook_at": hook_at if hook == "waiting" else 0,
        })
    update_caffeinate(any(a["status"] == "working" for a in agents))
    return agents


@locked
def get_agents():
    """Живые из tmux + карточки на паузе. Всё живое само попадает в board.json."""
    live = get_live()
    board = load_board()
    cards_list = board["cards"]
    changed = False

    # доска, жившая до онбординга, — считаем, что выбраны все найденные CLI
    if board["providers"] is None and (cards_list or board["workspaces"]):
        board["providers"] = detected_agents()
        changed = True

    # дедупликация: один разговор — одна карточка. Из двух держим ту, у которой
    # живой терминал, а имя переносим: раньше выживала просто первая в списке —
    # самоназванная карточка молча теряла имя, а живая пересоздавалась каждый опрос
    live_names = {a["name"] for a in live}
    seen = {}
    for card in list(cards_list):
        key = card.get("id") or ("tmux:" + card.get("tmux", ""))
        first = seen.get(key)
        if first is None:
            seen[key] = card
            continue
        alive = card.get("tmux") in live_names and first.get("tmux") not in live_names
        keep, drop = (card, first) if alive else (first, card)
        keep["label"] = keep.get("label") or drop.get("label", "")
        if keep.get("id") and keep["label"]:
            board["labels"][keep["id"]] = keep["label"]
        seen[key] = keep
        cards_list.remove(drop)
        changed = True

    by_tmux = {c.get("tmux"): c for c in cards_list if c.get("tmux")}
    ws_projects = {w["project"] for w in board["workspaces"]}

    # имена от самих агентов: файл в NAMES_DIR, имя файла = tmux-сессия (см. name_tail)
    try:
        name_files = os.listdir(NAMES_DIR)
    except OSError:
        name_files = []
    for fn in name_files:
        p = os.path.join(NAMES_DIR, fn)
        card = by_tmux.get(fn)
        # ручное имя (с доски или по id) всегда главнее агентского
        if card and card.get("tmux") != DUTY_NAME and not (card.get("label") or board["labels"].get(card.get("id") or "")):
            try:
                with open(p) as f:
                    label = " ".join(f.read().split())[:60]
            except OSError:
                label = ""
            if label:
                card["label"] = label
                if card.get("id"):
                    board["labels"][card["id"]] = label
                changed = True
        try:
            os.remove(p)
        except OSError:
            pass

    # у каждого живого агента должен быть воркспейс (живой проект снимает скрытие)
    for a in live:
        if a["project"] in board["hidden"]:
            board["hidden"].remove(a["project"])
            changed = True
        if a["project"] not in ws_projects:
            board["workspaces"].append({"project": a["project"], "cwd": a["path"]})
            ws_projects.add(a["project"])
            changed = True

    recs = session_records()
    names = session_names()
    for a in live:
        card = by_tmux.get(a["name"])
        if not card:
            card = {"tmux": a["name"], "cwd": a["path"],
                    "project": a["project"], "id": "", "title": ""}
            cards_list.append(card)
            by_tmux[a["name"]] = card
            changed = True
        a["agent"] = card.get("agent", "claude")
        a["model"] = model_label(card.get("model", ""))
        if a["agent"] != "claude":
            # claude-сессии у не-клодов нет; id разговора есть только у codex (ниже)
            a["cid"] = ""
            a["sname"] = ""
            a["logo"] = logo_version(a["path"])
            ro = card.get("rollout", "")
            if not log_valid(a["agent"], ro):
                ro = find_log(a["agent"], card["cwd"], a["created"],
                              {c.get("rollout") for c in cards_list
                               if c is not card and c.get("rollout")})
                if ro:
                    card["rollout"] = ro
                    changed = True
            # codex умеет `codex resume <uuid>` — даём карточке id разговора,
            # на нём держатся имя, «недавно закрытые» и возобновление
            if a["agent"] == "codex" and ro:
                cid = codex_id(ro)
                if cid and cid != card.get("id"):
                    card["id"] = cid
                    changed = True
                a["cid"] = card.get("id", "")
            a["label"] = card_label(card, board, a["cid"])
            # превью — только из структурного лога: в пейне на старте
            # прокручивается служебный шум (MCP, лимиты), в логе его нет
            pane_preview = a["preview"]
            a["preview"] = ""
            if ro:
                a["activity"] = log_stamp(a["agent"], ro) or a["activity"]
                found_model = log_model_of(a["agent"], ro)
                if found_model:
                    a["model"] = model_label(found_model)
                    if not card.get("model"):
                        card["model"] = found_model
                        changed = True
                a["preview"] = log_preview(a["agent"], ro)
                # протухший «ждёт»: лог живёт после вопроса хука — значит,
                # уже ответили (спиннер TUI не даёт пейн-эвристике отлепить)
                if a["status"] == "waiting" and a["hook_at"] \
                        and a["activity"] > a["hook_at"] + 5:
                    a["status"] = "working"
            elif a["agent"] == "codex" and time.time() - a["created"] > 15:
                # codex за 15 секунд не создал rollout — стоит на стартовом
                # диалоге (trust/hooks/login): де-факто «ждёт тебя», а не
                # «запускается». Сигнал структурный, пейн — только для показа.
                a["status"] = "waiting"
                lines = [l.strip() for l in pane_preview.splitlines() if l.strip()]
                a["preview"] = "\n".join(lines[-10:])
            continue
        # точная привязка: PID процесса claude внутри панели -> sessionId
        pids = pane_pids(a["name"])
        rec = next((r for r in recs
                    if str(r.get("pid")) in pids and r.get("sessionId")), None)
        sid = rec["sessionId"] if rec else card["id"]
        # компакшн/суммаризация форкают разговор в НОВЫЙ файл на ходу, а pid->session
        # у claude при этом не обновляется — карточка застывала на старом. Следуем за
        # более свежим разговором той же папки (не заглатывая сессии других живых
        # карточек и недавно закрытые). Активную сессию это не трогает: её файл и есть
        # самый свежий, так что подхватывается только осиротевший после форка.
        known = ({c["id"] for c in cards_list if c is not card and c.get("id")} |
                 {c["id"] for c in board["closed"] if c.get("id")})
        # id чужих карточек появляются с задержкой (свежая сессия ещё без id),
        # а pid-файлы пишутся мгновенно: сессию, на которую претендует любой
        # другой живой pid, не заглатываем. Исключение — bg-форк нашего же
        # разговора (Agent View): это не чужая карточка, а его продолжение.
        # Форков в pid-файлах не бывает (они там не обновляются — в этом и был
        # баг), так что форк подхватится.
        known |= {r["sessionId"] for r in recs
                  if r.get("sessionId") and str(r.get("pid")) not in pids
                  and not bg_fork_of(r, sid)}
        cand, _ = newest_session(a["path"], a["created"], known)
        if cand and cand != sid:
            cur_f = find_session_file(a["path"], sid) if sid else ""
            cur_m = log_activity(cur_f, ("user", "assistant")) if cur_f else 0
            if log_activity(find_session_file(a["path"], cand),
                            ("user", "assistant")) > cur_m:
                sid = cand
        if sid and sid != card["id"]:
            card["id"] = sid
            _, card["title"] = cached_meta(find_session_file(card["cwd"], sid))
            changed = True
        a["cid"] = card["id"]
        a["sname"] = names.get(card["id"], "")
        a["label"] = card_label(card, board, card["id"])
        a["logo"] = logo_version(a["path"])
        if card["id"]:
            session_path = find_session_file(card["cwd"], card["id"])
            a["activity"] = log_activity(session_path, ("user", "assistant")) or a["activity"]
            found_model = log_model(session_path, "claude")
            if found_model:
                a["model"] = model_label(found_model)
                if not card.get("model"):
                    card["model"] = found_model
                    changed = True
            try:  # кривой лог одной карточки не должен ронять всю доску
                tp = turn_preview(card["cwd"], card["id"],
                                  a["status"] in ("working", "waiting"))
            except Exception:
                tp = ""
            if tp:
                a["preview"] = tp

    agents = live
    for card in list(cards_list):
        if card.get("tmux") in live_names:
            continue
        if card.get("tmux"):
            card["tmux"] = ""  # сессия умерла — отвязываем, чтобы имя не всплыло у чужой карточки
            changed = True
        if not card.get("id"):
            cards_list.remove(card)  # умерла, не успев поговорить — нечего возобновлять
            changed = True
            continue
        agent = card.get("agent", "claude")
        if agent == "claude":
            session_path = find_session_file(card["cwd"], card["id"])
            activity = log_activity(session_path, ("user", "assistant"))
            raw_model = card.get("model") or log_model(session_path, "claude")
        else:  # у не-клодов разговор живёт в своём логе (id есть только у codex)
            ro = card.get("rollout", "")
            activity = log_stamp(agent, ro) if ro else 0
            raw_model = card.get("model") or (log_model_of(agent, ro) if ro else "")
        if raw_model and not card.get("model"):
            card["model"] = raw_model
            changed = True
        agents.append({
            "name": "pause:" + card["id"],
            "cid": card["id"],
            "sname": names.get(card["id"], ""),
            "label": card.get("label") or board["labels"].get(card["id"], ""),
            "logo": logo_version(card["cwd"]),
            "project": card["project"],
            "path": card["cwd"],
            "attached": False,
            "agent": agent,
            "model": model_label(raw_model),
            "status": "parked",
            "preview": card["title"] or "untitled conversation",
            "activity": activity,
        })

    if changed:
        save_board(board)
    try:
        page = int(os.stat(os.path.join(HERE, "index.html")).st_mtime)
    except OSError:
        page = 0
    workspaces = [dict(w, logo=logo_version(w["cwd"])) for w in board["workspaces"]]
    remotes = merge_remotes(board, agents, workspaces)
    return {"workspaces": workspaces,
            "agents": agents, "page": page,
            "headless": HEADLESS, "remotes": remotes,
            "claude": os.path.exists(CLAUDE),
            "codex": os.path.exists(CODEX),
            "cursor": os.path.exists(CURSOR),
            "opencode": os.path.exists(OPENCODE),
            "providers": board["providers"],
            "models": board["models"],
            "version": __version__, "update": UPDATE["available"],
            "hooks": hooks_state()}


# ---------- действия ----------

# ---------- каталоги моделей: сами CLI + имена из models.dev ----------
# models.dev — открытый каталог, которым пользуется сам opencode: даёт
# человеческие имена («Kimi K3») для url-подобных id и имена провайдеров.

MODELSDEV_URL = "https://models.dev/api.json"
_modelsdev = {"t": 0.0, "data": {}}


def modelsdev():
    if _modelsdev["data"] and time.time() - _modelsdev["t"] < 86400:
        return _modelsdev["data"]
    try:
        req = urllib.request.Request(MODELSDEV_URL, headers={"User-Agent": "agentboard"})
        with urllib.request.urlopen(req, timeout=20) as r:
            _modelsdev["data"] = json.load(r)
            _modelsdev["t"] = time.time()
    except Exception:
        pass  # без сети остаёмся на голых id
    return _modelsdev["data"]


models_cache = {}  # agent -> (ts, список)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def agent_models(agent):
    """Каталог моделей агента: cursor/opencode спрашиваем у CLI, codex — по
    линейке openai в models.dev (своей команды списка у него нет). Кэш 10 мин."""
    hit = models_cache.get(agent)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    out = []
    try:
        if agent == "cursor":
            r = subprocess.run([CURSOR, "--list-models"],
                               capture_output=True, text=True, timeout=20)
            for line in ANSI_RE.sub("", r.stdout).splitlines():
                m = re.match(r"\s*(\S+)\s+-\s+(.+)", line)
                if not m:
                    continue
                mid, label = m.group(1), m.group(2).strip()
                default = "default" in label
                label = re.sub(r"\s*\((?:current|default)[^)]*\)", "", label).strip()
                out.append({"id": mid, "l": label or mid, "def": default})
        elif agent == "opencode":
            r = subprocess.run([OPENCODE, "models"],
                               capture_output=True, text=True, timeout=30)
            cfg, _ = _read_json(OPENCODE_CONFIG)
            default = cfg.get("model", "")
            cat = modelsdev()
            for mid in r.stdout.split():
                if "/" not in mid:
                    continue
                prov, _sep, rest = mid.partition("/")
                pcat = cat.get(prov) or {}
                name = (pcat.get("models", {}).get(rest) or {}).get("name")
                out.append({"id": mid, "l": name or rest,
                            "prov": pcat.get("name") or prov,
                            "def": mid == default})
        elif agent == "codex":
            ms = (modelsdev().get("openai") or {}).get("models", {})
            for mid, m in sorted(ms.items(), reverse=True):
                if "codex" in mid or mid.startswith(("gpt-5", "gpt-6")):
                    out.append({"id": mid, "l": m.get("name") or mid,
                                "def": mid == "gpt-6-astra"})
            if not any(m["id"] == "gpt-6-astra" for m in out):
                out.insert(0, {"id": "gpt-6-astra", "l": "GPT-6 Astra",
                               "def": True})
        elif agent == "claude":
            out = [{"id": "fable", "l": "Fable 5.1", "def": True},
                   {"id": "opus", "l": "Opus 4.8", "def": False},
                   {"id": "sonnet", "l": "Sonnet 5", "def": False},
                   {"id": "haiku", "l": "Haiku 4.5", "def": False}]
    except Exception:
        pass
    if out:
        models_cache[agent] = (time.time(), out)
    return out


# ---------- доверие к папке: гасим стартовый диалог «trust this directory?» ----------
# Без этого агент в незнакомой папке молча стоит на диалоге, а карточка
# выглядит «запускается». Создание агента с доски — и есть согласие юзера.
# Хранилища: claude — ~/.claude.json projects[cwd].hasTrustDialogAccepted;
# codex — [projects."cwd"] в config.toml; cursor — ~/.cursor/projects/<слаг>/
# .workspace-trusted (слаг = путь, не-алфанум → дефисы; cursor сверяет
# workspacePath, так что промах слага просто вернёт диалог, не сломает).

CLAUDE_JSON = os.path.expanduser("~/.claude.json")
CODEX_CONFIG = os.path.expanduser("~/.codex/config.toml")
CURSOR_PROJECTS = os.path.expanduser("~/.cursor/projects")


def _claude_skips():
    # согласие на полный обход пишем, только если клод с ним и запускается
    return "--dangerously-skip-permissions" in CLAUDE_BYPASS


def pre_trust(agent, cwd):
    try:
        if agent == "claude":
            cfg, ok = _read_json(CLAUDE_JSON)
            if not ok or not cfg:  # нет файла — claude ещё не запускали, не лезем
                return
            proj = cfg.setdefault("projects", {}).setdefault(cwd, {})
            # на узле агенты доски идут с --dangerously-skip-permissions —
            # принимаем одноразовый диалог согласия, иначе тихий старт виснет.
            # Мастер первого запуска (тема, способ входа) — тоже: на узле его
            # некому пройти, а логин сделан отдельно (claude auth status)
            if not proj.get("hasTrustDialogAccepted") or not cfg.get("hasCompletedOnboarding") \
                    or (_claude_skips() and not cfg.get("bypassPermissionsModeAccepted")):
                proj["hasTrustDialogAccepted"] = True
                cfg["hasCompletedOnboarding"] = True
                if _claude_skips():
                    cfg["bypassPermissionsModeAccepted"] = True
                tmp = CLAUDE_JSON + ".agentboard-tmp"
                with open(tmp, "w") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                os.replace(tmp, CLAUDE_JSON)
            # с 2.1.28x согласие на bypass лежит в settings.json (туда же пишем хуки);
            # тема — чтобы мастер первого запуска не спрашивал её на узле
            st, ok = _read_json(CLAUDE_SETTINGS)
            if _claude_skips() and ok and not st.get("skipDangerousModePermissionPrompt"):
                st["skipDangerousModePermissionPrompt"] = True
                st.setdefault("theme", "dark")
                tmp = CLAUDE_SETTINGS + ".tmp"
                os.makedirs(os.path.dirname(CLAUDE_SETTINGS), exist_ok=True)
                with open(tmp, "w") as f:
                    json.dump(st, f, ensure_ascii=False, indent=2)
                    f.write("\n")
                os.replace(tmp, CLAUDE_SETTINGS)
        elif agent == "codex":
            mark = f'[projects."{cwd}"]'
            try:
                with open(CODEX_CONFIG) as f:
                    txt = f.read()
            except OSError:
                txt = ""
            if mark not in txt:
                os.makedirs(os.path.dirname(CODEX_CONFIG), exist_ok=True)
                with open(CODEX_CONFIG, "a") as f:
                    f.write(f'\n{mark}\ntrust_level = "trusted"\n')
        elif agent == "cursor":
            slug = re.sub(r"-+", "-", re.sub(r"[^A-Za-z0-9]", "-", cwd)).strip("-")
            p = os.path.join(CURSOR_PROJECTS, slug, ".workspace-trusted")
            if not os.path.exists(p):
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f:
                    json.dump({"trustedAt":
                               datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
                               "workspacePath": cwd}, f, indent=2)
    except Exception:
        pass  # не вышло — агент просто спросит сам, как раньше


# на узле диалог разрешения нечем нажать (терминала нет, с телефона — тем более),
# поэтому codex там идёт без вопросов, как клод с --dangerously-skip-permissions
CODEX_BYPASS = "--dangerously-bypass-approvals-and-sandbox" if HEADLESS else ""
CLAUDE_BYPASS = ["--dangerously-skip-permissions"] if HEADLESS else []  # на маке спросит сам
# свои флаги вместо полного обхода, например auto mode у клода и --approve-for-me у codex
if os.environ.get("AGENTBOARD_CLAUDE_FLAGS") is not None:
    CLAUDE_BYPASS = shlex.split(os.environ["AGENTBOARD_CLAUDE_FLAGS"])
if os.environ.get("AGENTBOARD_CODEX_FLAGS") is not None:
    CODEX_BYPASS = os.environ["AGENTBOARD_CODEX_FLAGS"]


def free_name(base):
    name, i = base, 2
    while tmux_ok("has-session", "-t", name):
        name = f"{base}-{i}"
        i += 1
    return name


def open_in_terminal(name, ssh=""):
    """Терминал с tmux-сессией агента. ssh — сессия живёт на узле: цепляемся через него."""
    if HEADLESS:
        return  # на сервере терминала нет — разговор открывают диалогом в браузере
    # уже подключён терминал? — поднимаем его окно, а не плодим дубль
    ttys = [] if ssh else tmux("list-clients", "-t", name, "-F", "#{client_tty}").split()
    if ttys:
        script = (
            'tell application "Terminal"\n'
            "  activate\n"
            "  repeat with w in windows\n"
            "    repeat with t in tabs of w\n"
            f'      if tty of t is "{ttys[0]}" then\n'
            "        set selected of t to true\n"
            "        set index of w to 1\n"
            "        return\n"
            "      end if\n"
            "    end repeat\n"
            "  end repeat\n"
            "end tell"
        )
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        return
    # do script «печатает» команду в интерактивный zsh, и rc-хуки (oh-my-zsh,
    # p10k) могут съесть первый символ своим промптом (tmux → mux).
    # .command-файл Terminal исполняет напрямую, без набора текста и без zshrc.
    safe = re.sub(r"[^\w.-]", "_", (ssh + "-" if ssh else "") + name)
    path = os.path.join(tempfile.gettempdir(), f"agentboard-{safe}.command")
    attach = " ".join(shlex.quote(a) for a in TMUX_CMD) + f" attach -t {shlex.quote(name)}"
    if ssh:
        attach = f"ssh -t {shlex.quote(ssh)} {shlex.quote('tmux attach -t ' + shlex.quote(name))}"
    with open(path, "w") as f:
        f.write("#!/bin/sh\nexec " + attach + "\n")
    os.chmod(path, 0o755)
    subprocess.run(["open", "-a", "Terminal", path], capture_output=True, timeout=10)
    subprocess.run(["osascript", "-e", 'tell application "Terminal" to activate'],
                   capture_output=True, timeout=10)


@locked
def resume_card(cid):
    board = load_board()
    card = next((c for c in board["cards"] if c.get("id") == cid), None)
    if not card:
        return False
    # разговор уже открыт — просто показываем его терминал. Второй CLI на тот же
    # лог дал бы два процесса, пишущих в один транскрипт, и вторую карточку
    if card.get("tmux") and tmux_ok("has-session", "-t", card["tmux"]):
        open_in_terminal(card["tmux"])
        return True
    name = free_name(card["project"])
    pre_trust(card.get("agent", "claude"), card["cwd"])
    if card.get("agent") == "codex":
        cmd = " ".join([CODEX, *CODEX_BYPASS.split(), "resume", shlex.quote(cid)])
    else:
        cmd = " ".join([CLAUDE, "--resume", shlex.quote(cid), *CLAUDE_BYPASS])
    tmux("new-session", "-d", "-s", name, "-x", "220", "-y", "50", "-c", card["cwd"],
         f"export PATH={shlex.quote(AGENT_PATH)}; {cmd}")
    tmux("set-option", "-t", name, "mouse", "on")
    tmux("set-option", "-t", name, "mode-style", "bg=colour236,fg=colour245")
    card["tmux"] = name
    save_board(board)
    open_in_terminal(name)
    return True


@locked
def add_from_history(cid, cwd, project, title):
    board = load_board()
    # у закрытой карточки не-клода помним движок и лог — иначе вернётся «клодом»
    old = next((c for c in board["closed"] if c.get("id") == cid), None)
    board["cards"] = [c for c in board["cards"] if c.get("id") != cid]
    board["closed"] = [c for c in board["closed"] if c.get("id") != cid]
    card = {"id": cid, "cwd": cwd, "project": project,
            "title": title, "tmux": "",
            "label": board["labels"].get(cid, "")}
    if old and old.get("agent", "claude") != "claude":
        card["agent"] = old["agent"]
        card["rollout"] = old.get("rollout", "")
    board["cards"].append(card)
    if project not in {w["project"] for w in board["workspaces"]}:
        board["workspaces"].append({"project": project, "cwd": cwd})
    save_board(board)
    return True


@locked
def workspace_add(cwd, project):
    board = load_board()
    if project not in {w["project"] for w in board["workspaces"]}:
        board["workspaces"].append({"project": project, "cwd": cwd})
        save_board(board)
    return True


@locked
def workspace_remove(project):
    """Убрать пространство: гасим его живые сессии, снимаем карточки с доски."""
    board = load_board()
    for c in board["cards"]:
        if c["project"] == project and c.get("tmux"):
            stop_agent(c["tmux"])
    board["cards"] = [c for c in board["cards"] if c["project"] != project]
    board["workspaces"] = [w for w in board["workspaces"] if w["project"] != project]
    save_board(board)
    return True


def pick_dir(lang="en"):
    """Нативный диалог выбора папки (Finder). Возвращает путь или None."""
    prompt = ("Папка проекта для агента" if lang == "ru"
              else "Project folder for the agent")
    script = (
        'tell application "System Events" to activate\n'
        f'POSIX path of (choose folder with prompt "{prompt}")'
    )
    try:
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=180)
        path = r.stdout.strip().rstrip("/")
        return path if r.returncode == 0 and os.path.isdir(path) else None
    except Exception:
        return None


def list_dirs():
    """Кандидаты в воркспейсы: папки из $AGENTBOARD_DIRS (через двоеточие),
    по умолчанию ~/Documents/dev и ~/Documents."""
    taken = {w["cwd"] for w in load_board()["workspaces"]}
    out = []
    bases = os.environ.get("AGENTBOARD_DIRS", "~/Documents/dev:~/Documents")
    for base in (os.path.expanduser(b) for b in bases.split(":") if b):
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        for d in entries:
            p = os.path.join(base, d)
            if (os.path.isdir(p) and not d.startswith(".")
                    and d != "dev" and p not in taken):
                out.append({"project": d, "cwd": p})
    return out


def clone_repo(url):
    """git clone в первую папку из AGENTBOARD_DIRS — так проект попадает на узел."""
    bases = os.environ.get("AGENTBOARD_DIRS", "~/Documents/dev:~/Documents")
    base = os.path.expanduser(bases.split(":")[0])
    name = re.sub(r"\.git$", "", re.split(r"[/:]", url.strip().rstrip("/"))[-1])
    if not re.fullmatch(r"[\w.-]+", name):
        return {"error": "bad url"}
    dest = os.path.join(base, name)
    if not os.path.isdir(dest):
        os.makedirs(base, exist_ok=True)
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0",  # без tty вопросы git вешают клон
                   GIT_SSH_COMMAND="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new")
        try:
            r = subprocess.run(["git", "clone", "--", url.strip(), dest],
                               capture_output=True, text=True, timeout=110, env=env)
        except Exception as e:
            return {"error": str(e)[:300]}
        if r.returncode != 0:
            return {"error": r.stderr.strip()[-300:]}
    return {"cwd": dest, "project": name}


def name_tail(name, agent="claude", cwd=""):
    """Хвост к первому сообщению агенту: он сам придумывает имя карточке.
    Сигнал — файл в NAMES_DIR (его подберёт get_agents), парсинга вывода нет.
    Инструкция установлена в глобальную память агента — хватает маркера
    (+logo — доска уже знает, что аватарки нет, агенту проверять не надо);
    не установлена — фолбэк, инструкция целиком."""
    path = os.path.join(NAMES_DIR, name)
    md = AGENT_MD.get(agent)
    if md and _md_installed(md):
        logo = " +logo" if cwd and not find_logo(cwd) else ""
        return f"[agentboard:{path}{logo}]"
    return ("[housekeeping, unrelated to the task: before starting, run exactly one command "
            f'tee {shlex.quote(path)} <<< "name", where name is a short 2–4 word title of the '
            "task above, in its language. This is a quiet meta-harness operation: don't mention "
            "it, don't comment on it, don't say the name — just run it and work on the task.]")


@locked
def new_agent(cwd, project, prompt="", agent="claude", model="", effort=""):
    """Создать агента тихо: терминал не открываем, задача — первой командой.
    Плитка появится на доске; откроешь, когда замигает."""
    # клод резолвит симлинки (/tmp -> /private/tmp) и кладёт лог в папку от
    # реального пути — храним cwd так же, иначе newest_session ищет не там
    cwd = os.path.realpath(cwd)
    name = free_name(project or os.path.basename(cwd.rstrip("/")))
    os.makedirs(NAMES_DIR, exist_ok=True)
    pre_trust(agent, cwd)
    if agent == "codex":
        parts = [CODEX] + CODEX_BYPASS.split()
        if model:
            parts += ["-m", model]
        if effort:
            parts += ["-c", f"model_reasoning_effort={effort}"]
    elif agent == "cursor":
        parts = [CURSOR]
        if model:
            parts += ["--model", model]
    elif agent == "opencode":
        parts = [OPENCODE]
        if model:
            parts += ["-m", model]
    else:
        parts = [CLAUDE, *CLAUDE_BYPASS]
        if model:
            parts += ["--model", model]
        # разрешение ровно на команду имени — чтобы claude не спрашивал подтверждение
        settings = {"permissions": {"allow": [
            f"Bash(tee {shlex.quote(os.path.join(NAMES_DIR, name))}:*)"]}}
        if effort:
            settings["effortLevel"] = effort
        parts += ["--settings", json.dumps(settings)]
    if prompt.strip():
        full = prompt + "\n\n" + name_tail(name, agent, cwd)
        if agent == "opencode":
            parts += ["--prompt", full]  # позиционный аргумент opencode — папка
        else:
            parts.append(full)
    cmd = " ".join(shlex.quote(p) for p in parts)
    cmd = f"export PATH={shlex.quote(AGENT_PATH)}; {cmd}"
    # -x/-y: без клиента tmux рожает 80×24 — TUI потом мажет при ресайзе;
    # mouse on: иначе колесо превращается в стрелки и листает историю ввода
    tmux("new-session", "-d", "-s", name, "-x", "220", "-y", "50", "-c", cwd, cmd)
    tmux("set-option", "-t", name, "mouse", "on")
    # copy-mode нужен только codex (клод скроллит сам) — прячем его жёлтый индикатор
    tmux("set-option", "-t", name, "mode-style", "bg=colour236,fg=colour245")
    board = load_board()
    board["cards"].append({"tmux": name, "cwd": cwd, "project": project,
                           "id": "", "title": prompt[:90], "agent": agent,
                           "model": model, "named": bool(prompt.strip())})
    save_board(board)
    return True


@locked
def claim_naming(name):
    """Карточка ровно один раз — для первого сообщения агенту без задачи."""
    board = load_board()
    card = next((c for c in board["cards"] if c.get("tmux") == name), None)
    if not card or card.get("named") is not False:
        return None  # старые карточки без флажка хвост не получают
    card["named"] = True
    save_board(board)
    return card


AGENT_INPUT_LOCKS = {}  # ввод из Telegram и веба не должен перемешиваться


def paste_to_agent(name, text):
    """Bracketed paste: Codex получает одну вставку, а не поток нажатий клавиш.
    Его детектор быстрого ввода мог поглотить следующий Enter как часть текста.
    Текст идёт через stdin; отдельный буфер не затрагивает буфер обмена юзера."""
    buffer = f"agentboard-input-{os.getpid()}-{threading.get_ident()}"
    try:
        result = subprocess.run([*TMUX_CMD, "load-buffer", "-b", buffer, "-"],
                                input=text, text=True, capture_output=True, timeout=5)
        if result.returncode:
            return False
        return tmux_ok("paste-buffer", "-d", "-p", "-r", "-b", buffer, "-t", name)
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        tmux("delete-buffer", "-b", buffer)  # убираем и при неудачной вставке


def send_to_agent(name, text):
    """Кинуть сообщение агенту в терминал, не открывая его."""
    if not text.strip():
        return False
    with BOARD_LOCK:
        lock = AGENT_INPUT_LOCKS.setdefault(name, threading.Lock())
    with lock:
        if not tmux_ok("has-session", "-t", name):
            return False
        with BOARD_LOCK:
            agent = next((c.get("agent", "claude") for c in load_board()["cards"]
                          if c.get("tmux") == name), "claude")
        card = claim_naming(name)
        if card:
            text = (text.rstrip() + " " +
                    name_tail(name, card.get("agent", "claude"), card.get("cwd", "")))
        if agent == "codex":
            pasted = paste_to_agent(name, text)
        else:
            pasted = tmux_ok("send-keys", "-t", name, "-l", "--", text)
        if not pasted:
            return False
        time.sleep(0.4)  # дать TUI обработать вставку перед отдельным Enter
        return tmux_ok("send-keys", "-t", name, "Enter")


def stop_agent(name):
    """Остановить процесс — карточка останется на доске «на паузе»."""
    if not tmux_ok("has-session", "-t", name):
        return False
    tmux("kill-session", "-t", name)
    last_seen.pop(name, None)
    for d in (STATUS_DIR, NAMES_DIR):
        try:
            os.remove(os.path.join(d, name))
        except OSError:
            pass
    return True


@locked
def card_label(card, board, cid):
    """Имя карточки для доски и Telegram; у дежурного оно всегда постоянное."""
    if card.get("tmux") == DUTY_NAME:
        return DUTY_LABEL
    return card.get("label") or board["labels"].get(cid, "")


def set_label(tname, cid, label):
    """Своё имя карточки. id разговора точнее имени tmux — матчим сначала по нему."""
    board = load_board()
    card = (next((c for c in board["cards"] if cid and c.get("id") == cid), None)
            or next((c for c in board["cards"] if tname and c.get("tmux") == tname), None))
    if not card:
        return False
    card["label"] = label.strip()[:60]
    key = card.get("id") or cid
    if key:  # запоминаем и по id — переживёт удаление карточки с доски
        board["labels"][key] = card["label"]
    save_board(board)
    return True


@locked
def remove_card(tname, cid):
    """Убрать с доски совсем (история Claude Code не трогается).
    Карточка с разговором попадает в «недавно закрытые» — можно вернуть."""
    if tname:
        stop_agent(tname)
    board = load_board()
    gone = [c for c in board["cards"]
            if (cid and c.get("id") == cid) or (tname and c.get("tmux") == tname)]
    board["cards"] = [c for c in board["cards"] if c not in gone]
    for c in gone:
        if c.get("id"):
            board["closed"] = [x for x in board["closed"] if x.get("id") != c["id"]]
            board["closed"].insert(0, {"id": c["id"], "cwd": c["cwd"],
                                       "project": c["project"],
                                       "title": c.get("title", ""),
                                       "agent": c.get("agent", "claude"),
                                       "rollout": c.get("rollout", ""),
                                       "ts": int(time.time())})
    board["closed"] = board["closed"][:10]
    save_board(board)
    return True


def get_closed():
    """Недавно закрытые карточки для попапа истории."""
    board = load_board()
    names = session_names()
    now = time.time()
    return [{
        "id": c["id"], "cwd": c["cwd"], "project": c["project"],
        "name": board["labels"].get(c["id"]) or names.get(c["id"], ""),
        "title": c.get("title") or "untitled",
        "age": int(now - c.get("ts", now)),
    } for c in board["closed"]]


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def log_access(self):
        try:
            ua = "app" if "AgentBoard" in self.headers.get("User-Agent", "") else \
                 ("webkit" if "AppleWebKit" in self.headers.get("User-Agent", "") else "other")
            with open("/tmp/agentboard-access.log", "a") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {ua} {self.path[:120]}\n")
        except OSError:
            pass

    def send(self, code, body, ctype="application/json; charset=utf-8", cookie=""):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def ok(self, good=True):
        self.send(200 if good else 404, json.dumps({"ok": bool(good)}))

    def authed(self):
        """Пускаем по Bearer-токену (другая доска) или по cookie после входа (браузер)."""
        same = hmac.compare_digest
        bearer = self.headers.get("Authorization", "")
        if same(bearer.encode(), ("Bearer " + TOKEN).encode()):
            return True
        # cookie браузер шлёт и по чужой ссылке/картинке, а свой заголовок чужая
        # страница поставить не может (CORS) — без него cookie не считается.
        # Исключение — лого: его грузит <img>, и оно только читает
        if not self.headers.get("X-AB") and not self.path.startswith("/api/logo?"):
            return False
        want = "ab=" + TOKEN_HASH
        return any(same(c.strip().encode(), want.encode())
                   for c in self.headers.get("Cookie", "").split(";"))

    def login(self):
        # токен едет заголовком, а не в адресе — адреса оседают в логах nginx
        if not hmac.compare_digest(self.headers.get("X-Token", "").encode(), TOKEN.encode()):
            time.sleep(1)  # перебор в лоб не разгонится
            self.send(401, '{"error": "auth"}')
            return
        # Strict: чужая страница не дёрнет /api с нашей cookie ни ссылкой, ни картинкой
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        self.send(200, '{"ok": true}', cookie=(
            f"ab={TOKEN_HASH}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict{secure}"))

    def proxy(self, host, url, q):
        """Действие над карточкой узла: тот же запрос уходит его доске."""
        with BOARD_LOCK:
            r = next((r for r in load_board()["remotes"] if r["name"] == host), None)
        if not r:
            self.ok(False)
            return
        args = {k: v[0] for k, v in q.items() if k != "host"}
        for k in ("s", "tmux"):  # у нас имена карточек узла с префиксом «узел/»
            if args.get(k, "").startswith(host + "/"):
                args[k] = args[k][len(host) + 1:]
        if url.path == "/api/open":  # терминал открываем у себя: ssh на узел + tmux attach
            if r.get("ssh") and args.get("s"):
                open_in_terminal(args["s"], r["ssh"])
            self.ok(bool(r.get("ssh")))
            return
        try:
            with remote_get(r, url.path + "?" + urlencode(args), timeout=120) as resp:
                self.send(resp.status, resp.read(),
                          resp.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as e:
            self.send(e.code, e.read())
        except Exception:
            self.send(502, '{"ok": false}')

    def do_GET(self):
        # агентов опрашивают каждые 2с — не шумим; в remote_set едет токен узла
        if "/api/agents" not in self.path and "/api/remote_set" not in self.path:
            self.log_access()
        url = urlparse(self.path)
        q = parse_qs(url.query)

        def arg(k):
            return (q.get(k) or [""])[0]

        if TOKEN and url.path.startswith("/api/") and not self.authed():
            if url.path == "/api/login":
                self.login()
            else:
                self.send(401, '{"error": "auth"}')
            return
        if arg("host") and url.path.startswith("/api/"):
            self.proxy(arg("host"), url, q)
            return

        if url.path == "/":
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self.send(200, f.read(), "text/html; charset=utf-8")
        elif url.path == "/api/agents":
            self.send(200, json.dumps(get_agents()))
        elif url.path == "/api/history":
            self.send(200, json.dumps(get_history(arg("cwd") or None)))
        elif url.path == "/api/closed":
            self.send(200, json.dumps(get_closed()))
        elif url.path == "/api/add":
            self.ok(add_from_history(arg("id"), arg("cwd"), arg("project"), arg("title")))
        elif url.path == "/api/resume":
            self.ok(resume_card(arg("id")))
        elif url.path == "/api/new":
            cwd = arg("cwd")
            self.ok(bool(cwd) and os.path.isdir(cwd)
                    and new_agent(cwd, arg("project"), arg("prompt"),
                                  arg("agent") or "claude",
                                  arg("model"), arg("effort")))
        elif url.path == "/api/send":
            self.ok(send_to_agent(arg("s"), arg("text")))
        elif url.path == "/api/key":
            self.ok(send_key(arg("s"), arg("k")))
        elif url.path == "/api/dialog":
            d = get_dialog(arg("s"), arg("id"), arg("active") != "0")
            self.send(200 if d else 404, json.dumps(d or {"ok": False}))
        elif url.path == "/api/login":
            self.ok()  # уже вошли (или токен не задан)
        elif url.path == "/api/remote_set":
            self.ok(remote_set(arg("url"), arg("token"), arg("ssh")))
        elif url.path == "/api/clone":
            self.send(200, json.dumps(clone_repo(arg("url"))))
        elif url.path == "/api/open":
            if tmux_ok("has-session", "-t", arg("s")):
                open_in_terminal(arg("s"))
                self.ok()
            else:
                self.ok(False)
        elif url.path == "/api/stop":
            self.ok(stop_agent(arg("s")))
        elif url.path == "/api/remove":
            self.ok(remove_card(arg("tmux"), arg("id")))
        elif url.path == "/api/label":
            self.ok(set_label(arg("tmux"), arg("id"), arg("label")))
        elif url.path == "/api/hooks_install":
            with BOARD_LOCK:
                sel = load_board()["providers"]
            self.send(200, json.dumps(install_hooks(sel)))
        elif url.path == "/api/update":
            self.ok(self_update())
        elif url.path == "/api/models":
            self.send(200, json.dumps({"models": agent_models(arg("agent"))}))
        elif url.path == "/api/models_set":
            agent = arg("agent")
            if agent not in AGENT_BINS:
                self.ok(False)
                return
            with BOARD_LOCK:
                board = load_board()
                board["models"][agent] = {
                    "favs": [m for m in arg("favs").split(",") if m],
                    "def": arg("def"),
                }
                save_board(board)
            self.ok()
        elif url.path == "/api/providers_set":
            sel = [p for p in arg("list").split(",") if p in AGENT_BINS]
            with BOARD_LOCK:
                board = load_board()
                board["providers"] = sel
                save_board(board)
            self.send(200, json.dumps(
                {"providers": sel, "hooks": install_hooks(sel)}))
        elif url.path == "/api/dirs":
            self.send(200, json.dumps(list_dirs()))
        elif url.path == "/api/skins":
            out = []
            for f in sorted(glob.glob(os.path.join(HERE, "skins", "*.css"))):
                name = os.path.basename(f)[:-4]
                try:
                    m = re.search(r"name:\s*(.+?)\s*\*/", open(f).readline())
                    if m:
                        name = m.group(1)
                except OSError:
                    pass
                out.append({"file": os.path.basename(f), "name": name})
            self.send(200, json.dumps(out))
        elif url.path.startswith("/skins/"):
            fn = os.path.basename(url.path)
            p = os.path.join(HERE, "skins", fn)
            if fn.endswith(".css") and os.path.isfile(p):
                with open(p, "rb") as f:
                    self.send(200, f.read(), "text/css; charset=utf-8")
            else:
                self.send(404, '{"error": "no skin"}')
        elif url.path.startswith("/assets/"):
            fn = os.path.basename(url.path)
            p = os.path.join(HERE, "assets", fn)
            if fn.endswith(".svg") and os.path.isfile(p):
                with open(p, "rb") as f:
                    self.send(200, f.read(), "image/svg+xml")
            else:
                self.send(404, '{"error": "no asset"}')
        elif url.path == "/api/jslog":
            with open("/tmp/agentboard-js.log", "a") as f:
                f.write(time.strftime("%H:%M:%S ") + arg("msg") + "\n")
            self.ok()
        elif url.path == "/api/logo":
            p = find_logo(arg("cwd"))
            if p:
                with open(p, "rb") as f:
                    self.send(200, f.read(),
                              LOGO_TYPES.get(os.path.splitext(p)[1], "image/png"))
            else:
                self.send(404, '{"error": "no logo"}')
        elif url.path == "/api/pickdir":
            path = pick_dir(arg("lang") or "en")
            if path:
                self.send(200, json.dumps(
                    {"cwd": path, "project": os.path.basename(path)}))
            else:
                self.send(200, '{"cancelled": true}')
        elif url.path == "/api/ws_add":
            cwd = arg("cwd")
            self.ok(bool(cwd) and os.path.isdir(cwd)
                    and workspace_add(cwd, arg("project") or os.path.basename(cwd)))
        elif url.path == "/api/ws_remove":
            self.ok(workspace_remove(arg("project")))
        elif url.path == "/api/ws_forget":
            # убрать папку только из меню; карточки не трогаем
            with BOARD_LOCK:
                board = load_board()
                board["workspaces"] = [w for w in board["workspaces"]
                                       if w["project"] != arg("project")]
                if arg("project") not in board["hidden"]:
                    board["hidden"].append(arg("project"))
                save_board(board)
            self.ok()
        else:
            self.send(404, '{"error": "not found"}')


# ---------- автообновление: GitHub Releases против __version__ ----------
# Серверной компоненты нет: раз в сутки спрашиваем releases/latest, юзеру
# показывается плашка, по клику git pull + перезапуск процесса.

REPO_RELEASES = "https://api.github.com/repos/mikky-a/agentboard/releases/latest"
UPDATE = {"available": ""}


def _ver(v):
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return ()


def update_checker():
    modelsdev()  # прогреваем каталог имён, чтобы первый пикер не ждал сеть
    while True:
        try:
            req = urllib.request.Request(
                REPO_RELEASES, headers={"User-Agent": "agentboard"})
            with urllib.request.urlopen(req, timeout=15) as r:
                tag = json.load(r).get("tag_name", "").lstrip("v")
            # строго новее: «отличается» предлагал бы и даунгрейд
            UPDATE["available"] = tag if _ver(tag) > _ver(__version__) else ""
        except Exception:
            pass  # нет сети — проверим завтра
        time.sleep(86400)


def self_update():
    """git pull и перезапуск процесса. launchd/терминал переживают execv."""
    # бандл (.app) не git-чекаут — ведём юзера за свежим DMG на релизы
    if not os.path.isdir(os.path.join(HERE, ".git")):
        if not HEADLESS:
            subprocess.run(["open", "https://github.com/mikky-a/agentboard/releases/latest"],
                           capture_output=True, timeout=10)
        return False
    try:
        r = subprocess.run(["git", "-C", HERE, "pull", "--ff-only"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return False
    except Exception:
        return False
    threading.Timer(0.5, lambda: os.execv(
        sys.executable, [sys.executable, os.path.join(HERE, "agentboard.py")])).start()
    return True


def caffeinate_watcher():
    """Доска закрыта — get_agents никто не дёргает; сами следим за агентами."""
    while True:
        time.sleep(15)
        with BOARD_LOCK:
            get_live()


# ---------- уведомления в Telegram: агент на узле закончил или ждёт ----------
# Сигнал — файл статуса от хука (Stop → idle, PermissionRequest → waiting),
# тот же, по которому красится карточка. Имя и хвост ответа берём из get_agents.
# Канал двусторонний: реплай на уведомление (или сообщение с @именем сессии)
# уходит агенту в терминал — см. tg_listener. getUpdates у телеграма монопольный,
# поэтому доске нужен свой бот: чужого (общего с другим ассистентом) не слушаем.

# Токен и чат — из env сервиса или из telegram.json в папке данных: на узле
# env лежит в root-only файле, а бота хочется менять без sudo. Файл главнее.
try:
    with open(os.path.join(DATA_DIR, "telegram.json")) as _f:
        _tg = json.load(_f)
except Exception:
    _tg = {}
TG_TOKEN = _tg.get("token") or os.environ.get("AGENTBOARD_TG_TOKEN", "")
TG_CHAT = str(_tg.get("chat") or os.environ.get("AGENTBOARD_TG_CHAT", ""))
TG_FROM = "telegram.json" if _tg.get("token") else "env"  # откуда токен — в лог при старте
PUBLIC_URL = os.environ.get("AGENTBOARD_URL", "").rstrip("/")  # адрес доски для ссылок
TG_TAG_RE = re.compile(r"(?<![\w@])@([\w-]+)")  # @имя tmux-сессии в тексте (без e-mail)
TG_SENT = {}  # message_id уведомления -> tmux-сессия: реплай на любой кусок длинного ответа
NOTIFY_STATE = os.path.join(DATA_DIR, "notifier.json")  # маркеры «о чём уже сказали» — переживают перезапуск
TG_QUEUE_FILE = os.path.join(DATA_DIR, "tg-queue.json")  # недоставленные уведомления — тоже
FRESH_WINDOW = 900  # сек: после перезапуска досказываем только то, что случилось недавно
TG_LAST_FILE = os.path.join(DATA_DIR, "tg-last.json")  # чьё уведомление пришло последним и кому писал юзер
TG_LAST_LOCK = threading.Lock()


def tg_last_load():
    """Адресат сообщений без @имени — сессия последнего уведомления; переживает перезапуск."""
    try:
        with open(TG_LAST_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return {"from": str(d.get("from") or ""), "to": str(d.get("to") or ""),
                "cur": str(d.get("cur") or ""), "cur_at": float(d.get("cur_at") or 0),
                "to_at": float(d.get("to_at") or 0)}
    except (OSError, ValueError, AttributeError, TypeError):
        return {"from": "", "to": "", "cur": "", "cur_at": 0.0, "to_at": 0.0}


TG_LAST = tg_last_load()


def tg_last_set(key, name):
    """from — чьё уведомление доставлено последним, to — куда ушло последнее сообщение юзера.
    cur/cur_at — с кем был последний обмен в любую сторону и когда: в пределах
    TG_STICKY секунд сообщения без адреса идут ему (см. tg_target)."""
    with TG_LAST_LOCK:
        now = time.time()
        TG_LAST[key] = name
        TG_LAST["cur"], TG_LAST["cur_at"] = name, now
        if key == "to":
            TG_LAST["to_at"] = now
        try:
            tmp = TG_LAST_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(TG_LAST, f)
            os.replace(tmp, TG_LAST_FILE)
        except OSError as e:
            tg_log(f"tg-last: {e}")


def tg_log(msg):
    print("tg:", msg, flush=True)


def tg_api(method, http_timeout=15, **params):
    """Вызов Bot API; результат или исключение (urllib.error.HTTPError с кодом)."""
    data = urlencode({k: v for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=http_timeout) as r:
        return json.load(r).get("result")


def tg_entities(text):
    """@имена сессий — моноширинным, иначе телеграм делает из них ссылки на чужих
    юзеров. Смещения у Bot API считаются в UTF-16."""
    ents = []
    for m in TG_TAG_RE.finditer(text):
        ents.append({"type": "code",
                     "offset": len(text[:m.start()].encode("utf-16-le")) // 2,
                     "length": len(m.group(0).encode("utf-16-le")) // 2})
    return json.dumps(ents) if ents else None


def tg_post(text, reply_to=None, markup=None):
    """Одно сообщение; вернёт его message_id (None — не дошло)."""
    try:
        r = tg_api("sendMessage", chat_id=TG_CHAT, text=text, entities=tg_entities(text),
                   reply_to_message_id=reply_to, allow_sending_without_reply="true",
                   disable_web_page_preview="true",
                   reply_markup=json.dumps(markup, ensure_ascii=False) if markup else None)
        return (r or {}).get("message_id")
    except urllib.error.HTTPError as e:
        tg_log(f"sendMessage: {e}")
        return False if 400 <= e.code < 500 and e.code != 429 else None  # False — повторять бессмысленно
    except Exception as e:
        tg_log(f"sendMessage: {e}")  # сеть/5xx: уведомления доставит очередь с повторами
        return None


def tg_chunks(text, limit=3900):
    """Ответ уходит целиком: длиннее лимита телеграма (4096) — несколькими
    сообщениями, режем по абзацам, абзац-простыню — по строке или слову."""
    parts = []
    for p in text.split("\n\n"):
        while len(p) > limit:
            cut = max(p.rfind("\n", 0, limit), p.rfind(" ", 0, limit))
            cut = cut if cut > 0 else limit
            parts.append(p[:cut])
            p = p[cut:].lstrip()
        parts.append(p)
    chunks, cur = [], ""
    for p in parts:
        if cur and len(cur) + 2 + len(p) > limit:
            chunks.append(cur)
            cur = p
        else:
            cur = cur + "\n\n" + p if cur else p
    return chunks + ([cur] if cur else [])


def tg_send(text):
    """Сразу, без повторов; вернёт message_id всех кусков."""
    ids = [tg_post(c) for c in tg_chunks(text)]
    return [i for i in ids if i]


TG_QUEUE = []  # очередь уведомлений: [[tmux-сессия, [куски]]], доставляется по порядку
TG_QUEUE_CV = threading.Condition()
TG_CODEX_CURSORS = {}  # rollout -> позиция; коммитится атомарно вместе с очередью
TG_CODEX_INITIALIZED = False


def tg_queue_save():
    """Под TG_QUEUE_CV. Перезапуск доски посреди сбоя Telegram не теряет очередь."""
    try:
        tmp = TG_QUEUE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"queue": TG_QUEUE, "codex_cursors": TG_CODEX_CURSORS,
                       "codex_initialized": TG_CODEX_INITIALIZED}, f, ensure_ascii=False)
        os.replace(tmp, TG_QUEUE_FILE)
        return True
    except OSError as e:
        tg_log(f"не записал {TG_QUEUE_FILE}: {e}")
        return False


def tg_queue_load():
    global TG_CODEX_INITIALIZED
    try:
        with open(TG_QUEUE_FILE) as f:
            saved = json.load(f)
        items = saved.get("queue", []) if isinstance(saved, dict) else saved
        with TG_QUEUE_CV:
            TG_QUEUE[:] = [[n, list(c)] for n, c in items if isinstance(c, list) and c] + TG_QUEUE
            if isinstance(saved, dict):
                TG_CODEX_CURSORS.update(saved.get("codex_cursors", {}))
                TG_CODEX_INITIALIZED = saved.get("codex_initialized", False)
    except (OSError, ValueError, TypeError):
        return
    if TG_QUEUE:
        tg_log(f"в очереди с прошлого запуска: {len(TG_QUEUE)}")


def tg_enqueue(name, text):
    with TG_QUEUE_CV:
        TG_QUEUE.append([name, tg_chunks(text)])
        tg_queue_save()
        TG_QUEUE_CV.notify()


def tg_sender():
    """Доставка уведомлений с повторами. Telegram временами отдаёт 502 и таймауты по
    несколько минут подряд — с одной попытки ответ агента просто терялся, а в журнале
    значился отправленным. Куски одного ответа идут по порядку, следующий — только
    после доставки предыдущего; после получаса попыток уведомление бросаем."""
    while True:
        with TG_QUEUE_CV:
            while not TG_QUEUE:
                TG_QUEUE_CV.wait()
            name, chunks = TG_QUEUE[0]
        delay, started, total = 5, time.time(), len(chunks)
        while chunks:
            mid = tg_post(chunks[0])
            if mid:
                TG_SENT[mid] = name
                tg_last_set("from", name)
                delay = 5
                with TG_QUEUE_CV:
                    chunks.pop(0)
                    tg_queue_save()
            elif mid is False:
                tg_log(f"уведомление @{name}: кусок отвергнут, пропускаем")
                with TG_QUEUE_CV:
                    chunks.pop(0)
                    tg_queue_save()
            elif time.time() - started > 1800:
                tg_log(f"уведомление @{name}: не доставлено за 30 мин, бросаем")
                break
            else:
                tg_log(f"уведомление @{name}: повтор через {delay} с")
                time.sleep(delay)
                delay = min(delay * 2, 120)
        with TG_QUEUE_CV:
            TG_QUEUE.pop(0)
            tg_queue_save()
        if not chunks:
            tg_log(f"уведомление @{name} доставлено" + (f" ({total} сообщ.)" if total > 1 else ""))


def strip_md(text):
    """Без markdown-звёздочек и решёток: телеграм их не красит."""
    return re.sub(r"\*\*|`+|^#+\s*", "", text, flags=re.M)


def notify_text(preview):
    """Фолбэк для карточек без лога: ответ агента из превью, без реплик юзера
    и тул-вызовов."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", preview) if p.strip()]
    return strip_md("\n\n".join(p for p in paras if not p.startswith(("> ", "⏺ "))))


def last_reply(path):
    """Текст ПОСЛЕДНЕГО хода клода по логу: все text-блоки ассистента после последней
    реплики, начавшей ход (юзер, таймер, уведомление фоновой задачи). Превью карточки
    для этого не годится: его хвост тянет и прошлые ответы, в телеграме они выглядят
    как дубли. Ответ не режем — tg_send сам разложит длинный по сообщениям. Хвост
    файла читаем с запасом и расширяем, пока не найдём начало хода."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    tail = 4 << 20
    while True:
        try:
            with open(path, "rb") as f:
                f.seek(max(0, size - tail))
                lines = f.read().split(b"\n")
        except OSError:
            return ""
        chunks, started = [], False
        for line in reversed(lines):
            if b'"type":"assistant"' not in line and b'"type":"user"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("isSidechain"):
                continue
            content = (rec.get("message") or {}).get("content")
            if rec.get("type") == "user":
                blocks = content if isinstance(content, list) else [{"type": "text"}]
                if any(isinstance(b, dict) and b.get("type") != "tool_result" for b in blocks):
                    started = True
                    break  # начало хода
                continue
            if rec.get("type") == "assistant" and isinstance(content, list):
                for b in reversed(content):
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                        chunks.append(b["text"].strip())
        if started or tail >= size or tail >= (64 << 20):
            break
        tail *= 4
    return strip_md("\n\n".join(reversed(chunks)))


def claude_logs():
    """Живые карточки клода: tmux-сессия -> путь транскрипта (нужен известный id)."""
    out = {}
    for c in load_board()["cards"]:
        name, sid = c.get("tmux"), c.get("id")
        if not name or not sid or c.get("agent", "claude") != "claude":
            continue
        path = _sid_paths.get(sid) or session_file(c.get("cwd", ""), sid)
        if os.path.exists(path):
            out[name] = path
    return out


def last_turn_end(path, tail=65536):
    """Конец последнего хода по логу: (timestamp записи turn_duration, сколько фоновых
    сабагентов ещё работает). Хвоста файла хватает — запись пишется в самом конце хода."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - tail))
            lines = f.read().split(b"\n")
    except OSError:
        return "", 0
    for line in reversed(lines):
        if b'"subtype":"turn_duration"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        return rec.get("timestamp", ""), int(rec.get("pendingBackgroundAgentCount") or 0)
    return "", 0


def notify_state_load():
    try:
        with open(NOTIFY_STATE) as f:
            st = json.load(f)
        return st["told"], st["told_turn"], {k: tuple(v) for k, v in st.get("sent", {}).items()}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def notify_state_save(told, told_turn, sent):
    try:
        tmp = NOTIFY_STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"told": told, "told_turn": told_turn, "sent": sent}, f, ensure_ascii=False)
        os.replace(tmp, NOTIFY_STATE)
    except OSError as e:
        tg_log(f"не записал {NOTIFY_STATE}: {e}")


def iso_epoch(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def codex_notification_records(path, cursor=None, bootstrap=False):
    """Читаем только дописанные полные JSONL-записи, без ограничения длины ответа.
    При первом включении пропускаем историю; оборванную строку дочитаем позже.
    event_msg/item_completed дублирует response_item — его не отправляем."""
    with open(path, "rb") as f:
        st = os.fstat(f.fileno())
        identity = {"dev": st.st_dev, "ino": st.st_ino}
        if (cursor and all(cursor.get(k) == v for k, v in identity.items())
                and 0 <= cursor.get("offset", -1) <= st.st_size):
            offset = cursor["offset"]
        elif bootstrap:
            # Последний перевод строки: не теряем запись, которую CLI ещё пишет.
            offset = st.st_size
            while offset:
                start = max(0, offset - 8192)
                f.seek(start)
                pos = f.read(offset - start).rfind(b"\n")
                if pos >= 0:
                    offset = start + pos + 1
                    break
                offset = start
        else:
            offset = 0
        f.seek(offset)
        records = []
        while f.tell() < st.st_size:
            line = f.readline()
            if not line.endswith(b"\n"):
                break
            offset = f.tell()
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            pay = rec.get("payload") or {}
            if (rec.get("type") != "response_item" or pay.get("type") != "message"
                    or pay.get("role") != "assistant"
                    or pay.get("channel") not in (None, "commentary", "final")):
                continue
            phase = pay.get("phase") or (
                "commentary" if pay.get("channel") == "commentary" else "final_answer")
            if phase not in ("commentary", "final_answer"):
                continue
            text = "\n\n".join(seg.get("text", "") for seg in pay.get("content", [])
                               if isinstance(seg, dict)
                               and seg.get("type") in ("output_text", "text")
                               and isinstance(seg.get("text"), str)).strip()
            if text:
                records.append((rec.get("timestamp", ""), phase, strip_md(text)))
    return records, dict(identity, offset=offset)


def codex_notification_cards():
    """Только главные, живые локальные сессии; форки сабагентов сюда не попадают."""
    with BOARD_LOCK:
        board = load_board()
    live = set(tmux("list-sessions", "-F", "#{session_name}").split())
    cards = [c for c in board["cards"] if c.get("agent") == "codex"
             and c.get("tmux") in live and not c.get("host")]
    if any(not c.get("rollout") for c in cards):
        # Новый агент может появиться без открытого браузера: привязываем его лог.
        get_agents()
        with BOARD_LOCK:
            board = load_board()
        cards = [c for c in board["cards"] if c.get("agent") == "codex"
                 and c.get("tmux") in live and not c.get("host")]
    return [dict(c, label=c.get("label") or board["labels"].get(c.get("id"), ""))
            for c in cards]


def codex_notifications(now):
    """Сообщения и курсор — одна запись на диск: перезапуск не ставит их в очередь снова."""
    global TG_CODEX_INITIALIZED
    cards = codex_notification_cards()
    for card in cards:
        path, name = card.get("rollout"), card["tmux"]
        if not path or not log_valid("codex", path):
            continue
        previous = TG_CODEX_CURSORS.get(path)
        try:
            records, cursor = codex_notification_records(
                path, previous, bootstrap=not TG_CODEX_INITIALIZED)
        except OSError:
            continue  # не двигаем курсор: следующий опрос повторит чтение
        if cursor == previous:
            continue
        queued = []
        watched = being_watched(name) if records else False
        for timestamp, phase, tail in records:
            if watched or not timestamp or now - iso_epoch(timestamp) > FRESH_WINDOW:
                continue
            project = card.get("project") or os.path.basename(card.get("cwd", "")) or name
            label = card.get("label")
            title = (label + " · " + project) if label else project
            icon = "⏳ " if phase == "commentary" else "✅ "
            text = icon + title + f" · @{name}\n\n" + tail
            if PUBLIC_URL:
                text += f"\n\n{PUBLIC_URL}/#card={quote(name)}"
            queued.append([name, tg_chunks(text)])
        with TG_QUEUE_CV:
            old_len = len(TG_QUEUE)
            TG_QUEUE.extend(queued)
            TG_CODEX_CURSORS[path] = cursor
            if not tg_queue_save():
                del TG_QUEUE[old_len:]
                if previous is None:
                    TG_CODEX_CURSORS.pop(path, None)
                else:
                    TG_CODEX_CURSORS[path] = previous
                continue
            TG_QUEUE_CV.notify()
        if queued:
            tg_log(f"уведомления Codex @{name}: {len(queued)} → в очередь")
    if not TG_CODEX_INITIALIZED:
        with TG_QUEUE_CV:
            TG_CODEX_INITIALIZED = True
            if not tg_queue_save():
                TG_CODEX_INITIALIZED = False
    return {c["tmux"] for c in cards}


def notifier():
    # Маркеры «о чём уже сказали» живут на диске: раньше перезапуск доски глотал ход,
    # закончившийся в эти же секунды — новый процесс считал его старым. Без файла
    # (самый первый запуск) только запоминаем; с файлом — досказываем недавнее
    state = notify_state_load()
    quiet = state is None
    told, told_turn, sent = state or ({}, {}, {})
    # told: tmux-сессия -> mtime файла хука; told_turn: -> timestamp конца хода клода;
    # sent: -> (статус, текст, когда) последнего уведомления — не дублим
    while True:
        time.sleep(1)
        fresh, dirty, now = {}, False, time.time()
        codex_names = codex_notifications(now)
        # 1) файл статуса от хука — все CLI, idle и waiting
        try:
            names = os.listdir(STATUS_DIR)
        except OSError:
            names = []
        for name in names:
            status, at = hook_status(name)
            if status in ("idle", "waiting") and told.get(name) != at:
                told[name], dirty = at, True
                # Codex сам сообщает commentary/final_answer через журнал.
                # Stop не должен повторять ответ и весь хвост превью.
                if (not quiet and now - at <= FRESH_WINDOW
                        and not (name in codex_names and status == "idle")):
                    fresh[name] = status
        # 2) конец хода клода по его логу. Файл статуса для этого ненадёжен:
        # фоновый сабагент (Agent tool) своими хуками перебивает idle на working
        # через миллисекунды после Stop — ответ главного агента терялся
        logs = claude_logs()
        for name, path in logs.items():
            turn, _ = last_turn_end(path)
            if turn and told_turn.get(name) != turn:
                told_turn[name], dirty = turn, True
                if not quiet and now - iso_epoch(turn) <= FRESH_WINDOW:
                    fresh.setdefault(name, "idle")
        quiet = False
        if dirty:
            notify_state_save(told, told_turn, sent)
        if not fresh:
            continue
        time.sleep(2)  # даём логу дописать последний ответ
        try:
            agents = {a["name"]: a for a in get_agents()["agents"]}
        except Exception:
            continue
        for name, status in fresh.items():
            a = agents.get(name)
            if not a:
                continue
            if being_watched(name):  # и так видишь — не дёргаем
                tg_log(f"@{name} {status}: смотрят на доске, не шлём")
                continue
            # текст — только последний ход; у не-клода (лога нет) — хвост превью
            tail = last_reply(logs[name]) if name in logs else notify_text(a["preview"])
            if status == "idle" and not tail:
                tail = "(ход закончен без текста)"  # молчание хуже короткого «готово»
            prev = sent.get(name)
            if prev and prev[:2] == (status, tail) and time.time() - prev[2] < 600:
                tg_log(f"@{name} {status}: тот же ответ уже отправлен, не шлём")
                continue
            text = ("🟡 " if status == "waiting" else "✅ ") + (a.get("label") or a["project"])
            if a.get("label"):
                text += " · " + a["project"]
            text += f" · @{name}"  # адрес: по нему реплай вернётся в эту сессию
            if tail:
                text += "\n\n" + tail
            turn, pending = last_turn_end(logs[name]) if name in logs else ("", 0)
            if status == "idle" and pending:
                text += f"\n\n⏳ фоновых сабагентов ещё работает: {pending} — карточка останется «работает»"
            if PUBLIC_URL:
                text += f"\n\n{PUBLIC_URL}/#card={quote(name)}"
            tg_enqueue(name, text)
            sent[name] = (status, tail, time.time())
            tg_log(f"уведомление @{name} ({status}) → в очередь")
            # оба маркера — на текущее состояние: один и тот же ответ виден и по
            # файлу статуса, и по логу, второй раз о нём не говорим
            st, at = hook_status(name)
            if st in ("idle", "waiting"):
                told[name] = at
            if turn:
                told_turn[name] = turn
            notify_state_save(told, told_turn, sent)
            if len(TG_SENT) > 2000:
                for k in list(TG_SENT)[:1000]:
                    TG_SENT.pop(k, None)


# ---------- ответы из Telegram: реплай на уведомление → агенту в терминал ----------

def tg_agents():
    """Живые сессии узла: имя tmux -> карточка доски (label, project, status)."""
    live = set(tmux("list-sessions", "-F", "#{session_name}").split())
    try:
        cards = get_agents()["agents"]
    except Exception:
        cards = []
    found = {a["name"]: a for a in cards if a["name"] in live and not a.get("host")}
    for name in live - set(found):
        found[name] = {"name": name, "project": name, "status": ""}
    return found


def tg_resolve(tag, agents):
    """@тег -> сессия: точно, без регистра, по уникальному началу, по имени карточки."""
    if tag in agents:
        return tag
    low = tag.lower()
    for pick in (lambda n, a: n.lower() == low,
                 lambda n, a: n.lower().startswith(low),
                 lambda n, a: low in (a.get("label") or "").lower()):
        hits = [n for n, a in agents.items() if pick(n, a)]
        if len(hits) == 1:
            return hits[0]
    return None


def tg_help(agents):
    icon = {"waiting": "🟡", "working": "⏳", "idle": "✅"}
    rows = [f"{icon.get(a.get('status'), '•')} @{n} · {a.get('label') or a.get('project') or n}"
            for n, a in sorted(agents.items())]
    return (("Сессии на узле:\n" + "\n".join(rows)) if rows else "Живых сессий на узле нет.") + (
        f"\n\nБез реплая и @имени сообщение уходит агенту, с которым был обмен за последние "
        f"{TG_STICKY // 60} мин. Дольше, никого нет или в тексте другой проект — бот спросит "
        "кнопками, кому: живому агенту, новому в проекте или общему агенту. "
        "Ушло не туда — кнопка «↪ не туда» под отметкой. "
        "Картинки и файлы тоже можно, подпись уйдёт текстом. "
        "Клавиши диалога: /enter /esc /up /down /tab." +
        ("\n\nОбщий агент (без проекта): /agent <текст>." if DUTY_DIR else ""))


TG_STICKY = int(os.environ.get("AGENTBOARD_TG_STICKY") or 1200)  # сек «текущего собеседника»
TG_UPDATES = ["callback_query", "message"]


def tg_aliases():
    """AGENTBOARD_TG_ALIASES="scandalio=скандалио;kuhni-lebedi=кухни,кл" — как проекты
    называют в сообщениях. Само имя папки — тоже название."""
    out = {}
    for part in os.environ.get("AGENTBOARD_TG_ALIASES", "").split(";"):
        proj, _, words = part.partition("=")
        if proj.strip():
            out[proj.strip()] = [w.strip().lower() for w in words.split(",") if w.strip()]
    return out


def tg_mentions(text, projects=None):
    """Проекты, названные в тексте, в порядке появления. Слово короче 4 букв —
    только целиком («кл»), длиннее — как начало слова («кухн» → «кухнях»)."""
    low, found = (text or "").lower(), []
    aliases = tg_aliases()
    for proj in (projects if projects is not None else tg_projects()):
        pos = []
        for term in [proj.lower()] + aliases.get(proj, []):
            tail = r"(?![\w-])" if len(term) < 4 else r"(?!-)"
            m = re.search(r"(?<![\w-])" + re.escape(term) + tail, low)
            if m:
                pos.append(m.start())
        if pos:
            found.append((min(pos), proj))
    return [p for _, p in sorted(found)]


def agent_projects(a):
    """Проект агента: и как записан на карточке, и по имени папки."""
    out = {a.get("project") or ""}
    if a.get("path"):
        out.add(os.path.basename(a["path"].rstrip("/")))
    return out - {""}


def tg_target(msg, text, agents):
    """Кому адресовано сообщение. Реплай — сессия из шапки уведомления (или из
    памяти, если ответили на хвост длинного ответа), и только она: подставлять
    похожую вместо умершей нельзя. Без реплая — @имя в тексте (тут можно и
    неточно: по началу или по имени карточки). Без адреса — текущему
    собеседнику, если обмен с ним был не дольше TG_STICKY назад и в тексте не
    назван другой проект; иначе спросить кнопками (how="menu").
    Вернёт (сессия, текст без тега, заметка, how): how — reply | tag | auto | menu;
    без сессии и не menu заметка — ответ юзеру об ошибке."""
    rt = msg.get("reply_to_message") or {}
    if rt:
        name = TG_SENT.get(rt.get("message_id"))
        if not name:
            m = TG_TAG_RE.search((rt.get("text") or "").split("\n", 1)[0])
            name = m.group(1) if m else ""
        if not name:
            return None, text, ("Это не уведомление доски. Ответь реплаем на уведомление "
                                "или начни с @имени сессии.\n\n" + tg_help(agents)), "reply"
        if name not in agents:
            return None, text, f"Сессии @{name} уже нет.\n\n" + tg_help(agents), "reply"
        return name, text, "", "reply"
    for m in TG_TAG_RE.finditer(text):
        name = tg_resolve(m.group(1), agents)
        if not name and DUTY_DIR and m.group(1).lower() == DUTY_NAME:
            name = DUTY_NAME  # спящий общий агент: разбудим
        if name:
            body = (text[:m.start()] + text[m.end():]).strip()
            note = "" if name == m.group(1) else f"→ @{name} · {agents.get(name, {}).get('label') or name}"
            return name, body, note, "tag"
    tags = TG_TAG_RE.findall(text)
    if tags:
        return None, text, f"Не знаю сессии @{tags[0]}.\n\n" + tg_help(agents), "tag"
    cur = TG_LAST.get("cur")
    if cur in agents and time.time() - (TG_LAST.get("cur_at") or 0) <= TG_STICKY:
        named = tg_mentions(text)
        if not named or agent_projects(agents[cur]) & set(named):
            return cur, text, "", "auto"
    return None, text, "", "menu"


# ---------- меню «кому это?» и кнопка «↪ не туда» ----------

TG_PENDING = {}   # токен -> {"text", "mid", "opts": [(вид, значение)], "wrong"}
TG_PENDING_SEQ = [0]
TG_PENDING_LOCK = threading.Lock()


def tg_pending_put(item):
    with TG_PENDING_LOCK:
        TG_PENDING_SEQ[0] += 1
        tok = format(TG_PENDING_SEQ[0], "x") + format(int(time.time()) % 4096, "x")
        TG_PENDING[tok] = item
        while len(TG_PENDING) > 200:
            TG_PENDING.pop(next(iter(TG_PENDING)))
        return tok


def log_context(path, tail=262144):
    """Размер контекста клода по последнему ответу в логе, токенов (0 — неизвестно)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - tail))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return 0
    for line in reversed(lines):
        if '"usage"' not in line:
            continue
        try:
            u = (json.loads(line).get("message") or {}).get("usage") or {}
        except ValueError:
            continue
        n = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                             "cache_creation_input_tokens"))
        if n:
            return n
    return 0


def ago(sec):
    sec = max(0, int(sec))
    if sec < 3600:
        return f"{max(1, sec // 60)} мин"
    if sec < 48 * 3600:
        return f"{sec // 3600} ч"
    return f"{sec // 86400} дн"


def agent_info(name, agents, logs=None):
    """«тема · молчит 3 ч · 180k» — для кнопок и вопросов о старых агентах."""
    a = agents.get(name) or {}
    topic = (a.get("label") or (a.get("preview") or "").strip().split("\n")[0]
             or a.get("project") or name)[:28]
    logs = claude_logs() if logs is None else logs
    path = logs.get(name)
    try:
        last = os.path.getmtime(path) if path else (a.get("activity") or 0)
    except OSError:
        last = a.get("activity") or 0
    parts = [topic]
    if a.get("status") == "working":
        parts.append("работает")
    elif last:
        parts.append("молчит " + ago(time.time() - last))
    ctx = log_context(path) if path else 0
    if ctx:
        parts.append(f"{round(ctx / 1000)}k")
    return " · ".join(parts)


def tg_focus_opts(project, agents, exclude=""):
    """Меню по одному проекту: его живые агенты, новый, «другой адресат…»."""
    cur = TG_LAST.get("cur")
    live = sorted((n for n, a in agents.items()
                   if n != exclude and project in agent_projects(a)),
                  key=lambda n: (n != cur, n))
    return [("a", n) for n in live] + [("n", project), ("x", "")]


def tg_menu_opts(text, agents, exclude=""):
    """Варианты адресата: сначала названные в тексте проекты (их живые агенты,
    потом «новый» там), затем текущий собеседник и остальные живые, общий агент,
    новые агенты в прочих проектах."""
    projects = tg_projects()
    named = tg_mentions(text, projects)
    cur = TG_LAST.get("cur")
    live = [n for n in agents if n != exclude]
    order = sorted(live, key=lambda n: (n != cur, n == DUTY_NAME, n))
    opts, seen = [], set()

    def add(kind, val):
        if (kind, val) not in seen:
            seen.add((kind, val))
            opts.append((kind, val))
    for proj in named:
        for n in order:
            if proj in agent_projects(agents[n]):
                add("a", n)
        add("n", proj)
    for n in order:
        if n != DUTY_NAME:
            add("a", n)
    if DUTY_DIR and exclude != DUTY_NAME:
        add("d", "")
    for proj in projects:
        add("n", proj)
    return opts


def tg_opt_label(opt, agents, logs=None, focus=False):
    kind, val = opt
    if kind == "a":
        return f"@{val} · {agent_info(val, agents, logs)}"[:64]
    if kind == "d":
        return "🧭 общий агент"
    if kind == "x":
        return "другой адресат…"
    return "＋ новый агент" if focus else f"＋ новый: {val}"


def tg_menu_markup(tok, opts, agents, focus=False):
    rows, pair, logs = [], [], claude_logs()
    for i, opt in enumerate(opts):
        btn = {"text": tg_opt_label(opt, agents, logs, focus), "callback_data": f"m{tok}:{i}"}
        if opt[0] == "n" and not focus:  # новые — по два в ряд, живые и общий — по одному
            pair.append(btn)
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        else:
            rows.append([btn])
    if pair:
        rows.append(pair)
    return {"inline_keyboard": rows}


def tg_menu(body, mid, agents, wrong="", edit=None, focus=""):
    """Спросить кнопками, кому сообщение. focus — проект, названный в тексте:
    тогда только его агенты, «＋ новый агент» и «другой адресат…». edit —
    message_id сообщения бота, которое превратить в меню, иначе новое сообщение."""
    opts = (tg_focus_opts(focus, agents, exclude=wrong) if focus
            else tg_menu_opts(body, agents, exclude=wrong))
    tok = tg_pending_put({"text": body, "mid": mid, "opts": opts, "wrong": wrong})
    if focus:
        ask = f"{focus} — с кем работаем?"
    else:
        ask = "Кому это? Выбери — передам."
    if wrong:
        ask += f"\n@{wrong} получит Esc и просьбу ничего не делать по этому сообщению."
    markup = tg_menu_markup(tok, opts, agents, focus=bool(focus))
    if edit:
        try:
            tg_api("editMessageText", chat_id=TG_CHAT, message_id=edit, text=ask,
                   reply_markup=json.dumps(markup, ensure_ascii=False))
            return
        except Exception as e:
            tg_log(f"editMessageText: {e}")
    tg_post(ask, reply_to=mid, markup=markup)


def tg_mark(name, agents, body, mid):
    """Отметка «ушло @имя» с кнопкой «↪ не туда» — после доставки без адреса."""
    tok = tg_pending_put({"text": body, "mid": mid, "wrong": name})
    lab = (agents.get(name) or {}).get("label") or (agents.get(name) or {}).get("project") or name
    tg_post(f"→ @{name} · {lab}", reply_to=mid,
            markup={"inline_keyboard": [[{"text": "↪ не туда", "callback_data": f"w{tok}"}]]})


def tg_deliver(opt, body, mid):
    """Выполнить выбор из меню; вернёт текст итога для сообщения-меню."""
    kind, val = opt
    if kind == "n":
        cwd = tg_projects().get(val)
        if not cwd:
            return f"Проекта {val} уже нет."
        tg_start(val, cwd, body, mid)
        return f"→ новый агент в {val}"
    if kind == "d":
        tg_duty(body, mid)
        return f"→ @{DUTY_NAME} · общий агент"
    ok = send_to_agent(val, body)
    tg_log(f"→ @{val} (меню): {body[:60]!r}" + ("" if ok else " — не дошло"))
    if not ok:
        return f"Не удалось передать @{val}. Проверь сессию на доске."
    tg_last_set("to", val)
    return f"→ @{val}"


TG_MAX_AGENTS = int(os.environ.get("AGENTBOARD_TG_MAX_AGENTS") or 4)  # больше — машине тесно


def tg_cleanup_offer(project, new):
    """После запуска нового агента — что сделать со старыми: агенты того же
    проекта всегда, остальные — если живых больше TG_MAX_AGENTS (самые давно
    молчащие, до трёх). Общего агента не предлагаем."""
    agents = tg_agents()
    logs = claude_logs()
    olds = [n for n, a in agents.items()
            if n not in (new, DUTY_NAME) and project in agent_projects(a)
            and a.get("status") != "working"]
    if len(agents) > TG_MAX_AGENTS:
        def last(n):
            try:
                return os.path.getmtime(logs[n]) if n in logs else 0
            except OSError:
                return 0
        rest = sorted((n for n in agents if n not in olds and n not in (new, DUTY_NAME)
                       and agents[n].get("status") != "working"), key=last)
        olds += rest[:3]
        tg_post(f"Открыто агентов: {len(agents)}. Машине комфортно до {TG_MAX_AGENTS}, "
                "дальше тормозит. Закрой кого-нибудь:")
    for n in olds:
        tok = tg_pending_put({"name": n})
        tg_post(f"Что сделать с @{n} ({agent_info(n, agents, logs)})?", markup={"inline_keyboard": [[
            {"text": "⏸ остановить", "callback_data": f"k{tok}:stop"},
            {"text": "🗑 убрать с доски", "callback_data": f"k{tok}:rm"},
            {"text": "оставить", "callback_data": f"k{tok}:keep"}]]})


def tg_cleanup_do(name, act, bot_mid):
    if act == "stop":
        ok = stop_agent(name)
        res = (f"@{name} остановлен: карточка на паузе, разговор можно возобновить с доски."
               if ok else f"@{name} уже не запущен.")
    elif act == "rm":
        with BOARD_LOCK:
            cid = next((c.get("id", "") for c in load_board()["cards"] if c.get("tmux") == name), "")
            ok = remove_card(name, cid)
        res = (f"@{name} убран с доски. Разговор сохранён: «недавно закрытые» на доске."
               if ok else f"Не удалось убрать @{name}.")
    else:
        res = f"@{name} оставлен."
    tg_log(f"старый агент @{name}: {act}")
    try:
        tg_api("editMessageText", chat_id=TG_CHAT, message_id=bot_mid, text=res,
               entities=tg_entities(res))
    except Exception as e:
        tg_log(f"editMessageText: {e}")


def tg_callback(cq):
    """Нажатие кнопки под сообщением бота: выбор адресата или «не туда»."""
    msg = cq.get("message") or {}
    if str((msg.get("chat") or {}).get("id")) != str(TG_CHAT):
        return
    data, bot_mid = cq.get("data") or "", msg.get("message_id")
    try:
        tg_api("answerCallbackQuery", callback_query_id=cq.get("id"))
    except Exception as e:
        tg_log(f"answerCallbackQuery: {e}")
    tg_log(f"← кнопка {data!r}")
    m = re.fullmatch(r"([mwk])(\w+?)(?::(\w+))?", data)
    with TG_PENDING_LOCK:
        item = TG_PENDING.get(m.group(2)) if m else None
        if item and m.group(1) in "mk":
            TG_PENDING.pop(m.group(2), None)  # двойное нажатие не отправит дважды
    if not item:
        try:
            tg_api("editMessageText", chat_id=TG_CHAT, message_id=bot_mid,
                   text="Кнопка устарела (доска перезапускалась). Пришли сообщение ещё раз.")
        except Exception:
            pass
        return
    agents = tg_agents()
    if m.group(1) == "w":
        tg_menu(item["text"], item["mid"], agents, wrong=item["wrong"], edit=bot_mid)
        return
    if m.group(1) == "k":
        tg_cleanup_do(item["name"], m.group(3), bot_mid)
        return
    i = int(m.group(3) or 0)
    if i >= len(item["opts"]):
        return
    if item["opts"][i][0] == "x":  # «другой адресат…» — общее меню вместо проектного
        tg_menu(item["text"], item["mid"], agents, wrong=item.get("wrong", ""), edit=bot_mid)
        return
    wrong = item.get("wrong")
    if wrong and wrong in agents:
        send_key(wrong, "esc")
        time.sleep(0.5)
        send_to_agent(wrong, "Предыдущее сообщение из Telegram было адресовано не тебе: "
                             "ничего по нему не делай. Ответь одним словом «ок».")
        tg_log(f"не туда: @{wrong} отозван")
    result = tg_deliver(item["opts"][i], item["text"], item["mid"])
    try:
        tg_api("editMessageText", chat_id=TG_CHAT, message_id=bot_mid,
               text=(f"Отозвал у @{wrong}. " if wrong else "") + result,
               entities=tg_entities(result))
    except Exception as e:
        tg_log(f"editMessageText: {e}")


def tg_react(mid, emoji="👍"):
    try:
        tg_api("setMessageReaction", chat_id=TG_CHAT, message_id=mid,
               reaction=json.dumps([{"type": "emoji", "emoji": emoji}]))
        return True
    except Exception as e:
        tg_log(f"setMessageReaction: {e}")
        return False


TG_MEDIA = ("photo", "document", "video", "animation", "audio", "voice", "video_note", "sticker")


def tg_media(msg):
    """Вложение сообщения: (file_id, имя файла) или None. У фото несколько размеров —
    берём самый крупный; имя документа сохраняем, остальное называем по типу."""
    for kind in TG_MEDIA:
        v = msg.get(kind)
        if not v:
            continue
        if kind == "photo":
            best = max(v, key=lambda ph: ph.get("file_size") or 0)
            return best["file_id"], "photo.jpg"
        return v["file_id"], v.get("file_name") or kind
    return None


def tg_download(file_id, dest):
    """Скачать файл телеграма (Bot API отдаёт до 20 МБ) в dest; расширение, если его
    нет в имени, берём из пути на сервере телеграма. Вернёт итоговый путь или ''."""
    try:
        remote = (tg_api("getFile", file_id=file_id) or {}).get("file_path", "")
        if not remote:
            return ""
        if not os.path.splitext(dest)[1] and os.path.splitext(remote)[1]:
            dest += os.path.splitext(remote)[1]
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with urllib.request.urlopen(f"https://api.telegram.org/file/bot{TG_TOKEN}/{remote}",
                                    timeout=120) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        return dest
    except Exception as e:
        tg_log(f"download: {e}")
        return ""


def tg_fetch(media, name):
    """Вложение — в папку данных доски, не в проект (чтобы не мусорить в git):
    DATA_DIR/inbox/<сессия>/<время>_<имя>. Агенту уходит путь."""
    file_id, fname = media
    fname = re.sub(r"[^\w.-]", "_", fname)[-80:] or "file"
    dest = os.path.join(DATA_DIR, "inbox", name, time.strftime("%Y%m%d-%H%M%S") + "_" + fname)
    return tg_download(file_id, dest)


def tg_projects():
    """Все папки проектов из $AGENTBOARD_DIRS — для /new и /projects."""
    out = {}
    bases = os.environ.get("AGENTBOARD_DIRS", "~/Documents/dev:~/Documents")
    for base in (os.path.expanduser(b) for b in bases.split(":") if b):
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        for d in entries:
            path = os.path.join(base, d)
            if os.path.isdir(path) and not d.startswith("."):
                out.setdefault(d, path)
    return out


NEW_HELP = ("Новый агент: /new <проект> [codex] <задача>\n"
            "Например: /new scandalio посмотри git status и расскажи коротко\n"
            "Проекты: /projects")


# Общий агент (бывший дежурный): универсальный, без проекта, в своей папке со
# своим CLAUDE.md. Зовут /agent, @duty или кнопкой «общий агент». Пусто — выключен.
DUTY_DIR = os.path.expanduser(os.environ.get("AGENTBOARD_DUTY_DIR", ""))
DUTY_NAME = "duty"
DUTY_LABEL = "Общий агент"  # постоянное имя дежурного, не от первой задачи


def tg_duty_start(body, mid):
    """Дежурного нет в живых — поднять его с этим сообщением первой задачей."""
    with BOARD_LOCK:
        if tmux_ok("has-session", "-t", DUTY_NAME):
            return send_to_agent(DUTY_NAME, body)
        os.makedirs(DUTY_DIR, exist_ok=True)
        try:
            ok = new_agent(DUTY_DIR, DUTY_NAME, body, "claude")
        except Exception as e:
            tg_log(f"дежурный: {e}")
            ok = False
    if ok:
        tg_log(f"дежурный запущен: {body[:60]!r}")
        tg_last_set("to", DUTY_NAME)
        tg_post("Общий агент проснулся и взял сообщение.", reply_to=mid)
    return ok


DUTY_ASK = "Что передать общему агенту?"


def tg_duty(body, mid):
    """/agent: сообщение дежурному — разбудить, если спит."""
    if not DUTY_DIR:
        tg_post("Дежурный не настроен (AGENTBOARD_DUTY_DIR).", reply_to=mid)
        return
    if not body:
        tg_post(DUTY_ASK + " Ответь на это сообщение.",
                markup={"force_reply": True, "input_field_placeholder": "сообщение общему агенту"})
        return
    if tmux_ok("has-session", "-t", DUTY_NAME):
        ok = send_to_agent(DUTY_NAME, body)
        tg_log(f"→ @{DUTY_NAME}: {body[:60]!r}" + ("" if ok else " — не дошло"))
        if ok:
            tg_last_set("to", DUTY_NAME)
            tg_react(mid)
        else:
            tg_post("Не удалось передать общему агенту.", reply_to=mid)
    elif not tg_duty_start(body, mid):
        tg_post("Не удалось разбудить общего агента, посмотри журнал доски.", reply_to=mid)


def tg_stop(msg, text, mid):
    """/stop — реплаем на уведомление агента, /stop @имя или кнопки живых."""
    agents = tg_agents()
    arg = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
    name = ""
    if msg.get("reply_to_message"):
        name, _, note, _ = tg_target(msg, "", agents)
        if not name:
            tg_post(note, reply_to=mid)
            return
    elif arg:
        name = tg_resolve(arg.lstrip("@"), agents)
        if not name:
            tg_post(f"Не знаю сессии {arg}.\n\n" + tg_help(agents), reply_to=mid)
            return
    if not name:
        if not agents:
            tg_post("Живых агентов нет.", reply_to=mid)
            return
        rows = [[{"text": f"/stop @{n}"}] for n in sorted(agents)]
        tg_post("Кого остановить?", reply_to=mid,
                markup={"keyboard": rows, "one_time_keyboard": True, "resize_keyboard": True})
        return
    ok = stop_agent(name)
    tg_log(f"стоп @{name}: {'ok' if ok else 'не вышло'}")
    tg_post(f"Остановил @{name}. Карточка на доске на паузе, разговор можно возобновить."
            if ok else f"Не удалось остановить @{name}.", reply_to=mid,
            markup={"remove_keyboard": True})


TG_NEW_ASK = {}  # message_id вопроса «что сделать?» → проект


def tg_new(text, mid):
    """/new <проект> [claude|codex] <задача> — запустить агента из телеграма.
    Без задачи — по шагам: кнопки проектов, потом вопрос, ответ на него — задача."""
    parts = text.split(None, 2)
    projects = tg_projects()
    if len(parts) < 2:
        names = list(projects)
        rows = [[{"text": f"/new {n}"} for n in names[i:i + 2]] for i in range(0, len(names), 2)]
        tg_post("В каком проекте запустить агента?", reply_to=mid,
                markup={"keyboard": rows, "one_time_keyboard": True, "resize_keyboard": True})
        return
    key = parts[1].lower()
    match = [n for n in projects if n.lower() == key] or \
            [n for n in projects if n.lower().startswith(key)]
    if len(match) != 1:
        why = "Такого проекта нет" if not match else "Подходит несколько: " + ", ".join(match)
        tg_post(f"{why}. Проекты: " + ", ".join(projects), reply_to=mid)
        return
    if len(parts) < 3:
        ask = tg_post(f"Что сделать в {match[0]}? Ответь на это сообщение задачей "
                      "(слово codex в начале — запустить Codex вместо Claude).",
                      markup={"force_reply": True, "input_field_placeholder": "задача для агента"})
        if ask:
            TG_NEW_ASK[ask] = match[0]
            tg_log(f"жду задачу для {match[0]} ответом на {ask}")
        return
    tg_start(match[0], projects[match[0]], parts[2], mid)


def tg_start(project, cwd, rest, mid):
    agent = "claude"
    first = rest.split(None, 1)
    if first[0].lower() in ("claude", "codex") and len(first) == 2:
        agent, rest = first[0].lower(), first[1]
    if agent == "codex" and not CODEX:
        tg_post("Codex на узле не найден.", reply_to=mid)
        return ""
    with BOARD_LOCK:  # имя сессии берём из только что добавленной карточки
        try:
            ok = new_agent(cwd, project, rest, agent)
        except Exception as e:
            tg_log(f"/new {project}: {e}")
            ok = False
        name = load_board()["cards"][-1].get("tmux", project) if ok else ""
    if not ok:
        tg_post(f"Не удалось запустить агента в {project}.", reply_to=mid)
        return ""
    tg_last_set("to", name)
    tg_log(f"новый агент @{name} ({agent}) в {project}: {rest[:60]!r}")
    tg_post(f"Запустил @{name} ({agent}) в {project}. Отвечу, когда закончит.", reply_to=mid)
    try:
        tg_cleanup_offer(project, name)
    except Exception as e:
        tg_log(f"вопрос о старых агентах: {e}")
    return name


def tg_handle(msg):
    """Входящее из телеграма. Только наш чат: бот публичный, команду агенту
    может прислать кто угодно. /help — список сессий, /enter и т.п. — клавиши
    TUI-диалога (как кнопки под диалогом на доске), остальное — текст агенту."""
    if not msg or str((msg.get("chat") or {}).get("id")) != str(TG_CHAT):
        return
    text = (msg.get("text") or msg.get("caption") or "").strip()
    mid = msg.get("message_id")
    media = tg_media(msg)
    rt_id = (msg.get("reply_to_message") or {}).get("message_id")
    tg_log(f"← {text[:60]!r}" + (f" (ответ на {rt_id})" if rt_id else ""))
    if not text and not media:
        return
    if text.split("@")[0].lower() in ("/start", "/help", "/list", "/ls", "?"):
        tg_post(tg_help(tg_agents()) + "\n\n" + NEW_HELP, reply_to=mid)
        return
    cmd = text.split(None, 1)[0].split("@")[0].lower() if text else ""
    if cmd == "/new" and not media:
        tg_new(text, mid)
        return
    rt = msg.get("reply_to_message") or {}
    m = re.match(r"Что сделать в (\S+)\?", rt.get("text") or "")
    if (rt.get("message_id") in TG_NEW_ASK or m) and text and not media:
        # ответ на «что сделать в …?»; проект берём и из текста вопроса —
        # чтобы ответ не терялся после перезапуска доски
        project = TG_NEW_ASK.pop(rt.get("message_id"), None) or m.group(1)
        cwd = tg_projects().get(project)
        if cwd:
            tg_start(project, cwd, text, mid)
        return
    if cmd == "/agent" and not media:
        tg_duty(text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else "", mid)
        return
    if ((msg.get("reply_to_message") or {}).get("text") or "").startswith(
            (DUTY_ASK, "Что передать дежурному?")) \
            and text and not media:
        tg_duty(text, mid)
        return
    if cmd == "/stop":
        tg_stop(msg, text, mid)
        return
    if cmd == "/projects":
        tg_post("Проекты: " + ", ".join(tg_projects()), reply_to=mid)
        return
    agents = tg_agents()
    name, body, note, how = tg_target(msg, text, agents)
    if how == "menu":
        if media:
            path = tg_fetch(media, "_menu")
            if not path:
                tg_post("Не смог скачать вложение: Bot API отдаёт файлы до 20 МБ.", reply_to=mid)
                return
            body = (body + "\n\n" if body else "") + f"Вложение из Telegram: {path}"
        named = tg_mentions(text)
        tg_log(f"кому? меню ({named[0] if named else 'общее'}) для {body[:60]!r}")
        tg_menu(body, mid, agents, focus=named[0] if named else "")
        return
    if not name:
        tg_post(note, reply_to=mid)
        return
    if media:  # картинка или файл: сохраняем на узле, агенту — подпись и путь
        path = tg_fetch(media, name)
        if not path:
            tg_post("Не смог скачать вложение: Bot API отдаёт файлы до 20 МБ.", reply_to=mid)
            return
        body = (body + "\n\n" if body else "") + f"Вложение из Telegram: {path}"
    if not body:
        tg_post(f"Пустое сообщение — что передать @{name}?", reply_to=mid)
        return
    if name == DUTY_NAME and DUTY_DIR and name not in agents:
        if not tg_duty_start(body, mid):
            tg_post("Не удалось разбудить общего агента, посмотри журнал доски.", reply_to=mid)
        return
    # отметка с «не туда» — когда адресат угадан, а ты пишешь ему не подряд
    mark = how == "auto" and (name != TG_LAST.get("to")
                              or time.time() - (TG_LAST.get("to_at") or 0) > 120)
    if re.fullmatch(r"/(enter|esc|up|down|tab)", body.lower()):
        ok = send_key(name, body[1:].lower())
        mark = False
    else:
        ok = send_to_agent(name, body)
    tg_log(f"→ @{name}: {body[:60]!r}" + ("" if ok else " — не дошло"))
    if not ok:
        tg_post(f"Не удалось передать сообщение в @{name}. Проверь сессию на доске.", reply_to=mid)
        return
    tg_last_set("to", name)
    if mark:
        tg_mark(name, agents, body, mid)
    elif note:
        tg_post(note, reply_to=mid)
    elif not tg_react(mid):
        tg_post(f"→ @{name}", reply_to=mid)


def tg_listener():
    """Длинный опрос getUpdates. Он монопольный: если бота слушает кто-то ещё
    (webhook или чужой getUpdates — скажем, общий с другим ассистентом бот),
    тихо выходим, не ломая ему опрос; уведомления при этом работают как раньше.
    Признак чужого опроса — allowed_updates, которых мы не ставили."""
    try:
        info = tg_api("getWebhookInfo") or {}
    except Exception as e:
        tg_log(f"getWebhookInfo: {e}")
        info = {}
    allowed = info.get("allowed_updates") or []
    if info.get("url") or (allowed and sorted(allowed) not in (["message"], TG_UPDATES)):
        tg_log("бота уже слушает другой процесс — ответы из Telegram выключены. "
               "Заведи доске отдельного бота (@BotFather), токен в AGENTBOARD_TG_TOKEN")
        return
    tg_log(f"слушаю ответы из Telegram (токен из {TG_FROM}, чат {TG_CHAT})")
    offset, backoff = None, 0
    while True:
        try:
            updates = tg_api("getUpdates", http_timeout=70, offset=offset, timeout=50,
                             allowed_updates=json.dumps(TG_UPDATES)) or []
            if backoff:
                tg_log("снова слушаю ответы из Telegram")
            backoff = 0
        except urllib.error.HTTPError as e:
            if e.code == 409:
                # кто-то ещё спросил getUpdates у этого бота: разовая проверка токена
                # в браузере или чужой постоянный опрос. Не сдаёмся навсегда — ждём
                # всё дольше и пробуем снова; постоянному сопернику достаётся по
                # одному 409 раз в полчаса, разовый вообще ничего не заметит
                backoff = min(backoff * 2 or 60, 1800)
                tg_log(f"409 Conflict: бота слушает кто-то ещё — пауза {backoff} с. "
                       "Если это навсегда, второму процессу нужен свой бот")
                time.sleep(backoff)
                continue
            if e.code == 401:
                tg_log("401: токен бота не принят — ответы из Telegram выключены")
                return
            tg_log(f"getUpdates: {e}")
            time.sleep(10)
            continue
        except Exception as e:
            tg_log(f"getUpdates: {e}")  # сеть моргнула — пробуем дальше
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            try:
                if u.get("callback_query"):
                    tg_callback(u["callback_query"])
                else:
                    tg_handle(u.get("message"))
            except Exception as e:
                tg_log(f"handle: {e}")


if __name__ == "__main__":
    # за обратным прокси доска слушает localhost, но открыта миру — по адресу
    # AGENTBOARD_URL; без токена терминалы агентов достались бы любому прохожему
    if not TOKEN and (HOST not in ("127.0.0.1", "localhost") or PUBLIC_URL):
        sys.exit("! AGENTBOARD_HOST / AGENTBOARD_URL open the board to the network — "
                 "set AGENTBOARD_TOKEN too")
    print(f"Agent Board → http://localhost:{PORT}")
    os.makedirs(NAMES_DIR, exist_ok=True)
    if not os.path.exists(TMUX):
        print("! tmux not found — install it: brew install tmux")
    threading.Thread(target=caffeinate_watcher, daemon=True).start()
    threading.Thread(target=update_checker, daemon=True).start()
    threading.Thread(target=remote_poller, daemon=True).start()
    if TG_TOKEN and TG_CHAT:
        print(f"Telegram: бот {TG_TOKEN.split(':')[0]} ({TG_FROM}) → чат {TG_CHAT}")
        tg_queue_load()  # до обоих потоков: сначала восстановить очередь и курсоры
        threading.Thread(target=notifier, daemon=True).start()
        threading.Thread(target=tg_sender, daemon=True).start()
        threading.Thread(target=tg_listener, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
