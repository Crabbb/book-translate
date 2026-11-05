"""Read a tagged PDF: per-character facts (pdfplumber) and the logical structure tree (pikepdf).

A block is a structure element that directly holds content. Each block keeps its ancestors and
table position, because one style (LBody, P_-_Regular) can mean a cell, a list item or an index entry.
"""

from __future__ import annotations

import gzip
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber
import pikepdf

INLINE_ROLES = {"/Span", "/Link", "/Reference", "/Note", "/Quote", "/Code", "/Em", "/Strong"}


@dataclass
class Char:
    idx: int  # position on the page, stable address for the ledger
    text: str
    font: str
    size: float
    x0: float
    top: float
    x1: float
    bottom: float
    mcid: int | None
    under: bool = False


@dataclass
class Page:
    number: int
    width: float
    height: float
    chars: list[Char]
    rects: list[dict]
    images: list[dict]
    lines: list[dict] = field(default_factory=list)


@dataclass
class Block:
    style: str
    ancestors: tuple[str, ...]
    page: int
    items: list[tuple[int, int, tuple[str, ...]]] = field(default_factory=list)  # page, mcid, path
    links: list[dict] = field(default_factory=list)
    alt: str | None = None
    cell: tuple[int, int, int, bool] | None = None  # table, row, column, is_header


def dump_pages(pdf_path: Path, cache: Path) -> None:
    """Extract all characters once: pdfplumber is the slow step, everything else reads the cache."""
    with pdfplumber.open(pdf_path) as pdf, gzip.open(cache, "wt", encoding="utf-8") as out:
        for number, page in enumerate(pdf.pages, start=1):
            row = {
                "n": number,
                "w": float(page.width),
                "h": float(page.height),
                "chars": [
                    [
                        c["text"],
                        c["fontname"].split("+")[-1],
                        round(c["size"], 3),
                        round(c["x0"], 2),
                        round(c["top"], 2),
                        round(c["x1"], 2),
                        round(c["bottom"], 2),
                        c.get("mcid"),
                    ]
                    for c in page.chars
                ],
                "rects": [
                    {k: round(float(r[k]), 2) for k in ("x0", "top", "x1", "bottom")}
                    for r in page.rects
                ],
                "images": [
                    {k: round(float(i[k]), 2) for k in ("x0", "top", "x1", "bottom")}
                    for i in page.images
                ],
                "lines": [
                    {k: round(float(r[k]), 2) for k in ("x0", "top", "x1", "bottom")}
                    for r in [*page.lines, *page.rects]
                    if r["bottom"] - r["top"] < 1.5 and 0 < r["x1"] - r["x0"] < 300
                ],
            }
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            page.flush_cache()


def mark_underlined(chars: list[Char], lines: list[dict]) -> None:
    """A thin rule in the lower part of the glyph box or just below it. It sits inside the box,
    because the box includes the descender."""
    for line in lines:
        for c in chars:
            if (
                c.x0 >= line["x0"] - 1
                and c.x1 <= line["x1"] + 1
                and c.top + 0.6 * (c.bottom - c.top) <= line["top"] <= c.bottom + 3.5
            ):
                c.under = True


