# 商品退货信号 → 质量改进与渠道处置

本应用把**商品上市后的售后记录**转化为可持续追踪的**质量调查决定**。它导入订单、退货、原因映射、质检复检、客服摘要、商品说明版本和渠道回执，按销售暴露与渠道政策分层计算风险，区分“退货率上升”与“产品缺陷率上升”，并把质量、商品、供应链三个角色的决定和后续改进结果串成完整证据链。

所有附带数据均为**本地合成数据**，标识和数值不对应真实个人、企业或交易。仅使用 Python 标准库，兼容 Python 3.11–3.14。

## 为什么不能直接看退货率

退货数量上升可能来自：

- **销量增长**：只比较退货件数而不除以同口径销售暴露；
- **渠道政策差异**：不同渠道退货窗口、退款政策不同；
- **编码口径切换**：同一原因换标签，历史对比出现假拐点；
- **观察期未满**：新订单的退货还没发生完，分母却被提前放大；
- **原因未知**：部分退货没有有效原因，直接当作“非质量问题”等于把未知清零。

应用对以上每一项都有显式处理，并在报告中列出被排除或仍未知的范围。

## 快速开始

```sh
python -m pip install -e .
python -m retail_return_quality_action --db var/app.db import --dir data
python -m retail_return_quality_action --db var/app.db review apply
python -m retail_return_quality_action --db var/app.db analyze --as-of 2026-11-25
python -m retail_return_quality_action --db var/app.db report --out reports/报告.md
```

不安装也可运行（等价写法）：

```sh
PYTHONPATH=src python -m retail_return_quality_action --db var/app.db import --dir data
```

完整演示（导入 → 复核 → 分析 → 三角色处置 → 改进核查 → 重新分析 → 报告）：

```sh
python scripts/run_demo.py --workdir /tmp/rrqa-demo
```

测试：

```sh
python -m unittest discover -s tests -v
```

## 核心设计

### 1. 暴露、成熟度与分层

- 观察窗口随 `policy_version` 变化（配置项 `policy_windows_days`），窗口未满的订单进入 `immature_units`，**既不进分母也不进分子**。
- 完全没有成熟暴露、但仍有未到期订单的对象标记为“观察（暴露未成熟）”，而不是“无问题”。
- 每个商品/批次与同一 `渠道 × 政策` 单元的背景率比较（间接标准化），期望值做平滑，避免小样本单元主导。
- 给出 SMR、单侧 Poisson 检验 p 值、跨对象 Benjamini–Hochberg q 值，以及精确保守的泊松置信区间。
- 明确输出 `mix_json`，把粗率差异拆成“渠道/政策构成”“单元内真实差异”和二者交互项（`residual`）。

### 2. 分类、修订与口径切换

- 原因编码经 `reason_aliases.csv` 桥接旧标签与新编码，再按 `received_at` 找到当时的 `reason_map` 版本。
- 退货复核以**追加修订**方式生效（`return_revision`）。重新分析会按新分类重算当前风险，但已退款、已执行供应商处置保持原样。
- 修订只应用 `reviewed_at <= 分析截止日` 的记录，因此“当时的判断”可以复现。

### 3. 证据链

- 质检发现只有 `confirmed` 才计权重（minor=1 / major=2 / critical=3）；`pending`/`disputed` 的重大发现会**阻断升级**并单独标注。
- 同一缺陷编码在 ≥2 个批次、或同批次 ≥2 次确认，视为重复问题；这是要求供应商调查的主要依据。
- 客服摘要用于印证或质疑编码；冲突会出现在结果 JSON 中，不会被自动改写。
- 商品说明版本只解释“说明理解类”退货，**不替代质量证据**。

### 4. 三角色工作流

| 角色 | 可做决定 |
| --- | --- |
| 质量（quality） | 确认问题等级、立案/关案/重开、要求供应商调查 |
| 商品（product） | 更新商品说明、调整包装、维持现状 |
| 供应链（supply） | 暂停未发货批次、要求供应商整改、解除暂停 |

约束：

- 角色不能越权写另一个角色的决定。
- 退款执行是既成事实，以追加事件（`refund_event`）记录；复核改判只影响风险重算，不改写已退款项。
- 对外动作（暂停、整改、说明发布、解除暂停）**必须有回执**才能完成；`rejected`/`partial` 回执不能当作成功。
- 有未完成动作时不能关案。
- 人工确认的等级与建议等级分开存储；重新分析只更新 `proposed_level`，不改写历史决定。

### 5. 改进核查

