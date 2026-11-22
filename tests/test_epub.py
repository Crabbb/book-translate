import json
from io import BytesIO
from pathlib import Path
from subprocess import CompletedProcess
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from bt import acceptance, batch, cli, epub, store
from lxml import etree
from PIL import Image

XHTML = "http://www.w3.org/1999/xhtml"
EPUB = "http://www.idpf.org/2007/ops"


def test_acceptance_reference_and_gate_use_the_same_criteria():
    text = acceptance.CONTRACT.read_text(encoding="utf-8-sig")
    for section in text.split("## Как закрыть маршрут в bt"):
        rows = [
            row.split("|")[1].strip() for row in section.splitlines() if row.startswith("| AC-")
        ]
        assert rows == list(acceptance.CRITERIA)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.epub"
    with ZipFile(path, "w") as archive:
        archive.writestr("mimetype", b"application/epub+zip")
        entries = {
            "META-INF/container.xml": """
                <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
                  <rootfiles><rootfile full-path="OPS/book.opf"
                    media-type="application/oebps-package+xml"/></rootfiles>
                </container>""",
            "OPS/book.opf": """
                <package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
                  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
                    <dc:identifier id="id">urn:test:book</dc:identifier>
                    <dc:language>en</dc:language><dc:title>Night Train</dc:title>
                    <meta property="dcterms:modified">2026-11-20T00:00:00Z</meta>
                  </metadata>
                  <manifest>
                    <item id="c" href="ch.xhtml" media-type="application/xhtml+xml"/>
                    <item id="n" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
                    <item id="i" href="scene.css" media-type="text/css"/>
                  </manifest>
                  <spine><itemref idref="c"/><itemref idref="n" linear="no"/></spine>
                </package>""",
            "OPS/ch.xhtml": f"""
                <html xmlns="{XHTML}" xmlns:epub="{EPUB}" xml:lang="en" lang="en">
                  <head><title>First Chapter</title><link rel="stylesheet" href="scene.css"/></head>
                  <body><section epub:type="chapter" id="ch"><h1>First Chapter</h1>
                    <p id="sentence">It was <em><a href="#ch">very</a> dark</em><span
                      epub:type="pagebreak" id="page-5" aria-label="5"/>.</p>
                    <hr class="scene"/><p>Then the train came.</p>
                  </section></body>
                </html>""",
            "OPS/nav.xhtml": f"""
                <html xmlns="{XHTML}" xmlns:epub="{EPUB}" xml:lang="en" lang="en">
                  <head><title>Contents</title></head>
                  <body><nav epub:type="toc" id="contents"><h1>Contents</h1>
                    <ol><li><a href="ch.xhtml#ch">First Chapter</a></li></ol>
                  </nav><nav epub:type="page-list"><ol>
                    <li><a href="ch.xhtml#page-5">5</a></li></ol>
                  </nav></body>
                </html>""",
            "OPS/scene.css": ".scene { margin: 1em; }",
        }
        for name, data in entries.items():
            archive.writestr(name, data.strip(), compress_type=ZIP_DEFLATED)
    return path


def test_managed_epub_ingest_and_english_selftest(source, tmp_path, capsys):
    work = tmp_path / "work"

    def run(*args):
        code = cli.main([*args, "--work", str(work)])
        response = json.loads(capsys.readouterr().out)
        assert code == 0, response
        return response

    passport = tmp_path / "passport.json"
    passport.write_text('{"passport":{"source":{"genre":"nonfiction"}}}', encoding="utf-8")
    brief = tmp_path / "brief.json"
    brief.write_text(
        '{"audience":"adults","owner_decisions":"none","provisional":"title"}', encoding="utf-8"
    )
    run("workflow-start", str(source))
    run("passport-set", "--file", str(passport))
    run("workflow-advance", "brief", "--file", str(brief))
    report = run("ingest", str(source))
    assert report["format"] == "epub"
    units = store.Workspace(work).units()
    scene = [unit for unit in units if unit["chapter"] == 1 and unit["region"] == "body"]
    assert len(scene) == 2
    assert scene[0]["scene"] == 1
    assert scene[1]["scene"] == 2
    assert scene[0]["src"]["text"] == "It was ⟦1⟧⟦2⟧very⟦/2⟧ dark⟦/1⟧⟦a3⟧."
    assert scene[0]["spans"]["2"]["data"]["link"]["dest"] == "#ch"
    assert not any(u["src"]["text"] == "5" for u in units)
    run("build", str(source), "--lang", "en", "--draft", "--name", "selftest")
    with ZipFile(source) as original, ZipFile(work / "build" / "selftest.epub") as rebuilt:
        assert original.namelist() == rebuilt.namelist()
        for name in original.namelist():
            assert original.read(name) == rebuilt.read(name), name


