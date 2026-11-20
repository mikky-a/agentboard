"""Group chats: an agent answers in the chat it was last addressed from."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_codex_notifications import board

OWNER = "100"
GROUP = "-200"


class ChatHarness(unittest.TestCase):
    """The listener with Telegram, tmux and the board faked out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.last_file = Path(self.tmp.name) / "tg-last.json"
        self.posts, self.sent = [], []
        self.group = (False, 1)  # (owner is in the group, members besides the bot)
        self.agents = {"sink": {"agent": "codex", "label": "Sink"},
                       "other": {"agent": "codex", "label": "Other"}}
        for name, kwargs in (
            ("TG_CHAT", {"new": OWNER}),
            ("TG_LAST_FILE", {"new": str(self.last_file)}),
            ("TG_LAST", {"new": {"chats": {}, "bind": {}}}),
            ("TG_BOT", {"new": {"id": "999", "username": "boardbot"}}),
            ("tg_agents", {"side_effect": lambda: self.agents}),
            ("tg_post", {"side_effect": lambda text, reply_to=None, chat=None:
                         self.posts.append((chat, text)) or 1}),
            ("tg_react", {"return_value": True}),
            ("tg_group", {"side_effect": lambda chat: self.group}),
            ("TG_CHATLOG", {"new": {}}),
            ("TG_CHATLOG_SEEN", {"new": {}}),
            ("TG_NOTED", {"new": set()}),
            ("tg_log", {}),
            ("send_to_agent", {"side_effect": lambda n, t: self.sent.append((n, t)) or True}),
        ):
            p = patch.object(board, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def handle(self, text, chat=GROUP, sender=OWNER, name="Mikhail", **extra):
        kind = "private" if chat == OWNER else "group"
        board.tg_handle({"chat": {"id": int(chat), "title": "T", "type": kind},
                         "from": {"id": int(sender), "first_name": name},
                         "message_id": 7, "text": text, **extra})


class TelegramChatsTest(ChatHarness):
    def test_legacy_state_is_the_main_chat(self):
        self.last_file.write_text(json.dumps({"from": "sink", "to": "other"}))
        state = board.tg_last_load()
        self.assertEqual(state["chats"][OWNER], {"from": "sink", "to": "other"})
        self.assertEqual(state["bind"], {})

    def test_strangers_in_group_are_ignored(self):
        self.handle("@sink rm -rf", sender="555")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.posts, [])

    def test_tag_in_group_moves_agent_there(self):
        self.assertEqual(board.tg_chat_of("sink"), OWNER)
        self.handle("@sink привет")
        self.assertEqual(self.sent, [("sink", "привет")])
        self.assertEqual(board.tg_chat_of("sink"), GROUP)
        self.assertIn("теперь отвечает в этом чате", self.posts[-1][1])
        self.assertEqual(self.posts[-1][0], GROUP)
        # without a tag the group talks to its only agent, and nothing moves again
        self.posts.clear()
        self.handle("дальше")
        self.assertEqual(self.sent[-1], ("sink", "дальше"))
        self.assertEqual(self.posts, [])
        # the main chat no longer routes untagged messages to the agent that moved away:
        # its only remaining session gets them, and the bot says so
        board.tg_last_set(OWNER, "from", "sink")
        self.handle("кому?", chat=OWNER)
        self.assertEqual(self.sent[-1], ("other", "кому?"))
        self.assertIn("→ @other", self.posts[-1][1])
        # with several sessions left it asks instead of guessing
        self.agents["third"] = {"agent": "codex"}
        self.handle("а теперь?", chat=OWNER)
        self.assertEqual(len(self.sent), 3)
        self.assertIn("уже в другом чате", self.posts[-1][1])

    def test_reply_in_main_chat_brings_agent_back(self):
        board.tg_bind("sink", GROUP)
        board.TG_SENT[(OWNER, 41)] = "sink"
        self.handle("ответ", chat=OWNER, reply_to_message={"message_id": 41, "text": "✅ x"})
        self.assertEqual(self.sent, [("sink", "ответ")])
        self.assertEqual(board.tg_chat_of("sink"), OWNER)

    def test_bot_mention_and_command_suffix_are_not_session_tags(self):
        with patch.object(board, "tg_help", return_value="HELP") as help_:
            self.handle("/help@boardbot")
        help_.assert_called_once()
        self.handle("@boardbot @sink go")
        self.assertEqual(self.sent, [("sink", "go")])

    def test_adding_bot_greets_only_for_owner(self):
        self.handle("", sender="555", new_chat_members=[{"id": 999}])
        self.assertEqual(self.posts, [])
        self.handle("", new_chat_members=[{"id": 999}])
        self.assertEqual(self.posts[0][0], GROUP)
        self.assertIn("Чат подключён", self.posts[0][1])

    def test_new_agent_is_created_and_bound_to_the_chat(self):
        with patch.object(board, "tg_project", return_value=("proj", "/p/proj", {})), \
             patch.object(board, "agent_models", return_value=[{"id": "m1", "def": True}]), \
             patch.object(board, "new_agent", return_value="proj-3") as new:
            self.handle("@proj new codex почини тесты")
        new.assert_called_once_with("/p/proj", "proj", "почини тесты", "codex", "m1", "high")
        self.assertEqual(board.tg_chat_of("proj-3"), GROUP)
        self.assertEqual(board.tg_last_get(GROUP, "from"), "proj-3")
        self.assertIn("Новый агент @proj-3", self.posts[-1][1])

    def test_unknown_project_lists_known_ones(self):
        with patch.object(board, "tg_project", return_value=(None, None, {"a": "/a", "b": "/b"})), \
             patch.object(board, "new_agent") as new:
            self.handle("@zzz new")
        new.assert_not_called()
        self.assertIn("Есть: a, b", self.posts[-1][1])

    def test_supergroup_migration_keeps_binding(self):
        board.tg_bind("sink", GROUP)
        board.tg_last_set(GROUP, "from", "sink")
        self.handle("", sender="555", migrate_to_chat_id=-1001)
        self.assertEqual(board.tg_chat_of("sink"), "-1001")
        self.assertEqual(board.tg_last_get("-1001", "from"), "sink")

    def test_sender_uses_bound_chat_and_falls_back_to_main(self):
        board.tg_bind("sink", GROUP)
        answers = iter([False, 55])

        class Stop(Exception):
            pass

        def post(text, reply_to=None, chat=None):
            self.posts.append((chat, text))
            return next(answers)

        def save():
            if not board.TG_QUEUE:
                raise Stop
            return True

        with patch.object(board, "tg_post", side_effect=post), \
             patch.object(board, "tg_queue_save", side_effect=save):
            board.TG_QUEUE[:] = [["sink", ["готово"]]]
            with self.assertRaises(Stop):
                board.tg_sender()
        self.assertEqual([c for c, _ in self.posts], [GROUP, OWNER])
        self.assertEqual(board.tg_chat_of("sink"), OWNER)
        self.assertEqual(board.TG_SENT[(OWNER, 55)], "sink")


