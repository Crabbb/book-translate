"""The subject index is rebuilt, not translated page by page: the pages it points to are new.

Each source reference points only at a page, so it is bound to a place on that page. Only an
unambiguous place is bound automatically. Everything else waits for a decision, and the final build
refuses to run without it: a wrong anchor still gives a real page number, just the wrong one.
"""

from __future__ import annotations

import json
import re

from .batch import plain
from .store import Workspace, read_jsonl, write_jsonl

NON_TEXT = {"toc", "index", "alt"}


def search_key(term: str) -> str:
    """What to look for on the page: no parenthetical, no qualifier after a comma."""
    key = re.sub(r"\([^)]*\)", " ", term)
    key = key.split(",")[0]
    return re.sub(r"\s+", " ", key).strip().lower()


def refs_of(entry: dict) -> list[list[int]]:
    """Target positions grouped by printed reference: a range `52-54` holds two targets."""
    groups: list[list[int]] = []
    at = 0
    for printed in entry["printed"]:
        size = 2 if "-" in printed else 1
        group = list(range(at, min(at + size, len(entry["targets"]))))
        if group:
            groups.append(group)
        at += size
    return groups


def _candidates(page: int, key: str, by_page: dict[int, list[dict]]) -> list[str]:
    if not key:
        return []
    return [
        u["unit_id"]
        for u in by_page.get(page, [])
        if re.search(rf"(?<!\w){re.escape(key)}(?!\w)", plain(u["src"]["text"]).lower())
    ]


def bind(ws: Workspace, positions: dict[str, dict] | None = None) -> dict:
    """Bind every reference. Decisions already made stay; positions settle same-page ambiguity."""
    units = ws.units()
    entries = read_jsonl(ws.ir / "index.jsonl")
    by_page: dict[int, list[dict]] = {}
    for u in units:
        if u["region"] not in NON_TEXT:
            by_page.setdefault(u["page"], []).append(u)
    path = ws.bible / "index_bindings.jsonl"
    decided = {(r["unit"], r["target"]): r for r in read_jsonl(path) if r["status"] == "decided"}
    rows: list[dict] = []
    for entry in entries:
        key = search_key(entry["term"])
        for i, target in enumerate(entry["targets"]):
            page = target.get("page")
            address = (entry["unit_id"], i)
            if address in decided:
                rows.append(decided[address])
                continue
            found = _candidates(page, key, by_page) if page else []
            row = {"unit": entry["unit_id"], "target": i, "page": page, "term": entry["term"]}
            pages = {positions[c]["page"] for c in found if c in positions} if positions else set()
            if len(found) == 1:
                row.update(status="auto", bind=found[0], kind="unique")
            elif found and positions and len(pages) == 1 and all(c in positions for c in found):
                # Several places, one new page: the printed number is the same for any of them.
                row.update(status="auto", bind=found[0], kind="same_page")
            else:
                row.update(status="open", candidates=found, kind="many" if found else "not_found")
            rows.append(row)
    write_jsonl(path, rows)
    return _summary(rows, ws)


def _summary(rows: list[dict], ws: Workspace) -> dict:
    counts: dict[str, int] = {}
    for r in rows:
        counts[f"{r['status']}:{r.get('kind', '')}"] = (
            counts.get(f"{r['status']}:{r.get('kind', '')}", 0) + 1
        )
    same = same_russian(ws)
    return {
        "references": len(rows),
        "counts": counts,
        "open": sum(r["status"] == "open" for r in rows),
        "same_russian_open": len(same),
    }


def open_report(ws: Workspace) -> dict:
    """What a decision needs: the term, its place in the source, and the units to choose from."""
    units = {u["unit_id"]: u for u in ws.units()}
    by_page: dict[int, list[str]] = {}
    for u in ws.units():
        if u["region"] not in NON_TEXT:
            by_page.setdefault(u["page"], []).append(u["unit_id"])
    out = []
    for r in read_jsonl(ws.bible / "index_bindings.jsonl"):
        if r["status"] != "open":
            continue
        ids = r.get("candidates") or by_page.get(r["page"], [])
        out.append(
            {
                "unit": r["unit"],
                "target": r["target"],
                "term": r["term"],
                "kind": r["kind"],
                "choose_from": [
                    {"id": i, "text": plain(units[i]["src"]["text"])[:160]} for i in ids
                ],
            }
        )
    return {"open": out, "same_russian": same_russian(ws)}


def decide(ws: Workspace, decisions: list[dict]) -> dict:
    """A binding must point at a unit of its own source page, with a reason."""
    path = ws.bible / "index_bindings.jsonl"
    rows = {(r["unit"], r["target"]): r for r in read_jsonl(path)}
    pages = {u["unit_id"]: u["page"] for u in ws.units() if u["region"] not in NON_TEXT}
    bad = []
    for d in decisions:
        row = rows.get((d.get("unit"), d.get("target")))
        if row is None:
            bad.append({"unit": d.get("unit"), "problem": "no such reference"})
        elif not (d.get("basis") or "").strip():
            bad.append({"unit": d["unit"], "problem": "a basis is required"})
        elif pages.get(d.get("bind")) != row["page"]:
            bad.append({"unit": d["unit"], "problem": "the bound unit is not on the target page"})
        else:
            row.update(status="decided", bind=d["bind"], basis=d["basis"])
    if bad:
        return {"ok": False, "problems": bad}
    write_jsonl(path, list(rows.values()))
    return {"ok": True, "decided": len(decisions)}


