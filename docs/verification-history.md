# 既往验收记录与证据索引

> 2026-10-01 从 AGENTS、README、架构、数据和路线文档整理而来。本文件记录历史结果，不是执行指令或当前环境探测结果。测试通过、依赖已安装、能力为 not_configured 等表述只适用于原记录的日期、代码和机器；其中“本轮”“当前”均指原验收当时。迁移文字未重新运行实验。
>
> 既往授权只用于解释那次操作，不自动授权新任务读取其他工具的凭据。原始年报、归档、代码快照与旧失败运行保持原样。没有所需本地文件时应明确不能复核，不能把记录中的“通过”当成本次验证。

## 主要工程证据

| 历史事项 | 记录索引与范围 |
| --- | --- |
| 海天 M1/M2 单样例 | annual-analysis-ffcd6078-a1df-416b-88b8-6774ebe66d42；8 条事实、8 项计算、16 条确定性 Claim 核验通过，2 条候选；仅依据同一 2024 年报比较列与可比性披露 |
| 海天 M3 四规则 | annual-analysis-5e7079fe-9ac9-4e96-85fa-1898f9177d21；10 条新事实、8 条显式单位副本、18 条直接核验；四条均可计算、总分为 0。非经常性损益合计副本只作审计 |
| 2026-09-28 候选调查在线单例 | annual-analysis-a32c16a7-00da-48c5-a97d-59ee284904df；评估 deepseek-live-m3-603288-20260928-a32c16a7；DeepSeek 官方直连，两次调用归档两条未核实解释，不含后来新增的最终评审 |
| 在线单例首轮缺陷 | annual-analysis-af95416a-272f-4045-b0fd-633c7090b4cb；有效回答曾因编排预算和 request_more_context 处理被丢弃，修复后另存成功记录 |
| 茅台 provisional | annual-analysis-10232970-a22b-498a-9a8b-4bde1b5bb8e6；8 条事实核验通过，可比性不足，8 项计算未确认；不是人工 golden |
| 五粮液修复后 provisional | annual-analysis-956db60d-6595-4d7c-8277-07d1d7e21326；可比性不足，8 条事实因币种不明未确认，8 项计算未确认，2 条筛查弃权。旧 0.00 误判已修复，不把修复前 conflict 当作新结果 |
| M4 单公司受控消融 | verification-ablation-m4-20260928T072029Z-825a8c4b；两个 clean、五个注入错误。核验支路接受 clean 2/2、拒绝错误 5/5，无核验支路接受错误 5/5；clean 由 agent 转录、未人工复核 |

annual-analysis 运行与报告分别在 `artifacts/runs/<run_id>/`、`artifacts/reports/<run_id>/`；评估位于 `artifacts/evaluations/<evaluation_id>/`。M4 协议与数据分母见 [数据规则](data-policy.md)，不是舞弊识别准确率或多公司泛化证明。

## 2026-09-30 检查快照

- 完整后端：344 passed、2 skipped，exit 0，97.35 秒。两个跳过均因 Windows 无法创建符号链接，覆盖设置文件和源 PDF。
- 模型设置专项：36 passed、2 skipped。最终评审专项：39 passed、1 skipped；mock 验证两条候选调查后一次评审，以及 report/manifest 共三次调用。
- LangGraph 1.2.12 的真实 StateGraph 本地 mock 集成检查为五项通过；这不是在线最终评审。
- 前端 typecheck/build 通过；设置页 Edge/CDP mock QA 35/35，含空密钥沿用、测试不保存、刷新不回显、清除回退及 390px 检查。
- 模型评审 UI 的 mock/历史归档检查覆盖成功、失败、待核验、M2/M3、页图；当时 capability 为 not_configured。不能由此推定之后的配置状态。

证据位置：`artifacts/tmp/model-settings-20260930/pytest-full.log`、`pytest-skip-detail.log`，`frontend/tmp/model-settings-20260930/`、`frontend/tmp/review-clarity-20260930/qa-results.json`、`frontend/tmp/reference-redesign-20260930/`。tmp 证据默认保留但不随 Git 分发；它们是历史旁证，不是正式功能依赖。

