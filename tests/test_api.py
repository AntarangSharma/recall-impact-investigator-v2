import csv
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from recall.app import create_app
from recall.criteria import AFFECTED, EXCLUDED, NEEDS
from recall.model import BudgetError, Model, ModelError, scripted
from recall.sources import DATA, SourceError, load_fixture, sha
from recall.store import Store

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
    from recall.workflow import Action

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