def test_pilot_scope_is_persisted_on_workspace_start(source, tmp_path, capsys):
    work = tmp_path / "pilot"
    arguments = [
        "workflow-start",
        str(source),
        "--work",
        str(work),
        "--pilot-chapter",
        "1",
        "--pilot-scene",
        "1",
    ]
    assert cli.main(arguments) == 0
    reply = json.loads(capsys.readouterr().out)
    assert reply["workflow"]["pilot"] == {"chapter": 1, "scene": 1}
    assert cli.main([*arguments[:-1], "2"]) == 2
    assert cli.main(["status", "--work", str(work)]) == 0
    outputs = capsys.readouterr().out
    assert "workspace already belongs to another scope" in outputs
    assert '"scene": 1' in outputs


@pytest.mark.parametrize("pilot", [False, True])
def test_fiction_epub_requires_acceptance_on_the_current_artifact(
    source, tmp_path, capsys, monkeypatch, pilot
):
    work = tmp_path / "work"
    ws = store.Workspace(work)
    monkeypatch.setenv("BT_EPUBCHECK", "checker")
    monkeypatch.setattr(
        epub.subprocess, "run", lambda *args, **kwargs: CompletedProcess(args, 0, "ok", "")
    )

    def run(*args, code=0):
        result = cli.main([*args, "--work", str(work)])
        reply = json.loads(capsys.readouterr().out)
        assert result == code, reply
        return reply

    def payload(data):
        path = tmp_path / "evidence.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def advance(step, data=None, chapter=None, *, code=0):
        args = ["workflow-advance", step]
        if data is not None:
            args.extend(["--file", payload(data)])
        if chapter is not None:
            args.extend(["--chapter", str(chapter)])
        reply = run(*args, code=code)
        return reply["workflow"] if code == 0 else reply

    def window(units):
        return [{"units": units, "observation": "Ритм коротких фраз сохранён."}]

    def review(audit):
        return {"artifact_sha256": audit["sha256"], "coverage": "Все единицы", "visual": "Титул"}

    scope = ["--pilot-chapter", "1", "--pilot-scene", "1"] if pilot else []
    run("workflow-start", str(source), *scope)
    brief = {"audience": "Взрослые", "owner_decisions": "Нет", "provisional": "Нет"}
    run("passport-set", "--file", payload({"passport": {"function": "literary"}}))
    advance("brief", brief, code=2)
    passport = {
        "source": {"genre": "fiction"},
        "fiction": dict.fromkeys(
            ("dialogue", "inner_speech", "written_messages", "embedded_texts"), "Книжная норма"
        ),
    }
    run("passport-set", "--file", payload({"passport": passport}))
    advance("brief", brief)
    run("ingest", str(source))
    run("build", str(source), "--lang", "en", "--draft", "--name", "selftest")
    intake = run("artifact-check")
    advance("ingest", {**review(intake), "source_anomalies": "Нет"})
    run("voice-set", "--file", payload({"voice_features": []}))
    run("term-candidates")
    route = cli.workflow.Route(ws)
    selected = batch._select(ws.units(), route.scope())
    ids = [u["unit_id"] for u in selected]
    risk = {
        "id": "arrival",
        "units": [ids[-1]],
        "question": "Когда появляется поезд?",
        "context": "Он появляется после описания темноты.",
        "decision": "Сохранить последовательность.",
        "sources": [],
        "context_only_reason": "Порядок действий указан в самой сцене.",
    }
    advance(
        "analyze",
        {
            **dict.fromkeys(("map", "voice", "notes", "figures"), "Чтение"),
            "windows": window(ids),
            "risks": [risk],
        },
    )
    run(
        "passport-set", "--file", payload({"passport": {"source": {"genre": "nonfiction"}}}), code=2
    )
    advance("bible", dict.fromkeys(("terms", "forms", "rules"), "Проверено"))
    assert run("status")["workflow"]["expected_step"] == "plan"
    criterion = {
        "requirement": "Сохранить появление поезда после описания темноты.",
        "evidence": "Сверить обе фразы в архиве на русском и в исходнике.",
        "units": ids[-1:],
        "risks": ["arrival"],
    }
    criteria = dict.fromkeys(acceptance.CRITERIA, criterion)
    plan = {
        "title": "Ночной поезд",
        "deliverable": "Русский EPUB",
        "scope_description": "Одна сцена." if pilot else "Весь EPUB.",
        "criteria": criteria,
    }
    incomplete = {key: row for key, row in criteria.items() if key != "AC-09"}
    advance("plan", {**plan, "criteria": incomplete}, code=2)
    invalid_units = {**criteria, "AC-01": {**criterion, "units": ["outside-registry"]}}
    advance("plan", {**plan, "criteria": invalid_units}, code=2)
    request = advance("plan", plan)["goal_request"]
    assert "→ acceptance" in request["objective"]
    assert ('"scene": 1' if pilot else '"full"') in request["objective"]
    advance(
        "goal",
        {"goal": {"threadId": "epub-test", "status": "active", "objective": request["objective"]}},
    )

    for chapter in route.chapter_numbers():
        units = [u for u in selected if u["chapter"] == chapter]
        chapter_ids = [u["unit_id"] for u in units]
        arg = ["--chapter", str(chapter)]
        checks = []
        while len(checks) < len(units):
            package = run("next", *arg)["batch"]
            packaged = json.loads((ws.batches / f"{package['batch']}.json").read_text("utf-8"))
            available = set(packaged["manifest"]["unit_ids"])
            entries = []
            for unit in units:
                if unit["unit_id"] not in available:
                    continue
                en = unit["src"]["text"]
                ru = en
                for left, right in (
                    ("Night Train", "Ночной поезд"),
                    ("First Chapter", "Первая глава"),
                    ("Contents", "Оглавление"),
                    ("It was", "Было"),
                    ("very", "очень"),
                    ("dark", "темно"),
                    ("Then the train came.", "Затем пришёл поезд."),
                ):
                    ru = ru.replace(left, right)
                entries.append({"id": unit["unit_id"], "tgt": ru})
                checks.append({"unit": unit["unit_id"], "en_propositions": [en], "ru_coverage": ru})
            draft = payload({"batch": package["batch"], "units": entries})
            run("cite", "--file", draft)
            run("submit", "--file", draft)
        advance("translate", chapter=chapter)
        run("evidence", "--step", "check", "--file", payload(checks))
        advance("check", chapter=chapter)
        advance("edit", {"windows": window(chapter_ids), "observations": "Ритм"}, chapter)
        run(
            "evidence",
            "--step",
            "read-target",
            "--file",
            payload([{"units": chapter_ids, "observations": "Ритм сохранён."}]),
        )
        advance("read-target", chapter=chapter)
        advance("recheck", chapter=chapter)
        run("qa", *arg)
        run("gate", *arg)
        advance("gate", chapter=chapter)

    advance("crossbook", {"consistency": "Поезд и темнота", "findings": "Сохранены"})
    advance("index", {"bindings": "Указателя нет"})
    run("build", str(source), *(["--draft"] if pilot else []))
    advance("build")
    advance("acceptance", {}, code=2)
    audited = run("artifact-check")
    progress = advance(
        "verify", {**review(audited), "artifact_checks": "Архив прочитан", "unverified": "Нет"}
    )
    assert progress["expected_step"] == "acceptance"
    assert not progress["verified_current"]

    addressed = {"units": ids, "observation": "Действие сверено с оригиналом"}
    delivery_scope = scope and {"chapter": 1, "scene": 1} or "full"
    receipt = {
        "source_sha256": epub.sha(source),
        "artifact_sha256": audited["sha256"],
        "scope": delivery_scope,
        "plan_sha256": request["plan_sha256"],
        "plan_review": {
            key: {"verdict": "pass", "observation": "Сверено с оригиналом.", "units": row["units"]}
            for key, row in criteria.items()
        },
        "criteria": {
            "AC-01": {"exceptions": []},
            "AC-02": {
                "scene_checks": [
                    {
                        **addressed,
                        "actor": "Рассказчик",
                        "event": "Поезд прибыл",
                        "unknown": "Кто в поезде",
                    }
                ]
            },
            "AC-03": {"decisions": [], "no_decisions_reason": "Трудных мест в тестовой сцене нет"},
            "AC-04": {"voice_and_devices": [addressed]},
            "AC-05": {"reading": [addressed]},
            "AC-06": {"consistency": [addressed]},
            "AC-07": {
                "reader": "Test reader 1.0",
                "checks": {
                    key: {"location": "ch.xhtml", "observation": "Просмотрено"}
                    for key in (
                        "cover",
                        "title",
                        "navigation",
                        "service",
                        "dialogue",
                        "italics",
                        "scene_break",
                        "scaling",
                        "anomalies",
                    )
                },
            },
            "AC-08": {},
            "AC-09": {"remaining": {"blocking": [], "major": [], "minor": []}},
        },
    }
    advance("acceptance", {}, code=2)
    assert (
        "identify the frozen plan"
        in advance("acceptance", {**receipt, "plan_sha256": "different-plan"}, code=2)["error"]
    )
    missing = receipt["criteria"].pop("AC-04")
    advance("acceptance", receipt, code=2)
    receipt["criteria"]["AC-04"] = missing
    receipt["criteria"]["AC-06"]["consistency"] = [{**addressed, "units": ["wrong-unit"]}]
    advance("acceptance", receipt, code=2)
    receipt["criteria"]["AC-06"]["consistency"] = [addressed]
    advance("acceptance", {**receipt, "artifact_sha256": "wrong"}, code=2)
    if pilot:
        advance("acceptance", {**receipt, "scope": "full"}, code=2)
    decision = {
        "units": [ids[0]],
        "quote": selected[0]["src"]["text"],
        "question": "Как передать образ?",
        "context": "Значение уточняет соседняя фраза",
        "alternatives": ["темно", "сумрачно"],
        "decision": "Выбрано «темно»",
        "recheck": "Проверены повторения",
        "sources": [],
    }
    receipt["criteria"]["AC-03"] = {"decisions": [decision]}
    advance("acceptance", receipt, code=2)
    decision["context_only_reason"] = "Сцена однозначно задаёт освещение"
    receipt["criteria"]["AC-09"]["remaining"]["major"] = [{"location": ids[0], "reason": "Открыт"}]
    advance("acceptance", receipt, code=2)
    receipt["criteria"]["AC-09"]["remaining"]["major"] = []
    check_log = Path(audited["epubcheck_report"])
    check_log.write_text("replaced", encoding="utf-8")
    advance("acceptance", receipt, code=2)
    run("artifact-check")
    progress = advance("acceptance", receipt)
    assert progress["expected_step"] == "done"
    assert progress["verified_current"]
    assert progress["acceptance"]["scope"] == delivery_scope
    assert len(progress["acceptance"]["criteria"]) == 9
    check_log.write_text("replaced after acceptance", encoding="utf-8")
    assert run("status")["workflow"]["expected_step"] == "acceptance"
    assert not run("status")["workflow"]["verified_current"]
    run("artifact-check")
    assert run("status")["workflow"]["verified_current"]
    assert not run(
        "workflow-reopen", "--chapter", str(route.chapter_numbers()[0]), "--reason", "Правка"
    )["workflow"]["verified_current"]