## 迁入的历史说明

下列摘录按原记录保留，便于定位旧运行及解释历史边界。它们不更新上方实现状态，也不要求执行其中的命令或旧工作安排。
### 原记录摘录 1

> 2026-09-30 按参考图完成前端主导航、工作区顶栏、分析首屏、四项指标和两年数据对照、候选/核验/计算/主张分区及证据页预览的视觉重排；保留旧预检流程与现有任务状态。Headless Edge/CDP 在 1440px、1584px 与 390px 视口检查海天 M2/M3、五粮液待核验归档及旧预检创建/回看，页面无横向溢出；截图与检查记录保存在 `frontend/tmp/reference-redesign-20260930/`。

### 原记录摘录 2

> 2026-09-28 建立一家公司、七个已知标签案例的 M4 有/无独立核验消融基线，运行 ID `verification-ablation-m4-20260928T072029Z-825a8c4b`，可从仓库根目录执行 `python scripts/evaluate_verification_ablation.py` 重跑。海天 603288 2024 年报 PDF 直接参考行上，独立核验支路正确接受 2/2 个 clean 对照并拒绝 5/5 个合成错误；无核验支路接受 5/5 个错误。茅台与五粮液只做 provisional 状态对照（分别为 `verified`、`insufficient_evidence`），未计入准确率分母。clean 真值由 agent 按原 PDF 转录、尚未经人工审核；本基线不等于 3–5 家公司的人工 golden 或完整 M4 验收。详情见 `artifacts/evaluations/verification-ablation-m4-20260928T072029Z-825a8c4b/` 和 `evaluation/verification_ablation_cases_v1.json`。

### 原记录摘录 3

> 2026-09-28 已通过单个 FastAPI app 对海天 603288 2024 年报完成 deterministic HTTP 联通验收：任务 `annual-job-09f7cbdc377345e2a11da516b4277a68` 从 `queued` 经实际阶段到 `completed`，归档 `run_id=annual-analysis-979a791e-9902-43c0-8173-d20f4bf486fc`，随后 GET 报告返回 200。该次未调用在线模型。

### 原记录摘录 4

> 该默认配置从单元与集成测试目录收集测试；建议在仓库根目录运行上面的带配置命令。`tests/__init__.py` 将项目测试目录标记为本地 Python 包，避免环境中同名第三方 `tests` 包抢占旧用例的导入。也可以进入 `backend/` 后执行 `python -m pytest`。截至 2026-09-30，最近一次完整后端检查为 344 passed、2 skipped，pytest exit 0，耗时 97.35 秒；两项跳过均为 Windows 无法创建符号链接的安全测试，分别覆盖本机设置文件与源 PDF。完整日志见 `artifacts/tmp/model-settings-20260930/pytest-full.log` 与 `pytest-skip-detail.log`。本机模型设置聚焦检查为 36 passed、2 skipped。2026-09-28 旧检查中，`test_chat_completion.py` 的 mock HTTP 线程收尾曾打印一次 WinError 10053，但对应测试通过。`tests/integration/test_annual_investigation_mock.py` 的 5 项真实 LangGraph StateGraph mock 测试均通过。2026-09-30 模型最终评审聚焦后端验证为 39 passed、1 skipped，命令为 `python -m pytest -c backend/pyproject.toml tests/unit/test_annual_model_review.py tests/unit/test_investigation_append.py tests/integration/test_annual_analysis_jobs_api.py tests/integration/test_annual_model_review_job_e2e.py -q`；本地 mock HTTP 实际验证两条候选调查后的一次最终评审、归档 GET，以及 report/manifest 记录的总调用数 3。该 39 项专项结果不是完整后端测试，也不包含真实在线最终评审。设置聚焦检查使用本地 mock；本轮未保存模型配置或调用真实供应方。前端设置交互 Headless Edge/CDP QA 为 35/35 通过，覆盖保存、测试连接成功/失败均不自动保存、空密钥沿用、刷新后不回显、清除后回退环境、M2 归档查看；1440px 与 390px 均通过，390px `scrollWidth=390`，无控制台错误或外网请求。该 QA 使用浏览器内存 API mock，没有写 `.env.model`。主控另以重启后的本机服务及实际导航验收设置页：`/api/v1/model-settings` 和能力接口均返回 200，显示未配置；没有真实模型请求，根目录未生成 `.env.model`。相关结果与截图见 `frontend/tmp/model-settings-20260930/`。`npm run typecheck` 与 `npm run build` 均通过。未做可编辑安装时，测试配置会把 `backend/src` 加入导入路径；运行集成测试还需要本机的公开年报样例 PDF，缺失时相关测试会跳过。

