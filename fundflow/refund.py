"""按业务单据抵销明确的退款。

规则（全部显式校验，拒绝静默猜测）：
1. 退款必须通过 ``original_txn_id`` 指向一笔真实流水；若提供 ``doc_ref``，
   还必须与原流水的单据号一致。
2. 退款账户方向必须与原流水相反（原付款方收回款、原收款方退付款）。
3. 退款日期不得早于原流水日期。
4. 同一笔退款可分次应用，但累计抵销额不得超过退款金额；
   同一原流水累计被抵销额不得超过其原始金额。
5. 抵销后原流水的 ``remaining_cents`` 减少，并记录抵销明细。
"""
from __future__ import annotations

from .models import Ledger, RefundRecord, Transaction, parse_date, cents_to_yuan


class RefundError(ValueError):
    """退款抵销校验失败。"""


def _check_refund(refund: RefundRecord, original: Transaction) -> None:
    # 单据校验：原流水必须有业务单据号；若退款显式记录了原单据号则必须一致
    # （退款凭证自身的 doc_ref 与原单据号允许不同，关联以 original_txn_id 为准）
    if original.doc_ref is None:
        raise RefundError(
            f"原流水 {original.txn_id} 没有业务单据号，无法凭单据抵销"
        )
    if (refund.original_doc_ref is not None
            and refund.original_doc_ref != original.doc_ref):
        raise RefundError(
            f"退款 {refund.refund_id} 标注的原单据号 "
            f"{refund.original_doc_ref!r} 与原流水 {original.txn_id} 单据号 "
            f"{original.doc_ref!r} 不一致"
        )
    # 方向相反
    if (refund.from_account != original.to_account
            or refund.to_account != original.from_account):
        raise RefundError(
            f"退款 {refund.refund_id} 账户方向与原流水 {original.txn_id} 不相反："
            f"{refund.from_account}->{refund.to_account} vs "
            f"{original.from_account}->{original.to_account}"
        )
    # 时间合理
    if parse_date(refund.date) < parse_date(original.date):
        raise RefundError(
            f"退款 {refund.refund_id} 日期 {refund.date} 早于原流水日期 "
            f"{original.date}"
        )


def apply_refunds(ledger: Ledger, *, strict: bool = True) -> dict:
    """把台账中的全部退款抵销到对应原流水上。

    幂等：重复执行会基于「未应用余额」继续抵销；已全额应用的退款跳过。
    返回抵销汇总（便于网页与测试展示）。
    """
    summary = {
        "applied": [],          # (refund_id, txn_id, cents)
        "errors": [],
        "total_offset_cents": 0,
    }

    for refund in sorted(ledger.refunds.values(), key=lambda r: (r.date, r.refund_id)):
        original = ledger.transactions.get(refund.original_txn_id)
        if original is None:
            msg = f"退款 {refund.refund_id} 指向不存在的原流水 {refund.original_txn_id}"
            if strict:
                raise RefundError(msg)
            summary["errors"].append(msg)
            continue

        try:
            _check_refund(refund, original)
        except RefundError as exc:
            if strict:
                raise
            summary["errors"].append(str(exc))
            continue

        # 剩余可抵销量受退款余额与原流水剩余可抵销额度双向约束
        room_on_refund = refund.unapplied_cents
        already_offset = sum(original.refund_offsets.values())
        room_on_original = original.amount_cents - already_offset
        if room_on_refund <= 0:
            continue
        if room_on_original <= 0:
            msg = (f"原流水 {original.txn_id}（{cents_to_yuan(original.amount_cents)} 元）"
                   f"已被退款全额抵销，退款 {refund.refund_id} 无法继续抵销")
            if strict:
                raise RefundError(msg)
            summary["errors"].append(msg)
            continue

        applied = min(room_on_refund, room_on_original)
        refund.applied_cents += applied
        original.refund_offsets[refund.refund_id] = (
            original.refund_offsets.get(refund.refund_id, 0) + applied
        )
        original.remaining_cents = original.amount_cents - sum(
            original.refund_offsets.values()
        )
        summary["applied"].append((refund.refund_id, original.txn_id, applied))
        summary["total_offset_cents"] += applied

    return summary


def reset_refund_offsets(ledger: Ledger) -> None:
    """撤销全部抵销（用于重算前恢复台账）。"""
    for t in ledger.transactions.values():
        t.refund_offsets.clear()
        t.remaining_cents = t.amount_cents
    for r in ledger.refunds.values():
        r.applied_cents = 0
