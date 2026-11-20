"""Input regression tests; integration uses an isolated tmux and a fake TUI."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, call, patch

from test_codex_notifications import board


class AgentInputTest(unittest.TestCase):
    def setUp(self):
        board.AGENT_INPUT_LOCKS.clear()
        for name, kwargs in (
            ("load_board", {"return_value": {"cards": [{"tmux": "sink", "agent": "codex"}]}}),
            ("claim_naming", {"return_value": None}),
        ):
            p = patch.object(board, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def test_codex_uses_paste_then_separate_enter(self):
        with patch.object(board, "paste_to_agent", return_value=True) as paste, \
             patch.object(board, "tmux_ok", return_value=True) as tmux, \
             patch.object(board.time, "sleep"):
            self.assertTrue(board.send_to_agent("sink", "first\nsecond"))
            paste.assert_called_once_with("sink", "first\nsecond")
            self.assertEqual(tmux.call_args_list, [call("has-session", "-t", "sink"),
                                                  call("send-keys", "-t", "sink", "Enter")])

    def test_failed_paste_does_not_press_enter(self):
        with patch.object(board, "paste_to_agent", return_value=False), \
             patch.object(board, "tmux_ok", return_value=True) as tmux:
            self.assertFalse(board.send_to_agent("sink", "message"))
            tmux.assert_called_once_with("has-session", "-t", "sink")

    def test_failed_enter_is_reported(self):
        with patch.object(board, "paste_to_agent", return_value=True), \
             patch.object(board, "tmux_ok", side_effect=[True, False]), \
             patch.object(board.time, "sleep"):
            self.assertFalse(board.send_to_agent("sink", "message"))

    def test_claude_keeps_existing_input_method(self):
        with patch.object(board, "load_board", return_value={"cards": []}), \
             patch.object(board, "paste_to_agent") as paste, \
             patch.object(board, "tmux_ok", return_value=True) as tmux, \
             patch.object(board.time, "sleep"):
            self.assertTrue(board.send_to_agent("sink", "message"))
            paste.assert_not_called()
            self.assertIn(call("send-keys", "-t", "sink", "-l", "--", "message"),
                          tmux.call_args_list)

    def test_failed_buffer_load_cleans_up_without_pasting(self):
        with patch.object(board.subprocess, "run", return_value=Mock(returncode=1)), \
             patch.object(board, "tmux_ok") as paste, \
             patch.object(board, "tmux") as cleanup:
            self.assertFalse(board.paste_to_agent("sink", "message"))
            paste.assert_not_called()
            self.assertEqual(cleanup.call_args.args[0], "delete-buffer")

    def test_real_tmux_multiline_unicode_is_one_bracketed_paste(self):
        # The receiving process records raw terminal bytes without running any agent.
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            script, ready, received = root / "sink.py", root / "ready", root / "received"
            script.write_text(
                "import os,sys,tty,time,select,pathlib\n"
                "tty.setraw(0)\n"
                "os.write(1,b'\\x1b[?2004h')\n"
                "pathlib.Path(sys.argv[1]).touch()\n"
                "data=b''\n"
                "while select.select([0],[],[],5)[0]:\n"
                " data+=os.read(0,65536)\n"
                " if data.endswith(b'\\r'):break\n"
                "pathlib.Path(sys.argv[2]).write_bytes(data)\n"
            )
            tmux = ["/usr/bin/tmux", "-L", f"agentboard-input-test-{os.getpid()}"]
            command = shlex.join([sys.executable, str(script), str(ready), str(received)])
            try:
                subprocess.run([*tmux, "new-session", "-d", "-s", "sink", command],
                               check=True, capture_output=True, timeout=5)
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "fake TUI failed to start")
                time.sleep(0.05)
                text = "Проверь разрешение фото\n" + "длинный текст " * 200 + "\nпоследняя строка"
                with patch.object(board, "TMUX_CMD", tmux):
                    self.assertTrue(board.send_to_agent("sink", text))
                deadline = time.monotonic() + 5
                while not received.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(received.exists(), "fake TUI received no submit")
                self.assertEqual(received.read_bytes(),
                                 b"\x1b[200~" + text.encode() + b"\x1b[201~\r")
            finally:
                subprocess.run([*tmux, "kill-server"], capture_output=True, timeout=5)


STATUS_SCREEN = """
   Settings  Status   Config   Usage   Stats

   Version:           2.1.296
   Model:             fable (claude-fable-5-1)

   Esc to cancel
