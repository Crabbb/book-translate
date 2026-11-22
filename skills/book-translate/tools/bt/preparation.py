"""Validate book-specific preparation and describe the goal for the host agent."""

from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit

from . import acceptance

GENERAL_CRITERIA = ("coverage", "fidelity", "terminology", "readability", "artifact", "workflow")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _fields(row, *fields):
    return isinstance(row, dict) and all(_text(row.get(field)) for field in fields)


def _addressed(row, selected):
    units = row.get("units")
    return (
        isinstance(units, list)
        and bool(units)
        and all(isinstance(unit, str) and unit in selected for unit in units)
        and len(units) == len(set(units))
    )


def _source(source):
    if not _fields(source, "url", "finding"):
        return False
    try:
        url = urlsplit(source["url"])
        return url.scheme in ("http", "https") and bool(url.hostname)
    except ValueError:
        return False


def check_context(report, selected):
    risks = report.get("risks")
    _require(isinstance(risks, list) and bool(risks), "analyze needs addressed book risks")
    seen = set()
    for row in risks:
        _require(
            _fields(row, "id", "question", "context", "decision") and _addressed(row, selected),
            "analyze risk needs id, units, question, context and decision",
        )
        _require(row["id"] not in seen, "duplicate analyze risk id")
        seen.add(row["id"])
        sources = row.get("sources")
        _require(
            isinstance(sources, list) and all(_source(source) for source in sources),
            "analyze sources must list URLs and findings",
        )
        _require(
            bool(sources) or _text(row.get("context_only_reason")),
            "analyze risk needs sources or a reason why the book resolves it",
        )


def check_plan(report, selected, context, fiction_epub):
    _require(
        _fields(report, "title", "deliverable", "scope_description"),
        "plan needs title, deliverable and scope_description",
    )
    criteria = report.get("criteria")
    required = acceptance.CRITERIA if fiction_epub else GENERAL_CRITERIA
    _require(
        isinstance(criteria, dict) and criteria.keys() == set(required),
        "plan must cover exactly these criteria: " + ", ".join(required),
    )
    risks = {row["id"]: row for row in context["risks"]}
    covered = set()
    for row in criteria.values():
        _require(
            _fields(row, "requirement", "evidence") and _addressed(row, selected),
            "each planned criterion needs a requirement, evidence and units",
        )
        links = row.get("risks")
        _require(
            isinstance(links, list)
            and all(isinstance(link, str) and link in risks for link in links),
            "planned criterion refers to an unknown analyze risk",
        )
        for link in links:
            _require(
                bool(set(row["units"]).intersection(risks[link]["units"])),
                "linked criterion must cite at least one unit of its analyze risk",
            )
        covered.update(links)
    _require(covered == risks.keys(), "plan must address every analyze risk")


def plan_sha256(plan):
    encoded = json.dumps(plan, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def goal_request(plan, workspace, source_sha256, scope, fiction_epub):
    plan_hash = plan_sha256(plan)
    final_step = " → acceptance" if fiction_epub else ""
    criteria = ", ".join(sorted(plan["criteria"]))
    objective = (
        f"Завершить перевод EN→RU «{plan['title']}» через book-translate. "
        f"Область: {json.dumps(scope, ensure_ascii=False, sort_keys=True)}. "
        f"Ожидаемый результат: {plan['deliverable']}. "
        f"Рабочая папка: {workspace.resolve()}. План и доказательства: workflow.json → book.plan. "
        f"Исходник SHA-256: {source_sha256}. План SHA-256: {plan_hash}. "
        f"Достичь всех критериев плана ({criteria}). "
        "Возобновлять по bt status. Для каждого раздела пройти "
        "translate → check → edit → read-target → recheck → gate; затем для всей области "
        f"crossbook → index → build → verify{final_step}. "
        "Сохранить проверенный артефакт и отчёт с доказательствами. "
        "Отметить цель выполненной после expected_step=done, verified_current=true "
        "и проверки области и всех критериев плана."
    )
    return {"objective": objective, "plan_sha256": plan_hash}


def check_goal(report, request):
    goal = report.get("goal")
    _require(_fields(goal, "threadId", "objective"), "goal needs the get_goal tool result")
    _require(goal.get("status") == "active", "goal must be active")
    _require(goal["objective"] == request["objective"], "goal objective differs from the plan")
    return {"threadId": goal["threadId"], **request}


def check_plan_review(report, plan, plan_sha256):
    _require(report.get("plan_sha256") == plan_sha256, "review must identify the frozen plan")
    review = report.get("plan_review")
    criteria = plan["criteria"]
    _require(
        isinstance(review, dict) and review.keys() == criteria.keys(),
        "review must address every planned criterion",
    )
    for key, row in review.items():
        _require(
            _fields(row, "observation")
            and row.get("verdict") == "pass"
            and row.get("units") == criteria[key]["units"],
            f"planned criterion {key} needs pass, observation and the planned units",
        )
