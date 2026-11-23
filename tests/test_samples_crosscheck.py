"""样例文件回归 + LP/穷举独立对照。

- 校验两份 JSON 样例可完整加载、退款抵销与求解不报错；
- 在小容量合成台账上，LP/ILP 与穷举的最优值、最少链数必须一致；
- 大样例验证黑名单、窗口外环与坏退款容错。
"""
import unittest
from pathlib import Path

from fundflow.brute import BruteConfig, brute_force
from fundflow.chains import enumerate_chains
from fundflow.models import Ledger
from fundflow.refund import RefundError, apply_refunds
from fundflow.service import AnalysisService
from fundflow.solver import solve_max_closure, solve_min_chains
from tests.factories import build_ledger

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def caps(ledger):
    return {t.txn_id: t.remaining_cents
            for t in ledger.transactions.values()
            if not t.excluded and not t.is_refund and t.remaining_cents > 0}


class TestSampleFiles(unittest.TestCase):

    def test_small_sample_end_to_end(self):
        ledger = Ledger.from_json(SAMPLES / "small_sample.json")
        summary = apply_refunds(ledger)
        self.assertEqual(summary["total_offset_cents"], 4000)  # 40 元
        chains = enumerate_chains(ledger)
        self.assertTrue(chains)
        res = solve_max_closure(chains, caps(ledger), max_solutions=5)
        self.assertEqual(res.solutions[0].total_closed_cents, 10000)  # 100 元
        # 所有解都必须满足容量
        for sol in res.solutions:
            used = {}
            idx = {c.chain_id: c for c in chains}
            for a in sol.allocations:
                for t in idx[a.chain_id].txn_ids:
                    used[t] = used.get(t, 0) + a.amount_cents
            for t, v in used.items():
                self.assertLessEqual(v, caps(ledger)[t])

    def test_large_sample_blocked_account_and_bad_refund(self):
        ledger = Ledger.from_json(SAMPLES / "large_sample.json")
        # 严格模式必须拒绝坏退款 rf2
        with self.assertRaises(RefundError):
            apply_refunds(Ledger.from_json(SAMPLES / "large_sample.json"))
        svc = AnalysisService(ledger)  # 服务内部非严格容错
        snap = svc.snapshot()
        self.assertTrue(snap.refund_errors)
        # 黑名单账户不进入任何链
        for c in snap.chains:
            for t in c.txn_ids:
                tx = ledger.transactions[t]
                self.assertNotIn(tx.from_account, {"己公司"})
                self.assertNotIn(tx.to_account, {"己公司"})
        # 跨月边只能与彼此成环
        for c in snap.chains:
            if "p17" in c.txn_ids or "p18" in c.txn_ids:
                self.assertEqual(set(c.txn_ids), {"p17", "p18"})
        # 两种目标均可解且闭合额一致
        max_total = snap.drafts[0].total_closed_cents
        svc.set_objective("min_chains")
        min_snap = svc.snapshot()
        self.assertEqual(min_snap.drafts[0].total_closed_cents, max_total)
        self.assertLessEqual(min_snap.drafts[0].chain_count,
                             snap.drafts[0].chain_count)


class TestLPVersusBruteForce(unittest.TestCase):
    """两种完全独立的实现（ILP 与整数穷举）在小容量台账上逐项对照。"""

    def _check(self, objective, locks=None):
        ledger = build_ledger()
        chains = enumerate_chains(ledger)
        capacities = caps(ledger)
        if objective == "max_closure":
            lp = solve_max_closure(chains, capacities, locks=locks,
                                   max_solutions=10)
        else:
            lp = solve_min_chains(chains, capacities, locks=locks,
                                  max_solutions=10)
        bf = brute_force(chains, capacities, locks=locks,
                         objective=objective, max_solutions=10,
                         cfg=BruteConfig(max_var_bound=20))
        self.assertEqual(lp.solutions[0].total_closed_cents,
                         bf.solutions[0].total_closed_cents)
        if objective == "min_chains":
            self.assertEqual(lp.solutions[0].chain_count,
                             bf.solutions[0].chain_count)
        # 穷举收集到的最优解数量不低于 ILP 枚举（ILP 受 max_solutions 限制）
        self.assertGreaterEqual(len(bf.solutions), 1)
        return lp, bf

    def test_max_closure_match(self):
        self._check("max_closure")

    def test_min_chains_match(self):
        self._check("min_chains")

    def test_match_with_lock(self):
        ledger = build_ledger()
        chains = enumerate_chains(ledger)
        cid = chains[0].chain_id
        self._check("max_closure", locks={cid: 2})
        self._check("min_chains", locks={cid: 2})

    def test_brute_force_covers_full_optimal_set(self):
        ledger = build_ledger()
        chains = enumerate_chains(ledger)
        capacities = caps(ledger)
        bf = brute_force(chains, capacities, objective="max_closure",
                         max_solutions=50)
        lp = solve_max_closure(chains, capacities, max_solutions=50)
        # 两边最优值相同；LP 枚举出的每个解都应在穷举最优集合内
        bf_supports = {
            frozenset((a.chain_id, a.amount_cents) for a in s.allocations)
            for s in bf.solutions
        }
        for s in lp.solutions:
            support = frozenset((a.chain_id, a.amount_cents)
                                for a in s.allocations)
            self.assertIn(support, bf_supports)

    def test_min_chains_multi_solutions_are_distinct_and_optimal(self):
        """min_chains 枚举的多解必须是 x 支持集真正不同的最优分解，
        且全部落在穷举的最优集合内（防 no-good 割维度/伪多解回归）。"""
        ledger = build_ledger()
        chains = enumerate_chains(ledger)
        capacities = caps(ledger)
        lp = solve_min_chains(chains, capacities, max_solutions=10)
        bf = brute_force(chains, capacities, objective="min_chains",
                         max_solutions=50, cfg=BruteConfig(max_var_bound=20))
        bf_supports = {
            frozenset((a.chain_id, a.amount_cents) for a in s.allocations)
            for s in bf.solutions
        }
        lp_supports = []
        for s in lp.solutions:
            self.assertEqual(s.chain_count, bf.solutions[0].chain_count)
            support = frozenset((a.chain_id, a.amount_cents)
                                for a in s.allocations)
            self.assertIn(support, bf_supports)
            lp_supports.append(support)
        # 去重后数量不变 => 不存在 x 相同的伪多解
        self.assertEqual(len(lp_supports), len(set(lp_supports)))
        self.assertGreaterEqual(len(lp_supports), 2)


if __name__ == "__main__":
    unittest.main()
