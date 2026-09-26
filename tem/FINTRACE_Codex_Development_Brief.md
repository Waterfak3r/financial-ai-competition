# FINTRACE 项目下一阶段开发任务说明

> 项目：`financial-ai-competition`  
> 目标：面向上市公司财务报告分析，构建一个可追溯、可核验的 AI 财务异常分析系统。  
> 本文件用于指导 Codex / 开发 Agent 继续实现项目。  
> 核心原则：**不要继续扩展功能宽度，优先打通一条高质量、可验证、可演示的端到端黄金路径。**

---

## 1. 项目定位

本项目不是“让大模型直接判断公司是否舞弊”，也不是单纯的 ChatPDF。

核心目标是：

> **让大模型负责组织财务分析，让所有关键数字、计算和结论都能够回到原始财报独立核验。**

系统核心链路应为：

```text
Financial Report PDF
        ↓
Fact Extraction
        ↓
Deterministic Financial Analysis
        ↓
Candidate Anomaly Detection
        ↓
MD&A / Notes Investigation
        ↓
Independent Verification
        ↓
Auditable Report
```

项目真正的核心卖点应从“Multi-Agent”转向：

> **Evidence-grounded Financial Analysis + Independent Verification**

多智能体只是实现手段，不应为了展示 Agent 数量增加无必要复杂度。

---

## 2. 当前项目状态

当前已实现或基本可用的能力：

- 文本型 PDF 逐页解析；
- 基于 PyMuPDF 获取文本块、页码与 bbox；
- 提取四项年度财务事实：
  - 营业收入；
  - 归属于母公司股东的净利润；
  - 经营活动产生的现金流量净额；
  - 非经常性损益合计；
- 同一报告年度列与比较列之间的确定性同比计算；
- 原始 PDF SHA256 校验；
- 基于引用 bbox 重新读取原始金额；
- FastAPI `annual-precheck` 接口；
- Chat Completions 文本连接器；
- 单元测试；
- 海天味业 2024 年报已作为真实公开样例跑通过解析和基础预检。

当前仍未完整实现：

- LangGraph 工作流；
- 完整 Agent 分析流程；
- 真正独立的原文核验；
- 年度列 / 表头 / 行列关系的独立确认；
- 更完整的财务指标体系；
- 异常检测闭环；
- MD&A / 附注调查；
- Claim / Evidence 结构；
- 报告生成；
- 前端；
- 端到端评测。

---

# 3. 下一阶段总原则

从现在开始，开发优先级必须遵循以下原则：

1. **先完成端到端闭环，再增加新功能。**
2. **优先提升事实正确性、口径正确性和可核验性。**
3. **不要让 LLM 执行确定性财务计算。**
4. **不要让多个模型或多个 Agent 的一致意见替代原文核验。**
5. **任何关键数字必须有明确原文来源。**
6. **任何推论必须与事实分开存储。**
7. **任何不可确定的数据必须允许 abstain / evidence insufficient。**
8. **优先完成真实年报黄金样例，而不是追求大量公司覆盖。**
9. **不要为了“多智能体”人为拆分代码。**
10. **当前不做与比赛核心展示无关的工程扩张。**

---

# 4. 第一阶段目标：打通 Golden Path

当前最重要的交付物：

> 使用一份真实上市公司年度报告，完整跑通从 PDF 到最终可核验分析报告的全过程。

优先继续使用：

```text
603288 海天味业
2024 年年度报告
```

目标链路：

```text
Upload / Register PDF
        ↓
Parse PDF
        ↓
Extract Financial Facts
        ↓
Confirm Period / Unit / Scope / Column
        ↓
Financial Calculations
        ↓
Run Anomaly Rules
        ↓
Retrieve MD&A / Notes Evidence
        ↓
Generate Candidate Claims
        ↓
Independent Verification
        ↓
Generate Final Report
```

只有这条链路完全可运行之后，才开始大规模扩指标、扩公司、扩 Agent。

---

# 5. P0：重新设计 FinancialFact 数据模型

这是下一阶段最高优先级之一。

禁止使用过于简单的数据结构，例如：

```json
{
  "name": "营业收入",
  "value": 26900977516.70,
  "page": 81
}
```

