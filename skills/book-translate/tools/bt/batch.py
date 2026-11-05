"""Work protocol: take a package -> prepare a result -> validate -> accept atomically."""

from __future__ import annotations

import datetime as dt
import json
import re

from . import bible as bible_mod
from . import store
from .store import Workspace, digest, read_jsonl, write_atomic, write_jsonl

SPAN_TOKEN = re.compile("⟦(/?)(a?\\d+)⟧")
TRANSLATABLE = {
    "body",
    "heading",
    "part",
    "list",
    "note",
    "note_title",
    "table",
    "caption",
    "quote",
    "title",
    "subtitle",
    "alt",
    "figure_text",
    "prompt_translation",
    "orphan",
    "index",
}
WORD = re.compile(r"[\w'’-]+")


class SubmitRejected(ValueError):
    def __init__(self, problems: list[dict]):
        super().__init__(f"{len(problems)} problems")
        self.problems = problems


def tokens_estimate(text: str) -> int:
    return max(1, len(text) // 3)


def plain(text: str) -> str:
    return SPAN_TOKEN.sub("", text)


def src_echo(text: str, words: int = 3) -> str:
    """The first words, ignoring how apostrophes and hyphens split them: a slip there is not a shift."""
    joined = re.sub(r"[\W_]+", " ", plain(text)).lower().split()
    return "".join(joined[: words + 2])


def echo_matches(given: str, source: str) -> bool:
    """`given` must be the start of the source, at least a few characters of it."""
    compact = re.sub(r"[\W_]+", "", plain(given)).lower()
    whole = re.sub(r"[\W_]+", "", plain(source)).lower()
    if not whole:  # a unit of punctuation only has no words to compare
        return plain(given).strip() == plain(source).strip()
    return bool(compact) and whole.startswith(compact)


def count_word(lowered: str, ru: str) -> int:
    """Occurrences of a form as whole words: "бенчмарки" is not inside "бенчмаркинга"."""
    return len(re.findall(rf"(?<!\w){re.escape(ru)}(?!\w)", lowered))


def check_spans(src_spans: dict, tgt: str) -> list[str]:
    """Spans can move inside the unit. Pairs stay balanced, atoms stay single, nothing is new."""
    problems: list[str] = []
    stack: list[str] = []
    seen: dict[str, int] = {}
    for m in SPAN_TOKEN.finditer(tgt):
        closing, key = m.group(1) == "/", m.group(2)
        span = src_spans.get(key)
        if span is None:
            problems.append(f"unknown span {key}")
            continue
        if span["kind"] == "atom":
            if closing:
                problems.append(f"atom {key} cannot close")
            seen[key] = seen.get(key, 0) + 1
        elif closing:
            if not stack or stack[-1] != key:
                problems.append(f"bad nesting at /{key}")
            else:
                stack.pop()
        else:
            seen[key] = seen.get(key, 0) + 1
            stack.append(key)
    if stack:
        problems.append(f"unclosed spans {stack}")
    for key in src_spans:
        n = seen.get(key, 0)
        if n != 1:
            problems.append(f"span {key} appears {n} times")
    return problems


class Chapters(frozenset):
    """A selection by chapter: chapters share pages, so a page range can catch a neighbour."""


def _select(units: list[dict], pages: set[int] | None) -> list[dict]:
    wanted = [u for u in units if u["class"] == "translate" and u["region"] in TRANSLATABLE]
    if pages is not None:
        field = "chapter" if isinstance(pages, Chapters) else "page"
        wanted = [u for u in wanted if u[field] in pages]
    return wanted


def _chapters(units: list[dict], accepted: dict) -> list[dict]:
    """Progress by internal chapter number; the printed number may differ."""
    out: dict[int, dict] = {}
    for u in _select(units, None):
        row = out.setdefault(
            u["chapter"],
            {"chapter": u["chapter"], "first_page": u["page"], "units": 0, "translated": 0},
        )
        row["last_page"] = u["page"]
        row["units"] += 1
        row["translated"] += u["unit_id"] in accepted
    return list(out.values())


def _context(ws: Workspace, unit_ids: list[str], units: list[dict], accepted, channels) -> dict:
    position = {u["unit_id"]: i for i, u in enumerate(units)}
    first = position[unit_ids[0]]
    last = position[unit_ids[-1]]
    before = []
    for u in reversed(units[max(0, first - 12) : first]):
        result = accepted.get(u["unit_id"])
        if result and u["region"] in ("body", "list", "heading", "note"):
            before.append({"src": plain(u["src"]["text"]), "tgt": plain(result["tgt"])})
        if len(before) == 3:
            break
    after = next(
        (u for u in units[last + 1 : last + 6] if u["region"] in ("body", "list", "heading")), None
    )
    return {
        "source_context": {
            "before": list(reversed(before)),
            "after_do_not_translate": plain(after["src"]["text"]) if after else None,
        }
        if channels.get("source_context", True)
        else None
    }


def next_batch(
    ws: Workspace,
    pages: set[int] | None = None,
    max_words: int = 1500,
    max_units: int = 30,
    max_input_tokens: int = 8000,
    max_output_tokens: int = 6000,
    channels: dict | None = None,
) -> dict | None:
    """Build the next package from untranslated units. Groups are never split silently."""
    channels = channels or {}
    units = ws.units()
    accepted = ws.accepted()
    todo = [u for u in _select(units, pages) if u["unit_id"] not in accepted]
    if not todo:
        return None
    concepts = ws.concepts()
    doc = ws.passport()
    notes = ws.notes()
    voice = ws.voice()
    chosen: list[dict] = []
    words = 0
    i = 0
    chapter = todo[0]["chapter"]
    while i < len(todo) and todo[i]["chapter"] == chapter:
        group = todo[i].get("group")
        block = [todo[i]]
        j = i + 1
        while group and j < len(todo) and todo[j].get("group") == group:
            block.append(todo[j])
            j += 1
        size = sum(len(WORD.findall(plain(u["src"]["text"]))) for u in block)
        if chosen and (words + size > max_words or len(chosen) + len(block) > max_units):
            break
        chosen += block
        words += size
        i = j
    deps_by_unit: dict[str, list[str]] = {}
    items = []
    for unit in chosen:
        occ = bible_mod.find_occurrences(unit["src"]["text"], concepts)
        occurrences = [{**o, "id": f"occ-{unit['unit_id']}-{o['n']}"} for o in occ]
        deps_by_unit[unit["unit_id"]] = sorted({c for o in occ for c in o["concepts"]})
        item = {
            "id": unit["unit_id"],
            "region": unit["region"],
            "src": unit["src"]["text"],
            "occurrences": occurrences,
            "notes": [n for n in notes if n["occ"] == unit["unit_id"]],
        }
        if unit.get("cell"):
            item["cell"] = {k: unit["cell"][k] for k in ("row", "col", "header")}
        if unit["spans"]:
            item["spans"] = {
                k: {
                    "kind": s["kind"],
                    "role": s["role"],
                    **({"text": s["data"]["text"]} if s["kind"] == "atom" else {}),
                }
                for k, s in unit["spans"].items()
            }
        items.append(item)
    used = sorted({c for deps in deps_by_unit.values() for c in deps})
    regions = {u["region"] for u in chosen}
    rules = [
        r
        for r in doc.get("rules", [])
        if r.get("status") == "active" and (not r.get("zones") or set(r["zones"]) & regions)
    ]
    payload = {
        "passport_min": {
            k: doc.get("passport", {}).get(k)
            for k in ("function", "address", "modal_map", "code_policy")
        },
        "rules": [{"id": r["id"], "when": r.get("when"), "do": r.get("do")} for r in rules]
        if channels.get("rules", True)
        else [],
        "voice": [
            v
            for v in voice.get("voice_features", [])
            if not v.get("zones") or set(v["zones"]) & regions
        ][:5]
        if channels.get("voice", True)
        else [],
        "terms": [_term_view(concepts[c]) for c in used] if channels.get("terms", True) else [],
        **_context(ws, [u["unit_id"] for u in chosen], units, accepted, channels),
        "translate_this": items,
    }
    in_tokens = tokens_estimate(json.dumps(payload, ensure_ascii=False))
    out_tokens = int(tokens_estimate(" ".join(i["src"] for i in items)) * 2.2)
    oversize = in_tokens > max_input_tokens or out_tokens > max_output_tokens
    fingerprints = {
        u["unit_id"]: store.translation_fp(
            u, deps_by_unit[u["unit_id"]], ws, concepts=concepts, doc=doc, voice=voice, notes=notes
        )
        for u in chosen
    }
    index = read_jsonl(ws.batches / "index.jsonl")
    # Decisions not shown in the package (term forms, for one) still change what is valid.
    context_hash = digest([payload, fingerprints])
    for row in reversed(index):
        # A package left open by an interrupted session is returned, not issued again.
        path = ws.batches / f"{row['batch']}.json"
        previous = json.loads(path.read_text("utf-8"))["manifest"]
        if previous["context_hash"] == context_hash:
            return {
                "batch": row["batch"],
                **previous["sizes"],
                "oversize": previous["oversize"],
                "units": len(previous["unit_ids"]),
                "file": str(path),
                "reused": True,
            }
    seq = len(index) + 1
    batch_id = f"b{seq:04d}"
    manifest = {
        "batch": batch_id,
        "unit_ids": [u["unit_id"] for u in chosen],
        "src_hashes": {u["unit_id"]: u["src"]["hash"] for u in chosen},
        "deps": deps_by_unit,
        "fingerprints": fingerprints,
        "context_hash": context_hash,
        "sizes": {"words": words, "input_tokens": in_tokens, "output_tokens": out_tokens},
        "oversize": oversize,
    }
    write_atomic(
        ws.batches / f"{batch_id}.json",
        json.dumps({"manifest": manifest, "package": payload}, ensure_ascii=False, indent=1),
    )
    with (ws.batches / "index.jsonl").open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"batch": batch_id, "units": len(chosen)}) + "\n")
    return {
        "batch": batch_id,
        **manifest["sizes"],
        "oversize": oversize,
        "units": len(chosen),
        "file": str(ws.batches / f"{batch_id}.json"),
    }


