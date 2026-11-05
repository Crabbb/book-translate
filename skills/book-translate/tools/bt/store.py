"""The book workspace on disk: atomic writes, one writer, and freshness computed from the files.

Nothing stores a "stale" flag. A result is current if the fingerprint computed from today's
decisions equals the one it was built with, so an interrupted write cannot leave a false "fresh".
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path

import yaml

# Fields that change what a correct translation is (spec 8.5). Everything else is commentary.
CONCEPT_NORM = (
    "status",
    "sense",
    "scope",
    "en",
    "rendering_policy",
    "variants",
    "forbidden",
    "first_mention",
    "applies_when",
    "not_when",
    "collocations",
    "supersedes",
)
CONCEPT_CHECK = ("polysemy", "competing")
RULE_NORM = ("status", "zones", "when", "do")
RULE_CHECK = ("check", "severity_on_violation")
VOICE_NORM = ("feature", "function", "ru_guidance")
VOICE_CHECK = ("violation",)
NOTE_NORM = ("type", "meaning", "strength", "strategy", "status", "src_hash")
PASSPORT_NORM = (
    "function",
    "content_correspondence",
    "address",
    "modal_map",
    "code_policy",
    "typography",
)


class WorkspaceLocked(RuntimeError):
    pass


def digest(value) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def project(obj: dict, fields: tuple[str, ...]) -> dict:
    return {k: obj[k] for k in fields if k in obj}


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    write_atomic(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


class Workspace:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.ir = self.root / "ir"
        self.bible = self.root / "bible"
        self.batches = self.root / "batches"
        self.results = self.root / "results"
        self.reports = self.root / "reports"

    @contextmanager
    def lock(self):
        """One writer at a time: a second one fails instead of racing."""
        path = self.root / ".writer.lock"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise WorkspaceLocked(f"another writer holds {path}") from error
        try:
            yield
        finally:
            os.close(fd)
            path.unlink(missing_ok=True)

    def units(self) -> list[dict]:
        """The registry, with figure text spliced in after its caption."""
        units = read_jsonl(self.ir / "units.jsonl")
        extra: dict[str, list[dict]] = {}
        for row in read_jsonl(self.bible / "figures.jsonl"):
            if row.get("has_text") and row.get("unit"):
                extra.setdefault(row["unit"]["after"], []).append(row["unit"])
        for row in read_jsonl(self.bible / "code_prompts.jsonl"):
            extra.setdefault(row["after"], []).append(row)
        if not extra:
            return units
        out: list[dict] = []
        for unit in units:
            out.append(unit)
            out.extend(extra.get(unit["unit_id"], []))
        return out

    def set_figures(self, rows: list[dict]) -> dict:
        """Every image gets a decision from looking at it; text on it becomes a unit to translate."""
        objects = {o["id"]: o for o in read_jsonl(self.ir / "objects.jsonl")}
        units = read_jsonl(self.ir / "units.jsonl")
        position = {u["unit_id"]: i for i, u in enumerate(units)}
        path = self.bible / "figures.jsonl"
        current = {r["id"]: r for r in read_jsonl(path)}
        problems = []
        for row in rows:
            obj = objects.get(row["id"])
            if obj is None or obj["kind"] != "figure":
                problems.append({"id": row["id"], "problem": "unknown figure"})
                continue
            image = self.ir / "figures" / f"{row['id']}.png"
            if not image.exists():
                problems.append({"id": row["id"], "problem": "image was not extracted"})
                continue
            entry = {
                "id": row["id"],
                "has_text": bool(row["has_text"]),
                "image_hash": hashlib.sha256(image.read_bytes()).hexdigest()[:16],
                "viewed_by": row.get("viewed_by", "agent"),
            }
            if row["has_text"]:
                text = (row.get("transcription") or "").strip()
                if not text:
                    problems.append({"id": row["id"], "problem": "has_text needs a transcription"})
                    continue
                start = position.get(obj.get("after"), -1) + 1
                caption = next(
                    (u for u in units[start : start + 3] if u["region"] == "caption"), None
                )
                if caption is None:
                    problems.append({"id": row["id"], "problem": "no caption to hold the text"})
                    continue
                entry["unit"] = {
                    "unit_id": f"u-fig-{row['id']}",
                    "chapter": caption["chapter"],
                    "page": caption["page"],
                    "region": "figure_text",
                    "class": "translate",
                    "origin": "view",
                    "after": caption["unit_id"],
                    "group": f"g-{row['id']}",
                    "src": {"text": text, "hash": digest(text)},
                    "spans": {},
                }
            current[row["id"]] = entry
        if problems:
            return {"ok": False, "problems": problems}
        write_jsonl(path, list(current.values()))
        figures = [o for o in objects.values() if o["kind"] == "figure"]
        return {"ok": True, "decided": len(current), "figures": len(figures)}

    def concepts(self) -> dict[str, dict]:
        return {c["id"]: c for c in read_jsonl(self.bible / "concepts.jsonl")}

    def notes(self) -> list[dict]:
        return read_jsonl(self.bible / "notes.jsonl")

    def questions(self) -> list[dict]:
        return read_jsonl(self.bible / "questions.jsonl")

    def passport(self) -> dict:
        path = self.bible / "passport.yaml"
        return yaml.safe_load(path.read_text("utf-8")) if path.exists() else {}

    def voice(self) -> dict:
        path = self.bible / "voice.yaml"
        return yaml.safe_load(path.read_text("utf-8")) if path.exists() else {}

    def journal(self, entry: dict) -> None:
        path = self.bible / "journal.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def accepted(self) -> dict[str, dict]:
        """Current result per unit, rebuilt from result files. The manifest is only a log."""
        out: dict[str, dict] = {}
        for row in read_jsonl(self.results / "accepted.jsonl"):
            path = self.results / row["batch"] / f"{row['hash']}.json"
            if not path.exists():
                continue
            payload = json.loads(path.read_text("utf-8"))
            for unit in payload["units"]:
                out[unit["id"]] = {**unit, "batch": row["batch"], "seq": row["seq"]}
        for row in read_jsonl(self.results / "edits.jsonl"):
            if row["id"] in out:
                out[row["id"]] = {**out[row["id"]], **row["patch"], "edited": row["step"]}
        return out


def applicable_rules(passport_doc: dict, region: str) -> list[dict]:
    return [
        r
        for r in passport_doc.get("rules", [])
        if r.get("status") == "active" and (not r.get("zones") or region in r["zones"])
    ]


def inputs_of(
    unit: dict, deps: list[str], ws: Workspace, concepts=None, doc=None, voice=None, notes=None
) -> dict:
    """Everything normative a translation of this unit depends on (spec 8.5)."""
    concepts = ws.concepts() if concepts is None else concepts
    doc = ws.passport() if doc is None else doc
    voice = ws.voice() if voice is None else voice
    notes = ws.notes() if notes is None else notes
    region = unit["region"]
    return {
        "concepts": {c: digest(project(concepts[c], CONCEPT_NORM)) for c in deps if c in concepts},
        "rules": {r["id"]: digest(project(r, RULE_NORM)) for r in applicable_rules(doc, region)},
        "voice": {
            v["id"]: digest(project(v, VOICE_NORM))
            for v in voice.get("voice_features", [])
            if not v.get("zones") or region in v["zones"]
        },
        "notes": {
            n["id"]: digest(project(n, NOTE_NORM)) for n in notes if n["occ"] == unit["unit_id"]
        },
        "passport": digest(project(doc.get("passport", {}), PASSPORT_NORM)),
    }


def translation_fp(unit: dict, deps: list[str], ws: Workspace, **loaded) -> str:
    return digest([unit["src"]["hash"], inputs_of(unit, deps, ws, **loaded)])


def checks_of(
    unit: dict, deps: list[str], ws: Workspace, concepts=None, doc=None, voice=None
) -> dict:
    """Inputs that change verdicts but not what the translation should be."""
    concepts = ws.concepts() if concepts is None else concepts
    doc = ws.passport() if doc is None else doc
    voice = ws.voice() if voice is None else voice
    uid = unit["unit_id"]
    out: dict = {"concepts": {}, "rules": {}, "voice": {}}
    for c in deps:
        concept = concepts.get(c)
        if concept:
            exceptions = [e for e in concept.get("exceptions", []) if e.get("unit") == uid]
            out["concepts"][c] = digest(
                {**project(concept, CONCEPT_CHECK), "exceptions": exceptions}
            )
    for rule in applicable_rules(doc, unit["region"]):
        out["rules"][rule["id"]] = digest(project(rule, RULE_CHECK))
    for feature in voice.get("voice_features", []):
        out["voice"][feature["id"]] = digest(project(feature, VOICE_CHECK))
    return out


def evidence_fp(
    unit: dict, tgt: str, deps: list[str], tool: str, tool_rev: str, ws: Workspace, **loaded
) -> str:
    return digest(
        [
            digest(tgt),
            translation_fp(unit, deps, ws, **loaded),
            checks_of(
                unit,
                deps,
                ws,
                concepts=loaded.get("concepts"),
                doc=loaded.get("doc"),
                voice=loaded.get("voice"),
            ),
            tool,
            tool_rev,
        ]
    )