class SharedGroupTest(ChatHarness):
    """A group with other people: the agent answers when called and sees the rest."""

    def setUp(self):
        super().setUp()
        self.group = (True, 3)  # the owner, a friend and someone else
        board.tg_bind("sink", GROUP)

    def friend(self, text, **extra):
        self.handle(text, sender="555", name="Ivan", **extra)

    def test_strangers_are_ignored_where_the_owner_is_absent(self):
        self.group = (False, 3)
        self.friend("@sink привет")
        self.assertEqual((self.sent, self.posts), ([], []))

    def test_chatter_is_context_for_the_next_call(self):
        self.friend("кнопка кривая")
        self.handle("согласен")
        self.friend("@vasya глянь", reply_to_message={"message_id": 3, "from": {"id": 777}})
        self.assertEqual((self.sent, self.posts), ([], []))
        self.friend("@sink поправь кнопку")
        name, typed = self.sent[-1]
        self.assertEqual(name, "sink")
        self.assertIn("Ivan: кнопка кривая\nMikhail (the owner): согласен\nIvan: @vasya глянь", typed)
        self.assertIn("Ivan writes to you", typed)
        self.assertIn("not the owner", typed)
        self.assertTrue(typed.endswith("]\n\nпоправь кнопку"))
        # the next call carries only what was said since
        self.handle("ещё и цвет")
        self.handle("@sink и цвет")
        typed = self.sent[-1][1]
        self.assertNotIn("кнопка кривая", typed)
        self.assertIn("Mikhail (the owner): ещё и цвет", typed)
        self.assertNotIn("writes to you", typed)  # the owner is not signed
        self.assertEqual(board.tg_chat_of("sink"), GROUP)

    def test_bot_mention_and_reply_to_bot_call_the_chat_agent(self):
        self.friend("@boardbot @vasya сказал, что падает")
        self.assertEqual(self.sent[-1][0], "sink")
        self.assertTrue(self.sent[-1][1].endswith("@vasya сказал, что падает"))
        board.TG_SENT[(GROUP, 41)] = "sink"
        self.friend("а теперь?", reply_to_message={"message_id": 41, "from": {"id": 999}})
        self.assertTrue(self.sent[-1][1].endswith("а теперь?"))
        self.assertEqual(len(self.sent), 2)

    def test_friend_talks_only_to_agents_of_this_chat(self):
        self.friend("@other привет")  # lives in another chat: just chatter here
        self.assertEqual(self.sent, [])
        self.friend("@boardbot @other привет")
        self.assertEqual(self.sent[-1][0], "sink")
        self.assertEqual(board.tg_chat_of("other"), OWNER)

    def test_friend_cannot_start_agents_or_send_commands(self):
        with patch.object(board, "tg_project", return_value=("proj", "/p/proj", {})), \
             patch.object(board, "new_agent") as new:
            self.friend("@proj new сделай всё")
        new.assert_not_called()
        self.assertIn("только владелец", self.posts[-1][1])
        self.friend("@sink /clear")
        self.assertEqual(self.sent, [])
        self.assertIn("только владелец", self.posts[-1][1])
        with patch.object(board, "send_key", return_value=True) as key:
            self.friend("@sink /esc")
        key.assert_called_once_with("sink", "esc")

    def test_people_joining_get_the_rules_once(self):
        self.handle("", sender="555", new_chat_members=[{"id": 555, "is_bot": False}])
        self.handle("", sender="556", new_chat_members=[{"id": 556, "is_bot": False}])
        self.assertEqual(len(self.posts), 1)
        self.assertIn("только когда их зовут", self.posts[0][1])

    def test_watched_card_still_notifies_a_group_with_people(self):
        with patch.object(board, "being_watched", return_value=True):
            self.assertFalse(board.tg_watched("sink"))
            self.assertTrue(board.tg_watched("other"))
            self.group = (True, 1)
            self.assertTrue(board.tg_watched("sink"))


