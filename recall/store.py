from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from recall.criteria import Criteria, Finding
from recall.inventory import Inventory, Stock
from recall.model import CALL_LIMIT, DEFAULT_BUDGET, BudgetError
from recall.sources import SCOPE_VERSION, Snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS investigations (
    id TEXT PRIMARY KEY, mode TEXT NOT NULL, notices TEXT NOT NULL, errors TEXT NOT NULL,
    created_at TEXT NOT NULL, state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stock (
    investigation TEXT REFERENCES investigations(id), stock_id TEXT, payload TEXT NOT NULL,
    PRIMARY KEY (investigation, stock_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    notice_id TEXT, hash TEXT, payload TEXT NOT NULL, PRIMARY KEY (notice_id, hash)
);
CREATE TABLE IF NOT EXISTS live_heads (
    notice_id TEXT PRIMARY KEY, hash TEXT NOT NULL, observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refresh_attempts (
    id INTEGER PRIMARY KEY, notice_id TEXT, observed_at TEXT, hash TEXT, problem TEXT
);
CREATE TABLE IF NOT EXISTS coverage (
    investigation TEXT REFERENCES investigations(id), notice_id TEXT,
    hash TEXT, observed_at TEXT, criteria TEXT, issue TEXT NOT NULL, detail TEXT NOT NULL,
    PRIMARY KEY (investigation, notice_id),
    FOREIGN KEY (notice_id, hash) REFERENCES snapshots(notice_id, hash)
);
CREATE TABLE IF NOT EXISTS findings (
    investigation TEXT, stock_id TEXT, notice_id TEXT, hash TEXT, payload TEXT NOT NULL,
    invalidated_at TEXT, PRIMARY KEY (investigation, stock_id, notice_id),
    FOREIGN KEY (investigation, stock_id) REFERENCES stock(investigation, stock_id),
    FOREIGN KEY (investigation, notice_id) REFERENCES coverage(investigation, notice_id)
);
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY, investigation TEXT, stock_id TEXT, field TEXT,
    question TEXT NOT NULL, answer TEXT, answered_at TEXT,
    UNIQUE (investigation, stock_id, field),
    FOREIGN KEY (investigation, stock_id) REFERENCES stock(investigation, stock_id)
);
CREATE TABLE IF NOT EXISTS drafts (
    investigation TEXT, stock_id TEXT, body TEXT NOT NULL,
    PRIMARY KEY (investigation, stock_id),
    FOREIGN KEY (investigation, stock_id) REFERENCES stock(investigation, stock_id)
);
CREATE TABLE IF NOT EXISTS tool_events (
    investigation TEXT REFERENCES investigations(id), event_key TEXT,
    tool TEXT NOT NULL, summary TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (investigation, event_key)
);
CREATE TABLE IF NOT EXISTS model_usage (
    id INTEGER PRIMARY KEY, investigation TEXT REFERENCES investigations(id), micros INTEGER NOT NULL
);
"""

COVERAGE_QUERY = """
SELECT c.*, s.payload AS snapshot,
       ((s.payload IS NOT NULL AND
         coalesce(json_extract(s.payload, '$.scope_version'), 1) <> :scope_version)
        OR (h.hash IS NOT NULL AND h.hash IS NOT c.hash)) AS superseded
FROM coverage c JOIN investigations i ON i.id=c.investigation
LEFT JOIN snapshots s ON c.notice_id=s.notice_id AND c.hash=s.hash
LEFT JOIN live_heads h ON c.notice_id=h.notice_id AND i.mode='live'
WHERE c.investigation=:key ORDER BY c.rowid
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.app = directory / "investigations.sqlite"
        self.checkpoints = directory / "checkpoints.sqlite"
        self.runner = directory / "runner.sqlite"
        with self.connection() as db:
            db.executescript(SCHEMA)
            # Retire prior results whose source capture omitted parts of the notice.
            db.execute(
                """UPDATE findings SET invalidated_at=? WHERE invalidated_at IS NULL AND EXISTS (
                    SELECT 1 FROM snapshots s
                    WHERE s.notice_id=findings.notice_id AND s.hash=findings.hash
                      AND coalesce(json_extract(s.payload, '$.scope_version'), 1) <> ?
                )""",
                (now(), SCOPE_VERSION),
            )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.app, timeout=50)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys = ON")
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def exclusive_run(self) -> Iterator[None]:
        # Serialize graph advances across processes. SQLite releases this lock on process exit.
        db = sqlite3.connect(self.runner, timeout=55)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield
        finally:
            db.close()

    def create(
        self, raw: bytes, inventory: Inventory, notices: list[str], mode: str, submission: str
    ) -> str:
        key = hashlib.sha256(
            json.dumps([mode, notices, submission]).encode() + b"\0" + raw
        ).hexdigest()[:24]
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO investigations VALUES (?, ?, ?, ?, ?, 'running')",
                (key, mode, json.dumps(notices), json.dumps(inventory.errors), now()),
            )
            db.executemany(
                "INSERT OR IGNORE INTO stock VALUES (?, ?, ?)",
                [(key, unit.stock_id, unit.model_dump_json()) for unit in inventory.units],
            )
        return key

    def investigation(self, key: str) -> sqlite3.Row | None:
        with self.connection() as db:
            row: sqlite3.Row | None = db.execute(
                "SELECT * FROM investigations WHERE id=?", (key,)
            ).fetchone()
        return row

    def state(self, key: str, state: str) -> None:
        with self.connection() as db:
            db.execute("UPDATE investigations SET state=? WHERE id=?", (state, key))

    def recent(self) -> list[sqlite3.Row]:
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM investigations ORDER BY created_at DESC LIMIT 10"
            ).fetchall()

    def inventory(self, key: str, stock_id: str | None = None) -> list[Stock]:
        with self.connection() as db:
            stocks = db.execute(
                "SELECT * FROM stock WHERE investigation=? AND (? IS NULL OR stock_id=?) ORDER BY rowid",
                (key, stock_id, stock_id),
            ).fetchall()
            answers = db.execute(
                "SELECT * FROM requests WHERE investigation=? AND answer IS NOT NULL", (key,)
            ).fetchall()
        overlays: dict[str, dict[str, str]] = {}
        for answer in answers:
            overlays.setdefault(answer["stock_id"], {})[answer["field"]] = answer["answer"]
        return [
            Stock.model_validate_json(row["payload"]).model_copy(
                update=overlays.get(row["stock_id"], {})
            )
            for row in stocks
        ]

    def cache(self, snapshot: Snapshot, *, live: bool) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO snapshots VALUES (?, ?, ?)",
                (snapshot.notice_id, snapshot.source_hash, snapshot.model_dump_json()),
            )
            if live:
                db.execute(
                    """INSERT INTO live_heads VALUES (?, ?, ?)
                    ON CONFLICT(notice_id) DO UPDATE SET hash=excluded.hash, observed_at=excluded.observed_at""",
                    (snapshot.notice_id, snapshot.source_hash, snapshot.retrieved_at.isoformat()),
                )
                db.execute(
                    """UPDATE findings SET invalidated_at=? WHERE notice_id=? AND hash<>?
                    AND invalidated_at IS NULL AND investigation IN
                    (SELECT id FROM investigations WHERE mode='live')""",
                    (now(), snapshot.notice_id, snapshot.source_hash),
                )
                db.execute(
                    "INSERT INTO refresh_attempts (notice_id, observed_at, hash) VALUES (?, ?, ?)",
                    (snapshot.notice_id, snapshot.retrieved_at.isoformat(), snapshot.source_hash),
                )

    def latest(self, notice_id: str) -> Snapshot | None:
        with self.connection() as db:
            row = db.execute(
                """SELECT s.payload, h.observed_at FROM live_heads h JOIN snapshots s
                ON h.notice_id=s.notice_id AND h.hash=s.hash WHERE h.notice_id=?""",
                (notice_id,),
            ).fetchone()
        if row is None:
            return None
        return Snapshot.model_validate_json(row["payload"]).model_copy(
            update={"retrieved_at": datetime.fromisoformat(row["observed_at"])},
        )

    def refresh_error(self, notice_id: str, problem: str) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO refresh_attempts (notice_id, observed_at, problem) VALUES (?, ?, ?)",
                (notice_id, now(), problem),
            )

    def record_coverage(
        self,
        key: str,
        notice_id: str,
        snapshot: Snapshot | None,
        criteria: Criteria | None,
        issue: str = "",
        detail: str = "",
    ) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO coverage VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    key,
                    notice_id,
                    snapshot.source_hash if snapshot else None,
                    snapshot.retrieved_at.isoformat() if snapshot else None,
                    criteria.model_dump_json() if criteria else None,
                    issue,
                    detail,
                ),
            )

    def coverage(self, key: str) -> list[sqlite3.Row]:
        with self.connection() as db:
            return db.execute(
                COVERAGE_QUERY, {"key": key, "scope_version": SCOPE_VERSION}
            ).fetchall()

    def save_findings(self, key: str, rows: list[tuple[str, str, str | None, Finding]]) -> None:
        with self.connection() as db:
            # The refresh and finding writes must observe the same ordering across processes.
            db.execute("BEGIN IMMEDIATE")
            superseded = {
                row["notice_id"]
                for row in db.execute(COVERAGE_QUERY, {"key": key, "scope_version": SCOPE_VERSION})
                if row["superseded"]
            }
            invalidated_at = now()
            db.executemany(
                """INSERT INTO findings (investigation, stock_id, notice_id, hash, payload, invalidated_at)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(investigation, stock_id, notice_id)
                DO UPDATE SET payload=excluded.payload, invalidated_at=excluded.invalidated_at
                WHERE findings.invalidated_at IS NULL""",
                [
                    (
                        key,
                        stock_id,
                        notice,
                        digest,
                        result.model_dump_json(),
                        invalidated_at if notice in superseded else None,
                    )
                    for stock_id, notice, digest, result in rows
                ],
            )

    def findings(self, key: str, stock_id: str | None = None) -> list[sqlite3.Row]:
        with self.connection() as db:
            return db.execute(
                """SELECT * FROM findings WHERE investigation=? AND (? IS NULL OR stock_id=?)
                ORDER BY rowid""",
                (key, stock_id, stock_id),
            ).fetchall()

    def tool(self, key: str, event: str, tool: str, summary: str) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO tool_events VALUES (?, ?, ?, ?, ?)",
                (key, event, tool, summary, now()),
            )

    def events(self, key: str) -> list[sqlite3.Row]:
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM tool_events WHERE investigation=? ORDER BY rowid", (key,)
            ).fetchall()

    def request(self, key: str, stock: str, field: str, question: str) -> sqlite3.Row:
        request_id = hashlib.sha256(json.dumps([key, stock, field]).encode()).hexdigest()[:24]
        with self.connection() as db:
            db.execute(
                """INSERT OR IGNORE INTO requests (id, investigation, stock_id, field, question)
                VALUES (?, ?, ?, ?, ?)""",
                (request_id, key, stock, field, question),
            )
            result: sqlite3.Row | None = db.execute(
                "SELECT * FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            assert result is not None
            return result

    def requests(self, key: str) -> list[sqlite3.Row]:
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM requests WHERE investigation=? ORDER BY rowid", (key,)
            ).fetchall()

    def answer(self, key: str, request_id: str, value: str) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT answer FROM requests WHERE investigation=? AND id=?", (key, request_id)
            ).fetchone()
            if row is None:
                raise LookupError("There is no matching identifier request.")
            if row["answer"] is not None and row["answer"] != value:
                raise FileExistsError("This request already has a different answer.")
            if row["answer"] is None:
                db.execute(
                    "UPDATE requests SET answer=?, answered_at=? WHERE id=?",
                    (value, now(), request_id),
                )

    def draft(self, key: str, stock: str, body: str | None) -> None:
        with self.connection() as db:
            if body is None:
                db.execute("DELETE FROM drafts WHERE investigation=? AND stock_id=?", (key, stock))
            else:
                db.execute(
                    """INSERT INTO drafts VALUES (?, ?, ?) ON CONFLICT(investigation, stock_id)
                    DO UPDATE SET body=excluded.body""",
                    (key, stock, body),
                )

    def drafts(self, key: str) -> list[sqlite3.Row]:
        with self.connection() as db:
            return db.execute(
                "SELECT * FROM drafts WHERE investigation=? ORDER BY rowid", (key,)
            ).fetchall()

    def reserve(self, key: str, micros: int, budget: float = DEFAULT_BUDGET) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            count, reserved = db.execute(
                "SELECT count(*), coalesce(sum(micros), 0) FROM model_usage WHERE investigation=?",
                (key,),
            ).fetchone()
            if count >= CALL_LIMIT or reserved + micros > budget * 1_000_000:
                raise BudgetError(
                    "The investigation reached its model call or estimated spend cap."
                )
            db.execute(
                "INSERT INTO model_usage (investigation, micros) VALUES (?, ?)", (key, micros)
            )
