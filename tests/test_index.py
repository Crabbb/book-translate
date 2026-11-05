"""The rebuilt index: binding to a place, decisions, order, ranges (spec 7.5)."""

from __future__ import annotations

import json
import os
import re

import pytest
from bt import batch, bible, indexer, qa, store, typeset


def unit(uid, text, page, region="body"):
    return {
        "unit_id": uid,
        "chapter": 1,
        "page": page,
        "region": region,
        "class": "translate",
        "src": {"text": text, "hash": store.digest(text)},
        "spans": {},
    }


def entry(uid, term, printed, pages, main=True):
    targets = [{"page": p, "label": str(p)} for p in pages]
    return {"unit_id": uid, "term": term, "printed": printed, "targets": targets, "main": main}


@pytest.fixture
def ws(tmp_path):
    w = store.Workspace(tmp_path)
    units = [
        unit("u-1", "Embedding models map text to vectors.", 10),
        unit("u-2", "A vector store keeps the vectors and embedding models again.", 10),
        unit("u-3", "Chunking splits long text.", 20),
        unit("u-4", "Nothing relevant here.", 30),
        unit("i-0", "Index", 90, "index"),
        unit("i-1", "embedding models", 90, "index"),
        unit("i-2", "chunking", 90, "index"),
        unit("i-3", "strategies", 90, "index"),
        unit("i-4", "rare term", 90, "index"),
    ]
    store.write_jsonl(w.ir / "units.jsonl", units)
    store.write_jsonl(
        w.ir / "index.jsonl",
        [
            entry("i-0", "Index", [], []),
            entry("i-1", "embedding models", ["10"], [10]),
            entry("i-2", "chunking, overview", ["20-30"], [20, 30]),
            entry("i-3", "strategies", ["20"], [20], main=False),
            entry("i-4", "rare term", ["30"], [30]),
        ],
    )
    return w


def rows(ws):
    return {
        (r["unit"], r["target"]): r for r in store.read_jsonl(ws.bible / "index_bindings.jsonl")
    }


def test_search_key_drops_parentheses_and_qualifiers():
    assert (
        indexer.search_key("generative adversarial networks (GANs)")
        == "generative adversarial networks"
    )
    assert indexer.search_key("data, securing in") == "data"


def test_range_is_one_printed_reference_with_two_targets():
    e = entry("x", "t", ["52-54", "56"], [52, 54, 56])
    assert indexer.refs_of(e) == [[0, 1], [2]]


def test_unambiguous_place_is_bound_and_others_wait(ws):
    report = indexer.bind(ws)
    r = rows(ws)
    assert r[("i-2", 0)]["status"] == "auto"
    assert r[("i-2", 0)]["bind"] == "u-3"
    assert r[("i-1", 0)]["status"] == "open"  # the term is twice on this page
    assert r[("i-1", 0)]["kind"] == "many"
    assert r[("i-4", 0)]["kind"] == "not_found"
    assert report["open"] >= 2


def test_several_places_on_one_new_page_need_no_decision(ws):
    pos = {"u-1": {"page": 5}, "u-2": {"page": 5}}
    indexer.bind(ws, pos)
    assert rows(ws)[("i-1", 0)]["kind"] == "same_page"


def test_several_places_on_different_new_pages_still_wait(ws):
    pos = {"u-1": {"page": 5}, "u-2": {"page": 6}}
    indexer.bind(ws, pos)
    assert rows(ws)[("i-1", 0)]["status"] == "open"


def test_decision_must_stay_on_the_target_page_and_has_a_reason(ws):
    indexer.bind(ws)
    good = {"unit": "i-1", "target": 0, "bind": "u-1", "basis": "определение термина здесь"}
    assert not indexer.decide(ws, [{**good, "bind": "u-3"}])["ok"]
    assert not indexer.decide(ws, [{**good, "basis": " "}])["ok"]
    assert indexer.decide(ws, [good])["ok"]
    assert rows(ws)[("i-1", 0)]["status"] == "decided"


def test_decision_survives_another_bind(ws):
    indexer.bind(ws)
    indexer.decide(ws, [{"unit": "i-1", "target": 0, "bind": "u-2", "basis": "основное место"}])
    indexer.bind(ws)
    assert rows(ws)[("i-1", 0)]["bind"] == "u-2"


