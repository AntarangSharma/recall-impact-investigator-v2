from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from recall.criteria import (
    AFFECTED,
    EXCLUDED,
    NEEDS,
    Criteria,
    Extraction,
    Finding,
    ProposedGroup,
    Rule,
    Span,
    authority,
    match,
    rule_key,
    validate_proposal,
)
from recall.inventory import FORBIDDEN, Stock, identifier
from recall.model import CALL_LIMIT, BudgetError, Model, ModelError, scripted
from recall.sources import NOTICES, ROOT, Snapshot, SourceError, load_fixture
from recall.workflow import model_budget, pull

LABELS = (AFFECTED, EXCLUDED, NEEDS)
GOLD = ROOT / "eval" / "gold_rules.json"
SCENARIOS = ROOT / "eval" / "scenarios.csv"


def cases() -> list[dict[str, str]]:
    with SCENARIOS.open(newline="") as handle:
        return list(csv.DictReader(handle))


def gold() -> dict[str, Any]:
    records: dict[str, Any] = json.loads(GOLD.read_text())
    return records


def stock(case: dict[str, str]) -> Stock:
    return Stock(
        stock_id=case["stock_id"],
        brand=case["brand"],
        model=case["model"],
        location="Synthetic",
        date_code=None if case["date_code"] == "<missing>" else case["date_code"],
        serial_number=case["serial_number"],
    )


def fixed_criteria(snapshot: Snapshot, entry: dict[str, Any]) -> Criteria:
    # Gold defines the rule fields. The scripted excerpts only supply locatable evidence.
    quotes = scripted(snapshot).groups
    rules = []
    for group, example in zip(entry["groups"], quotes, strict=True):
        start = snapshot.text.index(example.quote)
        span = Span(start=start, end=start + len(example.quote), quote=example.quote)
        rules.append(Rule.model_validate(dict(group, evidence=span)))
    return Criteria(rules=rules)


def baseline(unit: Stock, snapshot: Snapshot | None) -> Finding:
    if snapshot is None:
        return Finding(status=EXCLUDED, reason="no_keyword_hit")
    kind, model = identifier(unit.model)
    description = snapshot.text.splitlines()[1]
    tokens = set(re.findall(r"[A-Z0-9]+(?:[./_-][A-Z0-9]+)*", description.upper()))
    if (
        kind == "valid"
        and model in tokens
        and unit.brand.strip().casefold() == NOTICES[snapshot.notice_id].brand.casefold()
    ):
        return Finding(status=AFFECTED, reason="exact_keyword_hit")
    return Finding(status=EXCLUDED, reason="no_keyword_hit")


def group_fields(entry: dict[str, Any], proposed: Extraction | None) -> tuple[int, int]:
    expected = sorted(
        (ProposedGroup.model_validate(dict(group, quote="")) for group in entry["groups"]),
        key=rule_key,
    )
    received = sorted(proposed.groups, key=rule_key) if proposed else []
    fields = 4 * len(expected) + 1
    correct = int(proposed is not None and not proposed.unsupported)
    if len(received) == len(expected):
        correct += sum(
            actual_field == expected_field
            for actual, target in zip(received, expected, strict=True)
            for actual_field, expected_field in zip(rule_key(actual), rule_key(target), strict=True)
        )
    return correct, fields


def metrics(
    selected: list[dict[str, str]],
    predictions: dict[str, Finding],
    snapshots: dict[str, Snapshot],
    annotations: dict[str, Any],
) -> dict[str, Any]:
    confusion: dict[str, Counter[str]] = {
        label: Counter(dict.fromkeys(LABELS, 0)) for label in LABELS
    }
    valid_citations = expected_citations = correct_requests = requested = expected_requests = 0
    false_models = predicted_affected = correct = 0
    failures = []
    for case in selected:
        finding = predictions[case["stock_id"]]
        confusion[case["expected"]][finding.status] += 1
        correct += finding.status == case["expected"]
        if finding.status != case["expected"]:
            failures.append(case["stock_id"])
        if case["evidence"]:
            expected_citations += 1
            snap = snapshots.get(case["notice"])
            if (
                snap
                and finding.evidence
                and all(span.validates(snap) for span in finding.evidence)
                and any(case["evidence"] in span.quote for span in finding.evidence)
            ):
                valid_citations += 1
        expected_requests += bool(case["request"])
        requested += bool(finding.field)
        correct_requests += bool(case["request"] and finding.field == case["request"])
        if finding.status == AFFECTED:
            predicted_affected += 1
            models = {
                model
                for group in annotations[case["notice"]]["groups"]
                for model in group["models"] + group["aliases"]
            }
            false_models += identifier(case["model"])[1] not in models
    true_affected = confusion[AFFECTED][AFFECTED]
    return {
        "cases": len(selected),
        "correct": correct,
        "confusion": confusion,
        "affected_recall": [true_affected, sum(confusion[AFFECTED].values())],
        "affected_precision": [true_affected, predicted_affected],
        "incorrect_exclusions": [
            confusion[AFFECTED][EXCLUDED] + confusion[NEEDS][EXCLUDED],
            sum(confusion[label][EXCLUDED] for label in LABELS),
        ],
        "false_model_matches": [false_models, predicted_affected],
        "valid_case_citations": [valid_citations, expected_citations],
        "correct_requests": [correct_requests, expected_requests],
        "incorrect_or_unneeded_requests": [requested - correct_requests, requested],
        "misclassified_case_ids": failures,
    }


