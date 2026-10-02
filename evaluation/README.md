# 评测案例与协议

本文说明评测资产，不作为应用分析输入。标签隔离、分母和使用条件以 [数据规则](../docs/data-policy.md)为准。

- `cases/` 按“公司 + 报告期”组织案例。600519、000858 两份 2024 年案例是真实年报的财务披露事实参考，状态为 agent_provisional_pending_human；尚未人工复核，也未分配 train/validation/test 集。
- 上述标签只记录四项指标在原年报 2024/2023 比较列的数值与出处，不是人工 golden、异常标签、舞弊标签或无舞弊结论。原文和解析文本位于 data/raw、data/processed，未确认再分发许可。
- `verification_ablation_cases_v1.json` 是已存在的单公司受控错误消融协议，由 `scripts/evaluate_verification_ablation.py` 执行。两个 clean 值来自 agent 转录，五个错误为人工构造注入；不是多公司人工 golden。依赖原文和协议引用的归档输入，重跑前准备所需文件。
- `multicompany_evaluation_v2.json` 与 `scripts/evaluate_multicompany.py` 是 C-01 三公司协议。海天为适配参考；茅台、五粮液按公司隔离作为 post-hoc holdout 参考，但已有历史运行和 provisional 转录，因此不是盲测。
- C-01 先仅用登记的原始 PDF 运行年度分析，再直接重读各事实并做五类受控错误注入；协议中的事实、错误、复核字段不会进入分析输入。所有真实事实仍标 `agent_provisional_pending_human`，不进入人工 golden 或舞弊误报/漏报分母。
- C-01 把注入错误的 `verified`、`conflict`、`insufficient_evidence` 分别计为错误接受、拒绝和弃权；弃权不算正确拒绝。原始事实只报核验覆盖、冲突、证据不足和 unknown，不报准确率。无独立核验基线仅为 schema-only 输入信任，不代表历史产品或模型运行。
- 运行及本地归档：`python scripts/evaluate_multicompany.py`。评测结果写到唯一的 `artifacts/evaluations/<evaluation_id>/`，已有目录会拒绝覆盖。
- 根据评测归档生成人工复核包：`python scripts/build_human_review_packet.py --evaluation-id <evaluation_id>`。输出为新的 `artifacts/evaluations/<packet_id>/`，含结构化待填字段、Markdown/HTML 和本地 PDF 页图；不要把原文页图、片段或复核包公开提交。
- 真人复核人和其他电脑/网络边界尚未确定，C-01 human gate 保持 `pending`。包中复核人、日期、结论及依据均为空；程序不会自动通过。
- 流程、来源/使用范围、会计变化披露、统计口径和限制见 [多公司评测说明](../docs/multicompany-evaluation.md)。
- CLI 结果与弃权以案例 run_id 为准，提取失败不等于指标为零；评测标签不得进入分析材料或模型检索上下文。
- 既往分母和统计见 [验收记录](../docs/verification-history.md)。新增协议须单独版本化，明确未知样本、弃权与总体覆盖，不能回写旧运行以匹配新方案。
