"""numbers.py и quotes.py: нормализация чисел и поиск цитат."""
from __future__ import annotations

import unittest

import _ledger_env  # noqa: F401  (sys.path)
from tools.bulochka import numbers, quotes


class NumbersTest(unittest.TestCase):
    def one(self, text):
        res = numbers.extract(text)
        self.assertEqual(len(res), 1, res)
        return res[0]

    def test_csv_row_commas_are_field_separators(self):
        vals = [x["value"] for x in numbers.extract("2026-03,промо,12400,2121,990,666")]
        self.assertIn(12400, vals)
        self.assertIn(2121, vals)
        self.assertEqual([x["value"] for x in numbers.extract("рост 9,6%, выручка 445,4 млрд")], [0.096, 445400000000])

    def test_percent_to_share(self):
        n = self.one("Отток 41%")
        self.assertEqual((n["value"], n["unit"]), (0.41, "share"))
        self.assertEqual(self.one("12,5 %")["value"], 0.125)

    def test_scale_and_currency_with_nbsp(self):
        n = self.one("выручка 2,5 млн ₽")
        self.assertEqual((n["value"], n["unit"], n["scale"]), (2500000, "RUB", 1000000))
        self.assertEqual(self.one("$1.5 млрд")["unit"], "USD")

    def test_pp_and_rub_words(self):
        self.assertEqual(self.one("рост на 3,2 п.п.")["unit"], "pp")
        n = self.one("цена 299 руб.")
        self.assertEqual((n["value"], n["unit"]), (299, "RUB"))

    def test_thousands_separator_and_year(self):
        self.assertEqual(self.one("1 234 567 абонентов")["value"], 1234567)
        res = numbers.extract("в 2025 году 300 тыс. подписчиков")
        self.assertEqual([r["unit"] for r in res], ["year", "count"])
        self.assertEqual(res[1]["value"], 300000)

    def test_number_on_page_window(self):
        page = "Начало. Отток составил 41% за квартал. " + "x " * 600 + "Ещё 15% чего-то."
        off = [8, 38]
        self.assertEqual(numbers.number_on_page(numbers.extract("отток 41%"), page, off), "pass")
        # 15% есть на странице, но далеко от цитаты — не засчитываем
        self.assertEqual(numbers.number_on_page(numbers.extract("доля 15%"), page, off), "fail")
        # без цитаты — только вручную
        self.assertEqual(numbers.number_on_page(numbers.extract("отток 41%"), page, None), "needs_manual")
        self.assertIsNone(numbers.number_on_page(numbers.extract("без чисел"), page, off))

    def test_number_reformatted_and_near(self):
        page = "Выручка 2 500 000 рублей."
        self.assertEqual(numbers.number_on_page(numbers.extract("2,5 млн ₽"), page, [0, 10]), "pass")
        self.assertEqual(numbers.number_on_page(numbers.extract("41%"), "доля 41 пользователей", [0, 5]),
                         "needs_manual")


class QuotesTest(unittest.TestCase):
    TEXT = "Компания: «Отток после   триала — 41%» пользователей.\nДалее ёлка."

    def test_normalized_match_returns_original_offsets(self):
        off = quotes.verify_quote('"Отток после триала - 41%"', self.TEXT)
        self.assertIsNotNone(off)
        self.assertEqual(self.TEXT[off[0]:off[1]], "Отток после   триала — 41%")

    def test_ellipsis_and_yo(self):
        self.assertIsNotNone(quotes.verify_quote("Отток ... 41%", self.TEXT))
        self.assertIsNotNone(quotes.verify_quote("Далее елка", self.TEXT))

    def test_missing_quote(self):
        self.assertIsNone(quotes.verify_quote("Отток после триала 33%", self.TEXT))
        self.assertIsNone(quotes.verify_quote("", self.TEXT))


if __name__ == "__main__":
    unittest.main()
