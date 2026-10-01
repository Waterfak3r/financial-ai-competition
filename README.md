# FINTRACE

面向非金融上市公司文本型年报的可追溯财务异常分析。输出事实、变化、风险线索及证据；异常线索不等于确认舞弊。系统采用云端 API + 本地 Web + 本地计算，以海天味业 603288 的 2024 年报为首个已验收样例。

## 当前能力与边界

以下为 2026-10-01 文档整理时的实现状态。验收证据与日期集中在 [验收记录](docs/verification-history.md)，不据旧记录推定本机依赖、配置或在线服务当前可用。

| 能力 | 状态与边界 |
| --- | --- |
| 文本 PDF 上传与解析 | 已实现；保留来源、哈希、PDF 页序号与坐标。无 OCR，不承诺通用财报抽取 |
| v2 确定性年度分析 | 已贯通提取、独立事实核验、可比性、计算与重算核验、筛查、Claim 核验和归档；M1/M2 对海天单样例验收 |
| M3 四规则筛查 | 显式试行模式；不调用模型。海天样例四条均可计算且未触发；阈值未校准，部分语义映射只支持该样例 |
| AI 调查与最终评审 | CLI 和 Web 入口已实现，须模型配置与 LangGraph 就绪。候选调查有历史在线单例；后来新增的最终评审已有 mock 检查，真实在线验收待完成 |
| Web 使用流程 | 上传/本机样例、任务阶段、恢复活动 job_id、失败/中断重试、归档读取、已核验证据页图、本机模型设置均已实现 |
| 历史与导出 | 支持按 run_id 回看；尚无产品级任务历史列表、完整 URL 定位或产品级报告导出 |
| 跨公司与评测 | 茅台/五粮液有 provisional 事实参考；单公司七案例消融基线已建立；多公司人工 golden 和完整可靠性评测未完成 |
| 旧年度预检 | API、页面及旧归档单独保留；坐标金额复核不等同于 v2 独立事实核验 |

当前以年度比较为中心；季度、半年报与环比属于后续开发。项目无用户账户、模型训练或云部署。前端暂无独立自动化测试套件；已有 typecheck/build 与历史浏览器检查，不能把这些写成持续自动化回归已建立。CI 尚未建立。

## 启动

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

| 使用入口 | 配置来源 |
| --- | --- |
| Web 显式 AI 分析与评审 | 优先读取根目录 `.env.model`；文件不存在时回退服务进程的完整 `MODEL_*` 环境变量 |
| CLI `--with-model` | 只读当前进程环境变量，不读取 `.env.model` 或自动加载 `.env` |
| 确定性 / M3 筛查 | 不调用模型；Web 子进程不携带 `MODEL_*` 凭据 |

环境变量为 `MODEL_BASE_URL`、`MODEL_API_KEY`、`MODEL_NAME`。连接器在根地址末尾追加 `/chat/completions`，不要重复填写该路径。`.env.example` 仅提供空值。供应方的具体端点、地域及型号以该供应方配置说明为准。

“设置”页可保存、清除本机配置或按草稿测试连接：

- `.env.model` 以明文保存并被 Git 忽略，写入尽可能限制文件权限；损坏配置失败关闭，清除后回退环境配置。
- 已保存密钥不回显、不写浏览器存储。输入密钥只在表单中暂存，经本机代理交给后端。
- 服务地址不变且已有有效密钥时，空密钥可沿用，并保存为完整本机副本；更换地址须重新填写。
- 测试连接发送固定短消息，超时 8 秒；不发送年报、不保存草稿，会实际调用所填服务。配置完整不代表连接成功。
- `GET /v1/annual-analysis-capabilities` 只报告就绪状态。环境是否 ready 应当现场读取，不能照抄历史 not_configured 记录。

云端分析只发送任务所需且允许外发的片段，原文、计算与归档保存在本地。现场联网是否允许应按组委会环境说明确认；历史回放不能替代在线处理验收。安全与证据约束见 [数据规则](docs/data-policy.md)，具体路由边界见 [架构](docs/architecture.md)。

## CLI 与归档

在根目录运行海天确定性年度分析，前提是本机已有所列原文：

```powershell
python scripts/analyze_annual.py --source-pdf data/raw/603288/2024/cninfo-1222994233/1222994233.PDF --company-id 603288 --report-year 2024 --document-id cninfo-1222994233
```

在同一命令末尾添加 `--with-m3-screening` 可运行确定性四规则试行；添加 `--with-model --source-record data/raw/603288/2024/cninfo-1222994233/source.json` 可运行模型调查与最终评审。后者需要完整模型环境变量和 agents 依赖。两组选项不能在同次运行组合，模型调用会实际请求配置的服务。

运行记录与报告共享 run_id，分别位于 `artifacts/runs/<run_id>/` 和 `artifacts/reports/<run_id>/`；任务状态位于 `artifacts/annual-analysis-jobs/<job_id>/`。CLI 归档含输入 PDF 副本，原文未确认再分发许可，不能直接作为公开演示包上传。

三份公开年报的来源、哈希、用途与许可边界见 [数据规则](docs/data-policy.md)。原始 PDF、解析数据和运行产物默认不随 Git 分发；克隆后需准备原文。`data/samples/` 尚未形成可交付演示包。没有数据时不能把样例启动失败写成通过。

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

初赛材料已有 [计划书修订稿](submission/proposal/fintrace-project-proposal.docx)与[审阅说明](submission/proposal/proposal-review-notes.md)，不代表正式 PDF 已提交。[视频分镜与交接](submission/video/video-handoff.md)由视频负责人承接，软件侧交付可录系统与证据；上传由团队指定提交负责人。

`coop.md` 与 `tem/` 的旧资料按历史参考保留，不覆盖当前任务。业务能力状态只在本节简述、技术里程碑中细化；测试流水不再多处追加。
