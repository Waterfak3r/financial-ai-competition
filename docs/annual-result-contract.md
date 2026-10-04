# 年度结果页内容契约（A-01）

本契约的归档绑定、首屏数值、状态文字与证据边界已由主控按 A-01 审阅接受，并随 `921aa19d98161a54b7afcd426c567949f6173615` 提交。它描述海天 603288 确定性归档的内容要求；原始低保真线框作为历史信息顺序样板保留，不是当前页面截图。当前浏览器呈现与视口验收已通过 A03–A05 最终矩阵，具体证据见[验收记录](verification-history.md)；这不代表 30 秒非金融用户理解试验已完成。模块与核验状态仍以[架构文档](architecture.md)及具体归档为准。

## 真实归档绑定

样例固定使用确定性年度归档 `annual-analysis-21d8da07-097b-4cdf-97ce-6e109c8cc1b8`。报告身份为 `company_id=603288`、`report_year=2024`、`source_document_id=cninfo-1222994233`；manifest 与归档 PDF 的 SHA256 均为 `5a97b13534438f5e85249752ef492fbd9e23af73e67845e47bad1ab7d92e20ee`。任务状态为 completed，`model_called=false`，这是历史归档回看。

公司名称只在身份三元组 `(company_id, source_document_id, source_sha256)` 匹配时采用原 PDF：第 1 页同时写明“公司代码：603288”“公司简称：海天味业”及法定名称“佛山市海天调味食品股份有限公司”；第 5 页页眉再次出现法定名称。匹配前端展示“海天味业 · 603288”；映射缺失或不匹配时只展示公司代码，不从文件名、请求字段或模型文本猜名称。

年度可比性为 `comparability.status=verified`、期间 2024/2023、`restatement_status=not_restated`。证据覆盖 PDF 第 116、163、197 页；该结论依据本年报披露，不代表已与独立发布的 2023 年报重新勾稽。页面保留此限制。

### 首屏数字样例

以下显示按已核验原文单位元换算为亿元，并保留两位小数；精确值仍来自归档。财务事实为人民币、合并口径，2024 本期对 2023 比较期。

| 指标 | 本期（2024） | 上期（2023） | 年度差额 | 同比 |
| --- | ---: | ---: | ---: | ---: |
| 营业收入 | 269.01 亿元 | 245.59 亿元 | +23.42 亿元 | +9.53% |
| 归母净利润 | 63.44 亿元 | 56.27 亿元 | +7.17 亿元 | +12.75% |
| 经营活动产生的现金流量净额 | 68.44 亿元 | 73.56 亿元 | −5.12 亿元 | −6.96% |

本期/上期精确原值依次为：营业收入 `26900977516.70` / `24559312356.59` 元；归母净利润 `6344125969.00` / `5626626091.97` 元；经营活动产生的现金流量净额 `6843710887.07` / `7355650997.74` 元。差额精确值依次为 `2341665160.11`、`717499877.03`、`-511940110.67` 元；归档同比比值依次为 `0.0953473422264471918216274083054885`、`0.1275186702123275832749914720117`、`-0.06959820562820230931561832107767`。同比必须读取归档中的率值计算结果并格式化为百分数，不能由浏览器用两金额反算。

| 指标 | 事实与原文证据 ID | 差额与同比计算 ID | 归档状态 |
| --- | --- | --- | --- |
| 营业收入 | `cninfo-1222994233:revenue:2024:current` / `cninfo-1222994233:revenue:2023:comparative`；`ind-cninfo-1222994233:revenue:2024:current` / `ind-cninfo-1222994233:revenue:2023:comparative` | `calc:annual_difference:revenue` / `calc:annual_yoy_rate:revenue` | 两事实核验、两计算独立重算、对应 Claim 均为 `verified`；两计算 `independent_verification.status=verified` |
| 归母净利润 | `cninfo-1222994233:net_profit_parent:2024:current` / `cninfo-1222994233:net_profit_parent:2023:comparative`；`ind-cninfo-1222994233:net_profit_parent:2024:current` / `ind-cninfo-1222994233:net_profit_parent:2023:comparative` | `calc:annual_difference:net_profit_parent` / `calc:annual_yoy_rate:net_profit_parent` | 两事实核验、两计算独立重算、对应 Claim 均为 `verified`；两计算 `independent_verification.status=verified` |
| 经营活动产生的现金流量净额 | `cninfo-1222994233:operating_cash_flow:2024:current` / `cninfo-1222994233:operating_cash_flow:2023:comparative`；`ind-cninfo-1222994233:operating_cash_flow:2024:current` / `ind-cninfo-1222994233:operating_cash_flow:2023:comparative` | `calc:annual_difference:operating_cash_flow` / `calc:annual_yoy_rate:operating_cash_flow` | 两事实核验、两计算独立重算、对应 Claim 均为 `verified`；两计算 `independent_verification.status=verified` |

