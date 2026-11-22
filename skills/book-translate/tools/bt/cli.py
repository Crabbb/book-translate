"""`bt`: command line for the book-translate skill. Every command prints JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from zipfile import BadZipFile

import yaml

from . import batch, bible, epub, indexer, ingest, qa, revision, store, typeset, workflow

SKILL_DIR = Path(__file__).resolve().parents[2]


def _emit(payload: dict) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0 if payload.get("ok", True) else 2


def _emit_observed(args, payload: dict) -> int:
    route = getattr(args, "route", None)
    if route is not None and payload.get("ok", True):
        route.observe(args)
    return _emit(payload)


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
    route = getattr(args, "route", None)
    if route is not None and route.pilot and not args.pages:
        return route.scope(args.chapter)
    if args.chapter is not None:
        return batch.Chapters({args.chapter})
    return _pages(args.pages)


def _ws(args) -> store.Workspace:
    return store.Workspace(Path(args.work))


def _read(path: str):
    return json.loads(Path(path).read_text("utf-8"))


def cmd_ingest(args) -> int:
    if Path(args.pdf).suffix.lower() == ".epub":
        if args.profile:
            return _emit({"ok": False, "error": "EPUB does not use a PDF layout profile"})
        return _emit_observed(args, {"ok": True, **epub.ingest(Path(args.pdf), _ws(args))})
    profile = Path(args.profile) if args.profile else SKILL_DIR / "profiles" / "packt-indesign.yaml"
    try:
        return _emit_observed(
            args, {"ok": True, **ingest.BookIngest(Path(args.pdf), Path(args.work), profile).run()}
        )
    except ingest.IngestError as error:
        return _emit({"ok": False, "error": str(error)})


def cmd_build(args) -> int:
    if Path(args.pdf).suffix.lower() == ".epub":
        if args.pages:
            return _emit({"ok": False, "error": "EPUB output preserves the full container"})
        scope = args.route.scope() if getattr(args, "route", None) and args.route.pilot else None
        result = epub.build(
            Path(args.pdf), _ws(args), args.lang, args.draft, args.name, scope=scope
        )
        return _emit_observed(args, {"ok": True, **result})
    ws = _ws(args)
    setter = typeset.Typesetter(ws.root, lang=args.lang)
    typeset.extract_figures(Path(args.pdf), setter.objects, ws.ir / "figures")
    pages = _pages(args.pages)
    select = (lambda u: u["page"] in pages) if pages else None
    try:
        return _emit_observed(
            args,
            {
                "ok": True,
                **typeset.compile_book(
                    ws.root, setter.generate(select, draft=args.draft), args.name
                ),
            },
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
    return _emit_observed(args, report)


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
    ws = _ws(args)
    if workflow.managed(ws):
        args.route = workflow.Route(ws)
        route = {"workflow": args.route.progress()}
    else:
        route = {}
    return _emit({"ok": True, **batch.status(ws, _scope(args)), **route})


def cmd_qa(args) -> int:
    return _emit_observed(args, {"ok": True, **qa.run_qa(_ws(args), _scope(args), args.name)})


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
    ws = _ws(args)
    result = qa.chapter_gate(ws, _scope(args), args.name)
    if workflow.managed(ws):
        return _emit_observed(args, {"ok": result["verdict"] == "pass", **result})
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
    if getattr(args, "route", None):
        args.route.check_passport(doc["passport"])
    _write_yaml(_ws(args), "passport.yaml", doc)
    return _emit({"ok": True, "rules": len(rules)})


def cmd_voice_set(args) -> int:
    doc = yaml.safe_load(Path(args.file).read_text("utf-8"))
    if not isinstance(doc, dict):
        return _emit({"ok": False, "error": "voice must be a mapping"})
    _write_yaml(_ws(args), "voice.yaml", doc)
    return _emit({"ok": True})


def cmd_workflow_start(args) -> int:
    ws = _ws(args)
    route = workflow.Route.start(ws, Path(args.source), args.pilot_chapter, args.pilot_scene)
    return _emit({"ok": True, "workflow": route.progress()})


def cmd_workflow_advance(args) -> int:
    report = _read(args.file) if args.file else {}
    progress = workflow.Route(_ws(args)).advance(args.step, args.chapter, report)
    return _emit({"ok": True, "workflow": progress})


def cmd_workflow_reopen(args) -> int:
    progress = workflow.Route(_ws(args)).reopen(args.chapter, args.reason)
    return _emit({"ok": True, "workflow": progress})


def cmd_artifact_check(args) -> int:
    return _emit({"ok": True, **workflow.Route(_ws(args)).audit_artifact(selftest=args.selftest)})


def _run_command(args) -> int:
    ws = _ws(args)
    if args.command == "workflow-start":
        ws.root.mkdir(parents=True, exist_ok=True)
    if workflow.managed(ws) or args.command.startswith("workflow-"):
        with ws.lock(".workflow.lock"):
            if args.command.startswith("workflow-") or args.command == "status":
                return args.func(args)
            args.route = workflow.Route(ws)
            args.route.guard(args)
            return args.func(args)
    if args.command != "status" and not (ws.ir / "units.jsonl").is_file():
        raise workflow.Refusal("initialize a new book with workflow-start", "workflow-start")
    return args.func(args)


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
    command(
        "workflow-start",
        cmd_workflow_start,
        ("source", {}),
        ("--pilot-chapter", {"type": int}),
        ("--pilot-scene", {"type": int}),
    )
    command("workflow-advance", cmd_workflow_advance, ("step", {}), chapter, ("--file", {}))
    command("artifact-check", cmd_artifact_check, ("--selftest", {"action": "store_true"}))
    command(
        "workflow-reopen",
        cmd_workflow_reopen,
        ("--chapter", {"type": int, "required": True}),
        ("--reason", {"required": True}),
    )
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
        return _run_command(args)
    except workflow.Refusal as error:
        return _emit(error.payload)
    except (
        OSError,
        yaml.YAMLError,
        bible.BibleError,
        store.WorkspaceLocked,
        json.JSONDecodeError,
        epub.EpubError,
        BadZipFile,
    ) as error:
        return _emit({"ok": False, "error": str(error)})


if __name__ == "__main__":
    raise SystemExit(main())