### 原记录摘录 5

> 2026-09-23 与 2026-09-25 的浏览器验收记录覆盖旧预检创建、按 `run_id` 回看、错误提示和文本 PDF 上传。2026-09-27 的真实浏览器验收读取海天 2024 正式 v2 报告，并成功显示第 82 页已核验证据预览；该次只验证归档读取与证据查看。2026-09-28 后续验收覆盖了从页面启动海天样例、自动读取同一 `run_id` 报告，以及按 `run_id` 读取 M3 归档的四条规则；390px 检查无横向溢出，中文 PDF 选择控件可用键盘聚焦并显示焦点样式。该次 `npm run typecheck`、`npm run build` 均通过。当前页面没有单独的自动化测试套件，也没有产品级导出。此前旧流程构建/typecheck 记录与浏览器日志保留在 `artifacts/runs/web-qa-20260923-200437/browser/`；本轮截图保留在 `frontend/tmp/annual-job-frontend-smoke/`。2026-09-30 新增的模型评审页面检查使用 Headless Edge/CDP，QA 全断言为 true，覆盖 M2、M3、五粮液弃权/币种不足、历史模型调查、前端读取后端构造 mock fixture、模拟最终评审和失败、原文页预览；390px `scrollWidth=390`，无横向溢出和 console errors。主控另在真实 M2 页面人工确认标题、金额、原因及后续材料。能力接口返回 `not_configured`，本轮不含真实在线最终评审验收。结果见 `frontend/tmp/review-clarity-20260930/qa-results.json`，卡片截图见 `frontend/tmp/review-clarity-20260930/acceptance-candidates.png`。新增模型设置页面的 35/35 浏览器 QA、实际本机 HTTP 空配置读取和真实导航验收，以及 QA 本地截图见 `frontend/tmp/model-settings-20260930/`；该验收不包含真实供应方调用，根目录没有 `.env.model`。`npm run typecheck` 与 `npm run build` 均通过。

### 原记录摘录 6

> 正式事实运行是 `annual-facts-603288-2024-20260923-112307`。已归档的旧预检 `annual-precheck-603288-2024-20260923-160938` 同样得到 8 条事实和 4 组同比，原始 PDF 哈希与 `source_sha256` 一致，且未调用模型。该次 `precheck.json` 是旧结构，只有输入哈希，没有 `code` 和 `verification` 字段，记录保持原样。带代码快照、并把同一行空格与负号、括号、千分位一并计入金额边界的正式运行是 `annual-precheck-603288-2024-20260923-165111`：8 条事实均在引用坐标内重新读到原金额，数值换算复核通过，失败和弃权为 0。`164542`、`163707`、`163126` 和 `160938` 仍保留。`annual-precheck-603288-2024-20260925-105059` 是带 `screening` 的最新正式运行；较早的 `104521` 仍保留。该次利润和收入差额为正、经营现金流差额为负，两条线索为 candidate，引用页为 82 和 86；非经常性损益不对归母净利润做比值。预检状态仍是 completed，`model_called` 与 `independently_verified` 仍为 false。这尚未独立确认年度列、表头口径或完整财报事实。此后新建的预检会写入 `code`：有 Git 时记录 `git_head` 和 `git_dirty`，没有 Git 时把这两项标为空且 `git_available` 为 false，并始终记录关键业务源码的 SHA256。2024 年金额是当年列，2023 年金额是这份 2024 年报的比较列；是否追溯调整尚未确认，`restatement_status` 为 `unknown`。营业收入没有“一、营业收入”主行，改用“其中：营业收入”。非经常性损益合计的口径是披露表格口径。单位为元。较早的 `annual-facts-603288-2024-20260923-111838` 仍保留。

