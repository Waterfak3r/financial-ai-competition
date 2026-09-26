# 当前事实结构审计

本审计描述修改前的旧预检流程，也就是仍在跑的 `FinancialFact`、同比、坐标金额复核和 `screening`。`backend/src/finagent/schemas/financial_fact_v2.py` 里的 v2 schema 已经落盘，包括表格证据以及核验、计算、主张的类型，但独立核验仍未实现，预检也还没有改用 v2。旧 API 和历史 `artifacts/runs/` 不改写。

## 实际结构

`backend/src/finagent/schemas/financial_fact.py` 的 `FinancialFact` 字段是：

`document_id`、`source_sha256`、`company_id`、`indicator_name`、`table_name`、`raw_value`、`normalized_value`、`report_year`、`period_label`、`period_type`、`column_role`、`restatement_status`、`unit_multiplier`、`currency`、`statement_scope`、`extraction_method`、`hits`、`limitations`。

`FactHit` 是文字块：`page_number`、`block_index`、`text`、`x0`、`y0`、`x1`、`y1`。它不是表格单元格，没有行标签、列标签或印刷页码。

提取把 `period_type` 写成 `annual`，`restatement_status` 写成 `unknown`，`statement_scope` 写成中文 `合并` 或 `披露表格口径`。`column_role` 已是 `current` 或 `comparative`。规范值是十进制字符串。

`calculate_annual_changes` 在期间类型为 `annual` 且文档、哈希、公司、币种、单位倍率、口径一致时计算差额和比率。它不读取 `restatement_status`。因此追溯状态未知时仍会计算同比。上期规范值为 0 时同比弃权，代码是 `zero_prior`。

`verify_source_amounts` 用事实自带的 `hits` 回到原始 PDF 裁剪文字，检查原始金额是否落在这些坐标里，并用独立 Decimal 复核 `raw_value × unit_multiplier`。状态是 `passed`、`failed`、`abstained`。它明确不确认年度列、表头或完整事实。

`screen_annual_signals` 在复核通过且口径相容时，给出利润或收入上升同时经营现金流下降的候选，以及经营现金流与归母净利润的比值。归母净利润小于或等于 0 时比值弃权。没有相符同比结果时不自行重算差额来形成候选。上期为 0 时只记录绝对差额，并注明这不是通常意义的同比增速。非经常性损益披露合计不除以归母净利润。

HTTP 已有 `POST /v1/text-pdf-uploads`、`POST /v1/annual-prechecks`、`GET /v1/annual-prechecks/{run_id}`。预检页创建、回看和上传后填入路径已经接上。没有 Claim、TableCellEvidence 或独立语义核验类型。

## 缺口

- 没有 `fact_id`、`metric_id`，指标只靠中文 `indicator_name`。
- `period_type=annual` 不能区分时点与期间，也不能支持以后的半年报或季报。
- 没有 `period_start`、`period_end`。
- `statement_scope` 使用中文，不是 `consolidated`、`parent`、`unknown`。
- `restatement_status=unknown` 仍参与同比，等于把未知追溯状态默认为可比。
- `hits` 不能证明数字属于哪一行、哪一列。
- 没有稳定的公司或文档实体 ID；`company_id` 与 `document_id` 是字符串，上传生成的 `document_id` 与手工导入的 cninfo 目录标识不是同一套。
- 核验状态词汇与目标词汇不同：现为 passed、failed、abstained，目标为 verified、conflict、insufficient_evidence。
- 没有 Claim。screening 用 `statement_kind` 区分 fact、calculation、inference，但不是可引用的主张对象。同比结果没有 `calculation_id`。

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

## 推荐修改

新事实与单元格证据放在隔离的 v2 类型中，由适配层读取旧事实。适配未经验收前，不改 `FinancialFact`、预检响应或历史 JSON。v2 至少分开时点与期间、使用稳定 scope 枚举、在追溯状态未知时不做同比、用单元格证据而不是文字块充当语义来源。旧预检继续服务现有页面。

## 受影响文件

以后实现 v2 时会碰到：`backend/src/finagent/schemas/financial_fact.py`、`ingestion/extract_annual_facts.py`、`finance/annual_change.py`、`finance/annual_signals.py`、`verification/source_amount.py`、`api/annual_precheck.py`、`api/app.py`，以及 `frontend/src/types/precheck.ts` 和预检页。本文不修改这些文件。
