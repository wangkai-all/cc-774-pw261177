"""线性 / 整数规划求解器（纯 Python 从零实现，无第三方依赖）。

内容：
- ``SimplexLP``：两阶段单纯形法，``fractions.Fraction`` 精确运算，
  Bland 规则防循环，支持 <= / == / >= 约束与最大化 / 最小化；
- ``branch_and_bound``：在 LP 松弛上分支定界求整数解（含 0/1 变量）；
- ``solve_max_closure`` / ``solve_min_chains``：面向资金链分配的建模入口，
  支持锁定量、多解枚举（no-good 割）与两种解释目标。

语义提醒：求得的链条分配是资金关系的数学解释，不是违法事实认定。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional

from .models import CandidateChain, ChainAllocation

F = Fraction


class LPError(RuntimeError):
    pass


class InfeasibleError(LPError):
    pass


class UnboundedError(LPError):
    pass


def _row_value(tab, basis, active_rows, col) -> "F":
    """读取某列当前取值：在基则取对应行 RHS，非基为 0。"""
    for idx, r in enumerate(active_rows):
        if basis[idx] == col:
            return tab[r][-1]
    return F(0)


def _pivot(tab, basis, q: int, p_row: int):
    """单纯形转轴（模块级纯函数，避免 self 绑定导致参数错位）。

    q=入基列，p_row=出基行（表格行号，目标行为 0；数据行从 1 开始）。
    ``basis`` 为 0 基数组：数据行 r（表格行号）对应 basis[r-1]。
    """
    pivot = tab[p_row][q]
    if pivot == 0:
        raise LPError("单纯形主元为 0")
    tab[p_row] = [v / pivot for v in tab[p_row]]
    for r in range(len(tab)):
        if r == p_row:
            continue
        factor = tab[r][q]
        if factor != 0:
            tab[r] = [a - factor * b for a, b in zip(tab[r], tab[p_row])]
    basis[p_row - 1] = q


# ---------------------------------------------------------------------------
# 两阶段单纯形
# ---------------------------------------------------------------------------

class SimplexLP:
    """给定约束 A x {<=,==,>=} b（b >= 0）与目标 c，求最优解。

    内部统一转为最大化；最小化通过目标取负实现。
    表格列布局：结构变量 | 松弛/剩余变量(每约束一列) | 人工变量 | RHS
    """

    MAX_ITERS = 20000

    def __init__(self, var_names: list[str],
                 rows: list[tuple[list[F], str, F]],
                 objective: list[F], sense: str,
                 binary_vars: Optional[set[int]] = None):
        self.var_names = list(var_names)
        self.n = len(var_names)
        self.rows_in = rows
        self.m = len(rows)
        self.sense = sense
        # 统一为最大化
        sign = F(1) if sense == "max" else F(-1)
        self.c = [sign * v for v in objective]
        self.binary_vars = binary_vars or set()
        self._solve()

    # ---- 内部工具 ----
    def _build_tableau(self):
        n, m = self.n, self.m
        art_cols: dict[int, int] = {}
        # 松弛/剩余列只分配给 <= 与 >= 约束（== 没有该列）
        slack_col_of: dict[int, int] = {}
        next_slack = n
        for i, (_, sense, _) in enumerate(self.rows_in):
            if sense in ("<=", ">="):
                slack_col_of[i] = next_slack
                next_slack += 1
            elif sense != "==":
                raise LPError(f"未知约束方向: {sense!r}")
        n_art = sum(1 for _, s, _ in self.rows_in if s in ("==", ">="))
        # 变量列数（不含 RHS）；RHS 列下标 = var_cols
        var_cols = next_slack + n_art
        tab = [[F(0)] * (var_cols + 1) for _ in range(m + 1)]
        basis = [0] * m

        next_art = next_slack
        for i, (coeffs, sense, b) in enumerate(self.rows_in):
            if b < 0:
                raise LPError("约束 RHS 必须非负（调用方负责规范化）")
            row = tab[i + 1]
            for j, a in enumerate(coeffs):
                row[j] = F(a)
            if sense == "<=":
                col = slack_col_of[i]
                row[col] = F(1)
                basis[i] = col
            elif sense == ">=":
                row[slack_col_of[i]] = F(-1)
                art_col = next_art
                next_art += 1
                row[art_col] = F(1)
                art_cols[i] = art_col
                basis[i] = art_col
            else:  # "=="
                art_col = next_art
                next_art += 1
                row[art_col] = F(1)
                art_cols[i] = art_col
                basis[i] = art_col
            row[var_cols] = F(b)
        return tab, basis, art_cols, var_cols

    def _simplex(self, tab, basis, active_rows: list[int]):
        """对当前表格跑最大化单纯形（Bland 规则）。active_rows: 可用的表格行号。"""
        rhs_col = len(tab[0]) - 1
        n_var_cols = rhs_col  # 变量列下标为 0..rhs_col-1
        iters = 0
        while True:
            iters += 1
            if iters > self.MAX_ITERS:
                raise LPError(f"单纯形迭代超过上限 {self.MAX_ITERS}")
            # Bland 入基：最小下标的负检验数
            q = None
            for j in range(n_var_cols):
                if tab[0][j] < 0:
                    q = j
                    break
            if q is None:
                return "optimal"
            # 比值检验（平局按基变量列号最小，保持 Bland 规则）
            best_ratio = None
            p_row = None
            for r in active_rows:
                a = tab[r][q]
                if a > 0:
                    ratio = tab[r][rhs_col] / a
                    if best_ratio is None or ratio < best_ratio or (
                            ratio == best_ratio
                            and basis[r - 1] < basis[p_row - 1]):
                        best_ratio = ratio
                        p_row = r
            if p_row is None:
                raise UnboundedError("LP 无界（建模错误：变量缺容量约束）")
            _pivot(tab, basis, q, p_row)

    def _solve(self):
        tab, basis, art_cols, var_cols = self._build_tableau()
        # 松弛/剩余列总数（== 约束不占该列）
        n_slack = sum(1 for _, s, _ in self.rows_in if s in ("<=", ">="))
        # var_cols = 非 RHS 的变量列数；RHS 列下标恒为 len(row)-1
        active_rows = list(range(1, self.m + 1))

        # ---- 第一阶段：max  w = -sum(人工变量) ----
        if art_cols:
            art_set = set(art_cols.values())
            for r in active_rows:
                if (r - 1) in art_cols:
                    for j in range(var_cols + 1):
                        tab[0][j] -= tab[r][j]
            self._simplex(tab, basis, active_rows)
            # 可行性判定：人工变量必须全部为 0。不能只看目标行 RHS——
            # 退化情况下人工变量可能以零值留在基中，需要先 pivoting out。
            art_value = {
                col: _row_value(tab, basis, active_rows, col)
                for col in art_set
            }
            if any(v != 0 for v in art_value.values()):
                raise InfeasibleError("第一阶段判定约束不可行")
            # 人工变量仍在基（退化零值）：尝试 pivoting out，否则删冗余行
            dead_rows = []
            for r in list(active_rows):
                if basis[r - 1] in art_set:
                    q = None
                    for j in range(var_cols):
                        if j in art_set:
                            continue
                        if tab[r][j] != 0:
                            q = j
                            break
                    if q is None:
                        dead_rows.append(r)  # 冗余约束
                    else:
                        _pivot(tab, basis, q, r)
            if dead_rows:
                active_rows = [r for r in active_rows if r not in dead_rows]
                keep = set(active_rows)
                tab = [tab[0]] + [row for idx, row in enumerate(tab)
                                  if idx in keep]
                basis = [basis[r - 1] for r in active_rows]
            # 删除人工变量列（保留 RHS）；保留列 = 结构变量 + 松弛/剩余变量
            keep_cols = self.n + n_slack
            tab = [row[:keep_cols] + [row[var_cols]] for row in tab]
            var_cols = keep_cols
            if any(b >= var_cols for b in basis):
                raise LPError("人工变量离基处理失败")

        # ---- 第二阶段：真实目标 ----
        tab[0] = [F(0)] * (var_cols + 1)
        for j in range(self.n):
            tab[0][j] = -self.c[j]
        for idx, r in enumerate(active_rows):
            bcol = basis[idx]
            factor = tab[0][bcol]
            if factor != 0:
                tab[0] = [a - factor * b for a, b in zip(tab[0], tab[r])]
        self._simplex(tab, basis, active_rows)

        self.tableau = tab
        self.basis = basis
        self.active_rows = active_rows
        self.ncols = var_cols
        self.status = "optimal"
        self.x = [F(0)] * self.n
        rhs_col = var_cols
        for idx, bcol in enumerate(basis):
            if bcol < self.n:
                self.x[bcol] = tab[active_rows[idx]][rhs_col]
        self.objective = sum(self.c[j] * self.x[j] for j in range(self.n))
        # 还原成调用方请求方向的目标值
        self.objective_original = (
            self.objective if self.sense == "max" else -self.objective
        )


# ---------------------------------------------------------------------------
# 分支定界整数规划
# ---------------------------------------------------------------------------

@dataclass
class ILPResult:
    x: list[int]
    objective: F
    nodes: int
    status: str = "optimal"


def _is_integer(v: F) -> bool:
    return v.denominator == 1


def branch_and_bound(var_names: list[str],
                     base_rows: list[tuple[list[F], str, F]],
                     objective: list[F], sense: str,
                     integer_vars: set[int],
                     binary_vars: Optional[set[int]] = None,
                     node_cap: int = 3000) -> ILPResult:
    """对全部 ``integer_vars`` 要求整数（binary_vars 额外限定 0/1）。

    分支策略：优先选最接近 0.5 的分数变量（二元变量优先），
    用 x<=floor 与 x>=ceil 两个分支 DFS，按目标界剪枝。
    """
    binary_vars = binary_vars or set()
    best: Optional[list[int]] = None
    best_obj: Optional[F] = None
    nodes = 0

    def better(incumbent: F, candidate: F) -> bool:
        return candidate > incumbent if sense == "max" else candidate < incumbent

    def search(extra_rows):
        nonlocal best, best_obj, nodes
        nodes += 1
        if nodes > node_cap:
            raise LPError(f"分支定界节点数超过上限 {node_cap}，请缩小问题规模")
        try:
            lp = SimplexLP(var_names, base_rows + extra_rows, objective, sense,
                           binary_vars=binary_vars)
        except InfeasibleError:
            return
        # 目标界剪枝
        if best_obj is not None and not better(best_obj, lp.objective_original):
            if lp.objective_original != best_obj:
                return

        frac = [j for j in integer_vars if not _is_integer(lp.x[j])]
        if not frac:
            # 整数可行解
            cand_obj = lp.objective_original
            if best_obj is None or better(best_obj, cand_obj) or (
                    cand_obj == best_obj and best is None):
                best_obj = cand_obj
                best = [int(v) for v in lp.x]
            return

        def score(j):
            v = lp.x[j]
            frac_part = v - int(v)  # 0..1
            closeness = -abs(frac_part - F(1, 2))
            return (0 if j in binary_vars else 1, closeness)

        j = max(frac, key=score)
        v = lp.x[j]
        lo, hi = int(v), int(v) + 1

        # 两个分支：x_j <= lo，x_j >= hi
        row_lo = [[F(0)] * len(var_names), "<=", F(lo)]
        row_lo[0][j] = F(1)
        row_hi = [[F(0)] * len(var_names), ">=", F(hi)]
        row_hi[0][j] = F(1)
        search(extra_rows + [tuple(row_lo)])
        search(extra_rows + [tuple(row_hi)])

    search([])
    if best is None:
        raise InfeasibleError("整数规划无可行解")
    return ILPResult(x=best, objective=best_obj, nodes=nodes)


# ---------------------------------------------------------------------------
# 资金链分配建模
# ---------------------------------------------------------------------------

@dataclass
class ChainSolveSolution:
    allocations: list[ChainAllocation]
    total_closed_cents: int
    chain_count: int

    def support(self) -> set[int]:
        return {i for i, a in enumerate(self.allocations) if a.amount_cents > 0}


@dataclass
class ChainSolveResult:
    objective: str
    chain_ids: list[str]
    solutions: list[ChainSolveSolution] = field(default_factory=list)
    nodes_used: int = 0
    note: str = ""


def _build_capacity_rows(chains: list[CandidateChain],
                         capacities: dict[str, int]
                         ) -> tuple[list[str], list[tuple[list[F], str, F]]]:
    """构造每条流水的容量约束 sum(经过它的链 x) <= 剩余额度。"""
    txn_order = sorted(capacities)
    n = len(chains)
    rows = []
    for txn_id in txn_order:
        coeffs = [F(0)] * n
        for j, ch in enumerate(chains):
            if txn_id in ch.txn_ids:
                coeffs[j] = F(1)
        cap = capacities[txn_id]
        if cap < 0:
            raise LPError(f"流水 {txn_id} 可用额度为负")
        rows.append((coeffs, "<=", F(cap)))
    return txn_order, rows


def _lock_rows(n: int, locks: dict[int, int]
               ) -> list[tuple[list[F], str, F]]:
    rows = []
    for j, v in locks.items():
        coeffs = [F(0)] * n
        coeffs[j] = F(1)
        rows.append((coeffs, "==", F(int(v))))
    return rows


def _nogood_cut(n: int, support: set[int], support_sum: int
                ) -> tuple[list[F], str, F]:
    """no-good 割：已见解的非零变量之和必须至少少 1 分。"""
    coeffs = [F(1) if j in support else F(0) for j in range(n)]
    return (coeffs, "<=", F(max(support_sum - 1, 0)))


def solve_max_closure(chains: list[CandidateChain],
                      capacities: dict[str, int],
                      locks: Optional[dict[str, int]] = None,
                      max_solutions: int = 3,
                      node_cap: int = 3000) -> ChainSolveResult:
    """目标 1：最大化闭合资金总额（每单位链分配闭合一单位资金）。

    ``locks``: {chain_id: 固定分配金额（分）}，作为等式约束。
    返回至多 ``max_solutions`` 个**同最优值、结构不同**的整数解。
    """
    n = len(chains)
    var_names = [ch.chain_id for ch in chains]
    _, cap_rows = _build_capacity_rows(chains, capacities)
    lock_idx = _lock_index(locks, chains)
    base_rows = cap_rows + _lock_rows(n, lock_idx)
    objective = [F(1)] * n  # max sum x_j

    return _enumerate_solutions(
        objective="max_closure", chains=chains, var_names=var_names,
        base_rows=base_rows, objective_c=objective, sense="max",
        integer_vars=set(range(n)), binary_vars=set(),
        locks=lock_idx, max_solutions=max_solutions, node_cap=node_cap,
    )


def solve_min_chains(chains: list[CandidateChain],
                     capacities: dict[str, int],
                     locks: Optional[dict[str, int]] = None,
                     max_solutions: int = 3,
                     node_cap: int = 3000) -> ChainSolveResult:
    """目标 2：在**保持最大闭合金额**的前提下，最少使用链条数。

    词序目标：先求 max closure T*，再 min sum y_j，
    其中 x_j <= M_j y_j，y_j ∈ {0,1}，M_j 为该链可行的最大分配额。
    """
    n = len(chains)
    var_names = [ch.chain_id for ch in chains]
    txn_order, cap_rows = _build_capacity_rows(chains, capacities)
    lock_idx = _lock_index(locks, chains)

    # 第一步：最大闭合金额
    first = solve_max_closure(chains, capacities, locks=locks,
                              max_solutions=1, node_cap=node_cap)
    if not first.solutions:
        raise InfeasibleError("无可行闭合链")
    t_star = first.solutions[0].total_closed_cents

    # 变量布局：x_0..x_{n-1}, y_0..y_{n-1}
    m_vars = 2 * n
    full_names = var_names + [f"y_{name}" for name in var_names]
    rows = list(cap_rows) + _lock_rows(n, lock_idx)

    # 闭合总额保持 T*
    rows.append(([F(1)] * n + [F(0)] * n, "==", F(t_star)))

    # x_j <= M_j y_j
    for j, ch in enumerate(chains):
        m_j = min(capacities[txn] for txn in ch.txn_ids)
        coeffs = [F(0)] * m_vars
        coeffs[j] = F(1)
        coeffs[n + j] = F(-m_j) if m_j > 0 else F(0)
        rows.append((coeffs, "<=", F(0)))

    objective = [F(0)] * n + [F(1)] * n  # min sum y

    result = _enumerate_solutions(
        objective="min_chains", chains=chains, var_names=full_names,
        base_rows=rows, objective_c=objective, sense="min",
        integer_vars=set(range(m_vars)), binary_vars=set(range(n, m_vars)),
        locks=lock_idx, max_solutions=max_solutions, node_cap=node_cap,
        t_star=t_star,
    )
    result.note = f"最大闭合金额 {t_star} 分前提下的最少链条解释"
    return result


def _lock_index(locks: Optional[dict[str, int]],
                chains: list[CandidateChain]) -> dict[int, int]:
    if not locks:
        return {}
    idx = {ch.chain_id: j for j, ch in enumerate(chains)}
    out = {}
    for cid, amt in locks.items():
        if cid not in idx:
            raise LPError(f"锁定的链条 {cid} 不在候选集中")
        out[idx[cid]] = int(amt)
    return out


def _enumerate_solutions(*, objective: str, chains: list[CandidateChain],
                         var_names: list[str],
                         base_rows: list[tuple[list[F], str, F]],
                         objective_c: list[F], sense: str,
                         integer_vars: set[int], binary_vars: set[int],
                         locks: dict[int, int], max_solutions: int,
                         node_cap: int,
                         t_star: Optional[int] = None) -> ChainSolveResult:
    """用 no-good 割反复求解，枚举同最优值的不同整数解。"""
    n = len(chains)
    extra: list[tuple[list[F], str, F]] = []
    result = ChainSolveResult(objective=objective,
                              chain_ids=[ch.chain_id for ch in chains])
    seen_objs = []

    for _ in range(max_solutions):
        ilp = branch_and_bound(
            var_names, base_rows + extra, objective_c, sense,
            integer_vars=integer_vars, binary_vars=binary_vars,
            node_cap=node_cap,
        )
        result.nodes_used += ilp.nodes
        x_int = ilp.x[:n]
        # 校验目标值一致（多解必须同为最优）
        if seen_objs and ilp.objective not in seen_objs:
            break
        seen_objs.append(ilp.objective)

        support = {j for j, v in enumerate(x_int) if v > 0}
        support_sum = sum(x_int[j] for j in support)
        allocs = [
            ChainAllocation(
                chain_id=chains[j].chain_id,
                amount_cents=int(x_int[j]),
                locked=j in locks,
            )
            for j in range(n) if x_int[j] > 0
        ]
        total = int(sum(x_int))
        result.solutions.append(ChainSolveSolution(
            allocations=allocs,
            total_closed_cents=total,
            chain_count=len(allocs),
        ))
        # no-good 割排除当前 x 解：系数长度与 var_names 严格对齐。
        # min_chains 下 var_names = [x_0..x_{n-1}, y_0..y_{n-1}]，y 列补 0，
        # 因此割只作用于结构变量 x，长度仍为完整变量数，维度不会错位。
        n_vars = len(var_names)
        cut_coeffs = [F(1) if j in support else F(0)
                      for j in range(n_vars)]
        extra.append((cut_coeffs, "<=", F(max(support_sum - 1, 0))))

    if t_star is not None:
        for sol in result.solutions:
            assert sol.total_closed_cents == t_star
    return result