def _term_view(concept: dict) -> dict:
    view = {
        "id": concept["id"],
        "sense": concept["sense"],
        "policy": concept["rendering_policy"],
        "use": concept["variants"][0]["text"],
        "inflect": concept["rendering_policy"] in ("translate", "transliterate"),
    }
    others = [{"text": v["text"], "when": v.get("when")} for v in concept["variants"][1:]]
    if others:
        view["also"] = others
    for key in ("forbidden", "first_mention", "applies_when", "not_when"):
        if concept.get(key):
            view[key] = concept[key]
    return view


def _all_forms(concept: dict) -> set[str]:
    return {f.lower() for v in concept["variants"] for f in v.get("forms", [])}


def _citable(concept: dict) -> list[str]:
    forms = _all_forms(concept)
    if concept["rendering_policy"] in ("keep_latin", "keep_latin_with_gloss"):
        forms |= {a.lower() for a in concept["en"]["aliases"]}
    return sorted(forms, key=len, reverse=True)


def cite(ws: Workspace, draft: dict) -> dict:
    """Fill `terms` and `src_echo` of a draft from the translation itself. Nothing here decides a term.

    A form that is really in the text is cited literally. An occurrence with no form needs a word
    from the author of the translation in `paraphrases`, which then goes to the meaning check. Until
    then the draft stays open. The rules of the package are always returned: a draft can be correct
    in every term and still break them.
    """
    units = {u["unit_id"]: u for u in ws.units()}
    concepts = ws.concepts()
    regions = set()
    unresolved = []
    for item in draft.get("units", []):
        unit = units.get(item.get("id"))
        if unit is None:
            unresolved.append({"unit": item.get("id"), "problem": "unknown unit"})
            continue
        regions.add(unit["region"])
        item["src_echo"] = " ".join(plain(unit["src"]["text"]).split()[:3])
        text = plain(item.get("tgt", "")).lower()
        wanted: dict[str, list[dict]] = {}
        for occ in bible_mod.find_occurrences(unit["src"]["text"], concepts):
            wanted.setdefault(occ["n"].rsplit("-", 1)[0], []).append(occ)
        given = item.pop("paraphrases", {})
        terms = {}
        for cid, occs in wanted.items():
            pattern = "|".join(re.escape(f) for f in _citable(concepts[cid]))
            found = re.findall(rf"(?<!\w)(?:{pattern})(?!\w)", text) if pattern else []
            cited = found[: len(occs)] + [f"~{w}" for w in given.get(cid, [])]
            cited = cited[: len(occs)]
            if item.get("kept_original"):
                # A title left in the source language on purpose has no Russian form to cite.
                cited += [f"~{o['en']}" for o in occs[len(cited) :]]
            if len(cited) < len(occs):
                unresolved.append(
                    {
                        "unit": item["id"],
                        "concept": cid,
                        "needs": len(occs) - len(cited),
                        "en": [o["context"] for o in occs],
                        "found": found,
                    }
                )
            terms[cid] = cited
        item["terms"] = terms
    doc = ws.passport()
    rules = [
        {"id": r["id"], "zones": r.get("zones"), "do": r.get("do")}
        for r in doc.get("rules", [])
        if r.get("status") == "active" and (not r.get("zones") or regions & set(r["zones"]))
    ]
    return {"ok": not unresolved, "unresolved": unresolved, "rules": rules, "result": draft}


