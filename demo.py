#!/usr/bin/env python3
"""命令行演示：退款抵销 -> 候选链 -> 两种解释目标 -> 人工排除/锁定重算。

用法：
    python3 demo.py [samples/small_sample.json]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fundflow.models import Ledger, cents_to_yuan
from fundflow.service import AnalysisService


def line(char="─", n=72):
    print(char * n)


def show(title, svc):
    snap = svc.snapshot()
    line()
    print(f"【{title}】版本 v{snap.version}  目标={snap.objective}")
    if snap.refund_errors:
        print("  退款校验错误（已跳过）:")
        for e in snap.refund_errors:
            print("   !", e)
    if snap.conflicts:
        print("  冲突:")
        for c in snap.conflicts:
            print(f"   [{c['severity']}]", c["message"])
    print(f"  候选闭合链 {len(snap.chains)} 条；草稿解 {len(snap.drafts)} 个")
    for i, sol in enumerate(snap.drafts):
        print(f"  解释{i + 1}: 闭合 ¥{cents_to_yuan(sol.total_closed_cents)}"
              f"，{sol.chain_count} 条链")
        for a in sol.allocations:
            lock = " 🔒" if a.locked else ""
            print(f"      {a.chain_id}{lock}  ¥{cents_to_yuan(a.amount_cents)}")
    print("  已确认解释:", [s.solution_id for s in snap.confirmed] or "无")


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "samples/small_sample.json"
    ledger = Ledger.from_json(path)
    svc = AnalysisService(ledger)

    show("初始：最大闭合金额", svc)

    # 目标切换
    svc.set_objective("min_chains")
    show("切换为最少链条（保持最大闭合金额）", svc)
    svc.set_objective("max_closure")

    # 人工锁定一条链
    snap = svc.snapshot()
    if snap.chains:
        cid = snap.chains[0].chain_id
        try:
            svc.lock_chain(cid, 1)
            show(f"锁定 {cid}=1 分后重算", svc)
        except Exception as exc:  # noqa: BLE001
            print("锁定失败:", exc)

    # 人工排除一条业务边
    txn = snap.ledger.txns_sorted()[0].txn_id
    svc.exclude_edge(txn)
    show(f"人工排除业务边 {txn} 后重新分配受影响资金", svc)
    svc.include_edge(txn)

    # 确认一个解，再后台重算，验证不被覆盖
    sid = svc.snapshot().drafts[0].solution_id
    svc.confirm_solution(sid, note="演示确认")
    svc.recompute()
    show("确认解释后执行后台重算（确认解必须保留）", svc)

    line()
    print("提示：链条仅为资金关系的数学解释，不构成任何违法事实认定。")


if __name__ == "__main__":
    main()
