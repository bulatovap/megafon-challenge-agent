"""Обвязка тестов среза «леджер»: временный корень (BULOCHKA_ROOT), проект demo, вызов CLI с --json."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
if str(PRODUCT_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCT_ROOT))

from tools.bulochka import cli, core  # noqa: E402
from _isolate import isolate_ext  # noqa: E402

ENV_KEYS = ("BULOCHKA_ROOT", "BULOCHKA_PROJECT", "BULOCHKA_ACTOR")

PAGE = ("Отчёт оператора за третий квартал 2026 года.\n"
        "По данным компании, «отток после пробного периода составил 41%» пользователей подписки.\n"
        "Выручка сервиса выросла до 2,5 млн ₽, ARPU — 299 руб.\n"
        + "Прочий текст страницы без чисел. " * 40 + "\n"
        "Далеко внизу: доля мобильного трафика 15%.\n")


class LedgerCase(unittest.TestCase):
    def setUp(self):
        isolate_ext(self)  # расширения x_*.py и ext-хуки копии не влияют на тесты базового слоя
        self._env = {k: os.environ.get(k) for k in ENV_KEYS}
        self.tmp = Path(tempfile.mkdtemp(prefix="леджер тест-"))
        os.environ["BULOCHKA_ROOT"] = str(self.tmp)
        os.environ["BULOCHKA_PROJECT"] = "demo"
        os.environ.pop("BULOCHKA_ACTOR", None)
        shutil.copytree(str(PRODUCT_ROOT / "config"), str(self.tmp / "config"))
        (self.tmp / "projects" / "demo" / "state").mkdir(parents=True)
        (self.tmp / "projects" / "demo" / "report").mkdir(parents=True)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- CLI ---
    def run_cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["--json"] + [str(a) for a in argv])
        out = buf.getvalue().strip().splitlines()
        payload = json.loads(out[-1]) if out else {}
        return code, payload

    def ok(self, *argv):
        code, p = self.run_cli(*argv)
        self.assertEqual(code, 0, p)
        return p["data"]

    # --- фикстуры ---
    def add_source(self, text=PAGE, url="https://www.comnews.ru/content/1", kind="media", grade=None,
                   published="2026-10-01", **kw):
        argv = ["source", "add", "--url", url, "--title", "Тест", "--publisher", "Издатель", "--text", text,
                "--kind", kind]
        if published:
            argv += ["--published", published]
        if grade:
            argv += ["--grade", grade]
        for k, v in kw.items():
            argv += [f"--{k.replace('_', '-')}", v]
        return self.ok(*argv)["id"]

    def add_decision(self, question="Что делать с удержанием подписки после триала?", **kw):
        argv = ["decision", "add", "--question", question, "--owner", "продакт подписки", "--metric", "retention_m2",
                "--first-read", "2027-01-15"]
        for k, v in kw.items():
            argv += [f"--{k.replace('_', '-')}", v]
        return self.ok(*argv)["id"]

    def add_option(self, did, title, kind, distinct_by=None, **kw):
        argv = ["option", "add", "--decision", did, "--title", title, "--kind", kind]
        if distinct_by:
            argv += ["--distinct-by", distinct_by]
        for k, v in kw.items():
            argv += [f"--{k.replace('_', '-')}", v]
        return self.ok(*argv)["id"]

    def answered_gate(self, answer, kind="choose_option"):
        """Ворота с ответом человека — напрямую в реестр, чтобы не зависеть от модуля gates."""
        g = core.put("gate", {"step": "decide", "kind": kind, "question": "Какой вариант берём?",
                              "options": ["O001", "O002"], "claims_shown": [], "flips": True,
                              "why_ask": "выбор за человеком", "answer": answer, "answered_by": "human",
                              "answered_at": core.now_iso()})
        return g["id"]

    def reject_others(self, did):
        """Перед lock: открытые альтернативы выбранного варианта — отклонить с условием возврата."""
        import json as _json
        d = core.get("decision", did)
        alts = [o["id"] for o in core.latest("option").values() if o.get("decision_id") == did
                and o["id"] != d.get("chosen_option") and o.get("status") == "open"]
        if not alts:
            return None
        c = self.ok("claim", "add", "--type", "assumption", "--text", "Альтернативы слабее выбранного",
                    "--falsifier", "выбранный вариант не даёт эффекта")["id"]
        for oid in alts:
            self.ok("option", "reject", oid, "--killed-by", c,
                    "--revive-if", _json.dumps([{"assumption_id": c, "threshold": "<0,30"}]))
        return c

    def refusals(self):
        return list(core.latest("refusal").values())
