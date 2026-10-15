# 年报开放发现模块

本文说明 `finagent.agents.annual_discovery.run_annual_discovery`、`finagent.retrieval.annual_discovery_context` 及年度 CLI、API、归档、导出和页面展示契约。开放式发现不替代既有确定性分析、M3 筛查、固定候选调查或最终评审。

## 当前恢复执行路径

标准 Web 任务及默认数据/运行目录内的年度 CLI 活动外层为 `fintrace_annual_execution_binding_v10` / `execution_v10`，发现信封为 `fintrace_annual_discovery_execution_v10`；内层仍为业务绑定 v4 / plan_v3 / discovery_v3 解析器，提示词为 annual_discovery_v4。事项 pipeline_v1 / review_v3 与 wire/projection 不变，绑定继续保存 source_anchor_then_ngram_v2 补证版本及 v4 请求容量预算，并新增至多三个原始成员的局部归并计划身份。旧 execution_v9/v8/v7/v6/v5/v3 与旧提示词/响应只读；合法续跑仅接受 v10 exact，不跨提示词、归并分片计划、补证或预算身份复用。基础报告、完整绑定和待处理计划在首个请求前正式保存，扫描先于固定调查与评审，不依赖候选。合同见[架构](architecture.md#annual-execution-budget)，软件、各历史合同真实结果及新版在线关卡见[落地验收](verification-history.md#annual-analysis-recovery-r6-landing)。

v4 恢复路径使用最多三路滚动请求，由一个协调者完成引用检查和不可变检查点发布；最终 findings 按批次号及 finding ID 稳定排列。旧 v3 的串行归档保留原语义。每个批次记录 executed/reused/unattempted、请求准入与耐久响应、合格/拒绝发现以及继承发现。成功批次可由严格验证的父运行复用，本次请求数为零；失败或部分批次重新尝试时，父运行中已合格的发现仍保留。重试为空或再次失败不清空这些发现，重复项去重；复用和继承不等于重新完整处理。

恢复身份绑定原 PDF SHA、公司/期间/文档、原完整 mode、供应方及模型指纹、提示词、解析器、批次计划，以及集成模式的稳定 M3 语义输入。父链、批次、引用和来源均经严格读取器验证；绑定不符或旧归档没有绑定时拒绝续跑。续跑创建新的 job/run，保留父记录，且仅在原报告写入者已确认停止后开放。基础与阶段快照、原子指针、最终封存及服务重启的契约见[架构](architecture.md)。

独立调查与最终评审按阶段判断复用，不因扫描需要补跑而一律重调。评审要求实际语义输入未变；覆盖与缺口也属于输入。合法复用保留原成功审计来源，本次计数为零，多代父链继续严格验证；具体停止确认和评审复用契约见架构说明。

每次实际请求在联网前持久化准入，自动重试分别占账。`model_call_attempt_count` 计已准入尝试，`model_call_count` 计成功耐久保存的合法 Chat Completions 响应；响应中的发现全部非法时，响应数仍可为一，合格发现数为零。发现执行契约计扫描层，年度报告总数包含固定候选调查、扫描、事项归并/补证和评审，任务恢复进度另列父链/本次/累计；整个 goal 的共同额度单独据共同账本汇总。缺失、损坏或不完整的已使用账本拒绝准入，不能重建为空计数。

请求在连接器实际创建的子进程内执行，父进程在墙钟截止、取消或原 API 心跳失效时收束该子进程。401/403 不重试；限流、可重试服务错误及连接错误使用有限尝试与退避，实际准入均计账。扫描的连续故障及不可重试错误停止后续派发，保留已经通过检查的发现。次数额度耗尽只关闭新派发及后续重试准入，已准入调用继续受原阶段和整份截止约束；收束期间持续检查用户取消和真实时间截止，触发后中止在途并发布停止检查点。扫描自身额度或阶段时间耗尽与整份截止分别记录，前者保留其他阶段额度；取消、鉴权错误、共同额度耗尽或整份截止则停止后续流程。部分可读、全文覆盖完成和最终评审完成分别表达。实际时限见[年度执行合同](architecture.md#annual-execution-budget)，真实性能目标仍须由新在线运行验证。

## 输入与来源绑定

调用方必须显式传入标准年度报告、同源 `source.json`、原 PDF、`ModelSettings` 和 `artifacts/runs/<run_id>/` 下已存在的运行目录。模块核对报告类型、run/company/year/document/SHA、来源记录中的公司/文档/报告期/SHA，以及原 PDF 实际 SHA；运行目录必须是 `report.run_id` 对应的仓库运行目录直接子目录。身份或目录错误会在调用前拒绝；原 PDF 哈希错配会返回 `not_called`，不发模型请求。模块不读取 `.env`，也不修改输入报告。

发现上下文不依赖 fixed candidates、confirmed facts、M3 状态或历史评测标签。它复用 `parse_text_pdf` 逐页读取所有非空提取文字块，没有章节、财务主题或现金流关键词过滤。文字块编号沿用解析器的过滤后 `block_index`，不是 PyMuPDF 原始块编号。长块按字符位置顺序拆成不超过 1200 字的片段，片段绑定 PDF SHA、物理页序号、文字块编号、块内起始字符位置与 bbox；稳定 ASCII `evidence_id` 不由模型生成。

v3 计划的模型输入使用共享来源头和 `[fragment_alias, pdf_page, raw_text]` 数组。批内短别名包含有分隔符的批次命名空间，只能映射回本批的本地完整片段；页码、坐标、完整 ID 和原文仍在本地绑定。响应还原别名后继续逐字引用检查，不接受模型生成的定位。CLI 将本次解析对象传给来源绑定、发现规划及固定调查检索，先核对 document ID 与 SHA；独立财务事实核验仍使用其独立原文路径，不以缓存提取值证明自身正确。

批次减少不代表发现质量相同：每批严格三条的输出上限意味着，同一份文字合并到更少请求后，可输出线索的总上限也会下降。原文覆盖不等于事项发现完整；质量、后部事项保留及跨章节归并需由受控案例和新在线原文对照另行验收。

## 批次和覆盖范围

默认每批原始正文不超过 12000 字，按实际 Chat Completions 请求 JSON 体计算的序列化输入不超过 24000 字符；全文最多 128 批、120 万字符。所有内容上限都不可由调用方调高。遇到限制时保留已规划批次，并在 coverage 中明确遗漏字符、页面和 `truncated`，不把前段扫描描述为全文完成。批次仍按原文顺序规划；扫描使用有界滚动调度，默认最多 3 个在途请求。某请求结束后才从待处理队列补入下一批，不要求整组三批都结束才继续，因此慢批不会阻塞其他已完成请求的补位。LangGraph 仍是唯一编排层；其扫描节点内部使用有界 I/O 执行器，并发只用于模型请求，解析、进度协调和最终归并由协调线程执行。连接器准入按批次顺序受门控，但线程调度不保证连接器实际开始时严格按序。

规划按实际请求 JSON 的两层转义增量计长，不为每个新增片段重建整批别名与请求体；整批结束仍按原序列化器计算完整正文并核对精确一致。原文顺序、切片、边界、别名、请求字符数和恢复计划投影不变；这是本地规划开销修复，不放大批次上限、不复用跨请求未经重核的来源，也不证明模型质量。

可通过进程环境变量 `FINTRACE_DISCOVERY_CONCURRENCY` 设置并发上限，合法值为 1、2 或 3，默认 3；设为 1 可供需要串行请求的限流环境使用。非法值会明确拒绝，而不静默改成其他并发数。此设置只控制扫描请求并发，不改变文字范围、批次规划、响应规则、调用重试或其他模型阶段。运行目录保留 `discovery-execution.json`；v4 另在公开执行契约中记录 scheduler、phase_budget 与 timing，不含正文或密钥。scheduler 的峰值指已提交的在途工作，不冒充实际 HTTP 并发；实际 HTTP 并发由运行证据另行确认。`time_deadline_reason` 记录实际触发的时间截止，纯请求次数额度耗尽时为 null；先封顶、收束中再达到时间截止时更新该字段及停止原因，与 coverage 的 `time_budget_exhausted` 一致。

coverage 区分原始 PDF 页数、含可提取文字的页数、原始文字块数、文字字符数、规划批次和已结束批次。v2 的 `processed_batches` 等于 `successful_batches + partial_batches + failed_batches`；预算到期后停止派发新批次，未尝试的批次另列为 `unattempted_batches`，已准入或正在调用的批次保留其完成结果。`batch_results` 为每批记录安全状态、固定失败类别、页码和字符数，并在 v2 记录 `accepted_findings`、`rejected_findings` 与 `restored_quotes`，不含正文。`succeeded` 表示本批所有 finding 均通过（空 findings 合法），`partial` 表示有接受项也有拒绝项，`failed` 表示没有接受项或调用/容器失败。旧 v1 归档按严格 v1 规则读取，缺少 `partial_batches` 时按零处理；未知提示词版本拒绝读取。`processed_chars` 只计整批通过的批次；`attempted_chars` 计实际送入模型的正文，`unattempted_chars` 表示尚未尝试的字符，`unread_pages` 表示未完整处理的页面。失败批次页面列入 `failed_pages` 与 `unread_pages`；部分批次页面列入 `unread_pages`，但不标成整批失败页。两者都不能视为完整处理。

`no_text_pages` 记录没有可提取文字的页；`image_pages` 记录存在图片块的页；`unread_image_pages` 只记录没有文字且有图片的页。带正文和 logo/插图的页面仍可完整处理其中全部可提取文字。`text_only_scope` 明确本模块不执行 OCR，图片本身和图片中的文字不在范围内。`complete` 仅表示至少有可提取文字，且全部可提取文字均在上限内由完整成功批次处理、`partial_batches` 与 `failed_batches` 均为零、没有未尝试字符或未读图片页；零文字来源不能因零字符等于零计划字符而标为完整。它不表示没有风险，也不证明模型发现正确。空白 PDF 状态为 `not_called`。

## 模型输出与证据

全部新发现调用使用 `annual_discovery_v4` 提示词、plan_v3 紧凑输入及 discovery_v3 解析器，每批最多三条，使用 citations[].fragment_alias。提示词要求纳入同处及相关披露解释，区分主体、期间、分母和资金方向；每条可分别引用最多四个片段，不伪拼引文。旧 v2 解析器的六条上限只用于历史版本。输出仅含 findings/limitations，程序生成 ID 与未核验状态；严格 JSON 容器拒绝额外/缺失字段、重复键、null、围栏及越界，容器错误整批失败。每 finding 独立校验字段、长度、优先级、数字、核实/舞弊断言和引用，非法项拒绝而合格项保留；混合为 partial，无合格项为 failed。有限句式守卫保留审慎表达，但不能判定全部语义。新增提示词不意味着模型已经遵守，也不替代独立核验。

v2 每条 finding 最多四个 citation、每条 quote 不超过三百字符；同一批片段 ID 可用于多个不同的精确连续子串，最终 `evidence_ids` 按引用顺序保留唯一 ID；重复 `(evidence_id, quote)` 仍拒绝。若 quote 与指定片段只在 Unicode 空白排版上不同，解析器仅在去空白后至少两个字符且该片段内恰好有一个匹配（包括重叠匹配计数）时恢复为一个连续原文子串；恢复后的原文长度仍受三百字符限制，并重新执行逐字检查。它不改字符、标点、数字或顺序，不跨片段、不替换 ID、不拼接或删词；歧义、非空白改写、错误片段及恢复后重复引用均拒绝该条。`restored_quotes` 让成功恢复数可审计。v1 历史层维持原有严格解析，不执行空白恢复或 v2 多引文规则。数字可出现在来源 quote 中，不会被转成观点结论；词法子串匹配只绑定引用位置，不是语义核验。

程序为每条发现生成 `disc:<sha摘要>` ID，固定写入 `classification=unverified_model_finding`、`verification_status=unverified`，并将 `fraud_conclusion` 固定为 null。完全重复的发现去重；不同批次成功发现会保留。`evidences` 只返回实际被 findings 引用的原文片段。模型可返回空 findings；这表示本批没有提出可引用线索，不代表年报没有风险。v2 部分接受的 finding 与完整成功批次中的 finding 一样可读，但其所在批次不算完整成功、字符不计入 `processed_chars`、所涉页仍为未完整处理。

状态为 `completed`（完整覆盖且没有 partial/failed/unattempted 批次）、`partial`（存在部分接受、失败批次、超时或覆盖缺口；即使 `successful_batches=0`，只要 partial 批次保留了有效 finding 仍可有可读结果）、`failed`（没有任何可用 finding 且已尝试批次失败）或 `not_called`（没有进入任何模型调用）。调用失败与输出解析失败使用不同安全类别；可识别的超时归入 `model_call_timeout`，不伪装成 JSON 错误，报告不保存异常原文。发现预算到期停止准入，实际请求子进程受剩余墙钟时间约束；已经保存的结果继续保留。全部已尝试批次失败且没有可用发现时仍为 `failed`。调用、响应与复用计数按上面的当前执行契约区分。Progress callback 只收计数、序号和状态，不传入正文、quote、模型设置或路径；新进度事件明确给出 successful/partial/failed 三类计数，旧进度记录缺少 partial 时按零处理。兼容路径的进度回调异常记录安全 limitation；恢复路径的正式检查点写入失败会停止本次工作，不以未保存结果继续派发。最终 findings、去重、coverage 和 `batch_results` 按原批次顺序稳定归并。并发本身不增加规划批次数，连接器的自动重试则分别占用请求额度。

每批请求和响应通过现有审计连接器归档至运行目录的 UUID 子目录。并发扫描会在请求前为每批分配独立 UUID audit ID，使成功和失败审计都明确关联至对应批次；既有调用方不传 ID 时仍由连接器自行生成。发现结果本身不复制凭据、服务地址或异常原文；审计连接器记录请求前先脱敏，调用失败只记录安全类别。不得将调用审计文件当作公开分发内容。

## 年度 CLI、API 与导出接入

显式模型年度 CLI（`--with-model`）以及 Web 的 `model_investigation`、`integrated_analysis` 模式在来源和设置验证后独立运行全文发现；它不依赖固定候选或确认指标是否存在。正常生成的新归档同时写入顶层 `model_discovery` 与 `model_investigation.discovery`，两份内容必须一致。读取兼容只含顶层 `model_discovery` 的合法归档；只有嵌套 `model_investigation.discovery`、缺少顶层层级的归档会被拒绝；若两处都存在，则必须一致。旧调查、独立 M3 和最终评审继续保留；发现观点只作为未核验材料，不写入已核验事实、计算或候选信号。总模型调用计数合并旧调查、全文发现与最终评审，并单列各层次数。模型发现失败或覆盖不完整会体现在年度归档状态中，不会显示为完整全文分析。

年度报告读取接口包含 `discovery_evidences`，并提供 `GET /v1/annual-analyses/{run_id}/discovery-evidences` 索引及 `GET /v1/annual-analyses/{run_id}/discovery-evidences/{evidence_id}/preview.png` 页图。读取时重新解析已归档 PDF，校验来源 SHA、过滤后文字块编号、片段字符位置、bbox 和逐字引用；身份、引用或结构不匹配时拒绝该年度报告读取，不返回发现证据或页图。只暴露实际被发现引用的片段。

对新增发现层，JSON、Markdown 与离线 HTML 导出使用白名单投影来源身份、发现观点、未核验标签、优先级、覆盖与限制，以及可追溯的证据 ID/页码定位；发现层不导出其证据 `raw_text`、quote、审计请求/响应或原 PDF。既有 narrative 导出继续遵守自己的受控投影规则。最终评审可引用本次实际送入评审的发现 `finding_id`，不能把证据片段 ID 当成评审对象。页面可继续使用独立索引和 preview API 呈现原文片段。

任务进度写入当前 job 专属的 `discovery-progress.json`，GET 只在 job 正运行年度 CLI 阶段读取；路径、job ID 和来源身份必须匹配，未知字段、链接文件或不安全计数均被丢弃。响应只给状态、批次数、页数、全文字符计数与时间，不含原文或密钥。`annual-analysis-capabilities` 的 `model_investigation.annual_discovery_supported` 表示当前后端代码具备此接入能力；旧后端缺少该字段时客户端不应把旧两条规则能力说明成全文发现。

模型 Web/API 任务与独立 CLI 的整份截止、阶段上限、单次 HTTP/worker 上限、默认尝试次数及归档默认与 owner 实际分派，统一见[年度执行合同](architecture.md#annual-execution-budget)。live job 的 `analysis_budget` 记录实际 owner 时限；归档 `phase_budget` 是该版本固定默认合同，不能当作本次较短 owner 的实际时限。进度文件只代表批次事件，不代表最终报告完成。硬中断后是否可读，以严格读取器验证的 manifest、快照指针和来源为准，不能由计数进度合成结果。

## 页面、旧归档与任务阶段

Web 报告先显示最多三项“重点事项”或明确缺口，随后是基础财务对照，再保留完整“AI 待核查线索”、问题归并与成员处理账等详情。概览沿用归档 `presentation_order` 的合格事项及稳定候选顺序，不按 raw priority 重新排序；可信处理账已排除的成员不得重新作为首屏候选。合格事项不足时不制造问题，本地未运行 AI 时不把两条基础比较模板包装成公司特有重点。限量仅是展示，不改变归档、完整列表或导出。

每项重点默认显示完整观察、未核验性质及首个通过来源校验的发现原文入口；关注理由与核查动作、更多引用及补证使用原生折叠，不裁剪观点文本。补证保留 context、possible_explanation、unknown 或尚待标注的区别，来源定位不是独立因果核验。概览可定位到完整发现，搜索与优先级筛选仍在完整列表；覆盖、评审未完成、待处理及排除边界随结果明示。两条旧规则只称“基础年度比较”，不作为全文问题目录。空 `findings` 是合法结果，不为制造差异补问题；priority 只表示建议核查顺序，不是风险概率。原文引用片段按原换行连续显示，但只代表被引片段，不承诺完整自然段；页图高亮覆盖对应文字块 bbox，不定位到精确字符。JSON/Markdown/离线 HTML 的白名单和既有布局不随 Web 首屏重排改变。

全文状态独立于 `confirmed.metrics` 与 M3：本地模式、旧模型归档未记录全文层、全文 `completed`、部分覆盖、`not_called`、全部批次失败和合法空发现分别按各层实际归档说明。全文扫描成功但最终评审失败或未调用时，仍展示已保存发现，不概括成“没有 AI 结果”。全文层 `completed` 要求至少有可提取文字、原 PDF 的全部可提取文字均成功通过严格响应检查，且没有失败批次、未尝试文字、超限截断或未读图片页；这只说明提取文本覆盖，不代表语义正确或无风险。图像/扫描件、无文字页、失败页和超预算尾部按 coverage 明示。Job 进度中的批次 `completed` 只表示该批次处理已结束，不代表年度任务、最终评审或归档完成。

`discovery_evidences` 只为 finding 实际引用的原文提供独立索引。`annual_discovery` 页图与已核验金额、叙述引用分开标记，并按本次 PDF SHA、页码、原文引用、region 和 PNG 响应头校验；非法来源/引用时拒绝该层证据和页图。页图原文仍是引用定位材料，不提升模型观点状态。后端能力响应缺少或设为 false 的 `annual_discovery_supported` 时，旧后端客户端禁用新的全文 AI 提交并提示需加载/重启新版后端；确定性本地分析仍可使用。旧报告及旧 job 可继续查看；旧 `mode` 名称和旧候选调查记录不能作为已做全文扫描的证明。

## 未完成边界

本能力仅表示在给定预算内处理了哪些可提取文字。它没有独立财务事实核验、因果核验或舞弊分类能力。本地 mock 与构造 PDF 测试验证软件契约，不是在线供应方实测，也不证明全文语义准确或跨公司发现质量。扫描件、图片、被 PDF 解析器漏掉的内容、跨批推理和超出预算的尾部内容仍可能产生覆盖缺口。引用还原和覆盖账完成不等于财务语义真人正确率；开放问题的跨公司泛化能力须用隔离案例和人工复核另行评测。当前真实服务、性能及恢复的实测范围见[R6 验收](verification-history.md#annual-analysis-recovery-r6)与[落地验收](verification-history.md#annual-analysis-recovery-r6-landing)，不以旧 mock、历史回放、合成长等待或单分片 accepted 替代新真实主流程与财务人审。
