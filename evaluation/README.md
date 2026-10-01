# 评测案例与协议

本文说明评测资产，不作为应用分析输入。标签隔离、分母和使用条件以 [数据规则](../docs/data-policy.md)为准。

- `cases/` 按“公司 + 报告期”组织案例。600519、000858 两份 2024 年案例是真实年报的财务披露事实参考，状态为 agent_provisional_pending_human；尚未人工复核，也未分配 train/validation/test 集。
- 上述标签只记录四项指标在原年报 2024/2023 比较列的数值与出处，不是人工 golden、异常标签、舞弊标签或无舞弊结论。原文和解析文本位于 data/raw、data/processed，未确认再分发许可。
- `verification_ablation_cases_v1.json` 是已存在的单公司受控错误消融协议，由 `scripts/evaluate_verification_ablation.py` 执行。两个 clean 值来自 agent 转录，五个错误为人工构造注入；不是多公司人工 golden。依赖原文和协议引用的归档输入，重跑前准备所需文件。
- CLI 结果与弃权以案例 run_id 为准，提取失败不等于指标为零；评测标签不得进入分析材料或模型检索上下文。
- 既往分母和统计见 [验收记录](../docs/verification-history.md)。新增协议须单独版本化，明确未知样本、弃权与总体覆盖，不能回写旧运行以匹配新方案。