def term_candidates(ws: Workspace, top: int = 300, min_count: int = 3) -> dict:
    """Words and phrases the book repeats and the reference does not cover yet, for step 3.

    The publisher's index is the strongest signal: the author already chose those terms.
    """
    units = ws.units()
    concepts = ws.concepts()
    covered = bible_mod.alias_pattern(
        [a for c in concepts.values() for a in c["en"]["aliases"]] or ["\u0000"]
    )
    stop = {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "by",
        "from",
        "as",
        "at",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "we",
        "you",
        "they",
        "our",
        "your",
        "their",
        "can",
        "may",
        "will",
        "would",
        "should",
        "could",
        "not",
        "no",
        "but",
        "if",
        "then",
        "than",
        "so",
        "such",
        "which",
        "who",
        "what",
        "when",
        "where",
        "how",
        "why",
        "also",
        "more",
        "most",
        "very",
        "into",
        "over",
        "under",
        "about",
        "using",
        "use",
        "used",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "there",
        "here",
        "each",
        "other",
        "some",
        "any",
        "all",
        "both",
        "one",
        "two",
        "new",
        "like",
        "just",
        "only",
        "many",
        "much",
        "well",
        "get",
        "make",
        "see",
        "let",
        "us",
        "however",
        "example",
        "chapter",
        "figure",
    }
    counts: dict[str, dict] = {}
    index_terms = []
    for unit in units:
        text = plain(unit["src"]["text"])
        if unit["region"] == "index":
            if unit["class"] == "translate" and not covered.search(text):
                index_terms.append(text)
            continue
        if unit["class"] != "translate" or unit["region"] not in TRANSLATABLE:
            continue
        rest = covered.sub(" | ", text)
        for chunk in re.split(r"[^\w\-/ ]+|\s\|\s", rest):
            words = re.findall(r"[A-Za-z][\w\-/]*", chunk)
            for size in (1, 2, 3):
                for i in range(len(words) - size + 1):
                    gram = words[i : i + size]
                    low = [w.lower() for w in gram]
                    if low[0] in stop or low[-1] in stop:
                        continue
                    if size == 1 and not (
                        re.search(r"[A-Z].*[A-Z]|\d", gram[0]) or len(low[0]) > 6
                    ):
                        continue
                    row = counts.setdefault(
                        " ".join(low), {"count": 0, "chapters": set(), "example": unit["unit_id"]}
                    )
                    row["count"] += 1
                    row["chapters"].add(unit["chapter"])
    ranked = sorted(
        (
            {
                "term": k,
                "count": v["count"],
                "chapters": len(v["chapters"]),
                "example": v["example"],
            }
            for k, v in counts.items()
            if v["count"] >= min_count
        ),
        key=lambda x: (-x["chapters"], -x["count"]),
    )
    return {"index_terms": sorted(set(index_terms)), "frequent": ranked[:top], "total": len(ranked)}