### 原记录摘录 7

> 原 AGENTS 项目状态快照（截至 2026-09-30）
>
> - 目标：FINTRACE 面向非金融上市公司文本型年报，构建可追溯、可核验的财务异常分析，当前从海天 603288 的 2024 年报起步。路线见 `docs/roadmap.md`，旧预检字段审计见 `docs/current-schema-audit.md`。异常线索不是确认舞弊。
> - 技术方向：云端 API + 本地轻量 Web + 本地统计计算。前端为 React + TypeScript + Vite，后端为 Python + FastAPI；LangGraph 是第一版计划采用的唯一智能体编排框架，云端模型由后端调用，财务计算及数值核验在本地执行。模型连接器使用 OpenAI 兼容 Chat Completions 的共有子集，候选供应方包括 Qwen、DeepSeek 和 OpenAI；网页可通过“设置”配置服务地址、型号和密钥并保存到项目根目录 `.env.model`，Web AI 任务优先使用该文件、文件不存在时回退到 `MODEL_*` 环境变量；CLI 仍只读环境变量。项目未选定默认供应方或型号。
> - 旧年度预检流程已使用 FastAPI，并有本机预检页面。其接口和页面仍使用旧事实、同比、坐标金额复核与旧版确定性 screening；页面在记录包含 screening 时展示旧版候选线索，没有该字段的历史记录会明确显示未保存。上传与预检分步，不覆盖已有原始资料。应用本身不强制 host；按文档启动命令，后端默认绑定 `127.0.0.1:8000`，Vite 开发服务绑定 `127.0.0.1:5173` 并把 `/api` 代理到后端。设置页密钥只在用户填写后短暂留在页面内存，经浏览器 `/api` 代理发送给本机后端；API 不回显密钥，浏览器不将其写入本地存储。设置 API 限制为 loopback Host，并对修改/测试请求检查本机 Origin。旧预检不读取模型密钥。页面浏览器验收记录为 `annual-precheck-603288-2024-20260923-192840`；本机已在 `frontend/` 执行 `npm install`，生成 `frontend/package-lock.json`，并完成 `npm run typecheck` 与 `npm run build`。`node_modules` 不纳入 Git，克隆后仍需自行安装。页面没有单独的自动化测试套件。项目无用户注册、登录或账户管理，且不在当前项目范围。
> - v2 确定性年度分析 CLI `scripts/analyze_annual.py` 已打通 PDF 提取、独立事实核验、年度可比性检查、计算与独立计算核验、筛查、Claim 核验和报告归档。海天 603288 的 2024 样例正式运行 `annual-analysis-ffcd6078-a1df-416b-88b8-6774ebe66d42` 已归档：8 条事实、8 项计算、16 条确定性 Claim 均通过核验，形成 2 条候选信号。可比性依据来自同一 2024 年报 PDF 第 116、163、197 页；proof 由当前进程签发，序列化结果只供审计，结论不表示与此前已披露的 2023 年报独立勾稽。M1（海天样例）和 M2（单样例确定性 Golden Path）已验收，不代表跨公司泛化。后端另有只读 `GET /v1/annual-analyses/{run_id}` 和已核验证据 PDF 页图预览接口；新增 `POST /v1/annual-analysis-jobs` 与 `GET /v1/annual-analysis-jobs/{job_id}`，通过有界单进程队列启动 deterministic 或 M3 筛查并持久化任务状态。任务从 `data/raw` 已存 PDF 启动，校验路径、`source.json` 身份及 SHA256；服务重启后未结束任务标为 `interrupted`。默认一个运行任务、最多三个排队任务；`model_investigation` 模式仅在 LangGraph 依赖和有效模型设置就绪时启动。设置接口优先读取项目根目录 `.env.model`，缺少该文件时读取服务端 `MODEL_BASE_URL`、`MODEL_API_KEY`、`MODEL_NAME`；能力接口 `/v1/annual-analysis-capabilities` 只返回可用状态，不泄漏配置值，未就绪时创建任务返回 503。CLI 仍只从环境变量读取模型配置；Web 子进程仅在 `model_investigation` 模式注入已解析的设置，deterministic 与 M3 不带 `MODEL_*` 凭据。确定性与 M3 筛查模式不调用模型。
> - 前端保留旧年度预检页面，并新增 v2 正式年度分析流程：可上传文本 PDF 或一键使用本机海天样例，启动确定性分析或显式标记为试行的 M3 四规则筛查，查看准确阶段、失败/中断状态并重试；页面尽可能从浏览器存储恢复当前 job_id。成功后自动读取同一 run_id 报告和已核验证据页图，也保留按 run_id 回看旧归档的入口。M3 规则线索与核验事实分开显示，旧归档没有 M3 字段时正常隐藏；页面新增显式“AI 分析与评审”模式，仅在后端能力就绪时可启动；历史报告没有 `model_review` 字段时继续兼容。报告顶部先呈现筛查摘要，模型评审完成时显示 AI 评审意见；核查卡指出具体对象、满足独立核验条件的年度变化和建议查看材料，比较证据不足时不判断变化方向。模型调查后增加一次审计最终评审，模型评审只是观点，结构及引用 ID 绑定检查不代替独立核验；默认确定性/M3 模式不调用模型。暂无产品级导出。npm run typecheck 与 npm run build 通过；Chrome headless smoke 已从页面启动样例 job annual-job-f585b9c64c064ec48b12e038dd6a00e1，读取其 run annual-analysis-b788f884-e66d-4374-a2a7-2b5283ca4b44，并按 run_id 读取 M3 归档 annual-analysis-5e7079fe-9ac9-4e96-85fa-1898f9177d21 的四条规则。旧预检 API/UI 与正式年度分析分开，旧页面仍使用旧事实、同比、坐标金额复核与旧版 screening。默认入口及主导航现为“分析年报”，旧预检位于“基础预检（旧流程）”单独分组，旧报告占位已从导航移除；本轮非技术化中文界面通过 Chrome 桌面及 390px 浏览器验收，截图保留在 `frontend/tmp/nontechnical-redesign/`。本轮 `npm run typecheck` 与 `npm run build` 均通过。Headless Edge/CDP QA 全断言为 true，覆盖 M2、M3、五粮液弃权及币种不足、历史模型调查、前端读取后端构造 mock fixture、模拟最终评审与失败处理、原文页预览；390px `scrollWidth=390`，无溢出或 console errors。主控另在真实 M2 页面核对标题、金额、原因和后续材料。日志见 `frontend/tmp/review-clarity-20260930/qa-results.json`，截图见 `frontend/tmp/review-clarity-20260930/acceptance-candidates.png`。当前 capability 为 `not_configured`，真实在线最终评审仍未验证。
> - 2026-09-30 新增“设置”页面配置兼容 Chat Completions 的服务地址、模型名称和 API 密钥，提供保存、清除及按草稿测试连接。已保存密钥只由后端读取，不向页面回显或写入浏览器存储；密钥经浏览器 `/api` 代理提交并保存在项目根目录 `.env.model`，此文件被 Git 忽略、以明文保存并尽可能限制文件权限。完整本机设置优先于环境变量，删除后回退环境；损坏的本机配置失败关闭。服务地址不变且当前配置有有效密钥时，空密钥可沿用该密钥并保存完整本机副本；更换地址需重新填写。测试连接使用一条固定短消息、8 秒超时和审计记录，不保存草稿、不发送年报；用户点击后会真实调用所配服务。设置完整只表示已配置，不代表连接已成功。CLI 仍读环境变量，只有 Web `model_investigation` 任务使用解析后的设置；确定性/M3 任务不携带模型凭据。本轮设置后端聚焦检查为 36 passed、2 skipped，完整后端检查为 344 passed、2 skipped。设置页 Edge/CDP mock QA 为 35/35，390px 无溢出、无控制台错误；主控实际通过导航和本机 HTTP 核对空配置与 `not_configured` 能力。前端 `npm run typecheck` 与 `npm run build` 通过；本轮未保存模型配置，也未调用真实供应方。验证记录见 `artifacts/tmp/model-settings-20260930/` 和 `frontend/tmp/model-settings-20260930/`。项目没有默认供应方或型号。
> - 年度分析页在候选线索及待核查事实、计算、主张旁展示关联指标的 PDF 物理页序号。只有同源、独立核验通过且命中页图白名单的出处可以打开预览；未核实提取位置明确标为“待核实定位”，缺少可靠页码时显示“原文位置待定位”。这些页码不改变候选线索或待核查对象本身的状态。
> - M3 调查流程、有限年报内检索、提示词和审计调用代码已实现，并可由 `scripts/analyze_annual.py --with-model --source-record ...` 选择性调用。应收账款净额、存货净额、合并净利润、营业成本和扣非归母净利润的 v2 独立提取/核验切片已编码并通过海天真实 PDF 与构造 PDF 测试；新增 `finance/m3_screening.py` 已实现四条独立年度规则，并由 `scripts/analyze_annual.py --with-m3-screening` 显式可选调用；默认 M2 与旧筛查保持原样。海天 603288 2024 正式样例运行 `annual-analysis-5e7079fe-9ac9-4e96-85fa-1898f9177d21` 已归档 10 条新字段事实、8 条旧事实显式单位副本、18 条 M3 直接核验结果，四条规则均可计算且总分为 0。`non_recurring_total` 副本仅供单位证据审计，不输入规则；其审计问题不使核心规则失效。扣非归母净利润仍为 `key_financial_data`、`scope=unknown`；规则二仅接受规则专属语义 proof，该 proof 重读海天 603288 2024 年报物理第 7 页并逐年交叉核对“归属于上市公司股东的净利润”与合并利润表归母净利润。此实现只支持该样例版式，不代表跨公司能力；proof 由当前进程签名，序列化结果仅供审计。筛查入口的核验结果调用约定是同一进程直接取得的 `verify_financial_fact` 结果；`VerificationResult` 本身不是签名 proof，反序列化 JSON 或手工重建对象不能作为核验凭据；提取与核验证据 ID 还必须互不重叠。`langgraph` 是 `agents` 可选依赖；LangGraph 1.2.12 已安装；真实 StateGraph 模拟集成测试通过（5 项），M3 mock 流程已完成运行验收；2026-09-28 已对海天 603288 2024 样例完成 DeepSeek 官方直连单例验收，运行 `annual-analysis-a32c16a7-00da-48c5-a97d-59ee284904df` 归档两条未核实解释，评估 ID 为 `deepseek-live-m3-603288-20260928-a32c16a7`。首轮编排缺陷已修复；该历史单例只验收候选解释，不含后来新增的最终评审，也不代表跨公司能力、人工 golden 或独立核验。第三方来源、哈希和许可证记录见 README。茅台 600519 与五粮液 000858 的 2024 年报事实参考案例及 CLI 结果已归档，但案例仍为待人工审阅的 provisional 标签，不是人工 golden。五粮液 `0.00` 的 Decimal 误判已修复，正式运行 `annual-analysis-956db60d-6595-4d7c-8277-07d1d7e21326` 已归档：可比性为 `insufficient_evidence`，8 条事实币种未明确而未确认，8 项计算未确认，2 条筛查均弃权。M4 已建立一家公司、七个已知标签案例的有无独立核验消融基线；它不构成 3–5 家公司人工 golden 验收，两个跨公司 provisional 样例仍只作未知状态对照并排除准确率分母。结果和口径见 `artifacts/evaluations/verification-ablation-m4-20260928T072029Z-825a8c4b/`、`evaluation/verification_ablation_cases_v1.json` 与 `scripts/evaluate_verification_ablation.py`。
> - 最近完整后端检查结果（2026-09-30）为 344 passed、2 skipped，pytest exit 0，耗时 97.35 秒；两项跳过均因 Windows 无法创建符号链接，覆盖本机设置文件安全测试与既有源 PDF 符号链接测试。完整日志见 `artifacts/tmp/model-settings-20260930/pytest-full.log` 和 `pytest-skip-detail.log`。本机模型设置聚焦检查为 36 passed、2 skipped；此前模型评审聚焦检查为 39 passed、1 skipped，mock 验证两条候选调查后进行一次最终评审、归档 GET 及 report/manifest 总调用数 3。已通过海天 603288 2024 年报的 deterministic HTTP 联通验收，job `annual-job-09f7cbdc377345e2a11da516b4277a68` 对应 run `annual-analysis-979a791e-9902-43c0-8173-d20f4bf486fc`，GET 报告返回 200。前端页面没有单独的自动化测试套件。文本解析依赖 PyMuPDF。
> - 当前交付已超出纯骨架。具体能力以 README.md 和实际代码为准，不把计划描述为已实现。
> - 输出财务异常、风险线索及其依据，明确区分事实、推论与观点；异常信号不能直接作为确认舞弊的结论。
>
> - 2026-09-30 前端按 `tem/web_expected/` 四张参考图完成视觉重排，保留旧预检和现有分析 API/任务语义。Headless Edge/CDP 已检查 M2、M3、五粮液待核验归档、旧预检创建/回看及桌面和 390px 布局；截图与日志见 `frontend/tmp/reference-redesign-20260930/`。

