"""Validate evidence for the fiction EPUB acceptance criteria."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from . import batch, epub, store

CONTRACT = Path(__file__).resolve().parents[2] / "references" / "fiction-epub-acceptance.md"
CRITERIA = tuple(f"AC-{i:02d}" for i in range(1, 10))
ADDRESSED_FIELDS = {
    "AC-02": "scene_checks",
    "AC-04": "voice_and_devices",
    "AC-05": "reading",
    "AC-06": "consistency",
}
READER_CHECKS = {
    "cover",
    "title",
    "navigation",
    "service",
    "dialogue",
    "italics",
    "scene_break",
    "scaling",
    "anomalies",
}


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _fields(row, *fields):
    return isinstance(row, dict) and all(_text(row.get(key)) for key in fields)


def _units(row, selected):
    ids = row.get("units")
    return (
        isinstance(ids, list)
        and bool(ids)
        and all(isinstance(uid, str) and uid in selected for uid in ids)
        and len(ids) == len(set(ids))
    )


def _decisions(entry, selected):
    rows = entry.get("decisions")
    _require(isinstance(rows, list), "AC-03 needs a decisions list")
    if not rows:
        _require(_text(entry.get("no_decisions_reason")), "AC-03 needs a reason for no decisions")
    for row in rows:
        _require(
            _fields(row, "quote", "question", "context", "decision", "recheck")
            and _units(row, selected),
            "AC-03 decision needs addressed units, quote, question, context, decision and recheck",
        )
        alternatives = row.get("alternatives")
        _require(
            isinstance(alternatives, list)
            and len(alternatives) >= 2
            and all(_text(value) for value in alternatives)
            and len(set(alternatives)) == len(alternatives),
            "AC-03 decision needs at least two distinct alternatives",
        )
        sources = row.get("sources")
        _require(isinstance(sources, list), "AC-03 decision needs a sources list")
        if not sources:
            _require(
                _text(row.get("context_only_reason")), "AC-03 needs a reason for context alone"
            )
        for source in sources:
            _require(_fields(source, "url", "finding"), "AC-03 source needs a URL and a finding")
            url = urlsplit(source["url"])
            _require(url.scheme in ("http", "https") and url.hostname, "AC-03 needs a web URL")


def _reader_review(entry):
    checks = entry.get("checks")
    _require(
        _text(entry.get("reader")) and isinstance(checks, dict) and checks.keys() == READER_CHECKS,
        "AC-07 needs a reader/version and every named visual check",
    )
    for key, check in checks.items():
        absent = key not in ("navigation", "scaling") and _fields(check, "not_applicable")
        _require(
            absent or _fields(check, "location", "observation"),
            f"AC-07 needs a location and observation for {key}, or a reason when absent",
        )


def review(route, report: dict) -> dict:
    """Join human observations to machine receipts for the frozen scope."""
    ws = route.ws
    artifact = route.state["events"]["artifact"]
    audit = route.state["events"]["artifact_audit"]
    scope = route.pilot or "full"
    _require(report.get("scope") == scope, "acceptance scope does not match workflow-start")
    _require(
        report.get("source_sha256") == route.state["source"]["sha256"]
        and report.get("artifact_sha256") == artifact["sha256"],
        "acceptance needs the exact source and final EPUB SHA-256",
    )
    criteria = report.get("criteria")
    _require(
        isinstance(criteria, dict)
        and criteria.keys() == set(CRITERIA)
        and all(isinstance(value, dict) for value in criteria.values()),
        "acceptance needs one evidence object per AC-01 through AC-09",
    )

    units = ws.units()
    selected = {unit["unit_id"] for unit in batch._select(units, route.scope())}
    rows = store.read_jsonl(ws.reports / Path(audit["coverage_file"]).name)
    covered = {row["unit"] for row in rows}
    expected = {u["unit_id"] for u in units if not u.get("epub_figure") or u["unit_id"] in selected}
    _require(
        len(rows) == len(covered)
        and covered == expected
        and {row["unit"] for row in rows if row["status"] == "translated"} == selected,
        "AC-01 archive coverage must match the frozen scope and registry exactly",
    )
    exceptions = criteria["AC-01"].get("exceptions")
    _require(
        isinstance(exceptions, list)
        and all(_fields(row, "location", "reason") for row in exceptions),
        "AC-01 needs an exceptions list with locations and reasons for retained source forms",
    )
    for criterion, field in ADDRESSED_FIELDS.items():
        observations = criteria[criterion].get(field)
        _require(
            isinstance(observations, list)
            and bool(observations)
            and all(_fields(row, "observation") and _units(row, selected) for row in observations),
            f"{criterion} needs {field} with observations and valid unit IDs from this scope",
        )
    _require(
        all(_fields(row, "actor", "event", "unknown") for row in criteria["AC-02"]["scene_checks"]),
        "AC-02 scene checks need actor, event and what remains unknown",
    )
    chapter_by_unit = {u["unit_id"]: u["chapter"] for u in units if u["unit_id"] in selected}
    chapter_count = len(set(chapter_by_unit.values()))
    if chapter_count > 1:
        _require(
            all(
                len({chapter_by_unit[uid] for uid in row["units"]}) >= 2
                for row in criteria["AC-06"]["consistency"]
            ),
            "AC-06 each full-book consistency observation must link different chapters",
        )
    _decisions(criteria["AC-03"], selected)
    _reader_review(criteria["AC-07"])
    _require(not criteria["AC-08"], "AC-08 is verified by the workflow and needs an empty object")
    remaining = criteria["AC-09"].get("remaining")
    _require(
        isinstance(remaining, dict)
        and remaining.keys() == {"blocking", "major", "minor"}
        and remaining["blocking"] == []
        and remaining["major"] == []
        and isinstance(remaining["minor"], list)
        and all(_fields(row, "location", "reason") for row in remaining["minor"]),
        "AC-09 requires zero blocking or major issues and a disposition for each minor issue",
    )
    return {
        "scope": scope,
        "source_sha256": report["source_sha256"],
        "artifact_sha256": artifact["sha256"],
        "contract_sha256": epub.sha(CONTRACT),
        "criteria": dict.fromkeys(CRITERIA, "pass"),
        "evidence": criteria,
        "chapters_passed": route.chapter_numbers(),
        "coverage": {"translated": len(selected), "total": len(covered)},
        "audit": audit,
    }