def normalize_terms(uid: str, terms) -> list[dict]:
    """The short form: {"C-1": ["веса", "~перефраз"]}, one entry per occurrence of that concept.

    Occurrence IDs and ordinals follow from the order, so the agent cites words, not addresses.
    A leading "~" marks a paraphrase, which goes to the meaning check.
    """
    if not isinstance(terms, dict):
        return terms or []
    out: list[dict] = []
    seen: dict[str, int] = {}
    for cid, forms in terms.items():
        for k, form in enumerate(forms, start=1):
            entry = {"occ": [f"occ-{uid}-{cid}-{k}"], "c": cid, "ru": form.lstrip("~")}
            if form.startswith("~"):
                entry["paraphrase"] = True
            key = entry["ru"].lower()
            seen[key] = seen.get(key, 0) + 1
            entry["nth"] = seen[key]
            out.append(entry)
    return out


def validate_result(payload: dict, manifest: dict, units_by_id, concepts) -> list[dict]:
    problems: list[dict] = []

    def problem(uid, check, detail):
        problems.append({"unit": uid, "check": check, "detail": detail})

    ids = [u.get("id") for u in payload.get("units", [])]
    expected = manifest["unit_ids"]
    for uid in sorted(set(expected) - set(ids)):
        problem(uid, "R1", "unit has no result")
    for uid in sorted(set(ids) - set(expected)):
        problem(uid, "R2", "result for a unit outside this package")
    if len(ids) != len(set(ids)):
        problem(None, "R2", "duplicate unit in result")
    for item in payload.get("units", []):
        uid = item.get("id")
        unit = units_by_id.get(uid)
        if unit is None or uid not in expected:
            continue
        tgt = item.get("tgt", "")
        if not plain(tgt).strip() and plain(unit["src"]["text"]).strip():
            problem(uid, "E2", "empty translation")
            continue
        if not echo_matches(item.get("src_echo", ""), unit["src"]["text"]):
            problem(uid, "ECHO", f"src_echo does not match: {item.get('src_echo')!r}")
        for span_problem in check_spans(unit.get("spans", {}), tgt):
            problem(uid, "S1", span_problem)
        if re.search(r"[A-Za-z][А-Яа-яЁё]|[А-Яа-яЁё][A-Za-z]", plain(tgt)):
            for word in re.findall(
                r"\b\w*(?:[A-Za-z][А-Яа-яЁё]|[А-Яа-яЁё][A-Za-z])\w*\b", plain(tgt)
            ):
                problem(uid, "G3", f"mixed alphabets: {word}")
        problems += _check_terms(item, unit, tgt, concepts)
    return problems


