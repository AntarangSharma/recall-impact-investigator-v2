from __future__ import annotations

import argparse
import asyncio
import csv
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import httpx
from fastapi.testclient import TestClient

from recall.app import create_app
from recall.criteria import AFFECTED, EXCLUDED, NEEDS
from recall.inventory import parse_inventory
from recall.sources import DATA, NOTICES, Snapshot, load_fixture
from recall.store import Store
from recall.workflow import Workflow, pull


def refresh(directory: Path) -> None:
    store = Store(directory)
    failed = False
    with store.exclusive_run():
        for notice_id, result in zip(NOTICES, asyncio.run(pull(list(NOTICES))), strict=True):
            if not isinstance(result, Snapshot):
                failed = True
                store.refresh_error(notice_id, str(result))
                print(f"{notice_id} retrieval failed: {result}")
                continue
            previous = store.latest(notice_id)
            store.cache(result, live=True)
            approved = result.source_hash == load_fixture(notice_id).source_hash
            changed = previous is not None and previous.source_hash != result.source_hash
            label = "reviewed hash" if approved else "changed content requires review"
            version = ", previous live findings invalidated" if changed else ""
            print(
                f"{notice_id} {result.retrieved_at.isoformat()} {result.source_hash} ({label}{version})"
            )
    if failed:
        raise SystemExit(1)


def live_smoke() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit(
            "Live clarification unavailable: OPENAI_API_KEY is unset. No model calls were made."
        )
    raw = (DATA / "demo.csv").read_bytes()
    with TemporaryDirectory() as path:
        store = Store(Path(path))
        key = store.create(raw, parse_inventory(raw), ["23-794"], "live", "live-smoke")
        Workflow(store).advance(key)
        route = f"/api/investigations/{key}"
        with TestClient(create_app(Path(path))) as client:
            before = client.get(route).json()
            assert not before["sources"][0]["issue"], before["sources"][0]["detail"]
            assert before["totals"] == {AFFECTED: 1, EXCLUDED: 1, NEEDS: 1}, before
            request = before["requests"][0]
            assert request["stock_id"] == "DEMO-003" and request["field"] == "date_code"
            response = client.post(f"{route}/answers/{request['id']}", data={"value": "1901"})
            response.raise_for_status()
            after = response.json()
            assert after["totals"] == {AFFECTED: 2, EXCLUDED: 1, NEEDS: 0}
            assert any(
                x["field"] == "date_code" and x["answer"] == "1901" for x in after["requests"]
            )
            with store.connection() as db:
                calls, reserved = db.execute(
                    "SELECT count(*), sum(micros) FROM model_usage"
                ).fetchone()
            copy_fallbacks = sum(
                "Used fixed finding wording" in event["summary"] for event in after["events"]
            )
            print(
                f"Live Secura run resumed after restart. Model calls: {calls}. Estimated reservation: ${reserved / 1_000_000:.6f}. Explanation fallbacks: {copy_fallbacks}."
            )
            if copy_fallbacks:
                raise SystemExit(1)


def http_fixture(base: str) -> None:
    parsed = urlsplit(base)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.scheme != "http":
        raise SystemExit("Use the fixture smoke command with a loopback server.")
    raw = (DATA / "demo.csv").read_bytes()
    with httpx.Client(base_url=base, timeout=15) as client:
        assert client.get("/health").json() == {"status": "ok"}
        response = client.post(
            "/api/investigations",
            files={"file": ("synthetic.csv", raw)},
            data={"scope": "23-794", "token": os.urandom(8).hex()},
        )
        response.raise_for_status()
        investigation = response.json()
        assert investigation["totals"] == {AFFECTED: 1, EXCLUDED: 1, NEEDS: 1}
        path = f"/api/investigations/{investigation['id']}"
        request = investigation["requests"][0]
        after = client.post(f"{path}/answers/{request['id']}", data={"value": "1901"})
        after.raise_for_status()
        assert after.json()["totals"] == {AFFECTED: 2, EXCLUDED: 1, NEEDS: 0}
        export = client.get(path.removeprefix("/api") + "/export.csv")
        export.raise_for_status()
        lines = list(csv.DictReader(io.StringIO(export.text)))
        assert len(lines) == 3 and any(
            row["stock_id"] == "DEMO-003" and row["finding"] == AFFECTED for row in lines
        )
    print("Health, fixture upload, clarification, resume and CSV export passed over HTTP.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["refresh", "live-smoke", "fixture-http"])
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    if args.action == "refresh":
        refresh(Path(os.getenv("RECALL_DATA_DIR", ".runtime")))
    elif args.action == "live-smoke":
        live_smoke()
    else:
        http_fixture(args.url)


if __name__ == "__main__":
    main()
