"""The book's living reference: concepts, notes, questions. Written only here, always journaled."""

from __future__ import annotations

import datetime as dt
import itertools
import re
from pathlib import Path

from .store import CONCEPT_NORM, Workspace, digest, project, read_jsonl, write_jsonl

SCHEMA_REQUIRED = ("id", "status", "sense", "en", "rendering_policy", "variants", "reason")
STATUSES = {"candidate", "accepted", "superseded"}
POLICIES = {"translate", "transliterate", "keep_latin", "keep_latin_with_gloss"}
ORIGINS = {"owner", "agent", "research", "author"}
CASES = ("nomn", "gent", "datv", "accs", "ablt", "loct")


class BibleError(ValueError):
    pass


def _morph():
    import pymorphy3

    return pymorphy3.MorphAnalyzer()


def _pick_parse(word: str, morph, want: str | None = None):
    parses = morph.parse(word)
    if want:
        for p in parses:
            if want in p.tag:
                return p
    for p in parses:
        if "nomn" in p.tag:
            return p
    return parses[0]


def generate_forms(text: str, head: int | None = None, fixed: list[str] | None = None) -> dict:
    """All case and number forms of a term. Words in `fixed` and Latin words do not inflect.

    The head noun sets case and number; adjectives agree with it. `parse` is `predicted` when the
    dictionary does not know the head: the form set then needs a look by the author of the decision.
    """
    morph = _morph()
    fixed = [f.lower() for f in fixed or []]
    words = text.split()
    parses = [
        None
        if (w.lower() in fixed or re.fullmatch(r"[A-Za-z0-9\-.+#/]+", w))
        else _pick_parse(w.lower(), morph)
        for w in words
    ]
    if head is None:
        head = next(
            (i for i, p in enumerate(parses) if p is not None and "NOUN" in p.tag),
            None,
        )
        # "рабочий" is also a noun, but before a noun in the same form it is the adjective.
        if head is not None and head + 1 < len(words) and parses[head + 1] is not None:
            after = parses[head + 1]
            if (
                "NOUN" in after.tag
                and after.tag.case == "nomn"
                and any(
                    "ADJF" in p.tag and p.tag.case == "nomn"
                    for p in morph.parse(words[head].lower())
                )
            ):
                head += 1
    if head is None and fixed and any(p is not None and "ADJF" in p.tag for p in parses):
        # An adjective with an uninflected noun ("диалоговый ИИ"): only the adjective changes.
        forms = set()
        for case in CASES:
            per_word = []
            for word, parse in zip(words, parses, strict=True):
                changed = parse.inflect({case, "masc", "sing"}) if parse is not None else None
                per_word.append(changed.word if changed else word.lower())
            forms.add(" ".join(per_word))
        return {"forms": sorted(forms), "parse": "predicted"}
    if head is None:
        # Cyrillic with no noun found ("входные данные") is a gap in the analyser, not a fixed term.
        state = "predicted" if re.search(r"[а-яё]", text.lower()) else "fixed"
        return {"forms": sorted({text.lower()}), "parse": state}
    head_parse = _pick_parse(words[head].lower(), morph, "NOUN")
    gender = head_parse.tag.gender
    known = morph.word_is_known(words[head].lower())
    head_parses = (
        [head_parse]
        if known
        else [p for p in morph.parse(words[head].lower()) if "NOUN" in p.tag] or [head_parse]
    )
    # An adjective can have several lemmas ("большая" is "большой" and "больший"): keep them all
    # and flag the term, because a wrong single choice would reject a correct translation.
    options: list[list] = []
    ambiguous = False
    for i, parse in enumerate(parses):
        if parse is None or i >= head or "ADJF" not in parse.tag and "PRTF" not in parse.tag:
            options.append([parse])
            continue
        lemmas = {}
        for candidate in morph.parse(words[i].lower()):
            if ("ADJF" in candidate.tag or "PRTF" in candidate.tag) and "nomn" in candidate.tag:
                lemmas.setdefault(candidate.normal_form, candidate)
        ambiguous = ambiguous or len(lemmas) > 1
        options.append(list(lemmas.values()) or [parse])
    forms = set()
    for number in ("sing", "plur"):
        for case in CASES:
            per_word = []
            for i, word in enumerate(words):
                if i == head:
                    # An unknown word gets several guessed parses and each has gaps: join them.
                    variants = set()
                    for parse in head_parses:
                        changed = parse.inflect({case, number})
                        if changed:
                            variants.add(changed.word)
                    per_word.append(sorted(variants) or [word.lower()])
                elif i > head or options[i][0] is None:
                    per_word.append(
                        [word.lower()]
                    )  # after the head: "база данных" stays as written
                else:
                    wanted = {case, number} | ({gender} if number == "sing" and gender else set())
                    variants = []
                    for parse in options[i]:
                        changed = parse.inflect(wanted)
                        variants.append(changed.word if changed else word.lower())
                    per_word.append(sorted(set(variants)))
            forms.update(" ".join(c) for c in itertools.product(*per_word))
    parse_state = "ambiguous" if ambiguous else ("confirmed" if known else "predicted")
    return {"forms": sorted(forms), "parse": parse_state}


