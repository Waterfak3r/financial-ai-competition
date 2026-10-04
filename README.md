# FINTRACE

面向非金融上市公司文本型年报的可追溯财务异常分析。输出事实、变化、风险线索及证据；异常线索不等于确认舞弊。系统采用云端 API + 本地 Web + 本地计算，以海天味业 603288 的 2024 年报为首个已验收样例。

## 当前能力与边界

以下能力说明以 2026-10-05 当前代码基线 `07cb4e97e7b43327aa35ed7d6db260ec28fb1c59` 为准；10/03 的 `a0b5b3bc26b3004afcd359df48544adb9cba4a13` 冻结快照及其验收仍作为历史记录保留。验收证据与日期集中在 [验收记录](docs/verification-history.md)，不据旧记录推定本机依赖、配置或在线服务当前可用。

| 能力 | 状态与边界 |
| --- | --- |
| 文本 PDF 上传与解析 | 已实现；保留来源、哈希、PDF 页序号与坐标。无 OCR，不承诺通用财报抽取 |
| v2 确定性年度分析 | 已贯通提取、独立事实核验、可比性、计算与重算核验、筛查、Claim 核验和归档；M1/M2 对海天单样例验收 |
| M3 四规则筛查 | 独立试行模式不调用模型；也可在集成模式中作为独立审计层运行。海天样例四条均可计算且未触发；阈值未校准，部分语义映射只支持该样例 |
| AI 调查、年报全文发现与最终评审 | 显式 AI 年度模式保留固定候选调查，并以本次 PDF 全部可提取文字为扫描目标，在预算内按顺序分批尝试；全文发现不依赖固定候选、已核验指标或 M3。新增链路通过构造 PDF 与本地 mock connector 的 CLI/API/页面集成验收；不代表真实供应方或跨公司语义准确性 |
| Web 使用流程 | 上传/本机样例、任务阶段、活动 job_id 恢复、失败/中断重试、归档读取、已核验证据页图、本机模型设置、叙述原文定位和证据抽屉均已实现；A03–A05 最终浏览器矩阵已接受。非金融使用者的 30 秒理解试验尚未进行 |
| 年度导出 | `GET /v1/annual-analyses/{run_id}/export?format=json\|markdown\|html` 与页面下载入口已实现；专项、旧归档来源绑定、离线打印及原创样例 UI 上传/三格式下载已接受，结果见验收记录 |
| 季度分析 CLI | 独立处理营业收入、归母净利润、经营现金流三类流量指标；累计值、派生单季、期间可比性及同比/环比分层核验。原创模拟季度样例同比/环比完整通过；海天真实材料三项同比通过，三项环比因可比性证据不足弃权。当前无季度 Web/API 流程 |
| 跨公司与评测 | 三公司 24 条 provisional 事实参考与 15 条受控错误实验已运行；16 条独立核验通过、8 条证据不足，真人复核仍 pending。舞弊准确率分母为零，不能推算可靠性指标 |
| 旧年度预检 | API、页面及旧归档单独保留；坐标金额复核不等同于 v2 独立事实核验 |

当前主产品流程为年度 Web/API；季度能力是独立 CLI 切片，尚无 Web/API 接入。年报全文发现的批次、覆盖及失败状态独立于基础指标核验；模型观点始终标为未核验。决赛软件基线为 `07cb4e97e7b43327aa35ed7d6db260ec28fb1c59`；10/03 冻结快照、其历史检查和既有运行归档不被本轮改写。当前分支完整后端套件、前端 typecheck/build 与指定本地 mock 浏览器流程已分别验收；具体范围见[验收记录](docs/verification-history.md)。真实模型供应方、跨公司语义表现、非金融用户理解及另一台电脑复现仍待验收。项目无用户账户、模型训练或云部署。前端暂无独立自动化测试套件；typecheck/build 和指定浏览器演练不能写成持续自动化回归已建立。CI 尚未建立。

