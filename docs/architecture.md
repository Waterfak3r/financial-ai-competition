# 架构与模块边界

## 1. 当前状态和目标

文本型 PDF 的逐页解析与原文定位已经实现。`finagent.ingestion.parse_text_pdf` 读取文本型 PDF，保留文档标识、原始文件 SHA256、PDF 1-based 页序号、文字块和未旋转页面坐标。空白页与图片页不产生文字，也不执行 OCR。运行依赖为 PyMuPDF，声明在 `backend/pyproject.toml`。

旧年度预检在 `ParsedTextPdf` 上按表标题、年度表头、单位和币种提取四项年度事实，并由旧 `finance` 流程对同一指标计算报告年与上一年的差额和同比率。`scripts/extract_annual_facts.py` 读取已有 `text_pdf.json`，可选核对原始 PDF 的 SHA256，并把 `facts.json`、`calculation.json`、`summary.json` 写入新的运行目录。这不是通用财报抽取。该命令不复核引用坐标内的原文金额；这项旧式复核在年度预检中进行，仍不覆盖年度列、表头口径或完整财报事实。

v2 确定性年度分析 CLI `scripts/analyze_annual.py` 已把真实 PDF 提取、独立事实核验、年度可比性检查、计算与独立计算核验、筛查、Claim 核验及 JSON/Markdown 报告归档串成 Golden Path。海天 603288 的 2024 样例正式运行 `annual-analysis-ffcd6078-a1df-416b-88b8-6774ebe66d42` 中，8 条事实、8 项计算及 16 条确定性 Claim 均通过核验，产生 2 条 candidate 信号。可比性检查从同一报告第 116、163、197 页定位披露，在当前进程中签发绑定来源哈希与期间的 proof；序列化结果仅供审计，不能授权计算，也不表示已独立勾稽此前已披露的 2023 年报。旧回归 `tests/integration/test_haitian_v2_report.py` 仍验证未提供可比性 proof 时 `unknown` 状态必须失败关闭。M1（海天样例）与 M2（单样例确定性 Golden Path）已验收；这不代表跨公司泛化。

云端模型目前实现供应方中立的同步 Chat Completions 文本连接器，使用 `MODEL_BASE_URL`、`MODEL_API_KEY` 和 `MODEL_NAME`。请求只含 `model`、`messages` 和 `stream=false`。`finagent.audit.audited_complete_chat` 把脱敏请求、成功或失败记录写入本次运行目录的 UUID 子目录。默认供应方和型号仍未选定。M3 调查流程已使用 LangGraph StateGraph、同源年报限量检索及该审计调用，并由 `scripts/analyze_annual.py --with-model --source-record ...` 选择性接入；这表示代码已编码并接入 CLI，不表示已完成运行验收。`langgraph` 通过 `agents` 可选依赖声明，当前环境未安装，模拟模型集成测试因此跳过；没有真实在线模型调用。已确认先完成本地实现和模拟接口测试；真实在线模型验收等用户配置后再做。不读取其他工具的密钥，也不自行选定供应方。

本地 FastAPI 同时提供旧年度财务预检和 v2 只读报告读取。旧接口为 `POST /v1/annual-prechecks` 与 `GET /v1/annual-prechecks/{run_id}`；v2 有 `GET /v1/annual-analyses/{run_id}` 以及 `GET /v1/annual-analyses/{run_id}/evidence/{evidence_id}/preview.png`，读取已归档报告并只预览报告中已核验的证据。v2 正式分析仍由独立 CLI 启动，没有创建或启动分析的 HTTP `POST` 接口。应用本身不强制 host；按文档中的 Uvicorn 命令默认绑定 `127.0.0.1`。项目无用户注册、登录或账户管理，且不在当前项目范围。模型供应方的 API 密钥仍是独立配置，旧预检不读取它。旧预检在哈希一致后，从原始 PDF 的引用坐标重新读取金额，并独立复核单位换算。这尚未确认年度列、表头口径或完整财报事实，也不是 Agent 风险判断。旧预检记录中的 `screening` 是可选的确定性候选线索；它不改变预检 `status`，`model_called` 与 `independently_verified` 仍为 false。页面在记录含有该字段时展示线索，并明确提示缺失的历史记录未保存 screening。新的旧流程预检归档包含 `code` 和 `verification`。`annual-precheck-603288-2024-20260923-160938` 没有这两项，保持原样。把同一行空格与负号、括号、千分位一并计入金额边界的正式运行是 `annual-precheck-603288-2024-20260923-165111`。`164542`、`163707`、`163126` 和 `160938` 仍保留。

