"""服务层测试：人工排除重分配、锁定/解锁、冲突检测、解版本确认与并发安全。"""
import threading
import unittest

from fundflow.chains import enumerate_chains
from fundflow.service import AnalysisService, ConflictError, ServiceError
from fundflow.models import Ledger, cents_to_yuan
from tests.factories import build_ledger


class TestServiceLifecycle(unittest.TestCase):

    def setUp(self):
        self.svc = AnalysisService(build_ledger())

    def test_initial_solutions_feasible(self):
        s = self.svc.snapshot()
        self.assertTrue(s.drafts)
        for d in s.drafts:
            self.assertGreater(d.total_closed_cents, 0)

    def test_exclude_edge_reallocates_and_keeps_capacity(self):
        before = self.svc.snapshot()
        total_before = before.drafts[0].total_closed_cents
        self.svc.exclude_edge("t1")
        after = self.svc.snapshot()
        # 含 t1 的链全部消失
        self.assertTrue(all("t1" not in c.txn_ids for c in after.chains))
        self.assertIn("t1", after.excluded_txn_ids)
        # 重算后任何流水都不会超分配
        usage = self.svc.txn_usage()
        for txn_id, used in usage.items():
            self.assertLessEqual(used, after.ledger.transactions[txn_id].remaining_cents)
        # 可闭合资金一般会下降或持平（移除边不会增加可行空间）
        total_after = after.drafts[0].total_closed_cents if after.drafts else 0
        self.assertLessEqual(total_after, total_before)
        # 恢复后回到原候选集
        self.svc.include_edge("t1")
        restored = self.svc.snapshot()
        self.assertNotIn("t1", restored.excluded_txn_ids)
        self.assertTrue(any("t1" in c.txn_ids for c in restored.chains))

    def test_confirmed_solutions_survive_recompute(self):
        s = self.svc.snapshot()
        sid = s.drafts[0].solution_id
        confirmed = self.svc.confirm_solution(sid, note="保留我")
        # 多次重算（含排除/恢复）
        self.svc.exclude_edge("t2")
        self.svc.include_edge("t2")
        self.svc.recompute()
        snap = self.svc.snapshot()
        ids = [c.solution_id for c in snap.confirmed]
        self.assertIn(confirmed.solution_id, ids)
        self.assertTrue(all(c.confirmed for c in snap.confirmed))
        # 已确认解不会出现在草稿里被再次修改
        self.assertNotIn(confirmed.solution_id,
                         [d.solution_id for d in snap.drafts])

    def test_lock_then_exclude_creates_hard_conflict(self):
        s = self.svc.snapshot()
        # 选一条含「专属边」的链：三角链 t1,t2,t3，排除 t3 后该链必然消失
        target = next(c for c in s.chains if c.txn_ids == ["t1", "t2", "t3"])
        self.svc.lock_chain(target.chain_id, 1)
        self.svc.exclude_edge("t3")
        snap = self.svc.snapshot()
        hard = [c for c in snap.conflicts if c["severity"] == "hard"]
        self.assertTrue(hard)
        self.assertEqual(hard[0]["type"], "stale_lock")
        # 草稿被保留（未被不可行重算清空），等待人工处理
        self.assertTrue(snap.drafts)
        # 解除锁后系统恢复可解
        self.svc.unlock_chain(target.chain_id)
        snap2 = self.svc.snapshot()
        self.assertFalse([c for c in snap2.conflicts if c["severity"] == "hard"])
        self.assertTrue(snap2.drafts)

    def test_over_capacity_lock_rejected_with_409_payload(self):
        s = self.svc.snapshot()
        cid = s.chains[0].chain_id
        cap = min(s.ledger.transactions[t].remaining_cents
                  for t in s.chains[0].txn_ids)
        with self.assertRaises(ConflictError) as ctx:
            self.svc.lock_chain(cid, cap + 1)
        self.assertEqual(ctx.exception.conflicts[0]["type"],
                         "lock_over_capacity")

    def test_lock_nonexistent_chain_rejected(self):
        with self.assertRaises(ConflictError):
            self.svc.lock_chain("C999", 1)

    def test_chain_detail_shows_used_and_remaining(self):
        s = self.svc.snapshot()
        cid = s.drafts[0].allocations[0].chain_id
        detail = self.svc.chain_detail(cid)
        self.assertEqual(len(detail["steps"]),
                         len(next(c for c in s.chains if c.chain_id == cid).txn_ids))
        for step in detail["steps"]:
            self.assertEqual(step["used_by_this_chain_cents"],
                             detail["allocation_cents"])
            self.assertGreaterEqual(step["remaining_cents"], 0)

    def test_objective_switch_keeps_max_closure_value(self):
        self.svc.set_objective("min_chains")
        s_min = self.svc.snapshot()
        min_total = s_min.drafts[0].total_closed_cents
        min_count = s_min.drafts[0].chain_count

        self.svc.set_objective("max_closure")
        s_max = self.svc.snapshot()
        max_total = s_max.drafts[0].total_closed_cents
        # 词序目标：min_chains 闭合额必须等于最大值，链数不多于 max 方案
        self.assertEqual(min_total, max_total)
        self.assertLessEqual(min_count, s_max.drafts[0].chain_count)

    def test_version_monotonic(self):
        v0 = self.svc.snapshot().version
        self.svc.recompute()
        v1 = self.svc.snapshot().version
        self.svc.exclude_edge("t3")
        v2 = self.svc.snapshot().version
        self.assertLess(v0, v1)
        self.assertLess(v1, v2)


