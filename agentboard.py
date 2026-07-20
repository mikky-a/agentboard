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
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

__version__ = "0.2.0"

PORT = int(os.environ.get("AGENTBOARD_PORT", "8787"))
TMUX = shutil.which("tmux") or "/opt/homebrew/bin/tmux"
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
BOARD_FILE = os.path.join(HERE, "board.json")

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
    "fable": "Fable 5",
    "opus": "Opus 4.8",
    "sonnet": "Sonnet 5",
    "haiku": "Haiku 4.5",
    "claude-fable-5": "Fable 5",
    "claude-opus-4-8": "Opus 4.8",
    "claude-opus-4-7": "Opus 4.7",
    "claude-sonnet-5": "Sonnet 5",
    "claude-haiku-4-5": "Haiku 4.5",
    "gpt-5.6-sol": "GPT-5.6 Sol",
    "gpt-5.6-codex": "GPT-5.6 Codex",
    "composer-2.5": "Composer 2.5",
    "composer-2.5-fast": "Composer 2.5 Fast",
    "openrouter/moonshotai/kimi-k3": "Kimi K3",
    "moonshotai/kimi-k3": "Kimi K3",
}


def tmux(*args):
    try:
        r = subprocess.run([TMUX, *args], capture_output=True, text=True, timeout=5)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def tmux_ok(*args):
    try:
        return subprocess.run([TMUX, *args], capture_output=True, timeout=5).returncode == 0
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
    """Свежайший разговор проекта, начатый/тронутый после момента after."""
    best, best_m = None, 0
    for p in glob.glob(os.path.join(PROJECTS_DIR, sanitize(cwd), "*.jsonl")):
        if os.path.basename(p)[:-6] in exclude:
            continue
        try:
            m = os.stat(p).st_mtime
        except OSError:
            continue
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
    try:
        size = os.path.getsize(path)
        with open(path, errors="ignore") as f:
            if size > 2_000_000:
                f.seek(size - 2_000_000)
                f.readline()  # добить обрезанную строку
            lines = f.readlines()
    except OSError:
        return ""

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
        inp = item.get("input") or {}
        detail = (inp.get("command") or inp.get("file_path")
                  or inp.get("pattern") or inp.get("description") or "")
        detail = " ".join(str(detail).split())[:60]
        return f'{item.get("name", "tool")}({detail or "…"})'

    items = []  # ("u"|"a"|"t", текст)
    for l in lines:
        try:
            r = json.loads(l)
        except ValueError:
            continue
        if r.get("type") == "user":
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
    if not items:
        preview_cache[path] = (mtime, "", "")
        return ""
    def render(chunk):
        out = []
        for kind, text in chunk:
            if kind == "u":
                out.append("> " + " ".join(text.split())[:500])
            elif kind == "t":
                out.append("⏺ " + text)
            else:
                out.append(text)
        t = "\n\n".join(out)
        return "\n".join(t.splitlines()[-500:])

    # занятый агент: якорь — твоё последнее сообщение (виден вопрос + растущий ответ)
    last_u = max((i for i, (k, _) in enumerate(items) if k == "u"), default=0)
    busy_text = render(items[last_u:])

    # тихий агент: последний ЗАВЕРШЁННЫЙ тур; мёртвые неотвеченные хвосты не показываем
    answered = None
    for i, (kind, _) in enumerate(items):
        if kind == "u" and any(k != "u" for k, _ in items[i + 1:]):
            answered = i
    seq = items[answered:] if answered is not None else items[last_u:]
    cut = len(seq)
    while cut > 0 and seq[cut - 1][0] == "u":
        cut -= 1
    idle_text = render(seq[:cut]) or busy_text

    preview_cache[path] = (mtime, busy_text, idle_text)
    return busy_text if include_trailing else idle_text


CODEX_SESS = os.path.expanduser("~/.codex/sessions")
codex_cwd_cache = {}  # путь rollout -> cwd из session_meta


