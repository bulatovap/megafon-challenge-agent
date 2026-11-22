"""verify, check, refusals.

verify  — перепроверка цитат (verify_quote) и чисел на странице; --impact ID — что зависит от записи.
check   — quality gate по config/quality_gate.json (+ config/quality_gate.local.json поверх, если есть),
          правила базового слоя RULES + EXTRA_RULES из модулей x_*.py, профиль из config/gate_profile.json;
          счёт = 100 − 10·ошибки − 2·предупреждения; отчёт в state/quality_gate_report.json.
refusals — список отказов, точность (accepted / решённые), смена исхода.
"""
from __future__ import annotations

import fnmatch
import re
from typing import Any, Callable, Dict, List, Optional

from . import core, ext, numbers
from .claims import impact_of, number_window, is_demand_decision, linked_decisions, metric_signature
from .decisions import evidence_debt, option_claim_ids
from .quotes import verify_quote
from .sources import GRADE_ORDER, min_grade, read_text, staleness, today_date

# ---------- verify ----------


def verify_claim(c: Dict[str, Any], do_quotes: bool = True, do_numbers: bool = True) -> Dict[str, Any]:
    """Перепроверить один claim. Возвращает {id, quote, numbers, status, changed}."""
    res: Dict[str, Any] = {"id": c["id"], "quote": None, "numbers": None}
    upd = dict(c)
    page = ""
    if c.get("type") == "fact" and c.get("source_ids"):
        srcs = [core.get("source", s) for s in c["source_ids"]]
        srcs = [s for s in srcs if s]
        if do_quotes:
            found = None
            for s in srcs:
                off = verify_quote(c.get("quote") or "", read_text(s))
                if off:
                    found = (s, off)
                    break
            res["quote"] = "pass" if found else "fail"
            if found:
                upd["quote_offset"] = [found[1][0], found[1][1]]
                upd["quote_source"] = found[0]["id"]
                page = read_text(found[0])
            upd["quote_status"] = res["quote"]
        if do_numbers:
            if not page:
                qs = core.get("source", c.get("quote_source") or "") or (srcs[0] if srcs else None)
                page = read_text(qs) if qs else ""
            res["numbers"] = numbers.number_on_page(c.get("numbers_normalized") or numbers.extract(c.get("text", "")),
                                                    page, upd.get("quote_offset") if res["quote"] != "fail" else None,
                                                    number_window())
            upd["number_on_page"] = res["numbers"]
    statuses = [x for x in (res["quote"], res["numbers"]) if x]
    if "fail" in statuses:
        res["status"] = "fail"
    elif "needs_manual" in statuses:
        res["status"] = "needs_manual"
    else:
        res["status"] = "pass"
    keys = ("quote_offset", "quote_source", "quote_status", "number_on_page")
    res["changed"] = any(upd.get(k) != c.get(k) for k in keys)
    if res["changed"]:
        core.put("claim", upd)
    return res


def cmd_verify(args) -> int:
    if args.impact:
        imp = impact_of(args.impact)
        return core.emit("verify", {"checked": 0, "pass": 0, "fail": 0, "needs_manual": 0, "impact": imp},
                         human=f"От {args.impact} зависят: {', '.join(imp) or 'ничего'}.", as_json=args.json)
    do_q = args.quotes or not (args.quotes or args.numbers)
    do_n = args.numbers or not (args.quotes or args.numbers)
    claims = core.latest("claim")
    if args.id:
        c = claims.get(args.id)
        if not c:
            raise core.BulochkaError(f"{args.id} не найден.")
        targets = [c]
    else:
        targets = [c for c in sorted(claims.values(), key=lambda x: x["id"])
                   if c.get("status") == "active" and c.get("type") == "fact"]
    results = [verify_claim(c, do_q, do_n) for c in targets]
    out = {"checked": len(results), "pass": [r["id"] for r in results if r["status"] == "pass"],
           "fail": [r["id"] for r in results if r["status"] == "fail"],
           "needs_manual": [r["id"] for r in results if r["status"] == "needs_manual"],
           "impact": impact_of(args.id) if args.id else [], "details": results}
    errs = [{"rule": "VERIFY", "level": "error", "ids": [r["id"]],
             "msg": f"цитата: {r['quote']}, числа: {r['numbers']}"} for r in results if r["status"] == "fail"]
    human = (f"Проверено {out['checked']}: ок {len(out['pass'])}, не сошлось {len(out['fail'])}, "
             f"вручную {len(out['needs_manual'])}.")
    return core.emit("verify", out, errors=errs, human=human, as_json=args.json)