def _russian(ws: Workspace, unit: dict) -> str:
    result = ws.accepted().get(unit["unit_id"])
    return plain(result["tgt"]) if result else plain(unit["src"]["text"])


def same_russian(ws: Workspace) -> list[dict]:
    """Main entries that became one Russian term and have no decision yet."""
    return [g for g in _groups(ws) if not g["decided"]]


def _groups(ws: Workspace) -> list[dict]:
    entries = {e["unit_id"]: e for e in read_jsonl(ws.ir / "index.jsonl")}
    accepted = ws.accepted()
    seen: dict[str, list[str]] = {}
    for u in ws.units():
        e = entries.get(u["unit_id"])
        if e and e["main"] and u["unit_id"] in accepted:
            seen.setdefault(plain(accepted[u["unit_id"]]["tgt"]).strip().lower(), []).append(
                u["unit_id"]
            )
    merges = {tuple(r["units"]): r for r in read_jsonl(ws.bible / "index_merges.jsonl")}
    return [
        {"term": term, "units": ids, "decided": tuple(ids) in merges}
        for term, ids in seen.items()
        if len(ids) > 1
    ]


def decide_merge(ws: Workspace, decisions: list[dict]) -> dict:
    """`merge` joins entries; otherwise they stay apart, with a reason, as two senses."""
    path = ws.bible / "index_merges.jsonl"
    rows = {tuple(r["units"]): r for r in read_jsonl(path)}
    for d in decisions:
        if not (d.get("basis") or "").strip():
            return {"ok": False, "problem": "a basis is required"}
        rows[tuple(d["units"])] = {
            "units": d["units"],
            "merge": bool(d["merge"]),
            "basis": d["basis"],
        }
    write_jsonl(path, list(rows.values()))
    return {"ok": True}


def sort_key(term: str) -> tuple:
    """Russian alphabet first, then Latin, then the rest."""
    low = term.strip().lower().replace("ё", "е")
    first = low[:1]
    if "а" <= first <= "я":
        group = 0
    elif "a" <= first <= "z":
        group = 1
    else:
        group = 2
    return (group, low)


def letter_of(term: str) -> str:
    first = term.strip()[:1].upper()
    return "Е" if first == "Ё" else first


def layout(ws: Workspace, select=None) -> dict:
    """The final shape of the index: title, sorted main entries with their sub-entries."""
    entries = {e["unit_id"]: e for e in read_jsonl(ws.ir / "index.jsonl")}
    all_units = [u for u in ws.units() if u["region"] == "index"]
    units = [u for u in all_units if select is None or select(u)]
    bindings = {(r["unit"], r["target"]): r for r in read_jsonl(ws.bible / "index_bindings.jsonl")}
    merges = {
        u: r["units"][0]
        for r in read_jsonl(ws.bible / "index_merges.jsonl")
        if r["merge"]
        for u in r["units"]
    }
    title = None
    mains: dict[str, dict] = {}
    current = None
    for unit in units:
        uid = unit["unit_id"]
        e = entries[uid]
        if uid == all_units[0]["unit_id"]:
            title = unit
            continue
        if unit["class"] == "service":
            continue
        refs = _refs(e, uid, bindings)
        if e["main"] or current is None:
            host = merges.get(uid, uid)
            if host not in mains:
                mains[host] = {"unit": unit, "refs": [], "subs": []}
            mains[host]["refs"] += refs
            current = mains[host]
        else:
            current["subs"].append({"unit": unit, "refs": refs})
    ordered = sorted(mains.values(), key=lambda m: sort_key(_russian(ws, m["unit"])))
    for main in ordered:
        main["subs"].sort(key=lambda s: sort_key(_russian(ws, s["unit"])))
    return {"title": title, "mains": ordered}


def _refs(entry: dict, uid: str, bindings) -> list[dict]:
    out = []
    for group in refs_of(entry):
        points = []
        for i in group:
            row = bindings.get((uid, i))
            points.append(
                {
                    "bind": row.get("bind") if row and row["status"] != "open" else None,
                    "page": entry["targets"][i].get("page"),
                }
            )
        out.append({"points": points})
    return out


def blockers(ws: Workspace) -> dict:
    rows = read_jsonl(ws.bible / "index_bindings.jsonl")
    return {
        "bound": bool(rows),
        "open": sum(r["status"] == "open" for r in rows),
        "same_russian": len(same_russian(ws)),
    }


def dumps(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)
