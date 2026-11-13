"""Маршрутизация сообщений из Telegram без реплая и @имени."""
import time
import unittest
from unittest.mock import patch

from test_codex_notifications import board

PROJECTS = {"scandalio": "/p/scandalio", "kuhni-lebedi": "/p/kuhni-lebedi",
            "google-ads-scandalio": "/p/google-ads-scandalio"}
AGENTS = {"kl": {"name": "kl", "project": "kuhni-lebedi", "path": "/p/kuhni-lebedi"},
          "scandalio": {"name": "scandalio", "project": "scandalio", "path": "/p/scandalio"}}


class RoutingTest(unittest.TestCase):
    def setUp(self):
        for p in (patch.object(board, "tg_projects", return_value=PROJECTS),
                  patch.dict(board.os.environ, {"AGENTBOARD_TG_ALIASES":
                                                "scandalio=скандалио;kuhni-lebedi=кухни,кл"}),
                  patch.object(board, "DUTY_DIR", "/root/duty")):
            p.start()
            self.addCleanup(p.stop)
        self.last = dict(board.TG_LAST)
        self.addCleanup(lambda: (board.TG_LAST.clear(), board.TG_LAST.update(self.last)))

    def talk(self, name, ago):
        board.TG_LAST.update(cur=name, cur_at=time.time() - ago)

    def route(self, text):
        return board.tg_target({}, text, AGENTS)

    def test_fresh_current_gets_message(self):
        self.talk("kl", 60)
        self.assertEqual(self.route("а что с фасадами?")[::3], ("kl", "auto"))

    def test_stale_current_asks(self):
        self.talk("kl", 3600)
        self.assertEqual(self.route("а что с фасадами?")[3], "menu")

    def test_other_project_named_asks(self):
        self.talk("kl", 60)
        self.assertEqual(self.route("глянь в скандалио оплаты")[3], "menu")

    def test_same_project_named_sticks(self):
        self.talk("kl", 60)
        self.assertEqual(self.route("по кухням ещё вопрос")[0], "kl")

    def test_no_agents_asks(self):
        self.assertEqual(board.tg_target({}, "привет", {})[3], "menu")

    def test_mentions(self):
        self.assertEqual(board.tg_mentions("кл и scandalio", PROJECTS), ["kuhni-lebedi", "scandalio"])
        self.assertEqual(board.tg_mentions("клиент google-ads-scandalio", PROJECTS),
                         ["google-ads-scandalio"])

    def test_menu_puts_named_project_first(self):
        self.talk("kl", 60)
        opts = board.tg_menu_opts("в скандалио", AGENTS)
        self.assertEqual(opts[:2], [("a", "scandalio"), ("n", "scandalio")])
        self.assertIn(("d", ""), opts)

    def test_sleeping_duty_by_tag(self):
        self.assertEqual(self.route("@duty погода")[:2], ("duty", "погода"))

    def test_focus_menu(self):
        agents = dict(AGENTS, sc2={"name": "sc2", "project": "scandalio", "path": "/p/scandalio"})
        self.talk("sc2", 60)
        self.assertEqual(board.tg_focus_opts("scandalio", agents),
                         [("a", "sc2"), ("a", "scandalio"), ("n", "scandalio"), ("x", "")])

    def test_agent_info_and_context(self):
        import json, os, tempfile
        fd, path = tempfile.mkstemp()
        self.addCleanup(os.remove, path)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"message": {"usage": {"input_tokens": 5, "cache_read_input_tokens": 179000,
                                                      "cache_creation_input_tokens": 995}}}) + "\n")
        os.utime(path, (time.time() - 3 * 3600 - 60,) * 2)
        info = board.agent_info("scandalio", {"scandalio": {"label": "Зимний контент"}}, {"scandalio": path})
        self.assertEqual(info, "Зимний контент · молчит 3 ч · 180k")


if __name__ == "__main__":
    unittest.main()
