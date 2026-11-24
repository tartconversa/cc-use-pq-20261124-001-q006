from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from sqlite3 import Connection
import unicodedata

from pydantic import ValidationError

from guizhou_stay import templates
from guizhou_stay.contracts import Page, Supply
from guizhou_stay.digest import digest
from guizhou_stay.errors import conflict, invalid, missing
from guizhou_stay.storage import dump


def stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def canonical_key(item: Supply) -> str:
    return dump([normalize_text(value) for value in
                 (item.kind, item.provider_id, item.title, item.city, item.region)])


def identity_id(item: Supply) -> str:
    return "id-" + digest(canonical_key(item))[:24]


def gather_rows(body: Page) -> list[tuple[Supply, str]]:
    rows = {}
    for position, raw in enumerate(body.rows, start=1):
        try:
            item = Supply.model_validate({**raw, "source": body.source,
                                          "source_revision": body.source_revision})
        except ValidationError as exc:
            raise invalid("supply_row", "页面中的供给卡片不符合合同",
                          {"position": position,
                           "fields": [error["loc"][0] for error in exc.errors() if error["loc"]]}) from exc
        fingerprint = digest(item.model_dump(mode="json"))
        previous = rows.get(item.item_id)
        if previous is not None and fingerprint != previous[1]:
            raise conflict("supply_changed", "同一快照存在不同的同号供给卡片",
                           {"item_id": item.item_id, "position": position})
        rows[item.item_id] = (item, fingerprint)
    return list(rows.values())


def ingest_page(db: Connection, body: Page) -> dict:
    rows = gather_rows(body)
    for item, fingerprint in rows:
        previous = db.execute(
            """SELECT digest,page_no FROM staged WHERE session_id=? AND item_id=?
               ORDER BY page_no LIMIT 1""", (body.session_id, item.item_id)
        ).fetchone()
        if previous is not None and fingerprint != previous["digest"]:
            raise conflict("supply_changed", "跨页重复卡片的内容不一致",
                           {"item_id": item.item_id, "previous_page": previous["page_no"]})
        db.execute("INSERT INTO staged VALUES(?,?,?,?,?,?)",
                   (body.session_id, body.page_no, item.item_id, fingerprint,
                    dump(item.model_dump(mode="json")), item.template))
    if body.has_next:
        return {"accepted": len(rows), "repeated": 0, "visible": False,
                "source_revision": body.source_revision, "template": body.template}
    result = promote(db, body.session_id)
    return {**result, "visible": True, "source_revision": body.source_revision,
            "template": body.template}


def promote(db: Connection, session_id: str) -> dict:
    staged = db.execute(
        """SELECT item_id,page_no,digest,payload FROM staged WHERE session_id=?
           ORDER BY page_no,item_id""", (session_id,)
    )
    accepted = 0
    repeated = 0
    for row in staged:
        item = Supply.model_validate_json(row["payload"])
        fingerprint = row["digest"]
        if record_supply(db, item, fingerprint, row["payload"]):
            accepted += 1
        else:
            repeated += 1
        db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?)",
                   (item.item_id, item.source, item.source_revision, session_id,
                    row["page_no"], item.template))
    result = {"accepted": accepted, "repeated": repeated}
    result.update(index_interpretations(db, session_id))
    return result


def index_interpretations(db: Connection, session_id: str) -> dict:
    """为一次会话实际落地的每条供给冻结模板与字段口径。"""
    rows = db.execute(
        """SELECT s.item_id,s.source,s.source_revision,s.payload,
                  o.page_no,p.template AS page_template
           FROM observations AS o
           JOIN supplies AS s USING(item_id,source,source_revision,template)
           LEFT JOIN pages AS p USING(session_id,page_no)
           WHERE o.session_id=?""", (session_id,)
    ).fetchall()
    if not rows:
        return {"interpretations": {}, "field_basis": {}}
    interpretations = {}
    basis = {}
    for row in rows:
        item = Supply.model_validate_json(row["payload"])
        template = row["page_template"] or item.template
        key = f"{item.source}/{item.item_id}/{item.source_revision}"
        entry = {"template": template, "window_code": item.window_code,
                 "session_id": session_id, "page_no": row["page_no"],
                 "alternate_interpretations": item.alternate_interpretations,
                 "not_comparable_fields": item.not_comparable_fields}
        existing = interpretations.get(key)
        if existing is not None and existing != entry:
            raise conflict("interpretation_changed", "同一供给在同一会话得到两种模板口径",
                           {"item_id": item.item_id})
        interpretations[key] = entry
        basis[key] = item.field_basis
    return {"interpretations": interpretations, "field_basis": basis}


def record_supply(db, item: Supply, fingerprint: str, payload: str) -> bool:
    old = db.execute(
        """SELECT digest FROM supplies
           WHERE item_id=? AND source=? AND source_revision=? AND template=?""",
        (item.item_id, item.source, item.source_revision, item.template),
    ).fetchone()
    if old is not None:
        if old["digest"] != fingerprint:
            raise conflict("supply_changed", "另一完整采集已登记不同的同号同修订同模板卡片",
                           {"item_id": item.item_id, "source": item.source})
        return False
    db.execute("INSERT INTO supplies VALUES(?,?,?,?,?,?,?)",
               (item.item_id, item.source, item.source_revision, fingerprint, payload,
                stamp(), item.template))
    _attach_identity(db, item, fingerprint)
    return True