def _check_terms(item: dict, unit: dict, tgt: str, concepts) -> list[dict]:
    """Every EN occurrence is covered; each cited Russian span is a real form of that concept."""
    out: list[dict] = []
    uid = unit["unit_id"]
    occurrences = bible_mod.find_occurrences(unit["src"]["text"], concepts)
    known = {f"occ-{uid}-{o['n']}": o for o in occurrences}
    covered: set[str] = set()
    lowered = plain(tgt).lower()
    for term in item.get("terms", []):
        occ_ids = term.get("occ", [])
        cid = term.get("c")
        concept = concepts.get(cid)
        if concept is None:
            out.append({"unit": uid, "check": "T2", "detail": f"unknown concept {cid}"})
            continue
        if len(occ_ids) > 1 and not term.get("basis"):
            out.append(
                {
                    "unit": uid,
                    "check": "T1",
                    "detail": f"{occ_ids} share one Russian span without basis",
                }
            )
        for occ in occ_ids:
            if occ not in known:
                out.append({"unit": uid, "check": "T1", "detail": f"unknown occurrence {occ}"})
                continue
            covered.add(occ)
            if cid not in known[occ]["concepts"]:
                out.append(
                    {"unit": uid, "check": "T5", "detail": f"{occ} does not belong to {cid}"}
                )
        ru = (term.get("ru") or "").lower()
        if term.get("paraphrase"):
            continue  # an explicit paraphrase goes to the meaning check, not to a form match
        n = count_word(lowered, ru)
        if n == 0:
            out.append({"unit": uid, "check": "T2", "detail": f"quote not in translation: {ru!r}"})
            continue
        if n > 1 and not term.get("nth"):
            out.append({"unit": uid, "check": "T2", "detail": f"{ru!r} occurs {n} times: give nth"})
        policy = concept["rendering_policy"]
        if policy in ("keep_latin", "keep_latin_with_gloss"):
            if not any(a.lower() in ru for a in concept["en"]["aliases"]) and ru not in _all_forms(
                concept
            ):
                out.append(
                    {"unit": uid, "check": "T2", "detail": f"{ru!r} must keep the Latin name"}
                )
        elif ru not in _all_forms(concept):
            out.append(
                {"unit": uid, "check": "T2", "detail": f"{ru!r} is not a known form of {cid}"}
            )
        if ru in {f.lower() for f in concept.get("forbidden", [])}:
            out.append({"unit": uid, "check": "T4", "detail": f"forbidden variant {ru!r}"})
    for occ, data in known.items():
        if occ in covered:
            continue
        polysemous = len(data["concepts"]) > 1 or any(
            concepts[c].get("polysemy") for c in data["concepts"]
        )
        check = "T5" if polysemous else "T1"
        out.append({"unit": uid, "check": check, "detail": f"{occ} ({data['en']}) is not covered"})
    return out


