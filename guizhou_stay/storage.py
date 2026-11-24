from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
 session_id TEXT PRIMARY KEY, source TEXT NOT NULL, query TEXT NOT NULL,
 source_revision INTEGER NOT NULL, expected_pages INTEGER NOT NULL, locale TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('open','complete','stale','failed')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, reason TEXT,
 template TEXT NOT NULL DEFAULT 'directory-v1',
 template_transition INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pages(
 session_id TEXT NOT NULL REFERENCES sessions(session_id), page_no INTEGER NOT NULL,
 page_digest TEXT NOT NULL, source_revision INTEGER NOT NULL, captured_at TEXT NOT NULL,
 viewport TEXT NOT NULL, payload TEXT NOT NULL, next_token TEXT,
 template TEXT NOT NULL DEFAULT 'directory-v1',
 PRIMARY KEY(session_id,page_no)
);
CREATE TABLE IF NOT EXISTS collections(
 session_id TEXT PRIMARY KEY REFERENCES sessions(session_id),
 source_label TEXT NOT NULL, cursor TEXT, next_page INTEGER NOT NULL DEFAULT 1,
 attempts INTEGER NOT NULL DEFAULT 0, generation TEXT, lease_expires TEXT,retry_at TEXT
);
CREATE TABLE IF NOT EXISTS captures(
 session_id TEXT NOT NULL, page_no INTEGER NOT NULL,
 locator TEXT NOT NULL, raw_html TEXT NOT NULL, html_digest TEXT NOT NULL,
 PRIMARY KEY(session_id,page_no),
 FOREIGN KEY(session_id,page_no) REFERENCES pages(session_id,page_no)
);
CREATE TABLE IF NOT EXISTS capture_events(
 seq INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES collections(session_id),
 page_no INTEGER NOT NULL, code TEXT NOT NULL, detail TEXT NOT NULL, observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staged(
 session_id TEXT NOT NULL, page_no INTEGER NOT NULL, item_id TEXT NOT NULL,
 digest TEXT NOT NULL, payload TEXT NOT NULL, template TEXT NOT NULL DEFAULT 'directory-v1',
 PRIMARY KEY(session_id,page_no,item_id),
 FOREIGN KEY(session_id,page_no) REFERENCES pages(session_id,page_no)
);
CREATE TABLE IF NOT EXISTS observations(
 item_id TEXT NOT NULL, source TEXT NOT NULL, source_revision INTEGER NOT NULL,
 session_id TEXT NOT NULL, page_no INTEGER NOT NULL,
 template TEXT NOT NULL DEFAULT 'directory-v1',
 PRIMARY KEY(item_id,source,source_revision,session_id,page_no,template),
 FOREIGN KEY(session_id,page_no) REFERENCES pages(session_id,page_no)
);
CREATE TABLE IF NOT EXISTS supplies(
 item_id TEXT NOT NULL, source TEXT NOT NULL, source_revision INTEGER NOT NULL,
 digest TEXT NOT NULL, payload TEXT NOT NULL, received_at TEXT NOT NULL,
 template TEXT NOT NULL DEFAULT 'directory-v1',
 PRIMARY KEY(item_id,source,source_revision,template)
);
CREATE TABLE IF NOT EXISTS rebases(
 rebase_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, source TEXT NOT NULL,
 source_revision INTEGER NOT NULL, original_template TEXT NOT NULL,
 target_template TEXT NOT NULL, basis_digest TEXT NOT NULL, document TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identities(
 identity_id TEXT PRIMARY KEY, kind TEXT NOT NULL, canonical TEXT NOT NULL,
 aliases TEXT NOT NULL, current_digest TEXT NOT NULL, status TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS identity_sources(
 identity_id TEXT NOT NULL REFERENCES identities(identity_id), item_id TEXT NOT NULL,
 source TEXT NOT NULL, source_revision INTEGER NOT NULL, digest TEXT NOT NULL,
 template TEXT NOT NULL DEFAULT 'directory-v1',
 PRIMARY KEY(identity_id,item_id,source,source_revision,template)
);
CREATE TABLE IF NOT EXISTS evidence(
 evidence_id TEXT PRIMARY KEY, supply_id TEXT NOT NULL, category TEXT NOT NULL,
 issuer TEXT NOT NULL, issued_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 source TEXT NOT NULL, source_revision INTEGER NOT NULL, document_ref TEXT NOT NULL,
 status TEXT NOT NULL, digest TEXT NOT NULL,
 template TEXT NOT NULL DEFAULT 'directory-v1'
);
CREATE TABLE IF NOT EXISTS plans(
 plan_id TEXT PRIMARY KEY,digest TEXT NOT NULL,document TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments(
 plan_id TEXT NOT NULL REFERENCES plans(plan_id), slot TEXT NOT NULL,generation INTEGER NOT NULL,
 session_id TEXT NOT NULL REFERENCES sessions(session_id),assigned_at TEXT NOT NULL,
 PRIMARY KEY(plan_id,slot,generation)
);
CREATE TABLE IF NOT EXISTS packages(
 package_id TEXT NOT NULL, revision INTEGER NOT NULL, region TEXT NOT NULL,
 policy TEXT NOT NULL, items TEXT NOT NULL, gaps TEXT NOT NULL,
 digest TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('draft','published','superseded')),
 created_at TEXT NOT NULL, request_digest TEXT NOT NULL,document TEXT NOT NULL,
 source_revision INTEGER NOT NULL DEFAULT 0,
 interpretations TEXT NOT NULL DEFAULT '[]',
 PRIMARY KEY(package_id,revision), UNIQUE(package_id,request_digest)
);
CREATE TABLE IF NOT EXISTS deliveries(
 destination TEXT NOT NULL, delivery_id TEXT NOT NULL,package_id TEXT NOT NULL,edition INTEGER NOT NULL,
 sequence INTEGER NOT NULL,package_digest TEXT NOT NULL,request_digest TEXT NOT NULL,
 queued_at TEXT NOT NULL,acknowledged_at TEXT,receiver_reference TEXT,
 PRIMARY KEY(destination,delivery_id),UNIQUE(destination,sequence),UNIQUE(destination,receiver_reference),
 FOREIGN KEY(package_id,edition) REFERENCES packages(package_id,revision)
);
CREATE TABLE IF NOT EXISTS audit(
 seq INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, object_id TEXT NOT NULL,
 before_digest TEXT, after_digest TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS page_revision ON pages(session_id,source_revision);
CREATE INDEX IF NOT EXISTS supply_region ON supplies(source_revision);
CREATE INDEX IF NOT EXISTS staged_duplicates ON staged(session_id,item_id,page_no);
CREATE INDEX IF NOT EXISTS proof_version ON evidence(source,supply_id,category,source_revision);
CREATE INDEX IF NOT EXISTS collection_history ON capture_events(session_id,seq);
"""


class Store:
    def __init__(self, location: str | Path):
        self.location = Path(location)
        self.location.parent.mkdir(parents=True, exist_ok=True)
        db = self.connect()
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(SCHEMA)
            self._migrate(db)
        finally:
            db.close()

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        """为已有数据库补齐模板与口径列；旧记录按旧版口径回填。"""
        columns = {
            "sessions": {
                "template": "TEXT NOT NULL DEFAULT 'directory-v1'",
                "template_transition": "INTEGER NOT NULL DEFAULT 0",
            },
            "pages": {
                "template": "TEXT NOT NULL DEFAULT 'directory-v1'",
            },
            "packages": {
                "source_revision": "INTEGER NOT NULL DEFAULT 0",
                "interpretations": "TEXT NOT NULL DEFAULT '[]'",
            },
            "supplies": {
                "template": "TEXT NOT NULL DEFAULT 'directory-v1'",
            },
            "observations": {
                "template": "TEXT NOT NULL DEFAULT 'directory-v1'",
            },
            "staged": {
                "template": "TEXT NOT NULL DEFAULT 'directory-v1'",
            },
            "identity_sources": {
                "template": "TEXT NOT NULL DEFAULT 'directory-v1'",
            },
            "evidence": {
                "template": "TEXT NOT NULL DEFAULT 'directory-v1'",
            },
        }
        for table, additions in columns.items():
            present = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
            for name, definition in additions.items():
                if name not in present:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        # 旧库的主键没有模板列，无法同时保存同一记录的新旧两版视图；重建这四张表。
        Store._rebuild_with_template(db, "staged", """
            CREATE TABLE staged_new(
             session_id TEXT NOT NULL, page_no INTEGER NOT NULL, item_id TEXT NOT NULL,
             digest TEXT NOT NULL, payload TEXT NOT NULL,
             template TEXT NOT NULL DEFAULT 'directory-v1',
             PRIMARY KEY(session_id,page_no,item_id),
             FOREIGN KEY(session_id,page_no) REFERENCES pages(session_id,page_no)
            )""", "session_id,page_no,item_id,digest,payload,template")
        Store._rebuild_with_template(db, "observations", """
            CREATE TABLE observations_new(
             item_id TEXT NOT NULL, source TEXT NOT NULL, source_revision INTEGER NOT NULL,
             session_id TEXT NOT NULL, page_no INTEGER NOT NULL,
             template TEXT NOT NULL DEFAULT 'directory-v1',
             PRIMARY KEY(item_id,source,source_revision,session_id,page_no,template),
             FOREIGN KEY(session_id,page_no) REFERENCES pages(session_id,page_no)
            )""", "item_id,source,source_revision,session_id,page_no,template")
        Store._rebuild_with_template(db, "supplies", """
            CREATE TABLE supplies_new(
             item_id TEXT NOT NULL, source TEXT NOT NULL, source_revision INTEGER NOT NULL,
             digest TEXT NOT NULL, payload TEXT NOT NULL, received_at TEXT NOT NULL,
             template TEXT NOT NULL DEFAULT 'directory-v1',
             PRIMARY KEY(item_id,source,source_revision,template)
            )""", "item_id,source,source_revision,digest,payload,received_at,template")
        Store._rebuild_with_template(db, "identity_sources", """
            CREATE TABLE identity_sources_new(
             identity_id TEXT NOT NULL REFERENCES identities(identity_id), item_id TEXT NOT NULL,
             source TEXT NOT NULL, source_revision INTEGER NOT NULL, digest TEXT NOT NULL,
             template TEXT NOT NULL DEFAULT 'directory-v1',
             PRIMARY KEY(identity_id,item_id,source,source_revision,template)
            )""", "identity_id,item_id,source,source_revision,digest,template")

    @staticmethod
    def _rebuild_with_template(db: sqlite3.Connection, table: str,
                               create: str, columns: str) -> None:
        primary = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")
                   if row["pk"]}
        if "template" in primary:
            return
        new = table + "_new"
        db.execute(f"DROP TABLE IF EXISTS {new}")
        db.execute(create)
        db.execute(f"INSERT INTO {new}({columns}) SELECT {columns} FROM {table}")
        db.execute(f"DROP TABLE {table}")
        db.execute(f"ALTER TABLE {new} RENAME TO {table}")

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.location, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def transaction(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