class VoiceTest(ChatHarness):
    """Voice notes reach the agent as text: Claude and Codex cannot hear audio."""

    def setUp(self):
        super().setUp()
        self.voice = Path(self.tmp.name) / "20261120-060518_voice.oga"
        self.voice.write_bytes(b"OggS fake opus")
        board.tg_last_set(OWNER, "from", "sink")
        for name, kwargs in (("tg_fetch", {"side_effect": lambda media, name: str(self.voice)}),
                             ("DATA_DIR", {"new": self.tmp.name})):
            p = patch.object(board, name, **kwargs)
            p.start()
            self.addCleanup(p.stop)

    def voice_msg(self, text="", chat=OWNER):
        self.handle(text, chat=chat, voice={"file_id": "F1", "duration": 3})

    def test_voice_note_is_sent_as_its_transcript(self):
        with patch.object(board, "tg_transcribe", return_value="почини тесты") as stt:
            self.voice_msg()
        stt.assert_called_once_with(str(self.voice))
        name, body = self.sent[-1]
        self.assertEqual(name, "sink")
        self.assertTrue(body.startswith("почини тесты\n\n"))
        self.assertIn(f"запись: {self.voice}", body)
        self.assertFalse(any("расшифровать" in t for _, t in self.posts))

    def test_failed_transcript_falls_back_to_the_file(self):
        with patch.object(board, "tg_transcribe", return_value=""):
            self.voice_msg()
        self.assertEqual(self.sent[-1], ("sink", f"Вложение из Telegram: {self.voice}"))
        self.assertTrue(any("Не смог расшифровать" in t for _, t in self.posts))

    def test_photos_are_not_transcribed(self):
        with patch.object(board, "tg_transcribe") as stt:
            self.handle("глянь", chat=OWNER, photo=[{"file_id": "P", "file_size": 5}])
        stt.assert_not_called()
        self.assertEqual(self.sent[-1], ("sink", f"глянь\n\nВложение из Telegram: {self.voice}"))

    def test_new_agent_takes_the_model_from_the_caption_only(self):
        with patch.object(board, "tg_project", return_value=("proj", "/p/proj", {})), \
             patch.object(board, "model_alias", side_effect=lambda e, w: "m-fast" if w == "fast" else ""), \
             patch.object(board, "agent_models", return_value=[{"id": "m1", "def": True}]), \
             patch.object(board, "tg_transcribe", return_value="fast сделай лендинг"), \
             patch.object(board, "new_agent", return_value="proj-3") as new:
            self.voice_msg("@proj new codex")
            self.voice_msg("@proj new codex fast")
        first, second = (c.args for c in new.call_args_list)
        self.assertEqual(first[4], "m1")  # «fast» from the speech is not a model
        self.assertTrue(first[2].startswith("fast сделай лендинг\n\n"))
        self.assertEqual(second[4], "m-fast")
        self.assertTrue(second[2].startswith("fast сделай лендинг\n\n"))

    def test_transcribe_posts_the_file_to_groq(self):
        (Path(self.tmp.name) / "groq.json").write_text(json.dumps({"groq_api_key": "gk"}))
        seen = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"text": " привет "}).encode()

        def urlopen(req, timeout):
            seen.update(url=req.full_url, auth=req.get_header("Authorization"), body=req.data)
            return Resp()

        with patch.object(board.urllib.request, "urlopen", side_effect=urlopen):
            self.assertEqual(board.tg_transcribe(str(self.voice)), "привет")
        self.assertEqual(seen["url"], board.GROQ_STT_URL)
        self.assertEqual(seen["auth"], "Bearer gk")
        self.assertIn(b'filename="speech.ogg"', seen["body"])  # .oga is sent as .ogg
        self.assertIn(b"whisper-large-v3-turbo", seen["body"])
        self.assertIn(b"OggS fake opus", seen["body"])

    def test_no_key_means_no_request(self):
        with patch.dict(board.os.environ, {"GROQ_API_KEY": ""}), \
             patch.object(board.urllib.request, "urlopen") as urlopen:
            self.assertEqual(board.tg_transcribe(str(self.voice)), "")
        urlopen.assert_not_called()


class GroupMembersApiTest(unittest.TestCase):
    def test_owner_presence_and_member_count_are_cached(self):
        calls = []

        def api(method, **params):
            calls.append(method)
            return {"status": "member"} if method == "getChatMember" else 4

        with patch.object(board, "tg_api", side_effect=api), \
             patch.object(board, "TG_GROUPS", {}), patch.object(board, "TG_CHAT", OWNER):
            self.assertEqual(board.tg_group(GROUP), (True, 3))
            self.assertTrue(board.tg_shared(GROUP))
            self.assertFalse(board.tg_shared(OWNER))
        self.assertEqual(calls, ["getChatMember", "getChatMemberCount"])

    def test_bot_removed_from_group_means_nobody_is_listened_to(self):
        import urllib.error
        err = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        with patch.object(board, "tg_api", side_effect=err), patch.object(board, "tg_log"), \
             patch.object(board, "TG_GROUPS", {}):
            self.assertEqual(board.tg_group(GROUP), (False, 0))


if __name__ == "__main__":
    unittest.main()
