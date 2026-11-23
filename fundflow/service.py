"""分析服务：把退款抵销、链枚举、ILP 求解组织成可交互的状态机。

职责与关键语义：
- 人工排除业务边后，受影响资金自动重新分配；
- 锁定的链分配以等式约束参与重算，算法不得改动其金额；
- 重算只产生新的 **draft** 解版本；已确认（confirmed）的解永久保留，
  不会被后台重算覆盖；
- 全部状态变更在同一把可重入锁内完成，支持并发网页请求与压力测试。
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

from .chains import enumerate_chains
from .models import (
    CandidateChain,
    ChainAllocation,
    Ledger,
    SolutionRecord,
    cents_to_yuan,
)
from .refund import apply_refunds, reset_refund_offsets
from .solver import (
    InfeasibleError,
    LPError,
    solve_max_closure,
    solve_min_chains,
)


class ServiceError(RuntimeError):
    pass


class ConflictError(ServiceError):
    """操作与现有锁定 / 排除约束冲突，HTTP 层映射为 409。"""

    def __init__(self, message: str, conflicts: list[dict]):
        super().__init__(message)
        self.conflicts = conflicts


@dataclass
class ServiceState:
    """线程内快照（只读视图，供网页与测试使用）。"""

    ledger: Ledger
    chains: list[CandidateChain]
    drafts: list[SolutionRecord]
    confirmed: list[SolutionRecord]
    locks: dict[str, int]                     # chain_id -> 锁定金额（分）
    excluded_txn_ids: list[str]
    conflicts: list[dict]
    refund_errors: list[str]
    objective: str
    version: int


class AnalysisService:
    def __init__(self, ledger: Ledger):
        self._mu = threading.RLock()
        self._ledger = ledger
        self._chains: list[CandidateChain] = []
        self._drafts: list[SolutionRecord] = []
        self._confirmed: list[SolutionRecord] = []
        self._locks: dict[str, int] = {}       # chain_id -> cents
        self._objective = "max_closure"
        self._max_solutions = 3
        self._version = 0
        self._refund_summary: dict = {}
        self._conflicts: list[dict] = []
        # 首次构建
        self._rebuild_locked()

    # ------------------------------------------------------------------
    # 内部构建
    # ------------------------------------------------------------------
    def _rebuild_locked(self):
        """重算的完整流程（调用方已持锁）。"""
        # 1) 退款抵销（先重置保证幂等）。非严格模式：坏退款记入 errors 暴露给
        #    前端与测试，不阻断对其余合规退款与资金链的分析
        reset_refund_offsets(self._ledger)
        self._refund_summary = apply_refunds(self._ledger, strict=False)

        # 2) 候选链（排除边不参与）
        self._chains = enumerate_chains(self._ledger)

        # 3) 容量 = 抵销后的剩余额度（排除边与退款流水不参与）
        capacities = {
            t.txn_id: t.remaining_cents
            for t in self._ledger.transactions.values()
            if not t.excluded and not t.is_refund and t.remaining_cents > 0
        }

        # 4) 冲突检测：锁定链是否仍可满足
        conflicts = self._detect_conflicts(capacities)
        self._conflicts = conflicts
        blocking = [c for c in conflicts if c["severity"] == "hard"]
        if blocking:
            # 有硬冲突时保留旧草稿，等待人工处理
            self._version += 1
            return

        # 5) 求解（锁定作为等式约束）。任何不可行都转成硬冲突并保留旧草稿，
        #    由人工解除锁或恢复边后自动恢复，不在写操作路径上抛异常
        if not self._chains:
            self._drafts = []
            self._version += 1
            return
        try:
            if self._objective == "max_closure":
                res = solve_max_closure(
                    self._chains, capacities, locks=self._locks or None,
                    max_solutions=self._max_solutions,
                )
            else:
                res = solve_min_chains(
                    self._chains, capacities, locks=self._locks or None,
                    max_solutions=self._max_solutions,
                )
        except (InfeasibleError, LPError) as exc:
            self._conflicts.append({
                "type": "infeasible",
                "severity": "hard",
                "message": f"当前锁定约束下无可行分配: {exc}",
            })
            self._version += 1
            return

        # 6) 生成新的 draft 版本（confirmed 不动）
        excluded = sorted(t.txn_id for t in self._ledger.transactions.values()
                          if t.excluded)
        self._drafts = []
        for k, sol in enumerate(res.solutions):
            self._drafts.append(SolutionRecord(
                solution_id=f"sol-{uuid.uuid4().hex[:10]}",
                objective=res.objective,
                allocations=list(sol.allocations),
                total_closed_cents=sol.total_closed_cents,
                chain_count=sol.chain_count,
                confirmed=False,
                note=res.note,
                excluded_txn_ids=excluded,
                locked_chain_ids=sorted(self._locks),
                version=self._version + 1,
            ))
        self._version += 1

    def _detect_conflicts(self, capacities: dict[str, int]) -> list[dict]:
        conflicts: list[dict] = []
        chain_index = {c.chain_id: c for c in self._chains}
        for cid, amt in sorted(self._locks.items()):
            ch = chain_index.get(cid)
            if ch is None:
                conflicts.append({
                    "type": "stale_lock",
                    "severity": "hard",
                    "chain_id": cid,
                    "message": f"锁定链 {cid} 已不在候选集中（其边可能被排除）",
                })
                continue
            cap = min((capacities.get(t, 0) for t in ch.txn_ids), default=0)
            if amt > cap:
                conflicts.append({
                    "type": "lock_over_capacity",
                    "severity": "hard",
                    "chain_id": cid,
                    "locked_cents": amt,
                    "capacity_cents": cap,
                    "message": (f"锁定链 {cid} 金额 {cents_to_yuan(amt)} 元超过"
                                f"当前可行额度 {cents_to_yuan(cap)} 元"),
                })
        # 软冲突：被人工排除的边上仍有（旧草稿的）分配
        draft_ids = {a.chain_id for d in self._drafts for a in d.allocations}
        for t in self._ledger.transactions.values():
            if t.excluded:
                for cid in sorted(draft_ids):
                    ch = chain_index.get(cid)
                    if ch and t.txn_id in ch.txn_ids:
                        conflicts.append({
                            "type": "excluded_in_use",
                            "severity": "soft",
                            "txn_id": t.txn_id,
                            "chain_id": cid,
                            "message": (f"业务边 {t.txn_id} 已被人工排除，"
                                        f"旧草稿链 {cid} 曾使用该边，已在重算中移除"),
                        })
        return conflicts

    # ------------------------------------------------------------------
    # 对外操作
    # ------------------------------------------------------------------
    def set_objective(self, objective: str) -> None:
        if objective not in ("max_closure", "min_chains"):
            raise ServiceError("objective 只能是 max_closure 或 min_chains")
        with self._mu:
            self._objective = objective
            self._rebuild_locked()

    def exclude_edge(self, txn_id: str) -> None:
        """人工排除一条业务边，并重新分配受影响资金。"""
        with self._mu:
            t = self._ledger.transactions.get(txn_id)
            if t is None:
                raise ServiceError(f"流水 {txn_id} 不存在")
            t.excluded = True
            self._rebuild_locked()

    def include_edge(self, txn_id: str) -> None:
        """恢复一条被排除的业务边。"""
        with self._mu:
            t = self._ledger.transactions.get(txn_id)
            if t is None:
                raise ServiceError(f"流水 {txn_id} 不存在")
            t.excluded = False
            self._rebuild_locked()

    def lock_chain(self, chain_id: str, amount_cents: int) -> None:
        """锁定一条链的分配金额（等式约束）。

        金额必须为正整数分，且不超过该链当前最小流水容量；
        重复锁定同一链视为修改金额。
        """
        with self._mu:
            if amount_cents <= 0:
                raise ServiceError("锁定金额必须为正整数（分）")
            ch = next((c for c in self._chains if c.chain_id == chain_id), None)
            if ch is None:
                raise ConflictError(
                    f"链 {chain_id} 不在当前候选集中，无法锁定",
                    [{"type": "stale_lock", "severity": "hard",
                      "chain_id": chain_id,
                      "message": "候选集中不存在该链（可能含被排除的边）"}],
                )
            cap = min(self._ledger.transactions[t].remaining_cents
                      for t in ch.txn_ids)
            if amount_cents > cap:
                raise ConflictError(
                    f"锁定金额超过链 {chain_id} 的容量 {cap} 分",
                    [{"type": "lock_over_capacity", "severity": "hard",
                      "chain_id": chain_id,
                      "locked_cents": amount_cents,
                      "capacity_cents": cap,
                      "message": "锁定金额超出可行容量"}],
                )
            self._locks[chain_id] = int(amount_cents)
            self._rebuild_locked()

    def unlock_chain(self, chain_id: str) -> None:
        with self._mu:
            if chain_id not in self._locks:
                raise ServiceError(f"链 {chain_id} 未被锁定")
            del self._locks[chain_id]
            self._rebuild_locked()

    def confirm_solution(self, solution_id: str, note: str = "") -> SolutionRecord:
        """确认一个 draft 解：转为 confirmed 且后续重算不可覆盖。"""
        with self._mu:
            for sol in self._drafts:
                if sol.solution_id == solution_id:
                    sol.confirmed = True
                    if note:
                        sol.note = note
                    self._confirmed.append(sol)
                    self._drafts = [s for s in self._drafts
                                    if s.solution_id != solution_id]
                    return sol
            # 已确认的解再次确认直接返回
            for sol in self._confirmed:
                if sol.solution_id == solution_id:
                    return sol
            raise ServiceError(f"解 {solution_id} 不存在或已过期")

    def recompute(self) -> None:
        """手动触发后台重算（不影响 confirmed 解）。"""
        with self._mu:
            self._rebuild_locked()

    # ------------------------------------------------------------------
    # 读视图
    # ------------------------------------------------------------------
    def snapshot(self) -> ServiceState:
        with self._mu:
            # 计算每笔流水在「第一个草稿解」下的已分配额与每链使用明细
            per_txn = self._txn_usage(self._drafts[0]) if self._drafts else {}
            ledger_view = self._ledger.copy()
            for t in ledger_view.transactions.values():
                t.allocated_cents = per_txn.get(t.txn_id, 0)
            return ServiceState(
                ledger=ledger_view,
                chains=[CandidateChain(c.chain_id, list(c.txn_ids))
                        for c in self._chains],
                drafts=[_clone_solution(s) for s in self._drafts],
                confirmed=[_clone_solution(s) for s in self._confirmed],
                locks=dict(self._locks),
                excluded_txn_ids=sorted(
                    t.txn_id for t in self._ledger.transactions.values()
                    if t.excluded),
                conflicts=list(self._conflicts),
                refund_errors=list(self._refund_summary.get("errors", [])),
                objective=self._objective,
                version=self._version,
            )

    def chain_detail(self, chain_id: str,
                     solution_id: str | None = None) -> dict:
        """点击一条链：返回每笔使用金额与剩余金额。"""
        with self._mu:
            ch = next((c for c in self._chains if c.chain_id == chain_id), None)
            if ch is None:
                raise ServiceError(f"链 {chain_id} 不存在")
            sol = self._pick_solution(solution_id)
            alloc = next((a for a in sol.allocations
                          if a.chain_id == chain_id), None)
            used = alloc.amount_cents if alloc else 0
            steps = []
            for txn_id in ch.txn_ids:
                t = self._ledger.transactions[txn_id]
                # 该链上每笔流水使用金额等于链分配额（闭合链流量一致）
                steps.append({
                    "txn_id": txn_id,
                    "date": t.date,
                    "from_account": t.from_account,
                    "to_account": t.to_account,
                    "amount_cents": t.amount_cents,
                    "amount_yuan": cents_to_yuan(t.amount_cents),
                    "remaining_cents": t.remaining_cents,
                    "remaining_yuan": cents_to_yuan(t.remaining_cents),
                    "used_by_this_chain_cents": used,
                    "used_by_this_chain_yuan": cents_to_yuan(used),
                    "excluded": t.excluded,
                })
            return {
                "chain_id": chain_id,
                "txn_ids": list(ch.txn_ids),
                "allocation_cents": used,
                "allocation_yuan": cents_to_yuan(used),
                "locked": chain_id in self._locks,
                "solution_id": sol.solution_id,
                "steps": steps,
            }

    def _pick_solution(self, solution_id: str | None) -> SolutionRecord:
        pool = self._drafts + self._confirmed
        if not pool:
            raise ServiceError("当前没有任何解")
        if solution_id is None:
            return self._drafts[0] if self._drafts else self._confirmed[0]
        for s in pool:
            if s.solution_id == solution_id:
                return s
        raise ServiceError(f"解 {solution_id} 不存在")

    def _txn_usage(self, sol: SolutionRecord) -> dict[str, int]:
        usage: dict[str, int] = {}
        index = {c.chain_id: c for c in self._chains}
        for a in sol.allocations:
            ch = index.get(a.chain_id)
            if ch is None:
                continue
            for txn_id in ch.txn_ids:
                usage[txn_id] = usage.get(txn_id, 0) + a.amount_cents
        return usage

    # 供测试使用
    def txn_usage(self, solution_id: str | None = None) -> dict[str, int]:
        with self._mu:
            return dict(self._txn_usage(self._pick_solution(solution_id)))


def _clone_solution(sol: SolutionRecord) -> SolutionRecord:
    return SolutionRecord(
        solution_id=sol.solution_id,
        objective=sol.objective,
        allocations=[ChainAllocation(a.chain_id, a.amount_cents, a.locked)
                     for a in sol.allocations],
        total_closed_cents=sol.total_closed_cents,
        chain_count=sol.chain_count,
        confirmed=sol.confirmed,
        note=sol.note,
        excluded_txn_ids=list(sol.excluded_txn_ids),
        locked_chain_ids=list(sol.locked_chain_ids),
        version=sol.version,
    )