# ---------- check: контекст и правила ----------

EPI_TAG = re.compile(r"\[(?:Факт|Вывод|Гипотеза|Прогноз|Мнение|Синтетика)")
GRADE_TAG = re.compile(r"\[(?:[ABCDF])\]|грейд\s*[ABCDF]\b|\bgrade\s*[ABCDF]\b", re.IGNORECASE)
NO_TAG = "<!-- no-tag -->"


class Ctx:
    def __init__(self, export: bool = False, today: Optional[str] = None):
        self.sources = core.latest("source")
        self.claims = core.latest("claim")
        self.objections = core.latest("objection")
        self.options = core.latest("option")
        self.decisions = core.latest("decision")
        self.gates = core.latest("gate")
        self.export = export
        self.today = today_date(today)
        self.pdir = core.project_dir()
        self.text_exclude: List[str] = []

    def active(self, d: Dict[str, Dict[str, Any]], statuses=("active",)) -> List[Dict[str, Any]]:
        return [x for x in sorted(d.values(), key=lambda x: x["id"]) if x.get("status") in statuses]

    def live_decisions(self) -> List[Dict[str, Any]]:
        return self.active(self.decisions, ("draft", "chosen", "locked", "revision_proposed"))

    def proposed_revision_of(self, did: str) -> Optional[Dict[str, Any]]:
        """Предложенный пересмотр решения did (revision_proposed с supersedes=did) или None."""
        for d in self.decisions.values():
            if d.get("status") == "revision_proposed" and d.get("supersedes") == did:
                return d
        return None

    def under_revision(self, rid: str) -> Optional[str]:
        """Для locked-решения (D###) или варианта locked-решения (O###) с предложенным пересмотром — D### преемника."""
        did = rid
        if rid.startswith("O"):
            did = (self.options.get(rid) or {}).get("decision_id") or ""
        d = self.decisions.get(did) or {}
        if d.get("status") != "locked":
            return None
        rev = self.proposed_revision_of(did)
        return rev["id"] if rev else None

    def premise_claims(self, d: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        for p in d.get("premises") or []:
            c = self.claims.get(p.get("claim_id"))
            if c:
                out.append(c)
        return out

    def basis_claims(self) -> List[Dict[str, Any]]:
        """Утверждения в основании решений: premises + привязанные к вариантам/решениям."""
        ids: List[str] = []
        for d in self.live_decisions():
            ids += [p.get("claim_id") for p in d.get("premises") or []]
        for c in self.claims.values():
            if c.get("status") == "active" and c.get("decision_link"):
                ids.append(c["id"])
        out = []
        for i in ids:
            if i in self.claims and self.claims[i] not in out:
                out.append(self.claims[i])
        return out

    def exporting(self) -> bool:
        return self.export or any(d.get("status") in ("chosen", "locked") for d in self.decisions.values())

    def text_files(self, globs: List[str]) -> List[Any]:
        skip = {p for g in self.text_exclude for p in self.pdir.glob(g)}
        files = []
        for g in globs:
            for p in sorted(self.pdir.glob(g)):
                if p.is_file() and p not in files and p not in skip:
                    files.append(p)
        return files


Finding = Dict[str, Any]


def _f(ids: List[str], msg: str) -> Finding:
    return {"ids": ids, "msg": msg}


def r_E01(x: Ctx) -> List[Finding]:
    return [_f([c["id"]], "факт без source_ids или quote_offset") for c in x.active(x.claims)
            if c.get("type") == "fact" and (not c.get("source_ids") or not c.get("quote_offset"))]


def r_E02(x: Ctx) -> List[Finding]:
    out = []
    for c in x.active(x.claims):
        if c.get("type") != "fact" or not c.get("source_ids"):
            continue
        kinds = [(x.sources.get(s) or {}).get("kind") for s in c["source_ids"]]
        if kinds and all(k in ("synthetic", "agent") for k in kinds):
            out.append(_f([c["id"]], "факт только на синтетических источниках"))
    return out


def r_E03(x: Ctx) -> List[Finding]:
    return [_f([c["id"]], f"факт с грейдом {c.get('grade')} в основании решения") for c in x.basis_claims()
            if c.get("type") == "fact" and c.get("grade") in ("D", "F") and c.get("status") == "active"]


def _inactive_basis_refs(x: Ctx) -> List[Dict[str, Any]]:
    """Основания решений и вариантов, а также выводы (derived_from), ссылающиеся на неактивные утверждения:
    [{ids, msg, revision}]. revision — D### предложенного пересмотра, если ссылка у зафиксированного решения
    (или его варианта; для вывода — у решения или варианта из его decision_link)."""
    out = []
    for d in x.live_decisions():
        for c in x.premise_claims(d):
            if c.get("status") != "active" and not d.get("ack_inactive"):
                out.append({"ids": [d["id"], c["id"]], "msg": f"основание {c['id']} в статусе {c.get('status')}",
                            "revision": x.under_revision(d["id"])})
    for o in x.active(x.options, ("open", "chosen", "locked")):
        for cid in option_claim_ids(o):
            c = x.claims.get(cid)
            if c and c.get("status") != "active":
                out.append({"ids": [o["id"], cid], "msg": f"вариант ссылается на {cid} ({c.get('status')})",
                            "revision": x.under_revision(o["id"])})
    for c in x.active(x.claims):
        for p in c.get("derived_from") or []:
            pc = x.claims.get(p)
            if pc and pc.get("status") != "active":
                rev = next((r for r in (x.under_revision(l) for l in c.get("decision_link") or []) if r), None)
                out.append({"ids": [c["id"], p], "msg": f"вывод опирается на {p} ({pc.get('status')})",
                            "revision": rev})
    return out


def r_E04(x: Ctx) -> List[Finding]:
    out = [_f(r["ids"], r["msg"]) for r in _inactive_basis_refs(x) if not r["revision"]]
    for c in x.active(x.claims):
        for s in c.get("source_ids") or []:
            src = x.sources.get(s)
            if src and src.get("status") != "active":
                out.append(_f([c["id"], s], f"источник {s} в статусе {src.get('status')}"))
    return out


def _decision_option_rule(x: Ctx, pred: Callable[[List[Dict[str, Any]]], bool], msg: str) -> List[Finding]:
    """E05–E07. Предложенный пересмотр (revision_proposed) не проверяется, пока по нему нет выбора."""
    return [_f([d["id"]], msg) for d in x.live_decisions()
            if d.get("status") != "revision_proposed" and pred(options_of_ctx(x, d["id"]))]


def options_of_ctx(x: Ctx, did: str) -> List[Dict[str, Any]]:
    return [o for o in sorted(x.options.values(), key=lambda o: o["id"]) if o.get("decision_id") == did]


def r_E05(x: Ctx) -> List[Finding]:
    return _decision_option_rule(x, lambda os: len(os) < 3, "вариантов меньше трёх")


def r_E06(x: Ctx) -> List[Finding]:
    return _decision_option_rule(x, lambda os: not any(o.get("kind") == "keep" for o in os), "нет варианта «оставить»")


def r_E07(x: Ctx) -> List[Finding]:
    return _decision_option_rule(x, lambda os: not any(o.get("kind") == "close" for o in os), "нет варианта «закрыть»")


def r_E08(x: Ctx) -> List[Finding]:
    targets = {r.get("target_id") for r in x.objections.values()}
    return [_f([o["id"]], "у варианта нет ни одного возражения") for o in x.active(x.options, ("open", "chosen", "locked"))
            if o["id"] not in targets]


def r_E09(x: Ctx) -> List[Finding]:
    if not x.exporting():
        return []
    return [_f([r["id"]], "возражение не закрыто (нет «учтено/отклонено»)") for r in x.active(x.objections, ("open",))]


def r_E10(x: Ctx) -> List[Finding]:
    out = []
    for p in x.text_files(x.globs):
        in_promises, row = False, 0
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            st = line.strip()
            if st.startswith("#"):
                in_promises, row = bool(re.search(r"обещан", st, re.IGNORECASE)), 0
                continue
            if not in_promises:
                continue
            if not st.startswith("|"):
                row = 0
                continue
            row += 1
            if row == 1 or re.fullmatch(r"\|[\s:|-]+\|?", st) or NO_TAG in st:
                continue  # шапка, разделитель или явное исключение
            if not GRADE_TAG.search(st):
                out.append(_f([f"{p.name}:{n}"], "строка таблицы обещаний без тега грейда"))
    return out


def r_E11(x: Ctx) -> List[Finding]:
    out = []
    for p in x.text_files(x.globs):
        text = p.read_text(encoding="utf-8")
        if NO_TAG + "-file" in text:
            continue
        in_code = False
        for n, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_code or line.startswith("#") or NO_TAG in line:
                continue
            nums = [m for m in numbers.extract(line) if m["unit"] in ("share", "pp", "RUB", "USD") or m["scale"] != 1]
            if nums and not EPI_TAG.search(line) and not re.search(r"\[[CS]\d{3,}\]", line):
                out.append(_f([f"{p.name}:{n}"], f"число {nums[0]['raw']} без эпистемического тега"))
    return out


def r_E12(x: Ctx) -> List[Finding]:
    out = []
    vplan = bool(x.text_files(["report/*pilot_design*.md", "report/*validation*.md", "report/*plan*proverk*.md"]))
    for d in x.live_decisions():
        g = min_grade([c.get("grade") for c in x.premise_claims(d)])
        if g and GRADE_ORDER[g] <= GRADE_ORDER["C"] and not (d.get("caveat") and (d.get("validation_plan") or vplan)):
            out.append(_f([d["id"]], f"основания до грейда {g} без оговорки (--caveat) и плана проверки"))
    return out


def r_E13(x: Ctx) -> List[Finding]:
    out = []
    seen = set()
    for c in x.basis_claims():
        for s in c.get("source_ids") or []:
            src = x.sources.get(s)
            if not src or s in seen:
                continue
            stale, age = staleness(src, x.today)
            if stale and not src.get("stale_note"):
                seen.add(s)
                out.append(_f([s, c["id"]], f"источник устарел ({age} дн.) и стоит в основании решения без пометки"))
    return out


def r_E14(x: Ctx) -> List[Finding]:
    return [_f([c["id"]], "число не найдено на странице источника") for c in x.active(x.claims)
            if c.get("material") and c.get("number_on_page") == "fail"]


def r_E15(x: Ctx) -> List[Finding]:
    out = []
    for c in x.active(x.claims):
        if c.get("type") != "inference":
            continue
        if not c.get("derived_from"):
            out.append(_f([c["id"]], "вывод без derived_from"))
        elif c.get("calc") and not c.get("plan_hash"):
            out.append(_f([c["id"]], "расчётный вывод без plan_hash"))
    return out


def r_E16(x: Ctx) -> List[Finding]:
    out = []
    for d in x.live_decisions():
        group: Dict[str, List[Dict[str, Any]]] = {}
        for c in x.active(x.claims):
            if c.get("metric_id") and d["id"] in linked_decisions(c):
                group.setdefault(c["metric_id"], []).append(c)
        for mid, cs in group.items():
            if len({metric_signature(c) for c in cs}) > 1:
                out.append(_f([d["id"]] + [c["id"] for c in cs], f"метрика {mid} в одном сравнении с разными определениями"))
    return out


def r_E17(x: Ctx) -> List[Finding]:
    return [_f([d["id"], c["id"]], "синтетика в основании решения о спросе/рынке/готовности платить")
            for d in x.live_decisions() if is_demand_decision(d)
            for c in x.premise_claims(d) if c.get("type") == "synthetic"]


def r_E18(x: Ctx) -> List[Finding]:
    return [_f([d["id"]], "решение выбрано не человеком или без ворот G###")
            for d in x.decisions.values() if d.get("status") in ("chosen", "locked")
            and (d.get("chosen_by") != "human" or not d.get("human_gate_id"))]


def r_E19(x: Ctx) -> List[Finding]:
    out = []
    history = core.read_jsonl(core.entity_path("decision"))
    locked: Dict[str, Dict[str, Any]] = {}
    skip = ("updated_at", "created_at", "status", "superseded_by")
    for rec in history:
        rid = rec.get("id")
        if not rid:
            continue
        if rid in locked and rec.get("status") not in ("superseded", "invalidated"):
            a = {k: v for k, v in locked[rid].items() if k not in skip}
            b = {k: v for k, v in rec.items() if k not in skip}
            if core.canonical_json(a) != core.canonical_json(b) or rec.get("status") != "locked":
                out.append(_f([rid], "правка зафиксированного решения в обход supersede"))
        if rec.get("status") == "locked" and rid not in locked:
            locked[rid] = rec
    return out


def r_E20(x: Ctx) -> List[Finding]:
    return [_f([d["id"]], "выбранное решение без revise_if") for d in x.decisions.values()
            if d.get("status") in ("chosen", "locked") and not d.get("revise_if")]


def r_E21(x: Ctx) -> List[Finding]:
    out = []
    for d in x.live_decisions():
        seen: Dict[Any, str] = {}
        for o in options_of_ctx(x, d["id"]):
            if o.get("kind") != "change":
                continue
            if not o.get("distinct_by"):
                out.append(_f([o["id"]], "вариант «изменить» без distinct_by"))
                continue
            key = (o["distinct_by"], (o.get("bet") or o.get("title") or "").strip().lower())
            if key in seen:
                out.append(_f([seen[key], o["id"]], "два варианта с одной осью отличия и одной ставкой"))
            else:
                seen[key] = o["id"]
    return out


def r_W01(x: Ctx) -> List[Finding]:
    return [_f([c["id"]], "прогноз без horizon или base_rate") for c in x.active(x.claims)
            if c.get("type") == "forecast" and not (c.get("horizon") and c.get("base_rate"))]


def r_W02(x: Ctx) -> List[Finding]:
    return [_f([c["id"]], "гипотеза без falsifier") for c in x.active(x.claims)
            if c.get("type") == "assumption" and not c.get("falsifier")]


def r_W03(x: Ctx) -> List[Finding]:
    return [_f([c["id"]], "числа нужно сверить вручную") for c in x.active(x.claims)
            if c.get("number_on_page") == "needs_manual"]


def r_W04(x: Ctx) -> List[Finding]:
    return [_f([o["id"]], "отклонённый вариант без revive_if") for o in x.active(x.options, ("rejected",))
            if not o.get("revive_if")]


def leaders(x: Ctx) -> List[Dict[str, Any]]:
    out = []
    for d in x.live_decisions():
        os_ = [o for o in options_of_ctx(x, d["id"]) if o.get("status") != "rejected"]
        lead = [o for o in os_ if o.get("status") in ("chosen", "locked") or o.get("leader")]
        if not lead:
            scored = [(sum(float((v or {}).get("score") or 0) for v in (o.get("criteria_scores") or {}).values()
                           if isinstance(v, dict)), o) for o in os_ if o.get("criteria_scores")]
            if scored:
                lead = [max(scored, key=lambda t: t[0])[1]]
        out += lead
    return out


def r_W05(x: Ctx) -> List[Finding]:
    return [_f([o["id"]], "у лидера нет flip_condition") for o in leaders(x) if not o.get("flip_condition")]


def r_W06(x: Ctx) -> List[Finding]:
    return [_f([o["id"]], "у варианта нет money_line") for o in x.active(x.options, ("open", "chosen", "locked"))
            if not o.get("money_line")]


def r_W07(x: Ctx) -> List[Finding]:
    out = []
    for d in x.decisions.values():
        if d.get("status") in ("chosen", "locked") and d.get("chosen_option"):
            o = x.options.get(d["chosen_option"])
            debt = evidence_debt(o, x.claims) if o else []
            if debt:
                out.append(_f([d["id"]] + debt, f"у выбранного варианта долг доказательств: {len(debt)}"))
    return out


def r_W08(x: Ctx) -> List[Finding]:
    return [_f([g["id"]], "вопрос без flips/why_ask или больше 7 утверждений") for g in x.gates.values()
            if len(g.get("claims_shown") or []) > 7 or g.get("flips") is None or not g.get("why_ask")]


def r_W09(x: Ctx) -> List[Finding]:
    state: Dict[str, Dict[str, Any]] = {}
    try:
        for rec in core.read_jsonl(core.entity_path("journal")):
            if rec.get("key"):
                state[rec["key"]] = rec
    except Exception:
        return []
    return [_f([r.get("id") or k[:16]], "запись журнала uncertain без решения человека")
            for k, r in state.items() if r.get("status") == "uncertain"]


def r_W10(x: Ctx) -> List[Finding]:
    p = core.state_dir() / "cost.json"
    journal = core.entity_path("journal")
    if not journal.exists() or not journal.stat().st_size:
        return []
    est = None
    if p.exists():
        try:
            import json
            est = (json.loads(p.read_text(encoding="utf-8")) or {}).get("estimate")
        except Exception:
            est = None
    return [] if est else [_f(["run"], "прогон начат без сметы (bulochka cost --estimate)")]


def r_W11(x: Ctx) -> List[Finding]:
    out = []
    for c in x.active(x.claims):
        if c.get("type") != "forecast" or c.get("p") is None:
            continue
        pr = c.get("p_range")
        if not pr or not (pr[0] <= c["p"] <= pr[1]):
            out.append(_f([c["id"]], "точечная вероятность вне p_range"))
    return out


def r_W12(x: Ctx) -> List[Finding]:
    out = []
    for s in x.active(x.sources):
        if s.get("access_class") in ("dead", "unreachable") and not s.get("archived_url") and not read_text(s).strip():
            out.append(_f([s["id"]], "страница недоступна, нет архивной ссылки и локальной копии"))
    return out


def r_W13(x: Ctx) -> List[Finding]:
    """Ссылки зафиксированного решения на заменённые утверждения при предложенном пересмотре (вместо E04)."""
    return [_f(r["ids"] + [r["revision"]], f"{r['msg']}; предложен пересмотр {r['revision']}")
            for r in _inactive_basis_refs(x) if r["revision"]]


def r_W15(x: Ctx) -> List[Finding]:
    """Открытый/выбранный вариант без своих C### (scores, money_line, decision_link): долг и последствия правок
    считаются по решению целиком, а не по варианту."""
    out = []
    for o in x.active(x.options, ("open", "chosen", "locked")):
        if not option_claim_ids(o, x.claims):
            out.append(_f([o["id"]], f"{o['id']}: оценки без ссылок на C### — `bulochka option set {o['id']} --scores …`"))
    return out


def r_W14(x: Ctx) -> List[Finding]:
    """Выбранное или зафиксированное решение, у которого остались открытые варианты: им нужен reject + revive_if."""
    out = []
    for d in x.live_decisions():
        if d.get("status") not in ("chosen", "locked"):
            continue
        alts = [o["id"] for o in options_of_ctx(x, d["id"])
                if o["id"] != d.get("chosen_option") and o.get("status") == "open"]
        if alts:
            out.append(_f([d["id"]] + alts, f"открыты {', '.join(alts)} — `bulochka option reject … --revive-if …`"))
    return out


RULES: Dict[str, Callable[[Ctx], List[Finding]]] = {k[2:]: v for k, v in globals().items()
                                                     if k.startswith("r_") and callable(v)}


def all_rules() -> Dict[str, Callable[[Ctx], List[Finding]]]:
    """Правила базового слоя + EXTRA_RULES из модулей x_*.py. Правило базового слоя расширение не подменяет."""
    out = dict(RULES)
    for mod in ext.load():
        extra = getattr(mod, "EXTRA_RULES", None)
        if not isinstance(extra, dict):
            continue
        for rid, fn in extra.items():
            if not callable(fn):
                continue
            if rid in RULES:
                ext.warn(f"правило {rid} из {mod.__name__.rsplit('.', 1)[-1]}.py совпадает с правилом базового слоя — пропущено")
                continue
            out[str(rid)] = fn
    return out


def _merge_rules(base: List[Dict[str, Any]], local: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_id = {r.get("id"): dict(r) for r in base if isinstance(r, dict)}
    order = [r.get("id") for r in base if isinstance(r, dict)]
    for r in local or []:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        if r["id"] in by_id:
            by_id[r["id"]].update(r)
        else:
            by_id[r["id"]] = dict(r)
            order.append(r["id"])
    return [by_id[i] for i in order]


def load_rules() -> Dict[str, Any]:
    rules_fns = all_rules()
    cfg = dict(core.load_config("quality_gate.json", {}) or {})
    local = core.load_config("quality_gate.local.json", {}) or {}
    rules = cfg.get("rules") or [{"id": rid, "level": "error" if rid.startswith("E") else "warning",
                                  "message": rid} for rid in sorted(RULES)]
    if isinstance(local, dict):
        rules = _merge_rules(rules, local.get("rules") or [])
        cfg.update({k: v for k, v in local.items() if k != "rules"})
    known = {r.get("id") for r in rules}
    for rid in sorted(rules_fns):
        if rid not in known:  # правило расширения без строки в конфиге — уровень по букве id
            doc = (rules_fns[rid].__doc__ or "").strip().splitlines()
            rules.append({"id": rid, "level": "error" if rid.startswith("E") else "warning",
                          "message": doc[0] if doc else rid})
    return {"rules": rules, "fns": rules_fns, "text_globs": cfg.get("text_globs") or ["report/[0-9]*.md"],
            "text_exclude": cfg.get("text_exclude", ["report/90_price_tag.md"]),
            "penalty": cfg.get("score") or {"base": 100, "error": 10, "warning": 2}}


def load_profile(name: Optional[str]) -> (str, Dict[str, str]):
    """Профиль проверки: явный --profile, иначе config/gate_profile.json["default"], иначе default."""
    cfg = core.load_config("gate_profile.json", {}) or {}
    profiles = cfg.get("profiles", {})
    name = name or cfg.get("default") or "default"
    if name not in profiles and name != "default":
        raise core.BulochkaError(f"Профиль проверки «{name}» не найден в config/gate_profile.json.")
    return name, dict((profiles.get(name) or {}).get("overrides", {}))


def run_check(profile: Optional[str] = None, export: bool = False, today: Optional[str] = None) -> Dict[str, Any]:
    conf = load_rules()
    pname, overrides = load_profile(profile)
    x = Ctx(export=export, today=today)
    x.globs = conf["text_globs"]
    x.text_exclude = conf["text_exclude"]
    errors, warnings = [], []
    for rule in conf["rules"]:
        fn = conf["fns"].get(rule["id"])
        if not fn:
            continue
        level = overrides.get(rule["id"], rule.get("level", "warning"))
        if level == "off":
            continue
        if rule["id"] in RULES:
            found = fn(x)
        else:
            try:
                found = fn(x) or []
            except Exception as e:  # noqa: BLE001 — сбой правила расширения не роняет check
                ext.warn(f"правило {rule['id']} упало: {type(e).__name__}: {e}")
                continue
        for f in found:
            item = {"rule": rule["id"], "level": level, "ids": f["ids"],
                    "msg": f"{rule.get('message', '')}: {f['msg']}".strip(": ")}
            (errors if level == "error" else warnings).append(item)
    pen = conf["penalty"]
    score = max(0, int(pen.get("base", 100)) - int(pen.get("error", 10)) * len(errors)
                - int(pen.get("warning", 2)) * len(warnings))
    report = {"ts": core.now_iso(), "profile": pname, "score": score, "errors": errors, "warnings": warnings,
              "counts": {"errors": len(errors), "warnings": len(warnings)}}
    core.atomic_write_json(core.state_dir() / "quality_gate_report.json", report)
    return report


def cmd_check(args) -> int:
    rep = run_check(args.profile, export=args.export, today=args.today)
    human = f"Проверка качества: {rep['score']}/100 · ошибок {len(rep['errors'])} · предупреждений {len(rep['warnings'])}"
    lines = [human]
    for e in rep["errors"] + rep["warnings"]:
        lines.append(f"[{e['level']}] {e['rule']} {', '.join(e['ids'])} — {e['msg']}")
    if args.json:
        return core.emit("check", {"score": rep["score"], "profile": rep["profile"], "errors": rep["errors"],
                                   "warnings": rep["warnings"]}, errors=rep["errors"], as_json=True)
    print("\n".join(lines))
    return core.EXIT_GATE_ERRORS if rep["errors"] else core.EXIT_OK


# ---------- refusals ----------

def cmd_refusals(args) -> int:
    if args.resolve:
        x = core.get("refusal", args.resolve)
        if not x:
            raise core.BulochkaError(f"Отказ {args.resolve} не найден.")
        if not args.as_:
            raise core.BulochkaError("--as accepted|overridden")
        if args.as_ == "overridden" and not args.reason:
            raise core.BulochkaError("Для overridden нужен --reason.")
        upd = dict(x)
        upd.update({"outcome": args.as_, "override_reason": args.reason})
        core.put("refusal", upd)
        return core.emit("refusals", {"id": x["id"], "outcome": args.as_}, human=f"{x['id']}: {args.as_}.",
                         as_json=args.json)
    items = sorted(core.latest("refusal").values(), key=lambda r: r["id"])
    decided = [r for r in items if r.get("outcome") in ("accepted", "overridden")]
    precision = (round(sum(1 for r in decided if r["outcome"] == "accepted") / len(decided), 3) if decided else None)
    if args.open:
        items = [r for r in items if r.get("outcome") == "pending"]
    lines = [f"{r['id']} {r.get('tag')} → {r.get('target_id')} [{r.get('outcome')}] {r.get('reason')}" for r in items]
    if precision is not None:
        lines.append(f"Точность отказов: {precision:.0%} ({len(decided)} решённых)")
    return core.emit("refusals", {"items": items, "precision": precision}, human="\n".join(lines) or "Отказов нет.",
                     as_json=args.json)


def register(sub) -> None:
    v = sub.add_parser("verify", help="перепроверить цитаты и числа; --impact ID — что зависит")
    v.add_argument("id", nargs="?")
    v.add_argument("--impact")
    v.add_argument("--quotes", action="store_true")
    v.add_argument("--numbers", action="store_true")
    v.set_defaults(func=cmd_verify)

    c = sub.add_parser("check", help="quality gate: счёт, ошибки, предупреждения")
    c.add_argument("--profile", help="имя профиля из config/gate_profile.json (по умолчанию — default оттуда)")
    c.add_argument("--export", action="store_true", help="проверка перед экспортом (E09 — открытые возражения)")
    c.add_argument("--today", help="YYYY-MM-DD (для проверки свежести)")
    c.set_defaults(func=cmd_check)

    r = sub.add_parser("refusals", help="отказы: список, точность, исход")
    r.add_argument("--open", action="store_true")
    r.add_argument("--resolve", help="X### — записать исход")
    r.add_argument("--as", dest="as_", choices=("accepted", "overridden"))
    r.add_argument("--reason")
    r.set_defaults(func=cmd_refusals)
