"""Changing a decision: find what it touches, freeze the list, close each item. Spec 8.5.

The list is the union of four independent searches, because each one misses what another finds.
It lives on disk, so an interrupted revision resumes instead of being forgotten.
"""

from __future__ import annotations

import datetime as dt
import json
import re

from . import batch as batch_mod
from . import bible
from .store import CONCEPT_NORM, Workspace, digest, project, read_jsonl, write_atomic


def _forms(concept: dict) -> set[str]:
    return {f.lower() for v in concept.get("variants", []) for f in v.get("forms", [])} | {
        f.lower() for f in concept.get("forbidden", [])
    }


def affected(ws: Workspace, old: dict | None, new: dict) -> dict:
    """Units a change can reach, with the search that found each. Same list on every call."""
    units = ws.units()
    accepted = ws.accepted()
    cid = new["id"]
    paths: dict[str, set[str]] = {
        "occurrences": set(),
        "source": set(),
        "target": set(),
        "derived": set(),
    }
    probes = [c for c in (old, new) if c]
    forms = set().union(*(_forms(c) for c in probes))
    for unit in units:
        uid = unit["unit_id"]
        result = accepted.get(uid)
        if result and any(t.get("c") == cid for t in result.get("terms", [])):
            paths["occurrences"].add(uid)
        for probe in probes:
            if bible.find_occurrences(unit["src"]["text"], {cid: {**probe, "status": "accepted"}}):
                paths["source"].add(uid)
        if result and forms:
            text = batch_mod.plain(result["tgt"]).lower()
            if any(batch_mod.count_word(text, f) for f in forms):
                paths["target"].add(uid)
    for note in ws.notes():
        if cid in note.get("links", {}).get("concept", []) and note["occ"] in accepted:
            paths["derived"].add(note["occ"])
    # A neighbour in the same package saw the term but does not depend on it. Counting those units
    # would pull most of a chapter into every revision, so this is reported and not planned.
    seen_in_context = set()
    for path in sorted(ws.batches.glob("b*.json")):
        doc = json.loads(path.read_text("utf-8"))
        if any(t.get("id") == cid for t in doc["package"].get("terms", [])):
            seen_in_context.update(u for u in doc["manifest"]["unit_ids"] if u in accepted)
    union = set().union(*paths.values())
    return {
        "units": sorted(union),
        "by_path": {k: len(v) for k, v in paths.items()},
        "seen_in_context_only": sorted(seen_in_context - union),
    }


def change(
    ws: Workspace, concept: dict, reason: str, dry_run: bool = True, actor: str = "agent"
) -> dict:
    """Dry-run shows the diff and the reach. Applying freezes the list as a revision plan."""
    old = ws.concepts().get(concept["id"])
    old_norm = project(old, CONCEPT_NORM) if old else {}
    new_norm = project(concept, CONCEPT_NORM)
    changed = sorted(k for k in set(old_norm) | set(new_norm) if old_norm.get(k) != new_norm.get(k))
    reach = affected(ws, old, concept)
    report = {"concept": concept["id"], "normative_fields": changed, **reach}
    # A change of meaning can touch places without any shared words.
    report["unknown_scope"] = bool(old) and old.get("sense") != concept.get("sense")
    if report["unknown_scope"]:
        chapters = {u["chapter"] for u in ws.units() if u["unit_id"] in reach["units"]}
        report["units"] = sorted(
            u["unit_id"]
            for u in batch_mod._select(ws.units(), None)
            if u["chapter"] in chapters and u["unit_id"] in ws.accepted()
        )
        report["note"] = "смена смысла: проверить все единицы затронутых глав и межглавный проход"
    if dry_run:
        return {"ok": True, "dry_run": True, **report}
    if not reason.strip():
        return {"ok": False, "problem": "a reason is required"}
    bible.add(ws, [{**concept, "reason": reason}], actor)
    if changed:
        open_plan(ws, f"concept:{concept['id']}", report["units"], reason)
    return {"ok": True, "dry_run": False, **report}