2026-10-04 在冻结快照之后，开发工作区追加了年报叙述片段的只读阅读上下文和更清楚的定位说明。该补充从已校验来源 PDF 重建同页相邻正文，仅用于页面阅读；归档原引用、核验逻辑、模型输入和导出字段不变。它是独立于 2026-10-03 已交付快照的后续修改，不代表重建或替换既有 ZIP/PDF；本次测试、浏览器视口和原文来源证据单独记在[验收记录](docs/verification-history.md)。

## 决赛交付入口

- 可编辑技术报告：[Markdown](submission/final/FINTRACE-决赛技术报告.md)；排版版：[PDF](submission/final/FINTRACE-决赛技术报告.pdf)。
- 本机验收步骤、运行记录字段与未完成的外部关卡：[运行与交付验收指南](submission/final/运行与交付验收指南.md)。
- 原创样例、结构化分析与复现边界：[正式证据索引](submission/final/evidence/README.md)。
- 年度 JSON/Markdown/离线 HTML 下载行为与来源绑定契约：[年度导出说明](docs/annual-report-export.md)。
- 季度单季派生、同比/环比公式及运行命令：[季度分析 CLI 说明](docs/quarterly-analysis.md)。该切片独立于年度 Web/API。
- C01–C03 交付顺序与待完成关卡：[产品交付路线](docs/product-delivery-roadmap.md)。

## 启动

没有开发环境的 Windows 队友使用已构建的内部便携包：将 `artifacts/packages/<package_id>/<package_id>.zip` 完整解压到可写位置，然后双击包根目录的“启动 FINTRACE.bat”。浏览器会自动打开历史演示页面；无需安装 Python、Node.js 或项目依赖。便携包使用范围、配置和边界见[便携包说明](docs/portable-package.md)。仓库根目录 `start.bat` 仍是开发启动入口，不用于便携包。

需要 Python 3.11+、Node.js 和已声明依赖；当前依赖约束以 [后端清单](backend/pyproject.toml)、[前端清单](frontend/package.json)和锁文件为准。版本来源登记见 [dependencies.md](docs/dependencies.md)。安装由操作者按本次环境需要执行，启动脚本不会自行安装：

```powershell
python -m pip install -e ".\backend[test]"
cd frontend
npm ci
```

需要 AI 调查或 LangGraph mock 测试时，在仓库根目录安装可选依赖：

```powershell
python -m pip install -e ".\backend[test,agents]"
```

