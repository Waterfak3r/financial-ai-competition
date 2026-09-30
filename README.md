# FINTRACE

非金融上市公司文本型年报的可追溯异常分析。当前样例是海天味业 603288 的 2024 年报。异常线索不是确认舞弊。

旧年度预检 API/页面与 v2 正式年度分析是两条分开的流程。旧预检继续使用旧事实、同比、坐标金额复核和旧版确定性 screening；上传与创建预检分步。v2 CLI `scripts/analyze_annual.py` 已完成海天 603288 的 2024 Golden Path 并归档报告：8 条事实、8 项计算和 16 条确定性 Claim 均通过核验，形成 2 条候选信号。M1（海天样例）和 M2（单样例确定性 Golden Path）已验收；不据此声称跨公司泛化。年度可比性依据来自同一 PDF 第 116、163、197 页，由当前进程签发 proof；序列化结果只供审计，且未与此前已披露的 2023 年报做独立勾稽。v2 前端现可上传 PDF 或运行本机海天样例，启动确定性分析或明确标为试行的 M3 四规则筛查，查看任务阶段并自动读取成功任务对应的归档报告和已核验证据页图；也保留按 run_id 回看旧归档。在线模型调查启动不提供，产品级导出尚未实现。M3 调查工作流已编码并接入 CLI 的可选 `--with-model` 路径，LangGraph 1.2.12 的本地 StateGraph 模拟集成验收已通过。2026-09-28 在用户授权下，使用 CC Switch `claude/DeepSeek` 官方直连完成海天 603288 2024 年报单例在线验收：修复前运行 `annual-analysis-af95416a-272f-4045-b0fd-633c7090b4cb` 的两条原始回答均为有效 JSON 且含同源引用，但编排把两次调用集中在首个候选并因 `request_more_context=true` 丢弃有效解释；修复后运行 `annual-analysis-a32c16a7-00da-48c5-a97d-59ee284904df` 以两次调用为两个候选各留一次，归档两条未核实解释，GET 读取保留调查字段。评估记录 `artifacts/evaluations/deepseek-live-m3-603288-20260928-a32c16a7/` 记录了质量、审计和用量；这是单例，不是人工 golden 或独立核验，也不代表跨公司能力，默认供应商仍未选定。五类 M3 字段的 v2 提取与独立核验以及四规则模块已由 CLI 显式可选路径 `--with-m3-screening` 串联；海天 603288 2024 正式运行 `annual-analysis-5e7079fe-9ac9-4e96-85fa-1898f9177d21` 已归档 10 条新事实、8 条旧事实显式单位副本和 18 条 M3 直接核验结果，四条规则均可计算且总分为 0。该路径不改变默认 M2 或旧筛查；`non_recurring_total` 显式单位副本仅作审计，不进入规则。规则版本为 `m3-four-annual-rules-v1.0.0-trial`，每条触发计 25 分；任何一条无法计算时总分弃权。扣非归母净利润来自第 7 页“主要会计数据”，保持 `statement_type=key_financial_data`、`scope=unknown`。规则二仅对海天 603288 2024 年报第 7 页版式建立专属语义 proof，重读该页并逐年交叉核对“归属于上市公司股东的净利润”与合并利润表归母净利润；不代表跨公司通用映射。该 proof 和年度可比性 proof 由当前进程签发，序列化只供审计；筛查调用约定要求传入同一进程 `verify_financial_fact` 的直接结果。`VerificationResult` 不是签名 proof，反序列化 JSON 或手工重建对象不能作为核验凭据；提取和独立核验的 evidence ID 必须不相交。试行阈值仍待隔离评测验证，异常线索不构成舞弊结论。茅台、五粮液跨公司案例及 CLI 结果已归档，但案例仍是待人工审阅的 provisional 财务事实参考；五粮液修复后的正式运行 `annual-analysis-956db60d-6595-4d7c-8277-07d1d7e21326` 显示可比性证据不足、8 条事实币种未明确、8 项计算未确认、2 条筛查均弃权。M4 的 3–5 家公司人工 golden 与完整多公司消融评测仍未完成；单公司消融基线见下方评测记录。默认供应商仍未选定；模型输出不是人工 golden 或独立核验。

样例原文 `data/raw/603288/2024/cninfo-1222994233/1222994233.PDF`。最新 screening 运行 `artifacts/runs/annual-precheck-603288-2024-20260925-105059/`。启动：`uvicorn finagent.api.app:app --host 127.0.0.1 --port 8000`，再到 `frontend/` 执行 `npm run dev`，打开 http://127.0.0.1:5173 。

2026-09-28 建立一家公司、七个已知标签案例的 M4 有/无独立核验消融基线，运行 ID `verification-ablation-m4-20260928T072029Z-825a8c4b`，可从仓库根目录执行 `python scripts/evaluate_verification_ablation.py` 重跑。海天 603288 2024 年报 PDF 直接参考行上，独立核验支路正确接受 2/2 个 clean 对照并拒绝 5/5 个合成错误；无核验支路接受 5/5 个错误。茅台与五粮液只做 provisional 状态对照（分别为 `verified`、`insufficient_evidence`），未计入准确率分母。clean 真值由 agent 按原 PDF 转录、尚未经人工审核；本基线不等于 3–5 家公司的人工 golden 或完整 M4 验收。详情见 `artifacts/evaluations/verification-ablation-m4-20260928T072029Z-825a8c4b/` 和 `evaluation/verification_ablation_cases_v1.json`。