class TestConcurrentAccess(unittest.TestCase):
    """并发锁定/排除压力测试：所有状态变更在同一把锁内串行化。"""

    def test_concurrent_mutations_never_corrupt(self):
        svc = AnalysisService(build_ledger())
        errors = []

        def worker(seed):
            try:
                for i in range(40):
                    snap = svc.snapshot()
                    if not snap.chains:
                        continue
                    cid = snap.chains[(seed + i) % len(snap.chains)].chain_id
                    action = (seed + i) % 4
                    if action == 0:
                        try:
                            svc.lock_chain(cid, 1)
                        except ConflictError:
                            # 锁与当前排除状态冲突是合法业务结果
                            pass
                    elif action == 1:
                        try:
                            svc.unlock_chain(cid)
                        except ServiceError:
                            pass
                    elif action == 2:
                        # 排除可能使既有锁不可行：服务记录硬冲突并保留旧草稿，
                        # 不抛异常，这里仅断言状态自洽
                        svc.exclude_edge(f"t{(i % 7) + 1}")
                    else:
                        svc.include_edge(f"t{(i % 7) + 1}")
                    # 每个中间状态都必须自洽：无硬冲突时已分配不超容量
                    cur = svc.snapshot()
                    if cur.drafts and not [
                            c for c in cur.conflicts if c["severity"] == "hard"]:
                        usage = svc.txn_usage()
                        for txn_id, used in usage.items():
                            self.assertLessEqual(
                                used,
                                cur.ledger.transactions[txn_id].remaining_cents)
            except ConflictError:
                # 并发下锁/排除互相冲突是预期内的业务拒绝，不算损坏
                pass
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(k,)) for k in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], f"并发过程出现异常: {errors}")

    def test_concurrent_locks_on_same_chain(self):
        svc = AnalysisService(build_ledger())
        snap = svc.snapshot()
        cid = snap.chains[0].chain_id
        results = []

        def lock_one(v):
            try:
                svc.lock_chain(cid, v)
                results.append(("ok", v))
            except ConflictError:
                results.append(("conflict", v))

        threads = [threading.Thread(target=lock_one, args=(v,))
                   for v in (1, 2, 3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 最终只有一个值生效，且系统仍可行
        final = svc.snapshot()
        self.assertEqual(len(final.locks), 1)
        self.assertTrue(final.drafts or final.conflicts)
        self.assertEqual(len(results), 3)


if __name__ == "__main__":
    unittest.main()
