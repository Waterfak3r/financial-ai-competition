# C-01 多公司评测与人工复核准备

本文记录 C-01 v2 的协议边界和复核流程。来源 URL、SHA256、页码、使用范围与注入定义以 [`evaluation/multicompany_evaluation_v2.json`](../evaluation/multicompany_evaluation_v2.json) 为准；唯一评测输出在本机 `artifacts/evaluations/<evaluation_id>/`，不纳入 Git。

## 来源与样本角色

| 公司 | 样本角色 | 事实状态 | 来源与边界 |
|---|---|---|---|
| 海天味业 603288 | 适配参考 | agent_provisional_pending_human | 巨潮资讯 2024 年报；仅本地开发/评测，未确认再分发许可 |
| 贵州茅台 600519 | 公司隔离的 post-hoc holdout 参考 | agent_provisional_pending_human | 公司官网 2024 年报；未确认 PDF、片段或页图再分发许可 |
| 五粮液 000858 | 公司隔离的 post-hoc holdout 参考 | agent_provisional_pending_human | 巨潮资讯 2024 年报；未确认 PDF、片段或页图再分发许可 |

茅台和五粮液已有历史运行及 provisional 转录。按公司隔离有助于呈现跨公司适配情况，但本轮不能称为未见样本或盲测。C-01 不把任何一家标注为无舞弊，也不将异常信号解释为舞弊事实。

协议覆盖同一报告披露的 2024 本期和 2023 比较列：营业收入、归母净利润、经营活动现金流量净额、披露的非经常性损益合计。表中 2024/2023 原值关系仅表示同份年报列示数字的高低；不把该方向自动转成增长、原因、异常或风险结论。非经常性损益合计保持原披露表范围，scope unknown，不直接当作扣非归母净利润。单位、币种、期间或比较性缺证时保留 unknown。

会计变化和追溯披露单独列出原 PDF 页：海天有本期会计变化说明、政策/估计变更及追溯披露；茅台部分变更事项有明确勾选，但前期差错追溯和期初调整证明不足，状态保留 unknown；五粮液报告披露无需追溯调整且列出调整金额为 0.00 元，但当前独立可比性核验仍为 `insufficient_evidence`，不由人工转录覆盖。每家公司各披露定位及原文匹配检查在 v2 协议和评测归档中保存，仍须真人核对。

## 分析隔离与指标

执行器先校验 `source.json`、原始 PDF 哈希、字节数和页数，然后直接从已登记 PDF 调用确定性年度分析。该调用只接收 PDF 路径、公司代码、报告年度和文档标识；不读取 `evaluation/cases/` 标签、人工复核字段、注入定义或舞弊标签，也不调用模型。分析结束后，再独立调用事实核验器并构造每家公司五类人工注入：错金额、错单位、错期间、错报表口径、无支持 Claim。

真实事实按 `verified`、`conflict` 和 `insufficient_evidence` 分别报告覆盖。`insufficient_evidence` 是弃权/unknown，不等于冲突，也不等于正确。所有真实事实都保持 `agent_provisional_pending_human`，人工 golden 准确率、舞弊误报率和舞弊漏报率分母为 0。

受控注入的完整分母为 3 家公司 × 5 种错误，共 15 项。`verified` 记为错误接受，`conflict` 记为拒绝，`insufficient_evidence` 记为弃权；三项均以全部注入项为分母分别统计，弃权不算正确拒绝。无核验基线只做 schema 构造校验并信任输入，属于透明的 schema-only 对照，不代表历史产品版本或模型表现。每条独立核验分支记录耗时、输入摘要、独立核验状态、冲突/限制理由及公司和类别明细；schema-only 基线不单独计时。

## 运行与人工复核

在根目录运行：

```powershell
python scripts/evaluate_multicompany.py
```

程序产生新的 `artifacts/evaluations/<evaluation_id>/`，内含 `result.json`、Markdown 摘要、每家公司分析归档及逐事实独立重核记录。ID 已存在时程序失败，不覆盖旧结果。输出明确记载模型未调用、来源 SHA256、代码版本、holdout 限制、基线定义和各项分母。

生成内部复核包：

```powershell
python scripts/build_human_review_packet.py --evaluation-id <evaluation_id>
```

复核包在另一个唯一评测目录写入 `review_packet.json`、`review_packet.md`、`index.html` 和 `pages/`。它读取已归档的事实/核验记录并按白名单页码从本地原始 PDF 渲染页图，不复制整份 PDF。结构化项目列公司、报告、source URL/hash、表名、PDF 页、行列、原值/规范值、单位/币种/口径/期间和独立核验结果，并为真人结论、复核人、日期、依据留空。五粮液因币种披露不足，没有独立核验定位时，复核包可展示现有 provisional 案例中的候选页码/表行列供人工查看；该候选仍标记未核实，不进入独立证据、核验统计或标签分析，也不能把 `insufficient_evidence` 升级为通过。

复核步骤：确认来源与 PDF 页；核对表题、行列、金额、单位倍率、币种、合并/母公司口径和期间；分别复核会计政策/估计变化、差错更正、追溯与可比性；把分歧记为 conflict、把证据缺口保留为 unknown。不能根据分析器状态自动填入人工结论。

真人复核人尚未指定，另一台电脑和网络边界也未确定，因此 human gate 必须保持 `pending`。本机页图、原文片段和复核包含有受使用范围约束的年报内容，不能提交为公开 Git 资产。人工复核披露事实也不构成舞弊定性或再分发授权。