def codex_meta_cwd(path):
    if path in codex_cwd_cache:
        return codex_cwd_cache[path]
    cwd = ""
    try:
        with open(path, errors="ignore") as f:
            cwd = json.loads(f.readline()).get("payload", {}).get("cwd", "")
    except Exception:
        pass
    codex_cwd_cache[path] = cwd
    return cwd


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
        if codex_meta_cwd(path) == cwd:
            best, best_m = path, m
    return best


def codex_turn_preview(path):
    """Последний тур из rollout-файла codex."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    hit = preview_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        size = os.path.getsize(path)
        with open(path, errors="ignore") as f:
            if size > 400_000:
                f.seek(size - 400_000)
                f.readline()
            lines = f.readlines()
    except OSError:
        return ""
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
    start = None
    for i, (role, text) in enumerate(items):
        if role == "user" and text.strip() and not text.lstrip().startswith("<"):
            start = i
    if start is None:
        preview_cache[path] = (mtime, "", "")
        return ""
    out = ["> " + " ".join(NAME_RE.sub("", items[start][1]).split())[:500]]
    for role, text in items[start + 1:]:
        if role == "assistant" and text.strip():
            out.append(text.strip())
        elif role == "tool":
            out.append("⏺ " + text)
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
    return bool(_opencode_q("SELECT 1 FROM session WHERE id = ?", (ro,))) \
        if agent == "opencode" else os.path.isfile(ro)


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
        elif hook == "working" and now - rec["changed"] < 120:
            status = "working"  # хук сказал "работает", панель тихая — верим ещё 2 минуты
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

    # дедупликация: один разговор — одна карточка
    seen = set()
    for card in list(cards_list):
        key = card.get("id") or ("tmux:" + card.get("tmux", ""))
        if key in seen:
            cards_list.remove(card)
            changed = True
        else:
            seen.add(key)

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
        if card and not (card.get("label") or board["labels"].get(card.get("id") or "")):
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
            # у codex/cursor/opencode нет claude-сессий — не привязываем разговор
            a["cid"] = ""
            a["sname"] = ""
            a["label"] = card.get("label", "")
            a["logo"] = logo_version(a["path"])
            ro = card.get("rollout", "")
            if not log_valid(a["agent"], ro):
                ro = find_log(a["agent"], card["cwd"], a["created"],
                              {c.get("rollout") for c in cards_list
                               if c is not card and c.get("rollout")})
                if ro:
                    card["rollout"] = ro
                    changed = True
            # превью — только из структурного лога: в пейне на старте
            # прокручивается служебный шум (MCP, лимиты), в логе его нет
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
            continue
        # точная привязка: PID процесса claude внутри панели -> sessionId
        pids = pane_pids(a["name"])
        rec = next((r for r in recs
                    if str(r.get("pid")) in pids and r.get("sessionId")), None)
        sid = rec["sessionId"] if rec else ""
        if not sid and not card["id"]:
            # запасной вариант; чужие разговоры (карточек и недавно закрытые)
            # не подцепляем — иначе новая плитка мигает чужим превью
            known = ({c["id"] for c in cards_list if c.get("id")} |
                     {c["id"] for c in board["closed"] if c.get("id")})
            sid, _ = newest_session(a["path"], a["created"], known)
        if sid and sid != card["id"]:
            card["id"] = sid
            _, card["title"] = cached_meta(find_session_file(card["cwd"], sid))
            changed = True
        a["cid"] = card["id"]
        a["sname"] = names.get(card["id"], "")
        a["label"] = card.get("label") or board["labels"].get(card["id"], "")
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
            tp = turn_preview(card["cwd"], card["id"],
                              a["status"] in ("working", "waiting"))
            if tp:
                a["preview"] = tp

    live_names = {a["name"] for a in live}
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
        session_path = find_session_file(card["cwd"], card["id"])
        activity = log_activity(session_path, ("user", "assistant"))
        raw_model = (card.get("model") or
                     log_model(session_path, "claude"))
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
            "agent": card.get("agent", "claude"),
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
    return {"workspaces": [dict(w, logo=logo_version(w["cwd"]))
                           for w in board["workspaces"]],
            "agents": agents, "page": page,
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
                if "codex" in mid or mid.startswith("gpt-5"):
                    out.append({"id": mid, "l": m.get("name") or mid,
                                "def": mid == "gpt-5.6-sol"})
        elif agent == "claude":
            out = [{"id": "fable", "l": "Fable 5", "def": True},
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


def pre_trust(agent, cwd):
    try:
        if agent == "claude":
            cfg, ok = _read_json(CLAUDE_JSON)
            if not ok or not cfg:  # нет файла — claude ещё не запускали, не лезем
                return
            proj = cfg.setdefault("projects", {}).setdefault(cwd, {})
            if not proj.get("hasTrustDialogAccepted"):
                proj["hasTrustDialogAccepted"] = True
                tmp = CLAUDE_JSON + ".agentboard-tmp"
                with open(tmp, "w") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                os.replace(tmp, CLAUDE_JSON)
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


def free_name(base):
    name, i = base, 2
    while tmux_ok("has-session", "-t", name):
        name = f"{base}-{i}"
        i += 1
    return name


def open_in_terminal(name):
    # уже подключён терминал? — поднимаем его окно, а не плодим дубль
    ttys = tmux("list-clients", "-t", name, "-F", "#{client_tty}").split()
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
    script = (
        'tell application "Terminal"\n'
        f'  do script "tmux attach -t \'{name}\'"\n'
        "  activate\n"
        "end tell"
    )
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)


@locked
def resume_card(cid):
    board = load_board()
    card = next((c for c in board["cards"] if c.get("id") == cid), None)
    if not card:
        return False
    name = free_name(card["project"])
    tmux("new-session", "-d", "-s", name, "-x", "220", "-y", "50", "-c", card["cwd"],
         f"export PATH={shlex.quote(AGENT_PATH)}; {CLAUDE} --resume {cid}")
    tmux("set-option", "-t", name, "mouse", "on")
    tmux("set-option", "-t", name, "mode-style", "bg=colour236,fg=colour245")
    card["tmux"] = name
    save_board(board)
    open_in_terminal(name)
    return True


@locked
def add_from_history(cid, cwd, project, title):
    board = load_board()
    board["cards"] = [c for c in board["cards"] if c.get("id") != cid]
    board["closed"] = [c for c in board["closed"] if c.get("id") != cid]
    board["cards"].append({"id": cid, "cwd": cwd, "project": project,
                           "title": title, "tmux": "",
                           "label": board["labels"].get(cid, "")})
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
    name = free_name(project or os.path.basename(cwd.rstrip("/")))
    os.makedirs(NAMES_DIR, exist_ok=True)
    pre_trust(agent, cwd)
    if agent == "codex":
        parts = [CODEX]
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
        parts = [CLAUDE]
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


def send_to_agent(name, text):
    """Кинуть сообщение агенту в терминал, не открывая его."""
    if not text.strip() or not tmux_ok("has-session", "-t", name):
        return False
    card = claim_naming(name)
    if card:
        text = (text.rstrip() + " " +
                name_tail(name, card.get("agent", "claude"), card.get("cwd", "")))
    tmux("send-keys", "-t", name, "-l", "--", text)
    time.sleep(0.4)  # иначе TUI считает ввод вставкой и Enter не отправляет
    tmux("send-keys", "-t", name, "Enter")
    return True


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

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def ok(self, good=True):
        self.send(200 if good else 404, json.dumps({"ok": bool(good)}))

    def do_GET(self):
        if "/api/agents" not in self.path:  # агентов опрашивают каждые 2с — не шумим
            self.log_access()
        url = urlparse(self.path)
        q = parse_qs(url.query)

        def arg(k):
            return (q.get(k) or [""])[0]

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


if __name__ == "__main__":
    print(f"Agent Board → http://localhost:{PORT}")
    os.makedirs(NAMES_DIR, exist_ok=True)
    if not os.path.exists(TMUX):
        print("! tmux not found — install it: brew install tmux")
    threading.Thread(target=caffeinate_watcher, daemon=True).start()
    threading.Thread(target=update_checker, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
