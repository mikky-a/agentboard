"""Agents talk to each other through the `board` command and /api/peer/*."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from test_codex_notifications import board

HERE = Path(__file__).resolve().parent
AGENTS = {"claude-a": {"agent": "claude", "project": "proj", "label": "Fix", "model": "Fable 5.2",
                       "status": "idle", "path": "/p", "preview": "old\n⏺ Bash(ls)\nдумаю"},
          "codex-b": {"agent": "codex", "project": "proj", "status": "working", "path": "/p"}}


class PeerApiTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        for name, kwargs in (
            ("tg_agents", {"return_value": AGENTS}),
            ("agent_menu", {"return_value": ""}),
            ("peer_pulse", {"side_effect": lambda n: [time.monotonic()]}),
            ("peer_reply", {"return_value": ("t1", "old reply")}),
            ("send_to_agent", {"side_effect": lambda n, t: self.sent.append((n, t)) or True}),
            ("peer_log", {}),
        ):
            p = patch.object(board, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def test_ls_lists_sessions_with_last_line(self):
        rows = {r["name"]: r for r in board.peer_api("ls", {})["agents"]}
        self.assertEqual(rows["claude-a"]["last"], "думаю")
        self.assertEqual(rows["codex-b"]["status"], "working")

    def test_send_signs_message_and_reports_turn_marker(self):
        out = board.peer_api("send", {"to": "claude-a", "text": "вопрос", "from": "codex-b",
                                      "wait": True})
        self.assertEqual(out, {"ok": True, "to": "claude-a", "turn": "t1", "busy": False})
        name, text = self.sent[0]
        self.assertEqual(name, "claude-a")
        self.assertTrue(text.startswith("[message from agent @codex-b (Codex · proj). "
                                        "It is waiting for your reply"))
        self.assertTrue(text.endswith("\n\nвопрос"))

    def test_send_without_wait_tells_how_to_answer(self):
        board.peer_api("send", {"to": "claude-a", "text": "fyi", "from": "codex-b"})
        self.assertIn('To answer it: board send codex-b "..."', self.sent[0][1])

    def test_refusals(self):
        self.assertIn("own session", board.peer_api("send", {"to": "codex-b", "text": "x",
                                                             "from": "codex-b"})["error"])
        self.assertIn("@claude-a, @codex-b", board.peer_api("send", {"to": "nope", "text": "x"})["error"])
        with patch.object(board, "agent_menu", return_value="Esc to cancel"):
            self.assertIn("menu open", board.peer_api("send", {"to": "claude-a", "text": "x"})["error"])
        self.assertEqual(self.sent, [])

    def test_codex_last_turn_reads_answer_or_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rollout.jsonl"
            ok = {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "u1",
                                                   "last_agent_message": "**Готово**"}}
            empty = {"type": "event_msg", "payload": {
                "type": "task_complete", "turn_id": "u2", "last_agent_message": None,
                "error": {"message": "You've hit your usage limit.",
                          "codex_error_info": "usage_limit_exceeded"}}}
            path.write_text(json.dumps(ok) + "\n")
            self.assertEqual(board.codex_last_turn(str(path)), ("u1", "Готово"))
            path.write_text("\n".join(json.dumps(x) for x in (ok, empty)) + "\n")
            turn, reply = board.codex_last_turn(str(path))
            self.assertEqual(turn, "u2")
            self.assertIn("hit your usage limit", reply)


CODEX_MODEL_PICKER = """
  Select Model and Effort
› 2. GPT-6-Astra            Frontier intelligence for the most demanding work.
  5. GPT-6-Luna (current)   Fast and affordable model for easier tasks.
  enter select · esc back
"""
CODEX_EFFORT_PICKER = """
  Select Reasoning Level for GPT-6-Astra
› 3. High              Greater reasoning depth for complex problems
  5. More reasoning…   Max and Ultra consume usage limits faster
  enter default · s session · esc back
"""
CODEX_CONFIRM = """
  Switch to gpt-6-luna for lower usage?
› 1. Switch
  2. Keep current model
  Press enter to confirm or esc to go back
"""
CODEX_PROMPT = """
• Working (12s • esc to interrupt)
› Ask Codex to do anything
  GPT-6-Astra high · ~/projects/agentboard · Настройте удалённый браузер
  ? for shortcuts                                       ⚠ 4 warnings · f2 to view
