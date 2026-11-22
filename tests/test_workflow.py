import json
import os
import subprocess
import sys
from pathlib import Path

from bt import cli, store


def invoke(capsys, work, *args):
    code = cli.main([*args, "--work", str(work)])
    return code, json.loads(capsys.readouterr().out)


def test_managed_workspace_refuses_out_of_order_commands(tmp_path, capsys):
    source = tmp_path / "input.pdf"
    source.write_bytes(b"test source")
    work = tmp_path / "work"
    code, _ = invoke(capsys, work, "workflow-start", str(source))
    assert code == 0
    for args in [
        ("ingest", str(source)),
        ("next", "--chapter", "1"),
        ("build", str(source)),
        ("workflow-advance", "analyze"),
    ]:
        code, reply = invoke(capsys, work, *args)
        assert code == 2
        assert reply["expected_step"] == "brief"
    assert not (work / "results").exists()


def test_legacy_registry_keeps_status_command(tmp_path, capsys):
    work = tmp_path / "legacy"
    store.write_jsonl(work / "ir" / "units.jsonl", [])
    code, reply = invoke(capsys, work, "status")
    assert code == 0
    assert reply["selected"] == 0


def test_workspace_lock_is_released_after_an_interrupted_writer(tmp_path):
    modules = Path(store.__file__).resolve().parents[1]
    script = (
        "import os, sys; from pathlib import Path; from bt.store import Workspace\n"
        "with Workspace(Path(sys.argv[1])).lock('.workflow.lock'):\n"
        "    os._exit(19)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env={**os.environ, "PYTHONPATH": str(modules)},
        check=False,
    )
    assert result.returncode == 19
    with store.Workspace(tmp_path).lock(".workflow.lock"):
        assert (tmp_path / ".workflow.lock").is_file()


