from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from recall.inventory import FORBIDDEN, Stock, identifier
from recall.sources import Snapshot, SourceError, load_fixture, sha

AFFECTED: Literal["affected_by_notice"] = "affected_by_notice"
EXCLUDED: Literal["excluded_by_notice"] = "excluded_by_notice"
NEEDS: Literal["needs_information"] = "needs_information"
Status = Literal["affected_by_notice", "excluded_by_notice", "needs_information"]
Field = Literal["model", "date_code", "serial_number"]
TOKEN = r"[A-Z0-9]+(?:[./_-][A-Z0-9]+)*"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SerialRange(StrictModel):
    lower: str
    upper: str


class ProposedGroup(StrictModel):
    models: list[str]
    aliases: list[str]
    date_codes: list[str]
    serial: SerialRange | None
    quote: str


class Extraction(StrictModel):
    groups: list[ProposedGroup]
    unsupported: list[str]


class Span(StrictModel):
    start: int
    end: int
    quote: str

    def validates(self, snapshot: Snapshot) -> bool:
        return (
            0 <= self.start < self.end <= len(snapshot.text)
            and snapshot.text[self.start : self.end] == self.quote
        )


class Rule(StrictModel):
    models: list[str]
    aliases: list[str]
    date_codes: list[str]
    serial: SerialRange | None
    evidence: Span


class Criteria(StrictModel):
    rules: list[Rule]


class Finding(StrictModel):
    status: Status
    reason: str
    field: Field | None = None
    evidence: list[Span] = []
    explanation: str = ""


@dataclass(frozen=True)
class SourceRules:
    groups: list[ProposedGroup]


def authority(snapshot: Snapshot, brand: str) -> SourceRules:
    if snapshot.source_hash != sha(snapshot.text.encode()):
        raise SourceError("The stored text did not match the source hash.")
    if snapshot.source_hash != load_fixture(snapshot.notice_id).source_hash:
        raise SourceError("The notice changed. Its new snapshot requires a source review.")
    text = snapshot.text.splitlines()[1].removeprefix("Description: ")
    sentence = text.partition(". ")[0].rstrip(".")
    prefix = {
        "Secura": "This recall involves Secura air fryers with ",
        "Cosori": "This recall involves Cosori air fryers with ",
        "Insignia": "This recall involves Insignia Air Fryers and Insignia Air Fryer Ovens, ",
    }[brand]
    if not sentence.startswith(prefix):
        raise SourceError("The description uses unsupported scope wording.")
    scope = sentence.removeprefix(prefix)
    date_groups = scope.split(", and model number ")
    date_groups = [date_groups[0].removeprefix("model number "), *date_groups[1:]]
    date_pattern = rf"({TOKEN}) \(({TOKEN})\) with date codes? ([0-9]+(?: and [0-9]+)*)"
    matches = [re.fullmatch(date_pattern, clause) for clause in date_groups]
    if all(matches):
        return SourceRules(
            [
                ProposedGroup(
                    models=[match[1]],
                    aliases=[match[2]],
                    date_codes=match[3].split(" and "),
                    serial=None,
                    quote=match[0],
                )
                for match in matches
                if match is not None
            ]
        )
    if scope.startswith("model numbers "):
        model_list = re.sub(r",\s+and\s+", ", ", scope.removeprefix("model numbers "))
        models = model_list.split(", ")
        if models and all(re.fullmatch(TOKEN, model) for model in models):
            return SourceRules(
                [ProposedGroup(models=models, aliases=[], date_codes=[], serial=None, quote=scope)]
            )
    raise SourceError("The description contains unsupported matching criteria.")


def rule_key(group: Rule | ProposedGroup) -> tuple[tuple[str, ...], ...]:
    return (
        tuple(sorted(group.models)),
        tuple(sorted(group.aliases)),
        tuple(sorted(group.date_codes)),
        (group.serial.lower, group.serial.upper) if group.serial else (),
    )


def serial_parts(value: str) -> tuple[str, str] | None:
    match = re.fullmatch(r"([A-Z][A-Z0-9]*[-_/])?([0-9]+)", value)
    return (match[1] or "", match[2]) if match else None


