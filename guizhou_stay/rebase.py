from __future__ import annotations

import json
from datetime import datetime, timezone
from sqlite3 import Connection

from pydantic import model_validator

from guizhou_stay import templates
from guizhou_stay.catalog import canonical_key
from guizhou_stay.contracts import Key, StrictModel, Supply, Template
from guizhou_stay.digest import digest
from guizhou_stay.errors import conflict, missing
from guizhou_stay.storage import dump


class RebaseRequest(StrictModel):
    rebase_id: Key
    session_id: Key
    target_template: Template

    @model_validator(mode="after")
    def registered_target(self):
        if not templates.known(self.target_template):
            raise ValueError("重读目标模板未登记")
        return self


def stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _session(db: Connection, session_id: str):
    row = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if row is None:
        raise missing("session", session_id)
    return row


def _pinned_pages(db: Connection, session_id: str):
    return db.execute(
        """SELECT page_no,template,page_digest,captured_at,payload FROM pages
           WHERE session_id=? ORDER BY page_no""", (session_id,),
    ).fetchall()


def propose(db: Connection, body: RebaseRequest) -> dict:
    """按新口径重读历史，形成独立的新依据；不改变任何旧采集与旧产品。"""
    session = _session(db, body.session_id)
    pages = _pinned_pages(db, body.session_id)
    if not pages:
        raise missing("session", body.session_id)
    original = session["template"]
    rows = []
    for page in pages:
        document = json.loads(page["payload"])
        for raw in document["rows"]:
            item = Supply.model_validate({**raw, "source": session["source"],
                                          "source_revision": session["source_revision"]})
            rows.append({
                "record_key": item.item_id,
                "supply_key": canonical_key(item),
                "source_template": page["template"],
                "source_window_code": item.window_code,
                "source_digest": digest(item.model_dump(mode="json")),
                "page_no": page["page_no"],
                "page_digest": page["page_digest"],
            })
    reinterpreted = original != body.target_template
    incomparable = templates.incomparable(original, body.target_template)
    not_comparable = [
        {"field": field_name,
         "reason": f"{templates.caliber(original, field_name)} 与 "
                   f"{templates.caliber(body.target_template, field_name)} 口径不同",
         "recoverable_from_source": False}
        for field_name in incomparable
    ]
    document = {
        "rebase_id": body.rebase_id,
        "session_id": body.session_id,
        "source": session["source"],
        "source_revision": session["source_revision"],
        "session_state": session["state"],
        "original_template": original,
        "target_template": body.target_template,
        "created_at": stamp(),
        "original_pages": [
            {"page_no": page["page_no"], "template": page["template"],
             "page_digest": page["page_digest"], "captured_at": page["captured_at"]}
            for page in pages
        ],
        "rows": rows,
        "not_comparable_fields": [value["field"] for value in not_comparable],
        "not_comparable": not_comparable,
        "reinterpreted": reinterpreted,
        "supersedes_original": False,
        "note": ("这是按新口径重读历史形成的新依据。原采集、原页面与旧封存包保持原状，"
                 "复核旧包时仍使用当时采到的含义。不可对照字段不得跨口径直接比较，"
                 "也不能单凭旧页原件反推成新口径的值。"),
    }
    document["basis_digest"] = digest(document)
    return document


def record(db: Connection, body: RebaseRequest) -> dict:
    document = propose(db, body)
    existing = db.execute("SELECT document FROM rebases WHERE rebase_id=?",
                          (body.rebase_id,)).fetchone()
    if existing is not None:
        stored = json.loads(existing["document"])
        if stored["basis_digest"] != document["basis_digest"]:
            raise conflict("rebase_changed", "重读编号已对应另一份依据")
        return {**stored, "replayed": True}
    db.execute(
        """INSERT INTO rebases(rebase_id,session_id,source,source_revision,
           original_template,target_template,basis_digest,document,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (body.rebase_id, body.session_id, document["source"],
         document["source_revision"], document["original_template"],
         document["target_template"], document["basis_digest"], dump(document),
         document["created_at"]),
    )
    return {**document, "replayed": False}


def read(db: Connection, rebase_id: str) -> dict:
    row = db.execute("SELECT document FROM rebases WHERE rebase_id=?", (rebase_id,)).fetchone()
    if row is None:
        raise missing("rebase", rebase_id)
    return {**json.loads(row["document"]), "replayed": False}


def history(db: Connection, session_id: str) -> list[dict]:
    rows = db.execute(
        "SELECT rebase_id,basis_digest,original_template,target_template,created_at "
        "FROM rebases WHERE session_id=? ORDER BY created_at,rebase_id", (session_id,))
    return [dict(row) for row in rows]