规则见 [AGENTS.md](AGENTS.md)。协作、架构、数据、字段审计和路线见 [docs/development.md](docs/development.md)、[docs/architecture.md](docs/architecture.md)、[docs/data-policy.md](docs/data-policy.md)、[docs/current-schema-audit.md](docs/current-schema-audit.md)、[docs/roadmap.md](docs/roadmap.md)。下文保留安装、接口和历史运行。

方案材料位于 `submission/proposal/`：[原始大纲](submission/proposal/多智能体协同财务欺诈识别方案总结大纲.docx)保持原样；[修订大纲](submission/proposal/多智能体协同财务欺诈识别方案总结大纲_修订版.docx)保留 13 部分结构，明确第一版仅采用 LangGraph 编排、技术范围、独立核验、基础财报分析与评测方案。其中规则阈值和验收安排属于拟实施方案，尚无实验结果。

## 技术方向

| 部分 | 约定 |
| --- | --- |
| 前端 | React + TypeScript + Vite |
| 后端 | Python + FastAPI |
| 展示方式 | 本机浏览器访问 localhost Web |
| 本机开发地址 | 前端开发服务 http://127.0.0.1:5173，后端按文档命令 http://127.0.0.1:8000 |
| 模型 | 已有供应方中立的 Chat Completions 文本连接器。默认供应方和型号未选定，由环境变量配置 |
| 编排 | 第一版采用 LangGraph，不叠加其他智能体协作框架 |
| 计算与核验 | 本地 Python 执行财务公式、统计筛查和数值核验，保留原文依据 |
| 当前依赖状态 | `backend/pyproject.toml` 已声明 PyMuPDF、FastAPI 和 Uvicorn；`langgraph>=1.2,<1.3` 通过可选的 `agents` extra 声明，本机安装版本为 1.2.12。前端依赖已声明，本机 `npm install` 已生成 `frontend/package-lock.json`；`node_modules` 不纳入 Git |

第一版路线确定为“云端 API + 本地轻量 Web + 本地统计计算”，不要求本地 GPU，也不纳入模型本地部署或训练。原始财报、索引、计算和运行记录保存在本地，模型调用仅发送本任务所需且允许外发的片段。云端 API 需要网络；受控运行限制资料范围与外部连接，现场是否允许模型联网仍需依据组委会环境说明核实。历史回放与在线重新运行明确区分，断网回放不能替代实时处理验收。

## 目录导航

```text
.
├── AGENTS.md          # AI 协作与文件管理规则
├── README.md          # 项目入口与当前状态
├── 比赛通知.txt        # 原始比赛资料
├── .gitignore         # 本地文件与生成产物的忽略规则
├── .env.example       # 模型连接配置项
├── docs/              # 架构、数据与证据管理说明
├── frontend/          # 本地 Web 前端
├── backend/           # 财报分析服务与智能体
├── config/            # 提示词与财务规则配置
├── data/              # 原始资料、处理结果和演示样例
├── artifacts/         # 运行记录、报告、评测结果和公共 tmp/
├── evaluation/        # 评测案例、标注与对照基线
├── tests/             # 单元、集成和完整流程测试
├── scripts/           # 可复用工程脚本
└── submission/        # 初赛计划书、视频和决赛材料
```

前端保留旧年度预检页面，并新增 v2 正式年度分析流程。旧创建页上传文本 PDF 后仍需另点创建预检；旧结果页可展示运行记录中的 screening。v2 页面可上传 PDF、启动正式任务、查看状态、从本机样例运行，并读取归档报告与服务端按独立核验证据生成的 PDF 页图；还可按 run_id 回看既有报告。页面不提供在线模型调查启动或产品级报告导出。后端按财报解析、检索、智能体、财务计算、核验和报告等职责划分，具体边界见架构文档。

## 文件存放约定

| 文件类型 | 存放位置 |
| --- | --- |
| 原始财报 | data/raw/ |
| 解析文本、表格、标准化数据与索引 | data/processed/ |
| 可交付演示样例 | data/samples/ |
| 任务执行记录和可复现中间结果 | artifacts/runs/<run_id>/ |
| 生成的分析报告 | artifacts/reports/<run_id>/ |
| 异步年度分析任务状态 | artifacts/annual-analysis-jobs/<job_id>/ |
| 评测输出 | artifacts/evaluations/<evaluation_id>/ |
| 模块内临时脚本、截图和试验结果 | 所属模块的 tmp/<task_id>/ |
| 跨模块临时内容 | artifacts/tmp/<task_id>/ |
| 可复用脚本 | scripts/ |
| 评测案例和标签 | evaluation/cases/ |
| 比赛材料 | submission/ 对应阶段目录 |

tmp/ 按需创建，正式功能不能依赖其中的文件。已有 tmp/、任务子目录及其中临时文件默认保留，任务结束不主动清理；用户明确要求清理时，按 AGENTS.md 核对归属和路径后执行。正式运行证据仍需归档并保留。

正式空目录通过 .gitkeep 保留。原始数据、处理后数据及运行产物默认忽略；演示样例、评测案例和比赛材料可按来源与实际提交需要纳入版本管理。当前已初始化 Git 仓库。

## 配置示例

`.env.example` 预留三个空值，程序不在示例文件里选定供应方、型号或密钥：

- `MODEL_BASE_URL`：Chat Completions 服务根地址。连接器会在末尾追加 `/chat/completions`，根地址本身不要写这个路径。
- `MODEL_API_KEY`：访问密钥。可以写在未提交的本地 `.env` 里私下保存。错误信息不会回显密钥或完整请求正文。
- `MODEL_NAME`：请求体里的 `model`。

