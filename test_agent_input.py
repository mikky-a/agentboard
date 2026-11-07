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


if __name__ == "__main__":
    unittest.main()