def validate_proposal(snapshot: Snapshot, extraction: Extraction, source: SourceRules) -> Criteria:
    approved = {rule_key(group): group for group in source.groups}
    keys = [rule_key(group) for group in extraction.groups]
    if extraction.unsupported or sorted(keys) != sorted(approved):
        raise SourceError("Extracted criteria disagree with the supported notice conditions.")
    rules: list[Rule] = []
    for group in extraction.groups:
        key = rule_key(group)
        quote = group.quote
        approved_quote = approved[key].quote
        start = snapshot.text.find(quote)
        if (
            not quote
            or len(quote) > 600
            or FORBIDDEN in quote
            or approved_quote not in quote
            or start < 0
            or snapshot.text.find(quote, start + 1) >= 0
        ):
            raise SourceError("An extraction quote did not identify a unique supporting span.")
        span = Span(start=start, end=start + len(quote), quote=quote)
        if not span.validates(snapshot):
            raise SourceError("An extraction span did not match the stored notice.")
        if group.serial:
            lower = serial_parts(group.serial.lower)
            upper = serial_parts(group.serial.upper)
            if (
                not lower
                or not upper
                or lower[0] != upper[0]
                or len(lower[1]) != len(upper[1])
                or lower[1] > upper[1]
            ):
                raise SourceError(
                    "The serial range cannot be evaluated with a fixed prefix and width."
                )
        # Use source order to keep deterministic rendering and evaluation.
        rules.append(
            Rule(
                **approved[key].model_dump(exclude={"quote", "serial"}),
                serial=group.serial,
                evidence=span,
            )
        )
    return Criteria(rules=sorted(rules, key=lambda rule: rule.evidence.start))


def test_group(stock: Stock, rule: Rule) -> tuple[bool, Field | None, str]:
    if rule.date_codes:
        kind, date = identifier(stock.date_code)
        widths = {len(code) for code in rule.date_codes}
        if kind == "valid" and (len(date) not in widths or not date.isdigit()):
            kind = "malformed"
        if kind != "valid":
            return False, "date_code", kind
    else:
        date = ""
    serial_matches = True
    if rule.serial:
        kind, serial = identifier(stock.serial_number)
        bound = serial_parts(rule.serial.lower)
        candidate = serial_parts(serial)
        if kind != "valid":
            return False, "serial_number", kind
        if (
            not bound
            or not candidate
            or bound[0] != candidate[0]
            or len(bound[1]) != len(candidate[1])
        ):
            return False, "serial_number", "unsupported_format"
        serial_matches = rule.serial.lower <= serial <= rule.serial.upper
    return (not rule.date_codes or date in rule.date_codes) and serial_matches, None, ""


def match(stock: Stock, brand: str, criteria: Criteria | None, issue: str = "") -> Finding:
    if criteria is None or not criteria.rules:
        return Finding(status=NEEDS, reason=issue or "criteria_unavailable")
    all_evidence = [rule.evidence for rule in criteria.rules]
    kind, model = identifier(stock.model)
    brand_matches = stock.brand.strip().casefold() == brand.casefold()
    if kind != "valid":
        return Finding(
            status=NEEDS,
            reason=kind,
            field="model" if brand_matches else None,
            evidence=all_evidence if brand_matches else [],
        )
    candidates = [rule for rule in criteria.rules if model in rule.models or model in rule.aliases]
    evidence = [rule.evidence for rule in candidates] or all_evidence
    if not brand_matches:
        if candidates:
            return Finding(status=NEEDS, reason="brand_conflict", evidence=evidence)
        return Finding(status=NEEDS, reason="outside_brand_coverage")
    if not candidates:
        return Finding(status=EXCLUDED, reason="model_outside_list", evidence=evidence)
    results = [test_group(stock, rule) for rule in candidates]
    for rule, (matched, _, _) in zip(candidates, results, strict=True):
        if matched:
            return Finding(status=AFFECTED, reason="criteria_matched", evidence=[rule.evidence])
    for _, field, reason in results:
        if field:
            return Finding(status=NEEDS, reason=reason, field=field, evidence=evidence)
    return Finding(status=EXCLUDED, reason="identifier_outside_set", evidence=evidence)
