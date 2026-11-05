"""Generate Typst from the registry and compile it. Text comes from `tgt` when present, else `src`."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pypdfium2

from . import indexer, store
from .batch import plain

FULL_PAGE_WIDTH = 500  # pt: wider than any text block, so it is a cover and fills its own page

SPAN = re.compile("⟦(/?)(a?\\d+)(/?)⟧")
ESCAPE = re.compile(r"([\\#$*_`@<>\[\]~=+\-/'\"])")
SKILL_DIR = Path(__file__).resolve().parents[2]
PROMPT_BREAK = "¶"


class BuildError(RuntimeError):
    pass


def _esc(text: str) -> str:
    text = ESCAPE.sub(r"\\\1", text)
    # PROMPT_BREAK separates a prompt kept in the original from its translation.
    text = text.replace(PROMPT_BREAK, "#linebreak();")
    return re.sub(r"^(\d+)\.", r"\1\\.", text)


def _str(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _link_target(link: dict | None, resolve) -> str | None:
    if not link:
        return None
    if "uri" in link:
        return _str(link["uri"])
    return resolve(link.get("dest"))


def inline(text: str, spans: dict, resolve) -> str:
    """Tagged text to Typst markup. Span structure was validated before this point."""
    out: list[str] = []
    pos = 0
    stack: list[str] = []
    for m in SPAN.finditer(text):
        out.append(_esc(text[pos : m.start()]))
        pos = m.end()
        closing, key = m.group(1), m.group(2)
        span = spans[key]
        if span["kind"] == "atom":
            data = span["data"]
            target = _link_target(data.get("link"), resolve)
            bold = ", bold: true" if span.get("bold") else ""
            url = f", url: {target}" if target and target.startswith('"') else ""
            out.append(f"#mono({_str(data['text'])}{url}{bold});")
        elif closing:
            out.append(stack.pop())
        else:
            role = span["role"]
            open_part, close_part = "", ""
            if "strong" in role:
                open_part, close_part = "#strong[" + open_part, close_part + "]"
            if "em" in role:
                open_part, close_part = open_part + "#emph[", "]" + close_part
            if "underline" in role:
                open_part, close_part = open_part + "#underline[", "]" + close_part
            if "link" in role:
                target = _link_target(span.get("link"), resolve)
                if target:
                    open_part, close_part = open_part + f"#link({target})[", "]" + close_part
            out.append(open_part)
            # ";" ends the code expression: text starting with "(" or "[" must not become a call.
            stack.append(close_part + ";")
    out.append(_esc(text[pos:]))
    return "".join(out)


def extract_figures(pdf_path: Path, objects: list[dict], out_dir: Path, dpi: int = 300) -> None:
    """Crop each figure from the rendered page: the same pixels the reader sees."""
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = pypdfium2.PdfDocument(str(pdf_path))
    scale = dpi / 72
    for obj in objects:
        if obj["kind"] != "figure" or not obj.get("bbox"):
            continue
        target = out_dir / f"{obj['id']}.png"
        if target.exists():
            continue
        page = doc[obj["page"] - 1]
        # The bbox is in MediaBox coordinates, while PDFium renders the CropBox.
        left, bottom, right, top = page.get_cropbox()
        media_top = page.get_mediabox()[3]
        b = obj["bbox"]
        y0, y1 = media_top - b["bottom"], media_top - b["top"]
        image = page.render(
            scale=scale,
            crop=(
                max(b["x0"] - left, 0),
                max(y0 - bottom, 0),
                max(right - b["x1"], 0),
                max(top - y1, 0),
            ),
        ).to_pil()
        image.save(target)


class Typesetter:
    def __init__(self, workdir: Path, lang: str = "en"):
        self.workdir = workdir
        self.lang = lang
        ir = workdir / "ir"
        self.ws = store.Workspace(workdir)
        self.units = self.ws.units()
        self.objects = [
            json.loads(x) for x in (ir / "objects.jsonl").read_text("utf-8").splitlines()
        ]
        self.index = [json.loads(x) for x in (ir / "index.jsonl").read_text("utf-8").splitlines()]
        self.results: dict[str, dict] = self.ws.accepted()
        self.by_id = {u["unit_id"]: u for u in self.units}
        self.fig_after: dict[str | None, list[dict]] = {}
        for obj in self.objects:
            if obj["kind"] == "figure":
                self.fig_after.setdefault(obj.get("after"), []).append(obj)
        self.index_by_unit = {e["unit_id"]: e for e in self.index}
        self.dests = json.loads((ir / "dests.json").read_text("utf-8"))
        self.anchors = sorted({u["page"] for u in self.units if u["region"] not in ("toc", "alt")})
        self.pages_with_mark: set[int] = set()

    def text_of(self, unit: dict) -> str:
        result = self.results.get(unit["unit_id"])
        return result["tgt"] if result else unit["src"]["text"]

    def _resolve(self, dest: str | None) -> str | None:
        """An internal link points at the start of its source page's content."""
        page = self.dests.get(dest or "", {}).get("page")
        return f'label("src-{self._anchor(page)}")' if page else None

    def _inline(self, unit: dict) -> str:
        return inline(self.text_of(unit), unit.get("spans", {}), self._resolve)

    def _src_mark(self, unit: dict) -> str:
        page = unit["page"]
        if page in self.pages_with_mark:
            return ""
        self.pages_with_mark.add(page)
        return f"#srcpage({page});"

    def generate(self, select=None, draft: bool = False) -> str:
        if select is None and not draft:
            state = indexer.blockers(self.ws)
            if any(u["region"] == "index" for u in self.units) and (
                not state["bound"] or state["open"] or state["same_russian"]
            ):
                raise BuildError(f"the index is not ready: {state}; run bt index-bind, then decide")
        toc_title = "Содержание" if self.lang == "ru" else "Table of Contents"
        out = [
            '#import "book.typ": *',
            f'#show: book.with(lang: "{self.lang}")',
        ]
        toc_page = f"#page(foreground: none)[#toc-title[{toc_title}]\n#toc]"
        units = [u for u in self.units if select is None or select(u)]
        # The contents stay where the book has them; a part without them opens with the list.
        toc_pending = any(u["region"] == "toc" for u in units)
        if not toc_pending:
            out.append(toc_page)
        # A figure anchored to the caption of the previous one waits for that caption's text.
        for at, unit in enumerate(units[:-1]):
            if unit["region"] == "caption" and units[at + 1]["region"] == "figure_text":
                moved = self.fig_after.pop(unit["unit_id"], [])
                if moved:
                    self.fig_after.setdefault(units[at + 1]["unit_id"], [])[:0] = moved
        self.selected_ids = {u["unit_id"] for u in units}
        if select is not None:
            self.anchors = sorted({u["page"] for u in units if u["region"] not in ("toc", "alt")})
        if select is None:
            for fig in self.fig_after.get(None, []):
                out.append(self._figure(fig))
        selected = {u["unit_id"] for u in units}
        self.fig_before: dict[str, list[dict]] = {}
        if select is not None:
            # In a partial build a figure can follow a unit outside the selection: attach it to the
            # first selected unit on its page so that the page keeps its content.
            for fig in (o for o in self.objects if o["kind"] == "figure"):
                if fig.get("after") not in selected:
                    host = next((u for u in units if u["page"] == fig["page"]), None)
                    if host:
                        self.fig_before.setdefault(host["unit_id"], []).append(fig)
        i = 0
        while i < len(units):
            unit = units[i]
            region = unit["region"]
            for fig in self.fig_before.get(unit["unit_id"], []):
                out.append(self._figure(fig))
            if region in ("toc", "alt"):
                if region == "toc" and toc_pending:
                    out.append(toc_page)
                    toc_pending = False
                i += 1
                continue
            if region == "table":
                i = self._table(units, i, out)
                continue
            if region in ("note", "note_title"):
                i = self._note(units, i, out)
                continue
            if region == "index":
                i = self._index(units, i, out)
                continue
            if region == "part":
                i = self._part(units, i, out)
                continue
            # Built only here: the source-page mark must not be spent on a skipped unit.
            mark = f'#mark("{unit["unit_id"]}");' + self._src_mark(unit)
            if region == "chapter_no":
                out.append(f'#pagebreak(to: "odd", weak: true)\n#v(55.6pt)\n{mark}')
                out.append(f"#chapter-no[{_esc(unit['src']['text'])}]")
            elif region == "heading":
                level = unit["level"]
                body = self._inline(unit)
                if level == 1:
                    prev = units[i - 1]["region"] if i else None
                    if prev != "chapter_no":
                        out.append('#pagebreak(to: "odd", weak: true)\n#v(55.6pt)')
                outlined = "true" if unit.get("in_toc") else "false"
                out.append(f"{mark}#heading(level: {level}, outlined: {outlined})[{body}]")
                out.append("#bodymark")
            elif region == "code":
                arr = ", ".join(_str(x) for x in unit["lines"])
                bold = "true" if unit.get("bold") else "false"
                out.append(f"{mark}#codeblock(({arr},), bold: {bold})\n#bodymark")
            elif region == "list":
                out.append(mark + self._list_item(unit) + "\n#bodymark")
            elif region == "caption":
                has_text = i + 1 < len(units) and units[i + 1]["region"] == "figure_text"
                call = "#caption(sticky: true)" if has_text else "#caption"
                out.append(f"{mark}{call}[{self._inline(unit)}]\n#bodymark")
            elif region == "prompt_translation":
                default = "Перевод промпта" if self.lang == "ru" else "Prompt translation"
                label = (unit.get("label") if self.lang == "ru" else None) or default
                out.append(f"{mark}#figure-text[{label}][{self._inline(unit)}]\n#bodymark")
            elif region == "figure_text":
                label = "Текст на рисунке" if self.lang == "ru" else "Text in the figure"
                out.append(
                    f"{mark}#figure-text(keep: true)[{label}][{self._inline(unit)}]\n#bodymark"
                )
            else:
                out.append(f"{mark}{self._inline(unit)}\n#bodymark\n")
            for fig in self.fig_after.get(unit["unit_id"], []):
                out.append(self._figure(fig))
            i += 1
        out.append("#positions")
        return "\n".join(out)

    def _anchor(self, page: int) -> int:
        """A page holding only a figure has no unit: the next page with text stands in for it."""
        return next((p for p in self.anchors if p >= page), self.anchors[-1])

    def _list_item(self, unit: dict) -> str:
        marker = unit.get("marker") or {}
        level = unit.get("level") or 1
        symbol = {"bullet": '"•"', "number": _str(marker.get("text") or "")}.get(
            marker.get("kind"), '""'
        )
        if marker.get("kind") == "bullet" and level == 2:
            symbol = '"–"'
        return f"#list-item(level: {level}, marker: {symbol})[{self._inline(unit)}]"

    def _alt_text(self, fig: dict) -> str:
        unit = self.by_id.get(fig.get("alt_from") or "")
        return SPAN.sub("", self.text_of(unit)) if unit else ""

    def _figure(self, fig: dict) -> str:
        if not fig.get("bbox"):
            return ""
        width = fig["bbox"]["x1"] - fig["bbox"]["x0"]
        alt = self._alt_text(fig)
        mark = f'#mark("{fig["id"]}");'
        if width > FULL_PAGE_WIDTH:
            return f'#full-page-image("ir/figures/{fig["id"]}.png", [{mark}], alt: {_str(alt)})\n#bodymark'
        return f'{mark}#figure-block("ir/figures/{fig["id"]}.png", {width:.2f}pt, alt: {_str(alt)})\n#bodymark'

    def _table(self, units, i, out) -> int:
        group = units[i]["group"]
        cells: dict[tuple[int, int], list[dict]] = {}
        j = i
        while j < len(units) and units[j].get("group") == group:
            c = units[j]["cell"]
            cells.setdefault((c["row"], c["col"]), []).append(units[j])
            j += 1
        columns = max(col for _, col in cells) + 1
        rows = max(row for row, _ in cells) + 1
        widths = self._column_widths(cells, columns)
        parts = []
        carried = ""  # marks of repeated header rows: the engine repeats the header itself
        for row in range(rows):
            row_units = [u for col in range(columns) for u in cells.get((row, col), [])]
            is_header = bool(row_units) and all(u["cell"]["header"] for u in row_units)
            if is_header and row > 0:
                carried += "".join(f'#mark("{u["unit_id"]}");' for u in row_units)
                continue
            row_parts = []
            for col in range(columns):
                content = cells.get((row, col), [])
                inner = "\n\n".join(
                    f'#mark("{u["unit_id"]}");{self._src_mark(u)}'
                    + (self._list_item(u) if u.get("marker") else self._inline(u))
                    for u in content
                )
                if carried and inner:
                    inner, carried = carried + inner, ""
                body = f"#text(weight: 700)[{inner}]" if is_header else inner
                fill = "fill: rgb(85.1%, 85.1%, 85.1%)" if is_header else ""
                row_parts.append(f"table.cell({fill})[#set par(justify: false); {body}]")
            if is_header and row == 0:
                parts.append(f"table.header({', '.join(row_parts)})")
            else:
                parts.extend(row_parts)
        widths_src = ", ".join(f"{w:.3f}" for w in widths)
        out.append(f"#table-block(({widths_src},), ({', '.join(parts)},))\n#bodymark")
        return j

    @staticmethod
    def _column_widths(cells, columns: int) -> list[float]:
        """Column proportions from the source: left edge of each column, per page parity."""
        starts: dict[int, list[float]] = {}
        right = 0.0
        for (_, col), units in cells.items():
            for u in units:
                box = u["cell"].get("box")
                if box:
                    starts.setdefault(col, []).append(box[0])
                    right = max(right, box[1])
        if len(starts) < columns:
            return [1 / columns] * columns
        edges = [min(starts[c]) for c in range(columns)] + [right]
        widths = [max(edges[i + 1] - edges[i], 1.0) for i in range(columns)]
        total = sum(widths)
        return [w / total for w in widths]

    def _note(self, units, i, out) -> int:
        title = "none"
        if units[i]["region"] == "note_title":
            title = f'[#mark("{units[i]["unit_id"]}");{self._src_mark(units[i])}{self._inline(units[i])}]'
            i += 1
        bodies = []
        while i < len(units) and units[i]["region"] == "note":
            bodies.append(
                f'#mark("{units[i]["unit_id"]}");{self._src_mark(units[i])}{self._inline(units[i])}'
            )
            i += 1
        out.append(f"#note({title}, [{(chr(10) * 2).join(bodies)}])\n#bodymark")
        return i

    def _part(self, units, i, out) -> int:
        unit = units[i]
        outlined = "true" if unit.get("in_toc") else "false"
        title = (
            f'#mark("{unit["unit_id"]}");{self._src_mark(unit)}'
            f"#heading(level: 1, outlined: {outlined})[{self._inline(unit)}]"
        )
        i += 1
        body = []
        while i < len(units) and units[i]["region"] in ("body", "list"):
            u = units[i]
            mark = f'#mark("{u["unit_id"]}");{self._src_mark(u)}'
            body.append(mark + (self._list_item(u) if u["region"] == "list" else self._inline(u)))
            i += 1
        out.append(f"#part-page([{title}], [{chr(10).join(body)}\n])")
        return i

    def _index(self, units, i, out) -> int:
        """Sorted entries, letter headings and page references to bound places."""
        selected = {u["unit_id"] for u in units}
        figures = []  # A picture anchored to an entry follows the index: entries get sorted.
        while i < len(units) and units[i]["region"] == "index":
            figures += self.fig_after.get(units[i]["unit_id"], [])
            i += 1
        shape = indexer.layout(self.ws, lambda u: u["unit_id"] in selected)
        out.append('#pagebreak(to: "odd", weak: true)\n#v(55.6pt)')
        if shape["title"]:
            title = shape["title"]
            out.append(
                f'#mark("{title["unit_id"]}");#heading(level: 1)[{self._inline(title)}]\n#bodymark'
            )
        out.append("#columns(2, gutter: 18pt)[#set par(justify: false)")
        letter = None
        for main in shape["mains"]:
            term = plain(self.text_of(main["unit"]))
            first = indexer.letter_of(term)
            if first != letter:
                letter = first
                out.append(f"#index-letter[{_esc(first)}]")
            out.append(self._index_row(main, True))
            out.extend(self._index_row(sub, False) for sub in main["subs"])
        out.append("]\n#bodymark")
        out.extend(self._figure(fig) for fig in figures)
        return i

    def _index_row(self, row: dict, main: bool) -> str:
        unit = row["unit"]
        flag = "true" if main else "false"
        term = _esc(plain(self.text_of(unit)))
        refs = ""
        if row["refs"]:
            groups = ", ".join(
                "(" + ", ".join(self._point(p) for p in ref["points"]) + ",)" for ref in row["refs"]
            )
            refs = f"#pagerefs({groups})"
        return f'#mark("{unit["unit_id"]}");#index-entry(main: {flag})[{term}][{refs}]'

    def _point(self, point: dict) -> str:
        if point["bind"] and point["bind"] in self.selected_ids:
            return f'label("ix-{point["bind"]}")'
        # A draft or a partial build: the start of the source page stands in for the place.
        return f'label("src-{self._anchor(point["page"])}")'


