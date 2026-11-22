"""Утверждения: claim add / supersede / list. Проверки встроены (контракт §3).

claim add для type=fact:
  - нет источника или цитаты, цитаты нет в тексте источника, грейд F, источник неактивен → NO_SOURCE:
    утверждение записывается как assumption, отказ в refusals.jsonl, exit 2;
  - источник synthetic/agent → SYNTHETIC_AS_FACT: записывается как synthetic, exit 2;
  - устаревший источник у material-утверждения без пометки → STALE_SOURCE, exit 2;
  - числа нормализуются (numbers.py) и ищутся на странице → number_on_page.
type=synthetic со ссылкой на решение о спросе/рынке/готовности платить → SYNTHETIC_AS_FACT, ссылка снимается.
type=promise без активного claim-свойства продукта с грейдом ≥ B в --from → NO_PRODUCT_PROPERTY.
metric_id, по которому в том же решении уже есть claim с другим определением → INCOMPARABLE_METRICS, ссылка снимается.
Два сильных (A) источника по одной метрике расходятся > порога → A_CONFLICT: не блок, а авто-возражение R###.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from . import core, numbers, refusals
from .quotes import verify_quote
from .sources import GRADE_ORDER, min_grade, read_text, split_ids, staleness

TYPES = ("fact", "inference", "assumption", "judgment", "forecast", "promise", "synthetic", "common_knowledge")
STANCES = ("supports", "refutes", "neutral")
VERIFY_MINUTES = {"fact": 2, "inference": 3, "assumption": 1, "judgment": 1, "forecast": 3, "promise": 2,
                  "synthetic": 1, "common_knowledge": 0.5}
DEFAULT_GRADE = {"assumption": "D", "judgment": "C", "forecast": "D", "promise": "D", "synthetic": "D",
                 "common_knowledge": "B", "inference": "C"}
DEMAND_RE = re.compile(r"спрос|рын(?:ок|ка|ке|ку)|готовност\w*\s+(?:платить|заплатить)|\bwtp\b|demand|market|"
                       r"willingness", re.IGNORECASE)
DEMAND_TOPICS = ("demand", "market", "wtp")


# ---------- помощники ----------

def metrics_dict() -> Dict[str, Any]:
    cfg = core.load_config("metrics.json", {}) or {}
    return cfg.get("metrics", cfg)


def definition_hash(metric_id: str) -> Optional[str]:
    m = metrics_dict().get(metric_id)
    if not isinstance(m, dict):
        return None
    return core.sha256_text(core.canonical_json({"metric_id": metric_id, "formula": m.get("formula"),
                                                 "unit": m.get("unit")}))


def number_window() -> int:
    cfg = core.load_config("quality_gate.json", {}) or {}
    return int(cfg.get("number_window", numbers.DEFAULT_WINDOW))


def conflict_threshold() -> float:
    cfg = core.load_config("quality_gate.json", {}) or {}
    return float(cfg.get("conflict_threshold", 0.10))


def decision_of(ref: str) -> Optional[str]:
    """D### для ссылки O### или D###."""
    if ref.startswith("D"):
        return ref
    if ref.startswith("O"):
        o = core.get("option", ref)
        return o.get("decision_id") if o else None
    return None


def is_demand_decision(d: Optional[Dict[str, Any]]) -> bool:
    if not d:
        return False
    if d.get("topic") in DEMAND_TOPICS:
        return True
    return bool(DEMAND_RE.search(" ".join(str(d.get(k) or "") for k in ("question", "metric"))))


def primary_value(c: Dict[str, Any]) -> Optional[float]:
    for n in c.get("numbers_normalized") or []:
        if n.get("unit") != "year":
            return float(n["value"])
    return None


def metric_signature(c: Dict[str, Any]) -> Tuple:
    return (c.get("definition_hash"), c.get("period"), c.get("cohort"), c.get("denominator"))


def linked_decisions(c: Dict[str, Any]) -> List[str]:
    out = []
    for ref in c.get("decision_link") or []:
        d = decision_of(ref)
        if d and d not in out:
            out.append(d)
    return out


