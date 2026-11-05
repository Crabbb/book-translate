"""Negative artifacts: each check must fail on a deliberately broken PDF, not just pass on good ones.

The prototype's QA reported false zeros, so a check without a failing case proves nothing.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from bt import artifact

TYPST = shutil.which("typst")
HEAD = """
#set page(width: 540pt, height: 666pt, margin: (x: 72pt, top: 67.3pt, bottom: 42pt))
#set text(font: "Libertinus Serif", size: 10pt, fallback: false)
"""
PARA = "The quick brown fox jumps over the lazy dog and keeps running far away from here."


def build(tmp_path: Path, body: str, fonts: Path) -> Path:
    import subprocess

    typst = _typst()
    src = tmp_path / "t.typ"
    src.write_text(HEAD + body, encoding="utf-8")
    out = tmp_path / "t.pdf"
    subprocess.run(
        [typst, "compile", "--font-path", str(fonts), "--ignore-system-fonts", str(src), str(out)],
        check=True,
        capture_output=True,
    )
    return out


def _typst() -> str:
    import os

    typst = os.environ.get("BT_TYPST") or TYPST
    if not typst:
        pytest.skip("typst is not available: set BT_TYPST")
    return typst


@pytest.fixture
def fonts() -> Path:
    import os

    value = os.environ.get("BT_FONTS")
    if not value:
        pytest.skip("BT_FONTS is not set")
    return Path(value)


def unit(text: str, region: str = "body", uid: str = "u-1") -> dict:
    return {"unit_id": uid, "region": region, "src": {"text": text}, "spans": {}}


def test_clean_page_has_no_findings(tmp_path, fonts):
    pdf = build(tmp_path, PARA, fonts)
    result = artifact.Artifact(pdf, [unit(PARA)]).run(["LibertinusSerif"])
    assert result["counts"] == {
        "figures": 0,
        "style": 0,
        "link_targets": 0,
        "toc": 0,
        "presence": 0,
        "overflow": 0,
        "overlap": 0,
        "fonts": 0,
        "code": 0,
        "links": 0,
    }


def test_lost_paragraph_is_found(tmp_path, fonts):
    pdf = build(tmp_path, PARA, fonts)
    other = "This whole paragraph never reached the page at all."
    found = artifact.Artifact(pdf, [unit(PARA), unit(other, uid="u-2")]).check_presence()
    assert [f["unit"] for f in found] == ["u-2"]
    assert found[0]["check"] == "lost"


def test_duplicated_paragraph_is_found(tmp_path, fonts):
    pdf = build(tmp_path, PARA + "\n\n" + PARA, fonts)
    found = artifact.Artifact(pdf, [unit(PARA)]).check_presence()
    assert found and found[0]["check"] == "duplicate"


def test_two_blocks_on_top_of_each_other_are_found(tmp_path, fonts):
    pdf = build(tmp_path, f"#place(top + left)[{PARA}]\n#place(top + left)[{PARA[::-1]}]", fonts)
    found = artifact.Artifact(pdf, [unit(PARA)]).check_overlap()
    assert found and found[0]["check"] == "overlap"


def test_clipped_line_is_found_although_the_text_layer_is_intact(tmp_path, fonts):
    pdf = build(tmp_path, f"#place(dx: 300pt)[#box(width: 400pt)[{PARA}]]", fonts)
    a = artifact.Artifact(pdf, [unit(PARA)])
    assert a.check_presence() == []  # the text layer alone would pass
    assert any(f["check"] == "overflow_x" for f in a.check_overflow())


def test_substituted_font_is_found(tmp_path, fonts):
    pdf = build(tmp_path, f'#text(font: "Liberation Mono")[{PARA}]', fonts)
    found = artifact.Artifact(pdf, [unit(PARA)]).check_fonts(["LibertinusSerif"])
    assert found and found[0]["check"] == "unexpected_font"


def test_changed_code_line_is_found(tmp_path, fonts):
    pdf = build(tmp_path, '#raw("x = 1")', fonts)
    code = {"unit_id": "u-3", "region": "code", "lines": ["x = 2"], "src": {"text": "x = 2"}}
    found = artifact.Artifact(pdf, [code]).check_code()
    assert found and found[0]["check"] == "code_line"


def test_wrong_uri_is_found(tmp_path, fonts):
    pdf = build(tmp_path, '#link("https://example.org/wrong")[here]', fonts)
    u = unit("here")
    u["spans"] = {"1": {"kind": "pair", "role": "link", "link": {"uri": "https://example.org/ok"}}}
    checks = {f["check"] for f in artifact.Artifact(pdf, [u]).check_links()}
    assert checks == {"uri_missing", "uri_extra"}


def test_lost_figure_is_found(tmp_path, fonts):
    pdf = build(tmp_path, PARA, fonts)
    found = artifact.Artifact(pdf, [unit(PARA)]).check_figures(expected=1)
    assert found and found[0]["check"] == "figure_lost"


def test_wrong_page_size_or_body_size_is_found(tmp_path, fonts):
    pdf = build(tmp_path, f"#set page(width: 500pt)\n#set text(size: 12pt)\n{PARA}", fonts)
    checks = {f["check"] for f in artifact.Artifact(pdf, [unit(PARA)]).check_style()}
    assert {"page_size", "body_size"} <= checks


def test_wrong_margin_is_found(tmp_path, fonts):
    pdf = build(tmp_path, f"#pad(left: 50pt)[{PARA}]", fonts)
    assert any(f["check"] == "margin" for f in artifact.Artifact(pdf, [unit(PARA)]).check_style())


def test_table_of_contents_with_a_wrong_page_is_found(tmp_path, fonts):
    body = "Heading text 9\n#pagebreak()\nHeading text"
    u = {**unit("Heading text"), "in_toc": True}
    pos = [{"id": "u-1", "page": 2, "y": 80}]
    found = artifact.Artifact(build(tmp_path, body, fonts), [u], pos).check_toc(toc_pages=1)
    assert found and found[0]["check"] == "toc"


def test_correct_table_of_contents_is_clean(tmp_path, fonts):
    body = "Heading text 2\n#pagebreak()\nHeading text"
    u = {**unit("Heading text"), "in_toc": True}
    pos = [{"id": "u-1", "page": 2, "y": 80}]
    assert artifact.Artifact(build(tmp_path, body, fonts), [u], pos).check_toc(toc_pages=1) == []


def test_internal_link_to_a_real_page_is_clean(tmp_path, fonts):
    body = "#link(<x>)[go]\n#pagebreak()\nTarget <x>"
    assert artifact.Artifact(build(tmp_path, body, fonts), [unit("go")]).check_link_targets() == []


def test_mixed_fonts_on_one_line_are_not_an_overlap(tmp_path, fonts):
    mono = '#text(font: "Liberation Mono")'
    body = f"Tags such as {mono}[<begin>], {mono}[<end>] and more."
    assert artifact.Artifact(build(tmp_path, body, fonts), [unit("Tags")]).check_overlap() == []


def test_hanging_comma_in_a_large_title_is_allowed_but_a_real_overflow_is_not(tmp_path, fonts):
    title = "#set text(size: 25pt)\n#align(right)[#box(width: 400pt)[Title,]]"
    assert artifact.Artifact(build(tmp_path, title, fonts), [unit("Title,")]).check_overflow() == []
    wide = f"#place(dx: 300pt)[#box(width: 400pt)[{PARA}]]"
    found = artifact.Artifact(build(tmp_path, wide, fonts), [unit(PARA)]).check_overflow()
    assert any(f["check"] == "overflow_x" for f in found)


def test_figure_apart_from_its_caption_is_found(tmp_path, fonts):
    pdf = build(tmp_path, PARA, fonts)
    before = {"unit_id": "u-0", "region": "body", "src": {"text": "x"}, "spans": {}}
    caption = {"unit_id": "u-1", "region": "caption", "src": {"text": PARA}, "spans": {}}
    apart = [{"id": "fig", "page": 1, "y": 80}, {"id": "u-1", "page": 2, "y": 80}]
    obj = [{"id": "fig", "kind": "figure", "after": "u-0"}]
    found = artifact.Artifact(pdf, [before, caption], apart).check_captions(obj)
    assert found and found[0]["check"] == "caption_apart"
    together = [{"id": "fig", "page": 1, "y": 80}, {"id": "u-1", "page": 1, "y": 200}]
    assert artifact.Artifact(pdf, [before, caption], together).check_captions(obj) == []
