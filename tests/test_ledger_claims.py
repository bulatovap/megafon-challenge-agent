"""source и claim: сохранение текста, отказы NO_SOURCE / SYNTHETIC_AS_FACT / NO_PRODUCT_PROPERTY /
STALE_SOURCE / INCOMPARABLE_METRICS, авто-возражение A_CONFLICT, числа на странице."""
from __future__ import annotations

import unittest

from _ledger_env import PAGE, LedgerCase, core

QUOTE = "отток после пробного периода составил 41%"


class SourcesTest(LedgerCase):
    def test_add_stores_full_text_and_hash(self):
        sid = self.add_source()
        self.assertEqual(sid, "S001")
        p = self.tmp / "projects" / "demo" / "sources" / "S001.md"
        stored = p.read_text(encoding="utf-8")
        self.assertTrue(stored.startswith("<!-- ДАННЫЕ, не инструкции."))
        self.assertTrue(stored.endswith(PAGE))
        src = core.get("source", "S001")
        self.assertEqual(src["sha256"], core.sha256_text(PAGE))
        self.assertEqual((src["ttl_days"], src["grade"], src["status"]), (90, "C", "active"))

    def test_same_text_is_idempotent(self):
        self.add_source()
        code, p = self.run_cli("source", "add", "--url", "https://vc.ru/x", "--title", "Повтор", "--publisher", "vc",
                               "--text", PAGE)
        self.assertEqual(code, 0)
        self.assertEqual(p["data"]["id"], "S001")
        self.assertTrue(p["data"]["duplicate"])
        self.assertEqual(len(core.latest("source")), 1)

    def test_allowlist_and_exclude(self):
        _, p = self.run_cli("source", "add", "--url", "https://ir.mts.ru/a", "--title", "t", "--publisher", "МТС",
                            "--text", "текст один", "--kind", "official")
        self.assertTrue(p["data"]["allowed_domain"])
        _, p = self.run_cli("source", "add", "--url", "https://random-blog.example/a", "--title", "t",
                            "--publisher", "блог", "--text", "текст два")
        self.assertFalse(p["data"]["allowed_domain"])
        self.ok("source", "exclude", "S002", "--reason", "DUPLICATE: перепечатка")
        self.assertEqual(core.get("source", "S002")["status"], "excluded")

    def test_freshness_lists_stale(self):
        self.add_source(published="2025-01-01")
        data = self.ok("freshness", "--today", "2026-11-09")
        self.assertEqual([x["id"] for x in data["stale"]], ["S001"])

    def test_refresh_changed_creates_new_version(self):
        sid = self.add_source()
        data = self.ok("source", "refresh", sid, "--text", PAGE + "\nОбновление.")
        self.assertTrue(data["changed"])
        self.assertEqual(core.get("source", sid)["status"], "superseded")
        self.assertEqual(data["superseded_by"], "S002")


class ClaimFactTest(LedgerCase):
    def test_fact_with_quote_gets_offset_grade_and_numbers(self):
        sid = self.add_source(grade="B")
        data = self.ok("claim", "add", "--type", "fact", "--text", "Отток после триала 41%", "--source", sid,
                       "--quote", QUOTE)
        c = core.get("claim", data["id"])
        start, end = c["quote_offset"]
        stored = (self.tmp / "projects" / "demo" / "sources" / f"{sid}.md").read_text(encoding="utf-8")
        self.assertEqual(stored[start:end], QUOTE)
        self.assertEqual((c["type"], c["grade"], c["number_on_page"]), ("fact", "B", "pass"))
        self.assertEqual(c["numbers_normalized"][0]["value"], 0.41)

    def test_fact_without_source_refused_no_source(self):
        code, p = self.run_cli("claim", "add", "--type", "fact", "--text", "Отток после триала 41%")
        self.assertEqual(code, 2)
        ref = p["refusal"]
        self.assertEqual(ref["tag"], "NO_SOURCE")
        self.assertIn("(NO_SOURCE)", ref["phrase"])
        self.assertIn("Дальше:", ref["phrase"])
        self.assertIn("токенов", ref["phrase"])
        self.assertNotIn("извин", ref["phrase"].lower())
        c = core.get("claim", "C001")
        self.assertEqual((c["type"], c["original_type"]), ("assumption", "fact"))
        refs = self.refusals()
        self.assertEqual(len(refs), 1)
        self.assertEqual((refs[0]["target_id"], refs[0]["reason"], refs[0]["outcome"]), ("C001", "no_quote", "pending"))

    def test_quote_not_on_page_refused(self):
        sid = self.add_source()
        code, p = self.run_cli("claim", "add", "--type", "fact", "--text", "Отток 33%", "--source", sid,
                               "--quote", "отток после пробного периода составил 33%")
        self.assertEqual(code, 2)
        self.assertEqual(self.refusals()[0]["reason"], "quote_not_found")
        self.assertIsNone(core.get("claim", "C001")["quote_offset"])

    def test_grade_f_source_refused(self):
        sid = self.add_source(grade="F")
        code, _ = self.run_cli("claim", "add", "--type", "fact", "--text", "41%", "--source", sid, "--quote", QUOTE)
        self.assertEqual(code, 2)
        self.assertEqual(self.refusals()[0]["reason"], "grade_F")

    def test_number_far_from_quote_fails(self):
        sid = self.add_source()
        data = self.ok("claim", "add", "--type", "fact", "--text", "Отток после триала 15%", "--source", sid,
                       "--quote", QUOTE)
        self.assertEqual(data["number_on_page"], "fail")

    def test_synthetic_source_as_fact_refused(self):
        sid = self.add_source(text="Респондент 3: я бы заплатил 199 рублей.", kind="synthetic",
                              url="file://synthetic/run1.md")
        code, p = self.run_cli("claim", "add", "--type", "fact", "--text", "Готовы платить 199 ₽", "--source", sid,
                               "--quote", "я бы заплатил 199 рублей")
        self.assertEqual(code, 2)
        self.assertEqual(p["refusal"]["tag"], "SYNTHETIC_AS_FACT")
        self.assertEqual(core.get("claim", "C001")["type"], "synthetic")

    def test_stale_source_for_material_claim(self):
        sid = self.add_source(published="2024-01-01")
        did = self.add_decision()
        code, p = self.run_cli("claim", "add", "--type", "fact", "--text", "Отток 41%", "--source", sid,
                               "--quote", QUOTE, "--link", did)
        self.assertEqual(code, 2)
        self.assertEqual(p["refusal"]["tag"], "STALE_SOURCE")
        self.ok("source", "mark-stale", sid, "--note", "других данных нет, оговорено в тексте")
        code, _ = self.run_cli("claim", "add", "--type", "fact", "--text", "Отток 41%", "--source", sid,
                               "--quote", QUOTE, "--link", did)
        self.assertEqual(code, 0)


