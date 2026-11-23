"""核心数据模型：收支明细、退款、候选闭合链、分配方案与解版本。

金额内部一律用「整数分」表示，避免浮点误差；对外展示时再换算成元。
所有数据均为虚构收支明细，系统不连接任何银行账户。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Optional


# ---------------------------------------------------------------------------
# 金额工具
# ---------------------------------------------------------------------------

def to_cents(amount) -> int:
    """把元（float/str/int）换算为整数分。

    约定：JSON 中 ``amount`` 字段单位为元（可两位小数），整数输入同样按元处理；
    内部所有计算统一使用分。
    """
    if isinstance(amount, bool):
        raise TypeError("布尔值不是合法金额")
    if isinstance(amount, int):
        return amount * 100
    if isinstance(amount, float):
        # round 到最近的分，消除二进制浮点误差
        return int(round(amount * 100))
    if isinstance(amount, str):
        return int(round(float(amount) * 100))
    raise TypeError(f"不支持的金额类型: {type(amount)!r}")


def cents_to_yuan(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def parse_date(s: str) -> date:
    return date.fromisoformat(s)


# ---------------------------------------------------------------------------
# 明细与退款
# ---------------------------------------------------------------------------

@dataclass
class Transaction:
    """一笔虚构的资金流水（有向边：from_account -> to_account）。"""

    txn_id: str
    date: str                      # ISO 日期 YYYY-MM-DD
    from_account: str
    to_account: str
    amount_cents: int              # 原始金额（分）
    doc_ref: Optional[str] = None  # 业务单据号
    memo: str = ""
    # 抵销退款后剩余可参与链条分配的金额（分）
    remaining_cents: int = 0
    # 已被各条链分配的金额（分），由 service/solver 维护
    allocated_cents: int = 0
    # 被人工排除的业务边不参与任何链
    excluded: bool = False
    # 退款流水（已通过 refund 模块抵销）不参与闭合链枚举
    is_refund: bool = False
    # 抵销明细：refund_id -> 抵销金额（分）
    refund_offsets: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        if self.remaining_cents == 0 and not self.refund_offsets:
            self.remaining_cents = self.amount_cents

    @property
    def available_cents(self) -> int:
        """当前还能被分配的额度。"""
        return self.remaining_cents - self.allocated_cents

    def to_dict(self) -> dict:
        d = asdict(self)
        d["amount_yuan"] = cents_to_yuan(self.amount_cents)
        d["remaining_yuan"] = cents_to_yuan(self.remaining_cents)
        d["allocated_yuan"] = cents_to_yuan(self.allocated_cents)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Transaction":
        return cls(
            txn_id=d["txn_id"],
            date=d["date"],
            from_account=d["from_account"],
            to_account=d["to_account"],
            amount_cents=d.get("amount_cents", to_cents(d["amount"])),
            doc_ref=d.get("doc_ref"),
            memo=d.get("memo", ""),
            remaining_cents=d.get("remaining_cents", 0),
            allocated_cents=d.get("allocated_cents", 0),
            excluded=d.get("excluded", False),
            is_refund=d.get("is_refund", False),
            refund_offsets=dict(d.get("refund_offsets", {})),
        )


@dataclass
class RefundRecord:
    """一笔退款流水，必须凭业务单据号关联原收款。"""

    refund_id: str
    date: str
    from_account: str
    to_account: str
    amount_cents: int
    original_txn_id: str           # 被退款的原流水 id
    doc_ref: Optional[str] = None  # 退款凭证自身的单据号
    original_doc_ref: Optional[str] = None  # 原单据号（提供时强制一致性校验）
    memo: str = ""
    applied_cents: int = 0         # 已抵销金额

    @property
    def unapplied_cents(self) -> int:
        return self.amount_cents - self.applied_cents

    def to_dict(self) -> dict:
        d = asdict(self)
        d["amount_yuan"] = cents_to_yuan(self.amount_cents)
        d["applied_yuan"] = cents_to_yuan(self.applied_cents)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RefundRecord":
        return cls(
            refund_id=d["refund_id"],
            date=d["date"],
            from_account=d["from_account"],
            to_account=d["to_account"],
            amount_cents=d.get("amount_cents", to_cents(d["amount"])),
            original_txn_id=d["original_txn_id"],
            doc_ref=d.get("doc_ref"),
            original_doc_ref=d.get("original_doc_ref"),
            memo=d.get("memo", ""),
            applied_cents=d.get("applied_cents", 0),
        )


# ---------------------------------------------------------------------------
# 候选闭合链与分配
# ---------------------------------------------------------------------------

@dataclass
class CandidateChain:
    """一条候选闭合往来链：按时间顺序排列的流水 id 序列，首尾账户闭合。

    例如 [t1, t2, t3] 表示 t1 的收款方即 t2 的付款方……t3 最终回到
    t1 的付款方，且 date(t1) <= date(t2) <= date(t3)。
    """

    chain_id: str
    txn_ids: list[str]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CandidateChain":
        return cls(chain_id=d["chain_id"], txn_ids=list(d["txn_ids"]))


@dataclass
class ChainAllocation:
    """一条链在某个解释方案中的使用金额与锁定状态。"""

    chain_id: str
    amount_cents: int
    locked: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["amount_yuan"] = cents_to_yuan(self.amount_cents)
        return d


@dataclass
class SolutionRecord:
    """一个完整的解释方案（多解之一）。

    重要语义：算法分解出的链条只是资金关系的一种数学解释，
    不构成任何违法事实认定。
    """

    solution_id: str
    objective: str                 # "max_closure" | "min_chains"
    allocations: list[ChainAllocation]
    total_closed_cents: int
    chain_count: int
    confirmed: bool = False        # 人工确认后不再被后台重算覆盖
    note: str = ""
    # 生成该方案时的排除边集合与快照，用于解释与冲突判断
    excluded_txn_ids: list[str] = field(default_factory=list)
    locked_chain_ids: list[str] = field(default_factory=list)
    version: int = 1

    def to_dict(self) -> dict:
        return {
            "solution_id": self.solution_id,
            "objective": self.objective,
            "allocations": [a.to_dict() for a in self.allocations],
            "total_closed_cents": self.total_closed_cents,
            "total_closed_yuan": cents_to_yuan(self.total_closed_cents),
            "chain_count": self.chain_count,
            "confirmed": self.confirmed,
            "note": self.note,
            "excluded_txn_ids": list(self.excluded_txn_ids),
            "locked_chain_ids": list(self.locked_chain_ids),
            "version": self.version,
        }


# ---------------------------------------------------------------------------
# 配置与台账
# ---------------------------------------------------------------------------

@dataclass
class ChainConfig:
    """链条枚举与账户限制配置。"""

    max_span_days: int = 30            # 链上首末流水最大时间跨度
    max_chain_edges: int = 8           # 单条链最多使用几笔流水
    min_chain_edges: int = 2
    strict_time: bool = False          # True=日期必须严格递增；False=允许同日
    blocked_accounts: set[str] = field(default_factory=set)
    allowed_accounts: Optional[set[str]] = None  # 白名单优先；None 表示不限制
    max_candidates: int = 5000

    def to_dict(self) -> dict:
        return {
            "max_span_days": self.max_span_days,
            "max_chain_edges": self.max_chain_edges,
            "min_chain_edges": self.min_chain_edges,
            "strict_time": self.strict_time,
            "blocked_accounts": sorted(self.blocked_accounts),
            "allowed_accounts": (
                sorted(self.allowed_accounts)
                if self.allowed_accounts is not None else None
            ),
            "max_candidates": self.max_candidates,
        }


@dataclass
class Ledger:
    """完整台账：流水 + 退款 + 配置。"""

    transactions: dict[str, Transaction] = field(default_factory=dict)
    refunds: dict[str, RefundRecord] = field(default_factory=dict)
    config: ChainConfig = field(default_factory=ChainConfig)

    # ---- 访问辅助 ----
    def txns_sorted(self) -> list[Transaction]:
        return sorted(self.transactions.values(), key=lambda t: (t.date, t.txn_id))

    def accounts(self) -> set[str]:
        out: set[str] = set()
        for t in self.transactions.values():
            out.add(t.from_account)
            out.add(t.to_account)
        return out

    def copy(self) -> "Ledger":
        return Ledger(
            transactions={k: _clone_tx(v) for k, v in self.transactions.items()},
            refunds={k: RefundRecord(**asdict(v)) for k, v in self.refunds.items()},
            config=ChainConfig(
                max_span_days=self.config.max_span_days,
                max_chain_edges=self.config.max_chain_edges,
                min_chain_edges=self.config.min_chain_edges,
                strict_time=self.config.strict_time,
                blocked_accounts=set(self.config.blocked_accounts),
                allowed_accounts=(
                    set(self.config.allowed_accounts)
                    if self.config.allowed_accounts is not None else None
                ),
                max_candidates=self.config.max_candidates,
            ),
        )

    # ---- JSON 读写 ----
    def to_dict(self) -> dict:
        return {
            "transactions": [t.to_dict() for t in self.txns_sorted()],
            "refunds": [r.to_dict() for r in sorted(self.refunds.values(),
                                                    key=lambda r: r.refund_id)],
            "config": self.config.to_dict(),
        }

    def to_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> "Ledger":
        ledger = cls()
        for td in d.get("transactions", []):
            t = Transaction.from_dict(td)
            ledger.transactions[t.txn_id] = t
        for rd in d.get("refunds", []):
            r = RefundRecord.from_dict(rd)
            ledger.refunds[r.refund_id] = r
        if "config" in d:
            cd = d["config"]
            ledger.config = ChainConfig(
                max_span_days=cd.get("max_span_days", 30),
                max_chain_edges=cd.get("max_chain_edges", 8),
                min_chain_edges=cd.get("min_chain_edges", 2),
                strict_time=cd.get("strict_time", False),
                blocked_accounts=set(cd.get("blocked_accounts", [])),
                allowed_accounts=(
                    set(cd["allowed_accounts"])
                    if cd.get("allowed_accounts") is not None else None
                ),
                max_candidates=cd.get("max_candidates", 5000),
            )
        return ledger

    @classmethod
    def from_json(cls, path: str) -> "Ledger":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


def _clone_tx(t: Transaction) -> Transaction:
    return Transaction(
        txn_id=t.txn_id,
        date=t.date,
        from_account=t.from_account,
        to_account=t.to_account,
        amount_cents=t.amount_cents,
        doc_ref=t.doc_ref,
        memo=t.memo,
        remaining_cents=t.remaining_cents,
        allocated_cents=t.allocated_cents,
        excluded=t.excluded,
        is_refund=t.is_refund,
        refund_offsets=dict(t.refund_offsets),
    )
