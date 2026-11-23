"""测试共用的小容量台账构造工具（整数容量，便于穷举）。"""
from __future__ import annotations

from fundflow.models import ChainConfig, Ledger, RefundRecord, Transaction


def make_txn(txn_id, date, frm, to, amount_cents, doc_ref=None,
             memo="", is_refund=False) -> Transaction:
    t = Transaction(
        txn_id=txn_id, date=date, from_account=frm, to_account=to,
        amount_cents=amount_cents, doc_ref=doc_ref, memo=memo,
        is_refund=is_refund,
    )
    t.remaining_cents = amount_cents
    return t


def build_ledger() -> Ledger:
    """构造一个确定的小台账：

    A --t1(10)--> B --t2(8)--> C --t3(6)--> A   （三角环，受 t3 容量 6 限制）
    A --t4(5)---> D --t5(5)---> B --t6(4)--> A （四环，t1/t6 与三角环共用边）
    B --t7(3)--> A                               （与 t1 构成短环）
    """
    ledger = Ledger()
    rows = [
        ("t1", "2026-01-01", "A", "B", 10, "D1"),
        ("t2", "2026-01-02", "B", "C", 8, "D2"),
        ("t3", "2026-01-03", "C", "A", 6, "D3"),
        ("t4", "2026-01-04", "A", "D", 5, "D4"),
        ("t5", "2026-01-05", "D", "B", 5, "D5"),
        ("t6", "2026-01-06", "B", "A", 4, "D6"),
        ("t7", "2026-01-07", "B", "A", 3, "D7"),
    ]
    for txn_id, d, frm, to, amt, doc in rows:
        ledger.transactions[txn_id] = make_txn(txn_id, d, frm, to, amt, doc)
    ledger.config = ChainConfig(
        max_span_days=30, max_chain_edges=6, min_chain_edges=2,
        strict_time=False, max_candidates=500,
    )
    return ledger


def add_refund(ledger: Ledger, refund_id, date, frm, to, amount,
               original_id, doc_ref, original_doc_ref=None) -> RefundRecord:
    r = RefundRecord(
        refund_id=refund_id, date=date, from_account=frm, to_account=to,
        amount_cents=amount, original_txn_id=original_id, doc_ref=doc_ref,
        original_doc_ref=original_doc_ref,
    )
    ledger.refunds[refund_id] = r
    return r
