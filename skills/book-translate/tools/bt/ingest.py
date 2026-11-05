"""Turn a tagged PDF into the unit registry, object list and glyph ledger.

The invariant: every visible character is mapped to exactly one unit, to `service`, or to an
explicit decision. Anything else is reported, and an unknown style stops ingest.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pikepdf
import pypdfium2
import yaml

from . import pdfsrc
from .pdfsrc import Block, Char

OPEN, CLOSE = "⟦", "⟧"
CODE_LEADING = 12.33  # measured line step of code in this design


class IngestError(RuntimeError):
    pass


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class Run:
    text: str
    bold: bool = False
    italic: bool = False
    mono: bool = False
    link: dict | None = None
    under: bool = False

    def style(self) -> tuple:
        return (
            self.bold,
            self.italic,
            self.mono,
            self.under,
            json.dumps(self.link, sort_keys=True),
        )

    def styled(self) -> bool:
        return bool(self.bold or self.italic or self.mono or self.link or self.under)


@dataclass
class Draft:
    """A logical block before it becomes a unit."""

    region: str
    page: int
    top: float
    runs: list[Run] = field(default_factory=list)
    chars: list[Char] = field(default_factory=list)
    level: int | None = None
    marker: str | None = None
    marker_text: str | None = None
    cell: tuple | None = None
    bold_code: bool = False
    alt: str | None = None
    origin: str = "tree"
    lines: list[str] | None = None
    wraps: list[bool] | None = None
    style: str = ""


def _font_flags(font: str, profile: dict) -> tuple[bool, bool, bool]:
    f = profile["fonts"]
    bold = any(k in font for k in f["bold"])
    tail = font.split("-")[-1]
    italic = any(tail.endswith(k) for k in f["italic"])
    return bold, italic, f["mono"] in font


def _is_symbol(font: str, profile: dict) -> bool:
    return any(k in font for k in profile["fonts"]["symbol"])


def _link_for(char: Char, page_height: float, links: list[dict], page: int) -> dict | None:
    cx, cy = (char.x0 + char.x1) / 2, page_height - (char.top + char.bottom) / 2
    for link in links:
        if link["page"] != page:
            continue
        x0, y0, x1, y1 = link["rect"]
        if min(x0, x1) <= cx <= max(x0, x1) and min(y0, y1) <= cy <= max(y0, y1):
            if "uri" in link:
                return {"uri": link["uri"]}
            return {"dest": link.get("dest")}
    return None


def _merge_runs(runs: list[Run]) -> list[Run]:
    merged: list[Run] = []
    for run in runs:
        if merged and merged[-1].style() == run.style():
            merged[-1].text += run.text
        else:
            merged.append(run)
    return merged


def _normalize_runs(runs: list[Run]) -> list[Run]:
    """Move outer spaces out of styled runs, collapse repeats, keep the text otherwise untouched."""
    out: list[Run] = []
    for run in _merge_runs(runs):
        if run.styled():
            body = run.text.strip()
            if not body:
                out.append(Run(" "))
                continue
            if run.text[0].isspace():
                out.append(Run(" "))
            out.append(Run(body, run.bold, run.italic, run.mono, run.link, run.under))
            if run.text[-1].isspace():
                out.append(Run(" "))
        else:
            out.append(Run(run.text))
    out = _merge_runs(out)
    for run in out:
        if not run.mono:
            run.text = re.sub(r"\s+", " ", run.text)
    while out and not out[0].text.strip():
        out.pop(0)
    while out and not out[-1].text.strip():
        out.pop()
    if out and not out[0].styled():
        out[0].text = out[0].text.lstrip()
    if out and not out[-1].styled():
        out[-1].text = out[-1].text.rstrip()
    return [r for r in out if r.text]


def _runs_from_chars(
    items: list[tuple[Char, tuple[str, ...]]],
    page_of: list[int],
    pages,
    links,
    profile,
    under: bool = True,
) -> tuple[list[Run], str | None, list[Char]]:
    """Build styled runs. A leading /Span or symbol-font run is the list marker."""
    runs: list[Run] = []
    prev: Char | None = None
    prev_page = None
    marker_chars: list[str] = []
    in_marker = True
    used: list[Char] = []
    for (char, path), page_no in zip(items, page_of):
        used.append(char)
        if in_marker and ("/Span" in path or _is_symbol(char.font, profile)):
            marker_chars.append(char.text)
            prev, prev_page = char, page_no
            continue
        in_marker = False
        new_line = prev is not None and (prev_page != page_no or abs(prev.top - char.top) > 2.0)
        bold, italic, mono = _font_flags(char.font, profile)
        link = _link_for(char, pages[page_no].height, links, page_no)
        text = char.text
        if new_line:
            joined = (
                (runs and runs[-1].text.endswith("-"))
                or (runs and runs[-1].text[-1:].isspace())
                or (mono and link is not None and "uri" in link)
                or text.isspace()
            )
            if not joined and runs:
                runs[-1].text += " "
        runs.append(Run(text, bold, italic, mono, link, under and char.under))
        prev, prev_page = char, page_no
    if not runs and "".join(marker_chars).strip():
        # A whole paragraph inside /Span is text, not a marker: nothing follows it.
        plain_items = [(char, ()) for char, _ in items]
        return _runs_from_chars(plain_items, page_of, pages, links, profile, under)
    return _normalize_runs(runs), ("".join(marker_chars) or None), used


def _code_lines(chars: list[Char], column: float, left: float) -> tuple[list[str], list[bool]]:
    """Rebuild lines by glyph column: code indentation is exact only in column units.

    `left` is the code box edge for this page. A block whose lines are all indented must keep
    that indent, so the edge cannot be taken from the block itself.
    """
    rows: dict[float, list[Char]] = defaultdict(list)
    for c in chars:
        if c.text.strip():
            rows[round(c.top, 0)].append(c)
    if not rows:
        return [], []
    tops = sorted(rows)
    right = max(c.x1 for row in rows.values() for c in row)
    lines: list[str] = []
    wraps: list[bool] = []
    previous = None
    for top in tops:
        row = sorted(rows[top], key=lambda c: c.x0)
        if previous is not None:
            # A vertical gap of whole line heights is a blank source line: it has no glyphs.
            for _ in range(round((top - previous) / CODE_LEADING) - 1):
                lines.append("")
                wraps.append(False)
        previous = top
        buf: list[str] = []
        col = 0
        for c in row:
            target = round((c.x0 - left) / column)
            if target > col:
                buf.append(" " * (target - col))
                col = target
            buf.append(c.text)
            col += max(1, round((c.x1 - c.x0) / column))
        lines.append("".join(buf))
        # A remainder pushed against the right edge is a layout wrap, not a source line.
        indent = (row[0].x0 - left) / column
        wraps.append(indent >= 8 and abs(row[-1].x1 - right) < column and len(lines) > 1)
    return lines, wraps


def _spans(runs: list[Run]) -> tuple[str, dict]:
    """Flat spans: pairs for emphasis and links, atoms for inline code. Nothing can overlap."""
    parts: list[str] = []
    spans: dict[str, dict] = {}
    pair_no = atom_no = 0
    for run in runs:
        if run.mono:
            atom_no += 1
            key = f"a{atom_no}"
            data = {"text": run.text, "link": run.link}
            spans[key] = {
                "kind": "atom",
                "role": "inline_code",
                "data": data,
                "data_hash": sha(json.dumps(data, sort_keys=True, ensure_ascii=False)),
                "bold": run.bold,
            }
            parts.append(f"{OPEN}{key}{CLOSE}")
        elif run.bold or run.italic or run.link or run.under:
            pair_no += 1
            key = str(pair_no)
            role = "+".join(
                r
                for r, on in (
                    ("strong", run.bold),
                    ("em", run.italic),
                    ("underline", run.under),
                    ("link", run.link),
                )
                if on
            )
            spans[key] = {"kind": "pair", "role": role}
            if run.link:
                spans[key]["link"] = run.link
                spans[key]["data_hash"] = sha(json.dumps(run.link, sort_keys=True))
            parts.append(f"{OPEN}{key}{CLOSE}{run.text}{OPEN}/{key}{CLOSE}")
        else:
            parts.append(run.text)
    return "".join(parts), spans


INDEX_REFS = re.compile(r"\s+((?:\d+(?:-\d+)?)(?:,\s*\d+(?:-\d+)?)*)\s*$")


def _index_entries(
    chars: list[Char], char_pages: list[int], pages, links, labels, dests, profile
) -> list[dict]:
    """One structure element can hold several entries (a column break merges them): split by line.

    An entry ends on a line with page numbers. Every link target is kept with its entry.
    """
    lines: list[list[tuple[Char, int]]] = []
    prev = None
    for char, number in zip(chars, char_pages):
        if prev is None or prev[1] != number or abs(prev[0].top - char.top) > 2.0:
            lines.append([])
        lines[-1].append((char, number))
        prev = (char, number)
    entries: list[dict] = []
    current: list[tuple[Char, int]] = []
    for line in lines:
        current = current + line
        text = "".join(c.text for c, _ in line)
        if INDEX_REFS.search(text):
            entries.append(_index_entry(current, pages, links, labels, dests, profile))
            current = []
    if current:
        entries.append(_index_entry(current, pages, links, labels, dests, profile))
    return entries


def _index_entry(chars, pages, links, labels, dests, profile) -> dict:
    text = ""
    prev = None
    for char, number in chars:
        new_line = prev is not None and (prev[1] != number or abs(prev[0].top - char.top) > 2.0)
        if new_line and not text.endswith((" ", "-")):
            text += " "
        text += char.text
        prev = (char, number)
    text = re.sub(r"\s+", " ", text).strip()
    match = INDEX_REFS.search(text)
    term = text[: match.start()].strip() if match else text
    printed = re.split(r",\s*", match.group(1)) if match else []
    targets = []
    seen = set()
    for char, number in chars:
        if not char.text.strip():
            continue
        link = _link_for(char, pages[number].height, links, number)
        key = json.dumps(link, sort_keys=True) if link else None
        if key and key not in seen:
            seen.add(key)
            page = dests.get(link.get("dest") or "", {}).get("page")
            targets.append({"page": page, "label": labels.get(page) if page else None})
    bold, _, _ = _font_flags(chars[0][0].font, profile)
    return {"term": term, "printed": printed, "targets": targets, "main": bold}


class BookIngest:
    def __init__(self, pdf_path: Path, workdir: Path, profile_path: Path):
        self.pdf_path = pdf_path
        self.workdir = workdir
        self.profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        self.anomalies: list[dict] = []
        self.decisions: list[dict] = []
        self.ledger: dict[int, dict[int, str]] = defaultdict(dict)

    def _clean_links(self, blocks: list[Block]) -> None:
        """Source defects in URIs are fixed openly: a broken target would mislead the reader."""
        for block in blocks:
            for link in block.links:
                uri = link.get("uri")
                if uri is None:
                    continue
                clean = uri.replace("ï»¿", "").replace("\ufeff", "")
                if clean.startswith("mailto:http"):
                    clean = clean[len("mailto:") :]
                if clean != uri:
                    self.anomalies.append(
                        {
                            "kind": "bad_uri",
                            "page": link["page"],
                            "detail": f"{uri} -> {clean}",
                            "decision": "fix",
                        }
                    )
                    link["uri"] = clean

    def _check_index_refs(self, index: list[dict]) -> None:
        """A printed number that differs from its link target is a source defect to decide on."""
        for entry in index:
            targets = [t["label"] for t in entry["targets"]]
            expected: list[str] = []
            for printed in entry["printed"]:
                expected += printed.split("-") if "-" in printed else [printed]
            if targets and expected != targets:
                self.anomalies.append(
                    {
                        "kind": "index_ref_mismatch",
                        "unit": entry["unit_id"],
                        "detail": f"{entry['term']}: printed {expected}, targets {targets}",
                        "decision": "open",
                    }
                )

    def _assign(self, page: int, char: Char, owner: str) -> None:
        current = self.ledger[page].get(char.idx)
        if current is not None and current != owner:
            raise IngestError(f"glyph {page}:{char.idx} mapped twice: {current} and {owner}")
        self.ledger[page][char.idx] = owner

    def run(self) -> dict:
        ir = self.workdir / "ir"
        ir.mkdir(parents=True, exist_ok=True)
        cache = ir / "chars.jsonl.gz"
        if not cache.exists():
            pdfsrc.dump_pages(self.pdf_path, cache)
        self.pages = pdfsrc.load_pages(cache)
        pdf = pikepdf.open(self.pdf_path)
        if not pdfsrc.is_tagged(pdf):
            raise IngestError("PDF has no structure tree: untagged books are not supported in v0")
        self.labels = pdfsrc.page_labels(pdf)
        self.dests = pdfsrc.named_destinations(pdf)
        blocks = pdfsrc.read_blocks(pdf)
        self._check_styles(blocks)
        self._clean_links(blocks)
        drafts = self._drafts(blocks)
        drafts = self._add_untagged(drafts, blocks)
        drafts = self._finish(drafts)
        units, objects, index = self._units(drafts)
        self._check_index_refs(index)
        self._mark_toc(units)
        units = self._alt_units(units, objects)
        self._write(ir, units, objects, index)
        report = self._report(ir, units, objects, index)
        return report

    def _check_styles(self, blocks: list[Block]) -> None:
        unknown = {b.style for b in blocks} - set(self.profile["styles"])
        if unknown:
            raise IngestError(f"styles missing from the profile: {sorted(unknown)}")

    def _region(self, block: Block) -> tuple[str, dict]:
        meta = dict(self.profile["styles"][block.style])
        for ancestor, region in self.profile["ancestors"].items():
            if ancestor in block.ancestors:
                meta["region"] = region
        if block.cell:
            meta["cell"] = True
        return meta["region"], meta

    def _drafts(self, blocks: list[Block]) -> list[Draft]:
        by_page = {n: pdfsrc.chars_by_mcid(p) for n, p in self.pages.items()}
        drafts: list[Draft] = []
        for block in blocks:
            region, meta = self._region(block)
            items: list[tuple[Char, tuple[str, ...]]] = []
            page_of: list[int] = []
            for page, mcid, path in block.items:
                for char in by_page[page].get(mcid, []):
                    items.append((char, path))
                    page_of.append(page)
            if region == "figure":
                drafts.append(Draft("figure", block.page, 0.0, alt=block.alt, style=block.style))
                continue
            if not items:
                continue  # empty structure element, e.g. the code placeholders on p. 127
            first_page = page_of[0]
            draft = Draft(
                region,
                first_page,
                items[0][0].top,
                level=meta.get("level"),
                marker=meta.get("marker"),
                cell=block.cell,
                bold_code=bool(meta.get("bold")),
                style=block.style,
            )
            draft.chars = [c for c, _ in items]
            draft._pages = page_of  # type: ignore[attr-defined]
            draft._items = items  # type: ignore[attr-defined]
            draft._links = block.links  # type: ignore[attr-defined]
            if block.style == "/IMG_-_Figure":
                self.anomalies.append(
                    {
                        "kind": "style_misuse",
                        "page": first_page,
                        "detail": "prose carries a figure style",
                        "decision": "fix",
                    }
                )
            drafts.append(draft)
        return drafts

    def _add_untagged(self, drafts: list[Draft], blocks: list[Block]) -> list[Draft]:
        """Characters outside the tree: running heads are service, code and prose are real content."""
        reached: dict[int, set] = defaultdict(set)
        for block in blocks:
            for page, mcid, _ in block.items:
                reached[page].add(mcid)
        zones = self.profile["zones"]
        toc_pages = {d.page for d in drafts if d.region == "toc"}
        extra: dict[int, list[Draft]] = defaultdict(list)
        for number, page in self.pages.items():
            loose = [c for c in page.chars if c.mcid is None or c.mcid not in reached[number]]
            rows: dict[float, list[Char]] = defaultdict(list)
            for char in loose:
                if not char.text.strip():
                    continue
                if char.top < zones["running_head_bottom"]:
                    self._assign(number, char, "service:running_head")
                elif number in toc_pages:
                    self._assign(number, char, "service:toc_number")
                else:
                    rows[round(char.top)].append(char)
            for top in sorted(rows):
                row = sorted(rows[top], key=lambda c: c.x0)
                if self.profile["fonts"]["mono"] in row[0].font:
                    extra[number].append(
                        Draft(
                            "code",
                            number,
                            row[0].top,
                            chars=row,
                            origin="geometry",
                            bold_code="Bold" in row[0].font,
                        )
                    )
                    continue
                host = self._attach(row, number, drafts)
                if host is not None:
                    # Keep spaces of this line, then restore reading order inside the paragraph.
                    line = [c for c in loose if abs(c.top - row[0].top) < 2]
                    merged = host._items + [(c, ()) for c in line]  # type: ignore[attr-defined]
                    pages = host._pages + [number] * len(line)  # type: ignore[attr-defined]
                    pairs = sorted(
                        zip(merged, pages), key=lambda m: (m[1], round(m[0][0].top), m[0][0].x0)
                    )
                    host._items = [m[0] for m in pairs]  # type: ignore[attr-defined]
                    host._pages = [m[1] for m in pairs]  # type: ignore[attr-defined]
                    host.chars = [c for c, _ in host._items]  # type: ignore[attr-defined]
                    host.origin = "mixed"
                    self.anomalies.append(
                        {
                            "kind": "untagged_text_attached",
                            "page": number,
                            "detail": "".join(c.text for c in line)[:80],
                            "decision": "fix",
                        }
                    )
                    continue
                extra[number].append(
                    Draft(
                        "orphan",
                        number,
                        row[0].top,
                        chars=row,
                        runs=[Run("".join(c.text for c in row))],
                        origin="geometry",
                    )
                )
        # Insert by position among tree blocks of the same page.
        result: list[Draft] = []
        pending = {n: list(v) for n, v in extra.items()}
        for draft in drafts:
            queue = pending.get(draft.page, [])
            if draft.region != "figure":
                while queue and queue[0].top < draft.top and draft.cell is None:
                    result.append(queue.pop(0))
            result.append(draft)
        for number in sorted(pending):
            if pending[number]:
                anchor = max(
                    (i for i, d in enumerate(result) if d.page <= number), default=len(result) - 1
                )
                result[anchor + 1 : anchor + 1] = pending[number]
        return self._merge_code(result)

    def _finish(self, drafts: list[Draft]) -> list[Draft]:
        """Build runs once all characters are in place; split merged index entries."""
        out: list[Draft] = []
        for draft in drafts:
            if draft.region in ("figure", "code") or draft.origin == "geometry":
                out.append(draft)
                continue
            items = draft._items  # type: ignore[attr-defined]
            pages = draft._pages  # type: ignore[attr-defined]
            links = draft._links  # type: ignore[attr-defined]
            if draft.region == "index":
                chars = [c for c, _ in items]
                entries = _index_entries(
                    chars, pages, self.pages, links, self.labels, self.dests, self.profile
                )
                # Split the block back into one draft per entry.
                start = 0
                for entry in entries:
                    size = self._entry_size(items, start, entry)
                    part = Draft("index", pages[start], items[start][0].top, style=draft.style)
                    part.chars = chars[start : start + size]
                    part._pages = pages[start : start + size]  # type: ignore[attr-defined]
                    part.runs, part.marker_text, _ = _runs_from_chars(
                        items[start : start + size],
                        part._pages,
                        self.pages,
                        [],
                        self.profile,
                        under=False,
                    )
                    self._plain_style(part)
                    part._index = entry  # type: ignore[attr-defined]
                    out.append(part)
                    start += size
                continue
            # In a table, cell borders sit right under the last line and look like underlines.
            draft.runs, draft.marker_text, _ = _runs_from_chars(
                items, pages, self.pages, links, self.profile, under=draft.cell is None
            )
            if draft.region not in ("body", "list", "note", "quote"):
                self._plain_style(draft)
            out.append(draft)
        return out

    @staticmethod
    def _entry_size(items, start: int, entry: dict) -> int:
        """Characters from `start` up to the end of the line closing this entry."""
        target = "".join(entry["term"].split()) + "".join(entry["printed"]).replace(",", "")
        seen = 0
        for i in range(start, len(items)):
            ch = items[i][0].text
            if ch.strip() and ch != ",":
                seen += len(ch)
            if seen >= len(target):
                j = i + 1
                while j < len(items) and not items[j][0].text.strip():
                    j += 1
                return j - start
        return len(items) - start

    @staticmethod
    def _plain_style(draft: Draft) -> None:
        """Bold in a heading or caption is its paragraph style, not an inline span."""
        for flag in ("bold", "italic"):
            if draft.runs and all(getattr(r, flag) for r in draft.runs):
                for r in draft.runs:
                    setattr(r, flag, False)
        draft.runs = _merge_runs(draft.runs)

    def _attach(self, row: list[Char], number: int, drafts: list[Draft]) -> Draft | None:
        """A loose line that sits inside a tagged paragraph belongs to that paragraph."""
        top = row[0].top
        for draft in drafts:
            if draft.region in ("figure", "code", "toc") or draft.page != number:
                continue
            tops = [c.top for c, n in zip(draft.chars, draft._pages) if n == number]  # type: ignore[attr-defined]
            if tops and min(tops) - 2 <= top <= max(tops) + 14:
                return draft
        return None

    def _code_edges(self, drafts: list[Draft]) -> dict[int, float]:
        """The code box text edge per page parity: the leftmost code glyph in the book."""
        edges: dict[int, float] = {}
        for draft in drafts:
            if draft.region == "code":
                for char in draft.chars:
                    if char.text.strip():
                        parity = self._page_of_char(char, draft) % 2
                        edges[parity] = min(edges.get(parity, char.x0), char.x0)
        return edges

    def _merge_code(self, drafts: list[Draft]) -> list[Draft]:
        """Join adjacent code lines into one block; a real vertical gap starts a new block."""
        column = self.profile["zones"]["code_column_pt"]
        out: list[Draft] = []
        for draft in drafts:
            prev = out[-1] if out else None
            if (
                draft.region == "code"
                and prev is not None
                and prev.region == "code"
                and prev.bold_code == draft.bold_code
                and (prev.page != draft.page or draft.top - prev.chars[-1].bottom < 16)
            ):
                prev.chars = prev.chars + draft.chars
                prev.origin = "mixed" if prev.origin != draft.origin else prev.origin
                continue
            out.append(draft)
        edges = self._code_edges(out)
        for draft in out:
            if draft.region == "code":
                by_page: dict[int, list[Char]] = defaultdict(list)
                for c in draft.chars:
                    by_page[self._page_of_char(c, draft)].append(c)
                lines: list[str] = []
                wraps: list[bool] = []
                for page in sorted(by_page):
                    part, w = _code_lines(by_page[page], column, edges[page % 2])
                    lines += part
                    wraps += w
                draft.lines, draft.wraps = lines, wraps
        return out

    def _page_of_char(self, char: Char, draft: Draft) -> int:
        pages = getattr(draft, "_char_pages", None)
        if pages is None:
            pages = {}
            for number in {draft.page, draft.page + 1, draft.page + 2}:
                page = self.pages.get(number)
                if page:
                    for c in page.chars:
                        pages[id(c)] = number
            draft._char_pages = pages  # type: ignore[attr-defined]
        return pages.get(id(char), draft.page)

    def _units(self, drafts: list[Draft]) -> tuple[list[dict], list[dict], list[dict]]:
        units: list[dict] = []
        objects: list[dict] = []
        index: list[dict] = []
        chapter = 0
        seq = 0
        table_objects: dict[int, str] = {}
        figure_seen: Counter = Counter()
        part_re = re.compile(self.profile["part_title"])
        note_group = None
        for draft in drafts:
            if draft.region == "figure":
                figure_seen[draft.page] += 1
                imgs = sorted(self.pages[draft.page].images, key=lambda i: i["top"])
                pos = figure_seen[draft.page] - 1
                obj_id = f"fig-{len(objects):04d}"
                image = imgs[pos] if pos < len(imgs) else None
                if image is None:
                    self.anomalies.append(
                        {"kind": "figure_no_image", "page": draft.page, "decision": "keep"}
                    )
                objects.append(
                    {
                        "id": obj_id,
                        "kind": "figure",
                        "page": draft.page,
                        "bbox": image,
                        "alt": draft.alt,
                        "after": units[-1]["unit_id"] if units else None,
                    }
                )
                continue
            if draft.region == "heading" and draft.level == 1:
                chapter += 1
                seq = 0
            seq += 1
            uid = f"u-{chapter:02d}-{seq:04d}"
            if draft.region == "note_title":
                note_group = f"g-note-{uid}"
            unit = self._unit(draft, uid, chapter, part_re, note_group, table_objects, objects)
            if draft.region == "index":
                index.append({"unit_id": uid, **draft._index})  # type: ignore[attr-defined]
            units.append(unit)
            if draft.region not in ("note", "note_title"):
                note_group = None
            owner = uid if unit["class"] != "service" else f"service:{draft.region}"
            for char in draft.chars:
                if char.text.strip():
                    self._assign(self._page_of_char(char, draft), char, owner)
        return units, objects, index

    @staticmethod
    def _mark_toc(units: list[dict]) -> None:
        """A heading goes to the rebuilt table of contents only if the source one lists it."""

        def plain(text: str) -> str:
            return re.sub(r"[\W_]+", "", re.sub("⟦/?a?\\d+/?⟧", "", text)).lower()

        listed = {plain(u["src"]["text"]) for u in units if u["region"] == "toc"}
        for unit in units:
            if unit["region"] in ("heading", "part"):
                unit["in_toc"] = plain(unit["src"]["text"]) in listed

    def _alt_units(self, units: list[dict], objects: list[dict]) -> list[dict]:
        """Alt text is reader-facing but off the page. A copy of the caption reuses its translation."""
        position = {u["unit_id"]: i for i, u in enumerate(units)}
        extra: dict[int, list[dict]] = defaultdict(list)
        for obj in objects:
            alt = (obj.get("alt") or "").strip()
            if obj["kind"] != "figure" or not alt:
                continue
            start = position.get(obj.get("after"), -1) + 1
            caption = next((u for u in units[start : start + 3] if u["region"] == "caption"), None)
            plain = re.sub(r"\W+", "", alt).lower()
            if caption and plain == re.sub(r"\W+", "", caption["src"]["text"]).lower():
                obj["alt_from"] = caption["unit_id"]
                continue
            uid = f"u-alt-{obj['id']}"
            obj["alt_from"] = uid
            extra[start].append(
                {
                    "unit_id": uid,
                    "chapter": units[start - 1]["chapter"] if start else 0,
                    "page": obj["page"],
                    "region": "alt",
                    "class": "translate",
                    "origin": "tree",
                    "src": {"text": alt, "hash": sha(alt)},
                    "spans": {},
                }
            )
        out: list[dict] = []
        for i, unit in enumerate(units):
            out.extend(extra.get(i, []))
            out.append(unit)
        return out

    def _unit(self, draft, uid, chapter, part_re, note_group, table_objects, objects) -> dict:
        unit = {
            "unit_id": uid,
            "chapter": chapter,
            "page": draft.page,
            "region": draft.region,
            "class": "translate",
            "origin": draft.origin,
        }
        if draft.level:
            unit["level"] = draft.level
        if draft.region in ("note", "note_title"):
            unit["group"] = note_group
        if draft.cell:
            table, row, col, head = draft.cell
            tid = table_objects.get(table)
            if tid is None:
                tid = f"tab-{len(table_objects):03d}"
                table_objects[table] = tid
                objects.append({"id": tid, "kind": "table", "page": draft.page, "after": uid})
            parity = draft.page % 2  # odd and even source pages are offset by the trim box
            shift = self.profile["zones"].get("odd_page_shift", 0) if parity else 0
            xs = [(c.x0 - shift, c.x1 - shift) for c in draft.chars if c.text.strip()]
            box = [min(x[0] for x in xs), max(x[1] for x in xs)] if xs else None
            unit.update(
                region="table",
                group=tid,
                cell={"row": row, "col": col, "header": head, "box": box},
            )
        if draft.marker:
            unit["marker"] = {"kind": draft.marker, "text": draft.marker_text}
        if draft.region == "code":
            text = "\n".join(draft.lines or [])
            unit.update(
                {
                    "class": "keep",
                    "lines": draft.lines,
                    "wraps": draft.wraps,
                    "bold": draft.bold_code,
                    "src": {"text": text, "hash": sha(text)},
                    "spans": {},
                }
            )
            return unit
        text, spans = _spans(draft.runs)
        if not text.strip() and "".join(c.text for c in draft.chars).strip():
            # The ledger counts glyphs per unit, so it cannot see text lost after that point.
            raise IngestError(f"page {draft.page}: glyphs of {uid} have no text: {draft.region}")
        if draft.region == "index":
            # Page numbers are rebuilt; only the term is text. A letter heading is rebuilt too.
            entry = draft._index  # type: ignore[attr-defined]
            text, spans = entry["term"], {}
            if not entry["printed"] and not entry["targets"] and len(text) == 1:
                unit["class"] = "service"
        unit["src"] = {"text": text, "hash": sha(text)}
        unit["spans"] = spans
        if draft.region == "heading" and part_re.match(text):
            unit["region"] = "part"
        if draft.region == "toc":
            unit["class"] = "service"
        if draft.region == "chapter_no":
            unit["class"] = "keep"
        if draft.region == "orphan":
            self.decisions.append(
                {
                    "kind": "orphan_text",
                    "unit_id": uid,
                    "page": draft.page,
                    "text": text,
                    "status": "open",
                }
            )
        return unit

    def _write(self, ir: Path, units: list[dict], objects: list[dict], index: list[dict]) -> None:
        def dump(path: Path, rows: list[dict]) -> None:
            tmp = path.with_suffix(path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8", newline="\n") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            tmp.replace(path)

        dump(ir / "units.jsonl", units)
        dump(ir / "objects.jsonl", objects)
        dump(ir / "index.jsonl", index)
        (ir / "dests.json").write_text(json.dumps(self.dests), encoding="utf-8")
        dump(self.workdir / "anomalies.jsonl", self.anomalies)
        dump(self.workdir / "decisions.jsonl", self.decisions)
        with gzip.open(ir / "ledger.jsonl.gz", "wt", encoding="utf-8") as f:
            for number in sorted(self.pages):
                f.write(json.dumps({"page": number, "map": self._ranges(number)}) + "\n")

    def _ranges(self, number: int) -> list:
        ranges: list[list] = []
        for char in self.pages[number].chars:
            owner = self.ledger[number].get(char.idx, "space" if not char.text.strip() else None)
            if ranges and ranges[-1][2] == owner and ranges[-1][1] == char.idx - 1:
                ranges[-1][1] = char.idx
            else:
                ranges.append([char.idx, char.idx, owner])
        return ranges

    def _report(self, ir: Path, units, objects, index) -> dict:
        unmapped = []
        for number, page in self.pages.items():
            for char in page.chars:
                if char.text.strip() and char.idx not in self.ledger[number]:
                    unmapped.append((number, char.idx, char.text, char.font))
        regions = Counter(u["region"] for u in units)
        classes = Counter(u["class"] for u in units)
        report = {
            "pages": len(self.pages),
            "units": len(units),
            "regions": dict(regions),
            "classes": dict(classes),
            "objects": dict(Counter(o["kind"] for o in objects)),
            "index_entries": len(index),
            "unmapped_glyphs": len(unmapped),
            "unmapped_sample": unmapped[:20],
            "open_decisions": sum(d["status"] == "open" for d in self.decisions),
            "anomalies": len(self.anomalies),
            "pdfium": self._pdfium_check(),
        }
        (self.workdir / "reports").mkdir(exist_ok=True)
        (self.workdir / "reports" / "ingest.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return report

    def _pdfium_check(self) -> dict:
        """An independent engine must see the same text, apart from end-of-line hyphens."""
        doc = pypdfium2.PdfDocument(str(self.pdf_path))
        diffs = []
        total_a = total_b = 0
        for number, page in self.pages.items():
            tp = doc[number - 1].get_textpage()
            a = Counter(
                c
                for c in unicodedata.normalize("NFKC", tp.get_text_bounded())
                if not c.isspace() and c not in "\x02￾"
            )
            b = Counter(
                c
                for ch in page.chars
                for c in unicodedata.normalize("NFKC", ch.text)
                if not c.isspace()
            )
            total_a += sum(a.values())
            total_b += sum(b.values())
            d = {k: v for k, v in (b - a).items()}
            d2 = {k: v for k, v in (a - b).items()}
            if d or d2:
                diffs.append({"page": number, "only_plumber": d, "only_pdfium": d2})
        other = [x for x in diffs if set(x["only_plumber"]) - {"-"} or x["only_pdfium"]]
        return {
            "pdfium_chars": total_a,
            "pdfplumber_chars": total_b,
            "pages_differ": len(diffs),
            "unexplained": other[:20],
            "unexplained_pages": len(other),
        }
