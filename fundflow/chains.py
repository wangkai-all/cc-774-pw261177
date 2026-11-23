"""候选闭合往来链枚举。

关键设计：图的节点是**账户**，边是**逐笔流水（带日期、金额）**。
DFS 在流水级别行走，逐边检查账户衔接、时间窗口与账户限制，
而不是先把流水汇总成账户间的金额图再找环——汇总图会丢掉时间信息，
可能拼出时间顺序不成立的假环。

一条合法闭合链满足：
- 账户首尾闭合：to(e_i) == from(e_{i+1})，且 to(e_k) == from(e_1)；
- 时间有序：date(e_1) <= ... <= date(e_k)（strict_time 时为严格 <）；
- 首末跨度不超过 max_span_days；
- 链内每笔流水最多出现一次；
- 所有涉及账户通过黑白名单校验；
- 边数在 [min_chain_edges, max_chain_edges] 内。
"""
from __future__ import annotations

from .models import CandidateChain, ChainConfig, Ledger, Transaction, parse_date


class ChainEnumerationError(RuntimeError):
    pass


def _account_allowed(account: str, config: ChainConfig) -> bool:
    if account in config.blocked_accounts:
        return False
    if config.allowed_accounts is not None and account not in config.allowed_accounts:
        return False
    return True


def _time_ok(prev_date: str, next_date: str, strict: bool) -> bool:
    d1, d2 = parse_date(prev_date), parse_date(next_date)
    return d2 > d1 if strict else d2 >= d1


def _canonical_key(txn_ids: list[str]) -> tuple[str, ...]:
    """旋转等价的环用同一个 key：以字典序最小的流水 id 为起点旋转。"""
    n = len(txn_ids)
    idx = min(range(n), key=lambda i: txn_ids[i])
    return tuple(txn_ids[idx:] + txn_ids[:idx])


def _time_ordered_rotation(txn_ids: list[str], txns: dict[str, Transaction],
                           strict: bool) -> list[str]:
    """在所有旋转中找一个满足时间有序的表示（DFS 找到的原序必然满足之一）。"""
    n = len(txn_ids)
    for i in range(n):
        rot = txn_ids[i:] + txn_ids[:i]
        if all(
            _time_ok(txns[rot[j]].date, txns[rot[j + 1]].date, strict)
            for j in range(n - 1)
        ):
            return rot
    # 理论不可达：调用方保证原序列时间有序
    return list(txn_ids)


def enumerate_chains(ledger: Ledger,
                     config: ChainConfig | None = None) -> list[CandidateChain]:
    """枚举全部合法候选闭合链，去重后稳定排序并编号。"""
    cfg = config or ledger.config
    txns = ledger.transactions

    # 仅剩余额度 > 0、未排除的流水可入链
    active: list[Transaction] = [
        t for t in txns.values()
        if not t.excluded and not t.is_refund and t.remaining_cents > 0
    ]

    # 邻接表：账户 -> 该账户付出的、可用的流水
    adjacency: dict[str, list[Transaction]] = {}
    for t in active:
        if (not _account_allowed(t.from_account, cfg)
                or not _account_allowed(t.to_account, cfg)):
            continue
        adjacency.setdefault(t.from_account, []).append(t)
    for edges in adjacency.values():
        edges.sort(key=lambda e: (e.date, e.txn_id))

    discovered: dict[tuple[str, ...], list[str]] = {}

    def dfs(start: Transaction, path: list[Transaction], visited: set[str]) -> None:
        if len(discovered) >= cfg.max_candidates:
            raise ChainEnumerationError(
                f"候选链数量超过上限 {cfg.max_candidates}，请收紧时间窗口、"
                "链长或账户限制后重试"
            )
        current = path[-1]
        cur_date = parse_date(current.date)
        start_date = parse_date(start.date)

        for nxt in adjacency.get(current.to_account, []):
            # 时间窗口：相对首笔的跨度
            nd = parse_date(nxt.date)
            if (nd - start_date).days > cfg.max_span_days:
                continue  # 已按日期排序，break 也可，但 break 依赖排序一致性
            if not _time_ok(current.date, nxt.date, cfg.strict_time):
                continue
            if nxt.txn_id in visited:
                continue

            # 闭合：下一笔的收款方 == 首笔的付款方
            if nxt.to_account == start.from_account:
                cycle = path + [nxt]
                if cfg.min_chain_edges <= len(cycle) <= cfg.max_chain_edges:
                    ids = [e.txn_id for e in cycle]
                    key = _canonical_key(ids)
                    if key not in discovered:
                        discovered[key] = ids
                # 闭合后不再沿该边继续（继续会把闭合环套进更长路径，
                # 更长的环由别的起点枚举负责，避免重复与无界增长）
                continue

            if len(path) + 1 < cfg.max_chain_edges:
                visited.add(nxt.txn_id)
                path.append(nxt)
                dfs(start, path, visited)
                path.pop()
                visited.remove(nxt.txn_id)

    for start_edge in sorted(active, key=lambda e: (e.date, e.txn_id)):
        if (not _account_allowed(start_edge.from_account, cfg)
                or not _account_allowed(start_edge.to_account, cfg)):
            continue
        dfs(start_edge, [start_edge], {start_edge.txn_id})

    # 统一成时间有序的表示，再稳定排序、编号
    ordered: list[list[str]] = []
    for ids in discovered.values():
        rep = _time_ordered_rotation(ids, txns, cfg.strict_time)
        ordered.append(rep)
    ordered.sort(key=lambda ids: (
        len(ids),
        txns[ids[0]].date,
        _canonical_key(ids),
    ))

    # 链 id 必须对边内容稳定：用规范化边序列的短哈希，保证排除/恢复其他边后，
    # 同一条链的 id 不变（人工锁定才能正确跟踪）。显示序号另存 display_index。
    import hashlib
    chains: list[CandidateChain] = []
    for idx, ids in enumerate(ordered):
        key = "|".join(_canonical_key(ids))
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
        chains.append(CandidateChain(chain_id=f"CH-{digest}", txn_ids=ids))
    return chains