"""


class CodexMenuTest(unittest.TestCase):
    def test_codex_dialogs_are_detected_and_prompt_is_not(self):
        for screen, menu in ((CODEX_MODEL_PICKER, True), (CODEX_EFFORT_PICKER, True),
                             (CODEX_CONFIRM, True), (CODEX_PROMPT, False)):
            with patch.object(board, "tmux", return_value=screen):
                self.assertEqual(bool(board.agent_menu("x", "codex")), menu, screen)
        with patch.object(board, "tmux", return_value=CODEX_MODEL_PICKER):
            self.assertEqual(board.agent_menu("x", "opencode"), "")  # unknown TUI: no guess


class PeerDeliveryTest(unittest.TestCase):
    """An idle agent that got the text must start a turn; silence means a dialog ate it."""

    def setUp(self):
        for name, kwargs in (
            ("tg_agents", {"return_value": AGENTS}),
            ("agent_menu", {"return_value": ""}),
            ("peer_reply", {"return_value": ("t1", "")}),
            ("send_to_agent", {"return_value": True}),
            ("peer_log", {}),
            ("tmux", {"return_value": "some dialog\n1. Switch\n"}),
        ):
            p = patch.object(board, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(board.time, "sleep")
        p.start()
        self.addCleanup(p.stop)

    def test_idle_agent_that_stays_silent_is_reported(self):
        with patch.object(board, "peer_pulse", return_value=[1.0]):
            out = board.peer_api("send", {"to": "claude-a", "text": "hi"})
        self.assertIn("did not pick up the message", out["error"])
        self.assertIn("1. Switch", out["error"])

    def test_busy_agent_is_not_checked(self):
        with patch.object(board, "peer_pulse", return_value=[1.0]) as pulse:
            out = board.peer_api("send", {"to": "codex-b", "text": "hi"})
        self.assertTrue(out["ok"])
        self.assertTrue(out["busy"])
        self.assertEqual(pulse.call_count, 1)


class BoardCommandTest(unittest.TestCase):
    """The real `board` script against a real board HTTP server with a fake peer_api."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.calls = []
        self.reads = iter([{"to": "claude-a", "status": "working", "turn": "t1", "reply": ""},
                           {"to": "claude-a", "status": "idle", "turn": "t2", "reply": "ответ"}])

        def fake(op, req):
            self.calls.append((op, req))
            if op == "send":
                return {"ok": True, "to": "claude-a", "turn": "t1", "busy": False}
            if op == "read":
                return next(self.reads)
            return {"agents": [{"name": "claude-a", "agent": "claude", "project": "proj",
                                "label": "Fix", "model": "Fable 5.2", "status": "idle",
                                "cwd": "/p", "last": "думаю"}]}

        for name, kwargs in (("PEER_KEY", {"new": "k" * 48}), ("peer_api", {"side_effect": fake})):
            p = patch.object(board, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        self.server = board.ThreadingHTTPServer(("127.0.0.1", 0), board.Handler)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        cfg = Path(self.home.name) / ".config" / "agentboard"
        cfg.mkdir(parents=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        (cfg / "agent-api.json").write_text(json.dumps({"url": self.url, "key": "k" * 48}))

    def run_board(self, *args):
        env = {"HOME": self.home.name, "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "LANG": "C.UTF-8"}
        with patch.object(board.time, "sleep"):  # server side never sleeps here anyway
            return subprocess.run([sys.executable, str(HERE / "board"), *args], env=env,
                                  capture_output=True, text=True, timeout=30)

    def test_wrong_key_is_rejected(self):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(self.url + "/api/peer/ls", data=b"{}",
                                     headers={"Authorization": "Bearer wrong"})
        with patch.object(board.time, "sleep"), self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(e.exception.code, 401)
        self.assertEqual(self.calls, [])

    def test_ls(self):
        r = self.run_board("ls")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("@claude-a", r.stdout)
        self.assertIn("думаю", r.stdout)

    def test_send_wait_returns_the_reply(self):
        r = self.run_board("send", "claude-a", "как", "дела?", "--wait")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("reply from @claude-a:\n\nответ", r.stdout)
        op, req = self.calls[0]
        self.assertEqual((op, req["to"], req["text"], req["wait"]), ("send", "claude-a", "как дела?", True))
        self.assertEqual(req["from"], "")  # outside tmux: no signature

    def test_unknown_session_error_is_shown(self):
        with patch.object(board, "peer_api", return_value={"error": "no session @x; running: @a"}):
            r = self.run_board("read", "x")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("board: no session @x", r.stderr)


if __name__ == "__main__":
    unittest.main()
