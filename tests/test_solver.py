"""求解器测试：单纯形已知答案、分支定界、多解、锁定等式与容量可行性。"""
import unittest
from fractions import Fraction as F

from fundflow.brute import BruteConfig, BruteLimitExceeded, brute_force
from fundflow.chains import enumerate_chains
from fundflow.solver import (
    InfeasibleError,
    SimplexLP,
    solve_max_closure,
    solve_min_chains,
)
from tests.factories import build_ledger


def capacities_of(ledger):
    return {t.txn_id: t.remaining_cents
            for t in ledger.transactions.values()
            if not t.excluded and not t.is_refund and t.remaining_cents > 0}


class TestSimplexKnownAnswers(unittest.TestCase):

    def test_equality_constraint(self):
        lp = SimplexLP(["x"], [([F(1)], "==", F(5))], [F(1)], "max")
        self.assertEqual(lp.x[0], 5)

    def test_greater_than_and_minimization(self):
        lp = SimplexLP(["x"], [([F(1)], ">=", F(3))], [F(1)], "min")
        self.assertEqual(lp.x[0], 3)

    def test_classic_two_variable(self):
        rows = [([F(1), F(1)], "<=", F(4)),
                ([F(2), F(1)], "<=", F(5))]
        lp = SimplexLP(["x", "y"], rows, [F(3), F(2)], "max")
        self.assertEqual(lp.x, [F(1), F(3)])
        self.assertEqual(lp.objective_original, 9)

    def test_infeasible_detected(self):
        with self.assertRaises(InfeasibleError):
            SimplexLP(["x"], [([F(1)], ">=", F(5)),
                              ([F(1)], "<=", F(3))], [F(1)], "max")

    def test_degenerate_redundant_row(self):
        rows = [([F(1)], "<=", F(5)), ([F(1)], "==", F(5))]
        lp = SimplexLP(["x"], rows, [F(1)], "max")
        self.assertEqual(lp.x[0], 5)


class TestChainAllocation(unittest.TestCase):

    def setUp(self):
        self.ledger = build_ledger()
        self.chains = enumerate_chains(self.ledger)
        self.caps = capacities_of(self.ledger)

    def _assert_feasible(self, sol):
        used = {t: 0 for t in self.caps}
        chain_index = {c.chain_id: c for c in self.chains}
        for a in sol.allocations:
            ch = chain_index[a.chain_id]
            for t in ch.txn_ids:
                used[t] += a.amount_cents
        for t, total in used.items():
            self.assertLessEqual(total, self.caps[t],
                                 f"流水 {t} 分配 {total} 超过容量 {self.caps[t]}")

    def test_max_closure_matches_brute_force(self):
        lp = solve_max_closure(self.chains, self.caps, max_solutions=20)
        bf = brute_force(self.chains, self.caps, objective="max_closure",
                         max_solutions=20, cfg=BruteConfig(max_var_bound=20))
        self.assertEqual(lp.solutions[0].total_closed_cents,
                         bf.solutions[0].total_closed_cents)
        # 穷举得到的每个最优解目标值都应等于 ILP 最优值
        best = bf.solutions[0].total_closed_cents
        for s in lp.solutions:
            self.assertEqual(s.total_closed_cents, best)
            self._assert_feasible(s)

    def test_min_chains_matches_brute_force(self):
        lp = solve_min_chains(self.chains, self.caps, max_solutions=10)
        bf = brute_force(self.chains, self.caps, objective="min_chains",
                         max_solutions=10)
        self.assertEqual(lp.solutions[0].total_closed_cents,
                         bf.solutions[0].total_closed_cents)
        self.assertEqual(lp.solutions[0].chain_count,
                         bf.solutions[0].chain_count)
        # 最少链条数不应大于 max_closure 最优解的链数
        mc = lp.solutions[0]
        mx = solve_max_closure(self.chains, self.caps, max_solutions=1)
        self.assertLessEqual(mc.chain_count, mx.solutions[0].chain_count)
        self._assert_feasible(mc)

    def test_multiple_distinct_solutions(self):
        res = solve_max_closure(self.chains, self.caps, max_solutions=5)
        supports = {frozenset(a.chain_id for a in s.allocations)
                    for s in res.solutions}
        # 小台账存在多种最优分解，应保留多解且结构互不相同
        self.assertGreaterEqual(len(res.solutions), 2)
        self.assertEqual(len(supports), len(res.solutions))

    def test_partial_allocation_shared_edge(self):
        """同一笔流水可部分分配给多条链，分配总额 <= 原金额。"""
        res = solve_max_closure(self.chains, self.caps, max_solutions=10)
        # 找一个含共用边 t1 的多链解
        chain_index = {c.chain_id: c for c in self.chains}
        found_shared = False
        for sol in res.solutions:
            usage = 0
            for a in sol.allocations:
                if "t1" in chain_index[a.chain_id].txn_ids:
                    usage += a.amount_cents
            if usage > 0:
                self.assertLessEqual(usage, self.caps["t1"])
                # 至少有一个解里 t1 被多条链共用
                users = [a for a in sol.allocations
                         if "t1" in chain_index[a.chain_id].txn_ids]
                if len(users) >= 2:
                    found_shared = True
        self.assertTrue(found_shared, "应存在共用边 t1 的多链分解")

    def test_lock_equality_honored(self):
        cid = self.chains[0].chain_id
        locked_amt = 2
        res = solve_max_closure(self.chains, self.caps,
                                locks={cid: locked_amt}, max_solutions=5)
        for sol in res.solutions:
            alloc = next(a for a in sol.allocations if a.chain_id == cid)
            self.assertEqual(alloc.amount_cents, locked_amt)
            self.assertTrue(alloc.locked)
            self._assert_feasible(sol)

    def test_infeasible_lock_raises(self):
        cid = self.chains[0].chain_id
        cap = min(self.caps[t] for t in self.chains[0].txn_ids)
        with self.assertRaises((InfeasibleError, Exception)):
            solve_max_closure(self.chains, self.caps,
                              locks={cid: cap + 1000})

    def test_no_chains_handling(self):
        # 全部边排除后无候选链
        for t in self.ledger.transactions.values():
            t.excluded = True
        chains = enumerate_chains(self.ledger)
        self.assertEqual(chains, [])


class TestBruteForceGuards(unittest.TestCase):

    def test_bound_guard(self):
        ledger = build_ledger()
        chains = enumerate_chains(ledger)
        caps = capacities_of(ledger)
        # 人为放大容量触发上界保护
        caps = {k: v * 100 for k, v in caps.items()}
        with self.assertRaises(BruteLimitExceeded):
            brute_force(chains, caps)


if __name__ == "__main__":
    unittest.main()