def translate(ws, *pairs):
    store.write_jsonl(ws.batches / "index.jsonl", [{"batch": "b1", "units": len(pairs)}])
    store.write_atomic(
        ws.batches / "b1.json",
        json.dumps(
            {
                "manifest": {"unit_ids": [p[0] for p in pairs], "deps": {}, "fingerprints": {}},
                "package": {},
            }
        ),
    )
    payload = {"batch": "b1", "units": [{"id": a, "tgt": b, "terms": []} for a, b in pairs]}
    h = store.digest(payload)
    store.write_atomic(ws.results / "b1" / f"{h}.json", json.dumps(payload, ensure_ascii=False))
    store.write_jsonl(ws.results / "accepted.jsonl", [{"batch": "b1", "hash": h, "seq": 1}])


def test_two_entries_with_one_russian_term_wait_for_a_decision(ws):
    translate(ws, ("i-1", "эмбеддинги"), ("i-2", "Эмбеддинги"))
    assert indexer.same_russian(ws)[0]["units"] == ["i-1", "i-2"]
    assert not indexer.decide_merge(ws, [{"units": ["i-1", "i-2"], "merge": True, "basis": ""}])[
        "ok"
    ]
    indexer.decide_merge(ws, [{"units": ["i-1", "i-2"], "merge": True, "basis": "один концепт"}])
    assert indexer.same_russian(ws) == []
    mains = indexer.layout(ws)["mains"]
    assert [m["unit"]["unit_id"] for m in mains].count("i-2") == 0
    host = next(m for m in mains if m["unit"]["unit_id"] == "i-1")
    assert len(host["refs"]) == 2


def test_russian_comes_first_and_yo_sorts_as_ye():
    terms = ["zero-shot", "ёмкость", "агент", "Azure", "елка"]
    assert sorted(terms, key=indexer.sort_key) == ["агент", "елка", "ёмкость", "Azure", "zero-shot"]


def test_sub_entry_stays_under_its_main(ws):
    layout = indexer.layout(ws)
    chunking = next(m for m in layout["mains"] if m["unit"]["unit_id"] == "i-2")
    assert [s["unit"]["unit_id"] for s in chunking["subs"]] == ["i-3"]
    assert layout["title"]["unit_id"] == "i-0"


def test_final_build_refuses_open_references(ws):
    (ws.ir / "dests.json").write_text("{}", encoding="utf-8")
    store.write_jsonl(ws.ir / "objects.jsonl", [])
    indexer.bind(ws)
    setter = typeset.Typesetter(ws.root, lang="ru")
    with pytest.raises(typeset.BuildError):
        setter.generate()
    assert "#index-entry" in setter.generate(draft=True)


def test_bound_reference_points_at_the_place_not_the_page(ws):
    (ws.ir / "dests.json").write_text("{}", encoding="utf-8")
    store.write_jsonl(ws.ir / "objects.jsonl", [])
    indexer.bind(ws)
    indexer.decide(
        ws,
        [
            {"unit": "i-1", "target": 0, "bind": "u-2", "basis": "основное место"},
            {"unit": "i-3", "target": 0, "bind": "u-3", "basis": "стратегии описаны здесь"},
            {"unit": "i-2", "target": 1, "bind": "u-4", "basis": "конец диапазона"},
            {"unit": "i-4", "target": 0, "bind": "u-4", "basis": "единственный текст страницы"},
        ],
    )
    source = typeset.Typesetter(ws.root, lang="ru").generate()
    assert '(label("ix-u-2"),)' in source
    assert '(label("ix-u-3"), label("ix-u-4"),)' in source


@pytest.mark.skipif(not os.environ.get("BT_TYPST"), reason="needs BT_TYPST and BT_FONTS")
def test_reference_to_a_repeated_table_header_compiles(ws):
    def cell(uid, row):
        return {
            **unit(uid, "Header" if row == 0 else "Cell text", 40, "table"),
            "group": "tab-001",
            "cell": {"row": row, "col": 0, "header": row == 0, "box": [10.0, 300.0]},
        }

    table = [cell(f"t-{row}", row) for row in range(90)]  # taller than a page
    index = [unit("i-0", "Index", 90, "index"), unit("i-1", "header", 90, "index")]
    store.write_jsonl(ws.ir / "units.jsonl", [*table, *index])
    store.write_jsonl(
        ws.ir / "index.jsonl",
        [entry("i-0", "Index", [], []), entry("i-1", "header", ["40"], [40])],
    )
    (ws.ir / "dests.json").write_text("{}", encoding="utf-8")
    store.write_jsonl(ws.ir / "objects.jsonl", [])
    indexer.bind(ws)
    assert indexer.blockers(ws)["open"] == 0
    source = typeset.Typesetter(ws.root, lang="en").generate()
    typeset.compile_book(ws.root, source, "header")
    places = json.loads((ws.root / "build" / "header.positions.json").read_text(encoding="utf-8"))
    assert len([p for p in places if p["id"] == "t-0"]) > 1


