"""Offline regression tests: no Telegram requests and no live board mutations."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


with tempfile.TemporaryDirectory() as data_dir:
    with patch.dict(os.environ, {"AGENTBOARD_DATA": data_dir}):
        spec = importlib.util.spec_from_file_location(
            "board_under_test", Path(__file__).with_name("agentboard.py"))
        board = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(board)


def message(text="progress", phase="commentary", **overrides):
    payload = {"type": "message", "role": "assistant", "phase": phase,
               "content": [{"type": "output_text", "text": text}]}
    payload.update(overrides)
    return {"type": "response_item", "timestamp": "2026-10-16T15:00:00Z",
            "payload": payload}


class CodexNotificationsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "rollout.jsonl"
        self.path.touch()
        self.queue_path = Path(self.tmp.name) / "queue.json"
        self.cards = [{"agent": "codex", "tmux": "codex-test", "project": "test",
                       "rollout": str(self.path), "label": "Test"}]
        board.TG_QUEUE.clear()
        board.TG_CODEX_CURSORS.clear()
        board.TG_CODEX_INITIALIZED = False
        self.now = board.iso_epoch("2026-10-16T15:00:01Z")
        for target, value in (
            ("TG_QUEUE_FILE", str(self.queue_path)), ("PUBLIC_URL", ""),
        ):
            p = patch.object(board, target, value)
            p.start()
            self.addCleanup(p.stop)
        for target, kwargs in (
            ("codex_notification_cards", {"return_value": self.cards}),
            ("log_valid", {"return_value": True}),
            ("being_watched", {"return_value": False}),
            ("tg_log", {}),
            ("tg_api", {"side_effect": AssertionError("Network forbidden")}),
        ):
            p = patch.object(board, target, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def append(self, *records):
        with self.path.open("ab") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False).encode() + b"\n")

    def poll(self):
        return board.codex_notifications(self.now)

    def restart(self):
        board.TG_QUEUE.clear()
        board.TG_CODEX_CURSORS.clear()
        board.TG_CODEX_INITIALIZED = False
        board.tg_queue_load()

    def texts(self):
        return ["\n\n".join(chunks) for name, chunks in board.TG_QUEUE]

    def test_first_enable_skips_history_then_sends_progress_and_final(self):
        self.append(message("old"))
        self.poll()
        self.assertEqual(board.TG_QUEUE, [])
        self.append(message("Checking"), message("Done", "final_answer"))
        self.poll()
        self.assertEqual(self.texts(), ["⏳ Test · test · @codex-test\n\nChecking",
                                        "✅ Test · test · @codex-test\n\nDone"])
        self.poll()
        self.assertEqual(len(board.TG_QUEUE), 2)

    def test_restart_keeps_pending_queue_without_duplicate(self):
        self.poll()
        self.append(message())
        self.poll()
        self.restart()
        self.poll()
        self.assertEqual(len(board.TG_QUEUE), 1)
        # Simulate a successfully delivered queue item and restart again.
        board.TG_QUEUE.clear()
        board.tg_queue_save()
        self.restart()
        self.poll()
        self.assertEqual(board.TG_QUEUE, [])
        self.append(message("next"))
        self.poll()
        self.assertEqual(len(board.TG_QUEUE), 1)

    def test_messages_written_during_downtime_are_sent(self):
        self.poll()
        self.append(message("during restart"))
        self.restart()
        self.poll()
        self.assertIn("during restart", self.texts()[0])

    def test_partial_utf8_record_is_retried_including_at_bootstrap(self):
        self.append(message("old"))
        line = json.dumps(message("Проверяю"), ensure_ascii=False).encode() + b"\n"
        cut = line.index("П".encode()) + 1
        with self.path.open("ab") as f:
            f.write(line[:cut])
        self.poll()
        self.assertEqual(board.TG_QUEUE, [])
        with self.path.open("ab") as f:
            f.write(line[cut:])
        self.poll()
        self.assertIn("Проверяю", self.texts()[0])
        self.poll()
        self.assertEqual(len(board.TG_QUEUE), 1)

    def test_tools_reasoning_user_and_duplicate_events_are_ignored(self):
        self.poll()
        self.append(
            message("user", role="user"),
            message("private", type="reasoning"),
            message("private", channel="analysis"),
            message("private", phase="analysis"),
            {"type": "event_msg", "payload": {"type": "item_completed", "item": message()}},
            {"type": "response_item", "payload": {"type": "function_call_output", "output": "tool"}},
            message("public"),
        )
        self.poll()
        self.assertEqual(len(board.TG_QUEUE), 1)
        self.assertTrue(self.texts()[0].endswith("public"))

    def test_long_tool_output_does_not_hide_progress_or_truncate_final(self):
        self.poll()
        final = "finished " * 3000
        self.append(message("before tool"),
                    {"type": "response_item", "payload": {"type": "function_call_output",
                                                           "output": "x" * 500_000}},
                    message(final, "final_answer"))
        self.poll()
        self.assertEqual(len(board.TG_QUEUE), 2)
        chunks = board.TG_QUEUE[1][1]
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 3900 for c in chunks))
        self.assertEqual(" ".join(" ".join(chunks).split()),
                         " ".join(("✅ Test · test · @codex-test\n\n" + final).split()))

    def test_legacy_queue_migration_preserves_pending_messages(self):
        self.queue_path.write_text(json.dumps([["old", ["pending"]]]))
        self.restart()
        self.append(message("history"))
        self.poll()
        self.assertEqual(board.TG_QUEUE, [["old", ["pending"]]])
        self.assertTrue(json.loads(self.queue_path.read_text())["codex_initialized"])

    def test_write_failure_rolls_back_queue_and_cursor(self):
        self.poll()
        previous = dict(board.TG_CODEX_CURSORS)
        self.append(message())
        with patch.object(board, "tg_queue_save", return_value=False):
            self.poll()
        self.assertEqual(board.TG_QUEUE, [])
        self.assertEqual(board.TG_CODEX_CURSORS, previous)
        self.poll()
        self.assertEqual(len(board.TG_QUEUE), 1)

    def test_new_session_after_startup_is_read_from_beginning(self):
        self.cards.clear()
        self.poll()
        self.append(message("first message"))
        self.cards.append({"tmux": "new", "rollout": str(self.path), "project": "test"})
        self.poll()
        self.assertIn("first message", self.texts()[0])

    def test_watched_and_stale_messages_are_not_replayed(self):
        self.poll()
        self.append(message("watched"))
        with patch.object(board, "being_watched", return_value=True):
            self.poll()
        stale = message("stale")
        stale["timestamp"] = "2026-10-15T15:00:00Z"
        self.append(stale)
        self.poll()
        self.restart()
        self.poll()
        self.assertEqual(board.TG_QUEUE, [])

    def test_file_replacement_and_truncation_reset_cursor(self):
        self.poll()
        self.append(message("long" * 1000))
        self.poll()
        self.path.write_bytes(b"")
        self.append(message("after truncation"))
        self.poll()
        replacement = self.path.with_suffix(".new")
        replacement.write_text(json.dumps(message("after replacement")) + "\n")
        replacement.replace(self.path)
        self.poll()
        self.assertEqual(len(board.TG_QUEUE), 3)

    def test_idle_hook_does_not_repeat_codex_final_but_waiting_still_works(self):
        class StopLoop(Exception):
            pass

        self.poll()
        self.append(message("Done", "final_answer"))
        for status in ("idle", "waiting"):
            with self.subTest(status=status):
                sleeps = [None, StopLoop] if status == "idle" else [None, None, StopLoop]
                with patch.object(board, "notify_state_load", return_value=({}, {}, {})), \
                     patch.object(board, "notify_state_save"), \
                     patch.object(board.os, "listdir", return_value=["codex-test"]), \
                     patch.object(board, "hook_status", return_value=(status, self.now)), \
                     patch.object(board, "claude_logs", return_value={}), \
                     patch.object(board.time, "time", return_value=self.now), \
                     patch.object(board.time, "sleep", side_effect=sleeps), \
                     patch.object(board, "get_agents", return_value={"agents": [
                         {"name": "codex-test", "project": "test", "preview": "Permission?"}
                     ]}) as agents, \
                     patch.object(board, "tg_enqueue") as enqueue:
                    with self.assertRaises(StopLoop):
                        board.notifier()
                    if status == "idle":
                        agents.assert_not_called()
                        enqueue.assert_not_called()
                    else:
                        agents.assert_called_once()
                        enqueue.assert_called_once_with(
                            "codex-test", "🟡 test · @codex-test\n\nPermission?")
        # One final from the log; the idle hook did not append the preview again.
        self.assertEqual(len(board.TG_QUEUE), 1)

    def test_claude_turn_end_still_sends_full_reply(self):
        class StopLoop(Exception):
            pass

        self.cards.clear()
        with patch.object(board, "notify_state_load", return_value=({}, {}, {})), \
             patch.object(board, "notify_state_save"), \
             patch.object(board.os, "listdir", return_value=[]), \
             patch.object(board, "hook_status", return_value=("working", self.now)), \
             patch.object(board, "claude_logs", return_value={"claude-test": "dummy"}), \
             patch.object(board, "last_turn_end", return_value=("2026-10-16T15:00:00Z", 2)), \
             patch.object(board, "last_reply", return_value="Full Claude reply"), \
             patch.object(board.time, "time", return_value=self.now), \
             patch.object(board.time, "sleep", side_effect=[None, None, StopLoop]), \
             patch.object(board, "get_agents", return_value={"agents": [
                 {"name": "claude-test", "project": "test", "preview": "preview"}
             ]}), \
             patch.object(board, "tg_enqueue") as enqueue:
            with self.assertRaises(StopLoop):
                board.notifier()
            enqueue.assert_called_once()
            name, text = enqueue.call_args.args
            self.assertEqual(name, "claude-test")
            self.assertIn("Full Claude reply", text)
            self.assertIn("сабагентов ещё работает: 2", text)

    def test_subagent_log_is_not_sent(self):
        board.TG_CODEX_INITIALIZED = True
        self.append(message("subagent"))
        with patch.object(board, "log_valid", return_value=False):
            self.poll()
        self.assertEqual(board.TG_QUEUE, [])
        self.assertEqual(board.TG_CODEX_CURSORS, {})


if __name__ == "__main__":
    unittest.main()


def task(kind, turn, at, error=None):
    payload = {"type": f"task_{kind}", "turn_id": turn}
    if kind == "complete":
        payload.update(completed_at=at, last_agent_message=None, error=error)
    return {"type": "event_msg", "timestamp": "2026-11-20T12:00:00Z", "payload": payload}


OVERLOAD = {"message": "Selected model is at capacity. Please try a different model.",
            "codex_error_info": "server_overloaded"}


class CodexCapacityRetryTest(unittest.TestCase):
    """«Selected model is at capacity» ends the turn and Codex waits for the user
    forever; the board says «continue» itself, with a growing pause."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "rollout.jsonl"
        self.path.write_text("")
        self.sent, self.posts, self.menu = [], [], ""
        card = {"agent": "codex", "tmux": "cx", "project": "proj", "label": "Cx",
                "rollout": str(self.path)}
        for target, kwargs in (
            ("codex_notification_cards", {"return_value": [card]}),
            ("log_valid", {"return_value": True}),
            ("agent_menu", {"side_effect": lambda name, engine: self.menu}),
            ("send_to_agent", {"side_effect": lambda n, t: self.sent.append(t) or True}),
            ("tg_enqueue", {"side_effect": lambda n, t: self.posts.append(t)}),
            ("tg_log", {}),
            ("TG_TOKEN", {"new": "1:x"}),
            ("TG_CHAT", {"new": "100"}),
            ("CODEX_RETRY", {"new": {}}),
        ):
            p = patch.object(board, target, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def log(self, *records):
        with self.path.open("a") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def tick(self, now):
        board.codex_capacity_retries(now)

    def test_overload_is_retried_after_a_pause(self):
        self.log(task("started", "t1", 1000), task("complete", "t1", 1000, OVERLOAD))
        self.tick(1010)
        self.assertEqual(self.sent, [])  # 30 s pause first
        self.menu = "Some menu"
        self.tick(1031)
        self.assertEqual(self.sent, [])  # never types into an open menu
        self.menu = ""
        self.tick(1032)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("at capacity", self.sent[0])
        self.assertIn("попытка 1", self.sent[0])
        self.tick(1040)
        self.assertEqual(len(self.sent), 1)  # once per failed turn

    def test_pause_grows_and_success_resets_the_chain(self):
        self.log(task("complete", "t1", 1000, OVERLOAD))
        self.tick(1031)
        self.log(task("started", "t2", 1040))
        self.tick(1041)
        self.log(task("complete", "t2", 1050, OVERLOAD))
        self.tick(1100)
        self.assertEqual(len(self.sent), 1)  # second attempt waits 60 s
        self.tick(1111)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("попытка 2", self.sent[-1])
        self.log(task("started", "t3", 1120), task("complete", "t3", 1200))
        self.tick(1201)
        self.assertEqual(board.CODEX_RETRY, {})
        self.log(task("complete", "t4", 1300, OVERLOAD))
        self.tick(1331)
        self.assertIn("попытка 1", self.sent[-1])

    def test_gives_up_with_a_telegram_notice_and_restarts_on_user_turn(self):
        at = 1000
        for i, delay in enumerate(board.CODEX_RETRY_DELAYS):
            self.log(task("complete", f"t{i}", at, OVERLOAD))
            self.tick(at + 1)
            self.tick(at + delay + 1)
            at += delay + 10
        self.assertEqual(len(self.sent), len(board.CODEX_RETRY_DELAYS))
        self.log(task("complete", "last", at, OVERLOAD))
        self.tick(at + 1)
        self.tick(at + 2000)
        self.assertEqual(len(self.sent), len(board.CODEX_RETRY_DELAYS))
        self.assertEqual(len(self.posts), 1)
        self.assertIn("не помогло", self.posts[0])
        # the user writes, and that turn hits the overload again — a fresh chain
        self.log(task("started", "u", at + 3000), task("complete", "u", at + 3001, OVERLOAD))
        self.tick(at + 3002)
        self.tick(at + 3032)
        self.assertIn("попытка 1", self.sent[-1])

    def test_old_failures_limits_and_other_errors_are_left_alone(self):
        self.log(task("complete", "old", 1000, OVERLOAD))
        self.tick(1000 + board.CODEX_RETRY_FRESH + 100)
        self.tick(1000 + board.CODEX_RETRY_FRESH + 200)
        self.log(task("complete", "lim", 9000, {"message": "limit",
                                                "codex_error_info": "usage_limit_exceeded"}))
        self.tick(9001)
        self.tick(9100)
        self.log(task("started", "busy", 9200))
        self.tick(9300)
        self.assertEqual((self.sent, self.posts), ([], []))