应建立一个正式的 `FinancialFact` Schema。

建议至少包含：

```python
FinancialFact
├── fact_id
├── metric_id
├── label_raw
├── value_raw
├── value_normalized
├── currency
├── unit
├── period_start
├── period_end
├── period_type
├── statement_type
├── scope
├── comparison_role
├── restatement_status
├── source_document_id
├── source_pdf_page
├── source_printed_page
├── source_bbox
├── source_row_label
├── source_column_label
├── extraction_method
├── extraction_confidence
└── notes
```

### 5.1 period_type

至少支持：

```text
instant
duration
```

例如：

资产负债表：

```text
2024-12-31
period_type = instant
```

利润表：

```text
2024-01-01 → 2024-12-31
period_type = duration
```

后续半年报、季报和环比逻辑必须依赖此字段。

---

## 5.2 scope

至少支持：

```text
consolidated
parent
unknown
```

禁止把：

```text
合并现金流
```

和：

```text
母公司利润
```

直接混算。

---

## 5.3 comparison_role

至少支持：

```text
current
comparative
unknown
```

例如：

2024 年报中的：

```text
2024 → current
2023 → comparative
```

---

## 5.4 restatement_status

建议支持：

```text
not_restated
restated
unknown
```

如果无法确认是否为追溯调整数据：

```text
restatement_status = unknown
```

不得自动假定可比。

---

# 6. P0：新增 TableCellEvidence

当前系统能够验证“某个金额确实出现在 bbox 中”，但这还不等于确认了该金额的财务语义。

需要增加：

```python
TableCellEvidence
```

建议包含：

```python
TableCellEvidence
├── evidence_id
├── document_id
├── pdf_page
├── printed_page
├── table_title
├── row_label
├── column_label
├── value_raw
├── value_normalized
├── unit
├── bbox
├── surrounding_text
└── extraction_method
```

需要支持判断：

```text
某个数字
属于哪个表
属于哪一行
属于哪一列
是什么单位
是什么期间
```

核心目标：

> 不只是确认“数字存在”，而是确认“数字属于正确的行、正确的列、正确的口径”。

---

# 7. P0：真正实现 Independent Verification

现有 bbox 金额复核不能直接称为完整独立核验。

Verification 模块必须尽量独立于 Extraction 模块。

基本原则：

```text
Extraction 输出：
    营业收入 = X
    2024
    consolidated
    CNY

Verification：
    重新定位原始页面
        ↓
    重新判断表格
        ↓
    重新判断表头
        ↓
    重新判断行标签
        ↓
    重新确认单位
        ↓
    重新确认对应数字
        ↓
    与 Extraction 输出比较
```

不能仅做：

```text
读取 Extraction bbox
→ 找到同一个数字
→ 判定通过
```

---

## 7.1 VerificationStatus

统一定义：

```text
verified
conflict
insufficient_evidence
```

不要使用模糊布尔值：

```text
true / false
```

---

## 7.2 VerificationResult

建议结构：

```python
VerificationResult
├── verification_id
├── target_type
├── target_id
├── status
├── checks[]
├── conflicts[]
├── evidence_ids[]
├── limitations[]
└── verified_at
```

---

# 8. P0：新增 Claim 数据模型

项目必须正式区分：

```text
事实
计算
推论
假设
```

建议新增：

```python
Claim
```

结构：

```python
Claim
├── claim_id
├── claim_type
├── text
├── supporting_fact_ids[]
├── supporting_evidence_ids[]
├── calculation_ids[]
├── verification_status
├── limitations[]
└── follow_up_items[]
```

`claim_type` 至少包括：

```text
fact
calculation
inference
hypothesis
```

例如：

### fact

```text
经营活动现金流量净额同比下降 6.96%
```

### inference

```text
利润与经营现金流变化存在一定背离
```

### hypothesis

```text
该差异可能与回款节奏变化有关
```

### follow-up

```text
进一步核查应收账款、信用政策、合同负债及经营性现金流附注
```

禁止把以上内容合并成一个未经区分的大模型自然语言段落。

---

# 9. P0：确定性 Finance Engine

所有确定性财务计算必须由 Python 完成。

