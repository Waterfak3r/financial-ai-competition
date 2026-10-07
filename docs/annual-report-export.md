# 年度报告下载

年度分析归档可以下载为 JSON、Markdown 或离线 HTML：

```http
GET /v1/annual-analyses/{run_id}/export?format=json|markdown|html
```

格式值仅支持 `json`、`markdown` 与 `html`；其他值返回 HTTP 422。下载使用已归档的 `run_id`，后端先经年度归档读取器校验固定目录、manifest、报告身份和原始 PDF SHA256，再生成内容。缺失或不完整归档、身份不匹配和来源 PDF 被替换时，下载按归档读取错误返回，不读取调用方指定路径或未经校验的 `report.md`。响应采用附件下载、`Cache-Control: no-store` 和 `X-Content-Type-Options: nosniff`；HTML 另含限制资源来源的 CSP。

下载也支持通过严格读取的已发布部分快照。正文及 `X-Fintrace-Snapshot-State`、`X-Fintrace-Snapshot-Sequence`、`X-Fintrace-Snapshot-Is-Partial`、`X-Fintrace-Snapshot-Parent-Run-Id` 响应头来自同一次读取；三种格式均标明部分/最终状态与父运行关系。旧最终归档没有快照序号时使用兼容序号零。运行中的快照可能在点击下载前推进，下载以本次严格读取版本为准；未封存结果不能标作最终报告。取消和续跑不改写已下载版本，也不回写父报告。

JSON 只投影年度报告、manifest 摘要、后端来源性质登记、引用证据索引及导出限制中的受控字段。嵌套记录按字段白名单逐层筛选；任意扩展字段、调用审计请求正文、原始供应方请求/响应和密钥不进入导出。不会导出整份 PDF 或整页图。既有 narrative 层按其既定规则投影有限、受控的原文片段；新增全文发现层只投影未核验观点、覆盖和限制及安全来源定位，不投影该层的 `raw_text` 或模型 `quote`。每个事实、计算、可比性、M3、模型调查、全文发现和最终评审层保留其归档状态与可核对的来源绑定。年度可比性记录必须同时匹配归档的 `document_id`、来源 SHA256 和报告年度；任一字段不匹配时状态降为 `source_mismatch` 并移除该记录及其证据，缺少绑定字段时降为 `unverified` 并移除证据。M3、模型调查、全文发现和最终评审也校验来源身份；全文发现层的 SHA、引用或结构不合法时读取与导出 fail closed，不会通过旧层副本绕过校验。

若归档同时含顶层 `model_review` 与调查层嵌套的 `final_review`，导出以顶层评审的来源绑定和状态为准，并同步其受控投影；仅在没有顶层评审记录时，才回退到嵌套记录，兼容旧归档。错源的顶层记录不会被嵌套副本覆盖。

含全文发现的归档中，`model_call_count` / `model_call_attempt_count` 是固定候选调查、全文发现和最终评审三层的总计；`model_call_count` 计返回响应的调用，`model_call_attempt_count` 计实际调用尝试。JSON 投影在字段存在且有效时分别保留 `investigation_call_count` / `investigation_call_attempt_count`、`discovery_call_count` / `discovery_call_attempt_count`、`model_review_call_count` / `model_review_call_attempt_count` 三组分项。HTML 在 AI 调查区呈现总计与可用分项，并在全文发现区独立呈现该层状态、覆盖和调用数。旧归档只展示其实际含有的计数字段；缺失或类型不符的分项不会被补成 0。调用计数仅投影 0 到 JSON/JavaScript 安全整数上限内的整数。

导出列出来源文档 ID 与 PDF SHA256、金额、单位倍率、单位、币种、报告期间和范围口径、计算公式与独立重算状态、原文页码及证据 ID、候选状态、可比性记录及各层限制。M3 是未校准的规则试行分数，不是概率；AI 调查和最终评审是模型观点，不是独立核验或因果结论；候选线索不是确认舞弊。导出仅呈现已校验归档字段，不提升事实、计算或因果核验等级。

新增 `model_discovery` 层在三种格式中仅呈现动态标题、观察、关注理由、后续核查建议、priority（核查顺序）、未核验状态、限制、覆盖摘要和引用位置 ID/页码；它不包含原文片段正文或 quote。来源身份、finding ID 或引用关系不匹配时，报告读取、发现索引、页图及三种导出都拒绝不可信层，不把坏层降格后继续投影。旧归档没有该字段时仍可读取和导出，并只表示旧归档未记录全文发现，不从旧模式名称推断是否扫描。

Markdown 复用正式年度报告 renderer，并先将归档文本按纯文本处理，避免不可信内容形成 HTML 标签或 Markdown 链接。HTML 将文本按上下文转义，使用内嵌样式、可读表格、章节和打印 CSS，不加载外网资源或脚本。事实和计算的“已核实”/“待核查”标签按其所在的 `confirmed` / `pending_review` 容器生成，不依赖可选的 `placement` 字段。下载 HTML 后可离线查看，也可用浏览器打印成 PDF。

Web 年度报告页在核心指标之后提供 JSON、Markdown 和可打印 HTML 下载操作。下载期间显示状态，失败时保留明确错误；来源性质短标记保持可见，样例完整使用条件可展开查看。