"""
MODEL_SCREEN = """
   ❯ 3.  Fable 5.1 ✔            For your toughest challenges
   ● High effort (default) ←/→ to adjust
   Enter to set as default · s to use this session only · Esc to cancel
"""
IDLE_SCREEN = """
✻ Churned for 31m 30s · done 5:51 PM

────────────────────────────────────────────────────────────────────────────────
❯ 
────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on · 6 shells · esc to interrupt · ← for agents
"""


PERMISSION_SCREEN = """\
  ⎿  $ curl -s -m 40 -o crop.png https://example.com/x (8m 26s)
     (ctrl+b ctrl+b (twice) to run in background)
─────────────────────────────────────────────────────────────────────────────────────────
 Bash command · from the general-purpose agent
 Cut screenshot into parts
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 │ cd $S/bbcover; mkdir -p shots; rm -f shots/*
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 │ Dangerous rm operation on statically-unresolvable target:
 │ /home/agent/projects/scandalio/shots/*
 Do you want to proceed?
 ❯ 1. Yes
   2. No
 Esc to cancel · Tab to amend · ctrl+x ctrl+k twice to stop background agents
"""


class MenuNotificationTest(unittest.TestCase):
    """A subagent's permission question goes past the status hooks: the card stays
    "working" and nobody is told. The board sends the question itself."""

    def setUp(self):
        self.queued, self.screen, self.watched = [], PERMISSION_SCREEN, False
        cards = {"cards": [{"tmux": "sc", "agent": "claude", "project": "scandalio", "id": "u1"}],
                 "labels": {"u1": "Тренды"}}

        def tmux(*a):
            return "sc\n" if a[0] == "list-sessions" else self.screen

        for name, kwargs in (
            ("load_board", {"return_value": cards}),
            ("tmux", {"side_effect": tmux}),
            ("tg_enqueue", {"side_effect": lambda n, t: self.queued.append((n, t))}),
            ("tg_watched", {"side_effect": lambda n: self.watched}),
            ("tg_log", {}),
            ("TG_MENU_SEEN", {"new": {}}),
            ("TG_MENU_TOLD", {"new": {}}),
            ("PUBLIC_URL", {"new": ""}),
        ):
            p = patch.object(board, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def test_question_is_sent_once_after_it_stays(self):
        board.menu_notifications(100)
        board.menu_notifications(105)
        self.assertEqual(self.queued, [])  # a menu the user is clicking through
        board.menu_notifications(111)
        board.menu_notifications(200)
        self.assertEqual(len(self.queued), 1)
        name, text = self.queued[0]
        self.assertEqual(name, "sc")
        self.assertTrue(text.startswith("🟡 Тренды · scandalio · @sc"))
        self.assertIn("Do you want to proceed?\n ❯ 1. Yes\n   2. No", text)
        self.assertNotIn("8m 26s", text)  # what is above the dialog ticks
        self.assertNotIn("╌╌╌", text)
        self.assertIn("номер пункта", text)

    def test_closed_and_reopened_question_is_sent_again(self):
        for t in (100, 111):
            board.menu_notifications(t)
        self.screen = IDLE_SCREEN
        board.menu_notifications(120)
        self.screen = PERMISSION_SCREEN
        for t in (130, 141):
            board.menu_notifications(t)
        self.assertEqual(len(self.queued), 2)

    def test_nothing_while_watched_or_already_shown_in_telegram(self):
        self.watched = True
        for t in (100, 111):
            board.menu_notifications(t)
        self.assertEqual(self.queued, [])
        self.watched = False
        board.TG_MENU_TOLD["sc"] = board.menu_core(board.agent_menu("sc"))
        board.menu_notifications(120)
        self.assertEqual(self.queued, [])


class TelegramMenuTest(unittest.TestCase):
    """/status в Claude открывает меню, и следующие сообщения из телеграма в нём
    молча пропадали. Теперь текст в меню не печатается, а юзер видит экран."""

    def setUp(self):
        self.posts = []
        for name, kwargs in (
            ("TG_CHAT", {"new": "1"}),
            ("tg_agents", {"return_value": {"sink": {"agent": "claude", "label": "Sink"}}}),
            ("tg_post", {"side_effect": lambda text, reply_to=None, chat=None: self.posts.append(text)}),
            ("tg_react", {"return_value": True}),
            ("tg_last_set", {}),
            ("tg_log", {}),
        ):
            p = patch.object(board, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(board.time, "sleep")
        p.start()
        self.addCleanup(p.stop)

    def handle(self, text):
        board.tg_handle({"chat": {"id": 1}, "message_id": 7, "text": "@sink " + text})

    def test_menu_is_detected_only_by_its_hint(self):
        for screen, menu in ((STATUS_SCREEN, True), (MODEL_SCREEN, True), (IDLE_SCREEN, False)):
            with patch.object(board, "tmux", return_value=screen):
                self.assertEqual(bool(board.agent_menu("sink")), menu, screen)

    def test_text_is_not_typed_into_open_menu(self):
        with patch.object(board, "tmux", return_value=MODEL_SCREEN), \
             patch.object(board, "send_to_agent") as send:
            self.handle("а мы делали тест object swap?")
        send.assert_not_called()
        self.assertEqual(len(self.posts), 1)
        self.assertIn("открыто меню", self.posts[0])
        self.assertIn("Esc to cancel", self.posts[0])

    def test_number_picks_item_of_open_menu(self):
        with patch.object(board, "tmux", return_value=PERMISSION_SCREEN), \
             patch.object(board, "send_key", return_value=True) as key, \
             patch.object(board, "send_to_agent") as send:
            self.handle("1")
        key.assert_called_once_with("sink", "1")
        send.assert_not_called()

    def test_number_without_menu_is_plain_text(self):
        with patch.object(board, "tmux", return_value=IDLE_SCREEN), \
             patch.object(board, "send_to_agent", return_value=True) as send:
            self.handle("2")
        send.assert_called_once_with("sink", "2")

    def test_keys_still_reach_open_menu(self):
        with patch.object(board, "tmux", return_value=STATUS_SCREEN), \
             patch.object(board, "send_key", return_value=True) as key:
            self.handle("/esc")
        key.assert_called_once_with("sink", "esc")

    def test_slash_command_that_opens_menu_sends_screen(self):
        screens = iter([IDLE_SCREEN, STATUS_SCREEN])
        with patch.object(board, "tmux", side_effect=lambda *a: next(screens)), \
             patch.object(board, "send_to_agent", return_value=True) as send:
            self.handle("/status")
        send.assert_called_once_with("sink", "/status")
        self.assertEqual(len(self.posts), 1)
        self.assertIn("Экран @sink", self.posts[0])
        self.assertIn("Version:", self.posts[0])

    def test_plain_message_at_prompt_gets_reaction_only(self):
        with patch.object(board, "tmux", return_value=IDLE_SCREEN), \
             patch.object(board, "send_to_agent", return_value=True) as send:
            self.handle("привет")
        send.assert_called_once_with("sink", "привет")
        self.assertEqual(self.posts, [])
        board.tg_react.assert_called_once_with(7, chat="1")


if __name__ == "__main__":
    unittest.main()