禁止让 LLM 直接完成：

```text
YoY
QoQ
财务比率
金额差额
规则触发计算
风险指数
```

LLM 只允许：

```text
解释
规划
提出核查方向
组织报告
```

---

## 9.1 calculation 对象

建议定义：

```python
CalculationResult
├── calculation_id
├── formula_id
├── input_fact_ids[]
├── formula_expression
├── output_value
├── unit
├── status
├── failure_reason
└── rule_version
```

例如：

```json
{
  "formula_id": "yoy_growth",
  "input_fact_ids": [
    "ocf_2024",
    "ocf_2023"
  ],
  "formula_expression": "(current / previous) - 1",
  "output_value": -0.0696
}
```

---

# 10. P1：异常规则

第一版不需要复杂机器学习。

先实现透明规则。

至少支持：

```text
利润与经营现金流背离
非经常性损益影响
应收账款增长偏离
存货增长偏离
```

---

## 10.1 不把风险指数作为第一卖点

当前可以保留规则总分设计，但 UI 和报告中应优先展示：

```text
可判断规则：4 / 4
触发规则：2 / 4
```

例如：

```text
✓ 利润与现金流背离
✓ 应收增长偏离
○ 存货增长偏离
○ 非经常性损益影响
```

比直接展示：

```text
Risk Score = 50
```

更可靠。

总分只有在开发集验证阈值后才强化展示。

---

# 11. P1：扩展核心财务字段

不要直接开发“全财报字段抽取”。

下一批只增加支持异常分析所需的字段。

优先：

```text
营业收入
营业成本
归母净利润
扣非归母净利润
经营活动现金流量净额
应收账款
存货
总资产
总负债
非经常性损益
```

必要时增加：

```text
合同负债
应收票据
信用减值损失
资产减值损失
```

但这些属于下一层优先级。

---

# 12. P1：MD&A / Notes Investigation

不要做传统情绪分析作为核心能力。

不要优先开发：

```text
positive words
negative words
sentiment score
```

重点应该是：

```text
数字异常
    ↓
寻找管理层解释
    ↓
判断解释是否存在
    ↓
判断解释是否支持该变化
    ↓
保留替代解释
```

例如：

```text
Revenue YoY +9.53%
```

应搜索：

```text
销量
价格
产品结构
渠道
区域
原材料
客户
```

输出：

```python
ManagementExplanation
├── target_claim_id
├── source_text
├── source_page
├── explanation_type
├── support_status
└── limitations
```

`support_status` 可考虑：

```text
supported
partial
absent
unclear
```

---

# 13. P1：简化 LangGraph

第一版不要为了展示“多智能体”建立大量独立 Agent。

建议实际 workflow：

```text
Extract
    ↓
Analyze
    ↓
Investigate
    ↓
Verify
    ↓
Report
```

Manager 主要承担：

```text
状态管理
路由
补检
预算管理
停止条件
错误恢复
```

不要把 Manager 实现成复杂聊天 Agent。

---

## 13.1 推荐目录

可考虑：

```text
backend/src/finagent/

├── ingestion/
├── schemas/
├── extraction/
├── finance/
├── analysis/
├── investigation/
├── verification/
├── reporting/
├── workflow/
├── llm/
├── audit/
└── api/
```

Agent / workflow 代码不要重复实现：

```text
PDF parsing
financial formulas
verification rules
```

这些必须放在独立业务模块。

---

# 14. P1：报告生成

报告生成只允许使用：

```text
verified facts
verified calculations
verified evidence
```

如果某项状态为：

```text
conflict
```

或者：

```text
insufficient_evidence
```

必须明确显示。

禁止自动隐藏失败节点。

---

## 14.1 报告建议结构

```text
1. Company / Report Metadata

2. Key Financial Metrics

3. Performance Changes

4. Candidate Anomalies

5. Management Explanations

6. Verification Results

7. Conflicts / Missing Evidence

8. Follow-up Review Items

9. Scope / Limitations
```

---

# 15. P1：前端目标改为 Evidence Explorer

不要优先做普通 AI Dashboard。

核心页面应该让用户：

> 点击任何数字或异常，都可以回到原始财报证据。

建议交互：

