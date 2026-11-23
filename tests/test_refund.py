"""退款抵销测试：单据关联、方向校验、部分/多次抵销、超额拒绝、幂等。"""
import unittest

from fundflow.refund import RefundError, apply_refunds, reset_refund_offsets
from fundflow.models import cents_to_yuan
from tests.factories import add_refund, build_ledger


class TestRefundOffset(unittest.TestCase):

    def test_partial_refund_reduces_remaining(self):
        ledger = build_ledger()
        add_refund(ledger, "r1", "2026-01-08", "B", "A", 4, "t1", "RF1")
        summary = apply_refunds(ledger)
        self.assertEqual(summary["total_offset_cents"], 4)
        t1 = ledger.transactions["t1"]
        self.assertEqual(t1.remaining_cents, 6)
        self.assertEqual(t1.refund_offsets, {"r1": 4})
        self.assertEqual(ledger.refunds["r1"].applied_cents, 4)

    def test_multiple_refunds_same_original(self):
        ledger = build_ledger()
        add_refund(ledger, "r1", "2026-01-08", "B", "A", 3, "t1", "RF1")
        add_refund(ledger, "r2", "2026-01-09", "B", "A", 2, "t1", "RF2")
        apply_refunds(ledger)
        self.assertEqual(ledger.transactions["t1"].remaining_cents, 5)
        self.assertEqual(sum(ledger.transactions["t1"].refund_offsets.values()), 5)

    def test_refund_cannot_exceed_original_amount(self):
        ledger = build_ledger()
        add_refund(ledger, "r1", "2026-01-08", "B", "A", 10, "t1", "RF1")  # 全额
        add_refund(ledger, "r2", "2026-01-09", "B", "A", 1, "t1", "RF2")  # 超额
        with self.assertRaises(RefundError):
            apply_refunds(ledger)
        # 非严格模式：第二笔记录错误，第一笔仍生效
        ledger2 = build_ledger()
        add_refund(ledger2, "r1", "2026-01-08", "B", "A", 10, "t1", "RF1")
        add_refund(ledger2, "r2", "2026-01-09", "B", "A", 1, "t1", "RF2")
        summary = apply_refunds(ledger2, strict=False)
        self.assertTrue(summary["errors"])
        self.assertEqual(ledger2.transactions["t1"].remaining_cents, 0)

    def test_wrong_direction_rejected(self):
        ledger = build_ledger()
        # 原流水 A->B，退款也 A->B（同向，错误）
        add_refund(ledger, "r1", "2026-01-08", "A", "B", 1, "t1", "RF1")
        with self.assertRaises(RefundError):
            apply_refunds(ledger)

    def test_refund_before_original_date_rejected(self):
        ledger = build_ledger()
        add_refund(ledger, "r1", "2025-12-31", "B", "A", 1, "t1", "RF1")
        with self.assertRaises(RefundError):
            apply_refunds(ledger)

    def test_original_doc_mismatch_rejected(self):
        ledger = build_ledger()
        add_refund(ledger, "r1", "2026-01-08", "B", "A", 1, "t1", "RF1",
                   original_doc_ref="WRONG-DOC")
        with self.assertRaises(RefundError):
            apply_refunds(ledger)
        # 不填原单据号则仅以 original_txn_id 关联，允许通过
        ledger2 = build_ledger()
        add_refund(ledger2, "r1", "2026-01-08", "B", "A", 1, "t1", "RF1")
        apply_refunds(ledger2)
        self.assertEqual(ledger2.transactions["t1"].remaining_cents, 9)

    def test_missing_original_txn_rejected(self):
        ledger = build_ledger()
        add_refund(ledger, "r1", "2026-01-08", "B", "A", 1, "tX", "RF1")
        with self.assertRaises(RefundError):
            apply_refunds(ledger)

    def test_idempotent_reapply(self):
        ledger = build_ledger()
        add_refund(ledger, "r1", "2026-01-08", "B", "A", 4, "t1", "RF1")
        apply_refunds(ledger)
        # 重置后再次抵销应得到相同结果
        reset_refund_offsets(ledger)
        self.assertEqual(ledger.transactions["t1"].remaining_cents, 10)
        summary = apply_refunds(ledger)
        self.assertEqual(summary["total_offset_cents"], 4)
        self.assertEqual(ledger.transactions["t1"].remaining_cents, 6)

    def test_full_offset_removes_from_chain_capacity(self):
        ledger = build_ledger()
        add_refund(ledger, "r1", "2026-01-08", "B", "A", 10, "t1", "RF1")
        apply_refunds(ledger)
        self.assertEqual(ledger.transactions["t1"].remaining_cents, 0)
        # 全额抵销的边容量为 0，不应再出现在任何候选链中
        from fundflow.chains import enumerate_chains
        chains = enumerate_chains(ledger)
        self.assertTrue(all("t1" not in c.txn_ids for c in chains))


if __name__ == "__main__":
    unittest.main()
