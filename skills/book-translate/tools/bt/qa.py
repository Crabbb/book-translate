"""Deterministic checks on accepted text, evidence bound to text hashes, and the chapter gate.

Classes: B blocks, C needs a decision per occurrence, A is a hint. Code finds candidates; it never
claims the meaning is right. Numbers, negation and modality are candidates only.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

import yaml

from . import batch as batch_mod
from . import bible as bible_mod
from . import revision, store
from .batch import plain
from .store import Workspace, digest, read_jsonl, write_atomic, write_jsonl

TOOL_REV = "qa-3"
PROMPT_BREAK = "¶"
SKILL_DIR = Path(__file__).resolve().parents[2]
NUMBER = re.compile(r"(?<![\w.,])\d[\d\s.,]*\d|(?<![\w.,])\d")
IDENT = re.compile(r"\b(?:[a-z]+_[a-z0-9_]+|[a-z]+[A-Z]\w+|[A-Z][a-z]+[A-Z]\w+|--[a-z][\w-]+)\b")
URL = re.compile(r"https?://\S+")
MIXED = re.compile(r"\b\w*(?:[A-Za-z][А-Яа-яЁё]|[А-Яа-яЁё][A-Za-z])\w*\b")

# Negation is compared in both directions, each with its own wider net: a word starting with "не"
# is usually not a negation ("нейронный", "несколько"), while English often negates by a prefix.
NEG_EN = re.compile(r"\b(?:not|no|never|none|neither|nor|cannot)\b|n['\u2019]t\b", re.IGNORECASE)
NEG_EN_WIDE = re.compile(
    r"\b(?:not|no|never|none|without|neither|nor|cannot|lack\w*|unlike|limited|despite|instead|"
    r"rather|outside|beyond|prevent\w*|avoid\w*|"
    r"un[a-z]{4,}|in(?:efficient|effective)|reduced|fewer|less)\b|n['\u2019]t\b",
    re.IGNORECASE,
)
NEG_RU_STRICT = re.compile(r"\b(?:не|нет|ни|никогда|нельзя|невозможно)\b", re.IGNORECASE)
NEG_RU_WIDE = re.compile(
    r"\b(?:не|нет|ни|никогда|без|нельзя|невозможно|отсутств\w+|вместо|ограничен\w*|меньш\w+|"
    r"не[а-яё]*(?:практичн|возможн|достат|полн|эффектив|удоб|стабиль|последовательн|постоянн|"
    r"ясн|нужн|верн|точн|извест|доступ)\w*)\b",
    re.IGNORECASE,
)
MODAL = {
    "oblig": (
        re.compile(r"\b(?:must|need to|needs to|required|have to|has to)\b", re.IGNORECASE),
        re.compile(
            r"\b(?:долж\w+|обязан\w*|необходим\w*|нужно|нужн\w+|требу\w+|надо|приходит\w+)\b",
            re.IGNORECASE,
        ),
    ),
    "recom": (
        re.compile(r"\b(?:should|recommended|ought to)\b", re.IGNORECASE),
        re.compile(r"\b(?:следует|стоит|рекоменд\w+|лучше|желательно)\b", re.IGNORECASE),
    ),
    "poss": (
        re.compile(r"\b(?:may|might|could)\b", re.IGNORECASE),
        re.compile(
            r"\b(?:мо[гж]\w*|можно|возможн\w*|способн\w*|вероятн\w*|позволя\w+|быва\w+|удава\w+|"
            r"умеет|уме\w+|открыва\w+)\b",
            re.IGNORECASE,
        ),
    ),
}
LOGIC = {
    "cause": (
        re.compile(r"\b(?:because|therefore|hence|thus|since)\b", re.IGNORECASE),
        re.compile(
            r"\b(?:потому|поскольку|так как|из-за|поэтому|следовательно|ведь|значит|благодаря|итак|"
            r"таким образом|причин\w+|оттого|стало быть)\b",
            re.IGNORECASE,
        ),
    ),
    "condition": (
        re.compile(r"\b(?:if|unless)\b", re.IGNORECASE),
        re.compile(r"\b(?:если|при|когда|пока|кроме случаев|только)\b", re.IGNORECASE),
    ),
    "contrast": (
        re.compile(r"\b(?:although|however|but|whereas)\b", re.IGNORECASE),
        re.compile(r"\b(?:хотя|однако|но|а|тогда как|при этом|зато|впрочем)\b", re.IGNORECASE),
    ),
}


# The authors speak in the first person; losing it changes the voice of the book.
WE_EN = re.compile(r"\b(?:we|our|us)\b")
WE_RU = re.compile(r"\b(?:мы|нас|нам|нами|наш\w*)\b|\b[а-яё]+(?:ём|ем|им)\b", re.IGNORECASE)
AUTHOR_REGIONS = {"body", "list", "note", "table", "heading"}


def _numbers(text: str, decimal_comma: bool) -> list[str]:
    out = []
    for m in NUMBER.findall(text):
        v = re.sub(r"\s", "", m).strip(".,")
        if decimal_comma:
            v = v.replace(",", ".")
        else:
            v = re.sub(r",(?=\d{3}\b)", "", v)
        out.append(v)
    return sorted(out)


def load_style_rules() -> list[dict]:
    rules = yaml.safe_load((SKILL_DIR / "rules" / "style.yaml").read_text("utf-8"))
    for rule in rules:
        rule["_re"] = re.compile(rule["re"], re.IGNORECASE | re.UNICODE)
        rule["_en"] = re.compile(rule["en"], re.IGNORECASE) if rule.get("en") else None
    return rules


def _finding(rule, cls, unit, detail, extra=None) -> dict:
    f = {"rule": rule, "class": cls, "unit": unit["unit_id"], "detail": detail}
    if extra:
        f.update(extra)
    f["key"] = digest([rule, unit["unit_id"], detail])
    return f


def check_unit(unit: dict, result: dict, concepts: dict, rules: list[dict]) -> list[dict]:
    src, tgt = plain(unit["src"]["text"]), plain(result["tgt"])
    out: list[dict] = []
    if result.get("kept_original"):
        # A reference title or a name left in the source language on purpose, with a stated reason.
        return out
    # The translation of a prompt from code is not expected to repeat the code around it.
    from_code = unit["region"] == "prompt_translation"
    is_prompt = from_code or PROMPT_BREAK in tgt
    aliases = {a.lower() for c in concepts.values() for a in c.get("en", {}).get("aliases", [])}
    if PROMPT_BREAK in tgt:
        # A kept prompt: the original must stay word for word, the checks then read the translation.
        original, tgt = tgt.split(PROMPT_BREAK, 1)
        if original.strip() != src.strip():
            out.append(_finding("P2", "B", unit, "the prompt before the break is not the original"))

    if src.strip() == tgt.strip() and len(re.findall(r"[A-Za-z]{3,}", src)) > 2:
        out.append(_finding("E1", "C", unit, "translation equals the source"))
    for url in URL.findall(src):
        if url.rstrip(".,)") not in tgt:
            out.append(_finding("U1", "B", unit, f"URL changed: {url}"))
    for ident in set(IDENT.findall(src)):
        if ident not in tgt and not from_code and ident.lower() not in aliases:
            out.append(_finding("K3", "C", unit, f"identifier not found: {ident}"))
    if not from_code and _numbers(src, False) != _numbers(tgt, True):
        out.append(
            _finding(
                "N1", "C", unit, f"numbers differ: {_numbers(src, False)} / {_numbers(tgt, True)}"
            )
        )
    for word in MIXED.findall(tgt):
        out.append(_finding("G3", "B", unit, f"mixed alphabets: {word}"))
    doubled = re.findall(r"\b([а-яё]{2,})\s+\1\b", tgt, re.IGNORECASE)
    for word in doubled:
        out.append(_finding("G2", "C", unit, f"doubled word: {word}"))
    if len(src) > 60 and not from_code:
        ratio = len(tgt) / len(src)
        if not 0.6 <= ratio <= 1.9:
            out.append(_finding("L1", "C", unit, f"length ratio {ratio:.2f}"))
    if NEG_EN.search(src) and not NEG_RU_WIDE.search(tgt):
        out.append(_finding("M1", "C", unit, "negation in the source is not found in Russian"))
    elif NEG_RU_STRICT.search(tgt) and not NEG_EN_WIDE.search(src):
        out.append(_finding("M1", "C", unit, "negation in Russian has no source counterpart"))
    if (
        not is_prompt
        and unit["region"] in AUTHOR_REGIONS
        and WE_EN.search(src)
        and not WE_RU.search(tgt)
    ):
        out.append(_finding("M4", "C", unit, "the author's first person is not found in Russian"))
    for name, (en, ru) in MODAL.items():
        if en.search(src) and not ru.search(tgt):
            out.append(_finding("M2", "C", unit, f"modality {name} not found in Russian"))
    for name, (en, ru) in LOGIC.items():
        if en.search(src) and not ru.search(tgt):
            out.append(_finding("M3", "C", unit, f"{name} link not found in Russian"))

    for term in result.get("terms", []):
        concept = concepts.get(term.get("c"))
        ru = (term.get("ru") or "").lower()
        if concept is not None and term.get("paraphrase"):
            # A term left out of its form is exactly where its meaning can get lost.
            out.append(
                _finding(
                    "TP",
                    "C",
                    unit,
                    f"{concept['id']} ({concept['sense']}) paraphrased as {ru!r}: is the sense kept?",
                )
            )
        if concept is None or term.get("paraphrase"):
            continue
        preferred = {f.lower() for f in concept["variants"][0].get("forms", [])}
        allowed = [
            v for v in concept["variants"][1:] if ru in {f.lower() for f in v.get("forms", [])}
        ]
        if ru not in preferred and concept["rendering_policy"] in ("translate", "transliterate"):
            # A conditional variant needs a look at its `when`: this exact check hiding as a hint
            # let "open source" compared with source code pass on the first calibration.
            cls = "A" if allowed and not allowed[0].get("when") else "C"
            detail = f"{concept['id']}: variant {ru!r}"
            detail += (
                f" (allowed when: {allowed[0].get('when')})" if allowed else " is not declared"
            )
            out.append(_finding("T3", cls, unit, detail))

    for rule in rules:
        if rule.get("mode") == "metric" or unit["region"] in rule.get("skip_regions", ()):
            continue
        if is_prompt and rule.get("mode") != "direct":
            continue  # a prompt is a quoted command, not the author's prose
        for m in rule["_re"].finditer(tgt):
            mirrored = bool(rule["_en"] and rule["_en"].search(src))
            severity = "suggestion" if mirrored else rule.get("sev", "minor")
            cls = (
                "A"
                if severity == "suggestion"
                else (
                    "B"
                    if rule.get("mode") == "direct" and severity in ("critical", "major")
                    else "C"
                )
            )
            out.append(
                _finding(
                    rule["id"],
                    cls,
                    unit,
                    m.group(0)[:80],
                    {"origin": "mirrored" if mirrored else "introduced", "severity": severity},
                )
            )
            break  # one finding per rule per unit: a pattern is one problem
    return out


def check_book_level(units, accepted, concepts, all_units=None) -> list[dict]:
    """Consistency over the selection: one concept, several renderings; first mention."""
    out = []
    by_concept: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    by_form: dict[str, set[str]] = defaultdict(set)
    for unit in units:
        result = accepted.get(unit["unit_id"])
        if not result:
            continue
        for term in result.get("terms", []):
            cid, ru = term.get("c"), (term.get("ru") or "").lower()
            if cid in concepts and ru:
                by_concept[cid][ru].append(unit["unit_id"])
                by_form[ru].add(cid)
    for cid, variants in by_concept.items():
        concept = concepts[cid]
        lemmas = {
            v["text"]
            for v in concept["variants"]
            if any(f.lower() in variants for f in v.get("forms", []))
        }
        if len(lemmas) > 1:
            out.append(
                {
                    "rule": "T6",
                    "class": "A",  # all renderings are declared variants, this is a map to read
                    "unit": next(iter(next(iter(variants.values())))),
                    "detail": f"{cid}: several renderings {sorted(lemmas)}",
                    "key": digest(["T6", cid, sorted(lemmas)]),
                }
            )
    for form, cids in by_form.items():
        if len(cids) > 1:
            out.append(
                {
                    "rule": "T7",
                    "class": "C",
                    "unit": None,
                    "detail": f"{sorted(cids)} share the Russian form {form!r}",
                    "key": digest(["T7", form, sorted(cids)]),
                }
            )
    # "First" means first in the book, not in this selection: a chapter checked on its own must
    # not be asked to introduce a term that an earlier chapter already introduced.
    wanted = {u["unit_id"] for u in units}
    earliest: dict[str, tuple[dict, dict]] = {}
    for unit in all_units if all_units is not None else units:
        result = accepted.get(unit["unit_id"])
        for term in (result or {}).get("terms", []):
            # A heading names a term; the gloss belongs to the text under it.
            if term.get("c") in concepts and unit.get("region") != "heading":
                earliest.setdefault(term["c"], (unit, result))
    for cid, (unit, result) in earliest.items():
        mention = concepts[cid].get("first_mention")
        if (
            mention
            and unit["unit_id"] in wanted
            and not _has_mention(mention, result, concepts[cid])
        ):
            out.append(_finding("T8", "B", unit, f"{cid}: first mention must read {mention!r}"))
    return out


def _has_mention(mention: str, result: dict, concept: dict) -> bool:
    """The gloss must stand right after the term; the term itself may take any case."""
    text = plain(result["tgt"]).lower()
    if mention.lower() in text:
        return True
    gloss = re.search(r"\([^)]*\)", mention)
    if not gloss:
        return False
    return any(f"{form} {gloss.group(0).lower()}" in text for form in batch_mod._all_forms(concept))


def _deps(ws: Workspace, unit, result, concepts) -> list[str]:
    manifest = json.loads((ws.batches / f"{result['batch']}.json").read_text("utf-8"))["manifest"]
    return sorted(
        set(manifest["deps"].get(unit["unit_id"], []))
        | {
            c
            for o in bible_mod.find_occurrences(unit["src"]["text"], concepts)
            for c in o["concepts"]
        }
    )


def check_book_rules(unit: dict, result: dict, book_rules: list[dict]) -> list[dict]:
    """A book rule with a `pattern` check is a fact the code can verify, not a judgement."""
    out = []
    for rule in book_rules:
        check = rule.get("check") or {}
        zones = rule.get("zones")
        if check.get("kind") != "pattern" or (zones and unit["region"] not in zones):
            continue
        text = plain(result["tgt"])
        if text.strip() and not re.match(check["pattern"], text):
            out.append(_finding(rule["id"], "B", unit, f"does not match {check['pattern']}"))
    return out


def run_qa(ws: Workspace, pages: set[int] | None, name: str) -> dict:
    units = ws.units()
    accepted = ws.accepted()
    concepts = ws.concepts()
    rules = load_style_rules()
    doc, voice, notes = ws.passport(), ws.voice(), ws.notes()
    problems = bible_mod.rule_problems(doc.get("rules", []))
    # Validate before applying any rule, including passports edited outside the CLI.
    if problems:
        raise bible_mod.BibleError(json.dumps(problems, ensure_ascii=False))
    book_rules = [r for r in doc.get("rules", []) if r.get("status") == "active"]
    selected = list(batch_mod._select(units, pages))
    findings: list[dict] = []
    hashes: dict[str, str] = {}
    coverage = 0
    missing = []
    for unit in selected:
        result = accepted.get(unit["unit_id"])
        if result is None:
            missing.append(unit["unit_id"])
            findings.append(_finding("R1", "B", unit, "no accepted result"))
            continue
        coverage += 1
        deps = _deps(ws, unit, result, concepts)
        hashes[unit["unit_id"]] = store.evidence_fp(
            unit,
            result["tgt"],
            deps,
            "qa",
            TOOL_REV,
            ws,
            concepts=concepts,
            doc=doc,
            voice=voice,
            notes=notes,
        )
        findings += check_unit(unit, result, concepts, rules)
        findings += check_book_rules(unit, result, book_rules)
    findings += check_book_level(selected, accepted, concepts, units)
    report = {
        "tool_rev": TOOL_REV,
        "selected": len(selected),
        "covered": coverage,
        "evidence": hashes,
        "findings": findings,
        "counts": {c: sum(f["class"] == c for f in findings) for c in "BCA"},
    }
    write_atomic(ws.reports / f"qa-{name}.json", json.dumps(report, ensure_ascii=False, indent=1))
    return {k: report[k] for k in ("selected", "covered", "counts")}


def _expand_evidence(step: str, entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """A list of unit IDs proves nothing was read. Each pass must show what it looked at.

    `check`: per unit, what the English states and whether the Russian covers it.
    `read-target`: per window of units, what the reader noticed.
    """
    bad: list[dict] = []
    out: list[dict] = []
    for entry in entries:
        if step == "check":
            if not entry.get("en_propositions") or not str(entry.get("ru_coverage", "")).strip():
                bad.append(
                    {
                        "unit": entry.get("unit"),
                        "problem": "en_propositions and ru_coverage required",
                    }
                )
                continue
            out.append(entry)
        elif step == "read-target":
            if not entry.get("units") or not str(entry.get("observations", "")).strip():
                bad.append(
                    {"window": entry.get("units"), "problem": "units and observations required"}
                )
                continue
            window = digest(entry["units"])
            findings = entry.get("findings", [])
            for uid in entry["units"]:
                out.append(
                    {
                        "unit": uid,
                        "window": window,
                        "findings": [f for f in findings if f.get("unit") == uid],
                    }
                )
        else:
            out.append(entry)
    return out, bad


def record_evidence(ws: Workspace, step: str, entries: list[dict]) -> dict:
    """A semantic pass over units. Quotes are verified against the texts: invented ones are refused."""
    units = {u["unit_id"]: u for u in ws.units()}
    accepted = ws.accepted()
    concepts, doc, voice, notes = ws.concepts(), ws.passport(), ws.voice(), ws.notes()
    bad = []
    rows = read_jsonl(ws.reports / f"evidence-{step}.jsonl")
    current = {r["unit"]: r for r in rows}
    entries, shape_problems = _expand_evidence(step, entries)
    bad += shape_problems
    for entry in entries:
        uid = entry["unit"]
        unit, result = units.get(uid), accepted.get(uid)
        if unit is None or result is None:
            bad.append({"unit": uid, "problem": "no accepted result"})
            continue
        for finding in entry.get("findings", []):
            if finding.get("en") and finding["en"] not in plain(unit["src"]["text"]):
                bad.append({"unit": uid, "problem": f"EN quote not in source: {finding['en']!r}"})
            if finding.get("ru") and finding["ru"] not in plain(result["tgt"]):
                bad.append({"unit": uid, "problem": f"RU quote not in target: {finding['ru']!r}"})
        deps = _deps(ws, unit, result, concepts)
        current[uid] = {
            "unit": uid,
            "fp": store.evidence_fp(
                unit,
                result["tgt"],
                deps,
                step,
                TOOL_REV,
                ws,
                concepts=concepts,
                doc=doc,
                voice=voice,
                notes=notes,
            ),
            "findings": entry.get("findings", []),
            **{k: entry[k] for k in ("en_propositions", "ru_coverage", "window") if k in entry},
        }
    if bad:
        return {"ok": False, "problems": bad}
    write_jsonl(ws.reports / f"evidence-{step}.jsonl", list(current.values()))
    return {"ok": True, "recorded": len(entries)}


def apply_edit(ws: Workspace, step: str, edits: list[dict]) -> dict:
    """A text change goes through the same validators and drops the unit's evidence by hash."""
    units = {u["unit_id"]: u for u in ws.units()}
    accepted = ws.accepted()
    concepts = ws.concepts()
    doc, voice, notes = ws.passport(), ws.voice(), ws.notes()
    problems = []
    rows = read_jsonl(ws.results / "edits.jsonl")
    for edit in edits:
        uid = edit["id"]
        if not (edit.get("reason") or "").strip():
            problems.append({"unit": uid, "problem": "a reason is required"})
            continue
        unit, result = units.get(uid), accepted.get(uid)
        if unit is None or result is None:
            problems.append({"unit": uid, "problem": "no accepted result"})
            continue
        for span in batch_mod.check_spans(unit.get("spans", {}), edit["tgt"]):
            problems.append({"unit": uid, "problem": span})
        terms = batch_mod.normalize_terms(uid, edit.get("terms", result.get("terms", [])))
        problems.extend(
            batch_mod._check_terms({**result, "terms": terms}, unit, edit["tgt"], concepts)
        )
        fingerprint = store.translation_fp(
            unit,
            _deps(ws, unit, result, concepts),
            ws,
            concepts=concepts,
            doc=doc,
            voice=voice,
            notes=notes,
        )
        rows.append(
            {
                "id": uid,
                "step": step,
                "reason": edit["reason"],
                "patch": {
                    "tgt": edit["tgt"],
                    "terms": terms,
                    "fingerprint": fingerprint,
                    **({"deviations": edit["deviations"]} if "deviations" in edit else {}),
                },
            }
        )
    if problems:
        return {"ok": False, "problems": problems}
    write_jsonl(ws.results / "edits.jsonl", rows)
    return {"ok": True, "applied": len(edits)}