def _parse_range(s: Optional[str]) -> Optional[List[float]]:
    if not s:
        return None
    parts = [p for p in re.split(r"[,;\s]+", s.strip("[]() ")) if p]
    if len(parts) != 2:
        raise core.BulochkaError("--p-range: два числа через запятую, например 0.3,0.5")
    lo, hi = float(parts[0].replace("%", "")), float(parts[1].replace("%", ""))
    if lo > 1 or hi > 1:
        lo, hi = lo / 100, hi / 100
    if not (0 <= lo <= hi <= 1):
        raise core.BulochkaError("--p-range: 0 ≤ нижняя ≤ верхняя ≤ 1")
    return [lo, hi]


# ---------- claim add ----------

def _refuse(tag: str, cid: str, reason: str, done: str, not_doing: str, detail: str, changes: str,
            next_human: str, next_cmd: str, extra: Optional[Dict[str, Any]] = None,
            outcome: str = "pending") -> Dict[str, Any]:
    cost = refusals.DEFAULT_COST[tag]
    text = refusals.phrase(done, not_doing, tag, detail, changes, next_human, cost)
    return refusals.record(tag, target_id=cid, reason=reason, phrase_text=text, next_action=next_cmd,
                           cost=cost, extra=extra, outcome=outcome)


def cmd_add(args) -> int:
    ctype = args.type
    cid = core.next_id("claim")
    sids = split_ids(args.source)
    derived = split_ids(getattr(args, "from_", None))
    links = split_ids(args.link)
    warnings: List[Dict[str, Any]] = []

    srcs = []
    for sid in sids:
        s = core.get("source", sid)
        if not s:
            raise core.BulochkaError(f"Источник {sid} не найден: сначала `bulochka source add`.")
        srcs.append(s)
    for did in derived:
        if not core.get("claim", did):
            raise core.BulochkaError(f"Утверждение {did} не найдено.")
    for ref in links:
        if not ref[:1] in ("O", "D") or not core.resolve_ref(ref):
            raise core.BulochkaError(f"--link: {ref} не найден (ожидается O### или D###).")

    rec: Dict[str, Any] = {
        "id": cid, "text": args.text, "type": ctype, "grade": None, "source_ids": sids,
        "quote": args.quote, "quote_offset": None, "quote_source": None, "derived_from": derived,
        "stance": args.stance, "decision_link": links, "material": bool(links or args.material),
        "status": "active", "supersedes": None, "question_id": args.question,
        "numbers_normalized": numbers.extract(args.text), "number_on_page": None,
        "metric_id": args.metric, "definition_hash": None, "period": args.period, "cohort": args.cohort,
        "denominator": args.denominator, "data_period_end": args.data_period_end,
        "p_range": _parse_range(args.p_range), "p": args.p, "confidence": args.confidence, "horizon": args.horizon,
        "base_rate": args.base_rate, "falsifier": args.falsifier, "plan_hash": args.plan_hash,
        "calc": bool(args.calc), "verify_minutes": args.verify_minutes or VERIFY_MINUTES[ctype],
        "actor": "agent", "refusal_id": None,
    }
    if getattr(args, "actor", None) == "human":
        from .journal import is_human
        if is_human():
            rec["actor"] = "human"
        else:
            warnings.append({"rule": "W-ACTOR", "level": "warning", "ids": [cid],
                             "msg": "--actor human только из терминала человека (BULOCHKA_ACTOR=human или "
                                    "интерактивный ввод) — записано как agent"})
    if args.metric:
        dh = definition_hash(args.metric)
        if dh is None:
            warnings.append({"rule": "W-METRIC", "level": "warning", "ids": [cid],
                             "msg": f"метрики {args.metric} нет в config/metrics.json — сравнение по ней не проверить"})
        rec["definition_hash"] = dh

    refusal: Optional[Dict[str, Any]] = None
    links_txt = ", ".join(links)

    if ctype == "fact":
        refusal = _check_fact(rec, srcs, links_txt)
    elif ctype == "synthetic":
        demand = [ref for ref in links if is_demand_decision(core.get("decision", decision_of(ref) or ""))]
        if demand:
            rec["decision_link"] = [x for x in links if x not in demand]
            rec["material"] = bool(rec["decision_link"] or args.material)
            refusal = _refuse("SYNTHETIC_AS_FACT", cid, "synthetic_in_demand_link",
                              f"Записал {cid} как [Синтетика]", "Основанием решения о спросе не ставлю", "",
                              "Меняется: спрос и готовность платить остаются гипотезой",
                              "8–10 реальных интервью по гайду", "bulochka claim add --type assumption --falsifier …",
                              extra={"premise_of": decision_of(demand[0])})
        rec["grade"] = "D"
        if not sids:
            warnings.append({"rule": "W-SYNTH", "level": "warning", "ids": [cid],
                             "msg": "у синтетики нет source_ids — укажите сохранённый протокол ответов"})
    elif ctype == "promise":
        props = [core.get("claim", d) for d in derived]
        ok = [p for p in props if p and p.get("status") == "active"
              and GRADE_ORDER.get(p.get("grade") or "F", 0) >= GRADE_ORDER["B"]]
        rec["grade"] = min_grade([p.get("grade") for p in ok]) or "D"
        if not ok:
            rec["blocked"] = True
            refusal = _refuse("NO_PRODUCT_PROPERTY", cid, "no_property_claim",
                              f"Записал обещание {cid} в проверку", "В текст стратегии не ставлю",
                              "среди --from нет активного утверждения о свойстве продукта с грейдом A или B",
                              "Меняется: сообщение уходит в «проверить»",
                              "подтвердить свойство источником или переформулировать обещание",
                              "bulochka claim add --type fact --source S### --quote \"…\"",
                              extra={"missing_property": args.text})
    elif ctype == "inference":
        parents = [core.get("claim", d) for d in derived]
        rec["grade"] = min_grade([p.get("grade") for p in parents if p]) or DEFAULT_GRADE["inference"]
        if not derived:
            warnings.append({"rule": "E15", "level": "warning", "ids": [cid],
                             "msg": "вывод без --from C### — gate отметит ошибку"})
    else:
        rec["grade"] = DEFAULT_GRADE.get(ctype, "C")
        if ctype == "assumption" and not args.falsifier:
            warnings.append({"rule": "W02", "level": "warning", "ids": [cid],
                             "msg": "у гипотезы нет --falsifier (что её опровергло бы)"})
        if ctype == "forecast" and not (args.horizon and args.base_rate):
            warnings.append({"rule": "W01", "level": "warning", "ids": [cid],
                             "msg": "у прогноза нет --horizon или --base-rate"})

    if args.grade and GRADE_ORDER[args.grade] < GRADE_ORDER.get(rec["grade"] or "A", 4):
        rec["grade"] = args.grade  # понижать можно, повышать выше источника — нет

    # INCOMPARABLE_METRICS: в одном решении метрика с другим определением
    if refusal is None and rec["metric_id"] and rec["decision_link"]:
        refusal = _check_incomparable(rec)

    if refusal:
        rec["refusal_id"] = refusal["id"]
    core.put("claim", rec)

    conflict = None
    if refusal is None and rec["metric_id"] and rec["type"] == "fact" and rec["grade"] == "A":
        conflict = _check_a_conflict(rec)

    data = {"id": cid, "type": rec["type"], "grade": rec["grade"], "number_on_page": rec["number_on_page"],
            "quote_offset": rec["quote_offset"]}
    if refusal:
        data["refusal"] = refusals.as_payload(refusal)
    if conflict:
        data["conflict"] = conflict
    human = f"Записал {cid} ({rec['type']}, грейд {rec['grade']})."
    if rec["number_on_page"]:
        human += f" Числа на странице: {rec['number_on_page']}."
    if conflict:
        human += f" {conflict['phrase']}"
    if rec["number_on_page"] == "fail":
        warnings.append({"rule": "E14" if rec["material"] else "W-NUM", "level": "warning", "ids": [cid],
                         "msg": "числа из текста утверждения не найдены на странице источника"})
    return core.emit("claim add", data, errors=warnings,
                     refusal=refusals.as_payload(refusal) if refusal else None, human=None if refusal else human,
                     next_cmd=(refusal["next_action"] if refusal else f"bulochka verify {cid}"), as_json=args.json)


