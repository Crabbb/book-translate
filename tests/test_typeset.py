"""What the generated Typst source says, without compiling it."""

from __future__ import annotations

import json

from bt import store, typeset


def workdir(tmp_path, units):
    ir = tmp_path / "ir"
    store.write_jsonl(ir / "units.jsonl", units)
    store.write_jsonl(ir / "objects.jsonl", [])
    store.write_jsonl(ir / "index.jsonl", [])
    (ir / "dests.json").write_text(json.dumps({}), encoding="utf-8")
    return tmp_path


def unit(uid, region, text):
    return {
        "unit_id": uid,
        "chapter": 1,
        "page": 1,
        "region": region,
        "class": "translate",
        "src": {"text": text, "hash": "h"},
        "spans": {},
    }


def test_lines_of_a_note_stay_separate_paragraphs(tmp_path):
    """Joined by one newline they ran together: original and translation of a prompt mixed up."""
    units = [
        unit("u-1", "note_title", "Example:"),
        unit("u-2", "note", "First"),
        unit("u-3", "note", "Second"),
    ]
    source = typeset.Typesetter(workdir(tmp_path, units), lang="ru").generate()
    assert "First\n\n" in source


def test_prompt_break_becomes_a_line_break(tmp_path):
    units = [unit("u-1", "body", "Write a poem.¶Напиши стихотворение.")]
    source = typeset.Typesetter(workdir(tmp_path, units), lang="ru").generate()
    assert "poem.#linebreak();" in source
    assert "¶" not in source


def test_text_starting_with_a_bracket_cannot_become_a_call(tmp_path):
    units = [unit("u-1", "body", "(Split by tokens)")]
    source = typeset.Typesetter(workdir(tmp_path, units), lang="ru").generate()
    assert '#mark("u-1");' in source


def test_caption_stays_with_the_text_of_its_figure(tmp_path):
    units = [
        unit("u-1", "caption", "Рис. 1.1 – Схема"),
        unit("u-2", "figure_text", "Подписи на рисунке"),
        unit("u-3", "caption", "Рис. 1.2 – Без текста"),
        unit("u-4", "body", "Абзац"),
    ]
    source = typeset.Typesetter(workdir(tmp_path, units), lang="ru").generate()
    assert "#caption(sticky: true)[Рис. 1.1" in source
    assert "#figure-text(keep: true)[Текст на рисунке]" in source
    assert "#caption[Рис. 1.2" in source


def test_block_translation_may_name_a_model_answer(tmp_path):
    units = [{**unit("u-1", "prompt_translation", "ответ"), "label": "Перевод ответа модели"}]
    assert "#figure-text[Перевод ответа модели]" in typeset.Typesetter(workdir(tmp_path, units), lang="ru").generate()
    units = [unit("u-2", "prompt_translation", "промпт")]
    assert "#figure-text[Перевод промпта]" in typeset.Typesetter(workdir(tmp_path, units), lang="ru").generate()


def test_contents_stay_at_their_place_in_the_book(tmp_path):
    units = [unit("u-1", "body", "До содержания"), unit("u-2", "toc", "пункт"), unit("u-3", "body", "После")]
    source = typeset.Typesetter(workdir(tmp_path, units), lang="ru").generate()
    assert source.index("До содержания") < source.index("#toc-title") < source.index("После")


def test_next_figure_does_not_split_a_caption_from_its_text(tmp_path):
    units = [unit("u-1", "caption", "Рис. 1"), unit("u-2", "figure_text", "текст 1"), unit("u-3", "caption", "Рис. 2")]
    root = workdir(tmp_path, units)
    figure = {"id": "fig-2", "kind": "figure", "after": "u-1", "page": 1, "bbox": {"x0": 0, "x1": 100, "y0": 0, "y1": 10}}
    store.write_jsonl(root / "ir" / "objects.jsonl", [figure])
    source = typeset.Typesetter(root, lang="ru").generate()
    assert source.index("текст 1") < source.index("fig-2") < source.index("Рис. 2")