@pytest.mark.skipif(not os.environ.get("BT_TYPST"), reason="needs BT_TYPST and BT_FONTS")
def test_references_landing_on_one_new_page_print_one_number(ws):
    pdfplumber = pytest.importorskip("pdfplumber")
    store.write_jsonl(
        ws.ir / "units.jsonl",
        [
            unit("u-1", "Alpha here.", 10),
            unit("u-2", "Alpha there.", 11),
            unit("i-0", "Index", 90, "index"),
            unit("i-1", "alpha", 90, "index"),
            unit("i-2", "alphabet", 90, "index"),
        ],
    )
    store.write_jsonl(
        ws.ir / "index.jsonl",
        [
            entry("i-0", "Index", [], []),
            entry("i-1", "alpha", ["10", "11"], [10, 11]),
            entry("i-2", "alphabet", ["10-11"], [10, 11]),
        ],
    )
    (ws.ir / "dests.json").write_text("{}", encoding="utf-8")
    store.write_jsonl(ws.ir / "objects.jsonl", [])
    indexer.bind(ws)
    indexer.decide(
        ws,
        [
            {"unit": "i-2", "target": 0, "bind": "u-1", "basis": "начало"},
            {"unit": "i-2", "target": 1, "bind": "u-2", "basis": "конец"},
        ],
    )
    source = typeset.Typesetter(ws.root, lang="en").generate()
    pdf = typeset.compile_book(ws.root, source, "same")["pdf"]
    with pdfplumber.open(pdf) as doc:
        text = doc.pages[-1].extract_text()
    assert re.search(r"^alpha \d+$", text, re.MULTILINE), text
    assert re.search(r"^alphabet \d+$", text, re.MULTILINE), text


def test_partial_build_does_not_take_an_entry_for_the_title(ws):
    store.write_jsonl(
        ws.ir / "index.jsonl",
        [*store.read_jsonl(ws.ir / "index.jsonl")[:4], entry("i-4", "rare term", [], [])],
    )
    layout = indexer.layout(ws, lambda u: u["unit_id"] == "i-4")
    assert layout["title"] is None
    assert [m["unit"]["unit_id"] for m in layout["mains"]] == ["i-4"]


def test_batches_do_not_ask_to_translate_letter_headings():
    units = [unit("a", "G", 90, "index"), unit("b", "Galileo", 90, "index")]
    units[0]["class"] = "service"
    assert [u["unit_id"] for u in batch._select(units, None)] == ["b"]


def test_a_chapter_selection_does_not_catch_a_neighbour_on_a_shared_page():
    first, second = unit("a", "End of one.", 186), unit("b", "Start of two.", 186)
    second["chapter"] = 2
    assert [u["unit_id"] for u in batch._select([first, second], batch.Chapters({1}))] == ["a"]
    assert len(batch._select([first, second], {186})) == 2


def test_status_reports_progress_by_chapter(ws):
    translate(ws, ("u-1", "Перевод."))
    row = batch.status(ws)["chapters"][0]
    assert (row["chapter"], row["first_page"], row["translated"]) == (1, 10, 1)
    assert row["units"] > 1


def test_a_word_that_can_be_an_adjective_is_not_the_head_before_a_noun():
    forms = bible.generate_forms("рабочий процесс")["forms"]
    assert "рабочих процессов" in forms
    assert "рабочих процесс" not in forms
    assert "карточек модели" in bible.generate_forms("карточка модели")["forms"]


def test_a_unit_of_only_a_link_has_nothing_to_translate():
    assert batch.plain("⟦a1⟧").strip() == ""
    manifest = {"unit_ids": ["a"]}
    linked = {"a": {**unit("a", "⟦a1⟧", 1), "spans": {}}}
    item = {"id": "a", "tgt": "⟦a1⟧", "terms": []}
    problems = batch.validate_result({"units": [item]}, manifest, linked, {})
    assert not [p for p in problems if p["check"] == "E2"]


def test_a_book_rule_with_a_pattern_is_checked_by_the_code():
    rule = {
        "id": "R-1",
        "zones": ["caption"],
        "check": {"kind": "pattern", "pattern": r"Рис\. \d"},
    }
    caption = {**unit("c", "Figure 6.1", 1), "region": "caption"}
    assert qa.check_book_rules(caption, {"tgt": "Рисунок 6.1"}, [rule])[0]["class"] == "B"
    assert qa.check_book_rules(caption, {"tgt": "Рис. 6.1"}, [rule]) == []
    assert qa.check_book_rules(unit("b", "Text", 1), {"tgt": "Текст"}, [rule]) == []


