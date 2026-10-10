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

## 2026-10-02 A-01 页面信息契约审阅

主控审阅接受海天 603288 年报结果页信息契约，随 `921aa19d98161a54b7afcd426c567949f6173615` 提交。接受范围包括真实金额、公司/文档身份、报告期间、单位、合并口径、可比性及缺失原因边界；首屏在目标视口中的呈现随后由 A-03–A-05 最终浏览器矩阵核验，见下方“最终软件冻结与交接验收”。

## 2026-10-02 A-02 年报叙述证据与在线评审验收

对海天 603288 的 2024 年报执行了四次完整 Web `model_investigation` 任务，每次实际三个模型请求；旧 job/run 和原审计归档均保留，累计十二次尝试。前三次用于定位严格归档和输出契约问题，最终一次在提示词补足既有解析边界后复验。前三次状态如下：

| Job / run | 状态 | 模型评审 | 发现 |
| --- | --- | --- | --- |
| `annual-job-94fdb4e4e5fc44778192c590d41fe3e0` / `annual-analysis-828c06f0-1247-48fc-bbf1-a170204a4583` | failed | completed | 报告原文尾随换行与 PDF 文本块比较方式不一致，归档读取报告 `narrative_evidence_source_mismatch`；只修正为两端均忽略首尾空白并继续完整比对文本及坐标，旧归档未改。 |
| `annual-job-0afd1c16237148a795ec20b76817512b` / `annual-analysis-9b18ef83-58a7-4f80-9321-fa13d5fbfc24` | completed_with_issues | failed | 最终评审请求曾包含模型不可引用的叙述 `nctx-*` ID；从最终评审输入移除，保留 `investigation:<signal_id>` 引用，白名单解析保持严格。 |
| `annual-job-ff5d592924ec4e8a8b95771f620bbb7b` / `annual-analysis-a153e463-bd0f-4b0a-88ec-b311b662bf7e` | completed_with_issues | failed | 实际响应只含白名单 ID，但有一条理由引用九项，解析器按八项上限拒绝；据此在 `annual_review_v1` 明示引用数量、唯一性和逐字段文本/数组限额，并增加八项通过、九项及重复项拒绝回归。 |

最后一次有界复验为 job `annual-job-618e6dd4fa984e1dbabecf13a2ece4d8`、run `annual-analysis-8036d75c-6a13-432a-b602-5f869f9396ce`：job、调查和最终评审均 completed，评审 assessment 为 `prioritize_review`；调查两次调用，最终评审一次，总计三次。运行归档含四条被引用叙述片段与八条独立核验金额证据。GET 报告、叙述索引及两种 focus 页图均由真实 HTTP 读取；四条片段均与归档 PDF 的原文及 bbox 相符。金额和叙述白名单保持分离；旧报告读取仍兼容。

页 154 记载预收款在签订合同时收取、相关合同收入在履约义务完成后确认。这支持收入确认与现金收取可能跨期的机制假说，不证明其造成经营活动现金流下降；同一模型解释也明确原因无法确认。收入调查另有一句限制称“候选差异为计算字段，尚未核验；原文片段不足以确认任何因果关系或异常情形。”结构化报告中的三项核心指标、两年值合计六条事实均为 `verified`；净利润、营业收入及经营活动现金流三个年度差额均为 `succeeded` 且独立重算为 verified。该句作为未核实模型措辞保留，不覆盖确定性核验状态，也不作为系统级计算缺口。

正式证据在 `artifacts/evaluations/a02-live-web-20261002-annual-analysis-8036d75c-6a13-432a-b602-5f869f9396ce/`，其中保存尝试历史、核验摘要，以及第 154 页叙述焦点图和已核验金额焦点图；原始审计与 PDF 位于相应 `artifacts/runs/<run_id>/`。A-02 当时的 Web 服务通过 `scripts/start.ps1 -NoBrowser` 启动，API PID 为 32276；`artifacts/tmp/startup-c6e19440d0e841dc91945bceb757e3b5/` 是启动目录，未保留启动日志。之后的受控 API 重启见 A-04 记录。Python 3.14.3、FastAPI 0.136.3、PyMuPDF 1.27.2.3、LangGraph 1.2.12、pytest 9.0.3、Node 24.14.1；审计请求/输出的常见凭据模式扫描结果为 absent（布尔值）。评审提示文件 SHA256 记于同目录 `evaluation-evidence.json`。

最终运行的 manifest 记录 `git_head=921aa19d98161a54b7afcd426c567949f6173615`、`git_dirty=true`。在本轮冻结文档核对时，逐项重算 manifest 中 29 个分析源码/提示词 SHA256，29 项均与工作区当前对应文件一致。此结论只覆盖 manifest 列出的分析链文件，不覆盖后来接入的 history API 或前端代码；不能把该运行称为最终代码提交运行。可按运行目录 `manifest.json` 的 `code.source_file_sha256` 复核。

A-02 相关后端测试聚焦集合为 95 passed、1 skipped；跳过来自 Windows 环境无法创建测试所需 symlink/junction 的平台守卫，原 `-q` 输出未给出具体 nodeid。提示修正后评审单元与任务集成为 15 passed。准确命令、退出码、跳过说明及当时未执行完整套件的边界见同目录 `checks.json`；历史 API 接入后的唯一最终完整后端套件见下方 A-04 记录。上述在线结果不是多公司或因果识别验收。

## 2026-10-02 A-04 本机历史索引 API 验收

历史索引实现及路由通过主控代码审阅并提交于 `0d473da67a6a8b77cdbad86dfb8aaee708311368`（history 模块、app 路由集成和单元测试三个文件）。A-04 后端最终检查仅运行一次完整后端测试集：`python -m pytest -c backend/pyproject.toml tests -rs`，exit 0，367 collected、364 passed、3 skipped，86.33 秒。正式输出在 `artifacts/evaluations/annual-history-final-2813307e2c0440e0ac4429ad51743529/pytest-output.txt`；三个跳过分别为任务 API 的 PDF 符号链接、history 的目录符号链接和模型设置文件符号链接测试，均因当前 Windows 环境不能创建相应链接。

同一目录 `http-verification.txt` 记录受控重启项目 API 后的真实 HTTP 核对：`GET /v1/annual-analysis-history?limit=50&offset=0` 返回 `total=23`、23 条；A-02 job `annual-job-618e6dd4fa984e1dbabecf13a2ece4d8` 与 run `annual-analysis-8036d75c-6a13-432a-b602-5f869f9396ce` 均为 completed、模式为 `model_investigation`，来源为 `cninfo-1222994233` / `1222994233.PDF`，`replay=true`、`result_available=true`、`issues=[]`。高位请求 `limit=20&offset=999999` 返回总数 23、空项目列表。路由还通过 OpenAPI 暴露检查。此处是 A-04 当时的模块/API、全套后端检查及真实 HTTP 分页快照；其 history UI 浏览器矩阵后来通过，见下方“最终软件冻结与交接验收”。23 是该次快照，不是后续列表固定总数。

## 2026-10-02 A-05 样式与视口子项

主控审阅接受 CSS 样式子项并提交 `b865f23d8df3a168bc145201453bb2593c41a758`（仅 `app.css`）。修正后 1366×768 首页的“分析年报”按钮完整位于首屏，实测 top=619、bottom=662；1440×900 和 390×844 页面均无横向溢出，390px 长来源/状态/操作内容可读。核心表格文字为 16px、说明文字为 14.4px；聚焦区域/整页页图与 125%、175%、200% 缩放下的视口内滚动通过本轮样式核对。15 张截图和尺寸化证据见 `artifacts/evaluations/a05-style-20261002-02/a05-style-evidence.json`。`npm run build` 与差异检查通过。这里记录的是样式和视口子矩阵；A-03 证据交互、A-04 history UI 及 A-05 完整浏览器范围随后在最终矩阵中通过。早先样式证据 `a05-style-20261002-01` 仍保留为修正前记录。

## 2026-10-02 A-03 真实报告与证据交互矩阵

主控审阅接受的最终只读矩阵为 `artifacts/evaluations/a03-a05-frontend-20261002/frontend-readonly-qa.json`。记录中有 92 条通过记录、55 个不同检查名；续验有重复记录，不能把 92 说成 92 个独立自动化测试或一个前端自动化测试套件。最终失败数为 0；此前 9 次失败/部分恢复尝试及故意 503 fixture 均保留在证据中。该矩阵为只读浏览器检查，没有 POST、设置写入或模型调用；标准确定性任务与旧预检的两次真实 UI POST 另见下节。

矩阵通过真实 A-02 报告的三项核心金额与同比、四条叙述证据及原文来源、模型调用总数 3（调查 2 次、最终评审 1 次）、历史分页/回看、金额证据聚焦/整页/175% 缩放和第 154 页叙述限制文字与 PDF 预览响应头。Chromium 放大后 Tab 先经过原生滚动容器，再到 summary；按浏览器原生键盘行为，Enter 展开 summary、Tab 在抽屉内循环至关闭项、Escape 将操作返回来源入口均通过。报告路由的 URL 刷新、前进/后退、选择隔离、A→B→A、异步竞争和错误分页恢复均通过。此前 harness 对 Tab/Enter 的假设不符合原生行为，以及关闭 details 后的 `innerText` 读取假设，均以最终实际页面状态复验；这些失败尝试保留，不作为产品缺陷。

## 2026-10-02 A-05 实际 UI 创建与回看验收

实际 UI 本轮合计发出且仅发出两次任务 POST，未发起新模型请求。标准确定性分析 `POST /api/v1/annual-analysis-jobs` 返回 HTTP 202 / `queued`，job `annual-job-30d4e2fa50754e3b9abd2e749482b04d` 和 run `annual-analysis-a736d7e1-a932-4f88-9a0c-057f20d6c998` 后续均完成；按 `job_id` 的本机 URL 刷新恢复通过。主控独立 GET 确认公司 603288、报告年度 2024、来源 `cninfo-1222994233`、PDF SHA256 `5a97b13534438f5e85249752ef492fbd9e23af73e67845e47bad1ab7d92e20ee`，以及 `model_called=false`；归档 run 目录仅含 `analysis.json`、`manifest.json`、`parsed_text_pdf.json` 和 `source.pdf`，无模型审计内容。其数值模型调用数字段为 null；结合 deterministic 模式、无模型审计与 CDP 记录外部 provider origin 为 0，确认本轮 provider 请求数为 0。该任务入档后，history API 观察总数从 A-04 的 23 条快照变为 24 条；它们是不同时间的列表快照，不是固定总量。

旧预检通过实际 UI 仅创建一次，`POST` 返回 HTTP 201，run 为 `annual-precheck-603288-2024-20261002-133803`。主控独立 GET 确认归档完成、`model_called=false`、含 8 条事实；旧规则 `pdf_clip_amount_and_normalization` 的 8 条检查均通过（failed=0、abstained=0），且 `independently_verified=false`，不等同 v2 独立事实核验。最终浏览器 UI 的旧归档回看 GET 返回 200。

正式证据 `artifacts/evaluations/prototype-live-flow-20261002-f561f01a/evidence.json` 中 10 项 `final_checks` 全部为 passed，`unresolved_check_failures` 为空；`final_status` 将确定性分析标为 passed，将旧预检标为“passed with compatibility boundary”。其中四张正式截图依次展示新报告、job URL 刷新恢复、旧预检创建和旧归档回看，并包含最终页面标题、截图、输入框 run_id 与 GET 身份核验。较早的 harness 有两次说明文字等待超时（exit 1）及一次 `innerText` 中查找 run_id 的失败断言；这些过程记录保留，最终检查通过实际页面状态和归档身份核对完成，不表示每次 harness 都以 exit 0 结束。A-03 至 A-05 最终矩阵与这两次实际任务 POST 流程共同构成本轮浏览器验收范围。

## 2026-10-02 A-03–A-05 最终软件冻结与交接验收

软件原型由主控审阅并冻结于 `682471b71b2ee1b235abba317ab599caa42091f0`（`feat: complete annual analysis prototype workflow`）。该提交包含 8 个前端源码文件；A01 内容契约、A02 真实 AI 与原文预览、history API 和 CSS 分别沿用已接受提交 `921aa19d98161a54b7afcd426c567949f6173615`、`24ba5bf4fc7f2ca08b677c879041b949670bc248`、`0d473da67a6a8b77cdbad86dfb8aaee708311368`、`b865f23d8df3a168bc145201453bb2593c41a758`。前端最终 `functional_qa`、`npm run typecheck`、`npm run build` 与 `git diff --check` 均 exit 0；证据中的 8 个前端文件 SHA256 已由主控逐项比对一致。该版本冻结了可录软件原型与本机软件交接，不表示非金融用户 30 秒理解试验已完成，也不表示 A06、计划书 PDF、MP4 或上传已完成。

最终报告 URL `http://127.0.0.1:5173/?run_id=annual-analysis-8036d75c-6a13-432a-b602-5f869f9396ce` 回读后，1366×768 默认真实报告位置记录为 heading top 16.203125、核心表格 bottom 640.765625；同视口首页“分析年报”按钮 top 618.96875、bottom 662.15625，完整位于首屏。最终历史观察为 `total=24`、当前页 20 条，是完成确定性任务入档后的列表快照；A-04 HTTP 检查中的 23 条是更早快照。三张不同尺寸的报告截图是明确手动定位结果，不应称为默认自动位置；更早 history 顶端图也不是默认报告首屏。最终自动定位证据字段为 `automatic_report_position_after_real_run_url_readback`。

A-02 真实 AI 归档保持其原始 manifest：`git_head=921aa19d98161a54b7afcd426c567949f6173615`、`git_dirty=true`。本轮重算确认 manifest 指定的 29 个分析源码/提示词 SHA256 与对应文件一致；这只覆盖 29 个分析链文件，不包含后续 history API 或前端，故 A-02 run 不是由冻结提交 `682471b71b2ee1b235abba317ab599caa42091f0` 生成。原 manifest 未修改。

## 2026-10-02 A-06 M3 与 AI 同任务集成

在专用本机 Web/API 端口创建且只创建一个 `integrated_analysis` 任务：job `annual-job-afb9c788597f4e7f956d28fa9006a080` 返回 HTTP 202，run `annual-analysis-ae6587e1-b25b-4ac6-8eb2-1520803f663f` 最终为 `completed`。公司/期间为海天味业 603288 / 2024，来源文档 `cninfo-1222994233`，PDF SHA256 为 `5a97b13534438f5e85249752ef492fbd9e23af73e67845e47bad1ab7d92e20ee`。报告 GET 返回 200；manifest 与 report 绑定同一 run，报告同时包含 M3、AI 调查和最终评审，run 目录分别保留 `m3_screening.json` 与 `investigation.json`。

M3 四条规则均可计算、均未触发，总分 `0 / 100`。该值只表示当前未触发四条试行规则，不代表无风险或风险概率；阈值尚未校准，规则二语义映射仍限海天样例版式。AI 调查/最终评审共实际调用模型三次：两次调查和一次最终评审，三条调用审计均成功，没有传输/供应方错误。一次调查保留未核实解释，另一次因响应未满足结构与同源引用要求而弃权；最终评审保留原因未确认的限制。模型输出和评审没有改变基础事实核验状态，也未把 M3 分数当作证据。

Edge/CDP 检查覆盖四模式表单（桌面 1440×900 两列、390×844 一列）、同一报告的 M3 与 AI 评审区、history mode 行、直接 `run_id` URL、刷新回看及已核验原文页图入口。报告区截图在 390px 视口等待选中报告自动定位后拍摄，heading top 为 15.56px、scrollY 为 4740、文档宽度与视口均为 390px；无横向溢出、console/page error。第 81 页已核验证据页图 GET 返回 200。只读回看没有发起新的任务 POST。

聚焦后端检查命令覆盖 CLI 编排、任务 API（含真实 Pydantic 请求模式）、history 冲突判定和报告 manifest 读取，结果为 71 passed、2 skipped。跳过项为 `test_source_file_symlink_is_rejected`（当前环境不能创建源文件符号链接）与 `test_reparse_point_run_is_ignored_and_invalid_root_link_is_rejected`（当前 Windows 环境未允许创建目录符号链接）；详细输出见 `artifacts/evaluations/a06-integrated-20261002-098a06bb/pytest-focused-skip-reasons.txt`。`npm run typecheck` 与 `npm run build` 均 exit 0。该聚焦检查不是完整后端套件。

正式 JSON 证据与桌面/窄屏模式、报告、层、历史和页图截图位于 `artifacts/evaluations/a06-integrated-20261002-098a06bb/`，其中 `integrated-report-390.png` 是自动定位到集成报告后的 390px 报告截图，另存有历史列表截图 `history-list-390.png`；临时 CDP 脚本与过程日志在 `artifacts/tmp/a06-integrated-20261002/`。初次 UI 执行器在唯一任务被 HTTP 202 接收后因本地进度记录参数错误中止，随后通过 GET-only 轮询恢复并完成验收，没有重提任务。原 run manifest 记录 `git_head=f90e789175bc1cc37f9a3d830a9d750b51a3ef54`、`git_dirty=true`，其中 `scripts/analyze_annual.py` SHA256 为 `88724734fae64c8a6b185b887e30406b1795699dd96980203f0a604f76d34d50`；manifest 与代码快照见运行产物，未回写历史归档。以上 artifact 路径为当前开发机本地验收记录，不随源码分发包提供。

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

## 2026-10-02 Windows x64 便携包验收

便携包验收先对候选 `fintrace-windows-x64-20261002-candidate02` 完成运行测试；其后因补齐 README 配置差异、重建命令和 Microsoft payload 许可说明，以新唯一 ID 重建最终候选 `fintrace-windows-x64-20261002-candidate03`，没有覆盖旧产物。最终 ZIP 位于 `artifacts/packages/fintrace-windows-x64-20261002-candidate03/`，为 60,408,989 字节，SHA256 `c70a2dd45bdbfe0ec2c69fb1a113bf5bc39d0dd2cab8980849ecb9c5e730fc3a`。candidate03 使用与 candidate02 相同的运行时服务和 Python 依赖代码，新增/修订的是包内维护文档。candidate02 登记源提交 `a5b31583ef2a85f4024bd08028f4c4a9c301152d` 且构建时工作树有未提交变更；candidate03 也由该提交的未提交工作树构建。ZIP 包含 46 个 Python runtime distributions、选定海天 PDF 和历史归档、前端构建、经哈希校验的 Microsoft x64 VC payload DLL 集合、React/react-dom/scheduler 的原版 MIT 许可文件；不包含 `.env`、模型配置、Git、`node_modules` 或历史模型 request/response 审计目录。

candidate02 的初次运行测试使用中文和空格解压路径；最终 candidate03 又从另一中文空格路径解压并重跑下述隔离、启动和任务测试。使用最小 Windows 子进程环境启动包内 `runtime/python.exe`；服务 PATH 未包含 host Python/Node，也未传入 provider credential 环境变量。嵌入 Python 的 `isolated=1`、`ignore_environment=1`，`sys.path` 仅含 runtime 自带 ZIP/runtime、vendor、包内 backend/src 和 scripts；UTF-8 stdout 中文探针通过。真实运行的服务进程与 PyMuPDF 探针所加载的 MSVCP140、VCRUNTIME140、VCRUNTIME140_1 和 MuPDF DLL 都来自解压副本。浏览器自动打开本机历史 AI 报告，真实桌面 Chrome 页面显示海天报告与 4 条叙述项，证据抽屉成功打开并截图；历史 report、history、capability API、金额及叙述 focus PNG 返回 200，未配置模型能力为 `not_configured`，不存在资源、运行时路径和 API 错路由按预期返回 404。candidate02 通过本机 API 创建的 deterministic 子 CLI job `annual-job-18fa82d6aea24e75902a855a70989254` 完成，run `annual-analysis-9ba1a323-c0e8-4e73-8f5f-1402c50db4c4` GET 为 200 且 `model_called=false`；candidate03 复测也完成 deterministic job `annual-job-bf2d4e0188c243f0af3fdec4b31155e3`，run `annual-analysis-9daf5cd9-52b6-4ae0-b9ea-fe061c923f27` GET 为 200、`model_called=false`。同包重复启动只提示既有实例。

