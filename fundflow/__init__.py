"""fundflow —— 资金往来闭合关系分析系统（从零实现，不接银行账户）。

模块划分：
- models:  明细、退款、候选链、分配、解版本等数据结构与 JSON 读写
- refund:  按业务单据抵销明确退款
- chains:  时间窗口与账户约束下的候选闭合链枚举
- solver:  纯 Python 两阶段单纯形 + 分支定界整数规划，支持多解
- brute:   小样例整数穷举，用于与线性规划结果独立对照
- service: 人工排除、锁定、冲突检测、解版本管理（线程安全）
- web:     标准库 HTTP 服务与原生 JS 网页
"""

from .models import (
    Transaction,
    RefundRecord,
    CandidateChain,
    ChainAllocation,
    SolutionRecord,
    Ledger,
    ChainConfig,
)

__all__ = [
    "Transaction",
    "RefundRecord",
    "CandidateChain",
    "ChainAllocation",
    "SolutionRecord",
    "Ledger",
    "ChainConfig",
]