def test_epub_ncx_language_is_set_on_labels_not_text(source, tmp_path):
    ncx_source = tmp_path / "with-ncx.epub"
    with ZipFile(source) as original, ZipFile(ncx_source, "w") as modified:
        for entry in original.infolist():
            data = original.read(entry)
            if entry.filename == "OPS/book.opf":
                data = data.replace(
                    b"</manifest>",
                    b'<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
                    b"</manifest>",
                ).replace(b"<spine>", b'<spine toc="ncx">')
            modified.writestr(entry, data)
        modified.writestr(
            "OPS/toc.ncx",
            f'<ncx xmlns="{epub.NCX}" version="2005-1" xml:lang="en">'
            '<head><meta name="dtb:uid" content="urn:test:book"/></head>'
            "<docTitle><text>Night Train</text></docTitle><navMap>"
            '<navPoint id="chapter"><navLabel><text>First Chapter</text></navLabel>'
            '<content src="ch.xhtml#ch"/></navPoint></navMap></ncx>',
        )

    ws = store.Workspace(tmp_path / "work")
    ws.root.mkdir()
    epub.ingest(ncx_source, ws)
    units = [unit for unit in ws.units() if unit["epub"]["path"] == "OPS/toc.ncx"]
    targets = {"Night Train": "Ночной поезд", "First Chapter": "Первая глава"}
    xml_lang = epub.tag(epub.XML, "lang")
    selection = batch.Units({unit["unit_id"] for unit in units})
    info = batch.next_batch(ws, selection)
    draft = {
        "batch": info["batch"],
        "units": [{"id": unit["unit_id"], "tgt": targets[unit["src"]["text"]]} for unit in units],
    }
    cited = batch.cite(ws, draft)
    assert cited["ok"], cited
    assert batch.submit(ws, cited["result"])["ok"]
    epub.build(ncx_source, ws, "ru", True, "draft")
    with ZipFile(ws.root / "build" / "draft.epub") as output:
        ncx = etree.fromstring(output.read("OPS/toc.ncx"))
    assert ncx.get(xml_lang) == "en"
    labels = list(ncx.iter(epub.tag(epub.NCX, "text")))
    assert [node.text for node in labels] == ["Ночной поезд", "Первая глава"]
    assert all(node.get(xml_lang) is None for node in labels)
    assert all(node.getparent().get(xml_lang) == "ru" for node in labels)


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("темно", "светло"),
        ("Then the train came.", "Then the train vanished."),
        ("<em>", "<strong>"),
    ],
)
def test_epub_audit_refuses_changed_text_even_with_valid_markup(
    source, tmp_path, monkeypatch, original, replacement
):
    ws = store.Workspace(tmp_path / "work")
    ws.root.mkdir()
    epub.ingest(source, ws)
    unit = next(u for u in ws.units() if u["region"] == "body")
    selection = batch.Units({unit["unit_id"]})
    info = batch.next_batch(ws, selection)
    draft = {
        "batch": info["batch"],
        "units": [{"id": unit["unit_id"], "tgt": "Было ⟦1⟧⟦2⟧очень⟦/2⟧ темно⟦/1⟧⟦a3⟧."}],
    }
    cited = batch.cite(ws, draft)
    assert cited["ok"], cited
    assert batch.submit(ws, cited["result"])["ok"]
    epub.build(source, ws, "ru", True, "pilot", scope=selection)
    altered = tmp_path / "altered.epub"
    with ZipFile(ws.root / "build/pilot.epub") as valid, ZipFile(altered, "w") as invalid:
        for entry in valid.infolist():
            data = valid.read(entry)
            if entry.filename == "OPS/ch.xhtml":
                assert original.encode() in data
                data = data.replace(original.encode(), replacement.encode())
                if original == "<em>":
                    data = data.replace(b"</em>", b"</strong>")
            invalid.writestr(entry, data)
    monkeypatch.setenv("BT_EPUBCHECK", "dummy")
    monkeypatch.setattr(
        epub.subprocess,
        "run",
        lambda *a, **kw: epub.subprocess.CompletedProcess(a, 0, "", ""),
    )
    with pytest.raises(epub.EpubError, match="EPUB (text|inline markup) differs"):
        epub.audit(source, altered, ws, selftest=False)


