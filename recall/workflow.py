from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from difflib import SequenceMatcher
from typing import Any, Literal, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from recall.criteria import AFFECTED, NEEDS, Criteria, Finding, authority, match, validate_proposal
from recall.inventory import identifier
from recall.model import BudgetError, Model, ModelError, explanation_options, scripted
from recall.sources import NOTICES, Snapshot, SourceError, load_fixture, retrieve
from recall.store import Store


class State(TypedDict, total=False):
    key: str
    stock_id: str | None
    field: str | None
    request_id: str | None
    question: str


PICKUP_HELP = {
    "date_code": "Read the four-digit code from the label on the underside.",
    "model": "Read the full model from the product label, including punctuation and suffixes.",
    "serial_number": "Read the full serial from the product label, including leading zeros.",
}


def checked_answer(field: str, raw: str) -> str:
    kind, canonical = identifier(raw)
    if kind != "valid" or (
        field == "date_code" and (len(canonical) != 4 or not canonical.isdigit())
    ):
        raise ValueError(f"Enter a valid {field.replace('_', ' ')} as printed on the unit.")
    return canonical


def model_budget() -> float:
    budget = float(os.getenv("OPENAI_MAX_ESTIMATED_USD", "0.12"))
    if not 0 < budget <= 1:
        raise ValueError("OPENAI_MAX_ESTIMATED_USD must be greater than zero and at most 1.")
    return budget


async def pull(notices: list[str]) -> list[Snapshot | BaseException]:
    return await asyncio.gather(
        *(retrieve(NOTICES[notice]) for notice in notices), return_exceptions=True
    )


class Workflow:
    def __init__(self, store: Store) -> None:
        self.store = store

    @contextmanager
    def graph(self, key: str, mode: str) -> Iterator[CompiledStateGraph[State, Any, State, State]]:
        with SqliteSaver.from_conn_string(str(self.store.checkpoints)) as saver:
            work = Action(self.store, key, mode)
            graph = StateGraph(State)
            graph.add_node("search_notices", work.search_notices)
            graph.add_node("read_notice", work.read_notice)
            graph.add_node("lookup_inventory", work.lookup_inventory)
            graph.add_node("draft_inquiry", work.draft_inquiry)
            graph.add_node("request_identifier", work.request_identifier)
            graph.add_node("wait_for_identifier", work.wait_for_identifier)
            graph.add_node("finish", work.finish)
            graph.add_edge(START, "search_notices")
            graph.add_edge("search_notices", "read_notice")
            graph.add_edge("read_notice", "lookup_inventory")
            graph.add_edge("lookup_inventory", "draft_inquiry")
            graph.add_conditional_edges("draft_inquiry", work.next_step)
            graph.add_edge("request_identifier", "wait_for_identifier")
            graph.add_edge("wait_for_identifier", "lookup_inventory")
            graph.add_edge("finish", END)
            yield graph.compile(checkpointer=saver)

    def advance(self, key: str, request_id: str | None = None, answer: str = "") -> None:
        with self.store.exclusive_run():
            investigation = self.store.investigation(key)
            if investigation is None:
                raise LookupError("Unknown investigation.")
            with self.graph(key, investigation["mode"]) as graph:
                config: RunnableConfig = {"configurable": {"thread_id": key}, "recursion_limit": 45}
                checkpoint = graph.get_state(config)
                if request_id is not None:
                    # A request row can survive a crash before its interrupt was checkpointed.
                    if not checkpoint.interrupts and checkpoint.next:
                        graph.invoke(None, config, durability="sync")
                        checkpoint = graph.get_state(config)
                    request = next(
                        (row for row in self.store.requests(key) if row["id"] == request_id), None
                    )
                    if request is None:
                        raise LookupError("Unknown identifier request.")
                    self.store.answer(key, request_id, checked_answer(request["field"], answer))
                if checkpoint.interrupts:
                    waiting_id = cast(dict[str, str], checkpoint.interrupts[0].value)["request_id"]
                    pending = next(
                        row for row in self.store.requests(key) if row["id"] == waiting_id
                    )
                    if pending["answer"] is not None:
                        graph.invoke(Command(resume=waiting_id), config, durability="sync")
                elif checkpoint.next:
                    graph.invoke(None, config, durability="sync")
                elif not checkpoint.values:
                    graph.invoke({"key": key}, config, durability="sync")


