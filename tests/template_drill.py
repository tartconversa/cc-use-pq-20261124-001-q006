"""黔居采集台：模板版本升级的可演练流程。

运行：python -m tests.template_drill
使用临时数据库与仓库内合成页面，演示：
1. 旧版页面已收、下一次查询中途切换为新版模板时中止而不丢页；
2. 记录旧页原件、可复用的相同供给事实与需重采的页面；
3. 用同一范围的新模板完整会话补采后恢复；
4. 按新口径重读历史只形成新依据，不改旧采集与旧封存包。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from guizhou_stay import collector, coverage, rebase, recovery, verification
from guizhou_stay.contracts import CoveragePolicy, PublishRequest
from guizhou_stay.portal import FolderReader
from guizhou_stay.storage import Store

ROOT = Path(__file__).resolve().parents[1] / "fixtures"
OLD = ROOT / "partner-switch"
NEW = ROOT / "partner-match"
AT = datetime(2026, 11, 24, tzinfo=timezone.utc)


def step(store: Store, session: str, folder: Path, limit: int) -> dict:
    return collector.run(store, session, folder.resolve().as_uri(),
                         FolderReader(folder), limit)


def main() -> None:
    with TemporaryDirectory() as directory:
        store = Store(Path(directory) / "drill.sqlite3")

        # 1. 旧版第一页收下后，来源在第二页切到新版模板。
        step(store, "run-2026-11", OLD, 1)
        halted = step(store, "run-2026-11", OLD, 1)
        assert halted["state"] == "stale" and halted["template_transition"]
        assert halted["received_pages"] == 1
        with store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM supplies").fetchone()[0] == 0
            audit = verification.review(db, "run-2026-11")
            assert audit["valid"] and audit["template"] == "directory-v1"
            assert audit["interpretations"][0]["window_code"] == "unspecified-bookable"
            inventory = recovery.page_inventory(db, "run-2026-11")
            assert inventory["all_originals_preserved"]
        print("1. 切换中止：旧页保留、原件保留、未出版旧数据")

        # 2. 未补采前不能拼出全量结果，并给出需要重采的页码。
        with store.transaction() as db:
            pending = recovery.plan(db, "run-2026-11")
        assert pending["recapture_needed"] and not pending["common_basis"]
        print("2. 未补采：", pending["recapture_pages"])

        # 3. 同一范围用新版模板完整补采，再核对共同业务解释。
        step(store, "run-2026-12", NEW, 5)
        with store.transaction() as db:
            report = recovery.assemble(db, "run-2026-11", successor="run-2026-12")
        assert report["common_basis"] and report["preserved_originals"]
        assert report["duplicate_views_not_counted"] == 2
        for entry in report["cross_template"]:
            assert entry["differing_fields"] == []
            assert entry["not_comparable_fields"] == ["service_window"]
        print("3. 补采后：可复用", report["reusable_items"],
              "；同口径可证相同", len(report["same_supply_facts_provable"]))

        # 4. 按新口径重读旧历史，形成独立依据并列出不可对照字段。
        with store.transaction(write=True) as db:
            reread = rebase.record(db, rebase.RebaseRequest(
                rebase_id="reread-2026-11", session_id="run-2026-11",
                target_template="directory-v2"))
        assert reread["not_comparable_fields"] == ["service_window"]
        assert reread["supersedes_original"] is False
        print("4. 重读依据：", reread["original_template"], "->",
              reread["target_template"], "不可对照", reread["not_comparable_fields"])

        # 5. 签发：每条数据都带上所采用的模板与字段口径。
        policy = CoveragePolicy(required_categories=["residence"],
                                require_verified_evidence=False)
        with store.transaction(write=True) as db:
            package = coverage.create_package(db, PublishRequest(
                package_id="drill-package", region="贵阳", policy=policy,
                requested_at=AT, expected_revision=6))
        assert package["state"] == "published"
        assert package["template_basis"] == ["directory-v2"]
        assert {value["window_code"] for value in package["interpretations"]} <= {
            "all-day", "night-duty"}
        print("5. 签发：", package["template_basis"],
              [(value["item_id"], value["window_code"])
               for value in package["interpretations"]])

    print("模板升级演练通过：切换中止、原件保留、可证事实、补采恢复、新依据重读与签发口径均可核对")


if __name__ == "__main__":
    main()
