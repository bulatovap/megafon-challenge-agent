"""Варианты и решения: option add/set/reject/revive/choose/list, decision add/set/choose/lock/due/diff/list.

Выбор делает только человек: `decision choose` (и `option choose`) без ответа человека на воротах G###,
с --actor agent или с ответом, где нет выбранного варианта → отказ HUMAN_DECISION, exit 2.

PRE_CHOOSE — валидаторы перед записью выбора (`decision choose`, `option choose`) и фиксации (`decision lock`):
fn(decision, option) -> отказ | None. Отказ — запись refusals.record(...) или словарь с полями tag/phrase/
next_action; он отдаётся как есть, exit 2. Модули x_*.py добавляют свои функции при импорте:
`decisions.PRE_CHOOSE.append(fn)`.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

from . import core, ext, refusals
from .claims import is_demand_decision
from .sources import min_grade, split_ids, today_date, parse_date

KINDS = ("keep", "close", "change")
DISTINCT = ("audience", "revenue_model", "channel", "bet")
REVERSIBILITY = ("one_way", "two_way")
CERTAINTY = ("low", "moderate", "high")
STRENGTH = ("weak", "strong")
DEBT_TYPES = ("assumption", "forecast")
DEBT_STATUSES = ("open", "chosen", "locked", None)  # варианты, долг которых считается в сводке

# Пересмотр решения: `decision add --supersedes D### --proposed` → черновик-преемник в этом статусе.
# Старое решение остаётся в силе (locked/chosen), пока человек не выберет вариант преемника; тогда старое → superseded.
REVISION_PROPOSED = "revision_proposed"

PRE_CHOOSE: List[Callable[[Dict[str, Any], Dict[str, Any]], Optional[Dict[str, Any]]]] = []


# ---------- помощники ----------

def _json_arg(value: Optional[str], name: str, as_list: bool = False) -> Any:
    if value is None:
        return None
    try:
        obj = json.loads(value)
    except json.JSONDecodeError as e:
        raise core.BulochkaError(f"{name}: невалидный JSON ({e.msg}).")
    if as_list and isinstance(obj, dict):
        obj = [obj]
    if as_list and not isinstance(obj, list):
        raise core.BulochkaError(f"{name}: ожидается объект или список объектов.")
    return obj


def shared_claim_warnings(conds: Any, rid: str) -> List[Dict[str, Any]]:
    """W-INDICATOR: одно утверждение под разными индикаторами — замер одного попадёт во все (прогон 22.11:
    «3 %» по SP1 сработало и для SP1b). На каждый индикатор — своё измеримое утверждение."""
    by: Dict[str, set] = {}
    for c in conds or []:
        if isinstance(c, dict) and c.get("assumption_id") and c.get("indicator"):
            by.setdefault(c["assumption_id"], set()).add(str(c["indicator"]).strip().lower())
    return [{"rule": "W-INDICATOR", "level": "warning", "ids": [rid, cid],
             "msg": f"{cid} стоит под {len(ind)} разными индикаторами — заведите на каждый своё утверждение-замер "
                    f"(`claim add --type assumption …`) и укажите его в assumption_id, иначе один замер сработает за все"}
            for cid, ind in by.items() if len(ind) > 1]


def threshold_warnings(conds: Any, rid: str, field: str) -> List[Dict[str, Any]]:
    """W-THRESHOLD: порог в revise_if/revive_if, который не разобрать core.parse_threshold.
    Семантика: revise_if — граница, при которой решение держится; revive_if — условие возврата варианта."""
    out = []
    for c in conds or []:
        if not isinstance(c, dict) or c.get("threshold") in (None, ""):
            continue
        try:
            core.parse_threshold(c["threshold"])
        except core.BulochkaError as e:
            out.append({"rule": "W-THRESHOLD", "level": "warning", "ids": [rid],
                        "msg": f"{field}: {e} Сверка по данным не сработает — только по дате."})
    return out


def revise_if_arg(value: Optional[str], name: str = "--revise-if") -> Optional[List[Dict[str, Any]]]:
    """--revise-if: JSON-список условий. Условие пересмотра пишется как `revise_when` («≥ 50%» — когда пересматривать);
    CLI сам переводит его в `threshold` — границу, при которой решение держится («< 50%»). Оба поля сразу — должны
    совпадать по смыслу, иначе ошибка."""
    return normalize_revise_if(_json_arg(value, name, as_list=True), name)


def normalize_revise_if(conds: Any, name: str = "--revise-if") -> Any:
    """revise_when (когда пересматривать) → threshold (граница, где решение держится); см. revise_if_arg."""
    for c in conds or []:
        if not isinstance(c, dict) or c.get("revise_when") in (None, ""):
            continue
        holds = core.holds_from_trigger(c["revise_when"])
        if c.get("threshold") not in (None, ""):
            try:
                same = core.parse_threshold(c["threshold"]) == core.parse_threshold(holds)
            except core.BulochkaError:
                same = False
            if not same:
                raise core.BulochkaError(
                    f"{name}: «{c.get('indicator')}» — revise_when «{c['revise_when']}» и threshold «{c['threshold']}» "
                    f"противоречат: threshold — граница, где решение держится ({holds}). Оставьте одно — revise_when.")
        c["threshold"] = holds
    return conds


def revise_if_read(conds: Any, rid: str) -> List[str]:
    """Как CLI понял пороги: «держится, пока …; пересмотр, когда …» — чтобы перепутанный смысл было видно сразу."""
    out = []
    for c in conds or []:
        if not isinstance(c, dict) or c.get("threshold") in (None, ""):
            continue
        try:
            core.parse_threshold(c["threshold"])
        except core.BulochkaError:
            continue
        op, rest = core.split_threshold(c["threshold"])
        neg = core.NEG_OP.get(op)
        tail = f"; пересмотр, когда {neg} {rest}" if neg else ""
        out.append(f"{rid}: «{c.get('indicator') or c.get('assumption_id') or '—'}» — решение держится, пока {op} {rest}{tail}.")
    if out:
        out.append("Если наоборот — поправьте: `decision set " + rid + " --revise-if '[{\"indicator\":\"…\",\"revise_when\":\"…\"}]'`.")
    return out


def open_alternatives(d: Dict[str, Any]) -> List[str]:
    """Открытые варианты решения, кроме выбранного: до фиксации их надо отклонить с условием возврата."""
    return sorted(o["id"] for o in core.latest("option").values()
                  if o.get("decision_id") == d["id"] and o["id"] != d.get("chosen_option")
                  and o.get("status") == "open")


REJECT_HINT = ("`bulochka option reject O### --killed-by R###|C### --revive-if "
               "'[{\"assumption_id\":\"C###\",\"threshold\":\"<0,30\"}]'`")


def _need(entity: str, rid: str) -> Dict[str, Any]:
    rec = core.get(entity, rid)
    if not rec:
        raise core.BulochkaError(f"{rid} не найден.")
    return rec


def options_of(did: str) -> List[Dict[str, Any]]:
    return sorted((o for o in core.latest("option").values() if o.get("decision_id") == did), key=lambda o: o["id"])


def option_claim_ids(o: Dict[str, Any], claims: Optional[Dict[str, Dict[str, Any]]] = None) -> List[str]:
    ids: List[str] = []
    for v in (o.get("criteria_scores") or {}).values():
        if isinstance(v, dict):
            ids.extend(v.get("claim_ids") or [])
    ids.extend((o.get("money_line") or {}).get("claim_ids") or [])
    for c in (claims if claims is not None else core.latest("claim")).values():
        if o["id"] in (c.get("decision_link") or []):
            ids.append(c["id"])
    out: List[str] = []
    for i in ids:
        if i not in out:
            out.append(i)
    return out


def decision_claim_ids(did: Optional[str], claims: Dict[str, Dict[str, Any]]) -> List[str]:
    """Утверждения, привязанные к самому решению (decision_link = D###)."""
    return sorted(c["id"] for c in claims.values() if did and did in (c.get("decision_link") or [])
                  and c.get("status") == "active")


def evidence_debt(o: Dict[str, Any], claims: Optional[Dict[str, Dict[str, Any]]] = None) -> List[str]:
    """Material-утверждения варианта на допущении/прогнозе или с непроверенными числами. У варианта без своих
    ссылок (агент привязал всё к решению) долг считается по утверждениям решения — иначе «долг 0» на допущениях."""
    claims = claims if claims is not None else core.latest("claim")
    debt = []
    own = option_claim_ids(o, claims)
    for cid in own or decision_claim_ids(o.get("decision_id"), claims):
        c = claims.get(cid)
        if not c or c.get("status") != "active" or not c.get("material", True):
            continue
        if c.get("type") in DEBT_TYPES or c.get("number_on_page") in ("fail", "needs_manual"):
            debt.append(cid)
    return debt


def total_evidence_debt(slug: Optional[str] = None) -> int:
    """Долг доказательств проекта: сумма evidence_debt по открытым/выбранным/зафиксированным вариантам.
    Одна функция для `status`, statusline и расширений."""
    claims = core.latest("claim", slug)
    return sum(len(evidence_debt(o, claims)) for o in core.latest("option", slug).values()
               if o.get("status") in DEBT_STATUSES)


def run_pre_choose(d: Dict[str, Any], o: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Первый отказ валидаторов PRE_CHOOSE или None. Сбой валидатора — в stderr, не блок."""
    for fn in ext.active(PRE_CHOOSE):  # ext.load(): модули x_*.py регистрируют валидаторы при импорте
        try:
            res = fn(d, o)
        except Exception as e:  # noqa: BLE001
            ext.warn(f"валидатор {getattr(fn, '__name__', fn)} упал: {type(e).__name__}: {e}")
            continue
        if res:
            return res
    return None


def _pre_choose_block(cmd: str, d: Dict[str, Any], o: Optional[Dict[str, Any]], as_json: bool) -> Optional[int]:
    ref = run_pre_choose(d, o)
    if not ref:
        return None
    if not isinstance(ref, dict):
        ref = {"tag": "HUMAN_DECISION", "target_id": d["id"], "reason": "pre_choose", "phrase": str(ref)}
    payload = refusals.as_payload(ref)
    return core.emit(cmd, {"id": d["id"]}, refusal=payload, next_cmd=payload.get("next_action"), as_json=as_json,
                     code=core.EXIT_REFUSAL)


def decision_min_grade(d: Dict[str, Any]) -> Optional[str]:
    claims = core.latest("claim")
    return min_grade([(claims.get(p.get("claim_id")) or {}).get("grade") for p in d.get("premises") or []])


def _premises(ids: List[str], d: Dict[str, Any]) -> (List[Dict[str, Any]], List[str]):
    """premises + список отвергнутых синтетических оснований для решения о спросе."""
    out, rejected = [], []
    demand = is_demand_decision(d)
    for cid in ids:
        c = _need("claim", cid)
        if c.get("type") == "synthetic" and demand:
            rejected.append(cid)
            continue
        out.append({"claim_id": cid, "kind": "fact" if c.get("type") == "fact" else "assumption"})
    return out, rejected


def _synthetic_refusal(did: str, rejected: List[str]) -> Dict[str, Any]:
    cost = refusals.DEFAULT_COST["SYNTHETIC_AS_FACT"]
    text = refusals.phrase(f"Оставил {', '.join(rejected)} как [Синтетика]", f"В основание {did} не ставлю",
                           "SYNTHETIC_AS_FACT", "ответы ИИ-респондентов спрос не доказывают",
                           "Меняется: готовность платить остаётся гипотезой", "8–10 реальных интервью по гайду", cost)
    return refusals.record("SYNTHETIC_AS_FACT", target_id=rejected[0], reason="synthetic_in_premises",
                           phrase_text=text, next_action="bulochka claim add --type assumption --falsifier …",
                           cost=cost, extra={"premise_of": did})


# ---------- option ----------

def _apply_option_fields(rec: Dict[str, Any], args) -> None:
    if getattr(args, "distinct_by", None):
        rec["distinct_by"] = args.distinct_by
    if getattr(args, "bet", None):
        rec["bet"] = args.bet
    if getattr(args, "reversibility", None):
        rec["reversibility"] = args.reversibility
    if getattr(args, "flip_condition", None):
        rec["flip_condition"] = args.flip_condition
    if getattr(args, "money_line", None):
        rec["money_line"] = _json_arg(args.money_line, "--money-line")
    if getattr(args, "scores", None):
        scores = _json_arg(args.scores, "--scores")
        if not isinstance(scores, dict):
            raise core.BulochkaError("--scores: объект {критерий: {score, tag, claim_ids}}.")
        merged = dict(rec.get("criteria_scores") or {})
        merged.update(scores)
        rec["criteria_scores"] = merged
    if getattr(args, "leader", False):
        rec["leader"] = True
    if getattr(args, "cost_estimate", None):
        rec["cost_estimate"] = _json_arg(args.cost_estimate, "--cost-estimate")


def cmd_option_add(args) -> int:
    d = _need("decision", args.decision)
    if d.get("status") in ("locked", "superseded", "invalidated"):
        raise core.BulochkaError(f"{d['id']} в статусе {d['status']} — варианты не добавляются.")
    if args.kind == "change" and not args.distinct_by:
        raise core.BulochkaError("Для kind=change нужен --distinct-by audience|revenue_model|channel|bet.")
    rec: Dict[str, Any] = {"decision_id": d["id"], "title": args.title, "kind": args.kind, "distinct_by": None,
                           "criteria_scores": {}, "status": "open", "money_line": None, "reversibility": "two_way",
                           "flip_condition": None, "killed_by": [], "revive_if": [], "cost_estimate": None,
                           "actor": "agent"}
    _apply_option_fields(rec, args)
    rec = core.put("option", rec)
    n = len(options_of(d["id"]))
    return core.emit("option add", {"id": rec["id"], "decision_id": d["id"], "options_total": n},
                     human=f"Вариант {rec['id']} «{args.title}» ({args.kind}) к {d['id']}. Всего вариантов: {n}.",
                     next_cmd=f"bulochka objection add --target {rec['id']} --text \"…\" --severity …", as_json=args.json)


def cmd_option_set(args) -> int:
    o = _need("option", args.id)
    if o.get("status") == "locked":
        return core.emit("option set", {"id": o["id"]}, ok=False, code=core.EXIT_REFUSAL, as_json=args.json,
                         errors=[{"rule": "E19", "level": "error", "ids": [o["id"]],
                                  "msg": "вариант зафиксирован (locked) — правка только через новое решение"}])
    upd = dict(o)
    _apply_option_fields(upd, args)
    core.put("option", upd)
    return core.emit("option set", {"id": o["id"]}, human=f"{o['id']} обновлён.", as_json=args.json)


def cmd_option_reject(args) -> int:
    o = _need("option", args.id)
    killed = split_ids(args.killed_by)
    if not killed:
        raise core.BulochkaError("--killed-by: C### / R###, из-за которых вариант отклонён.")
    for k in killed:
        if not core.resolve_ref(k):
            raise core.BulochkaError(f"--killed-by: {k} не найден.")
    revive = _json_arg(args.revive_if, "--revive-if", as_list=True) or []
    upd = dict(o)
    upd.update({"status": "rejected", "killed_by": killed, "revive_if": revive})
    core.put("option", upd)
    errs = [] if revive else [{"rule": "W04", "level": "warning", "ids": [o["id"]],
                               "msg": "нет --revive-if: при каких условиях вариант вернуть"}]
    errs += threshold_warnings(revive, o["id"], "revive_if (условие возврата)")
    return core.emit("option reject", {"id": o["id"]}, errors=errs, human=f"{o['id']} отклонён из-за {', '.join(killed)}.",
                     as_json=args.json)


def met_conditions(o: Dict[str, Any]) -> List[Dict[str, Any]]:
    claims = core.latest("claim")
    met = []
    for cond in o.get("revive_if") or []:
        if not isinstance(cond, dict):
            continue
        aid = cond.get("assumption_id")
        if cond.get("met") or (aid and (claims.get(aid) or {}).get("status") in ("superseded", "invalidated")):
            met.append(cond)
    return met


def cmd_option_revive(args) -> int:
    o = _need("option", args.id)
    if o.get("status") != "rejected":
        raise core.BulochkaError(f"{o['id']} не отклонён (статус {o.get('status')}).")
    gate_id = getattr(args, "gate", None)
    gate = core.get("gate", gate_id) if gate_id else None
    reason, detail = None, ""
    if not gate_id:
        reason, detail = "no_gate", "вернуть вариант решает человек, вопрос ещё не задан"
    elif not gate:
        reason, detail = "gate_not_found", f"ворот {gate_id} нет в реестре"
    elif gate.get("kind") not in ("choose_option", "question", "approve_decision"):
        reason, detail = "gate_kind", f"{gate_id} — вопрос вида {gate.get('kind')}, а вернуть вариант решает выбор или пересмотр"
    elif not gate.get("answer") or gate.get("answered_by") != "human":
        reason, detail = "gate_unanswered", f"на воротах {gate_id} ещё нет ответа человека"
    else:
        ans = str(gate.get("answer")).lower()
        asked = o["id"].lower() in (str(gate.get("question")) + " " + " ".join(map(str, gate.get("options") or []))).lower()
        named = o["id"].lower() in ans or (o.get("title") or "#").lower() in ans
        yes = ans.strip().startswith(("да", "верн", "давай", "соглас")) and gate.get("kind") != "approve_decision"
        named = named or (gate.get("kind") == "approve_decision" and "верн" in ans and asked
                          and sum(1 for x in gate.get("options") or [] if "O" in str(x)) == 1)
        # «нет» в начале ответа или прямой отказ; «цифр пока нет, но O001 верни» — не отказ (прогон 22.11)
        refused = re.match(r"\s*нет\b", ans) or re.search(r"не\s+возвращ|не\s+верн|оставить\s+отклон|не\s+надо", ans)
        if not (named or (asked and yes)) or refused:
            reason, detail = "answer_mismatch", f"в ответе на {gate_id} нет «вернуть {o['id']}»"
    if reason:
        cost = refusals.DEFAULT_COST["HUMAN_DECISION"]
        text = refusals.phrase("", f"{o['id']} на стол не возвращаю", "HUMAN_DECISION", detail,
                               f"Меняется: {o['id']} остаётся отклонённым", "вопрос человеку на воротах", cost)
        ref = refusals.record("HUMAN_DECISION", target_id=o["id"], reason=reason, phrase_text=text,
                              next_action=f"bulochka ask --kind choose_option --question \"Вернуть {o['id']} в рассмотрение?\" "
                                          f"--options \"Вернуть {o['id']}\" \"Оставить отклонённым\"", cost=cost)
        return core.emit("option revive", {"id": o["id"]}, refusal=refusals.as_payload(ref),
                         next_cmd=ref["next_action"], as_json=args.json)
    met = met_conditions(o)
    upd = dict(o)
    upd.update({"status": "open", "revived_at": core.now_iso(), "revived_gate_id": gate_id})
    core.put("option", upd)
    return core.emit("option revive", {"id": o["id"], "met_conditions": met, "gate": gate_id},
                     human=f"{o['id']} снова в рассмотрении по ответу человека на {gate_id}. Выполнено условий: {len(met)}.",
                     as_json=args.json)


def cmd_option_choose(args) -> int:
    o = _need("option", args.id)
    args.option = o["id"]
    args.id = o.get("decision_id")
    return _choose(args, "option choose")


def cmd_option_list(args) -> int:
    items = sorted(core.latest("option").values(), key=lambda o: o["id"])
    if args.decision:
        items = [o for o in items if o.get("decision_id") == args.decision]
    claims = core.latest("claim")
    for o in items:
        o["evidence_debt"] = len(evidence_debt(o, claims))
    lines = [f"{o['id']} [{o.get('kind')}/{o.get('status')}] {o.get('title')} · долг {o['evidence_debt']}" for o in items]
    return core.emit("option list", {"items": items}, human="\n".join(lines) or "Вариантов нет.", as_json=args.json)


# ---------- decision ----------

def _apply_decision_fields(rec: Dict[str, Any], args) -> List[str]:
    """Возвращает отвергнутые синтетические основания."""
    rejected: List[str] = []
    for f in ("owner", "metric", "first_read", "topic", "evidence_certainty", "recommendation_strength",
              "why_divergent", "explain_by", "caveat", "validation_plan", "vs_previous", "question"):
        v = getattr(args, f, None)
        if v is not None:
            rec[f] = v
    if getattr(args, "premises", None):
        prem, rejected = _premises(split_ids(args.premises), rec)
        have = {p["claim_id"] for p in rec.get("premises") or []}
        rec["premises"] = (rec.get("premises") or []) + [p for p in prem if p["claim_id"] not in have]
    if getattr(args, "revise_if", None):
        rec["revise_if"] = revise_if_arg(args.revise_if)
    if getattr(args, "forecast", None):
        rec["forecast"] = _json_arg(args.forecast, "--forecast")
    if getattr(args, "because", None):
        rec["because"] = split_ids(args.because)
    rec["min_grade"] = decision_min_grade(rec)
    return rejected


def cmd_decision_add(args) -> int:
    if args.first_read and not parse_date(args.first_read):
        raise core.BulochkaError("--first-read: дата YYYY-MM-DD")
    rec: Dict[str, Any] = {"question": args.question, "status": "draft", "chosen_option": None, "chosen_by": None,
                           "human_gate_id": None, "owner": args.owner, "premises": [], "min_grade": None,
                           "metric": args.metric, "first_read": args.first_read, "forecast": None, "revise_if": [],
                           "vs_previous": "initial", "because": [], "supersedes": None,
                           "evidence_certainty": None, "recommendation_strength": None, "why_divergent": None,
                           "explain_by": None, "locked_at": None, "actor": "agent"}
    rec["id"] = core.next_id("decision")
    old = None
    proposed = bool(getattr(args, "proposed", False))
    if proposed and not args.supersedes:
        raise core.BulochkaError("--proposed только вместе с --supersedes D### (пересмотр существующего решения).")
    if args.supersedes:
        old = _need("decision", args.supersedes)
        if old.get("status") in ("superseded", "invalidated"):
            raise core.BulochkaError(f"{old['id']} уже в статусе {old['status']} — заменять нечего.")
        rec["supersedes"] = old["id"]
        rec["vs_previous"] = "changed" if args.because else "consistent"
        if proposed:
            rec["status"] = REVISION_PROPOSED
    rejected = _apply_decision_fields(rec, args)
    if rec.get("vs_previous") == "changed" and not rec.get("because"):
        raise core.BulochkaError("--vs-previous changed требует --because C###/R###.")
    core.put("decision", rec)
    if old and not proposed:
        ou = dict(old)
        ou.update({"status": "superseded", "superseded_by": rec["id"]})
        core.put("decision", ou)
    refusal = _synthetic_refusal(rec["id"], rejected) if rejected else None
    human = "\n".join([f"Решение {rec['id']}: {args.question}"] + revise_if_read(rec.get("revise_if"), rec["id"]))
    if proposed:
        human = (f"Предложен пересмотр {old['id']}: черновик-преемник {rec['id']} ({REVISION_PROPOSED}). "
                 f"{old['id']} остаётся в силе, пока человек не выберет вариант {rec['id']}.")
    return core.emit("decision add", {"id": rec["id"], "status": rec["status"]},
                     errors=threshold_warnings(rec.get("revise_if"), rec["id"], "revise_if (граница, где решение держится)"),
                     refusal=refusals.as_payload(refusal) if refusal else None,
                     human=None if refusal else human,
                     next_cmd=f"bulochka option add --decision {rec['id']} --title \"Оставить как есть\" --kind keep",
                     as_json=args.json)


def _locked_block(d: Dict[str, Any], cmd: str, as_json: bool) -> int:
    return core.emit(cmd, {"id": d["id"]}, ok=False, code=core.EXIT_REFUSAL, as_json=as_json,
                     errors=[{"rule": "E19", "level": "error", "ids": [d["id"]],
                              "msg": f"{d['id']} зафиксировано (locked): правка только через "
                                     f"`bulochka decision add --supersedes {d['id']}`"}])


def cmd_decision_set(args) -> int:
    d = _need("decision", args.id)
    if d.get("status") == "locked":
        return _locked_block(d, "decision set", args.json)
    upd = dict(d)
    rejected = _apply_decision_fields(upd, args)
    core.put("decision", upd)
    refusal = _synthetic_refusal(d["id"], rejected) if rejected else None
    warns = (threshold_warnings(upd.get("revise_if"), d["id"], "revise_if (граница, где решение держится)")
             + shared_claim_warnings(upd.get("revise_if"), d["id"])
             if getattr(args, "revise_if", None) else [])
    return core.emit("decision set", {"id": d["id"], "min_grade": upd.get("min_grade")}, errors=warns,
                     refusal=refusals.as_payload(refusal) if refusal else None,
                     human=None if refusal else "\n".join(
                         [f"{d['id']} обновлено."]
                         + (revise_if_read(upd.get("revise_if"), d["id"]) if getattr(args, "revise_if", None) else [])),
                     as_json=args.json)


def _choose(args, cmd: str) -> int:
    d = _need("decision", args.id)
    o = _need("option", args.option)
    if o.get("decision_id") != d["id"]:
        raise core.BulochkaError(f"{o['id']} не относится к {d['id']}.")
    if d.get("status") == "locked":
        return _locked_block(d, cmd, args.json)
    if d.get("status") in ("superseded", "invalidated"):
        raise core.BulochkaError(f"{d['id']} в статусе {d['status']}.")
    gate_id = getattr(args, "gate", None)
    gate = core.get("gate", gate_id) if gate_id else None
    reason, detail = None, ""
    if getattr(args, "actor", None) == "agent":
        reason, detail = "actor_agent", "решение за вами"
    elif not gate_id:
        reason, detail = "no_gate", "решение за вами, вопрос на воротах ещё не задан"
    elif not gate:
        reason, detail = "gate_not_found", f"ворот {gate_id} нет в реестре"
    elif not gate.get("answer") or gate.get("answered_by") != "human":
        reason, detail = "gate_unanswered", f"на воротах {gate_id} ещё нет ответа человека"
    else:
        ans = str(gate.get("answer")).lower()
        if o["id"].lower() not in ans and (o.get("title") or "").lower() not in ans:
            reason, detail = "answer_mismatch", f"в ответе на {gate_id} выбран не {o['id']}"
    if reason:
        cost = refusals.DEFAULT_COST["HUMAN_DECISION"]
        next_h = (f"ворота {gate_id}" if gate and reason != "answer_mismatch"
                  else "вопрос через `bulochka ask --kind choose_option`")
        text = refusals.phrase("", f"Рекомендую {o['id']}, но выбранным его не ставлю", "HUMAN_DECISION", detail,
                               f"Меняется: {d['id']} остаётся черновиком", next_h, cost)
        ref = refusals.record("HUMAN_DECISION", target_id=d["id"], reason=reason, phrase_text=text,
                              next_action=f"bulochka ask --kind choose_option --question \"{d.get('question')}\" "
                                          f"--options …", cost=cost, extra={"option_id": o["id"]})
        return core.emit(cmd, {"id": d["id"]}, refusal=refusals.as_payload(ref), next_cmd=ref["next_action"],
                         as_json=args.json)
    blocked = _pre_choose_block(cmd, d, o, args.json)
    if blocked is not None:
        return blocked
    upd = dict(d)
    if getattr(args, "revise_if", None):
        upd["revise_if"] = revise_if_arg(args.revise_if)
    if getattr(args, "forecast", None):
        upd["forecast"] = _json_arg(args.forecast, "--forecast")
    d = upd
    was_proposed = d.get("status") == REVISION_PROPOSED
    upd.update({"status": "chosen", "chosen_option": o["id"], "chosen_by": "human", "human_gate_id": gate_id,
                "actor": "human", "min_grade": decision_min_grade(d), "chosen_at": core.now_iso()})
    core.put("decision", upd)
    replaced = None
    if was_proposed and d.get("supersedes"):
        old = core.get("decision", d["supersedes"])
        if old and old.get("status") not in ("superseded", "invalidated"):
            ou = dict(old)
            ou.update({"status": "superseded", "superseded_by": d["id"]})
            core.put("decision", ou)
            replaced = old["id"]
    ou = dict(o)
    ou["status"] = "chosen"
    core.put("option", ou)
    errs = []
    if not d.get("revise_if"):
        errs.append({"rule": "E20", "level": "warning", "ids": [d["id"]],
                     "msg": "у выбранного решения нет revise_if — когда пересматривать"})
    errs += threshold_warnings(d.get("revise_if"), d["id"], "revise_if (граница, где решение держится)")
    errs += shared_claim_warnings(d.get("revise_if"), d["id"])
    alts = open_alternatives(upd)
    if alts:
        errs.append({"rule": "W14", "level": "warning", "ids": [d["id"]] + alts,
                     "msg": f"остальные варианты ({', '.join(alts)}) ещё открыты — отклоните каждый с причиной и "
                            f"условием возврата: {REJECT_HINT}. Без этого {d['id']} не зафиксировать, а после "
                            f"решения нечего будет вернуть на стол"})
    human = (f"{d['id']}: выбран {o['id']} по ответу человека на {gate_id}."
             + (f" {replaced} заменено на {d['id']}." if replaced else ""))
    human = "\n".join([human] + revise_if_read(d.get("revise_if"), d["id"]))
    nxt = (f"bulochka option reject {alts[0]} --killed-by … --revive-if …" if alts
           else f"bulochka decision lock {d['id']}")
    return core.emit(cmd, {"id": d["id"], "chosen_option": o["id"], "gate": gate_id, "replaced": replaced,
                           "open_alternatives": alts},
                     errors=errs, human=human, next_cmd=nxt, as_json=args.json)


def cmd_decision_choose(args) -> int:
    return _choose(args, "decision choose")


_STATUS_KEY = re.compile(r"(?i)^(?P<pre>[\s>*_\-]*статус(?:\s+решения)?\s*(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*)")
_STATUS_END = re.compile(r"\s*(?:\*\*|__|\s\|\s?|\s·\s)")


def restatus(text: str, value: str) -> str:
    """Заменить только значение статуса в первой строке «Статус: …» шапки, сохранив остальное.

    Форматы: «Статус: предложено | Тип двери: … | Владелец: …» (поля через «|» или «·»), блок из отдельных строк
    «**Статус:** предложено», «**Статус: предложено** · …». Жирное и соседние поля не трогаются (прогон 22.11:
    замена «до конца строки» оставляла незакрытое «**» и съедала «Тип двери» и «Владелец»).
    Уже утверждённый статус не переписывается.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        m = _STATUS_KEY.match(line)
        if not m:
            continue
        head, rest = line[:m.end()], line[m.end():]
        e = _STATUS_END.search(rest)
        old, tail = (rest[:e.start()], rest[e.start():]) if e else (rest.rstrip(), rest[len(rest.rstrip()):])
        if old.strip().lower().startswith("утвержд"):
            return text
        lines[i] = head + value + tail
        return "\n".join(lines)
    return text


def cmd_decision_lock(args) -> int:
    d = _need("decision", args.id)
    if d.get("status") != "chosen":
        raise core.BulochkaError(f"Зафиксировать можно только выбранное решение ({d['id']}: {d.get('status')}).")
    # Ворота 3: зафиксировать можно только по ответу человека «утвердить» на approve_decision.
    gate_id = getattr(args, "gate", None)
    gate = core.get("gate", gate_id) if gate_id else None
    reason, detail = None, ""
    if not gate_id:
        reason, detail = "no_gate", "утверждение за вами, вопрос approve_decision ещё не задан"
    elif not gate or gate.get("kind") != "approve_decision":
        reason, detail = "gate_not_found", f"ворот approve_decision {gate_id} нет в реестре"
    elif not gate.get("answer") or gate.get("answered_by") != "human":
        reason, detail = "gate_unanswered", f"на воротах {gate_id} ещё нет ответа человека"
    else:
        ans = str(gate.get("answer")).lower()
        if not re.search(r"утвер[дж]", ans) or re.search(r"не\s+утвер[дж]|вернуть|отклон", ans):  # «утвердить», «утверждаю», «утверждаем»
            reason, detail = "answer_mismatch", f"в ответе на {gate_id} нет «утвердить»"
    if reason:
        cost = refusals.DEFAULT_COST["HUMAN_DECISION"]
        text = refusals.phrase("", f"{d['id']} не фиксирую", "HUMAN_DECISION", detail,
                               f"Меняется: {d['id']} остаётся выбранным, но не утверждённым",
                               "ворота approve_decision", cost)
        ref = refusals.record("HUMAN_DECISION", target_id=d["id"], reason=reason, phrase_text=text,
                              next_action="bulochka ask --kind approve_decision --question … --options …", cost=cost)
        return core.emit("decision lock", {"id": d["id"]}, refusal=refusals.as_payload(ref),
                         next_cmd=ref["next_action"], as_json=args.json)
    chosen = core.get("option", d["chosen_option"]) if d.get("chosen_option") else None
    blocked = _pre_choose_block("decision lock", d, chosen, args.json)
    if blocked is not None:
        return blocked
    alts = open_alternatives(d)
    if alts:
        return core.emit("decision lock", {"id": d["id"], "open_alternatives": alts}, ok=False,
                         code=core.EXIT_GATE_ERRORS, as_json=args.json,
                         errors=[{"rule": "W14", "level": "error", "ids": [d["id"]] + alts,
                                  "msg": f"{d['id']} не фиксирую: варианты {', '.join(alts)} ещё открыты. Отклоните "
                                         f"каждый с причиной и условием возврата — {REJECT_HINT}, затем повторите "
                                         f"lock с теми же воротами (ответ человека уже есть)"}],
                         next_cmd=f"bulochka option reject {alts[0]} --killed-by … --revive-if …")
    ts = core.now_iso()
    upd = dict(d)
    upd.update({"status": "locked", "locked_at": ts, "locked_gate_id": gate_id})
    core.put("decision", upd)
    if d.get("chosen_option"):
        o = core.get("option", d["chosen_option"])
        if o:
            ou = dict(o)
            ou["status"] = "locked"
            core.put("option", ou)
    adr = core.project_dir() / "report" / "70_approval.md"
    if adr.exists():  # после lock агент ADR не правит (хук) — строку статуса дописывает сам CLI
        text = adr.read_text(encoding="utf-8").replace("ждёт утверждения", "утверждено человеком")
        # строка статуса в шапке («Статус: предложено (ждёт ворот 3)») — тоже, иначе шапка спорит с припиской
        text = restatus(text, f"утверждено человеком ({gate_id}, {ts[:10]})")
        core.atomic_write(adr, text.rstrip("\n") + f"\n\n> **Утверждено человеком:** {d['id']} зафиксировано "
                                                     f"{ts[:10]} по ответу на воротах {gate_id}.\n")
    return core.emit("decision lock", {"id": d["id"], "locked_at": ts}, human=f"{d['id']} зафиксировано.",
                     as_json=args.json)


def due_items(today_s: Optional[str] = None) -> List[Dict[str, Any]]:
    t = today_date(today_s)
    out = []
    for d in sorted(core.latest("decision").values(), key=lambda x: x["id"]):
        if d.get("status") not in ("chosen", "locked"):
            continue
        for cond in d.get("revise_if") or []:
            cb = parse_date((cond or {}).get("check_by"))
            if cb and cb <= t:
                item = {"id": d["id"], "indicator": cond.get("indicator"), "check_by": cond.get("check_by"),
                        "threshold": cond.get("threshold")}
                try:  # граница, при которой решение держится: (оператор, значение)
                    item["holds_if"] = list(core.parse_threshold(cond.get("threshold"))) if cond.get("threshold") else None
                except core.BulochkaError:
                    item["holds_if"] = None
                out.append(item)
        fr = parse_date(d.get("first_read"))
        if fr and fr <= t:
            out.append({"id": d["id"], "indicator": d.get("metric"), "check_by": d.get("first_read"),
                        "threshold": "первое чтение метрики"})
    return out


def cmd_decision_due(args) -> int:
    due = due_items(args.today)
    lines = [f"{x['id']}: {x['indicator']} — проверить до {x['check_by']}" for x in due]
    return core.emit("decision due", {"due": due}, human="\n".join(lines) or "Сроков пересмотра сейчас нет.",
                     as_json=args.json)


DIFF_FIELDS = ("question", "chosen_option", "premises", "metric", "first_read", "forecast", "revise_if",
               "evidence_certainty", "recommendation_strength", "owner", "status")


def cmd_decision_diff(args) -> int:
    d = _need("decision", args.id)
    vs_id = args.vs or d.get("supersedes")
    if not vs_id:
        raise core.BulochkaError(f"{d['id']} ничего не заменяет: укажите --vs D###.")
    v = _need("decision", vs_id)
    changed = [f for f in DIFF_FIELDS if f != "status" and core.canonical_json(d.get(f)) != core.canonical_json(v.get(f))]
    lines = [f"# Что изменилось: {d['id']} против {v['id']}", ""]
    for f in changed:
        lines.append(f"- **{f}**: `{json.dumps(v.get(f), ensure_ascii=False)}` → `{json.dumps(d.get(f), ensure_ascii=False)}`")
    if not changed:
        lines.append("- изменений нет")
    lines += ["", f"Почему: {', '.join(d.get('because') or []) or 'не указано'}", ""]
    path = core.project_dir() / "report" / "decision-diff.md"
    core.atomic_write(path, "\n".join(lines))
    return core.emit("decision diff", {"changed_fields": changed, "because": d.get("because") or [],
                                       "path": str(path)}, human="\n".join(lines), as_json=args.json)


def cmd_decision_list(args) -> int:
    items = sorted(core.latest("decision").values(), key=lambda x: x["id"])
    lines = [f"{d['id']} [{d.get('status')}] {d.get('question')} · вариантов {len(options_of(d['id']))}" for d in items]
    return core.emit("decision list", {"items": items}, human="\n".join(lines) or "Решений нет.", as_json=args.json)


# ---------- регистрация ----------

def _option_fields(p) -> None:
    p.add_argument("--distinct-by", dest="distinct_by", choices=DISTINCT)
    p.add_argument("--bet", help="ставка варианта (чем он отличается)")
    p.add_argument("--reversibility", choices=REVERSIBILITY)
    p.add_argument("--flip-condition", dest="flip_condition")
    p.add_argument("--money-line", dest="money_line", help='JSON {"amount_range":[min,max],"unit":"RUB",...}')
    p.add_argument("--scores", help='JSON {критерий: {"score":3,"tag":"Вывод","claim_ids":["C001"]}}')
    p.add_argument("--leader", action="store_true", help="вариант-лидер (рекомендация)")
    p.add_argument("--cost-estimate", dest="cost_estimate", help='JSON {"tokens":…, "minutes_human":…}')


def _decision_fields(p) -> None:
    p.add_argument("--premises", help="C### (через запятую)")
    p.add_argument("--topic", choices=("demand", "market", "wtp", "other"))
    p.add_argument("--revise-if", dest="revise_if", help='JSON [{"indicator","revise_when","source","check_by",'
                                                          '"assumption_id"}]: revise_when — когда пересматривать '
                                                          '(«≥ 50%%»); CLI сам запишет threshold — где решение держится')
    p.add_argument("--forecast", help='JSON {"metric","p_range","horizon"}')
    p.add_argument("--evidence-certainty", dest="evidence_certainty", choices=CERTAINTY)
    p.add_argument("--recommendation-strength", dest="recommendation_strength", choices=STRENGTH)
    p.add_argument("--why-divergent", dest="why_divergent")
    p.add_argument("--explain-by", dest="explain_by")
    p.add_argument("--caveat", help="оговорка о слабых основаниях (E12)")
    p.add_argument("--validation-plan", dest="validation_plan", help="путь или текст плана проверки (E12)")
    p.add_argument("--because", help="C### / R### (через запятую)")
    p.add_argument("--vs-previous", dest="vs_previous", choices=("initial", "consistent", "changed"))


def _choose_fields(p) -> None:
    p.add_argument("--revise-if", dest="revise_if", help='JSON [{"indicator","revise_when","source","check_by",'
                                                          '"assumption_id"}]: revise_when — когда пересматривать '
                                                          '(«≥ 50%%»); CLI сам запишет threshold — где решение держится')
    p.add_argument("--forecast", help='JSON {"metric","p_range","horizon"}')


def register(sub) -> None:
    p = sub.add_parser("option", help="варианты: add, set, reject, revive, choose, list")
    ss = p.add_subparsers(dest="option_cmd")
    a = ss.add_parser("add")
    a.add_argument("--decision", required=True)
    a.add_argument("--title", required=True)
    a.add_argument("--kind", required=True, choices=KINDS)
    _option_fields(a)
    a.set_defaults(func=cmd_option_add)
    s = ss.add_parser("set")
    s.add_argument("id")
    _option_fields(s)
    s.set_defaults(func=cmd_option_set)
    r = ss.add_parser("reject")
    r.add_argument("id")
    r.add_argument("--killed-by", dest="killed_by", required=True)
    r.add_argument("--revive-if", dest="revive_if")
    r.set_defaults(func=cmd_option_reject)
    v = ss.add_parser("revive")
    v.add_argument("id")
    v.add_argument("--gate", help="G### — ответ человека «вернуть O###» (ворота choose_option)")
    v.set_defaults(func=cmd_option_revive)
    c = ss.add_parser("choose")
    c.add_argument("id")
    c.add_argument("--gate")
    c.add_argument("--actor", choices=("agent", "human"))
    _choose_fields(c)
    c.set_defaults(func=cmd_option_choose)
    lst = ss.add_parser("list")
    lst.add_argument("--decision")
    lst.set_defaults(func=cmd_option_list)

    p = sub.add_parser("decision", help="решения: add, set, choose, lock, due, diff, list")
    ss = p.add_subparsers(dest="decision_cmd")
    a = ss.add_parser("add")
    a.add_argument("--question", required=True)
    a.add_argument("--owner", required=True, help="роль, без ФИО")
    a.add_argument("--metric", required=True)
    a.add_argument("--first-read", dest="first_read", required=True, help="YYYY-MM-DD")
    a.add_argument("--supersedes")
    a.add_argument("--proposed", action="store_true",
                   help="с --supersedes: черновик-преемник (revision_proposed); старое решение в силе до выбора")
    _decision_fields(a)
    a.set_defaults(func=cmd_decision_add)
    s = ss.add_parser("set")
    s.add_argument("id")
    s.add_argument("--question")
    s.add_argument("--owner")
    s.add_argument("--metric")
    s.add_argument("--first-read", dest="first_read")
    _decision_fields(s)
    s.set_defaults(func=cmd_decision_set)
    c = ss.add_parser("choose")
    c.add_argument("id")
    c.add_argument("--option", required=True)
    c.add_argument("--gate")
    c.add_argument("--actor", choices=("agent", "human"))
    _choose_fields(c)
    c.set_defaults(func=cmd_decision_choose)
    lk = ss.add_parser("lock")
    lk.add_argument("id")
    lk.add_argument("--gate", help="G### ворот approve_decision с ответом человека «утвердить»")
    lk.set_defaults(func=cmd_decision_lock)
    du = ss.add_parser("due")
    du.add_argument("--today", help="YYYY-MM-DD (по умолчанию сегодня)")
    du.set_defaults(func=cmd_decision_due)
    df = ss.add_parser("diff")
    df.add_argument("id")
    df.add_argument("--vs")
    df.set_defaults(func=cmd_decision_diff)
    lst = ss.add_parser("list")
    lst.set_defaults(func=cmd_decision_list)
