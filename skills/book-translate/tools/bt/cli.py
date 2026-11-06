"""`bt`: command line for the book-translate skill. Every command prints JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from . import batch, bible, indexer, ingest, qa, revision, store, typeset

SKILL_DIR = Path(__file__).resolve().parents[2]


def _emit(payload: dict) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0 if payload.get("ok", True) else 2


def _pages(value: str | None) -> set[int] | None:
    """`42-43,51` to a set of source pages; nothing means the whole book."""
    if not value:
        return None
    pages: set[int] = set()
    for part in value.split(","):
        a, _, b = part.partition("-")
        pages.update(range(int(a), int(b or a) + 1))
    return pages


def _scope(args) -> set[int] | None:
    if args.chapter is not None:
        return batch.Chapters({args.chapter})
    return _pages(args.pages)


def _ws(args) -> store.Workspace:
    return store.Workspace(Path(args.work))


def _read(path: str):
    return json.loads(Path(path).read_text("utf-8"))


def cmd_ingest(args) -> int:
    profile = Path(args.profile) if args.profile else SKILL_DIR / "profiles" / "packt-indesign.yaml"
    try:
        return _emit(
            {"ok": True, **ingest.BookIngest(Path(args.pdf), Path(args.work), profile).run()}
        )
    except ingest.IngestError as error:
        return _emit({"ok": False, "error": str(error)})


def cmd_build(args) -> int:
    ws = _ws(args)
    setter = typeset.Typesetter(ws.root, lang=args.lang)
    typeset.extract_figures(Path(args.pdf), setter.objects, ws.ir / "figures")
    pages = _pages(args.pages)
    select = (lambda u: u["page"] in pages) if pages else None
    try:
        return _emit(
            {
                "ok": True,
                **typeset.compile_book(
                    ws.root, setter.generate(select, draft=args.draft), args.name
                ),
            }
        )
    except typeset.BuildError as error:
        return _emit({"ok": False, "error": str(error)})


def cmd_bible_add(args) -> int:
    ws = _ws(args)
    try:
        with ws.lock():
            return _emit(
                {"ok": True, "changed": bible.add(ws, bible.load_many(Path(args.file)), args.actor)}
            )
    except bible.BibleError as error:
        return _emit({"ok": False, "error": str(error)})


def cmd_notes_add(args) -> int:
    ws = _ws(args)
    try:
        with ws.lock():
            units = {u["unit_id"]: u for u in ws.units()}
            return _emit(
                {"ok": True, "notes": bible.add_notes(ws, bible.load_many(Path(args.file)), units)}
            )
    except bible.BibleError as error:
        return _emit({"ok": False, "error": str(error)})


def cmd_figures_set(args) -> int:
    ws = _ws(args)
    with ws.lock():
        return _emit(ws.set_figures(bible.load_many(Path(args.file))))


def cmd_bible_change(args) -> int:
    ws = _ws(args)
    concepts = bible.load_many(Path(args.file))
    if len(concepts) != 1:
        return _emit({"ok": False, "error": f"one concept per change, got {len(concepts)}"})
    with ws.lock():
        return _emit(
            revision.change(
                ws, concepts[0], args.reason or "", dry_run=not args.apply, actor=args.actor
            )
        )


def cmd_procedure_change(args) -> int:
    ws = _ws(args)
    with ws.lock():
        return _emit(revision.procedure_change(ws, args.scope, args.reason))


def cmd_prompts_set(args) -> int:
    ws = _ws(args)
    with ws.lock():
        return _emit(revision.code_prompt_units(ws, bible.load_many(Path(args.file))))


def cmd_index_bind(args) -> int:
    ws = _ws(args)
    positions = None
    if args.positions:
        # A repeated table header reports several places; a reference goes to the first.
        positions = {}
        for place in _read(args.positions):
            positions.setdefault(place["id"], place)
    with ws.lock():
        report = indexer.bind(ws, positions)
    store.write_atomic(
        ws.reports / "index-open.json",
        json.dumps(indexer.open_report(ws), ensure_ascii=False, indent=1),
    )
    return _emit({"ok": True, **report, "open_file": str(ws.reports / "index-open.json")})


def cmd_index_decide(args) -> int:
    ws = _ws(args)
    with ws.lock():
        return _emit(indexer.decide(ws, _read(args.file)))


def cmd_index_merge(args) -> int:
    ws = _ws(args)
    with ws.lock():
        return _emit(indexer.decide_merge(ws, _read(args.file)))


def cmd_next(args) -> int:
    ws = _ws(args)
    with ws.lock():
        made = batch.next_batch(ws, _scope(args), args.max_words, args.max_units)
    return _emit({"ok": True, "batch": made} if made else {"ok": True, "batch": None, "done": True})


def cmd_submit(args) -> int:
    ws = _ws(args)
    try:
        with ws.lock():
            return _emit(batch.submit(ws, _read(args.file)))
    except batch.SubmitRejected as error:
        return _emit({"ok": False, "problems": error.problems})


def cmd_cite(args) -> int:
    ws = _ws(args)
    report = batch.cite(ws, _read(args.file))
    if report["ok"]:
        out = Path(args.out or args.file)
        store.write_atomic(out, json.dumps(report["result"], ensure_ascii=False, indent=1))
        report["written"] = str(out)
    report.pop("result")
    return _emit(report)


def cmd_term_candidates(args) -> int:
    ws = _ws(args)
    report = batch.term_candidates(ws, args.top, args.min_count)
    store.write_atomic(
        ws.reports / "term-candidates.json", json.dumps(report, ensure_ascii=False, indent=1)
    )
    return _emit(
        {
            "ok": True,
            "file": str(ws.reports / "term-candidates.json"),
            "index_terms": len(report["index_terms"]),
            "frequent": len(report["frequent"]),
            "total": report["total"],
        }
    )


def cmd_status(args) -> int:
    return _emit({"ok": True, **batch.status(_ws(args), _scope(args))})


def cmd_qa(args) -> int:
    return _emit({"ok": True, **qa.run_qa(_ws(args), _scope(args), args.name)})


def cmd_evidence(args) -> int:
    ws = _ws(args)
    with ws.lock():
        return _emit(qa.record_evidence(ws, args.step, _read(args.file)))


def cmd_edit(args) -> int:
    ws = _ws(args)
    with ws.lock():
        return _emit(qa.apply_edit(ws, args.step, _read(args.file)))


def cmd_decide(args) -> int:
    ws = _ws(args)
    with ws.lock():
        return _emit(qa.decide(ws, _read(args.file)))


def cmd_gate(args) -> int:
    result = qa.chapter_gate(_ws(args), _scope(args), args.name)
    return _emit({"ok": result["verdict"] != "empty", **result})


def _write_yaml(ws: store.Workspace, name: str, doc: dict) -> None:
    text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    with ws.lock():
        store.write_atomic(ws.bible / name, text)


def cmd_passport_set(args) -> int:
    doc = yaml.safe_load(Path(args.file).read_text("utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("passport"), dict):
        return _emit({"ok": False, "error": "the file needs a `passport` section"})
    rules = doc.get("rules", [])
    if not isinstance(rules, list):
        return _emit({"ok": False, "error": "`rules` must be a list"})
    problems = bible.rule_problems(rules)
    if problems:
        return _emit({"ok": False, "problems": problems})
    _write_yaml(_ws(args), "passport.yaml", doc)
    return _emit({"ok": True, "rules": len(rules)})


def cmd_voice_set(args) -> int:
    doc = yaml.safe_load(Path(args.file).read_text("utf-8"))
    if not isinstance(doc, dict):
        return _emit({"ok": False, "error": "voice must be a mapping"})
    _write_yaml(_ws(args), "voice.yaml", doc)
    return _emit({"ok": True})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bt")
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name, func, *arguments):
        p = sub.add_parser(name)
        p.add_argument("--work", required=True)
        for spec, kw in arguments:
            p.add_argument(spec, **kw)
        p.set_defaults(func=func)

    pages = ("--pages", {})
    chapter = ("--chapter", {"type": int})
    file = ("--file", {"required": True})
    command("ingest", cmd_ingest, ("pdf", {}), ("--profile", {}))
    command(
        "build",
        cmd_build,
        ("pdf", {}),
        ("--lang", {"default": "ru"}),
        ("--name", {"default": "book"}),
        ("--draft", {"action": "store_true"}),
        pages,
    )
    command("bible-add", cmd_bible_add, file, ("--actor", {"default": "agent"}))
    command(
        "bible-change",
        cmd_bible_change,
        file,
        ("--reason", {}),
        ("--apply", {"action": "store_true"}),
        ("--actor", {"default": "agent"}),
    )
    command(
        "procedure-change",
        cmd_procedure_change,
        ("--scope", {"required": True}),
        ("--reason", {"required": True}),
    )
    command("prompts-set", cmd_prompts_set, file)
    command("index-bind", cmd_index_bind, ("--positions", {}))
    command("index-decide", cmd_index_decide, file)
    command("index-merge", cmd_index_merge, file)
    command("notes-add", cmd_notes_add, file)
    command("figures-set", cmd_figures_set, file)
    command(
        "next",
        cmd_next,
        pages,
        chapter,
        ("--max-words", {"type": int, "default": 1500}),
        ("--max-units", {"type": int, "default": 30}),
    )
    command("cite", cmd_cite, file, ("--out", {}))
    command(
        "term-candidates",
        cmd_term_candidates,
        ("--top", {"type": int, "default": 300}),
        ("--min-count", {"type": int, "default": 3}),
    )
    command("submit", cmd_submit, file)
    command("status", cmd_status, pages, chapter)
    command("qa", cmd_qa, pages, chapter, ("--name", {"default": "book"}))
    command("evidence", cmd_evidence, ("--step", {"required": True}), file)
    command("edit", cmd_edit, ("--step", {"required": True}), file)
    command("decide", cmd_decide, file)
    command("gate", cmd_gate, pages, chapter, ("--name", {"default": "book"}))
    command("passport-set", cmd_passport_set, file)
    command("voice-set", cmd_voice_set, file)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, yaml.YAMLError, bible.BibleError) as error:
        return _emit({"ok": False, "error": str(error)})


if __name__ == "__main__":
    raise SystemExit(main())