每个 calculation ID 对应的 Claim ID 为 `claim:calculation:<calculation_id>`；三项差额和三项同比 Claim 的 `verification_status` 均为 `verified`，其支持证据及计算核验证据均指向该行上列两条独立事实 evidence ID。候选对象本身没有 evidence ID；其 `input_fact_ids` 经上表映射到三项事实证据，不能伪造候选证据编号。

`report.confirmed.metrics[].unit` 在这个旧归档中为空，但独立核验证据明确记录 `unit=元`、`currency=人民币`；不能把原始字段的空单位当作零，也不能只凭 `unit_multiplier=1` 猜单位。三项事实口径为 consolidated。只有来源单位和口径由匹配的已核验证据支持后，页面才可作亿元显示换算。

### 主要发现、核验状态与出处

可直接使用的主要发现文案：

> 2024 年营业收入和归母净利润同比上升，经营活动现金流净额同比下降。两组变化组合仍是待核查候选；这份归档没有提供原因，也没有确认因果关系或舞弊。

这句话绑定 `pending_review.candidate_signals`：`revenue_up_cash_down` 与 `profit_up_cash_down` 均为 `status=candidate`、`placement=pending_review`，`reason=""`。收入/现金流候选引用 `calc:annual_difference:revenue` 和 `calc:annual_difference:operating_cash_flow`；利润/现金流候选引用 `calc:annual_difference:net_profit_parent` 和 `calc:annual_difference:operating_cash_flow`。展示为“候选待核查”；空 reason 展示“原因尚未说明”，不补写解释。此归档 `interpretations=[]`、`model_called=false`、`fraud_conclusion=null`。模型观点、事实核验、计算核验与因果判断须分别呈现；原文引用、Claim 状态或数值已核验都不能证明因果。

页面中的“已核验”只复用归档已有状态：事实 `verifications[].status`、计算 `status` 与 `independent_verification.status`、Claim `verification_status`。状态不齐或冲突时沿用现有 `pending_review` / `conflict` / `insufficient_evidence` 等分类并解释缺项，不创建一个概括所有层级的新“可信度”分数。

原文定位绑定 PDF 物理页序号与 evidence ID：

- 营业收入：`ind-cninfo-1222994233:revenue:2024:current` 与 `ind-cninfo-1222994233:revenue:2023:comparative`；PDF 第 81 页“合并利润表”中“其中：营业收入”，列为 2024 年度 / 2023 年度，单位“元”、币种“人民币”。
- 归母净利润：`ind-cninfo-1222994233:net_profit_parent:2024:current` 与 `ind-cninfo-1222994233:net_profit_parent:2023:comparative`；PDF 第 82 页“合并利润表”行“归属于母公司股东的净利润（净亏损以‘−’号填列）”，两年金额与该行相符。表名、单位及年度列头位于前一页第 81 页；归档行定位框只覆盖换行行名的首行。
- 经营现金流：`ind-cninfo-1222994233:operating_cash_flow:2024:current` 与 `ind-cninfo-1222994233:operating_cash_flow:2023:comparative`；PDF 第 86 页“合并现金流量表”行“经营活动产生的现金流量净额”。表名、单位“元”、币种“人民币”及年度列头位于前一页第 85 页。

证据入口应显示 PDF 页序号、表名、行、年度列、单位/币种及对应金额；原归档 `printed_page=null`，因此不要称为印刷页码。来源、哈希或定位不匹配时禁用已核验页图入口，显示“原文位置待核实”。

`non_recurring_total` 在归档中是“披露的非经常性损益合计”，`statement_type=non_recurring`、`scope=unknown`；不能将其改名为扣非归母净利润，也不能从归母净利润直接相减生成扣非值。此样例未绑定可展示的 `adjusted_net_profit_parent` 事实。

## 低保真布局（2026-10-01 信息顺序样板）

以下保留 A-01 起草时提出的信息顺序，不是当前页面像素布局或浏览器结果。当前视觉状态以 A-05 验收证据为准。

```text
1366 × 768（首屏）
┌ FINTRACE  /  年度分析 · 历史归档 ───────────────────────────────┐
│ 海天味业 603288 · 2024 年报     已完成 · 确定性 · 模型未调用   │
│ 主要变化：收入 +9.53%，归母净利润 +12.75%，经营现金流 −6.96%   │
│ 两组方向组合为候选待核查；原因尚未说明。 [查看原文证据]        │
├ 指标              本期          上期          差额       同比   ┤
│ 营业收入          269.01亿元    245.59亿元    +23.42亿元 +9.53% │
│ 归母净利润         63.44亿元     56.27亿元     +7.17亿元 +12.75%│
│ 经营活动现金流     68.44亿元     73.56亿元     −5.12亿元 −6.96% │
│ 事实/计算：已核验  ·  年度可比：已核验  ·  原因：未提供       │
└ 证据与限制：收入 PDF 81 页；归母净利润 82 页；现金流 86 页 ┘
```

