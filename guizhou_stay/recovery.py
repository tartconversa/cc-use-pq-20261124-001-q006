from __future__ import annotations

import json
from sqlite3 import Connection

from guizhou_stay import templates
from guizhou_stay.catalog import canonical_key
from guizhou_stay.contracts import Supply
from guizhou_stay.digest import digest
from guizhou_stay.errors import conflict, invalid, missing


def _session(db: Connection, session_id: str):
    row = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if row is None:
        raise missing("session", session_id)
    return row


def _pages(db: Connection, session_id: str):
    return db.execute(
        """SELECT p.page_no,p.template,p.page_digest,p.captured_at,p.payload,
                  c.html_digest,c.locator
           FROM pages AS p LEFT JOIN captures AS c USING(session_id,page_no)
           WHERE p.session_id=? ORDER BY p.page_no""", (session_id,),
    ).fetchall()


def _facts(item: Supply) -> dict:
    return {key: getattr(item, key) for key in templates.VALUE_FIELDS}


def _session_rows(db: Connection, session_id: str) -> list[dict]:
    session = _session(db, session_id)
    rows = []
    for page in _pages(db, session_id):
        for raw in json.loads(page["payload"])["rows"]:
            item = Supply.model_validate({**raw, "source": session["source"],
                                          "source_revision": session["source_revision"]})
            rows.append({
                "record_key": item.item_id,
                "supply_key": canonical_key(item),
                "template": page["template"] or item.template,
                "window_code": item.window_code,
                "digest": digest(item.model_dump(mode="json")),
                "facts": _facts(item),
                "conflicts": item.conflicts,
                "not_comparable_fields": item.not_comparable_fields,
                "page_no": page["page_no"],
                "page_digest": page["page_digest"],
                "html_digest": page["html_digest"],
                "captured_at": page["captured_at"],
            })
    return rows


def page_inventory(db: Connection, session_id: str) -> dict:
    """逐页列出来源模板、记录键、可证明的供给事实与原件是否保留。"""
    session = _session(db, session_id)
    rows = _session_rows(db, session_id)
    pages = []
    for page in _pages(db, session_id):
        pages.append({
            "page_no": page["page_no"], "template": page["template"],
            "page_digest": page["page_digest"], "html_digest": page["html_digest"],
            "captured_at": page["captured_at"],
            "preserved_original": page["html_digest"] is not None,
            "locator": page["locator"],
            "rows": [{key: row[key] for key in ("record_key", "supply_key", "template",
                                                "window_code", "digest", "conflicts",
                                                "not_comparable_fields")}
                     for row in rows if row["page_no"] == page["page_no"]],
        })
    return {
        "session_id": session_id, "source": session["source"],
        "source_revision": session["source_revision"], "state": session["state"],
        "declared_template": session["template"],
        "template_transition": bool(session["template_transition"]),
        "pages": pages,
        "templates": sorted({page["template"] for page in pages}),
        "all_originals_preserved": all(page["preserved_original"] for page in pages),
    }


def _transition_observed(session) -> str | None:
    if not session["reason"]:
        return None
    try:
        value = json.loads(session["reason"])
    except (TypeError, ValueError):
        return None
    change = value.get("template_transition") if isinstance(value, dict) else None
    return change.get("observed") if isinstance(change, dict) else None


