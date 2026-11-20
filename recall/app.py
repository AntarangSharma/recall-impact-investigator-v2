from __future__ import annotations

import csv
import io
import json
import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from recall.criteria import AFFECTED, EXCLUDED, NEEDS, Finding
from recall.inventory import MAX_BYTES, export_cell, parse_inventory
from recall.sources import DATA, NOTICES, Snapshot
from recall.store import Store
from recall.workflow import Workflow

Mode = Literal["fixture", "live"]
PACKAGE = Path(__file__).resolve().parent

STATUS_LABEL = {
    AFFECTED: "Affected",
    EXCLUDED: "Excluded by notice",
    NEEDS: "Needs information",
}
REASON_LABEL = {
    "criteria_matched": "Listed identifiers matched",
    "model_outside_list": "Model outside the complete list",
    "identifier_outside_set": "Identifier outside the stated conditions",
    "missing": "Identifier missing",
    "empty": "Identifier blank",
    "malformed": "Identifier malformed",
    "unsupported_format": "Unsupported identifier format",
    "incomplete_source": "Source incomplete",
    "outside_brand_coverage": "Outside brand coverage",
    "ambiguous_model": "Enter a single model",
    "brand_conflict": "Conflicting brand and model",
    "changed_source": "Snapshot superseded or pending review",
    "extraction_failed": "Extraction could not be validated",
    "budget_exhausted": "Model budget exhausted",
}


def review(store: Store, key: str) -> dict[str, Any]:
    record = store.investigation(key)
    if record is None:
        raise HTTPException(404, "Investigation not found.")
    source_by_id = {}
    for row in store.coverage(key):
        notice_id = row["notice_id"]
        snap = Snapshot.model_validate_json(row["snapshot"]) if row["snapshot"] else None
        when = datetime.fromisoformat(row["observed_at"]) if row["observed_at"] else None
        if snap and when:
            snap = snap.model_copy(update={"retrieved_at": when})
        source_by_id[notice_id] = {
            "id": notice_id,
            "brand": NOTICES[notice_id].brand,
            "url": NOTICES[notice_id].url,
            "hash": row["hash"],
            "retrieved_at": row["observed_at"],
            "date": when.strftime("%b %d, %Y UTC") if when else "",
            "stale": snap.stale if snap else False,
            "issue": "changed_source" if row["superseded"] else row["issue"],
            "detail": row["detail"],
        }
    by_stock: dict[str, list[dict[str, Any]]] = {}
    invalidated: set[str] = set()
    active_fields: set[tuple[str, str]] = set()
    for row in store.findings(key):
        finding = Finding.model_validate_json(row["payload"])
        if row["invalidated_at"]:
            invalidated.add(row["stock_id"])
            finding = finding.model_copy(
                update={
                    "status": NEEDS,
                    "reason": "changed_source",
                    "field": None,
                    "explanation": "This evidence is from a superseded snapshot. Start a new review for current coverage.",
                }
            )
        if finding.field:
            active_fields.add((row["stock_id"], finding.field))
        by_stock.setdefault(row["stock_id"], []).append(
            {
                **finding.model_dump(),
                "notice": source_by_id[row["notice_id"]],
                "invalidated": bool(row["invalidated_at"]),
            }
        )
    units = []
    totals = {AFFECTED: 0, NEEDS: 0, EXCLUDED: 0}
    for unit in store.inventory(key):
        findings = by_stock.get(unit.stock_id, [])
        relevant = [f["status"] for f in findings if f["reason"] != "outside_brand_coverage"]
        status = (
            AFFECTED
            if AFFECTED in relevant
            else NEEDS
            if NEEDS in relevant or not relevant
            else EXCLUDED
        )
        totals[status] += 1
        units.append({**unit.model_dump(), "status": status, "findings": findings})
    order = {AFFECTED: 0, NEEDS: 1, EXCLUDED: 2}
    units.sort(key=lambda unit: order[unit["status"]])
    requests = store.requests(key)
    visible_requests = [
        dict(row)
        for row in requests
        if row["answer"] or (row["stock_id"], row["field"]) in active_fields
    ]
    return {
        "id": key,
        "mode": record["mode"],
        "state": record["state"],
        "errors": json.loads(record["errors"]),
        "sources": list(source_by_id.values()),
        "units": units,
        "totals": totals,
        "requests": visible_requests,
        "superseded_request": any(not row["answer"] for row in requests)
        and all(row["answer"] for row in visible_requests),
        "drafts": [dict(row) for row in store.drafts(key) if row["stock_id"] not in invalidated],
        "events": [dict(row) for row in store.events(key)],
    }


def csv_report(result: dict[str, Any]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "stock_id",
            "brand",
            "model",
            "date_code",
            "serial_number",
            "location",
            "inventory_status",
            "notice_id",
            "finding",
            "reason",
            "missing_field",
            "snapshot_retrieved_at",
            "source_sha256",
            "source_url",
            "evidence",
        ]
    )
    for unit in result["units"]:
        for finding in unit["findings"]:
            source = finding["notice"]
            writer.writerow(
                map(
                    export_cell,
                    (
                        unit["stock_id"],
                        unit["brand"],
                        unit["model"],
                        unit["date_code"],
                        unit["serial_number"],
                        unit["location"],
                        unit["status"],
                        source["id"],
                        finding["status"],
                        finding["reason"],
                        finding["field"],
                        source["retrieved_at"],
                        source["hash"],
                        source["url"],
                        " | ".join(span["quote"] for span in finding["evidence"]),
                    ),
                )
            )
    return output.getvalue()


