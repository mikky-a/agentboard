"""Antigravity CLI (agy) as a board engine: hooks, logs, start and Telegram."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from test_codex_notifications import board

# the real shape of brain/<id>/.system_generated/logs/transcript_full.jsonl (agy 1.5.12)
STEPS = [
    {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE",
     "content": "<USER_REQUEST>\nold question\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nt\n"
                "</ADDITIONAL_METADATA>"},
    {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "model": "gemini-3.8-flash-low",
     "content": "old answer"},
    {"step_index": 2, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE",
     "content": "<USER_REQUEST>\nfix the bug [agentboard:/tmp/agentboard-1-names/x]\n"
                "</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nmodel\n</USER_SETTINGS_CHANGE>"},
    {"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE", "model": "gemini-4-neon-high",
     "tool_calls": [{"name": "run_command", "args": {"CommandLine": f'tee "{board.NAMES_DIR}/x" <<< "Fix"'}},
                    {"name": "run_command",
                     "args": {"CommandLine": "ls   -la", "toolSummary": "List files"}}]},
    {"step_index": 4, "source": "MODEL", "type": "GENERIC", "status": "DONE",
     "content": "total 0\n" * 50},
    {"step_index": 5, "source": "SYSTEM", "type": "USER_INPUT", "content": "<system>noise</system>"},
    {"step_index": 6, "source": "MODEL", "type": "PLANNER_RESPONSE", "model": "gemini-4-neon-high",
     "content": "Looked around.",
     "tool_calls": [{"name": "write_file", "args": {"Path": "/p/hello.txt", "Content": "hi"}}]},
    {"step_index": 7, "source": "MODEL", "type": "PLANNER_RESPONSE", "model": "gemini-4-neon-high",
     "content": "**Done**: fixed."},
]


class AgyHome(unittest.TestCase):
    """A fake ~/.gemini: transcripts in brain/<id>/, summaries in a sqlite db."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        for name, value in (("AGY_HOME", str(self.home)), ("AGY_ITEMS", {}),
                            ("AGY_SETTINGS", str(self.home / "settings.json"))):
            p = patch.object(board, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.db = sqlite3.connect(self.home / "conversation_summaries.db")
        self.db.execute("CREATE TABLE conversation_summaries (conversation_id text, "
                        "workspace_uris text, last_modified_time datetime, "
                        "nesting_depth integer)")
        self.addCleanup(self.db.close)

    def conversation(self, cid, uris, stamp, depth=0, steps=STEPS):
        path = Path(board.agy_transcript(cid))
        path.parent.mkdir(parents=True)
        path.write_text("".join(json.dumps(s) + "\n" for s in steps))
        self.db.execute("INSERT INTO conversation_summaries VALUES (?, ?, ?, ?)",
                        (cid, json.dumps(uris), stamp, depth))
        self.db.commit()
        return str(path)


class AgyLogTest(AgyHome):
    def test_preview_is_the_last_turn_without_marker_and_noise(self):
        path = self.conversation("c1", [], "2026-11-19 20:00:00.1+00:00")
        self.assertEqual(board.agy_turn_preview(path),
                         "> fix the bug\n\n⏺ run_command(ls -la)\n\nLooked around.\n\n"
                         "⏺ write_file(/p/hello.txt)\n\n**Done**: fixed.")

    def test_last_reply_model_and_id(self):
        path = self.conversation("c1", [], "2026-11-19 20:00:00.1+00:00")
        self.assertEqual(board.agy_last_reply(path), "Looked around.\n\nDone: fixed.")
        self.assertEqual(board.agy_model(path), "gemini-4-neon-high")
        self.assertEqual(board.agy_id(path), "c1")
        self.assertEqual(board.log_model_of("agy", path), "gemini-4-neon-high")

    def append(self, path, *steps):
        path.write_text(path.read_text() + "".join(json.dumps(r) + "\n" for r in steps))

    def test_telegram_gets_each_reply_once_by_log_step(self):
        """The Katya chat. A turn can stop several times (the agent waits for its own
        background task), and agy takes a queued user message right after Stop, before
        the board reads the log. Neither may resend or lose a reply."""
        path = Path(self.conversation("c1", [], "2026-11-19 20:00:00.1+00:00"))
        steps = [json.loads(line) for line in path.read_text().splitlines()]
        path.write_text("".join(json.dumps(r) + "\n" for r in steps[:-1]))
        # first Stop: nothing sent yet — the last turn
        text, mark = board.agy_unsent(str(path))
        self.assertEqual((text, mark), ("Looked around.", 6))
        # woken by its background task, it finishes: only the new reply goes out
        self.append(path, steps[-1])
        self.assertEqual(board.agy_unsent(str(path), mark), ("Done: fixed.", 7))
        self.assertEqual(board.agy_unsent(str(path), 7), ("", 7))
        # the reply, then a queued message from Katya, all before the board reads the log
        self.append(path,
                    {"step_index": 8, "source": "USER_EXPLICIT", "type": "USER_INPUT",
                     "content": "<USER_REQUEST>\nтоп-10\n</USER_REQUEST>"},
                    {"step_index": 9, "source": "MODEL", "type": "PLANNER_RESPONSE",
                     "content": "Готово! Сводка: …"},
                    {"step_index": 10, "source": "USER_EXPLICIT", "type": "USER_INPUT",
                     "content": "<USER_REQUEST>\nКатя: найди топ-10\n</USER_REQUEST>"})
        self.assertEqual(board.agy_unsent(str(path), 7), ("Готово! Сводка: …", 9))
        # with no mark (first notice after a restart) — the last turn that has text
        self.assertEqual(board.agy_unsent(str(path)), ("Готово! Сводка: …", 9))
        self.assertEqual(board.agy_unsent(str(path), 9), ("", 10))

    def test_window_skips_the_torn_first_line(self):
        path = self.conversation("c1", [], "2026-11-19 20:00:00.1+00:00")
        size = os.path.getsize(path)
        items = board.agy_items(path, window=size - 10)
        self.assertEqual(items[-1], ("assistant", "**Done**: fixed."))

    def test_finds_the_fresh_conversation_of_the_folder(self):
        created = 1795118400  # 2026-11-19 20:00:00 UTC
        self.conversation("old", ["file:///p/my%20proj"], "2026-11-19 19:00:00.1+00:00")
        self.conversation("sub", ["file:///p/my%20proj"], "2026-11-19 20:05:00.1+00:00", depth=1)
        other = self.conversation("other", ["file:///p/else"], "2026-11-19 20:06:00.1+00:00")
        mine = self.conversation("mine", ["file:///p/my%20proj"], "2026-11-19 20:01:00.123456789+00:00")
        self.assertEqual(board.find_log("agy", "/p/my proj", created, set()), mine)
        self.assertEqual(board.find_log("agy", "/p/my proj", created, {mine}), "")
        self.assertEqual(board.find_log("agy", "/p/else", created, set()), other)

    def test_trust_adds_the_folder_once_and_keeps_settings(self):
        Path(board.AGY_SETTINGS).write_text(json.dumps({"colorScheme": "dark"}))
        board.pre_trust("agy", "/p/proj")
        board.pre_trust("agy", "/p/proj")
        st = json.loads(Path(board.AGY_SETTINGS).read_text())
        self.assertEqual(st, {"colorScheme": "dark", "trustedWorkspaces": ["/p/proj"]})


class AgyHooksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.hooks, self.script = d / "config" / "hooks.json", d / "config" / "agent-status.sh"
        for name, value in (("AGY_HOOKS_FILE", str(self.hooks)),
                            ("AGY_HOOK_SCRIPT", str(self.script))):
            p = patch.object(board, name, value)
            p.start()
            self.addCleanup(p.stop)

    def test_install_keeps_user_hooks_and_is_idempotent(self):
        self.hooks.parent.mkdir()
        mine = {"enabled": True, "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command",
                                                                            "command": "x"}]}]}
        self.hooks.write_text(json.dumps({"mine": mine}))
        self.assertEqual(board._agy_missing(json.loads(self.hooks.read_text())),
                         ["PreInvocation", "Stop"])
        board._install_agy()
        cfg = json.loads(self.hooks.read_text())
        self.assertEqual(cfg["mine"], mine)
        self.assertEqual(cfg["agent-status"]["Stop"],
                         [{"type": "command", "command": f"{self.script} idle"}])
        self.assertNotIn("PreToolUse", cfg["agent-status"])  # «{}» there denies the tool
        self.assertEqual(board._agy_missing(cfg), [])
        self.assertTrue(board._agy_script_ok() and os.access(self.script, os.X_OK))
        self.assertTrue(Path(str(self.hooks) + ".agentboard-bak").exists())
        before = self.hooks.read_text()
        board._install_agy()
        self.assertEqual(self.hooks.read_text(), before)

    def run_script(self, arg, payload, tmux=True):
        board._install_agy()
        home = Path(self.tmp.name) / "home"
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / "tmux").write_text("#!/bin/sh\necho sess\n")
        (bin_dir / "tmux").chmod(0o755)
        env = {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin"}
        if tmux:
            env["TMUX"] = "/tmp/fake,1,0"
        r = subprocess.run([str(self.script), arg], input=json.dumps(payload, separators=(",", ":")),
                           capture_output=True, text=True, env=env, timeout=10)
        status = home / ".claude" / "agent-status" / "sess"
        return r.stdout.strip(), status.read_text().strip() if status.exists() else None

    def test_script_answers_json_and_writes_status(self):
        self.assertEqual(self.run_script("working", {"invocationNum": 0}), ("{}", "working"))
        self.assertEqual(self.run_script("idle", {"fullyIdle": True}), ("{}", "idle"))

    def test_stop_with_background_subagents_ends_the_turn(self):
        self.run_script("working", {"conversationId": "main", "lastUserInput": "hi"})
        self.assertEqual(self.run_script("idle", {"conversationId": "main", "lastUserInput": "hi",
                                                  "fullyIdle": False}), ("{}", "idle"))

    def test_subagent_events_do_not_touch_the_card(self):
        main = {"conversationId": "main", "lastUserInput": "hi"}
        sub = {"conversationId": "sub", "lastUserInput": ""}
        self.run_script("working", main)
        self.run_script("idle", {**main, "fullyIdle": False})
        self.assertEqual(self.run_script("working", sub), ("{}", "idle"))
        self.run_script("working", main)
        self.assertEqual(self.run_script("idle", {**sub, "fullyIdle": True}), ("{}", "working"))
        # the main conversation woken by its subagent, even without user input, still counts
        self.assertEqual(self.run_script("idle", {"conversationId": "main", "lastUserInput": ""}),
                         ("{}", "idle"))

    def test_subagent_events_before_the_main_one_are_ignored(self):
        sub = {"conversationId": "sub", "lastUserInput": ""}
        self.assertEqual(self.run_script("working", sub), ("{}", None))

    def test_stop_with_continuation_keeps_working(self):
        main = {"conversationId": "main", "lastUserInput": "hi"}
        self.run_script("working", main)
        self.assertEqual(self.run_script("idle", {**main, "continuationActive": True}),
                         ("{}", "working"))

    def test_outside_tmux_only_answers(self):
        self.assertEqual(self.run_script("idle", {"fullyIdle": True}, tmux=False), ("{}", None))


class AgyStartTest(unittest.TestCase):
    def start(self, **kw):
        cmds = []
        with patch.object(board, "tmux", side_effect=lambda *a: cmds.append(a) or ""), \
             patch.object(board, "free_name", return_value="proj"), \
             patch.object(board, "pre_trust"), \
             patch.object(board, "tg_unbind"), \
             patch.object(board, "load_board", return_value={"cards": []}), \
             patch.object(board, "save_board"), \
             patch.object(board, "name_tail", return_value="[agentboard:/n/proj]"):
            board.new_agent("/tmp", "proj", **kw)
        return next(a for a in cmds if a[0] == "new-session")[-1]

    def test_command_line(self):
        with patch.object(board, "AGY", "/bin/agy"), \
             patch.object(board, "AGY_BYPASS", ["--dangerously-skip-permissions"]):
            cmd = self.start(prompt="fix it", agent="agy", model="gemini-4-neon-high")
        self.assertIn("export AGY_CLI_DISABLE_AUTO_UPDATE=1; /bin/agy --dangerously-skip-permissions "
                      "--model gemini-4-neon-high -i 'fix it\n\n[agentboard:/n/proj]'", cmd)

    def test_resume_keeps_the_model(self):
        card = {"id": "c1", "cwd": "/p", "project": "proj", "agent": "agy", "model": "gemini-3.8-flash-low"}
        cmds = []
        with patch.object(board, "load_board", return_value={"cards": [card]}), \
             patch.object(board, "save_board"), patch.object(board, "pre_trust"), \
             patch.object(board, "tg_unbind"), patch.object(board, "open_in_terminal"), \
             patch.object(board, "tmux_ok", return_value=False), \
             patch.object(board, "free_name", return_value="proj"), \
             patch.object(board, "AGY", "/bin/agy"), patch.object(board, "AGY_BYPASS", []), \
             patch.object(board, "tmux", side_effect=lambda *a: cmds.append(a) or ""):
            self.assertTrue(board.resume_card("c1"))
        cmd = next(a for a in cmds if a[0] == "new-session")[-1]
        self.assertIn("export AGY_CLI_DISABLE_AUTO_UPDATE=1; /bin/agy --conversation c1 "
                      "--model gemini-3.8-flash-low", cmd)


class AgyModelsTest(unittest.TestCase):
    def test_catalog_from_cli(self):
        out = ("gemini-4-neon-high\tGemini 4 Neon (High)\n"
               "claude-opus-5-5-low\tClaude Opus 5.5 (Low)\n")
        run = subprocess.CompletedProcess([], 0, stdout=out, stderr="Fetching available models...\n")
        with tempfile.TemporaryDirectory() as d:
            settings = Path(d) / "settings.json"
            with patch.object(board, "AGY_SETTINGS", str(settings)), \
                 patch.object(board.subprocess, "run", return_value=run) as cli:
                with patch.dict(board.models_cache, clear=True):
                    first = board.agent_models("agy")
                settings.write_text(json.dumps({"model": "Claude Opus 5.5 (Low)"}))
                with patch.dict(board.models_cache, clear=True):
                    chosen = board.agent_models("agy")
        self.assertEqual(first, [{"id": "gemini-4-neon-high", "l": "Gemini 4 Neon (High)", "def": True},
                                 {"id": "claude-opus-5-5-low", "l": "Claude Opus 5.5 (Low)", "def": False}])
        self.assertEqual([m["def"] for m in chosen], [False, True])  # agy's own default wins
        self.assertEqual(cli.call_args.kwargs["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"], "1")
        self.assertEqual(board.model_label("gemini-4-neon-high"), "Gemini 4 Neon (High)")


CATALOG = [{"id": "gemini-4-hydrogen-high", "l": "Gemini 4 Hydrogen (High)", "def": True},
           {"id": "gemini-4-hydrogen-xhigh", "l": "Gemini 4 Hydrogen (XHigh)", "def": False},
           {"id": "gemini-4-neon-low", "l": "Gemini 4 Neon (Low)", "def": False},
           {"id": "gemini-3.8-flash-low", "l": "Gemini 3.8 Flash (Low)", "def": False},
           {"id": "gemini-3.8-flash-high", "l": "Gemini 3.8 Flash (High)", "def": False},
           {"id": "claude-opus-5-5-low", "l": "Claude Opus 5.5 (Low)", "def": False},
           {"id": "claude-opus-5-5-high", "l": "Claude Opus 5.5 (High)", "def": False}]


class ModelAliasTest(unittest.TestCase):
    def setUp(self):
        p = patch.object(board, "agent_models", return_value=CATALOG)
        p.start()
        self.addCleanup(p.stop)

    def test_short_names(self):
        for word, mid in (("hydrogen", "gemini-4-hydrogen-high"),       # default wins
                          ("Hydrogen-XHigh", "gemini-4-hydrogen-xhigh"),
                          ("hydrogen-high", "gemini-4-hydrogen-high"),  # high is not xhigh
                          ("flash", "gemini-3.8-flash-high"),           # the default's step
                          ("flash-low", "gemini-3.8-flash-low"),
                          ("opus", "claude-opus-5-5-high"),
                          ("neon", "gemini-4-neon-low"),                # the only neon
                          ("gemini-3.8-flash-low", "gemini-3.8-flash-low")):
            self.assertEqual(board.model_alias("agy", word), mid, word)

    def test_task_words_are_not_models(self):
        for word in ("high", "fix", "gemini", "low-high", "hydrogenate", "argon"):
            self.assertEqual(board.model_alias("agy", word), "", word)

    def test_new_agent_from_telegram_takes_the_model_word(self):
        started = []
        with patch.object(board, "tg_project", return_value=("proj", "/p", {})), \
             patch.object(board, "new_agent", side_effect=lambda *a: started.append(a) or "proj"), \
             patch.object(board, "AGENT_BINS", dict(board.AGENT_BINS, agy="/bin/sh")), \
             patch.object(board, "tg_post"), patch.object(board, "tg_bind"), \
             patch.object(board, "tg_last_set"), patch.object(board, "tg_log"), \
             patch.object(board, "tg_shared", return_value=False):
            for text in ("@proj new agy hydrogen-xhigh fix the bug", "@proj new agy fix the bug",
                         "@proj new gemini flash-low", "@proj new agy high priority: fix"):
                m = board.TG_NEW_RE.fullmatch(text)
                board.tg_new_agent(m.group(1), m.group(2), m.group(3).strip(), 1, None, 2)
        self.assertEqual([(a[2], a[3], a[4]) for a in started], [
            ("fix the bug", "agy", "gemini-4-hydrogen-xhigh"),
            ("fix the bug", "agy", "gemini-4-hydrogen-high"),
            ("", "agy", "gemini-3.8-flash-low"),
            ("high priority: fix", "agy", "gemini-4-hydrogen-high")])

    def test_model_id_is_not_taken_for_the_engine(self):
        m = board.TG_NEW_RE.fullmatch("@proj new gemini-4-hydrogen-xhigh task")
        self.assertIsNone(m.group(2))


class AgyModelSwitchTest(unittest.TestCase):
    def setUp(self):
        board.AGENT_INPUT_LOCKS.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Path(self.tmp.name) / "settings.json"
        self.settings.write_text(json.dumps({"model": "Gemini 4 Hydrogen (High)", "x": 1}))
        for name, kwargs in (
            ("load_board", {"return_value": {"cards": [{"tmux": "a", "agent": "agy"}]}}),
            ("claim_naming", {"return_value": {"agent": "agy", "cwd": "/p"}}),
            ("agent_models", {"return_value": CATALOG}),
            ("AGY_SETTINGS", {"new": str(self.settings)}),
        ):
            p = patch.object(board, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def test_short_name_is_expanded_and_gets_no_naming_tail(self):
        with patch.object(board, "paste_to_agent", return_value=True) as paste, \
             patch.object(board, "tmux_ok", return_value=True), \
             patch.object(board.threading, "Thread") as thread, \
             patch.object(board.time, "sleep"):
            self.assertTrue(board.send_to_agent("a", "/model hydrogen-xhigh"))
        paste.assert_called_once_with("a", "/model gemini-4-hydrogen-xhigh")
        board.claim_naming.assert_not_called()
        self.assertEqual(thread.call_args.kwargs["args"], ("Gemini 4 Hydrogen (High)",))

    def test_default_comes_back_after_agy_rewrites_it(self):
        def agy_switches(_):
            self.settings.write_text(json.dumps({"model": "Gemini 4 Hydrogen (XHigh)", "x": 1}))
        with patch.object(board.time, "sleep", side_effect=agy_switches):
            board.agy_keep_default("Gemini 4 Hydrogen (High)", wait=5)
        self.assertEqual(json.loads(self.settings.read_text()),
                         {"model": "Gemini 4 Hydrogen (High)", "x": 1})


class AgyTelegramSwitchTest(unittest.TestCase):
    """@имя /model hydrogen-xhigh из телеграма: подтверждение моделью, опечатка — отказ."""

    def setUp(self):
        self.posts = []
        for name, kwargs in (
            ("TG_CHAT", {"new": "1"}),
            ("tg_agents", {"return_value": {"sink": {"agent": "agy", "label": "Sink"}}}),
            ("tg_post", {"side_effect": lambda text, reply_to=None, chat=None: self.posts.append(text)}),
            ("tg_react", {"return_value": True}),
            ("tg_last_set", {}), ("tg_log", {}), ("tg_bind", {"return_value": False}),
            ("agent_menu", {"return_value": ""}),
            ("agent_models", {"return_value": CATALOG}),
        ):
            p = patch.object(board, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def handle(self, text):
        board.tg_handle({"chat": {"id": 1}, "message_id": 7, "text": "@sink " + text})

    def test_switch_is_confirmed_with_the_model(self):
        with patch.object(board, "send_to_agent", return_value=True) as send:
            self.handle("/model hydrogen-xhigh")
        send.assert_called_once_with("sink", "/model hydrogen-xhigh")
        self.assertEqual(self.posts, ["@sink → Gemini 4 Hydrogen (XHigh)"])

    def test_unknown_model_is_not_sent(self):
        with patch.object(board, "send_to_agent") as send:
            self.handle("/model hydrogn")
        send.assert_not_called()
        self.assertIn("Не знаю у Antigravity модели «hydrogn»", self.posts[0])


class AgyTelegramTest(unittest.TestCase):
    def test_new_command_names(self):
        for text, engine in (("@proj new agy fix it", "agy"), ("@proj new Gemini", "Gemini"),
                             ("@proj new antigravity", "antigravity"), ("@proj new codex", "codex")):
            m = board.TG_NEW_RE.match(text)
            self.assertEqual(m.group(2), engine)
            self.assertEqual(board.TG_ENGINES.get(engine.lower(), engine.lower()),
                             "agy" if engine != "codex" else "codex")

    def test_menu_footer_is_a_menu(self):
        for footer in ("Keyboard: ↑/↓ Navigate  ←/→ Effort  enter Select  esc Go Back",
                       "  ↑/↓ Navigate · enter Confirm",
                       "Keyboard: ↑/↓ Navigate  ←/→ Change  enter Select  esc Clear Search/Exit"):
            self.assertTrue(board.MENU_HINTS["agy"].search(footer), footer)
        self.assertFalse(board.MENU_HINTS["agy"].search("? for shortcuts      Gemini 4 Neon · high"))


if __name__ == "__main__":
    unittest.main()