def load_pages(cache: Path) -> dict[int, Page]:
    pages: dict[int, Page] = {}
    with gzip.open(cache, "rt", encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            chars = [Char(i, *c) for i, c in enumerate(r["chars"])]
            mark_underlined(chars, r.get("lines", []))
            pages[r["n"]] = Page(
                r["n"], r["w"], r["h"], chars, r["rects"], r["images"], r.get("lines", [])
            )
    return pages


def is_tagged(pdf: pikepdf.Pdf) -> bool:
    return "/StructTreeRoot" in pdf.Root and "/MarkInfo" in pdf.Root


def _kids(node) -> list:
    k = node.get("/K")
    if k is None:
        return []
    return list(k) if isinstance(k, pikepdf.Array) else [k]


def _link_info(annot) -> dict:
    info = {"rect": [float(x) for x in annot.Rect]}
    action = annot.get("/A")
    if action is not None and str(action.get("/S")) == "/URI":
        info["uri"] = str(action.URI)
    elif action is not None and str(action.get("/S")) == "/GoTo":
        info["dest"] = str(action.D) if not isinstance(action.D, pikepdf.Array) else "explicit"
    elif "/Dest" in annot:
        info["dest"] = str(annot.Dest) if not isinstance(annot.Dest, pikepdf.Array) else "explicit"
    return info


def read_blocks(pdf: pikepdf.Pdf) -> list[Block]:
    page_of = {p.objgen: i for i, p in enumerate(pdf.pages, start=1)}
    blocks: list[Block] = []
    seen: set = set()
    table_no = row_no = col_no = -1

    def page_for(node, default):
        pg = node.get("/Pg")
        return page_of[pg.objgen] if pg is not None else default

    def visit_once(node) -> bool:
        og = node.objgen
        if og == (0, 0):
            return True
        if og in seen:
            return False
        seen.add(og)
        return True

    def collect(node, page, block: Block, path: tuple[str, ...]) -> None:
        if not isinstance(node, pikepdf.Dictionary):
            block.items.append((page, int(node), path))
            return
        kind = str(node.get("/Type", ""))
        if kind == "/MCR":
            block.items.append((page_for(node, page), int(node.MCID), path))
        elif kind == "/OBJR":
            obj = node.Obj
            if str(obj.get("/Subtype", "")) == "/Link":
                block.links.append({"page": page_for(node, page), **_link_info(obj)})
        elif str(node.get("/S", "")) in INLINE_ROLES:
            if visit_once(node):
                for kid in _kids(node):
                    collect(kid, page_for(node, page), block, path + (str(node.S),))
        elif visit_once(node):
            walk(node, page_for(node, page), block.ancestors + (block.style,))

    def walk(node, page, ancestors: tuple[str, ...]) -> None:
        nonlocal table_no, row_no, col_no
        style = str(node.get("/S", ""))
        page = page_for(node, page)
        if style == "/Table":
            table_no, row_no = table_no + 1, -1
        elif style == "/TR":
            row_no, col_no = row_no + 1, -1
        elif style in ("/TD", "/TH"):
            col_no += 1
        kids = _kids(node)
        holds_content = any(
            not isinstance(k, pikepdf.Dictionary)
            or str(k.get("/Type", "")) in ("/MCR", "/OBJR")
            or str(k.get("/S", "")) in INLINE_ROLES
            for k in kids
        )
        if holds_content or style == "/Figure":
            cell = None
            for a in reversed(ancestors):
                if a in ("/TD", "/TH"):
                    cell = (table_no, row_no, col_no, a == "/TH")
                    break
            block = Block(
                style, ancestors, page, alt=str(node.Alt) if "/Alt" in node else None, cell=cell
            )
            blocks.append(block)
            for kid in kids:
                collect(kid, page, block, ())
            return
        for kid in kids:
            if isinstance(kid, pikepdf.Dictionary) and visit_once(kid):
                walk(kid, page, ancestors + (style,))

    for top in _kids(pdf.Root.StructTreeRoot):
        walk(top, None, ())
    return blocks


def named_destinations(pdf: pikepdf.Pdf) -> dict[str, dict]:
    page_of = {p.objgen: i for i, p in enumerate(pdf.pages, start=1)}
    dests: dict[str, dict] = {}
    names = pdf.Root.get("/Names")
    if names is None or "/Dests" not in names:
        return dests
    for name, value in pikepdf.NameTree(names.Dests).items():
        target = value.D if isinstance(value, pikepdf.Dictionary) else value
        try:
            dests[str(name)] = {"page": page_of[target[0].objgen], "kind": str(target[1])}
        except (KeyError, IndexError, TypeError, AttributeError):
            dests[str(name)] = {"page": None, "kind": "broken"}
    return dests


def page_labels(pdf: pikepdf.Pdf) -> dict[int, str]:
    return {i: str(p.label) for i, p in enumerate(pdf.pages, start=1)}


def chars_by_mcid(page: Page) -> dict[int | None, list[Char]]:
    by: dict[int | None, list[Char]] = defaultdict(list)
    for c in page.chars:
        by[c.mcid].append(c)
    return by