def create_app(directory: Path) -> FastAPI:
    store = Store(directory)
    workflow = Workflow(store)
    app = FastAPI(title="Product Recall Impact Investigator")
    app.mount("/static", StaticFiles(directory=PACKAGE / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE / "templates")
    templates.env.globals.update(status_label=STATUS_LABEL, reason_label=REASON_LABEL)

    @app.middleware("http")
    async def limit_request_body(request: Request, call_next: Any) -> Response:
        if request.method == "POST":
            limit = MAX_BYTES + 8192
            declared = request.headers.get("content-length", "")
            if declared and (not declared.isdigit() or int(declared) > limit):
                return Response("Request body exceeds the upload limit.", status_code=413)
            # Read and count the actual stream before multipart parsing, including chunked uploads.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > limit:
                    return Response("Request body exceeds the upload limit.", status_code=413)
            request._body = bytes(body)
        response: Response = await call_next(request)
        return response

    def submit(raw: bytes, mode: Mode, scope: str, token: str) -> str:
        if scope != "all" and scope not in NOTICES:
            raise HTTPException(400, "Choose a supported notice scope.")
        if len(token) > 80:
            raise HTTPException(400, "The submission identifier is too long.")
        inventory = parse_inventory(raw)
        if not inventory.units:
            raise HTTPException(422, inventory.errors or ["The CSV contains no inventory rows."])
        if mode == "live" and not os.getenv("OPENAI_API_KEY"):
            raise HTTPException(
                400, "Live investigations require OPENAI_API_KEY in the app environment."
            )
        notices = list(NOTICES) if scope == "all" else [scope]
        key = store.create(raw, inventory, notices, mode, token)
        workflow.advance(key)
        return key

    def save_answer(key: str, request_id: str, value: str) -> None:
        try:
            workflow.advance(key, request_id, value)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except FileExistsError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def home_page(request: Request, errors: list[str] | None = None) -> Response:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "recent": store.recent(),
                "notices": NOTICES.values(),
                "form_token": secrets.token_hex(12),
                "errors": errors or [],
                "live_available": bool(os.getenv("OPENAI_API_KEY")),
            },
            status_code=422 if errors else 200,
        )

    def review_page(request: Request, key: str, error: str = "", resumed: bool = False) -> Response:
        return templates.TemplateResponse(
            request=request,
            name="review.html",
            context={"review": review(store, key), "error": error, "resumed": resumed},
            status_code=422 if error else 200,
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        return home_page(request)

    @app.get("/demo.csv")
    def demo_csv() -> Response:
        return Response(
            (DATA / "demo.csv").read_bytes(),
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="synthetic-secura-inventory.csv"'
            },
        )

    @app.post("/investigations")
    def upload(
        request: Request,
        scope: Annotated[str, Form()],
        mode: Annotated[Mode, Form()],
        token: Annotated[str, Form()],
        file: Annotated[UploadFile, File()],
    ) -> Response:
        try:
            key = submit(file.file.read(MAX_BYTES + 1), mode, scope, token)
        except HTTPException as exc:
            errors = exc.detail if isinstance(exc.detail, list) else [str(exc.detail)]
            return home_page(request, errors)
        return RedirectResponse(f"/investigations/{key}", status_code=303)

    @app.post("/demo")
    def demo(token: Annotated[str, Form()]) -> Response:
        key = submit((DATA / "demo.csv").read_bytes(), "fixture", "23-794", token)
        return RedirectResponse(f"/investigations/{key}", status_code=303)

    @app.get("/investigations/{key}", response_class=HTMLResponse)
    def view(request: Request, key: str, resumed: bool = False) -> Response:
        return review_page(request, key, resumed=resumed)

    @app.post("/investigations/{key}/answers/{request_id}")
    def answer_form(
        request: Request,
        key: str,
        request_id: str,
        value: Annotated[str, Form()],
    ) -> Response:
        try:
            save_answer(key, request_id, value)
        except HTTPException as exc:
            if exc.status_code == 404:
                raise
            return review_page(request, key, error=str(exc.detail))
        return RedirectResponse(f"/investigations/{key}?resumed=true", status_code=303)

    @app.get("/investigations/{key}/export.csv")
    def export(key: str) -> Response:
        return Response(
            csv_report(review(store, key)),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="recall-review-{key}.csv"'},
        )

    @app.get("/investigations/{key}/drafts.txt")
    def drafts(key: str) -> Response:
        body = "\n\n".join(draft["body"] for draft in review(store, key)["drafts"])
        return Response(
            body,
            media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="supplier-inquiries-{key}.txt"'},
        )

    @app.post("/api/investigations", status_code=201)
    def api_create(
        file: Annotated[UploadFile, File()],
        scope: Annotated[str, Form()] = "all",
        mode: Annotated[Mode, Form()] = "fixture",
        token: Annotated[str, Form()] = "",
    ) -> dict[str, Any]:
        key = submit(file.file.read(MAX_BYTES + 1), mode, scope, token)
        return review(store, key)

    @app.get("/api/investigations/{key}")
    def api_review(key: str) -> dict[str, Any]:
        return review(store, key)

    @app.post("/api/investigations/{key}/answers/{request_id}")
    def api_answer(key: str, request_id: str, value: Annotated[str, Form()]) -> dict[str, Any]:
        save_answer(key, request_id, value)
        return review(store, key)

    return app


def app_factory() -> FastAPI:
    return create_app(Path(os.getenv("RECALL_DATA_DIR", ".runtime")))