def _check_fact(rec: Dict[str, Any], srcs: List[Dict[str, Any]], links_txt: str) -> Optional[Dict[str, Any]]:
    cid = rec["id"]
    changes = (f"Из-за этого {links_txt} держится на допущении" if links_txt
               else "Опираться на него как на факт нельзя")
    next_h = "найти источник с дословной цитатой или спросить вас"
    next_cmd = "bulochka source add … && bulochka claim add --type fact --source S### --quote \"…\""

    def no_source(reason: str, detail: str) -> Dict[str, Any]:
        rec["type"] = "assumption"
        rec["original_type"] = "fact"
        rec["grade"] = "D"
        return _refuse("NO_SOURCE", cid, reason, f"Записал {cid} как гипотезу", "Как факт не беру", detail,
                       changes, next_h, next_cmd)

    if not srcs or not (rec.get("quote") or "").strip():
        return no_source("no_quote", "нет источника и дословной цитаты" if not srcs else "нет дословной цитаты")
    synth = [s["id"] for s in srcs if s.get("kind") in ("synthetic", "agent")]
    if synth:
        rec["type"] = "synthetic"
        rec["original_type"] = "fact"
        rec["grade"] = "D"
        return _refuse("SYNTHETIC_AS_FACT", cid, "synthetic_source",
                       f"Записал {cid} как [Синтетика]", "Как факт не беру", f"источник {', '.join(synth)} — синтетика",
                       "Меняется: на это утверждение нельзя опираться как на данные о рынке",
                       "найти реальный источник или 8–10 интервью по гайду", next_cmd)
    inactive = [s["id"] for s in srcs if s.get("status") != "active"]
    if inactive:
        return no_source("source_inactive", f"источник {', '.join(inactive)} исключён или заменён")
    found = None
    for s in srcs:
        off = verify_quote(rec["quote"], read_text(s))
        if off:
            found = (s, off)
            break
    if not found:
        return no_source("quote_not_found", f"цитаты нет на странице {', '.join(s['id'] for s in srcs)}")
    src, off = found
    rec["quote_offset"] = [off[0], off[1]]
    rec["quote_source"] = src["id"]
    rec["grade"] = src.get("grade") or "C"
    if rec["grade"] == "F":
        return no_source("grade_F", f"источник {src['id']} с грейдом F")
    rec["number_on_page"] = numbers.number_on_page(rec["numbers_normalized"], read_text(src), rec["quote_offset"],
                                                   number_window())
    if rec["material"]:
        is_stale, age = staleness(src)
        if is_stale and not src.get("stale_note"):
            rec["stale_source"] = src["id"]
            return _refuse("STALE_SOURCE", src["id"], f"age_days={age}",
                           f"Записал {cid} с пометкой «устарело»",
                           f"На {src['id']} без пометки не опираюсь", f"ему {age} дн. при сроке {src.get('ttl_days')}",
                           "Меняется: основание решения требует свежих данных",
                           f"`bulochka source refresh {src['id']} --file …` или `source mark-stale`",
                           f"bulochka source refresh {src['id']} --file <свежий текст>",
                           extra={"age_days": age, "claim_id": cid})
    return None