class Action:
    def __init__(self, store: Store, key: str, mode: str) -> None:
        self.store = store
        self.key = key
        self.mode = mode
        self.model = (
            Model(lambda micros: store.reserve(key, micros, model_budget()))
            if mode == "live"
            else None
        )

    def search_notices(self, state: State) -> dict[str, Any]:
        record = self.store.investigation(self.key)
        assert record is not None
        selected = json.loads(record["notices"])
        self.store.tool(
            self.key,
            "search",
            "search_notices",
            f"Selected {len(selected)} bounded CPSC notice records.",
        )
        return {}

    def read_notice(self, state: State) -> dict[str, Any]:
        record = self.store.investigation(self.key)
        assert record is not None
        done = {row["notice_id"] for row in self.store.coverage(self.key)}
        brands = {stock.brand.casefold() for stock in self.store.inventory(self.key)}
        selected = [notice for notice in json.loads(record["notices"]) if notice not in done]
        # Similarity affects discovery order only. Every selected notice gets a coverage record.
        selected.sort(
            key=lambda notice: (
                -max(
                    (
                        SequenceMatcher(None, brand, NOTICES[notice].brand.casefold()).ratio()
                        for brand in brands
                    ),
                    default=0.0,
                )
            )
        )
        snapshots = (
            asyncio.run(pull(selected)) if self.model else [load_fixture(id_) for id_ in selected]
        )
        for notice_id, result in zip(selected, snapshots, strict=True):
            self.evaluate_notice(notice_id, result)
        return {}

    def evaluate_notice(self, notice_id: str, result: Snapshot | BaseException) -> None:
        criteria = None
        detail = ""
        issue = ""
        if isinstance(result, BaseException):
            detail = (
                str(result)
                if isinstance(result, SourceError)
                else "Unexpected CPSC retrieval failure."
            )
            self.store.refresh_error(notice_id, detail)
            snapshot = self.store.latest(notice_id) or load_fixture(notice_id)
            self.store.cache(snapshot, live=False)
            issue = "incomplete_source"
        else:
            snapshot = result
            self.store.cache(snapshot, live=self.mode == "live")
            try:
                source = authority(snapshot, NOTICES[notice_id].brand)
            except SourceError as exc:
                issue, detail = "changed_source", str(exc)
            else:
                try:
                    proposed = self.model.extract(snapshot) if self.model else scripted(snapshot)
                    criteria = validate_proposal(snapshot, proposed, source)
                except BudgetError as exc:
                    issue, detail = "budget_exhausted", str(exc)
                except ValueError as exc:
                    issue, detail = "extraction_failed", str(exc)
        self.store.record_coverage(self.key, notice_id, snapshot, criteria, issue, detail)
        summary = issue.replace("_", " ") if issue else "validated source criteria"
        self.store.tool(self.key, f"read:{notice_id}", "read_notice", f"{notice_id}: {summary}.")

    def lookup_inventory(self, state: State) -> dict[str, Any]:
        stock_id = state.get("stock_id")
        refresh_only = state.get("field")
        invalidated = (
            {
                row["notice_id"]
                for row in self.store.findings(self.key, stock_id)
                if row["invalidated_at"]
            }
            if stock_id
            else set()
        )
        coverage = self.store.coverage(self.key)
        rows: list[tuple[str, str, str | None, Finding]] = []
        for stock in self.store.inventory(self.key, stock_id):
            for source in coverage:
                notice_id = source["notice_id"]
                if notice_id in invalidated:
                    continue
                criteria = (
                    Criteria.model_validate_json(source["criteria"]) if source["criteria"] else None
                )
                brand = NOTICES[notice_id].brand
                if refresh_only and refresh_only != "model":
                    model = identifier(stock.model)[1]
                    if (
                        not criteria
                        or stock.brand.casefold() != brand.casefold()
                        or not any(
                            (model in rule.models or model in rule.aliases)
                            and bool(
                                rule.date_codes if refresh_only == "date_code" else rule.serial
                            )
                            for rule in criteria.rules
                        )
                    ):
                        continue
                result = match(stock, brand, criteria, source["issue"])
                rows.append((stock.stock_id, notice_id, source["hash"], result))
        self.explain(rows, state.get("request_id"))
        self.store.save_findings(self.key, rows)
        event = state.get("request_id") or "initial"
        self.store.tool(
            self.key,
            f"lookup:{event}",
            "lookup_inventory",
            f"Evaluated {len(rows)} unit-notice pairs.",
        )
        return {}

    def explain(self, rows: list[tuple[str, str, str | None, Finding]], event: str | None) -> None:
        options = [explanation_options(row[3]) for row in rows]
        unique = list(dict.fromkeys(tuple(group) for group in options))
        chosen = [group[0] for group in unique]
        if self.model and unique:
            try:
                chosen = self.model.explain([list(group) for group in unique])
            except (ModelError, ValueError) as exc:
                self.store.tool(
                    self.key,
                    f"copy:{event or 'initial'}",
                    "lookup_inventory",
                    f"Used fixed finding wording. {exc}",
                )
        by_options = dict(zip(unique, chosen, strict=True))
        for row, group in zip(rows, options, strict=True):
            row[3].explanation = by_options[tuple(group)]

    def draft_inquiry(self, state: State) -> dict[str, Any]:
        drafts = 0
        for stock in self.store.inventory(self.key, state.get("stock_id")):
            issues = []
            for row in self.store.findings(self.key, stock.stock_id):
                finding = Finding.model_validate_json(row["payload"])
                notice_id = row["notice_id"]
                if row["invalidated_at"]:
                    continue
                if finding.status == AFFECTED:
                    issues.append(
                        f"CPSC {notice_id}: please confirm the supplier's handling instructions."
                    )
                elif finding.field:
                    issues.append(
                        f"CPSC {notice_id}: please verify the unit's {finding.field.replace('_', ' ')}."
                    )
                elif finding.status == NEEDS and (
                    finding.reason == "brand_conflict"
                    or NOTICES[notice_id].brand.casefold() == stock.brand.casefold()
                ):
                    issues.append(
                        f"CPSC {notice_id}: please verify the product identifiers and recall scope."
                    )
            body = None
            if issues:
                model = stock.model if stock.model else "unrecorded"
                body = (
                    f"Subject: Recall information for stock unit {stock.stock_id}\n\n"
                    f"Hello,\n\nCould you help us review {stock.brand} model {model}?\n"
                    + "\n".join(issues)
                    + "\n\nThank you."
                )
                drafts += 1
            self.store.draft(self.key, stock.stock_id, body)
        if drafts:
            event = state.get("request_id") or "initial"
            self.store.tool(
                self.key,
                f"draft:{event}",
                "draft_inquiry",
                f"Saved {drafts} supplier inquiry drafts.",
            )
        return {}

    def candidate(self) -> tuple[sqlite3.Row, Finding] | None:
        handled = {
            (row["stock_id"], row["field"])
            for row in self.store.requests(self.key)
            if row["answer"] is not None
        }
        for row in self.store.findings(self.key):
            finding = Finding.model_validate_json(row["payload"])
            if (
                not row["invalidated_at"]
                and finding.field
                and (row["stock_id"], finding.field) not in handled
            ):
                return row, finding
        return None

    def next_step(self, state: State) -> Literal["request_identifier", "finish"]:
        return "request_identifier" if self.candidate() is not None else "finish"

    def request_identifier(self, state: State) -> State:
        candidate = self.candidate()
        assert candidate is not None
        row, finding = candidate
        field = finding.field
        assert field is not None
        stock_id = row["stock_id"]
        question = f"What is the {field.replace('_', ' ')} for {stock_id}? {PICKUP_HELP[field]}"
        request = self.store.request(self.key, stock_id, field, question)
        self.store.state(self.key, "waiting")
        self.store.tool(
            self.key,
            f"request:{request['id']}",
            "request_identifier",
            f"Asked for {field} for stock {stock_id}.",
        )
        return {
            "stock_id": stock_id,
            "field": field,
            "request_id": request["id"],
            "question": question,
        }

    def wait_for_identifier(self, state: State) -> State:
        resumed = interrupt({"request_id": state["request_id"], "question": state["question"]})
        if resumed != state["request_id"]:
            raise ValueError("The response does not match the checkpoint request.")
        return {}

    def finish(self, state: State) -> State:
        self.store.state(self.key, "complete")
        return {}