Windows 可在根目录双击 `start.bat`。脚本优先使用已激活环境、根目录 `.venv`，再使用 PATH 中 Python；启动本机后端与前端并打开页面，日志进入 `artifacts/tmp/startup-<id>/`。它不读取模型凭据或调用模型。已识别的本项目服务可复用；未知端口占用会失败，不停止未知进程。关闭启动窗口后后台服务仍运行，按脚本显示的 PID 停止所需进程。无浏览器启动：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start.ps1 -NoBrowser
```

手动启动，在根目录运行后端，并在另一终端进入 frontend 启动前端：

```powershell
$env:PYTHONPATH = "backend\src"
python -m uvicorn finagent.api.app:app --host 127.0.0.1 --port 8000
```

```powershell
cd frontend
npm run dev
```

打开 http://127.0.0.1:5173 。Vite 绑定 loopback，使用 strictPort，并把 `/api` 代理到本机 8000 端口。应用本身不全局强制 host；上述启动命令限制监听地址。

## 模型配置

应用不内置默认供应方或型号，连接器使用 OpenAI 兼容 Chat Completions 的纯文本共有子集：`model`、`messages`、`stream=false`。不据此承诺视觉、工具调用或流式扩展互换。

新建年度任务默认使用不调用模型的 `deterministic` 模式；显式选择模型模式并通过能力检查后才会调用配置服务。

| 使用入口 | 配置来源 |
| --- | --- |
| Web 显式 AI 分析与评审 | 优先读取根目录 `.env.model`；文件不存在时回退服务进程的完整 `MODEL_*` 环境变量 |
| CLI `--with-model` | 只读当前进程环境变量，不读取 `.env.model` 或自动加载 `.env` |
| 确定性 / 独立 M3 筛查 | 不调用模型；Web 子进程不携带 `MODEL_*` 凭据 |
| `model_investigation` | 显式配置后执行固定候选调查、同源原 PDF 全文发现和最终评审；全文发现以本份 PDF 全部可提取文字为目标，在预算上限内按全文顺序分批尝试，失败与遗漏独立记录 |
| `integrated_analysis` | M3 独立使用本地规则；AI 分别执行固定候选调查、同源原 PDF 全文发现和最终评审。全文发现以本份 PDF 全部可提取文字为目标，在预算上限内按全文顺序分批尝试，失败与遗漏独立记录 |

上表的宿主环境回退适用于开发服务。Windows 便携启动会清除进程中的 `MODEL_*`，只读取便携包根目录的 `.env.model`，不会继承启动它的 shell 环境；详见[便携包说明](docs/portable-package.md)。

环境变量为 `MODEL_BASE_URL`、`MODEL_API_KEY`、`MODEL_NAME`。连接器在根地址末尾追加 `/chat/completions`，不要重复填写该路径。`.env.example` 仅提供空值。供应方的具体端点、地域及型号以该供应方配置说明为准。

“设置”页可保存、清除本机配置或按草稿测试连接：

- `.env.model` 以明文保存并被 Git 忽略，写入尽可能限制文件权限；损坏配置失败关闭，清除后回退环境配置。
- 已保存密钥不回显、不写浏览器存储。输入密钥只在表单中暂存，经本机代理交给后端。
- 服务地址不变且已有有效密钥时，空密钥可沿用，并保存为完整本机副本；更换地址须重新填写。
- 测试连接发送固定短消息，超时 8 秒；不发送年报、不保存草稿，会实际调用所填服务。配置完整不代表连接成功。
- `GET /v1/annual-analysis-capabilities` 只报告就绪状态，并通过可选 `annual_discovery_supported` 标示当前后端是否包含全文发现。旧后端缺少或返回 false 时，新全文 AI 提交会禁用；加载新代码后需由操作者重启后端。环境是否 ready 应当现场读取，不能照抄历史 not_configured 记录。

确定性与独立 M3 模式不调用模型。显式 AI 年度模式会以选中 PDF 的全部可提取文字为目标，在预算内将文本分批发往当前配置服务；单批失败后仍尝试后续批次，并独立记录失败和遗漏。费用与等待时间随文本量和调用次数增加。原 PDF、运行归档和审计记录保存在本地，配置服务会收到批次文本及有限评审输入。扫描件/OCR 不在范围内；预算、覆盖、遗漏与导出限制见[全文发现说明](docs/annual-discovery.md)和[数据规则](docs/data-policy.md)。若新 AI 模式因后端缺少 `annual_discovery_supported` 而禁用，需要加载新代码后由操作者重启后端；本轮装配没有重启现有服务。现场联网是否允许应按组委会环境说明确认；历史回放不能替代在线处理验收，mock 验收也不代表真实供应方表现。具体路由边界见 [架构](docs/architecture.md)。

## CLI 与归档

在根目录运行海天确定性年度分析，前提是本机已有所列原文：

```powershell
python scripts/analyze_annual.py --source-pdf data/raw/603288/2024/cninfo-1222994233/1222994233.PDF --company-id 603288 --report-year 2024 --document-id cninfo-1222994233
```

在同一命令末尾添加 `--with-m3-screening` 可运行确定性四规则试行；添加 `--with-model --source-record data/raw/603288/2024/cninfo-1222994233/source.json` 可运行旧固定候选调查、独立全文发现与最终评审。二者同时添加即在同一个 run/report 中分别执行 M3 审计和完整 AI 模式；M3 不进入模型输入，最终评审只接收基础报告与有界发现项，不重新读取全文。模型选项需要完整模型环境变量和 agents 依赖，启用后会实际发送全文批次至配置服务。Web 对应模式为 `integrated_analysis`。

运行记录与报告共享 run_id，分别位于 `artifacts/runs/<run_id>/` 和 `artifacts/reports/<run_id>/`；任务状态位于 `artifacts/annual-analysis-jobs/<job_id>/`。CLI 归档含输入 PDF 副本，原文未确认再分发许可，不能直接作为公开演示包上传。

运行原创季度样例的 Q2 同比/环比：

```powershell
python scripts/analyze_quarterly.py --year 2024 --quarter Q2
```

该命令读取 `data/samples/quarterly/` 中已登记的原创模拟材料，不调用模型；当前季度能力限营业收入、归母净利润和经营活动现金流量净额，运行公式、弃权条件与历史结果见[季度分析 CLI 说明](docs/quarterly-analysis.md)。

三份公开年报与季度真实材料的来源、哈希、用途与许可边界见 [数据规则](docs/data-policy.md)。原创年度、季度模拟 PDF 已登记在 `data/samples/`，可在保留模拟提示与使用条件时随比赛材料/团队演示分发；真实 PDF、整页图和原文归档许可未确认，不随提交件提供。克隆后如需真实案例应按官方来源自行取得并校验哈希。

旧流程与低层 API、类型约定见 [架构](docs/architecture.md)和[旧字段审计](docs/current-schema-audit.md)，不要用旧流程结果冒充 v2 核验。

## 检查与复现

在根目录检查后端：

```powershell
python -m pytest -c backend/pyproject.toml
```

在 frontend 检查类型与构建：

```powershell
npm run typecheck
npm run build
```

测试额外依赖包含 pytest 和 httpx；部分集成测试需要真实年报、历史归档或 agents 可选依赖。检查跳过原因和数据准备条件，不能把本机带资料的通过数当作干净克隆的通过保证。

M4 受控消融可用 `python scripts/evaluate_verification_ablation.py` 重跑，需按 `evaluation/verification_ablation_cases_v1.json` 准备原文及其依赖的归档输入。它是单公司、受控错误基线，不是舞弊识别准确率。历史执行结果见 [验收记录](docs/verification-history.md)；本轮文档整理没有重跑业务测试。

## 文档与文件归属

| 位置 | 负责内容 |
| --- | --- |
| [AGENTS.md](AGENTS.md) | 协作、开工阅读、文件保护与收尾规则 |
| [docs/architecture.md](docs/architecture.md) | 模块职责、模式、接口与核验契约 |
| [docs/data-policy.md](docs/data-policy.md) | 来源、数据隔离、证据与凭据约束 |
| [docs/roadmap.md](docs/roadmap.md) | M0–M5 工程阶段与完成条件 |
| [docs/product-delivery-roadmap.md](docs/product-delivery-roadmap.md) | 从可录原型到比赛成品的产品任务与验收 |
| [docs/development.md](docs/development.md) | 任务交接与兼容方式 |
| [docs/verification-history.md](docs/verification-history.md) | 带日期的既往验收与记录索引 |
| [docs/documentation-review.md](docs/documentation-review.md) | 本次 Markdown 审阅、已改事项与维护方法 |
| frontend / backend / config | 前后端工程、程序提示词与规则配置 |
| data / artifacts / evaluation / tests | 原始与处理后数据、运行产物、隔离标签与测试 |
| scripts | 可复用脚本；临时内容进入所属模块 tmp/<task_id>/ |
| submission | 计划书、视频交接与决赛材料，按阶段归档 |

初赛计划书及审阅材料由计划书负责人在[计划书目录](submission/proposal/)维护；本段不指向某个可编辑稿或正式提交版。[视频分镜与交接](submission/video/video-handoff.md)由视频负责人承接；本轮软件原型的本机复现、运行选择、逐步录屏路径和素材边界见[原型录屏交接](submission/video/prototype-recording-handoff.md)。软件可录原型冻结提交为 `682471b71b2ee1b235abba317ab599caa42091f0`；软件侧交付版本、真实运行和操作证据，视频上传由团队指定提交负责人。

`coop.md` 与 `tem/` 的旧资料按历史参考保留，不覆盖当前任务。业务能力状态只在本节简述、技术里程碑中细化；测试流水不再多处追加。
