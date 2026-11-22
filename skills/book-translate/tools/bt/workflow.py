"""Order and receipts for a managed book. Guard hooks run at the bt CLI boundary."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import acceptance, artifact, batch, bible, epub, indexer, preparation, qa, store

BOOK_STEPS = (
    "brief",
    "ingest",
    "analyze",
    "bible",
    "plan",
    "goal",
    "chapters",
    "crossbook",
    "index",
    "build",
    "verify",
    "acceptance",
)
CHAPTER_STEPS = ("translate", "check", "edit", "read-target", "recheck", "gate")
MANUAL_FIELDS = {
    "brief": ("audience", "owner_decisions", "provisional"),
    "ingest": ("coverage", "source_anomalies", "visual"),
    "analyze": ("map", "voice", "notes", "figures"),
    "bible": ("terms", "forms", "rules"),
    "edit": ("observations",),
    "crossbook": ("consistency", "findings"),
    "index": ("bindings",),
    "verify": ("artifact_checks", "visual", "coverage", "unverified"),
}


class Refusal(ValueError):
    def __init__(self, message: str, step: str | None = None, chapter: int | None = None):
        super().__init__(message)
        self.payload = {"ok": False, "error": message, "expected_step": step, "chapter": chapter}


def file_hash(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def managed(ws: store.Workspace) -> bool:
    return (ws.root / "workflow.json").exists() or (ws.ir / "workflow-required").exists()


class Route:
    def __init__(self, ws: store.Workspace):
        self.ws = ws
        self.path = ws.root / "workflow.json"
        if not self.path.is_file():
            raise Refusal("no workflow.json: start a new workspace with workflow-start")
        self.state = json.loads(self.path.read_text("utf-8"))
        if self.state.get("version") not in (1, 2):
            raise Refusal("unsupported workflow state version")

    @classmethod
    def start(cls, ws: store.Workspace, source: Path, pilot_chapter=None, pilot_scene=None):
        path = ws.root / "workflow.json"
        source = source.resolve()
        if source.suffix.lower() not in (".pdf", ".epub"):
            raise Refusal("workflow source must be a PDF or EPUB")
        pilot = None
        if pilot_chapter is not None or pilot_scene is not None:
            if (
                source.suffix.lower() != ".epub"
                or not (pilot_chapter and pilot_scene)
                or min(pilot_chapter, pilot_scene) < 1
            ):
                raise Refusal("EPUB pilot requires positive --pilot-chapter and --pilot-scene")
            pilot = {"chapter": pilot_chapter, "scene": pilot_scene}
        info = {"path": str(source), "sha256": file_hash(source)}
        if path.exists():
            route = cls(ws)
            route.require(
                route.state["source"] == info, "workspace already belongs to another source"
            )
            route.require(route.pilot == pilot, "workspace already belongs to another scope")
            return route
        if (ws.ir / "units.jsonl").exists() or ws.results.exists():
            raise Refusal("existing workspace already contains a registry or results")
        state = {
            "version": 2,
            "source": info,
            "pilot": pilot,
            "book": {},
            "chapters": {},
            "events": {},
            "reopens": [],
        }
        store.write_atomic(path, json.dumps(state, ensure_ascii=False, indent=2))
        return cls(ws)

    def _save(self):
        store.write_atomic(self.path, json.dumps(self.state, ensure_ascii=False, indent=2))

    @property
    def pilot(self) -> dict | None:
        return self.state.get("pilot")

    def chapter_numbers(self) -> list[int]:
        return list(
            dict.fromkeys(u["chapter"] for u in batch._select(self.ws.units(), self.scope()))
        )

    @staticmethod
    def _genre(passport: dict):
        source = passport.get("source")
        return source.get("genre") if isinstance(source, dict) else None

    def _is_epub(self) -> bool:
        return Path(self.state["source"]["path"]).suffix.lower() == ".epub"

    def _fiction_epub(self) -> bool:
        if not self._is_epub():
            return False
        brief = self.state["book"].get("brief", {})
        return brief.get("genre", self._genre(self.ws.passport().get("passport", {}))) == "fiction"

    def check_passport(self, passport: dict):
        brief = self.state["book"].get("brief")
        if not self._is_epub() or brief is None:
            return
        genre = brief.get("genre", self._genre(self.ws.passport().get("passport", {})))
        self.require(self._genre(passport) == genre, "EPUB genre is fixed by the brief")

    def expected(self) -> tuple[str, int | None]:
        for step in BOOK_STEPS:
            if step in ("plan", "goal"):
                if self.state["version"] == 1:
                    continue
                if step == "plan":
                    plan = self.state["book"].get("plan", {})
                    if plan.get("basis") != self._plan_basis() or preparation.plan_sha256(
                        plan
                    ) != self.state["events"].get("plan_sha256"):
                        return step, None
                if step == "goal":
                    request = self._goal_request()
                    goal = self.state["book"].get("goal", {})
                    if not goal.get("threadId") or any(
                        goal.get(key) != value for key, value in request.items()
                    ):
                        return step, None
            if step == "acceptance":
                if not self._fiction_epub():
                    continue
                if not self._acceptance_receipts_current():
                    return step, None
            if step in self.state["book"]:
                continue
            if step != "chapters":
                return step, None
            for chapter in self.chapter_numbers():
                entries = self.state["chapters"].get(str(chapter), {})
                for chapter_step in CHAPTER_STEPS:
                    if chapter_step not in entries:
                        return chapter_step, chapter
            continue
        return "done", None

    def _plan_basis(self) -> dict:
        book = self.state["book"]
        return {
            "source_sha256": self.state["source"]["sha256"],
            "scope": self.pilot or "full",
            "registry_sha256": book["ingest"]["registry_sha256"],
            "analysis": store.digest(book["analyze"]),
            "bible": store.digest(book["bible"]),
            "contract_sha256": file_hash(acceptance.CONTRACT) if self._fiction_epub() else None,
        }

    def _goal_request(self) -> dict:
        return preparation.goal_request(
            self.state["book"]["plan"],
            self.ws.root,
            self.state["source"]["sha256"],
            self.pilot or "full",
            self._fiction_epub(),
        )

    def progress(self) -> dict:
        step, chapter = self.expected()
        current = (
            step == "done"
            and self._artifact_current("artifact")
            and self._source_is_current()
            and self.state["book"]["verify"]["inputs"] == self._final_inputs()
        )
        pending_preparation = (
            BOOK_STEPS[BOOK_STEPS.index(step) :] if step in ("plan", "goal") else ()
        )
        result = {
            "expected_step": step,
            "chapter": chapter,
            "pilot": self.pilot,
            "book_steps_completed": [
                name
                for name in self.state["book"]
                if name not in pending_preparation and (name != "acceptance" or current)
            ],
            "chapters_passed": [
                int(n) for n, entries in self.state["chapters"].items() if "gate" in entries
            ],
            "verified_current": current,
        }
        if step == "goal":
            result["goal_request"] = self._goal_request()
        elif self.state["version"] == 2 and "goal" in result["book_steps_completed"]:
            result["goal"] = self.state["book"]["goal"]
        if self._fiction_epub():
            accepted = self.state["book"].get("acceptance") if step == "done" else None
            result["acceptance"] = (
                {
                    key: value
                    for key, value in accepted.items()
                    if key not in ("evidence", "audit", "plan_review")
                }
                if current and accepted
                else None
            )
        return result

    def _acceptance_receipts_current(self) -> bool:
        accepted = self.state["book"].get("acceptance")
        return bool(
            accepted
            and accepted["contract_sha256"] == file_hash(acceptance.CONTRACT)
            and accepted["audit"] == self.state["events"].get("artifact_audit")
            and self._audit_is_current("artifact", require_checker_hash=True)
            and self._audit_is_current("selftest")
            and self._artifact_current("selftest")
        )

    def require(self, condition: bool, message: str):
        if not condition:
            raise Refusal(message, *self.expected())

    def _source_is_current(self, path: Path | None = None) -> bool:
        source = self.state["source"]
        candidate = path.resolve() if path is not None else Path(source["path"])
        return candidate.is_file() and file_hash(candidate) == source["sha256"]

    def source_is_current(self, path: Path | None = None):
        self.require(self._source_is_current(path), "source differs from workflow-start")

    def scope(self, chapter: int | None = None) -> batch.Chapters | batch.Units | None:
        if self.pilot:
            return batch.Units(
                u["unit_id"]
                for u in self.ws.units()
                if u["chapter"] == self.pilot["chapter"]
                and (chapter is None or chapter == u["chapter"])
                and u.get("scene") == self.pilot["scene"]
                and u["region"] in {"body", "quote", "list", "note", "table", "caption"}
            )
        return batch.Chapters({chapter}) if chapter is not None else None

    def _registry_is_current(self):
        recorded = self.state["book"].get("ingest", {}).get("registry_sha256")
        if recorded:
            path = self.ws.ir / "units.jsonl"
            self.require(path.is_file() and file_hash(path) == recorded, "source registry changed")

    def _final_inputs(self) -> str:
        paths = [p for p in self.ws.ir.rglob("*") if p.is_file()]
        paths.extend(self.ws.root / name for name in ("anomalies.jsonl", "decisions.jsonl"))
        for folder in (self.ws.bible, self.ws.results, self.ws.batches):
            paths.extend(path for path in folder.rglob("*") if path.is_file())
        paths.extend(self.ws.reports.glob("evidence-*.jsonl"))
        paths.append(self.ws.reports / "dispositions.jsonl")
        return store.digest(
            [
                (p.relative_to(self.ws.root).as_posix(), file_hash(p))
                for p in sorted(paths)
                if p.is_file()
            ]
        )

    def guard(self, args):
        command = args.command
        step, chapter = self.expected()
        self._registry_is_current()
        if command in ("next", "cite", "submit"):
            self.source_is_current()
        if command == "ingest":
            self.require(step == "ingest", "finish brief before ingest")
            self.source_is_current(Path(args.pdf))
        if command == "build":
            self.source_is_current(Path(args.pdf))
            self.require(
                not args.pages and re.fullmatch(r"[\w-]+", args.name) is not None,
                "managed build needs the whole book and a simple artifact name",
            )
            self.require(
                (step == "ingest" and args.lang == "en" and args.draft)
                or (step == "index" and not self.pilot and args.lang == "ru" and args.draft)
                or (
                    step in ("build", "verify")
                    and args.lang == "ru"
                    and args.draft == bool(self.pilot)
                ),
                "build is available for the English selftest, index draft or final artifact",
            )
            if step == "ingest":
                self.require(
                    bool(self.state["events"].get("ingest")), "run bt ingest before selftest"
                )
                self._ingest_registry_is_current()
            if step in ("build", "verify"):
                self._ready_for_final()
                if step == "verify":
                    self.require(
                        self.state["book"]["build"]["inputs"] == self._final_inputs(),
                        "translation inputs changed after build; reopen the workflow",
                    )
        if command in ("next", "qa", "gate"):
            required = "translate" if command == "next" else "gate"
            self.require(
                step == required and chapter == args.chapter and not args.pages,
                f"{command} requires the current chapter at step {required}",
            )
            if command in ("qa", "gate"):
                self.require(re.fullmatch(r"[\w-]+", args.name) is not None, "invalid report name")
            if command == "gate":
                self.require(self._qa_is_current(chapter), "run bt qa on this chapter before gate")
        if command in ("cite", "submit"):
            self.require(step == "translate", f"{command} requires the translation step")
            payload = json.loads(Path(args.file).read_text("utf-8"))
            batch_id = payload.get("batch")
            self.require(
                isinstance(batch_id, str) and re.fullmatch(r"b\d+", batch_id) is not None,
                "invalid package ID",
            )
            path = self.ws.batches / f"{batch_id}.json"
            self.require(path.is_file(), "take a package with bt next first")
            package = json.loads(path.read_text("utf-8"))
            self._require_chapter(package["manifest"]["unit_ids"], chapter)
            if command == "cite":
                dest = Path(args.out or args.file).resolve()
                parents = (self.ws.root.resolve(), self.ws.ir.resolve(), self.ws.bible.resolve())
                reserved = (
                    *parents[1:],
                    self.ws.results.resolve(),
                    self.ws.reports.resolve(),
                    self.ws.batches.resolve(),
                )
                self.require(
                    dest.parent != parents[0] and not any(dest.is_relative_to(p) for p in reserved),
                    "write cited drafts in a separate drafts directory",
                )
            if command == "submit":
                self.require(
                    self.state["events"].get("cited", {}).get(batch_id)
                    == file_hash(Path(args.file)),
                    "run bt cite on this exact result before submit",
                )
        if command in ("evidence", "edit"):
            valid = {
                "check": {"check"},
                "edit": {"edit"},
                "read-target": {"read-target"},
                "recheck": {"check", "read-target"},
            }
            repair = step == "translate" and command == "edit" and args.step == "check"
            self.require(
                repair or args.step in valid.get(step, set()), f"{command} is out of order"
            )
            self.require(
                command != "evidence" or args.step != "edit", "record edit with workflow-advance"
            )
            entries = json.loads(Path(args.file).read_text("utf-8"))
            self.require(isinstance(entries, list) and bool(entries), "empty chapter input")
            self.require(all(isinstance(e, dict) for e in entries), "invalid chapter input")
            nested = args.step == "read-target" and command == "evidence"
            if nested:
                self.require(
                    all(isinstance(e.get("units"), list) and e["units"] for e in entries),
                    "read-target needs a window of unit IDs",
                )
            key = "id" if command == "edit" else "unit"
            ids = [uid for e in entries for uid in (e["units"] if nested else [e.get(key)])]
            self._require_chapter(ids, chapter)
            if repair:
                stale = batch.status(self.ws, self.scope(chapter))["stale"]
                self.require(
                    all(uid in stale for uid in ids),
                    "only stale units can be revised during translation",
                )
        if command == "decide":
            self.require(step == "gate", "candidate decisions belong to the gate step")
            self.require(
                self._qa_is_current(chapter), "run bt qa before deciding on its candidates"
            )
            report = Path(self.state["events"]["qa"][str(chapter)]["path"])
            candidates = {
                (finding["key"], finding.get("unit"))
                for finding in json.loads(report.read_text("utf-8"))["findings"]
                if finding["class"] == "C"
            }
            decisions = json.loads(Path(args.file).read_text("utf-8"))
            self.require(
                isinstance(decisions, list)
                and bool(decisions)
                and all(
                    isinstance(d, dict)
                    and isinstance(d.get("key"), str)
                    and (d.get("unit") is None or isinstance(d["unit"], str))
                    and (d["key"], d.get("unit")) in candidates
                    for d in decisions
                ),
                "decision does not match a candidate in the current chapter QA report",
            )
        if command.startswith("index-"):
            self.require(step == "index", "finish crossbook before rebuilding the index")
            if command == "index-bind":
                self.require(
                    self._artifact_current("index_draft", positions=args.positions),
                    "use positions from the current index draft",
                )
        earliest = {
            "notes-add": "analyze",
            "figures-set": "analyze",
            "prompts-set": "analyze",
            "voice-set": "analyze",
            "term-candidates": "analyze",
            "bible-add": "bible",
            "bible-change": "bible",
            "procedure-change": "bible",
        }
        if command in earliest:
            book_step = "chapters" if chapter is not None else step
            self.require(
                book_step
                in BOOK_STEPS[BOOK_STEPS.index(earliest[command]) : BOOK_STEPS.index("build")],
                f"{command} is unavailable at this step",
            )
        if command == "passport-set":
            self.require(
                step in ("brief", "bible", *CHAPTER_STEPS, "crossbook"),
                "passport change is out of order",
            )

    def _require_chapter(self, ids: list, chapter: int | None):
        wanted = {u["unit_id"] for u in batch._select(self.ws.units(), self.scope(chapter))}
        self.require(
            chapter is not None
            and bool(ids)
            and all(isinstance(uid, str) and uid in wanted for uid in ids),
            "unit does not belong to the current chapter",
        )

    def _artifact_current(self, event: str, positions: str | None = None) -> bool:
        artifact = self.state["events"].get(event)
        if not artifact:
            return False
        path = Path(artifact["path"])
        if not path.is_file() or file_hash(path) != artifact["sha256"]:
            return False
        if event != "index_draft":
            return True
        pos = path.with_suffix(".positions.json")
        return (
            positions is not None
            and Path(positions).resolve() == pos
            and pos.is_file()
            and file_hash(pos) == artifact["positions_sha256"]
        )

    def _chapter_inputs(self, chapter: int) -> str:
        units = batch._select(self.ws.units(), self.scope(chapter))
        accepted = self.ws.accepted()
        return store.digest(
            {
                "text": [(u["unit_id"], accepted.get(u["unit_id"])) for u in units],
                "concepts": self.ws.concepts(),
                "passport": self.ws.passport(),
                "voice": self.ws.voice(),
                "notes": self.ws.notes(),
            }
        )

    def _qa_is_current(self, chapter: int) -> bool:
        receipt = self.state["events"].get("qa", {}).get(str(chapter))
        if not receipt or receipt["inputs"] != self._chapter_inputs(chapter):
            return False
        path = Path(receipt["path"])
        return path.is_file() and file_hash(path) == receipt["sha256"]

    def observe(self, args):
        """Record successful actions from their actual outputs, never from an agent claim."""
        events = self.state["events"]
        command = args.command
        if command == "ingest":
            store.write_atomic(self.ws.ir / "workflow-required", "1\n")
            events["ingest"] = {
                "report_sha256": file_hash(self.ws.reports / "ingest.json"),
                "registry_sha256": file_hash(self.ws.ir / "units.jsonl"),
            }
            events.pop("selftest", None)
            events.pop("selftest_audit", None)
        elif command == "build":
            step, _ = self.expected()
            event = {"ingest": "selftest", "index": "index_draft"}.get(step, "artifact")
            suffix = Path(args.pdf).suffix.lower()
            path = (self.ws.root / "build" / f"{args.name}{suffix}").resolve()
            events[event] = {
                "path": str(path),
                "sha256": file_hash(path),
            }
            if suffix == ".pdf":
                events[event]["positions_sha256"] = file_hash(path.with_suffix(".positions.json"))
            events.pop(f"{event}_audit", None)
            if step == "verify":
                self.state["book"]["build"]["artifact"] = events[event]
        elif command == "cite":
            path = Path(args.out or args.file)
            package = json.loads(path.read_text("utf-8"))
            events.setdefault("cited", {})[package["batch"]] = file_hash(path)
        elif command == "qa":
            path = (self.ws.reports / f"qa-{args.name}.json").resolve()
            events.setdefault("qa", {})[str(args.chapter)] = {
                "inputs": self._chapter_inputs(args.chapter),
                "path": str(path),
                "sha256": file_hash(path),
            }
        elif command == "gate":
            events.setdefault("gate", {})[str(args.chapter)] = self._chapter_inputs(args.chapter)
        else:
            return
        self._save()

    def audit_artifact(self, *, selftest: bool = False) -> dict:
        step, _ = self.expected()
        self.require(
            step in ("ingest", "verify", "acceptance"),
            "artifact checks follow selftest or final build",
        )
        self.require(not selftest or step == "acceptance", "--selftest is for acceptance recovery")
        event = "selftest" if step == "ingest" or selftest else "artifact"
        if event == "selftest":
            self._ingest_registry_is_current()
        if step == "acceptance":
            self._ready_for_final()
        self.require(self._artifact_current(event), "build the current artifact first")
        receipt = self.state["events"][event]
        path = Path(receipt["path"])
        if path.suffix == ".epub":
            source = Path(self.state["source"]["path"])
            self.source_is_current(source)
            result = epub.audit(source, path, self.ws, selftest=event == "selftest")
            self.state["events"][f"{event}_audit"] = result
            self._save()
            return result
        position_file = path.with_suffix(".positions.json")
        self.require(
            position_file.is_file() and file_hash(position_file) == receipt["positions_sha256"],
            "positions differ from the build output",
        )
        accepted = self.ws.accepted() if event == "artifact" else {}
        units = [{**u, "result": accepted.get(u["unit_id"], {})} for u in self.ws.units()]
        objects = store.read_jsonl(self.ws.ir / "objects.jsonl")
        audit = artifact.Artifact(path, units, json.loads(position_file.read_text("utf-8"))).run(
            ["LibertinusSerif", "SourceSans3", "LiberationMono"],
            figures=sum(o["kind"] == "figure" for o in objects),
        )
        findings = {
            store.digest([check, finding]): {"check": check, "finding": finding}
            for check, rows in audit["findings"].items()
            for finding in rows
        }
        result = {"sha256": receipt["sha256"], "counts": audit["counts"], "findings": findings}
        self.state["events"][f"{event}_audit"] = result
        self._save()
        return result

    def _audit_is_current(self, event: str, *, require_checker_hash=False) -> bool:
        events = self.state["events"]
        recorded = events.get(f"{event}_audit")
        if not recorded or recorded["sha256"] != events.get(event, {}).get("sha256"):
            return False
        if "coverage_sha256" in recorded:
            coverage = self.ws.reports / Path(recorded["coverage_file"]).name
            if not coverage.is_file() or file_hash(coverage) != recorded["coverage_sha256"]:
                return False
            checker_report = self.ws.reports / Path(recorded["epubcheck_report"]).name
            checker_hash = recorded.get("epubcheck_sha256")
            return checker_report.is_file() and (
                (checker_hash is None and not require_checker_hash)
                or file_hash(checker_report) == checker_hash
            )
        return not require_checker_hash

    def _require_audit(self, event: str, report: dict):
        self.require(self._audit_is_current(event), "run bt artifact-check on the current file")
        recorded = self.state["events"][f"{event}_audit"]
        dispositions = report.get("artifact_findings", {})
        self.require(
            isinstance(dispositions, dict)
            and set(dispositions) == set(recorded["findings"])
            and all(isinstance(reason, str) and reason.strip() for reason in dispositions.values()),
            "resolve every artifact finding or record its ID and the reason to accept it",
        )

    def _manual(self, step: str, report: dict) -> dict:
        self.require(isinstance(report, dict), "checkpoint evidence must be an object")
        fields = MANUAL_FIELDS.get(step, ())
        empty = [
            key for key in fields if not isinstance(report.get(key), str) or not report[key].strip()
        ]
        self.require(not empty, "missing checkpoint evidence: " + ", ".join(empty))
        return report

    def _windows(self, report: dict, scope):
        windows = report.get("windows")
        self.require(
            isinstance(windows, list) and bool(windows), "checkpoint needs reading windows"
        )
        ids = []
        for window in windows:
            self.require(
                isinstance(window, dict)
                and isinstance(window.get("units"), list)
                and bool(window["units"])
                and all(isinstance(uid, str) for uid in window["units"])
                and isinstance(window.get("observation"), str)
                and bool(window["observation"].strip()),
                "every reading window needs unit IDs and an observation",
            )
            ids.extend(window["units"])
        selected = batch._select(self.ws.units(), scope)
        wanted = [u["unit_id"] for u in selected]
        self.require(
            ids == wanted and bool(wanted),
            "reading windows must cover the selection once in reading order",
        )

    def _ready_for_final(self):
        state = batch.status(self.ws, self.scope())
        self.require(
            state["selected"] > 0
            and not any(state[k] for k in ("todo_count", "stale_count", "blocking_questions")),
            "translation coverage or freshness changed; reopen the affected chapter",
        )
        self.require(
            self.state["book"]["index"]["inputs"] == self._final_inputs(),
            "chapter or index inputs changed; reopen the workflow",
        )

    def _ingest_registry_is_current(self):
        receipt = self.state["events"].get("ingest", {})
        path = self.ws.ir / "units.jsonl"
        self.require(
            path.is_file() and receipt.get("registry_sha256") == file_hash(path),
            "source registry differs from the ingest output; rerun ingest",
        )

    def _complete_ingest(self, report: dict):
        path = self.ws.reports / "ingest.json"
        receipt = self.state["events"].get("ingest", {})
        self.require(
            path.is_file() and receipt.get("report_sha256") == file_hash(path),
            "run bt ingest first",
        )
        self._ingest_registry_is_current()
        result = json.loads(path.read_text("utf-8"))
        mapped = (
            result.get("unmapped_text") == 0
            if result.get("format") == "epub"
            else result.get("unmapped_glyphs") == 0
            and result.get("pdfium", {}).get("unexplained_pages") == 0
        )
        self.require(
            bool(result.get("units")) and mapped,
            "ingest has unexplained text coverage",
        )
        if self.pilot:
            self.require(bool(self.scope()), "pilot scene is absent or has no translatable text")
        open_items = [
            row
            for name in ("anomalies.jsonl", "decisions.jsonl")
            for row in store.read_jsonl(self.ws.root / name)
            if row.get("status") == "open" or row.get("decision") == "open"
        ]
        self.require(not open_items, "resolve ingest anomalies and decisions first")
        self.require(
            self._artifact_current("selftest"), "build the complete English selftest first"
        )
        self.require(
            report.get("artifact_sha256") == self.state["events"]["selftest"]["sha256"],
            "visual review must identify the exact selftest SHA-256",
        )
        self._require_audit("selftest", report)
        report["registry_sha256"] = receipt["registry_sha256"]

    def _complete_analysis(self, report: dict):
        self._windows(report, self.scope())
        if self.state["version"] == 2:
            self._validate_preparation("analyze", report)
        self.require((self.ws.bible / "voice.yaml").is_file(), "record voice through bt voice-set")
        self.require((self.ws.reports / "term-candidates.json").is_file(), "run bt term-candidates")
        figures = {
            o["id"] for o in store.read_jsonl(self.ws.ir / "objects.jsonl") if o["kind"] == "figure"
        }
        decisions = set()
        for row in store.read_jsonl(self.ws.bible / "figures.jsonl"):
            path = self.ws.ir / "figures" / f"{row['id']}.png"
            if path.is_file() and file_hash(path)[:16] == row.get("image_hash"):
                decisions.add(row["id"])
        self.require(figures <= decisions, "inspect and record every figure through bt figures-set")

    def _complete_chapter(self, step: str, chapter: int, report: dict):
        scope = self.scope(chapter)
        state = batch.status(self.ws, scope)
        self.require(
            state["selected"] > 0
            and not any(state[k] for k in ("todo_count", "stale_count", "blocking_questions")),
            "chapter has missing or stale translations or blocking questions",
        )
        if step == "edit":
            self._windows(report, scope)
        checks = {
            "check": ("check",),
            "read-target": ("read-target",),
            "recheck": ("check", "read-target"),
            "gate": ("check", "read-target"),
        }.get(step)
        if checks:
            self.require(
                not bible.rule_problems(self.ws.passport().get("rules", [])),
                "invalid passport rules",
            )
            verdict = qa.chapter_gate(self.ws, scope, "workflow-chapter", steps=checks)
            self.require(
                not verdict.get("missing_evidence"),
                "required chapter evidence is incomplete or stale",
            )
            self.require(
                not any(b.get("rule") in checks for b in verdict.get("blockers", [])),
                "close major or critical findings from the required pass",
            )
            if step == "gate":
                self.require(
                    self._qa_is_current(chapter), "run bt qa for the current version first"
                )
                self.require(
                    self.state["events"].get("gate", {}).get(str(chapter))
                    == self._chapter_inputs(chapter),
                    "run bt gate for the current chapter first",
                )
                self.require(verdict["verdict"] == "pass", "chapter gate has not passed")
                report["gate"] = verdict

    def advance(self, step: str, chapter: int | None, report: dict) -> dict:
        expected, current_chapter = self.expected()
        # A resumed run can attach to another host thread without changing its frozen plan.
        rebind_goal = (
            step == "goal"
            and chapter is None
            and self.state["version"] == 2
            and "goal" in self.state["book"]
            and expected not in ("brief", "ingest", "analyze", "bible", "plan", "done")
        )
        self.require(
            rebind_goal or ((step, chapter) == (expected, current_chapter) and step != "done"),
            "checkpoint is out of order",
        )
        self._registry_is_current()
        self.source_is_current()
        report = self._manual(step, report)
        if chapter is not None:
            self._complete_chapter(step, chapter, report)
            self.state["chapters"].setdefault(str(chapter), {})[step] = report
            self._save()
            return self.progress()
        if step in ("brief", "bible"):
            doc = self.ws.passport()
            passport = doc.get("passport", {})
            self.require(
                isinstance(passport, dict)
                and bool(passport)
                and not bible.rule_problems(doc.get("rules", [])),
                "record the passport and valid rules through bt passport-set",
            )
            genre = self._genre(passport)
            if self._is_epub() and step == "brief":
                self.require(
                    genre in ("fiction", "nonfiction", "technical_book"),
                    "EPUB brief needs passport.source.genre: fiction, nonfiction or technical_book",
                )
                report["genre"] = genre
            if genre == "fiction":
                policies = passport.get("fiction")
                self.require(
                    isinstance(policies, dict)
                    and all(
                        isinstance(policies.get(key), str) and policies[key].strip()
                        for key in (
                            "dialogue",
                            "inner_speech",
                            "written_messages",
                            "embedded_texts",
                        )
                    ),
                    "fiction needs a policy for dialogue, inner speech, written messages and embedded texts",
                )
        if step == "ingest":
            self._complete_ingest(report)
        if step == "analyze":
            self._complete_analysis(report)
        if step in ("plan", "goal"):
            report = self._validate_preparation(step, report)
        if self.state["version"] == 2 and step == (
            "acceptance" if self._fiction_epub() else "verify"
        ):
            try:
                preparation.check_plan_review(
                    report, self.state["book"]["plan"], self._goal_request()["plan_sha256"]
                )
            except ValueError as error:
                self.require(False, str(error))
        if step == "crossbook":
            self.require(bool(self.chapter_numbers()), "there are no chapters to reconcile")
            for number in self.chapter_numbers():
                verdict = qa.chapter_gate(self.ws, self.scope(number), "workflow-crossbook")
                self.require(verdict["verdict"] == "pass", f"chapter {number} no longer passes")
        if step == "index":
            if store.read_jsonl(self.ws.ir / "index.jsonl"):
                status = indexer.blockers(self.ws)
                self.require(
                    status["bound"] and not status["open"] and not status["same_russian"],
                    "index bindings or Russian duplicates remain open",
                )
            report["inputs"] = self._final_inputs()
        if step in ("build", "verify"):
            self._ready_for_final()
            self.require(self._artifact_current("artifact"), "build the final artifact again")
            artifact = self.state["events"]["artifact"]
            report["artifact"] = artifact
            report["inputs"] = self._final_inputs()
            if step == "verify":
                self._require_audit("artifact", report)
                self.require(
                    report.get("artifact_sha256") == artifact["sha256"],
                    "verification must identify the exact final SHA-256",
                )
                self.require(
                    self.state["book"]["build"]["inputs"] == report["inputs"],
                    "translation inputs changed after build",
                )
        if step == "acceptance":
            self._ready_for_final()
            self.require(self._artifact_current("artifact"), "final artifact changed")
            self.require(self._artifact_current("selftest"), "English selftest artifact changed")
            self._require_audit("selftest", self.state["book"]["ingest"])
            self.require(
                self._audit_is_current("artifact", require_checker_hash=True),
                "run bt artifact-check on the current final EPUB",
            )
            verify = self.state["book"]["verify"]
            self.require(
                verify["artifact_sha256"] == self.state["events"]["artifact"]["sha256"],
                "artifact differs from the verified EPUB",
            )
            self._require_audit("artifact", verify)
            try:
                reviewed = acceptance.review(self, report)
            except ValueError as error:
                self.require(False, str(error))
            if self.state["version"] == 2:
                reviewed["plan_sha256"] = report["plan_sha256"]
                reviewed["plan_review"] = report["plan_review"]
            report = reviewed
        self.state["book"][step] = report
        self._save()
        return self.progress()

    def _validate_preparation(self, step: str, report: dict) -> dict:
        book = self.state["book"]
        try:
            if step == "goal":
                return preparation.check_goal(report, self._goal_request())
            selected = {unit["unit_id"] for unit in batch._select(self.ws.units(), self.scope())}
            if step == "analyze":
                preparation.check_context(report, selected)
            if step == "plan":
                self.require(
                    not any(
                        question.get("status") == "open" and question.get("blocking")
                        for question in self.ws.questions()
                    ),
                    "resolve blocking questions before freezing the plan",
                )
                preparation.check_context(book["analyze"], selected)
                preparation.check_plan(report, selected, book["analyze"], self._fiction_epub())
                report["basis"] = self._plan_basis()
                self.state["events"]["plan_sha256"] = preparation.plan_sha256(report)
                for later in ("goal", "crossbook", "index", "build", "verify", "acceptance"):
                    book.pop(later, None)
        except ValueError as error:
            self.require(False, str(error))
        return report

    def reopen(self, chapter: int, reason: str) -> dict:
        self.require(bool(reason.strip()), "reopen needs a reason")
        numbers = self.chapter_numbers()
        self.require(
            chapter in numbers and str(chapter) in self.state["chapters"],
            "only a chapter with a recorded checkpoint can be reopened",
        )
        for number in numbers[numbers.index(chapter) :]:
            entries = self.state["chapters"].pop(str(number), {})
            if "translate" in entries:
                self.state["chapters"][str(number)] = {"translate": entries["translate"]}
            self.state["events"].get("qa", {}).pop(str(number), None)
            self.state["events"].get("gate", {}).pop(str(number), None)
        for step in ("crossbook", "index", "build", "verify", "acceptance"):
            self.state["book"].pop(step, None)
        for event in ("index_draft", "artifact"):
            self.state["events"].pop(event, None)
        self.state["reopens"].append({"chapter": chapter, "reason": reason})
        self._save()
        return self.progress()
