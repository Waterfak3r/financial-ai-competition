# 财务异常识别与舞弊风险核验智能体

面向北京市大学生金融人工智能竞赛第 2 题“上市公司财务报告分析”，通过财报解析、大模型分析、独立财务计算与证据核验，形成可追溯的财务异常和舞弊风险分析结果。

**当前阶段：目录骨架已建立。文本型 PDF 逐页解析、四项年度事实提取、确定性同比、Chat Completions 文本连接器、本地年度财务预检、引用坐标内的原文金额复核，以及本机年度预检页面的源码已可查看；公开年报的解析、字段计算和预检已跑通。** 云端实测和智能体分析接口尚未实现。完整核验、舞弊分析、年度列与表头口径的独立确认仍未完成。本机已安装 `frontend/package.json` 声明的依赖并生成 `frontend/package-lock.json`；`npm run typecheck` 与 `npm run build` 已通过。开发服务上已用浏览器验收首页、海天样例创建、回看和错误提示。本地项目无用户注册、登录或账户管理，且不在当前项目范围。模型供应方的 API 密钥仍是另一项独立配置；预检不读取该密钥，浏览器也不持有它。

## 协作入口

开始开发前阅读 [AGENTS.md](AGENTS.md)。项目开发由 gpt-6-astra 持续负责实现策略和方向、拆解、验收与汇报，由 grok-4.7 负责具体实施，通过 Herdr 协作。主控按任务复杂度和精细度决定 reasoning effort，并在派发时明确；派发前核对实际模型为 grok-4.7。较大任务在 Grok 完成后由主控审阅是否符合既定方向及验收要求。

每次协作经主控审阅后，主控使用 gpt-6-luna 子代理把事实性协作记录追加到 [coop.md](coop.md)。该分工用于项目开发协作，后端分析财报所用云端模型仍待选定。细则见 [AGENTS.md](AGENTS.md)。架构与模块职责见 [架构说明](docs/architecture.md)，数据与产物管理见 [数据管理说明](docs/data-policy.md)。比赛原文位于 [比赛通知.txt](比赛通知.txt)。

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
| 当前依赖状态 | `backend/pyproject.toml` 已声明 PyMuPDF、FastAPI 和 Uvicorn。`frontend/package.json` 已声明 React、ReactDOM、Vite、TypeScript 和 React 插件，本机 `npm install` 已生成 `frontend/package-lock.json`。`node_modules` 不纳入 Git。LangGraph 尚未引入 |

第一版路线确定为“云端 API + 本地轻量 Web + 本地统计计算”，不要求本地 GPU，也不纳入模型本地部署或训练。原始财报、索引、计算和运行记录保存在本地，模型调用仅发送本任务所需且允许外发的片段。云端 API 需要网络；受控运行限制资料范围与外部连接，现场是否允许模型联网仍需依据组委会环境说明核实。历史回放与在线重新运行明确区分，断网回放不能替代实时处理验收。

## 目录导航