旧版确定性候选筛查写入旧预检的 `screening`，只表示规则命中的线索或弃权。后端还提供文本 PDF 上传与解析，创建页上传成功后填入路径，仍需用户另行创建旧预检。旧预检页面继续读取旧流程记录；独立的 v2 年度分析页已接入只读报告 API 和 Evidence Explorer，可浏览已归档报告及独立核验证据页图。海天正式报告与第 82 页证据预览已通过浏览器 smoke 验收。页面不启动正式分析、不提供产品级导出；M3 模型调查流程虽已编码并接入可选 CLI 路径，但未运行验收。多公司人工 golden、有无核验消融仍待完成。下文对目标模块边界的描述同时标明已实现能力与未完成集成。

目标是完成财报导入、财务分析、独立计算、证据核验及报告生成，提供财务异常和舞弊风险线索。第一阶段围绕可解释、可计算的财报问题展开，具体公司、行业和模型在开发阶段选定。

第一版方案详见 [修订大纲](../submission/proposal/多智能体协同财务欺诈识别方案总结大纲_修订版.docx)。优先覆盖口径可比的非金融上市公司文本型财报，先完成同比环比、非经常性损益、会计口径变化和利润现金流差异分析，再形成异常线索；特殊金融行业和复杂扫描件暂不纳入通用自动分析承诺。

技术方向确定为“云端 API + 本地轻量 Web + 本地统计计算”：React + TypeScript + Vite 前端、Python + FastAPI 后端在本机运行；LangGraph 作为第一版唯一的智能体编排框架；后端通过 API 调用云端模型。文本连接器兼容 Qwen、DeepSeek、OpenAI 的 Chat Completions 共有子集，具体供应方和型号由环境变量决定，项目没有内置默认值。文本型 PDF 解析使用 PyMuPDF。财务公式、统计筛查与数值核验由本地 Python 执行，第一版不部署或训练本地模型，不要求 GPU。最终展示是本机 localhost Web。先用 `uvicorn finagent.api.app:app --host 127.0.0.1 --port 8000` 启动后端；再由用户在 `frontend/` 安装已声明的 npm 依赖并执行 `npm run dev`。Vite 绑定 `127.0.0.1:5173`，把 `/api` 代理到 `127.0.0.1:8000`。浏览器打开 http://127.0.0.1:5173 。本机已执行 `npm install` 并生成 `frontend/package-lock.json`，`npm run typecheck` 与 `npm run build` 已通过。开发服务上的浏览器验收覆盖首页、海天 2024 样例创建、按 `run_id` 回看和错误提示，创建记录为 `annual-precheck-603288-2024-20260923-192840`。克隆后仍需自行安装依赖。浏览器不持有模型密钥。

原始文件、解析结果、检索索引和审计记录保存在本地。云端请求只携带允许外发且与任务有关的证据片段；第一版检索限定本次运行的材料清单，不开放任意互联网检索。模型端点、调用预算、超时和重试上限在实现时配置。现场云端 API 访问是否允许仍待组委会环境说明核实，历史回放不得作为实时处理能力的证明。

## 2. 数据流

```mermaid
flowchart TD
    A["财报原始材料"] --> B["解析、标准化与原文定位"]
    B --> C["带出处的财务事实与检索资料"]
    C --> D["智能体规划与工具编排"]
    C --> E["独立财务计算与风险筛查"]
    D --> F["候选分析与证据引用"]
    F --> G["原文、计算和结论依据核验"]
    E --> G
    C --> G
    G --> H["结构化结果与分析报告归档"]
    G -->|需要补充材料| D
    H --> I["只读报告 API 与 Evidence Explorer"]
```