以措施生效日为锚点，比较前后两段**同一批批次**的质量退货率：

- 事件数与暴露来自同一批订单，后段观察期未满时直接返回 `immature_exposure`；
- 使用精确条件二项检验与 Clopper–Pearson 区间，得到 `improved / worse / not_conclusive / insufficient_data`；
- 结论写入 `improvement_check`，与案件、快照、操作人一起留痕。

## 目录

```
config/analysis.json          阈值与规则（不含代码）
data/*.csv                    合成资料（可用 scripts/generate_data.py 重建）
docs/资料契约.md              字段、关联与缺失语义
src/retail_return_quality_action/
  analysis.py                 分层、暴露校正、证据与分级
  reasons.py                  编码映射、别名桥、修订台账
  workflow.py                 立案、跨角色决定、动作状态机、改进核查
  importer.py                 幂等导入、逐行校验、问题登记
  report.py                   Markdown 报告与 CSV 导出
  stats.py                    Poisson / 卡方 / 二项 / BH（纯标准库）
  cli.py                      命令行入口
scripts/generate_data.py      确定性合成数据生成器
scripts/run_demo.py           端到端演示
tests/                        71 个标准库测试
```

## 命令行速查

```sh
# 导入与复核
python -m retail_return_quality_action import --dir data
python -m retail_return_quality_action review list

# 立案与三角色决定
python -m retail_return_quality_action open-case --level lot --id LOT-006 --actor QI-01 --reason "…"
python -m retail_return_quality_action grade CASE-0001 --grade escalate --actor QI-01 --reason "…"
python -m retail_return_quality_action decide CASE-0001 --decision hold-shipment --role supply --actor SC-01 --lot LOT-006 --reason "…"
python -m retail_return_quality_action decide CASE-0001 --decision revise-copy --role product --actor PD-02 --reason "…"

# 回执与动作推进
python -m retail_return_quality_action receipt --receipt-ref RCP-1 --type hold-ack --outcome accepted --received-at 2026-11-27 --lot LOT-006 --actor SC-01
python -m retail_return_quality_action action ACT-0001 --to confirmed --role supply --actor SC-01
python -m retail_return_quality_action action ACT-0001 --to complete --role supply --actor SC-01 --receipt RCP-1

# 退款执行与改进核查
python -m retail_return_quality_action refund RET-0007 --state paid --actor CS-01 --amount 1
python -m retail_return_quality_action check CASE-0001 --cutoff 2027-01-31 --anchor 2026-11-27
python -m retail_return_quality_action close CASE-0001 --actor QI-01 --outcome "改进已验证" --reason "…"
```

## 输出说明

`report` 同时生成：

- 中文 Markdown 报告：结论、分层表、数据质量与未知范围、案件状态、改进核查、阅读须知；
- `risk_results_<snapshot>.csv`：每个商品/批次的暴露、期望、SMR、p/q、证据与理由；
- `strata_<snapshot>.csv`：渠道/政策分层明细；
- `case_decisions_<snapshot>.csv`：所有人工决定（含角色与时间）；
- `manifest_<snapshot>.json`：快照、配置摘要、数据摘要。

快照同时记录 `config_digest` 与 `data_digest`，任何一次结论都可以说明“用哪份规则、哪份数据”得出。

## 合成数据中的可复现场景

| 场景 | 期望体现 |
| --- | --- |
| LOT-006 | 暴露校正后显著偏高 + 反复确认的关键缺陷 → **升级**，供应链暂停并登记回执 |
| LOT-011 | 退货量不足以显著，但确认的重复缺陷 → **调查** |
| LOT-002 | 退货偏高但重大发现仍待审/争议 → **观察**，不得凭未验证发现升级 |
| LOT-008 / LOT-004 / LOT-000 | 追溯缺口与未知原因 → 观察 + 明确标注缺口范围 |
| LOT-003 | 说明理解类偏高 → 建议核对商品说明，不按质量缺陷升级（LOT-013 的说明类退货多发生在观察期后，暂不计入） |
| 2026-11-15 | 原因编码口径切换 → 通过别名桥保持分类连续 |
| 2026-12 / 2027-01 | 整改后跟踪期 → 改进核查给出 `improved` |

## 限制

- 这是筛查与决策支持工具，不是因果推断；显著信号仍需质量人员调查确认。
- 背景率来自同一批资料，样本很小，q 值可能不显著；应用会把“统计不足以显著但证据确认重复”的情况单独说明。
- 回执与退款只校验状态和批次一致性，不校验外部系统的真实业务含义。
