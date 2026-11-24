from __future__ import annotations

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from guizhou_stay.storage import Store

LEGACY = """
CREATE TABLE sessions(session_id TEXT PRIMARY KEY, source TEXT NOT NULL, query TEXT NOT NULL,
 source_revision INTEGER NOT NULL, expected_pages INTEGER NOT NULL, locale TEXT NOT NULL,
 state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, reason TEXT);
CREATE TABLE pages(session_id TEXT NOT NULL, page_no INTEGER NOT NULL,
 page_digest TEXT NOT NULL, source_revision INTEGER NOT NULL, captured_at TEXT NOT NULL,
 viewport TEXT NOT NULL, payload TEXT NOT NULL, next_token TEXT, PRIMARY KEY(session_id,page_no));
CREATE TABLE staged(session_id TEXT NOT NULL, page_no INTEGER NOT NULL, item_id TEXT NOT NULL,
 digest TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(session_id,page_no,item_id));
CREATE TABLE observations(item_id TEXT NOT NULL, source TEXT NOT NULL,
 source_revision INTEGER NOT NULL, session_id TEXT NOT NULL, page_no INTEGER NOT NULL,
 PRIMARY KEY(item_id,source,source_revision,session_id,page_no));
CREATE TABLE supplies(item_id TEXT NOT NULL, source TEXT NOT NULL, source_revision INTEGER NOT NULL,
 digest TEXT NOT NULL, payload TEXT NOT NULL, received_at TEXT NOT NULL,
 PRIMARY KEY(item_id,source,source_revision));
CREATE TABLE identities(identity_id TEXT PRIMARY KEY, kind TEXT NOT NULL, canonical TEXT NOT NULL,
 aliases TEXT NOT NULL, current_digest TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE identity_sources(identity_id TEXT NOT NULL, item_id TEXT NOT NULL, source TEXT NOT NULL,
 source_revision INTEGER NOT NULL, digest TEXT NOT NULL,
 PRIMARY KEY(identity_id,item_id,source,source_revision));
INSERT INTO sessions VALUES('s1','partner-map','贵州旅居',4,1,'zh-HK','complete','t','t',NULL);
INSERT INTO pages VALUES('s1',1,'pd',4,'t','v','{}',NULL);
INSERT INTO staged VALUES('s1',1,'stay-1','d','{}');
INSERT INTO observations VALUES('stay-1','partner-map',4,'s1',1);
INSERT INTO supplies VALUES('stay-1','partner-map',4,'d','{}','t');
INSERT INTO identities VALUES('id-x','property','[]','[]','d','active','t');
INSERT INTO identity_sources VALUES('id-x','stay-1','partner-map',4,'d');
"""


class MigrationTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "legacy.sqlite3"
        db = sqlite3.connect(self.path)
        db.executescript(LEGACY)
        db.commit()
        db.close()

    def test_legacy_rows_are_backfilled_with_the_old_template(self):
        store = Store(self.path)
        with store.transaction() as db:
            for table in ("sessions", "pages", "supplies", "observations", "staged",
                          "identity_sources"):
                row = db.execute(f"SELECT template FROM {table} LIMIT 1").fetchone()
                self.assertEqual(row["template"], "directory-v1", table)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_both_template_views_of_one_record_can_coexist_after_upgrade(self):
        store = Store(self.path)
        with store.transaction(write=True) as db:
            db.execute("INSERT INTO supplies VALUES"
                       "('stay-1','partner-map',4,'d2','{}','t2','directory-v2')")
            db.execute("INSERT INTO observations VALUES"
                       "('stay-1','partner-map',4,'s1',1,'directory-v2')")
        with store.transaction() as db:
            rows = db.execute("SELECT template FROM supplies ORDER BY template").fetchall()
        self.assertEqual([row["template"] for row in rows],
                         ["directory-v1", "directory-v2"])

    def test_reopening_an_already_migrated_database_is_idempotent(self):
        Store(self.path)
        Store(self.path)
        with Store(self.path).transaction() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM supplies").fetchone()[0], 1)
