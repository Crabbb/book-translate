"""Score a judge qualification run against its blind key (spec section 9).

Severity is computed from the judge's yes/no answers, and quotes are checked against the packet:
a major without real quotes is `invalid`, not a finding.

Usage: score_judge.py <run_dir> <key.jsonl> [--out report.json]
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from math import sqrt
from pathlib import Path

RANK = {"suggestion": 0, "minor": 1, "major": 2, "critical": 3}


def wilson(k: int, n: int, z: float = 1.96) -> list[float]:
    if not n:
        return [0.0, 1.0]
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(c - h, 3), round(c + h, 3)]


def severity(harm: dict | None, stated: str | None) -> str:
    """The decision tree: code decides from the answers, the stated label is only compared."""
    if not harm:
        return stated or "suggestion"
    if not harm.get("q1"):
        return "suggestion"
    if harm.get("q2"):
        return "critical"
    if harm.get("q3"):
        return "major"
    return "minor"


def norm(text: str) -> str:
    return re.sub(r"\W+", " ", re.sub("⟦/?a?\\d+⟧", "", text or "")).strip().lower()


def overlaps(a: str, b: str) -> bool:
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    wa, wb = set(a.split()), set(b.split())
    return len(wa & wb) >= max(2, min(len(wa), len(wb)) // 2)


def score(run_dir: Path, key_path: Path) -> dict:
    key = {
        json.loads(x)["item"]: json.loads(x)
        for x in key_path.read_text("utf-8").splitlines()
        if x.strip()
    }
    packets, results = {}, {}
    for f in sorted(run_dir.glob("w??.json")):
        for it in json.loads(f.read_text("utf-8"))["items"]:
            packets[it["item"]] = it
    for f in sorted(run_dir.glob("w??.result.json")):
        for it in json.loads(f.read_text("utf-8"))["items"]:
            results[it["item"]] = it.get("defects", [])
    invalid, stated_diff = [], 0
    cleaned: dict[str, list[dict]] = {}
    for item, defects in results.items():
        cleaned[item] = []
        for d in defects:
            sev = severity(d.get("harm"), d.get("severity"))
            stated_diff += sev != d.get("severity")
            pkt = packets[item]
            real = norm(d.get("en", "")) in norm(pkt["en"]) and norm(d.get("ru", "")) in norm(
                pkt["ru"]
            )
            if RANK[sev] >= 2 and not (real and d.get("en") and d.get("ru") and d.get("basis")):
                invalid.append({"item": item, "severity": sev, "ru": (d.get("ru") or "")[:60]})
                continue
            cleaned[item].append({**d, "computed": sev})

    missing = sorted(set(key) - set(results))
    a_found, a_any, misses, by_class = 0, 0, [], defaultdict(lambda: [0, 0])
    crit_found, crit_total, crit_missed = 0, 0, []
    agree = Counter()
    for item, k in key.items():
        if k["part"] != "A":
            continue
        by_class[k["class"]][1] += 1
        hits = [d for d in cleaned.get(item, []) if overlaps(d.get("ru", ""), k["ru_quote"])]
        strong = [d for d in hits if RANK[d["computed"]] >= 2]
        expected_crit = k["severity"] == "critical"
        crit_total += expected_crit
        if strong:
            a_found += 1
            by_class[k["class"]][0] += 1
            best = max(strong, key=lambda d: RANK[d["computed"]])
            agree[(k["severity"], best["computed"])] += 1
            crit_found += expected_crit and best["computed"] == "critical"
        else:
            misses.append(
                {
                    "item": item,
                    "class": k["class"],
                    "expected": k["severity"],
                    "seen": [d["computed"] for d in hits],
                    "other_on_item": [d["computed"] for d in cleaned.get(item, [])],
                }
            )
        a_any += bool(hits)
        if expected_crit and not (strong and max(RANK[d["computed"]] for d in strong) == 3):
            crit_missed.append({"item": item, "seen": [d["computed"] for d in hits]})

    false_major = []
    for item, k in key.items():
        if k["part"] in ("B", "C"):
            for d in cleaned.get(item, []):
                if RANK[d["computed"]] >= 2:
                    false_major.append(
                        {
                            "item": item,
                            "part": k["part"],
                            "severity": d["computed"],
                            "ru": (d.get("ru") or "")[:80],
                            "basis": (d.get("basis") or "")[:140],
                        }
                    )
    clean_items = sum(k["part"] in ("B", "C") for k in key.values())
    nits = Counter(
        d["computed"]
        for item, k in key.items()
        if k["part"] in ("B", "C")
        for d in cleaned.get(item, [])
        if RANK[d["computed"]] < 2
    )
    gates = {
        "recall_major_plus": a_found >= 36,
        "each_class_60pct": all(f / t >= 0.6 for f, t in by_class.values()),
        "false_major": len({f["item"] for f in false_major}) <= 2,
        "critical_all_reviewed": not crit_missed,
    }
    return {
        "missing_items": missing,
        "recall_major_plus": {"found": a_found, "of": 40, "wilson95": wilson(a_found, 40)},
        "detected_at_any_severity": a_any,
        "by_class": {k: v for k, v in sorted(by_class.items())},
        "critical": {"found_as_critical": crit_found, "of": crit_total, "not_found": crit_missed},
        "false_major_items": len({f["item"] for f in false_major}),
        "false_major_of": clean_items,
        "false_major_wilson95": wilson(len({f["item"] for f in false_major}), clean_items),
        "false_majors": false_major,
        "minor_or_suggestion_on_clean": dict(nits),
        "severity_agreement": {f"{a}->{b}": n for (a, b), n in sorted(agree.items())},
        "invalid_dropped": invalid,
        "stated_differs_from_tree": stated_diff,
        "misses": misses,
        "gates_provisional": gates,
        "qualified_provisional": all(gates.values()) and not missing,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("key", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = score(args.run_dir, args.key)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
