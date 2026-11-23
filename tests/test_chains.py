"""候选链枚举测试：时间窗口、严格时序、账户黑白名单、去重与非汇总图语义。"""
import unittest

from fundflow.chains import ChainEnumerationError, enumerate_chains
from fundflow.models import ChainConfig
from tests.factories import build_ledger, make_txn


class TestChainEnumeration(unittest.TestCase):

    def test_basic_cycles_found(self):
        ledger = build_ledger()
        chains = enumerate_chains(ledger)
        ids = {tuple(c.txn_ids) for c in chains}
        # 三角环
        self.assertIn(("t1", "t2", "t3"), ids)
        # 四环
        self.assertIn(("t4", "t5", "t6"), ids)
        # 短环 t1->...->t7
        self.assertIn(("t1", "t7"), ids)

    def test_rotation_dedup(self):
        ledger = build_ledger()
        chains = enumerate_chains(ledger)
        # 同一环无论从哪条边开始 DFS，只保留一条
        keys = [tuple(sorted(c.txn_ids)) for c in chains]
        self.assertEqual(len(keys), len(set(keys)))

    def test_time_span_limit(self):
        ledger = build_ledger()
        cfg = ChainConfig(max_span_days=2, max_chain_edges=6, min_chain_edges=2)
        chains = enumerate_chains(ledger, cfg)
        from fundflow.models import parse_date
        for c in chains:
            dates = [parse_date(ledger.transactions[t].date) for t in c.txn_ids]
            self.assertLessEqual((max(dates) - min(dates)).days, 2)
        # t1..t3 跨度 2 天保留；构造跨度 3 天的环验证被过滤
        self.assertIn(("t1", "t2", "t3"), [tuple(c.txn_ids) for c in chains])
        ledger.transactions["t3"].date = "2026-01-10"  # t1(01-01) 起跨度 9 天
        chains2 = enumerate_chains(ledger, cfg)
        self.assertNotIn(("t1", "t2", "t3"), [tuple(c.txn_ids) for c in chains2])

    def test_strict_time_rejects_same_day_cycle(self):
        ledger = build_ledger()
        # 同日双向边：非严格模式允许，严格模式必须拒绝
        ledger.transactions["s1"] = make_txn("s1", "2026-02-01", "A", "B", 5, "S1")
        ledger.transactions["s2"] = make_txn("s2", "2026-02-01", "B", "A", 5, "S2")
        loose = enumerate_chains(ledger, ChainConfig(strict_time=False))
        self.assertTrue(any(set(c.txn_ids) == {"s1", "s2"} for c in loose))
        strict = enumerate_chains(ledger, ChainConfig(strict_time=True))
        self.assertFalse(any(set(c.txn_ids) == {"s1", "s2"} for c in strict))

    def test_blocked_account_excluded(self):
        ledger = build_ledger()
        cfg = ChainConfig(blocked_accounts={"C"})
        chains = enumerate_chains(ledger, cfg)
        for c in chains:
            accts = set()
            for t in c.txn_ids:
                tx = ledger.transactions[t]
                accts.update((tx.from_account, tx.to_account))
            self.assertNotIn("C", accts)

    def test_allowed_account_whitelist(self):
        ledger = build_ledger()
        cfg = ChainConfig(allowed_accounts={"A", "B"})
        chains = enumerate_chains(ledger, cfg)
        # 白名单内只有 A/B 之间的短环 t1,t7
        self.assertTrue(chains)
        for c in chains:
            for t in c.txn_ids:
                tx = ledger.transactions[t]
                self.assertIn(tx.from_account, {"A", "B"})
                self.assertIn(tx.to_account, {"A", "B"})

    def test_excluded_edge_not_enumerated(self):
        ledger = build_ledger()
        ledger.transactions["t1"].excluded = True
        chains = enumerate_chains(ledger)
        self.assertTrue(all("t1" not in c.txn_ids for c in chains))

    def test_max_chain_edges_respected(self):
        ledger = build_ledger()
        cfg = ChainConfig(max_chain_edges=3)
        chains = enumerate_chains(ledger, cfg)
        self.assertTrue(all(len(c.txn_ids) <= 3 for c in chains))

    def test_candidate_cap_raises(self):
        ledger = build_ledger()
        cfg = ChainConfig(max_candidates=1)
        with self.assertRaises(ChainEnumerationError):
            enumerate_chains(ledger, cfg)

    def test_chain_ids_stable_and_sorted(self):
        ledger = build_ledger()
        chains = enumerate_chains(ledger)
        # id 基于边内容生成，全局唯一
        ids = [c.chain_id for c in chains]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(cid.startswith("CH-") for cid in ids))
        # 长度非递减（稳定排序约定）
        lens = [len(c.txn_ids) for c in chains]
        self.assertEqual(lens, sorted(lens))

    def test_chain_id_stable_across_re_enumeration(self):
        """排除无关边前后，同一条链的内容哈希 id 必须保持不变。"""
        ledger = build_ledger()
        before = {tuple(c.txn_ids): c.chain_id for c in enumerate_chains(ledger)}
        ledger.transactions["t3"].excluded = True
        after = {tuple(c.txn_ids): c.chain_id for c in enumerate_chains(ledger)}
        for path, cid in before.items():
            if "t3" not in path:
                self.assertIn(path, after)
                self.assertEqual(after[path], cid)


if __name__ == "__main__":
    unittest.main()