def decide(ws: Workspace, decisions: list[dict]) -> dict:
    """A candidate is closed by occurrence with a concrete basis, and only for the current text."""
    accepted = ws.accepted()
    rows = {r["key"]: r for r in read_jsonl(ws.reports / "dispositions.jsonl")}
    bad = []
    for d in decisions:
        if d.get("status") not in ("confirmed", "rejected", "not_applicable"):
            bad.append({"key": d.get("key"), "problem": "bad status"})
        elif not (d.get("basis") or "").strip():
            bad.append({"key": d.get("key"), "problem": "a basis is required"})
        elif d.get("unit") and d["unit"] not in accepted:
            bad.append({"key": d.get("key"), "problem": "unit has no result"})
        else:
            tgt = accepted[d["unit"]]["tgt"] if d.get("unit") else ""
            rows[d["key"]] = {**d, "tgt_hash": digest(tgt)}
    if bad:
        return {"ok": False, "problems": bad}
    write_jsonl(ws.reports / "dispositions.jsonl", list(rows.values()))
    return {"ok": True, "decided": len(decisions)}


def chapter_gate(
    ws: Workspace, pages: set[int] | None, name: str, steps=("check", "read-target")
) -> dict:
    """pass, blocked, or a list of what is missing. Recomputed from files, never from a stored flag."""
    problems = bible_mod.rule_problems(ws.passport().get("rules", []))
    if problems:
        return {
            "verdict": "blocked",
            "blockers": [
                {"rule": p["rule"], "unit": None, "detail": p["problem"]} for p in problems
            ],
            "blocker_count": len(problems),
        }
    run_qa(ws, pages, name)
    report = json.loads((ws.reports / f"qa-{name}.json").read_text("utf-8"))
    units = ws.units()
    accepted = ws.accepted()
    concepts, doc, voice, notes = ws.concepts(), ws.passport(), ws.voice(), ws.notes()
    selected = batch_mod._select(units, pages)
    if not list(selected):
        # Nothing was checked, so nothing passed: a mistyped chapter must not look green.
        return {"verdict": "empty", "selected": 0, "reason": "no units in this selection"}
    state = batch_mod.status(ws, pages)
    dispositions = {r["key"]: r for r in read_jsonl(ws.reports / "dispositions.jsonl")}
    blockers: list[dict] = []
    open_candidates = []
    for finding in report["findings"]:
        if finding["class"] == "B":
            blockers.append(finding)
        elif finding["class"] == "C":
            decision = dispositions.get(finding["key"])
            uid = finding.get("unit")
            current = digest(accepted[uid]["tgt"]) if uid in accepted else ""
            if not decision or (uid and decision.get("tgt_hash") != current):
                open_candidates.append(finding)
            elif decision["status"] == "confirmed":
                blockers.append({**finding, "confirmed": True})
    wanted = {u["unit_id"] for u in selected}
    open_revision = [i for i in revision.open_items(ws) if i["unit"] in wanted]
    missing_evidence: dict[str, list[str]] = {}
    for step in steps:
        rows = {r["unit"]: r for r in read_jsonl(ws.reports / f"evidence-{step}.jsonl")}
        lacking = []
        for unit in selected:
            result = accepted.get(unit["unit_id"])
            if result is None:
                continue
            deps = _deps(ws, unit, result, concepts)
            want = store.evidence_fp(
                unit,
                result["tgt"],
                deps,
                step,
                TOOL_REV,
                ws,
                concepts=concepts,
                doc=doc,
                voice=voice,
                notes=notes,
            )
            row = rows.get(unit["unit_id"])
            if not row or row["fp"] != want:
                lacking.append(unit["unit_id"])
            else:
                for f in row.get("findings", []):
                    if f.get("severity") in ("critical", "major") and f.get("status") != "fixed":
                        blockers.append({"rule": step, "unit": unit["unit_id"], "detail": f})
        if lacking:
            missing_evidence[step] = lacking
    verdict = "pass"
    if (
        state["todo_count"]
        or state["stale_count"]
        or state["blocking_questions"]
        or blockers
        or open_revision
    ):
        verdict = "blocked"
    elif open_candidates or missing_evidence:
        verdict = "incomplete"
    return {
        "verdict": verdict,
        "todo": state["todo_count"],
        "stale": state["stale_count"],
        "blocking_questions": state["blocking_questions"],
        "open_revision_items": len(open_revision),
        "blockers": blockers[:40],
        "blocker_count": len(blockers),
        "open_candidates": open_candidates[:60],
        "open_candidate_count": len(open_candidates),
        "missing_evidence": {k: len(v) for k, v in missing_evidence.items()},
    }