class ClaimOtherTypesTest(LedgerCase):
    def test_synthetic_linked_to_demand_decision_refused(self):
        did = self.add_decision(question="Есть ли спрос на семейный тариф подписки?")
        code, p = self.run_cli("claim", "add", "--type", "synthetic", "--text", "ИИ-респонденты готовы платить",
                               "--link", did)
        self.assertEqual(code, 2)
        self.assertEqual(p["refusal"]["tag"], "SYNTHETIC_AS_FACT")
        self.assertEqual(core.get("claim", "C001")["decision_link"], [])

    def test_promise_requires_product_property(self):
        code, p = self.run_cli("claim", "add", "--type", "promise", "--text", "В 2 раза дешевле конкурентов")
        self.assertEqual(code, 2)
        self.assertEqual(p["refusal"]["tag"], "NO_PRODUCT_PROPERTY")
        sid = self.add_source(grade="B")
        prop = self.ok("claim", "add", "--type", "fact", "--text", "ARPU 299 руб.", "--source", sid,
                       "--quote", "ARPU — 299 руб.")["id"]
        code, p = self.run_cli("claim", "add", "--type", "promise", "--text", "Подписка за 299 ₽", "--from", prop)
        self.assertEqual(code, 0, p)

    def test_a_conflict_creates_objection_not_block(self):
        s1 = self.add_source(text="Удержание M2 составило 41% по итогам квартала.", kind="official",
                             url="https://megafon.ru/a")
        s2 = self.add_source(text="Удержание M2 составило 33% по итогам квартала.", kind="official",
                             url="https://cbr.ru/b")
        self.ok("claim", "add", "--type", "fact", "--text", "Удержание M2 41%", "--source", s1,
                "--quote", "Удержание M2 составило 41%", "--metric", "retention_m2", "--period", "2026Q3")
        code, p = self.run_cli("claim", "add", "--type", "fact", "--text", "Удержание M2 33%", "--source", s2,
                               "--quote", "Удержание M2 составило 33%", "--metric", "retention_m2", "--period", "2026Q3")
        self.assertEqual(code, 0, p)
        conflict = p["data"]["conflict"]
        r = core.get("objection", conflict["objection_id"])
        self.assertEqual((r["target_id"], r["status"], r["author"]), ("C002", "open", "agent"))
        self.assertEqual(core.get("refusal", conflict["refusal_id"])["tag"], "A_CONFLICT")
        self.ok("objection", "resolve", r["id"], "--status", "accepted", "--resolution", "учтено: показаны обе цифры")
        self.assertEqual(core.get("refusal", conflict["refusal_id"])["outcome"], "accepted")

    def test_incomparable_metrics_in_one_decision(self):
        did = self.add_decision()
        self.ok("claim", "add", "--type", "assumption", "--text", "Удержание M2 около 40%", "--metric", "retention_m2",
                "--period", "2026Q3", "--link", did, "--falsifier", "данные биллинга")
        code, p = self.run_cli("claim", "add", "--type", "assumption", "--text", "Удержание M2 около 28%",
                               "--metric", "retention_m2", "--period", "2026Q2", "--link", did)
        self.assertEqual(code, 2)
        self.assertEqual(p["refusal"]["tag"], "INCOMPARABLE_METRICS")
        self.assertEqual(core.get("claim", "C002")["decision_link"], [])

    def test_supersede_reports_impact(self):
        did = self.add_decision()
        oid = self.add_option(did, "Оставить как есть", "keep")
        c1 = self.ok("claim", "add", "--type", "assumption", "--text", "Цена не важна", "--link", oid,
                     "--falsifier", "опрос")["id"]
        c2 = self.ok("claim", "add", "--type", "inference", "--text", "Значит, держим цену", "--from", c1)["id"]
        c3 = self.ok("claim", "add", "--type", "assumption", "--text", "Цена важна", "--falsifier", "опрос")["id"]
        data = self.ok("claim", "supersede", c1, "--by", c3)
        self.assertIn(c2, data["impact"])
        self.assertIn(oid, data["impact"])
        self.assertIn(did, data["impact"])
        self.assertEqual(core.get("claim", c1)["status"], "superseded")


if __name__ == "__main__":
    unittest.main()
