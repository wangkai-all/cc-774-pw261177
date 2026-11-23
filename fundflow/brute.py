"""小样例整数穷举——与线性规划完全独立的参考实现。

思路：对每条候选链的分配额 x_j ∈ [0, M_j]（M_j=链上最小流水容量）
递归枚举，逐条流水检查容量不超限，记录最优解。
为避免组合爆炸，节点数与单变量上界设硬上限；
单元测试使用小整数容量，JSON 小样例走「解的可行性 + 目标值」交叉校验。
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import CandidateChain, ChainAllocation
from .solver import ChainSolveResult, ChainSolveSolution, InfeasibleError, LPError


@dataclass
class BruteConfig:
    node_cap: int = 200_000
    max_var_bound: int = 20  # 单变量枚举上界硬上限（超出请改用 LP）


class BruteLimitExceeded(LPError):
    pass


def _chain_max(chains: list[CandidateChain], capacities: dict[str, int]) -> list[int]:
    bounds = []
    for ch in chains:
        m = min(capacities[t] for t in ch.txn_ids)
        bounds.append(m)
    return bounds


def brute_force(chains: list[CandidateChain],
                capacities: dict[str, int],
                locks: dict[str, int] | None = None,
                objective: str = "max_closure",
                max_solutions: int = 10,
                cfg: BruteConfig | None = None) -> ChainSolveResult:
    """穷举全部整数分配，返回与 solver 同构的结果。

    objective:
      - ``max_closure``：最大化总闭合金额，同值解全部保留；
      - ``min_chains``：先求最大闭合金额 T*，再在所有达到 T* 的分配中
        取使用链条数最少者（同最优全部保留）。
    """
    cfg = cfg or BruteConfig()
    locks = locks or {}
    n = len(chains)
    bounds = _chain_max(chains, capacities)
    for j, b in enumerate(bounds):
        if b > cfg.max_var_bound:
            raise BruteLimitExceeded(
                f"链 {chains[j].chain_id} 枚举上界 {b} 超过 {cfg.max_var_bound}，"
                "穷举仅适用于小样例"
            )

    # 每条流水经过哪些链
    txn_to_chains: dict[str, list[int]] = {}
    for j, ch in enumerate(chains):
        for t in ch.txn_ids:
            txn_to_chains.setdefault(t, []).append(j)

    nodes = 0
    x = [0] * n
    feasible: list[tuple[tuple[int, ...], int, int]] = []  # (分配, 总额, 链数)

    def record():
        total = sum(x)
        count = sum(1 for v in x if v > 0)
        feasible.append((tuple(x), total, count))

    def recurse(j: int, used: dict[str, int]):
        nonlocal nodes
        nodes += 1
        if nodes > cfg.node_cap:
            raise BruteLimitExceeded(f"穷举节点超过上限 {cfg.node_cap}")
        if j == n:
            record()
            return

        cid = chains[j].chain_id
        if cid in locks:
            choices = [locks[cid]]
        else:
            choices = range(bounds[j] + 1)

        for val in choices:
            # 容量预检
            touched = chains[j].txn_ids
            if all(used.get(t, 0) + val <= capacities[t] for t in touched):
                x[j] = val
                for t in touched:
                    used[t] = used.get(t, 0) + val
                recurse(j + 1, used)
                for t in touched:
                    used[t] -= val
                x[j] = 0

    recurse(0, {})
    if not feasible:
        raise InfeasibleError("穷举未找到可行分配")

    result = ChainSolveResult(objective=objective,
                              chain_ids=[ch.chain_id for ch in chains])
    result.nodes_used = nodes

    if objective == "max_closure":
        best_total = max(item[1] for item in feasible)
        pool = [item for item in feasible if item[1] == best_total]
    elif objective == "min_chains":
        best_total = max(item[1] for item in feasible)
        top = [item for item in feasible if item[1] == best_total]
        best_count = min(item[2] for item in top)
        pool = [item for item in top if item[2] == best_count]
    else:
        raise LPError(f"未知目标 {objective!r}")

    # 稳定排序后限量保留
    pool.sort(key=lambda item: (item[2], item[0]))
    for assignment, total, count in pool[:max_solutions]:
        lock_idx = {chains[j].chain_id for j in range(n)
                    if chains[j].chain_id in locks}
        result.solutions.append(ChainSolveSolution(
            allocations=[
                ChainAllocation(
                    chain_id=chains[j].chain_id,
                    amount_cents=int(assignment[j]),
                    locked=chains[j].chain_id in lock_idx,
                )
                for j in range(n) if assignment[j] > 0
            ],
            total_closed_cents=total,
            chain_count=count,
        ))
    if objective == "min_chains":
        result.note = f"最大闭合金额 {best_total} 分前提下的最少链条解释"
    return result