- 智能体根据任务检索正文、附注和结构化事实，并调用已有工具。
- 财务计算使用带来源的结构化数据和可复现公式；不能依赖大模型在自然语言中给出的计算结果。
- 核验需要回到原始出处。不同模块复用同一个错误提取值时，数值一致不能证明其正确。
- 目标流程是提取、事实核验、确定性计算与筛查、解释与调查、主张核验、报告。补充检索最多一次。v2 独立事实、计算与 Claim 核验以及可比性检查已进入正式 CLI；海天样例中 8 条事实、8 项计算和 16 条确定性 Claim 均验证通过。可比性 proof 来自当前报告第 116、163、197 页，仅在签发它的进程内授权计算，序列化副本仅作审计；尚未独立勾稽此前已披露的 2023 年报。旧预检的坐标金额复核仍只检查金额是否出现在给定区域内及单位换算，状态为 `passed`、`failed`、`abstained`。模型自评不能代替独立核验，未验证内容进入待核查区。
- 可以通过补充材料解决的问题进入有上限的重新检索与核验循环；达到上限、原文不可读或关键口径仍冲突时转人工复核。未解决的主张不能写为已核实结论，报告保留缺口和失败状态。
- 证据不足、提取失败或口径冲突应在结果中明确暴露，避免强行生成确定结论。
- 文件访问、工具调用、计算、核验与输出环节均由 audit 记录，运行记录按 run_id 归档。
- 事实、推论、观点及待核查事项需要区分，统计信号不能直接作为确认舞弊的判定。

## 3. 前端与后端职责

前端仅负责交互和展示，不保存服务端模型密钥或实现独立的财务计算口径。

| 前端目录 | 职责 |
| --- | --- |
| frontend/public/ | 正式静态资源 |
| frontend/src/pages/ | 保留旧年度预检仪表盘：首页、创建、结果、报告四个视图。摘要数字只来自已加载的旧预检记录；结果页展示可选的旧版 screening，并标明历史记录缺少该字段；创建页可上传文本 PDF，但不自动预检。另有只读 v2 年度报告与 Evidence Explorer 页面，通过 API 读取归档报告和已核验证据页图；海天报告及第 82 页证据预览已通过浏览器 smoke 验收。页面不启动正式分析、不提供产品级导出，任务进度尚未实现 |
| frontend/src/components/ | 公共界面组件 |
| frontend/src/api/ | 与后端的请求、响应及错误处理 |
| frontend/src/types/ | 前端数据类型 |
| frontend/src/styles/ | 页面及公共样式 |

后端业务代码位于 backend/src/finagent/：