def _attach_identity(db: Connection, item: Supply, item_digest: str) -> str:
    identity = identity_id(item)
    db.execute(
        """INSERT INTO identities VALUES(?,?,?,?,?,?,?) ON CONFLICT(identity_id) DO NOTHING""",
        (identity, item.kind, canonical_key(item), "[]", item_digest, item.status, stamp()),
    )
    db.execute(
        "INSERT OR IGNORE INTO identity_sources VALUES(?,?,?,?,?,?)",
        (identity, item.item_id, item.source, item.source_revision, item_digest,
         item.template))
    return identity


def supply(db: Connection, item_id: str, source: str, revision: int,
           *, template: str | None = None) -> Supply:
    if template is None:
        row = db.execute(
            """SELECT payload FROM supplies WHERE item_id=? AND source=? AND source_revision=?
               ORDER BY template DESC LIMIT 1""",
            (item_id, source, revision),
        ).fetchone()
    else:
        row = db.execute(
            """SELECT payload FROM supplies
               WHERE item_id=? AND source=? AND source_revision=? AND template=?""",
            (item_id, source, revision, template),
        ).fetchone()
    if row is None:
        raise missing("supply", f"{source}/{item_id}/{revision}")
    return Supply.model_validate_json(row["payload"])


def versions(db: Connection, *, revision: int | None = None,
             template: str | None = None) -> list[Supply]:
    clauses = []
    args: list = []
    if revision is not None:
        clauses.append("source_revision=?")
        args.append(revision)
    if template is not None:
        clauses.append("template=?")
        args.append(template)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    if revision is not None:
        rows = db.execute(
            f"SELECT payload FROM supplies {where} ORDER BY source,item_id,template", args)
    else:
        rows = db.execute(
            f"""WITH latest AS (
                 SELECT source,item_id,template,MAX(source_revision) AS source_revision
                 FROM supplies GROUP BY source,item_id,template
                 )
                 SELECT supplies.payload FROM supplies JOIN latest
                 USING(source,item_id,source_revision,template)
                 {where}
                 ORDER BY supplies.source,supplies.item_id,supplies.template""", args)
    return [Supply.model_validate_json(row["payload"]) for row in rows]


@dataclass(frozen=True)
class Group:
    identity_id: str
    rows: list[Supply]

    @property
    def winner(self) -> Supply:
        return min(self.rows, key=lambda item: (-item.updated_at.timestamp(),
                                                -templates.version(item.template),
                                                item.source, item.item_id))

    def disputed_fields(self) -> list[str]:
        calibers = {templates.caliber(item.template, "service_window") for item in self.rows}
        declared = set()
        for item in self.rows:
            declared.update(item.conflicts)
        ordered = []
        for field_name in ("category", "status", "address_hint", "price_band",
                           "service_window"):
            if field_name == "service_window" and len(calibers) > 1:
                continue
            if field_name in declared or len({getattr(item, field_name)
                                              for item in self.rows}) > 1:
                ordered.append(field_name)
        return ordered

    def not_comparable(self) -> list[str]:
        fields = set()
        for item in self.rows:
            fields.update(item.not_comparable_fields)
        if len({templates.caliber(item.template, "service_window") for item in self.rows}) > 1:
            fields.add("service_window")
        return sorted(fields)

    def conflicts(self) -> dict:
        """页内新版卡与兼容区卡片的字段冲突，双方取值都保留。"""
        merged = {}
        for item in self.rows:
            for field_name, value in item.conflicts.items():
                merged.setdefault(field_name, []).append({"item_id": item.item_id, **value})
        return merged

    def public(self) -> dict:
        return {
            "identity_id": self.identity_id,
            "canonical": canonical_key(self.winner),
            "kind": self.winner.kind,
            "aliases": sorted({item.item_id for item in self.rows}),
            "status": self.winner.status,
            "current_digest": digest(self.winner.model_dump(mode="json")),
            "disputed_fields": self.disputed_fields(),
            "not_comparable_fields": self.not_comparable(),
            "conflicts": self.conflicts(),
            "interpretations": templates.interpretations([
                {"template": item.template, "window_code": item.window_code}
                for item in self.rows
            ]),
            "sources": [
                {"source": item.source, "item_id": item.item_id, "revision": item.source_revision,
                 "template": item.template, "window_code": item.window_code}
                for item in self.rows
            ],
        }


def groups(db: Connection, *, region: str | None = None, revision: int | None = None,
           template: str | None = None) -> list[Group]:
    index = {}
    for item in versions(db, revision=revision, template=template):
        if region is None or item.region == region:
            index.setdefault(identity_id(item), []).append(item)
    return [Group(key, values) for key, values in sorted(index.items())]


def identities(db: Connection, *, kind: str | None = None, region: str | None = None) -> list[dict]:
    return [group.public() for group in groups(db, region=region)
            if kind is None or group.winner.kind == kind]


def current_supplies(db: Connection, *, region: str | None = None,
                     source_revision: int | None = None,
                     template: str | None = None) -> list[Supply]:
    return [group.winner for group in groups(db, region=region, revision=source_revision,
                                             template=template)]


def receipt_sources(db: Connection, item: Supply) -> list[dict]:
    rows = db.execute(
        """SELECT o.session_id,o.page_no,p.page_digest,p.captured_at,c.html_digest,c.locator,
                  p.template,o.template AS observed_template
           FROM observations AS o JOIN pages AS p USING(session_id,page_no)
           LEFT JOIN captures AS c USING(session_id,page_no)
           WHERE o.item_id=? AND o.source=? AND o.source_revision=? AND o.template=?
           ORDER BY o.session_id,o.page_no""",
        (item.item_id, item.source, item.source_revision, item.template),
    )
    return [dict(row) for row in rows]
