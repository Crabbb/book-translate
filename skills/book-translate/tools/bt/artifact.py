"""Checks on the built PDF. Each returns findings: an empty list means the signal was clean.

The text layer alone proves nothing about what the reader sees, so presence, geometry, glyphs and
link targets are separate signals.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pdfplumber
import pikepdf

SPAN = re.compile("⟦/?a?\\d+/?⟧")
HEADER_BOTTOM = 60.0
TEXT_LEFT = {0: 72.0, 1: 72.0}
PAGE_W, PAGE_H = 540.0, 666.0
BODY_RIGHT = PAGE_W - 72.0
BODY_BOTTOM = PAGE_H - 42.0
EPS = 2.5
BODY_SIZE = 10.0
DESIGN = {
    "page": (540.0, 666.0),
    "body_font": "LibertinusSerif-Regular",
    "body_size": 10.0,
    "margin_left": 72,
}
HANGING = 6.0  # Typst hangs punctuation, hyphens and tracked headings past the measure
HANGING_CHARS = set(".,:;-/\u00ad)")


def _norm(text: str) -> str:
    """Whitespace and hyphens are layout. U+00AD is how Typst marks an automatic break."""
    return re.sub(r"[\s\-\u00ad\u2010\u2011\u00b6]", "", text).replace("\u2026", "...")


def _unit_text(unit: dict, atoms: bool = True) -> str:
    text = unit.get("result", {}).get("tgt") or unit["src"]["text"]
    if unit["region"] == "code":
        return text

    def fill(match: re.Match) -> str:
        key = match.group(0).strip("⟦⟧")
        span = unit.get("spans", {}).get(key)
        return span["data"]["text"] if span and span["kind"] == "atom" and atoms else ""

    return SPAN.sub(fill, text)


class Artifact:
    def __init__(self, pdf_path: Path, units: list[dict], positions: list[dict] | None = None):
        self.pdf_path = pdf_path
        self.units = units
        self.positions = {p["id"]: p for p in positions or []}
        self.toc_pages = self._toc_pages()
        self.lines_by_page: dict[int, list[dict]] = {}
        self.chars_by_page: dict[int, list[dict]] = {}
        self.fonts: Counter = Counter()
        with pdfplumber.open(pdf_path) as pdf:
            self.page_count = len(pdf.pages)
            for number, page in enumerate(pdf.pages, start=1):
                chars = [c for c in page.chars if c["text"].strip()]
                self.chars_by_page[number] = chars
                for c in chars:
                    self.fonts[c["fontname"].split("+")[-1]] += 1
                self.lines_by_page[number] = self._lines(chars)
                page.flush_cache()

    @staticmethod
    def _lines(chars: list[dict]) -> list[dict]:
        rows: dict[int, list[dict]] = {}
        for c in chars:
            rows.setdefault(round((c["top"] + c["bottom"]) / 2 / 2), []).append(c)
        lines = []
        for row in rows.values():
            row.sort(key=lambda c: c["x0"])
            # A gap wider than a column splits a line: columns and table cells are separate.
            start = 0
            for i in range(1, len(row) + 1):
                if i == len(row) or row[i]["x0"] - row[i - 1]["x1"] > 14:
                    part = row[start:i]
                    lines.append(
                        {
                            "x0": part[0]["x0"],
                            "x1": part[-1]["x1"],
                            "top": min(c["top"] for c in part),
                            "bottom": max(c["bottom"] for c in part),
                            "text": "".join(c["text"] for c in part),
                            "size": max(c["size"] for c in part),
                        }
                    )
                    start = i
        return lines

    def _toc_pages(self) -> set[int]:
        """The contents repeat every heading: pages without marks right before the first listed one."""
        marks = {p["page"] for p in self.positions.values()}
        if not marks:
            return set()
        listed = [
            self.positions[u["unit_id"]]["page"]
            for u in self.units
            if u.get("in_toc") and u["unit_id"] in self.positions
        ]
        first = min(listed, default=min(marks))
        before = [m for m in marks if m < first]
        return set(range(max(before) + 1 if before else 1, first))

    def body_stream(self) -> str:
        """All text except running heads, in layout order of the PDF content."""
        parts = []
        for number in sorted(self.chars_by_page):
            if number in self.toc_pages:
                continue
            parts.append(
                "".join(c["text"] for c in self.chars_by_page[number] if c["top"] >= HEADER_BOTTOM)
            )
        return _norm("".join(parts))

    def check_presence(self) -> list[dict]:
        """Every unit is present exactly once. A duplicate or a lost paragraph is a defect."""
        stream = self.body_stream()
        # A short unit may legitimately repeat inside a longer one, so expectations are counted
        # over all unit texts rather than by exact equality.
        corpus = "\x00".join(
            _norm(_unit_text(u)) for u in self.units if u["region"] not in ("toc", "index", "alt")
        )
        findings = []
        for unit in self.units:
            if unit["region"] in ("toc", "alt"):
                continue  # alt text lives in the tag tree, not on the page
            text = _norm(_unit_text(unit))
            if not text:
                continue
            if unit["region"] == "index":
                continue  # Printed numbers change by design; terms are checked separately.
            if unit["region"] == "table" and text not in stream:
                # A cell can break across pages with its row neighbours between the parts.
                head = len(text)
                while head and text[:head] not in stream:
                    head -= 1
                if not head or text[head:] not in stream:
                    findings.append({"check": "lost", "unit": unit["unit_id"], "text": text[:60]})
                continue
            n = stream.count(text)
            if n == 0:
                findings.append({"check": "lost", "unit": unit["unit_id"], "text": text[:60]})
            elif len(text) > 40 and n > corpus.count(text) and unit["region"] != "heading":
                findings.append({"check": "duplicate", "unit": unit["unit_id"], "count": n})
        return findings

    def check_overflow(self) -> list[dict]:
        """No text line may leave the type area. Hanging punctuation is covered by the tolerance."""
        findings = []
        for number, lines in self.lines_by_page.items():
            for line in lines:
                if line["top"] < HEADER_BOTTOM:
                    continue
                # Hanging punctuation grows with the type size: a comma in a 25 pt title hangs more.
                hang = max(HANGING, HANGING * line["size"] / BODY_SIZE)
                allowed = hang if line["text"][-1:] in HANGING_CHARS else EPS
                if line["x0"] < 72 - EPS or line["x1"] > BODY_RIGHT + allowed:
                    findings.append(
                        {
                            "check": "overflow_x",
                            "page": number,
                            "x0": round(line["x0"], 1),
                            "x1": round(line["x1"], 1),
                            "text": line["text"][:50],
                        }
                    )
                if line["bottom"] > BODY_BOTTOM + 6:
                    findings.append(
                        {"check": "overflow_y", "page": number, "text": line["text"][:50]}
                    )
        return findings

    def check_overlap(self) -> list[dict]:
        """Glyphs must not cover each other. Boxes are compared directly: grouping into lines
        splits one visual line when fonts of different height share it, a false overlap.
        """
        findings = []
        for number, chars in self.chars_by_page.items():
            ordered = sorted(chars, key=lambda c: c["x0"])
            hit = None
            for i, a in enumerate(ordered):
                for b in ordered[i + 1 :]:
                    if b["x0"] >= a["x1"]:
                        break
                    w = min(a["x1"], b["x1"]) - b["x0"]
                    h = min(a["bottom"], b["bottom"]) - max(a["top"], b["top"])
                    narrow = min(a["x1"] - a["x0"], b["x1"] - b["x0"])
                    low = min(a["bottom"] - a["top"], b["bottom"] - b["top"])
                    if narrow > 0 and w > 0.6 * narrow and h > 0.6 * low:
                        hit = (a, b)
                        break
                if hit:
                    break
            if hit:
                findings.append(
                    {"check": "overlap", "page": number, "a": hit[0]["text"], "b": hit[1]["text"]}
                )
        return findings

    def check_fonts(self, expected_prefixes: list[str]) -> list[dict]:
        """A silently substituted font shows up as an unexpected family."""
        wrong = {
            f: n
            for f, n in self.fonts.items()
            if not any(f.startswith(p) for p in expected_prefixes)
        }
        return [{"check": "unexpected_font", "font": f, "chars": n} for f, n in wrong.items()]

    def check_code(self) -> list[dict]:
        """Code is compared line by line with the canon, including indentation width."""
        findings = []
        stream_lines = Counter()
        for chars in self.chars_by_page.values():
            rows: dict[int, list[dict]] = {}
            for c in chars:
                rows.setdefault(round((c["top"] + c["bottom"]) / 2 / 2), []).append(c)
            for row in rows.values():
                row.sort(key=lambda c: c["x0"])
                stream_lines[_norm("".join(c["text"] for c in row))] += 1
        for unit in self.units:
            if unit["region"] != "code":
                continue
            for text in unit["lines"]:
                key = _norm(text)
                if key and stream_lines[key] == 0:
                    findings.append({"check": "code_line", "unit": unit["unit_id"], "line": text})
        return findings

    def check_figures(self, expected: int) -> list[dict]:
        """Every registered picture is on some page. Text checks cannot see a lost image.

        Identical files share one PDF object, so `expected` counts distinct pictures.
        """
        pdf = pikepdf.open(self.pdf_path)
        seen = set()
        for page in pdf.pages:
            xobjects = page.get("/Resources", {}).get("/XObject", {})
            for obj in dict(xobjects).values():
                if str(obj.get("/Subtype")) == "/Image":
                    seen.add(obj.objgen)
        if len(seen) < expected:
            return [{"check": "figure_lost", "expected": expected, "found": len(seen)}]
        return []

    def check_style(self, style: dict | None = None) -> list[dict]:
        """The design is measured against the source: page size, body size, type area."""
        style = style or DESIGN
        findings = []
        pdf = pikepdf.open(self.pdf_path)
        for number, page in enumerate(pdf.pages, start=1):
            box = [float(x) for x in page.MediaBox]
            w, h = box[2] - box[0], box[3] - box[1]
            if abs(w - style["page"][0]) > 0.1 or abs(h - style["page"][1]) > 0.1:
                findings.append(
                    {"check": "page_size", "page": number, "size": [round(w, 1), round(h, 1)]}
                )
                break
        sizes: Counter = Counter()
        lefts: Counter = Counter()
        for chars in self.chars_by_page.values():
            for c in chars:
                if c["fontname"].split("+")[-1].startswith(style["body_font"]):
                    sizes[round(c["size"], 1)] += 1
                    if c["top"] >= HEADER_BOTTOM:
                        lefts[round(c["x0"])] += 1
        if sizes:
            body = sizes.most_common(1)[0][0]
            if abs(body - style["body_size"]) > 0.11:
                findings.append(
                    {"check": "body_size", "found": body, "expected": style["body_size"]}
                )
        if lefts and abs(min(lefts) - style["margin_left"]) > 2:
            findings.append(
                {"check": "margin", "found": min(lefts), "expected": style["margin_left"]}
            )
        return findings

    def check_toc(self, toc_pages: int | None = None) -> list[dict]:
        """Every listed heading is in the table of contents with the page it is really on."""
        findings = []
        page_text = {
            n: _norm("".join(c["text"] for c in self.chars_by_page.get(n, [])))
            for n in (range(1, toc_pages + 1) if toc_pages else sorted(self.toc_pages))
        }
        toc_text = "".join(page_text.values())
        for unit in self.units:
            if not unit.get("in_toc"):
                continue
            pos = self.positions.get(unit["unit_id"])
            title = _norm(_unit_text(unit))
            if pos and title and title + str(pos["page"]) not in toc_text:
                findings.append(
                    {
                        "check": "toc",
                        "unit": unit["unit_id"],
                        "title": title[:40],
                        "page": pos["page"],
                    }
                )
        return findings

    def check_link_targets(self) -> list[dict]:
        """An internal link must land on an existing page of this document."""
        pdf = pikepdf.open(self.pdf_path)
        pages = {p.objgen for p in pdf.pages}
        names = {}
        if "/Names" in pdf.Root and "/Dests" in pdf.Root.Names:
            names = dict(pikepdf.NameTree(pdf.Root.Names.Dests).items())
        findings = []
        for number, page in enumerate(pdf.pages, start=1):
            for annot in page.get("/Annots", []) or []:
                action = annot.get("/A")
                dest = annot.get("/Dest")
                if action is not None and str(action.get("/S")) == "/GoTo":
                    dest = action.D
                if dest is None:
                    continue
                if not isinstance(dest, pikepdf.Array):
                    dest = names.get(str(dest))
                    if isinstance(dest, pikepdf.Dictionary):
                        dest = dest.D
                if dest is None or dest[0].objgen not in pages:
                    findings.append({"check": "link_target", "page": number})
        return findings

    def check_captions(self, objects: list[dict]) -> list[dict]:
        """A figure and its caption are on one page: a reader identifies a figure by its caption."""
        findings = []
        order = [u["unit_id"] for u in self.units]
        for obj in objects:
            fig = self.positions.get(obj["id"])
            if obj.get("kind") != "figure" or not fig or obj.get("after") not in order:
                continue
            after = order.index(obj["after"]) + 1
            caption = next(
                (u for u in self.units[after : after + 3] if u["region"] == "caption"), None
            )
            pos = self.positions.get(caption["unit_id"]) if caption else None
            if pos and pos["page"] != fig["page"]:
                findings.append(
                    {
                        "check": "caption_apart",
                        "figure": obj["id"],
                        "pages": [fig["page"], pos["page"]],
                    }
                )
        return findings

    def check_links(self) -> list[dict]:
        """Internal link targets must exist; external URIs must match the source set exactly."""
        pdf = pikepdf.open(self.pdf_path)
        uris = set()
        broken = 0
        for page in pdf.pages:
            for annot in page.get("/Annots", []) or []:
                action = annot.get("/A")
                if action is not None and str(action.get("/S")) == "/URI":
                    uris.add(str(action.URI))
                elif "/Dest" not in annot and action is None:
                    broken += 1
        expected = set()
        for unit in self.units:
            if unit["region"] == "toc":
                continue
            for span in unit.get("spans", {}).values():
                link = span.get("link") or span.get("data", {}).get("link")
                if link and "uri" in link:
                    expected.add(link["uri"])
        findings = [{"check": "uri_missing", "uri": u} for u in sorted(expected - uris)]
        findings += [{"check": "uri_extra", "uri": u} for u in sorted(uris - expected)]
        if broken:
            findings.append({"check": "link_without_target", "count": broken})
        return findings

    def run(self, expected_fonts: list[str], figures: int = 0) -> dict:
        checks = {
            "figures": self.check_figures(figures),
            "style": self.check_style(),
            "link_targets": self.check_link_targets(),
            "toc": self.check_toc(),
            "presence": self.check_presence(),
            "overflow": self.check_overflow(),
            "overlap": self.check_overlap(),
            "fonts": self.check_fonts(expected_fonts),
            "code": self.check_code(),
            "links": self.check_links(),
        }
        return {
            "pages": self.page_count,
            "counts": {k: len(v) for k, v in checks.items()},
            "fonts_seen": dict(self.fonts),
            "findings": checks,
        }


def load_units(workdir: Path) -> list[dict]:
    return [json.loads(x) for x in (workdir / "ir" / "units.jsonl").read_text("utf-8").splitlines()]
