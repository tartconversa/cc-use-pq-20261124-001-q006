from __future__ import annotations

from datetime import datetime, timezone

from tests.support import ApiCase, item, page, policy


def v2_item(item_id, title, category="residence", *, window="all-day",
            template="directory-v2", **overrides):
    row = item(item_id, title, category, **overrides)
    row.update(template=template, window_code=window,
               service_window={"all-day": "全天接待", "night-duty": "夜间仅值班",
                               "pending": "待确认"}.get(window))
    return row


class InterpretationTest(ApiCase):
    def open_v2(self, session="hk-001", pages=1):
        return self.call("POST", "/sessions", {
            "session_id": session, "source": "partner-map", "query": "贵州旅居",
            "source_revision": 4, "expected_pages": pages, "template": "directory-v2"})

    def test_manual_page_can_declare_template_and_caliber_is_frozen(self):
        self.open_v2(pages=1)
        data = page(1, [v2_item("stay-1", "云上小院", window="night-duty")],
                    total=1, has_next=False)
        data["template"] = "directory-v2"
        self.send_page(data)
        audit = self.call("GET", "/sessions/hk-001/audit")
        self.assertTrue(audit["valid"])
        self.assertEqual(audit["template"], "directory-v2")
        self.assertEqual(audit["interpretations"][0]["template"], "directory-v2")
        self.assertEqual(audit["interpretations"][0]["window_code"], "night-duty")
        self.assertEqual(audit["interpretations"][0]["field_basis"]["service_window"],
                         "structured-window")

    def test_package_records_template_and_window_code_per_item(self):
        self.open_v2(pages=1)
        data = page(1, [v2_item("stay-1", "云上小院")], total=1, has_next=False)
        data["template"] = "directory-v2"
        self.send_page(data)
        at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        package = self.call("POST", "/packages", {
            "package_id": "pkg-v2", "region": "贵阳", "requested_at": at,
            "policy": policy(categories=("residence",)),
            "expected_revision": 4,
        })
        self.assertEqual(package["template_basis"], ["directory-v2"])
        entry = package["interpretations"][0]
        self.assertEqual((entry["item_id"], entry["template"], entry["window_code"]),
                         ("stay-1", "directory-v2", "all-day"))
        witness = package["items"]["residence"]["findings"][0]["witnesses"][0]
        self.assertEqual(witness["interpretation"]["template"], "directory-v2")

    def test_old_package_is_unchanged_after_parser_upgrade(self):
        self.open(pages=1)
        self.send_page(page(1, [item("stay-1", "云上小院")], total=1, has_next=False))
        at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        frozen = self.call("POST", "/packages", {
            "package_id": "pkg-old", "region": "贵阳", "requested_at": at,
            "policy": policy(categories=("residence",)), "expected_revision": 4,
        })
        # 之后登记了新版模板与新版卡片，但旧包读取仍按当时采到的含义。
        self.call("POST", "/sessions", {"session_id": "hk-002", "source": "partner-map",
            "query": "贵州旅居", "source_revision": 4, "expected_pages": 1,
            "template": "directory-v2"})
        second = page(1, [v2_item("stay-9", "云上小院")],
                      session="hk-002", total=1, has_next=False)
        second["template"] = "directory-v2"
        self.call("POST", "/sessions/hk-002/pages", second)
        reread = self.call("GET", "/packages/pkg-old")
        self.assertEqual(reread, frozen)
        self.assertEqual(reread["template_basis"], ["directory-v1"])

    def test_rebase_forms_new_basis_without_touching_old_capture(self):
        self.open(pages=1)
        self.send_page(page(1, [item("stay-1", "云上小院")], total=1, has_next=False))
        before = self.call("GET", "/sessions/hk-001/pages")["pages"]
        rebase = self.call("POST", "/rebases", {
            "rebase_id": "reread-one", "session_id": "hk-001",
            "target_template": "directory-v2",
        })
        self.assertEqual(rebase["original_template"], "directory-v1")
        self.assertEqual(rebase["target_template"], "directory-v2")
        self.assertFalse(rebase["supersedes_original"])
        self.assertEqual(rebase["not_comparable_fields"], ["service_window"])
        self.assertIn("口径不同", rebase["not_comparable"][0]["reason"])
        # 旧采集逐页不变；重读只落成新依据。
        self.assertEqual(self.call("GET", "/sessions/hk-001/pages")["pages"], before)
        self.assertEqual(self.call("GET", "/rebases/reread-one"), rebase)
        self.assertEqual(self.call("GET", "/sessions/hk-001/rebases")["items"][0]["rebase_id"],
                         "reread-one")
        self.assertEqual(self.call("POST", "/rebases", {
            "rebase_id": "reread-one", "session_id": "hk-001",
            "target_template": "directory-v2"})["replayed"], True)

    def test_template_switch_in_manual_pages_halts_without_mixing(self):
        self.open(pages=2)
        self.send_page(page(1, [item("stay-1", "云上小院")], total=2))
        second = page(2, [v2_item("stay-2", "温泉公寓")], total=2, has_next=False)
        second["template"] = "directory-v2"
        self.send_page(second, status=409)
        report = self.call("GET", "/sessions/hk-001")
        self.assertEqual((report["state"], report["template_transition"]), ("stale", True))
        self.assertEqual(report["received_pages"], 1)
        self.assertEqual(self.call("GET", "/catalog")["items"], [])
        plan = self.call("GET", "/sessions/hk-001/recovery")
        self.assertTrue(plan["recapture_needed"])
        self.assertFalse(plan["common_basis"])
        # 人工结构化页面没有原始 HTML，恢复时只能按结构化凭证复核。
        self.assertFalse(plan["preserved_originals"])
        self.assertIn("stay-1", plan["reusable_items"])

    def test_compat_conflict_blocks_disputed_field_in_coverage(self):
        self.open(pages=1)
        row = v2_item("stay-1", "云上小院")
        row["conflicts"] = {"price_band": {"sides": {"directory-v1": "低",
                                                     "directory-v2": "高"}}}
        row["not_comparable_fields"] = ["service_window"]
        self.send_page(page(1, [row], total=1, has_next=False))
        at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        preview = self.call("POST", "/coverage/preview", params={
            "region": "贵阳", "revision": 4, "at": at}, data=policy(categories=("residence",)))
        finding = preview["categories"]["residence"]["findings"][0]
        self.assertEqual(finding["state"], "disputed")
        self.assertIn("price_band", finding["disputed_fields"])
        self.assertEqual(finding["not_comparable_fields"], ["service_window"])
        reasons = {gap["reason"] for gap in preview["gaps"]}
        self.assertIn("source_conflict", reasons)

