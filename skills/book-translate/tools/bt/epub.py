"""Registry and round-trip builder for reflowable EPUB 3."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import os
import posixpath
import re
import subprocess
import tempfile
from collections import Counter
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import ZIP_STORED, ZipFile

from lxml import etree
from PIL import Image, UnidentifiedImageError

from . import batch, store

CONTAINER = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF = "http://www.idpf.org/2007/opf"
DC = "http://purl.org/dc/elements/1.1/"
XHTML = "http://www.w3.org/1999/xhtml"
EPUB = "http://www.idpf.org/2007/ops"
NCX = "http://www.daisy.org/z3986/2005/ncx/"
XML = "http://www.w3.org/XML/1998/namespace"
MIMETYPE = b"application/epub+zip"
BLOCKS = {
    "p": "body",
    "li": "list",
    "dt": "list",
    "dd": "list",
    "figcaption": "caption",
    "caption": "caption",
    "td": "table",
    "th": "table",
    "blockquote": "quote",
    "pre": "body",
    "title": "title",
    **{f"h{i}": "heading" for i in range(1, 7)},
}
CONTAINERS = {"address", "article", "aside", "body", "div", "footer", "header", "section"}
ATOMS = {"br", "wbr", "img", "code"}
FIGURE_MARKER = "data-bt-figure"


class EpubError(ValueError):
    pass


def tag(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def local(node) -> str:
    return etree.QName(node).localname if node is not None and isinstance(node.tag, str) else ""


def types(node) -> set[str]:
    return set((node.get(tag(EPUB, "type")) or "").split())


def sha(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def xml(data: bytes, path: str):
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    try:
        root = etree.fromstring(data, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise EpubError(f"{path}: {exc}") from exc
    info = root.getroottree().docinfo
    if info.doctype and (
        info.doctype != "<!DOCTYPE html>" or b"<!ENTITY" in data or info.system_url
    ):
        raise EpubError(f"{path}: unsupported document type")
    return root


def resolve(base: str, href: str) -> tuple[str | None, str]:
    uri = urlsplit(href)
    if uri.scheme or uri.netloc:
        return None, ""
    path = unquote(uri.path)
    target = posixpath.normpath(posixpath.join(posixpath.dirname(base), path)) if path else base
    if target.startswith(("/", "../")) or target == ".." or "\\" in target:
        raise EpubError(f"{base}: resource leaves the container: {href}")
    return target, unquote(uri.fragment)


def _inline(node):
    parts = []
    spans = {}
    originals = {}

    def read(parent):
        parts.append(parent.text or "")
        for child in parent:
            kind = local(child)
            number = len(spans) + 1
            atom = (
                not kind
                or kind in ATOMS
                or "pagebreak" in types(child)
                or not len(child)
                and not child.text
            )
            key = f"a{number}" if atom else str(number)
            data = {}
            if atom:
                data["text"] = "".join(child.itertext(with_tail=False)) if kind else ""
            if kind == "a" and child.get("href"):
                href = child.get("href")
                data["link"] = {"uri" if urlsplit(href).scheme else "dest": href}
            spans[key] = {
                "kind": "atom" if atom else "pair",
                "role": kind or "anchor",
                "data": data,
            }
            originals[key] = child
            parts.append(f"⟦{key}⟧")
            if not atom:
                read(child)
                parts.append(f"⟦/{key}⟧")
            parts.append(child.tail or "")

    read(node)
    return "".join(parts), spans, originals


def _render(node, unit: dict, target: str) -> None:
    problems = batch.check_spans(unit["spans"], target)
    if problems:
        raise EpubError(f"{unit['unit_id']}: " + "; ".join(problems))
    _, _, originals = _inline(node)
    node.text = None
    for child in list(node):
        node.remove(child)
    stack = [node]

    def append(text):
        parent = stack[-1]
        if len(parent):
            parent[-1].tail = (parent[-1].tail or "") + text
        else:
            parent.text = (parent.text or "") + text

    offset = 0
    for token in batch.SPAN_TOKEN.finditer(target):
        append(target[offset : token.start()])
        closing, key = token.groups()
        offset = token.end()
        if closing:
            stack.pop()
            continue
        child = copy.deepcopy(originals[key])
        child.tail = None
        if unit["spans"][key]["kind"] == "pair":
            child.text = None
            for nested in list(child):
                child.remove(nested)
        stack[-1].append(child)
        if unit["spans"][key]["kind"] == "pair":
            stack.append(child)
    append(target[offset:])


class Book:
    def __init__(self, archive: ZipFile):
        self.archive = archive
        entries = archive.infolist()
        self.names = {entry.filename for entry in entries}
        if (
            not entries
            or entries[0].filename != "mimetype"
            or entries[0].compress_type != ZIP_STORED
            or archive.read("mimetype") != MIMETYPE
        ):
            raise EpubError("invalid EPUB mimetype entry")
        if len(self.names) != len(entries) or any(
            name.startswith("/") or "\\" in name or ".." in name.split("/") for name in self.names
        ):
            raise EpubError("duplicate or unsafe paths in EPUB")
        if "META-INF/encryption.xml" in self.names or any(e.flag_bits & 1 for e in entries):
            raise EpubError("encrypted resources are outside the supported EPUB contour")
        if "META-INF/signatures.xml" in self.names:
            raise EpubError("signed EPUB is outside the supported contour")
        if sum(entry.file_size for entry in entries) > 512 * 1024**2:
            raise EpubError("EPUB content exceeds 512 MiB")
        container = self.read_xml("META-INF/container.xml")
        files = container.findall(f".//{tag(CONTAINER, 'rootfile')}")
        if len(files) != 1:
            raise EpubError("exactly one EPUB rendition is required")
        self.package_path = files[0].get("full-path", "")
        self.package = self.read_xml(self.package_path)
        if self.package.tag != tag(OPF, "package") or self.package.get("version") != "3.0":
            raise EpubError("EPUB 3 package is required")
        self.metadata = self.package.find(tag(OPF, "metadata"))
        spine = self.package.find(tag(OPF, "spine"))
        manifest = self.package.find(tag(OPF, "manifest"))
        if self.metadata is None or spine is None or manifest is None:
            raise EpubError("incomplete package document")
        if any(
            "pre-paginated" in (node.text or "")
            for node in self.metadata
            if node.get("property") == "rendition:layout"
        ):
            raise EpubError("fixed-layout EPUB is outside the supported contour")
        items = {}
        for node in manifest:
            if node.tag != tag(OPF, "item"):
                continue
            item_id, href, media = node.get("id"), node.get("href"), node.get("media-type")
            if not item_id or item_id in items or not href or not media:
                raise EpubError("manifest has a missing or duplicate item ID or href")
            path, fragment = resolve(self.package_path, href)
            properties = set(node.get("properties", "").split())
            if path not in self.names or fragment:
                raise EpubError(f"manifest resource is missing or unsupported: {href}")
            if properties & {"scripted", "remote-resources"} or node.get("media-overlay"):
                raise EpubError(f"active, remote or synchronized content is unsupported: {href}")
            items[item_id] = (path, media, properties)
        self.spine = []
        for ref in spine:
            if ref.tag != tag(OPF, "itemref"):
                continue
            item = items.get(ref.get("idref"))
            if not item or item[1] != "application/xhtml+xml":
                raise EpubError("spine must resolve to XHTML documents")
            if "rendition:layout-pre-paginated" in ref.get("properties", "").split():
                raise EpubError("fixed-layout spine item is unsupported")
            self.spine.append((item[0], ref.get("linear", "yes")))
        self.docs = {path: self.read_xml(path) for path, _ in self.spine}
        if not self.docs or len(self.docs) != len(self.spine):
            raise EpubError("empty spine or repeated content document")
        nav = []
        for path, media, properties in items.values():
            if "nav" in properties:
                nav.append(path)
            if (
                media in ("application/xhtml+xml", "application/x-dtbncx+xml")
                and path not in self.docs
            ):
                self.docs[path] = self.read_xml(path)
        if len(nav) != 1 or nav[0] not in self.docs:
            raise EpubError("exactly one navigation document is required")
        self.docs[self.package_path] = self.package
        self.assets = {
            path: media for path, media, _ in items.values() if media.startswith("image/")
        }
        self.figure_text = self._detach_figure_text()
        self.nodes = {path: list(root.iter()) for path, root in self.docs.items()}
        self.ids = {
            path: {node.get("id") for node in nodes if node.get("id")}
            for path, nodes in self.nodes.items()
        }
        self._check_links()

    def _detach_figure_text(self):
        supplements = []
        for path, root in self.docs.items():
            for node in list(root.iter(tag(XHTML, "span"))):
                image_id = node.get(FIGURE_MARKER)
                if image_id is None:
                    continue
                previous = node.getprevious()
                if local(previous) != "img" or len(node) or (node.tail or "").strip():
                    raise EpubError(f"{path}: invalid translated image caption")
                supplements.append((image_id, path, previous.get("src"), node.text or ""))
                node.getparent().remove(node)
        return supplements

    def images(self):
        for path, root in self.docs.items():
            for image in root.iter(tag(XHTML, "img")):
                asset, _ = resolve(path, image.get("src", ""))
                if asset not in self.assets:
                    raise EpubError(f"{path}: image is not declared in the manifest")
                yield asset, path, image

    def figures(self, units: list[dict]) -> list[dict]:
        occurrences = {}
        for asset, path, node in self.images():
            ordinal = self.nodes[path].index(node)
            occurrences.setdefault(asset, []).append(
                {"path": path, "node": ordinal, "src": node.get("src")}
            )
        figures = []
        for i, asset in enumerate(sorted(self.assets), 1):
            locations = occurrences.get(asset, [])
            anchor = None
            if locations:
                location = locations[0]
                choices = [u for u in units if u["epub"]["path"] == location["path"]]
                anchor = min(
                    choices,
                    key=lambda u: abs(u["epub"]["node"] - location["node"]),
                    default=None,
                )
            figures.append(
                {
                    "id": f"epub-{i:03d}",
                    "kind": "figure",
                    "epub_asset": asset,
                    "locations": locations,
                    "after": anchor["unit_id"] if anchor else None,
                }
            )
        return figures

    def read_xml(self, path):
        if path not in self.names:
            raise EpubError(f"missing XML document: {path}")
        return xml(self.archive.read(path), path)

    def _check_links(self):
        for path, nodes in self.nodes.items():
            identifiers = [node.get("id") for node in nodes if node.get("id")]
            if len(self.ids[path]) != len(identifiers):
                raise EpubError(f"{path}: duplicate XML ID")
            for node in nodes:
                for attr in ("href", "src"):
                    value = node.get(attr)
                    if not value:
                        continue
                    target, fragment = resolve(path, value)
                    if target is None:
                        continue
                    if (
                        target not in self.names
                        or fragment
                        and target in self.ids
                        and fragment not in self.ids[target]
                    ):
                        raise EpubError(f"{path}: unresolved {attr}={value!r}")

    def registry(self):
        units, records = [], []
        chapter, scene = 0, 1
        has_chapters = any("chapter" in types(n) for nodes in self.nodes.values() for n in nodes)
        seen_body = False
        backmatter = False
        for document, (path, nodes) in enumerate(self.nodes.items(), 1):
            is_xhtml = nodes[0].tag == tag(XHTML, "html")
            in_spine = path in dict(self.spine)
            chapter_nodes = [n for n in nodes if "chapter" in types(n)]
            if len(chapter_nodes) > 1:
                raise EpubError(f"{path}: multiple chapters in one content document")
            if chapter_nodes or not has_chapters and is_xhtml and in_spine and document > 1:
                chapter += 1
                scene = 1
                seen_body = True
            elif seen_body and not backmatter:
                chapter += 1
                backmatter = True
                scene = 1
            ordinal = {node: i for i, node in enumerate(nodes)}
            covered = set()
            for node in nodes:
                kind = local(node)
                if is_xhtml and kind in {
                    "script",
                    "iframe",
                    "object",
                    "embed",
                    "form",
                    "audio",
                    "video",
                }:
                    raise EpubError(f"{path}: unsupported active or multimedia element {kind}")
                if kind == "hr":
                    scene += 1
                if is_xhtml and (types(node) & {"page-list", "pagebreak"}):
                    covered.update(node.iter())
                region = None
                if is_xhtml and node not in covered and kind in BLOCKS.keys() | CONTAINERS | {"a"}:
                    if not any(
                        local(child) in BLOCKS.keys() | CONTAINERS
                        for child in node.iterdescendants()
                    ):
                        region = BLOCKS.get(kind, "body")
                elif (
                    path == self.package_path
                    and node.tag
                    in {tag(DC, key) for key in ("title", "description", "subject", "coverage")}
                    or node.tag == tag(NCX, "text")
                    and not any(
                        local(a) in {"pageTarget", "docAuthor"} for a in node.iterancestors()
                    )
                ):
                    region = "title" if local(node) == "title" else "body"
                fields = []
                if region:
                    value, spans, _ = _inline(node)
                    if batch.plain(value).strip():
                        fields.append(("", region, value, spans))
                    covered.update(node.iter())
                if is_xhtml and kind and "pagebreak" not in types(node):
                    for attr in ("alt", "title", "aria-label", "aria-description"):
                        value = node.get(attr, "")
                        if value.strip():
                            fields.append((attr, "alt", value, {}))
                for attr, role, value, spans in fields:
                    if batch.SPAN_TOKEN.search(value) and not spans:
                        raise EpubError(f"{path}: source text contains a reserved marker")
                    uid = f"u-{document:03d}-{ordinal[node]:05d}" + (f"-{attr}" if attr else "")
                    units.append(
                        {
                            "unit_id": uid,
                            "chapter": chapter,
                            "scene": scene,
                            "page": document,
                            "class": "translate",
                            "region": role,
                            "origin": "epub",
                            "src": {"text": value, "hash": store.digest(value)},
                            "spans": spans,
                            "epub": {"path": path, "node": ordinal[node], "attr": attr},
                        }
                    )
                    records.append((node, attr))
            if is_xhtml:
                for node in nodes:
                    if (
                        local(node) not in {"style"}
                        and node not in covered
                        and (node.text or "").strip()
                    ):
                        raise EpubError(f"{path}: unmapped text in {local(node)} #{ordinal[node]}")
                    parent = node.getparent()
                    if parent is not None and parent not in covered and (node.tail or "").strip():
                        raise EpubError(
                            f"{path}: unmapped text after {local(node)} #{ordinal[node]}"
                        )
        return units, records


def ingest(source: Path, ws: store.Workspace) -> dict:
    with ZipFile(source) as archive:
        book = Book(archive)
        if book.figure_text:
            raise EpubError("EPUB source has reserved image translation markers")
        corrupt = archive.testzip()
        if corrupt:
            raise EpubError(f"CRC error in {corrupt}")
        units, _ = book.registry()
        if not units:
            raise EpubError("empty EPUB translation registry")
        figures = book.figures(units)
        for figure in figures:
            data = archive.read(figure["epub_asset"])
            try:
                with Image.open(BytesIO(data)) as image:
                    image = image.convert("RGBA")
                    preview = BytesIO()
                    image.save(preview, format="PNG")
            except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
                raise EpubError(f"cannot prepare image preview: {figure['epub_asset']}") from exc
            output = ws.ir / "figures" / f"{figure['id']}.png"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(preview.getvalue())
        facts = {
            "format": "epub",
            "source_sha256": sha(source),
            "units": len(units),
            "documents": len(book.docs),
            "spine": book.spine,
            "regions": dict(Counter(u["region"] for u in units)),
            "unmapped_text": 0,
            "images": sorted(book.assets),
        }
    store.write_jsonl(ws.ir / "units.jsonl", units)
    store.write_jsonl(ws.ir / "objects.jsonl", figures)
    store.write_jsonl(ws.ir / "index.jsonl", [])
    store.write_atomic(ws.reports / "ingest.json", json.dumps(facts, ensure_ascii=False, indent=2))
    return facts


def _source_registry(book: Book, ws: store.Workspace) -> tuple[list[dict], list[tuple]]:
    units, records = book.registry()
    if units != store.read_jsonl(ws.ir / "units.jsonl"):
        raise EpubError("source registry differs from EPUB ingest output")
    base = {u["unit_id"] for u in units}
    if any(u["unit_id"] not in base and not u.get("epub_figure") for u in ws.units()):
        raise EpubError("workspace includes supplemental units without an EPUB location")
    return units, records


def _language(node, value: str) -> None:
    if node.tag == tag(NCX, "text"):
        # The NCX grammar puts language on the enclosing label or title.
        node = node.getparent()
    node.set(tag(XML, "lang"), value)
    if isinstance(node.tag, str) and node.tag.startswith(f"{{{XHTML}}}"):
        node.set("lang", value)


def _english_roundtrip(units: list[dict], records: list[tuple]) -> None:
    for unit, (node, attr) in zip(units, records, strict=True):
        if attr:
            if unit["src"]["text"] != node.get(attr):
                raise EpubError(f"{unit['unit_id']}: attribute round-trip changed")
            continue
        clone = copy.deepcopy(node)
        clone.tail = None
        before = etree.tostring(clone, method="c14n")
        _render(clone, unit, unit["src"]["text"])
        if etree.tostring(clone, method="c14n") != before:
            raise EpubError(f"{unit['unit_id']}: markup round-trip changed")


def _render_documents(
    book: Book,
    units: list[dict],
    records: list[tuple],
    accepted: dict,
    draft: bool,
    figures: dict,
):
    pairs = list(zip(units, records, strict=True))
    changed = set()
    for attrs in (True, False):
        for unit, (node, attr) in pairs:
            if bool(attr) != attrs or unit["unit_id"] not in accepted:
                continue
            result = accepted[unit["unit_id"]]
            value = result["tgt"]
            if attr:
                if batch.SPAN_TOKEN.search(value):
                    raise EpubError(f"{unit['unit_id']}: attribute contains markup tokens")
                node.set(attr, value)
            else:
                _render(node, unit, value)
                if not result.get("kept_original"):
                    _language(node, "ru")
            changed.add(unit["epub"]["path"])
    for asset, path, image in book.images():
        if asset not in figures:
            continue
        unit, _ = figures[asset]
        node = etree.Element(tag(XHTML, "span"), style="display: block")
        node.set(FIGURE_MARKER, unit["epub_figure"])
        node.text = accepted[unit["unit_id"]]["tgt"]
        _language(node, "ru")
        image.addnext(node)
        changed.add(path)
    if not draft:
        languages = book.metadata.findall(tag(DC, "language"))
        if not languages:
            raise EpubError("missing package language")
        for language in languages:
            if (language.text or "").lower().split("-")[0] == "en":
                language.text = "ru"
        stamp = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        for node in book.metadata.findall(tag(OPF, "meta")):
            if node.get("property") == "dcterms:modified":
                node.text = stamp
        for path, root in book.docs.items():
            _language(root, "ru")
            changed.add(path)
    return {
        path: etree.tostring(book.docs[path].getroottree(), encoding="UTF-8", xml_declaration=True)
        for path in changed
    }


def build(
    source: Path, ws: store.Workspace, lang: str, draft: bool, name: str, *, scope=None
) -> dict:
    if lang not in {"en", "ru"} or not re.fullmatch(r"[\w-]+", name):
        raise EpubError("invalid EPUB language or artifact name")
    facts = json.loads((ws.reports / "ingest.json").read_text("utf-8"))
    if facts.get("source_sha256") != sha(source):
        raise EpubError("source file differs from EPUB ingest")
    out = ws.root / "build"
    out.mkdir(exist_ok=True)
    path = out / f"{name}.epub"
    if path.resolve() == source.resolve():
        raise EpubError("build would overwrite the source EPUB")
    fd, temporary = tempfile.mkstemp(prefix=f".{name}-", suffix=".epub", dir=out)
    os.close(fd)
    try:
        with ZipFile(source) as original:
            book = Book(original)
            units, records = _source_registry(book, ws)
            accepted = {} if lang == "en" else ws.accepted()
            if lang == "ru" and scope is not None and set(accepted) != scope:
                raise EpubError("pilot translations must match the frozen scene selection")
            if lang == "en":
                _english_roundtrip(units, records)
                replacements = {}
            else:
                status = batch.status(ws)
                if status["stale_count"] or status["blocking_questions"]:
                    raise EpubError("EPUB build has stale translations or unresolved questions")
                if not draft and status["todo_count"]:
                    raise EpubError("final EPUB requires the complete translation")
                figures = _translated_figures(ws, accepted)
                replacements = _render_documents(book, units, records, accepted, draft, figures)
            with ZipFile(temporary, "w") as output:
                for info in original.infolist():
                    content = replacements.get(info.filename)
                    output.writestr(
                        copy.copy(info), original.read(info) if content is None else content
                    )
            with ZipFile(temporary) as verification:
                Book(verification)
                corrupt = verification.testzip()
                if corrupt:
                    raise EpubError(f"CRC error in rebuilt resource {corrupt}")
                for item in original.namelist():
                    expected = replacements.get(item)
                    if verification.read(item) != (
                        original.read(item) if expected is None else expected
                    ):
                        raise EpubError(f"rebuilt resource differs: {item}")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {
        "epub": str(path),
        "sha256": sha(path),
        "translated": len(accepted),
        "total_units": len(units),
        "draft": draft,
    }


def _targets(book: Book) -> Counter:
    return Counter(
        (path, local(node), name, val)
        for path, nodes in book.nodes.items()
        for node in nodes
        for name in ("id", "href", "src")
        if (val := node.get(name))
    )


def _visible_content(unit: dict, text: str) -> tuple[str, list]:
    """Recover text and semantic markup boundaries without running the renderer."""
    spans = unit["spans"]
    if not spans:
        return text, []
    chunks, boundaries = [], []
    source_end, position = 0, 0
    for marker in batch.SPAN_TOKEN.finditer(text):
        before = text[source_end : marker.start()]
        chunks.append(before)
        position += len(before)
        closing, key = marker.groups()
        span = spans[key]
        boundaries.append((position, bool(closing), span))
        if span["kind"] == "atom":
            inner = span["data"]["text"]
            chunks.append(inner)
            position += len(inner)
        source_end = marker.end()
    chunks.append(text[source_end:])
    return "".join(chunks), boundaries


def _readback(rebuilt: Book, units: list[dict], accepted: dict) -> list[dict]:
    actual, _ = rebuilt.registry()
    actual_by_id = {unit["unit_id"]: unit for unit in actual}
    expected = {unit["unit_id"] for unit in units}
    if actual_by_id.keys() != expected:
        missing = sorted(expected - actual_by_id.keys())[:5]
        extra = sorted(actual_by_id.keys() - expected)[:5]
        raise EpubError(f"EPUB registry differs: missing {missing}, extra {extra}")
    coverage = []
    for source in units:
        uid = source["unit_id"]
        output = actual_by_id[uid]
        result = accepted.get(uid)
        wanted, wanted_markup = _visible_content(
            source, result["tgt"] if result else source["src"]["text"]
        )
        read, read_markup = _visible_content(output, output["src"]["text"])
        if read != wanted or source["epub"] != output["epub"]:
            raise EpubError(f"{uid}: EPUB text differs from accepted content or source")
        if read_markup != wanted_markup:
            raise EpubError(f"{uid}: EPUB inline markup differs from accepted content or source")
        coverage.append(
            {
                "unit": uid,
                "location": output["epub"],
                "status": "translated" if result else "source",
                "visible_sha256": hashlib.sha256(read.encode("utf-8")).hexdigest(),
            }
        )
    return coverage


def _translated_figures(ws: store.Workspace, accepted: dict) -> dict:
    objects = {o["id"]: o for o in store.read_jsonl(ws.ir / "objects.jsonl") if "epub_asset" in o}
    translated = {}
    for unit in ws.units():
        figure_id = unit.get("epub_figure")
        if figure_id and unit["unit_id"] in accepted:
            obj = objects[figure_id]
            translated[obj["epub_asset"]] = (unit, obj)
    return translated


def _readback_figures(rebuilt: Book, ws: store.Workspace, accepted: dict) -> list[dict]:
    required = []
    rows = []
    for unit, figure in _translated_figures(ws, accepted).values():
        uid = unit["unit_id"]
        text = accepted[uid]["tgt"]
        for loc in figure["locations"]:
            required.append((figure["id"], loc["path"], loc["src"], text))
        rows.append(
            {
                "unit": uid,
                "locations": figure["locations"],
                "status": "translated",
                "visible_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )
    if Counter(required) != Counter(rebuilt.figure_text):
        raise EpubError("EPUB translated image text or its location differs from accepted content")
    return rows


def audit(source: Path, output: Path, ws: store.Workspace, *, selftest: bool) -> dict:
    checker = os.environ.get("BT_EPUBCHECK")
    if not checker:
        raise EpubError("set BT_EPUBCHECK to the local EPUBCheck executable or JAR")
    argv = ["java", "-jar", checker] if checker.lower().endswith(".jar") else [checker]
    with ZipFile(source) as original, ZipFile(output) as built:
        canonical, rebuilt = Book(original), Book(built)
        units, records = _source_registry(canonical, ws)
        if original.namelist() != built.namelist() or canonical.spine != rebuilt.spine:
            raise EpubError("EPUB member list or reading order changed")
        if _targets(canonical) != _targets(rebuilt):
            raise EpubError("EPUB links or anchors changed")
        corrupt = built.testzip()
        if corrupt:
            raise EpubError(f"CRC error in rebuilt resource {corrupt}")
        for name in original.namelist():
            if (selftest or name not in canonical.docs) and original.read(name) != built.read(name):
                raise EpubError(f"EPUB resource changed: {name}")
        if selftest:
            _english_roundtrip(units, records)
        accepted = {} if selftest else ws.accepted()
        coverage = _readback(rebuilt, units, accepted) + _readback_figures(rebuilt, ws, accepted)
    done = subprocess.run(
        [*argv, str(output.resolve())],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    log = done.stdout + done.stderr
    check_report = ws.reports / f"{output.stem}.epubcheck.txt"
    store.write_atomic(check_report, log)
    if done.returncode:
        raise EpubError(
            f"EPUBCheck failed with code {done.returncode}. Report: {output.stem}.epubcheck.txt"
        )
    warnings = [line for line in log.splitlines() if re.match(r"^(WARNING|WARN)\(", line)]
    findings = {store.digest(line): {"check": "epubcheck", "detail": line} for line in warnings}
    coverage_path = ws.reports / f"{output.stem}.coverage.jsonl"
    store.write_jsonl(coverage_path, coverage)
    return {
        "sha256": sha(output),
        "counts": {"epubcheck_warnings": len(warnings)},
        "findings": findings,
        "units": len(coverage),
        "readback": dict(Counter(row["status"] for row in coverage)),
        "coverage_file": str(coverage_path),
        "coverage_sha256": sha(coverage_path),
        "epubcheck_report": str(check_report),
        "epubcheck_sha256": sha(check_report),
    }