`load_model_settings()` 只读取进程环境变量 `os.environ`，没有 `.env` 自动加载器。把这三项写进本地 `.env` 并不会自动生效；调用前需由用户或后续启动器先载入进程环境。`complete_chat()` 只发送三家共同的字段 `model`、`messages` 和 `stream=false`，并接受 `system`、`user`、`assistant` 纯文本。默认超时 60 秒。这是 Chat Completions 的共有子集，不表示各家的工具调用、视觉、流式或其他扩展可以互换。

端点示例来自官方文档，不是本项目已经选定的服务：

| 供应方 | 根地址示例 | 文档 |
| --- | --- | --- |
| Qwen 华北 2（北京） | `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` | [阿里云兼容说明](https://help.aliyun.com/en/model-studio/compatibility-of-openai-with-dashscope) |
| Qwen 新加坡 | `https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1` | 同上 |
| Qwen 美国（弗吉尼亚） | `https://dashscope-us.aliyuncs.com/compatible-mode/v1` | 同上 |
| DeepSeek | `https://api.deepseek.com` | [DeepSeek API 文档](https://api-docs.deepseek.com/zh-cn/) |
| OpenAI | `https://api.openai.com/v1` | [OpenAI Chat API](https://developers.openai.com/api/reference/resources/chat) |

北京和新加坡使用当前兼容文档推荐的工作空间专属域名。`{WorkspaceId}` 换成该地域的业务空间 ID，`MODEL_API_KEY` 必须属于同一地域。美国（弗吉尼亚）使用上表中的官方地址。

连接器不会自动发送整份 PDF。M3 调查图的代码会在 `scripts/analyze_annual.py` 显式指定 `--with-model` 时调用经审计连接器；默认确定性 CLI 与旧预检均不调用模型。LangGraph 1.2.12 已安装，基于本地模拟 HTTP 服务的 StateGraph 集成测试 5 项通过；已完成一次单例在线调用验收（评估 ID：deepseek-live-m3-603288-20260928-a32c16a7）。

`audited_complete_chat(messages, settings, run_dir=..., evidence_refs=[{"document_id": "doc-1", "page": 12}], prompt_version="prompt-v1")` 包装上述连接器。`run_dir` 必须是本仓库 `artifacts/runs` 的直接子目录。每次调用新建 UUID 子目录，联网前写入 `request.json`（`status` 为 `started`），成功再写 `response.json`，失败写 `failure.json` 且只含安全类别。记录包含 UTC 起止时间。落盘字符串会去掉 `MODEL_API_KEY`，不写 `base_url`、请求头或异常原文。响应文件写失败会抛出 `AuditPersistError`，不返回成功。M3 可选调查路径已接入该包装器；旧年度预检不调用它。已完成一次单例在线调用验收，详情见评估 ID：deepseek-live-m3-603288-20260928-a32c16a7。

## 已实现：文本型 PDF 逐页解析

`finagent.ingestion.parse_text_pdf` 读取文本型 PDF，返回逐页文字块。结果包含文档标识、原始文件 SHA256、文件名、PDF 1-based 页序号、块文字和页面坐标，可用 `to_json()` 序列化。函数只在内存中打开文件字节，不修改原 PDF，也不写入 `data/processed/`。

文字块边界框使用未旋转页面坐标：原点在左上角，x 向右、y 向下，单位为 PDF point（1/72 英寸）。PyMuPDF 提取文字时按 0 度旋转计算边界框。结果里的页宽和页高来自当前 `page.rect`；页面旋转 90 或 270 度时，显示宽高与未旋转页面对调，不能拿这组宽高去套文字块坐标。

空白页、只有图片或矢量图形的页、以及提取结果只有空白的页，状态为 `no_extractable_text`，文字块为空。本增量不执行 OCR，也不编造文字。页内同时有文字和图片时，只返回文字块，并注明图片未做 OCR。加密或损坏的 PDF 会报错，不会被当成空白页。

在解析结果之上，旧流程可提取四项年度指标并做确定性同比；这不是通用财报抽取。字段提取命令本身不复核引用坐标里的原文金额。旧年度预检在哈希一致后从原始 PDF 引用坐标重读金额并复核单位换算：`POST /v1/annual-prechecks` 创建旧预检，`GET /v1/annual-prechecks/{run_id}` 读取旧预检。`POST /v1/text-pdf-uploads` 接收 multipart PDF、`company_id` 和 `report_year`，在新的 `data/raw/<company>/<year>/<document_id>/` 保存 `source.pdf` 与 `source.json`，并在平行 `data/processed` 路径保存解析 JSON，上限 32 MiB。上传来源记录注明公司和年度来自上传请求、未独立核实 PDF 内容；接口不覆盖已有资料、不调用模型，也不自动创建预检。

v2 确定性年度分析仍可由 `scripts/analyze_annual.py` 直接启动；新增 `POST /v1/annual-analysis-jobs` 与 `GET /v1/annual-analysis-jobs/{job_id}`，以已存 PDF 的 `source_pdf_path`、`company_id`、`report_year`、`document_id`、`sha256` 和 `mode` 启动/查询任务。模式支持 `deterministic` 与 `m3_screening`；`model_investigation` 返回 501，不读取模型密钥或调用云端模型。任务状态包括 `queued`、`running`、`completed`、`completed_with_issues`、`failed` 和 `interrupted`，成功终态返回已有报告 API 的 `run_id` 与 `result_url`。本地单进程最多一个运行任务并保留三个排队名额；服务重启会把残留任务标记为 `interrupted`。请求会核对 `data/raw` 路径、来源目录、`source.json` 身份和 PDF SHA256。后端的 `GET /v1/annual-analyses/{run_id}` 仍只读取已归档报告；`GET /v1/annual-analyses/{run_id}/evidence/{evidence_id}/preview.png` 只生成报告中已核验事实引用的证据页图。前端“年度分析”页已接入上传、本机样例启动、任务阶段与失败/中断状态、浏览器存储中的 job_id 恢复和重试；成功后自动读取同一 run_id 的报告与证据，也保留按 run_id 回看归档。该页面提供 deterministic 和显式试行的 M3 四规则筛查；HTTP 模型调查入口不支持，因此不提供在线模型启动选项。旧预检页面、类型及数据结构保持独立。M3 年报调查图、局部检索与经审计调用代码已实现，CLI 通过可选 `--with-model` 接入；LangGraph 1.2.12 的本地模拟集成测试已运行通过，已完成一次单例在线调用验收（评估 ID：deepseek-live-m3-603288-20260928-a32c16a7）。

2026-09-28 已通过单个 FastAPI app 对海天 603288 2024 年报完成 deterministic HTTP 联通验收：任务 `annual-job-09f7cbdc377345e2a11da516b4277a68` 从 `queued` 经实际阶段到 `completed`，归档 `run_id=annual-analysis-979a791e-9902-43c0-8173-d20f4bf486fc`，随后 GET 报告返回 200。该次未调用在线模型。

### 安装、调用与测试

运行依赖在 `backend/pyproject.toml` 中声明：PyMuPDF、FastAPI 和 Uvicorn。pytest 与 httpx 是 `test` 可选依赖；LangGraph 是单独的 `agents` 可选依赖。仓库不代为安装。在仓库根目录执行：

```powershell
python -m pip install -e ".\backend[test]"
python -m pytest -c backend/pyproject.toml
```

`backend[test]` 带上引号，是因为 PowerShell 会把方括号当成通配符。这个测试额外依赖同时包含 pytest 和 httpx；只安装 pytest 时，`TestClient` 测试无法运行。若要运行 M3 的模拟 LangGraph 集成测试，需另装 `agents` extra：

```powershell
python -m pip install -e ".\backend[test,agents]"
```

该默认配置从单元与集成测试目录收集测试；建议在仓库根目录运行上面的带配置命令。`tests/__init__.py` 将项目测试目录标记为本地 Python 包，避免环境中同名第三方 `tests` 包抢占旧用例的导入。也可以进入 `backend/` 后执行 `python -m pytest`。截至 2026-09-28，本次完整后端检查为 310 passed、1 skipped，pytest exit 0；跳过项是当前环境不允许创建符号链接的安全测试。`test_chat_completion.py` 的 mock HTTP 线程在收尾时打印一次 WinError 10053，但对应测试通过。tests/integration/test_annual_investigation_mock.py 的 5 项真实 LangGraph StateGraph mock 测试均通过。未做可编辑安装时，测试配置会把 `backend/src` 加入导入路径；运行集成测试还需要本机的公开年报样例 PDF，缺失时相关测试会跳过。

本机验收环境通过保留 TLS 证书校验的清华大学 PyPI 镜像安装 LangGraph 1.2.12（https://pypi.tuna.tsinghua.edu.cn/simple）。该 wheel 的 SHA256 95403af7b510de8d79164742f71daaf866c69ca804a8cb72bef2cc1fa1ab9813 与官方 PyPI 1.2.12 发布元数据一致；官方包元数据标注 MIT 许可证。该包仅用于 agents 可选 extra 的 StateGraph 编排。

本地项目无用户注册、登录或账户管理，且不在当前项目范围。模型供应方的 API 密钥仍单独配置给 Chat Completions 连接器；预检接口不读取该密钥，也不把财报发到云端。FastAPI 应用本身不强制监听地址。按下面的启动命令，默认绑定 `127.0.0.1`。

```powershell
$env:PYTHONPATH = "backend\src"
python -m uvicorn finagent.api.app:app --host 127.0.0.1 --port 8000
```

```powershell
$body = @{
  parsed_path = "603288/2024/cninfo-1222994233/text_pdf.json"
  source_pdf_path = "603288/2024/cninfo-1222994233/1222994233.PDF"
  company_id = "603288"
  report_year = 2024
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/v1/annual-prechecks" -ContentType "application/json; charset=utf-8" -Body $body
```

`parsed_path` 相对 `data/processed`，`source_pdf_path` 相对 `data/raw`。解析后的绝对路径必须仍在对应目录内。原始 PDF 的 SHA256 必须与解析 JSON 的 `source_sha256` 一致，否则不写事实。

本机展示先启动上面的后端，再另开一个终端进入 `frontend/`。本工作区已经执行过 `npm install`，并生成 `frontend/package-lock.json`。克隆后的环境没有 `node_modules`，需要先安装再启动：

```powershell
cd frontend
npm install
npm run dev
```

Vite 按 `frontend/vite.config.ts` 只绑定 `127.0.0.1:5173`，且 `strictPort` 为 true。浏览器打开 http://127.0.0.1:5173 。旧预检页请求 `/api/v1/annual-prechecks`；v2 年度分析页通过 `/api/v1/annual-analysis-jobs` 创建/查询任务，并请求只读 `/api/v1/annual-analyses/...` 报告与证据预览路由；Vite 去掉 `/api` 前缀后转发到 http://127.0.0.1:8000 。浏览器不保存模型密钥，项目也没有用户账户。

默认入口和主导航为“分析年报”，可上传 PDF 并启动本地确定性分析或明确标为试行的 M3 筛查，支持一键运行海天样例、恢复当前 job_id 与失败/中断重试；完成后自动读取同一 run_id 的归档报告及已核验证据 PDF 页图，也可展开按 run_id 回看旧归档。候选线索与待核查事实、计算和主张旁会显示关联指标的 PDF 页序号：同源且通过独立核验的证据可打开白名单页图，来自未核实提取的位置标为“待核实定位”，无法可靠定位时显示“原文位置待定位”。这些页码只作原文线索，候选线索本身仍保持待核查。旧年度预检收在“基础预检（旧流程）”分组内，保留预检说明、新建预检和预检结果入口；创建页上传文本 PDF 后仍需另行创建预检，结果按旧响应展示事实、同比、引用页、金额复核和可选 screening。旧报告占位已从主导航移除。年度分析页没有产品级导出。海天样例 PDF 与解析 JSON 在本机对应目录并被 Git 忽略，缺失时页面会显示可读错误。

2026-09-23 与 2026-09-25 的浏览器验收记录覆盖旧预检创建、按 `run_id` 回看、错误提示和文本 PDF 上传。2026-09-27 的真实浏览器验收读取海天 2024 正式 v2 报告，并成功显示第 82 页已核验证据预览；该次只验证归档读取与证据查看。2026-09-28 后续验收覆盖了从页面启动海天样例、自动读取同一 `run_id` 报告，以及按 `run_id` 读取 M3 归档的四条规则；390px 检查无横向溢出，中文 PDF 选择控件可用键盘聚焦并显示焦点样式。该次 `npm run typecheck`、`npm run build` 均通过。当前页面没有单独的自动化测试套件，也没有产品级导出。此前旧流程构建/typecheck 记录与浏览器日志保留在 `artifacts/runs/web-qa-20260923-200437/browser/`；本轮截图保留在 `frontend/tmp/annual-job-frontend-smoke/`。

```python
from pathlib import Path
from finagent.ingestion import parse_text_pdf

parsed = parse_text_pdf(Path("data/raw/example.pdf"), document_id="example-001")
Path("data/processed/example-001.json").write_text(parsed.to_json(), encoding="utf-8")
```

上面的路径只说明调用方式。不要把整份解析 JSON 打印到终端。单元测试在临时目录里生成小型 PDF。公开年报原文的导入位置见下文，不在 `data/samples/`。

已有 `text_pdf.json` 时，可用可复用命令提取四项事实并计算同比。`--source-pdf` 会核对原始文件 SHA256；不一致就停止提取。运行目录必须新建，不能放在 `data/raw/`。

```powershell
python scripts/extract_annual_facts.py `
  --parsed data/processed/603288/2024/cninfo-1222994233/text_pdf.json `
  --company-id 603288 `
  --report-year 2024 `
  --run-root artifacts/runs `
  --source-pdf data/raw/603288/2024/cninfo-1222994233/1222994233.PDF
```

从原始文本型 PDF 执行并归档 v2 确定性年度分析，可运行：

```powershell
python scripts/analyze_annual.py `
  --source-pdf data/raw/603288/2024/cninfo-1222994233/1222994233.PDF `
  --company-id 603288 `
  --report-year 2024 `
  --document-id cninfo-1222994233
```

运行记录和报告使用同一个 `run_id`，分别写入 `artifacts/runs/<run_id>/` 与 `artifacts/reports/<run_id>/`。海天样例正式运行是 `annual-analysis-ffcd6078-a1df-416b-88b8-6774ebe66d42`，来源 PDF SHA256 为 `5a97b13534438f5e85249752ef492fbd9e23af73e67845e47bad1ab7d92e20ee`。CLI 会在运行目录中保留输入 PDF 副本；该公开年报目前仅记录为本地开发使用，未确认再分发许可，不要把原文副本或生成归档当作可公开分发样例。

四条 M3 年度规则另有显式可选的确定性路径。该路径不会读取模型密钥，生成 M3 事实核验、proof 审计副本、逐规则结果与总分，并和 M2 一起写入同一运行及报告目录；不加选项时仍只运行原 M2 路径：

```powershell
python scripts/analyze_annual.py `
  --source-pdf data/raw/603288/2024/cninfo-1222994233/1222994233.PDF `
  --company-id 603288 `
  --report-year 2024 `
  --document-id cninfo-1222994233 `
  --with-m3-screening
```

`--with-model` 与 `--with-m3-screening` 暂不支持在同一次运行组合；CLI 会明确拒绝。确定性 M3 的试行阈值仍待隔离评测，不能将筛查信号解释为舞弊结论。 最终展示版正式运行是 `annual-analysis-5e7079fe-9ac9-4e96-85fa-1898f9177d21`；更早的运行 `annual-analysis-e6d2e772-bf36-4f3b-ae95-f6ca325b9987` 保留为未做 Markdown 舍入的先行验证记录，未被覆盖。Markdown 计算值最多显示 6 位小数，JSON 保留 Decimal 原值，规则判定不使用舍入值。

M3 调查是显式可选路径。运行前需安装 `agents` extra、在当前进程配置三个 `MODEL_*` 环境变量，并为同一原文提供 `source.json`。启用 `--with-model` 后才会调用模型；该命令已于 2026-09-28 完成一次受控单例验收（运行 ID：annual-analysis-a32c16a7-00da-48c5-a97d-59ee284904df；评估 ID：deepseek-live-m3-603288-20260928-a32c16a7）：

```powershell
python scripts/analyze_annual.py `
  --source-pdf data/raw/603288/2024/cninfo-1222994233/1222994233.PDF `
  --company-id 603288 `
  --report-year 2024 `
  --document-id cninfo-1222994233 `
  --with-model `
  --source-record data/raw/603288/2024/cninfo-1222994233/source.json
```

### 第三方依赖

| 项目 | 记录 |
| --- | --- |
| 软件 | PyMuPDF |
| Python 包 | `pymupdf`，声明于 `backend/pyproject.toml`，版本下限 `pymupdf>=1.24` |
| 来源 | PyPI [pymupdf](https://pypi.org/project/pymupdf/)，上游仓库 [pymupdf/PyMuPDF](https://github.com/pymupdf/PyMuPDF)，产品页 [pymupdf.io](https://pymupdf.io/) |
| 本次测试版本 | 运行单元测试时环境中的 `pymupdf.version` 为 `('1.27.2.3', '1.27.2', None)`。下标 0 是 PyMuPDF 1.27.2.3，写入 `extractor_version`；下标 1 是所绑定的 MuPDF 1.27.2 |
| 用途 | 从文本型 PDF 提取文字块和页面坐标，并按块类型计数图片。不执行 OCR，不把图片字节放进解析结果 |
| 许可 | 官方许可页 [pymupdf.io/licensing](https://pymupdf.io/licensing) 写明 AGPLv3 或商业许可。这里只记录该页表述 |

本地预检和其测试还使用下面三个已安装的包。版本和许可证字段来自本环境 `importlib.metadata`，来源链接来自同一元数据中的项目地址。

| 软件 | 本次测试版本 | 来源 | 元数据中的许可证 | 用途 |
| --- | --- | --- | --- | --- |
| FastAPI | 0.136.3 | [fastapi.tiangolo.com](https://fastapi.tiangolo.com/)，仓库 [fastapi/fastapi](https://github.com/fastapi/fastapi) | `License-Expression`: MIT | 本地年度预检 HTTP 接口 |
| Uvicorn | 0.49.0 | [uvicorn.dev](https://uvicorn.dev/)，仓库 [Kludex/uvicorn](https://github.com/Kludex/uvicorn) | `License-Expression`: BSD-3-Clause | 按文档命令启动预检应用的 ASGI 服务器 |
| httpx | 0.28.1 | [python-httpx.org](https://www.python-httpx.org)，仓库 [encode/httpx](https://github.com/encode/httpx) | `License`: BSD-3-Clause | `TestClient` 调用本地预检测试；预检本身不靠它访问云端 |

`frontend/package.json` 声明了下面这些精确版本。2026-09-23 用 `npm view <包>@<版本> license` 核对过 registry 的 `license` 字段，本机 `npm ls --depth=0` 看到的安装版本与声明一致。许可证栏仍是该 registry 字段，没有另录安装目录中的许可证全文。

| 包 | 声明版本 | registry `license` 字段 | 用途 |
| --- | --- | --- | --- |
| react | 19.3.0 | MIT | 年度预检页面 |
| react-dom | 19.3.0 | MIT | 在浏览器中渲染该页面 |
| vite | 8.3.0 | MIT | 本机开发服务与构建。产品页 [vite.dev](https://vite.dev) |
| @vitejs/plugin-react | 6.1.1 | MIT | Vite 的 React 转换 |
| typescript | 5.9.3 | Apache-2.0 | `tsc --noEmit` 类型检查。产品页 [typescriptlang.org](https://www.typescriptlang.org/) |
| @types/react | 19.3.0 | MIT | React 的 TypeScript 类型 |
| @types/react-dom | 19.3.0 | MIT | ReactDOM 的 TypeScript 类型 |

### 限制

- 只解析可选中文字的文本型 PDF，不处理扫描件 OCR。
- 文字来自 PyMuPDF 文本块。块内各行以换行拼接；较大视觉间隙可能被插入空格；不把行末连字符重新拼成词。
- 文字块坐标在未旋转页面上。页宽和页高来自旋转后的 `page.rect`，旋转 90 或 270 度时两者不能混用。
- 只记录 PDF 页序号，不识别印刷页码。
- 整份文件会读入内存。
- 旧年度预检通过 HTTP 创建和读取。FastAPI 提供 v2 年度分析任务 `POST /v1/annual-analysis-jobs`、任务状态 `GET /v1/annual-analysis-jobs/{job_id}`、只读年度报告 `GET /v1/annual-analyses/{run_id}` 与已核验证据页图预览 `GET /v1/annual-analyses/{run_id}/evidence/{evidence_id}/preview.png`。任务 API 支持 deterministic/M3 筛查；模型调查模式明确不支持。前端可上传文本 PDF、启动确定性或 M3 试行筛查、查看任务状态并在刷新后恢复 job_id；成功后会读取对应报告和证据，也可按 run_id 查看归档。页面不提供在线模型启动或产品级报告导出。M3 的可选调查代码接入 CLI `--with-model`，其 LangGraph 1.2.12 本地模拟集成测试已通过，已完成一次单例在线调用验收（评估 ID：deepseek-live-m3-603288-20260928-a32c16a7）。按文档中的 Uvicorn 命令，服务默认绑定 `127.0.0.1`；应用本身不强制 host。

## 公开样例

已导入海天味业一份真实公开年报，用于文本解析、年度事实和分析验收；另有贵州茅台与五粮液两份真实公开年报作为跨公司事实参考。三份资料均不是人工构造数据，也不在 `data/samples/`。茅台与五粮液案例只整理四项财务披露事实，`evaluation/cases/` 中的值仍为 agent provisional、待人工复核，没有舞弊或异常标签，也未纳入 M4 人工 golden。原始 PDF、解析 JSON 和运行摘要默认不纳入 Git；来源和使用条件见 `docs/data-policy.md`。克隆仓库后需要自行准备所需原文。

| 项目 | 内容 |
| --- | --- |
| 公司 | 佛山市海天调味食品股份有限公司，股票代码 603288 |
| 报告 | 2024 年年度报告，报告期 2024-12-31，披露日期 2025-04-03 |
| 来源 | https://static.cninfo.com.cn/finalpage/2025-04-03/1222994233.PDF |
| 原文件名 | 1222994233.PDF |
| 本地原文 | `data/raw/603288/2024/cninfo-1222994233/1222994233.PDF` |
| 取得时间 | 2026-09-23T10:25:30+08:00 |
| SHA256 | `5a97b13534438f5e85249752ef492fbd9e23af73e67845e47bad1ab7d92e20ee` |
| 大小 | 4,561,223 字节 |
| 使用条件 | 公开披露，仅本地开发使用，未确认再分发许可 |
| 解析 JSON | `data/processed/603288/2024/cninfo-1222994233/text_pdf.json` |
| 处理说明 | `data/processed/603288/2024/cninfo-1222994233/processing.md` |
| 验收摘要 | `artifacts/runs/pdf-parse-603288-2024annual-20260923-102530/summary.json` |

`parse_text_pdf` 使用 PyMuPDF 1.27.2.3。PDF 共 208 页，与预期一致；208 页都有可提取文字，无文字页为 0。解析结果里的 `source_sha256` 与原文件一致，解析没有改动原 PDF。

定位检查均通过：第 81 页有“合并利润表”，第 76–86 页窗口内第 81 与 83 页有“营业收入”；第 85 页有“合并现金流量表”；第 7–9 页有“非经常性损益”。全篇没有 U+FFFD 或 `(cid:)`。第 81 页和第 85 页各有 1 个图片块，未做 OCR。这两页的文字块多数内部含换行，表格没有拆成单元格。

正式事实运行是 `annual-facts-603288-2024-20260923-112307`。已归档的旧预检 `annual-precheck-603288-2024-20260923-160938` 同样得到 8 条事实和 4 组同比，原始 PDF 哈希与 `source_sha256` 一致，且未调用模型。该次 `precheck.json` 是旧结构，只有输入哈希，没有 `code` 和 `verification` 字段，记录保持原样。带代码快照、并把同一行空格与负号、括号、千分位一并计入金额边界的正式运行是 `annual-precheck-603288-2024-20260923-165111`：8 条事实均在引用坐标内重新读到原金额，数值换算复核通过，失败和弃权为 0。`164542`、`163707`、`163126` 和 `160938` 仍保留。`annual-precheck-603288-2024-20260925-105059` 是带 `screening` 的最新正式运行；较早的 `104521` 仍保留。该次利润和收入差额为正、经营现金流差额为负，两条线索为 candidate，引用页为 82 和 86；非经常性损益不对归母净利润做比值。预检状态仍是 completed，`model_called` 与 `independently_verified` 仍为 false。这尚未独立确认年度列、表头口径或完整财报事实。此后新建的预检会写入 `code`：有 Git 时记录 `git_head` 和 `git_dirty`，没有 Git 时把这两项标为空且 `git_available` 为 false，并始终记录关键业务源码的 SHA256。2024 年金额是当年列，2023 年金额是这份 2024 年报的比较列；是否追溯调整尚未确认，`restatement_status` 为 `unknown`。营业收入没有“一、营业收入”主行，改用“其中：营业收入”。非经常性损益合计的口径是披露表格口径。单位为元。较早的 `annual-facts-603288-2024-20260923-111838` 仍保留。

| 指标 | 2024 | 2023 | 差额 | 约同比 |
| --- | --- | --- | --- | --- |
| 营业收入 | 26,900,977,516.70 | 24,559,312,356.59 | 2,341,665,160.11 | 9.53% |
| 归属于母公司股东的净利润 | 6,344,125,969.00 | 5,626,626,091.97 | 717,499,877.03 | 12.75% |
| 经营活动产生的现金流量净额 | 6,843,710,887.07 | 7,355,650,997.74 | -511,940,110.67 | -6.96% |
| 披露的非经常性损益合计 | 274,709,462.33 | 231,962,157.80 | 42,747,304.53 | 18.43% |

以上表格和运行说明属于旧年度预检历史结果。约同比由 Decimal 同比率按当前计算精度换成两位百分比，是近似值。差额和原文金额的完整十进制仍在该运行产物和 `processing.md`；这不是 v2 独立原文核验，也不改变旧运行记录。

### 跨公司 provisional 事实参考

| 公司 | 案例文件与 CLI 归档 | 当前状态 |
| --- | --- | --- |
| 宜宾五粮液（000858） | `evaluation/cases/000858-2024-provisional.json`；`annual-analysis-956db60d-6595-4d7c-8277-07d1d7e21326` | 修复后正式归档；可比性为 `insufficient_evidence`，8 条事实因币种未明确而未确认，8 项计算未确认，2 条筛查均弃权；案例待人工复核 |
| 贵州茅台（600519） | `evaluation/cases/600519-2024-provisional.json`；`annual-analysis-10232970-a22b-498a-9a8b-4bde1b5bb8e6` | 年报事实参考值待人工复核；CLI 归档状态为 `completed_with_issues`，年度可比性不足，未确认计算或候选信号 |

案例仅覆盖四项披露事实的数值、单位、期间列与报表口径，标签被标为仅供评测并从分析材料排除；没有人工审核结果，也不能据此声称跨公司泛化。五粮液 `0.00` 的 Decimal 误判已修复，修复后运行已归档；该运行未确认事实或计算，可比性证据不足，筛查均弃权，不能据此声称形成异常信号或跨公司泛化。

### 旧预检限制

- 只覆盖上述四个指标，不承诺其他报表项目或全部上市公司版式。
- 年度表头、单位、币种或口径缺失、矛盾、主行重复时弃权，不猜数值，也不把空单元格当零。
- 上期规范值为零时不计算同比率。
- 非经常性损益若表题未写合并，只记披露表格口径。
- 同比只比较同一文档哈希、公司、币种、单位倍率、口径和年度期间类型。
- 2023 年金额来自 2024 年报比较列。比较列是否经过追溯调整尚未确认。
- 同比率是当前 Decimal 除法精度下的近似小数；差额不依赖该近似值。
- 原文复核只检查引用坐标里的金额和单位换算，不确认年度列、表头口径或完整财报事实，也不是舞弊结论。

## 后续开发顺序

本项目的软件开发工作聚焦可运行系统、数据、核验和复现说明。初赛计划书 PDF 和项目介绍视频 MP4 不在本开发任务内。`submission/` 仍保留，用于存放比赛材料；比赛对计划书和视频的客观要求不变。本文不指定这些材料的完成人，已有比赛资料保持原样。

1. 文本型 PDF 的逐页文字与坐标已实现，并已对上述公开年报做过定位验收。
2. 旧流程的四项年度事实、确定性同比，以及引用坐标内的原文金额与单位换算复核已实现。M1 海天样例与 M2 单样例确定性 Golden Path 已验收；正式 v2 CLI 已归档 8 条独立核验事实、8 项独立核验计算、16 条确定性 Claim 和 2 条候选信号。可比性 proof 只由当前 PDF 的第 116、163、197 页签发，序列化记录只用于审计，不是与此前已披露的 2023 年报独立勾稽；单一公司样例不代表泛化。
3. M3 调查图、候选线索的有限年报内检索、提示词、审计调用和可选 CLI 路径已编码；LangGraph 1.2.12 本地模拟集成验收通过，已完成一次单例在线调用验收（评估 ID：deepseek-live-m3-603288-20260928-a32c16a7）。
4. v2 年度分析页面已接入任务启动、阶段展示、刷新恢复、失败/中断重试、成功后同 run_id 报告及证据读取，并保留按 run_id 回看归档。2026-09-28 Chrome headless smoke 从页面启动海天样例任务 annual-job-f585b9c64c064ec48b12e038dd6a00e1，状态为 completed、stage 为 completed，页面自动显示 run annual-analysis-b788f884-e66d-4374-a2a7-2b5283ca4b44；随后成功读取 M3 归档 annual-analysis-5e7079fe-9ac9-4e96-85fa-1898f9177d21，显示四条规则，issues 数组解析正常。页面没有产品级导出。下一步完成人审 golden、有/无独立核验的对照评测与可靠性统计。
5. 旧年度预检接口与页面仍单独保留并已验收。启动方式见上文；旧预检上传与创建仍是分步操作。项目不包含用户注册、登录或账户管理。

当前有两条分开的本地流程：旧年度预检 API/页面继续使用旧事实与核验，页面可选展示旧版 screening；v2 确定性 Golden Path 由 `scripts/analyze_annual.py` 执行并已在海天 603288 的 2024 样例正式归档。该次运行有 8 条独立核验事实、8 项独立核验计算、16 条通过确定性检查的 Claim 和 2 条候选信号；M1 海天样例与 M2 单样例 Golden Path 已验收。前端年度分析页可上传 PDF 或运行本机样例，启动 deterministic/M3 任务并查看状态，成功后展示归档报告与已核验证据页图，也保留 run_id 回看入口；页面没有产品级导出。M3 调查代码与可选 CLI 已实现，LangGraph 1.2.12 本地 StateGraph mock 集成验收已通过，已完成一次单例在线调用验收（评估 ID：deepseek-live-m3-603288-20260928-a32c16a7）。独立模块 `finance/m3_screening.py` 的四条试行年度规则已接入 CLI 显式可选路径 `--with-m3-screening`；海天正式运行 `annual-analysis-5e7079fe-9ac9-4e96-85fa-1898f9177d21` 中 10 条新字段事实和 18 条直接独立核验均已归档，四条规则可计算且总分为 0。该路径不改变默认 M2 或旧筛查；阈值仍未校准，结果不表示跨公司泛化。规则二只使用海天 603288 2024 年报第 7 页版式的当前进程专属映射 proof；非样例公司或期间仍弃权。筛查调用约定要求使用同进程 `verify_financial_fact` 直接结果，并拒绝提取与核验 evidence ID 重叠；`VerificationResult` 不是签名 proof，反序列化 JSON 或手工重建对象不能作为核验依据。试行阈值仍待隔离评测验证，不能把异常线索写成舞弊结论。茅台和五粮液案例仍是 provisional 事实参考；五粮液修复后正式运行 `annual-analysis-956db60d-6595-4d7c-8277-07d1d7e21326` 已归档，结果为可比性 `insufficient_evidence`、8 条事实币种未明确而未确认、8 项计算未确认、2 条筛查弃权。M4 的 3–5 家公司人工 golden 与完整多公司消融评测仍未完成；单公司基线运行 `verification-ablation-m4-20260928T072029Z-825a8c4b` 正式归档，指标见前文。可比性 proof 只由海天报告第 116、163、197 页签发，序列化结果只供审计；没有独立勾稽此前已披露的 2023 年报，也不代表跨公司泛化。前端 npm run typecheck 与 npm run build 通过；Chrome headless smoke 另验证了任务启动、完成报告读取及 M3 归档解析。页面没有单独的自动化测试套件。