def _check_incomparable(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    mine = set(linked_decisions(rec))
    sig = metric_signature(rec)
    for c in core.latest("claim").values():
        if c.get("status") != "active" or c.get("metric_id") != rec["metric_id"]:
            continue
        if not mine.intersection(linked_decisions(c)):
            continue
        if metric_signature(c) != sig:
            diff = [f for f, a, b in zip(("definition_hash", "period", "cohort", "denominator"), sig,
                                         metric_signature(c)) if a != b]
            removed = rec["decision_link"]
            rec["decision_link"] = []
            rec["material"] = False
            return _refuse("INCOMPARABLE_METRICS", rec["id"], f"diff_fields={','.join(diff)}",
                           f"Записал {rec['id']} без привязки к {', '.join(removed)}",
                           f"Не сравниваю с {c['id']}", "разные " + ", ".join(diff),
                           "Меняется: в сравнении вариантов эта цифра не участвует",
                           "привести к одному определению через calc.py или убрать из сравнения",
                           "bulochka claim add --type inference --from … --plan-hash …",
                           extra={"target_ids": [rec["id"], c["id"]], "diff_fields": diff})
    return None


def _check_a_conflict(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    v = primary_value(rec)
    if v is None:
        return None
    thr = conflict_threshold()
    for c in sorted(core.latest("claim").values(), key=lambda x: x["id"]):
        if c["id"] == rec["id"] or c.get("status") != "active" or c.get("type") != "fact" or c.get("grade") != "A":
            continue
        if c.get("metric_id") != rec["metric_id"] or c.get("definition_hash") != rec.get("definition_hash"):
            continue
        if rec.get("period") and c.get("period") and rec["period"] != c["period"]:
            continue
        if set(c.get("source_ids") or []) & set(rec.get("source_ids") or []):
            continue
        w = primary_value(c)
        if w is None:
            continue
        d = max(abs(v), abs(w))
        if d == 0 or abs(v - w) / d <= thr:
            continue
        s1 = (c.get("quote_source") or (c.get("source_ids") or ["?"])[0])
        s2 = (rec.get("quote_source") or (rec.get("source_ids") or ["?"])[0])
        raw1 = (c["numbers_normalized"] or [{}])[0].get("raw")
        raw2 = (rec["numbers_normalized"] or [{}])[0].get("raw")
        obj = core.put("objection", {
            "target_id": rec["id"], "text": f"{s1} и {s2} расходятся по {rec['metric_id']}: {raw1} против {raw2}",
            "severity": "medium", "evidence_ids": [c["id"], rec["id"]], "status": "open", "resolution": None,
            "material": True, "seeded": False, "author": "agent", "actor": "agent"})
        ref = _refuse("A_CONFLICT", rec["metric_id"], f"rel_diff={abs(v - w) / d:.3f}",
                      f"{s1} и {s2} расходятся: {raw1} против {raw2}", "Одну цифру не выбираю", "показываю обе",
                      f"Завёл возражение {obj['id']}",
                      "уточнить период или задать вопрос", f"bulochka objection resolve {obj['id']} --status … --resolution …",
                      extra={"source_ids": [s1, s2], "values": [w, v], "claim_ids": [c["id"], rec["id"]],
                             "objection_id": obj["id"]})
        return {"objection_id": obj["id"], "refusal_id": ref["id"], "with": c["id"], "phrase": ref["phrase"]}
    return None


# ---------- supersede / list ----------

def _assumption_ids(conds: Any) -> List[str]:
    """assumption_id из revise_if[] / revive_if[] (условия пересмотра и возврата)."""
    if isinstance(conds, dict):
        conds = [conds]
    return [c.get("assumption_id") for c in conds or [] if isinstance(c, dict) and c.get("assumption_id")]


DEAD_DECISION = ("superseded", "invalidated")


def impact_of(rid: str) -> List[str]:
    """Что зависит от ID: утверждения (транзитивно через derived_from), варианты, решения, возражения.

    Рёбра: C → утверждения (derived_from), C → варианты/решения (decision_link, criteria_scores, money_line,
    killed_by, premises, because, revise_if[]/revive_if[].assumption_id), C → возражения (evidence_ids, target_id),
    R → вариант, который оно отклонило (killed_by), R → его цель (target_id), O → его решение (кроме отклонённого варианта).
    Заменённые и отменённые решения (superseded/invalidated) — история: затронутыми не считаются."""
    seen: List[str] = []
    frontier = [rid]
    claims = core.latest("claim")
    options = core.latest("option")
    decisions = core.latest("decision")
    objections = core.latest("objection")
    while frontier:
        cur = frontier.pop()
        hits: List[str] = []
        for c in claims.values():
            if c.get("status") != "active":
                continue
            if cur in (c.get("derived_from") or []) or cur in (c.get("source_ids") or []):
                hits.append(c["id"])
        if cur.startswith("C"):
            c = claims.get(cur)
            for ref in (c or {}).get("decision_link") or []:
                hits.append(ref)
            for o in options.values():
                for v in (o.get("criteria_scores") or {}).values():
                    if isinstance(v, dict) and cur in (v.get("claim_ids") or []):
                        hits.append(o["id"])
                if cur in ((o.get("money_line") or {}).get("claim_ids") or []) or cur in (o.get("killed_by") or []):
                    hits.append(o["id"])
                if cur in _assumption_ids(o.get("revive_if")):
                    hits.append(o["id"])
            for d in decisions.values():
                if cur in [p.get("claim_id") for p in d.get("premises") or []] or cur in (d.get("because") or []):
                    hits.append(d["id"])
                if cur in _assumption_ids(d.get("revise_if")):
                    hits.append(d["id"])
            for r in objections.values():
                if cur in (r.get("evidence_ids") or []) or r.get("target_id") == cur:
                    hits.append(r["id"])
        if cur.startswith("R"):
            r = objections.get(cur) or {}
            if r.get("target_id"):
                hits.append(r["target_id"])
            for o in options.values():
                if cur in (o.get("killed_by") or []):
                    hits.append(o["id"])
        if cur.startswith("O"):
            o = options.get(cur)
            # отклонённый вариант решение не двигает: он сам в списке затронутых, возврат — через ворота
            if o and o.get("decision_id") and o.get("status") != "rejected":
                hits.append(o["decision_id"])
        for h in hits:
            if h == rid or h in seen:
                continue
            if h.startswith("D") and (decisions.get(h) or {}).get("status") in DEAD_DECISION:
                continue
            seen.append(h)
            frontier.append(h)
    return sorted(seen)


def _swap_ids(ids: Any, old_id: str, new_id: str) -> Tuple[List[str], bool]:
    """Список ID с заменой old → new без дублей; второй элемент — была ли замена."""
    out: List[str] = []
    hit = False
    for i in ids or []:
        if i == old_id:
            i, hit = new_id, True
        if i not in out:
            out.append(i)
    return out, hit


def _frozen_ref(ref: str, decisions: Dict[str, Dict[str, Any]], options: Dict[str, Dict[str, Any]]) -> bool:
    """Ссылка на зафиксированное: locked-решение или вариант (сам locked или его решение locked)."""
    if ref.startswith("D"):
        return (decisions.get(ref) or {}).get("status") == "locked"
    if ref.startswith("O"):
        o = options.get(ref) or {}
        return o.get("status") == "locked" or (decisions.get(o.get("decision_id") or "") or {}).get("status") == "locked"
    return False


def move_refs(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, List[str]]:
    """Перенос ссылок со старого утверждения на новое (кроме зафиксированных решений и вариантов):
    основания решений (premises), decision_link утверждения, claim_ids в criteria_scores и money_line вариантов.
    Без переноса заменённое утверждение остаётся в основаниях навсегда и E04 не исправить.
    Зафиксированные (locked) не трогаем — для них E04 = повод к пересмотру. Возвращает, что перенесено."""
    oid, nid = old["id"], new["id"]
    decisions = core.latest("decision")
    options = core.latest("option")
    moved: Dict[str, List[str]] = {"premises": [], "options": [], "links": []}
    for d in decisions.values():
        if d.get("status") in ("draft", "chosen", "revision_proposed") and any(
                pr.get("claim_id") == oid for pr in d.get("premises") or []):
            du = dict(d)
            prem, have = [], set()
            for pr in d.get("premises") or []:
                cid = nid if pr.get("claim_id") == oid else pr.get("claim_id")
                if cid in have:
                    continue
                have.add(cid)
                prem.append(dict(pr, claim_id=cid) if cid != pr.get("claim_id") else pr)
            du["premises"] = prem
            core.put("decision", du)
            moved["premises"].append(d["id"])
    for o in sorted(options.values(), key=lambda x: x["id"]):
        if _frozen_ref(o["id"], decisions, options):
            continue
        ou = dict(o)
        hit = False
        scores = {}
        for k, v in (o.get("criteria_scores") or {}).items():
            if isinstance(v, dict) and oid in (v.get("claim_ids") or []):
                ids, _ = _swap_ids(v.get("claim_ids"), oid, nid)
                v = dict(v, claim_ids=ids)
                hit = True
            scores[k] = v
        ml = o.get("money_line")
        if isinstance(ml, dict) and oid in (ml.get("claim_ids") or []):
            ids, _ = _swap_ids(ml.get("claim_ids"), oid, nid)
            ou["money_line"] = dict(ml, claim_ids=ids)
            hit = True
        if hit:
            ou["criteria_scores"] = scores
            core.put("option", ou)
            moved["options"].append(o["id"])
    keep = [r for r in old.get("decision_link") or [] if _frozen_ref(r, decisions, options)]
    move = [r for r in old.get("decision_link") or [] if r not in keep]
    if move:
        links = list(new.get("decision_link") or [])
        for r in move:
            if r not in links:
                links.append(r)
        new["decision_link"] = links
        new["material"] = True
        old["decision_link"] = keep
        old["decision_link_moved"] = {"to": nid, "refs": move}
        moved["links"] = move
    return moved


def cmd_supersede(args) -> int:
    old = core.get("claim", args.id)
    new = core.get("claim", args.by)
    if not old or not new:
        raise core.BulochkaError("Нужны существующие C### для обоих аргументов.")
    if old["id"] == new["id"]:
        raise core.BulochkaError("Утверждение не заменяет само себя.")
    warnings: List[Dict[str, Any]] = []
    actor = "agent"
    if getattr(args, "actor", None) == "human":
        from .journal import is_human
        if is_human():
            actor = "human"
        else:
            warnings.append({"rule": "W-ACTOR", "level": "warning", "ids": [old["id"]],
                             "msg": "--actor human только из терминала человека (BULOCHKA_ACTOR=human или "
                                    "интерактивный ввод) — записано как agent"})
    impact = impact_of(old["id"])
    upd = dict(old)
    nupd = dict(new)
    nupd["supersedes"] = old["id"]
    moved = move_refs(upd, nupd)
    upd.update({"status": "superseded", "superseded_by": new["id"], "superseded_actor": actor})
    core.put("claim", upd)
    core.put("claim", nupd)
    parts = []
    if moved["premises"]:
        parts.append(f"основания {', '.join(moved['premises'])}")
    if moved["options"]:
        parts.append(f"оценки вариантов {', '.join(moved['options'])}")
    if moved["links"]:
        parts.append(f"привязки {', '.join(moved['links'])}")
    who = " (человек)" if actor == "human" else ""
    return core.emit("claim supersede", {"id": old["id"], "by": new["id"], "impact": impact, "actor": actor,
                                         "premises_moved": moved["premises"], "options_moved": moved["options"],
                                         "links_moved": moved["links"]},
                     errors=warnings,
                     human=f"{old['id']} заменён на {new['id']}{who}. Затронуто: {', '.join(impact) or 'ничего'}."
                           + (f" Переведены на {new['id']}: {'; '.join(parts)}." if parts else ""),
                     next_cmd=f"bulochka verify --impact {old['id']}", as_json=args.json)


def cmd_list(args) -> int:
    items = sorted(core.latest("claim").values(), key=lambda c: c["id"])
    if not args.all:
        items = [c for c in items if c.get("status") == "active"]
    if args.type:
        items = [c for c in items if c.get("type") == args.type]
    lines = [f"{c['id']} [{c.get('type')}/{c.get('grade')}] {c.get('text')}" for c in items]
    return core.emit("claim list", {"items": items}, human="\n".join(lines) or "Утверждений нет.", as_json=args.json)


def register(sub) -> None:
    p = sub.add_parser("claim", help="утверждения: add, supersede, list")
    ss = p.add_subparsers(dest="claim_cmd")

    a = ss.add_parser("add", help="записать утверждение с проверками")
    a.add_argument("--text", required=True)
    a.add_argument("--type", required=True, choices=TYPES)
    a.add_argument("--source", help="S### (через запятую)")
    a.add_argument("--quote", help="дословная цитата из текста источника")
    a.add_argument("--from", dest="from_", help="C### для inference/promise (через запятую)")
    a.add_argument("--link", help="O### / D### (через запятую)")
    a.add_argument("--metric", help="metric_id из config/metrics.json")
    a.add_argument("--period")
    a.add_argument("--cohort")
    a.add_argument("--denominator")
    a.add_argument("--data-period-end", dest="data_period_end")
    a.add_argument("--grade", choices=("A", "B", "C", "D", "F"), help="понизить грейд")
    a.add_argument("--stance", choices=STANCES, default="supports")
    a.add_argument("--material", action="store_true")
    a.add_argument("--question", help="Q# из плана")
    a.add_argument("--horizon")
    a.add_argument("--base-rate", dest="base_rate")
    a.add_argument("--falsifier")
    a.add_argument("--p-range", dest="p_range", help="0.3,0.5")
    a.add_argument("--p", type=float, help="точечная вероятность (не рекомендуется, W11)")
    a.add_argument("--confidence", choices=("low", "medium", "high"))
    a.add_argument("--plan-hash", dest="plan_hash")
    a.add_argument("--calc", action="store_true", help="вывод получен расчётом (нужен --plan-hash)")
    a.add_argument("--verify-minutes", dest="verify_minutes", type=float)
    a.add_argument("--actor", choices=("agent", "human"), default="agent",
                   help="human — только если команду запускает человек; иначе запись понижается до agent")
    a.set_defaults(func=cmd_add)

    s = ss.add_parser("supersede", help="заменить утверждение новым")
    s.add_argument("id")
    s.add_argument("--by", required=True)
    s.add_argument("--actor", choices=("agent", "human"), default="agent",
                   help="human — только если команду запускает человек; иначе запись понижается до agent")
    s.set_defaults(func=cmd_supersede)

    lst = ss.add_parser("list", help="список утверждений")
    lst.add_argument("--all", action="store_true")
    lst.add_argument("--type", choices=TYPES)
    lst.set_defaults(func=cmd_list)