def run(live: bool) -> dict[str, Any]:
    annotations = gold()
    snapshots: dict[str, Snapshot] = {}
    issues: dict[str, str] = {}
    notes: dict[str, str] = {}
    booked = calls = 0
    cap = model_budget() if live else 0.0

    def reserve(micros: int) -> None:
        nonlocal booked, calls
        if calls == CALL_LIMIT or booked + micros > cap * 1_000_000:
            raise BudgetError("Evaluation reached the configured model budget.")
        booked += micros
        calls += 1

    if live and not os.getenv("OPENAI_API_KEY"):
        raise SystemExit(
            "Live extraction unavailable: OPENAI_API_KEY is unset. No model calls were made."
        )
    model = Model(reserve) if live else None
    all_fixtures = {notice_id: load_fixture(notice_id) for notice_id in NOTICES}
    candidates = asyncio.run(pull(list(NOTICES))) if live else list(all_fixtures.values())
    extracted: dict[str, Criteria] = {}
    extraction_scores: dict[str, Any] = {}
    fixed = {
        notice_id: fixed_criteria(snap, annotations[notice_id])
        for notice_id, snap in all_fixtures.items()
    }
    for notice_id, value in zip(NOTICES, candidates, strict=True):
        proposed = None
        if isinstance(value, BaseException):
            issues[notice_id] = "incomplete_source"
            notes[notice_id] = str(value)
        else:
            snapshots[notice_id] = value
            try:
                # Score the raw output even when runtime validation subsequently rejects it.
                proposed = model.extract(value) if model else scripted(value)
                extracted[notice_id] = validate_proposal(
                    value, proposed, authority(value, NOTICES[notice_id].brand)
                )
            except BudgetError as exc:
                issues[notice_id], notes[notice_id] = "budget_exhausted", str(exc)
            except (ModelError, SourceError) as exc:
                issues[notice_id], notes[notice_id] = "extraction_failed", str(exc)
        score = group_fields(annotations[notice_id], proposed)
        extraction_scores[notice_id] = {
            "split": annotations[notice_id]["split"],
            "correct_supported_fields": score,
            "validated": notice_id in extracted,
            "problem": notes.get(notice_id, ""),
        }

    selected = cases()
    predictions: dict[str, dict[str, Finding]] = {
        "keyword_baseline": {},
        "fixed_criteria": {},
        "end_to_end": {},
    }
    for case in selected:
        notice_id = case["notice"]
        unit = stock(case)
        condition = case["source_condition"]
        brand = NOTICES[notice_id].brand
        predictions["keyword_baseline"][unit.stock_id] = baseline(
            unit,
            None if condition == "incomplete" else all_fixtures[notice_id],
        )
        predictions["fixed_criteria"][unit.stock_id] = match(
            unit,
            brand,
            None if condition else fixed[notice_id],
            condition,
        )
        predictions["end_to_end"][unit.stock_id] = match(
            unit,
            brand,
            None if condition else extracted.get(notice_id),
            condition or issues.get(notice_id, ""),
        )
    holdout = [case for case in selected if annotations[case["notice"]]["split"] == "holdout"]
    correct_fields = sum(
        score["correct_supported_fields"][0] for score in extraction_scores.values()
    )
    total_fields = sum(score["correct_supported_fields"][1] for score in extraction_scores.values())
    return {
        "mode": "live" if live else "scripted fixture",
        "model_calls": calls,
        "reserved_usd": booked / 1_000_000,
        "extraction": {"overall": [correct_fields, total_fields], "per_notice": extraction_scores},
        "all": {
            name: metrics(
                selected, findings, snapshots if name == "end_to_end" else all_fixtures, annotations
            )
            for name, findings in predictions.items()
        },
        "holdout": {
            name: metrics(
                holdout, findings, snapshots if name == "end_to_end" else all_fixtures, annotations
            )
            for name, findings in predictions.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.live)
    body = json.dumps(result, indent=2) + "\n"
    if FORBIDDEN in body:
        raise SystemExit("Evaluation report contains unsupported display punctuation.")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(body)
    print(body, end="")
    summary = result["all"]
    if (
        summary["fixed_criteria"]["correct"] != 60
        or summary["end_to_end"]["incorrect_exclusions"][0]
    ):
        raise SystemExit(1)
    if not args.live and summary["end_to_end"]["correct"] != 60:
        raise SystemExit(1)
    if args.live:
        correct, total = result["extraction"]["overall"]
        if correct / total < 0.9 or not all(
            item["validated"] for item in result["extraction"]["per_notice"].values()
        ):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