| 模块 | 职责与边界 |
| --- | --- |
| api | 本地 HTTP 接口，不直接堆放分析公式。旧年度预检有创建、按 run_id 读取及 `POST /v1/text-pdf-uploads`。v2 有只读 `GET /v1/annual-analyses/{run_id}` 和按独立核验证据生成页图的 `GET /v1/annual-analyses/{run_id}/evidence/{evidence_id}/preview.png`；没有创建或启动正式分析的 `POST` 接口。独立 CLI `scripts/analyze_annual.py` 负责正式分析和同 `run_id` 的运行/报告归档。创建页上传后仍需另行创建旧预检；项目不包含用户注册、登录或账户管理 |
| agents | LangGraph 年度调查 StateGraph、候选解释、调用审计、同源检索及最多一次补充检索已编码，并由 CLI 的可选 `--with-model --source-record` 路径接入。`langgraph` 属于 `agents` 可选依赖，当前环境未安装，mock 集成测试跳过；未完成 M3 运行验收，也没有在线模型调用 |
| ingestion | 文档解析、字段提取、单位与报告口径标准化，保留原文位置。已实现文本型 PDF 逐页文字、四项旧年度事实提取及适配到 v2 事实；其他字段和完整口径标准化尚未实现 |
| retrieval | 已实现针对当前年度报告 PDF 的确定性正文与附注片段检索，绑定文档标识、来源哈希、PDF 页码、坐标和证据 ID，并限制片段数与字符数；检索最多补充一次。它不开放任意互联网检索，也不使用评测标签 |
| tools | 将已有解析、检索、计算等能力适配为智能体可调用工具，避免复制业务实现 |
| finance | 确定性指标计算、财务规则、统计筛查与候选风险评分。旧流程已实现四项事实的年度差额和同比率及 `screen_annual_signals`。v2 年度计算和 `screen_v2_annual_candidates` 已进入正式 CLI；海天样例的 8 项计算均经独立重算核验为 `verified`，并产生 2 条 candidate 信号。该结果依赖本次报告内可比性证据，不代表跨公司验证。旧流程在上期为零时只记录绝对差额，不表示通常意义的同比增速；非经常性损益不对归母净利润做比值。通用风险评分和阈值判断尚未实现 |
| verification | 原文、字段、公式、引用及结论证据的核验。旧流程已实现按原始 PDF 引用坐标复核金额并用独立 Decimal 复核单位换算；v2 正式 CLI 独立核验海天样例的 8 条事实、年度可比性和 8 项计算，并对 16 条确定性 Claim 做结构与支持证据检查。可比性 proof 基于当前报告第 116、163、197 页，只在当前进程有效；审计 JSON 不能复用为 proof，且未和此前已披露的 2023 年报独立勾稽 |
| reports | 已实现 v2 JSON 年度报告组装和 Markdown 渲染；真实海天样例报告已由正式 CLI 归档到 `artifacts/reports/<run_id>/`，与 `artifacts/runs/<run_id>/` 共用 run_id。报告含 8 条已核验事实、8 项已核验计算、16 条已核验 Claim 和 2 条候选信号。独立 v2 页面已接入只读报告展示和证据预览；页面不启动分析，也没有产品级导出 |
| schemas | 文档、财务事实、证据、分析任务和结果等共用类型。除旧 `FinancialFact` 外，v2 已有财务事实、`TableCellEvidence`、核验、计算与 Claim 类型，并由正式年度分析 CLI 串联；只读报告 API 和 Evidence Explorer 已接入，尚无创建/启动 v2 分析的 HTTP POST |
| llm | 后端供应方中立的同步 Chat Completions 纯文本调用；M3 调查流程已从可选 CLI 路径调用该连接器。候选根地址见 README，北京和新加坡使用工作空间专属域名，协议限于三家共有字段。默认供应方和型号未选定，当前没有在线调用验收 |
| audit | 文件访问、工具调用、计算及生成过程记录，处理敏感凭据。`audited_complete_chat` 接收 `artifacts/runs` 的直接子目录、带 `document_id` 与 PDF 页码的 `evidence_refs`、`prompt_version`；联网前写入 `started`，成功写入 `succeeded`，失败只写安全类别，不记录 `base_url`、请求头或异常原文。M3 流程已调用该包装器；真实在线调用尚未发生 |
| core | 配置、路径、公共异常等基础能力 |

依赖清单和构建配置分别归属 frontend/ 和 backend/。`backend/pyproject.toml` 已声明 PyMuPDF、FastAPI 和 Uvicorn，测试可选依赖包含 httpx；`langgraph>=1.2,<1.3` 通过可选的 `agents` extra 声明，当前环境未安装。`frontend/package.json` 已声明页面依赖的精确版本，本机 `npm install` 已生成 `frontend/package-lock.json`。`node_modules` 不纳入 Git。LangGraph 是第一版唯一的智能体编排框架。

## 4. 其他目录的职责

| 目录 | 职责 |
| --- | --- |
| config/prompts/ | 可版本化的提示词 |
| config/rules/ | 财务规则、阈值、适用条件和计算口径配置 |
| data/raw/ | 保持原样的原始输入 |
| data/processed/ | 解析文本、表格、标准化事实和可重建索引 |
| data/samples/ | 具备明确来源和使用条件的演示样例 |
| artifacts/runs/ | 按运行归档的输入引用、执行记录与必要中间结果 |
| artifacts/reports/ | 生成的报告与导出 |
| artifacts/evaluations/ | 评测运行结果 |
| evaluation/cases/ | 案例清单、人工标注与评测标签 |
| evaluation/baselines/ | 仅大模型、仅规则或统计模块等对照评测实现 |
| tests/unit/ | 单模块测试。已覆盖文本型 PDF 解析、四项事实提取和年度同比；核验规则测试随核验模块编写 |
| tests/integration/ | 解析、检索、计算、核验等协作测试 |
| tests/e2e/ | 上传到报告输出的完整流程测试 |
| scripts/ | 可复用的工程操作脚本 |
| submission/proposal/ | 初赛计划书及可编辑源文件 |
| submission/video/ | 视频脚本及提交视频 |
| submission/final/ | 决赛报告及最终交付材料 |

模块内临时内容进入所属目录的 tmp/<task_id>/；跨模块临时内容进入 artifacts/tmp/<task_id>/。已有临时目录和文件默认保留，任务结束不主动清理。正式流程不依赖临时目录。

