from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from guizhou_stay import collector, recovery, verification
from guizhou_stay.errors import DomainError
from guizhou_stay.portal import CaptureFailure, FolderReader, convert
from guizhou_stay.storage import Store

ROOT = Path(__file__).resolve().parents[1] / "fixtures"
V2 = ROOT / "partner-v2"
SWITCH = ROOT / "partner-switch"
MATCH = ROOT / "partner-match"
CLOCK = datetime(2026, 11, 23, tzinfo=timezone.utc)


def card(**overrides):
    base = ("<article class=\"stay-card\" data-id=\"stay-1\" data-kind=\"property\" "
            "data-category=\"residence\" data-provider=\"partner-a\" "
            "data-updated=\"2026-11-19T11:00:00+08:00\"")
    return overrides, base


def page(template, *, items, extra_head="", compat="", revision=5, page_no=1, pages=1,
         cursor=None, nxt=None):
    head = (f"<meta name=\"stay-source\" content=\"partner-map\">"
            f"<meta name=\"stay-query\" content=\"贵州旅居\">"
            f"<meta name=\"stay-revision\" content=\"{revision}\">"
            f"<meta name=\"stay-locale\" content=\"zh-HK\">"
            f"<meta name=\"stay-template\" content=\"{template}\">"
            f"<meta name=\"stay-page\" content=\"{page_no}\">"
            f"<meta name=\"stay-pages\" content=\"{pages}\">"
            f"<meta name=\"stay-viewport\" content=\"desktop-{page_no}\">"
            f"<meta name=\"stay-observed\" content=\"2026-11-23T11:00:0{page_no}+08:00\">"
            + extra_head
            + (f"<meta name=\"stay-cursor\" content=\"{cursor}\">" if cursor else "")
            + (f"<meta name=\"stay-next\" content=\"{nxt}\">" if nxt else ""))
    return (f"<!doctype html><html><head>{head}</head><body><main>{''.join(items)}"
            f"</main>{compat}</body></html>")