CONCEPT = {
    "id": "C-1",
    "status": "accepted",
    "sense": "s",
    "en": {"aliases": ["agent", "agents"]},
    "rendering_policy": "translate",
    "variants": [{"kind": "preferred", "text": "агент"}],
    "origin": "agent",
    "reason": "r",
    "admission_reason": "a",
}


def test_cite_quotes_forms_and_keeps_open_what_has_no_form(ws):
    store.write_jsonl(ws.ir / "units.jsonl", [unit("u", "The agent helps. Agents plan.", 5)])
    bible.add(ws, [CONCEPT], "agent")
    draft = {"batch": "b", "units": [{"id": "u", "tgt": "Агент помогает. Они планируют."}]}
    report = batch.cite(ws, json.loads(json.dumps(draft)))
    assert not report["ok"]
    assert report["unresolved"][0]["needs"] == 1
    draft["units"][0]["paraphrases"] = {"C-1": ["они"]}
    report = batch.cite(ws, draft)
    assert report["ok"]
    item = report["result"]["units"][0]
    assert item["terms"] == {"C-1": ["агент", "~они"]}
    assert item["src_echo"] == "The agent helps."
    assert "paraphrases" not in item


def test_cite_always_shows_the_rules_of_the_units_in_the_draft(ws):
    ws.bible.mkdir(parents=True, exist_ok=True)
    (ws.bible / "passport.yaml").write_text(
        "rules:\n- {id: R-1, status: active, zones: [body], do: x}\n"
        "- {id: R-2, status: active, zones: [caption], do: y}\n",
        encoding="utf-8",
    )
    report = batch.cite(ws, {"units": [{"id": "u-1", "tgt": "Текст."}]})
    assert [r["id"] for r in report["rules"]] == ["R-1"]


def test_term_candidates_skip_what_the_reference_covers(ws):
    store.write_jsonl(
        ws.ir / "units.jsonl",
        [unit(f"u-{i}", "The agent uses a vector store and LangChain.", 5) for i in range(4)]
        + [unit("i-1", "vector store", 90, "index"), unit("i-2", "agent", 90, "index")],
    )
    bible.add(ws, [CONCEPT], "agent")
    report = batch.term_candidates(ws)
    assert report["index_terms"] == ["vector store"]
    terms = [row["term"] for row in report["frequent"]]
    assert "vector store" in terms and "langchain" in terms
    assert "agent" not in terms


def test_an_adjective_before_a_fixed_noun_inflects_alone():
    forms = bible.generate_forms("диалоговый ИИ", None, ["ИИ"])["forms"]
    assert {"диалоговый ии", "диалогового ии", "диалоговым ии", "диалоговом ии"} <= set(forms)


def test_cite_does_not_ask_for_russian_in_a_title_kept_in_the_source_language(ws):
    store.write_jsonl(ws.ir / "units.jsonl", [unit("u", "The Agent Handbook: agents at work", 5)])
    bible.add(ws, [CONCEPT], "agent")
    item = {"id": "u", "tgt": "The Agent Handbook: agents at work", "kept_original": "название"}
    report = batch.cite(ws, {"units": [item]})
    assert report["ok"]
    assert all(t.startswith("~") for t in report["result"]["units"][0]["terms"]["C-1"])


def test_first_mention_may_take_any_case_but_needs_the_gloss():
    concept = {"variants": [{"forms": ["опора на источники", "опоре на источники"]}]}
    mention = "опора на источники (grounding)"
    assert qa._has_mention(mention, {"tgt": "к опоре на источники (grounding) в данных"}, concept)
    assert not qa._has_mention(mention, {"tgt": "к опоре на источники в данных"}, concept)
    assert not qa._has_mention(mention, {"tgt": "к чему-то (grounding) в данных"}, concept)


def test_picture_anchored_to_an_index_entry_is_not_lost(tmp_path, monkeypatch):
    ir = tmp_path / "ir"
    entry = {
        "unit_id": "u-1",
        "chapter": 1,
        "page": 1,
        "region": "index",
        "class": "translate",
        "src": {"text": "term", "hash": "h"},
        "spans": {},
    }
    figure = {"id": "fig-1", "kind": "figure", "after": "u-1", "page": 1, "bbox": {"x0": 0, "x1": 100, "y0": 0, "y1": 10}}
    store.write_jsonl(ir / "units.jsonl", [entry])
    store.write_jsonl(ir / "objects.jsonl", [figure])
    store.write_jsonl(ir / "index.jsonl", [])
    (ir / "dests.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(indexer, "layout", lambda ws, keep: {"title": None, "mains": []})
    assert "fig-1" in typeset.Typesetter(tmp_path, lang="ru").generate(draft=True)