def plan(db: Connection, session_id: str, *, successor: str | None = None) -> dict:
    """切换后仍可证明相同供给事实的页面，与需要重采的页面。

    - 已收到的旧版页面及其原件一律保留，不因出现新版页面而整批丢弃。
    - 新版记录若在双方都能解释的字段上与旧页记录对齐，视为同一供给事实的
      另一视图，不双计；不可对照字段只列出，不用来重释旧页。
    - 无法对齐或对齐后仍不同的新版记录，必须重采，否则不能出版。
    """
    session = _session(db, session_id)
    inventory = page_inventory(db, session_id)
    transition = bool(session["template_transition"])
    original = inventory["declared_template"]
    recapture: list[dict] = []
    cross_template: list[dict] = []
    same_fact: set[str] = set()

    old_rows = _session_rows(db, session_id)
    reusable: dict[str, dict] = {}
    for row in old_rows:
        if row["template"] != original:
            continue
        entry = reusable.setdefault(row["supply_key"], {
            "record_key": row["record_key"],
            "supply_key": row["supply_key"],
            "template": original,
            "window_code": row["window_code"],
            "pages": [],
            "digest": row["digest"],
        })
        entry["pages"].append(row["page_no"])

    successor_report = None
    if successor is not None:
        new_session = _session(db, successor)
        for field_name in ("source", "query", "source_revision"):
            if new_session[field_name] != session[field_name]:
                raise conflict("successor_scope", "后续采集与中止会话的来源或范围不一致",
                               {"field": field_name})
        if successor == session_id:
            raise invalid("successor_identity", "恢复对照需要一个独立的新采集会话")
        new_rows = _session_rows(db, successor)
        new_templates = sorted({row["template"] for row in new_rows})
        if not new_templates or new_templates == [original]:
            raise invalid("successor_template", "后续采集没有使用新的模板版本")
        seen = set()
        for row in new_rows:
            counterpart = reusable.get(row["supply_key"])
            if counterpart is None:
                recapture.append({
                    "page_no": row["page_no"], "record_key": row["record_key"],
                    "template": row["template"], "reason": "no_old_counterpart",
                    "preserved": row["html_digest"] is not None,
                })
                continue
            old_side = next(value for value in old_rows
                            if value["supply_key"] == row["supply_key"]
                            and value["template"] == original)
            shared = templates.comparable(original, row["template"])
            differing = [field_name for field_name in sorted(shared)
                         if old_side["facts"].get(field_name) != row["facts"].get(field_name)]
            cross_template.append({
                "record_key": row["record_key"],
                "template_a": {"template": original, "page_no": old_side["page_no"],
                               "window_code": old_side["window_code"]},
                "template_b": {"template": row["template"], "page_no": row["page_no"],
                               "window_code": row["window_code"]},
                "comparable_fields": sorted(shared),
                "differing_fields": differing,
                "not_comparable_fields": templates.incomparable(original, row["template"]),
                "conflicts": row["conflicts"],
            })
            if differing:
                recapture.append({
                    "page_no": row["page_no"], "record_key": row["record_key"],
                    "template": row["template"], "reason": "facts_differ",
                    "fields": differing, "preserved": row["html_digest"] is not None,
                })
            else:
                same_fact.add(row["supply_key"])
            seen.add(row["supply_key"])
        for key in sorted(set(reusable) - seen):
            recapture.append({
                "record_key": reusable[key]["record_key"], "template": original,
                "reason": "missing_from_successor", "pages": reusable[key]["pages"],
                "preserved": True,
            })
        successor_report = {
            "session_id": successor, "templates": new_templates,
            "pages": len(_pages(db, successor)),
            "complete": new_session["state"] == "complete",
        }
    else:
        observed = _transition_observed(session) or "unknown"
        received = len(inventory["pages"])
        for page_no in range(received + 1, session["expected_pages"] + 1):
            recapture.append({
                "page_no": page_no, "template": observed,
                "reason": "source_now_serves_new_template", "preserved": False,
            })

    reusable_list = [reusable[key] for key in sorted(reusable)]
    recapture_pages = sorted({(row.get("page_no"), row["template"]) for row in recapture},
                             key=lambda value: (value[0] is None, value[0] or 0, value[1]))
    successor_complete = successor_report is None or successor_report["complete"]
    common_basis = (transition and not recapture and successor_complete
                    and not any(row["conflicts"] for row in cross_template))
    return {
        "session_id": session_id, "original_template": original,
        "observed_template": _transition_observed(session) if transition else None,
        "templates": inventory["templates"],
        "successor": successor_report,
        "reusable": reusable_list,
        "reusable_items": [value["record_key"] for value in reusable_list],
        "duplicate_views_not_counted": len(same_fact),
        "same_supply_facts_provable": sorted(same_fact),
        "cross_template": cross_template,
        "recapture": recapture,
        "recapture_pages": [{"page_no": page, "template": template}
                            for page, template in recapture_pages],
        "recapture_needed": bool(recapture),
        "common_basis": common_basis,
        "preserved_originals": inventory["all_originals_preserved"],
        "basis_digest": digest({key: value for key, value in inventory.items() if key != "pages"}),
        "note": ("已收旧页与新页原件都保留；在任何新依据形成前，不得把两种模板的页面"
                 "拼成一份没有共同业务解释的全量结果。"),
    }


def assemble(db: Connection, session_id: str, *, successor: str | None = None) -> dict:
    """只有在共同业务解释成立时才允许恢复出件；否则返回可复用/需重采清单。"""
    report = plan(db, session_id, successor=successor)
    if not report["common_basis"]:
        raise conflict("no_common_basis", "旧页与新页没有共同业务解释，不能拼成全量结果",
                       {"reusable": report["reusable_items"],
                        "recapture_pages": report["recapture_pages"]})
    return report