```text
.
├── AGENTS.md          # AI 协作与文件管理规则
├── README.md          # 项目入口与当前状态
├── coop.md            # 主控审阅后追加的事实性协作记录
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

前端已有年度预检页面、请求客户端、类型和样式。上传、任务进度和报告导出尚未实现。后端按财报解析、检索、智能体、财务计算、核验和报告等职责划分，具体边界见架构文档。

## 文件存放约定

| 文件类型 | 存放位置 |
| --- | --- |
| 原始财报 | data/raw/ |
| 解析文本、表格、标准化数据与索引 | data/processed/ |
| 可交付演示样例 | data/samples/ |
| 任务执行记录和可复现中间结果 | artifacts/runs/<run_id>/ |
| 生成的分析报告 | artifacts/reports/<run_id>/ |
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

连接器不会自动发送整份 PDF，也还没有接到智能体分析接口。测试使用本地模拟 HTTP，不访问上述云端地址。

`audited_complete_chat(messages, settings, run_dir=..., evidence_refs=[{"document_id": "doc-1", "page": 12}], prompt_version="prompt-v1")` 包装上述连接器。`run_dir` 必须是本仓库 `artifacts/runs` 的直接子目录。每次调用新建 UUID 子目录，联网前写入 `request.json`（`status` 为 `started`），成功再写 `response.json`，失败写 `failure.json` 且只含安全类别。记录包含 UTC 起止时间。落盘字符串会去掉 `MODEL_API_KEY`，不写 `base_url`、请求头或异常原文。响应文件写失败会抛出 `AuditPersistError`，不返回成功。年度预检和智能体尚未调用它，云端实测仍未进行。

## 已实现：文本型 PDF 逐页解析

`finagent.ingestion.parse_text_pdf` 读取文本型 PDF，返回逐页文字块。结果包含文档标识、原始文件 SHA256、文件名、PDF 1-based 页序号、块文字和页面坐标，可用 `to_json()` 序列化。函数只在内存中打开文件字节，不修改原 PDF，也不写入 `data/processed/`。

文字块边界框使用未旋转页面坐标：原点在左上角，x 向右、y 向下，单位为 PDF point（1/72 英寸）。PyMuPDF 提取文字时按 0 度旋转计算边界框。结果里的页宽和页高来自当前 `page.rect`；页面旋转 90 或 270 度时，显示宽高与未旋转页面对调，不能拿这组宽高去套文字块坐标。

空白页、只有图片或矢量图形的页、以及提取结果只有空白的页，状态为 `no_extractable_text`，文字块为空。本增量不执行 OCR，也不编造文字。页内同时有文字和图片时，只返回文字块，并注明图片未做 OCR。加密或损坏的 PDF 会报错，不会被当成空白页。

在解析结果之上，还可以提取合并利润表营业收入、归属于母公司股东的净利润、合并现金流量表经营活动产生的现金流量净额，以及非经常性损益表的披露合计，并对同一指标的报告年和上一年做确定性同比。这不是通用财报抽取。字段提取命令本身不复核引用坐标里的原文金额。云端侧目前只有同步 Chat Completions 文本连接器，尚未接到分析流程。本地 FastAPI 年度预检在哈希一致后，从原始 PDF 的引用坐标重新读取金额，并独立复核单位换算：`POST /v1/annual-prechecks` 同步完成，`GET /v1/annual-prechecks/{run_id}` 只返回该次运行。本机页面只展示这次预检。模型分析、检索、报告、LangGraph、上传和报告导出仍未实现。

### 安装、调用与测试

运行依赖在 `backend/pyproject.toml` 中声明：PyMuPDF、FastAPI 和 Uvicorn。pytest 与 httpx 是可选测试依赖。仓库不代为安装。在仓库根目录执行：

```powershell
python -m pip install -e ".\backend[test]"
python -m pytest tests/unit
```

`backend[test]` 带上引号，是因为 PowerShell 会把方括号当成通配符。这个测试额外依赖同时包含 pytest 和 httpx；只安装 pytest 时，`TestClient` 测试无法运行。

未做可编辑安装时，`tests/unit/conftest.py` 会把 `backend/src` 加入导入路径。当前环境已能导入 `pymupdf`、`pytest`、`fastapi` 和 `httpx` 时，可直接在仓库根目录运行 `python -m pytest tests/unit`。

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

Vite 按 `frontend/vite.config.ts` 只绑定 `127.0.0.1:5173`，且 `strictPort` 为 true。浏览器打开 http://127.0.0.1:5173 。页面请求 `/api/v1/annual-prechecks`；Vite 去掉 `/api` 前缀后转发到 http://127.0.0.1:8000 。浏览器不保存模型密钥，项目也没有用户账户。

页面可以一键填入海天 2024 样例的相对路径，也可以手写 `data/processed` 与 `data/raw` 下的相对路径、`company_id` 和 `report_year`，然后创建或按 `run_id` 回看。结果展示事实、同比和引用坐标内的原文金额复核。样例 PDF 与解析 JSON 已在本机对应目录，但被 Git 忽略；克隆仓库后需要先准备这两份文件，页面不会下载它们。

2026-09-23 在本机执行了 `npm run typecheck` 和 `npm run build`，二者通过。随后用已有 Playwright 打开 http://127.0.0.1:5173 。首页可以加载；填入海天 2024 样例并创建，得到 `annual-precheck-603288-2024-20260923-192840`，状态 `completed`，8 条事实、4 组同比，原文金额复核通过 8、未通过 0、弃权 0。按该 `run_id` 回看仍为这次记录。不存在的 `run_id` 显示 HTTP 404 `run_not_found`。空的解析路径会提示填写。这次验收没有覆盖上传、任务进度、报告导出、模型分析或完整核验。重新加载页面后，控制台不再出现 `favicon.ico` 的 404；`/favicon.svg` 返回 200。直接请求 `/favicon.ico` 仍然是 404。浏览器日志在 `artifacts/runs/web-qa-20260923-200437/browser/`。页面没有单独的自动化测试套件。

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
- 本地预检是 HTTP 入口。按文档中的 Uvicorn 命令默认绑定 `127.0.0.1`；应用本身不强制 host。本机页面只展示预检。尚无智能体编排入口。

## 公开样例

已导入一份真实公开年报，用于文本解析、四项年度事实和同比验收。它不是人工构造数据，也不在 `data/samples/`。原始 PDF、解析 JSON 和运行摘要默认不纳入 Git。本机已有样例 PDF 和解析 JSON；克隆仓库后需要先准备这两份文件，预检页面的一键填充不会下载它们。

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

正式事实运行是 `annual-facts-603288-2024-20260923-112307`。已归档的旧预检 `annual-precheck-603288-2024-20260923-160938` 同样得到 8 条事实和 4 组同比，原始 PDF 哈希与 `source_sha256` 一致，且未调用模型。该次 `precheck.json` 是旧结构，只有输入哈希，没有 `code` 和 `verification` 字段，记录保持原样。带代码快照、并把同一行空格与负号、括号、千分位一并计入金额边界的正式运行是 `annual-precheck-603288-2024-20260923-165111`：8 条事实均在引用坐标内重新读到原金额，数值换算复核通过，失败和弃权为 0。`164542`、`163707`、`163126` 和 `160938` 仍保留。这尚未独立确认年度列、表头口径或完整财报事实。此后新建的预检会写入 `code`：有 Git 时记录 `git_head` 和 `git_dirty`，没有 Git 时把这两项标为空且 `git_available` 为 false，并始终记录关键业务源码的 SHA256。2024 年金额是当年列，2023 年金额是这份 2024 年报的比较列；是否追溯调整尚未确认，`restatement_status` 为 `unknown`。营业收入没有“一、营业收入”主行，改用“其中：营业收入”。非经常性损益合计的口径是披露表格口径。单位为元。较早的 `annual-facts-603288-2024-20260923-111838` 仍保留。

| 指标 | 2024 | 2023 | 差额 | 约同比 |
| --- | --- | --- | --- | --- |
| 营业收入 | 26,900,977,516.70 | 24,559,312,356.59 | 2,341,665,160.11 | 9.53% |
| 归属于母公司股东的净利润 | 6,344,125,969.00 | 5,626,626,091.97 | 717,499,877.03 | 12.75% |
| 经营活动产生的现金流量净额 | 6,843,710,887.07 | 7,355,650,997.74 | -511,940,110.67 | -6.96% |
| 披露的非经常性损益合计 | 274,709,462.33 | 231,962,157.80 | 42,747,304.53 | 18.43% |

约同比由 Decimal 同比率按当前计算精度换成两位百分比，是近似值。差额和原文金额的完整十进制仍在该运行产物和 `processing.md`。这不是独立原文核验。

### 当前限制

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
2. 四项年度事实、确定性同比，以及引用坐标内的原文金额与单位换算复核已实现。完整核验、其他指标和完整财务分析仍未实现。
3. 接入模型、任务编排和调用记录，生成带证据的风险分析结果。
4. 本地年度预检接口和本机预检页面已在开发服务上验收。按文档先启动绑定 127.0.0.1:8000 的后端，再在 `frontend/` 安装依赖并执行 `npm run dev`。本机已生成 `frontend/package-lock.json`，类型检查与生产构建已通过，浏览器验收见上文。上传、任务进度和报告导出仍未实现。项目不包含用户注册、登录或账户管理。
5. 建立对照评测，并整理可复现说明。

当前可运行增量是文本型 PDF 解析、四项年度事实提取、确定性同比、Chat Completions 文本连接器、本地年度预检接口、引用坐标内的原文金额复核、本机预检页面，以及 `tests/unit` 中的后端测试。前端类型检查和生产构建已在本机通过，开发服务上的浏览器验收已完成上述预检流程。连接器尚未做云端实测，也未接到智能体接口。LangGraph 仍是后续唯一的智能体编排框架，模型分析、上传、任务进度和报告导出仍未实现。完整核验和舞弊分析仍未实现。页面没有单独的自动化测试套件。