def validate(concept: dict) -> None:
    missing = [k for k in SCHEMA_REQUIRED if k not in concept]
    if missing:
        raise BibleError(f"{concept.get('id')}: missing fields {missing}")
    if concept["status"] not in STATUSES:
        raise BibleError(f"{concept['id']}: bad status {concept['status']}")
    if concept["rendering_policy"] not in POLICIES:
        raise BibleError(f"{concept['id']}: bad rendering_policy")
    if concept.get("origin", "agent") not in ORIGINS:
        raise BibleError(f"{concept['id']}: bad origin")
    if not concept["en"].get("aliases"):
        raise BibleError(f"{concept['id']}: en.aliases is empty")
    if not any(v.get("kind") == "preferred" for v in concept["variants"]):
        raise BibleError(f"{concept['id']}: no preferred variant")


def rule_problems(rules: list) -> list[dict]:
    """An active rule without a usable check would look enforced while nothing checks it."""
    problems = []
    for rule in rules:
        rid = rule.get("id") if isinstance(rule, dict) else None
        if not rid or not isinstance(rule, dict):
            problems.append({"rule": rid, "problem": "a rule needs an id"})
            continue
        if rule.get("status") != "active":
            continue
        if not rule.get("do"):
            problems.append({"rule": rid, "problem": "an active rule needs `do`"})
        check = rule.get("check") or {}
        kind = check.get("kind")
        if kind == "pattern":
            try:
                re.compile(check.get("pattern") or "")
            except re.error as error:
                problems.append({"rule": rid, "problem": f"bad pattern: {error}"})
            if not check.get("pattern"):
                problems.append({"rule": rid, "problem": "check.pattern is empty"})
        elif kind == "judgement_only":
            if check.get("step") not in ("check", "read-target"):
                problems.append({"rule": rid, "problem": "check.step: check or read-target"})
        else:
            problems.append({"rule": rid, "problem": "check.kind: pattern or judgement_only"})
    return problems


def _complete_forms(concept: dict) -> None:
    for variant in concept["variants"]:
        if not variant.get("forms"):
            made = generate_forms(variant["text"], variant.get("head"), variant.get("fixed"))
            variant["forms"] = made["forms"]
            variant.setdefault("parse", made["parse"])


def add(ws: Workspace, concepts: list[dict], actor: str = "agent") -> list[dict]:
    """Add new concepts or new revisions. An owner decision cannot be overwritten by the agent."""
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    path = ws.bible / "concepts.jsonl"
    rows = {c["id"]: c for c in read_jsonl(path)}
    changed = []
    for concept in concepts:
        concept = dict(concept)
        validate(concept)
        _complete_forms(concept)
        old = rows.get(concept["id"])
        if old and old.get("origin") == "owner" and actor != "owner":
            raise BibleError(f"{concept['id']}: owner decision cannot be changed by {actor}")
        norm_changed = not old or digest(project(old, CONCEPT_NORM)) != digest(
            project(concept, CONCEPT_NORM)
        )
        concept["rev"] = (old or {}).get("rev", 0) + 1
        rows[concept["id"]] = concept
        row = {"id": concept["id"], "rev": concept["rev"], "normative": norm_changed}
        review = [
            {"text": v["text"], "parse": v.get("parse"), "forms": v["forms"]}
            for v in concept["variants"]
            if v.get("parse") in ("predicted", "ambiguous")
        ]
        if review:
            row["review_forms"] = review  # guessed forms must be read before they are relied on
        changed.append(row)
        ws.journal(
            {
                "at": now,
                "actor": actor,
                "kind": "concept",
                "id": concept["id"],
                "rev": concept["rev"],
                "normative": norm_changed,
                "reason": concept.get("reason"),
            }
        )
    write_jsonl(path, list(rows.values()))
    return changed