```text
1440 × 900（首屏，同一顺序、更多横向空间）
┌ FINTRACE / 年度分析 ─────────────────────────────────────────────────┐
│ 海天味业 603288 · 2024 年度报告       历史归档 · 已完成 · 未调用模型 │
│ 主要变化：收入与归母净利润增长，经营现金流下降。                  │
│ 候选待核查：收入—现金流、利润—现金流；原因尚未说明。 [查看证据] │
├ 指标 | 2024本期 | 2023比较期 | 年差额 | 同比 | 口径/核验 | 原文 ┤
│ 营业收入 | 269.01亿元 | 245.59亿元 | +23.42亿元 | +9.53% | 已核验 | 81页 │
│ 归母净利润 | 63.44亿元 | 56.27亿元 | +7.17亿元 | +12.75% | 已核验 | 82页 │
│ 经营现金流 | 68.44亿元 | 73.56亿元 | −5.12亿元 | −6.96% | 已核验 | 86页 │
│ 限制：可比性依据本年报披露，未与独立发布的2023年报重新勾稽。    │
└ 展开证据详情：表/行/列、evidence ID、来源哈希、计算/Claim ID ┘
```

```text
390 px（单列滚动，无页面横向溢出）
海天味业 603288
2024 年报 · 历史归档 · 已完成 · 未调用模型
主要变化：收入、归母净利润增长；经营现金流下降。
候选待核查 · 原因尚未说明               [查看证据]

营业收入                         已核验
2024 269.01亿元 · 2023 245.59亿元
差额 +23.42亿元 · 同比 +9.53%
人民币 · 合并口径 · PDF 第81页 [原文]

归母净利润                       已核验
2024 63.44亿元 · 2023 56.27亿元
差额 +7.17亿元 · 同比 +12.75%
人民币 · 合并口径 · PDF 第82页 [原文]

经营活动产生的现金流量净额       已核验
2024 68.44亿元 · 2023 73.56亿元
差额 −5.12亿元 · 同比 −6.96%
人民币 · 合并口径 · PDF 第86页 [原文]
可比性限制与原因缺口保持可见，不藏入折叠区。
```

## 字段与显示门槛

| 页面内容 | 读取与显示条件 |
| --- | --- |
| 公司、报告期、归档状态 | 公司名严格按 `(company_id, source_document_id, source_sha256)` 身份映射；报告期取已核验事实与 comparability；回看状态取归档/任务状态并明确是历史结果。 |
| 本期、上期、单位和口径 | 取 `confirmed.metrics` 的对应 fact ID；本期与上期分别按各自 fact 核验、来源哈希和 evidence 状态决定是否显示，不因另一侧缺证而抹掉本侧已核验金额。单位/币种从该侧验证证据读取；单位缺失则不换算亿元，scope 未知则显示“口径待核实”。 |
| 差额 | 取 `calc:annual_difference:<metric_id>` 输出；只有两侧事实均 verified、source identity/单位/币种/期间相容、scope 已知且可比性成立，并且 calculation 成功、独立重算 verified、相应 Claim verified 时显示。否则保留各自可显示的单侧金额，差额处说明弃权原因。 |
| 同比 | 只取 `calc:annual_yoy_rate:<metric_id>` 输出做百分比格式化；沿用该计算的独立重算及 Claim 状态。比较期不可比、基数非正、任一侧 scope 未知或证据不全时显示具体弃权原因，不显示空白、0% 或前端重算值。 |
| 主要发现 | 取 `pending_review.candidate_signals` 的 status、reason、calculation_ids、input_fact_ids。候选只标“候选待核查”；reason 为空时写“原因尚未说明”，不得自动生成原因或因果。 |
| 原文入口 | evidence 必须匹配本归档的 document ID、SHA256、独立核验状态和 PDF 物理页。证据不足或坐标不可靠时显示待核实定位，不让页码替代核验状态。 |
| 模型观点 | 此样例没有模型观点。其他归档如有 `model_review`，单独标明调用状态与观点；模型引用不能替代财务事实/计算核验，也不证明因果。 |

## 明确的缺证呈现

本样例的两个候选 `reason=""` 是真实缺证案例：保留候选信号和已核验数值，显示“原因尚未说明 / 尚无因果证据”。不得删掉候选、补写模型解释，或将其写成舞弊结论。另一个数据完整性边界是原始 fact 的 `unit` 为空而独立核验 evidence 有“元”；只使用证据确认的单位。若该证据也缺失，则金额不缩放、不显示亿元，单位状态显示待核实；若同比计算或可比性核验缺失，则弃权并说明原因。

## 2026-10-05 新增全文发现层

上文固定归档和数字样例仍只描述其对应的历史确定性报告，不作改写。当前报告另可包含 `model_discovery` 与 `discovery_evidences`：前者是绑定本次来源、逐批处理可提取 PDF 文字后形成的未核验模型观点；后者仅供原文定位与页图读取。页面将该区与基础年度比较、M3 及已核验事实分别呈现。旧归档没有这两个字段时保持兼容，并说明全文发现未记录；不得从历史 `mode` 名称推断做过全文扫描。新层字段与来源校验见[全文发现契约](annual-discovery.md)，总体接口和状态关系见[架构](architecture.md)。
