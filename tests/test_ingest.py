"""Pure parts of ingest: code canon and span building."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml
from bt.ingest import Run, _code_lines, _normalize_runs, _runs_from_chars, _spans
from bt.pdfsrc import Char, mark_underlined

PROFILE = yaml.safe_load(
    (Path(__file__).parents[1] / "skills/book-translate/profiles/packt-indesign.yaml").read_text(
        "utf-8"
    )
)


def row(text: str, top: float, left: float = 100.0, indent: int = 0) -> list[Char]:
    return [
        Char(
            i,
            ch,
            "Courier",
            9.0,
            left + (indent + i) * 5.4,
            top,
            left + (indent + i + 1) * 5.4,
            top + 9,
            None,
        )
        for i, ch in enumerate(text)
    ]


def test_indent_is_measured_from_the_code_box_not_from_the_block():
    lines, _ = _code_lines(row("return a", 10, indent=8) + row("x", 22.33, indent=4), 5.4, 100.0)
    assert lines == ["        return a", "    x"]


def test_blank_source_lines_survive():
    chars = row("a = 1", 10) + row("b = 2", 10 + 2 * 12.33) + row("c = 3", 10 + 4 * 12.33)
    assert _code_lines(chars, 5.4, 100.0)[0] == ["a = 1", "", "b = 2", "", "c = 3"]


def test_right_pushed_remainder_is_a_layout_wrap():
    first = row("x" * 60, 10)
    wrapped = row("b):", 22.33, indent=57)
    lines, wraps = _code_lines(first + wrapped, 5.4, 100.0)
    assert wraps == [False, True]
    assert lines[1].strip() == "b):"


def test_spaces_move_out_of_styled_runs():
    runs = _normalize_runs([Run("see "), Run(" bold ", bold=True), Run("end")])
    text, spans = _spans(runs)
    assert text == "see ⟦1⟧bold⟦/1⟧ end"
    assert spans["1"]["role"] == "strong"


def test_inline_code_is_an_atom_and_keeps_its_text_outside():
    text, spans = _spans([Run("run "), Run("ls -l", mono=True)])
    assert text == "run ⟦a1⟧"
    assert spans["a1"]["data"]["text"] == "ls -l"
    assert "ls -l" not in text


def test_underline_inside_italics_is_kept_as_its_own_span():
    runs = _normalize_runs(
        [
            Run("it tastes ", italic=True),
            Run("sweet", italic=True, under=True),
            Run(".", italic=True),
        ]
    )
    text, spans = _spans(runs)
    assert text == "⟦1⟧it tastes⟦/1⟧ ⟦2⟧sweet⟦/2⟧⟦3⟧.⟦/3⟧"
    assert spans["2"]["role"] == "em+underline"


def test_rule_inside_the_glyph_box_marks_only_the_word_above_it():
    chars = row("salty sweet", 100.0)
    # a rule under "sweet", inside the box, as on p. 62 of the book
    mark_underlined(chars, [{"x0": 132.4, "x1": 159.4, "top": 107.5, "bottom": 107.9}])
    assert "".join(c.text for c in chars if c.under) == "sweet"
    above = row("text", 100.0)
    mark_underlined(above, [{"x0": 100.0, "x1": 122.0, "top": 95.0, "bottom": 95.4}])
    assert not any(c.under for c in above)


def test_a_whole_paragraph_inside_span_is_text_not_a_marker():
    chars = row("https://example.org", 10)
    items = [(c, ("/Span",)) for c in chars]
    pages = {1: SimpleNamespace(height=666)}
    runs, marker, _ = _runs_from_chars(items, [1] * len(items), pages, {}, PROFILE)
    assert "".join(run.text for run in runs) == "https://example.org"
    assert marker is None


def test_a_real_marker_before_text_stays_a_marker():
    chars = row("1.Text", 10)
    items = [(c, ("/Span",) if c.text in "1." else ()) for c in chars]
    pages = {1: SimpleNamespace(height=666)}
    runs, marker, _ = _runs_from_chars(items, [1] * len(items), pages, {}, PROFILE)
    assert marker == "1."
    assert "".join(run.text for run in runs) == "Text"