在验收专用进程占用 18765 时，candidate02 与 candidate03 均保留该 listener 并自动改用 18766；本机启动页面仍正常。最终 candidate03 ZIP 外部 SHA、大小和包内 2,026 项逐文件长度/SHA inventory 全匹配；凭据签名扫描（模型 token、Bearer、AWS key、私钥）均为 absent。最终 JSON 证据、API 日志与抽屉截图位于 `artifacts/evaluations/windows-portable-20261002-candidate04/`；candidate02 的第一次完整 API/浏览器验收在 `artifacts/evaluations/windows-portable-20261002-candidate02/`，占用默认端口后的复验在 `artifacts/evaluations/windows-portable-20261002-candidate03/`。首轮 candidate01 QA 脚本曾因把 HTTP header 名按大小写敏感方式比较而失败；修正验证器后完整复验通过，失败记录保留在 `artifacts/evaluations/windows-portable-20261002-candidate01/failure.json`。

本验收是在现有 Windows x64 主机上用隔离的包内 Python 和裁剪后的子进程环境模拟未安装 Python/Node，不是在干净虚拟机或另一台 Windows 10/11 设备上测试；不能据此声称已验证全新设备的系统 DLL 状态。无需/未执行全后端业务测试，本次改动集中在可复用包装器、构建/分发和文档。本候选以未提交工作树构建；正式提交后如需绑定提交号，可按[便携包说明](portable-package.md)使用新的唯一 package ID 重建，不覆盖本候选。

## 2026-10-03 C-01 多公司与 C-02 季度评测切片

C-01 评测 `multicompany-v2-20261002T150236Z-4585d783` 使用海天味业、贵州茅台和五粮液各一份 2024 年真实公开年报，共 24 条事实参考。海天 8/8、茅台 8/8 独立核验通过；五粮液 8/8 因币种证据不足而弃权，0 条冲突。15 条构造注入错误中 schema-only 接受 15 条，独立核验 `verified` 0 条、`conflict` 11 条、`insufficient_evidence` 4 条。4 条弃权不是正确拒绝。三家公司人工复核包 [`human-review-v2-20261002T150319Z-6190c680`](../artifacts/evaluations/human-review-v2-20261002T150319Z-6190c680/) 含 24 条待复核记录，字段仍为 pending；该评测不是盲测，且没有可用于舞弊准确率、误报率或漏报率的人工分母。协议细节见[多公司评测](multicompany-evaluation.md)。

C-02 独立季度 CLI 已以 `af13785d9ba9d635ea79e670c42beb54415d9bbd` 提交。该切片将来源观察、季度派生、比较可比性与同比/环比结果分层保存，不改年度 `FinancialFactV2`。聚焦测试为 26 passed（2.51 秒），不是完整后端套件。原创季度 Q2 模拟运行 [`quarterly-analysis-6e7807fc-f25c-4dbc-bf80-5323fc902296`](../artifacts/reports/quarterly-analysis-6e7807fc-f25c-4dbc-bf80-5323fc902296/review-evidence.md) 对 24 个来源观察、9 个派生季度及 6 个比较均核验通过；经营现金流环比基数为 −20,000 元，差额 +160,000 元，增长率弃权。海天真实公开材料运行 [`quarterly-analysis-8118d2c1-aa50-4c2f-a6c2-7775b0557253`](../artifacts/reports/quarterly-analysis-8118d2c1-aa50-4c2f-a6c2-7775b0557253/quarterly-analysis.md) 的 24 个来源观察与 9 个派生季度通过核验；三项同比 verified，三项环比因未追溯/可比性证据不足为 `insufficient_evidence` 并弃权。真实来源仅本地使用，哈希与官方来源见[数据规则](data-policy.md)；不把模拟 Q2 环比结果替代真实完整环比或真人复核。

上述 C01/C02 聚焦验证不覆盖完整后端套件、前端 typecheck/build 与年度导出完整浏览器矩阵；最终冻结检查见随后单独记录。本节的历史聚焦测试和样例运行不能代替那些关卡。

## 2026-10-03 C-03 候选源码包同机复制演练

源码包候选 `fintrace-source-candidate-20261003-7d91dad` 基于提交 `7d91dada2c9d16b661b95cecc629d569a2447b66`、tree `1174531310ddb4da4caa09e3bee55cdcd578513e` 构建，ZIP SHA256 为 `3a8e12c0a75ac300a9c3439d3ec7ead2b71b14d9b99e61c758005d72ed2a4f9f`，248 项清单；Windows x64 / CPython 3.14.3 runtime 锁 SHA256 为 `81851b1d4d0953cb74633846f27a2ea20d665fdeb9f1e2f487465dfe254c8b7b`。源码包构建器的 19 项聚焦单测通过。本机原始校验日志与 smoke JSON 保存在 `artifacts/evaluations/final-source-package-candidate-20261003-7d91dad/`；随提交件分发的脱敏候选 smoke 摘要见[源码包证据索引](../submission/final/evidence/source-copy-smoke-candidate.json)。

从 ZIP 在同一台 Windows 主机的中文与空格路径下解压后，以隔离子进程环境运行原创年度 CLI、季度 CLI 及本机 API：两个 CLI 和 deterministic Web job 均 `completed`；年度归档重读、来源证据路由与白名单导出返回成功状态，导出入口结构符合约定；复制运行含 18 条来源证据、16 条已核验 Claim 和 32 条 `verified` 核验记录，`model_called=false`。该烟测未安装依赖、未使用 `.env` 或模型配置，用户 proposal 未进入副本。

这仅验证上述提交快照在当前主机的源码包构建、解压路径、隔离运行闭环；没有在另一台电脑或干净 Windows 系统验证，也不覆盖后续文档/证据交付快照。最终交付 ZIP 需在文档与证据冻结后使用唯一新 package ID 重建。冻结版本完整后端套件、前端 typecheck/build、最终导出浏览器矩阵及正式报告逐页验收另行记录，不由本候选演练替代。

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

## 2026-10-03 C-03 软件冻结、导出验收与浏览器演练

主控接受软件快照 `a0b5b3bc26b3004afcd359df48544adb9cba4a13`。从 `backend/` 执行 `python -m pytest -q`，结果为 **467 passed、3 skipped，149.57 秒**；三个 skip 分别是 `test_reparse_point_run_is_ignored_and_invalid_root_link_is_rejected`、`test_symlinked_settings_file_is_rejected` 和 `test_source_file_symlink_is_rejected`，均由当前 Windows 无法创建相应目录/文件符号链接触发。测试文件条件见后端源码；这不是后端套件失败。前端 `npm run typecheck` 和 `npm run build` 均 exit 0（Node v24.14.1、npm 11.11.0、Vite 8.3.0）。详细过程日志保留在 `artifacts/tmp/final-delivery-20261003/full-checks-a0b5b3b/`；分发摘要及精确 skip 原因见[正式软件检查摘要](../submission/final/evidence/software-freeze-checks.json)。

年度报告导出提交 `90e3a1faf0c80c68bb78e4795b37b7e17c2f95bf` 已由主控接受；调用计数与 HTML 状态修复在冻结提交 `a0b5b3bc26b3004afcd359df48544adb9cba4a13`。最终专项为 55 passed。确定性原创样例 CLI run `annual-analysis-4b7a43fb-e9ad-47b1-9319-5b8b7533211e` 和 Web job/run `annual-job-4303a6f6da3d4f57bc930c6421d3a809` / `annual-analysis-caffff8e-1937-4544-ad2f-b3be7f406c47` 均为 completed、`model_called=false`，各有 8 条已确认事实与 8 项独立核验计算。新导出接口 JSON/Markdown/HTML 均 HTTP 200；非法格式为 422。M3、model investigation 与 integrated historical archives 的三种只读导出均 HTTP 200，来源绑定状态受限规则通过，原归档前后哈希不变。历史 AI 调用展示为调查 2、最终评审 1、总计 3（总数包含最终评审）。详细本机评审文件为 `artifacts/evaluations/report-export-c03-20261003/evidence.json`，SHA256 `dd6922057f20f2e18345900bc890a86a62d599845fe3dd7b03f5c04411bb38a4`；该文件含开发机本地路径，不随源码 ZIP 提供，脱敏摘要见[年度导出验收摘要](../submission/final/evidence/annual-report-export-c03-summary.json)。API 白名单 JSON 为 175,190 字节、SHA256 `2f5801a2e7cbc57946767fab5ad6012d027f58057af16949b5f96779793a3a31`，字节副本位于[年度 API 导出](../submission/final/evidence/annual-sample-api-export-run-caffff8e.json)。

浏览器离线 HTML 打印检查覆盖 12 页 PDF，PDF SHA256 `9463c90aacca32981fbe4b2a5bb0d1ea70303ae74b191c3f3fd795e308d9c26b`、输入 HTML SHA256 `96aee3a2b47c809eb096951afa663235e71ad30bd35eb56f6d66e781978a9559`；8 条事实与 8 项计算在 HTML 中显示为 verified，记录外网请求为 0。主控逐页审阅了主要数据表所在第 3–7 页。