def test_workflow_requires_current_evidence_from_intake_to_final(tmp_path, capsys, monkeypatch):
    source = tmp_path / "input.pdf"
    source.write_bytes(b"test source")
    work = tmp_path / "work"
    ws = store.Workspace(work)

    def ingest(_self):
        store.write_jsonl(
            ws.ir / "units.jsonl",
            [
                {
                    "unit_id": uid,
                    "chapter": chapter,
                    "page": chapter,
                    "class": "translate",
                    "region": "body",
                    "src": {"text": text, "hash": store.digest(text)},
                    "spans": {},
                }
                for uid, chapter, text in [("u-1", 1, "She ran."), ("u-2", 2, "He hid.")]
            ],
        )
        report = {"units": 2, "unmapped_glyphs": 0, "pdfium": {"unexplained_pages": 0}}
        store.write_atomic(ws.reports / "ingest.json", json.dumps(report))
        return report

    class Setter:
        def __init__(self, *args, **kwargs):
            self.objects = []

        def generate(self, *args, **kwargs):
            return "typeset source"

    def compile_book(root, _text, name):
        path = root / "build" / f"{name}.pdf"
        store.write_atomic(path, f"built {name}")
        store.write_atomic(path.with_suffix(".positions.json"), "[]")
        return {"pdf": str(path)}

    class Artifact:
        def __init__(self, *args):
            pass

        def run(self, *args, **kwargs):
            return {"findings": {"presence": []}, "counts": {"presence": 0}}

    monkeypatch.setattr(cli.ingest.BookIngest, "run", ingest)
    monkeypatch.setattr(cli.typeset, "Typesetter", Setter)
    monkeypatch.setattr(cli.typeset, "compile_book", compile_book)
    monkeypatch.setattr(cli.typeset, "extract_figures", lambda *_: None)
    monkeypatch.setattr(cli.workflow.artifact, "Artifact", Artifact)

    def run(*args, code=0):
        status, reply = invoke(capsys, work, *args)
        assert status == code, reply
        return reply

    def write(name, data):
        path = tmp_path / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def advance(step, data=None, *, chapter=None, code=0):
        args = ["workflow-advance", step]
        if data is not None:
            args.extend(["--file", write("checkpoint.json", data)])
        if chapter is not None:
            args.extend(["--chapter", str(chapter)])
        return run(*args, code=code)

    def reading(*units):
        return [{"units": list(units), "observation": "Действие и точка зрения записаны."}]

    run("workflow-start", str(source))
    brief = {"audience": "Взрослые", "owner_decisions": "Нет", "provisional": "Название"}
    advance("brief", brief, code=2)
    run("passport-set", "--file", write("passport.yaml", {"passport": {"function": "literary"}}))
    advance("brief", brief)
    run("ingest", str(source))
    run("build", str(source), "--lang", "en", "--draft", "--name", "selftest")
    ingest_review = {"coverage": "Реестр полный", "source_anomalies": "Нет", "visual": "Стр. 1"}
    advance("ingest", ingest_review, code=2)
    ingest_review["artifact_sha256"] = run("artifact-check")["sha256"]
    registry = ws.ir / "units.jsonl"
    original_registry = registry.read_bytes()
    registry.write_bytes(original_registry + b"\n")
    advance("ingest", ingest_review, code=2)
    registry.write_bytes(original_registry)
    advance("ingest", ingest_review)
    run("voice-set", "--file", write("voice.yaml", {"voice_features": []}))
    run("term-candidates")
    analysis = {"map": "Действия", "voice": "Нейтральный", "notes": "Нет", "figures": "Нет"}
    advance("analyze", {**analysis, "windows": reading("u-1", "u-1")}, code=2)
    analysis["windows"] = reading("u-1", "u-2")
    assert "addressed book risks" in advance("analyze", analysis, code=2)["error"]
    risk = {
        "id": "motion",
        "units": ["u-1"],
        "question": "Какое действие совершено?",
        "context": "Она бежит, он прячется.",
        "decision": "Действия различены.",
        "sources": [],
    }
    analysis["risks"] = [risk]
    assert "needs sources or a reason" in advance("analyze", analysis, code=2)["error"]
    risk["context_only_reason"] = "Оба глагола однозначны в сцене."
    advance("analyze", analysis)
    advance("bible", {"terms": "Нет", "forms": "Нет", "rules": "Нет"})
    assert run("status")["workflow"]["expected_step"] == "plan"
    run("next", "--chapter", "1", code=2)
    advance("goal", {}, code=2)
    criterion = {
        "requirement": "Различить бег и укрытие.",
        "evidence": "Сопоставить оба действия с оригиналом и финальным файлом.",
        "units": ["u-1"],
        "risks": ["motion"],
    }
    criteria = {key: dict(criterion) for key in cli.workflow.preparation.GENERAL_CRITERIA}
    plan = {
        "title": "Два действия",
        "deliverable": "Русский PDF",
        "scope_description": "Оба раздела исходника.",
        "criteria": criteria,
    }
    assert "exactly these criteria" in advance("plan", {**plan, "criteria": {}}, code=2)["error"]
    skipped = {key: {**row, "risks": []} for key, row in criteria.items()}
    assert "every analyze risk" in advance("plan", {**plan, "criteria": skipped}, code=2)["error"]
    request = advance("plan", plan)["workflow"]["goal_request"]
    assert run("status")["workflow"]["goal_request"] == request
    run("next", "--chapter", "1", code=2)
    run("cite", "--file", write("premature.json", {}), code=2)
    run("submit", "--file", write("premature.json", {}), code=2)
    goal = {"threadId": "test-thread", "objective": request["objective"], "status": "active"}
    assert (
        "must be active" in advance("goal", {"goal": {**goal, "status": "paused"}}, code=2)["error"]
    )
    assert (
        "differs from the plan"
        in advance("goal", {"goal": {**goal, "objective": "Другая книга"}}, code=2)["error"]
    )
    advance("goal", {"goal": goal})
    assert run("status")["workflow"]["goal"]["plan_sha256"] == request["plan_sha256"]
    resumed = advance("goal", {"goal": {**goal, "threadId": "resumed-thread"}})["workflow"]
    assert resumed["goal"]["threadId"] == "resumed-thread"
    assert resumed["expected_step"] == "translate"
    ledger = work / "workflow.json"
    registered = ledger.read_text("utf-8")
    changed = json.loads(registered)
    changed["book"]["plan"]["deliverable"] = "Другой результат"
    ledger.write_text(json.dumps(changed), encoding="utf-8")
    assert run("status")["workflow"]["expected_step"] == "plan"
    run("next", "--chapter", "1", code=2)
    advance("goal", {"goal": goal}, code=2)
    ledger.write_text(registered, encoding="utf-8")
    run("next", "--chapter", "2", code=2)

    for chapter, uid, en, ru in [
        (1, "u-1", "She ran", "Она побежала."),
        (2, "u-2", "He hid", "Он спрятался."),
    ]:
        scope = ("--chapter", str(chapter))
        batch_id = run("next", *scope)["batch"]["batch"]
        draft = {"batch": batch_id, "units": [{"id": uid, "src_echo": en, "tgt": ru}]}
        draft_path = write("draft.json", draft)
        run("submit", "--file", draft_path, code=2)
        run("cite", "--file", draft_path)
        cited = Path(draft_path).read_text("utf-8")
        Path(draft_path).write_text(cited + " ", encoding="utf-8")
        run("submit", "--file", draft_path, code=2)
        run("cite", "--file", draft_path)
        run("submit", "--file", draft_path)
        if chapter == 1:
            patch = write("revision.json", [{"id": uid, "tgt": ru, "reason": "Смысл сохранён"}])
            run("edit", "--step", "check", "--file", patch, code=2)
            run(
                "passport-set",
                "--file",
                write("passport.yaml", {"passport": {"function": "literary", "address": "ты"}}),
            )
            advance("translate", chapter=chapter, code=2)
            run("edit", "--step", "check", "--file", patch)
        advance("translate", chapter=chapter)
        target = write(
            "target.json", [{"units": [uid], "observations": "Смысл и голос сохранены."}]
        )
        run("evidence", "--step", "read-target", "--file", target, code=2)
        advance("check", chapter=chapter, code=2)
        check = write("check.json", [{"unit": uid, "en_propositions": [en], "ru_coverage": ru}])
        run("evidence", "--step", "check", "--file", check)
        advance("check", chapter=chapter)
        advance(
            "edit",
            {"windows": reading(uid), "observations": "Абзац читается связно."},
            chapter=chapter,
        )
        run("evidence", "--step", "read-target", "--file", target)
        advance("read-target", chapter=chapter)
        advance("recheck", chapter=chapter)
        run("gate", *scope, code=2)
        run("qa", *scope)
        bogus = write("decisions.json", [{"key": "not-from-qa", "unit": uid}])
        run("decide", "--file", bogus, code=2)
        run("gate", *scope)
        advance("gate", chapter=chapter)

    run("build", str(source), code=2)
    advance("crossbook", {"consistency": "u-1 и u-2", "findings": "Хронология сохранена."})
    advance("index", {"bindings": "Указателя нет."})
    run("build", str(source))
    advance("build")
    audit = run("artifact-check")
    final = {
        "artifact_sha256": "wrong",
        "artifact_checks": "Текст присутствует",
        "visual": "Страницы 1 и 2",
        "coverage": "2/2",
        "unverified": "Читательская оценка",
        "plan_sha256": request["plan_sha256"],
        "plan_review": {
            key: {"verdict": "pass", "observation": "Действие сверено.", "units": row["units"]}
            for key, row in criteria.items()
        },
    }
    advance("verify", final, code=2)
    final["artifact_sha256"] = audit["sha256"]
    assert (
        "review must address every planned criterion"
        in advance("verify", {**final, "plan_review": {}}, code=2)["error"]
    )
    assert advance("verify", final)["workflow"]["verified_current"]
    source.write_bytes(b"another source")
    assert not run("status")["workflow"]["verified_current"]
    source.write_bytes(b"test source")
    assert run("status")["workflow"]["verified_current"]
    (work / "build" / "book.pdf").write_bytes(b"replaced")
    assert not run("status")["workflow"]["verified_current"]
    assert (
        run("workflow-reopen", "--chapter", "1", "--reason", "Повторная сверка")["workflow"][
            "expected_step"
        ]
        == "check"
    )
    assert run("status")["workflow"]["chapters_passed"] == []
