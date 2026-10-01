# 旧年度预检事实结构审计（历史基线）

> 本文冻结旧预检的字段与兼容风险，不是当前 v2 能力清单，也不直接生成新开发任务。2026-10-01 整理时移除了重复追加的 v2 进度；当前能力见 [README](../README.md)，接口见 [架构](architecture.md)，工程阶段见 [路线](roadmap.md)。修改旧预检前仍应核对当前代码。
>
> v2 已有独立事实/计算核验、任务 API、网页 AI 调查与评审入口；入口受能力状态控制。单公司消融基线已存在，多公司人工 golden 与完整可靠性评测未完成。不能继续引用旧文中“在线入口不支持”或“消融尚未建立”的说法。

## 实际结构

`backend/src/finagent/schemas/financial_fact.py` 的 `FinancialFact` 字段是：

`document_id`、`source_sha256`、`company_id`、`indicator_name`、`table_name`、`raw_value`、`normalized_value`、`report_year`、`period_label`、`period_type`、`column_role`、`restatement_status`、`unit_multiplier`、`currency`、`statement_scope`、`extraction_method`、`hits`、`limitations`。

`FactHit` 是文字块：`page_number`、`block_index`、`text`、`x0`、`y0`、`x1`、`y1`。它不是表格单元格，没有行标签、列标签或印刷页码。

提取把 `period_type` 写成 `annual`，`restatement_status` 写成 `unknown`，`statement_scope` 写成中文 `合并` 或 `披露表格口径`。`column_role` 已是 `current` 或 `comparative`。规范值是十进制字符串。

`calculate_annual_changes` 在期间类型为 `annual` 且文档、哈希、公司、币种、单位倍率、口径一致时计算差额和比率。它不读取 `restatement_status`。因此追溯状态未知时仍会计算同比。上期规范值为 0 时同比弃权，代码是 `zero_prior`。

`verify_source_amounts` 用事实自带的 `hits` 回到原始 PDF 裁剪文字，检查原始金额是否落在这些坐标里，并用独立 Decimal 复核 `raw_value × unit_multiplier`。状态是 `passed`、`failed`、`abstained`。它明确不确认年度列、表头或完整事实。

`screen_annual_signals` 在复核通过且口径相容时，给出利润或收入上升同时经营现金流下降的候选，以及经营现金流与归母净利润的比值。归母净利润小于或等于 0 时比值弃权。没有相符同比结果时不自行重算差额来形成候选。上期为 0 时只记录绝对差额，并注明这不是通常意义的同比增速。非经常性损益披露合计不除以归母净利润。

旧流程 HTTP 已有 `POST /v1/text-pdf-uploads`、`POST /v1/annual-prechecks`、`GET /v1/annual-prechecks/{run_id}`。预检页创建、回看和上传后填入路径已经接上。旧预检 API/数据结构没有 Claim、TableCellEvidence 或独立语义核验类型；这些类型和 v2 核验模块已存在，但没有接入旧接口或页面。

## 缺口

以下缺口仅针对上述旧 `FinancialFact` 与预检流程。v2 已经有相应类型或模块的项目另行说明。

- 没有 `fact_id`、`metric_id`，指标只靠中文 `indicator_name`。
- `period_type=annual` 不能区分时点与期间，也不能支持以后的半年报或季报。
- 没有 `period_start`、`period_end`。
- `statement_scope` 使用中文，不是 `consolidated`、`parent`、`unknown`。
- `restatement_status=unknown` 仍参与同比，等于把未知追溯状态默认为可比。
- `hits` 不能证明数字属于哪一行、哪一列。
- 没有稳定的公司或文档实体 ID；`company_id` 与 `document_id` 是字符串，上传生成的 `document_id` 与手工导入的 cninfo 目录标识不是同一套。
- 核验状态词汇与目标词汇不同：现为 passed、failed、abstained，目标为 verified、conflict、insufficient_evidence。
- 旧预检没有 Claim。其 `screening` 用 `statement_kind` 区分 fact、calculation、inference，但不是可引用的主张对象；旧同比结果也没有 `calculation_id`。v2 已定义 Claim 与 Calculation 类型，并在正式 CLI 中执行确定性 Claim 核验；该核验仅覆盖当前类型和已实现的确定性文案。

## 重复字段

- 页码和坐标同时出现在 `FactHit` 与核验结果的 `evidence` 里，核验结果不另给证据 ID。
- `document_id`、`source_sha256`、`company_id`、`currency`、`statement_scope` 在事实和同比结果里各存一份。
- 预检 JSON 同时保存 `facts`、`calculation`、`verification`、`screening` 和 `issues`，issues 又重复提取与同比的弃权原因。
- `normalized_value` 与 `raw_value`、`unit_multiplier` 三套并存。这是有意保留原文和换算，不是可以删掉的重复。

## 兼容风险

- 把 `period_type` 从 `annual` 改成 `duration` 会使现有同比直接弃权。
- 把 `statement_scope` 改成英文会使现有“必须为合并”的筛查全部弃权。
- 让 `restatement_status=unknown` 停止同比，会改变海天预检里 4 组同比和两条 candidate 的结果。那些历史运行不能改写。
- 上传路径是 `source.pdf` 与 `text_pdf.json`。海天样例仍是原始 PDF 文件名。两条路径约定并存。
- 前端按现有预检 JSON 读取。给旧记录补字段或改状态枚举会破坏回看。

## v2 与旧流程的关系

v2 类型、核验模块和报告与旧预检分开。旧记录的 passed/failed/abstained 不能直接等同于 v2 独立核验状态；不向旧 JSON 补写实际上没有执行过的核验结果。既有 FinancialFact、预检响应和页面保持兼容，必要修复应按当前任务审阅与验证。

旧实现涉及 `schemas/financial_fact.py`、`ingestion/extract_annual_facts.py`、`finance/annual_change.py`、`finance/annual_signals.py`、`verification/source_amount.py`、`api/annual_precheck.py` 及前端预检类型。后端模块均位于 `backend/src/finagent/`；具体文件与接口以当前代码为准。