def compile_book(workdir: Path, source: str, name: str = "book") -> dict:
    typst = os.environ.get("BT_TYPST", "typst")
    fonts = os.environ.get("BT_FONTS")
    if not fonts:
        raise BuildError("BT_FONTS is not set: fonts must come from a checked directory")
    (workdir / "book.typ").write_text(
        (SKILL_DIR / "templates" / "book.typ").read_text("utf-8"), encoding="utf-8"
    )
    src = workdir / f"{name}.main.typ"
    src.write_text(source, encoding="utf-8")
    out = workdir / "build"
    out.mkdir(exist_ok=True)
    pdf = out / f"{name}.pdf"
    args = [typst, "compile", "--root", str(workdir), "--font-path", fonts, "--ignore-system-fonts"]
    done = subprocess.run(
        [*args, str(src), str(pdf)], capture_output=True, text=True, encoding="utf-8", check=False
    )
    if done.returncode != 0:
        raise BuildError(done.stderr[:4000])
    q = subprocess.run(
        [
            typst,
            "query",
            "--root",
            str(workdir),
            "--font-path",
            fonts,
            "--ignore-system-fonts",
            str(src),
            "<positions>",
            "--field",
            "value",
            "--one",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if q.returncode != 0:
        raise BuildError(q.stderr[:2000])
    (out / f"{name}.positions.json").write_text(q.stdout, encoding="utf-8")
    return {"pdf": str(pdf), "warnings": done.stderr[:2000]}