## 原文涉及的运行标识索引

仅用于定位历史文件，列出不表示该运行成功、最新或可重新执行。摘录未展开的旧运行也保留在本地；不覆盖失败记录。

- `annual-analysis-10232970-a22b-498a-9a8b-4bde1b5bb8e6`
- `annual-analysis-5e7079fe-9ac9-4e96-85fa-1898f9177d21`
- `annual-analysis-956db60d-6595-4d7c-8277-07d1d7e21326`
- `annual-analysis-979a791e-9902-43c0-8173-d20f4bf486fc`
- `annual-analysis-a32c16a7-00da-48c5-a97d-59ee284904df`
- `annual-analysis-af95416a-272f-4045-b0fd-633c7090b4cb`
- `annual-analysis-b788f884-e66d-4374-a2a7-2b5283ca4b44`
- `annual-analysis-e6d2e772-bf36-4f3b-ae95-f6ca325b9987`
- `annual-analysis-ffcd6078-a1df-416b-88b8-6774ebe66d42`
- `annual-facts-603288-2024-20260923-111838`
- `annual-facts-603288-2024-20260923-112307`
- `annual-job-09f7cbdc377345e2a11da516b4277a68`
- `annual-job-f585b9c64c064ec48b12e038dd6a00e1`
- `annual-precheck-603288-2024-20260923-160938`
- `annual-precheck-603288-2024-20260923-164414`
- `annual-precheck-603288-2024-20260923-165111`
- `annual-precheck-603288-2024-20260923-192840`
- `annual-precheck-603288-2024-20260923-211321`
- `annual-precheck-603288-2024-20260925-105059`
- `deepseek-live-m3-603288-20260928-a32c16a7`
- `verification-ablation-m4-20260928T072029Z-825a8c4b`
