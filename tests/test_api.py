import csv
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from recall.app import create_app
from recall.criteria import AFFECTED, EXCLUDED, NEEDS
from recall.model import BudgetError, Model, ModelError, scripted
from recall.sources import DATA, SourceError, load_fixture, sha
from recall.store import Store
from recall.workflow import Action

DEMO = (DATA / "demo.csv").read_bytes()


def start(client, raw=DEMO, **params):
    return client.post(
        "/api/investigations",
        files={"file": ("inventory.csv", raw)},
        data={"scope": "23-794", **params},
    )


def test_restart_answer_and_export(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        response = start(client)
        assert response.status_code == 201
        previous = response.json()
        assert previous["totals"] == {AFFECTED: 1, EXCLUDED: 1, NEEDS: 1}
        assert previous["state"] == "waiting"
        assert start(client).json()["id"] == previous["id"]
        request = previous["requests"][0]
        assert (request["stock_id"], request["field"]) == ("DEMO-003", "date_code")
        assert len(previous["drafts"]) == 2
    key = previous["id"]
    path = f"/api/investigations/{key}/answers/{request['id']}"
    with TestClient(create_app(tmp_path)) as client:
        assert client.post(path, data={"value": "19?"}).status_code == 422
        assert client.post(path, data={"value": "1901"}).status_code == 200
        replay = client.post(path, data={"value": "1901"}).json()
        assert replay["totals"] == {AFFECTED: 2, EXCLUDED: 1, NEEDS: 0}
        assert replay["state"] == "complete"
        assert replay["requests"][0]["answer"] == "1901"
        assert len(replay["requests"]) == 1
        assert client.post(path, data={"value": "1903"}).status_code == 409
        export = client.get(f"/investigations/{key}/export.csv")
        lines = list(csv.DictReader(io.StringIO(export.text)))
        assert len(lines) == 3
        row = next(row for row in lines if row["stock_id"] == "DEMO-003")
        assert row["date_code"] == "1901" and row["finding"] == AFFECTED
        assert "SAF-53D (TXG-DT16E)" in row["evidence"] and row["source_sha256"]
        assert (
            len([event for event in replay["events"] if event["tool"] == "request_identifier"]) == 1
        )
        assert replay["events"][-2]["summary"] == "Evaluated 1 unit-notice pairs."
        draft_response = client.get(f"/investigations/{key}/drafts.txt")
        assert "DEMO-003" in draft_response.text and "CPSC 23-794" in draft_response.text
    with Store(tmp_path).connection() as db:
        assert db.execute("SELECT count(*) FROM investigations").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM findings").fetchone()[0] == 3
        original = db.execute("SELECT payload FROM stock WHERE stock_id='DEMO-003'").fetchone()[0]
        assert '"date_code":null' in original


def test_duplicate_concurrent_uploads_are_idempotent(tmp_path):
    with TestClient(create_app(tmp_path)) as client, ThreadPoolExecutor(max_workers=3) as pool:
        identifiers = list(pool.map(lambda _: start(client).json()["id"], range(3)))
    assert len(set(identifiers)) == 1
    with Store(tmp_path).connection() as db:
        assert db.execute("SELECT count(*) FROM requests").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM findings").fetchone()[0] == 3


def test_csv_formula_neutralization_and_full_notice_coverage(tmp_path):
    raw = b'stock_id,brand,model,location,date_code\n"=1+1",Secura,SAF-53D,"@floor",1901\n'
    with TestClient(create_app(tmp_path)) as client:
        result = start(client, raw, scope="all").json()
        unit = result["units"][0]
        assert unit["status"] == AFFECTED and len(unit["findings"]) == 3
        export = client.get(f"/investigations/{result['id']}/export.csv")
        records = list(csv.DictReader(io.StringIO(export.text)))
        assert all(row["stock_id"] == "'=1+1" and row["location"] == "'@floor" for row in records)
        assert "SHA-256" not in export.text and result["sources"][0]["hash"]
        assert client.post("/api/investigations", content=b"0" * 265000).status_code == 413


def test_live_change_failure_and_cached_coverage(monkeypatch, tmp_path):
    old = load_fixture("23-794").model_copy(
        update={"retrieved_at": datetime.now(UTC) - timedelta(days=9)}
    )
    text = old.text + "\nMore conditions apply."
    changed = old.model_copy(update={"text": text, "source_hash": sha(text.encode())})
    supply = iter([old, SourceError("Network unavailable."), changed])

    async def fake_pull(notices):
        return [next(supply)]

    monkeypatch.setenv("OPENAI_API_KEY", "mock")
    monkeypatch.setattr("recall.workflow.pull", fake_pull)
    monkeypatch.setattr(Model, "extract", lambda self, snapshot: scripted(snapshot))
    monkeypatch.setattr(Model, "explain", lambda self, groups: [group[0] for group in groups])
    with TestClient(create_app(tmp_path)) as client:
        first = start(client, mode="live", token="original").json()
        assert first["sources"][0]["stale"] and first["totals"][AFFECTED] == 1
        failed = start(client, mode="live", token="failed").json()
        assert failed["sources"][0]["issue"] == "incomplete_source"
        assert failed["sources"][0]["hash"] == old.source_hash
        assert failed["totals"][NEEDS] == 3
        assert client.get(f"/api/investigations/{first['id']}").json()["totals"][AFFECTED] == 1
        latest = start(client, mode="live", token="changed").json()
        assert latest["sources"][0]["issue"] == "changed_source"
        assert latest["sources"][0]["hash"] == changed.source_hash
        assert latest["totals"][NEEDS] == 3
        archived = client.get(f"/api/investigations/{first['id']}").json()
        assert archived["totals"][NEEDS] == 3
        assert all(unit["findings"][0]["invalidated"] for unit in archived["units"])
    with Store(tmp_path).connection() as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 2
        assert (
            db.execute(
                "SELECT count(*) FROM refresh_attempts WHERE problem IS NOT NULL"
            ).fetchone()[0]
            == 1
        )


def test_model_failures_and_budget_stay_unresolved(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "mock")

    async def fake_pull(notices):
        return [load_fixture(notices[0])]

    monkeypatch.setattr("recall.workflow.pull", fake_pull)
    monkeypatch.setattr(Model, "explain", lambda self, groups: [group[0] for group in groups])
    with TestClient(create_app(tmp_path)) as client:
        for exception, issue in (
            (ModelError("The model refused this response."), "extraction_failed"),
            (ModelError("The model returned an invalid response schema."), "extraction_failed"),
            (BudgetError("Estimated spend cap."), "budget_exhausted"),
        ):

            def fail(self, snapshot, failure=exception):
                raise failure

            monkeypatch.setattr(Model, "extract", fail)
            result = start(client, mode="live", token=str(exception)).json()
            assert result["sources"][0]["issue"] == issue
            assert result["totals"] == {AFFECTED: 0, EXCLUDED: 0, NEEDS: 3}


def test_answer_recovers_request_saved_before_interrupt(tmp_path, monkeypatch):
    original = Action.wait_for_identifier

    def crash_after_request(self, state):
        raise RuntimeError("Simulated process failure before writing the interrupt.")

    monkeypatch.setattr(Action, "wait_for_identifier", crash_after_request)
    with TestClient(create_app(tmp_path), raise_server_exceptions=False) as client:
        assert start(client).status_code == 500
    store = Store(tmp_path)
    key = store.recent()[0]["id"]
    request_id = store.requests(key)[0]["id"]
    monkeypatch.setattr(Action, "wait_for_identifier", original)
    with TestClient(create_app(tmp_path)) as client:
        response = client.post(
            f"/api/investigations/{key}/answers/{request_id}",
            data={"value": "1901"},
        )
        assert response.status_code == 200
        assert response.json()["totals"] == {AFFECTED: 2, EXCLUDED: 1, NEEDS: 0}
        assert len(response.json()["requests"]) == 1


def test_changed_unrelated_notice_preserves_active_request(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "mock")

    async def fake_pull(notices):
        return [load_fixture(notice_id) for notice_id in notices]

    monkeypatch.setattr("recall.workflow.pull", fake_pull)
    monkeypatch.setattr(Model, "extract", lambda self, snapshot: scripted(snapshot))
    monkeypatch.setattr(Model, "explain", lambda self, groups: [group[0] for group in groups])
    with TestClient(create_app(tmp_path)) as client:
        result = start(client, mode="live", scope="all").json()
        request = result["requests"][0]
        cosori = load_fixture("23-130")
        revised = cosori.text + "\nUpdated description."
        Store(tmp_path).cache(
            cosori.model_copy(update={"text": revised, "source_hash": sha(revised.encode())}),
            live=True,
        )
        path = f"/api/investigations/{result['id']}"
        refreshed = client.get(path).json()
        assert not refreshed["superseded_request"]
        assert refreshed["requests"][0]["id"] == request["id"]
        answered = client.post(f"{path}/answers/{request['id']}", data={"value": "1901"}).json()
        assert answered["totals"][AFFECTED] == 2
        assert answered["requests"][0]["answer"] == "1901"


@pytest.mark.parametrize(
    ("model", "reason"),
    [(" N/A ", "missing"), ("unknown", "missing"), ("SAF-53D/TXG-DT16E", "ambiguous_model")],
)
def test_model_placeholders_and_compounds_require_clarification(tmp_path, model, reason):
    raw = f"stock_id,brand,model,location,date_code\nX,Secura,{model},floor,1901\n".encode()
    with TestClient(create_app(tmp_path)) as client:
        response = start(client, raw)
        assert response.status_code == 201
        result = response.json()
        finding = result["units"][0]["findings"][0]
        assert (finding["status"], finding["reason"]) == (NEEDS, reason)
        request = result["requests"][0]
        assert request["field"] == "model"
        path = f"/api/investigations/{result['id']}/answers/{request['id']}"
        assert client.post(path, data={"value": model}).status_code == 422
        answered = client.post(path, data={"value": "TXG-DT16E"}).json()
        assert answered["totals"] == {AFFECTED: 1, EXCLUDED: 0, NEEDS: 0}


def test_restart_cannot_publish_findings_from_superseded_coverage(tmp_path, monkeypatch):
    snapshot = load_fixture("23-794")

    async def fake_pull(notices):
        return [snapshot]

    def stop_before_matching(self, state):
        raise RuntimeError("Simulated interruption after recording source coverage.")

    lookup = Action.lookup_inventory
    monkeypatch.setenv("OPENAI_API_KEY", "mock")
    monkeypatch.setattr("recall.workflow.pull", fake_pull)
    monkeypatch.setattr(Model, "extract", lambda self, snapshot: scripted(snapshot))
    monkeypatch.setattr(Model, "explain", lambda self, groups: [group[0] for group in groups])
    monkeypatch.setattr(Action, "lookup_inventory", stop_before_matching)
    with TestClient(create_app(tmp_path), raise_server_exceptions=False) as client:
        assert start(client, mode="live").status_code == 500

    store = Store(tmp_path)
    key = store.recent()[0]["id"]
    assert store.coverage(key)[0]["hash"] == snapshot.source_hash
    assert not store.findings(key)
    text = snapshot.text + "\nAdditional models were recalled."
    store.cache(
        snapshot.model_copy(update={"text": text, "source_hash": sha(text.encode())}), live=True
    )
    monkeypatch.setattr(Action, "lookup_inventory", lookup)
    with TestClient(create_app(tmp_path)) as client:
        resumed = start(client, mode="live").json()
        assert resumed["id"] == key
        assert resumed["sources"][0]["issue"] == "changed_source"
        assert resumed["totals"] == {AFFECTED: 0, EXCLUDED: 0, NEEDS: 3}
        assert not resumed["requests"] and not resumed["drafts"]
        export = client.get(f"/investigations/{key}/export.csv")
        assert {row["finding"] for row in csv.DictReader(io.StringIO(export.text))} == {NEEDS}
    assert all(row["invalidated_at"] for row in store.findings(key))


def test_existing_findings_with_legacy_source_capture_are_retired(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        result = start(client).json()
        key = result["id"]
        assert result["totals"][EXCLUDED] == 1
    # Old snapshot payloads have no scope version field.
    with Store(tmp_path).connection() as db:
        db.execute("UPDATE snapshots SET payload=json_remove(payload, '$.scope_version')")
    with TestClient(create_app(tmp_path)) as client:
        review = client.get(f"/api/investigations/{key}").json()
        assert review["sources"][0]["issue"] == "changed_source"
        assert review["totals"] == {AFFECTED: 0, EXCLUDED: 0, NEEDS: 3}
        assert all(unit["findings"][0]["invalidated"] for unit in review["units"])
