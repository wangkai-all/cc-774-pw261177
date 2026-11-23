# 资金往来闭合关系分析系统

多家公司之间的资金往来天然可能成环——正常货款链与需要关注的循环在**汇总图**上
看起来一样。本系统用**虚构收支明细**，在逐笔流水（带时间、账户、金额、单据）级别
重建资金关系：先按业务单据抵销明确退款，再在金额、时间窗口与账户限制下寻找
能够闭合的往来链，并保留多种数学解释。

> **重要语义**：算法分解出的链条只是资金关系的一种数学解释，
> **不构成任何违法事实认定**。系统不连接任何银行账户，全部数据为虚构。

## 设计原则与关键约束

1. **先抵销、再分析**：退款必须凭业务单据（`original_txn_id` + 单据校验）关联原
   流水，校验账户反向、时间合理、累计不超额；抵销后减少原流水可分配额度。
2. **不在汇总图上找环**：图节点是账户，边是**逐笔流水**。DFS 在流水级别行走，
   逐边检查账户衔接、时间有序与时间窗口，避免汇总图丢掉时间信息拼出假环。
3. **流水可拆分共用**：同一笔流水可部分分配给多条链，所有链在该流水上的
   分配总额 ≤ 抵销后的剩余额度（容量约束）。
4. **时间窗口硬约束**：链上流水日期非递减（可配置严格递增），首末跨度不超过
   `max_span_days`。
5. **两种解释目标，保留多解**：
   - `max_closure`：最大化闭合资金总额；
   - `min_chains`：在**保持最大闭合金额**的前提下最少使用链条（词序目标）。
   - 同最优值的不同分解通过 no-good 割枚举，全部保留供人工研判。
6. **人工优先**：
   - 排除业务边后受影响资金自动重新分配；
   - 锁定的链分配以等式约束参与重算，算法不得改动；
   - 重算只产生新的草稿版本，**已确认解释永久保留、不被后台重算覆盖**；
   - 锁与排除冲突时记录硬冲突并保留旧草稿，而不是静默改写。

## 从零实现，零第三方依赖

| 能力 | 实现位置 | 说明 |
| --- | --- | --- |
| 明细/退款/链/解数据模型 | `fundflow/models.py` | 金额内部统一整数分，JSON 金额按元换算 |
| 退款单据抵销 | `fundflow/refund.py` | 方向/时间/单据/累计额全校验，严格与容错两模式 |
| 候选闭合链枚举 | `fundflow/chains.py` | 流水级 DFS、时间窗口、账户黑白名单、内容哈希稳定链 id |
| 线性/整数规划 | `fundflow/solver.py` | 手写两阶段单纯形（`Fraction` 精确运算、Bland 规则）+ 分支定界 + 多解枚举 |
| 小样例穷举对照 | `fundflow/brute.py` | 与 LP 完全独立的整数递归枚举，带节点/上界保护 |
| 人工交互状态机 | `fundflow/service.py` | 锁定/排除/冲突/版本确认，可重入锁线程安全 |
| Web | `fundflow/web/` | 标准库 `ThreadingHTTPServer` + 原生 JS，无构建步骤 |

## 快速开始

需要 Python 3.11+，无需安装任何依赖。

```bash
# 命令行演示
python3 demo.py samples/small_sample.json
python3 demo.py samples/large_sample.json

# 启动网页（默认 http://127.0.0.1:8000/）
python3 run_server.py samples/small_sample.json
python3 run_server.py samples/large_sample.json --port 8001

# 运行全部测试（57 个，含 LP/穷举对照与并发压力测试）
python3 run_tests.py
```

> 运行读取外部数据的 Python 时使用 `-I` 隔离模式（测试启动器已内置），
> 防止工作目录被植入的同名模块干扰导入。

## 网页功能

- **资金流水时间线**：按日期展示每笔虚构流水的金额、抵销后剩余、当前已分配；
  点击业务边可人工排除/恢复。
- **候选链与解释方案**：多解以标签页切换，显示闭合总额与链数。
- **链详情抽屉**：点击任意链查看链上每笔流水的**本链使用金额**与**剩余金额**，
  可直接锁定/修改/解除分配，并确认当前解释。
- **冲突面板**：超额锁定、锁链边被排除等以硬/软冲突显式展示，操作被拒绝时
  返回 HTTP 409。
- **已确认解释区**：重算后仍保留，带版本号与备注。
- 页首常驻「不构成违法事实认定」声明。

## REST API 摘要

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| GET | `/api/state` | 全量快照（流水、链、草稿、确认解、锁、冲突、退款错误） |
| GET | `/api/chains/{id}` | 链上每笔使用金额与剩余金额 |
| POST | `/api/objective` | 切换 `max_closure` / `min_chains` |
| POST | `/api/exclude` `/api/include` | 人工排除/恢复业务边并重算 |
| POST | `/api/lock` `/api/unlock` | 锁定（等式约束）/解除链分配 |
| POST | `/api/confirm` | 确认草稿解（后续重算不可覆盖） |
| POST | `/api/recompute` | 后台重算 |

## 测试覆盖

`tests/` 共 57 个用例：

- `test_refund.py`：部分/多次抵销、超额拒绝、方向与时间校验、单据不一致、幂等；
- `test_chains.py`：时间窗口、严格时序、同日环、黑白名单、去重、容量上限；
- `test_solver.py`：单纯形已知答案、不可行/退化、分支定界、**LP 与穷举逐项对照**、
  锁定等式、环间共用资金的部分分配；
- `test_service.py`：排除重分配、确认解抗重算、硬冲突、版本单调、**6 线程并发
  锁定/排除压力测试**；
- `test_web.py`：随机端口起服务的 HTTP 端到端测试（含 409 冲突路径）；
- `test_samples_crosscheck.py`：两份样例文件回归，黑名单/窗口外环/坏退款容错，
  以及穷举最优集合对 ILP 每个解的包含校验。

## 数据格式（虚构）

```json
{
  "transactions": [
    {"txn_id": "t1", "date": "2026-01-05", "from_account": "A",
     "to_account": "B", "amount": 100, "doc_ref": "PO-1",
     "is_refund": false}
  ],
  "refunds": [
    {"refund_id": "r1", "date": "2026-01-12", "from_account": "B",
     "to_account": "A", "amount": 40, "original_txn_id": "t1",
     "doc_ref": "RF-1"}
  ],
  "config": {"max_span_days": 30, "max_chain_edges": 6,
             "strict_time": false, "blocked_accounts": []}
}
```

- `amount` 单位为元（整数或两位小数），加载时换算为整数分；
- 退款流水在 `transactions` 中以 `"is_refund": true` 标记，抵销后不参与找环；
- `allowed_accounts` 白名单与 `blocked_accounts` 黑名单可同时配置，白名单优先。

## 目录结构

```
fundflow/
  models.py      数据模型与 JSON 读写
  refund.py      单据级退款抵销
  chains.py      时间/账户约束下的候选闭合链枚举
  solver.py      两阶段单纯形 + 分支定界 ILP + 多解
  brute.py       小样例整数穷举（独立对照）
  service.py     人工交互与版本管理（线程安全）
  web/           HTTP 服务与静态网页
samples/         small_sample.json / large_sample.json（均为虚构）
tests/           57 个单元与端到端测试
run_server.py    网页启动器
run_tests.py     测试启动器
demo.py          命令行演示
```