def test_epub_text_in_image_becomes_a_translation_unit(source, tmp_path):
    illustrated = tmp_path / "illustrated.epub"
    png = BytesIO()
    Image.new("RGB", (4, 4), "white").save(png, format="PNG")
    with ZipFile(source) as original, ZipFile(illustrated, "w") as modified:
        for entry in original.infolist():
            data = original.read(entry)
            if entry.filename == "OPS/book.opf":
                data = data.replace(
                    b"</manifest>",
                    b'<item id="notice" href="notice.png" media-type="image/png"/></manifest>',
                )
            if entry.filename == "OPS/ch.xhtml":
                data = data.replace(
                    b"</section>",
                    b'<figure><img src="notice.png" alt="Station sign"/></figure></section>',
                )
            modified.writestr(entry, data)
        modified.writestr("OPS/notice.png", png.getvalue())
    ws = store.Workspace(tmp_path / "work")
    ws.root.mkdir()
    epub.ingest(illustrated, ws)
    [image] = store.read_jsonl(ws.ir / "objects.jsonl")
    assert image["epub_asset"] == "OPS/notice.png"
    assert ws.set_figures(
        [
            {
                "id": image["id"],
                "has_text": True,
                "transcription": "Platform closed",
            }
        ]
    )["ok"]
    unit = next(u for u in ws.units() if u["region"] == "figure_text")
    assert unit["src"]["text"] == "Platform closed"
    selection = batch.Units({unit["unit_id"]})
    info = batch.next_batch(ws, selection)
    cited = batch.cite(
        ws,
        {
            "batch": info["batch"],
            "units": [{"id": unit["unit_id"], "tgt": "Платформа закрыта"}],
        },
    )
    assert cited["ok"], cited
    assert batch.submit(ws, cited["result"])["ok"]
    epub.build(illustrated, ws, "ru", True, "pilot", scope=selection)
    with ZipFile(ws.root / "build/pilot.epub") as archive:
        assert "Платформа закрыта".encode() in archive.read("OPS/ch.xhtml")
        assert archive.read("OPS/notice.png") == png.getvalue()