```text
营业收入
269.01 亿
YoY +9.53%
✓ Verified
```

点击后显示：

```text
PDF Page 81

[高亮对应表格数字]
```

同时展示：

```text
Raw value
Normalized value
Unit
Period
Scope
Row
Column
Formula
Verification status
```

异常详情：

```text
Evidence
Calculation
Interpretation
Alternative Explanation
Follow-up Checks
```

---

# 16. P2：Verification Ablation 实验

这是整个项目后续最重要的实验之一。

需要人工构造至少五类错误：

```text
A. Wrong Value
B. Wrong Unit
C. Wrong Period / Year Column
D. Wrong Financial Scope
E. Unsupported Claim
```

例如：

真实：

```text
2024 营业收入 = 26,900,977,516.70
```

注入错误：

```text
2024 营业收入 = 24,559,312,356.59
```

实际上该值来自 2023 比较列。

比较：

```text
Without Verification
vs
With Verification
```

至少输出：

```text
error detection rate
false blocking rate
missed errors
abstention rate
```

最终应该能够回答：

> Independent Verification 是否真的减少了事实错误？

---

# 17. P2：评测体系

暂时不要把 ROC-AUC 当作当前首要指标。

在没有可靠真实舞弊标签集之前，优先评测系统可靠性。

第一阶段至少包括：

```text
字段提取正确率
单位正确率
期间正确率
财务口径正确率
原文定位正确率
确定性计算正确率
引用支持正确率
Verification 错误检出率
Verification 误拦截率
系统 abstention rate
```

---

# 18. 测试目录建议

将测试逐步拆分为：

```text
tests/
├── unit/
├── integration/
├── golden/
└── regression/
```

---

## 18.1 unit

测试：

```text
YoY
QoQ
unit conversion
period handling
ratio calculations
rule conditions
schema validation
```

---

## 18.2 integration

测试：

```text
PDF → FinancialFact
FinancialFact → Calculation
Calculation → Anomaly
Anomaly → Verification
Verification → Report
```

---

## 18.3 golden

至少维护 3～5 个真实人工核对案例。

第一例：

```text
603288 / 2024
海天味业 2024 年年度报告
```

Golden case 中需要人工确认：

```text
关键字段
页码
行列
单位
期间
公式结果
```

---

## 18.4 regression

专门保存曾经出现过的错误。

例如：

```text
wrong-column
negative-parentheses
unit-ten-thousand-yuan
restated-report
duplicate-row
zero-base
negative-base
consolidated-vs-parent
cumulative-vs-single-quarter
```

Codex 修复任何 parser / extraction / finance 逻辑时必须重新跑 regression。

---

# 19. 当前明确不做的功能

以下内容全部延期：

```text
OCR 全覆盖
扫描件全自动处理
向量数据库
企业关系网络
GraphRAG
模型微调
分类模型
联合训练
多模型 voting
用户系统
权限系统
云平台部署
复杂 Docker 集群
100+ 财务指标
Fancy Agent UI
企业级 SaaS 功能
```

除非现有 Golden Path 已完全跑通，否则不要实现这些功能。

---

# 20. README 后续重构方向

README 应从“技术规范文档”变成“项目入口”。

README 首屏建议：

```text
FINTRACE

Verifiable AI Financial Report Analysis

1. Upload a financial report
2. Extract financial facts with source coordinates
3. Calculate indicators deterministically
4. Detect candidate anomalies
5. Investigate management explanations
6. Independently verify evidence
7. Generate an auditable report
```

然后立即展示：

```text
PDF → Facts → Analysis → Verify → Report
```

再展示真实案例：

```text
603288 海天味业 2024
```

详细开发协作说明移动到：

```text
docs/development.md
```

例如：

```text
AGENTS
Herdr
开发模型分工
AI coding workflow
```

这些不是比赛项目核心价值，不要占 README 首屏。

---

# 21. 开发优先级

## P0 — 现在立即做

```text
FinancialFact schema
        ↓
TableCellEvidence
        ↓
Period / Scope / Unit / Column Confirmation
        ↓
Deterministic Finance Engine
        ↓
Claim Schema
        ↓
Independent Verification
```

P0 完成标准：

