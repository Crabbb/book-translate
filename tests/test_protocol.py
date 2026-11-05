"""The work protocol and freshness rules from spec sections 5, 6 and 8.5, on a tiny fake book."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bt import batch, bible, qa, revision, store
from hypothesis import given
from hypothesis import strategies as st


def unit(uid, text, region="body", page=10, spans=None, **extra):
    return {
        "unit_id": uid,
        "chapter": 1,
        "page": page,
        "region": region,
        "class": "translate",
        "origin": "tree",
        "src": {"text": text, "hash": store.digest(text)},
        "spans": spans or {},
        **extra,
    }


WEIGHTS = {
    "id": "C-1",
    "status": "accepted",
    "sense": "параметры нейросети",
    "scope": "book",
    "en": {"aliases": ["weights", "weight"]},
    "rendering_policy": "translate",
    "variants": [{"kind": "preferred", "text": "вес"}],
    "reason": "устойчивый термин",
}
EM = {"1": {"kind": "pair", "role": "em"}}
CODE = {"a1": {"kind": "atom", "role": "inline_code", "data": {"text": "x"}, "data_hash": "h"}}


@pytest.fixture
def ws(tmp_path) -> store.Workspace:
    w = store.Workspace(tmp_path)
    store.write_jsonl(
        w.ir / "units.jsonl",
        [
            unit("u-1", "Set the weights before training."),
            unit("u-2", "Do ⟦1⟧not⟦/1⟧ run ⟦a1⟧ on production.", spans={**EM, **CODE}),
        ],
    )
    bible.add(w, [WEIGHTS])
    return w


def result(batch_id: str, **over) -> dict:
    units = [
        {
            "id": "u-1",
            "src_echo": "Set the weights",
            "tgt": "Задайте веса до обучения.",
            "terms": [{"occ": ["occ-u-1-C-1-1"], "c": "C-1", "ru": "веса", "nth": 1}],
        },
        {
            "id": "u-2",
            "src_echo": "Do not run",
            "tgt": "⟦1⟧Не⟦/1⟧ запускайте ⟦a1⟧ на боевых данных.",
            "terms": [],
        },
    ]
    return {"batch": batch_id, "units": units, **over}


def take(ws) -> str:
    return batch.next_batch(ws)["batch"]


def checked(uid: str) -> dict:
    return {"unit": uid, "en_propositions": ["утверждение"], "ru_coverage": "на месте"}


def window(*uids: str) -> dict:
    return {"units": list(uids), "observations": "логика абзаца держится"}


def passes(ws) -> None:
    assert qa.record_evidence(ws, "check", [checked("u-1"), checked("u-2")])["ok"]
    assert qa.record_evidence(ws, "read-target", [window("u-1", "u-2")])["ok"]


# --- Spans ------------------------------------------------------------------------------------


def test_whole_span_may_move_inside_the_unit():
    spans = {"1": {"kind": "pair"}, "a1": {"kind": "atom"}}
    assert batch.check_spans(spans, "⟦a1⟧ потом ⟦1⟧слово⟦/1⟧") == []


@pytest.mark.parametrize(
    "broken",
    [
        "⟦1⟧слово",  # unclosed
        "слово⟦/1⟧ ⟦a1⟧",  # closed before open
        "⟦1⟧⟦1⟧слово⟦/1⟧⟦/1⟧ ⟦a1⟧",  # duplicated
        "⟦1⟧слово⟦/1⟧",  # atom lost
        "⟦1⟧слово⟦/1⟧ ⟦a1⟧ ⟦a2⟧",  # invented span
        "⟦1⟧слово⟦/1⟧ ⟦/a1⟧",  # atom closed
    ],
)
def test_damaged_spans_are_refused(broken):
    spans = {"1": {"kind": "pair"}, "a1": {"kind": "atom"}}
    assert batch.check_spans(spans, broken)


@given(st.permutations(["⟦1⟧а⟦/1⟧", "⟦2⟧б⟦/2⟧", "⟦a1⟧", "в"]))
def test_any_order_of_whole_spans_is_valid(parts):
    spans = {"1": {"kind": "pair"}, "2": {"kind": "pair"}, "a1": {"kind": "atom"}}
    assert batch.check_spans(spans, " ".join(parts)) == []


# --- Submit -----------------------------------------------------------------------------------


def test_same_result_twice_is_a_no_op(ws):
    b = take(ws)
    first = batch.submit(ws, result(b))
    second = batch.submit(ws, result(b))
    assert first["already_accepted"] is False
    assert second["already_accepted"] is True
    assert len(store.read_jsonl(ws.results / "accepted.jsonl")) == 1


def test_interrupted_accept_is_redone_not_trusted(ws):
    """A result file without its manifest row is not accepted: the row is written last."""
    b = take(ws)
    payload = result(b)
    target = ws.results / b / f"{store.digest(payload)}.json"
    store.write_atomic(target, json.dumps(payload))  # crash before the manifest row
    assert ws.accepted() == {}
    assert batch.submit(ws, payload)["already_accepted"] is False
    assert set(ws.accepted()) == {"u-1", "u-2"}


def test_manifest_row_without_file_returns_units_to_the_queue(ws):
    b = take(ws)
    payload = result(b)
    batch.submit(ws, payload)
    (ws.results / b / f"{store.digest(payload)}.json").unlink()
    assert batch.status(ws)["todo_count"] == 2


@pytest.mark.parametrize(
    "corrupt, check",
    [
        (lambda p: p["units"].pop(), "R1"),
        (lambda p: p["units"][0].update(id="u-9"), "R2"),
        (lambda p: p["units"][0].update(src_echo="Totally different words"), "ECHO"),
        (lambda p: p["units"][0].update(tgt="   "), "E2"),
        (lambda p: p["units"][1].update(tgt="Не запускайте на данных."), "S1"),
        (lambda p: p["units"][0].update(terms=[]), "T1"),
        (lambda p: p["units"][0]["terms"][0].update(ru="вага"), "T2"),
        (lambda p: p["units"][0].update(tgt="Задайте вecа до обучения."), "G3"),
    ],
)
def test_damaged_results_are_refused_with_an_address(ws, corrupt, check):
    b = take(ws)
    payload = result(b)
    corrupt(payload)
    with pytest.raises(batch.SubmitRejected) as caught:
        batch.submit(ws, payload)
    assert check in {p["check"] for p in caught.value.problems}
    assert ws.accepted() == {}


def test_echo_ignores_apostrophes_and_hyphens_but_not_a_shift():
    assert batch.echo_matches("Let's begin with", "Let\u2019s begin with text generation.")
    assert batch.echo_matches("Vendor Lock In", "Vendor Lock-In")
    assert not batch.echo_matches("Next paragraph", "Vendor Lock-In")
    assert not batch.echo_matches("", "Vendor Lock-In")
    assert batch.echo_matches("“””", "“””")
    assert not batch.echo_matches("###", "“””")


def test_a_form_inside_a_longer_word_is_not_an_occurrence():
    assert batch.count_word("после бенчмаркинга есть бенчмарки", "бенчмарки") == 1


def test_short_term_form_keeps_the_same_strictness(ws):
    b = take(ws)
    payload = result(b)
    payload["units"][0]["terms"] = {"C-1": ["веса"]}
    assert batch.submit(ws, payload)["ok"]
    assert ws.accepted()["u-1"]["terms"][0]["occ"] == ["occ-u-1-C-1-1"]


@pytest.mark.parametrize(
    "terms",
    [{}, {"C-1": ["веса", "веса"]}, {"C-1": ["вага"]}, {"C-9": ["веса"]}],
)
def test_short_term_form_refuses_what_the_long_form_refuses(ws, terms):
    b = take(ws)
    payload = result(b)
    payload["units"][0]["terms"] = terms
    with pytest.raises(batch.SubmitRejected):
        batch.submit(ws, payload)


def test_paraphrase_mark_in_short_form():
    entry = batch.normalize_terms("u", {"C-1": ["~их"]})[0]
    assert entry["paraphrase"] is True and entry["ru"] == "их"


def test_open_package_is_returned_not_issued_again(ws):
    first = batch.next_batch(ws)
    second = batch.next_batch(ws)
    assert second["batch"] == first["batch"]
    assert second["reused"] is True
    assert len(store.read_jsonl(ws.batches / "index.jsonl")) == 1


def test_changed_decisions_give_a_new_package_not_the_old_one(ws):
    first = batch.next_batch(ws)["batch"]
    forms = [{"kind": "preferred", "text": "вес", "forms": ["вес", "веса"], "parse": "confirmed"}]
    bible.add(ws, [{**WEIGHTS, "variants": forms}])
    assert batch.next_batch(ws)["batch"] != first


def test_second_writer_is_refused(ws):
    with ws.lock(), pytest.raises(store.WorkspaceLocked), ws.lock():
        pass


def test_result_for_changed_decisions_is_refused(ws):
    b = take(ws)
    bible.add(ws, [{**WEIGHTS, "applies_when": "только про нейросети"}])
    with pytest.raises(batch.SubmitRejected) as caught:
        batch.submit(ws, result(b))
    assert {p["check"] for p in caught.value.problems} == {"Q1"}


# --- Freshness (spec 8.5) ---------------------------------------------------------------------


def accepted_ws(ws) -> store.Workspace:
    batch.submit(ws, result(take(ws)))
    assert batch.status(ws)["stale_count"] == 0
    return ws


def test_commentary_change_does_not_make_the_translation_stale(ws):
    accepted_ws(ws)
    bible.add(
        ws, [{**WEIGHTS, "reason": "уточнённое объяснение", "examples": {"positive": ["u-1"]}}]
    )
    assert batch.status(ws)["stale_count"] == 0


@pytest.mark.parametrize(
    "change",
    [
        {"applies_when": "только про нейросети"},
        {"not_when": ["weight of a package"]},
        {"variants": [{"kind": "preferred", "text": "весовой коэффициент"}]},
        {"sense": "другой смысл"},
        {"rendering_policy": "keep_latin"},
    ],
)
def test_normative_concept_change_makes_dependants_stale(ws, change):
    accepted_ws(ws)
    bible.add(ws, [{**WEIGHTS, **change}])
    assert batch.status(ws)["stale"] == ["u-1"]


def test_new_alias_finding_a_new_occurrence_makes_that_unit_stale(ws):
    accepted_ws(ws)
    bible.add(
        ws,
        [
            {
                "id": "C-2",
                "status": "accepted",
                "sense": "промышленная среда",
                "en": {"aliases": ["production"]},
                "rendering_policy": "translate",
                "variants": [{"kind": "preferred", "text": "продакшн"}],
                "reason": "новая запись",
            }
        ],
    )
    assert batch.status(ws)["stale"] == ["u-2"]


def passport(ws, modal="могут"):
    store.write_atomic(
        ws.bible / "passport.yaml",
        json.dumps(
            {
                "passport": {"function": "instrumental", "modal_map": {"may": modal}},
                "rules": [
                    {
                        "id": "R-1",
                        "status": "active",
                        "zones": ["body"],
                        "when": "обращение",
                        "do": "на вы",
                        "check": {"kind": "lint"},
                        "examples": {"good": ["пример"]},
                    }
                ],
            },
            ensure_ascii=False,
        ),
    )


def test_passport_change_makes_all_stale_and_survives_resume(ws):
    passport(ws)
    accepted_ws(ws)
    passport(ws, modal="могло бы")
    fresh = store.Workspace(ws.root)  # a new session re-reads only the files
    assert sorted(batch.status(fresh)["stale"]) == ["u-1", "u-2"]


def test_rule_example_change_does_not_touch_the_translation(ws):
    passport(ws)
    accepted_ws(ws)
    doc = ws.passport()
    doc["rules"][0]["examples"] = {"good": ["другой пример"]}
    store.write_atomic(ws.bible / "passport.yaml", json.dumps(doc, ensure_ascii=False))
    assert batch.status(ws)["stale_count"] == 0


def test_rule_check_change_drops_evidence_but_keeps_the_translation(ws):
    passport(ws)
    accepted_ws(ws)
    passes(ws)
    before = qa.chapter_gate(ws, None, "t")["missing_evidence"]
    assert before == {}
    doc = ws.passport()
    doc["rules"][0]["check"] = {"kind": "aligned_llm"}
    store.write_atomic(ws.bible / "passport.yaml", json.dumps(doc, ensure_ascii=False))
    assert batch.status(ws)["stale_count"] == 0
    assert qa.chapter_gate(ws, None, "t")["missing_evidence"] == {"check": 2, "read-target": 2}


def test_removing_an_exception_requires_the_check_again(ws):
    with_exception = {**WEIGHTS, "exceptions": [{"unit": "u-1", "reason": "авторский приём"}]}
    bible.add(ws, [with_exception])
    accepted_ws(ws)
    passes(ws)
    assert qa.chapter_gate(ws, None, "t")["missing_evidence"] == {}
    bible.add(ws, [WEIGHTS])
    assert batch.status(ws)["stale_count"] == 0
    assert qa.chapter_gate(ws, None, "t")["missing_evidence"]["check"] == 1


# --- Evidence and edits ------------------------------------------------------------------------


def test_a_bare_list_of_units_proves_nothing_and_is_refused(ws):
    """The first calibration passed the gate on exactly this, with three major errors in the text."""
    accepted_ws(ws)
    assert not qa.record_evidence(ws, "check", [{"unit": "u-1"}, {"unit": "u-2"}])["ok"]
    assert not qa.record_evidence(ws, "read-target", [{"units": ["u-1"], "observations": " "}])[
        "ok"
    ]
    assert qa.record_evidence(ws, "check", [checked("u-1"), checked("u-2")])["ok"]


def test_stale_unit_is_closed_by_an_edit_with_a_reason(ws):
    accepted_ws(ws)
    bible.add(ws, [{**WEIGHTS, "applies_when": "только про нейросети"}])
    assert batch.status(ws)["stale"] == ["u-1"]
    same = ws.accepted()["u-1"]["tgt"]
    assert not qa.apply_edit(ws, "revision", [{"id": "u-1", "tgt": same}])["ok"]
    assert qa.apply_edit(ws, "revision", [{"id": "u-1", "tgt": same, "reason": "смысл тот же"}])[
        "ok"
    ]
    assert batch.status(ws)["stale_count"] == 0


def test_conditional_variant_and_paraphrase_need_a_decision():
    concept = {
        **WEIGHTS,
        "variants": [
            {"kind": "preferred", "text": "вес", "forms": ["веса"]},
            {"kind": "allowed", "text": "код", "when": "об исходном коде", "forms": ["кодом"]},
        ],
    }
    u = unit("u", "weights")
    variant = qa.check_unit(
        u, {"tgt": "кодом", "terms": [{"c": "C-1", "ru": "кодом"}]}, {"C-1": concept}, []
    )
    assert next(f for f in variant if f["rule"] == "T3")["class"] == "C"
    para = qa.check_unit(
        u,
        {"tgt": "встраивание", "terms": [{"c": "C-1", "ru": "встраивание", "paraphrase": True}]},
        {"C-1": concept},
        [],
    )
    assert next(f for f in para if f["rule"] == "TP")["class"] == "C"


def test_any_text_change_voids_the_evidence_of_that_unit(ws):
    accepted_ws(ws)
    passes(ws)
    assert qa.chapter_gate(ws, None, "t")["missing_evidence"] == {}
    edit = {
        "id": "u-2",
        "tgt": "⟦1⟧Можно⟦/1⟧ запускать ⟦a1⟧ на боевых данных.",  # a meaning error, no number or term
        "reason": "редактура",
    }
    assert qa.apply_edit(ws, "edit", [edit])["ok"]
    gate = qa.chapter_gate(ws, None, "t")
    assert gate["verdict"] != "pass"
    assert gate["missing_evidence"] == {"check": 1, "read-target": 1}


def test_edit_cannot_break_spans_or_terms(ws):
    accepted_ws(ws)
    assert not qa.apply_edit(ws, "edit", [{"id": "u-2", "tgt": "Без разметки."}])["ok"]
    assert not qa.apply_edit(ws, "edit", [{"id": "u-1", "tgt": "Задайте параметры до обучения."}])[
        "ok"
    ]


def test_invented_quote_in_evidence_is_refused(ws):
    accepted_ws(ws)
    entry = {**checked("u-1"), "findings": [{"en": "this is not in the book", "severity": "major"}]}
    assert not qa.record_evidence(ws, "check", [entry])["ok"]


def test_confirmed_major_finding_blocks_the_gate(ws):
    accepted_ws(ws)
    bad = {**checked("u-1"), "findings": [{"en": "weights", "ru": "веса", "severity": "major"}]}
    qa.record_evidence(ws, "check", [bad, checked("u-2")])
    qa.record_evidence(ws, "read-target", [window("u-1", "u-2")])
    gate = qa.chapter_gate(ws, None, "t")
    assert gate["verdict"] == "blocked"


def test_open_blocking_question_blocks_the_gate(ws):
    accepted_ws(ws)
    store.write_jsonl(
        ws.bible / "questions.jsonl", [{"id": "Q", "status": "open", "blocking": True}]
    )
    assert batch.status(ws)["blocking_questions"] == 1
    assert qa.chapter_gate(ws, None, "t")["verdict"] == "blocked"


def test_disposition_does_not_survive_a_text_change(ws):
    accepted_ws(ws)
    candidate = {"key": "k1", "unit": "u-1", "status": "rejected", "basis": "приём автора"}
    assert qa.decide(ws, [candidate])["ok"]
    rows = store.read_jsonl(ws.reports / "dispositions.jsonl")
    assert rows[0]["tgt_hash"] == store.digest(ws.accepted()["u-1"]["tgt"])
    assert not qa.decide(ws, [{"key": "k2", "unit": "u-1", "status": "rejected"}])["ok"]


# --- Revision (spec 8.5) -----------------------------------------------------------------------


def test_dry_run_shows_reach_and_changes_nothing(ws):
    accepted_ws(ws)
    new = {**WEIGHTS, "applies_when": "только про нейросети"}
    report = revision.change(ws, new, "уточнение", dry_run=True)
    assert report["units"] == ["u-1"]
    assert report["normative_fields"] == ["applies_when"]
    assert batch.status(ws)["stale_count"] == 0
    assert revision.open_items(ws) == []


def test_old_russian_form_found_outside_the_term_list_joins_the_plan(ws):
    """The Russian word is in the text although the English alias is not: only this search sees it."""
    batch.submit(ws, result(take(ws)))
    u = ws.accepted()["u-2"]["tgt"].replace("запускайте", "запускайте веса")
    assert qa.apply_edit(ws, "edit", [{"id": "u-2", "tgt": u, "reason": "тест"}])["ok"]
    report = revision.change(ws, {**WEIGHTS, "forbidden": ["весы"]}, "уточнение", dry_run=True)
    assert "u-2" in report["units"]
    assert report["by_path"]["target"] >= 2


def test_applied_change_blocks_the_gate_and_survives_an_interruption(ws):
    accepted_ws(ws)
    passes(ws)
    revision.change(
        ws, {**WEIGHTS, "applies_when": "только про нейросети"}, "уточнение", dry_run=False
    )
    fresh = store.Workspace(ws.root)  # a new session
    assert revision.open_items(fresh) == [{"plan": "rev-001", "unit": "u-1"}]
    assert qa.chapter_gate(fresh, None, "t")["verdict"] == "blocked"
    same = fresh.accepted()["u-1"]["tgt"]
    assert qa.apply_edit(fresh, "revision", [{"id": "u-1", "tgt": same, "reason": "смысл верен"}])[
        "ok"
    ]
    assert revision.open_items(fresh) == []
    assert batch.status(fresh)["stale_count"] == 0


def test_unit_translated_after_the_plan_needs_no_revision(ws):
    """The plan names units that had no translation yet: theirs is built from the new decision."""
    revision.change(ws, {**WEIGHTS, "applies_when": "про нейросети"}, "уточнение", dry_run=False)
    assert revision.open_items(ws) == []
    batch.submit(ws, result(take(ws)))
    assert revision.open_items(ws) == []


def test_change_of_meaning_widens_the_plan_to_the_chapter(ws):
    accepted_ws(ws)
    report = revision.change(
        ws, {**WEIGHTS, "sense": "масса объекта"}, "другой смысл", dry_run=True
    )
    assert report["unknown_scope"] is True
    assert report["units"] == ["u-1", "u-2"]


def test_explanation_only_change_opens_no_plan(ws):
    accepted_ws(ws)
    revision.change(ws, {**WEIGHTS, "research": {"status": "verified"}}, "источник", dry_run=False)
    assert revision.open_items(ws) == []


def test_procedure_change_with_scope_blocks_until_reviewed(ws):
    accepted_ws(ws)
    passes(ws)
    assert revision.procedure_change(ws, "all", "новый промт редактуры")["units"] == 2
    assert qa.chapter_gate(store.Workspace(ws.root), None, "t")["verdict"] == "blocked"


def test_procedure_change_none_is_recorded_and_blocks_nothing(ws):
    accepted_ws(ws)
    passes(ws)
    assert revision.procedure_change(ws, "none", "только форматирование отчёта")["ok"]
    assert revision.open_items(ws) == []
    assert not revision.procedure_change(ws, "none", "  ")["ok"]


def test_prompt_in_code_gets_a_translation_unit_and_code_stays(ws):
    rows = store.read_jsonl(ws.ir / "units.jsonl")
    code = unit("u-c", "Customer: hi\nBot: hello", region="code")
    code["class"] = "keep"
    store.write_jsonl(ws.ir / "units.jsonl", [*rows, code])
    assert revision.code_prompt_units(ws, [{"after": "u-c"}])["ok"]
    assert not revision.code_prompt_units(ws, [{"after": "u-1"}])["ok"]
    assert not revision.code_prompt_units(ws, [{"after": "u-c", "src": "not there"}])["ok"]
    ids = [u["unit_id"] for u in ws.units()]
    assert ids.index("u-prompt-c") == ids.index("u-c") + 1
    assert code["src"]["text"] in [u["src"]["text"] for u in ws.units() if u["region"] == "code"]


# --- Reference ----------------------------------------------------------------------------------


def test_owner_decision_is_protected(ws):
    bible.add(ws, [{**WEIGHTS, "origin": "owner"}], actor="owner")
    with pytest.raises(bible.BibleError):
        bible.add(ws, [{**WEIGHTS, "reason": "агент решил иначе"}], actor="agent")


def test_note_quote_is_verified(ws):
    units = {u["unit_id"]: u for u in ws.units()}
    good = {"id": "N-1", "occ": "u-1", "quote": "the weights", "type": "tone", "meaning": "м"}
    assert bible.add_notes(ws, [good], units) == 1
    with pytest.raises(bible.BibleError):
        bible.add_notes(ws, [{**good, "quote": "not there"}], units)
    other = next(u for u in units if u != good["occ"])
    with pytest.raises(bible.BibleError, match="already used"):
        bible.add_notes(
            ws, [{**good, "occ": other, "quote": units[other]["src"]["text"][:5]}], units
        )


def test_longer_alias_wins_and_polysemy_requires_a_choice():
    concepts = {
        "C-1": {"id": "C-1", "status": "accepted", "en": {"aliases": ["weights"]}},
        "C-2": {"id": "C-2", "status": "accepted", "en": {"aliases": ["model weights"]}},
        "C-3": {"id": "C-3", "status": "accepted", "en": {"aliases": ["weights"]}},
    }
    found = bible.find_occurrences("The model weights and the weights.", concepts)
    assert [o["concepts"] for o in found] == [["C-2"], ["C-1", "C-3"]]


def test_a_new_concept_does_not_renumber_existing_occurrences():
    weights = {"id": "C-1", "status": "accepted", "en": {"aliases": ["weights"]}}
    data = {"id": "C-2", "status": "accepted", "en": {"aliases": ["input data"]}}
    text = "Set input data and weights, then weights again."
    before = [o["n"] for o in bible.find_occurrences(text, {"C-1": weights})]
    after = [o["n"] for o in bible.find_occurrences(text, {"C-1": weights, "C-2": data})]
    assert before == ["C-1-1", "C-1-2"]
    assert after == ["C-2-1", "C-1-1", "C-1-2"]


def test_not_when_excludes_an_ordinary_use_of_the_alias():
    concepts = {
        "C-1": {
            "id": "C-1",
            "status": "accepted",
            "en": {"aliases": ["AI"]},
            "not_when": ["Stability AI"],
        }
    }
    assert bible.find_occurrences("Stability AI builds AI models.", concepts)[0]["context"]
    assert len(bible.find_occurrences("Stability AI builds AI models.", concepts)) == 1


@pytest.mark.parametrize(
    "text, form",
    [
        ("вес", "весами"),
        ("векторная база данных", "векторной базе данных"),
        ("большая языковая модель", "большой языковой модели"),
        ("промпт", "промпта"),
    ],
)
def test_term_forms_decline(text, form):
    assert form in bible.generate_forms(text)["forms"]


@pytest.mark.parametrize(
    "word, form",
    [
        ("трансформер", "трансформеров"),
        ("промпт", "промптов"),
        ("чанк", "чанков"),
        ("эмбеддинг", "эмбеддингов"),
        ("бенчмарк", "бенчмарков"),
        ("токен", "токенов"),
    ],
)
def test_unknown_words_get_all_case_forms(word, form):
    assert form in bible.generate_forms(word)["forms"]


def test_guessed_forms_are_shown_for_review(ws):
    changed = bible.add(
        ws,
        [
            {
                **WEIGHTS,
                "id": "C-9",
                "en": {"aliases": ["prompt"]},
                "variants": [{"kind": "preferred", "text": "промпт"}],
            }
        ],
    )
    assert changed[0]["review_forms"][0]["parse"] == "predicted"


def test_latin_term_keeps_its_form():
    assert bible.generate_forms("RAG")["forms"] == ["rag"]
    assert "точной настройки llm" in bible.generate_forms("точная настройка LLM")["forms"]


# --- Checks -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src, tgt, rule",
    [
        ("Costs 16 GB of memory here.", "Занимает 1,6 ГБ памяти здесь.", "N1"),
        ("It may reduce latency in this case.", "Это снижает задержку в этом случае.", "M2"),
        ("Do not run this on production data.", "Запускайте это на боевых данных.", "M1"),
        ("Fails because the index is stale now.", "Падает, индекс устарел уже сейчас.", "M3"),
        (
            "Call the my_func helper first please.",
            "Сначала вызовите вспомогательную функцию.",
            "K3",
        ),
    ],
)
def test_candidates_find_planted_errors(src, tgt, rule):
    found = qa.check_unit(unit("u", src), {"tgt": tgt, "terms": []}, {}, [])
    assert rule in {f["rule"] for f in found}


@pytest.mark.parametrize(
    "src, tgt",
    [
        # Each pair is a correct translation that the first calibration flagged as a defect.
        (
            "Neural networks are used in several small models.",
            "В нескольких небольших моделях используются нейронные сети.",
        ),
        ("It can be impractical due to cost.", "Это бывает непрактично из-за цены."),
        ("Reduced transparency of the models.", "Меньшая прозрачность моделей."),
        (
            "Teams often need to ensure backups.",
            "Командам часто приходится обеспечивать резервные копии.",
        ),
        ("Parsers were, thus, limited in speed.", "Таким образом, парсеры были ограничены по скорости."),
        ("Unable to draw charts as of 2024.", "На 2024 год не умеет рисовать графики."),
    ],
)
def test_correct_translation_gives_no_false_alarm(src, tgt):
    found = qa.check_unit(unit("u", src), {"tgt": tgt, "terms": []}, {}, [])
    assert not {f["rule"] for f in found} & {"M1", "M2", "M3"}


def test_unconditional_variant_is_a_hint_and_an_undeclared_one_needs_a_decision():
    concept = {
        **WEIGHTS,
        "variants": [
            {"kind": "preferred", "text": "вес", "forms": ["веса"]},
            {"kind": "allowed", "text": "коэффициент", "forms": ["коэффициент"]},
        ],
    }
    u = unit("u", "weights")
    allowed = qa.check_unit(
        u,
        {"tgt": "коэффициент", "terms": [{"c": "C-1", "ru": "коэффициент"}]},
        {"C-1": concept},
        [],
    )
    other = qa.check_unit(
        u, {"tgt": "параметры", "terms": [{"c": "C-1", "ru": "параметры"}]}, {"C-1": concept}, []
    )
    assert next(f for f in allowed if f["rule"] == "T3")["class"] == "A"
    assert next(f for f in other if f["rule"] == "T3")["class"] == "C"


def test_figure_number_in_a_caption_is_not_a_decimal():
    rules = qa.load_style_rules()
    u = unit("u", "Figure 1.6 - Differences", region="caption")
    found = qa.check_unit(u, {"tgt": "Рис. 1.6 – Различия", "terms": []}, {}, rules)
    assert "TYPO-06" not in {f["rule"] for f in found}


def test_decimal_comma_is_not_a_number_change():
    u = unit("u", "It is 0.83 of 1,500 tokens.")
    found = qa.check_unit(u, {"tgt": "Это 0,83 от 1500 токенов.", "terms": []}, {}, [])
    assert "N1" not in {f["rule"] for f in found}


def test_mirrored_device_is_only_a_suggestion():
    rules = qa.load_style_rules()
    src = unit("u", "It is worth noting that this works.")
    mirrored = qa.check_unit(
        src, {"tgt": "Стоит отметить, что это работает.", "terms": []}, {}, rules
    )
    plain_src = unit("u", "This works.")
    added = qa.check_unit(
        plain_src, {"tgt": "Стоит отметить, что это работает.", "terms": []}, {}, rules
    )
    pick = lambda found: next(f for f in found if f["rule"] == "STAMP-02")
    assert pick(mirrored)["origin"] == "mirrored"
    assert pick(mirrored)["class"] == "A"
    assert pick(added)["origin"] == "introduced"
    assert pick(added)["class"] == "C"


def test_chat_trace_blocks():
    rules = qa.load_style_rules()
    found = qa.check_unit(
        unit("u", "This works."), {"tgt": "Конечно! Это работает.", "terms": []}, {}, rules
    )
    assert any(f["rule"] == "CHAT-01" and f["class"] == "B" for f in found)


def test_first_mention_is_enforced():
    concepts = {"C-1": {**WEIGHTS, "first_mention": "веса (weights)"}}
    u = unit("u-1", "Set the weights.")
    result_row = {"tgt": "Задайте веса.", "terms": [{"c": "C-1", "ru": "веса"}]}
    found = qa.check_book_level([u], {"u-1": result_row}, concepts)
    assert any(f["rule"] == "T8" for f in found)


def test_first_mention_belongs_to_the_book_not_to_the_selection():
    concepts = {"C-1": {**WEIGHTS, "first_mention": "веса (weights)"}}
    early, later = unit("u-1", "Set the weights."), unit("u-2", "Save the weights.")
    accepted = {
        "u-1": {"tgt": "Задайте веса (weights).", "terms": [{"c": "C-1", "ru": "веса"}]},
        "u-2": {"tgt": "Сохраните веса.", "terms": [{"c": "C-1", "ru": "веса"}]},
    }
    found = qa.check_book_level([later], accepted, concepts, [early, later])
    assert not [f for f in found if f["rule"] == "T8"]


def test_a_heading_does_not_take_the_first_mention_from_the_text_under_it():
    concepts = {"C-1": {**WEIGHTS, "first_mention": "веса (weights)"}}
    head, body = unit("u-1", "Weights", region="heading"), unit("u-2", "Set the weights.")
    accepted = {
        "u-1": {"tgt": "Веса", "terms": [{"c": "C-1", "ru": "веса"}]},
        "u-2": {"tgt": "Задайте веса (weights).", "terms": [{"c": "C-1", "ru": "веса"}]},
    }
    found = qa.check_book_level([head, body], accepted, concepts)
    assert not [f for f in found if f["rule"] == "T8"]


def test_kept_prompt_must_stay_word_for_word():
    u = unit("u", "Summarize this report.")
    good = qa.check_unit(
        u, {"tgt": "Summarize this report.¶Кратко изложи этот отчёт.", "terms": []}, {}, []
    )
    bad = qa.check_unit(
        u, {"tgt": "Summarize.¶Кратко изложи этот отчёт.", "terms": []}, {}, []
    )
    assert "P2" not in {f["rule"] for f in good}
    assert "P2" in {f["rule"] for f in bad}


def test_kept_prompt_is_checked_by_its_translation():
    u = unit("u", "Do not write 5 lines.")
    found = qa.check_unit(u, {"tgt": "Do not write 5 lines.¶Напиши 7 строк.", "terms": []}, {}, [])
    assert {"N1", "M1"} <= {f["rule"] for f in found}


@pytest.mark.parametrize(
    "src, tgt",
    [
        # Each pair is a correct translation that the pilot flagged as a defect.
        ("This can be found at the site.", "Это находится на сайте."),
        ("While not mandatory, it helps.", "Это не обязательно, но помогает."),
        ("Sorry, this falls outside my domain.", "Извините, это не входит в мою область."),
        ("It covers many countries rather than one.", "Он охватывает много стран, а не одну."),
        (
            "Use filters to prevent offensive language.",
            "Используйте фильтры, чтобы не допускать оскорблений.",
        ),
    ],
)
def test_pilot_false_alarms_stay_quiet(src, tgt):
    found = qa.check_unit(unit("u", src), {"tgt": tgt, "terms": []}, {}, [])
    assert not {f["rule"] for f in found} & {"M1", "M2", "M3"}


def test_may_stated_as_fact_is_still_found():
    found = qa.check_unit(
        unit("u", "It may reduce latency."), {"tgt": "Это снижает задержку.", "terms": []}, {}, []
    )
    assert "M2" in {f["rule"] for f in found}


def test_model_version_is_not_a_decimal_and_a_real_one_is():
    rules = qa.load_style_rules()
    model = qa.check_unit(
        unit("u", "GPT-3.5-turbo", region="table"), {"tgt": "GPT-3.5-turbo", "terms": []}, {}, rules
    )
    decimal = qa.check_unit(
        unit("u", "It is 3.5 here"), {"tgt": "Здесь 3.5", "terms": []}, {}, rules
    )
    assert "TYPO-06" not in {f["rule"] for f in model}
    assert "TYPO-06" in {f["rule"] for f in decimal}


def test_kept_original_needs_no_translation_checks():
    u = unit("u", "Introduction to Prompt Engineering and more words")
    kept = {
        "tgt": "Introduction to Prompt Engineering and more words",
        "terms": [],
        "kept_original": "заголовок статьи",
    }
    assert qa.check_unit(u, kept, {}, []) == []
    assert any(
        f["rule"] == "E1" for f in qa.check_unit(u, {"tgt": u["src"]["text"], "terms": []}, {}, [])
    )


def test_prompt_from_code_is_not_compared_with_the_code_around_it():
    u = unit("u", "response = api.call(temperature=0.5)", region="prompt_translation")
    found = qa.check_unit(u, {"tgt": "Система: ты ассистент.", "terms": []}, {}, [])
    assert not {f["rule"] for f in found} & {"N1", "L1", "K3"}


def test_should_turned_into_must_is_found():
    u = unit("u", "Prompt engineers should gain user consent.")
    bad = qa.check_unit(
        u, {"tgt": "Промпт-инженеры должны получать согласие.", "terms": []}, {}, []
    )
    good = qa.check_unit(
        u, {"tgt": "Промпт-инженерам следует получать согласие.", "terms": []}, {}, []
    )
    assert "M2" in {f["rule"] for f in bad}
    assert "M2" not in {f["rule"] for f in good}


def test_a_prompt_keeps_the_strength_of_should():
    """A translated prompt shows the reader what the original asks: an imperative is stronger."""
    u = unit("u", "You should maintain a positive tone.", region="prompt_translation")
    bad = {
        f["rule"]
        for f in qa.check_unit(u, {"tgt": "Поддерживай позитивный тон.", "terms": []}, {}, [])
    }
    good = {
        f["rule"]
        for f in qa.check_unit(
            u, {"tgt": "Тебе следует поддерживать позитивный тон.", "terms": []}, {}, []
        )
    }
    assert "M2" in bad
    assert "M2" not in good


def test_lost_first_person_of_the_author_is_found():
    u = unit("u", "In this section, we outline the steps.")
    bad = qa.check_unit(u, {"tgt": "В этом разделе приведены шаги.", "terms": []}, {}, [])
    good = qa.check_unit(u, {"tgt": "В этом разделе мы приводим шаги.", "terms": []}, {}, [])
    assert "M4" in {f["rule"] for f in bad}
    assert "M4" not in {f["rule"] for f in good}


def _unused(_: Path) -> None:
    """Path import is kept for fixtures typed by tmp_path users."""