def submit(ws: Workspace, payload: dict) -> dict:
    """Validate and accept. The same result twice is a no-op; a stale one is refused."""
    batch_id = payload.get("batch")
    path = ws.batches / f"{batch_id}.json"
    if not path.exists():
        raise SubmitRejected([{"unit": None, "check": "R2", "detail": f"unknown batch {batch_id}"}])
    manifest = json.loads(path.read_text("utf-8"))["manifest"]
    units_by_id = {u["unit_id"]: u for u in ws.units()}
    concepts = ws.concepts()
    for item in payload.get("units", []):
        item["terms"] = normalize_terms(item.get("id"), item.get("terms"))
    result_hash = digest(payload)
    target = ws.results / batch_id / f"{result_hash}.json"
    log = read_jsonl(ws.results / "accepted.jsonl")
    if any(r["batch"] == batch_id and r["hash"] == result_hash for r in log):
        return {"ok": True, "batch": batch_id, "hash": result_hash, "already_accepted": True}
    doc, voice, notes = ws.passport(), ws.voice(), ws.notes()
    drift = []
    for uid in manifest["unit_ids"]:
        now = store.translation_fp(
            units_by_id[uid],
            manifest["deps"][uid],
            ws,
            concepts=concepts,
            doc=doc,
            voice=voice,
            notes=notes,
        )
        if now != manifest["fingerprints"][uid]:
            drift.append(
                {"unit": uid, "check": "Q1", "detail": "decisions changed since the package"}
            )
    if drift:
        raise SubmitRejected(drift)
    problems = validate_result(payload, manifest, units_by_id, concepts)
    if problems:
        raise SubmitRejected(problems)
    write_atomic(target, json.dumps(payload, ensure_ascii=False, indent=1))
    log.append(
        {
            "batch": batch_id,
            "hash": result_hash,
            "seq": len(log) + 1,
            "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }
    )
    write_jsonl(ws.results / "accepted.jsonl", log)  # written last: an interrupted accept is redone
    return {"ok": True, "batch": batch_id, "hash": result_hash, "already_accepted": False}


def status(ws: Workspace, pages: set[int] | None = None) -> dict:
    """Everything is recomputed from files; the manifest is never trusted as state."""
    units = ws.units()
    accepted = ws.accepted()
    concepts, doc, voice, notes = ws.concepts(), ws.passport(), ws.voice(), ws.notes()
    selected = _select(units, pages)
    todo, stale = [], []
    for unit in selected:
        uid = unit["unit_id"]
        result = accepted.get(uid)
        if result is None:
            todo.append(uid)
            continue
        manifest = json.loads((ws.batches / f"{result['batch']}.json").read_text("utf-8"))[
            "manifest"
        ]
        deps = sorted(
            set(manifest["deps"].get(uid, []))
            | {
                c
                for o in bible_mod.find_occurrences(unit["src"]["text"], concepts)
                for c in o["concepts"]
            }
        )
        now = store.translation_fp(
            unit, deps, ws, concepts=concepts, doc=doc, voice=voice, notes=notes
        )
        # A revision through `bt edit` records what it was built from.
        built = result.get("fingerprint") or manifest["fingerprints"].get(uid)
        if now != built:
            stale.append(uid)
    open_questions = [q for q in ws.questions() if q.get("status") == "open" and q.get("blocking")]
    return {
        "selected": len(selected),
        "translated": len(selected) - len(todo),
        "todo": todo[:50],
        "todo_count": len(todo),
        "stale": stale[:50],
        "stale_count": len(stale),
        "blocking_questions": len(open_questions),
        "chapters": _chapters(units, accepted),
    }
