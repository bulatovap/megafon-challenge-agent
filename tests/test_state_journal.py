"""Журнал: ключ идемпотентности, skip_done, uncertain без автоповтора, resolve только человеком."""
from __future__ import annotations

import os
import unittest

from _s_env import StateCase, core, journal


class TestKey(unittest.TestCase):
    def test_key_ignores_volatile_order_and_tracking(self):
        a = journal.make_key("demo", "source.fetch",
                             {"url": "HTTPS://Example.ru/a?utm_source=x&b=2&a=1#frag", "ts": "2026", "session_id": "s1"})
        b = journal.make_key("demo", "source.fetch", {"session_id": "s2", "url": "https://example.ru/a?a=1&b=2"})
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("sha256:"))

    def test_key_depends_on_slug_op_and_source_version(self):
        base = journal.make_key("demo", "claim.add", {"text": "x"})
        self.assertNotEqual(base, journal.make_key("other", "claim.add", {"text": "x"}))
        self.assertNotEqual(base, journal.make_key("demo", "claim.edit", {"text": "x"}))
        self.assertNotEqual(base, journal.make_key("demo", "claim.add", {"text": "x"}, "abc"))
        # NFC: «й» составной и готовый — один ключ
        self.assertEqual(journal.make_key("demo", "op", {"t": "й"}), journal.make_key("demo", "op", {"t": "й"}))


class TestJournal(StateCase):
    def setUp(self):
        super().setUp()
        self.new()

    def test_begin_twice_is_skip_running(self):
        st1, r1 = journal.begin("source.fetch", {"url": "https://a.ru"})
        st2, r2 = journal.begin("source.fetch", {"url": "https://a.ru"})
        self.assertEqual((st1, st2), ("started", "skip_running"))
        self.assertEqual(r1["id"], r2["id"])

    def test_begin_on_committed_is_skip_done_via_cli(self):
        code, res = self.cli("journal", "begin", "--op", "export", "--inputs", '{"kind":"one-pager"}')
        self.assertEqual((code, res["data"]["status"]), (0, "started"))
        key = res["data"]["key"]
        self.out("report/one_pager.md")
        code, res = self.cli("journal", "commit", key, "--output", "report/one_pager.md")
        self.assertEqual(code, 0)
        code, res = self.cli("journal", "begin", "--op", "export", "--inputs", '{"kind": "one-pager"}')
        self.assertEqual((code, res["data"]["status"]), (0, "skip_done"))
        n = len(journal.records())
        self.cli("journal", "begin", "--op", "export", "--inputs", '{"kind":"one-pager"}')
        self.assertEqual(len(journal.records()), n, "skip_done ничего не пишет")

    def test_commit_requires_existing_output(self):
        _, rec = journal.begin("step.run", {"step": "intake"})
        code, res = self.cli("journal", "commit", rec["id"], "--output", "report/нет.md")
        self.assertEqual(code, core.EXIT_USAGE)
        self.assertEqual(journal.find(rec["id"])["status"], "started")

    def test_stale_started_becomes_uncertain_no_autorepeat(self):
        journal.begin("source.fetch", {"url": "https://a.ru"}, side_effect="external")
        code, res = self.cli("journal", "begin", "--op", "source.fetch", "--inputs", '{"url":"https://a.ru"}',
                             "--timeout", "0")
        self.assertEqual((code, res["data"]["status"]), (core.EXIT_REFUSAL, "uncertain"))
        code, res = self.cli("journal", "begin", "--op", "source.fetch", "--inputs", '{"url":"https://a.ru"}')
        self.assertEqual((code, res["data"]["status"]), (core.EXIT_REFUSAL, "uncertain"))
        self.assertEqual([u["status"] for u in journal.uncertain_list()], ["uncertain"])

    def test_failed_allows_retry_with_attempt(self):
        _, rec = journal.begin("source.fetch", {"url": "https://a.ru"})
        st, _ = journal.fail(rec["id"], error="timeout")
        self.assertEqual(st, "failed")
        st, rec2 = journal.begin("source.fetch", {"url": "https://a.ru"})
        self.assertEqual((st, rec2["attempt"], rec2["id"]), ("started", 2, rec["id"]))
        self.assertEqual(rec2["retry_after_s"], 2)

    def test_retry_limit_for_side_effect_none(self):
        for _ in range(4):
            st, rec = journal.begin("source.fetch", {"url": "https://b.ru"})
            self.assertEqual(st, "started")
            journal.fail(rec["id"], error="x")
        st, _ = journal.begin("source.fetch", {"url": "https://b.ru"})
        self.assertEqual(st, "retry_limit")

    def test_fail_with_partial_output_is_uncertain(self):
        _, rec = journal.begin("step.run", {"step": "situation"}, side_effect="file_write")
        self.out("report/20_baseline.md", "половина")
        st, _ = journal.fail(rec["id"], error="обрыв", output="report/20_baseline.md")
        self.assertEqual(st, "uncertain")

    def test_uncertain_fail_only_for_side_effect_none(self):
        _, a = journal.begin("source.fetch", {"url": "https://a.ru"}, side_effect="none")
        _, b = journal.begin("export", {"k": 1}, side_effect="external")
        journal.mark_session_uncertain(None, except_session="другая")
        self.assertEqual(journal.fail(a["id"])[0], "failed")
        code, res = self.cli("journal", "fail", b["id"])
        self.assertEqual(code, core.EXIT_REFUSAL)
        self.assertEqual(journal.find(b["id"])["status"], "uncertain")

    def test_resolve_only_human(self):
        _, rec = journal.begin("export", {"k": 1}, side_effect="external")
        journal.mark_session_uncertain(None, except_session="другая")
        code, _ = self.cli("journal", "resolve", rec["id"], "--as", "failed")
        self.assertEqual(code, core.EXIT_REFUSAL)
        os.environ["BULOCHKA_ACTOR"] = "human"
        code, res = self.cli("journal", "resolve", rec["id"], "--as", "failed")
        self.assertEqual((code, res["data"]["status"]), (0, "failed"))
        last = journal.find(rec["id"])
        self.assertEqual(last["actor"], "human")
        self.assertEqual(journal.uncertain_list(), [])

    def test_unique_ids_and_same_id_per_key(self):
        _, a = journal.begin("op", {"n": 1})
        _, b = journal.begin("op", {"n": 2})
        self.assertNotEqual(a["id"], b["id"])
        journal.commit(a["id"])
        self.assertEqual(len({r["id"] for r in journal.records() if r.get("key") == a["key"]}), 1)


if __name__ == "__main__":
    unittest.main()
