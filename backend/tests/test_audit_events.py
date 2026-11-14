"""审计事件账本的行为测试：乱序还原、重放幂等、并发争抢、归档一致。

运行：backend/.venv/bin/python -m unittest discover -s backend/tests -v
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services.audit_ledger import STATUS_ORDER, AuditLedger  # noqa: E402


def values(chain):
    return [(item["变更前值"], item["变更后值"]) for item in chain["版本链"]]


class SeedChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = AuditLedger()

    def chain(self, object_no: str, field: str) -> dict:
        return next(c for c in self.ledger.chains(object_no) if c["变更对象"] == field)

    def test_audi_0001_threshold_chain(self) -> None:
        chain = self.chain("AUDI-0001", "参数阈值")
        self.assertEqual(values(chain), [("80", "90"), ("90", "100")])
        self.assertEqual([v["事件序号"] for v in chain["版本链"]], [1, 2])
        self.assertEqual([v["版本序号"] for v in chain["版本链"]], [1, 2])
        self.assertEqual(chain["复核状态"], "已复核")

    def test_audi_0003_owner_chain_out_of_order(self) -> None:
        chain = self.chain("AUDI-0003", "负责人")
        self.assertEqual(values(chain), [("甲", "乙"), ("乙", "丙")])
        self.assertEqual([v["事件序号"] for v in chain["版本链"]], [1, 3])

    def test_audi_0003_review_not_rolled_back(self) -> None:
        result = self.ledger.lifecycle_results("AUDI-0003")[0]
        self.assertEqual(result["复核状态"], "已复核")
        review_chain = self.chain("AUDI-0003", "复核状态")
        self.assertEqual(values(review_chain), [("已记录", "已复核")])

    def test_cata_0003_cross_month_order(self) -> None:
        chain = self.chain("CATA-0003", "负责人")
        self.assertEqual(values(chain), [("甲", "乙"), ("乙", "丙")])
        first = chain["版本链"][0]
        self.assertTrue(first["操作时间"].startswith("2026-08-31"))
        self.assertTrue(first["落库时间"].startswith("2026-09-01"))

    def test_retn_0003_same_second_stable_order(self) -> None:
        chain = self.chain("RETN-0003", "保留期限")
        self.assertEqual(values(chain), [("180", "90"), ("90", "30")])
        keys = [v["稳定排序键"] for v in chain["版本链"]]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(set(keys)), 2)

    def test_object_chain_covers_all_fields(self) -> None:
        links = self.ledger.object_chains("AUDI-0003")
        self.assertEqual([link["事件序号"] for link in links], [1, 2, 3])
        self.assertEqual([link["变更对象"] for link in links], ["负责人", "复核状态", "负责人"])
        # 对象事件链的父节点跨字段接续，而不是各自回到 ROOT
        self.assertEqual([link["父事件序号"] for link in links], [0, 1, 2])
        self.assertEqual([link["父事件指纹"] for link in links], ["ROOT", links[0]["校验码"], links[1]["校验码"]])


class ReplayAndIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = AuditLedger()

    def test_same_event_replay_no_duplicate(self) -> None:
        target = next(
            event for event in self.ledger.history("AUDI-0001") if event["事件序号"] == 1
        )
        before = len(self.ledger.list_events()[0])
        first = self.ledger.replay(target["id"])
        second = self.ledger.replay(target["id"])
        after = len(self.ledger.list_events()[0])
        self.assertTrue(first["ok"] and second["ok"])
        self.assertTrue(first["重放"] and second["重放"])
        self.assertEqual(before, after)
        self.assertEqual(self.ledger.history("AUDI-0001")[0]["变更前值"], "80")
        self.assertEqual(self.ledger.history("AUDI-0001")[0]["复核状态"], "已记录")
        self.assertEqual(self.ledger.history("AUDI-0001")[0]["操作时间"], "2026-09-01T09:15:00")

    def test_replay_with_different_conclusion_rejected(self) -> None:
        target = next(
            event for event in self.ledger.history("AUDI-0001") if event["事件序号"] == 1
        )
        before_event = dict(target)
        result = self.ledger.replay(target["id"], {"变更后值": "999", "复核状态": "已复核"})
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("冲突"))
        after_event = self.ledger.get_event(target["id"])
        self.assertEqual(after_event["变更后值"], before_event["变更后值"])
        self.assertEqual(after_event["复核状态"], before_event["复核状态"])

    def test_duplicate_merge_within_window(self) -> None:
        ledger = AuditLedger(seed=False, merge_window_seconds=120)
        base = {
            "来源模块编号": "catalog",
            "业务对象编号": "CATA-9001",
            "业务对象主键": 1,
            "变更对象": "负责人",
            "操作人": "甲",
            "操作时间": "2026-09-01T10:00:00",
            "复核状态": "已记录",
        }
        ledger.ingest({**base, "变更前值": "A", "变更后值": "B", "事件序号": 1, "幂等键": "k1"})
        ledger.ingest({**base, "变更前值": "A", "变更后值": "B", "事件序号": 1, "幂等键": "k2",
                       "落库时间": "2026-09-01T10:01:00"})
        events, _ = ledger.list_events(object_no="CATA-9001")
        self.assertEqual(len(events), 1)
        self.assertGreaterEqual(events[0]["合并计数"], 2)

    def test_conflicting_slot_takes_next_version(self) -> None:
        ledger = AuditLedger(seed=False)
        base = {
            "来源模块编号": "catalog",
            "业务对象编号": "CATA-9002",
            "业务对象主键": 1,
            "变更对象": "负责人",
            "操作人": "甲",
            "操作时间": "2026-09-01T10:00:00",
        }
        first = ledger.ingest({**base, "变更前值": "A", "变更后值": "B", "事件序号": 1,
                               "复核状态": "已记录", "幂等键": "c1"})
        second = ledger.ingest({**base, "变更前值": "X", "变更后值": "Y", "事件序号": 1,
                                "复核状态": "已复核", "幂等键": "c2",
                                "落库时间": "2026-12-31T23:00:00"})
        self.assertTrue(first["ok"] and second["ok"])
        self.assertTrue(second["event"]["冲突后补版本"])
        self.assertEqual(second["event"]["事件序号"], 2)
        chain = ledger.chains("CATA-9002")[0]
        self.assertEqual(values(chain), [("A", "B"), ("X", "Y")])


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_same_slot_only_one_wins(self) -> None:
        ledger = AuditLedger(seed=False)
        base = {
            "来源模块编号": "audit",
            "业务对象编号": "AUDI-7777",
            "业务对象主键": 99,
            "变更对象": "参数阈值",
            "操作人": "并发",
            "操作时间": "2026-09-01T10:00:00",
        }
        results: list[dict] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            result = ledger.ingest({
                **base,
                "变更前值": f"before-{index}",
                "变更后值": f"after-{index}",
                "事件序号": 1,
                "复核状态": "已复核",
                "幂等键": f"race-{index}",
                "落库时间": "2026-09-01T10:00:00",
            })
            with lock:
                results.append(result)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(ledger.chains("AUDI-7777")), 1)
        chain = ledger.chains("AUDI-7777")[0]
        self.assertEqual(len(chain["版本链"]), 8)
        seqs = [item["事件序号"] for item in chain["版本链"]]
        self.assertEqual(seqs, list(range(1, 9)))
        self.assertEqual(len({result["event"]["事件编号"] for result in results}), 8)


class ArchiveTests(unittest.TestCase):
    def test_archive_keeps_chain_readable(self) -> None:
        ledger = AuditLedger()
        before = ledger.chains("CATA-0003")[0]
        result = ledger.archive("2026-08")
        self.assertTrue(result["ok"])
        after = ledger.chains("CATA-0003")[0]
        self.assertEqual(values(before), values(after))
        self.assertTrue(after["版本链"][0]["已归档"])
        self.assertEqual(after["版本链"][0]["归档批次"], "ARCH-2026-08")
        events, _ = ledger.list_events(object_no="CATA-0003")
        self.assertTrue(all(event["已归档"] for event in events if event["归档期"] == "2026-08"))


class SummaryTests(unittest.TestCase):
    def test_summary_fields(self) -> None:
        ledger = AuditLedger()
        event = next(e for e in ledger.history("AUDI-0001") if e["事件序号"] == 1)
        summary = event["摘要"]
        for key in ("对象编号", "版本序号", "原值", "新值", "复核人", "落库时间", "入口标记"):
            self.assertIn(key, summary)
        self.assertTrue(summary["只读"])
        self.assertTrue(summary["只读校验戳"].startswith("SEAL-"))
        for key in ("来源模块编号", "业务对象主键", "父事件序号", "幂等键", "写入进程标识", "落库批次号"):
            self.assertIn(key, event)
        for key in ("本地序列号", "父事件指纹", "幂等键摘要", "落库批次戳", "校验码"):
            self.assertIn(key, event)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)

    def test_review_action_extends_review_chain(self) -> None:
        """AUDI-0002 复核：新增事件的变更前值必须接上对象当前复核状态。"""
        client = TestClient(app)
        events = client.get("/api/audit", params={"object_no": "AUDI-0002"}).json()["items"]
        target = events[0]
        before_status = client.get(
            "/api/audit/results", params={"object_no": "AUDI-0002"}
        ).json()["items"][0]["复核状态"]
        response = client.post(
            f"/api/audit/{target['id']}/actions", json={"values": {"action": "标记复核"}}
        ).json()
        self.assertTrue(response["ok"])
        review_chain = next(
            chain for chain in client.get(
                "/api/audit/chains", params={"object_no": "AUDI-0002"}
            ).json()["items"]
            if chain["变更对象"] == "复核状态"
        )
        first = review_chain["版本链"][0]
        self.assertEqual(first["变更前值"], before_status)
        self.assertEqual(first["变更后值"], "已复核")

    def test_audit_list_returns_event_chain_fields(self) -> None:
        response = self.client.get("/api/audit", params={"size": 200})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertGreater(payload["total"], 0)
        first = payload["items"][0]
        for key in ("记录编号", "事件序号", "版本序号", "父事件序号", "稳定排序键", "复核状态"):
            self.assertIn(key, first)

    def test_audit_flow_and_chains(self) -> None:
        response = self.client.get("/api/audit/chains", params={"object_no": "AUDI-0003"})
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        owner = next(item for item in items if item["变更对象"] == "负责人")
        self.assertEqual(values(owner), [("甲", "乙"), ("乙", "丙")])

    def test_catalog_row_shows_source_event(self) -> None:
        response = self.client.get("/api/catalog")
        self.assertEqual(response.status_code, 200)
        row = next(item for item in response.json()["items"] if item["资产编号"] == "CATA-0003")
        self.assertIn("来源事件", row)
        self.assertEqual(row["入口标记"], "资产模块入口")
        self.assertEqual(row["复核状态"], "已复核")

    def test_retention_chain(self) -> None:
        response = self.client.get("/api/audit/chains", params={"object_no": "RETN-0003"})
        items = response.json()["items"]
        chain = next(item for item in items if item["变更对象"] == "保留期限")
        self.assertEqual(values(chain), [("180", "90"), ("90", "30")])

    def test_audit_action_does_not_roll_back(self) -> None:
        client = TestClient(app)
        events = client.get("/api/audit", params={"size": 200}).json()["items"]
        target = next(item for item in events if item["记录编号"] == "AUDI-0003")
        response = client.post(f"/api/audit/{target['id']}/actions", json={"values": {"action": "标记复核"}})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertIn("不会把状态倒回", payload["message"])
        self.assertEqual(payload["entry"]["复核状态"], "已复核")

    def test_archive_endpoint(self) -> None:
        client = TestClient(app)
        response = client.post("/api/audit/archive", json={"values": {"archive_period": "2026-08"}})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        chains = client.get("/api/audit/chains", params={"object_no": "CATA-0003"}).json()["items"]
        owner = next(item for item in chains if item["变更对象"] == "负责人")
        self.assertEqual(values(owner), [("甲", "乙"), ("乙", "丙")])


if __name__ == "__main__":
    unittest.main()