class TemplateParseTest(unittest.TestCase):
    def parse(self, html):
        return convert("s1", html, "https://stay.example.test/catalog/first.html")

    def test_absent_all_day_and_pending_stay_distinct(self):
        items = [
            "<article class=\"stay-card\" data-id=\"a\" data-kind=\"service\" "
            "data-category=\"medical\" data-provider=\"partner-b\" "
            "data-updated=\"2026-11-19T11:00:00+08:00\" data-window=\"all-day\">"
            "<a href=\"/a\">全天</a><span class=\"region\">贵阳</span>"
            "<span class=\"hours\">全天接待</span></article>",
            "<article class=\"stay-card\" data-id=\"b\" data-kind=\"service\" "
            "data-category=\"medical\" data-provider=\"partner-b\" "
            "data-updated=\"2026-11-19T11:00:00+08:00\" data-window=\"night-duty\">"
            "<a href=\"/b\">值班</a><span class=\"region\">贵阳</span>"
            "<span class=\"hours\">夜间仅值班</span></article>",
            "<article class=\"stay-card\" data-id=\"c\" data-kind=\"service\" "
            "data-category=\"medical\" data-provider=\"partner-b\" "
            "data-updated=\"2026-11-19T11:00:00+08:00\" data-window=\"pending\">"
            "<a href=\"/c\">待确认</a><span class=\"region\">贵阳</span>"
            "<span class=\"hours\">待确认</span></article>",
            "<article class=\"stay-card\" data-id=\"d\" data-kind=\"service\" "
            "data-category=\"medical\" data-provider=\"partner-b\" "
            "data-updated=\"2026-11-19T11:00:00+08:00\">"
            "<a href=\"/d\">无声明</a><span class=\"region\">贵阳</span>"
            "<span class=\"hours\">全天接待</span></article>",
        ]
        capture = self.parse(page("directory-v2", items=items))
        codes = {row["item_id"]: row["window_code"] for row in capture.page.rows}
        self.assertEqual(codes, {"a": "all-day", "b": "night-duty",
                                 "c": "pending", "d": "absent"})
        # 明明写着“全天接待”，但没有 data-window，就不能用来反推全天。
        self.assertEqual(capture.page.rows[3]["service_window"], "全天接待")
        self.assertEqual(capture.page.rows[3]["window_code"], "absent")

    def test_legacy_card_never_gains_a_structured_window(self):
        items = ["<article class=\"stay-card\" data-id=\"a\" data-kind=\"service\" "
                 "data-category=\"medical\" data-provider=\"partner-b\" "
                 "data-updated=\"2026-11-19T11:00:00+08:00\">"
                 "<a href=\"/a\">诊所</a><span class=\"region\">贵阳</span>"
                 "<span class=\"hours\">全天接待</span></article>"]
        capture = self.parse(page("directory-v1", items=items))
        row = capture.page.rows[0]
        self.assertEqual(row["window_code"], "unspecified-bookable")
        self.assertEqual(row["service_window"], "全天接待")

    def test_unrecognized_window_attribute_is_rejected(self):
        items = ["<article class=\"stay-card\" data-id=\"a\" data-kind=\"service\" "
                 "data-category=\"medical\" data-provider=\"partner-b\" "
                 "data-updated=\"2026-11-19T11:00:00+08:00\" data-window=\"sometimes\">"
                 "<a href=\"/a\">诊所</a><span class=\"region\">贵阳</span></article>"]
        with self.assertRaises(CaptureFailure) as failure:
            self.parse(page("directory-v2", items=items))
        self.assertEqual(failure.exception.code, "window_code")

    def test_compat_block_is_not_double_counted(self):
        items = ["<article class=\"stay-card\" data-id=\"stay-1\" data-kind=\"property\" "
                 "data-category=\"residence\" data-provider=\"partner-a\" "
                 "data-updated=\"2026-11-19T11:00:00+08:00\" data-window=\"all-day\">"
                 "<a href=\"/stay-1\">雲上小院</a><span class=\"region\">贵阳</span>"
                 "<span class=\"hours\">全天接待</span></article>"]
        compat = ("<section class=\"stay-compat\"><article class=\"stay-card stay-legacy\" "
                  "data-item-id=\"stay-1\" data-id=\"stay-1\" data-kind=\"property\" "
                  "data-category=\"residence\" data-provider=\"partner-a\" "
                  "data-updated=\"2026-11-19T11:00:00+08:00\">"
                  "<a href=\"/stay-1\">雲上小院</a><span class=\"region\">贵阳</span>"
                  "<span class=\"hours\">可预约</span></article></section>")
        capture = self.parse(page("directory-v2", items=items, compat=compat))
        self.assertEqual(len(capture.page.rows), 1)
        row = capture.page.rows[0]
        self.assertEqual(row["template"], "directory-v2")
        self.assertEqual(row["window_code"], "all-day")
        self.assertEqual(row["alternate_interpretations"], [
            {"template": "directory-v1", "window_code": "unspecified-bookable"},
            {"template": "directory-v2", "window_code": "all-day"},
        ])
        self.assertIn("service_window", row["not_comparable_fields"])

    def test_conflicting_compat_view_lists_both_sides(self):
        items = ["<article class=\"stay-card\" data-id=\"stay-1\" data-kind=\"property\" "
                 "data-category=\"residence\" data-provider=\"partner-a\" "
                 "data-updated=\"2026-11-19T11:00:00+08:00\" data-window=\"all-day\">"
                 "<a href=\"/stay-1\">雲上小院</a><span class=\"region\">贵阳</span>"
                 "<span class=\"price\">高</span></article>"]
        compat = ("<section class=\"stay-compat\"><article class=\"stay-card stay-legacy\" "
                  "data-item-id=\"stay-1\" data-id=\"stay-1\" data-kind=\"property\" "
                  "data-category=\"residence\" data-provider=\"partner-a\" "
                  "data-updated=\"2026-11-19T11:00:00+08:00\">"
                  "<a href=\"/stay-1\">雲上小院</a><span class=\"region\">贵阳</span>"
                  "<span class=\"price\">低</span></article></section>")
        capture = self.parse(page("directory-v2", items=items, compat=compat))
        row = capture.page.rows[0]
        self.assertEqual(row["price_band"], "高")
        self.assertEqual(row["conflicts"]["price_band"]["sides"],
                         {"directory-v1": "低", "directory-v2": "高"})
        self.assertEqual(row["compat_view"]["service_window"], None)


class UpgradeAuditTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root / "stay.sqlite3")
        reader = FolderReader(V2)
        collector.run(self.store, "v2", V2.resolve().as_uri(), reader, 5)

    def test_audit_reports_parser_drift_but_keeps_stored_meaning(self):
        from guizhou_stay import verification as module
        from guizhou_stay import portal
        from guizhou_stay.contracts import Page
        original = portal.convert

        def upgraded(session_id, html, url):
            capture = original(session_id, html, url)
            document = capture.page.model_dump(mode="json")
            document["rows"][0]["service_window"] = "全天接待(升级推断)"
            return portal.Capture(capture.html, capture.url, Page.model_validate(document))

        module.convert = upgraded
        try:
            with self.store.transaction() as db:
                audit = verification.review(db, "v2")
                stored = db.execute(
                    "SELECT payload FROM pages WHERE session_id='v2' AND page_no=1"
                ).fetchone()["payload"]
        finally:
            module.convert = original
        self.assertFalse(audit["valid"])
        drifted = [finding for finding in audit["findings"]
                   if finding["code"] == "extraction_drift"]
        self.assertTrue(drifted)
        self.assertIn("rows", drifted[0]["fields"])
        # 已存页面与冻结口径未被当前解析器改写。
        self.assertNotIn("(升级推断)", stored)
        self.assertEqual(audit["interpretations"][0]["window_code"], "all-day")

    def test_audit_flags_template_drift(self):
        from guizhou_stay import verification as module
        from guizhou_stay import portal
        from guizhou_stay.contracts import Page
        original = portal.convert

        def downgraded(session_id, html, url):
            capture = original(session_id, html, url)
            document = capture.page.model_dump(mode="json")
            document["template"] = "directory-v1"
            return portal.Capture(capture.html, capture.url, Page.model_validate(document))

        module.convert = downgraded
        try:
            with self.store.transaction() as db:
                audit = verification.review(db, "v2")
        finally:
            module.convert = original
        self.assertIn("template_drift",
                      {finding["code"] for finding in audit["findings"]})


class SwitchDrillTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root / "stay.sqlite3")

    def collect(self, session, folder, limit=1):
        reader = FolderReader(folder)
        return collector.run(self.store, session, folder.resolve().as_uri(), reader, limit)

    def halted(self):
        self.collect("switch", SWITCH, 1)
        stopped = self.collect("switch", SWITCH, 1)
        self.assertEqual(stopped["state"], "stale")
        self.assertTrue(stopped["template_transition"])
        self.assertEqual(stopped["received_pages"], 1)
        return stopped

    def test_switch_stops_without_losing_old_pages_or_publishing(self):
        stopped = self.halted()
        self.assertEqual(stopped["events"][-1]["code"], "template_changed")
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM supplies").fetchone()[0], 0)
            inventory = recovery.page_inventory(db, "switch")
            original = collector.raw_page(db, "switch", 1)
        self.assertEqual(inventory["templates"], ["directory-v1"])
        self.assertTrue(inventory["all_originals_preserved"])
        self.assertIn("雲上小院", original["raw_html"])

    def test_recovery_names_reusable_and_recapture_pages(self):
        self.halted()
        self.collect("match", MATCH, 5)
        with self.store.transaction() as db:
            report = recovery.plan(db, "switch", successor="match")
        self.assertEqual(report["original_template"], "directory-v1")
        self.assertEqual(report["observed_template"], "directory-v2")
        self.assertFalse(report["recapture_needed"])
        self.assertTrue(report["common_basis"])
        self.assertEqual(report["duplicate_views_not_counted"], 2)
        self.assertEqual(len(report["cross_template"]), 2)
        for entry in report["cross_template"]:
            self.assertEqual(entry["differing_fields"], [])
            self.assertEqual(entry["not_comparable_fields"], ["service_window"])
        self.assertTrue(report["preserved_originals"])

    def test_no_successor_requires_recapture_of_unseen_pages(self):
        self.halted()
        with self.store.transaction() as db:
            report = recovery.plan(db, "switch")
            with self.assertRaises(DomainError) as failure:
                recovery.assemble(db, "switch")
        self.assertEqual(failure.exception.code, "no_common_basis")
        self.assertTrue(report["recapture_needed"])
        self.assertFalse(report["common_basis"])
        self.assertEqual(report["recapture_pages"], [
            {"page_no": 2, "template": "directory-v2"},
        ])

    def test_old_page_meaning_is_not_rewritten_by_new_template(self):
        self.halted()
        with self.store.transaction() as db:
            audit = verification.review(db, "switch")
            row = db.execute(
                "SELECT payload FROM supplies WHERE item_id='stay-2'").fetchone()
        self.assertTrue(audit["valid"])
        self.assertEqual(audit["template"], "directory-v1")
        # 旧页的“可预约”没有被新版卡片的“全天”重释。
        audit_basis = {entry["item_id"]: entry for entry in audit["interpretations"]}
        self.assertEqual(audit_basis["stay-2"]["window_code"], "unspecified-bookable")

    def test_successor_can_publish_only_under_its_own_basis(self):
        self.halted()
        self.collect("match", MATCH, 5)
        with self.store.transaction() as db:
            report = recovery.assemble(db, "switch", successor="match")
            old = db.execute(
                "SELECT DISTINCT template FROM supplies WHERE item_id='stay-1'").fetchall()
            match = db.execute(
                "SELECT DISTINCT template FROM supplies WHERE item_id='stay-1' "
                "AND template='directory-v2'").fetchall()
        self.assertTrue(report["common_basis"])
        # 旧页没有被推进供给表；可发布的是新模板的独立采集。
        self.assertEqual({row["template"] for row in old}, {"directory-v2"})
        self.assertTrue(match)