def add_notes(ws: Workspace, notes: list[dict], units_by_id: dict[str, dict]) -> int:
    """A note must quote its place in the source: the code verifies the quote."""
    path = ws.bible / "notes.jsonl"
    rows = {n["id"]: n for n in read_jsonl(path)}
    for note in notes:
        unit = units_by_id.get(note["occ"])
        if unit is None:
            raise BibleError(f"{note['id']}: unknown unit {note['occ']}")
        if note["quote"] not in unit["src"]["text"]:
            raise BibleError(f"{note['id']}: quote is not in {note['occ']}")
        old = rows.get(note["id"])
        if old is not None and old["occ"] != note["occ"]:
            # Two places under one number would silently drop a note other translations rely on.
            raise BibleError(f"{note['id']}: already used for {old['occ']}")
        rows[note["id"]] = {**note, "src_hash": unit["src"]["hash"]}
    write_jsonl(path, list(rows.values()))
    return len(notes)


def alias_pattern(aliases: list[str]) -> re.Pattern:
    parts = sorted({a.lower() for a in aliases}, key=len, reverse=True)
    return re.compile(
        r"(?<![\w-])(" + "|".join(re.escape(p) for p in parts) + r")(?![\w-])", re.IGNORECASE
    )


def find_occurrences(text: str, concepts: dict[str, dict]) -> list[dict]:
    """Stable EN occurrence IDs: longer aliases win, so "model weights" hides "weights" inside it."""
    plain = re.sub("⟦/?a?\\d+/?⟧", " ", text)
    hits: list[tuple[int, int, str, str]] = []
    for concept in concepts.values():
        if concept["status"] == "superseded":
            continue
        # not_when lists source phrases where the alias is an ordinary word or part of a name.
        excluded = [
            (x.start(), x.end())
            for phrase in concept.get("not_when", [])
            for x in re.finditer(re.escape(phrase), plain, re.IGNORECASE)
        ]
        for m in alias_pattern(concept["en"]["aliases"]).finditer(plain):
            if not any(a <= m.start() and m.end() <= b for a, b in excluded):
                hits.append((m.start(), m.end(), concept["id"], m.group(0)))
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    chosen: list[tuple[int, int, list[str], str]] = []
    for start, end, cid, surface in hits:
        if chosen and start < chosen[-1][1]:
            if (start, end) == (chosen[-1][0], chosen[-1][1]):
                chosen[-1][2].append(cid)  # the same words can belong to several concepts
            continue
        chosen.append((start, end, [cid], surface))
    # The ordinal counts only this concept: adding another concept must not renumber anything.
    seen: dict[str, int] = {}
    out = []
    for c in chosen:
        key = c[2][0]
        seen[key] = seen.get(key, 0) + 1
        out.append(
            {
                "n": f"{key}-{seen[key]}",
                "concepts": c[2],
                "en": c[3],
                "context": plain[max(0, c[0] - 30) : c[1] + 30],
            }
        )
    return out


def load_many(path: Path) -> list[dict]:
    import json

    import yaml

    text = path.read_text("utf-8")
    if path.suffix in (".yaml", ".yml"):
        doc = yaml.safe_load(text)
        return doc if isinstance(doc, list) else [doc]
    if text.lstrip().startswith("["):
        return json.loads(text)
    return [json.loads(x) for x in text.splitlines() if x.strip()]