> 对海天味业 2024 年报中的核心财务数字，可以稳定提取，并独立确认其表、行、列、单位、期间和财务口径。

---

## P1 — 形成完整比赛 Demo

```text
真实 PDF
    ↓
Fact Extraction
    ↓
Financial Analysis
    ↓
4 个异常规则
    ↓
MD&A / Notes Investigation
    ↓
Claim Generation
    ↓
Verification
    ↓
Final Report
```

P1 完成标准：

> 从一份真实 PDF 输入开始，不需要人工修改中间 JSON，即可得到最终可审阅报告。

---

## P2 — 证明项目价值

```text
Golden Cases
Verification Error Injection
Verification Ablation
System Reliability Metrics
```

P2 完成标准：

> 能用实验说明 Verification 相比无核验流程减少哪些错误，以及代价是什么。

---

## P3 — 展示体验

```text
Evidence Explorer
PDF Highlight
Run Progress
Verification View
Report View
```

---

## P4 — 后续扩展

```text
半年报
季度报告
QoQ
更多财务指标
更多公司
完整 benchmark
OCR
```

---

# 22. Codex 工作规则

执行后续开发时请遵守：

1. 不要一次性重写整个项目。
2. 每个任务保持小范围、可测试、可回滚。
3. 修改前先阅读相关现有代码和测试。
4. 不改变已有公开 API，除非确有必要。
5. 如需破坏兼容性，先明确列出影响。
6. 所有核心财务逻辑必须有测试。
7. 不允许只修改实现而不更新对应测试。
8. 不把临时 workaround 当正式架构。
9. 不允许用 LLM 自然语言输出替代确定性计算结果。
10. 不允许自动把缺失数据当 0。
11. 不允许自动把未知口径当作可比。
12. 不允许 Verification 直接信任 Extraction 的最终判断。
13. 所有异常都必须保留 alternative explanations。
14. 所有失败状态必须明确保存，不得伪装为成功。
15. 真实数据案例必须保留 source hash 和 provenance。
16. 优先复用现有模块，不重复实现相同逻辑。
17. 在完成 P0/P1 前，不实现“当前明确不做”的功能。

---

# 23. 下一任务建议

Codex 下一次开始工作时，优先完成：

## Task 1 — 审计当前 schema 和 extraction

先阅读：

```text
backend/src/finagent/
tests/
docs/architecture.md
README.md
```

确认当前：

```text
FinancialFact
ExtractionResult
VerificationResult
```

是否已经存在。

不要直接假定本文件提出的数据结构完全不存在。

输出：

```text
CURRENT_SCHEMA_AUDIT.md
```

内容包括：

```text
现有结构
缺失字段
重复字段
兼容性风险
推荐修改
受影响文件
```

---

## Task 2 — 实现 FinancialFact v2

基于现有代码进行最小兼容修改。

必须支持：

```text
period
period_type
scope
comparison_role
restatement_status
source evidence
```

增加完整测试。

---

## Task 3 — 实现 TableCellEvidence

建立统一表格证据结构。

至少覆盖：

```text
table
row
column
unit
value
page
bbox
```

---

## Task 4 — 实现真正 Verification MVP

目标：

> 不依赖 extraction 最终 row/column 判断，重新基于原始页面检查关键事实。

先只验证：

```text
营业收入
归母净利润
经营现金流量净额
非经常性损益
```

---

## Task 5 — 打通海天味业 Golden Path

要求：

```text
PDF
→ facts
→ calculations
→ anomaly candidates
→ verification
→ report JSON
```

先允许报告为：

```text
JSON + Markdown
```

不要优先实现 React。

---

# 24. 最终目标

第一阶段完成后，应能够稳定展示：

```text
输入：
一份真实上市公司年度报告 PDF

输出：
结构化财务事实
确定性同比 / 财务指标
候选异常
管理层解释
原始证据
独立核验结果
冲突 / 证据不足
最终可审计报告
```

任何关键结论都应该能够回答三个问题：

```text
这个数字从哪里来？
这个计算是怎么得到的？
这个结论为什么可信？
```

如果系统无法回答其中任何一个问题，则该部分仍未达到第一版完成标准。