## 5. 接口与类型的规划边界

旧年度预检业务类型包括文本型 PDF 解析结果和 `FinancialFact`，分别在 `backend/src/finagent/schemas/text_pdf.py` 与 `financial_fact.py`。隔离的 v2 schema 还包含新的财务事实、表格单元格证据、核验、计算和 Claim 类型。HTTP 路由包含旧流程 `POST /v1/text-pdf-uploads`、`POST /v1/annual-prechecks`、`GET /v1/annual-prechecks/{run_id}`，以及 v2 只读的 `GET /v1/annual-analyses/{run_id}` 和已核验证据页图预览 GET；没有创建或启动 v2 分析的 POST。v2 正式分析由独立 CLI `scripts/analyze_annual.py` 启动；M3 调查流程已接入 CLI 的可选 `--with-model --source-record` 路径，但尚未运行验收。字段缺口见 [current-schema-audit.md](current-schema-audit.md)，路线见 [roadmap.md](roadmap.md)。

旧年度预检的创建、按 run_id 读取和页面展示已经接上现有接口。创建页已接文本 PDF 上传，上传后仍需另一次旧预检。v2 报告归档由独立 CLI 写入 `artifacts/reports/<run_id>/`；只读报告和已核验证据页图 API 已接入独立的 v2 年度报告与 Evidence Explorer 页面，并通过海天报告及第 82 页证据预览浏览器验收。页面不启动正式分析，任务进度和产品级报告导出仍未实现。

财务事实至少需要表达原始出处、数值、单位、币种、报告期和报表口径；核验结果需要表达检查对象、依据与状态。确切类型定义集中在 schemas，避免各模块各自维护不一致的字段。

## 6. 开发与验证顺序

软件开发范围是可运行系统、数据、核验和复现说明。初赛计划书 PDF 和项目介绍视频 MP4 不在本开发任务内。`submission/` 仍保留比赛材料；比赛对计划书和视频的客观要求不变。本文不指定这些材料的完成人，已有比赛资料保持原样。

1. 文本型 PDF 的逐页文字与坐标已实现，并已用公开年报样例做过定位验收。
2. 旧流程的四项年度事实、确定性同比和引用坐标内的原文金额复核已实现。v2 确定性 Golden Path 已通过正式 CLI 在海天 2024 样例验收，并归档 8 条独立核验事实、8 项独立核验计算、16 条通过确定性检查的 Claim 和 2 条候选信号。M1、M2 对该样例已验收；可比性结论只依据当前年报披露，没有独立勾稽此前已披露的 2023 年报，也不代表跨公司泛化。
3. M3 年报内调查、LangGraph、提示词与审计调用已编码并接入 CLI 可选路径；当前环境未安装 LangGraph，mock 集成测试跳过，尚无运行验收或真实在线模型调用。待可选依赖可用后完成模拟验收，再等用户配置供应方和密钥进行在线验收。
4. 旧年度预检接口和页面已在开发服务上验收；v2 只读报告 API、Evidence Explorer 与独立核验证据预览也已接入，并通过海天归档报告和第 82 页证据预览浏览器 smoke 验收。v2 页面不启动分析，也没有产品级导出。按文档先启动绑定 127.0.0.1 的后端，再启动 Vite。后端文本 PDF 上传、页面上传与旧预检仍是分开的步骤；项目不包含用户注册、登录或账户管理。
5. 评测与复现说明：使用相同输入比较各方案，记录准确性、查准率、召回率、引用正确性和稳定性。

旧流程和 v2 的单元/集成检查覆盖 PDF 解析、事实提取、可比性、计算、报告读取及调查逻辑。`tests/integration/test_annual_investigation_mock.py` 使用可选 LangGraph 依赖测试调查图、调用审计和一次补充检索；该依赖当前未安装，因此此 mock 集成测试被跳过，本机最近完整后端结果为 241 passed、1 skipped。`tests/integration/test_cross_company_annual_facts.py` 覆盖海天、茅台和五粮液三份真实年报，但不构成多公司人工 golden 或泛化验收。前端没有单独的自动化测试套件；`npm run build` 已通过，海天归档报告及第 82 页证据预览已完成浏览器 smoke 验收，旧预检页面也有既有浏览器验收记录。