随后通过本地 UI 的“上传年报”实际上传同一原创 PDF，填写 `SIM-ANNUAL-001` / 2025 并选择 deterministic，创建新 job `annual-job-12b2970426b64de7a6b48d5caededfa3`、run `annual-analysis-a96e4ee6-2341-46c6-ac18-7729849a16c0`、upload/document `upload-c00acd385bd348fc8caf635deb4f2d6e`；状态 completed，模型未调用。输入 SHA256 `f193e7a497fe483d3ab70b7bde2880f715ff1127b1d8b14cc3f4fda678ba0bf7`。API 白名单导出包含 8 条 confirmed facts、8 项 confirmed calculations、16 条 verified claims 和 2 条候选待核查项。页面实际下载 JSON、Markdown、HTML 的 SHA256 依次为 `a9e0fd8be501bf655f6d2ea579972faaed5c7ed480846f0199387e29afc2a971`、`9a28f5a941d7348fc90f9926b5cccf7441ba2a7c3d3f9c3b43ef630cce8f4a25` 和 `425f60cbd80e2515e9aa0a2e703bfa351ff8559b4da6d0f5529dbc99910f08d6`。实际点击营业收入第 2 页证据后页图 GET HTTP 200，界面展示合并利润表、原值、人民币元、2025 期间及合并口径；刷新 `run_id` URL 后恢复同一归档。Edge 页面仅访问本机，非本机请求为 0、模型请求为 0。1366×900、1440×900、768×1024、390×844 的文档/正文宽度均与视口一致。桌面和 390×844 固定视口截图、run 摘要及 JSON 文件见[UI 演练证据](../submission/final/evidence/README.md#年度原创样例)。

这次 UI 的 API runtime 使用候选源码包 `fintrace-source-candidate-20261003-7d91dad` 的隔离副本，并把导出模块同步为冻结提交的精确文件；运行清单记录 HEAD 为 `a0b5b3bc26b3004afcd359df48544adb9cba4a13`、`git_dirty=true`。主控逐项比对了 31 个年报后端指纹与冻结 Git tree，并确认浏览器前端 6 个受测源码/锁文件匹配冻结提交，详细脱敏列表见[指纹文件](../submission/final/evidence/annual-sample-ui-code-fingerprint.json)。因此这是 a0 软件源码指纹下的同机 UI 演练，不是最终文档包的 ZIP 解压验收，也不是另一台电脑/干净安装通过。

为避免 `core.autocrlf=true` 规范化实际带哈希的 JSON 副本，新增 `submission/final/.gitattributes` 的 `evidence/*.json -text` 属性。季度原创 analysis JSON 的工作文件 SHA256 仍为 `25fc2fe1e5941ea44a3bbc475742b736e8c2f0653d3f3f549d6ea396325e8190`；主控在暂存与最终包生成时仍应分别按 Git blob 原字节和包内字节复核该哈希，不得改写历史运行归档。

最终源码 ZIP 从正式文档交付 commit/tree 构建；确切 package ID、source commit/tree、ZIP SHA256 和包校验结果以包外 manifest、`.sha256` sidecar 与独立验收记录为准。另一台 Windows 电脑干净安装、组委会网络/API 规则、C-01/C-02 真人复核以及非金融用户 30 秒理解试验仍 pending。初赛计划书、MP4 和平台上传仍由各自材料负责人负责。

## 2026-10-04 冻结后年报原文阅读修正

本次只调整叙述片段的页面阅读和定位说明，不重新分析年报、不调用模型，也不改变财务核验、模型输入、提示词、检索选择或排序、原归档及导出白名单。使用既有 run `annual-analysis-8036d75c-6a13-432a-b602-5f869f9396ce` 的片段 `nctx-5a97b1353443-p9-aaf639bcc7e460cd4dd4`。真实 PDF 第 9 页的阅读上下文由已校验来源 PDF 的文本块 27–30 组成，恢复自然段首、段尾及跨行断词；块 31 的下一段不并入。API 中 `raw_text` 和原 `bbox` 仍逐字、逐坐标匹配块 30。页面可展开查看原始引用、来源文本块与坐标。

独立端口 `8001` 的真实 HTTP 读取返回 `purpose=display_only`、`expanded=true`、`limited=false`；同一片段的聚焦页图和整页图均为 HTTP 200、PDF 第 9 页。PDF 原件与归档副本 SHA256 均为 `5a97b13534438f5e85249752ef492fbd9e23af73e67845e47bad1ab7d92e20ee`；本次前后核对的运行 `manifest.json` 与报告 `report.json` 哈希也记在本轮[来源完整性记录](../artifacts/evaluations/narrative-readability-20261004-8036d75c/source-integrity.json)，未改写 PDF、归档或历史最终 ZIP/PDF。

本轮聚焦后端回归命令 `D:\Programming\Python\python.exe -m pytest -c backend/pyproject.toml tests/unit/test_narrative_reading_context.py tests/unit/test_annual_report_read_api.py tests/integration/test_annual_report_read_real.py tests/unit/test_annual_report_export.py -q` 为 **60 passed**，覆盖段落方向/边界、跨列与表格排除、原始字符限额、伪造归档 context 忽略、真实来源匹配、旧引用 0.05 point 舍入兼容及 context 不进入年度导出白名单。前端 `npm run typecheck` 与 `npm run build` 均 exit 0。原始日志和命令见[本轮证据索引](../artifacts/evaluations/narrative-readability-20261004-8036d75c/evidence.json)。

只读浏览器检查使用 Microsoft Edge 154.0.4258.53，通过 Playwright 和本机项目页面 `5174`，数据由本机 API `8001` 提供。1366×900 与 390×844 两种视口均准确显示完整段落与易懂的“为什么显示这段”；键盘 Enter 可展开原始引用和逐块来源详情；聚焦/整页 PDF 图可切换，Escape 将焦点返回来源入口；打开详情及页图时文档宽度均无横向溢出。旧 API 缺少 `reading_context`、引用 bbox 增加 0.05 point 两项是对真实本机 HTTP 响应副本的前端拦截变换，明确属于合成兼容检查，不是服务端归档写入。浏览器仅发本机 GET，无 POST、非本机请求或页面脚本错误。截图保存在任务 `artifacts/tmp/narrative-readability-20261004-8036d75c/`；真实年报页图只保留在该本机临时目录，不进入 Git。

冻结后的修改尚未加载到原默认 API 服务：操作前确认 8000 属于本项目 Uvicorn（PID 18168）、5173 属于项目 Vite（PID 26324），16 个 `job.json` 中无 `queued`/`running` 且无解析错误。尝试为 8000 执行精确重载时，工具在启动 PowerShell 前以 `blocked by policy` 拒绝了命令；命令与工具返回说明留在任务 tmp。没有重试停止进程、没有改变默认端口服务；本段的通过证据仅适用于 8001/5174。默认 API 尚未应用本修正，原 5173 Vite 未停止。

## 2026-10-04 M3 结构化问题项前端读取修复

真实集成归档 `annual-job-68b7f11003be480f8a2d910df3b19ac4` / `annual-analysis-674537c3-3811-40fd-bb60-212ed5fa6e45` 的 API 返回 HTTP 200，但前端把 M3 `issues` 当字符串数组，导致整份报告拒绝显示。前端现在保留 `{code, message}`，兼容旧字符串项并将其归一为 `code: null`；结构化项字段缺失、为 null 或类型不符仍拒绝整份响应。展开规则详情后显示原始原因和 code，M3 弃权与 null 总分保持原样。接口契约见[架构](architecture.md#4-财务与证据契约)的 M3 规则说明。

使用 Microsoft Edge 154.0.4258.53，经默认 Vite 5173 和既有 API 8000 回读真实归档。桌面与 390×844 页面均显示 8 条已确认且独立核验通过的数据、四条 M3 弃权规则与 null 总分；报告中的 **14 条原始 issue code 与完整 message 均在两个视口的展开详情中逐条保留**，包括 `missing_fact`、`comparability_unverified`、`semantic_mapping` 和 `comparability_unbound`。模型评审状态为 completed，页面将其与本地独立核验分开展示。两种视口无横向溢出，页面无 `[object Object]`、console/page error；请求均为本机 GET，没有外网请求。另回读海天既有 M3 运行 `annual-analysis-5e7079fe-9ac9-4e96-85fa-1898f9177d21` 和 integrated 运行 `annual-analysis-ae6587e1-b25b-4ac6-8eb2-1520803f663f`，两者四条空 `issues` 均通过 API 与页面检查。

拦截响应兼容/拒绝矩阵共 16 个 parser case 和 5 个 UI fixture：legacy string、unknown 合法 code、多条 issue、empty array 接受；null/数组/数字项、code/message 缺失/null/错类型及 source SHA mismatch 均 fail-closed，响应路径见证据 JSON。前端 `npm run typecheck`、`npm run build` 均 exit 0。未重启 API、未创建新分析任务、未调用模型供应方。

用户 PDF、上传来源 PDF、运行归档 PDF、source.json、job.json、report.json、M3 JSON 与 manifest 前后哈希一致；PDF 封面核对为贵州茅台 600519 的 2025 年年度报告。详见[本次证据索引](../artifacts/evaluations/m3-issue-contract-20261004-maotai2025/evidence.json)。修复前/后桌面、移动、M3 详情与 PDF 页截图，以及原始日志和完整浏览器 JSON 保存在 `artifacts/tmp/m3-issue-contract-20261004-maotai2025/`。

## 2026-10-05 年报开放式发现软件阶段验收与决赛文档装配

当前软件基线为前端提交 `07cb4e97e7b43327aa35ed7d6db260ec28fb1c59`；后端接入提交为 `eacd4301c5d8f76c2ba144530fa9ed98b87c9a52`。本记录汇总已分别接受的后端与前端阶段证据，不表示真实供应方质量、跨公司语义泛化或整项决赛交付验收完成；10/03 冻结 `a0b5b3bc26b3004afcd359df48544adb9cba4a13` 的原计数、样例运行和历史包/PDF 记录均保持历史身份，没有被改写成新版证据。

后端完整套件命令 `python -m pytest tests -q` 的最终日志为 [`pytest-full-backend-final.log`](../artifacts/evaluations/annual-discovery-integration-20261004/pytest-full-backend-final.log)，结果 **535 passed, 3 skipped in 135.91s**。首次整套检查的 [`pytest-full-backend.log`](../artifacts/evaluations/annual-discovery-integration-20261004/pytest-full-backend.log) 保留 **1 failed, 530 passed, 3 skipped in 138.64s**；唯一失败是既有 job mock 未 patch 新 discovery run root，随后按正确 root 修复并明确返回 discovery 响应，未降低完成状态断言。专项合成 PDF E2E 以两个独立文本 PDF 参数化运行年度 CLI→本地 mock connector→报告 GET/发现索引→真实合成 PDF 页图→JSON、Markdown、HTML 导出。两份 PDF 的 `confirmed.metrics` 均为空，仍分别产生与来源绑定的 mock 动态问题和引用；它们不是上市公司分析或真实模型输出。直接替身/engine 测试有底层联网拒绝哨兵；早先 job E2E 的本机 HTTP mock 实际通过 loopback；因此不能概括整套都由全局网络阻断。所有此阶段测试均未读取真实模型配置或凭据，也未访问真实 provider。原引擎 `validation.json` 保留未覆盖；其中 `.invalid` dummy connector 至少出现过一次请求尝试，确切次数未确认，旧记录中的“一次”不作为精确计数依据。

前端阶段提交 `07cb4e97e7b43327aa35ed7d6db260ec28fb1c59` 的 `npm run typecheck` 与 `npm run build` 分别通过，最终兼容修正日志为 [`typecheck-legacy-compat.log`](../artifacts/evaluations/annual-discovery-frontend-20261005/typecheck-legacy-compat.log) 和 [`build-legacy-compat.log`](../artifacts/evaluations/annual-discovery-frontend-20261005/build-legacy-compat.log)。原 17 场景浏览器组与另一次旧终态 job 兼容场景分别留档，**不是一次 18 场景运行**：[`browser-qa.json`](../artifacts/evaluations/annual-discovery-frontend-20261005/browser-qa.json) 汇总 17 场景、510 个浏览器请求、102 个 API 请求；[`legacy-terminal-job-qa.json`](../artifacts/evaluations/annual-discovery-frontend-20261005/legacy-terminal-job-qa.json) 汇总 1 场景、31 个浏览器请求、7 个 API 请求。两份记录均为外部请求、POST、console error 和 page error 为 0。浏览器页面由本机 Vite 与人工构造 API 响应提供；这些计数仅适用于各自记录。预览使用 1×1 mock PNG，下载文件体也是 mock，不证明生产页图视觉或导出内容；实际导出内容由前述后端 E2E 检查。

本轮原件保护 SHA 由主控只读检查提供：茅台 2025 年原 PDF `474905deeaf0f875fc0a1b097a626c0c7852c427faadc5d7fc7816cbf45ea288`；既有 run manifest `93a254d13848a0af50b7d0dab28f68eb7d843f22ec8a42763630ef0fcd5d58e9`、report `8a3683ce28aee1126429fa801602599db64ac02212f3cb19f7106670f32076f6`、job `d1579ded388596f6fd1ab04dfafd6a5e9c433a216b1628e5ece05680e83aa1d5`；原创模拟 PDF `f193e7a497fe483d3ab70b7bde2880f715ff1127b1d8b14cc3f4fda678ba0bf7`。这是主控核对的历史保护记录，不冒称为本轮执行端重算。用户提供的茅台材料仅本地复现，不纳入公开提交件；官方披露时间和再分发许可未核实。

决赛技术报告 Markdown/PDF、架构图与本节软件证据汇总以软件基线 `07cb4e97e7b43327aa35ed7d6db260ec28fb1c59` 装配。最终 PDF SHA256 为 `0971e763370e55cf4253a5e10c6f0b18130aed66affbf28398376090a3dcd302`，共 9 页；修正 Markdown 硬换行后重建，9 张 110 dpi 页面 PNG 与此前 PDF SHA256 `4e0d8ba8459e3ae3ca7b6852a76e75495d819f5a12b242770f43d51fd7e7318b` 的渲染逐字节一致，主控已据当前 PDF/页图复核接受。页面尺寸、路径及 SHA 见[PDF 渲染清单](../artifacts/evaluations/annual-discovery-final-20261005/pdf-render-manifest.json)，构建和重渲染日志在同目录。架构 SVG 最终 SHA256 为 `10a6f4a9a505cee22c6cdf9ac5260e2bb5ed538ac84f4d68750733be5253a775`；Edge 1320×650 最终截图与日志留在 `artifacts/tmp/annual-discovery-final-20261005/`，主控查看后接受最终评审两行间距及内部小箭头。SVG 排版微调没有改变已接受 PDF 的数据流拓扑。代码原字节与 LF 规范化指纹见随附[代码指纹](../submission/final/evidence/annual-discovery-code-fingerprint.json)，测试汇总与限制见[决赛软件检查摘要](../submission/final/evidence/annual-discovery-software-checks.json)。文档检查命令 `git diff --check HEAD` exit 0。真实 provider 长任务耗时与发现质量、跨公司语义人审、OCR/图像文字仍未验收/当前不支持；另一台干净 Windows 电脑、现场网络/API、C-01/C-02 真人复核及非金融用户理解试验仍是独立待完成关卡。源码 ZIP 需在报告/证据入库后的交付 commit/tree 上另行构建并检查。

## 2026-10-05 年报开放发现 v2 逐条验收与历史响应离线重放

现行实现使用版本化的 `annual_discovery_v2` 解析和归档契约：逐条校验无效 finding 并保留同批其他合格项，允许同一来源片段绑定多个不同的精确引用；唯一的 Unicode 空白排版差异只在同片段内唯一定位时恢复，并计入 `restored_quotes`。v2 的批次状态分为完整成功、部分接受、失败和未尝试，部分批次不计入完整处理字符、coverage.complete 或失败页；有效部分结果仍可读。引用来源、数字限制和未核验状态保持严格；旧 v1 归档仍按 v1 规则读取。提示词目标是每批最多三条，解析器的六条上限仅用于兼容既有回应。契约详见[年报开放发现](annual-discovery.md)与[架构](architecture.md)。

真实运行归档 `annual-analysis-de3b94f9-2242-4775-8240-8f84ffe56cab` 的 23 批历史 request/response/failure 仅在本地离线重放，没有新模型调用、异常原文输出或归档回写。v2 实测为 **15 完整成功、5 部分接受、3 调用失败**；接受 104 条 finding、逐条拒绝 5 条、空白恢复 9 条 quote，三次原调用失败按安全类别保留为 `model_call_timeout`。这是对既有模型回应的解析兼容性重放，不能证明新模型质量提升或超时已解决。机器摘要见[`v2-offline-replay.json`](../artifacts/evaluations/discovery-real-output-20261005/v2-offline-replay.json)。

构造 PDF 的端到端测试包含一个完整成功 case 和一个 `0` 整批成功、`1` 批部分接受、仍保留有效 finding 的 case；运行年度 CLI 后通过报告 GET、发现索引、合成 PDF 页图预览以及 JSON、Markdown、HTML 三种导出检查同一 v2 结果。该链路使用本地 mock connector 和构造 PDF，不访问真实 provider。前端最新隔离浏览器回放[`browser-qa-trace.json`](../artifacts/evaluations/discovery-usability-ui-20261005/browser-qa-trace.json)记录 **57/57 检查通过**，包含桌面 1366×768 和手机 390×844；只读历史归档、所有 API 请求均由本机 mock 拦截，不发起真实后端或 provider 调用。页面截图保存在同一私有评估目录，不复制到提交材料。UI 端 `npm run typecheck` 与 `npm run build` 均通过。

后端完整套件最终复跑命令 `python -m pytest tests -q` 为 **553 passed、3 skipped，195.24s**；执行时清除了子进程的 `MODEL_*` 环境项。首轮结果为 1 failed、552 passed、3 skipped：既有 coverage 单测夹具遗漏显式 `successful_batches`。修正只补齐其 all-success fixture 的 `successful_batches=2`、`partial_batches=0`，没有放宽生产完整性判断；定向用例 1 passed，随后完整套件通过。首轮完整 stdout 未保留；[`pytest-full-backend-initial-failure-summary.txt`](../artifacts/evaluations/discovery-real-output-20261005/pytest-full-backend-initial-failure-summary.txt)是根据捕获的 pytest 完成输出重建的摘要，并明确标记这一限制；最终复跑原始日志见[`pytest-full-backend.log`](../artifacts/evaluations/discovery-real-output-20261005/pytest-full-backend.log)。

真实运行目录和年度报告的 58 个文件经修改前/后逐项 bytes 与 SHA256 比对，路径和值均一致；原 PDF SHA256 为 `474905deeaf0f875fc0a1b097a626c0c7852c427faadc5d7fc7816cbf45ea288`，既有 `report.json` SHA256 为 `6789aaf29469a7a4f1447ab78e1e83b689415c7df373690eef7e954db2fe32d4`。只读对照摘要见[`archive-snapshot-comparison.json`](../artifacts/evaluations/discovery-real-output-20261005/archive-snapshot-comparison.json)。

## 2026-10-05 年报发现有界并发与本地耗时验证

针对历史运行中 23 个 `annual_discovery_v1` 调用串行耗时约 1430.84 秒（单次中位数 60.22 秒，范围 6.66–92.76 秒；其中 3 次记录为约 60.2 秒超时）的诊断，年报发现现在按原文顺序规划并使用最多 3 个在途请求的有界滚动调度。批次文本范围、规划数量、严格 v2 解析规则与三十分钟预算上限不变；不自动重试。预算到期后停止准入新请求，已准入/在途批次结果仍保留；不把连接器 timeout 描述为 socket 硬墙钟保证。历史时间只来自既有审计记录的时间与安全计数，不读取或公开请求、响应正文，也不能作为当前 v2 在线速度结果。并发设置可由 `FINTRACE_DISCOVERY_CONCURRENCY=1|2|3` 调节，实际调度信息只写入运行目录执行 metadata，不扩展公开发现契约。

同一 23 批固定输入的本地 mock 对比中，每批固定延迟 25 ms：串行 wall time 为 **0.6566 秒**、峰值并发 1；默认并发 3 为 **0.2375 秒**、峰值并发 3，mock 条件下约快 **2.77 倍**。两次运行各规划/尝试/响应 23 批，逐批请求文本哈希、findings 哈希、coverage 哈希及最终批次顺序一致；并发运行的结束进度按完成顺序报告。连接器被本地 mock 替换，provider request 与 network call 均为 0。这只是调度开销对固定延迟 mock 的离线比较，不是供应方耗时或费用预测。完整指标见[`mock-23-batch-benchmark.json`](../artifacts/evaluations/annual-scan-latency-20261005/mock-23-batch-benchmark.json)。

对用户提供的本地 2025 年年报 PDF 单独进行离线解析测量：143 页、143 个有文字页、3286 个文字块、151314 个字符，规划 23 批。仅 `parse_text_pdf` 三次中位数为 **0.1991 秒**；context 规划三次中位数为 **0.6172 秒**，这项时间本身已包含它自己的 PDF 解析，不能与前一项相加。两项均未调用 provider 或网络，原 PDF SHA256 为 `474905deeaf0f875fc0a1b097a626c0c7852c427faadc5d7fc7816cbf45ea288`。原始测量见[`pdf-parse-context-timings.json`](../artifacts/evaluations/annual-scan-latency-20261005/pdf-parse-context-timings.json)。

并发专项、审计归属、预算边界和 v2 异常恢复/API 严格 reader 目标测试的最终轮为 **127 passed in 16.48s**。执行端最初一轮向主控报告 **77 passed, 2 failed（共 79 项）**，失败涉及共享 mock 的 `audit_id` 兼容；该轮 stdout 未保留，因此不重建或冒称存在原始日志。首个保留 stdout 的轮次为 **2 failed, 89 passed**，随后一轮 **2 failed, 93 passed**，第三轮 **95 passed**，reader 修复轮 **127 passed in 15.75s**，最终目标轮 **127 passed in 16.48s**；每轮日志使用独立文件名，无覆盖。最终目标与已留存各轮 stdout/可用 exit code 位于[`annual-scan-latency-20261005`](../artifacts/evaluations/annual-scan-latency-20261005/)。随后只运行一轮完整后端套件，命令 `D:\\Programming\\Python\\python.exe -m pytest -c backend/pyproject.toml tests -q` 的结果为 **576 passed, 3 skipped in 202.93s**，stdout 与 exit code 分别见[`backend-full-suite.txt`](../artifacts/evaluations/annual-scan-latency-20261005/backend-full-suite.txt)和[`backend-full-suite.exitcode.txt`](../artifacts/evaluations/annual-scan-latency-20261005/backend-full-suite.exitcode.txt)。

修改前与修改后的保护记录各自对照旧 snapshot 中的 58 个运行/报告文件，路径、字节数和 SHA256 均匹配；两次均为 58/58、缺失或变化 0。用户 PDF 前后 SHA256 均为 `474905deeaf0f875fc0a1b097a626c0c7852c427faadc5d7fc7816cbf45ea288`。详细清单见[`source-integrity-before.json`](../artifacts/evaluations/annual-scan-latency-20261005/source-integrity-before.json)、[`source-integrity-after.json`](../artifacts/evaluations/annual-scan-latency-20261005/source-integrity-after.json)和[`source-integrity-comparison.json`](../artifacts/evaluations/annual-scan-latency-20261005/source-integrity-comparison.json)。本轮未执行真实模型调用；并发可能在固定预算内完成更多批次，因此不据本地 mock 结果保证实际供应方总费用不变。

## 2026-10-05 决赛报告、并发摘要与文档交付快照

决赛报告、运行指南、证据索引、产品路线和 README 已对齐已接受软件基线 `c0c603bb896cd151d48cbdf74adbb7db9c15823b` / tree `f292583ed67a9eb7d72978ee3f64e752a9fcd773`。新增公开[并发软件检查摘要](../submission/final/evidence/annual-scan-latency-software-checks.json)记录 8 个相关运行文件的工作区 raw SHA256、c0 Git payload SHA256 和 Git blob OID，并将旧 v2 证据单列为历史快照；根核对确认 8 项工作区 bytes 与冻结 payload 一致，旧 v2 JSON 与 HEAD 字节一致。摘要只含软件验证统计、本地固定延迟 mock 与离线解析计时、源代码指纹和本报告 PDF 身份，不包含本地用户 PDF 文件标识/原文、模型请求/响应或审计正文、凭据或私有 UI 图片哈希。软件测试计数及既有原始日志沿用上一节，本次材料整理未重跑业务套件或前端矩阵。

报告由现有 `scripts/build_final_report.py` 从 Markdown 重建，最终 PDF 共 9 页，SHA256 为 `02c8e4cb75f241a84d3dbcea5d14ee3dbabdc53b1540a1ffbc78fbf560a60844`。逐页 110 dpi 渲染清单为[`pdf-render-manifest.json`](../artifacts/evaluations/latency-delivery-final-20261005/pdf-render-manifest.json)，包含 9 页图像尺寸和 PNG SHA256；主控复核最终 9 页无溢出，且摘要与结论的并发语义及隐私表述已修正。为消除 Markdown 版本行尾空格而重建 PDF 后，最终 9 页 PNG SHA256 与已视觉接受的前一版完全一致。渲染目录中较早试排遗留的 `page-010.png` 不在最终 manifest 中，也不属于本次 9 页输出。最终 PDF 构建与渲染 stdout/exit code 分别保存在 `artifacts/evaluations/latency-delivery-final-20261005/report-build-final.stdout.txt`、`report-build-final.exitcode.txt`、`pdf-render-final.stdout.txt` 和 `pdf-render-final.exitcode.txt`；最终摘要、材料核对和暂存检查分别见 `summary-build-lf.*`、`materials-verification-final.*`、`git-staged-check.*`，均在同一 evaluation 目录。

生成 exit-code 文本时，一条 PowerShell `Set-Content -NoNewline <目标路径> 0` 命令使用位置参数，意外在仓库根目录写出名为 `0` 的 87 字节文件，文件内容只是原拟写入的相对目标路径。已将该文件原样移动到 `artifacts/tmp/latency-delivery-final-20261005/powershell-positional-binding-artifact-0.txt` 留存，没有删除或暂存；之后命令改用显式 `-Path` 与 `-Value` 参数。此日志记录问题没有触及源码或用户材料，也未触发额外业务测试。

源码 ZIP 将从包含本报告、运行指南和正式证据的文档交付 commit/tree 独立构建；准确 package ID、source commit/tree、ZIP SHA256、manifest 和同机中文空格路径副本演练结果应以包外清单与独立验收记录为准。该同机检查不替代另一台 Windows 电脑干净安装、现场网络/API 或真人财务复核。

<a id="annual-analysis-recovery-r1-intermediate"></a>

## 2026-10-06 恢复 goal 开工与 R1 中间状态审阅

本次用户实际启动恢复 goal，并授权两份指定年报有限在线验收，累计真实 provider 请求尝试上限为 80 次。主控读取 AGENTS、README、恢复任务书及相关契约，核对工作区 HEAD 仍为 `b3c418ce4566b2161a9d86571c353f891e978d56`。开始时已有的产品路线/本验收文档修改、未跟踪任务书、计划书 Word 删除及另名文件、用户茅台 PDF 均保留；原始工作区差异和文档快照留在 `artifacts/tmp/annual-recovery-controller-20261006/`，不作为正式功能依赖。

R1 通过当前会话子代理机制显式配置 `model=gpt-6-luna`、`reasoning_effort=max`、`fork_turns=none` 派发。执行子代理首次回合因工具报告会话用量限制而终止；已要求同一子代理恢复本阶段，未换型号、未转派、未由主控代写业务代码。当前落盘内容仅包括新增 `core/annual_runtime_identity.py` 及 API、任务服务、年度 CLI 的部分身份握手/只读报告可用性改动。

主控实际审阅上述差异，尚不接受 R1：执行端回报正常 CLI 探测的 `loaded_source_match=false`，动态生成的 dataclass 方法被错误纳入源码比较；`_invoke_cli` 无期望身份时会传入空 fingerprint，且临时项目测试夹具的 CLI 身份探测兼容还未验证；只读投影的错误类别还需对齐读取器的实际身份错误码。启动器、历史索引、前端和对应测试尚未完成。修正清单已交回同一 R1 执行端，不能将这些中间改动当作已恢复版本或建议启动在线分析。

主控另做只读文件核对：三份既有归档的 manifest、report、source PDF 共 9 个 SHA256 均与前节审阅证据一致；旧任务仍分别为 failed、failed、completed_with_issues。结构化[中间审阅记录](../artifacts/evaluations/annual-recovery-controller-20261006/r1-intermediate-review.json)保留当前哈希、任务状态和检查范围。这只是文件与代码审阅，不是新的运行、归档 API 或浏览器验收。

截至本段记录，真实 provider 请求为 **0/80**，剩余 **80**；没有读取或显示模型密钥，没有新在线结果。首次处理预算暂分配两份各 30 次，另预留 20 次续跑/补证，须在 R3 用实际离线计划核对后才能联网。R1 自动化验收、启动演练、浏览器矩阵、R2–R6 均尚未通过；没有阶段 commit，没有 push。历史测试通过与执行端的一次失败探测不计作本轮修复通过。

<a id="annual-analysis-recovery-r1"></a>

## 2026-10-06 R1 版本一致性与历史报告恢复验收

R1 由本会话明确配置的 `gpt-6-luna`、`max` 执行子代理实施代码及运行验证，主控审阅实际差异、原始证据和页面，并维护文档。此前中间状态中的身份误判、空指纹和测试夹具问题已修正；中间记录保留其当时状态。本阶段只接受版本握手及历史报告恢复，未接受 R2–R6 或新的在线年报分析。

API/CLI 启动时捕获加载代码身份，并分别核对磁盘源码；新任务及 Web CLI 在开始分析前拒绝版本不一致。启动器仅对确认属于本项目且无活动任务的陈旧 API 执行重启；未知进程或无法确认的状态拒绝处理。failed/interrupted job 通过严格归档读取器提供只读可用性投影，原状态、错误和归档均不回写。历史列表、刷新后的 job 和按 run 查看均有保存报告入口，页面将旧错误明确标为原任务记录。

正式证据位于 [`annual-recovery-r1-20261006`](../artifacts/evaluations/annual-recovery-r1-20261006/)。实际检查及范围如下：

- 针对性后端套件的 [JUnit](../artifacts/evaluations/annual-recovery-r1-20261006/r1-targeted-pytest.xml) 为 **85 passed、2 skipped**，suite wall time 44.794 秒；跳过项分别为历史索引 reparse-point 和来源文件 symlink 检查，原因是本机未启用测试所需的符号链接创建权限。新增的失败 job 源 PDF 篡改拒绝测试另跑 **1 passed**，suite time 1.767 秒，见 [独立 JUnit](../artifacts/evaluations/annual-recovery-r1-20261006/source-tamper-projection.xml)。这两轮不是完整后端套件。
- [同版本 CLI](../artifacts/evaluations/annual-recovery-r1-20261006/same-version-cli.json) 实际执行身份探针和原创单页 PDF 的确定性分析，退出码为 0、模型调用为零。它证明协议和 CLI 可用，不作为真实财报结果或性能验收。
- [启动器未知进程演练](../artifacts/evaluations/annual-recovery-r1-20261006/startup-handshake-unknown-process.json) 使用本次自建的隔离 API；实际启动脚本退出码 1 并拒绝停止无法确认为项目入口的进程，原 PID 保留且历史接口继续响应。[活动任务保护](../artifacts/evaluations/annual-recovery-r1-20261006/startup-active-job-protection.json) 提取真实启动函数 AST，以模拟 Win32 进程和停止操作哨兵验证：JSON 解码活动计数 0 允许安全检查，1 拒绝重启，Stop-Process 调用为 0。后者是故障模拟，不写成实际杀停或重启演练。
- [最终浏览器回放](../artifacts/evaluations/annual-recovery-r1-20261006/browser-history-recovery-final.json) 通过已有 Python Playwright 与系统 Chrome，使用三份历史归档的逐字节隔离副本、当前后端和本地前端。历史入口、失败 job 路由刷新和保存报告操作可达；海天集成报告显示 **102 条待核查线索**及既有评审，另两份报告 HTTP 200，分别保留发现层 failed/0 条、partial/28 条及原评审状态。模拟旧 runtime 端点 404 后，前端报 `runtime_version_mismatch`，年度任务 POST 为 0。最终无 React 重复 key、无未预期控制台错误、无 page error；唯一 404 是该受控兼容性检查。主控查看了最终失败任务页面截图。三张最终截图在同一目录，初轮存在重复 key 的回放和截图另留 `*-initial`，不以初轮作为最终通过证据。
- 最终前端 `npm run build`（TypeScript + Vite）退出码 0，仅有既有大 chunk 提示；构建输出、退出码和运行命令由执行端归档在同一证据目录。没有自行安装依赖。

历史原件及隔离副本的 job、manifest、report、source PDF 共 12 项前后哈希均一致；三份原 job 仍为 failed、failed、completed_with_issues。上述 102/28 条和评审均来自历史运行，没有记作本 goal 新在线生成。自建浏览器服务已收尾，未停止用户服务、未清理 tmp 内容。

本阶段真实 provider 请求为 **0/80**，剩余 **80**；没有读取本机真实模型配置、没有外发年报。取消、部分报告、硬截止、跨进程累计额度和续跑尚属 R2；全局归并、入口收敛及两份新在线 Web/API 验收尚未通过。本阶段实施提交在任务书进度表登记，不 push；用户原有文档与比赛材料改动保持未暂存。

<a id="annual-analysis-recovery-r2"></a>

## 2026-10-06–07 R2 部分结果、故障恢复与请求准入验收

R2 由本会话明确配置的 `gpt-6-luna`、`max` 子代理实施代码及运行验证，主控负责方案、文档和实际差异、原始证据及页面审阅。此前失败与各轮源码身份保留。此处接受持久化、故障恢复及请求准入的软件关卡；不接受 R3 性能、R4 公司问题归并、R5 入口收敛或 R6 新在线结果。能力和接口见[架构](architecture.md)、[发现契约](annual-discovery.md)及[导出契约](annual-report-export.md)，阶段标准见[恢复任务书](annual-analysis-recovery-plan.md)。

正式基础报告在首个模型请求前保存完整恢复绑定和待处理阶段；批次与独立阶段结果经校验后发布不可变检查点，最终封存与部分可读明确区分。取消、硬截止和服务重启先关闭准入，再确认所属写入进程停止；无法确认时保留只读并暂缓续跑，不按裸 PID 终止未知进程。续跑创建新 job/run，复用成功批次及语义输入一致的已完成独立阶段，保留父归档和继承合格发现。每次实际联网尝试在持久账本准入，重试分别计数；已用账本缺失、损坏或结构不完整时拒绝外发。

### 检查范围与原始证据

以下均为离线、故障注入或本机 localhost HTTP。本机模拟 HTTP 有实际请求，真实配置服务请求为零。不同冻结版的定向检查不合并为最终完整套件计数。

- 归档/检查点专项为 **110 passed、1 skipped**，JUnit 17.597 秒；跳过为 Windows 目录符号链接条件。覆盖双写者序号、最终封存、发布窗口、严格来源/引用、重试审计及旧归档兼容。后续读取器改动另经相关定向检查和实际 CLI 复核。[范围、命令与结果](../artifacts/evaluations/annual-recovery-r2-20261006/archive-final.json)、[JUnit](../artifacts/evaluations/annual-recovery-r2-20261006/archive-final.xml)。
- 连接器/准入专项分别为 **46、15、9** 项通过，另有阻塞和滴流两项通过。12 个实际独立进程争用五次额度，仅允许五次 HTTP；重启保留计数，耗尽、旧 gate、账本缺失或损坏均拒绝。请求子进程有界退出，实际 Popen/PID 退出已核对。[核心 JUnit](../artifacts/evaluations/annual-recovery-r2-20261006/provider-runtime-20261006T205000-postfix-core-unit-03/core-unit-junit.xml)、[进程/HTTP JUnit](../artifacts/evaluations/annual-recovery-r2-20261006/provider-runtime-20261006T213000-postfix-integration-04/integration-junit.xml)、[最终边界证据](../artifacts/evaluations/annual-recovery-r2-20261006/provider-runtime-20261006T223000-postfix-final-boundaries-01/)。
- `71a9eac…` 冻结版 **八个实际 CLI 场景通过**：鉴权停止、连续连接故障、首请求取消、非法响应、固定候选鉴权停止、候选阶段取消/续跑、绑定漂移及部分父链多代续跑。每项有实际 argv、源码/加载身份、JUnit、退出码、本机 HTTP 与严格读取记录。partial child/grandchild 均只请求批次 3/4，固定调查和评审没有新 HTTP，原成功评审审计仍可读；新增语义输入触发新评审，sealed completed parent 被零请求拒绝，原父链不变。最后一项 JUnit 79.978 秒。[主控八场景审阅与逐项入口](../artifacts/evaluations/annual-recovery-controller-20261006/r2-eight-cli-review-20261007T124043.json)。
- 所属进程停止/超时定向检查为 **8 passed、42 deselected**，JUnit 11.275 秒；compile 与前端 typecheck/build exit 0。最终页面源码 SHA 为 `6dc526caf569999f8b4db132af74836425fda5a56103b4caf47441a7e3cb0b9d`。构建有既有单块大于 500 KB 的提示，未扩大范围拆包。[进程定向证据](../artifacts/evaluations/annual-recovery-r2-20261006/frontend-owner-stop-timeout-attempt-20261007T0342Z-95b8ac0ee7d24d89b68451729e50d2c1/)、[最终文案与构建证据](../artifacts/evaluations/annual-recovery-r2-20261006/frontend-queued-cancel-copy-9d577de0da4145aa89b4d513da8b16e7/)。

实际 API 轮询曾发现 Windows 正式报告原子替换失败（WinError 5）：AI 阶段已完成，指针仍为 running/partial sequence 16，而 manifest/job 被错误描述为 completed_with_issues。该原始失败及 partial 读取保留，不能记作最终封存通过。[缺陷独立核对](../artifacts/evaluations/annual-recovery-controller-20261006/r2-final-publication-failure-20261007T125451.json)。修复仅对指定 Windows 错误码最多六次有界重试；持续报告或最终指针发布失败写失败记录、failed manifest 与 exit 2，保留最后可信 partial。seal 已发布后的终态 manifest 替换失败则保留旧 manifest 和 sealed 指针，不破坏严格发布窗口。

发布修复为 **24 passed、0 failed/error/skipped**，JUnit 32.636 秒、exit 0；另有 sealed→manifest 窗口一项通过（JUnit 0.260 秒）和严格 failed-manifest/partial-reader probe。该 fault suite 的 AI 行为为注入，新 API/浏览器另行实际验证。[发布故障证据](../artifacts/evaluations/annual-recovery-r2-20261006/report-publication-fault-20261007T0501Z/)、[主控修复审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r2-publication-repair-review-20261007T131013.json)。最终后端源码 manifest SHA 为 `a09bef320bcc2f6c58a5108bcdbeb06d0aa8eaf8db520bd8176503d0d7fe0477`，CLI fingerprint 为 `b13bf43078d9903928648f44212d91cd34bc50a9264cebca6156d2662e0252c9`；主控确认 72/72 实际文件一致，API/CLI 加载身份匹配。此前八场景所用扫描、连接器、账本和严格读取源码未被这次发布修复改变，修复后的实际业务发布则由下面两个浏览器场景验证。[最终源码独立核对](../artifacts/evaluations/annual-recovery-controller-20261006/r2-source-freeze-review-20261007T130433.json)。

### 实际 API、浏览器与失败恢复

先前实际 API/Vite/system Chrome 验证了受阻请求期间基础报告可读、下一请求受阻期间合格发现可见、排队取消无 CLI/无保存报告误称、运行取消的 partial 刷新及三导出、同一模型服务绑定重启后的严格只读与可续跑。各次错误 harness 期望、旧读取包装遗漏与 Windows 心跳失败均保留当时失败结果，修正后再核对，未放宽生产绑定。原始失败目录和审阅入口集中于[阶段审阅记录](../artifacts/evaluations/annual-recovery-controller-20261006/r2-intermediate-review.json)。

**60 秒 owner deadline / 90 秒受阻 HTTP：** 64.318 秒内记录 analysis_timeout，保留首个合格 finding、failed/partial sequence 4，gate 关闭且仅两次准入，无第三请求。截止前记录所属 CLI 与请求 worker 的创建身份；原脚本执行到页面检查前的停止断言，随后因不存在的“分析超时”标签失败，原浏览器尝试仍为 failed。后续只读审计严格重载报告并核对落盘状态；进程复核在脚本清理后执行，其范围未冒称为截止后完整变量原始记录。[原尝试](../artifacts/evaluations/annual-recovery-r2-20261006/browser-owner-timeout-fb64efded17447409d867e427b8bfa16/browser-evidence.json)、[只读状态审计](../artifacts/evaluations/annual-recovery-r2-20261006/browser-owner-timeout-state-audit-a81aa3a962b646c2ab3eb94464915fb4/stdout.json)。

**新版实际续跑、刷新、历史与三导出：** 在新冻结版隔离副本复用上述超时父任务，无须重跑 60 秒故障。新 child `annual-job-d2a4232f20304ef3a97a9f835580ac25` / `annual-analysis-9a01ee93-e1c7-4802-bbf6-0da8774c2241` 最终 completed、sealed sequence 17、is_partial=false。父批次 1 复用且零新 HTTP，只请求批次 2–9；发现 8 次、固定调查 2 次、评审 1 次，共 11 次，与 gate admission 相同，父链累计 13 次。页面路由/刷新、API 历史和 JSON/Markdown/HTML 的正文与响应头同为 completed/17/false/同一父 run。主控核对三文件实际字节数/SHA、两张截图、72 个副本源码及 24 个父产物，均一致。direct runner exit 0，无 JUnit；原终端流未另重定向的限制已记录，API/Vite 日志及 JSON 保留。[浏览器结果与逐项证据](../artifacts/evaluations/annual-recovery-r2-20261006/browser-timeout-parent-resume-412eb7e3305c44b4aa804129c5141613/browser-evidence.json)、[主控独立核对](../artifacts/evaluations/annual-recovery-controller-20261006/r2-browser-child-review-20261007T131911.json)。

**并发读取及最后版本投递：** 单独复用同一受控 partial 父副本，只补此前没有及时挂好的轮询关卡。新 child `annual-job-4d00027450714aacbc1f87ab05783fe0` / `annual-analysis-74d2dfaf-03b8-4920-b5d9-80fb279cc223` 读取时间线为 running/partial sequence 2→10→sealed completed sequence 17；job 仍 running 时即可严格读取已经发布的封存快照。浏览器实际持有旧 sequence 2 响应，在终态 API 结果落盘后才释放；随后重新 GET 并收到 sequence 17，最终 DOM 展示完成状态和续跑发现。两次读取的 job/report 可能因正常并发推进使用不同序号，每个单独读取与导出内部保持一致。父 24 项哈希不变，发现 8+调查 2+评审 1 等于 11 次本机 HTTP 和准入。direct runner exit 0，API/Vite 收证后退出；主控阅读时间线并查看最终截图。过晚启动、连接被拒绝的旧 poller 单列失败，不算通过。[完整时间线、截图与命令](../artifacts/evaluations/annual-recovery-r2-20261006/browser-report-timeline-ec8643af444e4f14802076e47d248ddd/report-timeline-evidence.json)。

### 阶段边界与后续

原始保护基线共 703 个文件；最终主控核对缺失/变化零。用户初始产品路线/验收文档修改、计划书 Word 删除与另名文件、原 PDF 均保留；本阶段仅暂存本任务文件及主控新增验收段落，不纳入其他未提交内容，不 push。tmp 默认保留。正式产物位于 `artifacts/evaluations/annual-recovery-r2-20261006/`，主控[最终阶段审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r2-accepted-review.json)记录保护证据、验收范围和本地提交身份。

本 goal 真实配置服务请求累计 **0/80**，剩余 **80**；尚未读取真实模型配置。R3 同源输入压缩与有限主报告预算、R4 全部发现归并、R5 普通入口及 R6 两份新在线结果仍待执行。当前源码不表示 15/90/300 秒真实性能目标已通过；最终完整后端套件、前端构建和最终浏览器矩阵留在 R6 最终代码上验收。真人财务复核及其他外部交付关卡保持未通过状态。

<a id="annual-analysis-recovery-r3"></a>

## 2026-10-07–08 R3 请求压缩与阶段预算（离线工程验收通过）

R3 代码及运行验证由本会话新的、明确配置为 `gpt-6-luna` / `max` 的执行代理完成；主控负责方案、正式文档及差异和证据审阅。本节按发生顺序保留中间失败及修正，最后的实际 HTTP/Popen/API/CLI 与浏览器证据已审阅通过；离线工程通过不代表真实供应方性能或语义质量通过。预算、版本与独立 CLI 的边界分别见[发现契约](annual-discovery.md)和[架构](architecture.md)，真实 15/90/300 秒目标留在 R6。

### 同源请求规模对照

最终测量使用新 `annual_discovery_v3` 提示词、`annual_discovery_plan_v3` 和完整 Chat Completions 请求 JSON，模型名为离线占位值 `offline-sizing-only`。两份实际 PDF 的源 SHA、文字字符、UTF-8 正文和页面与旧计划一致；没有关键词过滤或文字截断。旧基线、紧凑原型和最终请求分别保存，原型使用 v2-sizing-only，不能替代最终请求。正式[最终测量](../artifacts/evaluations/annual-recovery-r3-20261006/attempt-03-final-v3-payload-measure.json) SHA 为 `eb3992e1f53ebf6b15b1ce35f89301299af35bc026bae3071baa479607806ec9`，主控已阅读并核对输入及实现哈希。

| 输入 | 原文字符 / 片段 | 旧→最终批次 | 请求字符（旧→最终） | 请求 UTF-8 字节（旧→最终） |
| --- | --- | --- | --- | --- |
| 海天 2024 | 204321 / 6856 | 42→18 | 989966→403633 | 1300706→669469 |
| 茅台 2025 | 151314 / 3289 | 23→13 | 540285→263369 | 761342→466750 |

两份最终规划均保留全部可提取文字、遗漏零。海天本次解析 0.439628 秒、规划 1.978184 秒；茅台解析 0.156394 秒、规划 0.699967 秒，两份测量 wall time 3.305523 秒。每份在此离线测量中先解析一次、规划复用对象，不据此宣称整个 CLI 只打开一次 PDF，也不把不同缓存状态下的解析时间差当作供应方提速。海天一批完整请求正好 24000 字符；R6 须在读取实际配置后用真实模型名重新离线规划，外发前核对限额。

扫描 31 次，加每份固定调查 2、归并/补证预留 8、评审 2，正常规划额度为 **55/60**，整个 goal 另留 **20** 次失败续跑额度。归并/补证仍是 R4 预留，未实施。每批严格最多三条使总输出机会随批次数下降，原文覆盖不能证明问题发现质量不变；后部事项、跨章节及新在线原文对照另验收。

### 已核对的源码与定向检查

浏览器续跑修复前的 [attempt-10 CLI 身份](../artifacts/evaluations/annual-recovery-r3-20261006/attempt-10-source-docstring-identity/cli-runtime-identity.json) 为 74 文件，源码 manifest `f55244503dbec6b13d01d8938ae841ac85926d607bafefc4bfd31fe1ad09c2aa`，加载代码 `1cdb31badb813cc01057b1e4507c003908563d7e2c77c3ad0861eb9937603ab0`，loaded_source_match=true、model_calls=0。主控独立逐文件重算 manifest 一致。attempt-08 及前面的单位检查使用先前身份，仅差预算函数说明字符串的档案/live 边界澄清；原证据保持各自身份，不覆盖。实际 API 身份由相应隔离服务记录，后续修复身份另列，不据此给旧证据换版本。

- [attempt-05](../artifacts/evaluations/annual-recovery-r3-20261006/attempt-05-backend-unit-regression-after-mock-fix/)：上下文、发现、调查附加、账本和严格读档 **138 passed / 8.07 秒**；主控核对原始日志、JUnit 与当时文件哈希。包括修改 live 默认值后旧 v4 档案预算仍能严格读取。attempt-04 的 **6 failed、132 passed** 保留，原因是旧测试替身未接受新增 abort_event 参数；修正替身后复测通过，不删失败。
- [attempt-06](../artifacts/evaluations/annual-recovery-r3-20261006/attempt-06-cli-atomic-budget-gate/)：独立 CLI 默认原子首次/续跑门及 API-owned gate 核对 **3 passed / 1.00 秒、14 deselected**。它是该门控定向检查，不冒称完整实际 CLI 故障矩阵。
- [attempt-07](../artifacts/evaluations/annual-recovery-r3-20261006/attempt-07-reader-export-regression/)：reader/API/export 定向回归 **64 passed / 20.70 秒**。JSON 保留 v4 安全 budget/scheduler/timing；Markdown/HTML 当前只显示状态、覆盖与限制，未实现预算摘要，三格式实际下载仍待浏览器核对。
- [前端最终静态检查](../artifacts/evaluations/annual-recovery-r3-20261006/frontend-budget-ui/final-attempt-20261007-01/)：typecheck/build 均 exit 0；主控阅读原始 stdout/stderr/exit 文件，核对八个源码/依赖清单及实际 JS/CSS 哈希。JS 为 547727 字节，仍有既有超过 500 kB 的 chunk 提示；此记录不代表浏览器已通过。
- attempt-09 仅回补此前 **49 passed** 的回溯元数据；该轮没有可核对的正式原始 console 文件或 JUnit，不与以上当前可核对结果合并计数，也未为补日志重复业务测试。

实际运行失败及成功关卡将逐项按唯一 attempt 保存；测试环境、夹具或观察器失败不记为产品通过，生产缺陷在同阶段修正后重新核对。当前真实配置服务请求累计仍为 **0/80**，剩余 **80**；未读取本机真实模型配置。历史归档、两份真实 PDF 与用户初始未提交改动保持保护，R3 尚未暂存或 commit。

### 实际 CLI 与本机 HTTP 健康场景

[healthy-010](../artifacts/evaluations/annual-recovery-r3-20261006/runtime-budget/healthy-010-20261007T213034-1a32a329/) 为原创模拟 36 页 PDF 的实际独立 CLI/Popen 和 loopback HTTP 验收，**1 passed / 12.80 秒**，JUnit 12.710 秒，outer runner exit 0。业务 CLI 自然 exit 0，用时 **5.683142 秒**；11/11 批次的 120040 个可提取字符成功处理，扫描 11 次、评审 1 次，与主控只读 SQLite 准入计数一致。本机 HTTP 并发峰值 3，首三批完成顺序为 2→3→1，后续批次在第一批未结束时滚动补位。请求保持挂起时实际观测到三个 worker 的 PID、父 PID 及创建身份，随后全部退出；单协调者发布、inflight drained=true，截止原因 null，未截断。

默认独立 CLI 的 new_run 额度为 task 30、阶段 18/2/8/2，档案工作预算为 300/285/15 秒；harness 未注入任务或阶段准入额度。actual CLI 使用新冻结 fingerprint 准入。最终快照为 completed_with_issues / sequence 18 / is_partial=false（原创材料存在基础财务缺口）；评审 completed/insufficient_evidence。此材料未提取出固定调查候选，调查零次，**不作为调查阶段验收**。直接调用导出层生成的 JSON/Markdown/HTML 状态、覆盖与快照一致，主控阅读正文并核对实际三文件字节和 SHA；这不是浏览器下载验收。JSON 有 v4 scheduler 和 phase_budget，Markdown/HTML 未声称有预算摘要。

此前 healthy-001 的导入环境错误、healthy-003 的短命 worker 观察失败、healthy-004 的旧 evidence_id 响应格式及其余观察器/断言调整均保留原失败 attempt。healthy-005 先通过核心运行关卡，healthy-010 补齐正文一致性与进程退出证据；不合并为多个独立场景通过。新的阶段耗尽、故障及浏览器关卡仍在执行，R3 整体保持未通过。

### 阶段额度与时间故障关卡

- [discovery-cap-002](../artifacts/evaluations/annual-recovery-r3-20261006/runtime-budget/discovery-cap-002-20261007T213714-025f8132/)：实际独立 CLI 新任务的默认原子扫描额度关卡通过，raw stdout **1 passed / 13.27 秒**、JUnit 13.181 秒；业务 CLI 自然 exit 2、8.653601 秒。为确定边界显式并发 1；扫描实际 HTTP 与只读 SQLite 同为 18。批次一直接成功，批次二至九各经过 429/503 后一次成功重试，批次十收到 429 后下一次准入拒绝，批次十一没有 HTTP：共九个错误响应（429 五次、503 四次）、八次成功重试。九条合格发现保留；扫描 stage cap 不标为时间耗尽，time_deadline_reason=null。评审仍独立完成，真实 loopback 与准入均一次；三种直接导出与严格 reader 状态/覆盖一致。discovery-cap-001 的连续故障夹具触发全流程停止，未到阶段 cap，作为原始失败保留。
- [stage-deadline-001](../artifacts/evaluations/annual-recovery-r3-20261006/runtime-budget/stage-deadline-001-20261007T213921-115e8e58/)：实际 CLI 的受控三秒扫描截止关卡通过，JUnit **1 passed / 10.501 秒**；业务 CLI 自然 exit 2、6.122815 秒。一批成功后第二个实际 HTTP 挂起；观测 worker 创建身份后，阶段截止使其退出，调度 drained/inflight=0，time_deadline_reason 与停止原因均为 discovery_stage_budget_exhausted。扫描准入两次，评审独立完成一次。终态后放行迟到响应，服务端确认客户端已断开，主控独立重算 run/report 树哈希与前后记录完全相同。该关卡使用受控 owner 60 秒和短阶段 override，不证明默认 150 秒或真实供应方耗时；三路并发异常停止另验收。

浏览器 attempt-03 揭示前端真实缺陷：合法 v4 未尝试批次保留 awaiting_model 行级初始原因，而顶层已有 stage cap；前端严格字段集合与顶层一致性检查拒绝报告，预算面板可见但正文不可见。原截图/DOM/failed 结果保留，执行端按后端版本契约最小修复后再验收，不能把该尝试写成浏览器通过。[失败证据](../artifacts/evaluations/annual-recovery-r3-20261006/frontend-budget-ui/browser-attempt-20261007-03/)。主控本次复核 3635 个原始及 R2 已归档保护文件，变化、缺失和不安全路径均零。

### 固定调查与并发故障的实际运行

- [sample-stage-003](../artifacts/evaluations/annual-recovery-r3-20261006/runtime-budget/sample-stage-003-20261007T134535-568d39dc/) 使用既有原创模拟年报的隔离派生 PDF，追加虚构经营讨论及附注，以产生两个可调查候选；不冒称原注册样例未变化。JUnit **1 passed / 7.902 秒**；业务 CLI 自然 exit 0、4.041748 秒，实际 HTTP 与主控只读 SQLite 同为扫描 1、固定调查 2、评审 1。全文扫描合法空发现，固定调查和评审完成。三种直接导出均为 completed / sequence 10 / is_partial=false，主控重算实际文件长度与 SHA 和清单一致。sample-stage-001 缺少调查材料、sample-stage-002 夹具语法错误的原失败证据保留。
- [investigation-retry-001](../artifacts/evaluations/annual-recovery-r3-20261006/runtime-budget/investigation-retry-001-20261007T134819-92f19a4c/)：raw stdout **1 passed / 7.26 秒**、JUnit 7.165 秒；业务 CLI 自然 exit 0、4.186718 秒。首个固定候选收到 429 后重试成功，实际两次调查准入均纳入预算；第二候选因现有 MAX_MODEL_CALLS 达到次数上限而没有 HTTP，原因是 model_call_budget_exhausted，不能将其写为账本 stage gate 拒绝。评审仍独立完成一次；实际 HTTP 与主控只读账本均为扫描 1、调查 2、评审 1。
- [auth-three-inflight-002](../artifacts/evaluations/annual-recovery-r3-20261006/runtime-budget/auth-three-inflight-002-20261007T135009-e0322fcc/)：raw stdout **1 passed / 12.78 秒**、JUnit 12.679 秒；业务 CLI 自然 exit 2、5.646782 秒。本机 HTTP 和只读账本均为三次扫描准入、峰值 3；一个 401 后 **0.623522 秒** CLI 退出，另两个挂起 worker 的父 PID、创建身份和退出已观测，没有第四个请求、重试或评审。停止原因为 authentication_failure_stopped，time_deadline_reason=null。终态后放行两个迟到响应，服务端确认连接已断开，主控独立重算实际 run/report 树哈希与前后记录一致。结果中的 worker_lifetime_after_observation_seconds 以进程查询完成后的单调起点计，与两个 UTC 观察时间差不是同一指标，不混用。auth-three-inflight-001 的观察窗口过短失败保留。

这些均为隔离 loopback 夹具，不是新真实供应方运行，不计入用户授权的 80 次额度；原始源码身份为 f5524450…09c2aa，不能代替后续修复版本的验证。

### 2026-10-08 浏览器续跑揭示的生产缺陷

[browser-attempt-04](../artifacts/evaluations/annual-recovery-r3-20261006/frontend-budget-ui/browser-attempt-20261007-04/) 在修正前端 v4 解析边界后，已有父报告正文可以实际加载；1440 与 390 像素视口的正文、实时预算和归档配置可见，窄屏 DOM 记录没有横向溢出。此尝试经实际 UI 创建子 job `annual-job-8ad5e8f788e249a78cadc8efbe44f7aa`，run `annual-analysis-b95131df-6625-4b12-95d3-c3d7f18a323a`，父 run 为 `annual-analysis-37b3e5b0-baa4-45c1-8409-887664fe52d2`。运行时预算是续跑 task 20、扫描 14；实际扫描准入 14、评审 1，但最终状态 **failed / archive_incompatible**，错误为 analysis_archive_invalid（annual_discovery_parent_invalid），三格式下载关卡未完成。不得把有预算截图或模型阶段结束写成浏览器验收通过。

主控已独立读取原 job、父子 discovery.json 和当前实现，定位两处生产缺陷：父链校验仍硬编码 v3 字段集合，拒绝合法 v4 父报告；LangGraph TypedDict 未声明 phase_budget 与 discovery_started_at，导致 invoke 输入被丢弃，子归档误记首次预算、阶段耗时为零。子报告实际上保留父成功批次的 reused 行；失败 job 的可用性投影中 reused_batch_count=0 不能反过来证明未复用。原失败归档保持不变，在 R3 内定向修复并以新源码身份重新验证，**阶段仍未通过、未提交**。真实配置服务累计 **0/80**，剩余 **80**，没有读取真实模型配置。


### 续跑契约修复与新源码冻结

[attempt-11](../artifacts/evaluations/annual-recovery-r3-20261006/attempt-11-resume-contract-repair-20261008/) 最小修复两个生产文件：严格父档读取按实际 v3/v4 精确字段选择并拒绝未知/跨版本；LangGraph 状态显式保留阶段预算和扫描单调时钟起点，缺少必需时序状态时失败，不再默默回落到首次默认值或零耗时。已接受的父关系决定 resume scope；拒绝父绑定时建立的零调用初始快照不冒充合法续跑。来源、原文引用、恢复绑定和审计检查仍保留。

修复后的定向回归 attempt-04-regression-fix-02 原始 stdout 为 **86 passed / 7.71 秒**，JUnit 为 **86/0 failed/0 error/0 skipped、7.630 秒**，exit 0。此前 bootstrap、六项失败及一项失败的夹具调整记录保持原样。包含真实 LangGraph 入口配合受控时钟的预算/耗时状态检查，以及合法 v4 父子、未知版本、跨版本、来源和引用篡改检查；这仍是离线测试，不替代实际 CLI 或浏览器续跑。

attempt-05-no-model-cli-freeze 的实际 CLI 身份为 **74 文件、manifest `89e68023523f16cf7f5d8ed47ead99830fd423254c0f1a52aa8c1e201f366a7f`**，加载代码 `96b0cab971a963c04381c901e7e0348bef7583752ed9e6a9f3c261fa8dc98d66`，fingerprint `bf5a35028170d28f9e1dfd6541eb027a1f81c5017787809cd870f5d3b4c03bc0`，loaded_source_match=true、model_calls=0。主控重算全部实际文件和 aggregate 一致，与 f552 只有上述两个生产文件变化。此前故障证据保留 f552 身份；新冻结版本的实际续跑、整份截止、API 排队计时和三格式浏览器下载继续验收。

[浏览器 attempt-05](../artifacts/evaluations/annual-recovery-r3-20261006/frontend-budget-ui/browser-attempt-20261008-05/) 在 Chrome 启动前因 runner 的预期 Markdown SHA 拼写错误失败，未记作浏览器通过。主控重算隔离父 job、JSON 和 Markdown 原始字节；键及另两个 SHA 均匹配，Markdown 实际 SHA 为 `4d9da428d9bfcb56ab8b2489649f87e7c126a1d5235b94c0eece1dfd61777e15`。原失败证据保留；后续使用新 attempt，不改旧归档或失败记录。该次 typecheck/build 均 exit 0。当前 R3 仍未通过或提交，真实服务 **0/80、剩余 80**，未读取真实模型配置。


### 新冻结版本的实际续跑、整份截止与 API 计时

以下三项实际 Popen/HTTP 验收均使用新 **89e680** / 74 文件冻结版本和原创模拟材料，仅访问隔离 loopback 服务；外部供应方请求零。主控阅读实际 runner、原始 stdout、JUnit、进程记录及 HTTP，并只读 SQLite、重算归档/导出原始字节；不将这些模拟运行写成真实年报在线验收。

- [resume-scope20](../artifacts/evaluations/annual-recovery-r3-20261006/runtime-budget/resume-scope20-20261008T023814560Z-efac5f9e/)：JUnit **1 passed / 12.109 秒**，outer runner 13.313471 秒；父 CLI 自然 exit 2、3.984083 秒，子 CLI 自然 exit 0、3.374386 秒。父扫描 11 次、评审一次，批次一响应结构不合格，其余十批成功；子任务仅实际请求批次一和评审，共 **2 次**，批次二至十一全部复用。续跑 scope 为 resume，task cap 20、扫描 cap 14，真实档案扫描耗时 0.54015 秒；11/11 批完整成功、文字覆盖完整。主控重算父 run/report 树与 before 一致，三格式文件 SHA 与清单一致，状态/覆盖/sequence 8 封存快照一致。
- [whole-work-deadline](../artifacts/evaluations/annual-recovery-r3-20261006/runtime-budget/whole-work-deadline-20261008T023935939Z-7d37adba/)：JUnit **1 passed / 8.758 秒**，outer 10.016047 秒；业务 CLI 自然 exit 2、5.521247 秒。受控 owner 60 秒、harness 起点五秒整份软截止、扫描阶段 40 秒；唯一计划批已准入并挂起，实际 HTTP/SQLite 扫描同为一次。调度停止及时间原因均为 workflow_time_budget_exhausted，而不是扫描阶段耗尽；worker 创建身份/退出已观察，迟到响应客户端断开，主控重算终态 run/report 树不变，三格式状态/覆盖与严格 reader 一致。它证明所有计划已派发时整份截止仍能结束，不代表默认 300 秒或真实供应方性能。
- [api-queue-timing](../artifacts/evaluations/annual-recovery-r3-20261006/runtime-budget/api-queue-timing-20261008T024139668Z-2022491a/)：JUnit **1 passed / 18.620 秒**，outer 19.791900 秒。实际 Uvicorn PID 14640、loopback 53628，模拟服务 53627，实际 API-owned CLI 为 PID 38704、21324；调用 Popen 返回时硬/软截止余量约 **59.989/44.989 秒、59.983/44.983 秒**，发布余量均为 15 秒。第二个任务排队时 running elapsed 为零，queue 从 0.033 增至 0.341 秒；实际运行后两任务终态耗时 **5.457/4.337 秒**，重复 GET 不再增长。每任务扫描一、固定调查二、评审一，HTTP 合计八次全部 200，两个只读 SQLite 各四次同阶段准入。主控重算实际 84 个 job/run/report 文件及 source PDF/source record 哈希全部一致。API 加载代码 `72b80d918fbff0a8f44c93b80932dbd1de52227ce6bb2fb7397dd3457e340fcc` 与 CLI 的 role-specific 加载哈希分别记录，两者共同 manifest 和 loaded_source_match 一致。短耗时仅验证冻结；超过 owner 的不裁剪实测保留 R2 历史身份。

API 的[首次失败 attempt](../artifacts/evaluations/annual-recovery-r3-20261006/runtime-budget/api-queue-timing-20261008T024055138Z-2aa3d0d0/) 在 Uvicorn 导入 Windows asyncio 时退出：观察器把 subprocess.Popen 包成普通函数，破坏其类继承语义。原始 **1 failed** 与退出记录保留，未有业务请求；修正独占测试观察器为原 Popen 子类后才得到上述成功，不改生产实现以迎合观察器。


### 新源码上的父报告浏览器与只读复核

[attempt-08](../artifacts/evaluations/annual-recovery-r3-20261006/frontend-budget-ui/browser-attempt-20261008-08/) 已实际使用 Chrome 154 在 **1440 桌面 / 390 窄屏**打开既有 R3 模拟父报告，并点击三个 UI 下载。主控查看预算截图及 DOM；两种宽度的 document/body 均无横向溢出，正文可读，无 JS page error、无 POST 或新任务。实际下载 JSON/Markdown/HTML 分别 **47880/5553/10617 字节**，主控重算文件 SHA 与下载清单一致；三格式均为 completed_with_issues / sequence 26 / is_partial=false，保留扫描 partial 和 18/49 已结束、16 成功、2 失败、31 未尝试及完整字符账。父档案仍如实保留修复前的零扫描耗时，不改历史值。

attempt-08 总 runner 结果仍是 **failed**：最后错误要求两次动态 job GET 的整份响应字节相等。该尝试只保存响应 SHA，未保存完整 before/after JSON，不能事后声称已经捕获其精确字段差异。随后保存的独立连续 GET 只观察到 recovery_progress.updated_at 变化，源码确实按当前 UTC 生成该投影字段。新的[attempt-11 只读复核](../artifacts/evaluations/annual-recovery-r3-20261006/frontend-budget-ui/browser-attempt-20261008-11/parent-stable-readonly-recheck.json) 按源、状态、快照、覆盖、终态 elapsed/queue 等稳定字段检查 **56/56**，重验 attempt-08 已捕获的实际图片/DOM/下载文件，不再启动浏览器或重复下载。主控再次重算原始父 job/run/report **122 个文件**，before/after/current 全相同，report API projection SHA 与修复前只读记录一致，模型请求零。新 API/CLI 共同源码 manifest 为 89e680，加载哈希分别按角色核对。

attempt-06 的 planned_chars 期望漏字段以及只读守恒关系的本地观察器错误记录均保留；不把修正观察器写成生产实现修复。实际页面续跑子任务仍需独立新证据，R3 此时仍未提交。

### 并发封顶取消在途请求：新增失败与修正关卡

[attempt-13](../artifacts/evaluations/annual-recovery-r3-20261006/frontend-budget-ui/browser-attempt-20261008-13/) 实际页面续跑创建子 job `annual-job-db94172a1b6640f1b2eb00fa02b16fe4`、run `annual-analysis-c97340a5-e9e2-4a51-aa6c-05a10549ff36`，仍使用 89e680 源码身份。父成功批次 1–16 复用；本次扫描 **14 次准入、12 个耐久响应**，批次 29、30 已准入却以 cancelled 失败。下一批次 31 因阶段次数额度拒绝，没有准入。实际发现耗时 **5.933921 秒**、整份工作剩余 **36.696063 秒**，time_deadline_reason=null，不能归因为时间超限。job 实际运行 9.876604 秒；加上评审一次，总准入 15。runner 原错误要求 response_count 等于 14，其失败记录保留；在本次受控无其他故障的场景里，两个取消仍是实际生产缺陷，不能只修观察器后宣称通过。

主控与执行端只读源码确认因果链：阶段次数拒绝触发 stop_dispatch，后者无条件设置共享 abort event；活跃模型 worker 收到该事件后被终止并记 cancelled。达到次数上限应停止新派发及重试准入，已准入调用仍须在既有阶段/整份截止内收束；取消、鉴权、安全拒绝、连续故障及真实时间截止仍需要中止在途。原单并发封顶测试没有覆盖这一三并发情形。R3 内追加定向修复，以及封顶后成功收束和封顶后仍能按取消/时间截止退出的关卡，不扩大额度、不改旧父子归档。

[attempt-14](../artifacts/evaluations/annual-recovery-r3-20261006/frontend-budget-ui/browser-attempt-20261008-14/) 对同一子报告进行只读浏览器/下载和尾部审计，无新 POST。主控查看 1440/390 预算截图，三格式实际下载分别 **46532/5588/10626 字节**，均为 completed_with_issues / sequence 22 / is_partial=false，并保留扫描 28 成功、2 失败、19 未尝试的覆盖账。is_partial=false 表示快照已封存，不代表 AI 扫描完整。两条取消耗时 **0.228527/0.105949 秒**；mock journal 仅有 12 条扫描和 1 条评审成功响应，没有请求身份的连接重置日志不能分别证明两条请求是否均到达服务。该次整体状态保留 failed_backend_child_outcome，UI/下载子关卡不能替代调度关卡。真实供应方仍 **0/80，剩余 80**，未读取本机真实模型配置。

[attempt-15 修复](../artifacts/evaluations/annual-recovery-r3-20261006/attempt-15-quota-drain-repair-20261008/) 分离次数关闭与在途中止；收束循环在 wait 前后持续检查取消及实际截止，停止升级立即保存检查点。扫描次数封顶保留 discovery_stage_budget_exhausted，task/ledger 次数封顶保留 provider_budget_exhausted；安全 gate 关闭不冒充次数额度耗尽。未尝试批次明确保留扫描阶段封顶原因，不再被白名单回落为 awaiting_model。来源、引用、预算数值及版本均未放宽。

最终聚焦回归 **8 passed / 3.04 秒**，JUnit 2.957 秒；发现引擎单元全文件 **57 passed / 3.45 秒**，JUnit **57/0 failed/0 error/0 skipped、3.360 秒**，均 exit 0。受控 LangGraph 测试涵盖阶段/task/ledger 封顶后正常收束、封顶后用户取消、阶段及整份截止，另覆盖 401 停止和 503 重试计账。此前成功的聚焦及 55 项运行保留，最终计数不累加重复运行。

新无模型 CLI PID 15704 的 **74 文件 manifest 为 `56c6fd3072261cec207b4ebcaceee523edb151512de36a4a331662867a0e7cd4`**，CLI 加载代码仍为 `96b0cab971a963c04381c901e7e0348bef7583752ed9e6a9f3c261fa8dc98d66`、fingerprint 为 `ebd20106ed025294eb6c34236725fb16ebf66e102846a70984bf39f6abaae987`，loaded_source_match=true、model_calls=0。主控重算全部文件/aggregate 一致，并相对保存的 89e680 实际运行副本审阅唯一生产文件差异。此时实际并发封顶/迟到响应和新冻结浏览器续跑仍在验收，不能据单元通过写成 R3 已通过。


### 最终三并发封顶、截止及浏览器关卡

新 56c6fd 冻结下的[标准 18 次封顶后收束](../artifacts/evaluations/annual-recovery-r3-20261006/runtime-budget/stage-quota-drain-20261008T041738Z-8ef1d1c4/) 使用原创 90 页、26 批材料；没有注入扫描次数上限。实际 HTTP 与主控只读 SQLite 均为扫描 18、评审 1，HTTP 峰值 3。live sequence 19 在额度关闭时仍有两个活跃 worker、time_deadline_reason=null；释放后 18 批全部成功、失败零、8 批未尝试，已处理/本次尝试字符同为 215600，遗漏 89228。最终在途零并排空。业务 CLI PID 14112 自然 exit 2、10.966624 秒；raw stdout **1 passed / 18.75 秒**、JUnit **18.658 秒**、outer 20.124083 秒。三格式原始字节由主控重算，均为 completed_with_issues / sequence 26 / is_partial=false。

[封顶后实际扫描截止](../artifacts/evaluations/annual-recovery-r3-20261006/runtime-budget/stage-quota-deadline-20261008T042311Z-c3d748aa/) 同样使用标准 18 次准入上限；受控 owner 60 秒、工作 45 秒、阶段 20/12/8/5 秒。实际 Popen 硬/软剩余 59.552069/44.552069 秒，差值 15 秒；归档 V1 默认仍为 300/285 和阶段 150/60/35/25，不能把默认配置当成本次实际期限。额度关闭 sequence 19 的时间原因为空，在途两条；**15.644828 秒**后 sequence 20 记录真实扫描截止，在途仍为两条。业务 CLI PID 30920 自然 exit 2、**23.401252 秒**，两 worker 的创建身份及退出已观察。终态扫描成功 16、失败 2、未尝试 8，review 一次完成；18 扫描和 1 评审由实际 HTTP 与只读 SQLite 双重核对。终态后释放两条挂起响应，客户端均已断连，未有 response_finished；主控重算实际 run/report 树与迟到前后记录相同，三格式同为 sequence 27 封存快照。raw stdout **1 passed / 30.86 秒**、JUnit **30.781 秒**、outer 32.019548 秒。这证明短期限故障收尾，不代替默认 300 秒或真实供应方测量。

[cap4 守卫失败尝试](../artifacts/evaluations/annual-recovery-r3-20261006/runtime-budget/stage-quota-drain-20261008T035551Z-a41f2c7/) 保留 1 failed：测试注入未绑定的独立 CLI 准入门，应用在 HTTP 外发前拒绝，实际请求零；随后改用标准 18 次额度，没有修改生产准入规则。[首次 deadline 失败尝试](../artifacts/evaluations/annual-recovery-r3-20261006/runtime-budget/stage-quota-deadline-20261008T041858Z-9b3f0d2a/) 保留 **1 failed / 30.51 秒**：前面的截止升级、worker 退出及迟到冻结检查已经执行，但末尾错误断言归档默认 300 应为运行时 60。只修测试观察口径后获得上述新尝试，不回写旧结果。主控自己的只读核对亦曾使用错误的 tmp 基目录和整份摘要相等，均在写入通过结论前纠正为 command.json 的实际 cwd 及完整导出中的对应字段；不将观察器问题记为生产缺陷。

[浏览器 attempt-15](../artifacts/evaluations/annual-recovery-r3-20261006/frontend-budget-ui/browser-attempt-20261008-15/) runner exit 0；唯一实际 UI resume POST 返回 202，产生 job `annual-job-eaa0345c6e6642a6beb24fff0e56d810`、run `annual-analysis-0cfbb4f3-fd2f-4627-a592-a983b1fcb349`。原父成功批次 1–16 复用，没有重复扫描；仅新扫描 17–30，14 次全部响应成功，加评审一次共准入 **15/20**。合并覆盖 49 批中成功 30、失败零、未尝试 19；累计处理 354780 / 569400 字符，本次尝试 165564 字符，遗漏 214620。job 实际运行 **11.173 秒**、排队 0.613 秒；扫描 **7.152866 秒**，峰值 3、最终在途零/排空、没有时间耗尽。API PID 62108 的实际加载身份及 CLI 身份共同绑定 56c6fd，活跃任务最终零；没有读取真实配置。

主控查看 1440 桌面与 390 窄屏截图、实际 DOM/网络记录；正文及两种预算面板可读，没有横向溢出或 JS page error。该次没有捕获运行中 child 截图，不将最终截图当作增量截图；增量行为另由先前专项和轮询时间线证明。JSON/Markdown/HTML 下载 **47003/5499/10555 字节**，SHA 分别 `9258a3bcc668c8175b546e3a3ed189a5b75bcff857b8be93eefb738ae57db531`、`40710ae8f1e6a788b31791fd425aa7edf889e8032f9eeb44fce7a43925226ca5`、`048e9fcc4160b84a464056bd0a35927a388715102e5f9483850e7ef6d30e68c9`，主控实际重算一致；三下载及响应头同为 completed_with_issues / sequence 22 / is_partial=false / 原父 run，覆盖字段一致。封存不等于全文完成，也不等于发现了合格问题。

主控再次重算隔离父 122 个文件、两个既有 child 各 102 个文件，共 **326 个**保持不变；R3 入口保护的原始与 R2 正式证据 **3635 个文件**无变化或缺失，用户原路线文档及验收记录前缀保留。前端 typecheck/build 的实际成功记录及七个源码 SHA 未因新浏览器尝试改变。综合本节定向回归、实际故障矩阵、源码审阅及三格式浏览器证据，R3 离线工程关卡接受，随后仅暂存本任务文件做本地 commit。旧失败和各冻结身份仍独立保留；R4/R5、最终源码完整检查与 R6 新在线验收未完成。真实供应方累计仍 **0/80，剩余 80**。


<a id="annual-analysis-recovery-r4"></a>

## 2026-10-08 R4 公司问题归并与同源补证

R3 离线工程通过后，主控仅暂存该阶段 35 个任务文件并本地提交 `4d4abe4de7fc48a6df26d2efd2f4e14607e16290`，没有 push；验收记录以 HEAD 加任务段落单独构造 index，用户原有 39 行留在工作区。阶段审阅和原始证据见上节及[主控 R3 接受记录](../artifacts/evaluations/annual-recovery-controller-20261006/r3-accepted-review.json)。工作区其余未提交内容仍为用户路线文档、原验收记录、计划书 Word 删除/另名文件及用户茅台 PDF。

R4 使用新的、明确配置为 `gpt-6-luna` / `max` 的执行子代理 `r4_issue_pipeline`；主控负责正式方案、文档、审阅、验收和阶段提交。实现方向见[恢复任务书 R4](annual-analysis-recovery-plan.md#r4从分批发现形成公司问题清单)。执行端先根据当前代码回报精确版本、成员去向、引用、审计及父子恢复合同，由主控在本阶段核对后实施。此时尚无新问题层代码或验收，不把版本提案写成已实现；真实模型配置仍未读取，真实供应方累计 **0/80，剩余 80**。本阶段只使用本地离线资料与隔离 loopback 模拟服务，正式运行证据写入唯一 R4 目录，失败尝试保留。


主控已完整读取执行端单份具体合同，并在同阶段接受实施：全量候选/成员账、严格局部分片结果、多个分片后的完整 global reconciliation、同源补证定位与角色、新 v5 恢复绑定及独立 review_v3 输入共同进入实际标准流程。新的评审 wire request 上限选为 **24000 字符**，旧 review_v2 的 16000 和原输入构造保持历史语义；这是新版本行为，不是将旧报告升级为全局评审。评审语义签名保留全部业务内容，剔除受控 run 绑定和易变执行元数据；复用仍独立核查原成功审计和完整父链。模型数字、舞弊和因果表述采用保守限制，原始合格发现保持原样。源码、故障关卡、实际进程/HTTP/账本及浏览器证据尚待执行和审阅，R4 仍未通过。

### 实施期源码审阅与证据边界

主控对实际工作区逐项审阅，发现集合签名仅含 ID、本地重复判断遗漏标题、补证角色越界、重复 JSON 键未拒绝、新评审仍经过旧有界输入裁剪等问题；另要求修正成员代表关系、增量闭合账、严格失败审计、补证预览、导出白名单及字符/字节计量。执行端在同阶段修正，尚未冻结源码或通过 R4。具体发现与当时源码 SHA 见[主控源码审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-source-review-20261008-01.json)。不能用新 validator 自洽或单元计数代替 API/CLI、原 HTTP/账本与浏览器验收。

执行端明确承认此前回归未将 stdout/stderr 落盘，主控不将会话中的通过计数作为正式测试证据，也不事后重建 raw 输出。后续每轮使用唯一尝试目录保存命令、原始输出、退出码、JUnit、耗时与源码身份；失败原件保留，验收只依据已检查的实际证据。

R4 入口保护清单的 6233 个文件已再次重算；当时仅 R3 的闲置 Vite stdout 增长，108 字节原前缀 SHA 保持一致，新增为本次前端编辑触发的七条 HMR 日志。主控要求执行端独立确认该已知进程创建身份及闲置状态后停止，避免继续追加历史日志；增长记录保持原样，未截断或回写旧证据。检查详情见[历史开发服务日志增长记录](../artifacts/evaluations/annual-recovery-controller-20261006/r4-historical-log-growth-review-20261008-01.json)。这不改变原阶段运行结果；不能将全部保护文件写成逐字未变。真实模型配置仍未读取，真实供应方累计 **0/80，剩余 80**。

首轮 R4 原始检查已保存：`r4-compile-01` 编译通过；`r4-unit-issue-review-reader-01` 因 runner 参数顺序启动失败，未执行业务测试，原始失败保留；`r4-unit-issue-review-reader-02` 实际执行 56 项，55 项通过、1 项失败、无跳过（JUnit 25.896 秒，外层 27.436 秒）。失败为运行中检查点的成员处理账重建，仍须修复并在新尝试复核，不能记为阶段通过。原始 stdout/stderr、JUnit 与前后来源清单已核读，见[主控首轮原始测试审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-raw-test-review-20261008-01.json)。真实服务请求仍为 0/80。

检查点专项修复的 `r4-checkpoint-retest-01` 已实际核读：17 项全部通过、无跳过，JUnit 6.765 秒，外层 8.072 秒；该范围为失败检查点与旧年度评审回归，尚未代表完整 R4 或真实运行通过。主控另发现 v3 评审复用签名包含本次扫描尝试字符数，要求在保留完整业务覆盖与原始审计的同时移除本次调用元数据的干扰，并验证不同父子运行的零调用复用及覆盖变化拒绝复用。

新增 v3 回归的前两轮原始失败分别是固定叙述证据测试漏计基础事实页（55 项中 1 项失败）和共享池辅助函数缺少 `hashlib` 导入（56 项中 1 项失败），均保留原件。修复后的 `unit-v3-reader-failure-pools-03` 实际执行 97 项全部通过、无跳过（JUnit 27.873 秒，外层 29.362 秒）；主控核读了 argv、JUnit、原始流哈希和相同的前后来源清单。该结果仅接受受影响单元回归；剩余集成、全请求测量、故障与真实浏览器关卡尚未通过，R4 未提交。见[主控回归审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-raw-test-review-20261008-02.json)。

同源补证检索模块、API 白名单页图和受控抽屉接入后的 `unit-supplement-api-export-01` 为 130 项全部通过、无跳过，JUnit 31.887 秒、外层 33.509 秒；`frontend-typecheck-02` exit 0、2.662 秒。两次前后 24 个来源文件清单一致，aggregate 为 `f023860c964f4b1c3eff3ebbb5d1ad09dfd0303fda1ba42491031b7b5a2cd365`。第一次 typecheck 因局部变量拼写失败，原始记录保留；主控已核对原始流哈希和 JUnit，见[专项审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-raw-test-review-20261008-03.json)。后续源码审阅另要求区分扫描、命中、选片与角色完成，从同源 PDF 重建检索账，并修正补证未完成时的阶段状态、常规 API 排除理由原文投影和新模块加载身份。这些修正及实际运行仍在实施；本记录不表示 R4 通过，在线请求仍为 0/80。

可选阶段计时和独立展示排序的单元子集先后两次各 79 项通过，不累加重复执行；后一次 `backend-review-export-02` 的 JUnit 为 24.588 秒、外层 26.299 秒，前后 27 文件清单一致。测试含零模型调用的空发现组件生成计时校验，不代表实际 CLI/API 性能或恢复已通过。预算集成的两次导入启动失败和前端一次启动器失败均保留；修正启动后的 `jobs-budget-projection-04` 实际为 52 项、16 项失败、1 项跳过，JUnit 50.795 秒、外层 52.791 秒。主控发现新增阶段耗时变量覆盖任务总时长，另要求逐类核对旧版本 fixture 与新 v5 恢复约束；没有放宽来源或旧归档读取规则。唯一跳过因当前 Windows 无法创建符号链接，该分支未验证。`frontend-typecheck-display-order-02` 实际 exit 0、2.421 秒。见[单元审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-raw-test-review-20261008-04.json)及[集成失败审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-raw-test-review-20261008-05.json)；R4 继续修正，尚未审阅通过或提交，真实请求仍为 0/80。

任务接口修复后的 `jobs-budget-projection-retest-07` 为 52 项中 51 项通过、1 项跳过、无失败或错误，JUnit 58.754 秒、外层 60.612 秒，前后 27 文件 aggregate 均为 `620e101878a90d0d701598cedd3d0b2ccb7f9730a8d95ce1e15860dcc230fba3`；跳过仍是无法创建 Windows 符号链接。之前的失败原件保留。主控核对了完整 v4 基础绑定的新 v5 严格入口，以及测试修正的含义：没有实际停止证据的模拟超时应保持 `process_stop_unconfirmed`；已失败 manifest 不投影为成功或别的终态。v5 模拟取消、重启及续跑可严格读取，不替代实际子进程验收。见[原始集成复核](../artifacts/evaluations/annual-recovery-controller-20261006/r4-raw-test-review-20261008-06.json)。同源检索的重叠窗口字符账仍在修正；完整请求测量、实际进程和浏览器关卡尚待完成，R4 未通过、未提交，真实请求累计 **0/80，剩余 80**。

补证重叠窗口修正后的 `issue-supplement-retrieval-regression-01` 为 112 项全部通过、无跳过（JUnit 27.222 秒，外层 29.046 秒）；原始记录经核读，见[检索字符账审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-raw-test-review-20261008-07.json)。随后严格旧归档投影的完整 wire 测量成功执行，但暴露当前方案不可直接上线：海天旧发现前 54 条、茅台旧 28 条加 11 条人工压力项、海天旧 102 条及再加 18 条人工项，其全局请求分别为 280120、223375、542962、633880 字符，补证请求分别为 64262、61571、101936、115506 字符，四组新评审均因超过 24000 字符而明确弃权；这些是历史/人工压力输入，不是新 v5 发现。主控接受唯一候选池、完整成员去向及引用共享池的无损压缩方向，要求当前无模型生成的两份基础报告及正常短输入可用后才进入实际运行关卡，见[完整请求与增量方向审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-wire-direction-review-20261008-01.json)。README、架构、发现与数据文档已按当前源码同步流程和可信边界，链接已核对；R4 仍实施中、未验收、未提交，真实请求累计 **0/80，剩余 80**。

候选与处理路径共享池改动后的 `review-wire-compaction-unit-02` 为 43 项全部通过（前一次 42 项中 4 项失败的原件保留），仅接受受影响单元回归。`issue-wire-compacted-01` 测量执行 exit 0、外层 11.754 秒、前后 29 文件来源一致，但正常短输入仍不可用：历史海天/茅台各三条候选的新评审请求分别为 93760、77695 字符，历史海天空候选约 73163 字符，均超过上限。压力集海天 54 条的七次局部归并加全局及补证至少需九次共用尝试，不能写成适配首次八次额度。随后 `issue-wire-compacted-02` 因旧 discovery_v2 与新 v5 入口不兼容而失败，保留原件，不将旧发现改标新版。主控另要求固定选片范围或永久超限不能制造零进展续跑；当前无模型基底生成、可读无损压缩及实际运行关卡继续执行，见[压缩测量审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-wire-compaction-review-20261008-02.json)。R4 未验收、未提交，真实请求仍为 **0/80，剩余 80**。

指定执行子代理 `r4_issue_pipeline` 与同配置的当前基底辅助子代理 `r4_normal_base` 均由协作工具明确报告账号用量限制而 errored；工具提示可在 20:40 后重试，不据此保证届时可用。主控没有换模型、接管代码执行或把中断记作通过。只读审阅发现恢复能力新调用 `_annual_issue_pipeline_has_executable_work` 尚无定义，属中断中的未完成改动；辅助端尚未产出当前 H/M 基底。主控重新核对 6233 个保护文件，无缺失，唯一变化仍是已记录的旧 Vite 日志 108→598 字节且原前缀保持；旧评审 22 个函数及 12 个常量 AST 均与 R3 基线一致。当前 33 个任务工作区文件身份、实际子代理状态和必需下一步已保存至[执行中断交接](../artifacts/evaluations/annual-recovery-controller-20261006/r4-execution-limit-handoff-20261008-01.json)。R4 未冻结为验收稿、未通过、未提交；R5/R6 未启动，goal 保持 active，在线额度 **已用 0、剩余 80**。

执行受限期间，主控完成剩余独立文档及静态审阅：年度导出说明已同步当前 v5 调用总计和问题层白名单边界，124 个本地文件链接均存在，`git diff --check` exit 0。另发现新补证检索已计算 `selected_unique_source_chars`，导出白名单却尚未保留该字段；此遗漏及对应验证要求已送入执行端待续接事项，未改写旧发现覆盖口径。见[文档与静态缺口审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-root-doc-static-review-20261008-09.json)。以上仅为主控可独立完成的工作，不能证明当前移动源码运行通过。

同配置子代理再次启动后已实际恢复 R4 工作，之前的用量错误记录保留。当前 H/M 无模型 CLI 基底完成：海天 `annual-analysis-b6916d2d-fd82-42a9-83c3-be6171da603c`，exit 0、5.684 秒、报告 SHA `254df425536edd8cfe98cad273168189c6f769fffd526a1df4210d2a875b7d80`；茅台 `annual-analysis-5eb006ed-d8db-425b-bfc7-3dd312a2a47f`，exit 0、2.971 秒、报告 SHA `adc5fa29b48cef81d2814e4a230904c64352abe6cb67962e105e99ebb728c79f`。两次均未携带模型配置、严格读取通过，80 文件前后清单相同；仅作为当前基底形状测量，不证明浏览器基础结果 15 秒或冻结运行验收。首轮捕获器错误丢失 raw 输出的两 run 明确排除且原件保留，见[当前基底审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-normal-base-root-review-20261008-01.json)。恢复 helper 定义和导出区间并集计数已实施，`recovery-helper-export-01` 54 项实际通过（JUnit 6.598 秒，外层 8.449 秒），但主控发现特定能力 negative fixture 因缺来源提前返回，要求用有效账和严格归档集成复核，未认定能力关卡通过，见[专项 raw 审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-raw-test-review-20261008-10.json)。正常新版 wire 压缩及运行关卡仍在执行；R4 未通过、未提交，在线请求 **0/80**。

### 后续材料与普通请求关卡复核

`recovery-helper-fixture-fix-01` 补正了有效来源、已接受首 32 项及尾部 8 项的账，以及范围内失败请求与未选尾部的区别。主控核读原始流、JUnit 和相同的 29 文件前后清单，实际 **54 项通过、无失败或跳过**，JUnit **6.810 秒**、外层 **8.449 秒**；仅接受局部 helper/导出回归，标准严格归档与父运行能力集成仍待验证，见[补正测试审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-raw-test-review-20261009-11.json)。

独立 `gpt-6-luna` / `max` 材料子代理生成两份原创文本 PDF，主控重算 PDF、解析 JSON、全部渲染页图和捕获流哈希，逐项核对 **15 个**原文字符切片与坐标，并查看末页、附录及清洁说明页。问题材料 **16 页、70775 个解析字符**，当时默认纯规划为 **6 批**且末页在第六批；清洁对照 **3 页、1053 字符、1 批**。材料仅作为后续离线输入，协议与期望断言单独保存；生成及 planning 不证明语义、故障或浏览器通过。失败生成尝试保留，最终冻结后须重核规划，见[材料主控审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-semantic-materials-root-review-20261009-01.json)。

下一 goal turn 的权威协作列表只返回主控，上一执行端句柄已不存在。主控查看磁盘最新 `current-h-zero-wire-17` 测量：当前海天空发现的完整请求 **37485 字符**，仍超过 24000；解码后可比性字段不一致，完整业务 SHA 不相等，且该目录缺少完整外层原始捕获，不能接受。新增私用码点文本流压缩也不符合已选可读输入方向。主控启动新的明确配置 `gpt-6-luna` / `max` 子代理 `r4_wire_repair` 继续同一未通过阶段，要求修正共享池/列式表示、精确等价及严格恢复集成，不扩大上限或静默删业务记录，见[普通请求拒绝记录](../artifacts/evaluations/annual-recovery-controller-20261006/r4-current-wire-root-review-20261009-03.json)。R4 仍未冻结、未验收、未提交；R5/R6 未开始，真实请求累计 **0/80，剩余 80**。

### 普通评审请求尺寸与压力材料复核

完整原件的 `wire-typed-v3-H-zero-01` 测量确认业务输入和还原输入逐字一致：各 55467 字符、60775 字节，SHA 为 `2f1edadb0ed94b36dc9a2b1dd52e8b9409e5cee8e570250254326f88223a3f36`；42 个允许引用 ID 保持一致。但实际 Chat 正文为 **35401 字符、40455 字节**，仍超 24000 上限 11401 字符，不能接受普通评审可用性。此轮只本地重建已有当前 v3 空发现审计并构造请求，没有新扫描、评审 HTTP 或真实供应方请求；完整输入、还原输入、编码输入及精确正文原件均保留。主控据分项开销安排列向量及精确常量重复的无损结构原型，不扩大上限、不删业务记录；尚无新原型通过证据。

第三次压力材料 `semantic-pressure-20261009T023745Z-d73354bd` 已接受为受控离线输入，先前两次原件保留。主控逐项核对问题/清洁 PDF 的 22/5 项引文精确字符切片和坐标、全部页图哈希及纯规划映射，并重算附录 11 页正文与页眉/页脚矩形交集为零；第七页及末段局部页图确认页脚分开。前五个实际批次各有三处同合同引文，后部独有事项在第六批，可供超旧前十二条的成员压力验证。它只证明输入及规划，不证明实际归并、恢复或模型语义；见[压力输入主控复核](../artifacts/evaluations/annual-recovery-controller-20261006/r4-pressure-material-root-review-20261009-02.json)。

主控在同一未验收 R4 内新增明确 `gpt-6-luna` / `max` 的独立执行子代理，分别准备标准严格归档恢复能力和标准 CLI/本地 HTTP 受控 PDF 语义集成。生产源码仍由当前编码修复子代理负责，新增测试文件所有权分开；各自实际版本、原始尝试和失败须单独保留。**R4 未通过、未提交；R5/R6 未开始；真实请求 0/80，剩余 80，真实模型配置未读取。**

### 标准管线暴露的契约错配及原型边界

列向量原型 `wire-typed-v4-proto-H-zero-02` 的完整业务输入与还原原件均为 55467 字符、60775 字节，SHA 与上一全量输入一致；编码输入为 25815 字符，完整 Chat 正文为 **30880 字符、35516 字节**，仍超上限 6880 字符。主控已重算四份原件哈希；此原型未进入生产协议，不能记为普通评审请求通过。只读残余评估表明，已测的池与列重复优化没有足够余量；主控转向先测量评审用途明确的业务投影，以及通用编码对完整跨片输入的适用性，不反复全量重跑，不修改请求上限。

实际标准 CLI/API 受控测试先后发现生产与严格读取错配：来源绑定字段名、归档递归键序导致请求正文哈希不同、完整单分片状态缺少关系记录，以及单分片的来源排序不一致。失败尝试均保留；修正不得以放宽来源、引用或关系比较实现。相应单元捕获分别为 15、109、134 项通过、无失败或跳过，主控已核读原始流、JUnit、相同的 35 文件前后来源清单及 aggregate。三轮 JUnit 为 **1.361、29.550、28.510 秒**，外层为 **2.528、30.859、30.328 秒**；重复测试不累加成独立覆盖，见[原始测试与原型审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-raw-and-wire-root-review-20261009-13.json)。单元结果尚不接受严格恢复能力、实际父子复用或完整 R4；来源排序修正仍须标准归档复测。

受控标准管线 `semantic-pipeline-20261009T032531Z-28c021c5` 的一项集成测试通过（JUnit 15.727 秒，外层 17.037 秒）；主控核对相同的 58 文件前后清单及 104 份原始审计文件。四个子运行分别覆盖 16 个发现和后部事项、8 个发现与三种未核验补证角色、清洁空发现，以及传输失败后保留 15 个已知候选。前两组分别有跨片请求超限及最终评审超限，均明确保留缺口，不作完整处理；只接受该捕获版本的软件语义边界，未捕获实际加载模块身份，未覆盖随后新增的通用 issue 编码。原创 PDF 已迁入 `data/samples/annual_issue_semantics_v1/`，测试不依赖临时目录。详情见[受控语义边界审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-semantic-boundaries-root-review-20261009-03.json)。

完整跨片输入的无损 typed 预检 `r4-global-stage-v3-preflight-02` 将 Chat 正文从 **29529 降至 14109 字符**，完整语义输入及还原 SHA 相同；该源 shard 因原尺寸限制未发出 HTTP，预检不冒充新跨片运行。提示词文本副本曾被 Windows 换行转换；补正尝试从已捕获的 Chat 原件提取精确 UTF-8 系统提示词，SHA `61354a911e308f00f33ba03aa928646657c2207475612de20cba6fc5e216e302`，旧文件保持原样。主控据此接受当前 v5 三种 issue 阶段共用 `annual_issue_input_compact_v1` 的实施方向，额度及 24000 字符上限不变。接入后的历史单元捕获为 **140 项通过、无跳过**（JUnit **28.373 秒**，外层 **30.235 秒**），随后源码仍有修改，不能作为最终候选通过证据；03:32 的严格测试快照碰上源码变动且捕获到尺寸 helper 参数错误，失败原件保留。后续先冻结候选再测试，见[通用编码方向与原始证据审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-common-issue-wire-root-review-20261009-14.json)。**R4 未通过、未提交；R5/R6 未开始；真实请求 0/80，剩余 80。**

### 冻结候选、取消归档及用途投影

当前事项请求与恢复能力已共用真实 Chat 序列化规则，受影响的最后单元捕获为 **29 项通过、无跳过**（JUnit 0.233 秒、外层约 1.613 秒）。主控逐项重算冻结清单中的 81 个运行源码/提示词、10 个业务配置、5 个前端文件和1个新增单元文件；运行源码身份为 `fed72c09a3d666af7980ba1387737334b3e7b48658a006533d6b1b4d8ceecbb7`，见[候选冻结审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-candidate-freeze-root-review-20261009-15.json)。冻结不表示全部关卡通过。

标准 API→Popen 的六个模拟运行均产生真实归档，首轮 outer 因把 CLI 身份探针计入分析进程，以及先读取取消时不存在的最终 investigation 文件而失败，原 exit 1 保留。另存只读诊断 `strict-capability-archive-readonly-20261009T040559Z-d1ea089c8f` 为 exit 0、11.707 秒；六份归档严格可读，core/public 能力一致。可执行局部、全局及补证取消任务可续；40 对角色仅首32对计划完成、尾8对未请求的任务不可作零进展续跑，44987字符永久超限压力例亦不可续，后者另有64对范围外角色，不能称只剩评审。主控重算前后归档/源文件哈希及原始流，实际 CLI profile 身份均为冻结81文件，见[只读归档审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-strict-archive-readonly-root-review-20261009-18.json)。当前空角色的合法 pending 补证仍使原文索引拒绝，记录为待修缺陷；不据此否定已严格可读的报告，也不把抽屉计为通过。

只读尺寸原型 `review-size-prototype-20261009-93d05685` 为 exit 0、0.504 秒，无新扫描或模型请求。主控核对12个变体的60份输入、编码、解码、提示词和正文原件；保留业务记录、显式省略机械定位及核验时间、仅别名内部池指针后再用整数节点编码，海天空/三条人工候选与茅台本地空/三条人工候选的完整 Chat 正文分别为 **21723、23409、20520、22270 字符**。解码精确等于用途投影对象，不声称精确等于含所有机械元数据的原对象；人工候选只作尺寸测量。主控接受任务书中的路径白名单用途投影及评审专用新协议实施方向，正式生产请求和实际调用/恢复仍须重新验证，见[用途投影原型审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-purpose-review-prototype-root-review-20261009-17.json)。**R4 未通过、未提交；R5/R6 未开始；真实请求 0/80，剩余80，真实模型配置未读取。**

冻结通用事项编码的 `semantic-pipeline-20261009T041201Z-dd4c53ca` 实际为 **1 项通过、无失败或跳过**（JUnit **24.554 秒**，外层 **26.075 秒**），四个标准 CLI 的 profile 捕获均为 fed72/81 文件。主控逐项核对108份原始审计文件、28对HTTP原件、请求解码语义 SHA 与审计 messages、只读 SQLite，并从源 PDF 重读36处补证字符切片和坐标。压力例16条发现形成5个问题，11条合并、5条保留，每个原 finding 恰有一条处理记录；第六批末页保函事项仍在。实际全局请求 **14061 字符、17237字节**并完成跨片闭合；该例和独立补证例最终评审仍分别为35428、29254字符而零请求弃权。清洁例有限评审完成为证据不足，部分例保留15条待处理发现及第六批失败，不称全文无重点。只接受通用事项编码的受控软件边界，见[冻结语义复核](../artifacts/evaluations/annual-recovery-controller-20261006/r4-common-codec-semantic-root-review-20261009-19.json)；首轮唯一块数量/argv观察错误原件保持失败。

`strict-capability-api-readonly-20261009T041116Z-4c7305b2` 在子进程内以实际 FastAPI ASGI 接口复核旧标准管线归档，exit 0、外层24.787秒。无进展尾部/永久超限及来源、模型、提示词漂移的5次续跑请求均返回409，没有创建分析子进程或新job；恢复原值后范围内失败任务可续。主控重算冻结81文件、6个SQLite表计数、原模拟服务事件及237个父归档文件树哈希，原件相同，见[API 拒绝专项审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-api-no-child-drift-root-review-20261009-20.json)。它是ASGI接口证据，不冒称浏览器或真实服务验收。随后三个执行端被工具用量限制中断；新建同型号/max 的 `r4_projected_review_execution` 已成功进入实施，负责下一评审候选及空角色原文修复。原6233文件保护清单再次核查，无缺失，唯一变化仍为已登记的598字节Vite日志且108字节原前缀保持。**R4 未通过、未提交，R5/R6 未开始，真实请求仍0/80。**

`runtime-harness-20261009T082841304Z-852f96bce05946b2ad9550ce6517ed74` 完成故障验收脚本的最小修正，捕获的静态编译及旧归档诊断为 exit 0。轮询文件使用全局单调序号；实际 CLI 身份由函数返回观察并绑定 Popen 的 PID/argv，排除身份探针；虚构发现从本次请求的原文片段引用，意见不含人工序号，评审回复保留输入要求的全部限制。另修正此前残留的缩进错误，未改变生产代码。主控核对测试文件 SHA、原始 stdout/stderr、静态脚本和旧失败记录；只接受静态修正，尚未运行此修正后的实际 API/CLI 故障或续跑测试，见[脚本静态审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-runtime-harness-static-root-review-20261009-21.json)。新版评审候选仍在实施，浏览器执行端仅准备独立脚本，冻结后再验收。**R4 未通过、未提交；真实请求0/80。**

用途投影的正式生产候选在 `projected-review-20261009T085535Z-7b1364e9` 重测：海天空/三条人工候选、茅台本地空/三条人工候选的完整 Chat 正文分别为 **21645、23331、20492、22242 字符**，均未超过24000；人工候选只作尺寸验收。主控重算13个当前源码和19份原始证据，解码投影 SHA、正文哈希及提示词逐项一致；另核对旧 review_v2 的22个函数和12个常量无变动。审阅发现并恢复“可能解释必须标为假设”后，四个业务约束段与前一冻结版本逐字相等，受影响单元 **154项通过、无跳过**（JUnit26.870秒、外层28.661秒）；前端文件未改，沿用源码相同的类型检查捕获，见[生产候选主控审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-projected-review-production-root-review-20261009-22.json)。当前运行源码已另存冻结清单，身份为 `4724745311667c38e9fb7d8a69e12ad669e1ebdf2395680676c5f3e87ebe5fe5`，81个运行文件、10个业务配置与23个前端文件固定；实际故障、语义/严格读取和浏览器关卡仍须以此版本新跑。**R4未通过、未提交；R5/R6未开始；真实请求0/80，剩余80。**

新冻结版本的首个 `runtime-acceptance-20261009T090906793Z-12d271ef52534c01b96deb9c220d2d7c` 实际预检 outer **exit1**：多进程观察日志出现碎行，验收脚本在读取Popen事件时失败，不能记为通过。原模拟服务15次请求原件及只读SQLite一致，分别为发现6次、事项8次（含两片各429/500重试）、评审1次；严格读取到18个合格发现、18条成员路径、1个已闭合问题和4对已完成补证角色，评审实际正文18043字符且完成。主控重算15对HTTP字节/响应SHA，正文与正式审计请求逐字相同，实际分析CLI profile绑定当前81文件；仅接受这些已取得的诊断事实，完整故障/续跑及浏览器关卡仍待完成，见[失败预检主控记录](../artifacts/evaluations/annual-recovery-controller-20261006/r4-runtime-preflight-failed-root-review-20261009-23.json)。执行端仅修观察日志原子写入，旧失败目录不改写。**真实请求仍0/80。**

第二次当前冻结预检 `runtime-acceptance-20261009T091736903Z-6d7e2d0e539b4fc79ebde587724c8b20` 为 exit 1、pytest20.55秒；并发观察日志419行全部可解析、无observer错误标记，失败点是验收脚本把字符串project_root传给Path接口。主控重算15对原始HTTP请求/响应及原审计正文，SHA、字节数、127.0.0.1和dummy鉴权均一致；实际CLI profile为472474/81文件，严格报告含18条发现、18条成员处理账、2片归并、全局/补证和完成评审。正式SQLite有15条准入，评审正文18043字符/21713字节，正文及投影SHA与审计一致。因收尾未完成，整项仍失败，最终source-after和preflight index缺失不补写；见[第二次失败原件审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-runtime-preflight-second-failed-root-review-20261009-24.json)。随后获准的新预检因验收脚本重复拼接artifacts审计路径而失败；旧原件保留，先作只读断言诊断再决定新运行。

当前冻结语义专项 `final-semantic-strict-20261009T091226Z-8537afa9/semantic` 为exit1、外层23.109秒；四个实际CLI已运行，唯一断言失败是verifier遗漏连续503导致上游停止、最终评审零调用弃权的合法分支。未将整项写为通过，严格能力阶段未运行；同一执行端仅修这个受控partial分支并准备新唯一运行。首次浏览器 `browser-r4-browser-20261009-4f01c34d62e64f74a762e5ab2c9d19fe` 实际退出1，等待取消按钮超时且未捕获页面DOM/截图。原job已经终态，不能据此判定按钮实现失效；该runner继承60秒故障时限，事项阶段按比例缩短，现核对正常300秒浏览器验收口径及失败页捕获。原始HTTP、Popen、退出及源码前后证据保留；浏览器关卡未通过。**R4未通过、未提交；R5/R6未开始，真实请求0/80，剩余80。**

第三次有限预检 `runtime-acceptance-20261009T092505231Z-f25177905270497b8b32837df26433ea` 仍为exit1。只读逐条诊断确认11条调查层汇总审计均对应发现6、局部归并2、全局1、补证1、评审1，没有独立调查调用；不能按汇总所在层重复计费。更重要的是，其真实API任务虽完成并严格可读，共享SQLite中所属准入门仍为 `closed=0`。主控核对源码确认正常终态未关门，记录为生产缺口，见[正常终态准入缺口审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-normal-terminal-gate-defect-root-review-20261009-25.json)。此前472474冻结身份及运行原件保留，不再用于声称后续修复候选已冻结。指定的gpt-6-luna（max）执行端已恢复并实施先关门再发布终态、关门失败暂缓续跑；完成代码及受影响检查审阅后才能另存新冻结并重跑专项。**R4未通过、未提交；真实模型配置未读取，真实请求0/80、剩余80。**

终态准入修复的正式本地API检查 `terminal-gate-20261009T145321Z-c2ffd56a/attempt-20261009T145844Z-64b4049b` 为 **57项通过、1项跳过、无失败**（JUnit72.333秒、外层74.203秒）；跳过项是当前Windows环境不能创建符号链接，不能记为该来源边界已验证。聚焦13项无跳过，JUnit24.479秒。主控核对原始argv/stdout/stderr/exit/JUnit、运行前后源码及既有冻结参考；仅年度job源码改变，80个其他运行文件、10个业务配置和23个前端文件相同。正常/失败终态先关所属gate，关门失败保持只读与不可续跑，settler安全恢复且封存内容不变，见[修复审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-terminal-gate-repair-root-review-20261009-26.json)。新冻结运行身份为 `842faf463fec2ab2fe5c4b54a3ec69474bd6e74c42d1dd61c5ebd4be30b22a34`；旧失败原件及原冻结身份保留。这里只接受代码与本地mock接口回归，新的实际语义、运行故障和浏览器验收仍待完成。**R4未通过、未提交；R5/R6未开始；真实请求0/80、剩余80。**

新842faf冻结的 `final-semantic-strict-20261009T150543Z-8b2ec473/semantic` 实际 **1项通过、无失败或跳过**（JUnit23.901秒、外层25.621秒）。四CLI分别检查压力归并、独立补证、清洁空发现及连续503部分失败。压力16条发现形成5个问题，全部成员与末页保函路径保留、全局闭合；其完整评审正文27894字符/42344字节超24000限额，明确零调用弃权。补证例8条成员形成5个问题，评审22465字符完成；清洁例不制造问题，有限评审8717字符；部分例保留15条发现，第6批同一请求连续3次503后停止、评审零调用。主控重算29对HTTP、112份审计原件/副本及各case只读SQLite计数，实际CLI身份均为新81文件，gate关闭；另直接重读PDF核对18处补证slice及bbox，见[新语义主控审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-final-semantic-root-review-20261009-27/root-review.json)。候选harness已在新的主控审阅目录另存完整源码，原运行目录不回填。只接受受控软件语义边界；严格能力、故障续跑和浏览器仍待完成，真实质量及性能不据此通过。**R4未通过、未提交；真实请求0/80、剩余80。**

新冻结严格能力首次运行 `final-semantic-strict-20261009T150543Z-8b2ec473/strict` 为exit1、外层1.803秒，在服务启动前因wrapper与测试同写 `source-manifest-before.json` 而失败。另存strict-only脚本后，`final-semantic-strict-strict-only-20261009T152025Z-6ef90a09/strict` 六个实际API→Popen案例仍全部失败，模拟服务共60次请求；局部/全局/补证目标请求未出现，另三例评审状态不满足专项要求。实际attempt结果为failed，恢复绑定漂移检查尚未执行；源码及受控PDF前后相同。续接时原执行端已不在工具列表，相关Python进程无存活；outer stdout/JUnit/phase-result缺失不补写，也不把六份归档存在记为通过。新的指定型号执行端先诊断受控provider与生产协议的实际适配，保持生产冻结与所有失败原件。

`runtime-acceptance-20261009T152602019Z-49d24f9b3a54492287f6aeac625ae1fe` 预检为exit1、pytest25.55秒，失败在验收脚本仅按Python对象地址配对退出记录。主控重读原观察记录确认同一CLI先后创建的两个worker复用地址 `0x9b47d50`，但PID及native创建时间不同，各有唯一正确退出。按父PID、子PID、对象地址和创建时间核对，19次返回/退出完全一一对应；观察到API/CLI/worker的382条加载模块记录均与冻结源码相符。15对HTTP原始字节/SHA与只读SQLite一致，发现6、事项8、评审1，所属终态gate为closed=1且数据库读前后哈希不变。只接受这些原件事实；outer仍失败、最终source-after缺失不回填，完整故障矩阵尚未通过，见[失败专项原件审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-current-failed-attempt-root-review-20261010-28.json)。新执行端仅修身份配对并准备完整模拟矩阵。**R4未通过、未提交；R5/R6未开始；真实模型配置未读取，真实请求0/80、剩余80。**

2026-10-10 的普通收集专项 `semantic-collection-guard-20261010T013345493Z-9dafd35ecb96` 为 exit0、1项跳过、无失败（JUnit0.140秒、外层2.175秒）。仅在三项专用R4运行变量任一缺失时跳过；变量齐备时保留原路径、冻结与业务断言。主控独立移除新增decorator后比较完整模块AST，原39个函数及21个顶层赋值相同，见[收集入口审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-semantic-collection-guard-root-review-20261010-29.json)。此前四个实际语义案例仍绑定原harness及其原始验收，不改称新harness运行。

严格六案的只读逐事件诊断确认60次本地模拟请求均与归档messages重建正文的SHA和长度一致，但回复状态、原始响应及response引用均缺失；54次发现和6次评审全部传输失败，未进入引用接受/拒绝检查。保留的旧harness在发送响应前调用未定义的 `_write_new`，故只补六行独占创建写入函数，未修改生产冻结或业务断言。故障矩阵harness同时将所有启动/退出、profile、外部verified stop与run映射改用父PID、子PID、对象地址和native创建时间，保留role/argv/cwd、来源及句柄前后一致要求；原其他函数AST不变，生产81/10/23文件仍完全匹配。主控接受静态修正并授权一次包含内置15次预检的完整本地矩阵，预计71次模拟请求、共享模拟上限80；这不是已执行结果，见[身份修正审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-runtime-compound-identity-root-review-20261010-30.json)。严格专项及浏览器仍待新运行与审阅。**R4未通过、未提交；R5/R6未开始；真实请求0/80、剩余80。**

新矩阵wrapper首轮 `runtime-acceptance-20261010T014044962Z-62c15786d01d4bc68bcbf7173889dcfa` 在Python启动前因Windows PowerShell有序字典的 `Measure-Object` 属性投射失败而exit1，实际请求为0。另存runner只将预算汇总改为显式字典键整数累加，71/80计划不变，见[预算脚本审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-runtime-runner-budget-root-review-20261010-32.json)。随后 `runtime-acceptance-20261010T014502097Z-008efcc14cd442278f9576a75cc94e0b` 实际为2项离线身份测试通过、1项集成失败（JUnit34.551秒），执行预检15次及可恢复父任务11次模拟请求后停止。父任务当前发现覆盖有缺口，事项闭合且有限评审完成，can_resume=true；错误在测试把整体partial要求为completed，未进入子续跑及后续故障案例，不能记完整矩阵通过。

严格专项 `final-semantic-strict-strict-only-20261010T013527Z-5fc247ad/strict` 保留完整outer输出与JUnit，为exit1、1项失败（JUnit134.483秒）。六案共71次模拟请求；尾部未计划角色、永久评审超限、范围内完成前三案达到各自专项断言，后三案实际取消后被driver无条件要求完成评审而拒绝。绑定漂移driver在输出末尾因未定义source_file失败，不能据此接受漂移专项。浏览器 `browser-r4-browser-20261010-4f7650d3dbd2438998efd46d31dafe2e` 在服务启动前因新wrapper与runner固定任务目录不一致而exit1，无provider请求、DOM或截图。主控核读上述原始失败与代码，保持原件并安排最小脚本修正，见[故障和严格专项失败审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-fault-and-strict-failed-root-review-20261010-34.json)。**R4仍未通过、未提交；R5/R6未开始；真实模型配置未读取，真实请求0/80、剩余80。**

恢复父报告断言只改为整体 `partial/discovery_coverage_partial`，仍要求完整成员闭合、成功分片审计、有限评审完成及可续跑；主控核对目标函数之外完整AST相同，见[部分状态断言审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-parent-partial-contract-root-review-20261010-35.json)。新完整矩阵 `runtime-acceptance-20261010T015537421Z-d4c58f7dcfdf4d39b1c3f8f9525ed79d` 保留exit1：2项离线身份测试通过、集成失败（JUnit52.241秒、外层53.430秒），实际27次本地模拟请求。预检与可恢复父任务通过后，子运行复用四个事项分片且本次事项准入为零，但审计摘要只收录两个局部分片，漏全局及补证记录，生产严格读取器以 `annual_issue_pipeline_audit_invalid` 拒绝。主控已从原始pointer/snapshot逐项核对，安排生产发布时从当前分片统一构造审计摘要，来源与读取强校验保持；旧缺失source-after不补写，见[复用审计缺陷审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-reused-audit-summary-defect-root-review-20261010-36.json)。另纠正只读诊断：严格取消案的便利report.json仅为基础报告，当前取消检查点实际保留发现、事项及待评审层，不能按便利文件推定持久结果丢失。新候选冻结及全部必要验收仍待完成。**R4未通过、未提交；R5/R6未开始；真实请求0/80、剩余80，真实模型配置未读取。**

复用审计摘要修复由指定执行端落入事项管线及其单元文件；每次发布与最终返回都按当前分片顺序构造完整摘要，删除旁路累计列表，严格读取器未改。`runtime-audit-summary-fix-unit-20261010T020017Z-afbafdd22d0d494e997736adb73f6394` 实际35项通过、无失败或跳过，外层1.924秒；主控直接读取JUnit suite墙钟为0.696秒，执行端0.394秒是单项累计，二者不混用。全复用四分片本次零调用但保留四审计、混合复用的阶段快照与损坏摘要拒绝均覆盖；主控核对全部原始哈希及代码差异，见[摘要修复审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-audit-summary-producer-fix-root-review-20261010-37.json)。新运行源码身份为 `38592c3737fb1a36bbb0be9c9c3ac193a99b1c95f2050883bfb7e465a14fd201`，仅一个生产文件变化；80个其他运行文件、10项配置和23个前端文件相同。新冻结清单另存，旧冻结与旧失败件保持。完整故障矩阵已按新冻结授权一次，严格及浏览器候选仅准备；这些运行尚未计为通过。**R4未通过、未提交；真实请求0/80、剩余80。**

新38592c冻结的矩阵命令在runner前置目录校验即exit1，约0.46秒：旧driver仅接受20261009冻结目录，拒绝新20261010目录。没有启动pytest、case或服务，请求为0，也未创建正式运行目录；原始证据为执行工具输出，不回填不存在的JUnit或前后清单。新唯一driver副本仅修冻结目录与拒绝消息两处日期，严格包含校验及其他字节不变，主控核对后接受，见[目录绑定审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-runtime-freeze-namespace-root-review-20261010-39.json)。严格候选同时只为独立确认的阶段取消要求待评审零调用，正常完成与超限弃权断言保留；漂移检查绑定合成父PDF并核对恢复后的SHA，五份正式源副本逐一相同。主控接受静态差异并启动一次六case及六绑定漂移检查，见[严格候选审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-strict-current-candidate-root-review-20261010-38.json)。旧842faf语义证据明确仅为历史前提，38592c实际语义仍待新运行。**R4未通过、未提交；真实配置未读取，真实请求0/80、剩余80。**

严格专项 `final-semantic-strict-strict-only-20261010T020617Z-3b67a6f3/strict` 在新38592c冻结上实际通过：1项pytest、六案完成，无失败或跳过（JUnit133.921秒，pytest外层135.271秒，runner外层135.491秒）。主控重算71对HTTP原始请求/响应，全部请求正文逐字对应审计messages及连接器序列化，核对142份审计文件、六个只读SQLite共71次准入且gate全关闭，读取前后数据库SHA不变。三个阶段取消保存cancelled部分检查点并可续，评审精确待调用且准入/审计均为零；尾部无进展和永久评审超限不提供无意义续跑，范围内工作可恢复。来源、模型、两提示词及评审wire/projection六次漂移均409、没有新增job/CLI/准入，合成父PDF和75个父归档文件保持。实际API与分析CLI均加载当前81文件，见[当前严格专项审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-strict-actual-root-review-20261010-41.json)。这里只接受受控严格能力边界，新的四例语义、完整故障和浏览器关卡仍待完成。**R4未通过、未提交；R5/R6未开始；真实配置未读取，真实请求0/80、剩余80。**


新冻结上的 `runtime-acceptance-20261010T021626200Z-674a8a4d0fdf4e4c99a969b5c7f64f1e` 实际仍失败（JUnit52.349秒、外层53.533秒），27次模拟请求完成预检、父任务及子续跑；子报告严格读取成功，四份事项审计完整保留且本次事项/评审零调用。失败是验收按同一document聚合了父子两个CLI。helper仅改为精确且唯一的run/job参数筛选，全部native身份/退出配对检查保留，见[筛选审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-runtime-run-job-scope-root-review-20261010-42.json)。随后 `runtime-acceptance-20261010T022417317Z-3671c49f345b4bc4a6de03a2e7c22fa1` 同样27次后失败（主case53.009秒、外层54.708秒）；独立身份两项通过，父子唯一捕获通过，但旧断言从不存在的terminal顶层读取parent_run_id。公开recovery、recovery_progress与严格快照中的父run完全相同。主控核对接口并审阅所有后续字段假设，接受仅改取nested recovery并增加严格快照父绑定检查；不是降低绑定要求或添加生产字段，见[父绑定契约审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-runtime-nested-parent-contract-root-review-20261010-45.json)。未达44个规划请求不计通过，缺失source-after不回填。

浏览器 `browser-r4-browser-20261010-47eb0ccffe8d419e8290e87429635d38` 实际exit1；1440/390页面、取消POST200及四张截图保留，7次本地模拟请求。原API所属CLI观察事件存在，但runner首次严格读取时尚未设置Popen路径，随后取证失败，尚未进入续跑/抽屉/导出。新runner仅将三项全局赋值移到身份验证后、首次严格读取前；主控重读完整差异并重算全部81/10/23冻结源及wrapper/helper/runner哈希，另存新attempt授权一次。语义runner首次同样在pytest前因登记路径错误退出，无正式case/CLI；只修清单runner相对路径后再运行，前置失败不计入测试，见[脚本及清单审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-browser-init-and-semantic-manifest-root-review-20261010-44.json)。以上均保留失败原件，不算浏览器通过。

`final-semantic-current-20261010T021657Z-luna01/semantic` 已在38592c当前冻结上完成标准CLI四例语义：pytest1项通过、无失败/错误/跳过（JUnit24.151秒、外层26.043秒）。主控重算29对HTTP请求/响应及112份审计，实际Chat正文多重集逐字对应；四个SQLite只读前后SHA相同、29次阶段准入与HTTP一致、gate全关闭；实际CLI身份均加载当前81文件。压力案例16条合格发现形成5个问题，后部第16页独有成员保留，评审完整请求27894字符超过24000而诚实弃权、零调用；补证案例8条形成5个问题，18段/17个原文块与20项角色保留，评审22465字符完成。清洁对照零发现、零问题，评审8717字符为证据不足，未凑出风险；部分案三次503后保留15条待处理成员、不声称全文完成。主控另从同SHA PDF重新检查36段补证字符范围及bbox，来源和未核验性质一致，见[当前语义原件审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-current-semantic-actual-root-review-20261010-46.json)。这是受控软件语义验收，不证明真实供应方质量或金融真人结论。**R4完整故障矩阵、浏览器和受影响回归仍未通过，阶段未提交；R5/R6未开始；真实配置未读取，真实请求0/80、剩余80。**


后续浏览器 `browser-r4-browser-20261010-cffa7f7a131f4ef4a8cd84c41bf6bcdd` 实际仍exit1（约109.434秒），共10次本地模拟请求。父报告取消、严格读取及实际CLI身份取证通过，续跑POST202后子job/report/narrative均有HTTP200且无JS/网络错误；runner却要求默认折叠技术详情中的run_id进入可见innerText，45秒等待失败。本轮没有超时后URL/DOM/截图，原因只作为源码与现有请求支持的推断，不能补造实际子页面取证。完整子视口、父归档不变、三下载、离线HTML、PDF/bbox、抽屉和历史尚未到达；所有所属进程已确认退出。新候选保留精确child URL、隐藏技术code的精确textContent身份及可见报告检查，失败立即取现场；待标注角色文案先打开其同源details再验可见内容，生产页面未改。

`runtime-acceptance-20261010T041132932Z-d4739b7e24984ed7b2c5c6f238569364` 仍exit1：已完成三case计27次，但另有未形成case-result的业务漂移子任务4次（发现1、429/500归并2、评审1），原始HTTP与只读SQLite总计31、四gate全关闭。严格父绑定修正已生效；漂移CLI正常exit0，job与封存快照最终为completed_with_issues、无error，失败点是GET状态行五秒读取超时，与整体65秒任务等待不同。最后成功GET为2.694秒、当时仍running且未提供可读快照。没有API stderr或最终source-after-run，不据终态补写当时测试通过；已安排独立副本上的有界只读诊断，先区分投影耗时与服务停滞，再决定必要修复，见[浏览器和API查询失败审阅](../artifacts/evaluations/annual-recovery-controller-20261006/r4-browser-and-api-poll-failed-root-review-20261010-47.json)。保护清单6233文件中6232相同，唯一已记录R3 Vite追加日志仍为原598字节SHA，无新增历史改写。

主控随后完成最后三份已落盘产物的只读核对：（1）受影响回归与前端构建 [`affected-regression-20261010T041718Z-532a1162a60c`](../artifacts/evaluations/annual-recovery-r4-20261010/affected-regression-20261010T041718Z-532a1162a60c/run-result.json) 按指定命令实际运行 `pytest` exit 0，251 项中 **245 passed、6 skipped、0 failures、0 errors**（JUnit 112.142 秒，外层 112.695 秒；6 项跳过为 5 个需专用 `FINTRACE_R4_*` 变量的 harness 入口及 1 个 Windows 符号链接测试，不计入通过数），`npm run typecheck`（2.933 秒）与 `npm run build`（3.028 秒）均 exit 0；114 个冻结文件（81 运行源码、10 业务配置、23 前端文件，manifest `38592c3737fb1a36bbb0be9c9c3ac193a99b1c95f2050883bfb7e465a14fd201`）及 9 个选定测试文件运行前后零变化，独立重算 `junit.xml`、`pytest.stdout.txt`、`npm-typecheck.stdout.txt`、`npm-build.stdout.txt` 与 [`independent-freeze-check.json`](../artifacts/evaluations/annual-recovery-r4-20261010/affected-regression-20261010T041718Z-532a1162a60c/independent-freeze-check.json) 哈希全部吻合。（2）只读克隆诊断 `artifacts/tmp/annual-api-get-projection-diagnostic-20261010T041825361Z-25d29f048db54e6b98c152f1e9630b74` 证实单次 `GET /v1/annual-analysis-jobs/{job_id}` 在锁外串行执行 4 次严格 `load_annual_report`（`result_availability`、`recovery_progress`、`discovery_recovery`、`analysis_budget`），冷/热轮单次 GET 分别耗时 **4.058 秒 / 3.530 秒**，逼近测试轮询器固定的 `urlopen(timeout=5)`；原失败目录 7 份关键证据哈希前后不变。（3）浏览器尝试 [`browser-r4-browser-20261010-d19273d8be104d3189351faaa198a653`](../artifacts/evaluations/annual-recovery-r4-20261010/browser-r4-browser-20261010-d19273d8be104d3189351faaa198a653/browser-result.json) 的超时截图 [`resume-child-report-wait-timeout.viewport.png`](../artifacts/evaluations/annual-recovery-r4-20261010/browser-r4-browser-20261010-d19273d8be104d3189351faaa198a653/resume-child-report-wait-timeout.viewport.png) 与 207023 字符捕获 DOM 证实续跑子报告 `annual-analysis-71264d19-e2d1-4924-a832-d89964f0b45f`（job `annual-job-fb5dfd6e37974bf993253775d8e5849a`）已在页面完整渲染，45 秒超时源于临时脚本 `querySelector('.annual-technical-record code')` 命中上方弃权卡片的 `abstained` 而非 `#annual-scope` 中的分析编号；未重跑的子报告三格式下载、抽屉和故障矩阵后半段不写为通过。主控据此按当前诚实边界在原分支仅暂存 R4 任务文件完成本地 commit（保留用户原有 39 行验收记录、路线文档、计划书 Word 改动与茅台 PDF 在工作区），真实模型请求累计仍为 **0/80，剩余 80**。