def open_plan(ws: Workspace, source: str, units: list[str], reason: str) -> None:
    path = ws.results / "revision_plans.jsonl"
    rows = read_jsonl(path)
    edits = read_jsonl(ws.results / "edits.jsonl")
    rows.append(
        {
            "id": f"rev-{len(rows) + 1:03d}",
            "source": source,
            "reason": reason,
            "units": units,
            "opened_after_edit": len(edits),
            "opened_after_accept": len(read_jsonl(ws.results / "accepted.jsonl")),
            "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }
    )
    write_atomic(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def open_items(ws: Workspace) -> list[dict]:
    """An item closes only by an edit made after the plan: changed, or confirmed with a reason."""
    edits = read_jsonl(ws.results / "edits.jsonl")
    accepted_units = ws.accepted()
    out = []
    for plan in read_jsonl(ws.results / "revision_plans.jsonl"):
        done = {e["id"] for e in edits[plan["opened_after_edit"] :]}
        for uid in plan["units"]:
            result = accepted_units.get(uid)
            # A unit first translated after the plan was built from the new decision already.
            if result and result["seq"] <= plan["opened_after_accept"] and uid not in done:
                out.append({"plan": plan["id"], "unit": uid})
    return out


def procedure_change(ws: Workspace, scope: str, reason: str) -> dict:
    """A change to how text is made. `none` keeps old results knowingly and says so in the report."""
    if not reason.strip():
        return {"ok": False, "problem": "a reason is required"}
    path = ws.results / "procedure_changes.jsonl"
    rows = read_jsonl(path)
    rev = f"proc-{len(rows) + 1:03d}"
    units: list[str] = []
    if scope == "all":
        units = sorted(ws.accepted())
    elif scope.startswith("zones:"):
        zones = set(scope.split(":", 1)[1].split(","))
        accepted = ws.accepted()
        units = sorted(
            u["unit_id"] for u in ws.units() if u["region"] in zones and u["unit_id"] in accepted
        )
    elif scope != "none":
        return {"ok": False, "problem": f"unknown scope {scope}"}
    rows.append({"proc_rev": rev, "scope": scope, "reason": reason, "units": len(units)})
    write_atomic(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    if units:
        open_plan(ws, f"procedure:{rev}", units, reason)
    return {"ok": True, "proc_rev": rev, "scope": scope, "units": len(units)}


def code_prompt_units(ws: Workspace, rows: list[dict]) -> dict:
    """A prompt in a code block stays as it is; its translation is a unit placed right under it."""
    units = read_jsonl(ws.ir / "units.jsonl")
    by_id = {u["unit_id"]: u for u in units}
    path = ws.bible / "code_prompts.jsonl"
    current = {r["after"]: r for r in read_jsonl(path)}
    problems = []
    for row in rows:
        code = by_id.get(row.get("after"))
        if code is None or code["region"] != "code":
            problems.append({"after": row.get("after"), "problem": "not a code block"})
            continue
        text = (row.get("src") or code["src"]["text"]).strip()
        if re.sub(r"\s+", " ", text) not in re.sub(r"\s+", " ", code["src"]["text"]):
            problems.append(
                {"after": row["after"], "problem": "src is not part of this code block"}
            )
            continue
        current[row["after"]] = {
            "unit_id": f"u-prompt-{code['unit_id'][2:]}",
            "chapter": code["chapter"],
            "page": code["page"],
            "region": "prompt_translation",
            "class": "translate",
            "origin": "analyze",
            "after": code["unit_id"],
            "src": {"text": text, "hash": digest(text)},
            "spans": {},
            # A block can hold a model answer: the agent names it, or it reads as a prompt.
            **({"label": row["label"]} if row.get("label") else {}),
        }
    if problems:
        return {"ok": False, "problems": problems}
    write_atomic(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in current.values()))
    return {"ok": True, "prompts": len(current)}
